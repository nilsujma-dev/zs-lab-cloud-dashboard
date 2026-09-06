"""Credentials for every cloud (v1.1 §C): registry + capabilities, backend-described forms,
GCP and Azure checklists with the SDK seams stubbed, honest unsupported inventory, AWS
rotation that replaces credentials only after every required check passes. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.providers import build_registry
from app.providers.azure import CHECK_ARM, CHECK_SUBSCRIPTION, CHECK_SUBSCRIPTIONS, AzureProvider
from app.providers.azure import CHECK_TOKEN as AZ_TOKEN
from app.providers.base import Check, ConnectionReport, ConnectResult, FormError, Identity
from app.providers.gcp import CHECK_COMPUTE, CHECK_JSON, CHECK_PROJECT, CHECK_TOKEN, GcpProvider
from app.store import Store

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKB\nxQNpWpqtMjxw3IJ6SzT7yUuHgKgv\n-----END PRIVATE KEY-----\n"
SA = {
    "type": "service_account",
    "project_id": "zs-lab-123456",
    "private_key_id": "0123456789abcdef",
    "private_key": PEM,
    "client_email": "switchboard@zs-lab-123456.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "token_uri": "https://oauth2.googleapis.com/token",
}
TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUB1 = "99999999-8888-7777-6666-555555555555"
SUB2 = "12121212-3434-5656-7878-909090909090"
AZ_SECRET = "very~secret.value_123"


# ---------------------------------------------------------------- registry / forms / capabilities
def test_registry_has_three_providers_with_capabilities(tmp_path: Path) -> None:
    reg = build_registry(tmp_path / "c.json")
    assert list(reg) == ["aws", "gcp", "azure"]
    assert reg["aws"].capabilities == {"inventory": True, "usecases": True}
    assert reg["gcp"].capabilities == {"inventory": False, "usecases": False}
    assert reg["azure"].capabilities == {"inventory": False, "usecases": False}


def test_list_providers_and_forms(logged_in) -> None:
    body = logged_in.get("/api/providers").json()
    assert [p["id"] for p in body] == ["aws", "gcp", "azure"]
    for p in body:
        assert set(p) >= {"id", "name", "status", "identity", "identity_label", "regions", "connected_at", "credentials_updated_at", "capabilities"}
        assert p["status"] == "disconnected" and p["identity"] is None
    assert body[0]["capabilities"] == {"inventory": True, "usecases": True}
    assert body[1]["capabilities"] == {"inventory": False, "usecases": False}

    gcp = logged_in.get("/api/providers/gcp/form").json()["fields"]
    assert [f["name"] for f in gcp] == ["service_account_json", "project_id"]
    assert gcp[0]["type"] == "file" and gcp[0]["required"] is True and gcp[0]["help"]
    assert gcp[1]["required"] is False
    azure = logged_in.get("/api/providers/azure/form").json()["fields"]
    assert [f["name"] for f in azure] == ["tenant_id", "client_id", "client_secret", "subscription_id"]
    assert next(f for f in azure if f["name"] == "client_secret")["type"] == "password"
    aws = logged_in.get("/api/providers/aws/form").json()["fields"]
    assert [f["name"] for f in aws] == ["access_key_id", "secret_access_key", "session_token"]
    assert all({"name", "label", "type", "required", "help"} == set(f) for f in gcp + azure + aws)
    assert all(f["type"] in ("text", "password", "textarea", "file") for f in gcp + azure + aws)
    assert logged_in.get("/api/providers/oci/form").status_code == 404


def test_form_requires_auth(client) -> None:
    assert client.get("/api/providers/gcp/form").status_code == 401


def test_unsupported_inventory_is_honest_200(logged_in) -> None:
    for pid in ("gcp", "azure"):
        r = logged_in.get(f"/api/providers/{pid}/inventory")
        assert r.status_code == 200
        body = r.json()
        assert body["supported"] is False and body["reason"] and body["generated_at"] is None
    assert logged_in.get("/api/providers/aws/inventory").status_code == 409  # supported, not connected


def test_connect_body_validation_never_echoes(logged_in) -> None:
    r = logged_in.post("/api/providers/gcp/connect", json={"project_id": "x"})
    assert r.status_code == 422 and r.json() == {"error": "Invalid request: check service_account_json", "code": "validation_error"}
    r = logged_in.post("/api/providers/azure/connect", json={"tenant_id": TENANT, "client_secret": "SUPERSECRET"})
    assert r.status_code == 422 and "SUPERSECRET" not in r.text and "client_id" in r.json()["error"]
    r = logged_in.post("/api/providers/azure/connect", json={"tenant_id": TENANT, "client_id": CLIENT, "client_secret": ["SUPERSECRET"]})
    assert r.status_code == 422 and "SUPERSECRET" not in r.text
    r = logged_in.post("/api/providers/aws/connect", json={"access_key_id": "a", "secret_access_key": "b", "regions": "eu-central-1"})
    assert r.status_code == 422 and "regions" in r.json()["error"]
    r = logged_in.post("/api/providers/aws/connect", json=["not", "a", "mapping"])
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


def test_parse_form_strips_and_defaults_optionals() -> None:
    creds = AzureProvider().parse_form({"tenant_id": f" {TENANT} ", "client_id": CLIENT, "client_secret": AZ_SECRET, "subscription_id": ""})
    assert creds == {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": AZ_SECRET, "subscription_id": None}
    with pytest.raises(FormError) as info:
        GcpProvider().parse_form({"service_account_json": 12})
    assert info.value.fields == ["service_account_json"] and "12" not in str(info.value)


# ---------------------------------------------------------------- GCP
class _GcpSeams:
    """Records which seams were hit; `fail` names the seam that should raise."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail: str | None = None, compute_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail

        def credentials(info: dict) -> Any:
            self.calls.append("credentials")
            assert info["client_email"] == SA["client_email"]
            return object()

        def refresh(creds: Any) -> None:
            self.calls.append("refresh")
            if fail == "refresh":
                raise RuntimeError("('invalid_grant: Invalid JWT Signature.', {'error': 'invalid_grant'})")

        def get_project(creds: Any, project_id: str) -> dict:
            self.calls.append(f"project:{project_id}")
            if fail == "project":
                exc = RuntimeError("denied")
                exc.status_code = 403  # type: ignore[attr-defined]
                exc.reason = "Permission denied on resource project zs-lab-123456"  # type: ignore[attr-defined]
                raise exc
            return {"name": "projects/424242", "projectId": project_id, "displayName": "ZS Lab", "state": "ACTIVE"}

        def list_regions(creds: Any, project_id: str) -> list[str]:
            self.calls.append("regions")
            if compute_error is not None:
                raise compute_error
            return ["europe-west3", "europe-west4"]

        monkeypatch.setattr(GcpProvider, "_credentials", staticmethod(credentials))
        monkeypatch.setattr(GcpProvider, "_refresh", staticmethod(refresh))
        monkeypatch.setattr(GcpProvider, "_get_project", staticmethod(get_project))
        monkeypatch.setattr(GcpProvider, "_list_regions", staticmethod(list_regions))


def test_gcp_rejects_bad_json_before_any_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _GcpSeams(monkeypatch)
    for text, needle in (("{not json", "not valid JSON"), ("[]", "must be an object"), (json.dumps({**SA, "type": "authorized_user"}), "expected 'service_account'"), (json.dumps({k: v for k, v in SA.items() if k != "private_key"}), "missing field(s): private_key")):
        result = GcpProvider().connect({"service_account_json": text, "project_id": None}, None)
        report = result.report.to_api()
        assert report["ok"] is False and result.credentials is None and report["identity"] is None
        assert [c["name"] for c in report["checks"]] == [CHECK_JSON, CHECK_TOKEN, CHECK_PROJECT, CHECK_COMPUTE]
        assert needle in report["checks"][0]["detail"]
        assert all(c["detail"].startswith("Skipped") for c in report["checks"][1:])
    assert seams.calls == []


def test_gcp_success_stores_compact_json_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _GcpSeams(monkeypatch)
    pretty = json.dumps(SA, indent=2)
    result = GcpProvider().connect({"service_account_json": pretty, "project_id": None}, None)
    report = result.report.to_api()
    assert report["ok"] is True and all(c["ok"] for c in report["checks"])
    assert report["identity"] == {"client_email": SA["client_email"], "project_id": "zs-lab-123456", "project_name": "ZS Lab", "project_number": "424242"}
    assert seams.calls == ["credentials", "refresh", "project:zs-lab-123456", "regions"]
    assert result.regions == ["europe-west3", "europe-west4"]
    assert result.credentials == {"service_account_json": json.dumps(SA, separators=(",", ":")), "project_id": "zs-lab-123456"}
    text = json.dumps(report)
    assert "PRIVATE KEY" not in text and "0123456789abcdef" not in text and "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKB" not in text
    assert GcpProvider().identity_label(report["identity"]) == SA["client_email"]


def test_gcp_explicit_project_overrides_key_project(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _GcpSeams(monkeypatch)
    result = GcpProvider().connect({"service_account_json": json.dumps(SA), "project_id": "other-project-42"}, None)
    assert result.report.ok and "project:other-project-42" in seams.calls and result.credentials["project_id"] == "other-project-42"
    bad = GcpProvider().connect({"service_account_json": json.dumps(SA), "project_id": "Not Valid!"}, None)
    assert not bad.report.ok and "not a valid GCP project id" in bad.report.checks[1].detail


def test_gcp_token_failure_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _GcpSeams(monkeypatch, fail="refresh")
    result = GcpProvider().connect({"service_account_json": json.dumps(SA), "project_id": None}, None)
    checks = {c.name: c for c in result.report.checks}
    assert checks[CHECK_JSON].ok and not checks[CHECK_TOKEN].ok and "invalid_grant" in checks[CHECK_TOKEN].detail
    assert checks[CHECK_PROJECT].detail.startswith("Skipped") and checks[CHECK_COMPUTE].detail.startswith("Skipped")
    assert result.credentials is None and "project" not in " ".join(seams.calls)


def test_gcp_project_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _GcpSeams(monkeypatch, fail="project")
    result = GcpProvider().connect({"service_account_json": json.dumps(SA), "project_id": None}, None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_PROJECT].ok and checks[CHECK_PROJECT].detail == "HTTP 403: Permission denied on resource project zs-lab-123456"
    assert result.credentials is None


def test_gcp_compute_disabled_is_informational(monkeypatch: pytest.MonkeyPatch) -> None:
    err = RuntimeError("Compute Engine API has not been used in project 424242 before or it is disabled")
    _GcpSeams(monkeypatch, compute_error=err)
    result = GcpProvider().connect({"service_account_json": json.dumps(SA), "project_id": None}, None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_COMPUTE].ok and checks[CHECK_COMPUTE].detail == "Compute Engine API not enabled in project zs-lab-123456"
    assert not checks[CHECK_COMPUTE].required
    assert result.report.ok is True and result.credentials is not None and result.regions == []


def test_gcp_secret_values_cover_pem_lines_and_env() -> None:
    creds = {"service_account_json": json.dumps(SA), "project_id": "zs-lab-123456"}
    values = GcpProvider.secret_values(creds)
    assert json.dumps(SA) in values and PEM in values and "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKB" in values
    assert "0123456789abcdef" in values and "-----BEGIN PRIVATE KEY-----" not in values  # bare PEM armour lines are not secrets
    env = GcpProvider().credential_env(creds)
    assert env["GOOGLE_CREDENTIALS"] == json.dumps(SA) and env["GOOGLE_PROJECT"] == "zs-lab-123456"


# ---------------------------------------------------------------- Azure
class _Sub:
    def __init__(self, sid: str, name: str, state: str = "Enabled") -> None:
        self.subscription_id, self.display_name, self.state = sid, name, state


class _Loc:
    def __init__(self, name: str) -> None:
        self.name = name


class _Tenant:
    tenant_id = TENANT
    display_name = "Zscaler Lab"


class _Tenants:
    @staticmethod
    def list():
        return iter([_Tenant()])


class _SubClient:
    def __init__(self, subs: list[_Sub]) -> None:
        self._subs = subs
        self.subscriptions = self
        self.tenants = _Tenants()

    def list(self):
        return iter(self._subs)

    def list_locations(self, sid: str):
        return iter([_Loc("westeurope"), _Loc("germanywestcentral")])


class _AzureSeams:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, subs: list[_Sub], *, fail: str | None = None) -> None:
        self.calls: list[str] = []

        class _Token:
            expires_on = 1_800_000_000

        def credential(tenant_id: str, client_id: str, client_secret: str) -> Any:
            self.calls.append("credential")
            assert client_secret == AZ_SECRET
            return object()

        def get_token(cred: Any) -> Any:
            self.calls.append("token")
            if fail == "token":
                raise RuntimeError(f"AADSTS7000215: Invalid client secret provided. secret={AZ_SECRET}\nTo troubleshoot, visit https://aka.ms/azsdk/python/identity/clientsecretcredential/troubleshoot")
            return _Token()

        def sub_client(cred: Any) -> Any:
            self.calls.append("subscriptions")
            if fail == "subscriptions":
                raise RuntimeError("(AuthorizationFailed) The client does not have authorization")
            return _SubClient(subs)

        def rg_count(cred: Any, sid: str) -> int:
            self.calls.append(f"arm:{sid}")
            if fail == "arm":
                raise RuntimeError("(AuthorizationFailed) Resource Manager says no")
            return 3

        monkeypatch.setattr(AzureProvider, "_credential", staticmethod(credential))
        monkeypatch.setattr(AzureProvider, "_get_token", staticmethod(get_token))
        monkeypatch.setattr(AzureProvider, "_subscription_client", staticmethod(sub_client))
        monkeypatch.setattr(AzureProvider, "_resource_group_count", staticmethod(rg_count))


def _az_creds(sub: str | None = None) -> dict[str, Any]:
    return {"tenant_id": TENANT, "client_id": CLIENT, "client_secret": AZ_SECRET, "subscription_id": sub}


def test_azure_success_single_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _AzureSeams(monkeypatch, [_Sub(SUB1, "Lab Subscription")])
    result = AzureProvider().connect(_az_creds(), None)
    report = result.report.to_api()
    assert report["ok"] is True and [c["name"] for c in report["checks"]] == [AZ_TOKEN, CHECK_SUBSCRIPTIONS, CHECK_SUBSCRIPTION, CHECK_ARM]
    assert all(c["ok"] for c in report["checks"])
    assert report["identity"] == {"tenant": TENANT, "tenant_name": "Zscaler Lab", "subscription_name": "Lab Subscription", "subscription_id": SUB1, "client_id": CLIENT}
    assert "expires 2027-" in report["checks"][0]["detail"] and report["checks"][3]["detail"] == "3 resource group(s)"
    assert result.regions == ["germanywestcentral", "westeurope"]
    assert result.credentials == _az_creds(SUB1)
    assert AZ_SECRET not in json.dumps(report)
    assert seams.calls == ["credential", "token", "subscriptions", f"arm:{SUB1}"]
    assert AzureProvider().identity_label(report["identity"]) == "Lab Subscription"


def test_azure_ambiguous_and_explicit_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    _AzureSeams(monkeypatch, [_Sub(SUB1, "One"), _Sub(SUB2, "Two")])
    result = AzureProvider().connect(_az_creds(), None)
    checks = {c.name: c for c in result.report.checks}
    assert not checks[CHECK_SUBSCRIPTION].ok and "2 subscriptions visible" in checks[CHECK_SUBSCRIPTION].detail
    assert checks[CHECK_ARM].detail.startswith("Skipped") and result.credentials is None
    chosen = AzureProvider().connect(_az_creds(SUB2.upper()), None)
    assert chosen.report.ok and chosen.report.identity.to_api()["subscription_name"] == "Two"
    missing = AzureProvider().connect(_az_creds("00000000-0000-0000-0000-000000000000"), None)
    assert not missing.report.ok and "not visible" in {c.name: c for c in missing.report.checks}[CHECK_SUBSCRIPTION].detail


def test_azure_bad_secret_fails_first_check_and_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _AzureSeams(monkeypatch, [_Sub(SUB1, "One")], fail="token")
    result = AzureProvider().connect(_az_creds(), None)
    report = result.report.to_api()
    assert report["ok"] is False and report["identity"] is None and result.credentials is None
    assert report["checks"][0]["detail"].startswith("AADSTS7000215: Invalid client secret provided.")
    assert AZ_SECRET not in json.dumps(report) and "troubleshoot" not in report["checks"][0]["detail"]
    assert all(c["detail"].startswith("Skipped") for c in report["checks"][1:])
    assert "subscriptions" not in seams.calls


def test_azure_arm_failure_and_non_guid_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    seams = _AzureSeams(monkeypatch, [_Sub(SUB1, "One")], fail="arm")
    result = AzureProvider().connect(_az_creds(), None)
    checks = {c.name: c for c in result.report.checks}
    assert checks[CHECK_SUBSCRIPTION].ok and not checks[CHECK_ARM].ok and result.credentials is None
    bad = AzureProvider().connect({**_az_creds(), "tenant_id": "contoso"}, None)
    assert not bad.report.ok and bad.report.checks[0].detail == "tenant id is not a GUID"
    assert seams.calls.count("credential") == 1  # the GUID check ran before any SDK call


def test_azure_env_and_secret_values() -> None:
    env = AzureProvider().credential_env(_az_creds(SUB1))
    assert env["ARM_CLIENT_SECRET"] == AZ_SECRET and env["AZURE_CLIENT_SECRET"] == AZ_SECRET
    assert env["ARM_SUBSCRIPTION_ID"] == SUB1 and env["ARM_TENANT_ID"] == TENANT and env["AZURE_CLIENT_ID"] == CLIENT
    assert AzureProvider.secret_values(_az_creds(SUB1)) == [AZ_SECRET]


# ---------------------------------------------------------------- storage + rotation through the API
def _canned(provider_cls: type, ok: bool, identity: dict[str, Any], credentials: dict[str, Any] | None, regions: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(self, creds: dict[str, Any], regions_arg: list[str] | None) -> ConnectResult:
        report = ConnectionReport(ok, Identity(**identity), [Check("stub", ok, "" if ok else "InvalidClientTokenId")])
        return ConnectResult(report, regions if ok else [], credentials if ok else None)

    monkeypatch.setattr(provider_cls, "connect", connect)


def test_gcp_connect_via_api_stores_encrypted_and_never_echoes(logged_in, data_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"service_account_json": json.dumps(SA, separators=(",", ":")), "project_id": "zs-lab-123456"}
    _canned(GcpProvider, True, {"client_email": SA["client_email"], "project_id": "zs-lab-123456"}, stored, ["europe-west3"], monkeypatch)
    r = logged_in.post("/api/providers/gcp/connect", json={"service_account_json": json.dumps(SA)})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "PRIVATE KEY" not in r.text
    listing = {p["id"]: p for p in logged_in.get("/api/providers").json()}
    assert listing["gcp"]["status"] == "connected" and listing["gcp"]["identity_label"] == SA["client_email"]
    assert listing["gcp"]["regions"] == ["europe-west3"] and listing["gcp"]["connected_at"] == listing["gcp"]["credentials_updated_at"]
    raw = (data_dir / "providers.json").read_text()
    assert "PRIVATE KEY" not in raw and SA["private_key_id"] not in raw
    assert Store(data_dir).provider_credentials("gcp") == stored
    inv = logged_in.get("/api/providers/gcp/inventory").json()
    assert inv["supported"] is False
    assert logged_in.delete("/api/providers/gcp").status_code == 204
    assert {p["id"]: p["status"] for p in logged_in.get("/api/providers").json()}["gcp"] == "disconnected"


def test_aws_reconnect_replaces_credentials_only_when_checks_pass(logged_in, data_dir, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers.aws import AwsProvider

    identity = {"account": "257300000000", "arn": "arn:aws:sts::257300000000:assumed-role/x/y", "alias": None}
    old = {"access_key_id": "asia-old-key-id", "secret_access_key": "old-secret", "session_token": "old-token"}
    new = {"access_key_id": "asia-new-key-id", "secret_access_key": "new-secret", "session_token": "new-token"}
    _canned(AwsProvider, True, identity, old, ["eu-central-1"], monkeypatch)
    assert logged_in.post("/api/providers/aws/connect", json={"access_key_id": "x", "secret_access_key": "y"}).json()["ok"]
    store = Store(data_dir)
    first = store.get_provider("aws")
    assert store.provider_credentials("aws") == old and first["connected_at"] == first["credentials_updated_at"]

    # A failing rotation must leave the old blob byte-for-byte intact.
    _canned(AwsProvider, False, identity, None, [], monkeypatch)
    r = logged_in.post("/api/providers/aws/connect", json={"access_key_id": "x", "secret_access_key": "bad"})
    assert r.status_code == 200 and r.json()["ok"] is False
    after_fail = store.get_provider("aws")
    assert after_fail == first and store.provider_credentials("aws") == old
    listing = {p["id"]: p for p in logged_in.get("/api/providers").json()}
    assert listing["aws"]["status"] == "connected"

    # A passing rotation replaces the blob and stamps credentials_updated_at, keeping connected_at.
    import app.main as main_mod

    monkeypatch.setattr(main_mod, "utcnow_iso", lambda: "2026-09-05T12:00:00+00:00")
    _canned(AwsProvider, True, identity, new, ["eu-central-1", "eu-west-1"], monkeypatch)
    assert logged_in.post("/api/providers/aws/connect", json={"access_key_id": "x", "secret_access_key": "y"}).json()["ok"]
    rotated = store.get_provider("aws")
    assert store.provider_credentials("aws") == new
    assert rotated["connected_at"] == first["connected_at"] and rotated["credentials_updated_at"] == "2026-09-05T12:00:00+00:00"
    assert rotated["credentials"] != first["credentials"] and rotated["regions"] == ["eu-central-1", "eu-west-1"]
    listing = {p["id"]: p for p in logged_in.get("/api/providers").json()}
    assert listing["aws"]["credentials_updated_at"] == "2026-09-05T12:00:00+00:00" and listing["aws"]["identity_label"] == "257300000000"
    raw = (data_dir / "providers.json").read_text()
    assert "old-secret" not in raw and "new-secret" not in raw and "asia-old-key-id" not in raw and "asia-new-key-id" not in raw


def test_unsupported_provider_usecase_is_explained(logged_in, tmp_path: Path, data_dir) -> None:
    from tests.conftest import GOOD_MANIFEST, write_manifest

    write_manifest(tmp_path / "usecases", "gcp-thing", GOOD_MANIFEST.replace("id: zpa-private-service-edge", "id: gcp-thing").replace("provider: aws", "provider: gcp"))
    store = Store(data_dir)
    store.save_provider("gcp", {"status": "connected", "identity": {"client_email": "x@y", "project_id": "p"}, "regions": [], "credentials": store.encrypt({"service_account_json": "{}", "project_id": "p"}), "connected_at": "2026-09-05T10:00:00+00:00"})
    cards = {c["id"]: c for c in logged_in.get("/api/usecases").json()}
    card = cards["gcp-thing"]
    assert card["provider_connected"] is True and card["provider_supported"] is False
    assert card["state"] == "unknown" and "does not support use cases" in card["state_error"]
    r = logged_in.post("/api/usecases/gcp-thing/on")
    assert r.status_code == 409 and r.json()["code"] == "provider_unsupported"
    outline = logged_in.get("/api/usecases/gcp-thing/outline?action=on").json()
    assert outline["plan"]["ok"] is False and "does not support" in outline["plan"]["error"]
