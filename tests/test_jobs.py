"""Job runner end to end with shell steps (no network): sequencing, stop-on-failure,
409 on concurrent job, scrubbed log, log tail offsets, restart recovery."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.jobs import JobConflict, JobRunner, Scrubber, StepSpec
from app.store import Store


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    return Store(tmp_path / "data")


def _wait(runner: JobRunner, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.get(job_id)
        assert job is not None
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def _env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/tmp"}


def test_steps_run_in_order_and_log_is_tailable(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)
    steps = [StepSpec("say hello", "echo hello"), StepSpec("say world", "echo world; echo err >&2")]
    job_id = runner.start("uc", "on", steps, cwd=tmp_path, env=_env(), scrubber=Scrubber())
    job = _wait(runner, job_id)
    assert job["state"] == "succeeded"
    assert [s["state"] for s in job["steps"]] == ["succeeded", "succeeded"]
    assert all(s["exit_code"] == 0 for s in job["steps"])
    assert job["started"] and job["ended"]

    lines, nxt = runner.read_log(job_id, 0)
    assert "hello" in lines and "world" in lines and "err" in lines
    assert lines.index("hello") < lines.index("world")
    assert nxt == len(lines)
    more, nxt2 = runner.read_log(job_id, nxt)
    assert more == [] and nxt2 == nxt
    tail, _ = runner.read_log(job_id, nxt - 1)
    assert tail == ["### job succeeded"]

    on_disk = store.get_run("uc", job_id)
    assert on_disk is not None and on_disk["state"] == "succeeded"


def test_stop_on_first_failure(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)
    steps = [StepSpec("ok", "true"), StepSpec("boom", "exit 3"), StepSpec("never", "echo never")]
    job_id = runner.start("uc", "off", steps, cwd=tmp_path, env=_env(), scrubber=Scrubber())
    job = _wait(runner, job_id)
    assert job["state"] == "failed"
    assert [s["state"] for s in job["steps"]] == ["succeeded", "failed", "skipped"]
    assert job["steps"][1]["exit_code"] == 3
    assert "boom" in job["error"]
    lines, _ = runner.read_log(job_id, 0)
    assert "never" not in lines


def test_one_job_per_usecase(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)
    job_id = runner.start("uc", "on", [StepSpec("sleep", "sleep 1")], cwd=tmp_path, env=_env(), scrubber=Scrubber())
    with pytest.raises(JobConflict):
        runner.start("uc", "off", [StepSpec("x", "true")], cwd=tmp_path, env=_env(), scrubber=Scrubber())
    other = runner.start("other", "on", [StepSpec("x", "true")], cwd=tmp_path, env=_env(), scrubber=Scrubber())
    assert runner.running_job("uc")["id"] == job_id
    _wait(runner, job_id)
    _wait(runner, other)
    assert runner.running_job("uc") is None


def test_log_is_scrubbed_before_write(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    env = {**_env(), "AWS_SECRET_ACCESS_KEY": secret}
    steps = [StepSpec("leak", 'echo "key=$AWS_SECRET_ACCESS_KEY id=AKIAIOSFODNN7EXAMPLE"')]
    job_id = runner.start("uc", "on", steps, cwd=tmp_path, env=env, scrubber=Scrubber([secret]))
    _wait(runner, job_id)
    raw = store.run_log_path("uc", job_id).read_text()
    assert secret not in raw and "AKIAIOSFODNN7EXAMPLE" not in raw
    assert "key=<redacted> id=<redacted>" in raw


def test_prelude_failure_skips_all_steps(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)

    def prelude(writer) -> None:
        writer.write("preparing")
        raise RuntimeError("git clone failed with secret hunter22")

    job_id = runner.start(
        "uc", "on", [StepSpec("x", "echo x")], cwd=tmp_path, env=_env(), scrubber=Scrubber(["hunter22"]), prelude=prelude
    )
    job = _wait(runner, job_id)
    assert job["state"] == "failed"
    assert job["steps"][0]["state"] == "skipped"
    assert "Preparation failed" in job["error"] and "hunter22" not in job["error"]


def test_on_finish_called(store: Store, tmp_path: Path) -> None:
    runner = JobRunner(store)
    seen: list[dict] = []
    job_id = runner.start("uc", "on", [StepSpec("x", "true")], cwd=tmp_path, env=_env(), scrubber=Scrubber(), on_finish=seen.append)
    _wait(runner, job_id)
    time.sleep(0.1)
    assert seen and seen[0]["id"] == job_id and seen[0]["state"] == "succeeded"


def test_recover_marks_interrupted_jobs_failed(store: Store) -> None:
    store.save_run(
        "uc",
        {
            "id": "20260905T090000Z-aaaaaa",
            "usecase": "uc",
            "action": "on",
            "state": "running",
            "steps": [{"name": "a", "state": "succeeded"}, {"name": "b", "state": "running"}, {"name": "c", "state": "pending"}],
            "started": "2026-09-05T09:00:00+00:00",
            "ended": None,
        },
    )
    runner = JobRunner(store)
    assert runner.recover() == 1
    rec = runner.get("20260905T090000Z-aaaaaa")
    assert rec is not None and rec["state"] == "failed"
    assert [s["state"] for s in rec["steps"]] == ["succeeded", "failed", "skipped"]
    assert "restarted" in rec["error"]


def test_list_runs_newest_first(store: Store) -> None:
    for i in range(3):
        store.save_run("uc", {"id": f"20260905T10000{i}Z-x", "usecase": "uc", "action": "on", "state": "succeeded", "steps": [], "started": f"2026-09-05T10:00:0{i}+00:00", "ended": None})
    runs = JobRunner(store).list_runs("uc", limit=2)
    assert [r["id"] for r in runs] == ["20260905T100002Z-x", "20260905T100001Z-x"]


def test_unknown_job(store: Store) -> None:
    runner = JobRunner(store)
    assert runner.get("nope") is None
    assert runner.read_log("nope", 0) is None
