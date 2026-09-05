"""AWS provider: connection checklist, remote-state bucket, parallel inventory, cost."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.providers.base import Check, ConnectionReport, ConnectResult, FormError, FormField, Identity, Provider
from app.providers.pricing import HOURS_PER_MONTH, PUBLIC_IPV4_HOURLY_USD, Pricing, platform_to_operating_system
from app.store import utcnow_iso

log = logging.getLogger("switchboard.aws")

HOME_REGION = "eu-central-1"
PRICING_REGION = "us-east-1"
STATE_BUCKET_PREFIX = "zs-lab-tfstate-"
GROUP_TAG = "Project"
UNTAGGED_GROUP = "untagged"
MAX_SCAN_WORKERS = 16
AMI_BATCH = 100
_KEY_ID_RE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
_BOTO_CONFIG = Config(retries={"max_attempts": 4, "mode": "standard"}, connect_timeout=10, read_timeout=30)

CHECK_STS = "Credentials valid (STS)"
CHECK_REGIONS = "Can list regions"
CHECK_EC2 = f"Can describe EC2 in {HOME_REGION}"
CHECK_PRICING = "Pricing API reachable"
CHECK_BUCKET = "State bucket ready"
CHECK_EXPIRY = "Session token expiry"


def _err(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        text = f"{error.get('Code', 'Error')}: {error.get('Message', '')}".strip(": ")
    else:
        text = f"{type(exc).__name__}: {exc}"
    return _KEY_ID_RE.sub("<redacted>", text)


def _tags(obj: dict[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in obj.get("Tags", []) or [] if "Key" in t}


def _round(value: float) -> float:
    return round(value + 1e-9, 2)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _uptime_hours(launched: Any, now: datetime) -> float | None:
    if not isinstance(launched, datetime):
        return None
    if launched.tzinfo is None:
        launched = launched.replace(tzinfo=timezone.utc)
    return round(max(0.0, (now - launched).total_seconds() / 3600), 1)


def _route_target(route: dict[str, Any]) -> str | None:
    """The one id a route points at, whichever field EC2 used for it."""
    for key in (
        "GatewayId", "NatGatewayId", "TransitGatewayId", "VpcPeeringConnectionId", "NetworkInterfaceId",
        "InstanceId", "EgressOnlyInternetGatewayId", "LocalGatewayId", "CarrierGatewayId", "CoreNetworkArn",
    ):
        if route.get(key):
            return str(route[key])
    return None


def _route_dest(route: dict[str, Any]) -> str | None:
    return route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock") or route.get("DestinationPrefixListId")


def _flatten_rules(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """IpPermissions -> one row per source: {proto, from, to, source} (source = CIDR, sg-…, or pl-…)."""
    rows: list[dict[str, Any]] = []
    for perm in permissions or []:
        proto = perm.get("IpProtocol", "-1")
        proto = "all" if proto in ("-1", -1) else str(proto)
        frm, to = perm.get("FromPort"), perm.get("ToPort")
        sources: list[str] = []
        sources += [r["CidrIp"] for r in perm.get("IpRanges", []) or [] if r.get("CidrIp")]
        sources += [r["CidrIpv6"] for r in perm.get("Ipv6Ranges", []) or [] if r.get("CidrIpv6")]
        sources += [r["GroupId"] for r in perm.get("UserIdGroupPairs", []) or [] if r.get("GroupId")]
        sources += [r["PrefixListId"] for r in perm.get("PrefixListIds", []) or [] if r.get("PrefixListId")]
        for source in sources or [None]:
            rows.append({"proto": proto, "from": frm, "to": to, "source": source})
    return rows


class AwsProvider(Provider):
    id = "aws"
    name = "Amazon Web Services"
    capabilities = {"inventory": True, "usecases": True}

    def __init__(self, pricing_cache_path: Path) -> None:
        self._pricing_cache_path = pricing_cache_path

    # ------------------------------------------------------------------ form
    def form_fields(self) -> list[FormField]:
        return [
            FormField("access_key_id", "Access key ID", "text", True, "AKIA… (long-lived) or ASIA… (SSO session)"),
            FormField("secret_access_key", "Secret access key", "password", True, "Never stored in clear; Fernet-encrypted at rest"),
            FormField("session_token", "Session token", "textarea", False, "Required with ASIA… keys from an SSO session"),
        ]

    def identity_label(self, identity: dict[str, Any] | None) -> str | None:
        if not identity:
            return None
        return identity.get("alias") or identity.get("account")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _session(credentials: dict[str, Any]) -> boto3.Session:
        return boto3.Session(
            aws_access_key_id=credentials["access_key_id"],
            aws_secret_access_key=credentials["secret_access_key"],
            aws_session_token=credentials.get("session_token") or None,
        )

    @staticmethod
    def _client(session: boto3.Session, service: str, region: str) -> Any:
        return session.client(service, region_name=region, config=_BOTO_CONFIG)

    def credential_env(self, credentials: dict[str, Any]) -> dict[str, str]:
        env = {
            "AWS_ACCESS_KEY_ID": str(credentials["access_key_id"]),
            "AWS_SECRET_ACCESS_KEY": str(credentials["secret_access_key"]),
        }
        if credentials.get("session_token"):
            env["AWS_SESSION_TOKEN"] = str(credentials["session_token"])
        return env

    def state_bucket(self, provider_record: dict[str, Any]) -> str | None:
        account = (provider_record.get("identity") or {}).get("account")
        return f"{STATE_BUCKET_PREFIX}{account}" if account else None

    # ------------------------------------------------------------------ connect
    def connect(self, credentials: dict[str, Any], regions: list[str] | None) -> ConnectResult:
        checks: list[Check] = []
        session = self._session(credentials)
        identity: Identity | None = None
        enabled_regions: list[str] = []
        scan_regions: list[str] = []

        # 1. STS
        try:
            ident = self._client(session, "sts", HOME_REGION).get_caller_identity()
            identity = Identity(account=ident["Account"], arn=ident["Arn"], alias=None)
            checks.append(Check(CHECK_STS, True, ident["Arn"].split(":", 5)[-1]))
        except (ClientError, BotoCoreError, KeyError) as exc:
            checks.append(Check(CHECK_STS, False, _err(exc)))
            for name in (CHECK_REGIONS, CHECK_EC2, CHECK_PRICING, CHECK_BUCKET, CHECK_EXPIRY):
                checks.append(Check(name, False, "Skipped: credentials not valid", required=name != CHECK_PRICING))
            return ConnectResult(ConnectionReport(False, None, checks), [], None)

        # 2. Regions
        try:
            resp = self._client(session, "ec2", HOME_REGION).describe_regions(AllRegions=False)
            enabled_regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
            if regions:
                unknown = sorted(set(regions) - set(enabled_regions))
                if unknown:
                    checks.append(Check(CHECK_REGIONS, False, f"not enabled in this account: {', '.join(unknown)}"))
                else:
                    scan_regions = sorted(set(regions))
                    checks.append(Check(CHECK_REGIONS, True, f"{len(enabled_regions)} enabled, {len(scan_regions)} selected"))
            else:
                scan_regions = enabled_regions
                checks.append(Check(CHECK_REGIONS, True, f"{len(enabled_regions)} enabled"))
        except (ClientError, BotoCoreError) as exc:
            checks.append(Check(CHECK_REGIONS, False, _err(exc)))

        # 3. EC2 in the home region
        try:
            self._client(session, "ec2", HOME_REGION).describe_instances(MaxResults=5)
            checks.append(Check(CHECK_EC2, True, ""))
        except (ClientError, BotoCoreError) as exc:
            checks.append(Check(CHECK_EC2, False, _err(exc)))

        # 4. Pricing (optional)
        try:
            self._client(session, "pricing", PRICING_REGION).describe_services(ServiceCode="AmazonEC2", MaxResults=1)
            checks.append(Check(CHECK_PRICING, True, PRICING_REGION, required=False))
        except (ClientError, BotoCoreError) as exc:
            checks.append(Check(CHECK_PRICING, False, _err(exc), required=False))

        # 5. State bucket
        bucket = f"{STATE_BUCKET_PREFIX}{identity.account}"
        try:
            created = self._ensure_state_bucket(session, bucket)
            checks.append(Check(CHECK_BUCKET, True, f"{bucket} (created)" if created else bucket))
        except (ClientError, BotoCoreError, RuntimeError) as exc:
            checks.append(Check(CHECK_BUCKET, False, _err(exc)))

        # 6. Token expiry (informational)
        if credentials.get("session_token"):
            checks.append(Check(CHECK_EXPIRY, True, "temporary credentials — expires when the SSO session does"))
        else:
            checks.append(Check(CHECK_EXPIRY, True, "long-lived access key — no expiry"))

        ok = all(c.ok for c in checks if c.required)
        report = ConnectionReport(ok, identity, checks)
        stored = (
            {
                "access_key_id": credentials["access_key_id"],
                "secret_access_key": credentials["secret_access_key"],
                "session_token": credentials.get("session_token") or None,
            }
            if ok
            else None
        )
        return ConnectResult(report, scan_regions if ok else [], stored)

    def _ensure_state_bucket(self, session: boto3.Session, bucket: str) -> bool:
        """Create-or-verify the tfstate bucket in eu-central-1. Returns True if created."""
        s3 = self._client(session, "s3", HOME_REGION)
        created = False
        try:
            s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("404", "NoSuchBucket") or status == 404:
                try:
                    s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": HOME_REGION})
                    created = True
                except ClientError as create_exc:
                    if create_exc.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                        raise
            elif code in ("301", "PermanentRedirect") or status == 301:
                raise RuntimeError(f"bucket {bucket} exists in another region; it must live in {HOME_REGION}")
            else:
                raise
        s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        s3.put_public_access_block(
            Bucket=bucket,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket=bucket,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        versioning = s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if versioning != "Enabled":
            raise RuntimeError(f"bucket {bucket}: versioning is {versioning or 'off'} after enabling it")
        location = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
        if location != HOME_REGION:
            raise RuntimeError(f"bucket {bucket} is in {location}, expected {HOME_REGION}")
        return created


    # ------------------------------------------------------------------ inventory
    def inventory(self, credentials: dict[str, Any], regions: list[str]) -> dict[str, Any]:
        regions = sorted(regions)
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_SCAN_WORKERS, len(regions)))) as pool:
            region_results = list(pool.map(lambda r: self._scan_region(credentials, r), regions))

        pricing = Pricing(self._pricing_cache_path, lambda: self._client(self._session(credentials), "pricing", PRICING_REGION))
        cost, groups = self._cost(region_results, pricing)

        totals = {"instances": 0, "running": 0, "vpcs": 0, "nat_gateways": 0, "eips": 0, "volumes_gb": 0, "security_groups": 0, "subnets": 0}
        for r in region_results:
            totals["instances"] += len(r["instances"])
            totals["running"] += sum(1 for i in r["instances"] if i["state"] == "running")
            totals["vpcs"] += len(r["vpcs"])
            totals["nat_gateways"] += len(r["nat_gateways"])
            totals["eips"] += len(r["eips"])
            totals["volumes_gb"] += sum(v["size_gb"] for v in r["volumes"])
            totals["security_groups"] += len(r.get("security_groups", []))
            totals["subnets"] += sum(len(v.get("subnets", [])) for v in r["vpcs"])

        return {
            "supported": True,
            "generated_at": utcnow_iso(),
            "stale": False,
            "regions": region_results,
            "totals": totals,
            "groups": groups,
            "cost": cost,
        }

    @staticmethod
    def _paginate(ec2: Any, operation: str, key: str, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in ec2.get_paginator(operation).paginate(**kwargs):
            items.extend(page.get(key, []) or [])
        return items

    @staticmethod
    def _ami_names(ec2: Any, ami_ids: list[str]) -> dict[str, dict[str, Any]]:
        """describe_images batched by id; a deregistered AMI must not sink the whole batch."""
        out: dict[str, dict[str, Any]] = {}
        ids = sorted({a for a in ami_ids if a})

        def describe(batch: list[str]) -> None:
            for img in ec2.describe_images(ImageIds=batch).get("Images", []) or []:
                out[img["ImageId"]] = img

        for i in range(0, len(ids), AMI_BATCH):
            batch = ids[i : i + AMI_BATCH]
            try:
                describe(batch)
            except ClientError as exc:
                log.info("describe_images batch failed (%s); retrying one by one", _err(exc))
                for ami in batch:
                    try:
                        describe([ami])
                    except ClientError:
                        continue
        return out

    def _scan_region(self, credentials: dict[str, Any], region: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "region": region,
            "instances": [],
            "vpcs": [],
            "nat_gateways": [],
            "eips": [],
            "volumes": [],
            "security_groups": [],
            "monthly_usd": 0.0,
            "resource_count": 0,
            "error": None,
        }
        now = datetime.now(timezone.utc)
        try:
            # One session per thread: boto3 sessions are not safe to share for client creation.
            ec2 = self._client(self._session(credentials), "ec2", region)

            # ---- instances
            raw_instances: list[dict[str, Any]] = []
            for reservation in self._paginate(ec2, "describe_instances", "Reservations"):
                for inst in reservation.get("Instances", []):
                    if inst.get("State", {}).get("Name", "unknown") != "terminated":
                        raw_instances.append(inst)
            ami_info = self._ami_names(ec2, [i.get("ImageId") for i in raw_instances])
            for inst in raw_instances:
                state = inst.get("State", {}).get("Name", "unknown")
                tags = _tags(inst)
                launched = inst.get("LaunchTime")
                profile_arn = (inst.get("IamInstanceProfile") or {}).get("Arn")
                ami = inst.get("ImageId")
                result["instances"].append(
                    {
                        "id": inst["InstanceId"],
                        "name": tags.get("Name"),
                        "type": inst.get("InstanceType"),
                        "state": state,
                        "private_ip": inst.get("PrivateIpAddress"),
                        "public_ip": inst.get("PublicIpAddress"),
                        "launched": _iso(launched),
                        "uptime_h": _uptime_hours(launched, now) if state == "running" else None,
                        "platform": inst.get("PlatformDetails") or "Linux/UNIX",
                        "architecture": inst.get("Architecture"),
                        "az": inst.get("Placement", {}).get("AvailabilityZone"),
                        "vpc": inst.get("VpcId"),
                        "subnet": inst.get("SubnetId"),
                        "ami": ami,
                        "ami_name": (ami_info.get(ami) or {}).get("Name") if ami else None,
                        "iam_instance_profile": profile_arn.rsplit("/", 1)[-1] if profile_arn else None,
                        "key_name": inst.get("KeyName"),
                        "security_groups": [{"id": g.get("GroupId"), "name": g.get("GroupName")} for g in inst.get("SecurityGroups", []) or []],
                        "root_device": inst.get("RootDeviceName"),
                        "monitoring": (inst.get("Monitoring") or {}).get("State") in ("enabled", "pending"),
                        "ebs_optimized": bool(inst.get("EbsOptimized")),
                        "volumes": [
                            m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) or [] if (m.get("Ebs") or {}).get("VolumeId")
                        ],
                        "user_data_present": self._user_data_present(ec2, inst["InstanceId"]),
                        "monthly_usd": None,
                        "tags": tags,
                    }
                )
            sg_instances: dict[str, list[str]] = {}
            for inst in result["instances"]:
                for g in inst["security_groups"]:
                    if g["id"]:
                        sg_instances.setdefault(g["id"], []).append(inst["id"])

            # ---- network fabric: subnets, route tables, internet gateways
            subnets_raw = self._paginate(ec2, "describe_subnets", "Subnets")
            rts_raw = self._paginate(ec2, "describe_route_tables", "RouteTables")
            igws_raw = self._paginate(ec2, "describe_internet_gateways", "InternetGateways")
            igw_by_vpc: dict[str, str] = {}
            for igw in igws_raw:
                for att in igw.get("Attachments", []) or []:
                    if att.get("VpcId"):
                        igw_by_vpc[att["VpcId"]] = igw["InternetGatewayId"]
            route_tables: dict[str, dict[str, Any]] = {}
            main_rt_by_vpc: dict[str, str] = {}
            rt_by_subnet: dict[str, str] = {}
            for rt in rts_raw:
                tags = _tags(rt)
                assoc_subnets: list[str] = []
                main = False
                for assoc in rt.get("Associations", []) or []:
                    if assoc.get("Main"):
                        main = True
                        main_rt_by_vpc[rt["VpcId"]] = rt["RouteTableId"]
                    if assoc.get("SubnetId"):
                        assoc_subnets.append(assoc["SubnetId"])
                        rt_by_subnet[assoc["SubnetId"]] = rt["RouteTableId"]
                routes = [
                    {"dest": _route_dest(r), "target": _route_target(r), "state": r.get("State")}
                    for r in rt.get("Routes", []) or []
                ]
                route_tables[rt["RouteTableId"]] = {
                    "id": rt["RouteTableId"],
                    "name": tags.get("Name"),
                    "vpc": rt.get("VpcId"),
                    "main": main,
                    "routes": routes,
                    "subnets": assoc_subnets,
                    "tags": tags,
                }

            def default_route(rt_id: str | None) -> str | None:
                for r in (route_tables.get(rt_id or "") or {}).get("routes", []):
                    if r["dest"] == "0.0.0.0/0":
                        return r["target"]
                return None

            subnets_by_vpc: dict[str, list[dict[str, Any]]] = {}
            for sn in subnets_raw:
                tags = _tags(sn)
                rt_id = rt_by_subnet.get(sn["SubnetId"]) or main_rt_by_vpc.get(sn.get("VpcId", ""))
                target = default_route(rt_id)
                subnets_by_vpc.setdefault(sn.get("VpcId", ""), []).append(
                    {
                        "id": sn["SubnetId"],
                        "name": tags.get("Name"),
                        "cidr": sn.get("CidrBlock"),
                        "az": sn.get("AvailabilityZone"),
                        "public": bool(target and target.startswith("igw-")),
                        "route_table": rt_id,
                        "default_route": target,
                        "map_public_ip": bool(sn.get("MapPublicIpOnLaunch")),
                        "available_ips": sn.get("AvailableIpAddressCount"),
                        "tags": tags,
                    }
                )
            for subnets in subnets_by_vpc.values():
                subnets.sort(key=lambda x: (x["cidr"] or "", x["id"]))

            # ---- NAT gateways (before VPCs so each VPC can list its NAT ids)
            nat_by_vpc: dict[str, list[str]] = {}
            nat_by_allocation: dict[str, str] = {}
            for nat in self._paginate(ec2, "describe_nat_gateways", "NatGateways"):
                if nat.get("State") in ("deleted", "deleting"):
                    continue
                addrs = nat.get("NatGatewayAddresses") or [{}]
                tags = _tags(nat)
                for a in addrs:
                    if a.get("AllocationId"):
                        nat_by_allocation[a["AllocationId"]] = nat["NatGatewayId"]
                nat_by_vpc.setdefault(nat.get("VpcId", ""), []).append(nat["NatGatewayId"])
                result["nat_gateways"].append(
                    {
                        "id": nat["NatGatewayId"],
                        "vpc": nat.get("VpcId"),
                        "subnet": nat.get("SubnetId"),
                        "state": nat.get("State"),
                        "public_ip": addrs[0].get("PublicIp"),
                        "private_ip": addrs[0].get("PrivateIp"),
                        "connectivity_type": nat.get("ConnectivityType", "public"),
                        "created": _iso(nat.get("CreateTime")),
                        "name": tags.get("Name"),
                        "monthly_usd": None,
                        "tags": tags,
                    }
                )

            # ---- VPCs
            for vpc in self._paginate(ec2, "describe_vpcs", "Vpcs"):
                tags = _tags(vpc)
                vpc_id = vpc["VpcId"]
                result["vpcs"].append(
                    {
                        "id": vpc_id,
                        "name": tags.get("Name"),
                        "cidr": vpc.get("CidrBlock"),
                        "default": bool(vpc.get("IsDefault")),
                        "state": vpc.get("State"),
                        "dns_hostnames": self._dns_hostnames(ec2, vpc_id),
                        "igw": igw_by_vpc.get(vpc_id),
                        "subnets": subnets_by_vpc.get(vpc_id, []),
                        "nat_gateways": nat_by_vpc.get(vpc_id, []),
                        "route_tables": [
                            {k: v for k, v in rt.items() if k != "vpc"}
                            for rt in sorted(route_tables.values(), key=lambda x: (not x["main"], x["id"]))
                            if rt["vpc"] == vpc_id
                        ],
                        "tags": tags,
                    }
                )

            # ---- Elastic IPs
            for addr in ec2.describe_addresses().get("Addresses", []):
                tags = _tags(addr)
                allocation = addr.get("AllocationId")
                eni = addr.get("NetworkInterfaceId")
                association: dict[str, Any] | None = None
                if addr.get("InstanceId"):
                    association = {"kind": "instance", "id": addr["InstanceId"], "eni": eni}
                elif allocation and allocation in nat_by_allocation:
                    association = {"kind": "nat", "id": nat_by_allocation[allocation], "eni": eni}
                elif eni:
                    association = {"kind": "eni", "id": eni, "eni": eni}
                result["eips"].append(
                    {
                        "ip": addr.get("PublicIp"),
                        "allocation_id": allocation,
                        "attached": bool(addr.get("AssociationId")),
                        "instance": addr.get("InstanceId") or None,
                        "association": association,
                        "private_ip": addr.get("PrivateIpAddress"),
                        "name": tags.get("Name"),
                        "monthly_usd": None,
                        "tags": tags,
                    }
                )

            # ---- Volumes
            for vol in self._paginate(ec2, "describe_volumes", "Volumes"):
                attachments = vol.get("Attachments") or []
                first = attachments[0] if attachments else {}
                tags = _tags(vol)
                result["volumes"].append(
                    {
                        "id": vol["VolumeId"],
                        "size_gb": int(vol.get("Size", 0)),
                        "type": vol.get("VolumeType"),
                        "az": vol.get("AvailabilityZone"),
                        "iops": vol.get("Iops"),
                        "throughput": vol.get("Throughput"),
                        "encrypted": bool(vol.get("Encrypted")),
                        "state": vol.get("State"),
                        "attached": bool(attachments),
                        "instance": first.get("InstanceId"),
                        "attached_to": first.get("InstanceId"),
                        "device": first.get("Device"),
                        "created": _iso(vol.get("CreateTime")),
                        "name": tags.get("Name"),
                        "monthly_usd": None,
                        "tags": tags,
                    }
                )

            # ---- Security groups
            for sg in self._paginate(ec2, "describe_security_groups", "SecurityGroups"):
                tags = _tags(sg)
                result["security_groups"].append(
                    {
                        "id": sg["GroupId"],
                        "name": sg.get("GroupName"),
                        "vpc": sg.get("VpcId"),
                        "description": sg.get("Description"),
                        "ingress": _flatten_rules(sg.get("IpPermissions", [])),
                        "egress": _flatten_rules(sg.get("IpPermissionsEgress", [])),
                        "attached_to": sorted(sg_instances.get(sg["GroupId"], [])),
                        "tags": tags,
                    }
                )
        except (ClientError, BotoCoreError) as exc:
            result["error"] = _err(exc)
            log.warning("inventory scan failed in %s: %s", region, result["error"])
        result["resource_count"] = self.resource_count(result)
        return result

    @staticmethod
    def resource_count(region: dict[str, Any]) -> int:
        return sum(len(region.get(k, [])) for k in ("instances", "vpcs", "nat_gateways", "eips", "volumes", "security_groups"))

    @staticmethod
    def _user_data_present(ec2: Any, instance_id: str) -> bool | None:
        try:
            attr = ec2.describe_instance_attribute(InstanceId=instance_id, Attribute="userData")
        except (ClientError, BotoCoreError):
            return None
        return bool((attr.get("UserData") or {}).get("Value"))

    @staticmethod
    def _dns_hostnames(ec2: Any, vpc_id: str) -> bool | None:
        try:
            attr = ec2.describe_vpc_attribute(VpcId=vpc_id, Attribute="enableDnsHostnames")
        except (ClientError, BotoCoreError):
            return None
        return bool((attr.get("EnableDnsHostnames") or {}).get("Value"))

    # ------------------------------------------------------------------ cost
    @staticmethod
    def _group_key(tags: dict[str, str]) -> str:
        value = tags.get(GROUP_TAG)
        return f"{GROUP_TAG}={value}" if value else UNTAGGED_GROUP

    def _cost(self, regions: list[dict[str, Any]], pricing: Pricing) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Cost lines + tag groups. Also writes `monthly_usd` onto every priced resource and
        `monthly_usd` / `resource_count` onto every region, from the same lookups."""
        lines: dict[tuple[str, str, str, float | None], dict[str, Any]] = {}
        notes: list[str] = ["Unattached elastic IPs are billed", "NAT data processing not included"]
        missing: set[str] = set()
        group_instances: dict[str, int] = {}
        stopped = 0

        def add(item: str, region: str, group: str, qty: float, unit: str, unit_usd: float | None) -> None:
            key = (item, region, group, unit_usd)
            line = lines.get(key)
            if line is None:
                line = lines[key] = {"item": item, "region": region, "group": group, "qty": 0.0, "unit": unit, "unit_usd": unit_usd, "monthly_usd": 0.0}
            line["qty"] += qty
            if unit_usd is not None:
                line["monthly_usd"] += qty * unit_usd
            else:
                missing.add(f"{item} in {region}")

        def monthly(qty: float, unit_usd: float | None) -> float | None:
            return _round(qty * unit_usd) if unit_usd is not None else None

        for r in regions:
            region = r["region"]
            instance_group = {i["id"]: self._group_key(i.get("tags") or {}) for i in r["instances"]}
            public_ips: dict[str, str] = {}  # ip -> group

            for inst in r["instances"]:
                group = instance_group[inst["id"]]
                group_instances[group] = group_instances.get(group, 0) + 1
                if inst["state"] != "running":
                    if inst["state"] == "stopped":
                        stopped += 1
                    inst["monthly_usd"] = 0.0
                    continue
                os_name = platform_to_operating_system(inst.get("platform"))
                hourly = pricing.instance_hourly(inst["type"], region, os_name)
                inst["monthly_usd"] = monthly(HOURS_PER_MONTH, hourly)
                add(f"{inst['type']} {os_name}", region, group, HOURS_PER_MONTH, "hr", hourly)
                if inst.get("public_ip"):
                    public_ips[inst["public_ip"]] = group

            for vol in r["volumes"]:
                vtags = vol.get("tags") or {}
                group = self._group_key(vtags) if vtags.get(GROUP_TAG) else instance_group.get(vol.get("instance") or "", UNTAGGED_GROUP)
                rate = pricing.volume_gb_month(region, vol["type"] or "gp3")
                vol["monthly_usd"] = monthly(vol["size_gb"], rate)
                add(f"{vol['type']} storage", region, group, vol["size_gb"], "GB-mo", rate)

            for nat in r["nat_gateways"]:
                if nat["state"] not in ("available", "pending"):
                    nat["monthly_usd"] = 0.0
                    continue
                group = self._group_key(nat.get("tags") or {})
                hourly = pricing.nat_hourly(region)
                nat["monthly_usd"] = monthly(HOURS_PER_MONTH, hourly)
                add("NAT gateway", region, group, HOURS_PER_MONTH, "hr", hourly)
                if nat.get("public_ip"):
                    public_ips.setdefault(nat["public_ip"], group)

            for eip in r["eips"]:
                if not eip.get("ip"):
                    eip["monthly_usd"] = 0.0
                    continue
                etags = eip.get("tags") or {}
                group = self._group_key(etags) if etags.get(GROUP_TAG) else instance_group.get(eip.get("instance") or "", None)
                if group is None:
                    group = public_ips.get(eip["ip"], UNTAGGED_GROUP)
                public_ips[eip["ip"]] = group
                eip["monthly_usd"] = monthly(HOURS_PER_MONTH, PUBLIC_IPV4_HOURLY_USD)

            for _ip, group in public_ips.items():
                add("Public IPv4 address", region, group, HOURS_PER_MONTH, "hr", PUBLIC_IPV4_HOURLY_USD)

        if stopped:
            notes.append(f"{stopped} stopped instance(s): storage only")
        for item in sorted(missing):
            notes.append(f"No list price found for {item}; excluded from the estimate")

        out_lines: list[dict[str, Any]] = []
        group_totals: dict[str, float] = {}
        region_totals: dict[str, float] = {}
        for line in sorted(lines.values(), key=lambda l: (-l["monthly_usd"], l["item"], l["region"])):
            rounded = _round(line["monthly_usd"])
            group_totals[line["group"]] = group_totals.get(line["group"], 0.0) + rounded
            region_totals[line["region"]] = region_totals.get(line["region"], 0.0) + rounded
            out_lines.append(
                {
                    "item": line["item"],
                    "region": line["region"],
                    "group": line["group"],
                    "qty": int(line["qty"]) if float(line["qty"]).is_integer() else line["qty"],
                    "unit": line["unit"],
                    "unit_usd": line["unit_usd"],
                    "monthly_usd": rounded,
                }
            )
        for r in regions:
            r["monthly_usd"] = _round(region_totals.get(r["region"], 0.0))
            r["resource_count"] = self.resource_count(r)
        total = _round(sum(l["monthly_usd"] for l in out_lines))
        group_keys = sorted(set(group_instances) | set(group_totals), key=lambda k: (k == UNTAGGED_GROUP, k))
        groups = [
            {"key": key, "instances": group_instances.get(key, 0), "monthly_usd": _round(group_totals.get(key, 0.0))}
            for key in group_keys
        ]
        cost = {
            "monthly_usd": total,
            "currency": "USD",
            "method": f"on-demand list price × {HOURS_PER_MONTH}h",
            "lines": out_lines,
            "notes": notes,
        }
        return cost, groups
