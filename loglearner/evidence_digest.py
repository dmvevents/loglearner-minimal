"""Mint-time evidence archival (mandated by the calibration audit).

Claude Code purges session JSONL after ~3.5 months; 49/55 shipped rules had
already lost their cited evidence by audit time, making them unfalsifiable.
This module persists a SCRUBBED digest of each rule's cited session window
next to the library it attests (<output-dir>/evidence-digests.jsonl), minted
at output time — so verification survives retention.

Fail-closed like every export surface: no scrub config => nothing persisted
(status "no-scrub-config"); detector hit on the scrubbed digest => nothing
persisted (status "quarantined"). Only status "ok" records carry text.

The calibration audit (scripts/calibration_audit.py) shares these extraction
helpers but writes LOCAL-ONLY unscrubbed digests for the human auditor —
a different artifact that never leaves state/calibration/ (gitignored).
"""

from __future__ import annotations

import glob
import json
from pathlib import Path

from .egress_gate import scrub_for_egress
from .export_gate import find_config

PROJECTS = Path.home() / ".claude" / "projects"
DIGEST_CAP = 1800


def find_session_file(uuid: str, projects_root: Path | None = None) -> Path | None:
    root = projects_root or PROJECTS
    hits = glob.glob(str(root / "*" / f"{uuid}*.jsonl"))
    return Path(hits[0]) if hits else None


def line_digest(obj: dict) -> str:
    t = obj.get("type", "?")
    msg = obj.get("message") or {}
    content = msg.get("content")
    bits = []
    if isinstance(content, str):
        bits.append(content[:220])
    elif isinstance(content, list):
        for b in content:
            bt = b.get("type")
            if bt == "text":
                bits.append(b.get("text", "")[:220])
            elif bt == "tool_use":
                arg = json.dumps(b.get("input", {}))[:160]
                bits.append(f"TOOL_USE {b.get('name')}: {arg}")
            elif bt == "tool_result":
                c = b.get("content")
                txt = c if isinstance(c, str) else json.dumps(c)[:220]
                bits.append(f"TOOL_RESULT: {str(txt)[:220]}")
    return f"[{t}] " + " | ".join(x.replace("\n", " ") for x in bits if x)


def extract_window(path: Path, start: int, end: int) -> list[str]:
    out = []
    with open(path, errors="replace") as f:
        for i, line in enumerate(f, 1):
            if i < start:
                continue
            if i > end:
                break
            try:
                out.append(line_digest(json.loads(line)))
            except json.JSONDecodeError:
                out.append("[unparseable line]")
    return out


def mint_digest(session_id: str, start: int, end: int,
                session_path: Path | None = None) -> dict:
    """Extract + scrub one rule's evidence window. The returned record always
    explains itself via `status`; only "ok" carries a digest."""
    ref = f"{(session_id or '?')[:8]} {start}-{end}"
    p = session_path or find_session_file(session_id)
    if not p or not Path(p).exists():
        return {"ref": ref, "status": "missing-file"}
    lines = extract_window(Path(p), int(start), int(end))
    if not lines:
        return {"ref": ref, "status": "empty-range"}
    if not find_config():
        return {"ref": ref, "status": "no-scrub-config"}
    scrubbed = scrub_for_egress("\n".join(lines)[:DIGEST_CAP],
                                "evidence-digest")
    if scrubbed is None:
        return {"ref": ref, "status": "quarantined"}
    return {"ref": ref, "status": "ok", "digest": scrubbed}


def mint_digests_for_examples(examples: list, out_path: Path) -> dict:
    """One digest record per shipped rule, ids matching write_instincts'
    L{i:03d} enumeration. Returns a status tally."""
    counts: dict[str, int] = {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for i, ex in enumerate(examples, 1):
            start, end = (getattr(ex, "line_range", (0, 0)) or (0, 0))
            rec = {
                "id": f"L{i:03d}",
                "rule": (getattr(ex, "instinct_rule", "") or "")[:120],
                "session": getattr(ex, "session_id", "") or "",
                "lines": [start, end],
                **mint_digest(getattr(ex, "session_id", "") or "", start, end),
            }
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            f.write(json.dumps(rec) + "\n")
    return counts
