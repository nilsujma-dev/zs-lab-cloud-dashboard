from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.usecases.manifest import ManifestError, load_all, load_manifest, parse_manifest
from tests.conftest import GOOD_MANIFEST, write_manifest

PROVIDERS = {"aws"}


def _data(**overrides: object) -> dict:
    data = yaml.safe_load(GOOD_MANIFEST)
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def test_good_manifest_loads(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, "zpa-private-service-edge", GOOD_MANIFEST)
    m = load_manifest(path, PROVIDERS)
    assert m.id == "zpa-private-service-edge"
    assert m.provider == "aws"
    assert m.terraform_dir == "terraform"
    assert m.state_key == "usecases/zpa-private-service-edge/terraform.tfstate"
    assert [s.name for s in m.on] == ["Create ZPA groups and keys", "Apply infrastructure"]
    assert len(m.off) == 1
    assert m.env == {"AWS_DEFAULT_REGION": "eu-central-1"}
    assert m.secrets == ("zscaler_oneapi",)
    assert m.status is not None and m.status.interval_s == 60
    assert m.tags == {"Project": "zpa-pse-lab"}
    assert "markdown" in m.description


def test_id_must_match_directory(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, "other-dir", GOOD_MANIFEST)
    with pytest.raises(ManifestError, match="directory is named 'other-dir'"):
        load_manifest(path, PROVIDERS)


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"id": "Bad_ID"}, "[a-z0-9-]+"),
        ({"name": None}, "missing required field 'name'"),
        ({"provider": "gcp"}, "not a registered provider"),
        ({"source": {"git": "ftp://x", "ref": "main"}}, "'git' must be"),
        ({"source": {"git": "https://example.com/r.git"}}, "missing required field 'ref'"),
        ({"terraform": {"dir": "../escape", "state_key": "k"}}, "relative path"),
        ({"terraform": {"dir": "/abs", "state_key": "k"}}, "relative path"),
        ({"on": []}, "'on' must be a non-empty list"),
        ({"off": [{"name": "x"}]}, "missing required field 'run'"),
        ({"on": [{"name": "x", "run": "y", "shell": "bash"}]}, "unknown step field"),
        ({"secrets": ["vault_token"]}, "unknown secret"),
        ({"status": {"run": "x", "interval_s": 0}}, "interval_s"),
        ({"status": {"run": "x", "cron": "* * *"}}, "unknown field"),
        ({"env": {"1BAD": "x"}}, "invalid key"),
        ({"extra": True}, "unknown field(s): extra"),
    ],
)
def test_bad_manifests(overrides: dict, needle: str) -> None:
    with pytest.raises(ManifestError) as info:
        parse_manifest(_data(**overrides), expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)
    assert needle in str(info.value)


def test_top_level_must_be_mapping() -> None:
    with pytest.raises(ManifestError, match="top level must be a mapping"):
        parse_manifest(["not", "a", "map"], expected_id="x", provider_ids=PROVIDERS)


def test_invalid_yaml_reports_file(tmp_path: Path) -> None:
    path = write_manifest(tmp_path, "broken", "id: [unclosed")
    with pytest.raises(ManifestError, match="invalid YAML"):
        load_manifest(path, PROVIDERS)


def test_status_optional_and_defaults() -> None:
    m = parse_manifest(_data(status=None), expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)
    assert m.status is None
    m = parse_manifest(_data(status={"run": "x"}), expected_id="zpa-private-service-edge", provider_ids=PROVIDERS)
    assert m.status is not None and m.status.interval_s == 60


def test_load_all_separates_good_and_bad(tmp_path: Path) -> None:
    write_manifest(tmp_path, "zpa-private-service-edge", GOOD_MANIFEST)
    write_manifest(tmp_path, "broken", "id: broken\n")
    (tmp_path / "no-manifest-here").mkdir()
    good, bad = load_all(tmp_path, PROVIDERS)
    assert set(good) == {"zpa-private-service-edge"}
    assert set(bad) == {"broken"}
    assert "missing required field" in bad["broken"]
