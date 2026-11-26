"""The index must never read 'database is locked' to the dashboard while the indexer rebuilds it.

Found by a harness running the periodic refresh: index.db was in rollback-journal mode and the similarity/edges
rebuild held the EXCLUSIVE lock for most of every cycle, so /api/tile.json went degraded and the tile read Down.
Three tests fail on 0.1.12, and a fourth guards that a lock with no good read still turns red: (1) the indexer leaves the file in WAL mode, so a mode=ro reader reads beside an open
write transaction; (2) a transient lock on the tile's read serves the last good counts with a note, status ok;
(3) --skip-similarity-when-unchanged keeps the pairs/edges when nothing changed. Hermetic: HOME and every
LOGLEARNER_* knob under tmp_path, the indexer run in a subprocess."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from dashboard import app  # noqa: E402
from dashboard import indexer  # noqa: E402

from test_tile_api import BANK_ROW, make_index  # noqa: E402


def _run_indexer(tmp_path, db, *extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOGLEARNER_")}
    env.update(HOME=str(tmp_path / "home"), LOGLEARNER_DB=str(db),
               LOGLEARNER_BANKS_DIR=str(tmp_path / "banks"),
               LOGLEARNER_INSTINCT_LIBRARIES_DIR=str(tmp_path / "libs"))
    (tmp_path / "home").mkdir(exist_ok=True)
    r = subprocess.run([sys.executable, str(REPO / "dashboard" / "indexer.py"), "--db", str(db), *extra],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


def test_indexer_leaves_wal_so_readers_read_beside_a_writer(tmp_path):
    db = tmp_path / "index.db"
    _run_indexer(tmp_path, db)
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("REPLACE INTO meta VALUES ('probe', 'uncommitted')")
    try:
        reader = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0  # no 'database is locked'
        assert reader.execute("SELECT value FROM meta WHERE key='probe'").fetchone() is None
        reader.close()
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_tile_serves_last_good_counts_through_a_transient_lock(tmp_path, monkeypatch):
    db = make_index(tmp_path)
    banks = tmp_path / "banks"; banks.mkdir()
    (banks / "b.jsonl").write_text(__import__("json").dumps(BANK_ROW) + "\n")
    monkeypatch.setattr(app, "_TILE_LAST_GOOD", {"ll": None, "at": None, "locked_since": None}, raising=False)
    good = app.api_tile(db_path=db, banks_dir=banks, ucb_dir=tmp_path / "ucb")
    assert good["status"] == "ok" and good["log_learner"]["sessions"] == 2

    real_connect = sqlite3.connect

    def locked(target, *a, **kw):
        if str(target).startswith("file:") and str(db) in str(target):
            raise sqlite3.OperationalError("database is locked")
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(app.sqlite3, "connect", locked)
    t = app.api_tile(db_path=db, banks_dir=banks, ucb_dir=tmp_path / "ucb")
    assert t["status"] == "ok" and t["errors"] == []
    assert t["log_learner"]["sessions"] == 2 and t["log_learner"]["stale"] is True
    assert "index refresh in progress" in t["log_learner"]["note"]


def test_tile_lock_without_a_good_read_still_turns_red(tmp_path, monkeypatch):
    db = make_index(tmp_path)
    banks = tmp_path / "banks"; banks.mkdir()
    (banks / "b.jsonl").write_text(__import__("json").dumps(BANK_ROW) + "\n")
    monkeypatch.setattr(app, "_TILE_LAST_GOOD", {"ll": None, "at": None, "locked_since": None}, raising=False)

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(app.sqlite3, "connect", locked)
    t = app.api_tile(db_path=db, banks_dir=banks, ucb_dir=tmp_path / "ucb")
    assert t["status"] == "degraded" and t["log_learner"]["ok"] is False


def test_skip_similarity_when_unchanged_keeps_pairs_and_edges(tmp_path):
    db = tmp_path / "index.db"
    _run_indexer(tmp_path, db)
    out = _run_indexer(tmp_path, db, "--skip-similarity-when-unchanged")
    assert "similarity/edges kept from the previous run" in out
    c = sqlite3.connect(db)
    assert c.execute("SELECT value FROM meta WHERE key='last_index_sig'").fetchone() is not None
    c.close()
