"""Egress gate for externally-bound LLM prompts.

Doctrine (same as the export gate and the gist index): no scrub config means
no bytes leave the host, and a detector hit on the *scrubbed* payload refuses
the call outright. Callers fall back to their local/heuristic path — privacy
degrades capability, never the other way around.

Every subprocess that ships prompt text to an external model API must call
`scrub_for_egress` and treat ``None`` as "do not dispatch":

    prompt = scrub_for_egress(prompt)
    if prompt is None:
        return None            # fail-closed; local fallback handles it

Covered call sites: example_synthesizer._query_model (synthesis + consensus +
the dashboard's LLM-abstract path which imports it), instinct_deduplicator.
_query_model, scripts/mine_from_labels.call_llm, scripts/
derive_labeling_profile.run_claude. tests/test_synthesizer_egress_gate.py
proves the property fails if a gate call is removed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from .export_gate import find_config
from .scrub_core import collect_numbered, detect, load_config, scrub_text, verify_clean


def resolve_query_model() -> Path:
    """Locate the multi-model query script: LOGLEARNER_QUERY_MODEL env wins,
    then a PATH lookup for query-model.sh, then a neutral home default.
    A missing script means LLM synthesis is off (heuristics still run);
    it is never an error. (Cherry-picked from the product repo.)"""
    env = os.environ.get("LOGLEARNER_QUERY_MODEL")
    if env:
        return Path(env).expanduser()
    which = shutil.which("query-model.sh")
    if which:
        return Path(which)
    return Path.home() / ".loglearner" / "bin" / "query-model.sh"


def scrub_for_egress(prompt: str, where: str = "llm-egress") -> Optional[str]:
    """Scrub an outbound prompt; return None when the call must not happen.

    None cases (all fail-closed): no scrub config resolvable; a detector
    fires on the scrubbed text; a replacement/numbered pattern still matches
    after scrubbing (residual).
    """
    cfg_path = find_config()
    if not cfg_path:
        return None
    cfg = load_config(cfg_path)
    scrubbed, _ = scrub_text(cfg, prompt, collect_numbered(cfg, [prompt]))
    if detect(cfg, scrubbed, where) or verify_clean(cfg, scrubbed):
        return None
    return scrubbed
