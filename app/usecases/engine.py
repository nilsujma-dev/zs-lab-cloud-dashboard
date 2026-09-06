"""Use-case engine: checkout, `tofu init`, state derivation, status probe, on/off jobs.

Nothing here is AWS-shaped: the provider supplies credential env vars and the state bucket name.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.jobs import JobConflict, JobRunner, LogWriter, Scrubber, StepSpec
from app.providers.base import Provider
from app.store import Store, utcnow_iso
from app.usecases.manifest import Manifest, load_all
from app.usecases.plan_graph import SCHEMAS, SourceIndex, build_plan_graph
from app.usecases.topology import build_graph

log = logging.getLogger("switchboard.engine")

STATE_BACKEND_REGION = "eu-central-1"
STATE_CACHE_TTL_S = 60
PROBE_TIMEOUT_S = 180
GIT_TIMEOUT_S = 600
TOFU_INIT_TIMEOUT_S = 900
TOFU_STATE_TIMEOUT_S = 120
TOFU_PLAN_TIMEOUT_S = 900
TOFU_SHOW_TIMEOUT_S = 120
OUTLINE_CACHE_TTL_S = 60  # one plan cache serves the outline and the planned topology (v1.4)
TOPOLOGY_CACHE_TTL_S = 60
PLAN_FILE = ".switchboard-{action}.tfplan"  # in the terraform dir of the checkout; deleted after `show`
PLAN_ACTIONS = {"create": "create", "update": "update", "delete": "destroy", "read": "read", "noop": "unchanged", "no-op": "unchanged"}
REFRESH_LOOP_S = 5
CODE_FILE_LIMIT = 512 * 1024
SKIP_DIRS = frozenset({".git", ".terraform", "__pycache__", "node_modules", ".venv"})
LANGUAGES = {
    ".tf": "hcl", ".tfvars": "hcl", ".hcl": "hcl",
    ".py": "python",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell", ".bash": "shell",
    ".json": "json", ".md": "markdown", ".toml": "toml", ".txt": "text",
}
ZSCALER_ONEAPI_VARS = ("ZS_ISSUER", "ZS_CLIENT_ID", "ZPA_CUSTOMER_ID", "ZS_GATEWAY")
ZSCALER_KEY_LINK = ".zscaler_api_key"


class EngineError(Exception):
    def __init__(self, message: str, code: str = "engine_error", status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class TofuError(Exception):
    """`tofu` exited non-zero; the message is the scrubbed tail of its output."""


class Engine:
    def __init__(
        self,
        store: Store,
        providers: dict[str, Provider],
        jobs: JobRunner,
        usecases_root: Path,
        *,
        tofu_bin: str = "tofu",
        git_bin: str = "git",
    ) -> None:
        self._store = store
        self._providers = providers
        self._jobs = jobs
        self._root = usecases_root
        self._tofu = tofu_bin
        self._git = git_bin
        self._state_cache: dict[str, dict[str, Any]] = {}
        # (usecase, action) -> {"outline", "show", "show_error", "generated_at", "_at"}: one `tofu plan`
        # feeds both the outline and the planned topology, so their counts come from one run.
        self._plan_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._outline_locks: dict[tuple[str, str], threading.Lock] = {}
        self._topology_cache: dict[str, dict[str, Any]] = {}
        # One inventory scan at a time per process; main.py's inventory route shares this lock.
        self.inventory_lock = threading.Lock()
        self._initialised: set[str] = set()
        self._last_probe: dict[str, float] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ manifests
    def manifests(self) -> tuple[dict[str, Manifest], dict[str, str]]:
        return load_all(self._root, set(self._providers))

    def manifest(self, usecase_id: str) -> Manifest:
        manifests, _ = self.manifests()
        try:
            return manifests[usecase_id]
        except KeyError:
            raise EngineError(f"Unknown use case '{usecase_id}'", "not_found", 404) from None

    def _lock(self, usecase_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(usecase_id)
            if lock is None:
                lock = self._locks[usecase_id] = threading.RLock()
            return lock

    # ------------------------------------------------------------------ env
    def provider_record(self, manifest: Manifest) -> dict[str, Any] | None:
        rec = self._store.get_provider(manifest.provider)
        return rec if rec and rec.get("status") == "connected" else None

    def provider_supports_usecases(self, manifest: Manifest) -> bool:
        provider = self._providers.get(manifest.provider)
        return bool(provider and provider.capabilities.get("usecases"))

    def provider_problem(self, manifest: Manifest) -> tuple[str, str] | None:
        """(code, message) explaining why this use case cannot run right now, or None."""
        if not self.provider_supports_usecases(manifest):
            return "provider_unsupported", f"Provider '{manifest.provider}' does not support use cases yet"
        if self.provider_record(manifest) is None:
            return "provider_not_connected", f"Provider '{manifest.provider}' is not connected"
        return None

    def secret_values(self) -> list[str]:
        """Every stored secret, expanded per provider (e.g. a GCP key's PEM lines), for the scrubber."""
        values = list(self._store.all_secret_values())
        for provider_id, provider in self._providers.items():
            try:
                creds = self._store.provider_credentials(provider_id)
            except RuntimeError:
                continue
            if creds:
                values.extend(provider.secret_values(creds))
        return values

    def step_env(self, manifest: Manifest, credentials: dict[str, Any] | None) -> dict[str, str]:
        """Minimal env per SPEC: PATH, HOME, provider creds, manifest env, mapped secrets. Nothing else."""
        home = self._store.usecase_dir(manifest.id)
        home.mkdir(parents=True, exist_ok=True)
        env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
            "HOME": str(home),
            # Non-interactive tooling: no ANSI colour in logs, no tofu prompts.
            "NO_COLOR": "1",
            "TF_IN_AUTOMATION": "1",
            "TF_CLI_ARGS": "-no-color",
        }
        if credentials:
            env.update(self._providers[manifest.provider].credential_env(credentials))
        env.update(manifest.env)
        for secret in manifest.secrets:
            if secret == "zscaler_oneapi":
                for var in ZSCALER_ONEAPI_VARS:
                    value = os.environ.get(var)
                    if value:
                        env[var] = value
                self._link_zscaler_key(home)
        return env

    @staticmethod
    def _link_zscaler_key(home: Path) -> None:
        target = Path(os.environ.get("ZSCALER_API_KEY_FILE") or "/run/secrets/zscaler_api_key")
        link = home / ZSCALER_KEY_LINK
        if link.is_symlink():
            if os.readlink(link) == str(target):
                return
            link.unlink()
        elif link.exists():
            link.unlink()
        if not target.exists():
            log.warning("zscaler api key file %s is not mounted; %s will dangle", target, link)
        link.symlink_to(target)

    # ------------------------------------------------------------------ subprocess helpers
    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout: int,
        log_line: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        """Run a command, streaming lines to `log_line` if given. Returns (exit_code, output)."""
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        chunks: list[str] = []
        deadline = time.monotonic() + timeout
        try:
            for line in proc.stdout:
                chunks.append(line)
                if log_line is not None:
                    log_line(line.rstrip("\n"))
                if time.monotonic() > deadline:
                    proc.kill()
                    raise TofuError(f"{args[0]} timed out after {timeout}s")
            code = proc.wait(timeout=max(1, int(deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise TofuError(f"{args[0]} timed out after {timeout}s") from None
        finally:
            proc.stdout.close()
        return code, "".join(chunks)

    def _git_env(self, manifest: Manifest) -> dict[str, str]:
        env = self.step_env(manifest, None)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    # ------------------------------------------------------------------ checkout + init
    def current_commit(self, manifest: Manifest) -> str | None:
        checkout = self._store.checkout_dir(manifest.id)
        if not (checkout / ".git").exists():
            return None
        try:
            code, out = self._run([self._git, "rev-parse", "HEAD"], cwd=checkout, env=self._git_env(manifest), timeout=30)
        except (TofuError, OSError):
            return None
        return out.strip() if code == 0 else None

    def ensure_checkout(self, manifest: Manifest, log_line: Callable[[str], None] | None = None) -> str:
        """Clone, or fetch + reset --hard to `source.ref`. Returns the commit sha."""
        say = log_line or (lambda _line: None)
        checkout = self._store.checkout_dir(manifest.id)
        env = self._git_env(manifest)
        with self._lock(manifest.id):
            if not (checkout / ".git").exists():
                checkout.parent.mkdir(parents=True, exist_ok=True)
                say(f">>> git clone {manifest.source_git} {checkout}")
                code, out = self._run(
                    [self._git, "clone", "--quiet", manifest.source_git, str(checkout)],
                    cwd=checkout.parent, env=env, timeout=GIT_TIMEOUT_S, log_line=say,
                )
                if code != 0:
                    raise EngineError(f"git clone failed (exit {code}): {out.strip()[-500:]}")
            say(f">>> git fetch origin {manifest.source_ref}")
            code, out = self._run(
                [self._git, "fetch", "--quiet", "--force", "origin", manifest.source_ref],
                cwd=checkout, env=env, timeout=GIT_TIMEOUT_S, log_line=say,
            )
            if code != 0:
                raise EngineError(f"git fetch failed (exit {code}): {out.strip()[-500:]}")
            say(">>> git reset --hard FETCH_HEAD")
            code, out = self._run(
                [self._git, "reset", "--quiet", "--hard", "FETCH_HEAD"],
                cwd=checkout, env=env, timeout=120, log_line=say,
            )
            if code != 0:
                raise EngineError(f"git reset failed (exit {code}): {out.strip()[-500:]}")
            commit = self.current_commit(manifest) or ""
            say(f">>> at commit {commit[:12]} ({manifest.source_ref})")
            return commit

    def tofu_init(self, manifest: Manifest, env: dict[str, str], log_line: Callable[[str], None] | None = None) -> None:
        provider_rec = self.provider_record(manifest)
        if provider_rec is None:
            raise EngineError(f"Provider '{manifest.provider}' is not connected", "provider_not_connected", 409)
        bucket = self._providers[manifest.provider].state_bucket(provider_rec)
        if not bucket:
            raise EngineError(f"Provider '{manifest.provider}' has no state bucket", "provider_not_connected", 409)
        say = log_line or (lambda _line: None)
        args = [
            self._tofu,
            f"-chdir={manifest.terraform_dir}",
            "init",
            "-input=false",
            "-reconfigure",
            "-no-color",
            f"-backend-config=bucket={bucket}",
            f"-backend-config=key={manifest.state_key}",
            f"-backend-config=region={STATE_BACKEND_REGION}",
            "-backend-config=use_lockfile=true",
        ]
        say(">>> " + " ".join(args))
        with self._lock(manifest.id):
            code, out = self._run(args, cwd=self._store.checkout_dir(manifest.id), env=env, timeout=TOFU_INIT_TIMEOUT_S, log_line=say)
            if code != 0:
                self._initialised.discard(manifest.id)
                raise TofuError(f"tofu init failed (exit {code}): {out.strip()[-800:]}")
            self._initialised.add(manifest.id)

    def prepare(self, manifest: Manifest, env: dict[str, str], log_line: Callable[[str], None] | None = None) -> str:
        """Checkout + init; idempotent. Returns the commit sha."""
        commit = self.ensure_checkout(manifest, log_line)
        self.tofu_init(manifest, env, log_line)
        return commit

    # ------------------------------------------------------------------ state
    @staticmethod
    def parse_state_list(output: str) -> list[str]:
        """Resource addresses from `tofu state list` stdout (blank and diagnostic lines dropped)."""
        resources: list[str] = []
        for raw in output.splitlines():
            line = raw.strip()
            if not line or line.startswith(("Warning:", "Error:", "│", "╷", "╵")):
                continue
            # Data sources live in state but are not infrastructure: never created by ON,
            # never destroyed by OFF. Counting them would show "4 unchanged" on a destroy
            # plan and inflate the card's resource count. Address forms: `data.x.y` or
            # `module.m.data.x.y`.
            if line.startswith("data.") or ".data." in line:
                continue
            resources.append(line)
        return resources

    def state_list(self, manifest: Manifest, env: dict[str, str]) -> list[str]:
        if manifest.id not in self._initialised:
            self.prepare(manifest, env)
        with self._lock(manifest.id):
            code, out = self._run(
                [self._tofu, f"-chdir={manifest.terraform_dir}", "state", "list", "-no-color"],
                cwd=self._store.checkout_dir(manifest.id), env=env, timeout=TOFU_STATE_TIMEOUT_S,
            )
        if code != 0:
            # "No state file was found" is an empty state, not an error.
            if "No state file was found" in out or "no state" in out.lower():
                return []
            raise TofuError(f"tofu state list failed (exit {code}): {out.strip()[-800:]}")
        return self.parse_state_list(out)

    @staticmethod
    def derive_state(
        *,
        running_action: str | None,
        last_run_state: str | None,
        resources: list[str] | None,
        tofu_error: bool,
    ) -> str:
        """Pure state rule from SPEC: running → turning_*; last job failed → error;
        resources → on/off; tofu error → unknown."""
        if running_action == "on":
            return "turning_on"
        if running_action == "off":
            return "turning_off"
        if last_run_state == "failed":
            return "error"
        if tofu_error or resources is None:
            return "unknown"
        return "on" if resources else "off"

    def invalidate(self, usecase_id: str) -> None:
        self._state_cache.pop(usecase_id, None)
        self._topology_cache.pop(usecase_id, None)
        for key in [k for k in self._plan_cache if k[0] == usecase_id]:
            self._plan_cache.pop(key, None)

    def state(self, manifest: Manifest, *, force: bool = False) -> dict[str, Any]:
        """{"state", "resources", "checked_at", "error"} with a 60s cache of the tofu probe."""
        running = self._jobs.running_job(manifest.id)
        runs = self._jobs.list_runs(manifest.id, limit=1)
        last_state = runs[0]["state"] if runs else None
        cached = self._state_cache.get(manifest.id)
        fresh = cached is not None and not force and (time.monotonic() - cached["_at"]) < STATE_CACHE_TTL_S

        if running is not None:
            resources = cached["resources"] if cached else None
            return {
                "state": self.derive_state(running_action=running["action"], last_run_state=last_state, resources=resources, tofu_error=False),
                "resources": len(resources) if resources else 0,
                "checked_at": cached["checked_at"] if cached else None,
                "error": None,
            }

        if not fresh:
            resources: list[str] | None = None
            error: str | None = None
            tofu_error = False
            problem = self.provider_problem(manifest)
            if problem is not None:
                error = problem[1]
                tofu_error = True
            else:
                try:
                    creds = self._store.provider_credentials(manifest.provider)
                    resources = self.state_list(manifest, self.step_env(manifest, creds))
                except (TofuError, EngineError, OSError, RuntimeError) as exc:
                    tofu_error = True
                    error = Scrubber(self.secret_values()).scrub(str(exc))
                    log.warning("state probe failed for %s: %s", manifest.id, error)
            cached = self._state_cache[manifest.id] = {
                "resources": resources,
                "tofu_error": tofu_error,
                "error": error,
                "checked_at": utcnow_iso(),
                "_at": time.monotonic(),
            }

        assert cached is not None
        return {
            "state": self.derive_state(running_action=None, last_run_state=last_state, resources=cached["resources"], tofu_error=cached["tofu_error"]),
            "resources": len(cached["resources"] or []),
            "checked_at": cached["checked_at"],
            "error": cached["error"],
        }

    # ------------------------------------------------------------------ status probe
    def probe_status(self, manifest: Manifest) -> dict[str, Any] | None:
        """Run `status.run`, persist status.json and return it. None if the manifest has no probe."""
        if manifest.status is None:
            return None
        self._last_probe[manifest.id] = time.monotonic()
        record: dict[str, Any] = {"generated_at": utcnow_iso(), "output": None, "error": None}
        problem = self.provider_problem(manifest)
        if problem is not None:
            record["error"] = problem[1]
            self._store.save_status(manifest.id, record)
            return record
        checkout = self._store.checkout_dir(manifest.id)
        if not (checkout / ".git").exists():
            record["error"] = "Source not checked out yet"
            self._store.save_status(manifest.id, record)
            return record
        env = self.step_env(manifest, self._store.provider_credentials(manifest.provider))
        scrubber = Scrubber(self.secret_values())
        try:
            proc = subprocess.run(
                manifest.status.run,
                shell=True,
                cwd=str(checkout),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=PROBE_TIMEOUT_S,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            record["error"] = f"Status probe timed out after {PROBE_TIMEOUT_S}s"
            self._store.save_status(manifest.id, record)
            return record
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-500:]
            record["error"] = scrubber.scrub(f"Status probe exited {proc.returncode}: {tail}")
        else:
            try:
                record["output"] = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                record["error"] = scrubber.scrub(f"Status probe did not print JSON: {exc}")
        self._store.save_status(manifest.id, record)
        return record

    def status_record(self, manifest: Manifest) -> dict[str, Any] | None:
        return self._store.get_status(manifest.id)

    # ------------------------------------------------------------------ jobs
    def start_job(self, manifest: Manifest, action: str) -> str:
        if action not in ("on", "off"):
            raise EngineError(f"Unknown action '{action}'", "bad_action", 400)
        problem = self.provider_problem(manifest)
        if problem is not None:
            raise EngineError(problem[1], problem[0], 409)
        creds = self._store.provider_credentials(manifest.provider)
        env = self.step_env(manifest, creds)
        scrubber = Scrubber(self.secret_values() + self._host_secret_values())
        steps = [StepSpec(s.name, s.run) for s in (manifest.on if action == "on" else manifest.off)]
        self._store.ensure_usecase_dirs(manifest.id)

        def prelude(writer: LogWriter) -> None:
            self.prepare(manifest, env, writer.write)

        try:
            job_id = self._jobs.start(
                manifest.id,
                action,
                steps,
                cwd=self._store.checkout_dir(manifest.id),
                env=env,
                scrubber=scrubber,
                prelude=prelude,
                on_finish=self._after_job,
            )
        except JobConflict as exc:
            raise EngineError(str(exc), "job_running", 409) from None
        self.invalidate(manifest.id)
        return job_id

    @staticmethod
    def _host_secret_values() -> list[str]:
        values = [os.environ.get("SWITCHBOARD_PASSWORD", ""), os.environ.get("SWITCHBOARD_SECRET_KEY", "")]
        key_file = Path(os.environ.get("ZSCALER_API_KEY_FILE") or "/run/secrets/zscaler_api_key")
        try:
            values.append(key_file.read_text(encoding="utf-8").strip())
        except OSError:
            pass
        return [v for v in values if v]

    def _after_job(self, job: dict[str, Any]) -> None:
        self.invalidate(job["usecase"])
        try:
            manifest = self.manifest(job["usecase"])
        except EngineError:
            return
        try:
            result = self.state(manifest, force=True)
            if result["state"] == "on":
                self.probe_status(manifest)
        except Exception:  # noqa: BLE001
            log.exception("post-job refresh failed for %s", job["usecase"])

    # ------------------------------------------------------------------ API shapes
    def summary(self, manifest: Manifest) -> dict[str, Any]:
        st = self.state(manifest)
        runs = self._jobs.list_runs(manifest.id, limit=1)
        last = runs[0] if runs else None
        return {
            "id": manifest.id,
            "name": manifest.name,
            "provider": manifest.provider,
            "summary": manifest.summary,
            "state": st["state"],
            "resources": st["resources"],
            "state_error": st["error"],
            "last_run": (
                {"job_id": last["id"], "action": last["action"], "state": last["state"], "ended": last["ended"]} if last else None
            ),
            "provider_connected": self.provider_record(manifest) is not None,
            "provider_supported": self.provider_supports_usecases(manifest),
        }

    def detail(self, manifest: Manifest) -> dict[str, Any]:
        status = self.status_record(manifest)
        runs = self._jobs.list_runs(manifest.id, limit=20)
        return {
            **self.summary(manifest),
            "description": manifest.description,
            "procedure": {
                "on": [s.to_api() for s in manifest.on],
                "off": [s.to_api() for s in manifest.off],
            },
            "source": {"git": manifest.source_git, "ref": manifest.source_ref, "commit": self.current_commit(manifest)},
            "status": status["output"] if status else None,
            "status_at": status["generated_at"] if status else None,
            "status_error": status["error"] if status else None,
            "status_interval_s": manifest.status.interval_s if manifest.status else None,
            "tags": manifest.tags,
            "effects": {"on": manifest.effects_on.to_api(), "off": manifest.effects_off.to_api()},
            "topology": manifest.topology.to_api(),
            "runs": [
                {"job_id": r["id"], "action": r["action"], "state": r["state"], "started": r["started"], "ended": r["ended"]}
                for r in runs
            ],
        }

    # ------------------------------------------------------------------ outline (plan, never apply)
    @staticmethod
    def parse_address(address: str) -> tuple[str, str, str | None]:
        """'module.a.data.aws_ami.x[0]' -> (type, name, module) using resource-address grammar."""
        module_parts: list[str] = []
        parts = address.split(".")
        i = 0
        while i + 1 < len(parts) and parts[i] == "module":
            module_parts.append(parts[i + 1].split("[", 1)[0])
            i += 2
        rest = parts[i:]
        if rest and rest[0] == "data":
            rest = rest[1:]
        rtype = rest[0] if rest else ""
        name = ".".join(rest[1:]).split("[", 1)[0] if len(rest) > 1 else ""
        return rtype, name, ".".join(module_parts) or None

    @classmethod
    def parse_plan_json(cls, output: str) -> dict[str, Any]:
        """Parse `tofu plan -json` (newline-delimited). Returns
        {"changes": {create|update|destroy|read|unchanged: [entry…]}, "summary": {…}|None,
         "diagnostics": [str…], "other": [non-JSON lines]}. `replace` counts as create + destroy."""
        changes: dict[str, list[dict[str, Any]]] = {"create": [], "update": [], "destroy": [], "read": [], "unchanged": []}
        diagnostics: list[str] = []
        other: list[str] = []
        summary: dict[str, Any] | None = None
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                other.append(line)
                continue
            if not isinstance(msg, dict):
                other.append(line)
                continue
            kind = msg.get("type")
            if kind == "planned_change":
                change = msg.get("change") or {}
                res = change.get("resource") or {}
                addr = res.get("addr") or ""
                ptype, pname, pmodule = cls.parse_address(addr)
                entry = {
                    "address": addr,
                    "type": res.get("resource_type") or ptype,
                    "name": res.get("resource_name") or pname,
                    "module": res.get("module") or pmodule,
                }
                action = str(change.get("action") or "")
                if action == "replace":
                    changes["destroy"].append(dict(entry, replace=True))
                    changes["create"].append(dict(entry, replace=True))
                elif action in PLAN_ACTIONS:
                    changes[PLAN_ACTIONS[action]].append(entry)
                else:
                    changes["update"].append(dict(entry, action=action))
            elif kind == "change_summary":
                summary = msg.get("changes") or {}
            elif kind == "diagnostic":
                diag = msg.get("diagnostic") or {}
                if diag.get("severity") == "error":
                    text = " ".join(str(diag.get("summary") or msg.get("@message") or "").split())
                    detail = " ".join(str(diag.get("detail") or "").split())
                    diagnostics.append(f"{text}: {detail}" if detail else text)
        return {"changes": changes, "summary": summary, "diagnostics": diagnostics, "other": other}

    @staticmethod
    def _group_by_type(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Stable order: by type in order of first appearance, then arrival order."""
        first_seen: dict[str, int] = {}
        for e in entries:
            first_seen.setdefault(e["type"], len(first_seen))
        return sorted(entries, key=lambda e: first_seen[e["type"]])

    def _outline_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._locks_guard:
            lock = self._outline_locks.get(key)
            if lock is None:
                lock = self._outline_locks[key] = threading.Lock()
            return lock

    def outline(self, manifest: Manifest, action: str) -> dict[str, Any]:
        """What ON/OFF will do: a real `tofu plan` (never apply) plus the manifest's declared effects."""
        return self.plan(manifest, action)["outline"]

    def plan(self, manifest: Manifest, action: str, *, refresh: bool = False) -> dict[str, Any]:
        """One plan run, two consumers (v1.4): `outline` (the v1.1 shape) and, for ON, `show` (the
        `tofu show -json` document the planned topology is drawn from). Cached 60 s per action;
        `refresh` re-plans; `invalidate` drops it when a job ends. 409 while a job runs."""
        if action not in ("on", "off"):
            raise EngineError(f"Unknown action '{action}'", "bad_action", 400)
        if self._jobs.running_job(manifest.id) is not None:
            raise EngineError(f"A job is already running for use case '{manifest.id}'", "job_running", 409)
        key = (manifest.id, action)
        with self._outline_lock(key):
            cached = self._plan_cache.get(key)
            if cached is not None and not refresh and (time.monotonic() - cached["_at"]) < OUTLINE_CACHE_TTL_S:
                return {k: v for k, v in cached.items() if k != "_at"}
            result = self._plan(manifest, action)
            self._plan_cache[key] = {**result, "_at": time.monotonic()}
            return result

    def _plan(self, manifest: Manifest, action: str) -> dict[str, Any]:
        provider_rec = self.provider_record(manifest)
        provider = self._providers.get(manifest.provider)
        bucket = provider.state_bucket(provider_rec) if (provider and provider_rec) else None
        steps = manifest.on if action == "on" else manifest.off
        outline: dict[str, Any] = {
            "action": action,
            "plan": None,
            "declared": manifest.effects(action).to_api(),
            "steps": [s.to_api() for s in steps],
            "retained_state": {"backend": "s3", "bucket": bucket, "key": manifest.state_key, "region": STATE_BACKEND_REGION},
        }
        result: dict[str, Any] = {"outline": outline, "show": None, "show_error": None, "generated_at": utcnow_iso()}

        def failed(error: str) -> dict[str, Any]:
            outline["plan"] = {"ok": False, "generated_at": result["generated_at"], "error": Scrubber(self.secret_values()).scrub(error)}
            return result

        problem = self.provider_problem(manifest)
        if problem is not None:
            return failed(problem[1])
        try:
            creds = self._store.provider_credentials(manifest.provider)
        except RuntimeError as exc:
            return failed(str(exc))
        env = self.step_env(manifest, creds)
        try:
            if manifest.id not in self._initialised:
                self.prepare(manifest, env)
        except FileNotFoundError as exc:
            return failed(f"'{exc.filename or self._tofu}' is not installed or not on PATH; a plan needs git and OpenTofu")
        except (TofuError, EngineError, OSError) as exc:
            return failed(f"Could not prepare the checkout: {exc}")

        checkout = self._store.checkout_dir(manifest.id)
        plan_file = PLAN_FILE.format(action=action)  # relative to -chdir
        plan_path = checkout / manifest.terraform_dir / plan_file
        args = [self._tofu, f"-chdir={manifest.terraform_dir}", "plan", "-json", "-input=false", "-lock=false", "-refresh=true", f"-out={plan_file}"]
        if action == "off":
            args.append("-destroy")
        started = time.monotonic()
        try:
            try:
                code, out = self._run(args, cwd=checkout, env=env, timeout=TOFU_PLAN_TIMEOUT_S)
            except FileNotFoundError:
                return failed(f"'{self._tofu}' is not installed or not on PATH; a plan needs OpenTofu")
            except (TofuError, OSError) as exc:
                return failed(f"tofu plan failed: {exc}")
            parsed = self.parse_plan_json(out)
            if code != 0:
                tail = parsed["diagnostics"] or parsed["other"]
                reason = tail[-1] if tail else f"exit code {code}"
                return failed(f"tofu plan exited {code}: {reason}")
            if action == "on":
                # The graph needs the plan's values and configuration references: `show -json` on the
                # plan file. Its failure leaves the outline intact; the topology reports the error.
                show_args = [self._tofu, f"-chdir={manifest.terraform_dir}", "show", "-json", "-no-color", plan_file]
                try:
                    show_code, show_out = self._run(show_args, cwd=checkout, env=env, timeout=TOFU_SHOW_TIMEOUT_S)
                    if show_code != 0:
                        raise TofuError(f"tofu show exited {show_code}: {show_out.strip()[-500:]}")
                    document = json.loads(show_out)
                    if not isinstance(document, dict):
                        raise TofuError("tofu show printed no plan document")
                    result["show"] = document
                except (TofuError, OSError, ValueError) as exc:
                    result["show_error"] = Scrubber(self.secret_values()).scrub(f"tofu show failed: {exc}")
                    log.warning("plan: show failed for %s: %s", manifest.id, result["show_error"])
        finally:
            try:
                plan_path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("could not delete plan file %s: %s", plan_path, exc)

        changes = parsed["changes"]
        changed_addrs = {e["address"] for lst in changes.values() for e in lst}
        # `planned_change` is only emitted for real changes; what is in state and untouched is unchanged.
        try:
            state_addrs = self.state_list(manifest, env)
        except (TofuError, EngineError, OSError, RuntimeError) as exc:
            log.warning("outline: state list failed for %s: %s", manifest.id, exc)
            state_addrs = []
        unchanged = list(changes["unchanged"])
        seen = {e["address"] for e in unchanged}
        for addr in state_addrs:
            if addr in changed_addrs or addr in seen:
                continue
            rtype, rname, module = self.parse_address(addr)
            unchanged.append({"address": addr, "type": rtype, "name": rname, "module": module})
        plan: dict[str, Any] = {
            "ok": True,
            "generated_at": result["generated_at"],
            "duration_s": round(time.monotonic() - started, 1),
            "create": self._group_by_type(changes["create"]),
            "update": self._group_by_type(changes["update"]),
            "destroy": self._group_by_type(changes["destroy"]),
            "read": self._group_by_type(changes["read"]),
            "unchanged": self._group_by_type(unchanged),
            "change_summary": parsed["summary"],
        }
        plan["summary"] = {k: len(plan[k]) for k in ("create", "update", "destroy", "unchanged", "read")}
        outline["plan"] = plan
        return result

    # ------------------------------------------------------------------ topology (v1.2)
    def inventory(self, provider_id: str, *, refresh: bool = False) -> dict[str, Any]:
        """The provider's cached inventory; scans (and persists) when missing or `refresh`.
        Raises EngineError when the provider is unsupported or not connected."""
        provider = self._providers.get(provider_id)
        if provider is None or not provider.capabilities.get("inventory"):
            raise EngineError(f"Provider '{provider_id}' has no inventory", "provider_unsupported", 409)
        record = self._store.get_provider(provider_id)
        if not record or record.get("status") != "connected":
            raise EngineError(f"Provider '{provider_id}' is not connected", "provider_not_connected", 409)
        cached = self._store.get_inventory(provider_id)
        if cached and not refresh:
            return cached
        with self.inventory_lock:
            if not refresh:
                latest = self._store.get_inventory(provider_id)  # another thread may have scanned meanwhile
                if latest:
                    return latest
            creds = self._store.provider_credentials(provider_id)
            if creds is None:
                raise EngineError(f"Provider '{provider_id}' is not connected", "provider_not_connected", 409)
            inventory = provider.inventory(creds, record.get("regions") or [])
            self._store.save_inventory(provider_id, inventory)
            return inventory

    def topology(self, manifest: Manifest, *, refresh: bool = False) -> dict[str, Any]:
        """Provider-neutral graph of the use case from the cached inventory (see topology.py).
        Always 200-shaped: when nothing can be drawn, `nodes` is empty and `reason` says why."""
        key = (manifest.id, "topology")
        with self._outline_lock(key):
            cached = self._topology_cache.get(manifest.id)
            if cached is not None and not refresh and (time.monotonic() - cached["_at"]) < TOPOLOGY_CACHE_TTL_S:
                return {k: v for k, v in cached.items() if k != "_at"}
            result = self._topology(manifest, refresh)
            self._topology_cache[manifest.id] = {**result, "_at": time.monotonic()}
            return result

    def _topology(self, manifest: Manifest, refresh: bool) -> dict[str, Any]:
        st = self.state(manifest, force=refresh)
        status = self.status_record(manifest)
        base: dict[str, Any] = {
            "generated_at": utcnow_iso(),
            "state": st["state"],
            "register": "deployed",
            "inventory_at": None,
            "status_at": status["generated_at"] if status else None,
            "plan": None,
        }

        def empty(reason: str, **extra: Any) -> dict[str, Any]:
            return {**base, **extra, **build_graph(manifest, None, None), "reason": reason}

        problem = self.provider_problem(manifest)
        if problem is not None:
            provider = self._providers.get(manifest.provider)
            reason = problem[1]
            if problem[0] == "provider_not_connected":
                reason = f"Connect {provider.name if provider else manifest.provider} to plan what ON deploys ({problem[1]})"
            return empty(reason, register="declared")  # no plan possible: the frontend sketches the manifest's declared roles
        # Nothing deployed: draw what ON would deploy, from a real plan (v1.4). A job in flight
        # invalidated the cache and must not be planned against; it redraws when the job ends.
        if st["resources"] == 0 and st["state"] in ("off", "error", "turning_on", "turning_off"):
            if self._jobs.running_job(manifest.id) is not None:
                return empty("A job is running; the drawing regenerates when it ends", register="planned")
            return self._planned_topology(manifest, base, refresh)
        try:
            inventory = self.inventory(manifest.provider, refresh=refresh)
        except EngineError as exc:
            return empty(str(exc))
        except Exception as exc:  # noqa: BLE001 - a failed scan must not 500 the card
            log.exception("topology: inventory scan failed for %s", manifest.provider)
            stale = self._store.get_inventory(manifest.provider)
            if not stale:
                return empty(f"Inventory scan failed: {type(exc).__name__}")
            inventory = stale
            base["stale"] = True
        if not inventory.get("supported", True):
            return empty(str(inventory.get("reason") or f"Inventory is not built for provider '{manifest.provider}' yet"))
        base["inventory_at"] = inventory.get("generated_at")
        graph = build_graph(manifest, inventory, status["output"] if status else None)
        reason = None
        if not graph["nodes"]:
            tags = ", ".join(f"{k}={v}" for k, v in manifest.tags.items()) or "none declared"
            reason = f"No resources carrying the use case's tags ({tags}) in the inventory"
        else:
            schema = SCHEMAS.get(manifest.provider)
            if schema is not None:
                cached = self._state_cache.get(manifest.id) or {}
                self.source_index(manifest).attach_live(graph["nodes"], schema, cached.get("resources"))
        return {**base, **graph, "reason": reason}

    def source_index(self, manifest: Manifest) -> SourceIndex:
        """`resource` blocks in the checkout's terraform dir, for `source: {path, line}` on nodes."""
        schema = SCHEMAS.get(manifest.provider)
        return SourceIndex.scan(self._store.checkout_dir(manifest.id), manifest.terraform_dir, name_tag=schema.name_tag if schema else "Name")

    def _planned_topology(self, manifest: Manifest, base: dict[str, Any], refresh: bool) -> dict[str, Any]:
        """The planned register: the same graph from the ON plan's `show -json` document."""
        base = {**base, "register": "planned"}
        try:
            run = self.plan(manifest, "on", refresh=refresh)
        except EngineError as exc:
            return {**base, **build_graph(manifest, None, None), "reason": str(exc)}
        outline_plan = run["outline"]["plan"] or {}
        plan_info: dict[str, Any] = {
            "generated_at": run["generated_at"],
            "resources": int((outline_plan.get("summary") or {}).get("create", 0)) if outline_plan.get("ok") else 0,
            "error": None,
        }
        if not outline_plan.get("ok"):
            plan_info["error"] = outline_plan.get("error") or "plan failed"
            return {**base, "plan": plan_info, **build_graph(manifest, None, None), "reason": f"Plan failed: {plan_info['error']}"}
        if run["show"] is None:
            plan_info["error"] = run["show_error"] or "plan produced no document to draw"
            return {**base, "plan": plan_info, **build_graph(manifest, None, None), "reason": f"Plan could not be drawn: {plan_info['error']}"}
        graph = build_plan_graph(manifest, run["show"], self.source_index(manifest))
        reason = None if graph["nodes"] else "The plan declares nothing drawable (no networks, instances or gateways)"
        return {**base, "plan": plan_info, **graph, "reason": reason}

    # ------------------------------------------------------------------ code browser
    def _checkout_root(self, manifest: Manifest) -> Path:
        checkout = self._store.checkout_dir(manifest.id)
        if not (checkout / ".git").exists():
            raise EngineError("Source not checked out yet; connect the provider or run the use case once", "no_checkout", 404)
        return checkout.resolve()

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            with path.open("rb") as fh:
                return b"\x00" in fh.read(8192)
        except OSError:
            return True

    def code_tree(self, manifest: Manifest) -> dict[str, Any]:
        root = self._checkout_root(manifest)
        files: list[dict[str, Any]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink() or self._is_binary(path):
                    continue
                files.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
        return {"commit": self.current_commit(manifest), "files": files}

    def code_file(self, manifest: Manifest, rel_path: str) -> dict[str, Any]:
        root = self._checkout_root(manifest)
        rel = Path(rel_path)
        if rel.is_absolute() or ".." in rel.parts or any(part in SKIP_DIRS for part in rel.parts):
            raise EngineError("Path must be inside the checkout", "bad_path", 400)
        path = (root / rel).resolve()
        if root not in path.parents or not path.is_file():
            raise EngineError(f"No such file: {rel_path}", "not_found", 404)
        if path.stat().st_size > CODE_FILE_LIMIT:
            raise EngineError("File too large to display", "too_large", 413)
        if self._is_binary(path):
            raise EngineError("Binary file", "binary", 415)
        content = path.read_text(encoding="utf-8", errors="replace")
        return {"path": str(rel), "language": self.language_for(path), "content": content}

    @staticmethod
    def language_for(path: Path) -> str:
        if path.suffix.lower() in LANGUAGES:
            return LANGUAGES[path.suffix.lower()]
        name = path.name.lower()
        if name in ("makefile",) or name.startswith("dockerfile"):
            return "shell"
        return "text"

    # ------------------------------------------------------------------ background
    def start_background(self) -> None:
        threading.Thread(target=self._warm_up, name="engine-warmup", daemon=True).start()
        threading.Thread(target=self._refresh_loop, name="engine-refresh", daemon=True).start()

    def stop_background(self) -> None:
        self._stop.set()

    def warm_up(self) -> None:
        """Derive every use case's state once (checkout + init + state list) and probe if on."""
        manifests, errors = self.manifests()
        for uc_id, err in errors.items():
            log.error("manifest %s ignored: %s", uc_id, err)
        for manifest in manifests.values():
            try:
                result = self.state(manifest, force=True)
                log.info("use case %s: %s (%d resources)", manifest.id, result["state"], result["resources"])
                if result["state"] == "on":
                    self.probe_status(manifest)
            except Exception:  # noqa: BLE001
                log.exception("warm-up failed for %s", manifest.id)

    def _warm_up(self) -> None:
        self.warm_up()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(REFRESH_LOOP_S):
            try:
                manifests, _ = self.manifests()
                for manifest in manifests.values():
                    if manifest.status is None or self._jobs.running_job(manifest.id):
                        continue
                    cached = self._state_cache.get(manifest.id)
                    if not cached or not cached["resources"]:
                        continue
                    last = self._last_probe.get(manifest.id, 0.0)
                    if time.monotonic() - last >= manifest.status.interval_s:
                        self.probe_status(manifest)
            except Exception:  # noqa: BLE001
                log.exception("status refresh loop error")


def iso_age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()
