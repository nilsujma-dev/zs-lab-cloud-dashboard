"""Planned topology (v1.4): the plan-document graph builder against a `tofu show -json` fixture
modelled on the PSE lab, `source` line resolution against a temp checkout, the unified plan
cache (outline and topology from one run), the failed/disconnected paths, and the route.
No cloud, no tofu binary: the runner is replaced by canned output."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from app.jobs import JobRunner
from app.providers import build_registry
from app.store import Store
from app.usecases.engine import Engine
from app.usecases.manifest import load_manifest, parse_manifest
from app.usecases.plan_graph import AWS, SCHEMAS, LINK_KINDS, STRUCTURE_KINDS, SourceIndex, build_plan_graph, strip_index
from app.usecases.topology import build_graph
from tests.conftest import GOOD_MANIFEST, write_manifest
from tests.plan_fixture import LAB_COUNT, RESOURCES, SOURCE_LINES, pse_plan_stream, pse_show, write_checkout
from tests.test_topology import I_PSE, IGW_A, IGW_B, NAT_B, VPC_A, VPC_B, MANIFEST_WITH_TOPOLOGY, PROVIDERS, pse_inventory

VPC_LAB, VPC_BB = "aws_vpc.lab", "aws_vpc.b"
IGW_LAB, IGW_BB = "aws_internet_gateway.igw", "aws_internet_gateway.b"
SN_PUB, SN_B_PUB, SN_PRIV, SN_MCU = "aws_subnet.public", "aws_subnet.b_public", "aws_subnet.priv", "aws_subnet.mcu"
NAT = "aws_nat_gateway.b"
PSE, CONN, PRIV, SRV, MCU = "aws_instance.pse", "aws_instance.connector", "aws_instance.priv_connector", "aws_instance.server", "aws_instance.mcu_client"
EIP_PSE, EIP_NAT, EIP_SPARE = "aws_eip.pse", "aws_eip.nat", "aws_eip.spare"
SG_PSE, SG_PRIV, SG_SRV = "aws_security_group.pse", "aws_security_group.priv_connector", "aws_security_group.server"


def _manifest(text: str = MANIFEST_WITH_TOPOLOGY):
    return parse_manifest(yaml.safe_load(text), expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)


def _nodes(graph: dict, kind: str) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"] if n["kind"] == kind}


def _edges(graph: dict, kind: str) -> list[dict]:
    return [e for e in graph["edges"] if e["kind"] == kind]


@pytest.fixture
def planned() -> dict:
    return build_plan_graph(_manifest(), pse_show(), None)


@pytest.fixture
def live() -> dict:
    return build_graph(_manifest(), pse_inventory(), None)


# ---------------------------------------------------------------- same drawing, other register
def test_same_counts_and_shape_as_the_live_graph(planned: dict, live: dict) -> None:
    assert planned["counts"] == live["counts"] == {"internet": 1, "vpc": 2, "subnet": 4, "instance": 5, "nat": 1, "igw": 2, "eip": 3}
    assert len(planned["nodes"]) == len(live["nodes"]) == 18
    assert {e["kind"] for e in planned["edges"]} == {e["kind"] for e in live["edges"]} == {"route", "uplink", "allow", "flow", "blocked"}
    assert {k: len(_edges(planned, k)) for k in ("route", "uplink", "allow", "flow", "blocked")} == {k: len(_edges(live, k)) for k in ("route", "uplink", "allow", "flow", "blocked")}
    assert planned["usecase"] == "zpa-private-service-edge" and planned["provider"] == "aws"
    assert planned["region"] == "eu-central-1" and planned["regions"] == ["eu-central-1"]
    assert planned["nodes"][0] == {"id": "internet", "kind": "internet", "label": "Internet", "parent": None}
    assert planned["enrolment"] == {} and planned["unknown"] == []
    assert planned["declared"]["roles"]["zpa-lab-pse"] == "pse"
    for node in planned["nodes"][1:]:
        assert set(node) >= {"id", "kind", "label", "parent", "address", "detail", "tagged", "region"}
        assert node["kind"] in STRUCTURE_KINDS


def test_ids_are_addresses_and_labels_come_from_name_tags(planned: dict) -> None:
    ids = {n["id"] for n in planned["nodes"]} - {"internet"}
    assert ids == {VPC_LAB, VPC_BB, IGW_LAB, IGW_BB, SN_PUB, SN_B_PUB, SN_PRIV, SN_MCU, NAT, PSE, CONN, PRIV, SRV, MCU, EIP_PSE, EIP_NAT, EIP_SPARE}
    assert all(n["address"] == n["id"] for n in planned["nodes"][1:])
    vpcs, insts, eips = _nodes(planned, "vpc"), _nodes(planned, "instance"), _nodes(planned, "eip")
    assert vpcs[VPC_LAB]["label"] == "zpa-lab-vpc-a" and vpcs[VPC_BB]["label"] == "zpa-lab-vpc-b"
    assert insts[PSE]["label"] == "zpa-lab-pse" and insts[MCU]["label"] == "zpa-lab-mcu-client"
    assert eips[EIP_PSE]["label"] == "zpa-lab-pse-eip"  # no IP yet: the name tag, not an invented address
    assert _nodes(planned, "igw")[IGW_LAB]["label"] == "IGW"
    # No name tag -> the resource name.
    show = pse_show()
    next(r for r in show["planned_values"]["root_module"]["resources"] if r["address"] == VPC_LAB)["values"].pop("tags")
    next(r for r in show["configuration"]["root_module"]["resources"] if r["address"] == VPC_LAB)["expressions"].pop("tags")
    assert _nodes(build_plan_graph(_manifest(), show), "vpc")[VPC_LAB]["label"] == "lab"


def test_parent_chains_from_configuration_references(planned: dict) -> None:
    vpcs, subnets, insts = _nodes(planned, "vpc"), _nodes(planned, "subnet"), _nodes(planned, "instance")
    assert vpcs[VPC_LAB]["parent"] is None and vpcs[VPC_LAB]["cidr"] == "10.91.0.0/16" and vpcs[VPC_BB]["cidr"] == "10.90.0.0/16"
    assert subnets[SN_PUB]["parent"] == VPC_LAB
    assert {s: subnets[s]["parent"] for s in (SN_B_PUB, SN_PRIV, SN_MCU)} == {SN_B_PUB: VPC_BB, SN_PRIV: VPC_BB, SN_MCU: VPC_BB}
    assert subnets[SN_PRIV]["cidr"] == "10.90.20.0/24" and subnets[SN_PRIV]["az"] == "eu-central-1a"
    assert insts[PSE]["parent"] == SN_PUB and insts[CONN]["parent"] == SN_PUB
    assert insts[PRIV]["parent"] == SN_PRIV and insts[SRV]["parent"] == SN_PRIV and insts[MCU]["parent"] == SN_MCU
    nat = _nodes(planned, "nat")[NAT]
    assert nat["parent"] == VPC_BB and nat["subnet"] == SN_B_PUB and nat["label"] == "zpa-lab-nat"
    igws = _nodes(planned, "igw")
    assert igws[IGW_LAB]["parent"] == VPC_LAB and igws[IGW_BB]["parent"] == VPC_BB
    assert [n["id"] for n in planned["nodes"]][:5] == ["internet", VPC_BB, IGW_BB, VPC_LAB, IGW_LAB]  # VPCs by CIDR, IGW after its VPC


def test_unknown_until_apply_is_null_never_invented(planned: dict) -> None:
    for inst in _nodes(planned, "instance").values():
        assert inst["private_ip"] is None and inst["public_ip"] is None and inst["state"] is None
        assert inst["az"] == "eu-central-1a"  # inherited from the planned subnet
    pse = _nodes(planned, "instance")[PSE]
    assert pse["type"] == "m5.large" and pse["role"] == "pse"
    assert {i: n["role"] for i, n in _nodes(planned, "instance").items()} == {PSE: "pse", CONN: "connector", PRIV: "connector", SRV: "app", MCU: "client"}
    nat = _nodes(planned, "nat")[NAT]
    assert nat["public_ip"] is None and nat["private_ip"] is None and nat["state"] is None
    eips = _nodes(planned, "eip")
    assert all(e["ip"] is None for e in eips.values())
    assert eips[EIP_PSE]["attached_to"] == PSE and eips[EIP_NAT]["attached_to"] == NAT
    assert eips[EIP_SPARE]["attached_to"] is None and eips[EIP_SPARE]["attached"] is False and eips[EIP_SPARE]["parent"] is None
    # `detail` is the planned values (plus address/type/name); long text is elided, IPs absent.
    assert pse["detail"]["type"] == "aws_instance" and pse["detail"]["name"] == "pse" and pse["detail"]["instance_type"] == "m5.large"
    assert "private_ip" not in pse["detail"] and pse["detail"]["user_data"].startswith("<") and "chars>" in pse["detail"]["user_data"]


def test_subnet_exposure_from_route_tables_via_associations(planned: dict) -> None:
    subnets = _nodes(planned, "subnet")
    assert subnets[SN_PUB]["exposure"] == "public" and subnets[SN_PUB]["default_route"] == IGW_LAB and subnets[SN_PUB]["route_table"] == "aws_route_table.public"
    assert subnets[SN_B_PUB]["exposure"] == "public" and subnets[SN_B_PUB]["default_route"] == IGW_BB
    assert subnets[SN_PRIV]["exposure"] == "private" and subnets[SN_PRIV]["default_route"] == NAT and subnets[SN_PRIV]["route_table"] == "aws_route_table.b_private"
    assert subnets[SN_MCU]["exposure"] == "private"
    routes = {(e["from"], e["to"]) for e in _edges(planned, "route")}
    assert routes == {(SN_PUB, IGW_LAB), (SN_B_PUB, IGW_BB), (SN_PRIV, NAT), (SN_MCU, NAT)}
    assert all(e["label"] == "0.0.0.0/0" for e in _edges(planned, "route"))
    uplinks = {(e["from"], e["to"]) for e in _edges(planned, "uplink")}
    assert uplinks == {(NAT, IGW_BB), (IGW_LAB, "internet"), (IGW_BB, "internet")}


def test_isolated_subnet_standalone_route_and_default_route_table() -> None:
    show = pse_show()
    cfg = show["configuration"]["root_module"]["resources"]
    # No association for mcu -> isolated.
    cfg[:] = [r for r in cfg if r["address"] != "aws_route_table_association.mcu"]
    show["planned_values"]["root_module"]["resources"] = [r for r in show["planned_values"]["root_module"]["resources"] if r["address"] != "aws_route_table_association.mcu"]
    g = build_plan_graph(_manifest(), show)
    assert _nodes(g, "subnet")[SN_MCU]["exposure"] == "isolated" and _nodes(g, "subnet")[SN_MCU]["default_route"] is None
    assert not any(e["from"] == SN_MCU for e in _edges(g, "route"))
    # A standalone aws_route on a table without inline routes.
    priv_rt = next(r for r in cfg if r["address"] == "aws_route_table.b_private")
    priv_rt["expressions"].pop("route")
    cfg.append({"address": "aws_route.b_default", "mode": "managed", "type": "aws_route", "name": "b_default", "provider_config_key": "aws",
                "expressions": {"route_table_id": {"references": ["aws_route_table.b_private.id", "aws_route_table.b_private"]},
                                "destination_cidr_block": {"constant_value": "0.0.0.0/0"},
                                "nat_gateway_id": {"references": ["aws_nat_gateway.b.id", "aws_nat_gateway.b"]}}})
    show["planned_values"]["root_module"]["resources"].append({"address": "aws_route.b_default", "mode": "managed", "type": "aws_route", "name": "b_default",
                                                               "values": {"destination_cidr_block": "0.0.0.0/0"}})
    g = build_plan_graph(_manifest(), show)
    assert _nodes(g, "subnet")[SN_PRIV]["exposure"] == "private" and _nodes(g, "subnet")[SN_PRIV]["default_route"] == NAT
    # A default route table for VPC B catches the unassociated mcu subnet.
    cfg.append({"address": "aws_default_route_table.b", "mode": "managed", "type": "aws_default_route_table", "name": "b", "provider_config_key": "aws",
                "expressions": {"default_route_table_id": {"references": ["aws_vpc.b.default_route_table_id", "aws_vpc.b"]},
                                "route": [{"cidr_block": {"constant_value": "0.0.0.0/0"}, "gateway_id": {"references": ["aws_internet_gateway.b.id", "aws_internet_gateway.b"]}}]}})
    show["planned_values"]["root_module"]["resources"].append({"address": "aws_default_route_table.b", "mode": "managed", "type": "aws_default_route_table", "name": "b", "values": {}})
    g = build_plan_graph(_manifest(), show)
    assert _nodes(g, "subnet")[SN_MCU]["exposure"] == "public" and _nodes(g, "subnet")[SN_MCU]["route_table"] == "aws_default_route_table.b"


def test_allow_edges_from_standalone_and_inline_rules(planned: dict) -> None:
    allows = _edges(planned, "allow")
    to_pse = {e["from"]: e for e in allows if e["to"] == PSE}
    assert set(to_pse) == {"10.91.0.0/16", EIP_NAT}
    assert to_pse["10.91.0.0/16"]["label"] == "tcp/443" and to_pse["10.91.0.0/16"]["rule"] == "aws_vpc_security_group_ingress_rule.pse_from_vpc_a"
    assert "source_nodes" not in to_pse["10.91.0.0/16"]
    # "${aws_eip.nat.public_ip}/32" is unknown until apply: the edge points at the EIP node instead.
    assert to_pse[EIP_NAT]["source_nodes"] == [EIP_NAT] and to_pse[EIP_NAT]["rule"] == "aws_vpc_security_group_ingress_rule.pse_from_vpc_b"
    assert all(e["group"] == {"id": SG_PSE, "name": "zpa-lab-pse"} for e in to_pse.values())
    to_srv = [e for e in allows if e["to"] == SRV]
    assert len(to_srv) == 1 and to_srv[0]["from"] == SG_PRIV and to_srv[0]["label"] == "tcp/8080" and to_srv[0]["source_nodes"] == [PRIV]
    assert to_srv[0]["group"] == {"id": SG_SRV, "name": "zpa-lab-server"} and "rule" not in to_srv[0]
    assert not any(e["to"] in (CONN, PRIV, MCU) for e in allows)  # egress-only groups, egress rules ignored


def test_legacy_security_group_rule_resource_counts_only_for_ingress() -> None:
    show = pse_show()
    for rtype_name, values in ((("aws_security_group_rule", "legacy_in"), {"type": "ingress", "from_port": 22, "to_port": 22, "protocol": "tcp", "cidr_blocks": ["10.0.0.0/8"]}),
                               (("aws_security_group_rule", "legacy_out"), {"type": "egress", "from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]})):
        addr = ".".join(rtype_name)
        show["planned_values"]["root_module"]["resources"].append({"address": addr, "mode": "managed", "type": rtype_name[0], "name": rtype_name[1], "values": values})
        show["configuration"]["root_module"]["resources"].append({"address": addr, "mode": "managed", "type": rtype_name[0], "name": rtype_name[1], "provider_config_key": "aws",
                                                                  "expressions": {"security_group_id": {"references": ["aws_security_group.connector.id", "aws_security_group.connector"]}}})
    g = build_plan_graph(_manifest(), show)
    to_conn = [e for e in _edges(g, "allow") if e["to"] == CONN]
    assert [(e["from"], e["label"]) for e in to_conn] == [("10.0.0.0/8", "tcp/22")]


def test_flows_resolve_through_the_planned_nat_and_blocked_pair(planned: dict) -> None:
    flows = _edges(planned, "flow")
    assert len(flows) == 5 and all(f["declared"] is True for f in flows)
    by = {(f["from"], f["to"]): f for f in flows}
    assert by[(MCU, PSE)]["via"] == [NAT, "internet"] and by[(MCU, PSE)]["label"] == "dials :443"
    assert by[(PRIV, PSE)]["via"] == [NAT, "internet"]
    assert by[(CONN, PSE)]["via"] == [] and by[(PRIV, SRV)]["label"] == ":8080 brokered"
    assert by[(PSE, "internet")]["label"] == "control plane :443"
    assert not any("via_missing" in f for f in flows)
    assert _edges(planned, "blocked") == [{"kind": "blocked", "from": MCU, "to": SRV, "via": [], "label": "no route", "declared": True}]


def test_unresolvable_flow_and_instance_without_subnet_go_to_unknown() -> None:
    data = yaml.safe_load(MANIFEST_WITH_TOPOLOGY)
    data["topology"]["flows"].append({"from": "zpa-lab-ghost", "to": "zpa-lab-pse", "label": "haunt"})
    show = pse_show()
    stray_cfg = next(r for r in show["configuration"]["root_module"]["resources"] if r["address"] == MCU)
    stray_cfg["expressions"].pop("subnet_id")
    g = build_plan_graph(parse_manifest(data, expected_id="zpa-private-service-edge", provider_ids=PROVIDERS), show)
    assert MCU not in _nodes(g, "instance") and g["counts"]["instance"] == 4
    kinds = {(u["kind"], u.get("id") or u.get("from")): u for u in g["unknown"]}
    assert "no subnet" in kinds[("instance", MCU)]["reason"]
    assert "'zpa-lab-ghost'" in kinds[("flow", "zpa-lab-ghost")]["reason"]
    assert kinds[("blocked", "zpa-lab-mcu-client")]["reason"]  # the blocked pair's source is gone too


def test_degenerate_plans_yield_no_nodes() -> None:
    m = _manifest()
    for show in (None, {}, {"planned_values": {}}, {"planned_values": {"root_module": {"resources": []}}}, {"format_version": "1.2", "errored": True}):
        g = build_plan_graph(m, show)
        assert g["nodes"] == [] and g["edges"] == [] and g["region"] is None and g["enrolment"] == {} and g["counts"]["vpc"] == 0
    data = yaml.safe_load(MANIFEST_WITH_TOPOLOGY)
    data["provider"] = "gcp"
    assert build_plan_graph(parse_manifest(data, expected_id="zpa-private-service-edge", provider_ids=PROVIDERS), pse_show())["nodes"] == []
    # Only IAM and NACLs: nothing drawable.
    show = pse_show()
    keep = {"aws_iam_role", "aws_network_acl", "aws_network_acl_rule"}
    show["planned_values"]["root_module"]["resources"] = [r for r in show["planned_values"]["root_module"]["resources"] if r["type"] in keep]
    assert build_plan_graph(m, show)["nodes"] == []


def test_child_modules_and_indexed_addresses_resolve() -> None:
    show = {
        "planned_values": {"root_module": {"resources": [
            {"address": "aws_vpc.v", "mode": "managed", "type": "aws_vpc", "name": "v", "values": {"cidr_block": "10.0.0.0/16", "tags": {"Name": "v"}}},
            {"address": "aws_subnet.s[0]", "mode": "managed", "type": "aws_subnet", "name": "s", "values": {"cidr_block": "10.0.0.0/24"}},
            {"address": "aws_subnet.s[1]", "mode": "managed", "type": "aws_subnet", "name": "s", "values": {"cidr_block": "10.0.1.0/24"}},
        ], "child_modules": [{"address": "module.box", "resources": [
            {"address": "module.box.aws_instance.i", "mode": "managed", "type": "aws_instance", "name": "i", "values": {"instance_type": "t3.nano", "tags": {"Name": "boxed"}}},
        ]}]}},
        "configuration": {"root_module": {
            "resources": [
                {"address": "aws_vpc.v", "mode": "managed", "type": "aws_vpc", "name": "v", "expressions": {}},
                {"address": "aws_subnet.s", "mode": "managed", "type": "aws_subnet", "name": "s", "expressions": {"vpc_id": {"references": ["aws_vpc.v.id", "aws_vpc.v"]}, "count": {"constant_value": 2}}},
            ],
            "module_calls": {"box": {"source": "./box", "module": {"resources": [
                {"address": "aws_instance.i", "mode": "managed", "type": "aws_instance", "name": "i", "expressions": {"subnet_id": {"references": ["var.subnet"]}}},
            ]}}},
        }},
    }
    g = build_plan_graph(_manifest(), show)
    subnets = _nodes(g, "subnet")
    assert set(subnets) == {"aws_subnet.s[0]", "aws_subnet.s[1]"} and all(s["parent"] == "aws_vpc.v" for s in subnets.values())
    assert all(s["exposure"] == "isolated" for s in subnets.values())
    # The module's instance references a variable, not a subnet: reported, not drawn.
    assert [u["id"] for u in g["unknown"] if u["kind"] == "instance"] == ["module.box.aws_instance.i"]
    assert strip_index('module.m["a"].aws_vpc.v[0]') == "module.m.aws_vpc.v"


def test_graph_is_deterministic_and_json_clean(planned: dict) -> None:
    again = build_plan_graph(_manifest(), pse_show())
    assert json.dumps(planned, sort_keys=True) == json.dumps(again, sort_keys=True)


# ---------------------------------------------------------------- the type table
def test_aws_table_is_the_only_provider_shaped_thing() -> None:
    kinds = AWS.kinds()
    assert {kinds[t] for t in ("aws_vpc", "aws_subnet", "aws_instance", "aws_nat_gateway", "aws_internet_gateway", "aws_eip")} == set(STRUCTURE_KINDS)
    assert set(kinds.values()) <= set(STRUCTURE_KINDS) | set(LINK_KINDS)
    assert SCHEMAS == {"aws": AWS}


def test_fixture_covers_every_mapped_type_present_in_the_lab() -> None:
    """Every AWS type the table maps that the real lab declares is exercised by the fixture, and
    the fixture holds exactly the lab's 45 managed resources (plus the spare EIP)."""
    lab_types = {rtype for rtype, *_ in RESOURCES}
    mapped_in_lab = {"aws_vpc", "aws_subnet", "aws_instance", "aws_nat_gateway", "aws_internet_gateway", "aws_eip",
                     "aws_route_table", "aws_route_table_association", "aws_security_group", "aws_vpc_security_group_ingress_rule"}
    assert mapped_in_lab <= lab_types and mapped_in_lab <= set(AWS.types)
    assert LAB_COUNT == 45
    show = pse_show()
    assert len(show["planned_values"]["root_module"]["resources"]) == 46 and len(show["resource_changes"]) == 46
    assert sum(1 for line in pse_plan_stream().splitlines() if '"planned_change"' in line) == 46
    # Unknown-until-apply attributes are absent from values, exactly as tofu writes them.
    pse = next(r for r in show["planned_values"]["root_module"]["resources"] if r["address"] == PSE)
    assert "private_ip" not in pse["values"] and "subnet_id" not in pse["values"]


# ---------------------------------------------------------------- source lines
def test_source_index_resolves_every_block_by_line(tmp_path: Path) -> None:
    checkout = write_checkout(tmp_path / "checkout")
    index = SourceIndex.scan(checkout, "terraform")
    assert {e.address: (e.path, e.line) for e in index.entries} == SOURCE_LINES
    # Heredocs and braces inside strings did not derail the block scanner: the block after them is right.
    assert index.source(PSE) == {"path": "terraform/main.tf", "line": 56} and index.source(SRV) == {"path": "terraform/vpc_b.tf", "line": 41}
    assert index.source("aws_subnet.public[0]") == {"path": "terraform/main.tf", "line": 17}
    assert index.source("aws_instance.mcu_client") is None and index.source("module.x.aws_vpc.lab") is None
    by = index.by_address
    assert by[IGW_LAB].name_tag == "zpa-lab-igw" and by[IGW_LAB].refs == {"vpc_id": VPC_LAB}
    assert by[PSE].refs == {"subnet_id": SN_PUB, "vpc_security_group_ids": SG_PSE}
    assert by["aws_route_table_association.public"].name_tag is None
    assert SourceIndex.scan(tmp_path / "nowhere", "terraform").entries == []


def test_planned_nodes_carry_source_when_the_checkout_has_the_block(tmp_path: Path) -> None:
    index = SourceIndex.scan(write_checkout(tmp_path / "checkout"), "terraform")
    g = build_plan_graph(_manifest(), pse_show(), index)
    nodes = {n["id"]: n for n in g["nodes"]}
    for addr, (path, line) in SOURCE_LINES.items():
        if addr in nodes:
            assert nodes[addr]["source"] == {"path": path, "line": line}, addr
    assert nodes[IGW_LAB]["source"] == {"path": "terraform/main.tf", "line": 13}
    assert "source" not in nodes[MCU] and "source" not in nodes[PRIV] and "source" not in nodes["internet"]
    assert "source" not in build_plan_graph(_manifest(), pse_show(), None)["nodes"][1]


def test_live_nodes_match_source_by_name_tag_and_gateways_by_vpc(tmp_path: Path) -> None:
    index = SourceIndex.scan(write_checkout(tmp_path / "checkout"), "terraform")
    g = build_graph(_manifest(), pse_inventory(), None)
    state = [f"{t}.{n}" for t, n, *_ in RESOURCES]
    index.attach_live(g["nodes"], AWS, state)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes[I_PSE]["source"] == {"path": "terraform/main.tf", "line": 56} and nodes[I_PSE]["address"] == PSE
    assert nodes[VPC_A]["source"] == {"path": "terraform/main.tf", "line": 9} and nodes[VPC_B]["address"] == VPC_BB
    assert nodes[IGW_A]["source"] == {"path": "terraform/main.tf", "line": 13} and nodes[IGW_B]["address"] == IGW_BB
    assert nodes[NAT_B]["address"] == NAT and nodes["eipalloc-0nat"]["address"] == EIP_NAT and nodes["eipalloc-0pse"]["address"] == EIP_PSE
    assert nodes["subnet-0b20"]["address"] == SN_PRIV
    assert "source" not in nodes["i-0mcu"] and "source" not in nodes["eipalloc-0idle"]  # no block in this checkout
    # Restricted to what is in state: a block not in state does not match.
    g2 = build_graph(_manifest(), pse_inventory(), None)
    index.attach_live(g2["nodes"], AWS, [VPC_BB])
    n2 = {n["id"]: n for n in g2["nodes"]}
    assert "source" not in n2[I_PSE] and n2[VPC_B]["address"] == VPC_BB and "source" not in n2[IGW_A]
    # Unknown state (None) matches on the blocks alone.
    g3 = build_graph(_manifest(), pse_inventory(), None)
    index.attach_live(g3["nodes"], AWS, None)
    assert {n["id"]: n for n in g3["nodes"]}[I_PSE]["address"] == PSE


# ---------------------------------------------------------------- engine: one plan, two consumers
class _PlanEngine(Engine):
    """tofu replaced: `plan` returns the fixture stream (and writes the -out file), `show` returns
    the fixture document, `state list` returns `state_output`. Counts every call."""

    def __init__(self, *args, plan_output: str | None = None, plan_code: int = 0, show_output: str | None = None, show_code: int = 0,
                 state_output: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.plan_output = pse_plan_stream() if plan_output is None else plan_output
        self.show_output = json.dumps(pse_show()) if show_output is None else show_output
        self.plan_code, self.show_code, self.state_output = plan_code, show_code, state_output
        self.calls: list[list[str]] = []
        self.plan_files_seen: list[bool] = []

    def prepare(self, manifest, env, log_line=None):  # type: ignore[override]
        self._initialised.add(manifest.id)
        write_checkout(self._store.checkout_dir(manifest.id))
        return "deadbeef"

    def _run(self, args, *, cwd, env, timeout, log_line=None):  # type: ignore[override]
        self.calls.append(args)
        if "plan" in args:
            out = next((a.split("=", 1)[1] for a in args if a.startswith("-out=")), None)
            if out and self.plan_code == 0:
                (Path(cwd) / "terraform" / out).write_bytes(b"PK\x03\x04planfile")
            return self.plan_code, self.plan_output
        if "show" in args:
            self.plan_files_seen.append((Path(cwd) / "terraform" / args[-1]).exists())
            return self.show_code, self.show_output
        if "state" in args:
            return 0, self.state_output
        return 0, ""

    def n(self, verb: str) -> int:
        return sum(1 for c in self.calls if verb in c)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "data")
    store.save_provider("aws", {
        "status": "connected",
        "identity": {"account": "257300000000", "arn": "arn:aws:sts::257300000000:assumed-role/x/y", "alias": None},
        "regions": ["eu-central-1"],
        "credentials": store.encrypt({"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "s3cr3t-value", "session_token": None}),
        "connected_at": "2026-09-05T10:00:00+00:00",
    })
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_TOPOLOGY)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", PROVIDERS)
    return store, build_registry(store.pricing_cache_path), manifest, tmp_path / "usecases"


def test_off_state_draws_the_plan_and_shares_one_run_with_the_outline(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root)
    topo = engine.topology(manifest)
    assert topo["state"] == "off" and topo["register"] == "planned" and topo["reason"] is None
    assert topo["counts"] == {"internet": 1, "vpc": 2, "subnet": 4, "instance": 5, "nat": 1, "igw": 2, "eip": 3}
    assert topo["plan"] == {"generated_at": topo["plan"]["generated_at"], "resources": 46, "error": None} and topo["plan"]["generated_at"]
    assert topo["enrolment"] == {} and topo["inventory_at"] is None and "_at" not in topo
    nodes = {n["id"]: n for n in topo["nodes"]}
    assert nodes[PSE]["private_ip"] is None and nodes[PSE]["source"] == {"path": "terraform/main.tf", "line": 56}
    assert engine.n("plan") == 1 and engine.n("show") == 1
    # The outline is served from the same run: no second plan, same count.
    outline = engine.outline(manifest, "on")
    assert outline["plan"]["ok"] and outline["plan"]["summary"]["create"] == 46 == topo["plan"]["resources"]
    assert outline["plan"]["generated_at"] == topo["plan"]["generated_at"]
    assert engine.n("plan") == 1 and engine.n("show") == 1
    assert engine.topology(manifest) == topo and engine.n("plan") == 1
    # Flags: as before, plus -out; show reads that file; the file is gone afterwards.
    plan_call = next(c for c in engine.calls if "plan" in c)
    assert plan_call[:4] == ["tofu", "-chdir=terraform", "plan", "-json"]
    assert {"-input=false", "-lock=false", "-refresh=true", "-out=.switchboard-on.tfplan"} <= set(plan_call) and "-destroy" not in plan_call
    show_call = next(c for c in engine.calls if "show" in c)
    assert show_call == ["tofu", "-chdir=terraform", "show", "-json", "-no-color", ".switchboard-on.tfplan"]
    assert engine.plan_files_seen == [True]
    assert not list((store.checkout_dir(manifest.id) / "terraform").glob("*.tfplan"))
    assert not any("apply" in c for c in engine.calls)
    # refresh=1 re-plans; a job ending invalidates.
    assert engine.topology(manifest, refresh=True)["register"] == "planned" and engine.n("plan") == 2
    engine._after_job({"usecase": manifest.id, "id": "j1", "action": "on", "state": "failed"})
    engine.topology(manifest)
    assert engine.n("plan") == 3


def test_outline_off_does_not_run_show_and_off_plan_file_is_deleted(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=pse_plan_stream())
    out = engine.outline(manifest, "off")
    assert out["plan"]["ok"] and engine.n("show") == 0
    plan_call = next(c for c in engine.calls if "plan" in c)
    assert "-destroy" in plan_call and "-out=.switchboard-off.tfplan" in plan_call
    assert not list((store.checkout_dir(manifest.id) / "terraform").glob("*.tfplan"))


def test_failed_plan_is_empty_planned_register_with_scrubbed_error(env) -> None:
    store, providers, manifest, root = env
    diag = json.dumps({"type": "diagnostic", "diagnostic": {"severity": "error", "summary": "No valid credential sources found", "detail": "tried AKIAIOSFODNN7EXAMPLE / s3cr3t-value"}})
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=diag, plan_code=1)
    topo = engine.topology(manifest)
    assert topo["register"] == "planned" and topo["nodes"] == [] and topo["edges"] == [] and topo["state"] == "off"
    assert topo["plan"]["resources"] == 0 and topo["plan"]["error"].startswith("tofu plan exited 1: No valid credential sources found")
    assert topo["reason"].startswith("Plan failed: ") and "s3cr3t-value" not in json.dumps(topo) and "AKIAIOSFODNN7EXAMPLE" not in json.dumps(topo)
    assert topo["declared"]["flows"] and engine.n("show") == 0
    assert engine.outline(manifest, "on")["plan"]["ok"] is False and engine.n("plan") == 1


def test_show_failure_keeps_the_outline_and_reports_on_the_drawing(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, show_output="not json at all s3cr3t-value", show_code=0)
    topo = engine.topology(manifest)
    assert topo["register"] == "planned" and topo["nodes"] == [] and topo["plan"]["resources"] == 46
    assert topo["plan"]["error"].startswith("tofu show failed:") and "s3cr3t-value" not in topo["plan"]["error"]
    assert topo["reason"].startswith("Plan could not be drawn: ")
    assert engine.outline(manifest, "on")["plan"]["summary"]["create"] == 46 and engine.n("plan") == 1
    engine2 = _PlanEngine(store, providers, JobRunner(store), root, show_output="Error: no such plan file", show_code=1)
    engine2.invalidate(manifest.id)
    assert "tofu show exited 1" in engine2.topology(manifest)["plan"]["error"]


def test_disconnected_provider_is_declared_register_with_connect_reason_and_declared(env) -> None:
    store, providers, manifest, root = env
    store.delete_provider("aws")
    engine = _PlanEngine(store, providers, JobRunner(store), root)
    topo = engine.topology(manifest)
    assert topo["register"] == "declared" and topo["nodes"] == [] and topo["plan"] is None
    assert topo["reason"].startswith("Connect Amazon Web Services to plan what ON deploys") and "not connected" in topo["reason"]
    assert topo["declared"]["roles"]["zpa-lab-pse"] == "pse" and len(topo["declared"]["flows"]) == 5
    assert engine.calls == []


def test_on_state_is_deployed_register_with_sources(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, providers, manifest, root = env
    store.save_inventory("aws", pse_inventory())
    state = "\n".join(f"{t}.{n}" for t, n, *_ in RESOURCES) + "\n"
    engine = _PlanEngine(store, providers, JobRunner(store), root, state_output=state)
    monkeypatch.setattr(providers["aws"], "inventory", lambda credentials, regions: pytest.fail("no scan expected"))
    topo = engine.topology(manifest)
    assert topo["register"] == "deployed" and topo["state"] == "on" and topo["plan"] is None and topo["reason"] is None
    assert topo["inventory_at"] == "2026-09-05T12:00:00+00:00" and topo["counts"]["instance"] == 5
    nodes = {n["id"]: n for n in topo["nodes"]}
    assert nodes[I_PSE]["source"] == {"path": "terraform/main.tf", "line": 56} and nodes[I_PSE]["address"] == PSE
    assert nodes[IGW_A]["source"] == {"path": "terraform/main.tf", "line": 13} and nodes[NAT_B]["address"] == NAT
    assert "source" not in nodes["i-0mcu"]
    assert engine.n("plan") == 0 and engine.n("show") == 0


def test_error_state_without_resources_is_planned_and_running_job_is_not_planned_against(env) -> None:
    store, providers, manifest, root = env
    jobs = JobRunner(store)
    engine = _PlanEngine(store, providers, jobs, root)
    from app.jobs import Scrubber, StepSpec

    job_id = jobs.start(manifest.id, "on", [StepSpec("sleep", "sleep 0.5")], cwd=root, env={"PATH": "/usr/bin:/bin"}, scrubber=Scrubber())
    topo = engine.topology(manifest)
    assert topo["state"] == "turning_on" and topo["register"] == "planned" and topo["nodes"] == [] and "job is running" in topo["reason"]
    assert engine.n("plan") == 0
    deadline = time.monotonic() + 10
    while jobs.get(job_id)["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    engine.invalidate(manifest.id)
    topo = engine.topology(manifest)  # the job "failed" (no real steps ran under a fake env) with nothing deployed: plan it
    assert topo["state"] in ("error", "off") and topo["register"] == "planned" and topo["counts"]["instance"] == 5


def test_topology_cache_ttl_still_applies_to_the_planned_register(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root)
    engine.topology(manifest)
    import app.usecases.engine as eng_mod

    monkeypatch.setattr(eng_mod, "TOPOLOGY_CACHE_TTL_S", 0)
    time.sleep(0.01)
    engine.topology(manifest)
    assert engine.n("plan") == 1  # topology cache expired, but the plan cache (60 s) still served it
    monkeypatch.setattr(eng_mod, "OUTLINE_CACHE_TTL_S", 0)
    engine.topology(manifest)
    assert engine.n("plan") == 2


# ---------------------------------------------------------------- API
def test_topology_endpoint_without_provider_is_declared_with_connect_reason(logged_in, data_dir, tmp_path: Path) -> None:
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_TOPOLOGY)
    r = logged_in.get("/api/usecases/zpa-private-service-edge/topology")
    assert r.status_code == 200
    body = r.json()
    assert body["register"] == "declared" and body["nodes"] == [] and body["plan"] is None
    assert body["reason"].startswith("Connect Amazon Web Services to plan what ON deploys")
    assert body["declared"]["flows"][0]["via"] == ["nat", "internet"] and body["state"] == "unknown"
    outline = logged_in.get("/api/usecases/zpa-private-service-edge/outline?action=on").json()
    assert outline["plan"]["ok"] is False and "not connected" in outline["plan"]["error"]
