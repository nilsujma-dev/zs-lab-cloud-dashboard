"""Provider interface. Nothing here may be AWS-shaped; AWS is one implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    def to_api(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class Identity:
    account: str
    arn: str
    alias: str | None = None

    def to_api(self) -> dict[str, Any]:
        return asdict(self)


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


class Provider(ABC):
    id: str
    name: str

    @abstractmethod
    def connect(self, credentials: dict[str, Any], regions: list[str] | None) -> ConnectResult:
        """Run the connection checklist. Must not persist anything; the caller stores credentials."""

    @abstractmethod
    def inventory(self, credentials: dict[str, Any], regions: list[str]) -> dict[str, Any]:
        """Full inventory + cost in the API shape (regions, totals, groups, cost, generated_at)."""

    @abstractmethod
    def credential_env(self, credentials: dict[str, Any]) -> dict[str, str]:
        """Environment variables that hand the credentials to a subprocess."""

    @abstractmethod
    def state_bucket(self, provider_record: dict[str, Any]) -> str | None:
        """Name of the remote-state bucket for this connected provider, if any."""

    @staticmethod
    def secret_values(credentials: dict[str, Any]) -> list[str]:
        return [str(v) for v in credentials.values() if v]
