"""Knowledge-object lifecycle.

FAILS-if-removed: these tests pin the two load-bearing lifecycle properties
from the state machine in docs/KNOWLEDGE-LIFECYCLE.md §1-§2 —
(1) two rules with opposing directives on the same signature emit a conflict
    record instead of being silently kept (remove the detector ⇒ fail), and
(2) a REVERT/refuted verdict removes the rule from the recall index
    (remove the filter ⇒ fail).
Plus the audit-mandated mint-time evidence archival, fail-closed.

Run: python3 -m pytest tests/test_knowledge_lifecycle.py -q
"""

import json

from loglearner import lifecycle as lc
from loglearner import evidence_digest as ed


def _rule(rid, text, conf=6, added=None, state=None, merge=1):
    r = {"id": rid, "rule": text, "confidence": conf, "merge_count": merge}
    if added:
        r["provenance"] = {"added_date": added}
    if state:
        r["lifecycle_state"] = state
    return r


OPPOSING_A = _rule("L001", "NEVER run `git add -A` when staging a release "
                   "commit; stage explicit paths only", conf=9,
                   added="2026-08-01")
OPPOSING_B = _rule("L002", "ALWAYS run `git add -A` when staging a release "
                   "commit so nothing is forgotten", conf=5,
                   added="2026-05-01")
UNRELATED = _rule("L003", "ALWAYS verify author.login after pushing a signed "
                  "commit", conf=8, added="2026-07-01")


# --- contradiction detection (KNOWLEDGE-LIFECYCLE.md §2) ---

def test_contradiction_fixture_emits_conflict_record():
    """State machine §2: opposing directives on the same signature ⇒ a
    conflict record is emitted (never silently keep both).
    Docstring anchor: docs/KNOWLEDGE-LIFECYCLE.md §2 step 1."""
    conflicts = lc.detect_contradictions([OPPOSING_A, OPPOSING_B, UNRELATED])
    assert len(conflicts) == 1, conflicts
    c = conflicts[0]
    assert {c["rule_a"], c["rule_b"]} == {"L001", "L002"}
    assert {c["polarity_a"], c["polarity_b"]} == {"negative", "positive"}
    assert c["shared_terms"], "conflict record must carry the shared signature"


def test_agreeing_rules_emit_no_conflict():
    conflicts = lc.detect_contradictions([OPPOSING_A, UNRELATED])
    assert conflicts == []


def test_conflict_resolution_is_deterministic_recency_first():
    """§2 step 2: fresher evidence wins; loser leaves the recall index as
    'contested' (docs/KNOWLEDGE-LIFECYCLE.md §2)."""
    rules = [dict(OPPOSING_A), dict(OPPOSING_B)]
    conflicts = lc.detect_contradictions(rules)
    rules, conflicts = lc.resolve_conflicts(rules, conflicts)
    c = conflicts[0]
    assert c["winner"] == "L001" and c["loser"] == "L002"  # 2026-08 > 2026-05
    assert c["resolution_basis"] == "evidence_recency"
    by_id = {r["id"]: r for r in rules}
    assert by_id["L002"]["lifecycle_state"] == "contested"
    assert by_id["L001"].get("lifecycle_state") not in lc.NON_SERVING
    served = {r["id"] for r in lc.filter_servable(rules)}
    assert "L002" not in served and "L001" in served


def test_high_confidence_loser_flags_pending_human_review():
    """§5 authority matrix: benching a confidence>=8 rule is executed but
    flagged pending_human_review (D1)."""
    a = _rule("L010", "NEVER retry a failed push immediately", conf=8,
              added="2026-08-01")
    b = _rule("L011", "ALWAYS retry a failed push immediately", conf=9,
              added="2026-04-01")
    rules, conflicts = lc.resolve_conflicts([a, b],
                                            lc.detect_contradictions([a, b]))
    c = conflicts[0]
    assert c["loser"] == "L011" and c["pending_human_review"] is True


# --- eviction (KNOWLEDGE-LIFECYCLE.md §1 serving set, §4 triggers) ---

def test_revert_verdict_evicts_from_recall_index():
    """State machine §1/§4: a REVERT verdict moves the rule to 'deprecated'
    and the recall index (filter_servable) stops serving it — in the JSON
    library AND therefore in the rebuilt Hindsight bank, which retains only
    filter_servable(rules). Docstring anchor: docs/KNOWLEDGE-LIFECYCLE.md §1."""
    rules = [dict(OPPOSING_A), dict(UNRELATED)]
    lc.apply_verdicts(rules, {"L003": "REVERT"}, by="test")
    by_id = {r["id"]: r for r in rules}
    assert by_id["L003"]["lifecycle_state"] == "deprecated"
    assert by_id["L003"]["lifecycle_history"][-1]["by"] == "test"
    served = {r["id"] for r in lc.filter_servable(rules)}
    assert "L003" not in served and "L001" in served


def test_legacy_rules_without_state_are_grandfathered_servable():
    assert lc.filter_servable([{"id": "X", "rule": "r"}])  # absent field serves


def test_efficacy_retrieval_never_serves_deprecated():
    """The live retrieval surface (scripts/efficacy_eval.py) loads rules
    through filter_servable: a deprecated rule with a perfect keyword match
    must not be retrieved."""
    import importlib.util
    from pathlib import Path
    import pytest
    repo = Path(__file__).resolve().parent.parent
    if not (repo / "scripts" / "efficacy_eval.py").is_file():
        pytest.skip("scripts/efficacy_eval.py is not part of the minimal (vendored) runtime")
    spec = importlib.util.spec_from_file_location(
        "efficacy_eval", repo / "scripts" / "efficacy_eval.py")
    ee = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ee)
    probe = {"rules_query": "release commit staging git add",
             "situation": "about to stage a release commit"}
    rules = [dict(OPPOSING_A, lifecycle_state="deprecated"), dict(UNRELATED)]
    hits = ee.retrieve(probe, rules)   # raw list: retrieve() itself must gate
    assert all(r["id"] != "L001" for r in hits)


# --- decay (KNOWLEDGE-LIFECYCLE.md §3, decision D2) ---

def test_decay_half_life_and_dormancy():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    fresh = _rule("F", "rule", conf=8, added="2026-08-01")
    old = _rule("O", "rule", conf=8, added="2025-08-01")       # >2 half-lives
    ancient = _rule("A", "rule", conf=4, added="2024-08-01")   # floors to <=2
    assert lc.decayed_confidence(fresh, now) == 8
    assert lc.decayed_confidence(old, now) == 2                # 8 // 2**2
    assert lc.decayed_confidence(ancient, now) <= 2
    undated = _rule("U", "rule", conf=7)
    assert lc.decayed_confidence(undated, now) == 7            # no clock, no decay
    marked = lc.apply_decay([fresh, ancient], now)
    by_id = {r["id"]: r for r in marked}
    assert by_id["A"]["lifecycle_state"] == "dormant"
    assert by_id["F"].get("lifecycle_state") not in lc.NON_SERVING


# --- mint-time evidence digests (KNOWLEDGE-LIFECYCLE.md §6) ---

def _fake_session(tmp_path, texts):
    p = tmp_path / "feedbeef-1111.jsonl"
    lines = [json.dumps({"type": "assistant",
                         "message": {"content": [{"type": "text", "text": t}]}})
             for t in texts]
    p.write_text("\n".join(lines) + "\n")
    return p


SCRUB_CFG = {
    "version": 1,
    "replacements": [{"class": "test-customer",
                      "regex": r"(?<![A-Za-z0-9])plantedcorp(?![A-Za-z0-9])",
                      "placeholder": "<CUSTOMER_T>", "flags": "i"}],
    "numbered": [],
    "detectors": [{"class": "detect-aws-access-key",
                   "regex": r"\bAKIA[0-9A-Z]{16}\b"}],
}


def test_mint_digest_no_scrub_config_persists_nothing(tmp_path, monkeypatch):
    """§6: no scrub config ⇒ no digest (fail-closed), status says why."""
    monkeypatch.setenv("LOGLEARNER_SCRUB_CONFIG", str(tmp_path / "absent.json"))
    sess = _fake_session(tmp_path, ["harmless text"])
    rec = ed.mint_digest("feedbeef-1111", 1, 1, session_path=sess)
    assert rec["status"] == "no-scrub-config" and "digest" not in rec


def test_mint_digest_quarantines_detector_hit(tmp_path, monkeypatch):
    """§6: detector hit on the scrubbed digest ⇒ quarantined, no text kept."""
    cfg = tmp_path / "scrub.json"
    cfg.write_text(json.dumps(SCRUB_CFG))
    monkeypatch.setenv("LOGLEARNER_SCRUB_CONFIG", str(cfg))
    sess = _fake_session(tmp_path, ["export AWS_ACCESS_KEY_ID=AKIA"
                                    + "AAAABBBBCCCCDDDD"])
    rec = ed.mint_digest("feedbeef-1111", 1, 1, session_path=sess)
    assert rec["status"] == "quarantined" and "digest" not in rec


def test_mint_digest_scrubs_and_persists_clean_window(tmp_path, monkeypatch):
    cfg = tmp_path / "scrub.json"
    cfg.write_text(json.dumps(SCRUB_CFG))
    monkeypatch.setenv("LOGLEARNER_SCRUB_CONFIG", str(cfg))
    sess = _fake_session(tmp_path, ["customer plantedcorp hit a build error",
                                    "agent fixed it"])
    rec = ed.mint_digest("feedbeef-1111", 1, 2, session_path=sess)
    assert rec["status"] == "ok"
    assert "plantedcorp" not in rec["digest"].lower()
    assert "<CUSTOMER_T>" in rec["digest"]
    assert "build error" in rec["digest"]
