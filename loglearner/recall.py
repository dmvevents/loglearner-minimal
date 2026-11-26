"""Recall over the installed knowledge banks, callable from a Bash tool call:

    python3 -m loglearner.cli recall "<query>" [--category X] [--k 5] [--json]

Read-only. The match semantics are the PreToolUse hook's (word-boundary tokens where an identifier's `_`
also matches a space or hyphen; rule-text hits rank first, then hits anywhere in title/tags/category/evidence,
then confidence, then recency), so what `recall` prints is what the hook would deliver for the same tokens.
The tokenizer is duplicated from scripts/hooks/pretooluse-instinct-context.py on purpose: the hook must stay
a standalone script (it runs on every tool call and must not import the package to match).
"""
import json
import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONF_RANK = {"high": 3, "med": 2, "medium": 2, "low": 1}
UNSERVABLE = {"deprecated", "contested", "dormant"}
RETIRED_STATUS = {"deprecated", "retired", "superseded", "withdrawn", "tombstoned"}   # same rule as the hook


def banks_dir():
    """Same resolution as the dashboard: LOGLEARNER_BANKS_DIR, then LOGLEARNER_LEARNINGS_DIR, then the default."""
    return Path(os.environ.get("LOGLEARNER_BANKS_DIR",
                               os.environ.get("LOGLEARNER_LEARNINGS_DIR",
                                              Path.home() / ".loglearner" / "banks"))).expanduser()


def store_files(extra=None):
    """Every bank file: *.jsonl under the banks dir (recursive, backups skipped) plus INSTINCTS_JSON when it
    points outside that dir."""
    d = banks_dir()
    files = sorted(p for p in d.rglob("*.jsonl") if p.is_file() and ".bak" not in p.name.lower()) if d.is_dir() else []
    for cand in ([extra] if extra else []) + [os.environ.get("INSTINCTS_JSON", "")]:
        if cand and Path(cand).expanduser().is_file() and Path(cand).expanduser().resolve() not in {f.resolve() for f in files}:
            files.append(Path(cand).expanduser())
    return files


def load_rows(files=None):
    """[(row, file, line_no, mtime)] for every parseable record; a .json store may be a list or {"instincts": [...]}."""
    out = []
    for f in (store_files() if files is None else files):
        try:
            mtime = f.stat().st_mtime
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if f.suffix == ".json":
            try:
                d = json.loads(text)
            except ValueError:
                continue
            rows = d if isinstance(d, list) else d.get("instincts", [])
            out.extend((r, f, n, mtime) for n, r in enumerate(rows) if isinstance(r, dict))
            continue
        for n, line in enumerate(text.splitlines()):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append((r, f, n, mtime))
    return out


def word_boundary(kw):
    parts = [re.escape(x) for x in str(kw).split("_") if x]
    body = r"[ _-]".join(parts) if parts else re.escape(str(kw))
    return re.compile(r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])", re.I)


def query_tokens(query, limit=12):
    """Lower-cased words (>= 3 chars) and identifiers; kebab-case becomes snake_case so it matches prose."""
    toks, seen = [], set()
    for t in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]*", query or ""):
        t = t.lower().strip(".+-").replace("-", "_")
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            toks.append(t)
    return toks[:limit]


def conf_rank(v):
    if isinstance(v, (int, float)):
        return float(v) / 10.0 * 3
    return CONF_RANK.get(str(v or "").lower(), 0)


def recall(query, k=5, category=None, status=None, rows=None):
    """Ranked records for `query`. Returns [] when nothing matches."""
    toks = query_tokens(query)
    if not toks:
        return []
    pats = [word_boundary(t) for t in toks]
    scored = []
    for r, f, n, mtime in (load_rows() if rows is None else rows):
        rule = str(r.get("rule") or r.get("text") or "").strip()
        if not rule or str(r.get("lifecycle_state") or "").lower() in UNSERVABLE:
            continue
        if str(r.get("status") or "").lower() in RETIRED_STATUS or r.get("tombstone") or r.get("retired"):
            continue
        if category and str(r.get("category") or "").lower() != category.lower():
            continue
        if status and str(r.get("status") or "").lower() != status.lower():
            continue
        rule_hits = sum(1 for p in pats if p.search(rule))
        others = " ".join([str(r.get("title") or ""), str(r.get("category") or ""), str(r.get("evidence") or ""),
                           str(r.get("source") or ""), " ".join(str(t) for t in (r.get("tags") or []))])
        any_hits = rule_hits + sum(1 for p in pats if p.search(others))
        if not any_hits:
            continue
        spec = sum(2 if ("_" in t or len(t) >= 8) else 1 for t, p in zip(toks, pats) if p.search(rule))
        scored.append(((-rule_hits, -spec, -any_hits, -conf_rank(r.get("confidence")), -mtime, -n), r, f))   # newest first on ties
    scored.sort(key=lambda t: t[0])
    return [dict(r, _store=str(f)) for _k, r, f in scored[:k]]


def format_hit(r):
    tags = ", ".join(str(t) for t in (r.get("tags") or []))
    meta = f"confidence={r.get('confidence', '?')} status={r.get('status', '?')}"
    if r.get("category"):
        meta += f" category={r['category']}"
    lines = [f"[{r.get('id', '?')}] {meta}", f"  {str(r.get('rule') or r.get('text') or '').strip()}"]
    tail = [f"evidence: {str(r.get('evidence') or 'n/a')[:300]}", f"source: {r.get('source') or 'n/a'}"]
    if r.get("version_scope"):
        tail.insert(0, f"scope: {r['version_scope']}")
    if tags:
        tail.append(f"tags: {tags}")
    lines.append("  " + " | ".join(tail))
    return "\n".join(lines)
