"""The UCB loop is closed — mint, measure, persist, fleet-compat.

Run: python3 -m pytest tests/test_ucb_loop.py -q
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loglearner import arms  # noqa: E402
from loglearner.example_synthesizer import SynthesizedExample  # noqa: E402
from loglearner.steps import MeasureRuleImpactStep, MintArmsStep  # noqa: E402
from loglearner.pipeline import PipelineContext  # noqa: E402
from loglearner.ucb import AsymmetricUCB  # noqa: E402

FLEET_SNAPSHOT_KEYS = {
    "num_arms", "arm_names", "c", "epsilon", "decay_rate", "n_pulls",
    "n_submitted", "total_reward", "total_reward_sq", "total_steps",
    "baseline_sum", "baseline_count",
}
FLEET_META_KEYS = {"arm", "action", "reward", "regret", "cross_ref", "labels"}


def example(rule="always pin the driver version", gap_type="tool_error_cluster"):
    return SynthesizedExample(
        gap_summary={"gap_type": gap_type, "severity": 8},
        root_cause="rc", negative_example={"situation": "s", "action": "a"},
        positive_example={"action": "p"}, instinct_rule=rule, confidence=7,
        model_agreement="majority", applicable_beyond_session=True,
        session_id="abcd1234-0000", line_range=(10, 20), category="build_deploy")


def test_bandit_snapshot_is_fleet_key_compatible(tmp_path):
    b = AsymmetricUCB(arm_names=["a1", "a2"])
    b.update(b.ensure_arm("a3"), 1.0)
    p = tmp_path / "ucb.json"
    b.save(p)
    state = json.loads(p.read_text())
    assert set(state) == FLEET_SNAPSHOT_KEYS
    b2 = AsymmetricUCB.load(p)
    assert b2.arm_names == ["a1", "a2", "a3"] and b2.total_steps == 1
    assert b2.select() in range(3)


def test_meta_block_on_both_output_shapes():
    ex = example()
    inst, paired = ex.to_instinct("L001"), ex.to_paired_jsonl()
    for meta in (inst["meta"], paired["meta"]):
        assert set(meta) == FLEET_META_KEYS
        assert meta["arm"].startswith("ll-") and meta["arm"].endswith("_apply")
        assert meta["reward"] is None and meta["regret"] is None
        assert meta["labels"] == ["build_deploy", "tool_error_cluster"]
        assert meta["cross_ref"] == ["session:abcd1234-0000 lines:10-20"]
    assert inst["meta"]["arm"] == paired["meta"]["arm"]  # same rule, same arm


def test_mint_measure_reward_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("LOGLEARNER_UCB_DIR", str(tmp_path))
    ex = example()

    ctx = MintArmsStep()(PipelineContext().update(
        verified_examples=[ex], project="proj-x"))
    minted = ctx.get("minted_arms")
    assert len(minted) == 1 and minted[0]["reward"] is None
    assert minted[0]["arm"] == arms.arm_id_for_rule(ex.instinct_rule)
    # idempotent: re-minting the same rule adds nothing
    ctx2 = MintArmsStep()(PipelineContext().update(
        verified_examples=[ex], project="proj-x"))
    assert ctx2.get("minted_arms") == []

    # #25: a session must have RECEIVED the rule for its outcome to count
    arms.record_exposure("post-1", [minted[0]["arm"]], project="proj-x",
                         decision_ref="test-prompt-build")
    later = SimpleNamespace(session_id="post-1",
                            date_range=("2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z"))
    recurring = SimpleNamespace(session_id="post-1",
                                gap_type="tool_error_cluster", category="build_deploy")

    # recurrence -> reward 0 / regret 1
    mctx = MeasureRuleImpactStep()(PipelineContext().update(
        sessions=[later], gaps=[recurring]))
    obs = mctx.get("arm_observations")
    assert len(obs) == 1 and obs[0]["reward"] == 0.0 and obs[0]["regret"] == 1.0

    # gap stopped appearing -> reward 1
    mctx = MeasureRuleImpactStep()(PipelineContext().update(
        sessions=[later], gaps=[]))
    assert mctx.get("arm_observations")[0]["reward"] == 1.0

    # no post-mint sessions -> skip entirely
    early = SimpleNamespace(session_id="pre-1",
                            date_range=("2000-01-01T00:00:00Z", "2000-01-01T01:00:00Z"))
    mctx = MeasureRuleImpactStep()(PipelineContext().update(
        sessions=[early], gaps=[]))
    assert mctx.get("arm_observations") == []

    # persistence: rewards jsonl + fleet-shaped ucb snapshot
    rewards = [json.loads(x) for x in
               (tmp_path / "loglearner_rewards.jsonl").read_text().splitlines()]
    assert len(rewards) == 2
    state = json.loads((tmp_path / "state" / "ucb.json").read_text())
    assert set(state) == FLEET_SNAPSHOT_KEYS and state["total_steps"] == 2


def test_fleet_ingest_is_read_only_index(tmp_path):
    fleet = tmp_path / "learnings"
    fleet.mkdir()
    (fleet / "demo.jsonl").write_text(json.dumps({
        "id": "x", "rule": "r",
        "meta": {"arm": "demo-arm", "action": "act", "reward": 1,
                 "regret": 0, "cross_ref": ["a"], "labels": ["l1"]}}) + "\n")
    (fleet / "demo.jsonl.bak-junk").write_text("not json\n")
    out = tmp_path / "fleet_arm_index.json"
    idx = arms.build_fleet_arm_index(learnings_dir=fleet, out_path=out)
    assert idx["demo-arm"]["last_reward"] == 1
    assert json.loads(out.read_text())["arm_count"] == 1
    # ingest never wrote into the fleet dir
    assert sorted(p.name for p in fleet.iterdir()) == [
        "demo.jsonl", "demo.jsonl.bak-junk"]
