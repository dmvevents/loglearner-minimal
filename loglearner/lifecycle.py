"""Knowledge-object lifecycle — the executable half of
docs/KNOWLEDGE-LIFECYCLE.md.

States: candidate -> screened -> recalled -> rewarded, with three benched
states (deprecated, contested, dormant) that the recall index never serves.
Everything here is deterministic (north-star invariant 6): contradiction
detection is lexical polarity + signature overlap, resolution is a total
order (evidence recency -> confidence -> merge_count -> id), decay is a
computed half-life. LLM output proposes; these functions dispose.

Eviction is a state change plus a filter, never a deletion:
`filter_servable` is THE recall-index membership test — the Hindsight sink
retains only servable rules (the bank rebuild evicts benched ones) and
deterministic retrieval (scripts/efficacy_eval.py) filters at load.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# States excluded from every recall surface (docs/KNOWLEDGE-LIFECYCLE.md §1).
NON_SERVING = frozenset({"deprecated", "contested", "dormant"})

DECAY_HALF_LIFE_DAYS = 180        # owner decision D2
DORMANCY_CONFIDENCE = 2           # decayed conf <= this => dormant
UNVERIFIABLE_CONFIDENCE_CAP = 5   # evidence expired + no digest (calibration cap)

_NEG = re.compile(r"\b(never|don'?t|do not|avoid|stop|refuse|ban)\b", re.I)
_POS = re.compile(r"\b(always|must|ensure|require|prefer)\b", re.I)
_WORD = re.compile(r"[a-z][a-z_-]{2,}")
_STOP = frozenset(
    "the and for with that this you your are was were from into when where "
    "before after all any one two has have had not now then them they its "
    "never don dont avoid stop refuse ban always must ensure require prefer "
    "should would could can may might will shall doing done".split())
_MIN_OVERLAP = 0.5


def is_servable(rule: dict) -> bool:
    """Recall-index membership. Legacy rules without a lifecycle_state field
    are grandfathered as 'screened' (servable) — the shipped unified library
    predates the field."""
    return rule.get("lifecycle_state") not in NON_SERVING


def filter_servable(rules: list[dict]) -> list[dict]:
    return [r for r in rules if is_servable(r)]


def mark(rule: dict, state: str, by: str, reason: str) -> dict:
    """Transition a rule (in place) and append to its history ledger."""
    rule["lifecycle_state"] = state
    rule.setdefault("lifecycle_history", []).append({
        "state": state, "by": by, "reason": reason[:200],
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return rule


def apply_verdicts(rules: list[dict], verdicts: dict[str, str],
                   by: str = "verification-gate") -> list[dict]:
    """REVERT/refuted => deprecated (fail-safe: bench first, review after).
    High-confidence benches carry pending_human_review (authority matrix D1)."""
    for r in rules:
        v = (verdicts.get(r.get("id", "")) or "").upper()
        if v in ("REVERT", "REFUTED"):
            mark(r, "deprecated", by, f"verdict={v}")
            if float(r.get("confidence", 0)) >= 8:
                r["pending_human_review"] = True
    return rules


def directive_polarity(text: str) -> str | None:
    """Leading directive wins (reference rules lead with NEVER/ALWAYS/WHEN/STOP).
    Lexical, not semantic: 'never skip X' reads negative even though it
    mandates X — the conflict queue is reviewable for exactly this reason."""
    neg, pos = _NEG.search(text or ""), _POS.search(text or "")
    if neg and (not pos or neg.start() < pos.start()):
        return "negative"
    if pos:
        return "positive"
    return None


def _signature(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP}


def detect_contradictions(rules: list[dict]) -> list[dict]:
    """Opposite polarity + signature overlap >= 0.5 => conflict record
    (docs/KNOWLEDGE-LIFECYCLE.md §2 step 1: flag, never silently keep both)."""
    enriched = [(r, directive_polarity(r.get("rule", "")),
                 _signature(r.get("rule", ""))) for r in rules]
    conflicts: list[dict] = []
    for i in range(len(enriched)):
        ra, pa, sa = enriched[i]
        if pa is None or not sa:
            continue
        for j in range(i + 1, len(enriched)):
            rb, pb, sb = enriched[j]
            if pb is None or pb == pa or not sb:
                continue
            overlap = sa & sb
            if len(overlap) / min(len(sa), len(sb)) >= _MIN_OVERLAP:
                conflicts.append({
                    "conflict_id": f"C{len(conflicts) + 1:03d}",
                    "rule_a": ra.get("id"), "rule_b": rb.get("id"),
                    "polarity_a": pa, "polarity_b": pb,
                    "shared_terms": sorted(overlap)[:12],
                    "detected_at": datetime.now(timezone.utc)
                                   .isoformat(timespec="seconds"),
                })
    return conflicts


def _added_date(rule: dict) -> str:
    return (rule.get("provenance") or {}).get("added_date") or ""


def _resolution_key(rule: dict) -> tuple:
    return (_added_date(rule), float(rule.get("confidence", 0)),
            int(rule.get("merge_count", 1) or 1))


def resolve_conflicts(rules: list[dict], conflicts: list[dict],
                      ) -> tuple[list[dict], list[dict]]:
    """Deterministic total order: evidence recency -> confidence ->
    merge_count -> id. Loser => contested (out of the recall index);
    confidence>=8 loser flags pending_human_review (D1). Winner keeps
    serving — recalling both directives would inject the contradiction
    straight into an agent's prompt."""
    by_id = {r.get("id"): r for r in rules}
    for c in conflicts:
        a, b = by_id.get(c["rule_a"]), by_id.get(c["rule_b"])
        if a is None or b is None:
            continue
        ka, kb = _resolution_key(a), _resolution_key(b)
        if ka == kb:
            winner, loser = sorted((a, b), key=lambda r: r.get("id", ""))
            basis = "id_tiebreak"
        else:
            winner, loser = (a, b) if ka > kb else (b, a)
            wk, lk = (ka, kb) if ka > kb else (kb, ka)
            basis = ("evidence_recency" if wk[0] != lk[0] else
                     "confidence" if wk[1] != lk[1] else "merge_count")
        c.update(winner=winner.get("id"), loser=loser.get("id"),
                 resolution_basis=basis,
                 pending_human_review=float(loser.get("confidence", 0)) >= 8)
        mark(loser, "contested", "conflict-resolution",
             f"lost {c['conflict_id']} to {winner.get('id')} on {basis}")
        if c["pending_human_review"]:
            loser["pending_human_review"] = True
        winner.setdefault("conflict_ids", []).append(c["conflict_id"])
    return rules, conflicts


def write_conflicts(conflicts: list[dict], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for c in conflicts:
            f.write(json.dumps(c) + "\n")
    return len(conflicts)


def _last_exercised(rule: dict) -> datetime | None:
    """Exercise clock: added_date, reset by any recalled/rewarded transition."""
    best: datetime | None = None
    added = _added_date(rule)
    if added:
        try:
            best = datetime.strptime(added, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            best = None
    for h in rule.get("lifecycle_history", []):
        if h.get("state") in ("recalled", "rewarded"):
            try:
                ts = datetime.fromisoformat(h["at"])
                if best is None or ts > best:
                    best = ts
            except (KeyError, ValueError):
                continue
    return best


def decayed_confidence(rule: dict, now: datetime | None = None,
                       half_life_days: int = DECAY_HALF_LIFE_DAYS) -> int:
    """conf(t) = conf0 // 2^(completed half-lives unexercised) — computed,
    never destructively written (D2). Step decay: nothing is lost until a
    full half-life passes. Undated rules do not decay; the evidence cap
    is applied separately by effective_confidence."""
    conf = int(float(rule.get("confidence", 0)))
    last = _last_exercised(rule)
    if last is None:
        return conf
    now = now or datetime.now(timezone.utc)
    elapsed_days = max(0.0, (now - last).total_seconds() / 86400.0)
    return conf // (2 ** int(elapsed_days // half_life_days))


def effective_confidence(rule: dict, now: datetime | None = None) -> int:
    """Decay plus the unverifiable cap: evidence expired AND no mint-time
    digest => cap at 5 (consensus-screened ceiling, #17)."""
    conf = decayed_confidence(rule, now)
    if rule.get("evidence_verifiable") is False and not rule.get("evidence_digest_ref"):
        conf = min(conf, UNVERIFIABLE_CONFIDENCE_CAP)
    return conf


def apply_decay(rules: list[dict], now: datetime | None = None) -> list[dict]:
    """Maintenance pass: serving rules whose effective confidence fell to
    the floor move to dormant (restore is a human action, §5)."""
    for r in rules:
        if is_servable(r) and decayed_confidence(r, now) <= DORMANCY_CONFIDENCE:
            if _last_exercised(r) is not None:
                mark(r, "dormant", "decay-pass",
                     f"decayed confidence <= {DORMANCY_CONFIDENCE} "
                     f"(half-life {DECAY_HALF_LIFE_DAYS}d)")
    return rules
