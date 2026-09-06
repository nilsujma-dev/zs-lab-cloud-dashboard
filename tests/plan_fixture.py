"""The PSE lab as `tofu plan -json` / `tofu show -json` see it before ON, modelled resource by
resource on the lab repo's `terraform/main.tf` and `terraform/vpc_b.tf` (same types, names,
tags, CIDRs, ports and references). Hand-built: nothing here ran tofu.

Unknown-until-apply attributes (ids, IPs, allocation ids, the NAT-derived cidr) are absent from
`values` and listed in `after_unknown`, exactly as a real plan document has them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLAN_REGION = "eu-central-1"
PROVIDER_NAME = "registry.opentofu.org/hashicorp/aws"

# (type, name, known values, expressions with references, unknown attribute names)
# `expressions` holds only what is not a constant: references and nested blocks.
RESOURCES: list[tuple[str, str, dict[str, Any], dict[str, Any], list[str]]] = [
    # ------------------------------------------------------------ main.tf (VPC A)
    ("aws_vpc", "lab", {"cidr_block": "10.91.0.0/16", "enable_dns_support": True, "enable_dns_hostnames": True, "tags": {"Name": "zpa-lab-vpc-a"}}, {}, ["id", "arn", "default_route_table_id"]),
    ("aws_internet_gateway", "igw", {"tags": {"Name": "zpa-lab-igw"}}, {"vpc_id": {"references": ["aws_vpc.lab.id", "aws_vpc.lab"]}}, ["id", "vpc_id"]),
    ("aws_subnet", "public", {"cidr_block": "10.91.10.0/24", "availability_zone": "eu-central-1a", "map_public_ip_on_launch": True, "tags": {"Name": "zpa-lab-public"}},
     {"vpc_id": {"references": ["aws_vpc.lab.id", "aws_vpc.lab"]}}, ["id", "vpc_id"]),
    # `route` is a nested set *attribute* in the AWS provider: the configuration pools every reference
    # inside the set into one expression, and the planned item omits the unknown target (gateway_id)
    # while carrying "" for the targets it is not. This is the real `tofu show -json` shape.
    ("aws_route_table", "public", {"tags": {"Name": "zpa-lab-public-rt"}, "route": [{"cidr_block": "0.0.0.0/0", "nat_gateway_id": "", "vpc_peering_connection_id": ""}]},
     {"vpc_id": {"references": ["aws_vpc.lab.id", "aws_vpc.lab"]},
      "route": {"references": ["aws_internet_gateway.igw.id", "aws_internet_gateway.igw"]}},
     ["id", "vpc_id"]),
    ("aws_route_table_association", "public", {}, {"subnet_id": {"references": ["aws_subnet.public.id", "aws_subnet.public"]}, "route_table_id": {"references": ["aws_route_table.public.id", "aws_route_table.public"]}}, ["id", "subnet_id", "route_table_id"]),
    ("aws_security_group", "pse", {"name": "zpa-lab-pse", "description": "PSE: accepts 443 from inside the lab VPC only", "tags": {"Name": "zpa-lab-pse"}},
     {"vpc_id": {"references": ["aws_vpc.lab.id", "aws_vpc.lab"]}}, ["id", "vpc_id", "ingress", "egress"]),
    ("aws_vpc_security_group_ingress_rule", "pse_from_vpc_a", {"cidr_ipv4": "10.91.0.0/16", "from_port": 443, "to_port": 443, "ip_protocol": "tcp", "description": "Client Connector and App Connector dial the broker"},
     {"security_group_id": {"references": ["aws_security_group.pse.id", "aws_security_group.pse"]}, "cidr_ipv4": {"references": ["aws_vpc.lab.cidr_block", "aws_vpc.lab"]}}, ["id", "security_group_id"]),
    ("aws_vpc_security_group_egress_rule", "pse_all", {"cidr_ipv4": "0.0.0.0/0", "ip_protocol": "-1"}, {"security_group_id": {"references": ["aws_security_group.pse.id", "aws_security_group.pse"]}}, ["id", "security_group_id"]),
    ("aws_security_group", "connector", {"name": "zpa-lab-connector", "description": "App Connector: dials outbound only, accepts nothing", "tags": {"Name": "zpa-lab-connector"},
                                         "egress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"], "security_groups": []}]},
     {"vpc_id": {"references": ["aws_vpc.lab.id", "aws_vpc.lab"]},
      "egress": [{"from_port": {"constant_value": 0}, "to_port": {"constant_value": 0}, "protocol": {"constant_value": "-1"}, "cidr_blocks": {"constant_value": ["0.0.0.0/0"]}}]},
     ["id", "vpc_id", "ingress"]),
    ("aws_iam_role", "node", {"name": "zpa-lab-node"}, {"assume_role_policy": {"references": ["data.aws_iam_policy_document.assume.json", "data.aws_iam_policy_document.assume"]}}, ["id", "arn", "assume_role_policy"]),
    ("aws_iam_role_policy", "ssm_read", {"name": "read-provisioning-keys"}, {"role": {"references": ["aws_iam_role.node.id", "aws_iam_role.node"]}}, ["id", "role", "policy"]),
    ("aws_iam_role_policy_attachment", "ssm_core", {"policy_arn": "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}, {"role": {"references": ["aws_iam_role.node.name", "aws_iam_role.node"]}}, ["id", "role"]),
    ("aws_iam_instance_profile", "node", {"name": "zpa-lab-node"}, {"role": {"references": ["aws_iam_role.node.name", "aws_iam_role.node"]}}, ["id", "arn", "role"]),
    ("aws_instance", "pse", {"ami": "ami-07811cc3852902146", "instance_type": "m5.large", "user_data": "#!/bin/bash\n" + "x" * 2000, "tags": {"Name": "zpa-lab-pse"},
                             "root_block_device": [{"volume_size": 80, "volume_type": "gp3", "encrypted": True}], "metadata_options": [{"http_tokens": "required"}]},
     {"subnet_id": {"references": ["aws_subnet.public.id", "aws_subnet.public"]},
      "vpc_security_group_ids": {"references": ["aws_security_group.pse.id", "aws_security_group.pse"]},
      "iam_instance_profile": {"references": ["aws_iam_instance_profile.node.name", "aws_iam_instance_profile.node"]},
      "ami": {"references": ["var.pse_ami"]}},
     ["id", "arn", "private_ip", "public_ip", "subnet_id", "vpc_security_group_ids", "availability_zone", "iam_instance_profile", "primary_network_interface_id"]),
    ("aws_eip", "pse", {"domain": "vpc", "tags": {"Name": "zpa-lab-pse-eip"}}, {"instance": {"references": ["aws_instance.pse.id", "aws_instance.pse"]}}, ["id", "allocation_id", "public_ip", "private_ip", "instance"]),
    ("aws_instance", "connector", {"ami": "ami-0aaa3d92aff1df4a0", "instance_type": "t3.medium", "user_data": "#!/bin/bash\n" + "y" * 2000, "tags": {"Name": "zpa-lab-connector"},
                                   "root_block_device": [{"volume_size": 80, "volume_type": "gp3", "encrypted": True}], "metadata_options": [{"http_tokens": "required"}]},
     {"subnet_id": {"references": ["aws_subnet.public.id", "aws_subnet.public"]},
      "vpc_security_group_ids": {"references": ["aws_security_group.connector.id", "aws_security_group.connector"]},
      "iam_instance_profile": {"references": ["aws_iam_instance_profile.node.name", "aws_iam_instance_profile.node"]},
      "ami": {"references": ["var.conn_ami"]}},
     ["id", "arn", "private_ip", "public_ip", "subnet_id", "vpc_security_group_ids", "availability_zone"]),
    # ------------------------------------------------------------ vpc_b.tf (VPC B)
    ("aws_vpc", "b", {"cidr_block": "10.90.0.0/16", "enable_dns_support": True, "enable_dns_hostnames": True, "tags": {"Name": "zpa-lab-vpc-b"}}, {}, ["id", "arn", "default_route_table_id"]),
    ("aws_internet_gateway", "b", {"tags": {"Name": "zpa-lab-b-igw"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}}, ["id", "vpc_id"]),
    ("aws_subnet", "b_public", {"cidr_block": "10.90.0.0/24", "availability_zone": "eu-central-1a", "tags": {"Name": "zpa-lab-b-public"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}}, ["id", "vpc_id"]),
    ("aws_subnet", "priv", {"cidr_block": "10.90.20.0/24", "availability_zone": "eu-central-1a", "tags": {"Name": "zpa-lab-priv"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}}, ["id", "vpc_id"]),
    ("aws_subnet", "mcu", {"cidr_block": "10.90.30.0/24", "availability_zone": "eu-central-1a", "tags": {"Name": "zpa-lab-mcu"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}}, ["id", "vpc_id"]),
    ("aws_eip", "nat", {"domain": "vpc", "tags": {"Name": "zpa-lab-nat-eip"}}, {}, ["id", "allocation_id", "public_ip", "private_ip", "instance"]),
    ("aws_nat_gateway", "b", {"connectivity_type": "public", "tags": {"Name": "zpa-lab-nat"}},
     {"allocation_id": {"references": ["aws_eip.nat.id", "aws_eip.nat"]}, "subnet_id": {"references": ["aws_subnet.b_public.id", "aws_subnet.b_public"]}}, ["id", "allocation_id", "subnet_id", "public_ip", "private_ip"]),
    ("aws_route_table", "b_public", {"tags": {"Name": "zpa-lab-b-public-rt"}, "route": [{"cidr_block": "0.0.0.0/0", "nat_gateway_id": "", "vpc_peering_connection_id": ""}]},
     {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]},
      "route": {"references": ["aws_internet_gateway.b.id", "aws_internet_gateway.b"]}}, ["id", "vpc_id"]),
    ("aws_route_table", "b_private", {"tags": {"Name": "zpa-lab-b-private-rt"}, "route": [{"cidr_block": "0.0.0.0/0", "gateway_id": "", "vpc_peering_connection_id": ""}]},
     {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]},
      "route": {"references": ["aws_nat_gateway.b.id", "aws_nat_gateway.b"]}}, ["id", "vpc_id"]),
    ("aws_route_table_association", "b_public", {}, {"subnet_id": {"references": ["aws_subnet.b_public.id", "aws_subnet.b_public"]}, "route_table_id": {"references": ["aws_route_table.b_public.id", "aws_route_table.b_public"]}}, ["id"]),
    ("aws_route_table_association", "priv", {}, {"subnet_id": {"references": ["aws_subnet.priv.id", "aws_subnet.priv"]}, "route_table_id": {"references": ["aws_route_table.b_private.id", "aws_route_table.b_private"]}}, ["id"]),
    ("aws_route_table_association", "mcu", {}, {"subnet_id": {"references": ["aws_subnet.mcu.id", "aws_subnet.mcu"]}, "route_table_id": {"references": ["aws_route_table.b_private.id", "aws_route_table.b_private"]}}, ["id"]),
    ("aws_network_acl", "priv", {"tags": {"Name": "zpa-lab-priv-nacl"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}, "subnet_ids": {"references": ["aws_subnet.priv.id", "aws_subnet.priv"]}}, ["id"]),
    ("aws_network_acl_rule", "priv_in_deny_mcu", {"rule_number": 100, "egress": False, "protocol": "-1", "rule_action": "deny", "cidr_block": "10.90.30.0/24"}, {"network_acl_id": {"references": ["aws_network_acl.priv.id", "aws_network_acl.priv"]}}, ["id"]),
    ("aws_network_acl_rule", "priv_in_allow", {"rule_number": 200, "egress": False, "protocol": "-1", "rule_action": "allow", "cidr_block": "0.0.0.0/0"}, {"network_acl_id": {"references": ["aws_network_acl.priv.id", "aws_network_acl.priv"]}}, ["id"]),
    ("aws_network_acl_rule", "priv_out_deny_mcu", {"rule_number": 100, "egress": True, "protocol": "-1", "rule_action": "deny", "cidr_block": "10.90.30.0/24"}, {"network_acl_id": {"references": ["aws_network_acl.priv.id", "aws_network_acl.priv"]}}, ["id"]),
    ("aws_network_acl_rule", "priv_out_allow", {"rule_number": 200, "egress": True, "protocol": "-1", "rule_action": "allow", "cidr_block": "0.0.0.0/0"}, {"network_acl_id": {"references": ["aws_network_acl.priv.id", "aws_network_acl.priv"]}}, ["id"]),
    ("aws_network_acl", "mcu", {"tags": {"Name": "zpa-lab-mcu-nacl"}}, {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]}, "subnet_ids": {"references": ["aws_subnet.mcu.id", "aws_subnet.mcu"]}}, ["id"]),
    ("aws_network_acl_rule", "mcu_in_deny_priv", {"rule_number": 100, "egress": False, "protocol": "-1", "rule_action": "deny", "cidr_block": "10.90.20.0/24"}, {"network_acl_id": {"references": ["aws_network_acl.mcu.id", "aws_network_acl.mcu"]}}, ["id"]),
    ("aws_network_acl_rule", "mcu_in_allow", {"rule_number": 200, "egress": False, "protocol": "-1", "rule_action": "allow", "cidr_block": "0.0.0.0/0"}, {"network_acl_id": {"references": ["aws_network_acl.mcu.id", "aws_network_acl.mcu"]}}, ["id"]),
    ("aws_network_acl_rule", "mcu_out_deny_priv", {"rule_number": 100, "egress": True, "protocol": "-1", "rule_action": "deny", "cidr_block": "10.90.20.0/24"}, {"network_acl_id": {"references": ["aws_network_acl.mcu.id", "aws_network_acl.mcu"]}}, ["id"]),
    ("aws_network_acl_rule", "mcu_out_allow", {"rule_number": 200, "egress": True, "protocol": "-1", "rule_action": "allow", "cidr_block": "0.0.0.0/0"}, {"network_acl_id": {"references": ["aws_network_acl.mcu.id", "aws_network_acl.mcu"]}}, ["id"]),
    ("aws_security_group", "priv_connector", {"name": "zpa-lab-priv-connector", "description": "PRIV App Connector: dials outbound only, accepts nothing", "tags": {"Name": "zpa-lab-priv-connector"},
                                              "egress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"], "security_groups": []}]},
     {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]},
      "egress": [{"from_port": {"constant_value": 0}, "to_port": {"constant_value": 0}, "protocol": {"constant_value": "-1"}, "cidr_blocks": {"constant_value": ["0.0.0.0/0"]}}]}, ["id", "vpc_id", "ingress"]),
    # `ingress` references another group's id, so the whole set is unknown in planned values.
    ("aws_security_group", "server", {"name": "zpa-lab-server", "description": "nginx: reachable ONLY from the PRIV App Connector. MCU is deliberately absent.", "tags": {"Name": "zpa-lab-server"},
                                      "egress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"], "security_groups": []}]},
     {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]},
      "ingress": [{"description": {"constant_value": "brokered app traffic from the connector only"}, "from_port": {"constant_value": 8080}, "to_port": {"constant_value": 8080},
                   "protocol": {"constant_value": "tcp"}, "security_groups": {"references": ["aws_security_group.priv_connector.id", "aws_security_group.priv_connector"]}}],
      "egress": [{"from_port": {"constant_value": 0}, "to_port": {"constant_value": 0}, "protocol": {"constant_value": "-1"}, "cidr_blocks": {"constant_value": ["0.0.0.0/0"]}}]}, ["id", "vpc_id", "ingress"]),
    ("aws_security_group", "mcu_client", {"name": "zpa-lab-mcu-client", "description": "Operator client: outbound only. No inbound, no path to PRIV.", "tags": {"Name": "zpa-lab-mcu-client"},
                                          "egress": [{"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"], "security_groups": []}]},
     {"vpc_id": {"references": ["aws_vpc.b.id", "aws_vpc.b"]},
      "egress": [{"from_port": {"constant_value": 0}, "to_port": {"constant_value": 0}, "protocol": {"constant_value": "-1"}, "cidr_blocks": {"constant_value": ["0.0.0.0/0"]}}]}, ["id", "vpc_id", "ingress"]),
    ("aws_instance", "priv_connector", {"ami": "ami-0aaa3d92aff1df4a0", "instance_type": "t3.medium", "user_data": "#!/bin/bash\n" + "z" * 2000, "tags": {"Name": "zpa-lab-priv-connector"},
                                        "root_block_device": [{"volume_size": 80, "volume_type": "gp3", "encrypted": True}], "metadata_options": [{"http_tokens": "required"}]},
     {"subnet_id": {"references": ["aws_subnet.priv.id", "aws_subnet.priv"]},
      "vpc_security_group_ids": {"references": ["aws_security_group.priv_connector.id", "aws_security_group.priv_connector"]},
      "iam_instance_profile": {"references": ["aws_iam_instance_profile.node.name", "aws_iam_instance_profile.node"]},
      "ami": {"references": ["var.conn_ami"]}},
     ["id", "arn", "private_ip", "public_ip", "subnet_id", "vpc_security_group_ids", "availability_zone"]),
    ("aws_instance", "server", {"instance_type": "t3.micro", "user_data": "#!/bin/bash\ndnf install -y nginx\n", "tags": {"Name": "zpa-lab-server"},
                                "root_block_device": [{"volume_size": 8, "volume_type": "gp3", "encrypted": True}], "metadata_options": [{"http_tokens": "required"}]},
     {"subnet_id": {"references": ["aws_subnet.priv.id", "aws_subnet.priv"]},
      "vpc_security_group_ids": {"references": ["aws_security_group.server.id", "aws_security_group.server"]},
      "iam_instance_profile": {"references": ["aws_iam_instance_profile.node.name", "aws_iam_instance_profile.node"]},
      "ami": {"references": ["data.aws_ami.al2023.id", "data.aws_ami.al2023"]}},
     ["id", "arn", "ami", "private_ip", "public_ip", "subnet_id", "vpc_security_group_ids", "availability_zone"]),
    ("aws_instance", "mcu_client", {"instance_type": "t3.medium", "get_password_data": False, "tags": {"Name": "zpa-lab-mcu-client"},
                                    "root_block_device": [{"volume_size": 50, "volume_type": "gp3", "encrypted": True}], "metadata_options": [{"http_tokens": "required"}]},
     {"subnet_id": {"references": ["aws_subnet.mcu.id", "aws_subnet.mcu"]},
      "vpc_security_group_ids": {"references": ["aws_security_group.mcu_client.id", "aws_security_group.mcu_client"]},
      "iam_instance_profile": {"references": ["aws_iam_instance_profile.node.name", "aws_iam_instance_profile.node"]},
      "ami": {"references": ["data.aws_ami.windows.id", "data.aws_ami.windows"]}},
     ["id", "arn", "ami", "private_ip", "public_ip", "subnet_id", "vpc_security_group_ids", "availability_zone"]),
    # The cross-VPC rule: its source is "${aws_eip.nat.public_ip}/32", unknown until apply.
    ("aws_vpc_security_group_ingress_rule", "pse_from_vpc_b", {"from_port": 443, "to_port": 443, "ip_protocol": "tcp", "description": "PRIV connector and MCU client, arriving via VPC B NAT"},
     {"security_group_id": {"references": ["aws_security_group.pse.id", "aws_security_group.pse"]}, "cidr_ipv4": {"references": ["aws_eip.nat.public_ip", "aws_eip.nat"]}}, ["id", "security_group_id", "cidr_ipv4"]),
]
# A spare, unattached address: not in the lab today, but it exercises the idle-EIP path the
# live fixture covers (test_topology's eipalloc-0idle). Appended last so counts stay honest.
SPARE_EIP = ("aws_eip", "spare", {"domain": "vpc", "tags": {"Name": "zpa-lab-spare"}}, {}, ["id", "allocation_id", "public_ip"])

DATA_SOURCES = [
    ("aws_ami", "al2023", {"most_recent": True, "owners": ["amazon"], "id": "ami-0al2023", "name": "al2023-ami-2023.6-x86_64"}),
    ("aws_ami", "windows", {"most_recent": True, "owners": ["amazon"], "id": "ami-0win2022", "name": "Windows_Server-2022-English-Full-Base-2026.08"}),
    ("aws_iam_policy_document", "assume", {"json": "{}"}),
    ("aws_caller_identity", "current", {"account_id": "257300000000"}),
]

LAB_COUNT = len(RESOURCES)  # 45: every managed resource block in main.tf + vpc_b.tf


def _constants(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in values.items():
        if isinstance(v, list) and v and all(isinstance(i, dict) for i in v):
            out[k] = [{kk: {"constant_value": vv} for kk, vv in item.items()} for item in v]
        else:
            out[k] = {"constant_value": v}
    return out


def pse_show(*, spare_eip: bool = True) -> dict[str, Any]:
    """A `tofu show -json <planfile>` document for ON from an empty state."""
    resources = list(RESOURCES) + ([SPARE_EIP] if spare_eip else [])
    planned, changes, config = [], [], []
    for rtype, name, values, exprs, unknown in resources:
        addr = f"{rtype}.{name}"
        planned.append({"address": addr, "mode": "managed", "type": rtype, "name": name, "provider_name": PROVIDER_NAME, "schema_version": 1,
                        "values": dict(values), "sensitive_values": {}})
        changes.append({"address": addr, "mode": "managed", "type": rtype, "name": name, "provider_name": PROVIDER_NAME,
                        "change": {"actions": ["create"], "before": None, "after": dict(values), "after_unknown": {u: True for u in unknown},
                                   "before_sensitive": False, "after_sensitive": {}}})
        expressions = _constants({k: v for k, v in values.items() if k not in exprs})
        expressions.update(exprs)
        config.append({"address": addr, "mode": "managed", "type": rtype, "name": name, "provider_config_key": "aws", "expressions": expressions, "schema_version": 1})
    for dtype, name, values in DATA_SOURCES:
        config.append({"address": f"data.{dtype}.{name}", "mode": "data", "type": dtype, "name": name, "provider_config_key": "aws", "expressions": _constants(values), "schema_version": 0})
    prior = [{"address": f"data.{dtype}.{name}", "mode": "data", "type": dtype, "name": name, "provider_name": PROVIDER_NAME, "schema_version": 0, "values": values, "sensitive_values": {}}
             for dtype, name, values in DATA_SOURCES]
    return {
        "format_version": "1.2",
        "terraform_version": "1.12.6",
        "planned_values": {"root_module": {"resources": planned}},
        "resource_changes": changes,
        "prior_state": {"format_version": "1.0", "terraform_version": "1.12.6", "values": {"root_module": {"resources": prior}}},
        "configuration": {
            "provider_config": {"aws": {"name": "aws", "full_name": PROVIDER_NAME, "version_constraint": "~> 5.0",
                                        "expressions": {"region": {"constant_value": PLAN_REGION}, "default_tags": [{"tags": {"constant_value": {"Project": "zpa-pse-lab", "ManagedBy": "opentofu", "Owner": "nujma"}}}]}}},
            "root_module": {"resources": config, "variables": {"pse_ami": {"default": "ami-07811cc3852902146"}, "conn_ami": {"default": "ami-0aaa3d92aff1df4a0"}, "region": {"default": PLAN_REGION}}},
        },
        "timestamp": "2026-09-06T10:00:00Z",
        "applyable": True,
        "complete": True,
        "errored": False,
    }


def pse_plan_stream(*, spare_eip: bool = True) -> str:
    """The matching `tofu plan -json` line stream: one planned_change per resource + the summary."""
    resources = list(RESOURCES) + ([SPARE_EIP] if spare_eip else [])
    lines = [json.dumps({"@level": "info", "@message": "OpenTofu 1.12.6", "type": "version", "tofu": "1.12.6", "ui": "1.2"})]
    for rtype, name, _values, _exprs, _unknown in resources:
        addr = f"{rtype}.{name}"
        lines.append(json.dumps({"@level": "info", "@message": f"{addr}: Plan to create", "type": "planned_change",
                                 "change": {"resource": {"addr": addr, "module": "", "resource": addr, "implied_provider": "aws", "resource_type": rtype, "resource_name": name, "resource_key": None}, "action": "create"}}))
    lines.append(json.dumps({"@level": "info", "@message": f"Plan: {len(resources)} to add, 0 to change, 0 to destroy.", "type": "change_summary",
                             "changes": {"add": len(resources), "change": 0, "import": 0, "remove": 0, "operation": "plan"}}))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- a checkout with the resource blocks
MAIN_TF = '''terraform {
  required_version = ">= 1.6"
}
provider "aws" {
  region = "eu-central-1"
}

# ---------------------------------------------------------------- network
resource "aws_vpc" "lab" {
  cidr_block           = "10.91.0.0/16"
  tags                 = { Name = "zpa-lab-vpc-a" }
}
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.lab.id
  tags   = { Name = "zpa-lab-igw" }
}
resource "aws_subnet" "public" {
  vpc_id     = aws_vpc.lab.id
  cidr_block = "10.91.10.0/24"
  tags       = { Name = "zpa-lab-public" }
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.lab.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "zpa-lab-public-rt" }
}
resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}
resource "aws_security_group" "pse" {
  name        = "zpa-lab-pse"
  description = "PSE: accepts 443 { from inside } the lab VPC only" # braces in a string
  vpc_id      = aws_vpc.lab.id
  tags        = { Name = "zpa-lab-pse" }
}
resource "aws_vpc_security_group_ingress_rule" "pse_from_vpc_a" {
  security_group_id = aws_security_group.pse.id
  cidr_ipv4         = aws_vpc.lab.cidr_block
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

locals {
  bootstrap = <<-BASH
    #!/bin/bash
    SVC="__SERVICE__"; PARAM="__PARAM__"
    if [ -n "$${KEY:-}" ]; then echo "{unbalanced"; fi
  BASH
}

resource "aws_instance" "pse" {
  ami                    = var.pse_ami
  instance_type          = "m5.large"
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.pse.id]
  user_data = replace(local.bootstrap, "__SERVICE__", "zpa-service-edge")
  metadata_options { http_tokens = "required" }
  tags = { Name = "zpa-lab-pse" }
}
resource "aws_eip" "pse" {
  instance = aws_instance.pse.id
  domain   = "vpc"
  tags     = { Name = "zpa-lab-pse-eip" }
}
resource "aws_instance" "connector" {
  ami           = var.conn_ami
  instance_type = "t3.medium"
  subnet_id     = aws_subnet.public.id
  tags          = { Name = "zpa-lab-connector" }
}
'''

VPC_B_TF = '''# VPC B - the private network.
resource "aws_vpc" "b" {
  cidr_block = "10.90.0.0/16"
  tags       = { Name = "zpa-lab-vpc-b" }
}

resource "aws_internet_gateway" "b" {
  vpc_id = aws_vpc.b.id
  tags   = { Name = "zpa-lab-b-igw" }
}

resource "aws_subnet" "b_public" {
  vpc_id     = aws_vpc.b.id
  cidr_block = "10.90.0.0/24"
  tags       = { Name = "zpa-lab-b-public" }
}

resource "aws_subnet" "priv" {
  vpc_id     = aws_vpc.b.id
  cidr_block = "10.90.20.0/24"
  tags       = { Name = "zpa-lab-priv" }
}

resource "aws_subnet" "mcu" {
  vpc_id     = aws_vpc.b.id
  cidr_block = "10.90.30.0/24"
  tags       = { Name = "zpa-lab-mcu" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "zpa-lab-nat-eip" }
}

resource "aws_nat_gateway" "b" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.b_public.id
  tags          = { Name = "zpa-lab-nat" }
}

resource "aws_instance" "server" {
  instance_type = "t3.micro"
  subnet_id     = aws_subnet.priv.id
  user_data     = <<-BASH
    #!/bin/bash
    cat > /usr/share/nginx/html/index.html <<'HTML'
    <h1>zpa-lab PRIV server</h1>
    HTML
  BASH
  tags = { Name = "zpa-lab-server" }
}
'''
# Line numbers of the `resource` lines above, checked by the tests (1-based).
SOURCE_LINES = {
    "aws_vpc.lab": ("terraform/main.tf", 9),
    "aws_internet_gateway.igw": ("terraform/main.tf", 13),
    "aws_subnet.public": ("terraform/main.tf", 17),
    "aws_route_table.public": ("terraform/main.tf", 22),
    "aws_route_table_association.public": ("terraform/main.tf", 30),
    "aws_security_group.pse": ("terraform/main.tf", 34),
    "aws_vpc_security_group_ingress_rule.pse_from_vpc_a": ("terraform/main.tf", 40),
    "aws_instance.pse": ("terraform/main.tf", 56),
    "aws_eip.pse": ("terraform/main.tf", 65),
    "aws_instance.connector": ("terraform/main.tf", 70),
    "aws_vpc.b": ("terraform/vpc_b.tf", 2),
    "aws_internet_gateway.b": ("terraform/vpc_b.tf", 7),
    "aws_subnet.b_public": ("terraform/vpc_b.tf", 12),
    "aws_subnet.priv": ("terraform/vpc_b.tf", 18),
    "aws_subnet.mcu": ("terraform/vpc_b.tf", 24),
    "aws_eip.nat": ("terraform/vpc_b.tf", 30),
    "aws_nat_gateway.b": ("terraform/vpc_b.tf", 35),
    "aws_instance.server": ("terraform/vpc_b.tf", 41),
}


def write_checkout(checkout: Path) -> Path:
    """A minimal checkout (with a `.git` marker) holding the two terraform files."""
    tf = checkout / "terraform"
    tf.mkdir(parents=True, exist_ok=True)
    (checkout / ".git").mkdir(exist_ok=True)
    (tf / "main.tf").write_text(MAIN_TF, encoding="utf-8")
    (tf / "vpc_b.tf").write_text(VPC_B_TF, encoding="utf-8")
    (tf / "backend.tf").write_text('terraform {\n  backend "s3" {}\n}\n', encoding="utf-8")
    return checkout
