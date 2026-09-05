"""Shared fixtures: isolated /data dir, env, and a TestClient with background threads off."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("SWITCHBOARD_DATA", str(data))
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("SWITCHBOARD_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("SWITCHBOARD_USECASES", str(tmp_path / "usecases"))
    monkeypatch.setenv("SWITCHBOARD_BACKGROUND", "0")
    (tmp_path / "usecases").mkdir()
    return data


@pytest.fixture
def client(data_dir: Path) -> Iterator["TestClient"]:
    from fastapi.testclient import TestClient

    from app.main import build_app

    with TestClient(build_app(background=False), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def logged_in(client: "TestClient") -> "TestClient":
    r = client.post("/api/auth/login", json={"password": TEST_PASSWORD})
    assert r.status_code == 204
    return client


def write_manifest(root: Path, uc_id: str, text: str) -> Path:
    d = root / uc_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "usecase.yaml"
    p.write_text(text, encoding="utf-8")
    return p


GOOD_MANIFEST = """\
id: zpa-private-service-edge
name: ZPA Private Service Edge lab
provider: aws
summary: A Private Service Edge in an isolated VPC.
description: |
  Some **markdown**.
source:
  git: https://github.com/nilsujma-dev/zs-zpa-private-service-edge-lab.git
  ref: main
terraform:
  dir: terraform
  state_key: usecases/zpa-private-service-edge/terraform.tfstate
env:
  AWS_DEFAULT_REGION: eu-central-1
secrets:
  - zscaler_oneapi
on:
  - name: Create ZPA groups and keys
    run: python3 scripts/zpa_create.py
  - name: Apply infrastructure
    run: tofu -chdir=terraform apply -auto-approve -input=false
off:
  - name: Destroy infrastructure
    run: tofu -chdir=terraform destroy -auto-approve -input=false
status:
  run: python3 scripts/status.py --json
  interval_s: 60
tags:
  Project: zpa-pse-lab
"""
