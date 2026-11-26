#!/usr/bin/env python3
"""Build/refresh the dashboard SQLite index over ~/.claude/projects.

Read-only over session files (streams, never json.load on a whole file);
incremental by (mtime, size); writes only dashboard/index.db.

Usage: python3 dashboard/indexer.py [--project SUBSTR] [--db PATH]
"""

import argparse
import json
import re
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from loglearner import session_scanner as scanner  # noqa: E402

DB_DEFAULT = Path(os.environ.get("LOGLEARNER_DB", REPO / "dashboard" / "index.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT, project TEXT, path TEXT PRIMARY KEY,
  size_bytes INT, mtime REAL, classification TEXT, total_lines INT,
  user_messages INT, assistant_messages INT, tool_calls INT,
  user_corrections INT, tool_errors INT, context_overflows INT,
  first_ts TEXT, last_ts TEXT, top_tools TEXT, intent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_sid ON sessions(session_id);
CREATE TABLE IF NOT EXISTS instincts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT, category TEXT,
  confidence INT, merge_count INT, source_file TEXT, source_projects TEXT,
  evidence TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(kind, key, text);
CREATE TABLE IF NOT EXISTS learnings (
  file TEXT, lid TEXT, rule TEXT, status TEXT, confidence TEXT,
  source TEXT, arm TEXT, reward REAL
);
CREATE TABLE IF NOT EXISTS store_instincts (
  id TEXT, rule TEXT, confidence TEXT, status TEXT, source TEXT, evidence TEXT,
  lane TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS store_inventory (
  store TEXT PRIMARY KEY, kind TEXT, records INT, sample_keys TEXT, path TEXT,
  lanes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS store_session_events (
  ts TEXT, source TEXT, session_id TEXT, event_type TEXT, evidence_ref TEXT
);
CREATE TABLE IF NOT EXISTS store_coverage (item TEXT, rating TEXT, section TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS body USING fts5(
  text, tool, files, errclass, day, project, session UNINDEXED, line UNINDEXED
);
CREATE TABLE IF NOT EXISTS body_meta (path TEXT PRIMARY KEY, mtime REAL);
CREATE TABLE IF NOT EXISTS seek (
  path TEXT, line INT, off INT, PRIMARY KEY (path, line)
);
CREATE TABLE IF NOT EXISTS session_extras (
  path TEXT PRIMARY KEY, timeline TEXT, err_layers TEXT
);
CREATE TABLE IF NOT EXISTS state_vectors (path TEXT PRIMARY KEY, vec TEXT);
CREATE TABLE IF NOT EXISTS sessions_similarity (
  a TEXT, b TEXT, score REAL, components TEXT, PRIMARY KEY (a, b)
);
CREATE TABLE IF NOT EXISTS edges (
  src_kind TEXT, src_id TEXT, dst_kind TEXT, dst_id TEXT,
  edge_kind TEXT, weight REAL, evidence_source TEXT, created_at TEXT,
  PRIMARY KEY (src_kind, src_id, dst_kind, dst_id, edge_kind)
);
CREATE TABLE IF NOT EXISTS arms (
  arm_id TEXT PRIMARY KEY, instinct_id TEXT, action TEXT,
  pulls INT, reward_sum REAL, regret_sum REAL, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  obs_id TEXT PRIMARY KEY, arm_id TEXT, session_id TEXT,
  reward REAL, regret REAL, notes TEXT, created_at TEXT
);
"""

LEARNINGS_DIR = Path(
    os.environ.get(
        "LOGLEARNER_BANKS_DIR",
        os.environ.get(
            "LOGLEARNER_LEARNINGS_DIR",
            Path.home() / ".loglearner" / "banks",
        ),
    )
).expanduser()


def bank_files():
    """Every bank file under the banks dir (recursive, so <banks>/global/instincts.jsonl counts; backups skipped)."""
    if not LEARNINGS_DIR.is_dir():
        return []
    return sorted(p for p in LEARNINGS_DIR.rglob("*.jsonl") if p.is_file() and ".bak" not in p.name.lower())


def _conf_int(v):
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    return {"high": 9, "med": 6, "medium": 6, "low": 3}.get(str(v or "").lower(), 0)

KNOWLEDGE_KB = Path(
    os.environ.get(
        "LOGLEARNER_KNOWLEDGE_DIR",   # neutral name; the older LOGLEARNER_KNOWLEDGE_DIR is still accepted
        os.environ.get(
            "LOGLEARNER_KNOWLEDGE_DIR",
            Path.home() / ".loglearner" / "reference"
            / "reference-flywheel" / "knowledge_base",
        ),
    )
)


def index_store(db):
    """reference knowledge base (optional — dir may not exist off-fleet).

    Catalogs every surface: the main instinct DB fully, every top-level JSONL
    store by count + schema sample, every subdirectory by file count.
    """
    # rebuilt from scratch every run — drop so schema changes apply cleanly
    db.execute("DROP TABLE IF EXISTS store_instincts")
    db.execute(
        "CREATE TABLE store_instincts (id TEXT, rule TEXT, confidence TEXT,"
        " status TEXT, source TEXT, evidence TEXT, lane TEXT DEFAULT 'active')")
    db.execute("DROP TABLE IF EXISTS store_inventory")
    db.execute(
        "CREATE TABLE store_inventory (store TEXT PRIMARY KEY, kind TEXT,"
        " records INT, sample_keys TEXT, path TEXT, lanes TEXT DEFAULT '')")
    if not KNOWLEDGE_KB.is_dir():
        return 0

    def put_store(store, kind, records, sample_keys, path, lanes=""):
        db.execute("REPLACE INTO store_inventory VALUES (?,?,?,?,?,?)",
                   (store, kind, records, json.dumps(sample_keys[:14]),
                    str(path), lanes))

    # active + pending instinct lanes (pending is the raw mint queue —
    # decision_engine verdicts append there before promotion)
    for lane, fname in (("active", "instincts.json"),
                        ("pending", "instincts_pending.json")):
        inst_path = KNOWLEDGE_KB / "learned_instincts" / fname
        try:
            items = json.loads(inst_path.read_text())
            items = items if isinstance(items, list) else items.get("instincts", [])
        except (OSError, json.JSONDecodeError):
            items = []
        for it in items:
            if not isinstance(it, dict):
                continue
            db.execute(
                "INSERT INTO store_instincts VALUES (?,?,?,?,?,?,?)",
                (str(it.get("id", "")), str(it.get("rule", ""))[:800],
                 str(it.get("confidence", "")), str(it.get("status", "")),
                 str(it.get("source", ""))[:200],
                 str(it.get("evidence", ""))[:500], lane),
            )
        if lane == "active":
            _li_keys = sorted(items[0].keys()) if items else []
            _li_active = len(items)
        else:
            put_store("learned_instincts", "instinct-db", _li_active, _li_keys,
                      KNOWLEDGE_KB / "learned_instincts" / "instincts.json",
                      lanes=json.dumps({"active": _li_active,
                                        "pending": len(items)}))

    # agent-behavior lane: reference's paired examples in LogLearner's own format
    ab = KNOWLEDGE_KB / "agent_behavior"
    if ab.is_dir():
        n_ex = sum(1 for line in (ab / "examples.jsonl").open(errors="replace")
                   if line.strip()) if (ab / "examples.jsonl").exists() else 0
        put_store("agent_behavior/examples", "jsonl", n_ex,
                  ["session", "lines", "category", "severity", "root_cause",
                   "situation", "negative", "positive", "instinct"],
                  ab / "examples.jsonl")
        try:
            n_rules = len(json.loads((ab / "rules.json").read_text()))
        except (OSError, json.JSONDecodeError):
            n_rules = 0
        put_store("agent_behavior/rules", "json", n_rules,
                  ["id", "rule", "evidence", "status", "scope"], ab / "rules.json")

    # sqlite stores (row counts across user tables; 0-byte decoys show 0)
    for rel in ("learned_instincts/instincts.sqlite",):
        f = KNOWLEDGE_KB / rel
        if not f.exists():
            continue
        total, tables = 0, []
        try:
            sq = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            for (t,) in sq.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                n = sq.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                total += n
                tables.append(f"{t}:{n}")
            sq.close()
        except sqlite3.Error:
            pass
        put_store(rel, "sqlite", total, tables, f)

    # every store's *_pending / *_staged_* / .bak-<ts> / .lock siblings are
    # LIFECYCLE STATES of one store, not separate stores
    def _base_and_lane(name):
        if name.endswith(".lock"):
            return name[:-5].split(".jsonl")[0], "lock"
        stem, bak = name.split(".jsonl")[0], (".bak" in name)
        for tag, lane in (("_pending", "pending"), ("_staged", "staged")):
            if tag in stem:
                return stem.split(tag)[0], ("bak" if bak else lane)
        return stem, ("bak" if bak else "active")

    groups = {}
    for f in sorted(list(KNOWLEDGE_KB.glob("*.jsonl*")) + list(KNOWLEDGE_KB.glob("*.lock"))):
        base, lane = _base_and_lane(f.name)
        groups.setdefault(base, {}).setdefault(lane, []).append(f)
    for base, lanes in sorted(groups.items()):
        n, keys, main = 0, [], None
        for f in lanes.get("active", []):
            main = f
            for line in f.open(errors="replace"):
                line = line.strip()
                if not line:
                    continue
                if not keys:
                    try:
                        keys = sorted(json.loads(line).keys())
                    except (json.JSONDecodeError, AttributeError):
                        pass
                n += 1
        lane_counts = {}
        for lane in ("pending", "staged"):
            m = sum(sum(1 for l in f.open(errors="replace") if l.strip())
                    for f in lanes.get(lane, []))
            if m:
                lane_counts[lane] = m
        if lanes.get("bak"):
            lane_counts["bak_files"] = len(lanes["bak"])
        if lanes.get("lock"):
            lane_counts["lock"] = True
        if n:
            lane_counts = {"active": n, **lane_counts}
        put_store(base, "jsonl", n, keys, main or next(iter(lanes.values()))[0],
                  lanes=json.dumps(lane_counts) if lane_counts else "")

    for d in sorted(p for p in KNOWLEDGE_KB.iterdir() if p.is_dir()):
        if d.name == "learned_instincts":
            continue
        files = [p for p in d.rglob("*") if p.is_file()]
        put_store(d.name, "dir", len(files),
                  sorted({p.suffix or p.name for p in files})[:14], d)

    db.commit()
    return db.execute("SELECT COUNT(*) FROM store_instincts").fetchone()[0]


def index_learnings(db):
    """instinct-schema learnings corpus (optional — dir may not exist off-fleet)."""
    db.execute("DELETE FROM learnings")
    for f in bank_files():
        for line in f.open(errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = d.get("meta") or {}
            reward = m.get("reward")
            db.execute(
                "INSERT INTO learnings VALUES (?,?,?,?,?,?,?,?)",
                (
                    f.stem, d.get("id", ""), str(d.get("rule", ""))[:600],
                    d.get("status", ""), str(d.get("confidence", "")),
                    str(d.get("source", ""))[:200], m.get("arm", ""),
                    float(reward) if isinstance(reward, (int, float)) else None,
                ),
            )
    db.commit()
    return db.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]


SEEK_EVERY = 2000  # byte-offset checkpoint cadence (lines)
_BODY_TEXT_CAP = 1800

# claude-session-analyzer error layers 1-4 (E2): applied to tool_result text
_ERR_LAYERS = (
    ("fatal", re.compile(r"SIGSEGV|SIGABRT|segfault|signal 11|CrashLoop|OOM"
                         r"|core dumped|\bkilled\b", re.I)),
    ("operational", re.compile(r"\berror\b|\bfailed\b|exit code [1-9]"
                               r"|Traceback \(most recent", re.I)),
    ("warnings", re.compile(r"\bwarn(?:ing)?\b|deprecated|\btimeout\b|timed out"
                            r"|\bhang\b|\bstuck\b", re.I)),
    ("tool_use", re.compile(r"tool_use_error|not found in file|rejected"
                            r"|not yet read|has not been read", re.I)),
)
IDLE_MIN_S = 300
TIMELINE_BUCKETS = 60


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


class _ExtrasAcc:
    """Per-session accumulator for the timeline strip + error layers,
    filled during the same stream as the body FTS pass."""

    def __init__(self):
        self.pts = []           # (epoch, line_no)
        self.layers = {k: {"n": 0, "samples": []} for k, _ in _ERR_LAYERS}
        # state-vector raw material
        self.tools = {}
        self.files = set()
        self.bash = set()
        self.errclasses = set()

    def see_ts(self, ts, line_no):
        e = _parse_ts(ts)
        if e is not None:
            self.pts.append((e, line_no))

    def see_result_text(self, text, line_no):
        for name, pattern in _ERR_LAYERS:
            if pattern.search(text):
                layer = self.layers[name]
                layer["n"] += 1
                if len(layer["samples"]) < 3:
                    layer["samples"].append(
                        {"line": line_no, "text": text.strip()[:160]})
                break

    def finish(self):
        if not self.pts:
            return json.dumps({}), json.dumps(self.layers)
        t0, t1 = self.pts[0][0], self.pts[-1][0]
        span = max(t1 - t0, 1.0)
        buckets = [0] * TIMELINE_BUCKETS
        idles = []
        prev = None
        for e, line_no in self.pts:
            # timestamps are not guaranteed monotonic (clock skew, replayed
            # envelopes) — clamp both ends or a rogue ts walks off the array
            buckets[max(0, min(int((e - t0) / span * TIMELINE_BUCKETS),
                               TIMELINE_BUCKETS - 1))] += 1
            if prev and e - prev[0] >= IDLE_MIN_S:
                idles.append({"line": line_no, "min": int((e - prev[0]) // 60),
                              "frac": round((prev[0] - t0) / span, 4)})
            prev = (e, line_no)
        idles.sort(key=lambda d: -d["min"])
        timeline = {"t0": self.pts[0][0], "t1": self.pts[-1][0],
                    "span_min": int(span // 60), "buckets": buckets,
                    "idles": idles[:20]}
        return json.dumps(timeline), json.dumps(self.layers)


def _body_row(obj, project, sid, line_no, acc=None):
    """One FTS row per user/assistant line: joined text + facet columns.
    `acc` (optional _ExtrasAcc) receives tool_result text for error layers."""
    from loglearner.gap_detector import ERROR_PATTERNS
    from loglearner.session_scanner import _INJECTED_PREFIXES

    msg_type = obj.get("type")
    if msg_type not in ("user", "assistant"):
        return None
    content = (obj.get("message") or {}).get("content", "")
    day = (obj.get("timestamp") or "")[:10]

    texts, tools, files, errs = [], [], [], []
    blocks = content if isinstance(content, list) else [
        {"type": "text", "text": content}] if isinstance(content, str) else []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            t = b["text"].strip()
            if t and not t[:40].startswith(_INJECTED_PREFIXES):
                texts.append(t[:_BODY_TEXT_CAP])
        elif b.get("type") == "tool_use":
            name = b.get("name", "")
            tools.append(name)
            inp = b.get("input") or {}
            for k in ("file_path", "path", "notebook_path"):
                if inp.get(k):
                    files.append(Path(str(inp[k])).name)
            if acc is not None:
                acc.tools[name] = acc.tools.get(name, 0) + 1
                acc.files.update(files[-3:])
                if name == "Bash" and inp.get("command"):
                    from loglearner.state_vector import bash_signature
                    tok = bash_signature(inp["command"])
                    if tok:
                        acc.bash.add(tok)
        elif b.get("type") == "tool_result":
            rc = b.get("content", "")
            rtexts = ([rc] if isinstance(rc, str) else
                      [x.get("text", "") for x in rc
                       if isinstance(x, dict) and x.get("type") == "text"])
            joined = "\n".join(rtexts)
            if acc is not None and joined:
                acc.see_result_text(joined, line_no)
            for err_name, pattern in ERROR_PATTERNS.items():
                if pattern.search(joined):
                    errs.append(err_name)
                    if acc is not None:
                        acc.errclasses.add(err_name)
                    break
    if not (texts or tools or errs):
        return None
    return (" \n".join(texts)[:_BODY_TEXT_CAP], " ".join(tools),
            " ".join(files[:12]), " ".join(errs), day, project, sid, line_no)


def index_bodies(db):
    """Message-body FTS + byte-offset seek table over PRIMARY sessions
    (KR1.3): incremental by mtime; ~facets: tool / files / error-class /
    day / project as FTS columns for `tool:Bash`-style filters."""
    known = dict(db.execute("SELECT path, mtime FROM body_meta"))
    todo = db.execute(
        "SELECT path, project, session_id, mtime, first_ts, last_ts,"
        " context_overflows, user_corrections FROM sessions"
        " WHERE classification='primary'").fetchall()
    scanned = rows_total = 0
    for path, project, sid, mtime, first_ts, last_ts, n_ovf, n_corr in todo:
        if known.get(path) == mtime:
            continue
        db.execute("DELETE FROM body WHERE session = ?", (sid,))
        db.execute("DELETE FROM seek WHERE path = ?", (path,))
        rows = []
        acc = _ExtrasAcc()
        try:
            with open(path, "rb") as f:
                off, line_no = 0, 0
                for raw in f:
                    line_no += 1
                    if line_no == 1 or line_no % SEEK_EVERY == 0:
                        db.execute("REPLACE INTO seek VALUES (?,?,?)",
                                   (path, line_no, off))
                    off += len(raw)
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("timestamp")
                    if ts:
                        acc.see_ts(ts, line_no)
                    row = _body_row(obj, project, sid, line_no, acc)
                    if row:
                        rows.append(row)
        except OSError:
            continue
        db.executemany("INSERT INTO body VALUES (?,?,?,?,?,?,?,?)", rows)
        tl, el = acc.finish()
        db.execute("REPLACE INTO session_extras VALUES (?,?,?)", (path, tl, el))
        from loglearner.adapters import detect_adapters_for_session
        from loglearner.state_vector import (SessionStateVector,
                                             duration_minutes,
                                             normalize_tool_dist,
                                             outcome_signal)
        vec = SessionStateVector(
            session_id=sid, project=project,
            domains=tuple(detect_adapters_for_session(Path(path))),
            duration_min=duration_minutes(first_ts, last_ts),
            tool_dist=normalize_tool_dist(acc.tools),
            error_classes=frozenset(acc.errclasses),
            file_targets=frozenset(list(acc.files)[:200]),
            bash_signatures=frozenset(list(acc.bash)[:100]),
            outcome_signal=outcome_signal(n_ovf, n_corr),
        )
        db.execute("REPLACE INTO state_vectors VALUES (?,?)", (path, vec.to_json()))
        db.execute("REPLACE INTO body_meta VALUES (?,?)", (path, mtime))
        rows_total += len(rows)
        scanned += 1
        if scanned % 20 == 0:
            db.commit()
            print(f"  ...bodies: {scanned} sessions", flush=True)
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM body").fetchone()[0]
    return scanned, n


def index_search(db):
    db.execute("DELETE FROM search")
    db.executemany(
        "INSERT INTO search VALUES ('session', ?, ?)",
        [(f"{p}/{s}", i) for p, s, i in db.execute(
            "SELECT project, session_id, intent FROM sessions WHERE intent != ''")],
    )
    db.executemany(
        "INSERT INTO search VALUES ('instinct', ?, ?)",
        db.execute("SELECT CAST(id AS TEXT), rule FROM instincts").fetchall(),
    )
    db.executemany(
        "INSERT INTO search VALUES ('learning', ?, ?)",
        db.execute("SELECT file || ':' || lid, rule FROM learnings").fetchall(),
    )
    db.executemany(
        "INSERT INTO search VALUES ('store', ?, ?)",
        db.execute("SELECT id, rule FROM store_instincts WHERE rule != ''").fetchall(),
    )
    db.commit()
    return db.execute("SELECT COUNT(*) FROM search").fetchone()[0]


def first_user_intent(path, max_lines=80):
    """First real user text in the session (the operator's ask)."""
    try:
        with open(path, errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                texts = []
                if isinstance(content, str):
                    texts = [content]
                elif isinstance(content, list):
                    texts = [
                        b.get("text", "")
                        for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                for t in texts:
                    t = t.strip()
                    if t and not t.startswith("<") and "tool_result" not in t[:40]:
                        return t[:280]
    except OSError:
        pass
    return ""


def index_sessions(db, project_filter=None):
    known = dict(db.execute("SELECT path, mtime FROM sessions"))
    projects = scanner.list_projects()
    scanned = skipped = 0
    for proj in projects:
        name = proj["name"]
        if project_filter and project_filter not in name:
            continue
        pdir = scanner.PROJECTS_ROOT / name
        for jpath in sorted(pdir.glob("*.jsonl")):
            st = jpath.stat()
            if known.get(str(jpath)) == st.st_mtime:
                skipped += 1
                continue
            meta = scanner.scan_session(jpath, name)
            intent = (
                first_user_intent(jpath)
                if meta.classification == "primary"
                else ""
            )
            db.execute(
                "REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    meta.session_id, name, str(jpath), st.st_size, st.st_mtime,
                    meta.classification, meta.total_lines, meta.user_messages,
                    meta.assistant_messages, meta.tool_calls,
                    meta.user_corrections, meta.tool_errors,
                    meta.context_overflows, meta.date_range[0],
                    meta.date_range[1],
                    json.dumps(sorted(
                        meta.tool_call_names.items(),
                        key=lambda kv: -kv[1],
                    )[:8]), intent,
                ),
            )
            scanned += 1
            if scanned % 25 == 0:
                db.commit()
                print(f"  ...{scanned} scanned ({name})", flush=True)
    db.commit()
    return scanned, skipped


def index_instincts(db):
    """Load merged instinct libraries (newest unified first, then per-project). When no synthesized library
    exists (the minimal runtime ships none), the installed banks ARE the instinct library, so /instincts and
    /graph render from them instead of staying empty; meta.instincts_source records which mode applied."""
    db.execute("DELETE FROM instincts")
    n = _index_instinct_libraries(db)
    source = "libraries"
    if n == 0:
        for f in bank_files():
            for line in f.open(errors="replace"):
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rule = str(d.get("rule") or "").strip()
                if not rule:
                    continue
                db.execute(
                    "INSERT INTO instincts (rule, category, confidence, merge_count,"
                    " source_file, source_projects, evidence) VALUES (?,?,?,?,?,?,?)",
                    (rule[:600], str(d.get("category") or ""), _conf_int(d.get("confidence")), 1,
                     f"bank:{f.stem}", json.dumps([f"bank:{f.stem}"]),
                     json.dumps([str(d.get("evidence") or "")])),
                )
        n = db.execute("SELECT COUNT(*) FROM instincts").fetchone()[0]
        source = "banks" if n else "none"
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("instincts_source", source))
    db.commit()
    return n


def _index_instinct_libraries(db):
    seen_rules = set()
    lib_root = Path(os.environ.get("LOGLEARNER_INSTINCT_LIBRARIES_DIR", REPO / "output_llm")).expanduser()
    unified = sorted(lib_root.glob("unified*/instincts.json"), reverse=True)
    per_project = sorted(lib_root.glob("*/instincts.json"))
    for src in list(unified[:1]) + [p for p in per_project if "unified" not in str(p)]:
        try:
            items = json.loads(src.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(items, dict):
            items = items.get("instincts", [])
        for it in items:
            rule = it.get("rule", "")
            if not rule or rule in seen_rules:
                continue
            seen_rules.add(rule)
            evidence = it.get("evidence_sources") or (
                [it["evidence"]] if it.get("evidence") else []
            )
            db.execute(
                "INSERT INTO instincts (rule, category, confidence, merge_count,"
                " source_file, source_projects, evidence) VALUES (?,?,?,?,?,?,?)",
                (
                    rule, it.get("category", ""), it.get("confidence", 0),
                    it.get("merge_count", 1),
                    str(src.relative_to(REPO)) if REPO in src.parents else str(src),
                    json.dumps(it.get("source_projects", [])),
                    json.dumps(evidence),
                ),
            )
    db.commit()
    return db.execute("SELECT COUNT(*) FROM instincts").fetchone()[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", help="only index projects containing SUBSTR")
    ap.add_argument("--db", default=str(DB_DEFAULT))
    args = ap.parse_args()

    t0 = time.time()
    db = sqlite3.connect(args.db)
    db.executescript(SCHEMA)
    scanned, skipped = index_sessions(db, args.project)
    n_instincts = index_instincts(db)
    n_learn = index_learnings(db)
    n_store = index_store(db)
    index_store_extras(db)
    n_search = index_search(db)
    b_scanned, n_body = index_bodies(db)
    n_sim, n_edges = index_similarity_and_edges(db)
    db.execute(
        "REPLACE INTO meta VALUES ('last_index', ?)", (time.strftime("%F %T"),)
    )
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    print(
        f"index: {n} sessions ({scanned} scanned, {skipped} unchanged), "
        f"{n_instincts} instincts, {n_learn} learnings, {n_store} store instincts, "
        f"{n_search} search docs, {n_body} body rows ({b_scanned} sessions), "
        f"{n_sim} similarity pairs, {n_edges} edges, "
        f"{time.time() - t0:.1f}s -> {args.db}"
    )




def index_similarity_and_edges(db):
    """Top-k session similarity + the deterministic
    edge set — all derivable today, no LLM. Rebuilt every run (cheap)."""
    from loglearner.state_vector import SessionStateVector, similarity

    db.execute("DELETE FROM sessions_similarity")
    db.execute("DELETE FROM edges")
    db.execute("DELETE FROM arms")
    db.execute("DELETE FROM observations")
    now = time.strftime("%FT%TZ", time.gmtime())

    def edge(sk, si, dk, di, kind, w, src):
        db.execute("INSERT OR REPLACE INTO edges VALUES (?,?,?,?,?,?,?,?)",
                   (sk, si, dk, di, kind, round(w, 4), src[:200], now))

    vecs = []
    for path, vj in db.execute("SELECT path, vec FROM state_vectors"):
        try:
            vecs.append(SessionStateVector.from_json(vj))
        except (json.JSONDecodeError, TypeError):
            continue

    # top-k=10 neighbours per session + typed session<->session edges
    TOPK, n_sim = 10, 0
    for i, a in enumerate(vecs):
        scored = []
        for j, b in enumerate(vecs):
            if i == j:
                continue
            score, comp = similarity(a, b)
            scored.append((score, b.session_id, comp))
        scored.sort(reverse=True)
        for score, bsid, comp in scored[:TOPK]:
            if score < 0.15:
                break
            db.execute("INSERT OR REPLACE INTO sessions_similarity"
                       " VALUES (?,?,?,?)",
                       (a.session_id, bsid, score, json.dumps(comp)))
            n_sim += 1
            if comp["files"] >= 0.2:
                edge("session", a.session_id, "session", bsid,
                     "shares_file", comp["files"], "state_vector")
            if comp["errors"] > 0:
                edge("session", a.session_id, "session", bsid,
                     "shares_error", comp["errors"], "state_vector")
            if comp["shared_bash"] >= 3:
                edge("session", a.session_id, "session", bsid,
                     "shares_bash_root", comp["shared_bash"], "state_vector")
            if comp["domains"] > 0:
                edge("session", a.session_id, "session", bsid,
                     "same_domain", comp["domains"], "state_vector")

    # temporal_adjacent: same project, next session starts <5 min after prev ends
    rows = db.execute("SELECT project, session_id, first_ts, last_ts FROM"
                      " sessions WHERE classification='primary'"
                      " AND last_ts != '' ORDER BY project, first_ts").fetchall()
    from loglearner.state_vector import duration_minutes
    for k in range(1, len(rows)):
        p0, s0, _, e0 = rows[k - 1]
        p1, s1, f1, _ = rows[k]
        if p0 == p1 and e0 and f1 and 0 <= duration_minutes(e0, f1) <= 5:
            edge("session", s0, "session", s1, "temporal_adjacent", 1.0,
                 "sessions.first_ts/last_ts")

    # evidences + contains: instinct evidence resolved EXACTLY at index time
    # (kills the fragile 8-char prefix join in the dashboard)
    ev_re = re.compile(r"session:([0-9a-f-]{8,40})\s+lines:(\d+)-(\d+)")
    sid_exact = {r[0] for r in db.execute("SELECT session_id FROM sessions")}
    sid_by_prefix = {}
    for (s,) in db.execute("SELECT session_id FROM sessions"):
        sid_by_prefix.setdefault(s[:8], []).append(s)
    for iid, evidence in db.execute("SELECT id, evidence FROM instincts"):
        for ev in json.loads(evidence or "[]"):
            m = ev_re.search(ev or "")
            if not m:
                continue
            sid, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            if sid not in sid_exact:
                hits = sid_by_prefix.get(sid[:8], [])
                if len(hits) != 1:
                    continue  # ambiguous or unknown — no guessing
                sid = hits[0]
            gap_id = f"{sid}:{a}-{b}"
            edge("instinct", str(iid), "session", sid, "evidences", 1.0, ev)
            edge("session", sid, "gap", gap_id, "contains", 1.0, ev)
            edge("gap", gap_id, "instinct", str(iid), "synthesized", 1.0, ev)

    # tracked_as / evaluated_by + arms/observations from the E9 lane
    arms_f = REPO / "output" / "loglearner_arms.jsonl"
    if arms_f.is_file():
        for line in arms_f.open():
            try:
                a = json.loads(line)
            except json.JSONDecodeError:
                continue
            db.execute("INSERT OR REPLACE INTO arms VALUES (?,?,?,?,?,?,?,?)",
                       (a["arm"], a.get("rule", "")[:80], a.get("action", ""),
                        0, 0.0, 0.0, a.get("mint_ts", ""), a.get("mint_ts", "")))
            edge("instinct", a.get("rule", "")[:80], "arm", a["arm"],
                 "tracked_as", 1.0, "loglearner_arms.jsonl")
    rew_f = REPO / "output" / "loglearner_rewards.jsonl"
    if rew_f.is_file():
        for line in rew_f.open():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)",
                       (o["obs_id"], o["arm"], ";".join(o.get("matched_sessions", [])[:3]),
                        o.get("reward"), o.get("regret"), o.get("notes", ""), o.get("ts", "")))
            db.execute("UPDATE arms SET pulls = pulls + 1,"
                       " reward_sum = reward_sum + ?, regret_sum = regret_sum + ?,"
                       " last_seen = ? WHERE arm_id = ?",
                       (o.get("reward") or 0, o.get("regret") or 0,
                        o.get("ts", ""), o["arm"]))
            for msid in o.get("matched_sessions", [])[:5]:
                edge("arm", o["arm"], "session", msid, "evaluated_by",
                     o.get("reward") or 0, "loglearner_rewards.jsonl")

    db.commit()
    n_edges = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    return n_sim, n_edges




def index_store_extras(db):
    """Stores that live OUTSIDE knowledge_base/ — evaluation
    sqlites, the session-learning spine, the measurement lane — plus the
    mining-catalog coverage ratings. All read-only."""
    root = KNOWLEDGE_KB.parent  # reference-flywheel/
    db.execute("DELETE FROM store_session_events")
    db.execute("DELETE FROM store_coverage")
    if not root.is_dir():
        return 0

    def put_store(store, kind, records, sample_keys, path, lanes=""):
        db.execute("REPLACE INTO store_inventory VALUES (?,?,?,?,?,?)",
                   (store, kind, records, json.dumps(sample_keys[:14]),
                    str(path), lanes))

    # sqlite renderers: evaluation/candidates.sqlite + pr_intelligence.sqlite
    for rel in ("evaluation/candidates.sqlite",
                "evaluation/pr_intelligence.sqlite"):
        f = root / rel
        if not f.exists():
            continue
        total, tables = 0, []
        try:
            sq = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
            for (t,) in sq.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"):
                n = sq.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                total += n
                tables.append(f"{t}:{n}")
            sq.close()
        except sqlite3.Error:
            pass
        put_store(rel, "sqlite", total, tables, f)

    # session-learning spine -> full rows (5.6k, small) for the browser join
    spine = root / "analysis" / "session_learning" / "session_events.jsonl"
    n_spine = 0
    if spine.is_file():
        for line in spine.open(errors="replace"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            db.execute("INSERT INTO store_session_events VALUES (?,?,?,?,?)",
                       (d.get("ts", ""), d.get("source", ""),
                        d.get("session_id", ""), d.get("event_type", ""),
                        str(d.get("evidence_ref", ""))[:300]))
            n_spine += 1
        put_store("analysis/session_learning/session_events", "spine",
                  n_spine, ["ts", "source", "session_id", "event_type",
                            "evidence_ref"], spine)

    # measurement lane: drift_index fully counted; raw_measurements is
    # 133k+ rows -> aggregate ONLY (count + per-unit histogram), never full
    drift = KNOWLEDGE_KB / "measurement_reconciliation" / "drift_index.jsonl"
    if drift.is_file():
        rows = [json.loads(l) for l in drift.open(errors="replace")
                if l.strip()]
        top = sorted(rows, key=lambda d: -(d.get("divergence_pct") or 0))[:5]
        put_store("measurement_reconciliation/drift_index", "measurement",
                  len(rows),
                  [f"{d.get('experiment_key', '?')}:{d.get('divergence_pct')}%"
                   for d in top], drift)
    raw = root / "analysis" / "measurement_reconciliation" / "raw_measurements.jsonl"
    if raw.is_file():
        n_raw, units = 0, {}
        for line in raw.open(errors="replace"):
            n_raw += 1
            m = re.search(r'"unit"\s*:\s*"([^"]{1,12})"', line)
            if m:
                units[m.group(1)] = units.get(m.group(1), 0) + 1
        put_store("analysis/measurement_reconciliation/raw_measurements",
                  "measurement", n_raw,
                  [f"{u}:{n}" for u, n in
                   sorted(units.items(), key=lambda kv: -kv[1])[:10]], raw,
                  lanes=json.dumps({"aggregated_only": True}))

    # coverage ratings from mining_catalog.md (GREEN/YELLOW/RED emojis)
    catalog = KNOWLEDGE_KB / "mining_catalog.md"
    n_cov = 0
    if catalog.is_file():
        section = ""
        rating_by_marker = {"\U0001f7e2": "GREEN", "\U0001f7e1": "YELLOW",
                            "\U0001f534": "RED"}
        cur_rating = ""
        for line in catalog.read_text(errors="replace").splitlines():
            for marker, rating in rating_by_marker.items():
                if marker in line and ("##" in line or "**" in line):
                    cur_rating, section = rating, line.strip("#* ")[:60]
            m = re.match(r"^\s*\d+\.\s+(.+)$", line)
            if m and cur_rating:
                item = re.sub(r"[`*]", "", m.group(1)).strip()[:100]
                db.execute("INSERT INTO store_coverage VALUES (?,?,?)",
                           (item, cur_rating, section))
                n_cov += 1
    db.commit()
    return n_spine + n_cov


if __name__ == "__main__":
    main()
