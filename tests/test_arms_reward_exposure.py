"""D2 — reward attribution must require actual rule EXPOSURE and dedupe by evidence identity.

Defect (reproduced 2026-09-26 by an external campaign audit, re-confirmed 2026-09-27):
`measure_arms` rewarded a minted rule 1.0 whenever later sessions lacked a signature-matched
gap — with no join on project, exposure, or applicability — and keyed its observation id on
wall-clock time, so replaying identical evidence produced a second observation and a second
bandit pull. Doctrine (KNOWLEDGE-LIFECYCLE / RULE-INJECTION): no applicable exposure means
UNMEASURED, never success; outcomes dedupe by evidence identity, not by timestamp.
"""
import json
from types import SimpleNamespace

from loglearner import arms

LATER = dict(date_range=("2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z"))


def _mint(tmp_path, monkeypatch, project="proj-x"):
    monkeypatch.setenv("LOGLEARNER_UCB_DIR", str(tmp_path))
    ex = SimpleNamespace(instinct_rule="Never assume a configured hook delivered context",
                         gap_summary={"gap_type": "tool_error_cluster"},
                         category="build_deploy", line_range=(1, 9), session_id="mint-1")
    arm = arms.mint_arm(ex, project=project)
    arms.append_jsonl(arms.arms_path(), [arm])
    return arm


def _ledger():
    p = arms.rewards_path()
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_unexposed_session_is_unmeasured_not_rewarded(tmp_path, monkeypatch):
    """An unrelated session that never received the rule proves nothing — no observation."""
    _mint(tmp_path, monkeypatch)
    css = SimpleNamespace(session_id="css-1", project="proj-x", **LATER)
    obs, bandit = arms.measure_arms([css], [])
    assert obs == []
    assert _ledger() == []
    assert bandit is None or sum(bandit.n_pulls) == 0


def test_exposed_session_without_recurrence_rewards_once(tmp_path, monkeypatch):
    arm = _mint(tmp_path, monkeypatch)
    s = SimpleNamespace(session_id="post-1", project="proj-x", **LATER)
    arms.record_exposure("post-1", [arm["arm"]], project="proj-x", decision_ref="prompt-build-7")
    obs, bandit = arms.measure_arms([s], [])
    assert len(obs) == 1 and obs[0]["reward"] == 1.0 and obs[0]["regret"] == 0.0
    assert obs[0]["matched_sessions"] == ["post-1"]
    assert bandit.n_pulls[bandit.arm_names.index(arm["arm"])] == 1


def test_replay_of_identical_evidence_is_deduplicated(tmp_path, monkeypatch):
    arm = _mint(tmp_path, monkeypatch)
    s = SimpleNamespace(session_id="post-1", project="proj-x", **LATER)
    arms.record_exposure("post-1", [arm["arm"]])
    first, _ = arms.measure_arms([s], [])
    second, bandit = arms.measure_arms([s], [])          # identical evidence, later wall-clock
    assert len(first) == 1 and second == []
    assert len(_ledger()) == 1
    assert bandit.n_pulls[bandit.arm_names.index(arm["arm"])] == 1
    assert first[0]["obs_id"] == _ledger()[0]["obs_id"]  # evidence-keyed, not time-keyed


def test_exposed_recurrence_is_still_zero(tmp_path, monkeypatch):
    arm = _mint(tmp_path, monkeypatch)
    s = SimpleNamespace(session_id="post-1", project="proj-x", **LATER)
    gap = SimpleNamespace(session_id="post-1", gap_type="tool_error_cluster",
                          category="build_deploy", start_line=10, end_line=20)
    arms.record_exposure("post-1", [arm["arm"]])
    obs, _ = arms.measure_arms([s], [gap])
    assert len(obs) == 1 and obs[0]["reward"] == 0.0 and obs[0]["recurrence_count"] == 1


def test_exposure_in_another_project_is_unmeasured(tmp_path, monkeypatch):
    """Exposure alone is not applicability: a rule minted for proj-x measured on a proj-y session is unmeasured."""
    arm = _mint(tmp_path, monkeypatch, project="proj-x")
    s = SimpleNamespace(session_id="post-y", project="proj-y", **LATER)
    arms.record_exposure("post-y", [arm["arm"]], project="proj-y")
    obs, _ = arms.measure_arms([s], [])
    assert obs == []


def test_exposure_ledger_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGLEARNER_UCB_DIR", str(tmp_path))
    assert arms.record_exposure("s1", ["a1", "a2"], decision_ref="d1",
                                instinct_ids=["fx-inst-01", "fx-perf-001"]) == 2
    assert arms.record_exposure("s2", ["a1"]) == 1
    assert arms.load_exposures() == {"a1": {"s1", "s2"}, "a2": {"s1"}}
    rows = [json.loads(x) for x in arms.exposures_path().read_text().splitlines()]
    assert {r["session_id"] for r in rows} == {"s1", "s2"} and rows[0]["decision_ref"] == "d1"
    assert rows[0]["instinct_id"] == "fx-inst-01" and rows[1]["instinct_id"] == "fx-perf-001"
    assert "instinct_id" not in rows[2]  # optional: absent when not supplied
