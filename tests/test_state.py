"""State derivation from (mocked) `tofu state list` output and the SPEC state rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.jobs import JobRunner
from app.store import Store
from app.usecases.engine import Engine, TofuError
from app.usecases.manifest import load_manifest
from tests.conftest import GOOD_MANIFEST, write_manifest

STATE_LIST_OUTPUT = """\
aws_instance.pse
aws_vpc.pse
module.client.aws_instance.client
module.client.aws_vpc.this
data.aws_ami.al2023
"""


def test_parse_state_list_counts_resources() -> None:
    assert Engine.parse_state_list(STATE_LIST_OUTPUT) == [
        "aws_instance.pse",
        "aws_vpc.pse",
        "module.client.aws_instance.client",
        "module.client.aws_vpc.this"]


def test_parse_state_list_drops_blank_and_diagnostic_lines() -> None:
    out = "\nWarning: something\n╷\n│ detail\n╵\naws_instance.pse\n\n"
    assert Engine.parse_state_list(out) == ["aws_instance.pse"]
    assert Engine.parse_state_list("") == []


@pytest.mark.parametrize(
    ("running", "last", "resources", "err", "expected"),
    [
        ("on", None, None, False, "turning_on"),
        ("off", "succeeded", ["a"], False, "turning_off"),
        (None, "succeeded", ["a", "b"], False, "on"),
        (None, None, [], False, "off"),
        (None, "succeeded", None, True, "unknown"),
        (None, "failed", ["a"], False, "error"),
        (None, "failed", [], False, "error"),
        ("on", "failed", [], False, "turning_on"),
    ],
)
def test_derive_state_rules(running: str | None, last: str | None, resources: list[str] | None, err: bool, expected: str) -> None:
    assert Engine.derive_state(running_action=running, last_run_state=last, resources=resources, tofu_error=err) == expected


class _FakeTofuEngine(Engine):
    """Engine with the tofu subprocess replaced by canned output."""

    def __init__(self, *args: object, output: str = "", code: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._output = output
        self._code = code
        self.calls: list[list[str]] = []

    def prepare(self, manifest, env, log_line=None):  # type: ignore[override]
        self._initialised.add(manifest.id)
        return "deadbeef"

    def _run(self, args, *, cwd, env, timeout, log_line=None):  # type: ignore[override]
        self.calls.append(args)
        return self._code, self._output


@pytest.fixture
def engine_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "data")
    store.save_provider(
        "aws",
        {
            "status": "connected",
            "identity": {"account": "257300000000", "arn": "arn:aws:sts::257300000000:assumed-role/x/y", "alias": None},
            "regions": ["eu-central-1"],
            "credentials": store.encrypt({"access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "s3cr3t-value", "session_token": None}),
            "connected_at": "2026-09-05T10:00:00+00:00",
        },
    )
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", GOOD_MANIFEST)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", {"aws"})
    from app.providers import build_registry

    providers = build_registry(store.pricing_cache_path)
    return store, providers, manifest, tmp_path / "usecases"


def test_state_on_from_tofu_output(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output=STATE_LIST_OUTPUT)
    result = engine.state(manifest)
    assert result["state"] == "on"
    assert result["resources"] == 4  # the fixture's data source is not a resource
    assert engine.calls[-1][:3] == ["tofu", "-chdir=terraform", "state"]


def test_state_off_when_state_list_empty(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output="")
    assert engine.state(manifest)["state"] == "off"


def test_state_unknown_on_tofu_error(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output="Error: Failed to load state", code=1)
    result = engine.state(manifest)
    assert result["state"] == "unknown"
    assert "tofu state list failed" in result["error"]


def test_state_unknown_when_provider_disconnected(engine_env) -> None:
    store, providers, manifest, root = engine_env
    store.delete_provider("aws")
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output=STATE_LIST_OUTPUT)
    result = engine.state(manifest)
    assert result["state"] == "unknown"
    assert engine.calls == []


def test_state_is_cached_until_invalidated(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output=STATE_LIST_OUTPUT)
    engine.state(manifest)
    engine.state(manifest)
    assert len(engine.calls) == 1
    engine.invalidate(manifest.id)
    engine.state(manifest)
    assert len(engine.calls) == 2


def test_state_error_when_last_job_failed(engine_env) -> None:
    store, providers, manifest, root = engine_env
    store.save_run(
        manifest.id,
        {"id": "20260905T100000Z-abc123", "usecase": manifest.id, "action": "on", "state": "failed", "steps": [], "started": "2026-09-05T10:00:00+00:00", "ended": "2026-09-05T10:01:00+00:00"},
    )
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output=STATE_LIST_OUTPUT)
    assert engine.state(manifest)["state"] == "error"


def test_step_env_is_minimal(engine_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store, providers, manifest, root = engine_env
    monkeypatch.setenv("LEAKY_HOST_VAR", "should-not-appear")
    monkeypatch.setenv("ZS_ISSUER", "https://issuer.example")
    monkeypatch.setenv("ZS_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZPA_CUSTOMER_ID", "1234")
    key_file = tmp_path / "zscaler_api_key"
    key_file.write_text("api-key-value\n")
    monkeypatch.setenv("ZSCALER_API_KEY_FILE", str(key_file))
    engine = Engine(store, providers, JobRunner(store), root)
    env = engine.step_env(manifest, store.provider_credentials("aws"))
    assert set(env) == {"PATH", "HOME", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION", "ZS_ISSUER", "ZS_CLIENT_ID", "ZPA_CUSTOMER_ID"}
    assert env["HOME"] == str(store.usecase_dir(manifest.id))
    assert env["AWS_SECRET_ACCESS_KEY"] == "s3cr3t-value"
    link = Path(env["HOME"]) / ".zscaler_api_key"
    assert link.is_symlink() and link.read_text() == "api-key-value\n"


def test_tofu_init_arguments(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output="")
    engine.tofu_init(manifest, {"PATH": "/usr/bin"})
    args = engine.calls[-1]
    assert args[:4] == ["tofu", "-chdir=terraform", "init", "-input=false"]
    assert "-reconfigure" in args
    assert "-backend-config=bucket=zs-lab-tfstate-257300000000" in args
    assert "-backend-config=key=usecases/zpa-private-service-edge/terraform.tfstate" in args
    assert "-backend-config=region=eu-central-1" in args
    assert "-backend-config=use_lockfile=true" in args


def test_tofu_init_failure_raises(engine_env) -> None:
    store, providers, manifest, root = engine_env
    engine = _FakeTofuEngine(store, providers, JobRunner(store), root, output="Error: bad backend", code=1)
    with pytest.raises(TofuError, match="tofu init failed"):
        engine.tofu_init(manifest, {"PATH": "/usr/bin"})


def test_parse_state_list_drops_data_sources():
    from app.usecases.engine import Engine
    out = "aws_vpc.lab\ndata.aws_ami.al2023\nmodule.net.data.aws_caller_identity.me\naws_instance.pse\n"
    assert Engine.parse_state_list(out) == ["aws_vpc.lab", "aws_instance.pse"]
