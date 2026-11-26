#!/usr/bin/env python3
"""
Context Harvester — Extract structured context windows around detected gaps.

For each GapRegion, reads the surrounding JSONL lines and produces a
self-contained context snippet suitable for LLM analysis. Captures:
  - What the agent was trying to do (preceding user message + plan)
  - What it actually did (tool calls with inputs and outputs)
  - What went wrong (error messages, user corrections)
  - What fixed it (subsequent successful approach, if present)
  - The conversation arc (topic, progression, resolution)
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from .gap_detector import GapRegion


CONTEXT_WINDOW = 30  # lines before/after gap to capture
MAX_TEXT_LEN = 800
MAX_SNIPPET_SIZE = 6000  # target ~4-6KB per snippet for LLM analysis


@dataclass
class HarvestedContext:
    gap: dict
    intent: str
    actions: list
    failure: dict
    resolution: Optional[dict]
    conversation_arc: str
    raw_snippet: str
    estimated_tokens: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class _Action:
    line_num: int
    role: str  # user, assistant
    action_type: str  # text, tool_use, tool_result
    tool_name: str = ""
    tool_input_summary: str = ""
    tool_output_summary: str = ""
    text: str = ""
    is_error: bool = False


def _extract_text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, list):
        return [b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return []


def _summarize_tool_input(name: str, inp: dict) -> str:
    if not inp:
        return ""
    if name == "Bash":
        return inp.get("command", "")[:200]
    if name in ("Read", "Edit", "Write"):
        path = inp.get("file_path", "")
        if name == "Edit":
            old = inp.get("old_string", "")[:80]
            new = inp.get("new_string", "")[:80]
            return f"{path} | {old} → {new}"
        return path
    if name == "Grep":
        return f"pattern={inp.get('pattern', '')[:60]} path={inp.get('path', '')}"
    if name == "Agent":
        return inp.get("prompt", "")[:150]
    return str(inp)[:150]


def _is_error_text(text: str) -> bool:
    lower = text.lower()
    return any(w in lower for w in [
        "error", "traceback", "exception", "failed", "permission denied",
        "not found", "exit code", "panic", "killed", "timeout",
    ])


def _line_actions(line_num: int, obj: dict) -> list[_Action]:
    """Transform one parsed JSONL object into its _Action records (pure)."""
    msg_type = obj.get("type", "")
    if msg_type not in ("user", "assistant"):
        return []
    content = obj.get("message", {}).get("content", "")
    actions: list[_Action] = []

    if msg_type == "user":
        texts = _extract_text_blocks(content)
        for text in texts:
            if text and len(text) > 5 and "system-reminder" not in text.lower()[:30]:
                actions.append(_Action(
                    line_num=line_num,
                    role="user",
                    action_type="text",
                    text=text[:MAX_TEXT_LEN],
                ))

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    result_texts = _extract_text_blocks(block.get("content", ""))
                    result_text = "\n".join(result_texts)[:MAX_TEXT_LEN]
                    is_err = _is_error_text(result_text)
                    actions.append(_Action(
                        line_num=line_num,
                        role="user",
                        action_type="tool_result",
                        tool_output_summary=result_text,
                        is_error=is_err,
                    ))

    elif msg_type == "assistant":
        texts = _extract_text_blocks(content)
        for text in texts:
            if text and len(text) > 10:
                actions.append(_Action(
                    line_num=line_num,
                    role="assistant",
                    action_type="text",
                    text=text[:MAX_TEXT_LEN],
                ))

        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input", {})
                    actions.append(_Action(
                        line_num=line_num,
                        role="assistant",
                        action_type="tool_use",
                        tool_name=name,
                        tool_input_summary=_summarize_tool_input(name, inp),
                    ))
    return actions


def _finalize_context(gap: GapRegion, actions: list[_Action]) -> HarvestedContext:
    """Synthesize the structured fields from the collected window actions."""
    intent = _extract_intent(actions, gap)
    failure = _extract_failure(actions, gap)
    resolution = _extract_resolution(actions, gap)
    arc = _build_conversation_arc(actions, gap)
    raw_snippet = _build_raw_snippet(actions, gap)

    return HarvestedContext(
        gap=gap.to_dict(),
        intent=intent,
        actions=[_action_to_dict(a) for a in actions],
        failure=failure,
        resolution=resolution,
        conversation_arc=arc,
        raw_snippet=raw_snippet,
        estimated_tokens=len(raw_snippet) // 4,
    )


def harvest_context(gap: GapRegion, extra_window: int = 0) -> HarvestedContext:
    """Read the session JSONL and extract structured context around a gap."""
    path = Path(gap.session_path)
    window = CONTEXT_WINDOW + extra_window

    target_start = gap.start_line - window
    target_end = gap.end_line + window

    actions = []
    with open(path, errors="replace") as f:
        for line_num, raw in enumerate(f, 1):
            if line_num < target_start:
                continue
            if line_num > target_end:
                break
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            actions.extend(_line_actions(line_num, obj))

    return _finalize_context(gap, actions)


def _extract_intent(actions: list[_Action], gap: GapRegion) -> str:
    """What was the agent trying to do? Look for the preceding user message."""
    if gap.user_message:
        return gap.user_message[:500]

    for a in actions:
        if a.role == "user" and a.action_type == "text" and a.line_num <= gap.start_line:
            if len(a.text) > 20:
                return a.text[:500]
    return "Unknown intent"


def _extract_failure(actions: list[_Action], gap: GapRegion) -> dict:
    """What went wrong? Collect errors, corrections, and failed tool calls."""
    errors = []
    corrections = []
    failed_tools = []

    for a in actions:
        if a.is_error:
            errors.append(a.tool_output_summary[:300])
        if a.role == "user" and a.action_type == "text" and a.line_num >= gap.start_line:
            if gap.correction_text and gap.correction_text[:50] in a.text:
                corrections.append(a.text[:300])

    if gap.tool_errors:
        errors.extend(e[:300] for e in gap.tool_errors[:5])

    return {
        "errors": errors[:5],
        "corrections": corrections[:3],
        "failed_tools": [
            {"name": a.tool_name, "input": a.tool_input_summary[:200]}
            for a in actions
            if a.action_type == "tool_use" and a.line_num >= gap.start_line and a.line_num <= gap.end_line
        ][:5],
        "gap_type": gap.gap_type,
        "category": gap.category,
    }


def _extract_resolution(actions: list[_Action], gap: GapRegion) -> Optional[dict]:
    """What fixed it? Look for successful tool calls after the gap."""
    post_gap_tools = []
    post_gap_text = []

    for a in actions:
        if a.line_num <= gap.end_line:
            continue
        if a.action_type == "tool_use":
            post_gap_tools.append({"name": a.tool_name, "input": a.tool_input_summary[:200]})
        if a.action_type == "text" and a.role == "assistant" and len(a.text) > 20:
            post_gap_text.append(a.text[:300])

    if not post_gap_tools and not post_gap_text:
        return None

    return {
        "approach": post_gap_text[0] if post_gap_text else "",
        "tools_used": post_gap_tools[:5],
    }


def _build_conversation_arc(actions: list[_Action], gap: GapRegion) -> str:
    """One-paragraph summary of the conversation flow around the gap."""
    parts = []

    pre_gap = [a for a in actions if a.line_num < gap.start_line]
    in_gap = [a for a in actions if gap.start_line <= a.line_num <= gap.end_line]
    post_gap = [a for a in actions if a.line_num > gap.end_line]

    if pre_gap:
        tool_names = [a.tool_name for a in pre_gap if a.tool_name][:5]
        parts.append(f"Before gap: {len(pre_gap)} actions ({', '.join(tool_names) if tool_names else 'text only'})")

    if in_gap:
        errors = sum(1 for a in in_gap if a.is_error)
        tools = sum(1 for a in in_gap if a.action_type == "tool_use")
        parts.append(f"During gap (L{gap.start_line}-{gap.end_line}): {tools} tool calls, {errors} errors, type={gap.gap_type}")

    if post_gap:
        tool_names = [a.tool_name for a in post_gap if a.tool_name][:5]
        parts.append(f"After gap: {len(post_gap)} actions ({', '.join(tool_names) if tool_names else 'text only'})")

    return " → ".join(parts)


def _build_raw_snippet(actions: list[_Action], gap: GapRegion) -> str:
    """Build a raw text snippet for LLM analysis, capped at MAX_SNIPPET_SIZE."""
    lines = []
    lines.append(f"=== GAP: {gap.gap_type} [{gap.category}] severity={gap.severity} ===")
    lines.append(f"Session: {gap.session_id}")
    lines.append(f"Lines: {gap.start_line}-{gap.end_line}")
    lines.append(f"Summary: {gap.summary}")
    lines.append("")

    budget = MAX_SNIPPET_SIZE - len("\n".join(lines))

    for a in actions:
        if budget <= 0:
            break

        if a.action_type == "text":
            prefix = "USER:" if a.role == "user" else "ASSISTANT:"
            entry = f"[L{a.line_num}] {prefix} {a.text[:300]}"
        elif a.action_type == "tool_use":
            entry = f"[L{a.line_num}] TOOL: {a.tool_name}({a.tool_input_summary[:150]})"
        elif a.action_type == "tool_result":
            err_marker = " !!ERROR!!" if a.is_error else ""
            entry = f"[L{a.line_num}] RESULT{err_marker}: {a.tool_output_summary[:200]}"
        else:
            continue

        budget -= len(entry) + 1
        if budget > 0:
            lines.append(entry)

    return "\n".join(lines)


def _action_to_dict(a: _Action) -> dict:
    return {
        "line_num": a.line_num,
        "role": a.role,
        "type": a.action_type,
        "tool_name": a.tool_name or None,
        "tool_input": a.tool_input_summary or None,
        "tool_output": a.tool_output_summary[:200] if a.tool_output_summary else None,
        "text": a.text[:300] if a.text else None,
        "is_error": a.is_error,
    }


def harvest_gaps(gaps: list[GapRegion], max_gaps: int = 50) -> list[HarvestedContext]:
    """Harvest context for a list of gaps, up to max_gaps.

    One cursor walk per session FILE: gaps are grouped by
    path and every line is parsed once, its actions distributed to all gaps
    whose window covers it — instead of re-streaming the file from line 1 for
    every gap. Output order matches the input gap order.
    """
    take = gaps[:max_gaps]
    by_path: dict[str, list[int]] = {}
    for idx, gap in enumerate(take):
        by_path.setdefault(gap.session_path, []).append(idx)

    contexts: list[Optional[HarvestedContext]] = [None] * len(take)
    for path, idxs in by_path.items():
        windows = []  # (start, end, actions_list, gap)
        for idx in idxs:
            g = take[idx]
            windows.append((g.start_line - CONTEXT_WINDOW,
                            g.end_line + CONTEXT_WINDOW, [], g, idx))
        last_line = max(w[1] for w in windows)

        try:
            f = open(path, errors="replace")
        except OSError:
            for _, _, _, g, idx in windows:
                contexts[idx] = _finalize_context(g, [])
            continue
        with f:
            for line_num, raw in enumerate(f, 1):
                if line_num > last_line:
                    break
                if not any(s <= line_num <= e for s, e, _, _, _ in windows):
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                acts = _line_actions(line_num, obj)
                if not acts:
                    continue
                for s, e, bucket, _, _ in windows:
                    if s <= line_num <= e:
                        bucket.extend(acts)

        for _, _, bucket, g, idx in windows:
            contexts[idx] = _finalize_context(g, bucket)

    return [c for c in contexts if c is not None]


if __name__ == "__main__":
    import argparse
    from .gap_detector import detect_gaps
    from .session_scanner import scan_project

    parser = argparse.ArgumentParser(description="LogLearner Context Harvester")
    parser.add_argument("--project", default="reference-flywheel")
    parser.add_argument("--max-sessions", type=int, default=3)
    parser.add_argument("--max-gaps", type=int, default=5)
    parser.add_argument("--min-severity", type=int, default=6)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    for i, session in enumerate(scan_project(args.project, primary_only=True)):
        if i >= args.max_sessions:
            break

        gaps = detect_gaps(session.path, session.session_id)
        gaps = [g for g in gaps if g.severity >= args.min_severity]

        if not gaps:
            continue

        contexts = harvest_gaps(gaps, max_gaps=args.max_gaps)

        if args.json:
            for ctx in contexts:
                print(json.dumps(ctx.to_dict()))
        else:
            print(f"\n{'='*70}")
            print(f"Session: {session.session_id[:12]}... ({session.size_bytes / 1024 / 1024:.1f}MB)")
            print(f"High-severity gaps: {len(gaps)}, harvested: {len(contexts)}")

            for ctx in contexts:
                print(f"\n  --- Gap: {ctx.gap['gap_type']} [{ctx.gap['category']}] "
                      f"severity={ctx.gap['severity']} L{ctx.gap['start_line']}-{ctx.gap['end_line']} ---")
                print(f"  Intent: {ctx.intent[:100]}")
                print(f"  Arc: {ctx.conversation_arc[:120]}")
                if ctx.failure.get("errors"):
                    print(f"  Errors: {ctx.failure['errors'][0][:100]}")
                if ctx.resolution:
                    print(f"  Resolution: {ctx.resolution.get('approach', '')[:100]}")
                print(f"  Snippet size: {len(ctx.raw_snippet)} chars (~{ctx.estimated_tokens} tokens)")
