"""Stale-entry pruning (SPEC v1.6), dashboard side: the prune steps in both shipped manifests,
what the cards declare about them, and a status probe that carries `stale`/`keys`.

The prune itself is `prune.py` in the lab repos; Switchboard only has to run it at the right
places, keep saying the truth on the card, and pass the extra probe fields through to the
frontend without losing the enrolment mapping the drawing needs. No cloud calls here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.usecases.manifest import Manifest, load_manifest
from app.usecases.topology import _parse_components, build_graph

from tests.test_topology import I_CONN, I_PRIV, I_PSE, _manifest, pse_inventory, pse_status

REPO = Path(__file__).resolve().parent.parent
PROVIDERS = {"aws", "gcp", "azure"}
PSE = REPO / "usecases" / "zpa-private-service-edge" / "usecase.yaml"
CC = REPO / "usecases" / "zcc-aws-workload" / "usecase.yaml"

PRUNE_OFF = "python3 scripts/prune.py --phase off --apply"
PRUNE_ON_PRE = "python3 scripts/prune.py --phase on-pre --apply"
PRUNE_ON_POST = "python3 scripts/prune.py --phase on-post --apply"
MAX_STEP_NAME = 40


@pytest.fixture(scope="module")
def pse() -> Manifest:
    return load_manifest(PSE, PROVIDERS)


@pytest.fixture(scope="module")
def cc() -> Manifest:
    return load_manifest(CC, PROVIDERS)


# ---------------------------------------------------------------- the steps, where the hooks are
def test_pse_lab_prunes_after_destroy_and_before_apply(pse: Manifest) -> None:
    assert [(s.name, s.run) for s in pse.on] == [
        ("Create ZPA groups and keys", "python3 scripts/zpa_create.py"),
        ("Create PRIV connector group", "python3 scripts/zpa_create_priv.py"),
        ("Prune stale entries", PRUNE_ON_PRE),                       # groups resolve by name; before SSM and apply
        ("Seed provisioning keys into SSM", "python3 scripts/put_keys_ssm.py"),
        ("Apply infrastructure", "tofu -chdir=terraform apply -auto-approve -input=false"),
        ("Wait for enrolment", "python3 scripts/wait_enrolled.py --timeout 900"),
    ]
    assert [(s.name, s.run) for s in pse.off] == [
        ("Destroy infrastructure", "tofu -chdir=terraform destroy -auto-approve -input=false"),
        ("Prune stale entries", PRUNE_OFF),                          # the instances are gone, so the entries are stale
    ]


def test_cc_lab_prunes_again_after_the_zia_rules_are_rescoped(cc: Manifest) -> None:
    names = [s.name for s in cc.on]
    assert names.index("Prune stale entries") == names.index("Create CC admin, templates and secret") + 1
    assert names.index("Prune stale entries") < names.index("Apply infrastructure")
    # the superseded ZIA location is unreferenced only after zia_policy.py has moved the rules
    assert names.index("Prune superseded CC group and location") == names.index("ZIA URL and DLP policy") + 1
    assert names.index("Prune superseded CC group and location") < names.index("Verify nothing pre-existing changed")
    runs = {s.name: s.run for s in cc.on}
    assert runs["Prune stale entries"] == PRUNE_ON_PRE and runs["Prune superseded CC group and location"] == PRUNE_ON_POST
    assert [(s.name, s.run) for s in cc.off] == [
        ("Destroy infrastructure", "tofu -chdir=terraform destroy -auto-approve -input=false"),
        ("Prune stale entries", PRUNE_OFF),
    ]


def test_prune_step_names_fit_the_card(pse: Manifest, cc: Manifest) -> None:
    prune_steps = [s for m in (pse, cc) for s in (*m.on, *m.off) if s.run.startswith("python3 scripts/prune.py")]
    assert len(prune_steps) == 5
    assert all(len(s.name) <= MAX_STEP_NAME for s in prune_steps), [s.name for s in prune_steps if len(s.name) > MAX_STEP_NAME]
    assert all("--apply" in s.run for s in prune_steps)              # prune.py is dry-run by default


def test_both_manifests_list_the_prune_steps_over_the_api(logged_in, tmp_path: Path) -> None:
    for uc in ("zpa-private-service-edge", "zcc-aws-workload"):
        shutil.copytree(REPO / "usecases" / uc, tmp_path / "usecases" / uc)
    pse = logged_in.get("/api/usecases/zpa-private-service-edge").json()["procedure"]
    cc = logged_in.get("/api/usecases/zcc-aws-workload").json()["procedure"]
    assert [s["name"] for s in pse["on"]] == [
        "Create ZPA groups and keys", "Create PRIV connector group", "Prune stale entries",
        "Seed provisioning keys into SSM", "Apply infrastructure", "Wait for enrolment"]
    assert [s["name"] for s in pse["off"]] == ["Destroy infrastructure", "Prune stale entries"]
    assert [s["name"] for s in cc["on"]] == [
        "Baseline the tenant", "Preflight quotas and secret", "Create ZPA connector group and app segment",
        "Create CC admin, templates and secret", "Prune stale entries", "Seed provisioning key into SSM",
        "Apply infrastructure", "Wait for CC and connector registration", "Forward the app segment to ZPA",
        "Allow the lab CC group to the private app", "ZIA URL and DLP policy",
        "Prune superseded CC group and location", "Verify nothing pre-existing changed",
        "Wait for egress and ZPA evidence"]
    assert [s["name"] for s in cc["off"]] == ["Destroy infrastructure", "Prune stale entries"]
    assert [s["run"] for s in cc["off"]][1] == PRUNE_OFF


# ---------------------------------------------------------------- what the card now declares
def test_the_cards_no_longer_promise_that_entries_accumulate(pse: Manifest, cc: Manifest) -> None:
    for m in (pse, cc):
        declared = [*m.effects_on.creates, *m.effects_off.destroys, *m.effects_off.retains, m.description]
        assert not any("accumulate one per rebuild" in t for t in declared)
        assert not any("pruning them is deliberately manual" in t.lower() for t in declared)
        assert any("stale entries from earlier rebuilds are pruned before apply" in t for t in m.effects_on.creates)
        assert any("stale disconnected entries in the lab's own groups are deleted" in t for t in m.effects_off.destroys)
    # only the CC lab has a ZIA location, and it is the one thing that outlives the OFF
    assert any("one ZIA location survives until the next ON" in t for t in cc.effects_off.destroys)
    assert any("ZIA location" in t for t in cc.effects_off.retains)
    assert not any("ZIA location" in t for t in pse.effects_off.retains)


# ---------------------------------------------------------------- the probe keeps working
def test_parse_components_ignores_the_prune_bookkeeping() -> None:
    """`stale` is a mapping and `keys` a list, but neither is an enrolment component."""
    probe = {
        "pse": {"status": "ZPN_STATUS_AUTHENTICATED"},
        "stale": {"count": 3, "connectors": 1, "service_edges": 0, "cc_vms": 1, "cc_groups": 0,
                  "locations": 1, "last_prune": "2026-09-06T18:02:11Z", "last_prune_deleted": 4},
        "keys": [{"type": "connector", "name": "AWS-Lab CONNECTOR_GRP key v2", "usage": "7/200", "current": True}],
        "checked_at": "2026-09-06T18:30:00Z",
    }
    assert [c["id"] for c in _parse_components(probe)] == ["pse"]


def test_a_probe_carrying_stale_and_keys_still_maps_enrolment() -> None:
    status = pse_status()
    status["stale"] = {"count": 2, "connectors": 2, "service_edges": 0, "cc_vms": 0, "cc_groups": 0,
                       "locations": 0, "last_prune": "2026-09-06T18:02:11Z", "last_prune_deleted": 4}
    status["keys"] = [{"type": "connector", "name": "AWS-Lab CONNECTOR_GRP key v2", "usage": "7/200", "current": True},
                      {"type": "service edge", "name": "AWS-Lab SERVICE_EDGE_GRP key v2", "usage": "8/200", "current": True}]
    status["summary"] = "3/3 components authenticated, 5/5 instances running, 2 stale entries"
    graph = build_graph(_manifest(), pse_inventory(), status)
    assert graph["enrolment"][I_PSE]["authenticated"] is True and graph["enrolment"][I_PSE]["matched_by"] == "private_ip"
    assert graph["enrolment"][I_CONN]["matched_by"] == "private_ip" and graph["enrolment"][I_PRIV]["matched_by"] == "role"
    assert not [u for u in graph["unknown"] if u["kind"] == "component"]
    # the graph is a drawing of the cloud: the prune counts belong to the probe block, not to a node
    assert not any("stale" in n for n in graph["nodes"])
