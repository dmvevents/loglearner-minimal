"""Source registry — pluggable trace/knowledge sources (E1 S1.4).

A *source* is where LogLearner records come from. Two kinds:
  - "sessions":  agent transcripts the mine→detect→synthesize pipeline walks
                 (claude is the default and keeps its existing behavior).
  - "knowledge": already-distilled artifacts (gists, reference folders) that get
                 scrubbed, faceted onto the axes taxonomy, and indexed rather
                 than gap-mined.

Contract: a registered source is a class with class attributes
  source_name (set by the decorator), kind, description
and a `run(args) -> int` staticmethod for CLI dispatch (knowledge sources).
Session sources instead expose the scanner they wrap; the v2 pipeline keeps
using session_scanner directly so `--source claude` changes nothing.

Every byte a knowledge source persists passes through loglearner.scrub_core
first — no scrub config, no index (fail-closed, same posture as export_gate).
"""

from __future__ import annotations

_SOURCES: dict[str, type] = {}
_BUILTINS_LOADED = False


def register_source(name: str):
    """Class decorator: @register_source("gist") registers a source plugin."""
    def deco(obj):
        obj.source_name = name
        _SOURCES[name] = obj
        return obj
    return deco


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    import importlib
    for _mod in ("claude_session", "reference", "gist", "slack_kb"):  # import = register
        try:
            importlib.import_module(f"{__name__}.{_mod}")
        except ImportError:  # a minimal (vendored) runtime may omit optional sources
            continue


def get_source(name: str):
    _load_builtins()
    if name not in _SOURCES:
        known = ", ".join(sorted(_SOURCES))
        raise KeyError(f"unknown source {name!r} (registered: {known})")
    return _SOURCES[name]


def list_sources() -> list[dict]:
    _load_builtins()
    return [
        {
            "name": name,
            "kind": getattr(src, "kind", "?"),
            "description": getattr(src, "description", ""),
        }
        for name, src in sorted(_SOURCES.items())
    ]
