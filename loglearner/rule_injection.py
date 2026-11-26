"""Rule->prompt injection — the executable half of
docs/RULE-INJECTION.md.

The one entry point is `render_rules_block`: lifecycle gate -> read-only
conflict resolution -> confidence-ranked greedy packing under a token
budget -> a stable block (single header line + `- [id] rule` lines) plus
a manifest recording every dropped id and why. Rendering a prompt never
mutates the library.
"""

from __future__ import annotations

from .lifecycle import (
    _resolution_key,
    detect_contradictions,
    effective_confidence,
    filter_servable,
    is_servable,
)

DEFAULT_TOKEN_BUDGET = 1200
_CHARS_PER_TOKEN = 4

HEADER = ("RULES RECALLED FROM PAST SESSIONS (mined from real agent "
          "failures; advisory context — consult before deciding, and on "
          "any conflict the task instructions above win):")


def estimate_tokens(text: str) -> int:
    """Deterministic chars//4 heuristic (docs/RULE-INJECTION.md §3)."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _drop_conflict_losers(rules: list[dict]) -> tuple[list[dict], list[str]]:
    """Read-only twin of lifecycle.resolve_conflicts: same total order
    (shared _resolution_key + id tiebreak), no mark(), no state change —
    a prompt build must not mutate lifecycle state (§2)."""
    conflicts = detect_contradictions(rules)
    losers: set[str] = set()
    by_id = {r.get("id"): r for r in rules}
    for c in conflicts:
        a, b = by_id.get(c["rule_a"]), by_id.get(c["rule_b"])
        if a is None or b is None:
            continue
        ka, kb = _resolution_key(a), _resolution_key(b)
        if ka == kb:
            loser = max((a, b), key=lambda r: r.get("id", ""))
        else:
            loser = b if ka > kb else a
        losers.add(loser.get("id"))
    return [r for r in rules if r.get("id") not in losers], sorted(losers)


def select_rules(rules: list[dict], budget: int = DEFAULT_TOKEN_BUDGET,
                 now=None) -> tuple[list[dict], dict]:
    """Selection pipeline; returns (selected, manifest)."""
    servable = filter_servable(list(rules))
    benched = sorted(r.get("id", "?") for r in rules if not is_servable(r))
    resolved, conflict_dropped = _drop_conflict_losers(servable)
    ranked = sorted(
        resolved,
        key=lambda r: (-effective_confidence(r, now),
                       -int(r.get("merge_count", 1) or 1),
                       r.get("id", "")))
    spent = estimate_tokens(HEADER)
    selected, over_budget = [], []
    for r in ranked:
        line_cost = estimate_tokens(f"- [{r.get('id', '?')}] {r['rule']}")
        if spent + line_cost <= budget:
            selected.append(r)
            spent += line_cost
        else:
            # dropped WHOLE, packing continues (never truncate mid-rule)
            over_budget.append(r.get("id", "?"))
    manifest = {
        "included": [r.get("id", "?") for r in selected],
        "dropped_benched": benched,
        "dropped_conflict": conflict_dropped,
        "dropped_budget": over_budget,
        "est_tokens": spent if selected else 0,
        "budget": budget,
    }
    return selected, manifest


def render_rules_block(rules: list[dict],
                       budget: int = DEFAULT_TOKEN_BUDGET,
                       now=None) -> tuple[str | None, dict]:
    """(block_text | None, manifest). None = inject nothing — callers must
    not emit an empty header when no rule survives selection."""
    selected, manifest = select_rules(rules, budget, now)
    if not selected:
        return None, manifest
    lines = [HEADER] + [f"- [{r.get('id', '?')}] {r['rule']}"
                        for r in selected]
    return "\n".join(lines), manifest
