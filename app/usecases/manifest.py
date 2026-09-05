"""Schema, loader and validation for `usecases/<id>/usecase.yaml` (see SPEC.md)."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ID_RE = re.compile(r"^[a-z0-9-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KNOWN_SECRETS = frozenset({"zscaler_oneapi"})
TOP_LEVEL_KEYS = frozenset(
    {"id", "name", "provider", "summary", "description", "source", "terraform", "env", "secrets", "on", "off", "status", "tags", "effects"}
)
EFFECT_KEYS = ("creates", "destroys", "retains")
DEFAULT_STATUS_INTERVAL_S = 60


class ManifestError(ValueError):
    """A manifest failed validation. The message names the file and the offending field."""


@dataclass(frozen=True)
class Step:
    name: str
    run: str

    def to_api(self) -> dict[str, str]:
        return {"name": self.name, "run": self.run}


@dataclass(frozen=True)
class StatusProbe:
    run: str
    interval_s: int = DEFAULT_STATUS_INTERVAL_S


@dataclass(frozen=True)
class Effects:
    """What one action does outside OpenTofu, in prose (the plan supplies the rest)."""

    creates: tuple[str, ...] = ()
    destroys: tuple[str, ...] = ()
    retains: tuple[str, ...] = ()

    def to_api(self) -> dict[str, list[str]]:
        return {"creates": list(self.creates), "destroys": list(self.destroys), "retains": list(self.retains)}


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    provider: str
    summary: str
    description: str
    source_git: str
    source_ref: str
    terraform_dir: str
    state_key: str
    on: tuple[Step, ...]
    off: tuple[Step, ...]
    env: dict[str, str] = field(default_factory=dict)
    secrets: tuple[str, ...] = ()
    status: StatusProbe | None = None
    tags: dict[str, str] = field(default_factory=dict)
    effects_on: Effects = field(default_factory=Effects)
    effects_off: Effects = field(default_factory=Effects)
    path: Path | None = None

    def effects(self, action: str) -> Effects:
        return self.effects_on if action == "on" else self.effects_off


def _fail(where: str, msg: str) -> ManifestError:
    return ManifestError(f"{where}: {msg}")


def _req_str(data: dict[str, Any], key: str, where: str, *, allow_empty: bool = False) -> str:
    if key not in data:
        raise _fail(where, f"missing required field '{key}'")
    value = data[key]
    if not isinstance(value, str):
        raise _fail(where, f"'{key}' must be a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise _fail(where, f"'{key}' must not be empty")
    return value


def _opt_str_map(data: dict[str, Any], key: str, where: str, *, name_re: re.Pattern[str] | None = None) -> dict[str, str]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(where, f"'{key}' must be a mapping of string to string")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or (name_re and not name_re.match(k)):
            raise _fail(where, f"'{key}' has an invalid key {k!r}")
        if isinstance(v, bool) or not isinstance(v, (str, int, float)):
            raise _fail(where, f"'{key}.{k}' must be a string")
        out[k] = str(v)
    return out


def _steps(data: dict[str, Any], key: str, where: str) -> tuple[Step, ...]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise _fail(where, f"'{key}' must be a non-empty list of steps")
    steps: list[Step] = []
    for i, item in enumerate(value, start=1):
        w = f"{where} {key}[{i}]"
        if not isinstance(item, dict):
            raise _fail(w, "each step must be a mapping with 'name' and 'run'")
        extra = set(item) - {"name", "run"}
        if extra:
            raise _fail(w, f"unknown step field(s): {', '.join(sorted(extra))}")
        steps.append(Step(name=_req_str(item, "name", w), run=_req_str(item, "run", w)))
    return tuple(steps)


def _str_list(data: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise _fail(where, f"'{key}' must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _effects(data: dict[str, Any], where: str) -> tuple[Effects, Effects]:
    """Optional `effects: {on: {...}, off: {...}}`; keys may be quoted or YAML-1.1 booleans."""
    raw = data.get("effects")
    if raw is None:
        return Effects(), Effects()
    if not isinstance(raw, dict):
        raise _fail(where, "'effects' must be a mapping with optional 'on' and 'off'")
    raw = {("on" if k is True else "off" if k is False else k): v for k, v in raw.items()}
    unknown = set(raw) - {"on", "off"}
    if unknown:
        raise _fail(f"{where} effects", f"unknown field(s): {', '.join(sorted(str(k) for k in unknown))}")
    out: list[Effects] = []
    for action in ("on", "off"):
        block = raw.get(action)
        if block is None:
            out.append(Effects())
            continue
        w = f"{where} effects.{action}"
        if not isinstance(block, dict):
            raise _fail(w, "must be a mapping with 'creates', 'destroys' and/or 'retains'")
        extra = set(block) - set(EFFECT_KEYS)
        if extra:
            raise _fail(w, f"unknown field(s): {', '.join(sorted(str(k) for k in extra))}")
        out.append(Effects(creates=_str_list(block, "creates", w), destroys=_str_list(block, "destroys", w), retains=_str_list(block, "retains", w)))
    return out[0], out[1]


def _relative_path(value: str, where: str, key: str) -> str:
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise _fail(where, f"'{key}' must be a relative path inside the checkout (no '..')")
    return value


def parse_manifest(
    data: Any,
    *,
    expected_id: str | None,
    provider_ids: Collection[str],
    where: str = "usecase.yaml",
    path: Path | None = None,
) -> Manifest:
    if not isinstance(data, dict):
        raise _fail(where, "top level must be a mapping")
    # YAML 1.1 reads bare `on:` / `off:` keys as booleans; map them back to the spec's names.
    data = {("on" if k is True else "off" if k is False else k): v for k, v in data.items()}
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise _fail(where, f"unknown field(s): {', '.join(sorted(str(k) for k in unknown))}")

    uc_id = _req_str(data, "id", where)
    if not ID_RE.match(uc_id):
        raise _fail(where, f"'id' must match [a-z0-9-]+, got {uc_id!r}")
    if expected_id is not None and uc_id != expected_id:
        raise _fail(where, f"'id' is {uc_id!r} but the directory is named {expected_id!r}; they must match")

    provider = _req_str(data, "provider", where)
    if provider not in provider_ids:
        raise _fail(where, f"'provider' {provider!r} is not a registered provider (known: {', '.join(sorted(provider_ids)) or 'none'})")

    source = data.get("source")
    if not isinstance(source, dict):
        raise _fail(where, "'source' must be a mapping with 'git' and 'ref'")
    src_where = f"{where} source"
    git_url = _req_str(source, "git", src_where)
    if not re.match(r"^(https?://|ssh://|git@)", git_url):
        raise _fail(src_where, f"'git' must be an https://, ssh:// or git@ URL, got {git_url!r}")
    ref = _req_str(source, "ref", src_where)
    if ref.startswith("-") or any(ch.isspace() for ch in ref):
        raise _fail(src_where, f"'ref' is not a valid git ref: {ref!r}")

    tf = data.get("terraform")
    if not isinstance(tf, dict):
        raise _fail(where, "'terraform' must be a mapping with 'dir' and 'state_key'")
    tf_where = f"{where} terraform"
    tf_dir = _relative_path(_req_str(tf, "dir", tf_where), tf_where, "dir")
    state_key = _req_str(tf, "state_key", tf_where)
    if state_key.startswith("/") or ".." in Path(state_key).parts:
        raise _fail(tf_where, "'state_key' must be a plain S3 object key")

    secrets_raw = data.get("secrets") or []
    if not isinstance(secrets_raw, list) or not all(isinstance(s, str) for s in secrets_raw):
        raise _fail(where, "'secrets' must be a list of secret names")
    unknown_secrets = [s for s in secrets_raw if s not in KNOWN_SECRETS]
    if unknown_secrets:
        raise _fail(where, f"unknown secret(s): {', '.join(unknown_secrets)} (known: {', '.join(sorted(KNOWN_SECRETS))})")

    status: StatusProbe | None = None
    if data.get("status") is not None:
        st = data["status"]
        if not isinstance(st, dict):
            raise _fail(where, "'status' must be a mapping with 'run' and optional 'interval_s'")
        st_where = f"{where} status"
        extra = set(st) - {"run", "interval_s"}
        if extra:
            raise _fail(st_where, f"unknown field(s): {', '.join(sorted(extra))}")
        interval = st.get("interval_s", DEFAULT_STATUS_INTERVAL_S)
        if isinstance(interval, bool) or not isinstance(interval, int) or interval < 5:
            raise _fail(st_where, "'interval_s' must be an integer >= 5")
        status = StatusProbe(run=_req_str(st, "run", st_where), interval_s=interval)

    effects_on, effects_off = _effects(data, where)
    return Manifest(
        id=uc_id,
        name=_req_str(data, "name", where),
        provider=provider,
        summary=_req_str(data, "summary", where),
        description=_req_str(data, "description", where, allow_empty=True) if "description" in data else "",
        source_git=git_url,
        source_ref=ref,
        terraform_dir=tf_dir,
        state_key=state_key,
        on=_steps(data, "on", where),
        off=_steps(data, "off", where),
        env=_opt_str_map(data, "env", where, name_re=ENV_NAME_RE),
        secrets=tuple(secrets_raw),
        status=status,
        tags=_opt_str_map(data, "tags", where),
        effects_on=effects_on,
        effects_off=effects_off,
        path=path,
    )


def load_manifest(path: Path, provider_ids: Collection[str]) -> Manifest:
    """Load one manifest; `id` must equal the directory name."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"{path}: cannot read manifest: {exc.strerror}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: invalid YAML: {exc}") from exc
    return parse_manifest(data, expected_id=path.parent.name, provider_ids=provider_ids, where=str(path), path=path)


def load_all(root: Path, provider_ids: Collection[str]) -> tuple[dict[str, Manifest], dict[str, str]]:
    """All manifests under `root/<id>/usecase.yaml`. Returns (valid by id, errors by directory)."""
    manifests: dict[str, Manifest] = {}
    errors: dict[str, str] = {}
    if not root.is_dir():
        return manifests, errors
    for uc_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        manifest_path = uc_dir / "usecase.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifests[uc_dir.name] = load_manifest(manifest_path, provider_ids)
        except ManifestError as exc:
            errors[uc_dir.name] = str(exc)
    return manifests, errors
