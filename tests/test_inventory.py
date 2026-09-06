"""Richer AWS inventory (v1.1 §A) with a fake EC2 client: every new field, rule flattening,
subnet public/private from the effective route table, EIP association, per-resource cost and
region total == sum of the region's cost lines. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.providers.aws import AwsProvider, _flatten_rules
from app.providers.pricing import HOURS_PER_MONTH, PUBLIC_IPV4_HOURLY_USD, SECRET_MONTHLY_USD

NOW = datetime.now(timezone.utc)
LAUNCHED = NOW - timedelta(hours=49, minutes=30)
TAGS = [{"Key": "Project", "Value": "zpa-pse-lab"}, {"Key": "Name", "Value": "zpa-lab-pse"}]


class _Paginator:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages

    def paginate(self, **_kw: Any):
        yield from self._pages


class _EC2:
    """Just enough of the EC2 API for one region: two VPCs, one public + one private subnet,
    an IGW, a NAT, a running and a stopped instance, two volumes, three EIPs, one SG."""

    def __init__(self, *, ami_missing: bool = False) -> None:
        self.ami_missing = ami_missing
        self.describe_images_calls: list[list[str]] = []
        self.attribute_calls: list[str] = []

    def get_paginator(self, name: str) -> _Paginator:
        return _Paginator({
            "describe_instances": [{"Reservations": [{"Instances": [self._pse(), self._client()]}]}],
            "describe_vpcs": [{"Vpcs": [
                {"VpcId": "vpc-a", "CidrBlock": "10.91.0.0/16", "IsDefault": False, "State": "available", "Tags": TAGS},
                {"VpcId": "vpc-default", "CidrBlock": "172.31.0.0/16", "IsDefault": True, "State": "available"},
            ]}],
            "describe_subnets": [{"Subnets": [
                {"SubnetId": "subnet-pub", "VpcId": "vpc-a", "CidrBlock": "10.91.10.0/24", "AvailabilityZone": "eu-central-1a", "MapPublicIpOnLaunch": True, "AvailableIpAddressCount": 250, "Tags": [{"Key": "Name", "Value": "public"}]},
                {"SubnetId": "subnet-priv", "VpcId": "vpc-a", "CidrBlock": "10.91.20.0/24", "AvailabilityZone": "eu-central-1b", "MapPublicIpOnLaunch": False, "AvailableIpAddressCount": 251},
            ]}],
            "describe_route_tables": [{"RouteTables": [
                {"RouteTableId": "rtb-main", "VpcId": "vpc-a", "Associations": [{"Main": True}], "Tags": [{"Key": "Name", "Value": "main"}],
                 "Routes": [{"DestinationCidrBlock": "10.91.0.0/16", "GatewayId": "local", "State": "active"}, {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "active"}]},
                {"RouteTableId": "rtb-pub", "VpcId": "vpc-a", "Associations": [{"SubnetId": "subnet-pub", "Main": False}],
                 "Routes": [{"DestinationCidrBlock": "10.91.0.0/16", "GatewayId": "local", "State": "active"}, {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1", "State": "active"}]},
            ]}],
            "describe_internet_gateways": [{"InternetGateways": [{"InternetGatewayId": "igw-1", "Attachments": [{"VpcId": "vpc-a", "State": "available"}]}]}],
            "describe_network_interfaces": [{"NetworkInterfaces": [
                {"NetworkInterfaceId": "eni-pse", "VpcId": "vpc-a", "SubnetId": "subnet-pub", "AvailabilityZone": "eu-central-1a", "PrivateIpAddress": "10.91.10.5",
                 "Description": "", "Status": "in-use", "InterfaceType": "interface", "SourceDestCheck": True,
                 "Attachment": {"InstanceId": "i-pse", "DeviceIndex": 0, "Status": "attached"}, "TagSet": [{"Key": "Name", "Value": "zpa-lab-pse-eni"}]},
                {"NetworkInterfaceId": "eni-nat", "VpcId": "vpc-a", "SubnetId": "subnet-pub", "AvailabilityZone": "eu-central-1a", "PrivateIpAddress": "10.91.10.200",
                 "Description": "Interface for NAT Gateway nat-1", "Status": "in-use", "InterfaceType": "nat_gateway", "TagSet": []},
            ]}],
            "list_secrets": [{"SecretList": [
                {"ARN": "arn:aws:secretsmanager:eu-central-1:1:secret:ZS/CC/credentials/aws-lab-zcc-AbCdEf", "Name": "ZS/CC/credentials/aws-lab-zcc",
                 "Description": "Cloud Connector deployment admin", "CreatedDate": LAUNCHED, "LastChangedDate": LAUNCHED, "RotationEnabled": False,
                 "Tags": [{"Key": "Project", "Value": "zcc-workload-lab"}]},
                {"ARN": "arn:aws:secretsmanager:eu-central-1:1:secret:old-Gone", "Name": "old", "DeletedDate": LAUNCHED, "Tags": []},
            ]}],
            "describe_nat_gateways": [{"NatGateways": [
                {"NatGatewayId": "nat-1", "VpcId": "vpc-a", "SubnetId": "subnet-pub", "State": "available", "ConnectivityType": "public", "CreateTime": LAUNCHED,
                 "NatGatewayAddresses": [{"AllocationId": "eipalloc-nat", "PublicIp": "63.1.1.9", "PrivateIp": "10.91.10.200"}], "Tags": TAGS},
                {"NatGatewayId": "nat-gone", "VpcId": "vpc-a", "State": "deleted"},
            ]}],
            "describe_volumes": [{"Volumes": [
                {"VolumeId": "vol-root", "Size": 80, "VolumeType": "gp3", "AvailabilityZone": "eu-central-1a", "Iops": 3000, "Throughput": 125, "Encrypted": True, "State": "in-use", "CreateTime": LAUNCHED,
                 "Attachments": [{"InstanceId": "i-pse", "Device": "/dev/xvda"}]},
                {"VolumeId": "vol-orphan", "Size": 20, "VolumeType": "gp3", "AvailabilityZone": "eu-central-1a", "Encrypted": False, "State": "available", "CreateTime": LAUNCHED, "Attachments": []},
            ]}],
            "describe_security_groups": [{"SecurityGroups": [
                {"GroupId": "sg-pse", "GroupName": "pse", "VpcId": "vpc-a", "Description": "PSE ingress", "Tags": TAGS,
                 "IpPermissions": [
                     {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443, "IpRanges": [{"CidrIp": "63.1.1.9/32"}], "UserIdGroupPairs": [{"GroupId": "sg-other"}]},
                     {"IpProtocol": "-1", "IpRanges": [], "Ipv6Ranges": [{"CidrIpv6": "::/0"}]},
                 ],
                 "IpPermissionsEgress": [{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]},
            ]}],
        }[name])

    @staticmethod
    def _pse() -> dict[str, Any]:
        return {
            "InstanceId": "i-pse", "InstanceType": "m5.large", "State": {"Name": "running"}, "PrivateIpAddress": "10.91.10.5", "PublicIpAddress": "63.1.1.1",
            "LaunchTime": LAUNCHED, "PlatformDetails": "Linux/UNIX", "Architecture": "x86_64", "Placement": {"AvailabilityZone": "eu-central-1a"},
            "VpcId": "vpc-a", "SubnetId": "subnet-pub", "ImageId": "ami-pse", "IamInstanceProfile": {"Arn": "arn:aws:iam::1:instance-profile/zpa-lab-pse-profile"},
            "KeyName": "lab-key", "SecurityGroups": [{"GroupId": "sg-pse", "GroupName": "pse"}], "RootDeviceName": "/dev/xvda",
            "Monitoring": {"State": "disabled"}, "EbsOptimized": True, "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeId": "vol-root"}}],
            "Tags": TAGS,
        }

    @staticmethod
    def _client() -> dict[str, Any]:
        return {
            "InstanceId": "i-client", "InstanceType": "t3.micro", "State": {"Name": "stopped"}, "PrivateIpAddress": "10.91.20.9",
            "LaunchTime": LAUNCHED, "PlatformDetails": "Windows", "Architecture": "x86_64", "Placement": {"AvailabilityZone": "eu-central-1b"},
            "VpcId": "vpc-a", "SubnetId": "subnet-priv", "ImageId": "ami-win", "Monitoring": {"State": "enabled"}, "BlockDeviceMappings": [],
        }

    def describe_images(self, ImageIds: list[str]) -> dict[str, Any]:
        self.describe_images_calls.append(list(ImageIds))
        if self.ami_missing and "ami-win" in ImageIds:
            raise ClientError({"Error": {"Code": "InvalidAMIID.NotFound", "Message": "ami-win"}}, "DescribeImages")
        names = {"ami-pse": "zpa-pse-2026-08", "ami-win": "Windows_Server-2022"}
        return {"Images": [{"ImageId": i, "Name": names[i]} for i in ImageIds if i in names]}

    def describe_addresses(self) -> dict[str, Any]:
        return {"Addresses": [
            {"PublicIp": "63.1.1.1", "AllocationId": "eipalloc-pse", "AssociationId": "eipassoc-1", "InstanceId": "i-pse", "NetworkInterfaceId": "eni-pse", "PrivateIpAddress": "10.91.10.5", "Tags": TAGS},
            {"PublicIp": "63.1.1.9", "AllocationId": "eipalloc-nat", "AssociationId": "eipassoc-2", "NetworkInterfaceId": "eni-nat", "PrivateIpAddress": "10.91.10.200"},
            {"PublicIp": "63.1.1.7", "AllocationId": "eipalloc-idle"},
        ]}

    def describe_instance_attribute(self, InstanceId: str, Attribute: str) -> dict[str, Any]:
        self.attribute_calls.append(InstanceId)
        return {"UserData": {"Value": "IyEvYmluL2Jhc2g="} if InstanceId == "i-pse" else {}}

    def describe_vpc_attribute(self, VpcId: str, Attribute: str) -> dict[str, Any]:
        return {"EnableDnsHostnames": {"Value": VpcId == "vpc-a"}}


class _Secrets:
    """Secrets Manager: only `list_secrets` is used, and only for the cost rollup."""

    def __init__(self, ec2: _EC2, *, denied: bool = False) -> None:
        self.ec2, self.denied = ec2, denied

    def get_paginator(self, name: str) -> _Paginator:
        if self.denied:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "ListSecrets")
        return self.ec2.get_paginator(name)


class _Session:
    def __init__(self, ec2: _EC2, *, secrets_denied: bool = False) -> None:
        self.ec2 = ec2
        self.secrets = _Secrets(ec2, denied=secrets_denied)

    def client(self, service: str, region_name: str, config: Any = None) -> Any:
        assert service in ("ec2", "secretsmanager")
        return self.ec2 if service == "ec2" else self.secrets


class _StubPricing:
    def instance_hourly(self, instance_type: str, region: str, operating_system: str = "Linux") -> float | None:
        return {("m5.large", "Linux"): 0.115, ("t3.micro", "Windows"): 0.02}.get((instance_type, operating_system))

    def nat_hourly(self, region: str) -> float | None:
        return 0.052

    def volume_gb_month(self, region: str, volume_type: str = "gp3") -> float | None:
        return 0.0952


@pytest.fixture
def scanned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[dict[str, Any], _EC2, AwsProvider]:
    ec2 = _EC2()
    monkeypatch.setattr(AwsProvider, "_session", staticmethod(lambda creds: _Session(ec2)))
    provider = AwsProvider(tmp_path / "pricing-cache.json")
    region = provider._scan_region({"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "x"}, "eu-central-1")
    return region, ec2, provider


def test_instance_fields(scanned) -> None:
    region, ec2, _ = scanned
    assert region["error"] is None
    pse = next(i for i in region["instances"] if i["id"] == "i-pse")
    assert pse["ami"] == "ami-pse" and pse["ami_name"] == "zpa-pse-2026-08"
    assert pse["az"] == "eu-central-1a" and pse["platform"] == "Linux/UNIX" and pse["architecture"] == "x86_64"
    assert pse["iam_instance_profile"] == "zpa-lab-pse-profile" and pse["key_name"] == "lab-key"
    assert pse["security_groups"] == [{"id": "sg-pse", "name": "pse"}]
    assert pse["subnet"] == "subnet-pub" and pse["vpc"] == "vpc-a" and pse["root_device"] == "/dev/xvda"
    assert pse["monitoring"] is False and pse["ebs_optimized"] is True
    assert pse["volumes"] == ["vol-root"]
    assert pse["launched"] == LAUNCHED.isoformat()
    assert 49.4 <= pse["uptime_h"] <= 49.6
    assert pse["user_data_present"] is True
    assert pse["tags"] == {"Project": "zpa-pse-lab", "Name": "zpa-lab-pse"} and pse["name"] == "zpa-lab-pse"
    client = next(i for i in region["instances"] if i["id"] == "i-client")
    assert client["uptime_h"] is None and client["monitoring"] is True and client["user_data_present"] is False
    assert client["ami_name"] == "Windows_Server-2022"
    assert ec2.describe_images_calls == [["ami-pse", "ami-win"]]  # batched, sorted, deduplicated
    assert sorted(ec2.attribute_calls) == ["i-client", "i-pse"]


def test_vpc_subnets_routes_and_public_flag(scanned) -> None:
    region, _, _ = scanned
    vpc = next(v for v in region["vpcs"] if v["id"] == "vpc-a")
    assert vpc["igw"] == "igw-1" and vpc["nat_gateways"] == ["nat-1"] and vpc["dns_hostnames"] is True
    subnets = {s["id"]: s for s in vpc["subnets"]}
    assert subnets["subnet-pub"]["public"] is True and subnets["subnet-pub"]["route_table"] == "rtb-pub" and subnets["subnet-pub"]["default_route"] == "igw-1"
    assert subnets["subnet-priv"]["public"] is False and subnets["subnet-priv"]["route_table"] == "rtb-main" and subnets["subnet-priv"]["default_route"] == "nat-1"
    assert subnets["subnet-pub"]["name"] == "public" and subnets["subnet-pub"]["map_public_ip"] is True
    rts = {r["id"]: r for r in vpc["route_tables"]}
    assert rts["rtb-main"]["main"] is True and rts["rtb-main"]["subnets"] == []
    assert rts["rtb-pub"]["subnets"] == ["subnet-pub"] and {"dest": "0.0.0.0/0", "target": "igw-1", "state": "active"} in rts["rtb-pub"]["routes"]
    assert [r["id"] for r in vpc["route_tables"]] == ["rtb-main", "rtb-pub"]
    default = next(v for v in region["vpcs"] if v["id"] == "vpc-default")
    assert default["default"] is True and default["subnets"] == [] and default["igw"] is None and default["dns_hostnames"] is False


def test_nat_eips_volumes_security_groups(scanned) -> None:
    region, _, _ = scanned
    assert [n["id"] for n in region["nat_gateways"]] == ["nat-1"]
    nat = region["nat_gateways"][0]
    assert nat["subnet"] == "subnet-pub" and nat["private_ip"] == "10.91.10.200" and nat["connectivity_type"] == "public"
    assert nat["created"] == LAUNCHED.isoformat() and nat["public_ip"] == "63.1.1.9"

    eips = {e["ip"]: e for e in region["eips"]}
    assert eips["63.1.1.1"]["association"] == {"kind": "instance", "id": "i-pse", "eni": "eni-pse"} and eips["63.1.1.1"]["attached"] is True
    assert eips["63.1.1.9"]["association"] == {"kind": "nat", "id": "nat-1", "eni": "eni-nat"}
    assert eips["63.1.1.7"]["association"] is None and eips["63.1.1.7"]["attached"] is False and eips["63.1.1.7"]["allocation_id"] == "eipalloc-idle"
    assert eips["63.1.1.1"]["instance"] == "i-pse"  # v1 field kept

    vols = {v["id"]: v for v in region["volumes"]}
    assert vols["vol-root"]["attached_to"] == "i-pse" and vols["vol-root"]["device"] == "/dev/xvda" and vols["vol-root"]["iops"] == 3000
    assert vols["vol-root"]["throughput"] == 125 and vols["vol-root"]["encrypted"] is True and vols["vol-root"]["az"] == "eu-central-1a"
    assert vols["vol-orphan"]["attached"] is False and vols["vol-orphan"]["attached_to"] is None and vols["vol-orphan"]["device"] is None
    assert vols["vol-root"]["created"] == LAUNCHED.isoformat()

    assert len(region["security_groups"]) == 1
    sg = region["security_groups"][0]
    assert sg["id"] == "sg-pse" and sg["vpc"] == "vpc-a" and sg["description"] == "PSE ingress" and sg["attached_to"] == ["i-pse"]
    assert sg["ingress"] == [
        {"proto": "tcp", "from": 443, "to": 443, "source": "63.1.1.9/32"},
        {"proto": "tcp", "from": 443, "to": 443, "source": "sg-other"},
        {"proto": "all", "from": None, "to": None, "source": "::/0"},
    ]
    assert sg["egress"] == [{"proto": "all", "from": None, "to": None, "source": "0.0.0.0/0"}]
    assert region["resource_count"] == 2 + 2 + 1 + 3 + 2 + 1  # ENIs and secrets are not drawn resources


def test_network_interfaces_carry_their_instance(scanned) -> None:
    """v1.5: a default route can point at an ENI, and only the ENI record knows whose it is."""
    region, _, _ = scanned
    enis = {e["id"]: e for e in region["network_interfaces"]}
    assert enis["eni-pse"]["instance"] == "i-pse" and enis["eni-pse"]["device_index"] == 0
    assert enis["eni-pse"]["subnet"] == "subnet-pub" and enis["eni-pse"]["vpc"] == "vpc-a"
    assert enis["eni-pse"]["private_ip"] == "10.91.10.5" and enis["eni-pse"]["name"] == "zpa-lab-pse-eni"
    assert enis["eni-pse"]["source_dest_check"] is True and enis["eni-pse"]["status"] == "in-use"
    assert enis["eni-nat"]["instance"] is None and enis["eni-nat"]["interface_type"] == "nat_gateway"


def test_secrets_are_listed_and_priced(scanned) -> None:
    region, _, provider = scanned
    assert [s["name"] for s in region["secrets"]] == ["ZS/CC/credentials/aws-lab-zcc"]  # the deleted one is skipped
    secret = region["secrets"][0]
    assert secret["tags"] == {"Project": "zcc-workload-lab"} and secret["rotation_enabled"] is False
    cost, groups = provider._cost([region], _StubPricing())  # type: ignore[arg-type]
    line = next(l for l in cost["lines"] if l["item"] == "Secrets Manager secret")
    assert line["qty"] == 1 and line["unit"] == "secret-mo" and line["unit_usd"] == SECRET_MONTHLY_USD
    assert line["monthly_usd"] == SECRET_MONTHLY_USD and line["group"] == "Project=zcc-workload-lab"
    assert secret["monthly_usd"] == SECRET_MONTHLY_USD
    assert any("Secrets Manager secret(s)" in n for n in cost["notes"])
    assert next(g for g in groups if g["key"] == "Project=zcc-workload-lab")["monthly_usd"] == SECRET_MONTHLY_USD


def test_secrets_denied_leaves_the_inventory_intact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A role without secretsmanager:ListSecrets still gets a full inventory, minus the line."""
    ec2 = _EC2()
    monkeypatch.setattr(AwsProvider, "_session", staticmethod(lambda creds: _Session(ec2, secrets_denied=True)))
    region = AwsProvider(tmp_path / "c.json")._scan_region({"access_key_id": "a", "secret_access_key": "b"}, "eu-central-1")
    assert region["error"] is None and region["secrets"] == [] and len(region["instances"]) == 2


def test_flatten_rules_prefix_lists_and_empty_sources() -> None:
    rows = _flatten_rules([{"IpProtocol": "udp", "FromPort": 53, "ToPort": 53, "PrefixListIds": [{"PrefixListId": "pl-1"}]}, {"IpProtocol": "icmp", "FromPort": -1, "ToPort": -1}])
    assert rows == [{"proto": "udp", "from": 53, "to": 53, "source": "pl-1"}, {"proto": "icmp", "from": -1, "to": -1, "source": None}]


def test_missing_ami_does_not_sink_the_batch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ec2 = _EC2(ami_missing=True)
    monkeypatch.setattr(AwsProvider, "_session", staticmethod(lambda creds: _Session(ec2)))
    region = AwsProvider(tmp_path / "c.json")._scan_region({"access_key_id": "a", "secret_access_key": "b"}, "eu-central-1")
    names = {i["id"]: i["ami_name"] for i in region["instances"]}
    assert names == {"i-pse": "zpa-pse-2026-08", "i-client": None}
    assert ec2.describe_images_calls == [["ami-pse", "ami-win"], ["ami-pse"], ["ami-win"]]


def test_per_resource_cost_and_region_total_equals_sum_of_lines(scanned) -> None:
    region, _, provider = scanned
    cost, groups = provider._cost([region], _StubPricing())  # type: ignore[arg-type]

    by_id = {i["id"]: i for i in region["instances"]}
    assert by_id["i-pse"]["monthly_usd"] == round(HOURS_PER_MONTH * 0.115, 2)
    assert by_id["i-client"]["monthly_usd"] == 0.0  # stopped: storage only
    vols = {v["id"]: v for v in region["volumes"]}
    assert vols["vol-root"]["monthly_usd"] == round(80 * 0.0952, 2) and vols["vol-orphan"]["monthly_usd"] == round(20 * 0.0952, 2)
    assert region["nat_gateways"][0]["monthly_usd"] == round(HOURS_PER_MONTH * 0.052, 2)
    assert all(e["monthly_usd"] == round(HOURS_PER_MONTH * PUBLIC_IPV4_HOURLY_USD, 2) for e in region["eips"])

    region_lines = [l for l in cost["lines"] if l["region"] == "eu-central-1"]
    assert region["monthly_usd"] == round(sum(l["monthly_usd"] for l in region_lines), 2)
    assert region["monthly_usd"] == cost["monthly_usd"]  # single region: same number
    assert region["monthly_usd"] > 0
    # The resources' own figures add up to the lines they came from.
    compute = sum(i["monthly_usd"] for i in region["instances"])
    storage = sum(v["monthly_usd"] for v in region["volumes"])
    nat = sum(n["monthly_usd"] for n in region["nat_gateways"])
    ips = sum(e["monthly_usd"] for e in region["eips"])
    secrets = sum(s["monthly_usd"] for s in region["secrets"])
    assert round(compute + storage + nat + ips + secrets, 2) == region["monthly_usd"]
    assert {g["key"] for g in groups} == {"Project=zpa-pse-lab", "Project=zcc-workload-lab", "untagged"}


def test_unknown_price_leaves_monthly_null_but_region_total_consistent(scanned) -> None:
    region, _, provider = scanned

    class _NoPrices(_StubPricing):
        def instance_hourly(self, *a: Any, **k: Any) -> float | None:
            return None

    cost, _ = provider._cost([region], _NoPrices())  # type: ignore[arg-type]
    assert next(i for i in region["instances"] if i["id"] == "i-pse")["monthly_usd"] is None
    assert region["monthly_usd"] == round(sum(l["monthly_usd"] for l in cost["lines"]), 2)
    assert any("m5.large" in n for n in cost["notes"])


def test_inventory_shape_is_backward_compatible(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ec2 = _EC2()
    monkeypatch.setattr(AwsProvider, "_session", staticmethod(lambda creds: _Session(ec2)))
    provider = AwsProvider(tmp_path / "c.json")
    monkeypatch.setattr("app.providers.aws.Pricing", lambda *_a, **_k: _StubPricing())
    inv = provider.inventory({"access_key_id": "a", "secret_access_key": "b"}, ["eu-central-1"])
    assert inv["supported"] is True and inv["stale"] is False and inv["generated_at"]
    assert set(inv) >= {"regions", "totals", "groups", "cost"}
    assert inv["totals"]["instances"] == 2 and inv["totals"]["running"] == 1 and inv["totals"]["vpcs"] == 2
    assert inv["totals"]["nat_gateways"] == 1 and inv["totals"]["eips"] == 3 and inv["totals"]["volumes_gb"] == 100
    assert inv["totals"]["security_groups"] == 1 and inv["totals"]["subnets"] == 2
    r = inv["regions"][0]
    assert set(r) >= {"region", "instances", "vpcs", "nat_gateways", "eips", "volumes", "security_groups", "monthly_usd", "resource_count", "error"}
    assert r["monthly_usd"] == inv["cost"]["monthly_usd"]
