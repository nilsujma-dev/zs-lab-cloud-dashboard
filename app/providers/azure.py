"""Microsoft Azure provider: service-principal credential form + connection checklist.

Inventory and use cases are not built for Azure yet; `capabilities` says so and the
inventory endpoint answers `{"supported": false}` honestly.

Credentials stored (Fernet-encrypted by the caller):
    {"tenant_id", "client_id", "client_secret", "subscription_id"}
Identity (never secret): tenant, tenant_name, subscription_name, subscription_id, client_id.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.jobs import Scrubber
from app.providers.base import Check, ConnectionReport, ConnectResult, FormField, Identity, Provider

log = logging.getLogger("switchboard.azure")

ARM_SCOPE = "https://management.azure.com/.default"
CHECK_TOKEN = "Token from client secret"
CHECK_SUBSCRIPTIONS = "Subscriptions listable"
CHECK_SUBSCRIPTION = "Subscription readable"
CHECK_ARM = "Resource Manager reachable"
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _error_text(exc: BaseException) -> str:
    text = getattr(exc, "message", None) or str(exc)
    # azure-identity appends a multi-line troubleshooting essay; keep the first sentence.
    first = str(text).strip().splitlines()[0] if str(text).strip() else type(exc).__name__
    return " ".join(first.split())[:300]


class AzureProvider(Provider):
    id = "azure"
    name = "Microsoft Azure"
    capabilities = {"inventory": False, "usecases": False}
    unsupported_inventory_reason = "Inventory and cost are not built for Azure yet; the connection is real, the scan is not"

    # ------------------------------------------------------------------ form
    def form_fields(self) -> list[FormField]:
        return [
            FormField("tenant_id", "Tenant ID", "text", True, "Directory (tenant) ID of the Entra ID tenant, a GUID"),
            FormField("client_id", "Client ID", "text", True, "Application (client) ID of the service principal, a GUID"),
            FormField("client_secret", "Client secret", "password", True, "The secret *value* (not its ID); Fernet-encrypted at rest"),
            FormField("subscription_id", "Subscription ID", "text", False, "Required only if the principal can see more than one subscription"),
        ]

    def identity_label(self, identity: dict[str, Any] | None) -> str | None:
        return (identity or {}).get("subscription_name") or (identity or {}).get("subscription_id")

    # ------------------------------------------------------------------ SDK seams (patched in tests)
    @staticmethod
    def _credential(tenant_id: str, client_id: str, client_secret: str) -> Any:
        from azure.identity import ClientSecretCredential  # noqa: PLC0415 - lazy: only needed on connect

        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)

    @staticmethod
    def _get_token(credential: Any) -> Any:
        return credential.get_token(ARM_SCOPE)

    @staticmethod
    def _subscription_client(credential: Any) -> Any:
        from azure.mgmt.resource.subscriptions import SubscriptionClient  # noqa: PLC0415

        return SubscriptionClient(credential)

    @staticmethod
    def _resource_group_count(credential: Any, subscription_id: str) -> int:
        from azure.mgmt.resource.resources import ResourceManagementClient  # noqa: PLC0415

        client = ResourceManagementClient(credential, subscription_id)
        return sum(1 for _ in client.resource_groups.list())

    # ------------------------------------------------------------------ connect
    def connect(self, credentials: dict[str, Any], regions: list[str] | None) -> ConnectResult:
        checks: list[Check] = []
        tenant_id = str(credentials.get("tenant_id") or "").strip()
        client_id = str(credentials.get("client_id") or "").strip()
        client_secret = str(credentials.get("client_secret") or "")
        wanted_sub = (credentials.get("subscription_id") or "").strip() or None
        scrubber = Scrubber([client_secret])

        def skip(reason: str, *names: str) -> ConnectResult:
            for name in names:
                checks.append(Check(name, False, f"Skipped: {reason}"))
            return ConnectResult(ConnectionReport(False, None, checks), [], None)

        for label, value in (("tenant id", tenant_id), ("client id", client_id)):
            if not _GUID_RE.match(value):
                checks.append(Check(CHECK_TOKEN, False, f"{label} is not a GUID"))
                return skip(f"{label} not valid", CHECK_SUBSCRIPTIONS, CHECK_SUBSCRIPTION, CHECK_ARM)
        if wanted_sub and not _GUID_RE.match(wanted_sub):
            checks.append(Check(CHECK_TOKEN, False, "subscription id is not a GUID"))
            return skip("subscription id not valid", CHECK_SUBSCRIPTIONS, CHECK_SUBSCRIPTION, CHECK_ARM)

        # 1. Token
        try:
            credential = self._credential(tenant_id, client_id, client_secret)
            token = self._get_token(credential)
            expires = getattr(token, "expires_on", None)
            detail = "token issued"
            if isinstance(expires, (int, float)):
                detail += f", expires {datetime.fromtimestamp(expires, tz=timezone.utc).replace(microsecond=0).isoformat()}"
            checks.append(Check(CHECK_TOKEN, True, detail))
        except Exception as exc:  # noqa: BLE001 - every SDK failure becomes a checklist line
            checks.append(Check(CHECK_TOKEN, False, scrubber.scrub(_error_text(exc))))
            return skip("no token", CHECK_SUBSCRIPTIONS, CHECK_SUBSCRIPTION, CHECK_ARM)

        # 2. Subscriptions
        subs: list[Any] = []
        tenant_name: str | None = None
        try:
            client = self._subscription_client(credential)
            subs = list(client.subscriptions.list())
            try:
                for t in client.tenants.list():
                    if str(getattr(t, "tenant_id", "")) == tenant_id:
                        tenant_name = getattr(t, "display_name", None)
                        break
            except Exception:  # noqa: BLE001 - tenant name is decoration
                tenant_name = None
            enabled = sum(1 for s in subs if str(getattr(s, "state", "")).lower() == "enabled")
            checks.append(Check(CHECK_SUBSCRIPTIONS, True, f"{len(subs)} visible, {enabled} enabled"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(CHECK_SUBSCRIPTIONS, False, scrubber.scrub(_error_text(exc))))
            return skip("subscriptions not listable", CHECK_SUBSCRIPTION, CHECK_ARM)

        # 3. The chosen (or only) subscription
        chosen: Any = None
        if wanted_sub:
            chosen = next((s for s in subs if str(getattr(s, "subscription_id", "")).lower() == wanted_sub.lower()), None)
            if chosen is None:
                checks.append(Check(CHECK_SUBSCRIPTION, False, f"subscription {wanted_sub} is not visible to this principal"))
                return skip("subscription not readable", CHECK_ARM)
        elif len(subs) == 1:
            chosen = subs[0]
        elif not subs:
            checks.append(Check(CHECK_SUBSCRIPTION, False, "the principal has no subscription; assign it a role on one"))
            return skip("no subscription", CHECK_ARM)
        else:
            checks.append(Check(CHECK_SUBSCRIPTION, False, f"{len(subs)} subscriptions visible; set the subscription id to pick one"))
            return skip("subscription ambiguous", CHECK_ARM)
        subscription_id = str(getattr(chosen, "subscription_id", ""))
        subscription_name = str(getattr(chosen, "display_name", "") or subscription_id)
        sub_state = str(getattr(chosen, "state", "") or "")
        scan_regions: list[str] = []
        try:
            scan_regions = sorted(
                str(getattr(loc, "name", "")) for loc in client.subscriptions.list_locations(subscription_id) if getattr(loc, "name", None)
            )
        except Exception:  # noqa: BLE001 - locations are decoration
            scan_regions = []
        checks.append(Check(CHECK_SUBSCRIPTION, sub_state.lower() in ("", "enabled"), f"{subscription_name} ({subscription_id}) {sub_state}".strip()))
        identity = Identity(
            tenant=tenant_id,
            tenant_name=tenant_name,
            subscription_name=subscription_name,
            subscription_id=subscription_id,
            client_id=client_id,
        )

        # 4. Resource Manager
        try:
            count = self._resource_group_count(credential, subscription_id)
            checks.append(Check(CHECK_ARM, True, f"{count} resource group(s)"))
        except Exception as exc:  # noqa: BLE001
            checks.append(Check(CHECK_ARM, False, scrubber.scrub(_error_text(exc))))

        ok = all(c.ok for c in checks if c.required)
        stored = (
            {"tenant_id": tenant_id, "client_id": client_id, "client_secret": client_secret, "subscription_id": subscription_id}
            if ok
            else None
        )
        return ConnectResult(ConnectionReport(ok, identity, checks), scan_regions if ok else [], stored)

    # ------------------------------------------------------------------ use cases
    def credential_env(self, credentials: dict[str, Any]) -> dict[str, str]:
        env: dict[str, str] = {}
        for prefix in ("ARM", "AZURE"):  # terraform azurerm reads ARM_*, azure-identity reads AZURE_*
            env[f"{prefix}_TENANT_ID"] = str(credentials["tenant_id"])
            env[f"{prefix}_CLIENT_ID"] = str(credentials["client_id"])
            env[f"{prefix}_CLIENT_SECRET"] = str(credentials["client_secret"])
            if credentials.get("subscription_id"):
                env[f"{prefix}_SUBSCRIPTION_ID"] = str(credentials["subscription_id"])
        return env

    @staticmethod
    def secret_values(credentials: dict[str, Any]) -> list[str]:
        secret = credentials.get("client_secret")
        return [str(secret)] if secret else []
