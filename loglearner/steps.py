"""
Pipeline Step wrappers around the existing LogLearner modules.

Each Step is a thin adapter that:
  1. Reads its inputs from PipelineContext (declared in `requires`).
  2. Calls an existing function in loglearner/*.py (unchanged).
  3. Writes outputs back to PipelineContext (declared in `provides`).

No new domain logic lives here. If you want to add a real new stage
(e.g. tool-pattern mining, verification gating), add a new file for the
algorithm and a new Step class here to plumb it in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pipeline import PipelineContext
from .session_scanner import scan_project
from .gap_detector import detect_gaps
from .detectors_extra import detect_gaps_extended
from .context_harvester import harvest_gaps
from .example_synthesizer import (
    QUERY_MODEL_SCRIPT,
    synthesize_batch,
    write_instincts,
    write_paired_jsonl,
    write_markdown_report,
)
from .adapters import detect_adapters_for_session
from .verification import classify_verdict, Verdict
from .sinks.hindsight_sink import retain_to_hindsight, hindsight_available, HindsightConfig
from .telemetry import emit_event


@dataclass
class ScanStep:
    name: str = "scan"
    requires: tuple[str, ...] = ("project",)
    provides: tuple[str, ...] = ("sessions",)
    max_sessions: int = 20
    primary_only: bool = True

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        sessions = []
        for session in scan_project(ctx.get("project"), primary_only=self.primary_only):
            sessions.append(session)
            if len(sessions) >= self.max_sessions:
                break
        return ctx.set("sessions", sessions)


@dataclass
class AdapterDispatchStep:
    """Compute domain-adapter hints per session (kubernetes / nccl_trace / bench_output / ...).

    Result is stored in ctx as `session_adapters: dict[session_id -> list[adapter_names]]`
    rather than mutating SessionMetadata. Downstream steps (N-gram miner, verification)
    will key into this map by session_id.
    """

    name: str = "adapter_dispatch"
    requires: tuple[str, ...] = ("sessions",)
    provides: tuple[str, ...] = ("session_adapters",)
    sample_lines: int = 200

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        mapping: dict[str, list[str]] = {}
        for s in ctx.get("sessions", []):
            mapping[s.session_id] = detect_adapters_for_session(s.path, sample_lines=self.sample_lines)
        return ctx.set("session_adapters", mapping)


@dataclass
class DetectGapsStep:
    name: str = "detect_gaps"
    requires: tuple[str, ...] = ("sessions", "min_severity", "max_gaps")
    provides: tuple[str, ...] = ("gaps",)
    use_extended: bool = True

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        min_sev = ctx.get("min_severity", 6)
        max_gaps = ctx.get("max_gaps", 50)
        detector = detect_gaps_extended if self.use_extended else detect_gaps
        all_gaps: list = []
        for session in ctx.get("sessions", []):
            gaps = detector(session.path, session.session_id)
            gaps = [g for g in gaps if g.severity >= min_sev]
            all_gaps.extend(gaps)
        all_gaps.sort(key=lambda g: -g.severity)
        all_gaps = all_gaps[:max_gaps]
        return ctx.set("gaps", all_gaps)


@dataclass
class HarvestContextStep:
    name: str = "harvest_context"
    requires: tuple[str, ...] = ("gaps",)
    provides: tuple[str, ...] = ("contexts",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        gaps = ctx.get("gaps", [])
        contexts = harvest_gaps(gaps, max_gaps=len(gaps))
        return ctx.set("contexts", contexts)


@dataclass
class SynthesizeStep:
    name: str = "synthesize"
    requires: tuple[str, ...] = ("contexts",)
    provides: tuple[str, ...] = ("examples",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        use_llm = ctx.get("use_llm", True)
        if use_llm and not QUERY_MODEL_SCRIPT.exists():
            use_llm = False
        examples = synthesize_batch(ctx.get("contexts", []), use_consensus=use_llm)
        return ctx.update(examples=examples, effective_use_llm=use_llm)


@dataclass
class VerifyStep:
    """Assign KEEP / MAYBE / REVERT to each synthesized example.

    Input: examples (list of SynthesizedExample)
    Output: verified_examples (KEEP only), maybe_examples (MAYBE), rejected (REVERT),
            and verification_report (list of dicts for rendering).
    """

    name: str = "verify"
    requires: tuple[str, ...] = ("examples",)
    provides: tuple[str, ...] = ("verified_examples", "verification_report")

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        examples = ctx.get("examples", [])
        # Build a per-rule registry from the batch so merge_count reflects
        # repeated discovery across sessions within this run.
        import hashlib
        reg = {"rule_sessions": {}, "rule_projects": {}}
        project = ctx.get("project", "")
        for ex in examples:
            rkey = hashlib.sha1((getattr(ex, "instinct_rule", "") or "").encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
            reg["rule_sessions"].setdefault(rkey, set()).add(getattr(ex, "session_id", "?") or "?")
            if project:
                reg["rule_projects"].setdefault(rkey, set()).add(project)
        verified: list = []
        maybe: list = []
        rejected: list = []
        report: list[dict] = []
        for ex in examples:
            rule_dict = _example_to_rule_dict(ex, registry=reg)
            result = classify_verdict(rule_dict)
            report_entry = {**result.to_dict(), "rule_summary": rule_dict.get("rule", "")[:120]}
            report.append(report_entry)
            if result.verdict == Verdict.KEEP:
                verified.append(ex)
            elif result.verdict == Verdict.MAYBE:
                maybe.append(ex)
            else:
                rejected.append(ex)
        return ctx.update(
            verified_examples=verified,
            maybe_examples=maybe,
            rejected_examples=rejected,
            verification_report=report,
        )


def _example_to_rule_dict(example, registry: dict | None = None) -> dict:
    """Translate a SynthesizedExample into the rule dict schema that
    classify_verdict() expects.

    `registry` carries runtime-discovered provenance signals:
      registry["rule_sessions"][rule_text_hash] -> set of session_ids seen
      registry["rule_projects"][rule_text_hash] -> set of projects seen

    If a registry is provided, merge_count / cross_project / evidence_sources
    reflect real cross-session and cross-project observations rather than
    the hardcoded defaults that made verification unreachable in single-session
    mining runs (bug caught in review 2026-05-13).
    """
    import hashlib
    line_range = getattr(example, "line_range", (0, 0)) or (0, 0)
    session_id = getattr(example, "session_id", "?") or "?"
    rule_text = getattr(example, "instinct_rule", "") or ""
    rule_key = hashlib.sha1(rule_text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]

    sessions: set[str] = {session_id}
    projects: set[str] = set()
    evidence: list[str] = [f"session:{session_id} lines:{line_range[0]}-{line_range[1]}"]

    if registry is not None:
        seen_sessions = registry.get("rule_sessions", {}).get(rule_key, set())
        seen_projects = registry.get("rule_projects", {}).get(rule_key, set())
        sessions |= seen_sessions
        projects |= seen_projects
        for sid in sorted(seen_sessions):
            if sid != session_id:
                evidence.append(f"session:{sid}")

    return {
        "id": session_id[:8] + f"-L{line_range[0]}",
        "rule": rule_text,
        "confidence": int(getattr(example, "confidence", 5) or 5),
        "category": getattr(example, "category", "unknown") or "unknown",
        "evidence_sources": evidence,
        "source_projects": sorted(projects),
        "merge_count": max(1, len(sessions)),
        "cross_project": len(projects) >= 2,
    }


@dataclass
class WriteToHindsightStep:
    """Push verified rules into a vectorize-io/hindsight bank for runtime recall.

    No-op when:
    - LOGLEARNER_HINDSIGHT_DISABLE=1 in env
    - hindsight_api package is not installed
    - ctx.get('hindsight') is falsy
    - HindsightConfig validation fails (missing db_url / llm_api_key)

    Hindsight is the runtime memory layer. This step is the
    only place inside LogLearner that talks to it. Callers opt in via
    --hindsight on the CLI or by setting ctx['hindsight']=True.
    """

    name: str = "write_to_hindsight"
    requires: tuple[str, ...] = ("verified_examples",)
    provides: tuple[str, ...] = ("hindsight_observation_ids",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        import os
        if os.environ.get("LOGLEARNER_HINDSIGHT_DISABLE") == "1":
            return ctx.set("hindsight_observation_ids", [])
        if not ctx.get("hindsight"):
            return ctx.set("hindsight_observation_ids", [])
        if not hindsight_available():
            print("  ⚠ WriteToHindsightStep: hindsight_api not installed; skipping (pip install loglearning[hindsight])")
            return ctx.set("hindsight_observation_ids", [])

        verified = ctx.get("verified_examples", []) or []
        if not verified:
            return ctx.set("hindsight_observation_ids", [])

        # Convert SynthesizedExample → rule dict using the same translator
        rules = [_example_to_rule_dict(ex) for ex in verified]

        cfg = HindsightConfig.from_env()
        bank = ctx.get("hindsight_bank_id") or cfg.bank_id
        cfg = HindsightConfig(
            db_url=cfg.db_url, bank_id=bank, llm_provider=cfg.llm_provider,
            llm_api_key=cfg.llm_api_key, llm_model=cfg.llm_model,
        )
        try:
            res = retain_to_hindsight(rules, cfg=cfg, return_full=True)
            obs_ids = res["retained_ids"]
            n_skip = len(res["skipped"])
            n_fail = len(res["failures"])
            tally = f"{len(obs_ids)} success"
            if n_skip:
                tally += f", {n_skip} skipped (Hindsight extracted no entities)"
            if n_fail:
                tally += f", {n_fail} FAILED"
            print(f"  ✓ Hindsight: {tally} into bank '{bank}'")
            emit_event("hindsight_retain_batch", {
                "bank": bank,
                "total": res["total"],
                "success": len(obs_ids),
                "skipped": n_skip,
                "failures": n_fail,
                "skip_rule_ids": [s["rule_id"] for s in res["skipped"]],
                "fail_rule_ids": [f["rule_id"] for f in res["failures"]],
            })
        except Exception as e:
            print(f"  ⚠ Hindsight retain failed: {type(e).__name__}: {e}")
            obs_ids = []
            emit_event("hindsight_retain_error", {"bank": bank, "error": f"{type(e).__name__}: {str(e)[:200]}"})
        return ctx.set("hindsight_observation_ids", obs_ids)


@dataclass
class WriteOutputsStep:
    name: str = "write_outputs"
    requires: tuple[str, ...] = ("examples", "gaps", "output_dir", "project")
    provides: tuple[str, ...] = ("output_paths",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        out = Path(ctx.get("output_dir"))
        out.mkdir(parents=True, exist_ok=True)

        # Prefer verified-only if VerifyStep ran; fall back to all examples.
        verified = ctx.get("verified_examples")
        maybe = ctx.get("maybe_examples", [])
        all_examples = ctx.get("examples", [])
        primary = verified if verified is not None else all_examples
        gaps = ctx.get("gaps", [])

        instincts_path = out / "instincts.json"
        paired_path = out / "examples.jsonl"
        report_path = out / "report.md"
        gaps_path = out / "gaps.jsonl"
        maybe_path = out / "instincts.maybe.json"
        verif_path = out / "verification.md"

        n_inst = write_instincts(primary, instincts_path)
        n_paired = write_paired_jsonl(primary, paired_path)
        n_report = write_markdown_report(primary, report_path, project=ctx.get("project"))

        # Mint-time evidence archival (mandated by the calibration audit):
        # scrubbed digests of each shipped rule's cited window survive
        # session retention. Fail-closed — no scrub config / detector hit
        # persists a status, never text.
        from .evidence_digest import mint_digests_for_examples
        digests_path = out / "evidence-digests.jsonl"
        digest_counts = mint_digests_for_examples(primary, digests_path)

        with open(gaps_path, "w") as f:
            for g in gaps:
                f.write(json.dumps(g.to_dict()) + "\n")

        paths = {
            "instincts": str(instincts_path),
            "examples": str(paired_path),
            "report": str(report_path),
            "gaps": str(gaps_path),
            "evidence_digests": str(digests_path),
        }
        counts = {"instincts": n_inst, "examples": n_paired, "report_entries": n_report, "gaps": len(gaps),
                  "evidence_digests": digest_counts}

        # Emit MAYBE + verification report when available
        if verified is not None:
            n_maybe = write_instincts(maybe, maybe_path)
            counts["maybe"] = n_maybe
            paths["maybe"] = str(maybe_path)
            _write_verification_md(verif_path, ctx.get("verification_report", []),
                                   keep_count=len(verified), maybe_count=len(maybe),
                                   revert_count=len(ctx.get("rejected_examples", [])))
            paths["verification"] = str(verif_path)

        paths["counts"] = counts
        return ctx.set("output_paths", paths)


def _write_verification_md(path: Path, report: list[dict], *,
                            keep_count: int, maybe_count: int, revert_count: int) -> None:
    with open(path, "w") as f:
        f.write("# Rule Verification Report\n\n")
        f.write(f"- **KEEP**: {keep_count}\n- **MAYBE**: {maybe_count}\n- **REVERT**: {revert_count}\n\n")
        f.write("| ID | Verdict | Score | Reasons | Rule |\n")
        f.write("|---|---|---:|---|---|\n")
        verdict_order = {"KEEP": 0, "MAYBE": 1, "REVERT": 2}
        for e in sorted(report, key=lambda r: (verdict_order.get(r.get("verdict", ""), 9), -r.get("score", 0))):
            rule_txt = e.get("rule_summary", "").replace("|", "\\|")
            reasons = "; ".join(e.get("reasons", [])).replace("|", "\\|")
            f.write(f"| `{e['rule_id']}` | {e['verdict']} | {e['score']:.2f} | {reasons} | {rule_txt} |\n")


@dataclass
class MeasureRuleImpactStep:
    """Close the UCB loop (corrected after review): reward
    previously minted arms by whether their gap signature recurs in sessions
    that post-date the mint AND actually RECEIVED the rule (exposure ledger,
    arms.record_exposure) AND match the arm's project. Runs right after gap
    detection — it only measures arms minted in PRIOR runs, so it is
    independent of this run's synthesis. reward=1 when exposed, signature-
    matched later sessions stop showing the gap; recurrence means
    reward=0/regret=1; no exposed post-mint session means UNMEASURED (skip,
    no bandit pull). Observations are evidence-keyed, so replays dedupe.
    Persists output/loglearner_rewards.jsonl + output/state/ucb.json.
    """

    name: str = "measure_rule_impact"
    requires: tuple[str, ...] = ("sessions", "gaps")
    provides: tuple[str, ...] = ("arm_observations",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        from loglearner.arms import measure_arms

        observations, _bandit = measure_arms(
            ctx.get("sessions", []), ctx.get("gaps", []))
        return ctx.set("arm_observations", observations)


@dataclass
class MintArmsStep:
    """Mint one fleet-shape arm per KEEP rule (reward null at mint) into
    output/loglearner_arms.jsonl. Arm ids are rule-text hashes, so re-mining
    the same rule never duplicates an arm."""

    name: str = "mint_arms"
    requires: tuple[str, ...] = ("verified_examples",)
    provides: tuple[str, ...] = ("minted_arms",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        from loglearner.arms import append_jsonl, arms_path, load_arms, mint_arm

        existing = {a.get("arm") for a in load_arms()}
        minted = []
        for ex in ctx.get("verified_examples", []):
            rec = mint_arm(ex, project=ctx.get("project", ""))
            if rec["arm"] not in existing:
                minted.append(rec)
                existing.add(rec["arm"])
        append_jsonl(arms_path(), minted)
        return ctx.set("minted_arms", minted)


@dataclass
class WriteIsolatesStep:
    """Emit one prompt-injectable isolate JSON per KEEP example
    (after verification, so only KEEPs ship). Isolates land in
    output/isolates/ — which is inside the scrub-gated export scope."""

    name: str = "write_isolates"
    requires: tuple[str, ...] = ("verified_examples",)
    provides: tuple[str, ...] = ("isolate_paths",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        from loglearner.isolate import write_isolates

        paths = write_isolates(
            ctx.get("verified_examples", []),
            contexts=ctx.get("contexts", []),
            project=ctx.get("project", ""),
            session_adapters=ctx.get("session_adapters", {}),
        )
        return ctx.set("isolate_paths", [str(p) for p in paths])


@dataclass
class ComputeStateVectorStep:
    """SessionStateVector per scanned session. In the
    pipeline this is built from already-computed ctx data (metadata counters,
    adapters, detected gaps) — the full-fidelity variant with file/bash/error
    sets lives in the dashboard indexer where the file is streamed anyway.
    Fixes the review's complaint that AdapterDispatchStep output died on
    ctx.state: domains now land in a persisted, comparable artifact."""

    name: str = "compute_state_vector"
    requires: tuple[str, ...] = ("sessions", "session_adapters", "gaps")
    provides: tuple[str, ...] = ("state_vectors",)

    def __call__(self, ctx: PipelineContext) -> PipelineContext:
        from loglearner.state_vector import (
            SessionStateVector, duration_minutes, normalize_tool_dist)

        adapters = ctx.get("session_adapters", {})
        gaps_by_sid: dict[str, set] = {}
        for g in ctx.get("gaps", []):
            gaps_by_sid.setdefault(g.session_id, set()).add(g.gap_type)
        vectors = {}
        for s in ctx.get("sessions", []):
            vectors[s.session_id] = SessionStateVector(
                session_id=s.session_id,
                project=s.project,
                domains=tuple(adapters.get(s.session_id, [])),
                duration_min=duration_minutes(*(s.date_range or ("", ""))),
                tool_dist=normalize_tool_dist(s.tool_call_names or {}),
                gap_types=frozenset(gaps_by_sid.get(s.session_id, set())),
                outcome_signal=("ended_context_overflow"
                                if s.context_overflows else "ended_clean"),
            )
        return ctx.set("state_vectors", vectors)
