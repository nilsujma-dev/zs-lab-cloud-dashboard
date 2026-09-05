"""Procedure outline (v1.1 §B): plan-JSON parsing, the outline response for on/off, failure
modes that must never 500, the 60 s cache, the 409 while a job runs, and the manifest
`effects` block. tofu is replaced by canned output; no network."""

from __future__ import annotations

import errno
import json
import time
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from app.jobs import JobRunner
from app.providers import build_registry
from app.store import Store
from app.usecases.engine import Engine, EngineError, TofuError
from app.usecases.manifest import ManifestError, load_manifest, parse_manifest
from tests.conftest import GOOD_MANIFEST, TEST_PASSWORD, write_manifest

EFFECTS = """\
effects:
  "on":
    creates:
      - "ZPA Service Edge Group, App Connector Groups and provisioning keys"
      - "Three SSM SecureString parameters under /zpa-lab/"
    retains: []
  "off":
    destroys:
      - "Everything OpenTofu manages in this use case (see plan)"
    retains:
      - "ZPA groups and provisioning keys"
      - "SSM parameters under /zpa-lab/"
      - "The S3 state object (versioned) and the remote lock"
      - "Enrolled Service Edge / App Connector entries in ZPA"
"""
MANIFEST_WITH_EFFECTS = GOOD_MANIFEST + EFFECTS


def _msg(kind: str, **payload: object) -> str:
    return json.dumps({"@level": "info", "@message": kind, "type": kind, **payload})


def _change(addr: str, rtype: str, name: str, action: str, module: str | None = None) -> str:
    res = {"addr": addr, "resource_type": rtype, "resource_name": name, "module": module or "", "implied_provider": "aws", "resource_key": None}
    return _msg("planned_change", change={"resource": res, "action": action})


CREATE_PLAN = "\n".join([
    _msg("version", tofu="1.12.6", ui="1.2"),
    _change("aws_vpc.pse", "aws_vpc", "pse", "create"),
    _change("aws_instance.pse", "aws_instance", "pse", "create"),
    _change("module.client.aws_instance.client[0]", "aws_instance", "client", "create", "module.client"),
    _change("aws_eip.pse", "aws_eip", "pse", "replace"),
    _change("aws_security_group.pse", "aws_security_group", "pse", "update"),
    _change("data.aws_ami.al2023", "aws_ami", "al2023", "read"),
    _msg("change_summary", changes={"add": 4, "change": 1, "remove": 1, "import": 0, "operation": "plan"}),
])
DESTROY_PLAN = "\n".join([
    _change("aws_instance.pse", "aws_instance", "pse", "delete"),
    _change("aws_vpc.pse", "aws_vpc", "pse", "delete"),
    _change("aws_nat_gateway.b", "aws_nat_gateway", "b", "delete"),
    _msg("change_summary", changes={"add": 0, "change": 0, "remove": 3, "import": 0, "operation": "plan"}),
])
EMPTY_PLAN = "\n".join([_msg("change_summary", changes={"add": 0, "change": 0, "remove": 0, "import": 0, "operation": "plan"})])
STATE = "aws_instance.pse\naws_vpc.pse\naws_nat_gateway.b\nmodule.client.aws_instance.client[0]\n"


# ---------------------------------------------------------------- parsing
def test_parse_plan_json_actions_and_summary() -> None:
    parsed = Engine.parse_plan_json(CREATE_PLAN)
    ch = parsed["changes"]
    assert [e["address"] for e in ch["create"]] == ["aws_vpc.pse", "aws_instance.pse", "module.client.aws_instance.client[0]", "aws_eip.pse"]
    assert ch["create"][2] == {"address": "module.client.aws_instance.client[0]", "type": "aws_instance", "name": "client", "module": "module.client"}
    assert [e["address"] for e in ch["destroy"]] == ["aws_eip.pse"] and ch["destroy"][0]["replace"] is True
    assert [e["address"] for e in ch["update"]] == ["aws_security_group.pse"]
    assert [e["address"] for e in ch["read"]] == ["data.aws_ami.al2023"]
    assert ch["unchanged"] == []
    assert parsed["summary"] == {"add": 4, "change": 1, "remove": 1, "import": 0, "operation": "plan"}
    assert parsed["diagnostics"] == [] and parsed["other"] == []


def test_parse_plan_json_tolerates_noise_and_collects_error_diagnostics() -> None:
    out = "\n".join([
        "Initializing the backend...",
        "not json {",
        _msg("diagnostic", diagnostic={"severity": "warning", "summary": "meh"}),
        _msg("diagnostic", diagnostic={"severity": "error", "summary": "Error acquiring the state lock", "detail": "ConditionalCheckFailed\n  again"}),
        "[1,2]",
    ])
    parsed = Engine.parse_plan_json(out)
    assert parsed["diagnostics"] == ["Error acquiring the state lock: ConditionalCheckFailed again"]
    assert parsed["other"] == ["Initializing the backend...", "not json {", "[1,2]"]


@pytest.mark.parametrize(
    ("addr", "expected"),
    [
        ("aws_instance.pse", ("aws_instance", "pse", None)),
        ("data.aws_ami.al2023", ("aws_ami", "al2023", None)),
        ("module.client.aws_vpc.this", ("aws_vpc", "this", "client")),
        ("module.a[0].module.b.aws_subnet.x[\"k\"]", ("aws_subnet", "x", "a.b")),
        ("aws_instance.pse[2]", ("aws_instance", "pse", None)),
    ],
)
def test_parse_address(addr: str, expected: tuple) -> None:
    assert Engine.parse_address(addr) == expected


# ---------------------------------------------------------------- engine
class _PlanEngine(Engine):
    """tofu replaced: `plan` returns `plan_output`/`plan_code`, `state list` returns `state_output`."""

    def __init__(self, *args, plan_output: str = "", plan_code: int = 0, state_output: str = "", raise_plan: BaseException | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.plan_output, self.plan_code, self.state_output, self.raise_plan = plan_output, plan_code, state_output, raise_plan
        self.calls: list[list[str]] = []
        self.prepared = 0

    def prepare(self, manifest, env, log_line=None):  # type: ignore[override]
        self.prepared += 1
        self._initialised.add(manifest.id)
        return "deadbeef"

    def _run(self, args, *, cwd, env, timeout, log_line=None):  # type: ignore[override]
        self.calls.append(args)
        if "plan" in args:
            if self.raise_plan is not None:
                raise self.raise_plan
            return self.plan_code, self.plan_output
        if "state" in args:
            return 0, self.state_output
        return 0, ""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_EFFECTS)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", {"aws", "gcp", "azure"})
    return store, build_registry(store.pricing_cache_path), manifest, tmp_path / "usecases"


def test_outline_on_groups_by_type_and_derives_unchanged(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=CREATE_PLAN, state_output="aws_security_group.pse\naws_eip.pse\naws_route_table.x\n")
    out = engine.outline(manifest, "on")
    assert out["action"] == "on"
    plan = out["plan"]
    assert plan["ok"] is True and plan["generated_at"]
    assert [e["type"] for e in plan["create"]] == ["aws_vpc", "aws_instance", "aws_instance", "aws_eip"]
    assert plan["summary"] == {"create": 4, "update": 1, "destroy": 1, "unchanged": 1, "read": 1}
    assert plan["unchanged"] == [{"address": "aws_route_table.x", "type": "aws_route_table", "name": "x", "module": None}]
    assert plan["change_summary"]["add"] == 4
    assert out["declared"] == {"creates": ["ZPA Service Edge Group, App Connector Groups and provisioning keys", "Three SSM SecureString parameters under /zpa-lab/"], "destroys": [], "retains": []}
    assert [s["name"] for s in out["steps"]] == ["Create ZPA groups and keys", "Apply infrastructure"]
    assert out["retained_state"] == {"backend": "s3", "bucket": "zs-lab-tfstate-257300000000", "key": "usecases/zpa-private-service-edge/terraform.tfstate", "region": "eu-central-1"}
    plan_call = next(c for c in engine.calls if "plan" in c)
    assert plan_call[:4] == ["tofu", "-chdir=terraform", "plan", "-json"] and "-input=false" in plan_call and "-destroy" not in plan_call
    assert not any("apply" in c for c in engine.calls)


def test_outline_off_uses_destroy_plan(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=DESTROY_PLAN, state_output=STATE)
    out = engine.outline(manifest, "off")
    plan = out["plan"]
    assert plan["ok"] and plan["summary"] == {"create": 0, "update": 0, "destroy": 3, "unchanged": 1, "read": 0}
    assert [e["address"] for e in plan["destroy"]] == ["aws_instance.pse", "aws_vpc.pse", "aws_nat_gateway.b"]
    assert plan["unchanged"][0]["address"] == "module.client.aws_instance.client[0]"
    assert "-destroy" in next(c for c in engine.calls if "plan" in c)
    assert len(out["declared"]["retains"]) == 4 and out["declared"]["destroys"] == ["Everything OpenTofu manages in this use case (see plan)"]
    assert [s["name"] for s in out["steps"]] == ["Destroy infrastructure"]


def test_outline_on_when_already_on_is_all_unchanged(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=EMPTY_PLAN, state_output=STATE)
    plan = engine.outline(manifest, "on")["plan"]
    assert plan["ok"] and plan["create"] == [] and plan["summary"]["unchanged"] == 4
    assert [e["type"] for e in plan["unchanged"]] == ["aws_instance", "aws_instance", "aws_vpc", "aws_nat_gateway"]


def test_outline_tofu_missing_is_a_failed_plan_not_an_exception(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, raise_plan=FileNotFoundError(errno.ENOENT, "No such file or directory", "tofu"))
    out = engine.outline(manifest, "on")
    assert out["plan"]["ok"] is False and "not installed" in out["plan"]["error"] and "tofu" in out["plan"]["error"]
    assert out["declared"]["creates"] and out["steps"] and out["retained_state"]["bucket"]


def test_outline_nonzero_exit_reports_last_diagnostic_scrubbed(env) -> None:
    store, providers, manifest, root = env
    out_text = "\n".join([
        _msg("diagnostic", diagnostic={"severity": "error", "summary": "Error: No valid credential sources found", "detail": "tried AKIAIOSFODNN7EXAMPLE with s3cr3t-value"}),
    ])
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=out_text, plan_code=1)
    plan = engine.outline(manifest, "off")["plan"]
    assert plan["ok"] is False
    assert plan["error"].startswith("tofu plan exited 1: Error: No valid credential sources found")
    assert "s3cr3t-value" not in plan["error"] and "AKIAIOSFODNN7EXAMPLE" not in plan["error"]


def test_outline_timeout_and_provider_disconnected(env) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, raise_plan=TofuError("tofu timed out after 900s"))
    assert "timed out" in engine.outline(manifest, "on")["plan"]["error"]
    store.delete_provider("aws")
    engine2 = _PlanEngine(store, providers, JobRunner(store), root, plan_output=CREATE_PLAN)
    out = engine2.outline(manifest, "on")
    assert out["plan"]["ok"] is False and "not connected" in out["plan"]["error"]
    assert out["retained_state"]["bucket"] is None and engine2.calls == []


def test_outline_cached_per_action_and_invalidated(env, monkeypatch: pytest.MonkeyPatch) -> None:
    store, providers, manifest, root = env
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=EMPTY_PLAN, state_output=STATE)
    first = engine.outline(manifest, "on")
    plans_after_first = sum(1 for c in engine.calls if "plan" in c)
    assert engine.outline(manifest, "on") == first
    assert sum(1 for c in engine.calls if "plan" in c) == plans_after_first
    engine.outline(manifest, "off")
    assert sum(1 for c in engine.calls if "plan" in c) == plans_after_first + 1
    import app.usecases.engine as eng_mod

    monkeypatch.setattr(eng_mod, "OUTLINE_CACHE_TTL_S", 0)
    time.sleep(0.01)
    engine.outline(manifest, "on")
    assert sum(1 for c in engine.calls if "plan" in c) == plans_after_first + 2
    monkeypatch.setattr(eng_mod, "OUTLINE_CACHE_TTL_S", 60)
    engine.outline(manifest, "on")
    engine.invalidate(manifest.id)
    engine.outline(manifest, "on")
    assert sum(1 for c in engine.calls if "plan" in c) == plans_after_first + 3


def test_outline_409_while_job_runs_and_400_bad_action(env) -> None:
    store, providers, manifest, root = env
    jobs = JobRunner(store)
    engine = _PlanEngine(store, providers, jobs, root, plan_output=EMPTY_PLAN)
    with pytest.raises(EngineError) as info:
        engine.outline(manifest, "sideways")
    assert info.value.status == 400
    from app.jobs import Scrubber, StepSpec

    job_id = jobs.start(manifest.id, "on", [StepSpec("sleep", "sleep 1")], cwd=root, env={"PATH": "/usr/bin:/bin"}, scrubber=Scrubber())
    with pytest.raises(EngineError) as info:
        engine.outline(manifest, "off")
    assert info.value.status == 409 and info.value.code == "job_running"
    deadline = time.monotonic() + 10
    while jobs.get(job_id)["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)


def test_outline_manifest_without_effects_has_empty_declared(tmp_path: Path, env) -> None:
    store, providers, _manifest, root = env
    write_manifest(root, "zpa-private-service-edge", GOOD_MANIFEST)
    manifest = load_manifest(root / "zpa-private-service-edge" / "usecase.yaml", {"aws"})
    engine = _PlanEngine(store, providers, JobRunner(store), root, plan_output=EMPTY_PLAN)
    out = engine.outline(manifest, "off")
    assert out["declared"] == {"creates": [], "destroys": [], "retains": []}


# ---------------------------------------------------------------- API
def test_outline_endpoint(logged_in, data_dir, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", MANIFEST_WITH_EFFECTS)
    r = logged_in.get("/api/usecases/zpa-private-service-edge/outline")
    assert r.status_code == 422
    r = logged_in.get("/api/usecases/zpa-private-service-edge/outline?action=maybe")
    assert r.status_code == 400 and r.json()["code"] == "bad_action"
    r = logged_in.get("/api/usecases/nope/outline?action=on")
    assert r.status_code == 404
    # Nothing connected: a failed plan is still a 200 with the declared effects and steps.
    r = logged_in.get("/api/usecases/zpa-private-service-edge/outline?action=off")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "off" and body["plan"]["ok"] is False and "not connected" in body["plan"]["error"]
    assert len(body["declared"]["retains"]) == 4 and body["steps"][0]["name"] == "Destroy infrastructure"
    assert body["retained_state"]["backend"] == "s3" and body["retained_state"]["key"].endswith("terraform.tfstate")
    detail = logged_in.get("/api/usecases/zpa-private-service-edge").json()
    assert detail["effects"]["off"]["retains"] == body["declared"]["retains"] and detail["effects"]["on"]["creates"]


def test_outline_requires_auth(client) -> None:
    r = client.get("/api/usecases/x/outline?action=on")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


# ---------------------------------------------------------------- manifest effects
def _data(effects: object) -> dict:
    data = yaml.safe_load(GOOD_MANIFEST)
    data["effects"] = effects
    return data


def test_effects_parse_quoted_and_boolean_keys() -> None:
    quoted = parse_manifest(yaml.safe_load(MANIFEST_WITH_EFFECTS), expected_id="zpa-private-service-edge", provider_ids={"aws"})
    assert len(quoted.effects_on.creates) == 2 and quoted.effects_on.retains == () and len(quoted.effects_off.retains) == 4
    boolean = parse_manifest(_data({True: {"creates": ["a"]}, False: {"retains": ["b"]}}), expected_id="zpa-private-service-edge", provider_ids={"aws"})
    assert boolean.effects("on").creates == ("a",) and boolean.effects("off").retains == ("b",) and boolean.effects("off").destroys == ()
    none = parse_manifest(_data(None), expected_id="zpa-private-service-edge", provider_ids={"aws"})
    assert none.effects_on.to_api() == {"creates": [], "destroys": [], "retains": []}


@pytest.mark.parametrize(
    ("effects", "needle"),
    [
        (["x"], "'effects' must be a mapping"),
        ({"maybe": {}}, "unknown field(s): maybe"),
        ({"on": ["x"]}, "effects.on: must be a mapping"),
        ({"on": {"creates": "x"}}, "'creates' must be a list of non-empty strings"),
        ({"off": {"retains": ["ok", ""]}}, "'retains' must be a list"),
        ({"off": {"removes": []}}, "unknown field(s): removes"),
    ],
)
def test_effects_validation(effects: object, needle: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest(_data(effects), expected_id="zpa-private-service-edge", provider_ids={"aws"})
    assert needle in str(info.value)
