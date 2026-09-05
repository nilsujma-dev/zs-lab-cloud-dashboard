"""Google Cloud provider: service-account credential form + connection checklist.

Inventory and use cases are not built for GCP yet; `capabilities` says so and the
inventory endpoint answers `{"supported": false}` honestly.

Credentials stored (Fernet-encrypted by the caller):
    {"service_account_json": "<compact JSON>", "project_id": "<id>"}
Identity (never secret): client_email, project_id, project_name, project_number.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.jobs import Scrubber
from app.providers.base import Check, ConnectionReport, ConnectResult, FormField, Identity, Provider

log = logging.getLogger("switchboard.gcp")

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CHECK_JSON = "Service account JSON valid"
CHECK_TOKEN = "Token obtainable"
CHECK_PROJECT = "Project resolvable"
CHECK_COMPUTE = "Compute Engine API enabled"
REQUIRED_SA_KEYS = ("client_email", "private_key", "project_id", "token_uri")
_PROJECT_ID_RE = re.compile(r"^(?:[a-z][a-z0-9.-]*:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_API_DISABLED_RE = re.compile(r"has not been used|is disabled|API not enabled|accessNotConfigured|SERVICE_DISABLED", re.I)


def _http_error_text(exc: BaseException) -> str:
    """A one-line, secret-free description of a googleapiclient / google-auth failure."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "resp", None), "status", None)
    reason = getattr(exc, "reason", None)
    if status or reason:
        text = f"HTTP {status}: {reason}".strip(": ") if status else str(reason)
    else:
        text = f"{type(exc).__name__}: {exc}"
    return " ".join(text.split())[:300]


class GcpProvider(Provider):
    id = "gcp"
    name = "Google Cloud"
    capabilities = {"inventory": False, "usecases": False}
    unsupported_inventory_reason = "Inventory and cost are not built for Google Cloud yet; the connection is real, the scan is not"

    # ------------------------------------------------------------------ form
    def form_fields(self) -> list[FormField]:
        return [
            FormField(
                "service_account_json",
                "Service account key (JSON)",
                "file",
                True,
                "Paste or upload the key file downloaded from IAM → Service accounts → Keys. It must be of type service_account.",
            ),
            FormField(
                "project_id",
                "Project ID",
                "text",
                False,
                "Defaults to the key's own project_id; set it to validate the key against another project.",
            ),
        ]

    def identity_label(self, identity: dict[str, Any] | None) -> str | None:
        return (identity or {}).get("client_email") or (identity or {}).get("project_id")

    # ------------------------------------------------------------------ SDK seams (patched in tests)
    @staticmethod
    def _credentials(info: dict[str, Any]) -> Any:
        from google.oauth2 import service_account  # noqa: PLC0415 - lazy: only needed on connect

        return service_account.Credentials.from_service_account_info(info, scopes=[CLOUD_PLATFORM_SCOPE])

    @staticmethod
    def _refresh(creds: Any) -> None:
        from google.auth.transport.requests import Request  # noqa: PLC0415

        creds.refresh(Request())

    @staticmethod
    def _get_project(creds: Any, project_id: str) -> dict[str, Any]:
        from googleapiclient.discovery import build  # noqa: PLC0415

        service = build("cloudresourcemanager", "v3", credentials=creds, cache_discovery=False)
        return service.projects().get(name=f"projects/{project_id}").execute()

    @staticmethod
    def _list_regions(creds: Any, project_id: str) -> list[str]:
        from googleapiclient.discovery import build  # noqa: PLC0415

        service = build("compute", "v1", credentials=creds, cache_discovery=False)
        names: list[str] = []
        request = service.regions().list(project=project_id)
        while request is not None:
            page = request.execute()
            names.extend(item["name"] for item in page.get("items", []) if item.get("name"))
            request = service.regions().list_next(previous_request=request, previous_response=page)
        return sorted(names)

    # ------------------------------------------------------------------ connect
    @staticmethod
    def parse_service_account(text: str) -> tuple[dict[str, Any] | None, str]:
        """(info, detail). info is None when the JSON is not a usable service-account key."""
        try:
            info = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            return None, f"not valid JSON (line {getattr(exc, 'lineno', '?')})"
        if not isinstance(info, dict):
            return None, "JSON must be an object"
        if info.get("type") != "service_account":
            return None, f"'type' is {str(info.get('type'))!r}; expected 'service_account'"
        missing = [k for k in REQUIRED_SA_KEYS if not isinstance(info.get(k), str) or not info[k]]
        if missing:
            return None, f"missing field(s): {', '.join(missing)}"
        return info, f"{info['client_email']} (key {str(info.get('private_key_id', ''))[:8] or '?'}…)"

    def connect(self, credentials: dict[str, Any], regions: list[str] | None) -> ConnectResult:
        checks: list[Check] = []
        text = str(credentials.get("service_account_json") or "")
        scrubber = Scrubber(self.secret_values({"service_account_json": text}))

        def skip(reason: str, *names: str) -> ConnectResult:
            for name in names:
                checks.append(Check(name, False, f"Skipped: {reason}", required=name != CHECK_COMPUTE))
            return ConnectResult(ConnectionReport(False, None, checks), [], None)

        # 1. Parse — before any network call.
        info, detail = self.parse_service_account(text)
        if info is None:
            checks.append(Check(CHECK_JSON, False, detail))
            return skip("key not valid", CHECK_TOKEN, CHECK_PROJECT, CHECK_COMPUTE)
        checks.append(Check(CHECK_JSON, True, detail))
        project_id = (credentials.get("project_id") or info["project_id"]).strip()
        if not _PROJECT_ID_RE.match(project_id):
            checks.append(Check(CHECK_TOKEN, False, f"Skipped: project id {project_id!r} is not a valid GCP project id"))
            return skip("project id not valid", CHECK_PROJECT, CHECK_COMPUTE)

        # 2. Token
        try:
            creds = self._credentials(info)
            self._refresh(creds)
            checks.append(Check(CHECK_TOKEN, True, f"OAuth2 token for {info['client_email']}"))
        except Exception as exc:  # noqa: BLE001 - every SDK failure becomes a checklist line
            checks.append(Check(CHECK_TOKEN, False, scrubber.scrub(_http_error_text(exc))))
            return skip("no token", CHECK_PROJECT, CHECK_COMPUTE)

        # 3. Project
        identity: Identity | None = None
        try:
            project = self._get_project(creds, project_id)
            display = project.get("displayName") or project_id
            number = str(project.get("name", "")).rsplit("/", 1)[-1] or None
            state = project.get("state", "")
            identity = Identity(client_email=info["client_email"], project_id=project_id, project_name=display, project_number=number)
            checks.append(Check(CHECK_PROJECT, state in ("", "ACTIVE"), f"{display} ({project_id}) {state}".strip()))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(CHECK_PROJECT, False, scrubber.scrub(_http_error_text(exc))))
            return skip("project not resolvable", CHECK_COMPUTE)

        # 4. Compute API (informational: a project without Compute can still be plugged in)
        scan_regions: list[str] = []
        try:
            scan_regions = self._list_regions(creds, project_id)
            checks.append(Check(CHECK_COMPUTE, True, f"{len(scan_regions)} regions", required=False))
        except Exception as exc:  # noqa: BLE001
            text_err = scrubber.scrub(_http_error_text(exc))
            if _API_DISABLED_RE.search(str(exc)) or _API_DISABLED_RE.search(text_err):
                text_err = f"Compute Engine API not enabled in project {project_id}"
            checks.append(Check(CHECK_COMPUTE, False, text_err, required=False))

        ok = all(c.ok for c in checks if c.required)
        stored = {"service_account_json": json.dumps(info, separators=(",", ":")), "project_id": project_id} if ok else None
        return ConnectResult(ConnectionReport(ok, identity, checks), scan_regions if ok else [], stored)

    # ------------------------------------------------------------------ use cases
    def credential_env(self, credentials: dict[str, Any]) -> dict[str, str]:
        # Terraform's google provider accepts the key *contents* in GOOGLE_CREDENTIALS.
        env = {"GOOGLE_CREDENTIALS": str(credentials["service_account_json"])}
        if credentials.get("project_id"):
            env["GOOGLE_PROJECT"] = str(credentials["project_id"])
            env["CLOUDSDK_CORE_PROJECT"] = str(credentials["project_id"])
        return env

    @staticmethod
    def secret_values(credentials: dict[str, Any]) -> list[str]:
        """The JSON blob, plus the private key as it would appear if it ever leaked line by line."""
        values: list[str] = []
        text = credentials.get("service_account_json")
        if text:
            values.append(str(text))
            try:
                info = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                info = {}
            if isinstance(info, dict):
                key = info.get("private_key")
                if isinstance(key, str) and key:
                    values.append(key)
                    values.extend(line for line in key.splitlines() if line and not line.startswith("-----"))
                if isinstance(info.get("private_key_id"), str):
                    values.append(info["private_key_id"])
        return values
