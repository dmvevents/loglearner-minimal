"""loglearner.pack — idempotent ingest of a curated knowledge pack (aws-do-agent K1 schema or reference)."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loglearner import pack  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "sample-pack.jsonl"


def rows(bank):
    return [json.loads(l) for l in Path(bank).read_text().splitlines() if l.strip()]


def test_simple_schema_converts_to_instinct_row_and_is_servable(tmp_path):
    bank = tmp_path / "banks" / "global" / "instincts.jsonl"
    out = pack.ingest(FIX, bank, name="aws-basics", version="v1")
    assert out["added"] == 3 and out["updated"] == 0 and out["skipped"] == 0 and not out["noop"]
    r = {x["id"]: x for x in rows(bank)}
    a = r["aws-basics-001"]
    assert a["rule"].startswith("Bind every local web UI") and a["title"] == "Bind local services to loopback"
    assert a["meta"]["arm"].startswith("ll-") and a["meta"]["arm"].endswith("_apply")
    assert a["lifecycle_state"] == "screened" and a["lifecycle_history"][0]["by"] == "pack:aws-basics@v1"
    assert a["status"] == "positive" and a["confidence"] == "high"
    assert a["pack"] == {"name": "aws-basics", "version": "v1", "sha256": pack.sha256_file(FIX),
                         "ingested_at": a["pack"]["ingested_at"]}
    assert r["aws-basics-003"]["status"] == "negative" and r["aws-basics-003"]["meta"]["reward"] == 0  # 'Never ...' + tag
    assert r["aws-basics-002"]["category"] == "ec2-capacity"


def test_reingest_same_bytes_is_a_noop(tmp_path):
    bank = tmp_path / "b" / "instincts.jsonl"
    pack.ingest(FIX, bank, name="aws-basics", version="v1")
    before = bank.read_bytes()
    out = pack.ingest(FIX, bank, name="aws-basics", version="v1")
    assert out["noop"] and bank.read_bytes() == before
    inst = pack.installed(bank)
    assert len(inst) == 1 and inst[0]["sha256"] == pack.sha256_file(FIX)


def test_changed_record_updates_in_place_with_history_and_local_rows_untouched(tmp_path):
    bank = tmp_path / "b" / "instincts.jsonl"
    bank.parent.mkdir(parents=True)
    local = {"id": "local-1", "rule": "my own project lesson", "status": "positive", "confidence": "high",
             "meta": {"arm": "ll-local00001_apply", "reward": 1}}
    bank.write_text(json.dumps(local) + "\n")
    pack.ingest(FIX, bank, name="aws-basics", version="v1")
    v2 = tmp_path / "pack-v2.jsonl"
    recs = [json.loads(l) for l in FIX.read_text().splitlines()]
    recs[0]["text"] = recs[0]["text"] + " Rotate the tunnel key regularly."
    v2.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    out = pack.ingest(v2, bank, name="aws-basics", version="v2")
    assert out["updated"] == 1 and out["skipped"] == 2 and out["added"] == 0
    r = {x["id"]: x for x in rows(bank)}
    assert len(r) == 4 and r["local-1"] == local                      # untouched, not even re-serialized differently
    assert r["aws-basics-001"]["rule"].endswith("Rotate the tunnel key regularly.")
    hist = r["aws-basics-001"]["lifecycle_history"]
    assert len(hist) == 2 and "updated by pack ingest" in hist[-1]["reason"]
    assert len(pack.installed(bank)) == 2


def test_manifest_mismatch_refuses_and_leaves_bank_untouched(tmp_path):
    bank = tmp_path / "b" / "instincts.jsonl"
    pack.ingest(FIX, bank, name="aws-basics", version="v1")
    before = bank.read_bytes()
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps({"sha256": "0" * 64}))
    with pytest.raises(pack.PackRefused):
        pack.ingest(FIX, bank, name="aws-basics", version="v2", manifest_path=bad)
    assert bank.read_bytes() == before
    good = tmp_path / "manifest-ok.json"
    good.write_text(json.dumps({"files": [{"path": "sample-pack.jsonl", "sha256": pack.sha256_file(FIX)}]}))
    assert pack.verify_manifest(FIX, good) == pack.sha256_file(FIX)


def test_reference_schema_records_ingest_unchanged_in_meaning(tmp_path):
    bank = tmp_path / "b" / "instincts.jsonl"
    p = tmp_path / "reference.jsonl"
    p.write_text(json.dumps({"id": "x-1", "rule": "Verify binary freshness after a rebuild.", "evidence": "e",
                             "confidence": "high", "source": "s", "provenance": "p", "status": "positive",
                             "meta": {"arm": "ll-deadbeef00_apply", "reward": 1, "cross_ref": ["y-9"]}}) + "\n")
    pack.ingest(p, bank, name="reference-sample", version="v1")
    r = rows(bank)[0]
    assert r["meta"]["arm"] == pack.arm_id_for_rule(r["rule"])      # content hash recomputed, not trusted
    assert r["meta"]["cross_ref"] == ["y-9"] and r["lifecycle_state"] == "screened"


def test_bad_record_is_refused(tmp_path):
    p = tmp_path / "bad.jsonl"; p.write_text(json.dumps({"id": "no-text", "category": "x"}) + "\n")
    with pytest.raises(pack.PackRefused):
        pack.ingest(p, tmp_path / "b" / "i.jsonl")


def test_cli_roundtrip(tmp_path, capsys):
    bank = tmp_path / "b" / "instincts.jsonl"
    assert pack.main(["ingest", "--pack", str(FIX), "--bank", str(bank), "--name", "aws-basics", "--version", "v1"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == 3
    assert pack.main(["installed", "--bank", str(bank)]) == 0
    assert pack.main(["verify", "--pack", str(FIX), "--manifest", str(tmp_path / "missing.json")]) == 2
    good = tmp_path / "m.json"; good.write_text(json.dumps({"sha256": pack.sha256_file(FIX)}))
    assert pack.main(["verify", "--pack", str(FIX), "--manifest", str(good)]) == 0
