"""Code browser: tree listing, file read with language, and path rejection."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.jobs import JobRunner
from app.providers import build_registry
from app.store import Store
from app.usecases.engine import Engine, EngineError
from app.usecases.manifest import load_manifest
from tests.conftest import GOOD_MANIFEST, write_manifest


@pytest.fixture
def engine_with_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "data")
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", GOOD_MANIFEST)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", {"aws"})
    checkout = store.checkout_dir(manifest.id)
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (checkout / "terraform").mkdir()
    (checkout / "terraform" / "main.tf").write_text('resource "aws_vpc" "pse" {}\n')
    (checkout / "terraform" / ".terraform").mkdir()
    (checkout / "terraform" / ".terraform" / "plugin.bin").write_bytes(b"\x00\x01")
    (checkout / "scripts").mkdir()
    (checkout / "scripts" / "status.py").write_text("print('{}')\n")
    (checkout / "README.md").write_text("# lab\n")
    (checkout / "logo.png").write_bytes(b"\x89PNG\x00\x00")
    (tmp_path / "outside.txt").write_text("secret")
    (checkout / "link.txt").symlink_to(tmp_path / "outside.txt")
    engine = Engine(store, build_registry(store.pricing_cache_path), JobRunner(store), tmp_path / "usecases")
    monkeypatch.setattr(engine, "current_commit", lambda m: "abc1234")
    return engine, manifest


def test_tree_skips_git_terraform_binaries_and_symlinks(engine_with_checkout) -> None:
    engine, manifest = engine_with_checkout
    tree = engine.code_tree(manifest)
    assert tree["commit"] == "abc1234"
    assert [f["path"] for f in tree["files"]] == ["README.md", "scripts/status.py", "terraform/main.tf"]
    assert all(f["size"] > 0 for f in tree["files"])


def test_file_read_with_language(engine_with_checkout) -> None:
    engine, manifest = engine_with_checkout
    out = engine.code_file(manifest, "terraform/main.tf")
    assert out == {"path": "terraform/main.tf", "language": "hcl", "content": 'resource "aws_vpc" "pse" {}\n'}
    assert engine.code_file(manifest, "scripts/status.py")["language"] == "python"
    assert engine.code_file(manifest, "README.md")["language"] == "markdown"


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "terraform/../../outside.txt", ".git/HEAD", "terraform/.terraform/plugin.bin"])
def test_paths_outside_or_hidden_are_rejected(engine_with_checkout, path: str) -> None:
    engine, manifest = engine_with_checkout
    with pytest.raises(EngineError) as info:
        engine.code_file(manifest, path)
    assert info.value.status in (400, 404)


def test_symlink_escape_and_binary_rejected(engine_with_checkout) -> None:
    engine, manifest = engine_with_checkout
    with pytest.raises(EngineError) as info:
        engine.code_file(manifest, "link.txt")
    assert info.value.status == 404
    with pytest.raises(EngineError) as info:
        engine.code_file(manifest, "logo.png")
    assert info.value.code == "binary"


def test_no_checkout_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHBOARD_SECRET_KEY", Fernet.generate_key().decode())
    store = Store(tmp_path / "data")
    write_manifest(tmp_path / "usecases", "zpa-private-service-edge", GOOD_MANIFEST)
    manifest = load_manifest(tmp_path / "usecases" / "zpa-private-service-edge" / "usecase.yaml", {"aws"})
    engine = Engine(store, build_registry(store.pricing_cache_path), JobRunner(store), tmp_path / "usecases")
    with pytest.raises(EngineError) as info:
        engine.code_tree(manifest)
    assert info.value.code == "no_checkout" and info.value.status == 404
