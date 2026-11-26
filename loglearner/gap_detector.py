#!/usr/bin/env python3
"""
Gap Detector — Find failure patterns in Claude Code session JSONL logs.

Streams through primary sessions finding:
  1. User corrections (explicit negative feedback)
  2. Tool errors (non-zero exit, tracebacks, error messages)
  3. Repeated attempts (3+ tool calls to same target = struggling)
  4. Context overflows (session continuations)
  5. Abandoned approaches (tool sequence then pivot)
  6. Time sinks (high message count for low tool success)

Outputs Gap objects with line ranges, classification, and severity.
"""

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator, Optional


@dataclass
class GapRegion:
    session_id: str
    session_path: str
    start_line: int
    end_line: int
    gap_type: str
    severity: int  # 1-10
    category: str
    summary: str
    user_message: str = ""
    tool_calls: list = field(default_factory=list)
    tool_errors: list = field(default_factory=list)
    correction_text: str = ""
    context_lines: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


# --- Detection patterns ---

CORRECTION_PATTERNS = {
    "explicit_no": re.compile(r"\b(no[,.]?\s+(?:that|don|stop|wrong|not))", re.IGNORECASE),
    "stop_command": re.compile(r"\b(stop\s+(?:doing|that|this|it)|don'?t\s+(?:do|use|add|change|modify|remove))", re.IGNORECASE),
    "wrong_approach": re.compile(r"\b(wrong|incorrect|that'?s not|not what I|that broke|you broke)", re.IGNORECASE),
    "revert_request": re.compile(r"\b(revert|undo|go back|roll back|put it back|restore)", re.IGNORECASE),
    "try_again": re.compile(r"\b(try again|start over|redo|let'?s try|different approach)", re.IGNORECASE),
    "wait_redirect": re.compile(r"\b(wait|hold on|actually|instead|no,?\s+let)", re.IGNORECASE),
}

ERROR_PATTERNS = {
    "traceback": re.compile(r"Traceback \(most recent call last\)"),
    "python_error": re.compile(r"(TypeError|ValueError|KeyError|AttributeError|ImportError|ModuleNotFoundError|FileNotFoundError|NameError|IndexError|RuntimeError|OSError)\b"),
    "bash_error": re.compile(r"(command not found|No such file or directory|Permission denied|cannot access|syntax error)"),
    "build_error": re.compile(r"(FAILED|BUILD FAILURE|compilation error|undefined reference|fatal error|make\[\d+\]: \*\*\*)"),
    "cuda_error": re.compile(r"(CUDA error|cudaError|NCCL error|nvshmem.*error)", re.IGNORECASE),
    "k8s_error": re.compile(r"(CrashLoopBackOff|ImagePullBackOff|OOMKilled|Evicted|error from server)"),
    "exit_code": re.compile(r"Exit code [1-9]\d*"),
}

CONTEXT_OVERFLOW = re.compile(
    r"(continued from a previous conversation|ran out of context|"
    r"context limit|conversation was compressed|picking up where)",
    re.IGNORECASE,
)

# --- Security patterns (from OpenAI misalignment monitoring research 2026-03-19) ---
# Detects agent behavior patterns that indicate attempts to circumvent
# security controls, extract credentials, exfiltrate data, or obfuscate
# commands. See output_llm/openai-misalignment-monitoring-research.md.
SECURITY_PATTERNS = {
    # base64: only flag as security if used with decode or pipe-to-shell —
    # pure mention like "base64 encoded logo" should not fire.
    "base64_encoding": re.compile(
        r"(base64\s+(?:-d|--decode|-w\s*0)|b64encode\s*\(|b64decode\s*\(|atob\s*\(|"
        r"echo\s+[A-Za-z0-9+/=]{30,}\s*\|\s*base64|"
        r"base64\s+[^|]*\|\s*(?:bash|sh|python|eval))",
        re.IGNORECASE,
    ),
    # command_obfuscation: shell-level evasion patterns. exec(open(...)) and
    # pytest-style reads are benign; flag only when the executed target is
    # obfuscated (env, stdin, base64, network, or shell=True with dynamic cmd).
    "command_obfuscation": re.compile(
        r"(eval\s+[\"']?\$\(|"                      # eval $(untrusted)
        r"eval\s+[\"']?\$[A-Z_]+[\"']?\s*$|"        # eval $VAR (dangerous)
        r"subprocess.*\(\s*[a-z_]+\s*,\s*shell\s*=\s*True|"  # subprocess(var, shell=True)
        r"os\.system\s*\(\s*[a-z_]+[^)]*\)|"        # os.system(dynamic)
        r"bash\s+-c\s+['\"]?\$\(|"                  # bash -c "$(untrusted)"
        r"`\s*echo\s+[A-Za-z0-9+/=]{30,}\s*\|\s*base64|"  # `echo BASE64 | base64`
        r"curl\s+[^|;&]*\|\s*(?:bash|sh|zsh)\s*$)", # curl ... | sh
    ),
    "credential_access": re.compile(
        r"(AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY|GOOGLE_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|NGC_API_KEY|"
        r"password\s*=\s*['\"][^'\"]{4,}|secret\s*=\s*['\"][^'\"]{4,}|"
        r"id_rsa\b|credentials\.json|auth\.json|\.netrc)",
    ),
    "data_exfiltration": re.compile(
        r"(curl\s+(?:-X\s+POST|--data[- ]binary|-F\s+)[^|;]*https?://|"
        r"wget\s+.*--post-(?:data|file)|"
        r"requests\.post\s*\(\s*['\"]https?://(?!localhost|127\.)|"
        r"\bnc\s+\S+\s+\d{3,5}|"
        r"curl\s+[^|;&]*\|\s*(?:bash|sh|python))",
    ),
    # restriction_bypass: matches security evasion ONLY. "bypass" in
    # systems-architecture prose (a transport layer bypassing a proxy) is
    # design, not evasion — require the bypass target to be a
    # check/control/policy keyword.
    # Excludes: "bypass permissions" (Claude's own flag), test tooling
    # exec(open()) which is benign pytest pattern.
    "restriction_bypass": re.compile(
        r"((?:bypass|circumvent|evade)\s+(?:the\s+|this\s+|that\s+|a\s+|any\s+)?"
        r"(?:security\s+)?"
        r"(?:check|restriction|limit(?!s?\b)|block|firewall|policy|guard|sandbox|approval|gate|validator|audit|monitor|"
        r"restriction|access\s+control|rate\s+limit|scan)|"
        r"work[-\s]?around(?:\s+(?:the|this|that))?\s+(?:check|restriction|limit|block|firewall|permission|policy|approval|audit)|"
        r"disable\s+(?:the\s+|a\s+)?(?:check|validation|security|audit|monitoring|firewall|sandbox)|"
        r"(?:--no-verify|--no-check|--skip-verify|--insecure|--dangerously-skip-permissions|--dangerously-bypass-approvals)(?:\b|=|\s))",
        re.IGNORECASE,
    ),
    "self_reference": re.compile(
        r"(spawn\s+\S+\s*(?:claude|agent|subagent)|"
        r"spawn\s+(?:claude|agent|subagent)|"
        r"invoke.*claude.*(?:api|cli)|"
        r"recursive.*(?:claude|agent)|"
        r"call\s+(?:myself|another\s+claude)|"
        r"anthropic.*api\.(?:messages|completions))",
        re.IGNORECASE,
    ),
    # filesystem_sensitive is a superset of credential_access for file paths;
    # both can fire if present, dedup is handled by _deduplicate_gaps.
    "filesystem_sensitive": re.compile(
        r"(/etc/(?:shadow|passwd|sudoers)|~/\.ssh/|~/\.aws/credentials|/proc/self/environ|"
        r"/var/run/secrets/|kubectl\s+get\s+secret)",
    ),
}

SECURITY_CATEGORY = "security_circumvention"


CATEGORY_CLASSIFIERS = {
    "symptom_fix_tell": re.compile(
        r"(added?\s+(?:an?\s+)?env\s*(?:var|variable)"
        r"|made\s+(?:\w+\s+)*?(?:configurable|tunable)"
        r"|expose(?:d|s)?\s+(?:\w+\s+)*?as\s+(?:an?\s+)?(?:env|flag|option)"
        r"|new\s+(?:env|flag|option|knob)"
        r"|default(?:ed|s)?\s+to\s+(?:previous|existing|prior)\s+behavior"
        r"|preserve(?:s|d)?\s+(?:existing|prior|previous)\s+default"
        r"|introduce\s+(?:an?\s+)?(?:knob|tunable|option))",
        re.IGNORECASE,
    ),
    "api_misuse": re.compile(r"(fi_\w+|nvshmem\w*|cuda\w+|torch\w*|api|sdk|endpoint|writemsg|FI_MORE)", re.IGNORECASE),
    "build_deploy": re.compile(r"(docker|build|compile|deploy|push|image|pod|container|cmake|makefile|pip install)", re.IGNORECASE),
    "measurement": re.compile(r"(benchmark|latency|throughput|GB/?s|performance|measure|profile|nsys|timing)", re.IGNORECASE),
    "debugging": re.compile(r"(debug|diagnose|investigate|trace|root cause|hypothesis|reproduce|bisect)", re.IGNORECASE),
    "scope_management": re.compile(r"(layer|scope|target|focus|wrong file|wrong function|too broad|too narrow)", re.IGNORECASE),
    "recovery": re.compile(r"(retry|fallback|recover|workaround|alternative|plan b|backup)", re.IGNORECASE),
    "architecture": re.compile(r"(design|architecture|pattern|approach|strategy|refactor|restructure)", re.IGNORECASE),
    "synchronization": re.compile(r"(sync|fence|barrier|race|deadlock|hang|timeout|stall|stuck)", re.IGNORECASE),
}


def _extract_text(content) -> list[str]:
    """Pull text from message content."""
    if isinstance(content, str):
        return [content] if content.strip() else []
    if isinstance(content, list):
        return [b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return []


def _extract_tool_uses(content) -> list[dict]:
    """Pull tool_use blocks from assistant content."""
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"]


def _extract_tool_results(content) -> list[dict]:
    """Pull tool_result blocks from user content."""
    if not isinstance(content, list):
        return []
    return [b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"]


def _tool_result_text(result_block: dict) -> str:
    """Extract text from a tool_result block."""
    content = result_block.get("content", "")
    texts = _extract_text(content)
    return "\n".join(texts)


def _classify_category(texts: list[str]) -> str:
    """Classify gap category from surrounding text."""
    combined = " ".join(texts)[:2000]
    scores = {}
    for cat, pattern in CATEGORY_CLASSIFIERS.items():
        matches = pattern.findall(combined)
        scores[cat] = len(matches)
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
    return "unknown"


def _severity_for_gap_type(gap_type: str, error_count: int, correction_strength: int) -> int:
    """Assign severity 1-10 based on gap type and signals."""
    base = {
        "user_correction": 6,
        "tool_error_cluster": 5,
        "repeated_attempts": 7,
        "context_overflow": 4,
        "abandoned_approach": 8,
        "security_circumvention": 9,
    }.get(gap_type, 3)

    if error_count > 5:
        base = min(base + 2, 10)
    elif error_count > 2:
        base = min(base + 1, 10)

    if correction_strength > 1:
        base = min(base + correction_strength - 1, 10)

    return base


# Bounded-record streaming: every regex that needs FULL
# message text runs once at parse time; the retained record stores only
# capped snippets + precomputed flags, so a 46 MB session no longer holds
# its raw payloads in RAM while detectors run.
_TEXT_CAP = 2000  # >= every downstream slice ([:1000] max) and >= the
                  # _classify_category join window ([:2000]), so capping
                  # cannot change any detector output.

# Shared with detectors_extra.detect_false_success — defined here so the
# parse-time precompute and the detector consume the same pattern objects.
_EXTRA_SUCCESS_CLAIMS = re.compile(
    r"\b(passed|works|success|fixed|all green|test passes|correctness)\b",
    re.IGNORECASE)
_EXTRA_CORRECTION_WORDS = re.compile(
    r"\b(no[,. ]|stop|wrong|revert|that broke|actually|wait|not that"
    r"|didn'?t work|still failing|still broken|fail)\b",
    re.IGNORECASE)


@dataclass
class _ParsedLine:
    line_num: int
    msg_type: str
    timestamp: str = ""
    texts: list = field(default_factory=list)            # each capped at _TEXT_CAP
    tool_uses: list = field(default_factory=list)        # reduced dicts (below)
    # precomputed on FULL text at parse time:
    correction_names: list = field(default_factory=list)  # parallel to texts
    overflow_flags: list = field(default_factory=list)    # parallel to texts
    sysrem_flags: list = field(default_factory=list)      # parallel to texts:
    #   'system-reminder' anywhere in the FULL text (an `in` check the cap
    #   would otherwise silently change — caught by the old/new backtest)
    result_errors: list = field(default_factory=list)     # [(err_type, snippet300)]
    security_hits: list = field(default_factory=list)     # [(pat, kind, snippet)]
    success_claim: bool = False                            # assistant joined texts
    correction_word: bool = False                          # user joined texts


def _reduce_tool_use(block: dict) -> dict:
    """Bounded projection of a tool_use block — exactly the fields any
    detector consumes (name, display summary, dedup target, drift key)."""
    name = block.get("name", "")
    inp = block.get("input", {}) or {}
    return {
        "name": name,
        "input_summary": _summarize_input(inp),
        "target": _tool_target_key(name, inp),
        "drift_target": (inp.get("file_path")
                         or str(inp.get("command", ""))[:40] or ""),
    }


def _security_surfaces(texts: list, tool_blocks: list):
    """Assistant-turn scannable surfaces in the original detector's order."""
    for text in texts:
        yield ("text", text)
    for tool in tool_blocks:
        if tool.get("name") == "Bash":
            cmd = (tool.get("input", {}) or {}).get("command", "")
            if cmd:
                yield ("bash", cmd)
        if tool.get("name") in ("Write", "Edit"):
            inp = tool.get("input", {}) or {}
            for k in ("content", "new_string"):
                v = inp.get(k, "")
                if v:
                    yield (f"{tool['name']}:{k}", v)


def _parse_session_lines(path: Path) -> Iterator[_ParsedLine]:
    """Stream-parse a session JSONL into BOUNDED structured line records.

    All full-text regex work happens here, once per line; retained memory per
    line is O(_TEXT_CAP), never O(payload). errors='replace' so one mojibake
    byte no longer aborts detection for the whole session.
    """
    with open(path, errors="replace") as f:
        for line_num, raw in enumerate(f, 1):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "")
            if msg_type not in ("user", "assistant", "system"):
                continue

            content = obj.get("message", {}).get("content", "")
            ts = obj.get("timestamp", "")
            texts = _extract_text(content)

            parsed = _ParsedLine(
                line_num=line_num,
                msg_type=msg_type,
                timestamp=ts,
                texts=[t[:_TEXT_CAP] for t in texts],
            )

            if msg_type == "user":
                for text in texts:
                    parsed.correction_names.append(
                        [n for n, p in CORRECTION_PATTERNS.items() if p.search(text)])
                    parsed.overflow_flags.append(bool(CONTEXT_OVERFLOW.search(text)))
                    parsed.sysrem_flags.append("system-reminder" in text.lower())
                for tr in _extract_tool_results(content):
                    result_text = _tool_result_text(tr)
                    for err_type, pattern in ERROR_PATTERNS.items():
                        if pattern.search(result_text):
                            parsed.result_errors.append((err_type, result_text[:300]))
                            break
                parsed.correction_word = bool(
                    _EXTRA_CORRECTION_WORDS.search(" ".join(texts)))

            elif msg_type == "assistant":
                tool_blocks = _extract_tool_uses(content)
                parsed.tool_uses = [_reduce_tool_use(b) for b in tool_blocks]
                for kind, text in _security_surfaces(texts, tool_blocks):
                    for pat_name, pattern in SECURITY_PATTERNS.items():
                        m = pattern.search(text)
                        if m:
                            snippet = text[max(0, m.start() - 20):m.end() + 40]
                            parsed.security_hits.append((pat_name, kind, snippet))
                parsed.success_claim = bool(
                    _EXTRA_SUCCESS_CLAIMS.search(" ".join(texts)))

            yield parsed


def detect_user_corrections(lines: list[_ParsedLine], session_id: str, session_path: str) -> list[GapRegion]:
    """Detect user corrections — explicit negative feedback to the agent."""
    gaps = []

    for i, line in enumerate(lines):
        if line.msg_type != "user":
            continue

        for text, matched_patterns in zip(line.texts, line.correction_names):
            correction_strength = len(matched_patterns)
            if correction_strength == 0:
                continue

            # Gather context: preceding assistant response + subsequent fix
            context_texts = []
            preceding_tools = []
            for j in range(max(0, i - 5), i):
                if lines[j].msg_type == "assistant":
                    context_texts.extend(lines[j].texts)
                    preceding_tools.extend(
                        {"name": t["name"], "input_summary": t["input_summary"]}
                        for t in lines[j].tool_uses
                    )

            subsequent_tools = []
            for j in range(i + 1, min(len(lines), i + 10)):
                if lines[j].msg_type == "assistant":
                    subsequent_tools.extend(
                        {"name": t["name"], "input_summary": t["input_summary"]}
                        for t in lines[j].tool_uses
                    )
                    if subsequent_tools:
                        break

            category = _classify_category([text] + context_texts)

            gap = GapRegion(
                session_id=session_id,
                session_path=session_path,
                start_line=lines[max(0, i - 5)].line_num,
                end_line=lines[min(len(lines) - 1, i + 10)].line_num,
                gap_type="user_correction",
                severity=_severity_for_gap_type("user_correction", 0, correction_strength),
                category=category,
                summary=f"User correction ({', '.join(matched_patterns)})",
                user_message=text[:500],
                tool_calls=preceding_tools[:10],
                correction_text=text[:1000],
                tool_errors=[],
            )
            gaps.append(gap)

    return gaps


def detect_tool_error_clusters(lines: list[_ParsedLine], session_id: str, session_path: str) -> list[GapRegion]:
    """Detect clusters of tool errors (3+ errors within 20 lines)."""
    gaps = []
    error_lines = []

    for i, line in enumerate(lines):
        if line.msg_type != "user":
            continue

        for err_type, snippet in line.result_errors:
            error_lines.append((i, line.line_num, err_type, snippet))

    # Cluster errors within 20-line windows
    if len(error_lines) < 2:
        return gaps

    cluster_start = 0
    for j in range(1, len(error_lines)):
        if error_lines[j][0] - error_lines[j - 1][0] > 20:
            cluster = error_lines[cluster_start:j]
            if len(cluster) >= 2:
                gaps.append(_make_error_cluster_gap(cluster, lines, session_id, session_path))
            cluster_start = j

    # Last cluster
    cluster = error_lines[cluster_start:]
    if len(cluster) >= 2:
        gaps.append(_make_error_cluster_gap(cluster, lines, session_id, session_path))

    return gaps


def _make_error_cluster_gap(cluster, lines, session_id, session_path):
    first_idx = cluster[0][0]
    last_idx = cluster[-1][0]

    context_texts = []
    for k in range(max(0, first_idx - 3), min(len(lines), last_idx + 3)):
        context_texts.extend(lines[k].texts)

    user_msg = ""
    for k in range(first_idx, -1, -1):
        if lines[k].msg_type == "user" and lines[k].texts:
            for t, has_sysrem in zip(lines[k].texts, lines[k].sysrem_flags):
                if len(t) > 20 and not has_sysrem:
                    user_msg = t[:500]
                    break
            if user_msg:
                break

    error_types = [e[2] for e in cluster]
    error_texts = [e[3] for e in cluster]
    category = _classify_category(error_texts + context_texts)

    return GapRegion(
        session_id=session_id,
        session_path=session_path,
        start_line=cluster[0][1],
        end_line=cluster[-1][1],
        gap_type="tool_error_cluster",
        severity=_severity_for_gap_type("tool_error_cluster", len(cluster), 0),
        category=category,
        summary=f"{len(cluster)} errors in {last_idx - first_idx} lines ({', '.join(set(error_types))})",
        user_message=user_msg,
        tool_calls=[],
        tool_errors=error_texts[:10],
    )


def detect_repeated_attempts(lines: list[_ParsedLine], session_id: str, session_path: str) -> list[GapRegion]:
    """Detect when agent calls the same tool with similar input 3+ times in a window."""
    gaps = []
    window_size = 30

    for i in range(0, len(lines), window_size // 2):
        window = lines[i:i + window_size]
        tool_targets = []

        for line in window:
            if line.msg_type != "assistant":
                continue
            for tu in line.tool_uses:
                if tu["target"]:
                    tool_targets.append((line.line_num, tu["name"], tu["target"]))

        target_counts = Counter(t[2] for t in tool_targets)
        for target, count in target_counts.items():
            if count < 3:
                continue

            matching = [t for t in tool_targets if t[2] == target]
            user_msg = ""
            first_line_idx = next((j for j, l in enumerate(lines[i:i + window_size])
                                   if l.msg_type == "user" and l.texts), None)
            if first_line_idx is not None:
                for t in lines[i + first_line_idx].texts:
                    if len(t) > 20:
                        user_msg = t[:500]
                        break

            category = _classify_category([target, matching[0][1]])

            gap = GapRegion(
                session_id=session_id,
                session_path=session_path,
                start_line=matching[0][0],
                end_line=matching[-1][0],
                gap_type="repeated_attempts",
                severity=_severity_for_gap_type("repeated_attempts", 0, 0),
                category=category,
                summary=f"{count}x repeated {matching[0][1]}→{target[:60]}",
                user_message=user_msg,
                tool_calls=[{"name": m[1], "target": m[2]} for m in matching[:5]],
            )
            gaps.append(gap)

    return gaps


def detect_context_overflows(lines: list[_ParsedLine], session_id: str, session_path: str) -> list[GapRegion]:
    """Detect context overflow / session continuation points."""
    gaps = []
    for i, line in enumerate(lines):
        if line.msg_type != "user":
            continue
        for text, overflowed in zip(line.texts, line.overflow_flags):
            if overflowed:
                gaps.append(GapRegion(
                    session_id=session_id,
                    session_path=session_path,
                    start_line=line.line_num,
                    end_line=lines[min(len(lines) - 1, i + 5)].line_num,
                    gap_type="context_overflow",
                    severity=4,
                    category="scope_management",
                    summary="Session continued from context overflow",
                    user_message=text[:500],
                ))
                break
    return gaps


def _tool_target_key(name: str, inp: dict) -> Optional[str]:
    """Create a dedup key for a tool call's target."""
    if name == "Bash":
        cmd = inp.get("command", "")[:80]
        return f"bash:{cmd}" if cmd else None
    elif name == "Read":
        return f"read:{inp.get('file_path', '')}"
    elif name == "Edit":
        return f"edit:{inp.get('file_path', '')}"
    elif name == "Write":
        return f"write:{inp.get('file_path', '')}"
    elif name == "Grep":
        return f"grep:{inp.get('pattern', '')[:40]}"
    elif name == "Glob":
        return f"glob:{inp.get('pattern', '')}"
    elif name == "Agent":
        return None  # Agents are intentionally diverse
    return f"{name}:{str(inp)[:40]}"


def _summarize_input(inp: dict) -> str:
    """One-line summary of tool input."""
    if not inp:
        return ""
    if "command" in inp:
        return inp["command"][:100]
    if "file_path" in inp:
        return inp["file_path"]
    if "pattern" in inp:
        return inp["pattern"][:60]
    if "prompt" in inp:
        return inp["prompt"][:100]
    return str(inp)[:100]


def detect_security_circumvention(lines: list[_ParsedLine], session_id: str, session_path: str) -> list[GapRegion]:
    """Detect attempts to circumvent security controls, extract credentials,
    obfuscate commands, exfiltrate data, or self-reference for agent recursion.

    Inspired by OpenAI's internal coding agent misalignment monitoring
    (see output_llm/openai-misalignment-monitoring-research.md). Scans
    assistant tool_use Bash commands AND assistant text for the 7 pattern
    families in SECURITY_PATTERNS.
    """
    gaps = []

    for i, line in enumerate(lines):
        if line.msg_type != "assistant":
            continue

        # Surfaces (free text, Bash commands, Write/Edit content) were scanned
        # at parse time on the FULL payloads; only the hits are retained.
        hits = line.security_hits  # [(pattern_name, surface_kind, snippet)]
        if not hits:
            continue

        # One hit is interesting, two+ is a strong signal of concerted evasion
        strength = len(hits)
        unique_patterns = len({h[0] for h in hits})
        severity = _severity_for_gap_type(
            "security_circumvention", error_count=0, correction_strength=min(unique_patterns, 3)
        )
        # Cap at 10, boost if multiple distinct patterns in one assistant turn
        if unique_patterns >= 2:
            severity = min(severity + 1, 10)

        # Context: the preceding user message gives us intent
        user_context = ""
        for j in range(max(0, i - 3), i):
            if lines[j].msg_type == "user" and lines[j].texts:
                user_context = " | ".join(lines[j].texts)[:300]
                break

        matched_names = sorted({h[0] for h in hits})
        gap = GapRegion(
            session_id=session_id,
            session_path=session_path,
            start_line=lines[max(0, i - 3)].line_num,
            end_line=lines[min(len(lines) - 1, i + 2)].line_num,
            gap_type="security_circumvention",
            severity=severity,
            category=SECURITY_CATEGORY,
            summary=f"Security pattern(s): {', '.join(matched_names)}",
            user_message=user_context,
            tool_calls=[{"name": t["name"], "input_summary": t["input_summary"]}
                        for t in line.tool_uses],
            correction_text="",
            tool_errors=[f"{k}: {s[:120]}" for (_, k, s) in hits[:5]],
        )
        gaps.append(gap)

    return gaps


def detect_gaps(session_path: str, session_id: str = None,
                lines: list = None) -> list[GapRegion]:
    """Run all gap detectors on a session file. Returns sorted by severity.

    `lines` lets detect_gaps_extended share one parse instead of streaming
    the file twice. Records are bounded (see _parse_session_lines), so the
    retained list is O(lines x _TEXT_CAP), never O(file size).
    """
    path = Path(session_path)
    if session_id is None:
        session_id = path.stem

    if lines is None:
        lines = list(_parse_session_lines(path))
    if not lines:
        return []

    all_gaps = []
    all_gaps.extend(detect_user_corrections(lines, session_id, session_path))
    all_gaps.extend(detect_tool_error_clusters(lines, session_id, session_path))
    all_gaps.extend(detect_repeated_attempts(lines, session_id, session_path))
    all_gaps.extend(detect_context_overflows(lines, session_id, session_path))
    all_gaps.extend(detect_security_circumvention(lines, session_id, session_path))

    # Deduplicate overlapping gaps
    all_gaps = _deduplicate_gaps(all_gaps)

    # Sort by severity descending, then by line number
    all_gaps.sort(key=lambda g: (-g.severity, g.start_line))

    return all_gaps


def _deduplicate_gaps(gaps: list[GapRegion]) -> list[GapRegion]:
    """Remove gaps that overlap significantly, keeping highest severity."""
    if not gaps:
        return gaps

    gaps.sort(key=lambda g: (-g.severity, g.start_line))
    kept = []
    used_ranges = []

    for gap in gaps:
        overlaps = False
        for start, end in used_ranges:
            overlap = max(0, min(gap.end_line, end) - max(gap.start_line, start))
            gap_span = max(gap.end_line - gap.start_line, 1)
            if overlap / gap_span > 0.5:
                overlaps = True
                break
        if not overlaps:
            kept.append(gap)
            used_ranges.append((gap.start_line, gap.end_line))

    return kept


if __name__ == "__main__":
    import argparse
    import sys
    from .session_scanner import scan_project

    parser = argparse.ArgumentParser(description="LogLearner Gap Detector")
    parser.add_argument("--project", default="reference-flywheel")
    parser.add_argument("--max-sessions", type=int, default=10)
    parser.add_argument("--min-severity", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    total_gaps = 0
    for session in scan_project(args.project, primary_only=True):
        if args.max_sessions <= 0:
            break
        args.max_sessions -= 1

        gaps = detect_gaps(session.path, session.session_id)
        gaps = [g for g in gaps if g.severity >= args.min_severity]

        if not gaps:
            continue

        total_gaps += len(gaps)

        if args.json:
            for g in gaps:
                print(json.dumps(g.to_dict()))
        else:
            size_mb = session.size_bytes / 1024 / 1024
            print(f"\n{'='*70}")
            print(f"Session: {session.session_id[:12]}... ({size_mb:.1f}MB)")
            print(f"Gaps found: {len(gaps)}")
            for g in gaps[:10]:
                print(f"  [{g.severity:>2d}] L{g.start_line}-{g.end_line} "
                      f"{g.gap_type:<22s} [{g.category}] {g.summary[:60]}")

    if not args.json:
        print(f"\nTotal gaps detected: {total_gaps}")
