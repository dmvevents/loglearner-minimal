"""Knowledge packs — idempotent ingest of a curated default knowledge pack into a local bank.

A pack is a JSONL (or JSON list) of records in EITHER
  * the reference instinct schema this project's banks use
      {id, rule, evidence, confidence, source, provenance, status, meta{arm, reward, cross_ref}}, or
  * the curator's simple schema (aws-do-agent K1, `knowledge/defaults/aws-basics.jsonl`)
      {id, title, text, category, tags, confidence, source}.
Simple records are converted to reference on ingest: rule = text, meta.arm = content hash of the rule
(`arm_id_for_rule`), lifecycle_state = "screened" (servable), polarity from tags/wording.

Idempotency: the pack file's sha256 is recorded in `<bank_dir>/.installed-packs.json` together with
(name, version); re-ingesting the same bytes is a no-op (exit 0, nothing written). Within a pack,
records are deduplicated against the bank by `meta.arm` (same content → skipped) and by `id` (same id,
new content → the bank row is replaced and a lifecycle_history entry records the change — never a
silent overwrite). Bank rows that did not come from the pack are never touched. An optional
`manifest.json` ({"sha256": ...} or {"files":[{"path","sha256"}]}) is verified fail-closed.

CLI:
  python3 -m loglearner.pack ingest --pack P.jsonl --bank ~/.loglearner/banks/global/instincts.jsonl [--name N --version V] [--manifest M]
  python3 -m loglearner.pack verify --pack P.jsonl --manifest M
  python3 -m loglearner.pack installed --bank ~/.loglearner/banks/global/instincts.jsonl
Exit codes: 0 ok/no-op · 2 refused (manifest mismatch, unreadable pack) · 3 bad record.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from loglearner.arms import arm_id_for_rule
from loglearner.lifecycle import mark

INSTALLED_FILE = ".installed-packs.json"
_NEG = re.compile(r"^\s*(never|do not|don'?t|avoid|stop|refuse|banlist|negative)\b", re.I)


class PackRefused(Exception):
    """Fail-closed refusal; str() is safe to show (no record contents)."""


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_records(path):
    text = Path(path).read_text(errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise PackRefused("pack JSON must be a list of records")
        return [r for r in data if isinstance(r, dict)]
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            raise PackRefused(f"pack line {n} is not JSON: {e.msg}") from None
        if isinstance(r, dict):
            rows.append(r)
    return rows


def detect_format(rec):
    if rec.get("rule"):
        return "reference"
    if rec.get("text") or rec.get("title"):
        return "simple"
    return "unknown"


def _confidence(v):
    if isinstance(v, bool) or v is None:
        return "med"
    if isinstance(v, (int, float)):
        x = float(v)
        if x <= 1.0:
            return "high" if x >= 0.8 else "med" if x >= 0.5 else "low"
        return "high" if x >= 8 else "med" if x >= 5 else "low"
    s = str(v).strip().lower()
    return {"high": "high", "h": "high", "medium": "med", "med": "med", "m": "med",
            "low": "low", "l": "low"}.get(s, "med")


def to_instinct_row(rec, pack_name, pack_version, pack_sha, now=None):
    """Convert a record of either schema to a servable reference row. Raises PackRefused on bad records."""
    now = now or _now()
    fmt = detect_format(rec)
    if fmt == "unknown":
        raise PackRefused(f"record {rec.get('id', '?')!r} has neither 'rule' nor 'text'")
    if fmt == "reference":
        rule = str(rec["rule"]).strip()
        out = {k: v for k, v in rec.items() if k not in ("lifecycle_state", "lifecycle_history", "pack")}
        tags = list(rec.get("tags") or [])
    else:
        rule = str(rec.get("text") or "").strip()
        title = str(rec.get("title") or "").strip()
        tags = list(rec.get("tags") or [])
        if not rule:
            raise PackRefused(f"record {rec.get('id', '?')!r} has an empty text")
        out = {"rule": rule, "title": title,
               "evidence": f"pack:{pack_name}@{pack_version}; source: {rec.get('source') or 'n/a'}",
               "source": str(rec.get("source") or f"pack:{pack_name}"),
               "provenance": f"default knowledge pack {pack_name}@{pack_version} (curated; converted on ingest)",
               "category": rec.get("category") or "", "tags": tags}
        for k in ("version_scope", "as_of", "source_type", "model_likely_knows"):  # applicability + provenance metadata
            if rec.get(k) not in (None, ""):
                out[k] = rec[k]
    rid = str(rec.get("id") or "").strip()
    if not rid:
        rid = f"{pack_name}-{arm_id_for_rule(rule)[3:13]}"
    out["id"] = rid
    neg = any(str(t).lower() in ("negative", "banlist", "anti-pattern", "never") for t in tags) \
        or str(rec.get("status", "")).lower() == "negative" or bool(_NEG.match(rule))
    out["status"] = "negative" if neg else "positive"
    out["confidence"] = _confidence(rec.get("confidence"))
    meta = dict(out.get("meta") or {})
    meta["arm"] = arm_id_for_rule(rule)
    meta.setdefault("reward", 0 if neg else 1)       # bank polarity field, not an outcome reward
    meta.setdefault("regret", 1 if neg else 0)
    meta.setdefault("cross_ref", [])
    out["meta"] = meta
    out["pack"] = {"name": pack_name, "version": pack_version, "sha256": pack_sha, "ingested_at": now}
    mark(out, "screened", f"pack:{pack_name}@{pack_version}", "ingested from a curated default knowledge pack")
    return out


def verify_manifest(pack_path, manifest_path):
    """Return the pack sha256 if the manifest agrees; raise PackRefused otherwise."""
    actual = sha256_file(pack_path)
    if not Path(manifest_path).is_file():
        raise PackRefused(f"manifest not found: {manifest_path}")
    try:
        m = json.loads(Path(manifest_path).read_text())
    except json.JSONDecodeError as e:
        raise PackRefused(f"manifest is not JSON: {e.msg}") from None
    expected = None
    if isinstance(m, dict):
        if isinstance(m.get("sha256"), str):
            expected = m["sha256"]
        elif isinstance(m.get("files"), list):
            name = Path(pack_path).name
            for f in m["files"]:
                if isinstance(f, dict) and Path(str(f.get("path") or f.get("dest") or "")).name == name:
                    expected = f.get("sha256")
    if not expected:
        raise PackRefused("manifest carries no sha256 for the pack")
    if expected.lower() != actual:
        raise PackRefused("pack sha256 does not match its manifest — refusing to ingest")
    return actual


def _read_bank(bank_path):
    if not Path(bank_path).is_file():
        return []
    rows = []
    for line in Path(bank_path).read_text(errors="replace").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_atomic(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    os.replace(tmp, path)


def installed(bank_path):
    p = Path(bank_path).parent / INSTALLED_FILE
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []


def ingest(pack_path, bank_path, name=None, version=None, manifest_path=None, now=None):
    """Idempotent ingest. Returns a summary dict; raises PackRefused (fail-closed)."""
    pack_path, bank_path = Path(pack_path), Path(bank_path)
    if not pack_path.is_file():
        raise PackRefused(f"pack not found: {pack_path}")
    sha = verify_manifest(pack_path, manifest_path) if manifest_path else sha256_file(pack_path)
    name = name or pack_path.stem
    version = version or "v1"
    now = now or _now()
    prior = installed(bank_path)
    if any(e.get("sha256") == sha and e.get("name") == name and e.get("version") == version for e in prior):
        return {"noop": True, "added": 0, "updated": 0, "skipped": 0, "total": len(_read_bank(bank_path)),
                "sha256": sha, "name": name, "version": version}
    recs = load_records(pack_path)
    rows = _read_bank(bank_path)
    by_arm = {str((r.get("meta") or {}).get("arm")): i for i, r in enumerate(rows) if (r.get("meta") or {}).get("arm")}
    by_id = {str(r.get("id")): i for i, r in enumerate(rows) if r.get("id")}
    added = updated = skipped = 0
    for rec in recs:
        row = to_instinct_row(rec, name, version, sha, now)
        arm = row["meta"]["arm"]
        if arm in by_arm:
            skipped += 1
            continue
        if row["id"] in by_id:
            i = by_id[row["id"]]
            old = rows[i]
            row["lifecycle_history"] = list(old.get("lifecycle_history") or []) + [{
                "state": "screened", "by": f"pack:{name}@{version}",
                "reason": f"record updated by pack ingest (previous arm {str((old.get('meta') or {}).get('arm'))[:16]})",
                "at": now}]
            rows[i] = row
            by_arm[arm] = i
            updated += 1
            continue
        rows.append(row)
        by_arm[arm] = len(rows) - 1
        by_id[row["id"]] = len(rows) - 1
        added += 1
    _write_atomic(bank_path, rows)
    entry = {"name": name, "version": version, "sha256": sha, "pack": str(pack_path), "at": now,
             "added": added, "updated": updated, "skipped": skipped}
    inst_path = bank_path.parent / INSTALLED_FILE
    tmp = inst_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(prior + [entry], indent=1) + "\n")
    os.replace(tmp, inst_path)
    return {"noop": False, "added": added, "updated": updated, "skipped": skipped, "total": len(rows),
            "sha256": sha, "name": name, "version": version}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="loglearner.pack", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ingest"); a.add_argument("--pack", required=True); a.add_argument("--bank", required=True)
    a.add_argument("--name"); a.add_argument("--version"); a.add_argument("--manifest")
    v = sub.add_parser("verify"); v.add_argument("--pack", required=True); v.add_argument("--manifest", required=True)
    s = sub.add_parser("installed"); s.add_argument("--bank", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ingest":
            out = ingest(args.pack, args.bank, args.name, args.version, args.manifest)
        elif args.cmd == "verify":
            out = {"ok": True, "sha256": verify_manifest(args.pack, args.manifest)}
        else:
            out = {"installed": installed(args.bank)}
    except (PackRefused, FileNotFoundError, json.JSONDecodeError) as e:
        print(json.dumps({"refused": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
