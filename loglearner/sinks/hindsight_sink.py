"""
Hindsight sink — write LogLearner rules into a vectorize-io/hindsight bank.

Hindsight is the runtime memory layer. This sink runs after
VerifyStep and feeds KEEP rules to a Hindsight bank for downstream recall.

Optional dep — `pip install hindsight-all` (or `loglearning[hindsight]`).
The module loads cleanly even when the dep is absent; callers check
`hindsight_available()` before calling `retain_to_hindsight`.

API design notes (verified via spike 2026-05-13, see HINDSIGHT_NOTES.md):
- MemoryEngine.retain_async — sync wrapper calls asyncio.run, can't be
  used inside an event loop; we always use the async path.
- request_context is a required keyword arg.
- fact_type_override="experience" tags rules as agent-experience facts.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Iterable

from ..lifecycle import filter_servable

try:
    import hindsight_api as _ha  # type: ignore
    _HINDSIGHT = True
except ImportError:
    _ha = None  # type: ignore
    _HINDSIGHT = False


def hindsight_available() -> bool:
    """True iff hindsight_api is importable in this environment."""
    return _HINDSIGHT


@dataclass(frozen=True)
class HindsightConfig:
    """Connection config for retain_to_hindsight. Reads from env when fields absent."""
    db_url: str = ""
    bank_id: str = "loglearner-default"
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"

    @classmethod
    def from_env(cls) -> "HindsightConfig":
        return cls(
            db_url=os.environ.get("HINDSIGHT_DB_URL", "postgresql://postgres:loglearner@127.0.0.1:55432/hindsight"),
            bank_id=os.environ.get("HINDSIGHT_BANK_ID", "loglearner-default"),
            llm_provider=os.environ.get("HINDSIGHT_LLM_PROVIDER", "openai"),
            llm_api_key=os.environ.get("HINDSIGHT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""),
            llm_model=os.environ.get("HINDSIGHT_LLM_MODEL", "gpt-4o-mini"),
        )


import re as _re
_TAG_PATTERN = _re.compile(r"<([a-zA-Z0-9_\-]+)>|</([a-zA-Z0-9_\-]+)>")


def _sanitize_for_retain(text: str) -> str:
    """Wrap literal `<tag>` references in quotes so the retain LLM doesn't
    interpret them as live instructions or refuse to process them.

    Discovered in Tier A diagnostic: rule L022 contains literal
    `<local-command-caveat>` tags; gpt-4o-mini returned empty observation
    IDs (silent failure). Quoting the tags fixes the round-trip.
    """
    return _TAG_PATTERN.sub(lambda m: "'" + m.group(0) + "'", text)


def _rule_to_content(rule: dict) -> tuple[str, str]:
    """Render a rule dict to (content, context) strings for retain_async."""
    rid = rule.get("id", "?")
    text = _sanitize_for_retain(rule.get("rule", "") or "")
    confidence = rule.get("confidence", "?")
    category = rule.get("category", "unknown") or "unknown"
    content = f"[{rid}] {text[:600]}"
    context = f"category={category} confidence={confidence}/10"
    return content, context


async def _retain_one_async(eng, bank_id: str, rule: dict, request_context) -> tuple[str | None, str | None, str]:
    """Returns (obs_id_or_None, reason_or_None, kind) where kind ∈
    {'success', 'skipped_no_entities', 'failed_exception'}.

    'skipped_no_entities' is a known Hindsight behavior: the LLM finds no
    extractable entities in the content (often happens with meta-rules,
    self-referential text, or content full of code-tag-like markers).
    Not a bug; logged as a skip not a failure."""
    content, context = _rule_to_content(rule)
    try:
        ids = await eng.retain_async(
            bank_id=bank_id,
            content=content,
            context=context,
            fact_type_override="experience",
            request_context=request_context,
        )
        if ids:
            return (ids[0], None, "success")
        return (None, "Hindsight LLM extracted 0 entities from rule content", "skipped_no_entities")
    except Exception as e:
        return (None, f"{type(e).__name__}: {str(e)[:300]}", "failed_exception")


async def _retain_async(rules: list[dict], cfg: HindsightConfig, recreate_bank: bool = True) -> dict:
    """Retain a batch of rules; returns dict with retained_ids, failures, total."""
    eng = _ha.MemoryEngine(
        db_url=cfg.db_url,
        retain_llm_provider=cfg.llm_provider, retain_llm_api_key=cfg.llm_api_key, retain_llm_model=cfg.llm_model,
        memory_llm_provider=cfg.llm_provider, memory_llm_api_key=cfg.llm_api_key, memory_llm_model=cfg.llm_model,
        skip_llm_verification=True,
    )
    await eng.initialize()
    rctx = _ha.RequestContext(internal=True)

    if recreate_bank:
        try:
            await eng.delete_bank(bank_id=cfg.bank_id)
        except Exception:
            pass

    obs_ids: list[str] = []
    skipped: list[dict] = []
    failures: list[dict] = []
    for r in rules:
        oid, reason, kind = await _retain_one_async(eng, cfg.bank_id, r, rctx)
        if kind == "success":
            obs_ids.append(oid)
        else:
            entry = {
                "rule_id": r.get("id", "?"),
                "category": r.get("category", "unknown"),
                "rule_len": len(r.get("rule", "") or ""),
                "reason": reason or "unknown",
            }
            if kind == "skipped_no_entities":
                skipped.append(entry)
            else:
                failures.append(entry)

    await eng.close()
    return {
        "retained_ids": obs_ids,
        "skipped": skipped,            # Hindsight LLM extracted no entities — by-design
        "failures": failures,           # Real exceptions — actual bugs
        "total": len(rules),
    }


def retain_to_hindsight(rules: list[dict] | Iterable[dict],
                         cfg: HindsightConfig | None = None,
                         recreate_bank: bool = True,
                         return_full: bool = False):
    """Retain LogLearner rules into a Hindsight bank.

    Args:
        rules: list of rule dicts as emitted by example_synthesizer / unified-v*
        cfg:   HindsightConfig; if None, reads HindsightConfig.from_env()
        recreate_bank: if True, deletes+recreates the bank for idempotent re-runs
        return_full: if True, return dict with retained_ids+failures+total;
                     if False (legacy), return list of observation IDs.

    Returns (return_full=True):
        {"retained_ids": [...], "failures": [{rule_id, category, rule_len, error}], "total": N}

    Raises:
        RuntimeError if hindsight_api is not installed.
    """
    if not _HINDSIGHT:
        raise RuntimeError(
            "hindsight_api is not installed. Run: pip install hindsight-all "
            "(or `pip install loglearning[hindsight]`)."
        )
    cfg = cfg or HindsightConfig.from_env()
    if not cfg.db_url:
        raise RuntimeError("HindsightConfig.db_url is empty; set HINDSIGHT_DB_URL.")
    if not cfg.llm_api_key:
        raise RuntimeError("HindsightConfig.llm_api_key is empty; set HINDSIGHT_LLM_API_KEY or OPENAI_API_KEY.")
    # Lifecycle gate: only servable rules reach the bank; with
    # recreate_bank=True the rebuild is also how benched rules get evicted
    # from recall (docs/KNOWLEDGE-LIFECYCLE.md §1).
    rules_list = filter_servable(list(rules))
    result = asyncio.run(_retain_async(rules_list, cfg, recreate_bank=recreate_bank))
    return result if return_full else result["retained_ids"]
