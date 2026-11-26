"""SessionStateVector: a deterministic, comparable
fingerprint of what a session did — no LLM anywhere.

Consumed by: sessions_similarity precompute (dashboard indexer), the
related-sessions panel (KR3.3), dedup candidate pruning, and the retrieval
router as a category hint.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

_MODEL_RE = re.compile(
    r"query-model\.sh\s+(\S+)|\bmodel[= ]\s*[\"']?([a-z0-9.-]+)", re.I)
_BASH_TOKEN_RE = re.compile(r"^\s*([A-Za-z0-9_./-]+)")


@dataclass(frozen=True)
class SessionStateVector:
    session_id: str
    project: str
    domains: tuple = ()             # adapters that fired
    model_family: str = ""          # first model reference seen
    duration_min: float = 0.0
    tool_dist: dict = field(default_factory=dict)   # L1-normalized
    error_classes: frozenset = frozenset()
    correction_classes: frozenset = frozenset()
    gap_types: frozenset = frozenset()
    file_targets: frozenset = frozenset()           # basenames
    bash_signatures: frozenset = frozenset()        # first tokens
    outcome_signal: str = "unknown"

    def to_json(self):
        d = asdict(self)
        for k in ("error_classes", "correction_classes", "gap_types",
                  "file_targets", "bash_signatures"):
            d[k] = sorted(d[k])
        d["domains"] = list(self.domains)
        return json.dumps(d)

    @classmethod
    def from_json(cls, text):
        d = json.loads(text)
        for k in ("error_classes", "correction_classes", "gap_types",
                  "file_targets", "bash_signatures"):
            d[k] = frozenset(d.get(k) or [])
        d["domains"] = tuple(d.get("domains") or [])
        return cls(**d)


def normalize_tool_dist(counter):
    total = sum(counter.values()) or 1
    return {k: round(v / total, 4) for k, v in counter.items()}


def duration_minutes(first_ts, last_ts):
    try:
        a = datetime.fromisoformat((first_ts or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((last_ts or "").replace("Z", "+00:00"))
        return round(max((b - a).total_seconds() / 60, 0.0), 1)
    except ValueError:
        return 0.0


def model_family(texts):
    for t in texts:
        m = _MODEL_RE.search(t)
        if m:
            return (m.group(1) or m.group(2) or "")[:40]
    return ""


def bash_signature(command):
    m = _BASH_TOKEN_RE.match(command or "")
    return Path(m.group(1)).name if m else ""


def outcome_signal(last_overflow, last_correction):
    if last_overflow:
        return "ended_context_overflow"
    if last_correction:
        return "ended_correction"
    return "ended_clean"


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(da, db):
    if not da or not db:
        return 0.0
    dot = sum(v * db.get(k, 0.0) for k, v in da.items())
    na = sum(v * v for v in da.values()) ** 0.5
    nb = sum(v * v for v in db.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


# Weights: tool mix dominates (it is the behaviour), files tie work together,
# errors and domains refine. Deterministic, no LLM.
_W = {"tools": 0.5, "files": 0.25, "errors": 0.15, "domains": 0.10}


def similarity(a: SessionStateVector, b: SessionStateVector):
    """Returns (score, components) — components exposed so edges can be
    typed (shares_file / shares_error / ...) from the same computation."""
    comp = {
        "tools": round(_cosine(a.tool_dist, b.tool_dist), 4),
        "files": round(_jaccard(a.file_targets, b.file_targets), 4),
        "errors": round(_jaccard(a.error_classes, b.error_classes), 4),
        "domains": round(_jaccard(set(a.domains), set(b.domains)), 4),
        "shared_bash": len(a.bash_signatures & b.bash_signatures),
    }
    score = round(sum(_W[k] * comp[k] for k in _W), 4)
    return score, comp
