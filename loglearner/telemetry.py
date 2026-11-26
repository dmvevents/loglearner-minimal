"""
Append-only event telemetry for LogLearner runtime.

Sprint 4 DoD requires a `rule_applied` / `rule_violated` signal so we can
measure injection impact later. This module is the minimum-viable emitter.
Tier-A scope: writer only. Reader / aggregator come in Sprint 5.

Events land at `output/telemetry.jsonl` by default (overridable via
LOGLEARNER_TELEMETRY_PATH). Honor LOGLEARNER_TELEMETRY_DISABLE=1 for tests.

Shape:
  {"ts": "2026-05-13T22:00:00Z", "event": "rule_retained",
   "payload": {"rule_id": "L001", "obs_id": "..."}}

Designed to be drop-in callable from sinks, recall demo, future
A/B replay harness — no Pipeline coupling, no async required.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("output/telemetry.jsonl")


def _path() -> Path:
    p = os.environ.get("LOGLEARNER_TELEMETRY_PATH")
    return Path(p) if p else DEFAULT_PATH


def _disabled() -> bool:
    return os.environ.get("LOGLEARNER_TELEMETRY_DISABLE") == "1"


def emit_event(event: str, payload: dict[str, Any] | None = None) -> None:
    """Append one telemetry event. Best-effort — never raises into callers."""
    if _disabled():
        return
    rec = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": event,
        "payload": payload or {},
    }
    try:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # telemetry must not crash the pipeline


def read_events(event_filter: str | None = None) -> list[dict]:
    """Read events back (used by tests + Sprint 5 aggregator)."""
    path = _path()
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event_filter is None or rec.get("event") == event_filter:
                out.append(rec)
    return out
