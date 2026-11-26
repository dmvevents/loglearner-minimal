"""/api/tile.json — the Log Learner + Meta Learner tile contract. A missing data source must turn the
tile RED (ok:false + error + status:degraded), never blank; numbers must equal the index's own counts."""
import json
import sqlite3
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dashboard import app  # noqa: E402
from dashboard import indexer  # noqa: E402

BANK_ROW = {"id": "p-1", "rule": "r", "status": "positive", "lifecycle_state": "screened", "meta": {"arm": "ll-1_apply"}}


def make_index(tmp_path):
    db = tmp_path / "index.db"
    c = sqlite3.connect(db)
    c.executescript(indexer.SCHEMA)
    c.execute("INSERT INTO sessions(session_id, project, path, size_bytes, classification, total_lines, user_messages,"
              " assistant_messages, tool_calls, user_corrections, tool_errors, context_overflows, first_ts, last_ts)"
              " VALUES ('s1','proj','/x/s1.jsonl',1000,'primary',10,2,2,7,1,3,0,'2026-01-01','2026-01-02')")
    c.execute("INSERT INTO sessions(session_id, project, path, size_bytes, classification, total_lines, user_messages,"
              " assistant_messages, tool_calls, user_corrections, tool_errors, context_overflows, first_ts, last_ts)"
              " VALUES ('s2','proj','/x/s2.jsonl',500,'observer',5,0,0,0,0,0,0,'2026-01-01','2026-01-02')")
    c.execute("REPLACE INTO meta VALUES ('last_index','2026-10-13 00:00:00')")
    c.commit(); c.close()
    return db


def test_tile_equals_index_counts_and_is_ok_when_sources_present(tmp_path):
    db = make_index(tmp_path)
    banks = tmp_path / "banks" / "global"; banks.mkdir(parents=True)
    (banks / "instincts.jsonl").write_text(json.dumps(BANK_ROW) + "\n" + json.dumps({**BANK_ROW, "id": "p-2", "status": "negative", "lifecycle_state": "contested"}) + "\n")
    t = app.api_tile(db_path=db, banks_dir=tmp_path / "banks", ucb_dir=tmp_path / "ucb")
    c = sqlite3.connect(db)
    assert t["status"] == "ok" and t["errors"] == []
    assert t["log_learner"]["sessions"] == c.execute("select count(*) from sessions").fetchone()[0] == 2
    assert t["log_learner"]["primary"] == 1 and t["log_learner"]["tool_calls"] == 7
    assert t["log_learner"]["gaps"] == {"corrections": 1, "tool_errors": 3}
    assert t["meta_learner"]["instincts_total"] == 2
    assert t["meta_learner"]["by_status"] == {"positive": 1, "negative": 1}
    assert t["meta_learner"]["lifecycle"] == {"screened": 1, "contested": 1}
    assert t["meta_learner"]["bandit"]["note"].startswith("no ledgers")
    assert t["schema"] == "loglearner-tile/v1"


def test_missing_index_turns_red_not_blank(tmp_path):
    banks = tmp_path / "banks"; banks.mkdir(); (banks / "b.jsonl").write_text(json.dumps(BANK_ROW) + "\n")
    t = app.api_tile(db_path=tmp_path / "nope.db", banks_dir=banks, ucb_dir=tmp_path / "ucb")
    assert t["status"] == "degraded"
    assert t["log_learner"]["ok"] is False and "index not found" in t["log_learner"]["error"]
    assert t["meta_learner"]["ok"] is True
    assert any(e.startswith("log_learner:") for e in t["errors"])


def test_missing_banks_turns_red_not_blank(tmp_path):
    db = make_index(tmp_path)
    t = app.api_tile(db_path=db, banks_dir=tmp_path / "no-banks", ucb_dir=tmp_path / "ucb")
    assert t["status"] == "degraded" and t["meta_learner"]["ok"] is False
    assert "no bank files" in t["meta_learner"]["error"] and t["log_learner"]["ok"] is True


def test_bandit_ledgers_are_counted(tmp_path):
    db = make_index(tmp_path)
    banks = tmp_path / "banks"; banks.mkdir(); (banks / "b.jsonl").write_text(json.dumps(BANK_ROW) + "\n")
    ucb = tmp_path / "ucb"; ucb.mkdir()
    (ucb / "loglearner_arms.jsonl").write_text(json.dumps({"arm": "a"}) + "\n")
    (ucb / "loglearner_exposures.jsonl").write_text(
        json.dumps({"session_id": "abcdef12-x", "arm": "a", "ts": "2999-01-01T00:00:00Z"}) + "\n"
        + json.dumps({"session_id": "abcdef12-x", "arm": "a", "ts": "2000-01-01T00:00:00Z"}) + "\n")
    t = app.api_tile(db_path=db, banks_dir=banks, ucb_dir=ucb)
    b = t["meta_learner"]["bandit"]
    assert b["arms"] == 1 and b["exposures"] == 2 and b["exposures_24h"] == 1
    assert b["deliveries_by_session"] == {"abcdef12": 2} and b["note"] == ""


def test_http_endpoint_serves_json_200_even_when_degraded(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "missing.db")
    monkeypatch.setattr(app, "BANKS_DIR", tmp_path / "missing-banks")
    monkeypatch.setattr(app, "UCB_DIR", tmp_path / "ucb")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/api/tile.json", timeout=10) as r:
            assert r.status == 200 and r.headers["Content-Type"] == "application/json"
            body = json.loads(r.read())
        assert body["status"] == "degraded" and body["log_learner"]["ok"] is False and body["meta_learner"]["ok"] is False
    finally:
        srv.shutdown(); srv.server_close()
