"""Topology (v1.2): the pure graph builder against a realistic PSE-lab inventory, the manifest
`topology` block, the engine's cache/refresh/reason paths, and the API route. No cloud calls."""

from __future__ import annotations

import copy
import time
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from app.jobs import JobRunner
from app.providers import build_registry
from app.store import Store
from app.usecases.engine import Engine
from app.usecases.manifest import ManifestError, load_manifest, parse_manifest
from app.usecases.topology import build_graph, rule_label
from tests.conftest import GOOD_MANIFEST, TEST_PASSWORD, write_manifest

TOPOLOGY = """\
topology:
  roles:
    zpa-lab-pse: pse
    zpa-lab-connector: connector
    zpa-lab-priv-connector: connector
    zpa-lab-server: app
    zpa-lab-mcu-client: client
  flows:
    - {from: zpa-lab-mcu-client,     to: zpa-lab-pse,    label: "dials :443",         via: [nat, internet]}
    - {from: zpa-lab-priv-connector, to: zpa-lab-pse,    label: "dials :443",         via: [nat, internet]}
    - {from: zpa-lab-connector,      to: zpa-lab-pse,    label: "dials :443 (local)"}
    - {from: zpa-lab-priv-connector, to: zpa-lab-server, label: ":8080 brokered"}
    - {from: zpa-lab-pse,            to: internet,       label: "control plane :443"}
  blocked:
    - {from: zpa-lab-mcu-client, to: zpa-lab-server, label: "no route"}
"""
MANIFEST_WITH_TOPOLOGY = GOOD_MANIFEST + TOPOLOGY
PROVIDERS = {"aws", "gcp", "azure"}

# ---------------------------------------------------------------- the PSE lab as the v1.1 inventory sees it
VPC_A, VPC_B, VPC_DEFAULT = "vpc-0a1111111111111a1", "vpc-0b2222222222222b2", "vpc-0d3333333333333d3"
IGW_A, IGW_B, IGW_DEFAULT = "igw-0a1111111111111a1", "igw-0b2222222222222b2", "igw-0d3333333333333d3"
SN_A_PUB, SN_B_PUB, SN_B_PRIV, SN_B_MCU, SN_DEFAULT = "subnet-0a10", "subnet-0b00", "subnet-0b20", "subnet-0b30", "subnet-0d00"
RT_A_PUB, RT_A_MAIN, RT_B_PUB, RT_B_PRIV, RT_B_MAIN = "rtb-0a10", "rtb-0a00", "rtb-0b10", "rtb-0b20", "rtb-0b00"
NAT_B = "nat-0b2222222222222b2"
I_PSE, I_CONN, I_PRIV, I_SRV, I_MCU, I_OTHER = "i-0pse", "i-0conn", "i-0priv", "i-0srv", "i-0mcu", "i-0other"
SG_PSE, SG_CONN, SG_PRIV, SG_SRV, SG_MCU, SG_DEFAULT = "sg-0pse", "sg-0conn", "sg-0priv", "sg-0srv", "sg-0mcu", "sg-0default"
EIP_PSE, EIP_NAT, EIP_IDLE = "eipalloc-0pse", "eipalloc-0nat", "eipalloc-0idle"
NAT_IP = "18.196.44.9"
LAB = {"Project": "zpa-pse-lab", "ManagedBy": "opentofu"}


def _t(name: str, **extra: str) -> dict[str, str]:
    return {**LAB, "Name": name, **extra}


def _sn(sid: str, name: str, cidr: str, rt: str, target: str | None, tags: dict | None = None) -> dict:
    return {"id": sid, "name": name, "cidr": cidr, "az": "eu-central-1a", "public": bool(target and target.startswith("igw-")),
            "route_table": rt, "default_route": target, "map_public_ip": False, "available_ips": 250, "tags": tags if tags is not None else _t(name)}


def _rt(rid: str, name: str | None, main: bool, target: str | None, subnets: list[str], vpc_cidr: str) -> dict:
    routes = [{"dest": vpc_cidr, "target": "local", "state": "active"}]
    if target:
        routes.append({"dest": "0.0.0.0/0", "target": target, "state": "active"})
    return {"id": rid, "name": name, "main": main, "routes": routes, "subnets": subnets, "tags": _t(name) if name else {}}


def _inst(iid: str, name: str | None, itype: str, vpc: str, subnet: str, priv: str, pub: str | None, sg: str, tags: dict | None = None) -> dict:
    return {"id": iid, "name": name, "type": itype, "state": "running", "private_ip": priv, "public_ip": pub, "launched": "2026-09-01T08:00:00+00:00",
            "uptime_h": 100.0, "platform": "Linux/UNIX", "architecture": "x86_64", "az": "eu-central-1a", "vpc": vpc, "subnet": subnet,
            "ami": "ami-0", "ami_name": None, "iam_instance_profile": "zpa-lab-node", "key_name": None,
            "security_groups": [{"id": sg, "name": sg}], "root_device": "/dev/xvda", "monitoring": False, "ebs_optimized": True,
            "volumes": [f"vol-{iid}"], "user_data_present": True, "monthly_usd": 10.0, "tags": tags if tags is not None else _t(name or iid)}


def _sg(sid: str, name: str, vpc: str, ingress: list[dict], attached: list[str], tags: dict | None = None) -> dict:
    return {"id": sid, "name": name, "vpc": vpc, "description": name, "ingress": ingress,
            "egress": [{"proto": "all", "from": None, "to": None, "source": "0.0.0.0/0"}], "attached_to": attached, "tags": tags if tags is not None else _t(name)}


def _eip(alloc: str, ip: str, assoc: dict | None, tags: dict) -> dict:
    return {"ip": ip, "allocation_id": alloc, "attached": assoc is not None, "instance": assoc["id"] if assoc and assoc["kind"] == "instance" else None,
            "association": assoc, "private_ip": None, "name": tags.get("Name"), "monthly_usd": 3.65, "tags": tags}


def pse_inventory() -> dict:
    eu = {
        "region": "eu-central-1",
        "instances": [
            _inst(I_PSE, "zpa-lab-pse", "m5.large", VPC_A, SN_A_PUB, "10.91.10.5", "63.188.16.52", SG_PSE),
            _inst(I_CONN, "zpa-lab-connector", "t3.medium", VPC_A, SN_A_PUB, "10.91.10.20", "3.120.5.6", SG_CONN),
            _inst(I_PRIV, "zpa-lab-priv-connector", "t3.medium", VPC_B, SN_B_PRIV, "10.90.20.10", None, SG_PRIV),
            _inst(I_SRV, "zpa-lab-server", "t3.micro", VPC_B, SN_B_PRIV, "10.90.20.20", None, SG_SRV),
            _inst(I_MCU, "zpa-lab-mcu-client", "t3.medium", VPC_B, SN_B_MCU, "10.90.30.10", None, SG_MCU),
            _inst(I_OTHER, "someone-elses-box", "t3.nano", VPC_DEFAULT, SN_DEFAULT, "172.31.0.9", "3.1.1.1", SG_DEFAULT, tags={"Name": "someone-elses-box"}),
        ],
        "vpcs": [
            {"id": VPC_A, "name": "zpa-lab-vpc-a", "cidr": "10.91.0.0/16", "default": False, "state": "available", "dns_hostnames": True, "igw": IGW_A,
             "subnets": [_sn(SN_A_PUB, "zpa-lab-public", "10.91.10.0/24", RT_A_PUB, IGW_A)], "nat_gateways": [],
             "route_tables": [_rt(RT_A_MAIN, None, True, None, [], "10.91.0.0/16"), _rt(RT_A_PUB, "zpa-lab-public-rt", False, IGW_A, [SN_A_PUB], "10.91.0.0/16")],
             "tags": _t("zpa-lab-vpc-a")},
            {"id": VPC_B, "name": "zpa-lab-vpc-b", "cidr": "10.90.0.0/16", "default": False, "state": "available", "dns_hostnames": True, "igw": IGW_B,
             "subnets": [
                 _sn(SN_B_PUB, "zpa-lab-b-public", "10.90.0.0/24", RT_B_PUB, IGW_B),
                 _sn(SN_B_PRIV, "zpa-lab-priv", "10.90.20.0/24", RT_B_PRIV, NAT_B),
                 _sn(SN_B_MCU, "zpa-lab-mcu", "10.90.30.0/24", RT_B_PRIV, NAT_B),
             ],
             "nat_gateways": [NAT_B],
             "route_tables": [
                 _rt(RT_B_MAIN, None, True, None, [], "10.90.0.0/16"),
                 _rt(RT_B_PUB, "zpa-lab-b-public-rt", False, IGW_B, [SN_B_PUB], "10.90.0.0/16"),
                 _rt(RT_B_PRIV, "zpa-lab-b-private-rt", False, NAT_B, [SN_B_PRIV, SN_B_MCU], "10.90.0.0/16"),
             ],
             "tags": _t("zpa-lab-vpc-b")},
            {"id": VPC_DEFAULT, "name": None, "cidr": "172.31.0.0/16", "default": True, "state": "available", "dns_hostnames": True, "igw": IGW_DEFAULT,
             "subnets": [_sn(SN_DEFAULT, None, "172.31.0.0/20", "rtb-0d00", IGW_DEFAULT, tags={})], "nat_gateways": [],
             "route_tables": [_rt("rtb-0d00", None, True, IGW_DEFAULT, [], "172.31.0.0/16")], "tags": {}},
        ],
        "nat_gateways": [
            {"id": NAT_B, "vpc": VPC_B, "subnet": SN_B_PUB, "state": "available", "public_ip": NAT_IP, "private_ip": "10.90.0.12",
             "connectivity_type": "public", "created": "2026-09-01T07:55:00+00:00", "name": "zpa-lab-nat", "monthly_usd": 35.04, "tags": _t("zpa-lab-nat")},
        ],
        "eips": [
            _eip(EIP_PSE, "63.188.16.52", {"kind": "instance", "id": I_PSE, "eni": "eni-1"}, _t("zpa-lab-pse-eip")),
            _eip(EIP_NAT, NAT_IP, {"kind": "nat", "id": NAT_B, "eni": "eni-2"}, _t("zpa-lab-nat-eip")),
            _eip(EIP_IDLE, "3.70.113.155", None, _t("zpa-lab-spare")),
            _eip("eipalloc-0foreign", "3.1.1.1", {"kind": "instance", "id": I_OTHER, "eni": "eni-3"}, {}),
        ],
        "volumes": [],
        "security_groups": [
            _sg(SG_PSE, "zpa-lab-pse", VPC_A, [
                {"proto": "tcp", "from": 443, "to": 443, "source": "10.91.0.0/16"},
                {"proto": "tcp", "from": 443, "to": 443, "source": f"{NAT_IP}/32"},
            ], [I_PSE]),
            _sg(SG_CONN, "zpa-lab-connector", VPC_A, [], [I_CONN]),
            _sg(SG_PRIV, "zpa-lab-priv-connector", VPC_B, [], [I_PRIV]),
            _sg(SG_SRV, "zpa-lab-server", VPC_B, [{"proto": "tcp", "from": 8080, "to": 8080, "source": SG_PRIV}], [I_SRV]),
            _sg(SG_MCU, "zpa-lab-mcu-client", VPC_B, [], [I_MCU]),
            _sg(SG_DEFAULT, "default", VPC_DEFAULT, [{"proto": "all", "from": None, "to": None, "source": SG_DEFAULT}], [I_OTHER], tags={}),
        ],
        "monthly_usd": 284.89, "resource_count": 20, "error": None,
    }
    quiet = {"region": "us-east-1", "instances": [], "vpcs": [], "nat_gateways": [], "eips": [], "volumes": [], "security_groups": [],
             "monthly_usd": 0.0, "resource_count": 0, "error": None}
    return {"supported": True, "generated_at": "2026-09-05T12:00:00+00:00", "stale": False, "regions": [eu, quiet],
            "totals": {}, "groups": [], "cost": {"monthly_usd": 284.89, "lines": []}}


def pse_status() -> dict:
    """What scripts/status.py --json prints in the lab repo."""
    return {
        "healthy": True, "summary": "3/3 components authenticated, 5/5 instances running", "region": "eu-central-1",
        "components": [
            {"id": "pse", "label": "Private Service Edge", "group": "AWS-Lab PSE Group", "authenticated": True, "control_channel": "ZPN_STATUS_AUTHENTICATED",
             "version": "25.62.1", "private_ip": "10.91.10.5", "public_ip": "63.188.16.52", "enrolled_as": "ip-10-91-10-5"},
            {"id": "connector_vpc_a", "label": "App Connector (VPC A)", "authenticated": True, "control_channel": "ZPN_STATUS_AUTHENTICATED",
             "version": "25.62.1", "private_ip": "10.91.10.20", "public_ip": None, "enrolled_as": "ip-10-91-10-20"},
            {"id": "connector_priv", "label": "App Connector (PRIV)", "authenticated": False, "control_channel": "ZPN_STATUS_DISCONNECTED",
             "version": None, "private_ip": None, "public_ip": None, "enrolled_as": None},
        ],
        "instances": [],
    }


def _manifest(text: str = MANIFEST_WITH_TOPOLOGY):
    return parse_manifest(yaml.safe_load(text), expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)


@pytest.fixture
def graph() -> dict:
    return build_graph(_manifest(), pse_inventory(), pse_status())


def _nodes(graph: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"] if n["kind"] == kind}


def _edges(graph: dict, kind: str) -> list[dict]:
    return [e for e in graph["edges"] if e["kind"] == kind]


# ---------------------------------------------------------------- nodes
def test_node_counts_by_kind_and_envelope(graph: dict) -> None:
    assert graph["counts"] == {"internet": 1, "vpc": 2, "subnet": 4, "instance": 5, "nat": 1, "igw": 2, "eip": 3}
    assert len(graph["nodes"]) == 18
    assert graph["usecase"] == "zpa-private-service-edge" and graph["provider"] == "aws"
    assert graph["region"] == "eu-central-1" and graph["regions"] == ["eu-central-1"]
    assert graph["nodes"][0] == {"id": "internet", "kind": "internet", "label": "Internet", "parent": None}
    assert graph["declared"]["roles"]["zpa-lab-pse"] == "pse" and len(graph["declared"]["flows"]) == 5
    # Nothing from the default VPC or the other region leaked in.
    ids = {n["id"] for n in graph["nodes"]}
    assert not ids & {VPC_DEFAULT, IGW_DEFAULT, SN_DEFAULT, I_OTHER, "eipalloc-0foreign"}
    for node in graph["nodes"]:
        assert set(node) >= {"id", "kind", "label", "parent"}
        assert node["kind"] in ("internet", "vpc", "subnet", "instance", "nat", "igw", "eip")


def test_parent_chains(graph: dict) -> None:
    vpcs, subnets, insts = _nodes(graph, "vpc"), _nodes(graph, "subnet"), _nodes(graph, "instance")
    assert vpcs[VPC_A]["parent"] is None and vpcs[VPC_A]["cidr"] == "10.91.0.0/16" and vpcs[VPC_A]["label"] == "zpa-lab-vpc-a"
    assert subnets[SN_A_PUB]["parent"] == VPC_A
    assert {s: subnets[s]["parent"] for s in (SN_B_PUB, SN_B_PRIV, SN_B_MCU)} == {SN_B_PUB: VPC_B, SN_B_PRIV: VPC_B, SN_B_MCU: VPC_B}
    assert insts[I_PSE]["parent"] == SN_A_PUB and insts[I_CONN]["parent"] == SN_A_PUB
    assert insts[I_PRIV]["parent"] == SN_B_PRIV and insts[I_SRV]["parent"] == SN_B_PRIV and insts[I_MCU]["parent"] == SN_B_MCU
    nat = _nodes(graph, "nat")[NAT_B]
    assert nat["parent"] == VPC_B and nat["subnet"] == SN_B_PUB and nat["public_ip"] == NAT_IP
    igws = _nodes(graph, "igw")
    assert igws[IGW_A]["parent"] == VPC_A and igws[IGW_B]["parent"] == VPC_B and igws[IGW_A]["label"] == "IGW"
    # `detail` carries the raw inventory record, minus the nested lists the graph already expresses.
    assert vpcs[VPC_A]["detail"]["dns_hostnames"] is True and "subnets" not in vpcs[VPC_A]["detail"]
    assert insts[I_PSE]["detail"]["iam_instance_profile"] == "zpa-lab-node"


def test_subnet_exposure_from_default_route(graph: dict) -> None:
    subnets = _nodes(graph, "subnet")
    assert subnets[SN_A_PUB]["exposure"] == "public" and subnets[SN_A_PUB]["default_route"] == IGW_A
    assert subnets[SN_B_PUB]["exposure"] == "public"
    assert subnets[SN_B_PRIV]["exposure"] == "private" and subnets[SN_B_PRIV]["default_route"] == NAT_B
    assert subnets[SN_B_MCU]["exposure"] == "private"
    assert subnets[SN_B_PRIV]["cidr"] == "10.90.20.0/24" and subnets[SN_B_PRIV]["az"] == "eu-central-1a"


def test_isolated_subnet_and_older_inventory_without_default_route() -> None:
    inv = pse_inventory()
    vpc_b = next(v for v in inv["regions"][0]["vpcs"] if v["id"] == VPC_B)
    mcu = next(s for s in vpc_b["subnets"] if s["id"] == SN_B_MCU)
    del mcu["default_route"]  # v1.0-shaped subnet: derive from the route table
    mcu["route_table"] = RT_B_MAIN  # main table has no default route
    g = build_graph(_manifest(), inv, None)
    assert _nodes(g, "subnet")[SN_B_MCU]["exposure"] == "isolated"
    assert not any(e["from"] == SN_B_MCU for e in _edges(g, "route"))
    priv = next(s for s in vpc_b["subnets"] if s["id"] == SN_B_PRIV)
    del priv["default_route"]
    g = build_graph(_manifest(), inv, None)
    assert _nodes(g, "subnet")[SN_B_PRIV]["exposure"] == "private"


def test_instance_roles_and_facts(graph: dict) -> None:
    insts = _nodes(graph, "instance")
    assert {i: insts[i]["role"] for i in insts} == {I_PSE: "pse", I_CONN: "connector", I_PRIV: "connector", I_SRV: "app", I_MCU: "client"}
    pse = insts[I_PSE]
    assert pse["label"] == "zpa-lab-pse" and pse["type"] == "m5.large" and pse["state"] == "running"
    assert pse["private_ip"] == "10.91.10.5" and pse["public_ip"] == "63.188.16.52" and pse["az"] == "eu-central-1a"
    g = build_graph(_manifest(GOOD_MANIFEST), pse_inventory(), None)
    assert all(n["role"] is None for n in _nodes(g, "instance").values())


def test_eips_attached_and_idle(graph: dict) -> None:
    eips = _nodes(graph, "eip")
    assert eips[EIP_PSE]["attached_to"] == I_PSE and eips[EIP_PSE]["label"] == "63.188.16.52"
    assert eips[EIP_NAT]["attached_to"] == NAT_B
    assert eips[EIP_IDLE]["attached_to"] is None and eips[EIP_IDLE]["attached"] is False and eips[EIP_IDLE]["parent"] is None


# ---------------------------------------------------------------- structural edges
def test_route_and_uplink_edges(graph: dict) -> None:
    routes = {(e["from"], e["to"]) for e in _edges(graph, "route")}
    assert routes == {(SN_A_PUB, IGW_A), (SN_B_PUB, IGW_B), (SN_B_PRIV, NAT_B), (SN_B_MCU, NAT_B)}
    assert all(e["label"] == "0.0.0.0/0" for e in _edges(graph, "route"))
    uplinks = {(e["from"], e["to"]) for e in _edges(graph, "uplink")}
    assert uplinks == {(NAT_B, IGW_B), (IGW_A, "internet"), (IGW_B, "internet")}


def test_allow_edges_from_security_rules(graph: dict) -> None:
    allows = _edges(graph, "allow")
    to_pse = sorted((e["from"], e["label"]) for e in allows if e["to"] == I_PSE)
    assert to_pse == [("10.91.0.0/16", "tcp/443"), (f"{NAT_IP}/32", "tcp/443")]
    assert all(e["group"] == {"id": SG_PSE, "name": "zpa-lab-pse"} for e in allows if e["to"] == I_PSE)
    to_srv = [e for e in allows if e["to"] == I_SRV]
    assert len(to_srv) == 1 and to_srv[0]["from"] == SG_PRIV and to_srv[0]["label"] == "tcp/8080" and to_srv[0]["source_nodes"] == [I_PRIV]
    assert not any(e["to"] in (I_CONN, I_PRIV, I_MCU) for e in allows)
    assert not any(e["to"] == I_OTHER for e in allows)


@pytest.mark.parametrize(
    ("rule", "label"),
    [
        ({"proto": "tcp", "from": 443, "to": 443}, "tcp/443"),
        ({"proto": "udp", "from": 1024, "to": 2048}, "udp/1024-2048"),
        ({"proto": "all", "from": None, "to": None}, "all"),
        ({"proto": "icmp", "from": -1, "to": -1}, "icmp"),
        ({"proto": "tcp", "from": None, "to": None}, "tcp"),
    ],
)
def test_rule_label(rule: dict, label: str) -> None:
    assert rule_label(rule) == label


# ---------------------------------------------------------------- declared flows
def test_all_five_flows_resolve_with_via_expanded(graph: dict) -> None:
    flows = _edges(graph, "flow")
    assert len(flows) == 5 and all(f["declared"] is True for f in flows)
    by = {(f["from"], f["to"]): f for f in flows}
    assert by[(I_MCU, I_PSE)]["via"] == [NAT_B, "internet"] and by[(I_MCU, I_PSE)]["label"] == "dials :443"
    assert by[(I_PRIV, I_PSE)]["via"] == [NAT_B, "internet"]
    assert by[(I_CONN, I_PSE)]["via"] == [] and by[(I_CONN, I_PSE)]["label"] == "dials :443 (local)"
    assert by[(I_PRIV, I_SRV)]["via"] == [] and by[(I_PRIV, I_SRV)]["label"] == ":8080 brokered"
    assert by[(I_PSE, "internet")]["label"] == "control plane :443"
    assert not any("via_missing" in f for f in flows)


def test_blocked_pair(graph: dict) -> None:
    blocked = _edges(graph, "blocked")
    assert blocked == [{"kind": "blocked", "from": I_MCU, "to": I_SRV, "via": [], "label": "no route", "declared": True}]


def test_unresolvable_flow_goes_to_unknown_with_reason() -> None:
    text = MANIFEST_WITH_TOPOLOGY + "    - {from: zpa-lab-ghost, to: zpa-lab-pse, label: haunt}\n"
    data = yaml.safe_load(text)
    data["topology"]["flows"].append({"from": "zpa-lab-ghost", "to": "zpa-lab-pse", "label": "haunt"})
    data["topology"]["blocked"].append({"from": "zpa-lab-mcu-client", "to": "zpa-lab-nowhere"})
    m = parse_manifest(data, expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)
    g = build_graph(m, pse_inventory(), None)
    assert len(_edges(g, "flow")) == 5 and len(_edges(g, "blocked")) == 1
    unknown = {(u["kind"], u["from"], u["to"]): u for u in g["unknown"] if u["kind"] in ("flow", "blocked")}
    assert "'zpa-lab-ghost'" in unknown[("flow", "zpa-lab-ghost", "zpa-lab-pse")]["reason"]
    assert "'zpa-lab-nowhere'" in unknown[("blocked", "zpa-lab-mcu-client", "zpa-lab-nowhere")]["reason"]


def test_via_nat_without_a_nat_in_the_source_vpc_is_reported_not_fatal() -> None:
    inv = pse_inventory()
    inv["regions"][0]["nat_gateways"] = []
    for v in inv["regions"][0]["vpcs"]:
        v["nat_gateways"] = []
    g = build_graph(_manifest(), inv, None)
    mcu = next(f for f in _edges(g, "flow") if f["from"] == I_MCU)
    assert mcu["via"] == ["internet"] and mcu["via_missing"] == ["nat"]
    assert _nodes(g, "subnet")[SN_B_PRIV]["exposure"] == "private"  # the route table still says nat-…
    assert not any(e["to"] == NAT_B for e in g["edges"])


# ---------------------------------------------------------------- enrolment
def test_enrolment_mapped_by_private_ip_then_role(graph: dict) -> None:
    enr = graph["enrolment"]
    assert enr[I_PSE] == {"authenticated": True, "label": "Private Service Edge", "component": "pse", "status": "ZPN_STATUS_AUTHENTICATED",
                          "version": "25.62.1", "matched_by": "private_ip"}
    assert enr[I_CONN]["authenticated"] is True and enr[I_CONN]["matched_by"] == "private_ip"
    # connector_priv has no private_ip yet (not enrolled); the only unclaimed `connector` is the PRIV one.
    assert enr[I_PRIV]["authenticated"] is False and enr[I_PRIV]["matched_by"] == "role" and enr[I_PRIV]["status"] == "ZPN_STATUS_DISCONNECTED"
    assert I_SRV not in enr and I_MCU not in enr
    assert not [u for u in graph["unknown"] if u["kind"] == "component"]


def test_enrolment_ambiguous_or_unmatched_component_lands_in_unknown() -> None:
    status = pse_status()
    for c in status["components"]:
        c["private_ip"] = None  # nothing enrolled: two `connector` instances, two connector components
    status["components"].append({"id": "mystery", "label": "Mystery box", "authenticated": True, "private_ip": "10.0.0.1"})
    g = build_graph(_manifest(), pse_inventory(), status)
    assert set(g["enrolment"]) == {I_PSE}
    unknown = {u["id"]: u for u in g["unknown"] if u["kind"] == "component"}
    assert "ambiguous" in unknown["connector_vpc_a"]["reason"] and "ambiguous" in unknown["connector_priv"]["reason"]
    assert "no instance" in unknown["mystery"]["reason"] and unknown["mystery"]["authenticated"] is True


def test_enrolment_tolerates_mapping_shaped_status_and_none() -> None:
    mapping = {"pse": {"status": "ZPN_STATUS_AUTHENTICATED"}, "client": {"enrolled": False}, "checked_at": "x"}
    g = build_graph(_manifest(), pse_inventory(), mapping)
    assert g["enrolment"][I_PSE]["authenticated"] is True and g["enrolment"][I_PSE]["matched_by"] == "role"
    assert g["enrolment"][I_MCU]["authenticated"] is False
    assert build_graph(_manifest(), pse_inventory(), None)["enrolment"] == {}
    assert build_graph(_manifest(), pse_inventory(), "garbage")["enrolment"] == {}


# ---------------------------------------------------------------- degenerate inventories
def test_empty_or_untagged_inventory_yields_no_nodes() -> None:
    m = _manifest()
    for inv in (None, {}, {"regions": []}, {"supported": False}):
        g = build_graph(m, inv, pse_status())
        assert g["nodes"] == [] and g["edges"] == [] and g["enrolment"] == {} and g["region"] is None
    inv = pse_inventory()
    for r in inv["regions"]:
        for coll in ("instances", "vpcs", "nat_gateways", "eips", "security_groups"):
            for item in r[coll]:
                item["tags"] = {}
    assert build_graph(m, inv, None)["nodes"] == []
    data = yaml.safe_load(MANIFEST_WITH_TOPOLOGY)
    data.pop("tags")
    assert build_graph(parse_manifest(data, expected_id="zpa-private-service-edge", provider_ids=PROVIDERS), pse_inventory(), None)["nodes"] == []


def test_tagged_instance_in_untagged_vpc_pulls_in_context_nodes() -> None:
    inv = pse_inventory()
    other = next(i for i in inv["regions"][0]["instances"] if i["id"] == I_OTHER)
    other["tags"] = _t("stray")
    g = build_graph(_manifest(), inv, None)
    vpcs, subnets = _nodes(g, "vpc"), _nodes(g, "subnet")
    assert vpcs[VPC_DEFAULT]["tagged"] is False and vpcs[VPC_A]["tagged"] is True
    assert subnets[SN_DEFAULT]["parent"] == VPC_DEFAULT and _nodes(g, "instance")[I_OTHER]["parent"] == SN_DEFAULT
    assert "eipalloc-0foreign" in _nodes(g, "eip") and _nodes(g, "eip")["eipalloc-0foreign"]["tagged"] is False
    assert g["counts"]["vpc"] == 3 and g["counts"]["subnet"] == 5


def test_tagged_instance_without_a_placeable_parent_goes_to_unknown() -> None:
    inv = pse_inventory()
    stray = copy.deepcopy(next(i for i in inv["regions"][0]["instances"] if i["id"] == I_PSE))
    stray.update(id="i-0stray", name="zpa-lab-stray", subnet="subnet-gone", vpc="vpc-gone", private_ip="10.9.9.9", public_ip=None)
    inv["regions"][0]["instances"].append(stray)
    g = build_graph(_manifest(), inv, None)
    assert "i-0stray" not in _nodes(g, "instance")
    assert [u for u in g["unknown"] if u["kind"] == "instance"] == [
        {"kind": "instance", "id": "i-0stray", "label": "zpa-lab-stray", "region": "eu-central-1", "reason": "Tagged instance is not in any subnet or VPC the inventory describes"}
    ]


def test_graph_is_deterministic_and_json_clean(graph: dict) -> None:
    import json

    again = build_graph(_manifest(), pse_inventory(), pse_status())
    assert json.dumps(graph, sort_keys=True) == json.dumps(again, sort_keys=True)
    assert [n["id"] for n in graph["nodes"]][:4] == ["internet", VPC_B, IGW_B, VPC_A]  # VPCs by CIDR (10.90 < 10.91), each IGW right after its VPC


# ---------------------------------------------------------------- manifest validation
def test_topology_block_parses_and_is_optional() -> None:
    m = _manifest()
    assert m.topology.roles == {"zpa-lab-pse": "pse", "zpa-lab-connector": "connector", "zpa-lab-priv-connector": "connector", "zpa-lab-server": "app", "zpa-lab-mcu-client": "client"}
    assert [f.via for f in m.topology.flows] == [("nat", "internet"), ("nat", "internet"), (), (), ()]
    assert m.topology.flows[4].to == "internet" and m.topology.blocked[0].label == "no route"
    assert m.topology.to_api()["flows"][0] == {"from": "zpa-lab-mcu-client", "to": "zpa-lab-pse", "label": "dials :443", "via": ["nat", "internet"]}
    plain = _manifest(GOOD_MANIFEST)
    assert plain.topology.roles == {} and plain.topology.flows == () and plain.topology.blocked == ()
    assert plain.topology.to_api() == {"roles": {}, "flows": [], "blocked": []}


def test_shipped_manifest_loads_with_topology() -> None:
    shipped = Path(__file__).resolve().parent.parent / "usecases" / "zpa-private-service-edge" / "usecase.yaml"
    m = load_manifest(shipped, PROVIDERS)
    assert len(m.topology.flows) == 5 and len(m.topology.blocked) == 1 and len(m.topology.roles) == 5


@pytest.mark.parametrize(
    ("topology", "needle"),
    [
        (["x"], "'topology' must be a mapping"),
        ({"edges": []}, "unknown field(s): edges"),
        ({"roles": ["pse"]}, "'roles' must be a mapping"),
        ({"roles": {"zpa-lab-pse": 1}}, "'roles' entries must be non-empty strings"),
        ({"roles": {"": "pse"}}, "'roles' entries must be non-empty strings"),
        ({"flows": {"from": "a"}}, "'flows' must be a list"),
        ({"flows": ["a->b"]}, "flows[1]: each entry must be a mapping"),
        ({"flows": [{"from": "a"}]}, "flows[1]: missing required field 'to'"),
        ({"flows": [{"from": "a", "to": "b", "colour": "amber"}]}, "flows[1]: unknown field(s): colour"),
        ({"flows": [{"from": "a", "to": "b", "label": ""}]}, "'label' must be a non-empty string"),
        ({"flows": [{"from": "a", "to": "b", "via": "nat"}]}, "'via' must be a list"),
        ({"flows": [{"from": "a", "to": "b", "via": ["nat", "vpn"]}]}, "'via' has unknown hop(s): vpn"),
        ({"blocked": [{"from": "a", "to": "", "label": "x"}]}, "blocked[1]: 'to' must not be empty"),
    ],
)
def test_topology_validation(topology: object, needle: str) -> None:
    data = yaml.safe_load(GOOD_MANIFEST)
    data["topology"] = topology
    with pytest.raises(ManifestError) as info:
        parse_manifest(data, expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)
    assert needle in str(info.value)


# ---------------------------------------------------------------- engine
class _TopoEngine(Engine):
    """tofu replaced by canned `state list` output; `prepare` is a no-op."""

    def __init__(self, *args, state_output: str = "aws_instance.pse\n", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.state_output = state_output
        self.calls: list[list[str]] = []

    def prepare(self, manifest, env, log_line=None):  # type: ignore[override]
        self._initialised.add(manifest.id)
        return "deadbeef"

    def _run(self, args, *, cwd, env, timeout, log_line=None):  # type: ignore[override]
        self.calls.append(args)
        return 0, self.state_output if "state" in args else ""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "data")
    store.save_provider(
        "aws",
        {
            "status": "connected",
            "identity": {"account": "257300000000", "arn": "arn:aws:sts::257300000000:assumed-role/x/y", "alias": None},
            "regions": ["eu-central-1", "us-east-1"],
            "credentials": store.encrypt({"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "s3cr3t-value", "session_token": None}),
            "connected_at": "2026-09-05T10:00:00+00:00",
        },
    )
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_TOPOLOGY)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", PROVIDERS)
    providers = build_registry(store.pricing_cache_path)
    scans: list[tuple[dict, list[str]]] = []

    def fake_inventory(credentials: dict, regions: list[str]) -> dict:
        scans.append((credentials, regions))
        inv = pse_inventory()
        inv["generated_at"] = f"2026-09-05T13:00:0{len(scans)}+00:00"
        return inv

    monkeypatch.setattr(providers["aws"], "inventory", fake_inventory)
    return store, providers, manifest, tmp_path / "usecases", scans


def test_engine_topology_from_cached_inventory_and_status(env) -> None:
    store, providers, manifest, root, scans = env
    store.save_inventory("aws", pse_inventory())
    store.save_status(manifest.id, {"generated_at": "2026-09-05T12:30:00+00:00", "output": pse_status(), "error": None})
    engine = _TopoEngine(store, providers, JobRunner(store), root)
    out = engine.topology(manifest)
    assert out["reason"] is None and out["state"] == "on" and out["generated_at"]
    assert out["inventory_at"] == "2026-09-05T12:00:00+00:00" and out["status_at"] == "2026-09-05T12:30:00+00:00"
    assert out["counts"]["instance"] == 5 and len(out["edges"]) == 5 + 1 + 4 + 3 + 3
    assert out["enrolment"][I_PSE]["authenticated"] is True
    assert scans == []  # served from the cache on disk; no scan
    assert "_at" not in out


def test_engine_topology_scans_when_no_inventory_and_on_refresh(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, providers, manifest, root, scans = env
    engine = _TopoEngine(store, providers, JobRunner(store), root)
    first = engine.topology(manifest)
    assert first["reason"] is None and len(scans) == 1 and scans[0][1] == ["eu-central-1", "us-east-1"]
    assert scans[0][0]["access_key_id"] == "AKIAIOSFODNN7EXAMPLE"
    assert store.get_inventory("aws")["generated_at"] == first["inventory_at"] == "2026-09-05T13:00:01+00:00"
    # Cached for 60 s: no scan, same answer.
    assert engine.topology(manifest) == first and len(scans) == 1
    # refresh=1 rebuilds the inventory first and bypasses the cache.
    second = engine.topology(manifest, refresh=True)
    assert len(scans) == 2 and second["inventory_at"] == "2026-09-05T13:00:02+00:00"
    assert engine.topology(manifest)["inventory_at"] == second["inventory_at"]
    # TTL expiry re-reads the (still cached on disk) inventory without a scan.
    import app.usecases.engine as eng_mod

    monkeypatch.setattr(eng_mod, "TOPOLOGY_CACHE_TTL_S", 0)
    time.sleep(0.01)
    assert engine.topology(manifest)["inventory_at"] == second["inventory_at"] and len(scans) == 2
    monkeypatch.setattr(eng_mod, "TOPOLOGY_CACHE_TTL_S", 60)
    engine.invalidate(manifest.id)
    assert engine.topology(manifest)["inventory_at"] == second["inventory_at"] and len(scans) == 2


def test_engine_topology_when_off_is_the_planned_register_not_the_inventory(env) -> None:
    """v1.4: off draws what ON deploys from a plan (see test_plan_graph); the inventory is not consulted."""
    from tests.plan_fixture import pse_plan_stream, pse_show

    store, providers, manifest, root, scans = env
    store.save_inventory("aws", pse_inventory())

    class _OffEngine(_TopoEngine):
        def _run(self, args, *, cwd, env, timeout, log_line=None):  # type: ignore[override]
            self.calls.append(args)
            if "plan" in args:
                return 0, pse_plan_stream()
            if "show" in args:
                import json

                return 0, json.dumps(pse_show())
            return 0, ""

    engine = _OffEngine(store, providers, JobRunner(store), root, state_output="")
    out = engine.topology(manifest)
    assert out["state"] == "off" and out["register"] == "planned" and out["reason"] is None
    assert out["counts"] == {"internet": 1, "vpc": 2, "subnet": 4, "instance": 5, "nat": 1, "igw": 2, "eip": 3}
    assert out["plan"]["resources"] == 46 and out["enrolment"] == {} and out["inventory_at"] is None
    assert all(n["id"].startswith(("aws_", "internet")) for n in out["nodes"])
    assert out["declared"]["flows"] and out["usecase"] == manifest.id and out["provider"] == "aws"
    assert scans == []


def test_engine_topology_reason_when_disconnected_or_unsupported(env) -> None:
    store, providers, manifest, root, scans = env
    store.delete_provider("aws")
    engine = _TopoEngine(store, providers, JobRunner(store), root)
    out = engine.topology(manifest)
    assert out["nodes"] == [] and "not connected" in out["reason"] and out["state"] == "unknown" and out["generated_at"]
    assert engine.calls == [] and scans == []
    data = yaml.safe_load(MANIFEST_WITH_TOPOLOGY)
    data["provider"] = "gcp"
    write_manifest(root, "zpa-private-service-edge", yaml.safe_dump(data))
    gcp_manifest = load_manifest(root / "zpa-private-service-edge" / "usecase.yaml", PROVIDERS)
    out = _TopoEngine(store, providers, JobRunner(store), root).topology(gcp_manifest)
    assert out["nodes"] == [] and "does not support use cases" in out["reason"]


def test_engine_topology_reason_when_nothing_tagged_or_scan_fails(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, providers, manifest, root, scans = env
    inv = pse_inventory()
    inv["regions"][0]["instances"] = inv["regions"][0]["vpcs"] = inv["regions"][0]["nat_gateways"] = []
    inv["regions"][0]["eips"] = inv["regions"][0]["security_groups"] = []
    store.save_inventory("aws", inv)
    engine = _TopoEngine(store, providers, JobRunner(store), root)
    out = engine.topology(manifest)
    assert out["nodes"] == [] and "Project=zpa-pse-lab" in out["reason"] and out["state"] == "on"

    def boom(credentials, regions):
        raise RuntimeError("EC2 exploded")

    monkeypatch.setattr(providers["aws"], "inventory", boom)
    stale = engine.topology(manifest, refresh=True)
    assert stale["stale"] is True and stale["nodes"] == []  # served the stale copy rather than 500
    # delete_provider also drops the on-disk inventory: reconnect, and now there is nothing stale to serve.
    record = store.get_provider("aws")
    store.delete_provider("aws")
    store.save_provider("aws", record)
    fresh = _TopoEngine(store, providers, JobRunner(store), root)
    out = fresh.topology(manifest, refresh=True)
    assert out["nodes"] == [] and out["reason"] == "Inventory scan failed: RuntimeError"


def test_detail_exposes_declared_topology(env) -> None:
    store, providers, manifest, root, _ = env
    engine = _TopoEngine(store, providers, JobRunner(store), root)
    detail = engine.detail(manifest)
    assert detail["topology"]["roles"]["zpa-lab-mcu-client"] == "client" and len(detail["topology"]["blocked"]) == 1


# ---------------------------------------------------------------- API
def test_topology_endpoint_without_provider_is_200_with_reason(logged_in, data_dir, tmp_path: Path) -> None:
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_TOPOLOGY)
    r = logged_in.get("/api/usecases/zpa-private-service-edge/topology")
    assert r.status_code == 200
    body = r.json()
    assert body["nodes"] == [] and body["edges"] == [] and body["enrolment"] == {} and body["unknown"] == []
    assert "not connected" in body["reason"] and body["generated_at"] and body["state"] == "unknown"
    assert body["usecase"] == "zpa-private-service-edge" and body["provider"] == "aws" and body["region"] is None
    assert body["declared"]["flows"][0]["via"] == ["nat", "internet"] and body["counts"]["instance"] == 0
    assert logged_in.get("/api/usecases/zpa-private-service-edge/topology?refresh=1").status_code == 200
    assert logged_in.get("/api/usecases/zpa-private-service-edge/topology?refresh=2").status_code == 422
    assert logged_in.get("/api/usecases/nope/topology").status_code == 404
    detail = logged_in.get("/api/usecases/zpa-private-service-edge").json()
    assert detail["topology"]["roles"]["zpa-lab-pse"] == "pse"


def test_topology_requires_auth(client) -> None:
    r = client.get("/api/usecases/x/topology")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"
