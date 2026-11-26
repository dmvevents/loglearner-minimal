"""Per-category retrieval router.

Recursive evaluation showed no single method dominates everywhere:
  - keyword wins on api_misuse (literal token overlap with rare nouns)
  - embed wins on build_deploy (semantic vocabulary clusters)
  - hindsight wins on architecture / security_circumvention / false_success / synchronization / bimodal_performance

When the caller knows the gap_category in advance (label, classifier output),
this router picks the right method. When not, it ensembles.

Doesn't add a new retrieval primitive — just dispatches across the three we
already have. Keeps the router cheap and auditable.
"""

from __future__ import annotations

from typing import Callable, Iterable

# Routing table derived from the v3 baseline and the v5 measurement.
# Updated whenever the recursive_eval per-category table shifts > 0.05.
_ROUTING_TABLE: dict[str, str] = {
    "api_misuse": "keyword",
    "build_deploy": "embed",
    "architecture": "hindsight",
    "scope_management": "hindsight",
    "synchronization": "hindsight",
    "recovery": "hindsight",
    "security_circumvention": "hindsight",
    "false_success": "hindsight",
    "bimodal_performance": "hindsight",
    "drift": "hindsight",
    "stuck": "hindsight",
    "measurement": "ensemble",
    "debugging": "ensemble",
    "unknown": "ensemble",
}

# Sparse-data fallback: if a category has < THRESHOLD label samples, use ensemble.
_ENSEMBLE_FALLBACK_THRESHOLD = 5


def route_method(category_hint: str | None, sample_count: int | None = None) -> str:
    """Pick the best retrieval method for a category.

    Returns one of: 'keyword', 'embed', 'hindsight', 'ensemble'.
    """
    if not category_hint:
        return "ensemble"
    cat = category_hint.lower()
    if sample_count is not None and sample_count < _ENSEMBLE_FALLBACK_THRESHOLD:
        return "ensemble"
    return _ROUTING_TABLE.get(cat, "ensemble")


def route(
    query: str,
    category_hint: str | None,
    keyword_top_k: Callable,
    embed_top_k: Callable,
    hindsight_top_k: Callable,
    rules: list[dict],
    k: int = 3,
    sample_count: int | None = None,
) -> list[dict]:
    """Dispatch a single retrieval to the best-suited method.

    All three top_k callables are passed in as plain functions to avoid coupling
    this module to backtest.py / recursive_eval.py / hindsight async details.

    keyword_top_k(query, rules, k) -> list[dict]
    embed_top_k(query, rules, k)   -> list[dict]
    hindsight_top_k(query, k)      -> list[dict]   (already has bank context)
    """
    method = route_method(category_hint, sample_count)
    if method == "keyword":
        return keyword_top_k(query, rules, k)
    if method == "embed":
        return embed_top_k(query, rules, k)
    if method == "hindsight":
        return hindsight_top_k(query, k)
    # ensemble: union of top-k from each, dedupe by id, cap at k
    seen_ids: set = set()
    out: list[dict] = []
    for fn_call in (
        lambda: keyword_top_k(query, rules, k),
        lambda: embed_top_k(query, rules, k),
        lambda: hindsight_top_k(query, k),
    ):
        try:
            hits = fn_call()
        except Exception:
            continue
        for h in hits:
            rid = h.get("id", repr(h))
            if rid in seen_ids:
                continue
            seen_ids.add(rid)
            out.append(h)
            if len(out) >= k:
                return out
    return out
