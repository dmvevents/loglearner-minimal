"""
Sprint 2 gap detectors.

Separate module to preserve the invariant that the original 5 modules
in loglearner/*.py are not modified. detect_gaps_extended() wraps
detect_gaps() and adds:

- detect_bimodal_performance  — p50 >> min in bench numbers
- detect_false_success        — assistant claims success, user corrects
- detect_timeline_gaps        — >5 min between agent actions (stuck)
- detect_drift                — same tool target called >20× in one session
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from .gap_detector import (
    GapRegion,
    detect_gaps as _detect_core,
    _parse_session_lines,
    _deduplicate_gaps,
    _EXTRA_SUCCESS_CLAIMS,
    _EXTRA_CORRECTION_WORDS,
)


# ---------- S2.2 bimodal performance ----------

_NUM_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(us|ms|GB/s|MB/s)\b", re.IGNORECASE)


def detect_bimodal_performance(lines: list, session_id: str, session_path: str,
                               min_samples: int = 20, ratio_threshold: float = 3.0) -> list[GapRegion]:
    """Flag sessions whose timing numbers show p50 > ratio_threshold × min."""
    path = Path(session_path)
    if not path.exists():
        return []
    us_samples: list[float] = []
    first_hit_line = last_hit_line = None
    with open(path, errors="ignore") as f:
        for i, line in enumerate(f, start=1):
            for m in _NUM_UNIT.finditer(line):
                val, unit = float(m.group(1)), m.group(2).lower()
                if unit == "us":
                    us = val
                elif unit == "ms":
                    us = val * 1000
                else:
                    continue
                if 10.0 <= us <= 10_000_000.0:
                    us_samples.append(us)
                    if first_hit_line is None:
                        first_hit_line = i
                    last_hit_line = i
    if len(us_samples) < min_samples:
        return []
    us_samples.sort()
    p50 = us_samples[len(us_samples) // 2]
    low = us_samples[0]
    if low == 0 or p50 / low < ratio_threshold:
        return []
    return [GapRegion(
        session_id=session_id,
        session_path=session_path,
        start_line=first_hit_line or 1,
        end_line=last_hit_line or 1,
        gap_type="bimodal_performance",
        severity=7,
        category="bimodal_performance",
        summary=f"p50={p50:.0f}us min={low:.0f}us ratio={p50/low:.1f}x (samples={len(us_samples)})",
    )]


# ---------- S2.3 false-success ----------

# Patterns live in gap_detector (applied at parse time on full text);
# aliased here for backward compatibility.
_SUCCESS_CLAIMS = _EXTRA_SUCCESS_CLAIMS
_CORRECTION_WORDS = _EXTRA_CORRECTION_WORDS


def detect_false_success(lines: list, session_id: str, session_path: str,
                          window_lines: int = 20) -> list[GapRegion]:
    """Assistant claims success, user pushes back within `window_lines`."""
    out: list[GapRegion] = []
    n = len(lines)
    for i, pl in enumerate(lines):
        if pl.msg_type != "assistant":
            continue
        if not pl.success_claim:  # precomputed on full text at parse time
            continue
        for j in range(i + 1, min(n, i + window_lines)):
            if lines[j].msg_type != "user":
                continue
            if lines[j].correction_word:
                utext = " ".join(lines[j].texts or [])
                out.append(GapRegion(
                    session_id=session_id,
                    session_path=session_path,
                    start_line=pl.line_num,
                    end_line=lines[j].line_num,
                    gap_type="false_success",
                    severity=8,
                    category="false_success",
                    summary=f"Claimed success -> user correction within {lines[j].line_num - pl.line_num} lines",
                    user_message=utext[:200],
                ))
                break
    return out


# ---------- S2.4 timeline gaps ----------

def detect_timeline_gaps(lines: list, session_id: str, session_path: str,
                          min_gap_seconds: int = 300, max_per_session: int = 10) -> list[GapRegion]:
    """Flag >=5-minute silences between agent actions as 'stuck' gaps.
    Capped per-session to avoid swamping the gap list — longest gaps win."""
    out: list[GapRegion] = []
    prev_ts = None
    prev_line = 0
    for pl in lines:
        ts = pl.timestamp or ""
        if not ts:
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if prev_ts is not None:
            delta = (t - prev_ts).total_seconds()
            if delta >= min_gap_seconds:
                out.append(GapRegion(
                    session_id=session_id,
                    session_path=session_path,
                    start_line=prev_line,
                    end_line=pl.line_num,
                    gap_type="timeline_gap",
                    severity=6,
                    category="stuck",
                    summary=f"{int(delta // 60)}min silence between actions",
                ))
        prev_ts = t
        prev_line = pl.line_num
    # Keep only the longest `max_per_session` gaps — long silences
    # carry more signal than a swarm of brief ones.
    out.sort(key=lambda g: -int(g.summary.split("min")[0]) if "min" in g.summary else 0)
    return out[:max_per_session]


# ---------- S2.5 drift stub ----------

def detect_drift(lines: list, session_id: str, session_path: str,
                  threshold: int = 20) -> list[GapRegion]:
    """Same tool target called > threshold times in one session."""
    from collections import Counter
    targets: Counter = Counter()
    first_line: dict[str, int] = {}
    last_line: dict[str, int] = {}
    for pl in lines:
        if pl.msg_type != "assistant":
            continue
        for tu in pl.tool_uses or []:
            key = f"{tu.get('name') or '?'}::{tu.get('drift_target', '')}"
            targets[key] += 1
            if key not in first_line:
                first_line[key] = pl.line_num
            last_line[key] = pl.line_num
    out: list[GapRegion] = []
    for key, count in targets.items():
        if count > threshold:
            out.append(GapRegion(
                session_id=session_id,
                session_path=session_path,
                start_line=first_line[key],
                end_line=last_line[key],
                gap_type="drift",
                severity=5,
                category="drift",
                summary=f"Tool target invoked {count}x in one session: {key[:80]}",
            ))
    return out


# ---------- unified dispatcher ----------

def detect_gaps_extended(session_path: str, session_id: str | None = None) -> list[GapRegion]:
    """Core detect_gaps() + 4 Sprint 2 detectors, merged and deduplicated."""
    path = Path(session_path)
    if session_id is None:
        session_id = path.stem

    # ONE parse shared by core + extras (was two full streams of the file).
    lines = list(_parse_session_lines(path))
    core = _detect_core(session_path, session_id, lines=lines)
    if not lines:
        return core

    extras: list[GapRegion] = []
    extras.extend(detect_bimodal_performance(lines, session_id, session_path))
    extras.extend(detect_false_success(lines, session_id, session_path))
    extras.extend(detect_timeline_gaps(lines, session_id, session_path))
    extras.extend(detect_drift(lines, session_id, session_path))

    # Per-category dedup: line-range collisions within the same category
    # collapse, but different categories at the same line range are kept.
    # This preserves signal density across the 10 detectors.
    all_gaps: list[GapRegion] = []
    by_cat: dict[str, list[GapRegion]] = {}
    for g in core + extras:
        by_cat.setdefault(g.category, []).append(g)
    for cat_gaps in by_cat.values():
        all_gaps.extend(_deduplicate_gaps(cat_gaps))
    all_gaps.sort(key=lambda g: (-g.severity, g.start_line))
    return all_gaps
