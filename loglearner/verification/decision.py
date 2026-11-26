"""
Rule-level KEEP / MAYBE / REVERT verdict assignment.

Lightweight threshold-based gate over the rule schema produced by
example_synthesizer.py + instinct_deduplicator.py. Not a port of reference's
decision_engine.py (which is A/B benchmark comparison — different domain).

Verdict logic (intentionally simple for Sprint 2):

    KEEP    if cross_project AND merge_count >= 2
    KEEP    if confidence >= 8 AND has >= 2 evidence sources
    MAYBE   if confidence in [5, 7] OR (confidence >= 8 but only 1 evidence source)
    REVERT  otherwise (confidence < 5, no evidence, orphaned rule)

Category-specific boost: rules in 'synchronization' or 'api_misuse' with
confidence >= 7 get promoted one tier (these categories are high-impact
and usually have clear signals).

Category-specific penalty: 'unknown' category with confidence < 9 is
demoted one tier (too vague to ship without explicit category).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.json"


@lru_cache(maxsize=1)
def _config() -> dict:
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text())
    # Fallback defaults when config.json is absent (test environments).
    return {
        "high_impact_categories": ["synchronization", "api_misuse"],
        "low_impact_categories": [],
        "score_thresholds": {"keep": 0.70, "maybe": 0.45},
        "weights": {"confidence": 0.35, "evidence": 0.25, "cross_project": 0.25, "category": 0.15},
    }


class Verdict(str, Enum):
    KEEP = "KEEP"
    MAYBE = "MAYBE"
    REVERT = "REVERT"


@dataclass(frozen=True)
class VerificationResult:
    rule_id: str
    verdict: Verdict
    score: float           # 0.0 - 1.0 composite
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "verdict": self.verdict.value,
            "score": round(self.score, 3),
            "reasons": list(self.reasons),
        }


# Score components — weights come from config.json (see _config()).
# Overridable without code edits (a review flagged the hardcoded list).


def _score(rule: dict) -> tuple[float, list[str]]:
    """Compute 0-1 score + reason list. Higher is better."""
    cfg = _config()
    high_impact = {c.lower() for c in cfg.get("high_impact_categories", [])}
    weights = cfg.get("weights", {})
    w_conf = weights.get("confidence", 0.35)
    w_ev = weights.get("evidence", 0.25)
    w_cross = weights.get("cross_project", 0.25)
    w_cat = weights.get("category", 0.15)

    reasons: list[str] = []

    conf = float(rule.get("confidence", 0))
    conf_norm = min(conf, 10.0) / 10.0
    reasons.append(f"confidence={conf:.0f}/10")

    ev = rule.get("evidence_sources") or rule.get("evidence", [])
    if isinstance(ev, str):
        ev_count = 1 if ev else 0
    else:
        ev_count = len(ev) if ev else 0
    ev_norm = min(ev_count / 3.0, 1.0)
    reasons.append(f"evidence_sources={ev_count}")

    cross = bool(rule.get("cross_project"))
    merge_count = int(rule.get("merge_count", 1) or 1)
    if cross and merge_count >= 2:
        cross_norm = 1.0
        reasons.append(f"cross_project=true merge_count={merge_count}")
    elif merge_count >= 2:
        cross_norm = 0.6
        reasons.append(f"merge_count={merge_count} (not cross-project)")
    else:
        cross_norm = 0.0
        reasons.append("single-source")

    cat = (rule.get("category") or "unknown").lower()
    if cat in high_impact:
        cat_norm = 1.0
        reasons.append(f"category={cat} (high-impact)")
    elif cat == "unknown":
        cat_norm = 0.2
        reasons.append("category=unknown (penalty)")
    else:
        cat_norm = 0.6
        reasons.append(f"category={cat}")

    score = (
        conf_norm * w_conf
        + ev_norm * w_ev
        + cross_norm * w_cross
        + cat_norm * w_cat
    )
    return score, reasons


def classify_verdict(rule: dict) -> VerificationResult:
    """Assign KEEP / MAYBE / REVERT to a single rule."""
    cfg = _config()
    thresholds = cfg.get("score_thresholds", {})
    keep_th = thresholds.get("keep", 0.70)
    maybe_th = thresholds.get("maybe", 0.45)
    high_impact = {c.lower() for c in cfg.get("high_impact_categories", [])}

    rule_id = rule.get("id", "?")
    score, reasons = _score(rule)

    if score >= keep_th:
        verdict = Verdict.KEEP
    elif score >= maybe_th:
        verdict = Verdict.MAYBE
    else:
        verdict = Verdict.REVERT

    cat = (rule.get("category") or "unknown").lower()
    conf = float(rule.get("confidence", 0))

    if cat == "unknown" and conf < 9 and verdict == Verdict.KEEP:
        verdict = Verdict.MAYBE
        reasons.append("demoted: unknown category + confidence<9")
    elif cat in high_impact and conf >= 7 and verdict == Verdict.MAYBE:
        verdict = Verdict.KEEP
        reasons.append("promoted: high-impact category + confidence>=7")

    return VerificationResult(
        rule_id=rule_id,
        verdict=verdict,
        score=score,
        reasons=tuple(reasons),
    )
