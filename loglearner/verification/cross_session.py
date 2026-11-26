"""
Cross-session recall check.

A rule extracted from session A is more trustworthy if its keywords also
appear (with matching category) in sessions B, C, D. Orphaned rules —
extracted from only one session AND with merge_count=1 — are candidates
for REVERT regardless of their intrinsic score.

Implementation: share the keyword extraction + context-match logic with
scripts/backtest.py (both answer "would this rule have fired?"), but
operate over the training corpus rather than labeled gaps.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

_STOP = {"when", "then", "always", "never", "if", "you", "your", "a", "an", "the", "to",
         "and", "or", "but", "not", "is", "are", "be", "with", "from", "for", "of", "on",
         "in", "at", "as", "that", "this", "do", "does", "done", "it", "its", "by", "any",
         "all", "use", "using", "must", "should", "may", "can", "stop", "no", "yes"}


def _rule_keywords(rule_text: str, top_k: int = 8) -> set[str]:
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", rule_text.lower())
    kws = [w for w in words if w not in _STOP]
    seen: dict[str, int] = {}
    for w in kws:
        seen[w] = seen.get(w, 0) + 1
    ranked = sorted(seen.items(), key=lambda kv: (-kv[1], list(seen).index(kv[0])))
    return {w for w, _ in ranked[:top_k]}


def _session_text(path: Path, sample_lines: int = 500) -> str:
    out: list[str] = []
    with open(path, errors="ignore") as f:
        for i, line in enumerate(f):
            if i >= sample_lines:
                break
            out.append(line)
    return "".join(out).lower()


def cross_session_recall(rule: dict, session_paths: Iterable[Path], min_kw_hits: int = 4) -> dict:
    """For a rule, count how many of the given sessions contain at least
    `min_kw_hits` of its keywords in the first 500 lines. Used to detect
    orphaned rules (recall_count == 0).

    Returns:
        {recall_count, recall_sessions, orphan: bool}
    """
    kws = _rule_keywords(rule.get("rule", ""))
    if not kws:
        return {"recall_count": 0, "recall_sessions": [], "orphan": True}

    recall_sessions: list[str] = []
    for path in session_paths:
        text = _session_text(Path(path))
        hits = sum(1 for kw in kws if kw in text)
        if hits >= min_kw_hits:
            recall_sessions.append(str(path))

    merge_count = int(rule.get("merge_count", 1) or 1)
    orphan = len(recall_sessions) == 0 and merge_count < 2
    return {
        "recall_count": len(recall_sessions),
        "recall_sessions": recall_sessions[:5],  # cap for readability
        "orphan": orphan,
    }
