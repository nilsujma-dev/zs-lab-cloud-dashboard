"""Persistence for the `/data` volume: JSON files, atomic writes, Fernet-encrypted secrets.

Layout (see SPEC.md):

    /data/providers.json
    /data/inventory/<provider>.json
    /data/usecases/<id>/{checkout/, runs/, status.json}
    /data/pricing-cache.json
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fernet_from_env_key(key: str) -> Fernet:
    """Accept a proper Fernet key, or derive one from an arbitrary secret string.

    deploy.sh is expected to generate a real Fernet key; the derivation path just keeps a
    hand-typed secret from crashing the app at startup.
    """
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
        return Fernet(derived)


class Store:
    def __init__(self, root: Path | str | None = None, secret_key: str | None = None) -> None:
        self.root = Path(root or os.environ.get("SWITCHBOARD_DATA") or "/data")
        key = secret_key if secret_key is not None else os.environ.get("SWITCHBOARD_SECRET_KEY", "")
        if not key:
            raise RuntimeError("SWITCHBOARD_SECRET_KEY is not set")
        self._fernet = _fernet_from_env_key(key)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "inventory").mkdir(exist_ok=True)
        (self.root / "usecases").mkdir(exist_ok=True)

    # ------------------------------------------------------------------ paths
    @property
    def providers_path(self) -> Path:
        return self.root / "providers.json"

    @property
    def pricing_cache_path(self) -> Path:
        return self.root / "pricing-cache.json"

    def inventory_path(self, provider_id: str) -> Path:
        return self.root / "inventory" / f"{provider_id}.json"

    def usecase_dir(self, usecase_id: str) -> Path:
        return self.root / "usecases" / usecase_id

    def checkout_dir(self, usecase_id: str) -> Path:
        return self.usecase_dir(usecase_id) / "checkout"

    def runs_dir(self, usecase_id: str) -> Path:
        return self.usecase_dir(usecase_id) / "runs"

    def status_path(self, usecase_id: str) -> Path:
        return self.usecase_dir(usecase_id) / "status.json"

    def ensure_usecase_dirs(self, usecase_id: str) -> None:
        self.runs_dir(usecase_id).mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ locks
    def lock(self, name: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(name)
            if lock is None:
                lock = self._locks[name] = threading.Lock()
            return lock

    # ------------------------------------------------------------------ JSON
    @staticmethod
    def read_json(path: Path, default: Any = None) -> Any:
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            return default

    @staticmethod
    def write_json(path: Path, data: Any) -> None:
        """Atomic write: serialise to a sibling temp file, fsync, rename over the target."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------------ secrets
    def encrypt(self, data: dict[str, Any]) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, blob: str) -> dict[str, Any]:
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except InvalidToken as exc:
            raise RuntimeError("stored credentials cannot be decrypted with SWITCHBOARD_SECRET_KEY") from exc

    # ------------------------------------------------------------------ providers
    def get_providers(self) -> dict[str, dict[str, Any]]:
        return self.read_json(self.providers_path, {}) or {}

    def get_provider(self, provider_id: str) -> dict[str, Any] | None:
        return self.get_providers().get(provider_id)

    def save_provider(self, provider_id: str, record: dict[str, Any]) -> None:
        with self.lock("providers"):
            data = self.get_providers()
            data[provider_id] = record
            self.write_json(self.providers_path, data)

    def delete_provider(self, provider_id: str) -> None:
        with self.lock("providers"):
            data = self.get_providers()
            data.pop(provider_id, None)
            self.write_json(self.providers_path, data)
        try:
            self.inventory_path(provider_id).unlink()
        except FileNotFoundError:
            pass

    def provider_credentials(self, provider_id: str) -> dict[str, Any] | None:
        """Decrypted credentials for in-process use only. Never return these to a client."""
        rec = self.get_provider(provider_id)
        if not rec or not rec.get("credentials"):
            return None
        return self.decrypt(rec["credentials"])

    def all_secret_values(self) -> list[str]:
        """Every stored secret value, for the log scrubber."""
        values: list[str] = []
        for provider_id, rec in self.get_providers().items():
            if rec.get("credentials"):
                try:
                    creds = self.decrypt(rec["credentials"])
                except RuntimeError:
                    continue
                values.extend(str(v) for v in creds.values() if v)
        return values

    # ------------------------------------------------------------------ inventory
    def get_inventory(self, provider_id: str) -> dict[str, Any] | None:
        return self.read_json(self.inventory_path(provider_id))

    def save_inventory(self, provider_id: str, inventory: dict[str, Any]) -> None:
        self.write_json(self.inventory_path(provider_id), inventory)

    # ------------------------------------------------------------------ use cases
    def get_status(self, usecase_id: str) -> dict[str, Any] | None:
        return self.read_json(self.status_path(usecase_id))

    def save_status(self, usecase_id: str, status: dict[str, Any]) -> None:
        self.ensure_usecase_dirs(usecase_id)
        self.write_json(self.status_path(usecase_id), status)

    def get_run(self, usecase_id: str, job_id: str) -> dict[str, Any] | None:
        return self.read_json(self.runs_dir(usecase_id) / f"{job_id}.json")

    def save_run(self, usecase_id: str, record: dict[str, Any]) -> None:
        self.ensure_usecase_dirs(usecase_id)
        self.write_json(self.runs_dir(usecase_id) / f"{record['id']}.json", record)

    def run_log_path(self, usecase_id: str, job_id: str) -> Path:
        return self.runs_dir(usecase_id) / f"{job_id}.log"

    def list_runs(self, usecase_id: str) -> list[dict[str, Any]]:
        """All job records for a use case, newest first."""
        runs_dir = self.runs_dir(usecase_id)
        if not runs_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for path in runs_dir.glob("*.json"):
            rec = self.read_json(path)
            if isinstance(rec, dict) and rec.get("id"):
                records.append(rec)
        records.sort(key=lambda r: (r.get("started") or "", r["id"]), reverse=True)
        return records

    def find_run(self, job_id: str) -> tuple[str, dict[str, Any]] | None:
        """Locate a job record by id across all use cases."""
        usecases_root = self.root / "usecases"
        if not usecases_root.is_dir():
            return None
        for uc_dir in usecases_root.iterdir():
            path = uc_dir / "runs" / f"{job_id}.json"
            if path.is_file():
                rec = self.read_json(path)
                if isinstance(rec, dict):
                    return uc_dir.name, rec
        return None
