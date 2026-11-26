"""Arm telemetry lane: loglearner's own UCB stores,
byte-compatible with the fleet reference learnings schema, plus a read-only
ingest of the fleet's arms for dedup/priors.

Constraint honoured everywhere in this module: the fleet learnings dir is
NEVER written; loglearner owns only files under its own repo:
    output/loglearner_arms.jsonl     one line per minted (KEEP) rule-arm
    output/loglearner_rewards.jsonl  one line per measured observation
    output/loglearner_exposures.jsonl one line per (session, arm) the rule was DELIVERED to
    output/state/ucb.json            AsymmetricUCB snapshot (fleet key set)
    state/fleet_arm_index.json       read-only ingest product

Paths are env-overridable mirroring telemetry.py:
    LOGLEARNER_UCB_DIR              (default: <repo>/output)
    LOGLEARNER_FLEET_LEARNINGS      (default: ~/.loglearner/banks)
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from loglearner.ucb import AsymmetricUCB

REPO = Path(__file__).resolve().parent.parent


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ucb_dir():
    return Path(os.environ.get("LOGLEARNER_UCB_DIR", REPO / "output"))


def arms_path():
    return ucb_dir() / "loglearner_arms.jsonl"


def rewards_path():
    return ucb_dir() / "loglearner_rewards.jsonl"


def ucb_state_path():
    return ucb_dir() / "state" / "ucb.json"


def arm_id_for_rule(rule):
    """Stable across runs: the same rule text always mints the same arm."""
    return f"ll-{hashlib.sha1((rule or '').encode(), usedforsecurity=False).hexdigest()[:10]}_apply"


def load_arms():
    path = arms_path()
    if not path.is_file():
        return []
    out = []
    for line in path.open(errors="replace"):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def mint_arm(example, project=""):
    """Fleet-shape arm record for one KEEP example (reward null at mint)."""
    rule = getattr(example, "instinct_rule", "") or ""
    gap_type = (getattr(example, "gap_summary", {}) or {}).get("gap_type", "")
    category = getattr(example, "category", "") or ""
    a, b = (getattr(example, "line_range", (0, 0)) or (0, 0))[:2]
    sid = getattr(example, "session_id", "") or ""
    return {
        "arm": arm_id_for_rule(rule),
        "action": "loglearner_verified_keep",
        "rule": rule[:300],
        "signature": {"gap_type": gap_type, "category": category},
        "labels": [x for x in (category, gap_type) if x],
        "cross_ref": [f"session:{sid} lines:{a}-{b}"],
        "project": project,
        "reward": None,
        "regret": None,
        "mint_ts": _now(),
    }


def append_jsonl(path, records):
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


def exposures_path():
    return ucb_dir() / "loglearner_exposures.jsonl"


def record_exposure(session_id, arm_ids, project="", decision_ref="", instinct_ids=None):
    """Record that a session actually RECEIVED these rule-arms (the injection
    manifest's included ids) before a decision. This is the join the reward
    pass requires: a rule never delivered to a session cannot be
    credited for that session's outcome. `instinct_ids` (optional, parallel to
    `arm_ids`) keeps the source library id next to the content-hash arm so a
    delivered fleet instinct is joinable from either side. Returns rows appended."""
    ts = _now()
    instinct_ids = list(instinct_ids or [])
    rows = []
    for i, a in enumerate(arm_ids or []):
        if not a:
            continue
        row = {"session_id": session_id, "arm": a, "project": project,
               "decision_ref": decision_ref, "ts": ts}
        if i < len(instinct_ids) and instinct_ids[i]:
            row["instinct_id"] = instinct_ids[i]
        rows.append(row)
    return append_jsonl(exposures_path(), rows)


def load_exposures():
    """arm -> set(session_id) from the exposure ledger (empty if none)."""
    out = {}
    p = exposures_path()
    if not p.is_file():
        return out
    for line in p.open(errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("arm") and r.get("session_id"):
            out.setdefault(r["arm"], set()).add(r["session_id"])
    return out


def _existing_obs_ids():
    p = rewards_path()
    if not p.is_file():
        return set()
    ids = set()
    for line in p.open(errors="replace"):
        try:
            ids.add(json.loads(line).get("obs_id"))
        except (json.JSONDecodeError, AttributeError):
            continue
    return ids


def measure_arms(sessions, gaps, exposures=None):
    """Deterministic reward pass (corrected after review).

    For every previously minted arm, the MEASURED set is the sessions from this
    run that (a) post-date the mint, (b) actually RECEIVED the rule (an
    exposure row from record_exposure — the injection manifest's included
    ids), and (c) belong to the arm's project when both sides declare one.
    No measured session => the arm is UNMEASURED for this run: no observation
    and no bandit pull (an unrelated or unexposed session proves nothing).
    Among the measured sessions, a gap with the arm's exact gap_type+category
    signature means the rule failed to prevent recurrence (reward 0,
    regret 1); otherwise the gap stopped appearing (reward 1).

    Observations are keyed by EVIDENCE identity (arm + measured sessions +
    recurrence evidence), never by wall-clock, so replaying the same evidence
    is deduplicated against the rewards ledger and cannot double-pull.

    `exposures` (arm -> set(session_id)) defaults to the on-disk ledger.
    Returns (new_observations, bandit) and persists rewards + ucb.json.
    """
    arms = load_arms()
    if not arms:
        return [], None
    if exposures is None:
        exposures = load_exposures()

    bandit = (AsymmetricUCB.load(ucb_state_path())
              if ucb_state_path().is_file() else AsymmetricUCB())
    seen = _existing_obs_ids()
    sess_project = {getattr(s, "session_id", ""): (getattr(s, "project", "") or "")
                    for s in sessions}

    observations = []
    for arm in arms:
        mint_ts = arm.get("mint_ts", "")
        sig = arm.get("signature", {})
        arm_project = arm.get("project", "") or ""
        exposed = exposures.get(arm["arm"], set())
        measured = set()
        for s in sessions:
            sid = getattr(s, "session_id", "")
            last = (getattr(s, "date_range", ("", "")) or ("", ""))[1] or ""
            if not (last and mint_ts and last > mint_ts):
                continue  # pre-mint: cannot measure the rule's effect
            if sid not in exposed:
                continue  # never received the rule: unmeasured, not success
            sp = sess_project.get(sid, "")
            if arm_project and sp and sp != arm_project:
                continue  # not applicable: different project
            measured.add(sid)
        if not measured:
            continue
        recurrences = [
            g for g in gaps
            if getattr(g, "session_id", "") in measured
            and getattr(g, "gap_type", "") == sig.get("gap_type")
            and getattr(g, "category", "") == sig.get("category")
        ]
        evidence = sorted(
            f"{getattr(g, 'session_id', '')}:{getattr(g, 'start_line', 0)}-"
            f"{getattr(g, 'end_line', 0)}:{getattr(g, 'gap_type', '')}"
            for g in recurrences)
        key = f"{arm['arm']}|{','.join(sorted(measured))}|{','.join(evidence)}"
        obs_id = f"obs-{hashlib.sha1(key.encode(), usedforsecurity=False).hexdigest()[:10]}"
        if obs_id in seen:
            continue  # identical evidence already counted (replay)
        seen.add(obs_id)
        reward = 0.0 if recurrences else 1.0
        observations.append({
            "obs_id": obs_id,
            "arm": arm["arm"],
            "reward": reward,
            "regret": 1.0 - reward,
            "matched_sessions": sorted(measured)[:8],
            "recurrence_count": len(recurrences),
            "notes": ("gap recurred in exposed sessions post-mint" if recurrences
                      else "no recurrence in exposed, signature-matched sessions"),
            "ts": _now(),
        })
        bandit.update(bandit.ensure_arm(arm["arm"]), reward)

    if observations:
        append_jsonl(rewards_path(), observations)
        bandit.save(ucb_state_path())
    return observations, bandit


def build_fleet_arm_index(learnings_dir=None, out_path=None):
    """READ-ONLY ingest of fleet reference learnings -> state/fleet_arm_index.json.
    Used for dedup-against-fleet and priors, never for mutation."""
    learnings_dir = Path(learnings_dir or os.environ.get(
        "LOGLEARNER_FLEET_LEARNINGS",
        Path.home() / ".loglearner" / "banks"))
    out_path = Path(out_path or REPO / "state" / "fleet_arm_index.json")

    index = {}
    if learnings_dir.is_dir():
        for f in sorted(learnings_dir.glob("*.jsonl")):
            if ".bak" in f.name.lower():
                continue
            for line in f.open(errors="replace"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                meta = d.get("meta") or {}
                arm = meta.get("arm")
                if not arm:
                    continue
                ent = index.setdefault(arm, {
                    "action": meta.get("action", ""),
                    "labels": [], "cross_ref": [], "sources": [],
                    "last_reward": None, "records": 0})
                ent["records"] += 1
                if meta.get("reward") is not None:
                    ent["last_reward"] = meta["reward"]
                for k in ("labels", "cross_ref"):
                    for v in meta.get(k) or []:
                        if v not in ent[k]:
                            ent[k].append(v)
                if f.stem not in ent["sources"]:
                    ent["sources"].append(f.stem)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {"built": _now(), "learnings_dir": str(learnings_dir),
         "arm_count": len(index), "arms": index}, indent=1))
    return index


if __name__ == "__main__":
    idx = build_fleet_arm_index()
    print(f"fleet_arm_index: {len(idx)} arms -> state/fleet_arm_index.json")
