"""The sanctioned write path into a knowledge bank:

    python3 -m loglearner.cli bank --json '<entry>'      (or --stdin)

Modelled on the fleet's bank.sh rules: a schema gate (required fields and enums), recall-before-append (the
nearest existing records are echoed), near-duplicate rejection (same content hash, or token Jaccard >= 0.8
unless --force), stable union-max id allocation across every bank file, and an append-only write into the
global bank, which the Meta Learner tile counts immediately. Nothing here assigns a reward: meta.reward is the
status polarity (1 positive / 0 negative), never an outcome.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from loglearner.arms import arm_id_for_rule
from loglearner.lifecycle import mark
from loglearner.recall import banks_dir, load_rows, store_files

REQUIRED = ("rule", "evidence", "confidence", "source", "provenance", "status")
OPTIONAL = ("id", "title", "category", "tags", "cross_ref", "corrects", "version_scope", "as_of")
CONFIDENCE = ("high", "med", "low")
STATUS = ("positive", "negative")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
JACCARD_DUP = 0.8


class BankRefused(Exception):
    """Schema or duplicate refusal; str() lists every problem."""


def default_bank_path():
    return Path(os.environ.get("LOGLEARNER_BANK", banks_dir() / "global" / "instincts.jsonl")).expanduser()


def _confidence(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return "high" if v >= 8 else "med" if v >= 5 else "low"
    v = str(v or "").lower().strip()
    return {"medium": "med"}.get(v, v) if {"medium": "med"}.get(v, v) in CONFIDENCE else None


def validate(entry):
    """Return a normalized copy or raise BankRefused listing every problem (never the first one only)."""
    problems = []
    if not isinstance(entry, dict):
        raise BankRefused("entry must be a JSON object")
    unknown = sorted(k for k in entry if k not in REQUIRED + OPTIONAL)
    if unknown:
        problems.append(f"unknown field(s) {unknown}; allowed: {list(REQUIRED + OPTIONAL)}")
    for k in REQUIRED:
        if k == "confidence":
            continue
        if not str(entry.get(k) or "").strip():
            problems.append(f"missing required field {k!r}")
    conf = _confidence(entry.get("confidence"))
    if conf is None:
        problems.append(f"confidence must be one of {CONFIDENCE} (or an int 1-10), got {entry.get('confidence')!r}")
    if str(entry.get("status") or "").lower() not in STATUS:
        problems.append(f"status must be one of {STATUS}, got {entry.get('status')!r}")
    for k in ("tags", "cross_ref", "corrects"):
        v = entry.get(k)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            problems.append(f"{k} must be a list of strings")
    if entry.get("id") is not None and not ID_RE.match(str(entry["id"])):
        problems.append(f"id {entry['id']!r} must match {ID_RE.pattern}")
    if len(str(entry.get("title") or "")) > 80:
        problems.append("title must be <= 80 chars")
    if len(str(entry.get("rule") or "")) > 1200:
        problems.append("rule must be <= 1200 chars (one rule per record)")
    if problems:
        raise BankRefused("; ".join(problems))
    out = {k: entry[k] for k in REQUIRED + OPTIONAL if k in entry}
    out["rule"] = str(out["rule"]).strip()
    out["confidence"] = conf
    out["status"] = str(out["status"]).lower()
    out["tags"] = [str(t).lower() for t in (out.get("tags") or [])]
    return out


def _tokens(text):
    return set(re.findall(r"[a-z0-9]{3,}", str(text or "").lower()))


def jaccard(a, b):
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def similar(rule, rows, k=3):
    """[(score, row)] nearest existing records by rule-token Jaccard, best first."""
    scored = []
    for r, _f, _n, _m in rows:
        other = str(r.get("rule") or r.get("text") or "")
        if other:
            scored.append((jaccard(rule, other), r))
    scored.sort(key=lambda t: -t[0])
    return scored[:k]


def next_id(rows, prefix):
    """Union-max over every bank file: the highest <prefix>-NNNN plus one, zero-padded. Never reuses an id."""
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    mx = 0
    for r, _f, _n, _m in rows:
        m = pat.match(str(r.get("id") or ""))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{prefix}-{mx + 1:04d}"


LIFECYCLE_ON_BANK = ("screened", "candidate")   # screened = verified by the caller; candidate = mined, awaiting review


def bank(entry, bank_path=None, prefix="kb", force=False, by="cli:bank", now=None, dry_run=False, lifecycle="screened"):
    """Validate, dedupe, allocate an id, append. Returns the result dict; raises BankRefused.
    `lifecycle` is screened (default: the caller verified the rule) or candidate (mined, awaiting human review; a
    candidate is written to the bank file given, normally a candidates file the hook does not serve)."""
    if lifecycle not in LIFECYCLE_ON_BANK:
        raise BankRefused(f"lifecycle must be one of {LIFECYCLE_ON_BANK}, got {lifecycle!r}")
    rec = validate(entry)
    bank_path = Path(bank_path or default_bank_path()).expanduser()
    files = store_files(extra=str(bank_path))
    rows = load_rows(files)
    ids = {str(r.get("id") or "") for r, _f, _n, _m in rows}
    arm = arm_id_for_rule(rec["rule"])
    for r, f, n, _m in rows:
        if (r.get("meta") or {}).get("arm") == arm or arm_id_for_rule(str(r.get("rule") or "")) == arm:
            raise BankRefused(f"duplicate: the same rule is already banked as {r.get('id')!r} in {f.name}:{n + 1}")
    near = similar(rec["rule"], rows)
    if near and near[0][0] >= JACCARD_DUP and not force:
        raise BankRefused(f"near-duplicate (jaccard {near[0][0]:.2f}) of {near[0][1].get('id')!r}: "
                          f"{str(near[0][1].get('rule'))[:120]!r}; pass --force to bank anyway")
    for t in rec.get("corrects") or []:
        if t not in ids:
            raise BankRefused(f"corrects {t!r}: no banked record has that id")
    rid = str(rec.get("id") or "").strip()
    if rid and rid in ids:
        raise BankRefused(f"id {rid!r} is already used; omit id to allocate the next {prefix}-NNNN")
    rec["id"] = rid or next_id(rows, prefix)
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    neg = rec["status"] == "negative"
    meta = {"arm": arm, "reward": 0 if neg else 1, "regret": 1 if neg else 0,  # polarity, not an outcome
            "cross_ref": list(rec.pop("cross_ref", []) or [])}
    if rec.get("corrects"):
        meta["corrects"] = list(rec["corrects"])
    out = {"id": rec["id"], "rule": rec["rule"]}
    for k in ("title", "evidence", "confidence", "source", "provenance", "status", "category", "tags", "corrects",
              "version_scope", "as_of"):
        if rec.get(k) not in (None, "", []):
            out[k] = rec[k]
    out["meta"] = meta
    out["banked_at"] = ts
    mark(out, lifecycle, by, "banked through loglearner.cli bank: schema gate + dedupe passed; "
         + ("caller verified" if lifecycle == "screened" else "mined candidate awaiting human review"))
    result = {"banked": out["id"], "bank": str(bank_path), "arm": arm, "dry_run": dry_run, "lifecycle": lifecycle,
              "similar": [{"id": r.get("id"), "jaccard": round(s, 2)} for s, r in near if s > 0]}
    if not dry_run:
        bank_path.parent.mkdir(parents=True, exist_ok=True)
        with open(bank_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return result
