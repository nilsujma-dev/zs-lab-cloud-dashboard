"""The Cloud Connector use case (SPEC v1.5): a default route into a network interface.

Both registers must draw the same fact — the workload subnet's traffic is steered into the
Cloud Connector, so its exposure is `inspected` and a route edge lands on the instance, not on
a gateway. The live register resolves the interface through the inventory's ENI records; the
planned register resolves it through the plan's references. Also: the shipped manifest loads
and shows up beside the PSE lab. No cloud calls anywhere here.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from app.usecases.manifest import load_manifest
from app.usecases.plan_graph import build_plan_graph
from app.usecases.topology import build_graph

from tests.plan_fixture import cc_show

REPO = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO / "usecases" / "zcc-aws-workload" / "usecase.yaml"
PROVIDERS = {"aws", "gcp", "azure"}

# ---------------------------------------------------------------- the CC lab as the inventory sees it
VPC_C, VPC_D = "vpc-0c1111111111111c1", "vpc-0d2222222222222d2"
IGW_C, IGW_D = "igw-0c1111111111111c1", "igw-0d2222222222222d2"
SN_PUB, SN_CC, SN_WL, SN_CONN, SN_APP = "subnet-0c00", "subnet-0c20", "subnet-0c01", "subnet-0d10", "subnet-0d20"
RT_PUB, RT_CC, RT_WL, RT_CONN, RT_APP = "rtb-0c00", "rtb-0c20", "rtb-0c01", "rtb-0d10", "rtb-0d20"
NAT_C = "nat-0c1111111111111c1"
I_CC, I_WL, I_AC, I_APP = "i-0cc", "i-0workload", "i-0appconnector", "i-0app"
ENI_SVC, ENI_MGMT, ENI_NAT = "eni-0service", "eni-0mgmt", "eni-0nat"
SG_SVC, SG_MGMT, SG_WL, SG_AC, SG_APP = "sg-0svc", "sg-0mgmt", "sg-0wl", "sg-0ac", "sg-0app"
LAB = {"Project": "zcc-workload-lab", "ManagedBy": "opentofu"}


def _t(name: str) -> dict[str, str]:
    return {**LAB, "Name": name}


def _sn(sid: str, name: str, cidr: str, rt: str, target: str | None) -> dict[str, Any]:
    return {"id": sid, "name": name, "cidr": cidr, "az": "eu-central-1a", "public": bool(target and target.startswith("igw-")),
            "route_table": rt, "default_route": target, "map_public_ip": False, "available_ips": 250, "tags": _t(name)}


def _rt(rid: str, name: str, target: str | None, subnets: list[str], vpc_cidr: str) -> dict[str, Any]:
    routes = [{"dest": vpc_cidr, "target": "local", "state": "active"}]
    if target:
        routes.append({"dest": "0.0.0.0/0", "target": target, "state": "active"})
    return {"id": rid, "name": name, "main": False, "routes": routes, "subnets": subnets, "tags": _t(name)}


def _inst(iid: str, name: str, itype: str, vpc: str, subnet: str, priv: str, sg: str, pub: str | None = None) -> dict[str, Any]:
    return {"id": iid, "name": name, "type": itype, "state": "running", "private_ip": priv, "public_ip": pub,
            "launched": "2026-09-06T08:00:00+00:00", "uptime_h": 5.0, "platform": "Linux/UNIX", "architecture": "x86_64",
            "az": "eu-central-1a", "vpc": vpc, "subnet": subnet, "ami": "ami-0", "ami_name": None,
            "iam_instance_profile": "zcc-lab-node", "key_name": None, "security_groups": [{"id": sg, "name": sg}],
            "root_device": "/dev/xvda", "monitoring": False, "ebs_optimized": True, "volumes": [f"vol-{iid}"],
            "user_data_present": True, "monthly_usd": 10.0, "tags": _t(name)}


def _eni(eid: str, name: str | None, subnet: str, vpc: str, ip: str, instance: str | None, index: int | None) -> dict[str, Any]:
    return {"id": eid, "name": name, "description": "", "vpc": vpc, "subnet": subnet, "az": "eu-central-1a",
            "private_ip": ip, "instance": instance, "device_index": index, "status": "in-use",
            "interface_type": "interface", "source_dest_check": instance is None, "tags": _t(name) if name else {}}


def cc_inventory(*, enis: bool = True) -> dict[str, Any]:
    """eu-central-1 holding the CC lab: VPC C (public / cc / workload) and VPC D (connector / app)."""
    region: dict[str, Any] = {
        "region": "eu-central-1",
        "instances": [
            _inst(I_CC, "zcc-lab-cc", "m6i.large", VPC_C, SN_CC, "10.92.200.11", SG_MGMT),
            _inst(I_WL, "zcc-lab-workload", "t3.micro", VPC_C, SN_WL, "10.92.1.10", SG_WL),
            _inst(I_AC, "zcc-lab-app-connector", "t3.medium", VPC_D, SN_CONN, "10.93.10.10", SG_AC, pub="3.72.0.9"),
            _inst(I_APP, "zcc-lab-app", "t3.micro", VPC_D, SN_APP, "10.93.20.10", SG_APP),
        ],
        "vpcs": [
            {"id": VPC_C, "name": "zcc-lab-vpc-c", "cidr": "10.92.0.0/16", "default": False, "state": "available", "dns_hostnames": True,
             "igw": IGW_C, "nat_gateways": [NAT_C],
             "subnets": [_sn(SN_PUB, "zcc-lab-public", "10.92.0.0/24", RT_PUB, IGW_C),
                         _sn(SN_WL, "zcc-lab-workload", "10.92.1.0/24", RT_WL, ENI_SVC),
                         _sn(SN_CC, "zcc-lab-cc", "10.92.200.0/24", RT_CC, NAT_C)],
             "route_tables": [_rt(RT_PUB, "zcc-lab-public-rt", IGW_C, [SN_PUB], "10.92.0.0/16"),
                              _rt(RT_WL, "zcc-lab-workload-rt", ENI_SVC, [SN_WL], "10.92.0.0/16"),
                              _rt(RT_CC, "zcc-lab-cc-rt", NAT_C, [SN_CC], "10.92.0.0/16")],
             "tags": _t("zcc-lab-vpc-c")},
            {"id": VPC_D, "name": "zcc-lab-vpc-d", "cidr": "10.93.0.0/16", "default": False, "state": "available", "dns_hostnames": True,
             "igw": IGW_D, "nat_gateways": [],
             "subnets": [_sn(SN_CONN, "zcc-lab-connector", "10.93.10.0/24", RT_CONN, IGW_D),
                         _sn(SN_APP, "zcc-lab-app", "10.93.20.0/24", RT_APP, None)],
             "route_tables": [_rt(RT_CONN, "zcc-lab-connector-rt", IGW_D, [SN_CONN], "10.93.0.0/16"),
                              _rt(RT_APP, "zcc-lab-app-rt", None, [SN_APP], "10.93.0.0/16")],
             "tags": _t("zcc-lab-vpc-d")},
        ],
        "nat_gateways": [{"id": NAT_C, "name": "zcc-lab-nat", "vpc": VPC_C, "subnet": SN_PUB, "state": "available",
                          "public_ip": "18.194.7.7", "private_ip": "10.92.0.20", "connectivity_type": "public",
                          "created": "2026-09-06T08:00:00+00:00", "monthly_usd": 38.0, "tags": _t("zcc-lab-nat")}],
        "eips": [{"ip": "18.194.7.7", "allocation_id": "eipalloc-0nat", "attached": True, "instance": None,
                  "association": {"kind": "nat", "id": NAT_C, "eni": ENI_NAT}, "private_ip": "10.92.0.20",
                  "name": "zcc-lab-nat-eip", "monthly_usd": 3.65, "tags": _t("zcc-lab-nat-eip")}],
        "volumes": [],
        "security_groups": [
            {"id": SG_SVC, "name": "zcc-lab-cc-service", "vpc": VPC_C, "description": "service interface",
             "ingress": [{"proto": "tcp", "from": 0, "to": 65535, "source": "10.92.1.0/24"}],
             "egress": [], "attached_to": [], "tags": _t("zcc-lab-cc-service")},
            {"id": SG_APP, "name": "zcc-lab-app", "vpc": VPC_D, "description": "private app",
             "ingress": [{"proto": "tcp", "from": 8080, "to": 8080, "source": SG_AC}],
             "egress": [], "attached_to": [I_APP], "tags": _t("zcc-lab-app")},
        ],
        "network_interfaces": [
            _eni(ENI_SVC, "zcc-lab-cc-service", SN_CC, VPC_C, "10.92.200.10", I_CC, 1),
            _eni(ENI_MGMT, "zcc-lab-cc-mgmt", SN_CC, VPC_C, "10.92.200.11", I_CC, 0),
            _eni(ENI_NAT, None, SN_PUB, VPC_C, "10.92.0.20", None, None),
        ],
        "secrets": [],
        "monthly_usd": 192.0,
        "resource_count": 12,
        "error": None,
    }
    if not enis:
        region["network_interfaces"] = []
    return {"generated_at": "2026-09-06T12:00:00+00:00", "regions": [region], "totals": {}, "groups": [], "cost": {}}


@pytest.fixture(scope="module")
def manifest():
    return load_manifest(MANIFEST_PATH, PROVIDERS)


def subnets_of(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["label"]: n for n in graph["nodes"] if n["kind"] == "subnet"}


def route_edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [e for e in graph["edges"] if e["kind"] == "route"]


# ---------------------------------------------------------------- the shipped manifest
def test_manifest_loads_with_the_contracted_shape(manifest) -> None:
    assert manifest.id == "zcc-aws-workload" and manifest.provider == "aws"
    assert manifest.source_git == "https://github.com/nilsujma-dev/zs-zcc-aws-workload-lab.git" and manifest.source_ref == "main"
    assert manifest.terraform_dir == "terraform" and manifest.state_key == "usecases/zcc-aws-workload/terraform.tfstate"
    assert manifest.tags == {"Project": "zcc-workload-lab"} and manifest.secrets == ("zscaler_oneapi",)
    assert manifest.status is not None and manifest.status.run == "python3 scripts/status.py --json" and manifest.status.interval_s == 60
    assert [s.name for s in manifest.on] == [
        "Baseline the tenant", "Preflight quotas and secret", "Create ZPA connector group and app segment",
        "Create CC admin, templates and secret", "Seed provisioning key into SSM", "Apply infrastructure",
        "Wait for CC and connector registration", "Forward the app segment to ZPA",
        "Allow the lab CC group to the private app", "ZIA URL and DLP policy",
        "Verify nothing pre-existing changed", "Wait for egress and ZPA evidence"]
    assert [s.run for s in manifest.off] == ["tofu -chdir=terraform destroy -auto-approve -input=false"]
    assert manifest.topology.roles == {"zcc-lab-cc": "cloud-connector", "zcc-lab-workload": "workload",
                                       "zcc-lab-app-connector": "connector", "zcc-lab-app": "app"}
    assert len(manifest.topology.flows) == 4 and len(manifest.topology.blocked) == 1
    assert manifest.effects_on.creates and manifest.effects_off.retains
    assert any("Secrets Manager" in r for r in manifest.effects_off.retains)


def test_it_is_listed_beside_the_pse_lab(logged_in, tmp_path: Path) -> None:
    for uc in ("zpa-private-service-edge", "zcc-aws-workload"):
        shutil.copytree(REPO / "usecases" / uc, tmp_path / "usecases" / uc)
    cards = {c["id"]: c for c in logged_in.get("/api/usecases").json()}
    assert set(cards) == {"zpa-private-service-edge", "zcc-aws-workload"}
    assert [c["provider"] for c in cards.values()] == ["aws", "aws"]
    card = cards["zcc-aws-workload"]
    assert card["name"] == "Cloud Connector — AWS workload zero trust" and card["state"] in ("off", "unknown")
    detail = logged_in.get("/api/usecases/zcc-aws-workload").json()
    assert len(detail["procedure"]["on"]) == 12 and detail["procedure"]["on"][3]["name"] == "Create CC admin, templates and secret"
    assert "$192/month" in detail["description"]


# ---------------------------------------------------------------- live register
def test_live_subnet_routed_to_an_eni_is_inspected(manifest) -> None:
    graph = build_graph(manifest, cc_inventory(), None)
    subs = subnets_of(graph)
    assert subs["zcc-lab-workload"]["exposure"] == "inspected"
    assert subs["zcc-lab-workload"]["default_route"] == I_CC  # the instance, not the interface
    assert subs["zcc-lab-public"]["exposure"] == "public" and subs["zcc-lab-cc"]["exposure"] == "private"
    assert subs["zcc-lab-connector"]["exposure"] == "public" and subs["zcc-lab-app"]["exposure"] == "isolated"


def test_live_route_edge_lands_on_the_appliance_and_names_its_interface(manifest) -> None:
    graph = build_graph(manifest, cc_inventory(), None)
    edge = next(e for e in route_edges(graph) if e["from"] == SN_WL)
    assert edge["to"] == I_CC and edge["label"] == "0.0.0.0/0" and edge["inspected"] is True
    assert edge["eni"] == {"id": ENI_SVC, "name": "zcc-lab-cc-service", "private_ip": "10.92.200.10"}
    # the gateway routes are untouched
    assert {(e["from"], e["to"]) for e in route_edges(graph)} == {
        (SN_PUB, IGW_C), (SN_CC, NAT_C), (SN_WL, I_CC), (SN_CONN, IGW_D)}
    assert graph["unknown"] == []


def test_live_flows_and_blocked_pair_resolve(manifest) -> None:
    graph = build_graph(manifest, cc_inventory(), None)
    flows = {(e["from"], e["to"]): e for e in graph["edges"] if e["kind"] == "flow"}
    assert flows[(I_WL, I_CC)]["label"] == "0/0 + DNS ~zcc-lab.internal → service ENI"
    assert flows[(I_CC, "internet")]["via"] == [NAT_C, "internet"]
    assert flows[(I_AC, "internet")]["via"] == [IGW_D, "internet"]
    assert (I_AC, I_APP) in flows
    blocked = [e for e in graph["edges"] if e["kind"] == "blocked"]
    assert len(blocked) == 1 and (blocked[0]["from"], blocked[0]["to"]) == (I_WL, I_APP)
    roles = {n["label"]: n["role"] for n in graph["nodes"] if n["kind"] == "instance"}
    assert roles == {"zcc-lab-cc": "cloud-connector", "zcc-lab-workload": "workload",
                     "zcc-lab-app-connector": "connector", "zcc-lab-app": "app"}


def test_live_without_eni_records_the_subnet_is_not_claimed_to_be_inspected(manifest) -> None:
    """An inventory that never collected interfaces (or an ENI outside the use case) must not
    invent an appliance: the subnet keeps the old reading and no edge is drawn."""
    graph = build_graph(manifest, cc_inventory(enis=False), None)
    workload = subnets_of(graph)["zcc-lab-workload"]
    assert workload["exposure"] == "private" and workload["default_route"] == ENI_SVC
    assert not [e for e in route_edges(graph) if e["from"] == SN_WL]


def test_live_eni_owned_by_an_undrawn_instance_is_reported_not_dropped(manifest) -> None:
    inv = cc_inventory()
    region = inv["regions"][0]
    region["network_interfaces"][0]["instance"] = "i-0somewhere-else"
    graph = build_graph(manifest, inv, None)
    assert subnets_of(graph)["zcc-lab-workload"]["exposure"] == "inspected"
    assert not [e for e in route_edges(graph) if e["from"] == SN_WL]
    reason = next(u for u in graph["unknown"] if u["kind"] == "route")["reason"]
    assert "i-0somewhere-else" in reason


# ---------------------------------------------------------------- planned register
@pytest.mark.parametrize("route_style", ["inline", "standalone"])
@pytest.mark.parametrize("attach_style", ["inline", "resource"])
def test_planned_register_reads_the_same_as_the_live_one(manifest, route_style: str, attach_style: str) -> None:
    graph = build_plan_graph(manifest, cc_show(route_style=route_style, attach_style=attach_style))
    subs = subnets_of(graph)
    assert subs["zcc-lab-workload"]["exposure"] == "inspected"
    assert subs["zcc-lab-workload"]["default_route"] == "aws_instance.cc"
    assert subs["zcc-lab-public"]["exposure"] == "public" and subs["zcc-lab-cc"]["exposure"] == "private"
    assert subs["zcc-lab-app"]["exposure"] == "isolated"
    edge = next(e for e in route_edges(graph) if e["from"] == "aws_subnet.workload")
    assert edge["to"] == "aws_instance.cc" and edge["inspected"] is True
    assert edge["eni"] == {"id": "aws_network_interface.cc_service", "name": "zcc-lab-cc-service", "private_ip": "10.92.200.10"}
    assert graph["counts"] == {"internet": 1, "vpc": 2, "subnet": 5, "instance": 4, "nat": 1, "igw": 2, "eip": 1}
    assert graph["unknown"] == [] and graph["enrolment"] == {} and graph["region"] == "eu-central-1"
    # nothing exists yet: every address is unknown until apply
    assert all(n.get("private_ip") is None for n in graph["nodes"] if n["kind"] == "instance")


@pytest.mark.parametrize("attach_style", ["inline", "resource"])
def test_planned_appliance_sits_in_the_subnet_of_its_interfaces(manifest, attach_style: str) -> None:
    """Upstream's Cloud Connector takes no `subnet_id`; its device-index-0 interface places it."""
    graph = build_plan_graph(manifest, cc_show(attach_style=attach_style))
    # `zcc-lab-cc` names both the subnet and the appliance in it, so ask for the instance.
    cc = next(n for n in graph["nodes"] if n["kind"] == "instance" and n["label"] == "zcc-lab-cc")
    assert cc["parent"] == "aws_subnet.cc" and cc["role"] == "cloud-connector" and cc["type"] == "m6i.large"
    assert cc["az"] == "eu-central-1a"  # inherited from the subnet the plan places it in


def test_planned_security_rules_reach_an_appliance_through_its_interface(manifest) -> None:
    graph = build_plan_graph(manifest, cc_show(attach_style="inline"))
    allows = {(e["from"], e["to"]): e["label"] for e in graph["edges"] if e["kind"] == "allow"}
    assert allows[("10.92.1.0/24", "aws_instance.cc")] == "tcp/0-65535"
    assert allows[("aws_security_group.app_connector", "aws_instance.app")] == "tcp/8080"


def test_planned_flows_route_through_the_right_gateways(manifest) -> None:
    graph = build_plan_graph(manifest, cc_show())
    flows = {(e["from"], e["to"]): e for e in graph["edges"] if e["kind"] == "flow"}
    assert flows[("aws_instance.cc", "internet")]["via"] == ["aws_nat_gateway.c", "internet"]
    assert flows[("aws_instance.app_connector", "internet")]["via"] == ["aws_internet_gateway.d", "internet"]
    assert ("aws_instance.workload", "aws_instance.cc") in flows
    blocked = [e for e in graph["edges"] if e["kind"] == "blocked"]
    assert len(blocked) == 1 and blocked[0]["from"] == "aws_instance.workload"
