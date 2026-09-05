"""AwsProvider.connect with a stubbed boto3 session: exact checklist order, credentials
only returned when every required check passes, pricing optional, bucket create-or-verify."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.providers.aws import (
    CHECK_BUCKET, CHECK_EC2, CHECK_EXPIRY, CHECK_PRICING, CHECK_REGIONS, CHECK_STS, AwsProvider,
)

ACCOUNT = "257300000000"
CREDS = {"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "session_token": "IQoJb3JpZ2luX2Vj"}


def _client_error(code: str, status: int, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} happened"}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class _S3:
    def __init__(self, exists: bool, *, head_error: ClientError | None = None) -> None:
        self.exists = exists
        self.head_error = head_error
        self.calls: list[str] = []
        self.versioning = "Suspended"

    def head_bucket(self, Bucket: str) -> dict:
        self.calls.append("head_bucket")
        if self.head_error:
            raise self.head_error
        if not self.exists:
            raise _client_error("404", 404, "HeadBucket")
        return {}

    def create_bucket(self, Bucket: str, CreateBucketConfiguration: dict) -> dict:
        self.calls.append("create_bucket")
        assert CreateBucketConfiguration == {"LocationConstraint": "eu-central-1"}
        self.exists = True
        return {}

    def put_bucket_versioning(self, Bucket: str, VersioningConfiguration: dict) -> None:
        self.calls.append("put_bucket_versioning")
        assert VersioningConfiguration == {"Status": "Enabled"}
        self.versioning = "Enabled"

    def put_public_access_block(self, Bucket: str, PublicAccessBlockConfiguration: dict) -> None:
        self.calls.append("put_public_access_block")
        assert all(PublicAccessBlockConfiguration.values()) and len(PublicAccessBlockConfiguration) == 4

    def put_bucket_encryption(self, Bucket: str, ServerSideEncryptionConfiguration: dict) -> None:
        self.calls.append("put_bucket_encryption")
        assert ServerSideEncryptionConfiguration["Rules"][0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"

    def get_bucket_versioning(self, Bucket: str) -> dict:
        return {"Status": self.versioning}

    def get_bucket_location(self, Bucket: str) -> dict:
        return {"LocationConstraint": "eu-central-1"}


class _Session:
    """Fake boto3.Session: `clients` maps service name -> object or exception to raise."""

    def __init__(self, clients: dict[str, Any]) -> None:
        self.clients = clients

    def client(self, service: str, region_name: str, config: Any = None) -> Any:
        obj = self.clients[service]
        if isinstance(obj, Exception):
            raise obj
        obj.region = region_name
        return obj


class _STS:
    def get_caller_identity(self) -> dict:
        return {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin/nils"}


class _EC2:
    def __init__(self, fail_instances: bool = False) -> None:
        self.fail_instances = fail_instances

    def describe_regions(self, AllRegions: bool) -> dict:
        return {"Regions": [{"RegionName": r} for r in ("eu-central-1", "us-east-1", "eu-west-1")]}

    def describe_instances(self, MaxResults: int) -> dict:
        if self.fail_instances:
            raise _client_error("UnauthorizedOperation", 403, "DescribeInstances")
        return {"Reservations": []}


class _PricingOK:
    def describe_services(self, ServiceCode: str, MaxResults: int) -> dict:
        return {"Services": []}


def _provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clients: dict[str, Any]) -> AwsProvider:
    provider = AwsProvider(tmp_path / "pricing-cache.json")
    monkeypatch.setattr(AwsProvider, "_session", staticmethod(lambda creds: _Session(clients)))
    return provider


def test_all_checks_pass_creates_bucket_and_returns_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s3 = _S3(exists=False)
    provider = _provider(monkeypatch, tmp_path, {"sts": _STS(), "ec2": _EC2(), "pricing": _PricingOK(), "s3": s3})
    result = provider.connect(dict(CREDS), None)
    report = result.report.to_api()
    assert report["ok"] is True
    assert report["identity"] == {"account": ACCOUNT, "arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_Admin/nils", "alias": None}
    assert [c["name"] for c in report["checks"]] == [CHECK_STS, CHECK_REGIONS, CHECK_EC2, CHECK_PRICING, CHECK_BUCKET, CHECK_EXPIRY]
    assert all(c["ok"] for c in report["checks"])
    checks = {c["name"]: c["detail"] for c in report["checks"]}
    assert checks[CHECK_STS] == "assumed-role/AWSReservedSSO_Admin/nils"
    assert checks[CHECK_REGIONS] == "3 enabled"
    assert checks[CHECK_PRICING] == "us-east-1"
    assert checks[CHECK_BUCKET] == f"zs-lab-tfstate-{ACCOUNT} (created)"
    assert "temporary credentials" in checks[CHECK_EXPIRY]
    assert s3.calls == ["head_bucket", "create_bucket", "put_bucket_versioning", "put_public_access_block", "put_bucket_encryption"]
    assert result.regions == ["eu-central-1", "eu-west-1", "us-east-1"]
    assert result.credentials == CREDS
    # Nothing secret in the report.
    text = str(report)
    assert CREDS["secret_access_key"] not in text and CREDS["access_key_id"] not in text and CREDS["session_token"] not in text


def test_existing_bucket_is_verified_not_recreated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s3 = _S3(exists=True)
    provider = _provider(monkeypatch, tmp_path, {"sts": _STS(), "ec2": _EC2(), "pricing": _PricingOK(), "s3": s3})
    result = provider.connect({**CREDS, "session_token": None}, ["eu-central-1"])
    checks = {c.name: c for c in result.report.checks}
    assert checks[CHECK_BUCKET].ok and checks[CHECK_BUCKET].detail == f"zs-lab-tfstate-{ACCOUNT}"
    assert "create_bucket" not in s3.calls and "put_bucket_versioning" in s3.calls
    assert checks[CHECK_REGIONS].detail == "3 enabled, 1 selected"
    assert result.regions == ["eu-central-1"]
    assert "long-lived" in checks[CHECK_EXPIRY].detail
    assert result.credentials == {**CREDS, "session_token": None}


def test_pricing_failure_is_not_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider(
        monkeypatch, tmp_path,
        {"sts": _STS(), "ec2": _EC2(), "pricing": _client_error("AccessDeniedException", 403, "DescribeServices"), "s3": _S3(exists=True)},
    )
    result = provider.connect(dict(CREDS), None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_PRICING].ok and "AccessDeniedException" in checks[CHECK_PRICING].detail
    assert result.report.ok is True
    assert result.credentials is not None


def test_required_failure_withholds_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider(monkeypatch, tmp_path, {"sts": _STS(), "ec2": _EC2(fail_instances=True), "pricing": _PricingOK(), "s3": _S3(exists=True)})
    result = provider.connect(dict(CREDS), None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_EC2].ok and "UnauthorizedOperation" in checks[CHECK_EC2].detail
    assert result.report.ok is False
    assert result.credentials is None
    assert result.regions == []


def test_bad_credentials_short_circuit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _BadSTS:
        def get_caller_identity(self) -> dict:
            raise _client_error("InvalidClientTokenId", 403, "GetCallerIdentity")

    provider = _provider(monkeypatch, tmp_path, {"sts": _BadSTS()})
    result = provider.connect(dict(CREDS), None)
    report = result.report.to_api()
    assert report["ok"] is False and report["identity"] is None
    assert [c["name"] for c in report["checks"]] == [CHECK_STS, CHECK_REGIONS, CHECK_EC2, CHECK_PRICING, CHECK_BUCKET, CHECK_EXPIRY]
    assert "InvalidClientTokenId" in report["checks"][0]["detail"]
    assert all(c["detail"].startswith("Skipped") for c in report["checks"][1:])
    assert result.credentials is None


def test_unknown_region_selection_fails_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _provider(monkeypatch, tmp_path, {"sts": _STS(), "ec2": _EC2(), "pricing": _PricingOK(), "s3": _S3(exists=True)})
    result = provider.connect(dict(CREDS), ["eu-central-1", "mars-1"])
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_REGIONS].ok and "mars-1" in checks[CHECK_REGIONS].detail
    assert result.credentials is None


def test_bucket_in_wrong_region_fails_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    s3 = _S3(exists=True, head_error=_client_error("301", 301, "HeadBucket"))
    provider = _provider(monkeypatch, tmp_path, {"sts": _STS(), "ec2": _EC2(), "pricing": _PricingOK(), "s3": s3})
    result = provider.connect(dict(CREDS), None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_BUCKET].ok and "another region" in checks[CHECK_BUCKET].detail
    assert result.credentials is None


def test_credential_env_and_state_bucket(tmp_path: Path) -> None:
    provider = AwsProvider(tmp_path / "c.json")
    env = provider.credential_env(CREDS)
    assert env == {"AWS_ACCESS_KEY_ID": CREDS["access_key_id"], "AWS_SECRET_ACCESS_KEY": CREDS["secret_access_key"], "AWS_SESSION_TOKEN": CREDS["session_token"]}
    assert "AWS_SESSION_TOKEN" not in provider.credential_env({**CREDS, "session_token": None})
    assert provider.state_bucket({"identity": {"account": ACCOUNT}}) == f"zs-lab-tfstate-{ACCOUNT}"
    assert provider.state_bucket({}) is None
