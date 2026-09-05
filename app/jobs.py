"""In-process job runner: one thread per job, at most one running job per use case,
steps run sequentially via subprocess with merged stdout/stderr streamed to a scrubbed log."""

from __future__ import annotations

import logging
import re
import secrets as _secrets
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app.store import Store, utcnow_iso

log = logging.getLogger("switchboard.jobs")

REDACTED = "<redacted>"
AWS_KEY_RE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}")
BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/=_-]{300,}")
MIN_SECRET_LEN = 4


class Scrubber:
    """Replaces stored secret values, AWS access key ids and long base64 runs with <redacted>."""

    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        values = {v for v in secret_values if isinstance(v, str) and len(v) >= MIN_SECRET_LEN}
        # Longest first so a secret that contains another is removed whole.
        self._values = sorted(values, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        text = AWS_KEY_RE.sub(REDACTED, text)
        text = BASE64_RUN_RE.sub(REDACTED, text)
        return text


class LogWriter:
    """Append-only, scrubbed line writer for `runs/<job_id>.log`."""

    def __init__(self, path: Path, scrubber: Scrubber) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._scrubber = scrubber
        self._lock = threading.Lock()

    def write(self, line: str) -> None:
        clean = self._scrubber.scrub(line.rstrip("\r\n"))
        with self._lock:
            self._fh.write(clean + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


@dataclass(frozen=True)
class StepSpec:
    name: str
    run: str


class JobConflict(Exception):
    """A job is already running for this use case."""


def _new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{_secrets.token_hex(3)}"


def _public_record(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "usecase": rec["usecase"],
        "action": rec["action"],
        "state": rec["state"],
        "steps": [dict(s) for s in rec["steps"]],
        "started": rec.get("started"),
        "ended": rec.get("ended"),
        "error": rec.get("error"),
    }


class JobRunner:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._running: dict[str, dict[str, Any]] = {}  # usecase_id -> live record

    # ------------------------------------------------------------------ recovery
    def recover(self) -> int:
        """Mark jobs left `running` by a previous process as failed. Returns how many."""
        fixed = 0
        usecases_root = self._store.root / "usecases"
        if not usecases_root.is_dir():
            return 0
        for uc_dir in usecases_root.iterdir():
            for rec in self._store.list_runs(uc_dir.name):
                if rec.get("state") != "running":
                    continue
                now = utcnow_iso()
                rec["state"] = "failed"
                rec["ended"] = now
                rec["error"] = "Interrupted: Switchboard restarted while this job was running"
                for step in rec.get("steps", []):
                    if step.get("state") == "running":
                        step["state"] = "failed"
                        step["ended"] = now
                    elif step.get("state") == "pending":
                        step["state"] = "skipped"
                self._store.save_run(uc_dir.name, rec)
                fixed += 1
        return fixed

    # ------------------------------------------------------------------ queries
    def running_job(self, usecase_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._running.get(usecase_id)
            return _public_record(rec) if rec else None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            for rec in self._running.values():
                if rec["id"] == job_id:
                    return _public_record(rec)
        found = self._store.find_run(job_id)
        return _public_record(found[1]) if found else None

    def list_runs(self, usecase_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [_public_record(r) for r in self._store.list_runs(usecase_id)[:limit]]

    def read_log(self, job_id: str, since: int = 0) -> tuple[list[str], int] | None:
        """Lines after offset `since` and the next offset, or None if the job is unknown."""
        rec = self.get(job_id)
        if rec is None:
            return None
        path = self._store.run_log_path(rec["usecase"], job_id)
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            lines = []
        since = max(0, since)
        return lines[since:], len(lines)

    # ------------------------------------------------------------------ start
    def start(
        self,
        usecase_id: str,
        action: str,
        steps: list[StepSpec],
        *,
        cwd: Path,
        env: dict[str, str],
        scrubber: Scrubber,
        prelude: Callable[[LogWriter], None] | None = None,
        on_finish: Callable[[dict[str, Any]], None] | None = None,
    ) -> str:
        """Start a job in a background thread. Raises JobConflict if one is already running."""
        record: dict[str, Any] = {
            "id": _new_job_id(),
            "usecase": usecase_id,
            "action": action,
            "state": "running",
            "steps": [
                {"name": s.name, "run": s.run, "state": "pending", "started": None, "ended": None, "exit_code": None}
                for s in steps
            ],
            "started": utcnow_iso(),
            "ended": None,
            "error": None,
        }
        with self._lock:
            if usecase_id in self._running:
                raise JobConflict(f"A job is already running for use case '{usecase_id}'")
            self._running[usecase_id] = record
            self._store.save_run(usecase_id, record)

        thread = threading.Thread(
            target=self._run,
            args=(record, steps, cwd, env, scrubber, prelude, on_finish),
            name=f"job-{record['id']}",
            daemon=True,
        )
        thread.start()
        return record["id"]

    # ------------------------------------------------------------------ execution
    def _save(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._store.save_run(record["usecase"], record)

    def _run(
        self,
        record: dict[str, Any],
        steps: list[StepSpec],
        cwd: Path,
        env: dict[str, str],
        scrubber: Scrubber,
        prelude: Callable[[LogWriter], None] | None,
        on_finish: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        usecase_id = record["usecase"]
        writer = LogWriter(self._store.run_log_path(usecase_id, record["id"]), scrubber)
        failed = False
        try:
            writer.write(f"### job {record['id']} · {usecase_id} · {record['action']} · {len(steps)} step(s)")
            if prelude is not None:
                try:
                    prelude(writer)
                except Exception as exc:  # noqa: BLE001 - reported through the job record
                    failed = True
                    record["error"] = scrubber.scrub(f"Preparation failed: {exc}")
                    writer.write(f"!!! {record['error']}")
                    for step in record["steps"]:
                        step["state"] = "skipped"
                    self._save(record)

            for index, (spec, step) in enumerate(zip(steps, record["steps"]), start=1):
                if failed:
                    step["state"] = "skipped"
                    continue
                step["state"] = "running"
                step["started"] = utcnow_iso()
                self._save(record)
                writer.write(f"=== [{index}/{len(steps)}] {spec.name}")
                writer.write(f"$ {spec.run}")
                started = time.monotonic()
                try:
                    exit_code = self._exec(spec.run, cwd, env, writer)
                except Exception as exc:  # noqa: BLE001
                    writer.write(scrubber.scrub(f"!!! could not start step: {exc}"))
                    exit_code = -1
                elapsed = time.monotonic() - started
                step["exit_code"] = exit_code
                step["ended"] = utcnow_iso()
                step["state"] = "succeeded" if exit_code == 0 else "failed"
                writer.write(f"--- exit {exit_code} ({elapsed:.1f}s)")
                if exit_code != 0:
                    failed = True
                    record["error"] = f"Step '{spec.name}' failed with exit code {exit_code}"
                self._save(record)

            record["state"] = "failed" if failed else "succeeded"
            record["ended"] = utcnow_iso()
            writer.write(f"### job {record['state']}")
        finally:
            self._save(record)
            writer.close()
            with self._lock:
                self._running.pop(usecase_id, None)
            if on_finish is not None:
                try:
                    on_finish(_public_record(record))
                except Exception:  # noqa: BLE001
                    log.exception("on_finish hook failed for job %s", record["id"])

    @staticmethod
    def _exec(command: str, cwd: Path, env: dict[str, str], writer: LogWriter) -> int:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            writer.write(line)
        return proc.wait()
