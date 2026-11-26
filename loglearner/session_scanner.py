#!/usr/bin/env python3
"""
Session Scanner — Stream-parse Claude Code JSONL logs and classify sessions.

Reads ~/.claude/projects/<project>/*.jsonl, classifies each as:
  primary   — Real work sessions with tool calls
  observer  — claude-mem watchers (observed_from_primary_session)
  cron      — Autonomous monitoring (>80% identical user messages)
  subagent  — Agent-dispatched sub-sessions

Outputs structured session metadata for downstream gap detection.
"""

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Optional


# Any ~/.claude/projects-shaped tree works — point CLAUDE_PROJECTS_ROOT at it.
PROJECTS_ROOT = Path(
    os.environ.get("CLAUDE_PROJECTS_ROOT", Path.home() / ".claude" / "projects")
)

OBSERVER_MARKER = "observed_from_primary_session"
CRON_REPETITION_THRESHOLD = 0.80


@dataclass
class SessionMetadata:
    session_id: str
    project: str
    path: str
    size_bytes: int
    classification: str  # primary, observer, cron, subagent
    total_lines: int = 0
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_call_names: dict = field(default_factory=dict)
    user_corrections: int = 0
    tool_errors: int = 0
    context_overflows: int = 0
    date_range: tuple = ("", "")
    top_tools: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def find_project_dir(project_name: str) -> Optional[Path]:
    """Find a project directory by partial name match."""
    if not PROJECTS_ROOT.exists():
        return None
    for d in sorted(PROJECTS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if project_name in d.name:
            return d
    return None


def list_projects() -> list[dict]:
    """List all projects with session counts and sizes."""
    if not PROJECTS_ROOT.exists():
        return []
    projects = []
    for d in sorted(PROJECTS_ROOT.iterdir()):
        if not d.is_dir():
            continue
        jsonl_files = list(d.glob("*.jsonl"))
        total_size = sum(f.stat().st_size for f in jsonl_files)
        projects.append({
            "name": d.name,
            "sessions": len(jsonl_files),
            "size_bytes": total_size,
            "size_mb": round(total_size / 1024 / 1024, 1),
        })
    return sorted(projects, key=lambda p: -p["size_bytes"])


def _extract_text_from_content(content) -> list[str]:
    """Extract text strings from message content (string or block array)."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
        return texts
    return []


def _extract_tool_results(content) -> list[dict]:
    """Extract tool_result blocks from user message content."""
    if not isinstance(content, list):
        return []
    results = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            results.append(block)
    return results


def _count_tool_uses(content) -> list[str]:
    """Extract tool_use names from assistant message content."""
    if not isinstance(content, list):
        return []
    names = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            names.append(block.get("name", "unknown"))
    return names


# Correction matching is two-tier: STRONG markers
# are unambiguous correction language and count anywhere in the text; WEAK
# markers ("no", "wait", "wrong", ...) are everyday prose words that only
# count when they OPEN the message — that is where a human interrupts the
# agent. A guard kills the "no issues/no errors/no need" negation family.
_CORRECTION_STRONG = re.compile(
    r"\b(revert\b|undo\b|roll(?:ing)? back\b|go back\b|put it back\b|"
    r"that broke\b|you broke\b|try again\b|not what i\b|"
    r"still (?:failing|broken|wrong)\b|(?:doesn|didn)'?t work\b|"
    r"stop (?:doing|that|this|it)\b|stop and return\b)",
    re.IGNORECASE,
)
_CORRECTION_WEAK_INITIAL = re.compile(
    r"^(no[,.!]?\s|stop\b|wait\b|wrong\b|actually[,.]?\s|hold on\b|"
    r"not that\b|don'?t\b)",
    re.IGNORECASE,
)
_CORRECTION_NEG_GUARD = re.compile(
    r"^no\s+(issues?|errors?|problems?|changes?|worries|need|longer|more|"
    r"matches|results|files?|tests?|new\b|other\b|one\b)",
    re.IGNORECASE,
)


def _is_correction(text: str) -> bool:
    if _CORRECTION_STRONG.search(text):
        return True
    t = text.lstrip()
    return bool(_CORRECTION_WEAK_INITIAL.match(t)
                and not _CORRECTION_NEG_GUARD.match(t))


class _CorrectionMatcher:
    """Back-compat shim: exposes .search() like the old CORRECTION_PATTERN
    regex so existing call sites and external scripts keep working."""

    @staticmethod
    def search(text):
        return _is_correction(text) or None


CORRECTION_PATTERN = _CorrectionMatcher()

ERROR_PATTERN = re.compile(
    r"(error|traceback|exception|permission denied|not found|"
    r"command not found|No such file|FAILED|panic|segfault|"
    r"core dumped|killed|timeout|OOM)",
    re.IGNORECASE,
)

CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"(ran out of context|being continued from a previous conversation|"
    r"context limit|conversation was compressed)",
    re.IGNORECASE,
)


def classify_session(path: Path, sample_lines: int = 300) -> str:
    """Classify a session by sampling the first N lines.

    Returns: 'primary', 'observer', 'cron', or 'subagent'
    """
    if "/subagents/" in str(path) or path.name.startswith("agent-"):
        return "subagent"

    observer_hits = 0
    user_prefixes = []
    tool_use_count = 0
    user_count = 0

    with open(path) as f:
        for i, line in enumerate(f):
            if i >= sample_lines:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")

            if msg_type == "user":
                user_count += 1
                content = obj.get("message", {}).get("content", "")
                texts = _extract_text_from_content(content)
                for text in texts:
                    if OBSERVER_MARKER in text:
                        observer_hits += 1
                    if len(text) > 10:
                        user_prefixes.append(text[:60])

            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                tool_use_count += len(_count_tool_uses(content))

    return _classify_from_signals(
        observer_hits, user_count, user_prefixes, tool_use_count
    )


def _classify_from_signals(observer_hits, user_count, user_prefixes,
                           tool_use_count) -> str:
    """Shared classification policy over first-N-line signals."""
    if observer_hits > 0 and observer_hits >= user_count * 0.3:
        return "observer"

    if user_count >= 10:
        prefix_counts = Counter(user_prefixes)
        if prefix_counts:
            most_common_count = prefix_counts.most_common(1)[0][1]
            if most_common_count / max(len(user_prefixes), 1) > CRON_REPETITION_THRESHOLD:
                return "cron"

    if tool_use_count == 0 and user_count > 5:
        return "observer"

    return "primary"


_INJECTED_PREFIXES = (
    "<system-reminder",
    "<task-notification",
    "<teammate-message",
    "<command-name",
    "<local-command",
    "Base directory for this skill",
    "Caveat: the messages below",
    # teammate relays arrive wrapped in prose before the XML tag
    "Another Claude session sent",
    "[SYSTEM NOTIFICATION",
)


def scan_session(path: Path, project_name: str,
                 classify_sample_lines: int = 300) -> SessionMetadata:
    """Full scan of a session file — SINGLE pass.

    Classification signals accumulate inline over the first
    `classify_sample_lines` lines; observer/cron sessions still stop reading
    at that boundary (same early-exit as before), while primary/subagent
    sessions no longer pay a second full stream for metadata.
    """
    session_id = path.stem
    size_bytes = path.stat().st_size

    meta = SessionMetadata(
        session_id=session_id,
        project=project_name,
        path=str(path),
        size_bytes=size_bytes,
        classification="primary",
    )
    bare = SessionMetadata(  # what observer/cron callers historically got
        session_id=session_id,
        project=project_name,
        path=str(path),
        size_bytes=size_bytes,
        classification="primary",
    )

    is_subagent = "/subagents/" in str(path) or path.name.startswith("agent-")

    # classification signals (first N lines only)
    observer_hits = 0
    cls_user_count = 0
    cls_tool_uses = 0
    user_prefixes: list = []
    classified = is_subagent  # subagents skip signal-based classification

    tool_counter = Counter()
    timestamps = []
    line_count = 0

    with open(path, errors="replace") as f:
        for line in f:
            line_count += 1
            if not classified and line_count > classify_sample_lines:
                cls = _classify_from_signals(
                    observer_hits, cls_user_count, user_prefixes, cls_tool_uses)
                if cls in ("observer", "cron"):
                    bare.classification = cls
                    return bare
                classified = True

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type")

            ts = obj.get("timestamp")
            if ts:
                timestamps.append(ts)

            if msg_type == "user":
                meta.user_messages += 1
                content = obj.get("message", {}).get("content", "")
                texts = _extract_text_from_content(content)

                if not classified:
                    cls_user_count += 1
                    for text in texts:
                        if OBSERVER_MARKER in text:
                            observer_hits += 1
                        if len(text) > 10:
                            user_prefixes.append(text[:60])

                for text in texts:
                    # Injected envelopes arrive as user-type messages but are
                    # not the human speaking; counting them inflates
                    # user_corrections (measured: skill bodies and
                    # task-notifications dominate the false positives).
                    if text.lstrip()[:40].startswith(_INJECTED_PREFIXES):
                        continue
                    if CORRECTION_PATTERN.search(text):
                        meta.user_corrections += 1
                    if CONTEXT_OVERFLOW_PATTERN.search(text):
                        meta.context_overflows += 1

                for tr in _extract_tool_results(content):
                    result_content = tr.get("content", "")
                    result_texts = _extract_text_from_content(result_content)
                    for rt in result_texts:
                        if ERROR_PATTERN.search(rt):
                            meta.tool_errors += 1

            elif msg_type == "assistant":
                meta.assistant_messages += 1
                content = obj.get("message", {}).get("content", [])
                tools = _count_tool_uses(content)
                meta.tool_calls += len(tools)
                tool_counter.update(tools)
                if not classified:
                    cls_tool_uses += len(tools)

    if not classified:  # file shorter than the sample window
        cls = _classify_from_signals(
            observer_hits, cls_user_count, user_prefixes, cls_tool_uses)
        if cls in ("observer", "cron"):
            bare.classification = cls
            return bare

    meta.classification = "subagent" if is_subagent else "primary"
    meta.total_lines = line_count
    meta.tool_call_names = dict(tool_counter)
    meta.top_tools = [t for t, _ in tool_counter.most_common(10)]

    if timestamps:
        meta.date_range = (min(timestamps), max(timestamps))

    return meta


def scan_project(project_name: str, primary_only: bool = True) -> Iterator[SessionMetadata]:
    """Scan all sessions in a project. Yields SessionMetadata objects."""
    project_dir = find_project_dir(project_name)
    if not project_dir:
        raise FileNotFoundError(f"No project matching '{project_name}' in {PROJECTS_ROOT}")

    jsonl_files = sorted(project_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    for path in jsonl_files:
        if path.stat().st_size < 10240:
            continue

        meta = scan_session(path, project_name)

        if primary_only and meta.classification != "primary":
            continue

        yield meta


def scan_all_subagents(project_name: str) -> Iterator[SessionMetadata]:
    """Scan all sub-agent session files in a project."""
    project_dir = find_project_dir(project_name)
    if not project_dir:
        return

    for sa_path in project_dir.rglob("subagents/agent-*.jsonl"):
        if sa_path.stat().st_size < 5120:
            continue
        yield scan_session(sa_path, project_name)


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="LogLearner Session Scanner")
    parser.add_argument("--project", default="reference-flywheel", help="Project name (partial match)")
    parser.add_argument("--list-projects", action="store_true", help="List all projects")
    parser.add_argument("--all", action="store_true", help="Include observer/cron sessions")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.list_projects:
        projects = list_projects()
        for p in projects:
            print(f"  {p['name']:<60s} {p['sessions']:>5d} sessions  {p['size_mb']:>8.1f} MB")
        sys.exit(0)

    primary_only = not args.all
    sessions = list(scan_project(args.project, primary_only=primary_only))

    if args.json:
        for s in sessions:
            print(json.dumps(s.to_dict()))
    else:
        print(f"\nProject: {args.project}")
        print(f"Sessions found: {len(sessions)} ({'primary only' if primary_only else 'all'})\n")

        for s in sessions:
            size_mb = s.size_bytes / 1024 / 1024
            print(
                f"  {s.session_id[:12]}... {size_mb:6.1f}MB  "
                f"[{s.classification:>8s}]  "
                f"msgs={s.user_messages:>4d}  tools={s.tool_calls:>5d}  "
                f"corrections={s.user_corrections:>3d}  errors={s.tool_errors:>3d}"
            )

        total_tools = sum(s.tool_calls for s in sessions)
        total_corrections = sum(s.user_corrections for s in sessions)
        total_errors = sum(s.tool_errors for s in sessions)
        print(f"\n  Totals: {total_tools} tool calls, {total_corrections} corrections, {total_errors} errors")
