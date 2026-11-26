"""
Log-domain adapter dispatch.

Loads loglearner/registry/log_adapters.json (ported from reference) and selects a
domain analyzer by regex pattern match. This stage only classifies — no
per-adapter analysis is run yet. That comes later when we port the domain
parsers from reference's evaluation/ tree.

Adapters currently registered:
  generic_text, jsonl_events, claude_session, kubernetes, docker_build,
  cloud_infra, kernel_system, network_transport, nccl_trace.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Iterable

REGISTRY_PATH = Path(__file__).parent / "registry" / "log_adapters.json"

_CACHE: list[dict] | None = None
_ANALYZERS: dict[str, Callable[[str | Path], dict]] = {}


def load_adapters() -> list[dict]:
    """Load the adapter registry. Cached after first call."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    with open(REGISTRY_PATH) as f:
        _CACHE = json.load(f)["adapters"]
    return _CACHE


def _pattern_regex(patterns: Iterable[str]) -> re.Pattern:
    # Literal patterns joined with |; case-insensitive.
    alternation = "|".join(re.escape(p) for p in patterns if p)
    return re.compile(alternation, re.IGNORECASE)


def detect_adapter(text: str, adapters: list[dict] | None = None) -> str:
    """Return the adapter name whose detect_patterns match text, or "generic_text".

    First-match-wins. Adapters without detect_patterns are skipped.
    """
    adapters = adapters or load_adapters()
    for adapter in adapters:
        patterns = adapter.get("detect_patterns")
        if not patterns:
            continue
        if _pattern_regex(patterns).search(text):
            return adapter["name"]
    return "generic_text"


def register_analyzer(adapter_name: str, fn: Callable[[str | Path], dict]) -> None:
    """Register a per-adapter analyzer. fn(session_path) -> dict of extracted signals.

    Unregistered adapters return {} from analyze_session. This is the extension
    point for porting reference's domain parsers (nccl_log_parser, etc.) without
    changing the Step code.
    """
    _ANALYZERS[adapter_name] = fn


def analyze_session(adapter_name: str, session_path: str | Path) -> dict:
    """Dispatch to the registered analyzer for adapter_name, or {}."""
    fn = _ANALYZERS.get(adapter_name)
    return fn(session_path) if fn else {}


def detect_adapters_for_session(session_path: str | Path, sample_lines: int = 200) -> list[str]:
    """Sample the first N lines of a session file and return unique adapter matches.

    Always includes the primary match; additional domains found (e.g. a Claude
    session that also contains NCCL trace output) are appended in first-seen order.
    """
    path = Path(session_path)
    if not path.exists():
        return ["generic_text"]
    adapters = load_adapters()
    seen: list[str] = []
    with open(path, errors="ignore") as f:
        for i, line in enumerate(f):
            if i >= sample_lines:
                break
            name = detect_adapter(line, adapters)
            if name not in seen:
                seen.append(name)
    if not seen:
        seen = ["generic_text"]
    return seen
