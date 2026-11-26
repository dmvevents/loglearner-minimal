#!/usr/bin/env python3
"""LogLearner dashboard — read-only local UI over the session corpus.

Security posture (deliberate, do not "fix"):
  * binds 127.0.0.1 ONLY (no interface option) — access remotely via SSH tunnel
  * GET-only HTTP surface; session JSONL files are never opened for write
  * stdlib only: http.server + sqlite3 + urllib (Hindsight proxy to localhost)

Usage: python3 dashboard/app.py [--port 7777]   (index first: dashboard/indexer.py)
"""

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from loglearner import export_gate  # noqa: E402

DB_PATH = Path(os.environ.get("LOGLEARNER_DB", REPO / "dashboard" / "index.db"))
# The Hindsight memory API: HINDSIGHT_API is where THIS server reaches it (loopback; the same name the harness and
# the spoke loop use). The person's browser may reach it elsewhere, e.g. through a proxy path such as /memory-api
# when only the dashboard's port is published, so links on the pages are built from LOGLEARNER_HINDSIGHT_LINK_BASE,
# which defaults to the server address (right when the person tunnels the same loopback port).
HINDSIGHT = os.environ.get("HINDSIGHT_API", "http://127.0.0.1:8888").rstrip("/")
HINDSIGHT_LINK_BASE = (os.environ.get("LOGLEARNER_HINDSIGHT_LINK_BASE") or HINDSIGHT).rstrip("/")
HINDSIGHT_BANK = os.environ.get("LOGLEARNER_HINDSIGHT_BANK", "loglearner")
UUID_RE = re.compile(r"^[0-9a-f-]{8,40}$")
EVIDENCE_RE = re.compile(r"session:([0-9a-f-]{8,40})\s+lines:(\d+)-(\d+)")

# ---------------------------------------------------------------- palette css
# Tokens from the dataviz reference palette (light + dark, toggle wins over OS).
CSS = """
:root { color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --seq:#2a78d6;
  --good:#0ca30c; --warn:#fab219; --serious:#ec835a; --crit:#d03b3b; }
:root[data-theme="dark"] { color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --seq:#3987e5; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7;
  --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --seq:#3987e5; } }
* { box-sizing:border-box }
body { margin:0; background:var(--page); color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
a { color:var(--s1); text-decoration:none } a:hover { text-decoration:underline }
nav { display:flex; gap:18px; align-items:center; padding:10px 22px;
  background:var(--surface); border-bottom:1px solid var(--grid);
  position:sticky; top:0; z-index:5 }
nav .brand { font-weight:700 } nav a { color:var(--ink2) }
nav a.on { color:var(--ink); font-weight:600 }
nav .sp { flex:1 } nav button { background:none; border:1px solid var(--axis);
  border-radius:6px; color:var(--ink2); padding:2px 10px; cursor:pointer }
main { max-width:1200px; margin:0 auto; padding:20px 22px 60px }
h1 { font-size:20px; margin:14px 0 } h2 { font-size:15px; color:var(--ink2);
  margin:22px 0 8px; font-weight:600 }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:12px; margin:16px 0 }
.tile { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:12px 14px }
.tile .v { font-size:26px; font-weight:700; letter-spacing:-.02em }
.tile .l { color:var(--muted); font-size:12px; margin-top:2px }
table { border-collapse:collapse; width:100%; background:var(--surface);
  border:1px solid var(--border); border-radius:10px; overflow:hidden }
th { text-align:left; color:var(--muted); font-size:12px; font-weight:600;
  padding:8px 12px; border-bottom:1px solid var(--grid) }
td { padding:7px 12px; border-bottom:1px solid var(--grid); vertical-align:top }
tr:last-child td { border-bottom:none }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums }
.chip { display:inline-block; font-size:11px; padding:1px 8px;
  border-radius:99px; border:1px solid var(--axis); color:var(--ink2) }
.chip.crit { color:var(--crit); border-color:var(--crit) }
.chip.warn2 { color:var(--serious); border-color:var(--serious) }
.card { background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:14px 16px; margin:10px 0 }
.muted { color:var(--muted) } .ink2 { color:var(--ink2) }
p.good { color:var(--good) } p.crit { color:var(--crit) }
.msg { border-left:3px solid var(--grid); margin:6px 0; padding:4px 10px;
  overflow-wrap:anywhere }
.msg .who { font-size:11px; color:var(--muted); font-weight:600 }
.msg.user { border-color:var(--s1) } .msg.assistant { border-color:var(--s3) }
.msg.tool { border-color:var(--axis) }
.msg.sys { border-color:var(--grid); padding:1px 10px; font-size:12px }
.msg.gapline { background:color-mix(in srgb, var(--crit) 7%, transparent) }
.gapmark { border:1px solid var(--crit); border-radius:8px; padding:8px 12px;
  margin:10px 0; font-size:13px }
.gapmark b { color:var(--crit) }
pre { background:color-mix(in srgb, var(--ink) 5%, transparent); padding:8px;
  border-radius:6px; overflow-x:auto; font-size:12px; white-space:pre-wrap }
details summary { cursor:pointer; color:var(--muted); font-size:12px }
.bar-tip { position:fixed; background:var(--ink); color:var(--page);
  padding:3px 9px; border-radius:6px; font-size:12px; pointer-events:none;
  display:none; z-index:9 }
.pager a { margin-right:12px }
input,select { background:var(--surface); color:var(--ink);
  border:1px solid var(--axis); border-radius:6px; padding:4px 8px }
#cy { height:74vh; min-height:520px; background:var(--surface);
  border:1px solid var(--border); border-radius:10px }
.legend { display:flex; gap:16px; font-size:12px; color:var(--ink2);
  margin:8px 0 } .legend .sw { display:inline-block; width:10px; height:10px;
  border-radius:3px; margin-right:5px; vertical-align:-1px }
.controls { display:flex; gap:14px; align-items:center; flex-wrap:wrap;
  font-size:12px; color:var(--ink2); margin:8px 0 }
.controls label { cursor:pointer }
.controls .sw { display:inline-block; width:10px; height:10px;
  border-radius:3px; margin:0 4px 0 2px; vertical-align:-1px }
@media (max-width:700px) {
  nav { flex-wrap:wrap; gap:8px 10px }
  nav .sp { display:none }
  nav form { flex-basis:100%; order:1 }
  nav input { width:100%; min-width:0 }
  main { overflow-wrap:anywhere }
  main form { display:flex; flex-wrap:wrap; gap:8px }
  main input, main select { min-width:0; max-width:100% }
  .stacked-table, .stacked-table tbody, .stacked-table tr,
  .stacked-table td { display:block }
  .stacked-table { border:0; background:none; overflow:visible }
  .stacked-table th { display:none }
  .stacked-table tr + tr { margin:10px 0; border:1px solid var(--border);
    border-radius:10px; background:var(--surface) }
  .stacked-table td { border:0; text-align:left }
  .stacked-table td::before { content:attr(data-label) ": ";
    color:var(--muted); font-size:12px; font-weight:600 }
  .stacked-table td[data-label="rule"]::before { display:block }
}
"""

JS = """
const t=document.createElement('div');t.className='bar-tip';document.body.appendChild(t);
document.addEventListener('mousemove',e=>{const b=e.target.closest('[data-tip]');
  if(b){t.textContent=b.dataset.tip;t.style.display='block';
    t.style.left=(e.clientX+14)+'px';t.style.top=(e.clientY-8)+'px';}
  else t.style.display='none';});
function themeToggle(){const r=document.documentElement;
  const cur=r.dataset.theme||(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  r.dataset.theme=cur==='dark'?'light':'dark';localStorage.theme=r.dataset.theme;}
if(localStorage.theme)document.documentElement.dataset.theme=localStorage.theme;
"""


def esc(s):
    return html.escape(str(s), quote=True)


def db():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)  # wait through an indexer commit
    conn.row_factory = sqlite3.Row
    return conn


def fmt_bytes(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}" if u != "B" else f"{n} B"
        n /= 1024


def page(title, body, active=""):
    tabs = [("Overview", "/"), ("Instincts", "/instincts"),
            ("Learnings", "/learnings"), ("Knowledge", "/knowledge"),
            ("Gists", "/gists"), ("Graph", "/graph"),
            ("Hindsight", "/hindsight"), ("Export", "/export")]
    nav = "".join(
        f'<a href="{h}" class="{"on" if n == active else ""}">{n}</a>'
        for n, h in tabs
    ) + ('<form action="/search" method="get" style="margin:0">'
         '<input name="q" placeholder="search…" size="18"></form>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · LogLearner</title><style>{CSS}</style></head>
<body><nav><span class="brand">LogLearner</span>{nav}<span class="sp"></span>
<button onclick="themeToggle()" aria-label="toggle theme">◐</button></nav>
<main>{body}</main><script>{JS}</script></body></html>"""


def tile(value, label):
    return f'<div class="tile"><div class="v">{value}</div><div class="l">{esc(label)}</div></div>'


def hbar_svg(pairs, unit=""):
    """Horizontal bars: single sequential hue, thin marks, rounded data-end,
    direct labels in ink tokens, hover tooltip via data-tip."""
    if not pairs:
        return '<span class="muted">none</span>'
    mx = max(v for _, v in pairs) or 1
    rows, y, W, LBL = [], 4, 460, 150
    for name, v in pairs:
        w = max(6, int((W - LBL - 60) * v / mx))
        rows.append(
            f'<text x="{LBL - 8}" y="{y + 13}" text-anchor="end" fill="var(--ink2)" font-size="12">{esc(str(name)[:22])}</text>'
            f'<path d="M{LBL} {y} h{w - 4} a4 4 0 0 1 4 4 v10 a4 4 0 0 1 -4 4 h-{w - 4} z"'
            f' fill="var(--seq)" data-tip="{esc(name)}: {v}{unit}"></path>'
            f'<text x="{LBL + w + 6}" y="{y + 13}" fill="var(--muted)" font-size="11">{v}</text>'
        )
        y += 24
    return (f'<svg role="img" aria-label="bar chart" width="{W}" height="{y}"'
            f' style="max-width:100%">{"".join(rows)}</svg>')


# ------------------------------------------------------------------- handlers
def h_overview(qs):
    c = db()
    tot = c.execute(
        "SELECT COUNT(*) n, SUM(size_bytes) b, SUM(tool_calls) t,"
        " SUM(user_corrections) uc, SUM(tool_errors) te FROM sessions"
    ).fetchone()
    prim = c.execute(
        "SELECT COUNT(*) FROM sessions WHERE classification='primary'"
    ).fetchone()[0]
    n_inst = c.execute("SELECT COUNT(*) FROM instincts").fetchone()[0]
    last = dict(c.execute("SELECT * FROM meta")).get("last_index", "?")
    tiles = "".join([
        tile(f"{(tot['b'] or 0) / 2**30:.1f} GB", "corpus"),
        tile(tot["n"] or 0, "sessions"), tile(prim, "primary"),
        tile(f"{(tot['t'] or 0):,}", "tool calls"),
        tile(tot["uc"] or 0, "user corrections"),
        tile(tot["te"] or 0, "tool errors"), tile(n_inst, "instincts"),
    ])
    rows = c.execute(
        "SELECT project, COUNT(*) n,"
        " SUM(classification='primary') p, SUM(size_bytes) b,"
        " SUM(tool_errors) te, SUM(user_corrections) uc, MAX(last_ts) ts"
        " FROM sessions GROUP BY project"
        " ORDER BY (SUM(classification='primary') > 0) DESC, b DESC"
    ).fetchall()
    show_all = qs.get("all", [""])[0] == "1"
    main_rows = [r for r in rows if r["p"] or (r["b"] or 0) >= 2**20]
    hidden = len(rows) - len(main_rows)
    if show_all:
        main_rows = rows
    trs = "".join(
        f'<tr><td><a href="/project/{esc(r["project"])}">{esc(r["project"])}</a></td>'
        f'<td class="num">{r["p"]}/{r["n"]}</td><td class="num">{fmt_bytes(r["b"] or 0)}</td>'
        f'<td class="num">{r["uc"] or 0}</td><td class="num">{r["te"] or 0}</td>'
        f'<td class="muted">{esc((r["ts"] or "")[:10])}</td></tr>'
        for r in main_rows
    )
    fold = ("" if show_all or not hidden else
            f'<p class="muted">{hidden} projects with no primary sessions and '
            f'&lt;1 MB hidden — <a href="/?all=1">show all</a></p>')
    body = (f"<h1>Session corpus</h1><div class='tiles'>{tiles}</div>"
            f"<div class='muted'>indexed: {esc(last)} · read-only · 127.0.0.1</div>"
            f"<h2>Projects</h2><table><tr><th>project</th><th class='num'>primary/all</th>"
            f"<th class='num'>size</th><th class='num'>corrections</th>"
            f"<th class='num'>tool errors</th><th>last activity</th></tr>{trs}</table>{fold}")
    return page("Overview", body, "Overview")


# Claude Code names a project directory after its path with every character that is not a letter or digit turned into
# "-" (-home-<user>-<path>; a user name first.last reads first-last). Pages show the name without the owner's home: the home of
# the user running the dashboard, derived here with the same rule, or any home-directory prefix when the index was
# built on another host (there a dotted user name cannot be told from the path that follows, so its tail may show).
# Never a hard-coded user (0.1.9 shipped one).
_ENC_HOME = "-" + re.sub(r"[^A-Za-z0-9]", "-", str(Path.home()).strip("/")) + "-"
_ENC_HOME_RE = re.compile(r"^-?(?:home|Users)-[^-]+-")


def short_project(name):
    s = str(name or "")
    if len(_ENC_HOME) > 2 and s.startswith(_ENC_HOME):
        return s[len(_ENC_HOME):] or s
    return (_ENC_HOME_RE.sub("", s, count=1) or s).lstrip("-")


def h_project(qs, project):
    c = db()
    cls = qs.get("class", ["primary"])[0]
    where, args = "project=?", [project]
    if cls != "all":
        where += " AND classification=?"
        args.append(cls)
    rows = c.execute(
        f"SELECT * FROM sessions WHERE {where} ORDER BY last_ts DESC", args
    ).fetchall()
    chips = " ".join(
        f'<a class="chip" href="?class={k}">{k}</a>'
        for k in ("primary", "all", "observer", "cron", "subagent")
    )
    cards = []
    for r in rows:
        tools = ", ".join(
            f"{n}×{c_}" for n, c_ in (json.loads(r["top_tools"]) or [])[:4]
        )
        warn = (f'<span class="chip crit">{r["user_corrections"]} corrections</span> '
                if r["user_corrections"] else "") + (
                f'<span class="chip warn2">{r["tool_errors"]} tool errors</span>'
                if r["tool_errors"] else "")
        cards.append(
            f'<div class="card"><a href="/session/{esc(project)}/{r["session_id"]}">'
            f'<b>{r["session_id"][:8]}</b></a> <span class="chip">{r["classification"]}</span>'
            f' <span class="muted">{esc((r["first_ts"] or "")[:16])} → {esc((r["last_ts"] or "")[:16])}'
            f' · {fmt_bytes(r["size_bytes"])} · {r["total_lines"]} lines · {r["tool_calls"]} tools</span>'
            f' {warn}<br><span class="ink2">{esc(r["intent"] or "")}</span>'
            f'<br><span class="muted">{esc(tools)}</span></div>'
        )
    body = (f'<h1>{esc(project)}</h1><div>{chips}</div>'
            + ("".join(cards) or "<p class='muted'>no sessions in this class</p>"))
    return page(project, body, "Overview")


def gap_ranges_for(c, session_id):
    """Gap evidence for a session — from the edges table, where instinct
    evidence was resolved to EXACT session ids at index time (this fixed
    the mutual-8-char-prefix join that could false-positive across 17k
    files). Falls back to the old prefix scan on a pre-edges index.db."""
    out = []
    try:
        rows = c.execute(
            "SELECT e.dst_id gap_id, i.rule FROM edges e"
            " JOIN edges e2 ON e2.src_kind='gap' AND e2.src_id=e.dst_id"
            "   AND e2.edge_kind='synthesized'"
            " JOIN instincts i ON CAST(i.id AS TEXT)=e2.dst_id"
            " WHERE e.src_kind='session' AND e.src_id=?"
            "   AND e.edge_kind='contains'", (session_id,)).fetchall()
        for r in rows:
            a, b = r["gap_id"].rsplit(":", 1)[1].split("-")
            out.append((int(a), int(b), r["rule"]))
        return out
    except (sqlite3.OperationalError, ValueError, IndexError):
        pass
    for r in c.execute("SELECT rule, evidence FROM instincts"):
        for ev in json.loads(r["evidence"] or "[]"):
            m = EVIDENCE_RE.search(ev or "")
            if m and session_id.startswith(m.group(1)[:8]) and m.group(1).startswith(session_id[:8]):
                out.append((int(m.group(2)), int(m.group(3)), r["rule"]))
    return out


def render_msg(i, raw, gapset):
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    typ = obj.get("type", "?")
    cls = "gapline " if i in gapset else ""
    out = []

    def block(who, body_html, kind):
        out.append(
            f'<div class="msg {kind} {cls}" id="L{i}"><span class="who">'
            f'<a class="muted" href="#L{i}">L{i}</a> {who}</span> {body_html}</div>'
        )

    def trunc(t, n=700):
        t = str(t)
        if len(t) <= n:
            return f"<pre>{esc(t)}</pre>"
        return (f"<pre>{esc(t[:n])}…</pre><details><summary>show all"
                f" ({len(t)} chars)</summary><pre>{esc(t[n:8000])}</pre></details>")

    content = (obj.get("message") or {}).get("content")
    if typ == "user":
        if isinstance(content, str):
            block("user", trunc(content), "user")
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    block("user", trunc(b.get("text", "")), "user")
                elif b.get("type") == "tool_result":
                    c_ = b.get("content")
                    txt = c_ if isinstance(c_, str) else json.dumps(c_)[:4000]
                    block("tool_result", trunc(txt, 400), "tool")
    elif typ == "assistant":
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                block("assistant", trunc(b.get("text", "")), "assistant")
            elif b.get("type") == "tool_use":
                inp = b.get("input") or {}
                brief = inp.get("command") or inp.get("file_path") or \
                    inp.get("pattern") or json.dumps(inp)[:200]
                block(f'tool_use · <b>{esc(b.get("name", "?"))}</b>',
                      trunc(brief, 300), "tool")
    else:
        # meta/system rows: one-line human summary, never a raw JSON dump
        if typ in ("file-history-snapshot", "queue-operation"):
            return ""  # pure bookkeeping — line numbers stay stable, row hidden
        salient = (
            obj.get("customTitle") or obj.get("agentName")
            or obj.get("permissionMode") or obj.get("prUrl")
            or (obj.get("lastPrompt") or "")[:100]
            or (obj.get("summary") or "")[:100]
        )
        if not salient:
            c_ = (obj.get("message") or {}).get("content")
            salient = c_[:100] if isinstance(c_, str) else ""
        block(esc(typ), f'<span class="muted">{esc(str(salient))}</span>', "sys")
    return "".join(out)


def timeline_svg(tl, project, sid):
    """Activity strip (60 buckets, sequential hue) + idle-gap markers (crit
    ticks, deep-linked). claude-session-analyzer layer 5 as one glance."""
    buckets = tl.get("buckets") or []
    if not buckets:
        return ""
    W, H, mx = 640, 38, max(max(buckets), 1)
    bw = W / len(buckets)
    bars = "".join(
        f'<rect x="{i * bw:.1f}" y="{H - 4 - (H - 10) * b / mx:.1f}"'
        f' width="{bw - 1.2:.1f}" height="{(H - 10) * b / mx + 1:.1f}"'
        f' fill="var(--seq)" opacity="0.85"><title>{b} msgs</title></rect>'
        for i, b in enumerate(buckets))
    idles = "".join(
        f'<a href="/session/{project}/{sid}?start={max(0, d["line"] - 20)}'
        f'&amp;limit=80#L{d["line"]}"><rect x="{d["frac"] * W - 1.5:.1f}" y="0"'
        f' width="3" height="{H}" fill="var(--crit)" opacity="0.85">'
        f'<title>{d["min"]} min idle → L{d["line"]}</title></rect></a>'
        for d in tl.get("idles", []))
    span = tl.get("span_min", 0)
    label = (f"{span // 1440}d {span % 1440 // 60}h" if span >= 1440
             else f"{span // 60}h {span % 60}m" if span >= 60 else f"{span}m")
    return (f'<div class="card"><b>timeline</b> <span class="muted">{label} · '
            f'{len(tl.get("idles", []))} idle gaps ≥5 min (red ticks link to '
            f'the silence)</span><br><svg width="{W}" height="{H}"'
            f' role="img" aria-label="activity timeline">{bars}{idles}</svg></div>')


def err_layers_html(el, project, sid):
    """claude-session-analyzer layers 1-4 as chips + sample drill-downs."""
    style = {"fatal": "crit", "operational": "warn2",
             "warnings": "", "tool_use": ""}
    chips, details = [], []
    for name in ("fatal", "operational", "warnings", "tool_use"):
        layer = el.get(name) or {}
        n = layer.get("n", 0)
        if not n:
            continue
        chips.append(f'<span class="chip {style[name]}">{n} {name}</span>')
        rows = "".join(
            f'<div><a href="/session/{project}/{sid}?start='
            f'{max(0, s["line"] - 15)}&amp;limit=60#L{s["line"]}">L{s["line"]}'
            f'</a> <span class="muted">{esc(s["text"])}</span></div>'
            for s in layer.get("samples", []))
        details.append(f"<details><summary>{name} samples</summary>{rows}</details>")
    if not chips:
        return ""
    return (f'<div class="card"><b>error layers</b> {" ".join(chips)}'
            + "".join(details) + "</div>")


def related_sessions_html(c, sid):
    """KR3.3: sessions like this one — precomputed state-vector similarity
    (E7), component chips explain WHY each is related."""
    try:
        rows = c.execute(
            "SELECT ss.b, ss.score, ss.components, s.project, s.intent"
            " FROM sessions_similarity ss JOIN sessions s ON s.session_id = ss.b"
            " WHERE ss.a = ? ORDER BY ss.score DESC LIMIT 6", (sid,)).fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    items = []
    for r in rows:
        comp = json.loads(r["components"] or "{}")
        chips = "".join(
            f'<span class="chip">{k} {v}</span> '
            for k, v in (("tools", comp.get("tools")),
                         ("files", comp.get("files")),
                         ("errors", comp.get("errors")),
                         ("bash", comp.get("shared_bash")))
            if v)
        items.append(
            f'<div><a href="/session/{esc(r["project"])}/{esc(r["b"])}">'
            f'{esc(short_project(r["project"]))}/{r["b"][:8]}</a>'
            f' <span class="chip">{r["score"]:.2f}</span> {chips}'
            f'<br><span class="muted">{esc((r["intent"] or "")[:110])}</span></div>')
    return (f'<div class="card"><b>related sessions</b> <span class="muted">'
            f'state-vector similarity, no LLM</span>{"".join(items)}</div>')


def spine_events_html(c, sid):
    """Cross-tool audit events referencing this session (the
    session-learning spine joined to the browser)."""
    try:
        rows = c.execute(
            "SELECT ts, source, event_type, evidence_ref"
            " FROM store_session_events WHERE session_id LIKE ?"
            " OR evidence_ref LIKE ? ORDER BY ts DESC LIMIT 8",
            (f"%{sid[:8]}%", f"%{sid[:8]}%")).fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    items = "".join(
        f'<div><span class="muted">{esc(r["ts"][:16])}</span> '
        f'<span class="chip">{esc(r["source"])}</span> {esc(r["event_type"])} '
        f'<span class="muted">{esc(r["evidence_ref"][:90])}</span></div>'
        for r in rows)
    return (f'<div class="card"><b>spine events</b> <span class="muted">'
            f'cross-tool audit trail (store session events)</span>{items}</div>')


ABSTRACT_MODEL = os.environ.get("LOGLEARNER_ABSTRACT_MODEL", "gemini-3.5-flash")


def _abstract_block(c, r, qs, tl, el):
    """Opt-in cached LLM abstract (KR2.3). Two explicit gates: the server env
    LOGLEARNER_LLM_ABSTRACT=1 (budget flag) AND ?abstract=1 per session.
    Input digest is capped (<6KB ≈ 1.5K tokens) so a mini-tier call stays
    well under $0.01; result cached in index.db keyed by (path, mtime)."""
    if os.environ.get("LOGLEARNER_LLM_ABSTRACT") != "1":
        return ('<p class="muted">LLM abstract: off (start server with '
                'LOGLEARNER_LLM_ABSTRACT=1 to enable; ≤$0.01/session, cached)</p>')
    rw = sqlite3.connect(DB_PATH, timeout=30)  # wait for the indexer instead of failing at 5 s
    rw.execute("CREATE TABLE IF NOT EXISTS abstracts"
               " (path TEXT PRIMARY KEY, mtime REAL, model TEXT, text TEXT)")
    row = rw.execute("SELECT mtime, model, text FROM abstracts WHERE path=?",
                     (r["path"],)).fetchone()
    if row and row[0] == r["mtime"]:
        rw.close()
        return (f'<div class="card"><b>abstract</b> <span class="chip">'
                f'{esc(row[1])} · cached</span><br>{esc(row[2])}</div>')
    if qs.get("abstract", ["0"])[0] != "1":
        rw.close()
        return ('<p class="muted"><a href="?abstract=1">generate LLM abstract'
                '</a> (one mini-tier call, cached until the session changes)</p>')
    layer_counts = {k: (el.get(k) or {}).get("n", 0) for k in
                    ("fatal", "operational", "warnings", "tool_use")}
    digest = (
        f"intent: {r['intent']}\ntools: {r['top_tools']}\n"
        f"messages: {r['user_messages']} user / {r['assistant_messages']} asst, "
        f"{r['tool_calls']} tool calls, {r['user_corrections']} corrections\n"
        f"error layers: {layer_counts}\n"
        f"span_min: {tl.get('span_min')}, idle gaps: {len(tl.get('idles', []))}\n"
        f"error samples: " + " | ".join(
            s["text"][:120] for k in el.values() for s in (k.get("samples") or [])[:2]))
    prompt = ("Summarize this Claude Code session in <=4 sentences: what was "
              "attempted, what broke, how it ended. Be concrete.\n\n" + digest[:6000])
    try:
        from loglearner.example_synthesizer import _query_model
    except ImportError:  # minimal runtime without the synthesis layer
        rw.close()
        return '<p class="muted">abstract generation unavailable in this build</p>'
    text = (_query_model(ABSTRACT_MODEL, prompt, timeout=60) or "").strip()
    if not text:
        rw.close()
        return '<p class="muted">abstract generation failed (model unavailable)</p>'
    rw.execute("REPLACE INTO abstracts VALUES (?,?,?,?)",
               (r["path"], r["mtime"], ABSTRACT_MODEL, text[:1500]))
    rw.commit()
    rw.close()
    return (f'<div class="card"><b>abstract</b> <span class="chip">'
            f'{esc(ABSTRACT_MODEL)}</span><br>{esc(text[:1500])}</div>')


def h_session(qs, project, sid):
    if not UUID_RE.match(sid):
        return page("bad id", "<p>invalid session id</p>")
    c = db()
    r = c.execute(
        "SELECT * FROM sessions WHERE project=? AND session_id=?", (project, sid)
    ).fetchone()
    if not r:
        return page("not found", "<p>session not in index</p>")
    start = max(0, int(qs.get("start", ["0"])[0]))
    limit = min(500, int(qs.get("limit", ["200"])[0]))

    gaps = gap_ranges_for(c, sid)
    gapset = set()
    for a, b_, _ in gaps:
        gapset.update(range(a, b_ + 1))
    gap_html = "".join(
        f'<div class="gapmark"><b>gap evidence</b> lines '
        f'<a href="?start={max(0, a - 5)}#L{a}">{a}–{b_}</a> — {esc(rule[:220])}</div>'
        for a, b_, rule in gaps
    )

    # Byte-offset seek table (KR1.4): jump to the checkpoint at/below `start`
    # instead of decoding every prior line. Sessions without seek rows fall
    # back to a plain skip from line 1.
    try:
        srow = c.execute(
            "SELECT line, off FROM seek WHERE path=? AND line<=?"
            " ORDER BY line DESC LIMIT 1", (r["path"], max(start, 1))).fetchone()
    except sqlite3.OperationalError:  # pre-seek-table index.db
        srow = None
    seek_line, seek_off = (srow["line"], srow["off"]) if srow else (1, 0)

    msgs, n = [], 0
    with open(r["path"], "rb") as f:
        if seek_off:
            f.seek(seek_off)
        for i, raw in enumerate(f, seek_line):
            if i < start:
                continue
            if n >= limit:
                break
            h = render_msg(i, raw.decode("utf-8", "replace"), gapset)
            if h:
                msgs.append(h)
                n += 1
    tools = json.loads(r["top_tools"] or "[]")
    warn = (f' <span class="chip crit">{r["user_corrections"]} corrections</span>'
            if r["user_corrections"] else "") + (
            f' <span class="chip warn2">{r["tool_errors"]} tool errors</span>'
            if r["tool_errors"] else "")
    summary = (
        f'<div class="card"><b>{sid[:8]}</b> <span class="chip">{r["classification"]}</span>'
        f'{warn}<br><span class="ink2">{esc(r["intent"] or "")}</span>'
        f'<br><span class="muted">{esc(r["first_ts"] or "")} → {esc(r["last_ts"] or "")} · '
        f'{fmt_bytes(r["size_bytes"])} · {r["total_lines"]} lines · '
        f'{r["user_messages"]} user / {r["assistant_messages"]} assistant msgs</span>'
        f'<h2>Tool usage</h2>{hbar_svg([(t, k) for t, k in tools])}</div>'
    )
    try:
        ex = c.execute("SELECT timeline, err_layers FROM session_extras"
                       " WHERE path=?", (r["path"],)).fetchone()
    except sqlite3.OperationalError:  # pre-extras index.db
        ex = None
    tl = json.loads(ex["timeline"]) if ex and ex["timeline"] else {}
    el = json.loads(ex["err_layers"]) if ex and ex["err_layers"] else {}
    summary += (timeline_svg(tl, esc(project), esc(sid))
                + err_layers_html(el, esc(project), esc(sid))
                + related_sessions_html(c, sid)
                + spine_events_html(c, sid)
                + _abstract_block(c, r, qs, tl, el))
    nxt = start + limit
    pager = (
        f'<div class="pager">'
        + (f'<a href="?start={max(0, start - limit)}">← prev</a>' if start else "")
        + (f'<a href="?start={nxt}">next {limit} →</a>'
           if nxt < (r["total_lines"] or 0) else "")
        + f' <span class="muted">lines {start or 1}–{min(nxt, r["total_lines"])} of {r["total_lines"]}</span></div>'
    )
    body = (f'<h1><a href="/project/{esc(project)}">{esc(project)}</a> / {sid[:8]}</h1>'
            f'{summary}{gap_html}{pager}{"".join(msgs)}{pager}')
    return page(f"session {sid[:8]}", body, "Overview")


def h_instincts(qs):
    c = db()
    cat = qs.get("cat", [""])[0]
    q = qs.get("q", [""])[0]
    minc = int(qs.get("minc", ["0"])[0])
    where, args = "confidence>=?", [minc]
    if cat:
        where += " AND category=?"
        args.append(cat)
    if q:
        where += " AND rule LIKE ?"
        args.append(f"%{q}%")
    rows = c.execute(
        f"SELECT * FROM instincts WHERE {where}"
        " ORDER BY confidence DESC, merge_count DESC LIMIT 400", args
    ).fetchall()
    cats = [r[0] for r in c.execute(
        "SELECT DISTINCT category FROM instincts ORDER BY 1")]
    opts = '<option value="">all categories</option>' + "".join(
        f'<option {"selected" if x == cat else ""}>{esc(x)}</option>' for x in cats if x
    )
    filt = (f'<form method="get"><input name="q" value="{esc(q)}" placeholder="search rules"> '
            f'<select name="cat" onchange="this.form.submit()">{opts}</select> '
            f'<select name="minc" onchange="this.form.submit()">'
            + "".join(f'<option value="{i}" {"selected" if i == minc else ""}>conf ≥ {i}</option>'
                      for i in (0, 5, 8, 10))
            + '</select> <button>filter</button></form>')
    trs = []
    for r in rows:
        evs = []
        for ev in json.loads(r["evidence"] or "[]")[:4]:
            m = EVIDENCE_RE.search(ev or "")
            if m:
                sid, a = m.group(1), int(m.group(2))
                c2 = c.execute(
                    "SELECT project, session_id FROM sessions WHERE session_id LIKE ?",
                    (sid[:8] + "%",),
                ).fetchone()
                if c2:
                    evs.append(f'<a href="/session/{esc(c2["project"])}/{c2["session_id"]}'
                               f'?start={max(0, a - 5)}#L{a}">{sid[:8]}:{a}</a>')
                else:
                    evs.append(f'<span class="muted">{esc(sid[:8])}:{a}</span>')
        trs.append(
            f'<tr><td data-label="rule">{esc(r["rule"])}<br><span class="muted">{esc(r["source_file"])}</span></td>'
            f'<td data-label="conf" class="num">{r["confidence"]}</td>'
            f'<td data-label="category"><span class="chip">{esc(r["category"] or "—")}</span></td>'
            f'<td data-label="merged" class="num">{r["merge_count"]}</td>'
            f'<td data-label="evidence">{" ".join(evs) or "—"}</td></tr>'
        )
    src = dict(c.execute("SELECT * FROM meta")).get("instincts_source", "")
    note = ("<p class='muted'>Showing the installed knowledge banks: no synthesized instinct library is present "
            "in this runtime, so the banks are the library (bank/<file> in the source column).</p>" if src == "banks"
            else "<p class='muted'>No instinct library and no banks indexed yet; ingest a pack and re-run the indexer.</p>"
            if src == "none" else "")
    body = (f"<h1>Instinct library</h1>{filt}{note}<p class='muted'>{len(rows)} shown</p>"
            f"<table class='stacked-table'><tr><th>rule</th><th class='num'>conf</th><th>category</th>"
            f"<th class='num'>merged</th><th>evidence</th></tr>{''.join(trs)}</table>")
    return page("Instincts", body, "Instincts")


def lifecycle_board_html(c):
    """Instinct lifecycle board (KR4.1): real counts per state across the
    three stores that exist — the reference store's mint queue, fleet learnings verdicts,
    loglearner's own arm lane. Columns link to the store that holds them."""
    cols = []
    try:
        n_pend = c.execute("SELECT COUNT(*) FROM store_instincts"
                           " WHERE lane='pending'").fetchone()[0]
        cols.append(("pending (mint queue)", n_pend, "/knowledge?lane=pending"))
    except sqlite3.OperationalError:
        pass
    for label, status in (("KEEP / positive", "positive"),
                          ("REVERT / negative", "negative")):
        n = c.execute("SELECT COUNT(*) FROM learnings WHERE status=?",
                      (status,)).fetchone()[0]
        cols.append((label, n, "/learnings"))
    n_pushed = c.execute(
        "SELECT COUNT(*) FROM learnings WHERE arm != ''").fetchone()[0]
    cols.append(("pushed (arm-tracked)", n_pushed, "/learnings"))
    arms_file = UCB_DIR / "loglearner_arms.jsonl"       # honours LOGLEARNER_UCB_DIR (was a hard-coded path)
    rewards_file = UCB_DIR / "loglearner_rewards.jsonl"
    n_mint = sum(1 for _ in arms_file.open()) if arms_file.is_file() else 0
    n_meas = sum(1 for _ in rewards_file.open()) if rewards_file.is_file() else 0
    cols.append(("loglearner minted", n_mint, "/learnings"))
    cols.append(("measured (rewards)", n_meas, "/learnings"))
    cells = "".join(
        f'<a class="tile" href="{href}" style="text-decoration:none">'
        f'<div class="v">{n}</div><div class="l">{esc(label)}</div></a>'
        for label, n, href in cols)
    return (f"<h2>Lifecycle board</h2><p class='muted'>pending → KEEP/REVERT →"
            f" pushed → measured; counts are live store totals</p>"
            f"<div class='tiles'>{cells}</div>")


def measured_rewards_html():
    """Real bandit outcomes from the reward ledger under LOGLEARNER_UCB_DIR (exposure-gated, see arms.measure_arms)."""
    path = UCB_DIR / "loglearner_rewards.jsonl"
    rows = _jsonl_rows(path) if path.is_file() else []
    if not rows:
        return (f"<h2>Measured rewards (ledger)</h2><p class='muted'>none yet — {esc(str(path))} is "
                f"{'empty' if path.is_file() else 'absent'}; rewards appear only after an exposure→outcome join</p>")
    agg = {}
    for r in rows:
        a = str(r.get("arm_id") or r.get("arm") or "")
        if not a:
            continue
        pulls, wins = agg.get(a, (0, 0.0))
        agg[a] = (pulls + 1, wins + float(r.get("reward") or 0))
    arms = sorted(({"arm": a, "pulls": p, "avg_r": (w / p) if p else None, "wins": w} for a, (p, w) in agg.items()),
                  key=lambda d: -d["pulls"])[:30]
    tr = "".join(f'<tr><td>{esc(r["arm"][:60])}</td><td class="num">{r["pulls"]}</td>'
                 f'<td class="num">{r["wins"]:.0f}</td><td class="num">{r["avg_r"]:.2f}</td></tr>' for r in arms)
    return (f"<h2>Measured rewards (ledger)</h2><p class='muted'>{len(rows)} observations from {esc(str(path))}</p>"
            + arm_charts_html(arms)
            + f"<table><tr><th>arm</th><th class='num'>pulls</th><th class='num'>reward Σ</th>"
            f"<th class='num'>reward ⌀</th></tr>{tr}</table>")


def arm_charts_html(arms):
    """Pulls + mean-reward bars for the top arms (KR4.1 charts)."""
    if not arms:
        return ""
    top = arms[:14]
    pulls = [(r["arm"][:26], r["pulls"]) for r in top]
    rewards = [(r["arm"][:26], round(r["avg_r"], 2))
               for r in top if r["avg_r"] is not None]
    html_ = (f"<div class='card'><b>pulls</b> (top {len(pulls)})<br>"
             f"{hbar_svg(pulls)}</div>")
    if rewards:
        html_ += (f"<div class='card'><b>mean reward</b> "
                  f"<span class='muted'>regret ⌀ = 1 − reward ⌀</span><br>"
                  f"{hbar_svg(rewards)}</div>")
    return html_


def eval_trend_html():
    """Retrieval-eval trend (KR4.3): router category-match rate per release
    from output/backtest-history/**/metrics.json, Tier-D 0.52 baseline."""
    pts = []
    for f in sorted((REPO / "output" / "backtest-history").rglob("metrics.json")):
        try:
            m = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        rates = m.get("category_match_rate") or {}
        score = rates.get("router") or (max(rates.values()) if rates else None)
        if score is None:
            continue
        rel = f.parent.relative_to(REPO / "output" / "backtest-history")
        pts.append((m.get("timestamp", ""), str(rel.parts[0]), round(score, 3)))
    if not pts:
        return ""
    pts.sort()
    seen, series = set(), []
    for ts, rel, score in pts:  # newest metrics.json per release dir
        series = [s for s in series if s[0] != rel]
        series.append((rel, score, ts[:10]))
    rows = "".join(
        f"<tr><td>{esc(rel)}</td><td class='muted'>{esc(day)}</td>"
        f"<td class='num'>{score:.3f}</td>"
        f"<td>{'▲' if score >= 0.52 else '▽'}</td></tr>"
        for rel, score, day in series)
    best = max(s[1] for s in series)
    return (f"<h2>Retrieval-eval trend</h2><p class='muted'>router "
            f"category-match rate per release · Tier-D baseline 0.52 · "
            f"best {best:.3f}</p><table><tr><th>release</th><th>date</th>"
            f"<th class='num'>match rate</th><th>vs 0.52</th></tr>{rows}</table>")


def h_learnings(qs):
    c = db()
    tot = c.execute(
        "SELECT COUNT(*) n, SUM(status='positive') p, SUM(status='negative') neg,"
        " SUM(arm != '') m, COUNT(DISTINCT file) f FROM learnings"
    ).fetchone()
    if not tot["n"]:
        return page("Learnings", "<h1>Learnings</h1><p class='muted'>no learnings bank "
                    "found (set LOGLEARNER_BANKS_DIR and re-index)</p>",
                    "Learnings")
    tiles = "".join([
        tile(tot["n"], "learnings"), tile(tot["f"], "files"),
        tile(tot["p"] or 0, "positive"), tile(tot["neg"] or 0, "negative"),
        tile(tot["m"] or 0, "with UCB arm"),
    ])
    files = c.execute(
        "SELECT file, COUNT(*) n, SUM(status='positive') p,"
        " SUM(status='negative') neg FROM learnings GROUP BY file ORDER BY n DESC"
    ).fetchall()
    ftr = "".join(
        f'<tr><td>{esc(r["file"])}</td><td class="num">{r["n"]}</td>'
        f'<td class="num">{r["p"] or 0}</td><td class="num">{r["neg"] or 0}</td></tr>'
        for r in files
    )
    arms = c.execute(
        "SELECT arm, COUNT(*) pulls, AVG(reward) avg_r, SUM(reward) wins"
        " FROM learnings WHERE arm != '' GROUP BY arm ORDER BY pulls DESC LIMIT 30"
    ).fetchall()
    atr = "".join(
        f'<tr><td>{esc(r["arm"][:60])}</td><td class="num">{r["pulls"]}</td>'
        f'<td class="num">{r["wins"] if r["wins"] is not None else "—"}</td>'
        f'<td class="num">{f"{r[2]:.2f}" if r["avg_r"] is not None else "—"}</td></tr>'
        for r in arms
    )
    q = qs.get("q", [""])[0]
    rows = c.execute(
        "SELECT * FROM learnings WHERE rule LIKE ? ORDER BY file, lid LIMIT 200",
        (f"%{q}%",),
    ).fetchall() if q else c.execute(
        "SELECT * FROM learnings WHERE rule != '' ORDER BY file, lid LIMIT 50"
    ).fetchall()
    rtr = "".join(
        f'<tr><td data-label="rule">{esc(r["rule"])}<br><span class="muted">{esc(r["file"])}:{esc(r["lid"])}'
        f' · {esc(r["source"])}</span></td>'
        f'<td data-label="status"><span class="chip {"crit" if r["status"] == "negative" else ""}">'
        f'{esc(r["status"] or "—")}</span></td><td data-label="conf">{esc(r["confidence"])}</td></tr>'
        for r in rows
    )
    body = (
        f"<h1>Learnings</h1><div class='tiles'>{tiles}</div>"
        + lifecycle_board_html(c)
        + f"<h2>Files</h2><table><tr><th>file</th><th class='num'>entries</th>"
        f"<th class='num'>positive</th><th class='num'>negative</th></tr>{ftr}</table>"
        f"<h2>Bank arms — status polarity</h2><p class='muted'>meta.reward on a bank record is its status "
        f"polarity (1 positive / 0 negative), not a measured outcome; measured rewards come from the ledger below</p>"
        f"<table><tr><th>arm</th><th class='num'>records</th>"
        f"<th class='num'>positive Σ</th><th class='num'>positive share</th></tr>"
        f"{atr or '<tr><td colspan=4 class=muted>no arm metadata</td></tr>'}</table>"
        + measured_rewards_html()
        + eval_trend_html()
        + f'<h2>Rules</h2><form method="get"><input name="q" value="{esc(q)}"'
        f' placeholder="filter rules" size="36"> <button>filter</button></form>'
        + (("" if q else "<p class='muted'>first 50 — use the filter to narrow</p>")
           + f"<table class='stacked-table'><tr><th>rule</th><th>status</th><th>conf</th></tr>{rtr}</table>"
           if rows else "<p class='muted'>no rules match</p>")
    )
    return page("Learnings", body, "Learnings")


def knowledge_store_detail(c, qs, stores):
    """Detail views: ?store=<name> renders sqlite tables, the
    session-event spine, or agent_behavior paired examples. None = no detail
    requested."""
    name = qs.get("store", [""])[0]
    if not name:
        return None
    row = next((r for r in stores if r["store"] == name), None)
    if not row:
        return page("Knowledge", "<p>unknown store</p>", "Knowledge")
    back = '<p><a href="/knowledge">← all stores</a></p>'

    if row["kind"] == "sqlite":
        sects = []
        try:
            sq = sqlite3.connect(f'file:{row["path"]}?mode=ro', uri=True)
            sq.row_factory = sqlite3.Row
            for (t,) in sq.execute("SELECT name FROM sqlite_master"
                                   " WHERE type='table'").fetchall():
                rows = sq.execute(f'SELECT * FROM "{t}" LIMIT 8').fetchall()
                n = sq.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                if not rows:
                    sects.append(f"<h2>{esc(t)} — 0 rows</h2>")
                    continue
                cols = rows[0].keys()[:6]
                head = "".join(f"<th>{esc(k)}</th>" for k in cols)
                trs = "".join(
                    "<tr>" + "".join(
                        f"<td>{esc(str(r[k])[:120])}</td>" for k in cols)
                    + "</tr>" for r in rows)
                sects.append(f"<h2>{esc(t)} — {n:,} rows (first 8)</h2>"
                             f"<table><tr>{head}</tr>{trs}</table>")
            sq.close()
        except sqlite3.Error as e:
            sects = [f"<p class='crit'>sqlite error: {esc(e)}</p>"]
        return page(name, f"<h1>{esc(name)}</h1>{back}" + "".join(sects),
                    "Knowledge")

    if row["kind"] == "spine":
        src = qs.get("source", [""])[0]
        where, args = "1=1", []
        if src:
            where, args = "source=?", [src]
        srcs = c.execute("SELECT source, COUNT(*) n FROM store_session_events"
                         " GROUP BY source ORDER BY n DESC").fetchall()
        chips = " ".join(
            f'<a class="chip" href="?store={esc(name)}&source={esc(r["source"])}">'
            f'{esc(r["source"])} {r["n"]:,}</a>' for r in srcs)
        rows = c.execute(
            f"SELECT * FROM store_session_events WHERE {where}"
            " ORDER BY ts DESC LIMIT 60", args).fetchall()
        trs = "".join(
            f'<tr><td class="muted">{esc(r["ts"][:16])}</td>'
            f'<td><span class="chip">{esc(r["source"])}</span></td>'
            f'<td>{esc(r["event_type"])}</td>'
            f'<td class="muted">{esc(r["session_id"][:40])}</td>'
            f'<td class="muted">{esc(r["evidence_ref"][:80])}</td></tr>'
            for r in rows)
        return page(name, f"<h1>session-learning spine</h1>{back}"
                    f"<p class='ink2'>5,574 cross-tool audit events — the "
                    f"ready-made Session↔Session edge source (E7).</p>"
                    f"<p>{chips}</p><table><tr><th>ts</th><th>source</th>"
                    f"<th>event</th><th>session</th><th>evidence</th></tr>"
                    f"{trs}</table>", "Knowledge")

    if name == "agent_behavior/examples":
        cards = []
        try:
            with open(row["path"], errors="replace") as f:
                for i, line in enumerate(f):
                    if len(cards) >= 20:
                        break
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    neg, pos = d.get("negative") or {}, d.get("positive") or {}
                    cards.append(
                        f'<div class="card"><span class="chip">'
                        f'{esc(d.get("category", "?"))}</span> '
                        f'<span class="chip warn2">sev {esc(d.get("severity", "?"))}</span>'
                        f'<br><b>{esc(d.get("situation", "")[:200])}</b>'
                        f'<br><span class="chip crit">wrong</span> '
                        f'{esc((neg.get("action") or "")[:220])}'
                        f'<br><span class="chip">right</span> '
                        f'{esc((pos.get("action") or "")[:220])}'
                        f'<br><span class="muted">rule: '
                        f'{esc(d.get("instinct", "")[:200])} · session '
                        f'{esc(str(d.get("session", ""))[:12])}</span></div>')
        except OSError:
            pass
        return page(name, f"<h1>agent_behavior paired examples</h1>{back}"
                    f"<p class='ink2'>The store's examples in LogLearner's own "
                    f"paired schema — rendered exactly like our gap examples "
                    f"(first 20 of {row['records']}).</p>" + "".join(cards),
                    "Knowledge")
    return page(name, f"<h1>{esc(name)}</h1>{back}<p class='muted'>no "
                "renderer for this kind</p>", "Knowledge")


def knowledge_coverage_html(c):
    """Mining-catalog coverage: GREEN/YELLOW/RED per source."""
    try:
        rows = c.execute("SELECT item, rating, section FROM store_coverage"
                         " ORDER BY CASE rating WHEN 'RED' THEN 0"
                         " WHEN 'YELLOW' THEN 1 ELSE 2 END").fetchall()
    except sqlite3.OperationalError:
        return ""
    if not rows:
        return ""
    counts = {}
    for r in rows:
        counts[r["rating"]] = counts.get(r["rating"], 0) + 1
    cls = {"GREEN": "", "YELLOW": "warn2", "RED": "crit"}
    chips = " ".join(f'<span class="chip {cls[k]}">{k} {v}</span>'
                     for k, v in sorted(counts.items()))
    trs = "".join(
        f'<tr><td><span class="chip {cls[r["rating"]]}">{r["rating"]}</span></td>'
        f'<td>{esc(r["item"])}</td></tr>' for r in rows)
    return (f"<h2>Mining coverage (knowledge_base/mining_catalog.md)</h2>"
            f"<p class='muted'>{chips} — RED = raw data present, no mining "
            f"tool yet</p><details><summary>all {len(rows)} sources</summary>"
            f"<table><tr><th>rating</th><th>source</th></tr>{trs}</table>"
            f"</details>")


def knowledge_measurement_html(stores):
    """Measurement lane: drift index rendered, raw_measurements
    aggregate-only by design (133k rows never fully rendered)."""
    drift = next((r for r in stores
                  if r["store"].endswith("drift_index")), None)
    raw = next((r for r in stores
                if r["store"].endswith("raw_measurements")), None)
    if not (drift or raw):
        return ""
    parts = ["<h2>Measurement lane</h2>"]
    if drift:
        tops = json.loads(drift["sample_keys"] or "[]")
        lis = "".join(f"<li><code>{esc(t)}</code></li>" for t in tops)
        parts.append(f"<div class='card'><b>drift_index</b> "
                     f"{drift['records']} reconciled experiment keys — top "
                     f"divergences:<ul>{lis}</ul></div>")
    if raw:
        units = ", ".join(str(x) for x in json.loads(raw["sample_keys"] or "[]"))
        parts.append(f"<div class='card'><b>raw_measurements</b> "
                     f"{raw['records']:,} rows <span class='chip warn2'>"
                     f"aggregated only</span><br><span class='muted'>by unit: "
                     f"{esc(units)}</span></div>")
    return "".join(parts)


def knowledge_groups_html(qs):
    """'Knowledge by class': records grouped by their class (category), read live from LOGLEARNER_BANKS_DIR, with a count
    per group, the banks that hold it, a class filter (?cls=) and, when a class is selected, that class's records with an
    honest 'showing N of M' line. Separate from banks_overview_html so the two sections evolve independently."""
    from collections import Counter, defaultdict
    files = sorted(p for p in BANKS_DIR.rglob("*.jsonl") if p.is_file() and ".bak" not in p.name.lower()) \
        if BANKS_DIR.is_dir() else []
    rows = []
    for f in files:
        rel = str(f.relative_to(BANKS_DIR)) if BANKS_DIR in f.parents else f.name
        rows.extend((rel, r) for r in _jsonl_rows(f) if str(r.get("rule") or "").strip())
    if not rows:
        return ""
    cls_of = lambda r: str(r.get("category") or "").strip() or "(uncategorized)"   # noqa: E731
    groups = defaultdict(list)
    for rel, r in rows:
        groups[cls_of(r)].append((rel, r))
    selected = qs.get("cls", [""])[0].strip()
    opts = "".join(f'<option value="{esc(c)}" {"selected" if c == selected else ""}>{esc(c)} ({len(g)})</option>'
                   for c, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])))
    form = (f'<form method="get" class="controls"><label>class <select name="cls" onchange="this.form.submit()">'
            f'<option value="">all classes ({len(rows)} records)</option>{opts}</select></label> <button>show</button>'
            + (f' <a href="/knowledge">clear</a>' if selected else "") + "</form>")
    trs = []
    for c, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        pos = sum(1 for _r, r in g if r.get("status") == "positive")
        neg = sum(1 for _r, r in g if r.get("status") == "negative")
        banks = Counter(rel for rel, _r in g)
        sample = str(g[-1][1].get("rule") or "")[:160]
        trs.append(f"<tr><td data-label='class'><a href='/knowledge?cls={esc(c)}'><b>{esc(c)}</b></a></td>"
                   f"<td data-label='records' class='num'>{len(g)}</td><td data-label='positive' class='num'>{pos}</td>"
                   f"<td data-label='negative' class='num'>{neg}</td>"
                   f"<td data-label='banks'>{esc(', '.join(f'{b} ×{n}' for b, n in banks.most_common(4)))}</td>"
                   f"<td data-label='sample rule'>{esc(sample)}</td></tr>")
    html_ = (f"<h2>Knowledge by class</h2><p class='muted'>{len(groups)} classes across {len(rows)} records in {len(files)} bank file(s); "
             f"a class is a record's category</p>{form}"
             f"<table class='stacked-table'><tr><th>class</th><th class='num'>records</th><th class='num'>positive</th>"
             f"<th class='num'>negative</th><th>banks</th><th>sample rule</th></tr>{''.join(trs)}</table>")
    if selected:
        g = groups.get(selected, [])
        shown = g[-50:][::-1]   # newest first
        rtr = "".join(f"<tr><td data-label='id'><code>{esc(str(r.get('id', '')))}</code></td>"
                      f"<td data-label='status'><span class='chip {'crit' if r.get('status') == 'negative' else ''}'>{esc(str(r.get('status') or '—'))}</span></td>"
                      f"<td data-label='rule'>{esc(str(r.get('rule') or ''))}</td>"
                      f"<td data-label='scope'>{esc(str(r.get('version_scope') or '—'))}</td>"
                      f"<td data-label='bank' class='muted'>{esc(rel)}</td></tr>" for rel, r in shown)
        html_ += (f"<h3>Records in class {esc(selected)}</h3><p class='muted'>showing {len(shown)} of {len(g)} in {esc(selected)}"
                  f"{' (newest first; narrow further with the search box)' if len(g) > len(shown) else ''}</p>"
                  + (f"<table class='stacked-table'><tr><th>id</th><th>status</th><th>rule</th><th>scope</th><th>bank</th></tr>{rtr}</table>"
                     if shown else "<p class='muted'>no records carry this class</p>"))
    return html_


def banks_overview_html():
    """Installed banks and the packs they came from, read live from LOGLEARNER_BANKS_DIR (no re-index needed).
    Returns (html, record_count). This is what a fresh install sees on /knowledge."""
    from collections import Counter
    files = sorted(p for p in BANKS_DIR.rglob("*.jsonl") if p.is_file() and ".bak" not in p.name.lower()) \
        if BANKS_DIR.is_dir() else []
    if not files:
        return (f"<h2>Installed banks</h2><p class='muted'>none under <code>{esc(str(BANKS_DIR))}</code> — ingest a pack: "
                "<code>python3 -m loglearner.cli ingest --pack &lt;pack.jsonl&gt; --bank $LOGLEARNER_BANKS_DIR/global/instincts.jsonl"
                " --name &lt;pack&gt; --version &lt;v1&gt;</code>, then this page and the Meta Learner tile count it.</p>"), 0
    brows, sample, installed, packs_all, total, pos_all, neg_all = [], [], [], Counter(), 0, 0, 0
    for f in files:
        rows = _jsonl_rows(f)
        total += len(rows)
        pos = sum(1 for r in rows if r.get("status") == "positive")
        neg = sum(1 for r in rows if r.get("status") == "negative")
        pos_all, neg_all = pos_all + pos, neg_all + neg
        life = Counter(str(r.get("lifecycle_state") or "screened(legacy)") for r in rows)
        packs = Counter(f"{(r.get('pack') or {}).get('name')}@{(r.get('pack') or {}).get('version')}"
                        for r in rows if isinstance(r.get("pack"), dict))
        packs_all.update(packs)
        rel = f.relative_to(BANKS_DIR) if BANKS_DIR in f.parents else f
        brows.append(f"<tr><td data-label='bank'><code>{esc(str(rel))}</code></td>"
                     f"<td data-label='records' class='num'>{len(rows)}</td>"
                     f"<td data-label='positive' class='num'>{pos}</td><td data-label='negative' class='num'>{neg}</td>"
                     f"<td data-label='lifecycle'>{esc(', '.join(f'{k} {v}' for k, v in life.most_common()))}</td>"
                     f"<td data-label='packs'>{esc(', '.join(f'{k} ×{v}' for k, v in packs.most_common(6))) or '—'}</td></tr>")
        sample.extend((str(rel), r) for r in rows[-4:])
        inst = f.parent / ".installed-packs.json"
        if inst.is_file():
            try:
                installed.extend(e for e in json.loads(inst.read_text()) if isinstance(e, dict))
            except (OSError, json.JSONDecodeError):
                pass
    tiles = "".join([tile(len(files), "bank files"), tile(total, "records"), tile(pos_all, "positive"),
                     tile(neg_all, "negative"), tile(len(packs_all), "packs")])
    prow = "".join(f"<tr><td data-label='pack'>{esc(str(e.get('name')))}</td>"
                   f"<td data-label='version'>{esc(str(e.get('version')))}</td>"
                   f"<td data-label='sha256'><code>{esc(str(e.get('sha256', ''))[:16])}</code></td>"
                   f"<td data-label='ingested'>{esc(str(e.get('at', '')))}</td>"
                   f"<td data-label='added' class='num'>{e.get('added', '—')}</td>"
                   f"<td data-label='updated' class='num'>{e.get('updated', '—')}</td>"
                   f"<td data-label='skipped' class='num'>{e.get('skipped', '—')}</td></tr>" for e in installed)
    srow = "".join(f"<tr><td data-label='id'><code>{esc(str(r.get('id', '')))}</code></td>"
                   f"<td data-label='status'><span class='chip {'crit' if r.get('status') == 'negative' else ''}'>{esc(str(r.get('status') or '—'))}</span></td>"
                   f"<td data-label='category'>{esc(str(r.get('category') or '—'))}</td>"
                   f"<td data-label='rule'>{esc(str(r.get('rule') or '')[:200])}</td>"
                   f"<td data-label='pack · bank' class='muted'>{esc((r.get('pack') or {}).get('name', '') if isinstance(r.get('pack'), dict) else '')}"
                   f" · {esc(rel)}</td></tr>" for rel, r in sample[-12:])
    html_ = (f"<h2>Installed banks</h2><p class='muted'>read live from <code>{esc(str(BANKS_DIR))}</code>; "
             f"the hook and <code>loglearner.cli recall</code> serve these records</p><div class='tiles'>{tiles}</div>"
             f"<table class='stacked-table'><tr><th>bank</th><th class='num'>records</th><th class='num'>positive</th><th class='num'>negative</th>"
             f"<th>lifecycle</th><th>packs (name@version ×records)</th></tr>{''.join(brows)}</table>"
             f"<h2>Installed packs</h2>"
             + (f"<table class='stacked-table'><tr><th>pack</th><th>version</th><th>sha256</th><th>ingested</th><th class='num'>added</th>"
                f"<th class='num'>updated</th><th class='num'>skipped</th></tr>{prow}</table>" if prow
                else "<p class='muted'>no pack manifest recorded (records were banked directly)</p>")
             + f"<h2>Sample rules</h2><p class='muted'>newest {min(len(sample), 12)} of {total}</p>"
             f"<table class='stacked-table'><tr><th>id</th><th>status</th><th>category</th><th>rule</th><th>pack · bank</th></tr>{srow}</table>")
    return html_, total


def h_knowledge(qs):
    """Knowledge base — the installed banks and packs (every install), plus every surface of the maintainers'
    reference store when its inventory was indexed (fleet hosts only)."""
    c = db()
    banks_html, _n_bank_rows = banks_overview_html()
    groups_html = knowledge_groups_html(qs)
    stores = c.execute(
        "SELECT * FROM store_inventory ORDER BY kind, records DESC").fetchall()
    if not stores:
        return page("Knowledge", "<h1>Knowledge base</h1><p class='ink2'>Installed knowledge banks and the packs "
                    "they came from. Bank a verified rule with <code>python3 -m loglearner.cli bank</code>.</p>"
                    + groups_html + banks_html, "Knowledge")
    n_inst = c.execute(
        "SELECT COUNT(*) FROM store_instincts WHERE lane='active'").fetchone()[0]
    n_pend = c.execute(
        "SELECT COUNT(*) FROM store_instincts WHERE lane='pending'").fetchone()[0]
    by_status = c.execute(
        "SELECT COALESCE(NULLIF(status,''),'(none)') s, COUNT(*) n"
        " FROM store_instincts WHERE lane='active' GROUP BY s ORDER BY n DESC"
    ).fetchall()
    n_jsonl = sum(r["records"] for r in stores if r["kind"] == "jsonl")
    tiles = "".join([
        tile(n_inst, "active instincts"),
        tile(f"{n_pend:,}", "pending (mint queue)"),
        tile(len([r for r in stores if r["kind"] == "jsonl"]), "JSONL stores"),
        tile(f"{n_jsonl:,}", "JSONL records"),
        tile(len([r for r in stores if r["kind"] == "dir"]), "knowledge dirs"),
    ])
    status_chart = hbar_svg([(r["s"], r["n"]) for r in by_status[:8]])

    q = qs.get("q", [""])[0]
    st = qs.get("status", [""])[0]
    lane = qs.get("lane", ["active"])[0]
    where, args = "rule != ''", []
    if lane in ("active", "pending"):
        where += " AND lane=?"
        args.append(lane)
    if q:
        where += " AND rule LIKE ?"
        args.append(f"%{q}%")
    if st:
        where += " AND status=?"
        args.append(st)
    rows = c.execute(
        f"SELECT * FROM store_instincts WHERE {where} LIMIT 200", args).fetchall()
    st_opts = '<option value="">any status</option>' + "".join(
        f'<option {"selected" if r["s"] == st else ""}>{esc(r["s"])}</option>'
        for r in by_status if r["s"] != "(none)")
    lane_opts = "".join(
        f'<option {"selected" if x == lane else ""}>{x}</option>'
        for x in ("active", "pending", "all"))
    filt = (f'<form method="get"><input name="q" value="{esc(q)}" '
            f'placeholder="search {n_inst + n_pend} rules" size="30"> '
            f'<select name="lane">{lane_opts}</select> '
            f'<select name="status">{st_opts}</select> <button>filter</button></form>')
    itr = "".join(
        f'<tr><td><b>{esc(r["id"])}</b> {esc(r["rule"])}<br>'
        f'<span class="muted">{esc(r["evidence"][:180])}</span></td>'
        f'<td><span class="chip">{esc(r["status"] or "—")}</span>'
        + (' <span class="chip warn2">pending</span>' if r["lane"] == "pending" else "")
        + f'</td><td class="num">{esc(r["confidence"])}</td></tr>'
        for r in rows[:200])

    def lane_chips(r):
        try:
            lanes = json.loads(r["lanes"] or "{}")
        except (json.JSONDecodeError, KeyError):
            lanes = {}
        out = []
        for k, v in lanes.items():
            cls = {"pending": "warn2", "staged": "warn2",
                   "lock": "crit"}.get(k, "")
            out.append(f'<span class="chip {cls}">{esc(k)} {esc(v)}</span>')
        return " ".join(out)

    _DETAIL_KINDS = ("sqlite", "spine")
    def store_cell(r):
        name = esc(r["store"])
        if r["kind"] in _DETAIL_KINDS or r["store"] == "agent_behavior/examples":
            return f'<a href="/knowledge?store={name}">{name}</a>'
        return name

    str_ = "".join(
        f'<tr><td>{store_cell(r)}</td><td><span class="chip">{esc(r["kind"])}</span> '
        f'{lane_chips(r)}</td><td class="num">{r["records"]:,}</td>'
        f'<td class="muted">{esc(", ".join(str(x) for x in json.loads(r["sample_keys"] or "[]")))}</td></tr>'
        for r in stores)

    docs = []
    for store in ("positive_examples", "negative_examples", "api_contracts"):
        row = next((r for r in stores if r["store"] == store), None)
        if not row:
            continue
        sect = [f'<h2>{esc(store.replace("_", " "))}</h2>']
        base = Path(row["path"])
        for f in sorted(base.glob("*.md"))[:12]:
            try:
                txt = f.read_text(errors="replace")[:6000]
            except OSError:
                continue
            first = next((ln.lstrip("# ") for ln in txt.splitlines() if ln.strip()), f.name)
            sect.append(f'<details class="card"><summary><b>{esc(f.name)}</b> '
                        f'<span class="muted">{esc(first[:110])}</span></summary>'
                        f'<pre>{esc(txt)}</pre></details>')
        docs.append("".join(sect))

    detail = knowledge_store_detail(c, qs, stores)
    if detail is not None:
        return detail

    coverage = knowledge_coverage_html(c)
    measurement = knowledge_measurement_html(stores)

    body = (
        f"<h1>Knowledge base</h1>"
        f"<p class='ink2'>Every knowledge surface of the maintainers' reference store — instincts are "
        f"one of {len(stores)} stores. Read-only mirror of "
        f"<code>{esc(str(Path(stores[0]['path']).parent))}</code>.</p>"
        + groups_html + banks_html
        + f"<h2>Reference store</h2><div class='tiles'>{tiles}</div>"
        f"<h2>Instincts by status</h2>{status_chart}"
        f"<h2>Learned instincts</h2>{filt}<p class='muted'>{len(rows)} shown</p>"
        f"<table><tr><th>rule</th><th>status</th><th class='num'>conf</th></tr>{itr}</table>"
        f"<h2>All stores</h2><p class='muted'>lifecycle siblings "
        f"(_pending / _staged / .bak / .lock) fold into their base store</p>"
        f"<table><tr><th>store</th><th>kind · lanes</th>"
        f"<th class='num'>records</th><th>schema / contents</th></tr>{str_}</table>"
        + measurement + coverage + "".join(docs)
    )
    return page("Knowledge", body, "Knowledge")


_FACETS = {"tool": "tool", "file": "files", "err": "errclass",
           "day": "day", "project": "project"}


def _body_fts_query(q):
    """`fix the build tool:Bash err:traceback day:2026-08-25` ->
    FTS5 column-filtered match string. Returns None when q is facet-free
    AND empty of terms."""
    terms, cols = [], []
    for tok in q.split():
        k, _, v = tok.partition(":")
        if v and k in _FACETS:
            cols.append(f'{_FACETS[k]}: "{v}"')
        else:
            terms.append(tok)
    if terms:
        cols.insert(0, "text: " + " ".join(
            '"' + t.replace('"', "") + '"' for t in terms))
    return " AND ".join(cols) if cols else None


def h_search(qs):
    q = qs.get("q", [""])[0].strip()
    rows, body_rows = [], []
    if q:
        c = db()
        try:
            rows = c.execute(
                "SELECT kind, key, snippet(search, 2, '<b>', '</b>', '…', 24) s"
                " FROM search WHERE search MATCH ? ORDER BY rank LIMIT 40",
                (q.split(" ")[0] if ":" in q else q,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = c.execute(
                "SELECT kind, key, text FROM search WHERE text LIKE ? LIMIT 40",
                (f"%{q}%",),
            ).fetchall()
        fts = _body_fts_query(q)
        if fts:
            try:
                body_rows = c.execute(
                    "SELECT project, session, line, day, tool,"
                    " snippet(body, 0, '<b>', '</b>', '…', 18) s"
                    " FROM body WHERE body MATCH ? ORDER BY bm25(body) LIMIT 20",
                    (fts,)).fetchall()
            except sqlite3.OperationalError:
                body_rows = []
    items = []
    for r in rows:
        if r["kind"] == "session":
            proj, sid = r["key"].rsplit("/", 1)
            link = f'<a href="/session/{esc(proj)}/{esc(sid)}">{esc(proj)}/{sid[:8]}</a>'
        elif r["kind"] == "learning":
            link = f'<a href="/learnings?q={esc(q)}">{esc(r["key"])}</a>'
        elif r["kind"] == "store":
            link = f'<a href="/knowledge?q={esc(q)}">store {esc(r["key"])}</a>'
        else:
            link = f'<a href="/instincts?q={esc(q)}">instinct #{esc(r["key"])}</a>'
        items.append(f'<div class="card"><span class="chip">{r["kind"]}</span> '
                     f'{link}<br><span class="ink2">{r[2]}</span></div>')
    body_items = []
    for r in body_rows:
        link = (f'/session/{esc(r["project"])}/{esc(r["session"])}'
                f'?start={max(0, r["line"] - 20)}&limit=80#L{r["line"]}')
        chips = "".join(f'<span class="chip">{esc(x)}</span> '
                        for x in (r["day"], (r["tool"] or "").split(" ")[0]) if x)
        body_items.append(
            f'<div class="card">{chips}<a href="{link}">'
            f'{esc(short_project(r["project"]))}'
            f'/{r["session"][:8]} L{r["line"]}</a>'
            f'<br><span class="ink2">{r["s"]}</span></div>')
    body_html = (
        f"<h2>Message bodies — {len(body_items)} hits</h2>" + "".join(body_items)
        if q else "")
    body = (f'<h1>Search</h1><form method="get"><input name="q" value="{esc(q)}"'
            f' size="40" autofocus> <button>search</button></form>'
            f'<p class="muted">facets: <code>tool:Bash</code> <code>file:app.py</code>'
            f' <code>err:traceback</code> <code>day:2026-08-25</code>'
            f' <code>project:loglearning</code> — combine with free text</p>'
            f'<p class="muted">{len(items)} metadata hits'
            f' (intents + instinct rules + learnings + store records)</p>'
            + "".join(items) + body_html)
    return page(f"search: {q}", body, "")


_STOPWORDS = frozenset(
    "the a an and or to of in on for with is are this that it as at by from"
    " be we you i our your do does not no if then so let lets please can"
    " need needs want run running use using make making get new all".split())


def api_graph(hindsight_layer=False):
    c = db()
    nodes, edges, seen = [], [], set()

    def add(nid, label, typ, href="", conf=None):
        if nid not in seen:
            seen.add(nid)
            d = {"id": nid, "label": label, "type": typ, "href": href}
            if conf is not None:
                d["conf"] = conf
            nodes.append({"data": d})

    graph_sids = {}
    for r in c.execute("SELECT id, rule, category, confidence, evidence,"
                       " source_projects FROM instincts"):
        iid = f"i{r['id']}"
        add(iid, (r["rule"][:60] + "…") if len(r["rule"]) > 60 else r["rule"],
            "instinct", conf=r["confidence"] or 0)
        if r["category"]:
            add(f"t:{r['category']}", r["category"], "topic",
                f"/instincts?cat={r['category']}")
            edges.append({"data": {"source": iid, "target": f"t:{r['category']}",
                                   "kind": "about"}})
        for p in json.loads(r["source_projects"] or "[]"):
            add(f"p:{p}", p, "project")
            edges.append({"data": {"source": iid, "target": f"p:{p}"}})
        for ev in json.loads(r["evidence"] or "[]"):
            m = EVIDENCE_RE.search(ev or "")
            if not m:
                continue
            sid = m.group(1)
            srow = c.execute(
                "SELECT project, session_id, intent FROM sessions"
                " WHERE session_id = ? OR session_id LIKE ?",
                (sid, sid[:8] + "%")).fetchone()
            href = (f"/session/{srow['project']}/{srow['session_id']}" if srow else "")
            add(f"s:{sid[:8]}", sid[:8], "session", href)
            edges.append({"data": {"source": iid, "target": f"s:{sid[:8]}"}})
            if srow:
                graph_sids[srow["session_id"]] = (f"s:{sid[:8]}",
                                                  srow["intent"] or "")

    # topic nodes from intent keywords of graph sessions (deterministic)
    from collections import Counter
    words = Counter()
    for _, (nid, intent) in graph_sids.items():
        words.update(w for w in re.findall(r"[a-z][a-z0-9_-]{3,}", intent.lower())
                     if w not in _STOPWORDS)
    top_kw = {w for w, n in words.most_common(10) if n >= 2}
    for sid_full, (nid, intent) in graph_sids.items():
        low = intent.lower()
        for w in top_kw:
            if w in low:
                add(f"t:{w}", w, "topic", f"/search?q={w}")
                edges.append({"data": {"source": nid, "target": f"t:{w}",
                                       "kind": "about"}})

    # state-vector similarity edges between sessions already in the graph
    try:
        full_by_prefix = {s[:8]: nid for s, (nid, _) in graph_sids.items()}
        for a, b, score in c.execute(
                "SELECT a, b, score FROM sessions_similarity WHERE score >= 0.3"):
            na, nb = full_by_prefix.get(a[:8]), full_by_prefix.get(b[:8])
            if na and nb and na != nb:
                edges.append({"data": {"source": na, "target": nb,
                                       "kind": "related", "w": score}})
    except sqlite3.OperationalError:
        pass

    if hindsight_layer:
        hg = hindsight_get(f"/v1/default/banks/{HINDSIGHT_BANK}/graph")
        if isinstance(hg, dict) and hg.get("nodes"):
            add("hb:bank", f"hindsight: {HINDSIGHT_BANK}", "entity", "/hindsight")
            for n in hg["nodes"][:120]:
                d = n.get("data", {})
                nid = f"h:{d.get('id', '')[:12]}"
                add(nid, (d.get("label") or "")[:40], "entity", "/hindsight")
                edges.append({"data": {"source": "hb:bank", "target": nid,
                                       "kind": "observes"}})
    return {"nodes": nodes, "edges": edges}


GRAPH_JS = """
fetch('/api/graph'+(window.HIND?'?hindsight=1':'')).then(r=>r.json()).then(g=>{
const style=getComputedStyle(document.documentElement);
const col={project:style.getPropertyValue('--s1'),session:style.getPropertyValue('--s2'),
           instinct:style.getPropertyValue('--s3'),topic:style.getPropertyValue('--warn'),
           entity:style.getPropertyValue('--axis')};
const deg={};g.edges.forEach(e=>{deg[e.data.source]=(deg[e.data.source]||0)+1;
                                 deg[e.data.target]=(deg[e.data.target]||0)+1;});
const sz=e=>10+Math.min(24,2*(deg[e.data('id')]||1));
const cy=cytoscape({container:document.getElementById('cy'),
 elements:[...g.nodes,...g.edges],
 style:[{selector:'node',style:{'background-color':e=>col[e.data('type')],
   'label':'','width':sz,'height':sz}},
  {selector:'node[type="instinct"]',style:{'shape':'round-rectangle'}},
  {selector:'node.labeled',style:{'label':'data(label)','font-size':10,
   'color':style.getPropertyValue('--ink'),'text-wrap':'wrap','text-max-width':190,
   'text-background-color':style.getPropertyValue('--surface'),
   'text-background-opacity':0.95,'text-background-padding':3,'z-index':9}},
  {selector:'node.dim',style:{'opacity':0.12}},
  {selector:'node[type="topic"]',style:{'shape':'diamond'}},
  {selector:'edge',style:{'width':1,'line-color':style.getPropertyValue('--grid'),
   'curve-style':'haystack','opacity':0.7}},
  {selector:'edge[kind="related"]',style:{'line-style':'dashed',
   'line-color':style.getPropertyValue('--s2'),'width':2,'opacity':0.55}},
  {selector:'edge[kind="about"]',style:{'opacity':0.35}},
  {selector:'edge.dim',style:{'opacity':0.05}}],
 layout:{name:'cose',animate:false,nodeRepulsion:26000,componentSpacing:110,
         idealEdgeLength:70}});
cy.on('mouseover','node',e=>{const n=e.target;
  cy.elements().addClass('dim');
  const hood=n.closedNeighborhood();hood.removeClass('dim');
  n.addClass('labeled');n.neighborhood('node').addClass('labeled');});
cy.on('mouseout','node',()=>{cy.elements().removeClass('dim');
  if(!document.getElementById('lbl').checked)cy.nodes().removeClass('labeled');});
cy.on('tap','node',e=>{const h=e.target.data('href');if(h)location=h;});
const apply=()=>{const q=document.getElementById('gq').value.toLowerCase();
 const minc=+document.getElementById('minconf').value;
 const on=t=>{const el=document.getElementById('t-'+t);return !el||el.checked};
 cy.batch(()=>cy.nodes().forEach(n=>{
   const okConf=n.data('type')!=='instinct'||(n.data('conf')||0)>=minc;
   const vis=on(n.data('type'))&&okConf&&(!q||(n.data('label')||'').toLowerCase().includes(q));
   n.style('display',vis?'element':'none');}));};
['gq','minconf','t-instinct','t-session','t-project','t-topic','t-entity'].forEach(id=>{
  const el=document.getElementById(id); if(el)el.addEventListener('input',apply);});
document.getElementById('lbl').addEventListener('input',e=>
  cy.nodes()[e.target.checked?'addClass':'removeClass']('labeled'));
document.getElementById('cnt').textContent=
  g.nodes.length+' nodes · '+g.edges.length+' edges';
}).catch(e=>{document.getElementById('cy').innerHTML=
 '<p style="padding:20px">graph lib unavailable (offline?) — use the <a href="/instincts">instinct table</a></p>'});
"""


def h_graph(qs):
    hind = qs.get("hindsight", ["0"])[0] == "1"
    hind_toggle = (f'<a href="/graph{"" if hind else "?hindsight=1"}">'
                   f'{"hide" if hind else "show"} hindsight layer</a>')
    body = f"""<script>window.HIND={1 if hind else 0}</script><h1>Knowledge graph</h1>
<p class="ink2">Instincts ↔ evidence sessions ↔ source projects. Hover a node to
reveal its label and neighborhood; click a session node to open its transcript at
the evidence. <span id="cnt" class="muted"></span></p>
<div class="controls">
 <input id="gq" placeholder="filter nodes…" size="24">
 <label><input type="checkbox" id="t-instinct" checked>
  <span class="sw" style="background:var(--s3)"></span>instinct</label>
 <label><input type="checkbox" id="t-session" checked>
  <span class="sw" style="background:var(--s2)"></span>session</label>
 <label><input type="checkbox" id="t-project" checked>
  <span class="sw" style="background:var(--s1)"></span>project</label>
 <label><input type="checkbox" id="t-topic" checked>
  <span class="sw" style="background:var(--warn)"></span>topic</label>
 <label><input type="checkbox" id="t-entity" checked>
  <span class="sw" style="background:var(--axis)"></span>entity</label>
 <label>conf ≥ <select id="minconf"><option value="0">0</option>
  <option value="5">5</option><option value="7">7</option>
  <option value="9">9</option></select></label>
 <label><input type="checkbox" id="lbl"> all labels</label>
 {hind_toggle}</div>
<div id="cy"></div>
<script src="https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js"></script>
<script>{GRAPH_JS}</script>"""
    return page("Graph", body, "Graph")


def hindsight_get(path):
    try:
        with urllib.request.urlopen(HINDSIGHT + path, timeout=6) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — surface any proxy failure in UI
        return {"error": str(e)}


def hindsight_post(path, payload):
    """Server-side proxy to the loopback Hindsight API. The dashboard's own HTTP
    surface stays GET-only; this outbound call never leaves 127.0.0.1."""
    try:
        req = urllib.request.Request(
            HINDSIGHT + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001 — surface any proxy failure in UI
        return {"error": str(e)}


def h_hindsight(qs):
    banks = hindsight_get("/v1/default/banks")
    if isinstance(banks, dict) and banks.get("error"):
        body = (f"<h1>Hindsight</h1><div class='card'>API not reachable at {HINDSIGHT}: "
                f"<code>{esc(banks['error'])}</code><br>Start it per the hindsight-memory "
                f"runbook, or use the CLI: <code>hindsight ui</code></div>")
        return page("Hindsight", body, "Hindsight")
    items = banks if isinstance(banks, list) else banks.get("items", banks.get("banks", []))
    bank_ids = [b.get("bank_id") or b.get("id") or "?" for b in items or []]
    q = qs.get("q", [""])[0].strip()
    bank_sel = qs.get("bank", [bank_ids[0] if bank_ids else ""])[0]
    mode = qs.get("mode", ["recall"])[0]

    recall_html = ""
    if q and bank_sel in bank_ids and mode == "reflect":
        res = hindsight_post(f"/v1/default/banks/{bank_sel}/reflect",
                             {"query": q})
        if res.get("error"):
            recall_html = (f'<div class="card">reflect failed: '
                           f'<code>{esc(res["error"])}</code></div>')
        else:
            recall_html = (
                "<h2>Reflection — LLM-composed over the bank (verify against "
                "committed sources)</h2><div class='card'><span class='ink2' "
                "style='white-space:pre-wrap'>"
                + esc(res.get("text", ""))[:6000] + "</span></div>")
    elif q and bank_sel in bank_ids:
        res = hindsight_post(
            f"/v1/default/banks/{bank_sel}/memories/recall", {"query": q})
        if res.get("error"):
            recall_html = f'<div class="card">recall failed: <code>{esc(res["error"])}</code></div>'
        else:
            hits = []
            for m in (res.get("results") or [])[:12]:
                ents = " ".join(f'<span class="chip">{esc(e)}</span>'
                                for e in (m.get("entities") or [])[:5])
                hits.append(
                    f'<div class="card"><span class="chip">{esc(m.get("type", "?"))}</span> '
                    f'<span class="muted">{esc((m.get("occurred_start") or "")[:10])}</span>'
                    f'<br>{esc(m.get("text", ""))}<br>{ents}</div>')
            recall_html = (f"<h2>{len(hits)} recalled — pointers, not evidence</h2>"
                           + ("".join(hits) or "<p class='muted'>nothing recalled</p>"))

    opts = "".join(f'<option {"selected" if b == bank_sel else ""}>{esc(b)}</option>'
                   for b in bank_ids)
    mode_opts = "".join(
        f'<option {"selected" if mode == m else ""}>{m}</option>'
        for m in ("recall", "reflect"))
    form = (f'<form method="get" class="controls"><select name="bank">{opts}</select>'
            f'<select name="mode">{mode_opts}</select>'
            f'<input name="q" value="{esc(q)}" placeholder="ask the memory bank…" size="42">'
            f'<button>go</button></form>')
    cards = []
    for b in items or []:
        bid = b.get("bank_id") or b.get("id") or "?"
        cards.append(
            f'<div class="card"><b>{esc(bid)}</b> '
            f'<span class="muted">{esc(b.get("name", ""))}</span><br>'
            f'<a href="{HINDSIGHT_LINK_BASE}/v1/default/banks/{esc(bid)}/graph" target="_blank">entity graph JSON</a> · '
            f'<span class="muted">CLI: hindsight memory recall {esc(bid)} "question"</span></div>'
        )
    body = (
        "<h1>Hindsight memory</h1>"
        "<p class='ink2'>Local semantic memory (LLM-extracted observations). "
        "Treat recalls as <b>pointers</b> — authoritative numbers live in committed "
        "files / the JSONL banks, so cross-check before quoting.</p>"
        + form + recall_html + "<h2>Banks</h2>"
        + ("".join(cards) or "<p class='muted'>no banks yet</p>")
    )
    return page("Hindsight", body, "Hindsight")


def export_gate_howto_html():
    """What to do when the gate is closed because no scrub config exists: the file's shape, the shipped
    example to copy, the two ways to install it, and the fact that export stays closed until it exists and parses. No
    default config ships on purpose — its values are per installation and a default would be false safety."""
    example = next((p for p in ("config/scrub-patterns.example.json", "scripts/scrub-patterns.example.json")
                    if (REPO / p).is_file()), "scrub-patterns.example.json")
    return (
        "<h2>How to open the gate</h2>"
        "<p>Export stays closed until a scrub config exists and parses. None ships by default: its values (account ids, "
        "hostnames, customer and people names, token shapes) belong to your installation.</p>"
        f"<ol><li>Copy the shipped example <code>{esc(example)}</code> — it carries FAKE values on purpose — to "
        "<code>state/scrub-patterns.json</code> under this runtime, or anywhere and point <code>LOGLEARNER_SCRUB_CONFIG</code> at it.</li>"
        "<li>Fill the REAL values for your environment. Shape: <code>version</code> (1); <code>replacements</code> (name → placeholder, "
        "e.g. a customer or host name → <code>&lt;customer-a&gt;</code>); <code>numbered</code> (classes such as account ids, instance ids, "
        "ENIs, each replaced by a numbered placeholder); <code>detectors</code> (regexes for secret shapes that BLOCK an export outright); "
        "<code>detector_allowlist</code> (known false positives); <code>file_denylist</code> (file names that never leave, such as "
        "settings.json and .env*).</li>"
        "<li>Reload this page: it dry-runs the gate and lists every file and replacement before anything can be downloaded.</li></ol>"
        "<p class='muted'>The hook, recall and the dashboard work without a scrub config; only export needs it.</p>"
    )


def h_export(qs):
    """Scrub-gated export (KR5.2): dry-runs the gate and shows exactly what
    would leave, or why the gate refuses. Download goes via /export.tar.gz —
    the same fail-closed path, so a block here is a block there."""
    cfg_path = export_gate.find_config()
    files = export_gate.collect_files()
    rows = "".join(
        f"<tr><td>{esc(rel)}</td><td class='num'>{len(text):,}</td></tr>"
        for rel, text in sorted(files.items())
    )
    status, verdict = "", ""
    try:
        data, manifest = export_gate.scrubbed_tarball()
        n_repl = sum(sum(m["replacements"].values()) for m in manifest)
        verdict = (
            f"<p class='good'>GATE OPEN — {len(manifest)} file(s), "
            f"{n_repl} replacement(s) applied, bundle {len(data):,} bytes. "
            f"<a href='/export.tar.gz'>download loglearner-export.tar.gz</a></p>"
        )
    except export_gate.ExportBlocked as e:
        verdict = (
            f"<p class='crit'><b>GATE CLOSED</b> — {esc(e)}</p>"
            "<p class='muted'>Fail-closed by design: no config, a detector hit, "
            "or a residual pattern means nothing leaves this host.</p>"
        )
        if cfg_path is None:
            verdict += export_gate_howto_html()
    status = (
        f"<p>scrub config: <code>{esc(cfg_path) if cfg_path else 'NONE'}</code>"
        f" · artifacts matched: {len(files)}</p>"
    )
    body = (
        "<h1>Export</h1>"
        "<p class='ink2'>Everything on this page routes through the fail-closed "
        "scrub gate (<code>loglearner/scrub_core.py</code>). Scope is mined "
        "artifacts only — raw session transcripts are never exportable.</p>"
        + status + verdict
        + "<h2>Artifacts in scope</h2>"
        + (f"<table><tr><th>file</th><th>bytes</th></tr>{rows}</table>"
           if rows else "<p class='muted'>none found</p>")
    )
    return page("Export", body, "Export")


# --------------------------------------------------------------------- server
GIST_INDEX = Path(os.environ.get(
    "LOGLEARNER_GIST_INDEX",
    Path(__file__).parent.parent / "state" / "gist-index.jsonl"))


def _gist_records():
    if not GIST_INDEX.is_file():
        return [], {}
    recs = []
    for line in GIST_INDEX.open():
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    meta_p = GIST_INDEX.with_name(GIST_INDEX.name + ".meta.json")
    meta = {}
    if meta_p.is_file():
        try:
            meta = json.loads(meta_p.read_text())
        except json.JSONDecodeError:
            meta = {}
    return recs, meta


def h_gists(qs):
    recs, meta = _gist_records()
    if not recs:
        return page("gists", "<h2>Gist corpus</h2><p class='muted'>No index yet — run "
                    "<code>python3 loglearner.py --source gist --manage</code>.</p>", "Gists")

    if qs.get("report", ["0"])[0] == "1":
        rp = GIST_INDEX.with_name("gist-manage-report.md")
        body = (f"<h2>Management report</h2><p><a href='/gists'>← corpus</a></p>"
                f"<pre style='white-space:pre-wrap'>{esc(rp.read_text())}</pre>"
                if rp.is_file() else "<p class='muted'>no report</p>")
        return page("gist report", body, "Gists")

    lane_f = qs.get("lane", [""])[0]
    type_f = qs.get("type", [""])[0]
    flag_f = qs.get("flag", [""])[0]
    q = qs.get("gq", [""])[0].strip().lower()

    def flagged(r, name):
        f = r["facets"]
        return (f.get(name) is True) or (name == "public" and f["visibility"] == "public")

    shown = [r for r in recs
             if (not lane_f or r["facets"]["lane"] == lane_f)
             and (not type_f or r["facets"]["artifact_type"] == type_f)
             and (not flag_f or flagged(r, flag_f))
             and (not q or q in (r["desc"] or "").lower()
                  or q in " ".join(r.get("files", [])).lower())]

    lanes, types = {}, {}
    for r in recs:
        lanes[r["facets"]["lane"]] = lanes.get(r["facets"]["lane"], 0) + 1
        t = r["facets"]["artifact_type"]
        types[t] = types.get(t, 0) + 1
    n_quar = sum(1 for r in recs if r["facets"]["quarantined"])
    n_und = sum(1 for r in recs if r["facets"]["undescribed"])
    # Corpus-wide (meta sidecar) — the violating gist may be un-mirrored, and
    # a violations tile that undercounts is worse than none.
    n_pub = meta.get("public", sum(1 for r in recs if flagged(r, "public")))
    pub_v = (f'<span style="color:var(--crit)">{n_pub}</span>'
             if n_pub else "0")

    tiles = (tile(len(recs), "gists indexed")
             + tile(meta.get("live_total", "—"), "live on GitHub")
             + tile(meta.get("unmirrored", "—"), "un-mirrored")
             + tile(n_und, "un-described")
             + tile(n_quar, "quarantined")
             + tile(pub_v, "PUBLIC (violation)"))

    lane_bar = hbar_svg(sorted(lanes.items(), key=lambda kv: -kv[1]))
    opts = lambda d, cur: "".join(  # noqa: E731
        f'<option value="{esc(k)}" {"selected" if k == cur else ""}>{esc(k)} ({v})</option>'
        for k, v in sorted(d.items(), key=lambda kv: -kv[1]))
    controls = f"""<form class="controls" method="get" action="/gists" style="margin:12px 0">
<select name="lane" onchange="this.form.submit()"><option value="">all lanes</option>{opts(lanes, lane_f)}</select>
<select name="type" onchange="this.form.submit()"><option value="">all types</option>{opts(types, type_f)}</select>
<select name="flag" onchange="this.form.submit()"><option value="">all flags</option>
<option value="undescribed" {"selected" if flag_f == "undescribed" else ""}>un-described</option>
<option value="quarantined" {"selected" if flag_f == "quarantined" else ""}>quarantined</option>
<option value="public" {"selected" if flag_f == "public" else ""}>public</option></select>
<input name="gq" value="{esc(q)}" placeholder="filter text…" size="18">
<button>filter</button> <a href="/gists">reset</a> · <a href="/gists?report=1">management report</a></form>"""

    rows = []
    for r in sorted(shown, key=lambda r: (r["facets"]["date"] or ""), reverse=True)[:200]:
        f = r["facets"]
        chips = ""
        if f["visibility"] == "public":
            chips += '<span class="chip crit">PUBLIC</span> '
        if f["quarantined"]:
            chips += '<span class="chip warn2">quarantined</span> '
        if f["undescribed"]:
            chips += '<span class="chip">un-described</span> '
        desc = esc((r["desc"] or "")[:110]) or "<span class='muted'>(no description)</span>"
        rows.append(
            f"<tr><td><code>{r['id'][:8]}</code></td><td>{desc}</td>"
            f"<td><a href='/gists?lane={esc(f['lane'])}'>{esc(f['lane'])}</a></td>"
            f"<td>{esc(f['artifact_type'])}</td><td>{esc(f['date'] or '')}</td>"
            f"<td>{f['file_count']}</td><td>{chips}</td></tr>")
    table = ("<table><tr><th>id</th><th>description (scrubbed)</th><th>lane</th>"
             "<th>type</th><th>date</th><th>files</th><th>flags</th></tr>"
             + "".join(rows) + "</table>")

    body = (f"<h2>Gist corpus <span class='muted'>· profile: "
            f"{esc(str(meta.get('profile', '?')))} · metadata: "
            f"{esc(str(meta.get('metadata', '?')))}</span></h2>"
            f"<div class='tiles'>{tiles}</div>"
            f"<h3>By lane</h3>{lane_bar}{controls}"
            f"<p class='muted'>{len(shown)} match{'es' if len(shown) != 1 else ''}"
            f"{' (first 200 shown)' if len(shown) > 200 else ''} · bodies never "
            f"leave the mirror; descriptions are scrubbed at index time</p>{table}")
    return page("gists", body, "Gists")


# ---------------------------------------------------------------------------
# /api/tile.json — the Log Learner + Meta Learner tile contract (aws-do-agent and others).
# Always HTTP 200 + JSON; a missing data source is reported as ok:false + error and
# status:"degraded" so a renderer turns the tile RED instead of blank. Never raises.
# ---------------------------------------------------------------------------
BANKS_DIR = Path(os.environ.get("LOGLEARNER_BANKS_DIR",
                                os.environ.get("LOGLEARNER_LEARNINGS_DIR",
                                               str(Path.home() / ".loglearner" / "banks")))).expanduser()
UCB_DIR = Path(os.environ.get("LOGLEARNER_UCB_DIR", str(REPO / "output"))).expanduser()
CAUTIONS_PATH = os.environ.get("LOGLEARNER_CAUTIONS_PATH", "")


def _jsonl_rows(path):
    rows = []
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


_TILE_LAST_GOOD = {"ll": None, "at": None, "locked_since": None}  # last good index counts, served through a transient lock


def api_tile(db_path=None, banks_dir=None, ucb_dir=None, cautions_path=None, now=None):
    """Build the tile JSON. Every section carries ok/error; status is 'ok' only when the
    two primary sources (index + banks) are present. A transient 'database is locked' on the
    index (the indexer committing) serves the last good counts with a note instead of degraded."""
    import datetime as _dt
    db_path = Path(db_path or DB_PATH)
    banks_dir = Path(banks_dir or BANKS_DIR).expanduser()
    ucb_dir = Path(ucb_dir or UCB_DIR).expanduser()
    cautions_path = cautions_path if cautions_path is not None else CAUTIONS_PATH
    now = now or _dt.datetime.now(_dt.timezone.utc)
    errors = []

    ll = {"ok": False}
    try:
        if not db_path.is_file():
            raise FileNotFoundError(f"index not found: {db_path} (run dashboard/indexer.py)")
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)  # the tile waits through a commit instead of going red
        c.row_factory = sqlite3.Row
        tot = c.execute("SELECT COUNT(*) n, SUM(size_bytes) b, SUM(tool_calls) t,"
                        " SUM(user_corrections) uc, SUM(tool_errors) te FROM sessions").fetchone()
        prim = c.execute("SELECT COUNT(*) FROM sessions WHERE classification='primary'").fetchone()[0]
        ll = {"ok": True, "sessions": tot["n"] or 0, "primary": prim,
              "corpus_bytes": tot["b"] or 0, "tool_calls": tot["t"] or 0,
              "instincts": c.execute("SELECT COUNT(*) FROM instincts").fetchone()[0],
              "learnings": c.execute("SELECT COUNT(*) FROM learnings").fetchone()[0],
              "gaps": {"corrections": tot["uc"] or 0, "tool_errors": tot["te"] or 0},
              "last_index": dict(c.execute("SELECT * FROM meta")).get("last_index"),
              "index_db": str(db_path)}
        c.close()
        _TILE_LAST_GOOD["ll"] = dict(ll)
        _TILE_LAST_GOOD["at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _TILE_LAST_GOOD["locked_since"] = None
    except sqlite3.OperationalError as e:
        # A transient lock (the indexer committing) is not an outage - serve the last good
        # counts with a note and status ok, so the home tiles never flip Down for an index refresh.
        if "locked" in str(e).lower() and _TILE_LAST_GOOD.get("ll"):
            since = _TILE_LAST_GOOD.get("locked_since") or now.strftime("%H:%MZ")
            _TILE_LAST_GOOD["locked_since"] = since
            ll = dict(_TILE_LAST_GOOD["ll"])
            ll["note"] = (f"index refresh in progress since {since} "
                          f"(counts from {_TILE_LAST_GOOD.get('at')})")
            ll["stale"] = True
        else:
            ll = {"ok": False, "error": f"{type(e).__name__}: {e}", "index_db": str(db_path)}
            errors.append("log_learner: " + ll["error"])
    except Exception as e:  # noqa: BLE001 — report, never raise
        ll = {"ok": False, "error": f"{type(e).__name__}: {e}", "index_db": str(db_path)}
        errors.append("log_learner: " + ll["error"])

    ml = {"ok": False}
    try:
        files = sorted(p for p in banks_dir.rglob("*.jsonl") if p.is_file() and ".bak" not in p.name) \
            if banks_dir.is_dir() else []
        if not files:
            raise FileNotFoundError(f"no bank files under {banks_dir}")
        rows = [r for f in files for r in _jsonl_rows(f)]
        by_status, lifecycle = {}, {}
        for r in rows:
            by_status[str(r.get("status") or "unset")] = by_status.get(str(r.get("status") or "unset"), 0) + 1
            st = r.get("lifecycle_state") or "screened(legacy)"
            lifecycle[st] = lifecycle.get(st, 0) + 1
        ml = {"ok": True, "instincts_total": len(rows), "banks": [str(f) for f in files],
              "by_status": by_status, "lifecycle": lifecycle, "banks_dir": str(banks_dir)}
    except Exception as e:  # noqa: BLE001
        ml = {"ok": False, "error": f"{type(e).__name__}: {e}", "banks_dir": str(banks_dir)}
        errors.append("meta_learner: " + ml["error"])

    bandit = {"arms": 0, "pulls": 0, "rewards": 0, "exposures": 0, "exposures_24h": 0,
              "deliveries_by_session": {}, "ledger_dir": str(ucb_dir), "note": ""}
    try:
        arms_p, rew_p, exp_p = ucb_dir / "loglearner_arms.jsonl", ucb_dir / "loglearner_rewards.jsonl", ucb_dir / "loglearner_exposures.jsonl"
        if arms_p.is_file():
            bandit["arms"] = len(_jsonl_rows(arms_p))
        if rew_p.is_file():
            rr = _jsonl_rows(rew_p)
            bandit["pulls"] = len(rr)
            bandit["rewards"] = sum(1 for r in rr if (r.get("reward") or 0) >= 1)
        if exp_p.is_file():
            ex = _jsonl_rows(exp_p)
            bandit["exposures"] = len(ex)
            cutoff = (now - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bandit["exposures_24h"] = sum(1 for r in ex if str(r.get("ts", "")) >= cutoff)
            bs = {}
            for r in ex:
                k = str(r.get("session_id", ""))[:8]
                bs[k] = bs.get(k, 0) + 1
            bandit["deliveries_by_session"] = dict(sorted(bs.items(), key=lambda kv: -kv[1])[:10])
        if not (arms_p.is_file() or rew_p.is_file() or exp_p.is_file()):
            bandit["note"] = "no ledgers yet (fresh install)"
    except Exception as e:  # noqa: BLE001
        bandit["note"] = f"ledger read error: {type(e).__name__}: {e}"
    ml["bandit"] = bandit
    if cautions_path:
        try:
            ml["caution_rules"] = len(_jsonl_rows(cautions_path))
        except Exception as e:  # noqa: BLE001
            ml["caution_rules"] = None
            ml["caution_rules_error"] = f"{type(e).__name__}: {e}"

    status = "ok" if ll.get("ok") and ml.get("ok") else "degraded"
    return {"schema": "loglearner-tile/v1", "status": status,
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "log_learner": ll, "meta_learner": ml,
            "links": {"overview": "/", "instincts": "/instincts", "graph": "/graph",
                      "learnings": "/learnings", "search": "/search"},
            "errors": errors}


ROUTES = [
    (re.compile(r"^/$"), lambda qs, m: h_overview(qs)),
    (re.compile(r"^/gists$"), lambda qs, m: h_gists(qs)),
    (re.compile(r"^/project/([^/]+)$"), lambda qs, m: h_project(qs, m.group(1))),
    (re.compile(r"^/session/([^/]+)/([0-9a-f-]+)$"),
     lambda qs, m: h_session(qs, m.group(1), m.group(2))),
    (re.compile(r"^/instincts$"), lambda qs, m: h_instincts(qs)),
    (re.compile(r"^/search$"), lambda qs, m: h_search(qs)),
    (re.compile(r"^/learnings$"), lambda qs, m: h_learnings(qs)),
    (re.compile(r"^/knowledge$"), lambda qs, m: h_knowledge(qs)),
    (re.compile(r"^/graph$"), lambda qs, m: h_graph(qs)),
    (re.compile(r"^/hindsight$"), lambda qs, m: h_hindsight(qs)),
    (re.compile(r"^/export$"), lambda qs, m: h_export(qs)),
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/tile.json":
            body = json.dumps(api_tile(), indent=1).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/graph":
            body = json.dumps(api_graph(
                hindsight_layer=qs.get("hindsight", ["0"])[0] == "1")).encode()
            self._send(200, "application/json", body)
            return
        if u.path == "/export.tar.gz":
            # fail-closed: any gate condition -> 403, zero bytes leave
            try:
                data, _ = export_gate.scrubbed_tarball()
                self.send_response(200)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition",
                                 "attachment; filename=loglearner-export.tar.gz")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except export_gate.ExportBlocked as e:
                self._send(403, "text/plain; charset=utf-8",
                           f"export blocked (fail-closed): {e}".encode())
            return
        for rx, fn in ROUTES:
            m = rx.match(u.path)
            if m:
                try:
                    out = fn(qs, m)
                except Exception as e:  # noqa: BLE001 — render, don't crash the server
                    out = page("error", f"<pre>{esc(e)}</pre>")
                self._send(200, "text/html; charset=utf-8", out.encode())
                return
        self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError) as e:
            # the client gave up (a home page may abort a slow tile read): one line, no traceback
            sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} client closed "
                             f"{self.path} before the reply ({type(e).__name__}, {len(body)} bytes)\n")

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    args = ap.parse_args()
    if not DB_PATH.exists():
        print("no index yet — run: python3 dashboard/indexer.py")
        return
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"LogLearner dashboard: http://127.0.0.1:{args.port}  (loopback only; "
          f"remote: ssh -L {args.port}:localhost:{args.port} <host>)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
