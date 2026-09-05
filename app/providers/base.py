"""Provider interface. Nothing here may be AWS-shaped; AWS is one implementation.

A provider declares:
  - `capabilities`: what Switchboard can do with it once connected;
  - `form_fields()`: the credential form the UI renders (no provider-specific UI code);
  - `parse_form()`: raw request body -> credentials dict (+ optional region selection);
  - `connect()`: the checklist; credentials are only returned when every required check passed;
  - `inventory()`: optional; the default reports `supported: false` honestly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

FIELD_TYPES = frozenset({"text", "password", "textarea", "file"})


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


class Identity:
    """Free-form identity facts (never secrets). Keys differ per provider; see each module."""

    def __init__(self, **values: Any) -> None:
        self.values: dict[str, Any] = dict(values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["values"][name]
        except KeyError:
            raise AttributeError(name) from None

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Identity) and other.values == self.values

    def __repr__(self) -> str:
        return f"Identity({self.values!r})"

    def to_api(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass
class ConnectionReport:
    ok: bool
    identity: Identity | None
    checks: list[Check] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "identity": self.identity.to_api() if self.identity else None,
            "checks": [c.to_api() for c in self.checks],
        }


@dataclass
class ConnectResult:
    """Outcome of Provider.connect. `credentials` is only set when every required check passed."""

    report: ConnectionReport
    regions: list[str]
    credentials: dict[str, Any] | None


@dataclass(frozen=True)
class FormField:
    name: str
    label: str
    type: str = "text"
    required: bool = True
    help: str = ""

    def to_api(self) -> dict[str, Any]:
        assert self.type in FIELD_TYPES
        return {"name": self.name, "label": self.label, "type": self.type, "required": self.required, "help": self.help}


class FormError(ValueError):
    """The connect body is malformed. `fields` names the offending fields; never their values."""

    def __init__(self, fields: list[str]) -> None:
        super().__init__("Invalid request: check " + ", ".join(fields))
        self.fields = fields


class Provider(ABC):
    id: str
    name: str
    capabilities: dict[str, bool] = {"inventory": False, "usecases": False}
    unsupported_inventory_reason: str = "Inventory is not built for this provider yet"

    # ------------------------------------------------------------------ form
    @abstractmethod
    def form_fields(self) -> list[FormField]:
        """Credential form the UI renders. Never includes defaults or stored values."""

    def parse_form(self, body: dict[str, Any]) -> dict[str, Any]:
        """Validate the raw connect body against `form_fields()`. Returns the credentials dict
        (stripped strings; optional fields None when absent). Raises FormError; never echoes values."""
        bad: list[str] = []
        out: dict[str, Any] = {}
        for f in self.form_fields():
            value = body.get(f.name)
            if value is None or (isinstance(value, str) and not value.strip()):
                if f.required:
                    bad.append(f.name)
                out[f.name] = None
                continue
            if not isinstance(value, str):
                bad.append(f.name)
                continue
            out[f.name] = value.strip()
        if bad:
            raise FormError(bad)
        return out

    # ------------------------------------------------------------------ connect
    @abstractmethod
    def connect(self, credentials: dict[str, Any], regions: list[str] | None) -> ConnectResult:
        """Run the connection checklist. Must not persist anything; the caller stores credentials."""

    def identity_label(self, identity: dict[str, Any] | None) -> str | None:
        """One short line the UI can show on the jack (e.g. account id, service-account email)."""
        if not identity:
            return None
        for key in ("account", "client_email", "subscription_name", "name", "id"):
            if identity.get(key):
                return str(identity[key])
        return None

    # ------------------------------------------------------------------ inventory
    def inventory(self, credentials: dict[str, Any], regions: list[str]) -> dict[str, Any]:
        """Full inventory + cost in the API shape. Default: honest 'not supported'."""
        return self.unsupported_inventory()

    def unsupported_inventory(self) -> dict[str, Any]:
        return {"supported": False, "reason": self.unsupported_inventory_reason}

    # ------------------------------------------------------------------ use cases
    @abstractmethod
    def credential_env(self, credentials: dict[str, Any]) -> dict[str, str]:
        """Environment variables that hand the credentials to a subprocess."""

    def state_bucket(self, provider_record: dict[str, Any]) -> str | None:
        """Name of the remote-state bucket for this connected provider, if any."""
        return None

    @staticmethod
    def secret_values(credentials: dict[str, Any]) -> list[str]:
        """Every string the log scrubber must redact for these credentials."""
        return [str(v) for v in credentials.values() if v]
