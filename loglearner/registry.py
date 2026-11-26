"""
Keyword-indexed axis registry for LogLearner v2.

Axes map problem shapes ("gap detection", "verification", "runtime injection")
to primitives across ACE, reference-flywheel, LogLearner, and global skills.
Source of truth: loglearner/registry/axes.json (derived from KEYWORD_INDEX.md).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

REGISTRY_PATH = Path(__file__).parent / "registry" / "axes.json"
DISCOVERED_PATH = Path(__file__).parent / "registry" / "discovered_axes.json"


@lru_cache(maxsize=1)
def _load_static() -> dict:
    with open(REGISTRY_PATH) as f:
        return json.load(f)


def _load_discovered() -> dict:
    if not DISCOVERED_PATH.exists():
        return {"axes": []}
    with open(DISCOVERED_PATH) as f:
        return json.load(f)


def load_registry() -> dict:
    """Union of static axes.json and discovered_axes.json. Discovered primitives
    are appended to the matching axis; new axes are appended as-is."""
    static = _load_static()
    discovered = _load_discovered()
    # Deep-merge: append discovered primitives onto matching static axes
    by_id = {a["id"]: {"id": a["id"], "title": a["title"], "problem": a["problem"],
                       "primitives": list(a["primitives"])} for a in static["axes"]}
    for a in discovered.get("axes", []):
        if a["id"] in by_id:
            by_id[a["id"]]["primitives"].extend(a["primitives"])
        else:
            by_id[a["id"]] = a
    return {"axes": list(by_id.values()), "_schema_version": static.get("_schema_version", 1)}


def append_primitive(axis_id: str, keyword: str, source: str, role: str) -> None:
    """Persist a newly discovered primitive to registry/discovered_axes.json.

    If the axis doesn't exist yet (neither in static nor discovered), creates a
    stub axis with placeholder title/problem. Does NOT modify the static registry.
    """
    # Validate axis_id against static registry (or let it pass as a new axis)
    data = _load_discovered()
    for a in data["axes"]:
        if a["id"] == axis_id:
            a["primitives"].append({"keyword": keyword, "source": source, "role": role})
            break
    else:
        # Not yet in discovered — check static first
        static_ids = {a["id"] for a in _load_static()["axes"]}
        if axis_id in static_ids:
            data["axes"].append({"id": axis_id,
                                 "primitives": [{"keyword": keyword, "source": source, "role": role}]})
        else:
            data["axes"].append({"id": axis_id,
                                 "title": axis_id.replace("_", " ").title(),
                                 "problem": f"(discovered axis — describe later)",
                                 "primitives": [{"keyword": keyword, "source": source, "role": role}]})
    DISCOVERED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERED_PATH, "w") as f:
        json.dump(data, f, indent=2)


def axes() -> list[dict]:
    """List all axes with id, title, problem. No primitives."""
    return [
        {"id": a["id"], "title": a["title"], "problem": a["problem"],
         "primitive_count": len(a["primitives"])}
        for a in load_registry()["axes"]
    ]


def primitives(axis_id: str) -> list[dict]:
    """Return primitives for one axis."""
    for a in load_registry()["axes"]:
        if a["id"] == axis_id:
            return a["primitives"]
    raise KeyError(f"No axis '{axis_id}'. Known: {[a['id'] for a in load_registry()['axes']]}")


def find(keyword: str) -> list[dict]:
    """Case-insensitive substring search across keywords and roles.

    Returns a flat list of {axis, keyword, source, role}, sorted by axis.
    """
    needle = keyword.lower()
    hits: list[dict] = []
    for a in load_registry()["axes"]:
        for p in a["primitives"]:
            haystack = (p["keyword"] + " " + p.get("role", "")).lower()
            if needle in haystack:
                hits.append({
                    "axis": a["id"],
                    "keyword": p["keyword"],
                    "source": p["source"],
                    "role": p.get("role", ""),
                })
    return hits
