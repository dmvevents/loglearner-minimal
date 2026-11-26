"""Example isolation artifacts: one self-contained,
prompt-injectable JSON per KEEP example at output/isolates/{example_id}.json.

Each isolate carries: a stable content-hash id, a path-anonymized situation,
the exact transcript window it came from, the wrong-path/right-path
counterfactual, a keyword-recall signature (gap_type + category + domains +
tool_shape), and a prompt_ready_md block kept under ~2000 tokens so any
planner can inject it wholesale at plan time.

Retrieval model: signature keys match on NEW sessions' plain keywords —
a plan-time recall is a dict lookup, not a vector scan.
"""

import hashlib
import json
import os
import re
from pathlib import Path

from loglearner.sinks.hindsight_sink import _sanitize_for_retain

REPO = Path(__file__).resolve().parent.parent

_HOME_RE = re.compile(r"/home/[a-z0-9_-]+")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

_ROLE_MAP = {"text": None, "tool_use": "tool", "tool_result": "result"}


def isolates_dir():
    return Path(os.environ.get("LOGLEARNER_ISOLATES_DIR",
                               REPO / "output" / "isolates"))


def _anon(text):
    """Path/host anonymization: home dirs and IPs never ship."""
    text = _HOME_RE.sub("~", str(text or ""))
    text = _IP_RE.sub("<IP>", text)
    return _sanitize_for_retain(text)


def _transcript(context, max_items=40, max_chars=300):
    out = []
    for a in (context.actions if hasattr(context, "actions") else []):
        a = a if isinstance(a, dict) else a.__dict__
        role = _ROLE_MAP.get(a.get("action_type")) or a.get("role", "user")
        text = (a.get("text") or a.get("tool_output_summary")
                or f"{a.get('tool_name', '')} {a.get('tool_input_summary', '')}".strip())
        if not text:
            continue
        out.append({"role": role, "line": a.get("line_num", 0),
                    "text": _anon(text)[:max_chars]})
        if len(out) >= max_items:
            break
    return out


def example_id_for(example):
    basis = "|".join([
        getattr(example, "instinct_rule", "") or "",
        (getattr(example, "negative_example", {}) or {}).get("situation", ""),
        (getattr(example, "negative_example", {}) or {}).get("action", ""),
    ])
    return "iso_" + hashlib.sha256(basis.encode()).hexdigest()[:12]


def build_isolate(example, context=None, instinct_ref="", project="",
                  domains=()):
    neg = getattr(example, "negative_example", {}) or {}
    pos = getattr(example, "positive_example", {}) or {}
    gap = (getattr(example, "gap_summary", {}) or {})
    gap_type = gap.get("gap_type", "")
    category = getattr(example, "category", "") or ""
    a, b = (getattr(example, "line_range", (0, 0)) or (0, 0))[:2]

    tool_shape = []
    if context is not None:
        for t in (getattr(context, "failure", {}) or {}).get("failed_tools", []):
            name = t.get("name")
            if name and name not in tool_shape:
                tool_shape.append(name)

    situation = _anon(neg.get("situation", ""))[:600]
    wrong = {k: _anon(neg.get(k, ""))[:400]
             for k in ("action", "consequence", "root_cause")}
    right = {k: _anon(pos.get(k, ""))[:400] for k in ("action", "reasoning")}
    rule = _anon(getattr(example, "instinct_rule", ""))[:400]

    md = (f"### Situation\n{situation}\n\n"
          f"### Wrong path\n{wrong['action']}\n"
          f"**Consequence:** {wrong['consequence']}\n"
          f"**Root cause:** {wrong['root_cause']}\n\n"
          f"### Right path\n{right['action']}\n"
          f"**Why:** {right['reasoning']}\n\n"
          f"### Rule\n{rule}\n")

    return {
        "example_id": example_id_for(example),
        "instinct_ref": instinct_ref,
        "title": rule[:160],
        "situation": situation,
        "transcript": _transcript(context) if context is not None else [],
        "wrong_path": wrong,
        "right_path": right,
        "would_prevent": [x for x in (f"gap_type:{gap_type}" if gap_type else "",
                                      f"category:{category}" if category else "")
                          if x],
        "provenance": {
            "session_id": getattr(example, "session_id", ""),
            "lines": [a, b],
            "project": project,
            "model_agreement": getattr(example, "model_agreement", ""),
        },
        "signature": {
            "gap_type": gap_type,
            "category": category,
            "domains": list(domains),
            "tool_shape": tool_shape,
        },
        "prompt_ready_md": md,
        "token_estimate": len(md) // 4,
    }


def write_isolates(examples, contexts=None, out_dir=None, project="",
                   session_adapters=None):
    """One JSON per example. Contexts matched by (session_id, line overlap).
    Returns the written paths."""
    out_dir = Path(out_dir or isolates_dir())
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx_index = {}
    for c in contexts or []:
        g = c.gap if isinstance(c.gap, dict) else {}
        ctx_index[(g.get("session_id"), g.get("start_line"), g.get("end_line"))] = c

    paths = []
    for i, ex in enumerate(examples, 1):
        sid = getattr(ex, "session_id", "")
        a, b = (getattr(ex, "line_range", (0, 0)) or (0, 0))[:2]
        context = ctx_index.get((sid, a, b))
        domains = (session_adapters or {}).get(sid, [])
        iso = build_isolate(ex, context=context, instinct_ref=f"L{i:03d}",
                            project=project, domains=domains)
        p = out_dir / f"{iso['example_id']}.json"
        p.write_text(json.dumps(iso, indent=1, ensure_ascii=False))
        paths.append(p)
    return paths
