#!/usr/bin/env python3
"""
Example Synthesizer — Multi-LLM consensus synthesis of positive/negative examples.

Takes harvested context from gap regions and produces:
  1. instinct-compatible instincts (instincts.json format)
  2. Paired JSONL examples (situation/negative/positive)
  3. Markdown summary report

Uses 3 frontier models (GPT-5.4, Gemini 3 Pro, Claude) in parallel,
then runs a consensus merge pass.
"""

import json
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .context_harvester import HarvestedContext
from .egress_gate import scrub_for_egress


HPC_STACK_ROOT = Path.home() / ".loglearner" / "reference"
QUERY_MODEL_SCRIPT = HPC_STACK_ROOT / "scripts" / "multi-model" / "query-model.sh"

MODELS = ["gpt-5.4", "gemini-3-pro", "claude-think"]

SYNTHESIS_PROMPT_TEMPLATE = """You are analyzing a Claude Code agent session log to identify failure patterns and generate training examples.

## Context
{raw_snippet}

## Gap Information
- Type: {gap_type}
- Category: {category}
- Severity: {severity}/10
- Intent: {intent}

## Your Task

Analyze this gap and produce a structured analysis in EXACTLY this JSON format:

```json
{{
  "root_cause": "One sentence explaining WHY this failure happened at a fundamental level",
  "negative_example": {{
    "situation": "What the agent was facing (2-3 sentences)",
    "action": "What the agent did wrong (1-2 sentences)",
    "consequence": "What happened as a result (1-2 sentences)",
    "root_cause": "Why this action was wrong (1 sentence)"
  }},
  "positive_example": {{
    "situation": "Same situation as above",
    "action": "What the agent should have done instead (1-2 sentences)",
    "reasoning": "Why this is the correct approach (1-2 sentences)"
  }},
  "instinct_rule": "A concise, imperative rule the agent should follow (e.g., 'ALWAYS verify X before Y', 'NEVER do X because Y')",
  "confidence": 1-10,
  "applicable_beyond_this_session": true/false
}}
```

Be specific and actionable. Reference actual tool names, file paths, and error messages from the context. Do NOT be generic — the value is in the specificity.
"""

CONSENSUS_PROMPT_TEMPLATE = """You are synthesizing analyses from 3 different AI models about the same agent failure pattern.

## Original Gap
{raw_snippet}

## Model Analyses
{model_analyses}

## Your Task

Synthesize these into ONE definitive analysis. For each field:
- If all 3 agree: use the best-worded version
- If 2/3 agree: use the majority view, note the dissent in root_cause
- If all 3 disagree: pick the most evidence-grounded one

Output EXACTLY this JSON format:

```json
{{
  "root_cause": "Consensus root cause (note any disagreements)",
  "negative_example": {{
    "situation": "...",
    "action": "...",
    "consequence": "...",
    "root_cause": "..."
  }},
  "positive_example": {{
    "situation": "...",
    "action": "...",
    "reasoning": "..."
  }},
  "instinct_rule": "The strongest, most actionable rule from the 3 analyses",
  "confidence": 1-10,
  "model_agreement": "full|majority|split",
  "applicable_beyond_this_session": true/false
}}
```
"""


@dataclass
class SynthesizedExample:
    gap_summary: dict
    root_cause: str
    negative_example: dict
    positive_example: dict
    instinct_rule: str
    confidence: int
    model_agreement: str
    applicable_beyond_session: bool
    session_id: str
    line_range: tuple
    category: str
    model_responses: dict = field(default_factory=dict)

    def _meta_block(self) -> dict:
        """Fleet instinct-compatible meta block: identical key set to
        fleet-bank records, reward/regret null at mint —
        MeasureRuleImpactStep fills them in via loglearner_rewards.jsonl."""
        from loglearner.arms import arm_id_for_rule

        gap_type = (self.gap_summary or {}).get("gap_type", "")
        return {
            "arm": arm_id_for_rule(self.instinct_rule),
            "action": "loglearner_verified_keep",
            "reward": None,
            "regret": None,
            "cross_ref": [
                f"session:{self.session_id} lines:{self.line_range[0]}-{self.line_range[1]}"
            ],
            "labels": [x for x in (self.category, gap_type) if x],
        }

    def to_instinct(self, instinct_id: str) -> dict:
        """Convert to instinct-compatible instinct format."""
        return {
            "id": instinct_id,
            "rule": self.instinct_rule,
            "evidence": f"session:{self.session_id} lines:{self.line_range[0]}-{self.line_range[1]}",
            "confidence": self.confidence,
            "source": f"loglearner_{self.session_id[:8]}",
            "category": self.category,
            "provenance": {
                "origin": "loglearner_multi_llm",
                "tested": False,
                "ground_truth": False,
                "ai_adapted": True,
                "model_agreement": self.model_agreement,
                "added_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "last_verified": None,
            },
            "meta": self._meta_block(),
        }

    def to_paired_jsonl(self) -> dict:
        """Convert to paired JSONL format."""
        return {
            "session": self.session_id,
            "lines": list(self.line_range),
            "category": self.category,
            "severity": self.gap_summary.get("severity", 0),
            "root_cause": self.root_cause,
            "situation": self.negative_example.get("situation", ""),
            "negative": {
                "action": self.negative_example.get("action", ""),
                "consequence": self.negative_example.get("consequence", ""),
                "root_cause": self.negative_example.get("root_cause", ""),
            },
            "positive": {
                "action": self.positive_example.get("action", ""),
                "reasoning": self.positive_example.get("reasoning", ""),
            },
            "instinct": self.instinct_rule,
            "confidence": self.confidence,
            "model_agreement": self.model_agreement,
            "meta": self._meta_block(),
        }


def _query_model(model: str, prompt: str, timeout: int = 120) -> Optional[str]:
    """Query a single model via query-model.sh.

    Egress-gated: the prompt carries harvested session windows,
    so it is scrubbed fail-closed before the subprocess boundary; a refusal
    (no config / detector hit) returns None and callers fall back to the
    local heuristic path.
    """
    if not QUERY_MODEL_SCRIPT.exists():
        return None
    prompt = scrub_for_egress(prompt, "synthesis-prompt")
    if prompt is None:
        return None

    try:
        result = subprocess.run(
            [str(QUERY_MODEL_SCRIPT), model, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _extract_json_from_response(response: str) -> Optional[dict]:
    """Extract JSON from an LLM response, handling markdown code blocks."""
    if not response:
        return None

    # Try direct parse
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass

    # Try extracting from code block
    import re
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { to last }
    first_brace = response.find("{")
    last_brace = response.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(response[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass

    return None


def synthesize_single(ctx: HarvestedContext, use_consensus: bool = True) -> Optional[SynthesizedExample]:
    """Synthesize examples for a single harvested context using multi-LLM consensus."""
    gap = ctx.gap
    prompt = SYNTHESIS_PROMPT_TEMPLATE.format(
        raw_snippet=ctx.raw_snippet[:4000],
        gap_type=gap.get("gap_type", "unknown"),
        category=gap.get("category", "unknown"),
        severity=gap.get("severity", 0),
        intent=ctx.intent[:300],
    )

    model_responses = {}
    model_analyses = {}

    if use_consensus and QUERY_MODEL_SCRIPT.exists():
        # Query all 3 models in parallel
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_query_model, model, prompt): model
                for model in MODELS
            }
            for future in as_completed(futures):
                model = futures[future]
                try:
                    response = future.result()
                    model_responses[model] = response
                    parsed = _extract_json_from_response(response)
                    if parsed:
                        model_analyses[model] = parsed
                except Exception:
                    pass

        if len(model_analyses) >= 2:
            # Run consensus merge
            consensus = _run_consensus(ctx, model_analyses)
            if consensus:
                return _build_example(consensus, gap, ctx, model_responses, "consensus")

        # Fall back to best single model
        if model_analyses:
            best_model = max(model_analyses, key=lambda m: model_analyses[m].get("confidence", 0))
            return _build_example(model_analyses[best_model], gap, ctx, model_responses, f"single:{best_model}")

    # Local heuristic fallback
    return _heuristic_synthesis(ctx)


def _run_consensus(ctx: HarvestedContext, analyses: dict) -> Optional[dict]:
    """Run consensus merge across model analyses."""
    analyses_text = ""
    for model, analysis in analyses.items():
        analyses_text += f"\n### {model}\n```json\n{json.dumps(analysis, indent=2)}\n```\n"

    prompt = CONSENSUS_PROMPT_TEMPLATE.format(
        raw_snippet=ctx.raw_snippet[:3000],
        model_analyses=analyses_text,
    )

    # Use GPT-5.4 as consensus synthesizer (best at structured reasoning)
    response = _query_model("gpt-5.4", prompt, timeout=180)
    return _extract_json_from_response(response)


def _build_example(analysis: dict, gap: dict, ctx: HarvestedContext,
                   model_responses: dict, source: str) -> SynthesizedExample:
    """Build a SynthesizedExample from a parsed analysis."""
    return SynthesizedExample(
        gap_summary={
            "gap_type": gap.get("gap_type"),
            "category": gap.get("category"),
            "severity": gap.get("severity"),
            "summary": gap.get("summary"),
        },
        root_cause=analysis.get("root_cause", ""),
        negative_example=analysis.get("negative_example", {}),
        positive_example=analysis.get("positive_example", {}),
        instinct_rule=analysis.get("instinct_rule", ""),
        confidence=analysis.get("confidence", 3),
        model_agreement=analysis.get("model_agreement", source),
        applicable_beyond_session=analysis.get("applicable_beyond_this_session", True),
        session_id=gap.get("session_id", ""),
        line_range=(gap.get("start_line", 0), gap.get("end_line", 0)),
        category=gap.get("category", "unknown"),
        model_responses=model_responses,
    )


def _heuristic_synthesis(ctx: HarvestedContext) -> SynthesizedExample:
    """Fallback: produce a shallow example from heuristics when LLMs unavailable."""
    gap = ctx.gap
    failure = ctx.failure

    errors = failure.get("errors", [])
    corrections = failure.get("corrections", [])

    if corrections:
        root_cause = f"User had to correct the agent: {corrections[0][:200]}"
    elif errors:
        root_cause = f"Tool produced error: {errors[0][:200]}"
    else:
        root_cause = f"Gap detected: {gap.get('summary', 'unknown')}"

    neg_action = ""
    failed_tools = failure.get("failed_tools", [])
    if failed_tools:
        neg_action = f"Agent called {failed_tools[0].get('name', '?')} with: {failed_tools[0].get('input', '')[:100]}"

    return SynthesizedExample(
        gap_summary={
            "gap_type": gap.get("gap_type"),
            "category": gap.get("category"),
            "severity": gap.get("severity"),
            "summary": gap.get("summary"),
        },
        root_cause=root_cause,
        negative_example={
            "situation": ctx.intent[:300],
            "action": neg_action or "See raw snippet for details",
            "consequence": errors[0][:200] if errors else "Failure — see gap details",
            "root_cause": root_cause[:200],
        },
        positive_example={
            "situation": ctx.intent[:300],
            "action": "Needs LLM analysis — run with multi-model consensus enabled",
            "reasoning": "Heuristic fallback cannot determine correct approach",
        },
        instinct_rule=f"INVESTIGATE: {gap.get('summary', 'unknown')[:100]}",
        confidence=2,
        model_agreement="heuristic",
        applicable_beyond_session=False,
        session_id=gap.get("session_id", ""),
        line_range=(gap.get("start_line", 0), gap.get("end_line", 0)),
        category=gap.get("category", "unknown"),
    )


def synthesize_batch(contexts: list[HarvestedContext],
                     use_consensus: bool = True,
                     max_parallel: int = 3) -> list[SynthesizedExample]:
    """Synthesize examples for multiple contexts."""
    results = []

    # Process sequentially to avoid overwhelming model APIs
    for i, ctx in enumerate(contexts):
        example = synthesize_single(ctx, use_consensus=use_consensus)
        if example:
            results.append(example)

    return results


# --- Output writers ---

def write_instincts(examples: list[SynthesizedExample], output_path: Path):
    """Write instinct-compatible instincts.json."""
    instincts = []
    for i, ex in enumerate(examples, 1):
        instinct_id = f"L{i:03d}"
        instincts.append(ex.to_instinct(instinct_id))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(instincts, f, indent=2)

    return len(instincts)


def write_paired_jsonl(examples: list[SynthesizedExample], output_path: Path):
    """Write paired example JSONL."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex.to_paired_jsonl()) + "\n")

    return len(examples)


def write_markdown_report(examples: list[SynthesizedExample], output_path: Path,
                          project: str = ""):
    """Write human-readable markdown report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# LogLearner Analysis Report",
        f"",
        f"**Generated**: {now}",
        f"**Project**: {project}",
        f"**Examples synthesized**: {len(examples)}",
        f"",
    ]

    # Summary stats
    from collections import Counter
    categories = Counter(ex.category for ex in examples)
    agreements = Counter(ex.model_agreement for ex in examples)
    severities = [ex.gap_summary.get("severity", 0) for ex in examples]
    avg_severity = sum(severities) / max(len(severities), 1)
    avg_confidence = sum(ex.confidence for ex in examples) / max(len(examples), 1)

    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Total examples | {len(examples)} |")
    lines.append(f"| Avg severity | {avg_severity:.1f}/10 |")
    lines.append(f"| Avg confidence | {avg_confidence:.1f}/10 |")
    lines.append(f"| Model agreement | {dict(agreements)} |")
    lines.append("")

    lines.append("### Categories")
    lines.append("")
    for cat, count in categories.most_common():
        lines.append(f"- **{cat}**: {count} examples")
    lines.append("")

    # Individual examples
    lines.append("## Examples")
    lines.append("")

    for i, ex in enumerate(examples, 1):
        lines.append(f"### {i}. {ex.instinct_rule[:80]}")
        lines.append(f"")
        lines.append(f"**Category**: {ex.category} | "
                      f"**Severity**: {ex.gap_summary.get('severity', '?')}/10 | "
                      f"**Confidence**: {ex.confidence}/10 | "
                      f"**Agreement**: {ex.model_agreement}")
        lines.append(f"**Session**: `{ex.session_id[:12]}...` lines {ex.line_range[0]}-{ex.line_range[1]}")
        lines.append(f"")
        lines.append(f"**Root cause**: {ex.root_cause}")
        lines.append(f"")

        neg = ex.negative_example
        lines.append(f"**Negative (what went wrong)**:")
        lines.append(f"- Situation: {neg.get('situation', 'N/A')}")
        lines.append(f"- Action: {neg.get('action', 'N/A')}")
        lines.append(f"- Consequence: {neg.get('consequence', 'N/A')}")
        lines.append(f"")

        pos = ex.positive_example
        lines.append(f"**Positive (what should have happened)**:")
        lines.append(f"- Action: {pos.get('action', 'N/A')}")
        lines.append(f"- Reasoning: {pos.get('reasoning', 'N/A')}")
        lines.append(f"")
        lines.append("---")
        lines.append("")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))

    return len(examples)
