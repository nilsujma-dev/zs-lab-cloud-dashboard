"""AWS provider: connection checklist, remote-state bucket, parallel inventory, cost."""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.providers.base import Check, ConnectionReport, ConnectResult, Identity, Provider
from app.providers.pricing import HOURS_PER_MONTH, PUBLIC_IPV4_HOURLY_USD, Pricing, platform_to_operating_system
from app.store import utcnow_iso

log = logging.getLogger("switchboard.aws")

HOME_REGION = "eu-central-1"
PRICING_REGION = "us-east-1"
STATE_BUCKET_PREFIX = "zs-lab-tfstate-"
GROUP_TAG = "Project"
UNTAGGED_GROUP = "untagged"
MAX_SCAN_WORKERS = 16
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


class AwsProvider(Provider):
    id = "aws"
    name = "Amazon Web Services"

    def __init__(self, pricing_cache_path: Path) -> None:
        self._pricing_cache_path = pricing_cache_path

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
            identity = Identity(account=ident["Account"], arn=ident["Arn"])
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

        totals = {"instances": 0, "running": 0, "vpcs": 0, "nat_gateways": 0, "eips": 0, "volumes_gb": 0}
        for r in region_results:
            totals["instances"] += len(r["instances"])
            totals["running"] += sum(1 for i in r["instances"] if i["state"] == "running")
            totals["vpcs"] += len(r["vpcs"])
            totals["nat_gateways"] += len(r["nat_gateways"])
            totals["eips"] += len(r["eips"])
            totals["volumes_gb"] += sum(v["size_gb"] for v in r["volumes"])

        return {
            "generated_at": utcnow_iso(),
            "stale": False,
            "regions": region_results,
            "totals": totals,
            "groups": groups,
            "cost": cost,
        }

    def _scan_region(self, credentials: dict[str, Any], region: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "region": region,
            "instances": [],
            "vpcs": [],
            "nat_gateways": [],
            "eips": [],
            "volumes": [],
            "error": None,
        }
        try:
            # One session per thread: boto3 sessions are not safe to share for client creation.
            ec2 = self._client(self._session(credentials), "ec2", region)
            for page in ec2.get_paginator("describe_instances").paginate():
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        state = inst.get("State", {}).get("Name", "unknown")
                        if state == "terminated":
                            continue
                        tags = _tags(inst)
                        launched = inst.get("LaunchTime")
                        result["instances"].append(
                            {
                                "id": inst["InstanceId"],
                                "name": tags.get("Name"),
                                "type": inst.get("InstanceType"),
                                "state": state,
                                "private_ip": inst.get("PrivateIpAddress"),
                                "public_ip": inst.get("PublicIpAddress"),
                                "launched": launched.isoformat() if launched else None,
                                "platform": inst.get("PlatformDetails") or "Linux/UNIX",
                                "az": inst.get("Placement", {}).get("AvailabilityZone"),
                                "vpc": inst.get("VpcId"),
                                "tags": tags,
                            }
                        )
            for page in ec2.get_paginator("describe_vpcs").paginate():
                for vpc in page.get("Vpcs", []):
                    tags = _tags(vpc)
                    result["vpcs"].append(
                        {"id": vpc["VpcId"], "name": tags.get("Name"), "cidr": vpc.get("CidrBlock"), "default": bool(vpc.get("IsDefault")), "tags": tags}
                    )
            for page in ec2.get_paginator("describe_nat_gateways").paginate():
                for nat in page.get("NatGateways", []):
                    if nat.get("State") in ("deleted", "deleting"):
                        continue
                    addrs = nat.get("NatGatewayAddresses") or [{}]
                    tags = _tags(nat)
                    result["nat_gateways"].append(
                        {
                            "id": nat["NatGatewayId"],
                            "vpc": nat.get("VpcId"),
                            "state": nat.get("State"),
                            "public_ip": addrs[0].get("PublicIp"),
                            "name": tags.get("Name"),
                            "tags": tags,
                        }
                    )
            for addr in ec2.describe_addresses().get("Addresses", []):
                tags = _tags(addr)
                result["eips"].append(
                    {
                        "ip": addr.get("PublicIp"),
                        "attached": bool(addr.get("AssociationId")),
                        "instance": addr.get("InstanceId") or None,
                        "name": tags.get("Name"),
                        "tags": tags,
                    }
                )
            for page in ec2.get_paginator("describe_volumes").paginate():
                for vol in page.get("Volumes", []):
                    attachments = vol.get("Attachments") or []
                    tags = _tags(vol)
                    result["volumes"].append(
                        {
                            "id": vol["VolumeId"],
                            "size_gb": int(vol.get("Size", 0)),
                            "type": vol.get("VolumeType"),
                            "attached": bool(attachments),
                            "instance": attachments[0].get("InstanceId") if attachments else None,
                            "name": tags.get("Name"),
                            "tags": tags,
                        }
                    )
        except (ClientError, BotoCoreError) as exc:
            result["error"] = _err(exc)
            log.warning("inventory scan failed in %s: %s", region, result["error"])
        return result

    # ------------------------------------------------------------------ cost
    @staticmethod
    def _group_key(tags: dict[str, str]) -> str:
        value = tags.get(GROUP_TAG)
        return f"{GROUP_TAG}={value}" if value else UNTAGGED_GROUP

    def _cost(self, regions: list[dict[str, Any]], pricing: Pricing) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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

        for r in regions:
            region = r["region"]
            instance_group = {i["id"]: self._group_key(i["tags"]) for i in r["instances"]}
            public_ips: dict[str, str] = {}  # ip -> group

            for inst in r["instances"]:
                group = instance_group[inst["id"]]
                group_instances[group] = group_instances.get(group, 0) + 1
                if inst["state"] != "running":
                    if inst["state"] == "stopped":
                        stopped += 1
                    continue
                os_name = platform_to_operating_system(inst.get("platform"))
                hourly = pricing.instance_hourly(inst["type"], region, os_name)
                add(f"{inst['type']} {os_name}", region, group, HOURS_PER_MONTH, "hr", hourly)
                if inst.get("public_ip"):
                    public_ips[inst["public_ip"]] = group

            for vol in r["volumes"]:
                group = self._group_key(vol["tags"]) if vol["tags"].get(GROUP_TAG) else instance_group.get(vol.get("instance") or "", UNTAGGED_GROUP)
                rate = pricing.volume_gb_month(region, vol["type"] or "gp3")
                add(f"{vol['type']} storage", region, group, vol["size_gb"], "GB-mo", rate)

            for nat in r["nat_gateways"]:
                if nat["state"] not in ("available", "pending"):
                    continue
                group = self._group_key(nat["tags"])
                add("NAT gateway", region, group, HOURS_PER_MONTH, "hr", pricing.nat_hourly(region))
                if nat.get("public_ip"):
                    public_ips.setdefault(nat["public_ip"], group)

            for eip in r["eips"]:
                if not eip.get("ip"):
                    continue
                group = self._group_key(eip["tags"]) if eip["tags"].get(GROUP_TAG) else instance_group.get(eip.get("instance") or "", None)
                if group is None:
                    group = public_ips.get(eip["ip"], UNTAGGED_GROUP)
                public_ips[eip["ip"]] = group

            for _ip, group in public_ips.items():
                add("Public IPv4 address", region, group, HOURS_PER_MONTH, "hr", PUBLIC_IPV4_HOURLY_USD)

        if stopped:
            notes.append(f"{stopped} stopped instance(s): storage only")
        for item in sorted(missing):
            notes.append(f"No list price found for {item}; excluded from the estimate")

        out_lines: list[dict[str, Any]] = []
        group_totals: dict[str, float] = {}
        for line in sorted(lines.values(), key=lambda l: (-l["monthly_usd"], l["item"], l["region"])):
            group_totals[line["group"]] = group_totals.get(line["group"], 0.0) + line["monthly_usd"]
            out_lines.append(
                {
                    "item": line["item"],
                    "region": line["region"],
                    "group": line["group"],
                    "qty": int(line["qty"]) if float(line["qty"]).is_integer() else line["qty"],
                    "unit": line["unit"],
                    "unit_usd": line["unit_usd"],
                    "monthly_usd": _round(line["monthly_usd"]),
                }
            )
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
