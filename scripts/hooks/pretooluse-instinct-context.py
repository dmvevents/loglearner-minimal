#!/usr/bin/env python3
"""PreToolUse (Edit|Write|Bash) hook: deliver matched learned instincts + caution rules to the model as
`hookSpecificOutput.additionalContext`, bounded, and record EXPOSURE (which session received which
rule, before which edit) for loglearner's reward join.

Why (2026-09-27): the previous inline hook (a) routed on $CLAUDE_FILE_PATH, a variable Claude Code
never sets, and (b) printed plain text, which PreToolUse does not add to the model's context
(docs: plain stdout on exit 0 goes to the debug log). Both halves delivered nothing. Context reaches
the model only via JSON on stdout: {"hookSpecificOutput": {"hookEventName": "PreToolUse",
"additionalContext": "..."}}.

Retrieval = two passes, task-context first:
  pass 2  content tokens (snake_case identifiers in the text being written) over BOTH stores —
          the fleet learnings bank (the JSONL banks, newest first) and instincts.json;
  pass 1  file route (the 2026-05 glob table + the CUDA/C++ header/source forms it never covered).
Shell text/process tools (grep, awk, sed, tail, cat, echo, pkill, ...) route to the shell vocabulary plus the tools present;
git/gh route with their subcommand (git:rebase, gh:pr) so each draws its own records; a .sh Write carries shell vocabulary.
Retired rows (lifecycle_state deprecated/contested/dormant; status deprecated/retired/superseded/withdrawn/tombstoned; a
tombstone flag) are never delivered, from either store and never as a riding correction; negative stays deliverable.
Inspection commands (git status/log/diff/show/branch/ls-files/stash/worktree, find) add a second tier of up to STATE_LIMIT (2)
records tagged with a repository-state word (nested_repo, dirty_index, detached_head, ...) after the command-specific ones.
Agent-configuration edits (settings.json, hooks, skills, AGENTS/CLAUDE files, codex config) and Bash calls to claude /
codex / tmux route to the agent-engineering class (route label agent-config / agent-tooling).
Routing is TOKEN-based, never substring: a path is split into segments and words (on / _ - . and case),
so "defaults/Dockerfile" carries no "efa" token (consumer-found defect, 2026-10-13). A Bash call routes on
the command's program names (docker/kubectl/aws <service>/fi_info/srun/git/pip/vllm ...) and gets the same
content-token pass over the command text, the same budget, delivery cache and exposure ledger; it is quiet
when nothing matches. A record's tags and category are part of the match haystack, so pack records tagged
with the routing vocabulary are found even when the rule text uses other words.
Ranking within instincts.json mirrors the reference store's loader script (word-boundary match
over rule/evidence/source/category; rule-text hits first, then confidence) plus one refinement:
more distinct keywords hit in the rule ranks higher (so a specific token can outrank an old generic
rule of equal confidence — the tie-break-by-age miss measured 2026-09-27).

Bounds: INSTINCT_LIMIT route rules, INSTINCT_BUDGET_CHARS total (whole rules dropped, never
truncated), CAUTION_BUDGET_CHARS, and a per-session delivery cache (DELIVERY_TTL_S) so the same rule
is not re-injected on every consecutive edit. Suppression is applied BEFORE each store's limit, so
lower-ranked matches rotate in on later edits until the matched set is exhausted.
Correction-aware: a selected record with an EXPLICITLY linked correction (rule opening
"CORRECTION ... to <id>", or a `corrects` list on the record/meta) is never emitted alone — the
correction rides along regardless of query tokens or the TTL cache, the pair is bounded
atomically, and if the pair cannot fit the correction alone is delivered in the target's place
(or the target is omitted when nothing fits / the correction was already delivered this session).
Generic cross_ref is not a link. Exposure rows describe DELIVERY only — not rewards,
and they promote nothing (candidates in loglearning/state/candidates are never read here).

Guarantees: always exit 0; never blocks the tool; stdout is empty or exactly one JSON object;
diagnostics to stderr only; read-only on every knowledge store.

Ranking (0.1.6, validator finding): string confidences rank (high 9 / med 6 / low 3; before, every string was 0),
matches are weighted by specificity (identifier tokens 2, generic words 1; rule text before tags), and ties break by
recency (banked_at / pack.ingested_at / as_of, else append position newest-first) — never by file position ascending,
so a late-appended exact match is delivered ahead of earlier generic records. The header says why and names the caps.

Env: INSTINCT_LIMIT (6)  INSTINCT_BUDGET_CHARS (3600 ~ 900 tokens)  CAUTION_BUDGET_CHARS (2000)
     CONTENT_TOKENS (3)  TOKEN_INSTINCT_LIMIT (3)  FLEET_LIMIT (3)  STATE_LIMIT (2)  DELIVERY_TTL_S (1800)
     FLEET_LEARNINGS_DIR  INSTINCTS_JSON  LOGLEARNER_UCB_DIR (exposure ledger + delivery cache dir)
     HOOK_DISABLE_EXPOSURE=1  HOOK_TIMEOUT_S (6, cautions subprocess)
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Every store path is ENV-FIRST. The literal fallbacks are this deployment's fleet paths; the
# shipped (vendored) copy carries neutral ~/.loglearner defaults instead (see scripts/ship_minimal_runtime.py).
def _repo_root():
    env = os.environ.get("LOGLEARNER_REPO")
    if env:
        return env
    here = Path(__file__).resolve()
    for cand in (here.parents[2] if len(here.parents) > 2 else None, here.parents[1] if len(here.parents) > 1 else None):
        if cand and (cand / "loglearner" / "__init__.py").is_file():
            return str(cand)
    return "~/.loglearner"


_x = os.path.expanduser
REFERENCE = _x(os.environ.get("LOGLEARNER_KNOWLEDGE_ROOT")  # neutral name; the older LOGLEARNER_KNOWLEDGE_ROOT is still accepted
         or os.environ.get("LOGLEARNER_KNOWLEDGE_ROOT", "~/.loglearner/reference"))
INSTINCTS_JSON = _x(os.environ.get("INSTINCTS_JSON", f"{REFERENCE}/knowledge_base/learned_instincts/instincts.json"))
CAUTIONS = _x(os.environ.get("LOGLEARNER_CAUTIONS_SCRIPT", f"{REFERENCE}/scripts/load-cautions.sh"))
FLEET_LEARNINGS_DIR = _x(os.environ.get("FLEET_LEARNINGS_DIR", "~/.loglearner/banks"))
LOGLEARNING = _x(_repo_root())
UCB_DIR = Path(_x(os.environ.get("LOGLEARNER_UCB_DIR", f"{LOGLEARNING}/output")))

# First match wins. Name-based routes precede extension routes (same order as the 2026-05 inline
# hook), then the CUDA/C++ header + source forms the kernel lane edits, which the old hook never routed.
# An entry is (name_tokens, extensions, keywords): name_tokens is any-of over the path's TOKENS
# (() = any name), extensions is any-of over the file suffix (() = any). Tokens, never substrings:
# "defaults" is one token and does not contain "efa".
ROUTES = [
    (("nki", "neuron", "trn2", "trainium"), (), ["neuron", "nki", "trn2", "trainium"]),
    (("proxy",), (".cpp", ".cu", ".cuh", ".h", ".hpp"), ["proxy"]),
    (("libfabric",), (".cpp", ".h", ".hpp"), ["fi_write", "libfabric"]),
    (("internode",), (".cu", ".cuh"), ["dispatch", "combine", "nvshmem"]),
    (("nvshmem",), (), ["nvshmem"]),
    ((), (".cu", ".cuh"), ["kernel", "cuda"]),
    (("efa", "rdma", "transport"), (), ["efa", "rdma"]),
    ((), (".py",), ["kernel"]),
    ((), (".h", ".hpp", ".hxx", ".hh", ".cpp", ".cc", ".cxx", ".c"), ["kernel", "cuda"]),
    # generic infrastructure files (default AWS knowledge pack users): cloud + cluster vocabulary
    (("env", "dotenv"), (".env", ".local", ".production", ".example", ""), ["env", "environment", "secret", "settings"]),  # fix.env, .env.local
    (("eksctl", "kustomization", "helmfile"), (), ["aws", "eks", "ec2", "kubernetes"]),
    (("workflows",), (), ["aws", "docker", "git", "eks", "kubernetes"]),   # CI workflows build images AND deploy
    (("dockerfile",), (), ["aws", "docker", "git"]),
    ((), (".tf", ".tfvars", ".yaml", ".yml"), ["aws", "eks", "ec2", "kubernetes"]),
    ((), (".sh",), ["shell", "bash", "aws", "docker", "git"]),
    ((), (".dockerfile",), ["aws", "docker", "git"]),
]
# Coding-agent engineering (operator direction 2026-10-13): knowledge about skills, hooks, settings and permission modes,
# model fallbacks, sandboxing, lane operating rules and the autonomy contract must reach an agent at the moment it edits
# its own configuration. Records of the class carry category AGENT_CLASS and tags from this vocabulary; the class id is
# itself a keyword and the category field is in the match haystack, so every class record is a candidate on these edits.
AGENT_CLASS = "agent-engineering"
AGENT_KWS = [AGENT_CLASS, "claude_code", "codex", "hook", "hooks", "skill", "skills", "settings", "permission", "permissions",
             "allowed_tools", "model_fallback", "safety_stop", "sandbox", "tmux", "send_keys", "autonomy", "agents_md", "claude_md"]
AGENT_BASENAMES = {"settings.json", "settings.local.json", "keybindings.json", "agents.md", "claude.md", "skill.md"}
AGENT_CODE_EXTS = (".json", ".md", ".toml", ".py", ".sh", ".mjs", ".js")


def agent_config_route(low):
    """True when the path is an agent's own configuration: Claude Code / Codex settings, hooks, skills, AGENTS/CLAUDE
    files, codex config. Specific on purpose: firewall-rules.tf or a random .json never lands here."""
    base = low.rsplit("/", 1)[-1]
    toks = set(_path_tokens(low))
    ext = os.path.splitext(low)[1]
    if base in AGENT_BASENAMES:
        return True
    if base == "config.toml" and "codex" in toks:
        return True
    if ({"claude", "codex"} & toks) and ext in AGENT_CODE_EXTS:
        return True
    if ({"hooks", "hook"} & toks) and ext in (".py", ".sh", ".mjs", ".js"):
        return True   # pretool-hook.py, hooks/x.sh
    if "memory" in toks and ext == ".md" and (base == "memory.md" or base.split("_")[0] in ("feedback", "reference", "project", "user")):
        return True   # agent memory files hold operating rules (feedback_*, reference_*, project_*, user_*, MEMORY.md)
    return "skills" in toks and ext == ".md"


# Shell text/process tools (knowledge-lane finding 2026-10-13): grep, tail, awk, pkill, echo, cat ... never routed, so the
# mechanical-trap records (quoting, pkill -f self-match, loose greps, result markers) could not fire at tool time. The tools
# present in the command become keywords themselves, next to the shell vocabulary.
SHELL_TOOLS = {"grep", "egrep", "fgrep", "rg", "awk", "sed", "tail", "head", "cat", "echo", "printf", "pkill", "pgrep", "kill",
               "killall", "xargs", "find", "sort", "uniq", "cut", "tr", "wc", "tee", "less", "ps", "test", "sleep", "timeout"}
# Keywords are the tools PRESENT plus a few universal words (quoting traps, result markers) and tool-family words; the
# broad words "shell"/"bash" are deliberately absent here so `echo done` does not draw every shell-tagged record —
# only a .sh Write (editing a script) carries them.
SHELL_KWS = ["quoting", "marker"]
SHELL_FAMILY = {"regex": {"grep", "egrep", "fgrep", "rg", "sed", "awk"}, "process": {"pkill", "pgrep", "kill", "killall", "ps"},
                "pipe": {"xargs", "tee", "sort", "uniq", "cut", "tr", "wc"}, "exit_code": {"test", "timeout"}}
SCRIPT_RUNNERS = {"bash", "sh", "zsh", "source"}   # running a script is the moment for shell-script rules
# Repository-STATE records (lift battery, 2026-10-13): a rule about a state that inspection reveals (a nested repo, a dirty
# index, a detached head) was lost when it routed only on the command that causes the damage — agents inspected with
# git status / git log / find, saw the state themselves, and the rule never arrived. Inspection commands therefore also
# draw records tagged with a repository-state word, as a small second tier AFTER the command-specific records.
STATE_KWS = ["repository_state", "nested_repo", "nested_git", "dirty_index", "dirty_worktree", "detached_head", "untracked",
             "submodule", "merge_in_progress", "rebase_in_progress", "stale_branch"]
INSPECT_SUBS = {"status", "log", "diff", "show", "branch", "ls-files", "stash", "worktree"}


def inspection_command(command):
    """True for git inspection subcommands (status, log, diff, show, branch, ls-files, stash, worktree) and `find`,
    read from the program positions."""
    programs, toks = program_tokens(command)
    if "find" in programs:
        return True
    return "git" in programs and any(t == "git" and any(x in INSPECT_SUBS for x in toks[i + 1:i + 4]) for i, t in enumerate(toks))
# git/gh: the subcommand becomes a keyword, so `git rebase` and `gh pr` draw their own records, not one GitHub set for all.
VCS_TOOLS = {"git": ["git"], "gh": ["github", "gh"]}
VCS_SUBS = {"git": {"status", "add", "commit", "push", "pull", "fetch", "rebase", "merge", "checkout", "switch", "branch", "log", "diff",
                    "stash", "reset", "revert", "cherry-pick", "tag", "clone", "init", "remote", "worktree", "clean", "show", "blame",
                    "bisect", "am", "apply", "format-patch", "submodule", "config", "mv", "rm", "restore", "describe", "rev-parse"},
            "gh": {"pr", "issue", "repo", "run", "api", "auth", "release", "gist", "workflow", "label", "search", "secret"}}

# Bash: route on the command's program-name tokens. (names, label, keywords); several may match one command.
BASH_ROUTES = [
    (("claude", "codex", "tmux", "send-keys"), "agent-tooling", AGENT_KWS),
    (("docker", "buildx", "podman", "nerdctl"), "containers", ["docker", "container", "image", "ecr"]),
    (("kubectl", "eksctl", "helm", "kustomize", "k9s"), "eks-k8s", ["kubernetes", "k8s", "eks", "kubectl", "helm", "pod"]),
    (("fi_info", "fi_pingpong", "nccl-tests", "all_reduce_perf", "all_gather_perf", "alltoall_perf", "efa", "ibv_devinfo"),
     "efa-networking", ["efa", "libfabric", "nccl", "rdma"]),
    (("srun", "sbatch", "salloc", "sinfo", "squeue", "scancel"), "slurm", ["slurm", "srun", "sbatch"]),
    (("pip", "pip3", "uv", "npm", "apt", "apt-get", "conda", "pipx"), "deps", ["pip", "dependency", "package", "wheel", "install"]),
    (("vllm", "sglang", "trtllm", "trtllm-serve", "dynamo", "aiperf", "genai-perf"), "serving",
     ["vllm", "sglang", "trtllm", "dynamo", "serving", "inference", "benchmark"]),
]
# `aws <service>` pairs route to that service's vocabulary (plus "aws"); a bare `aws` routes to "aws".
AWS_SERVICES = ("ec2", "eks", "iam", "bedrock", "s3", "ssm", "fsx", "cloudwatch", "logs", "ecr", "sts", "autoscaling",
                "sagemaker", "service-quotas", "efs", "sqs", "sns", "lambda", "batch")
IDENT_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")  # snake_case identifiers
FLAG_RE = re.compile(r"(?<![A-Za-z0-9_-])--([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b")  # --kebab-case flags (Bash)
CMD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.+-]*")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


CONF_RANK = {"high": 9, "med": 6, "medium": 6, "low": 3}


def conf_rank(v):
    """Numeric rank for a confidence value: ints/floats as given (reference 1-11), strings high/med/low -> 9/6/3.
    Before 0.1.6 every string mapped to 0, so pack records tied and file position decided delivery."""
    if isinstance(v, bool):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    return float(CONF_RANK.get(str(v or "").strip().lower(), 0))


def specificity(kw):
    """Weight of a matched keyword: identifier-like tokens (EFA_VERSION, capacity_block, >= 8 chars) count 2, generic
    route words (aws, docker) count 1. Exact identifier matches must outrank generic vocabulary hits."""
    k = str(kw)
    return 2 if ("_" in k or len(k) >= 8) else 1


def recency(r, idx):
    """Newest first: banked_at / pack.ingested_at / as_of when present, else append position (later line = newer)."""
    pack = r.get("pack") if isinstance(r.get("pack"), dict) else {}
    stamp = str(r.get("banked_at") or pack.get("ingested_at") or r.get("as_of") or "")
    return (stamp, idx)


def _wb(kw):
    """Word-boundary match where an identifier's `_` also matches a space or hyphen in prose
    (capacity_block ~ 'Capacity Block' ~ 'capacity-block'), so code tokens find prose rules."""
    parts = [re.escape(x) for x in str(kw).split("_") if x]
    body = r"[ _-]".join(parts) if parts else re.escape(str(kw))
    return re.compile(r"(?<![A-Za-z0-9_])" + body + r"(?![A-Za-z0-9_])", re.I)


def _path_tokens(path):
    return [t for t in re.split(r"[^a-z0-9]+", (path or "").lower()) if t]


def route(file_path):
    """Keywords for a file path, matched on path TOKENS (segments/words) and extension, never substrings."""
    low = (file_path or "").lower()
    if not low:
        return []
    if agent_config_route(low):
        return AGENT_KWS
    toks, ext = set(_path_tokens(low)), os.path.splitext(low)[1]
    for names, exts, kws in ROUTES:
        if (not names or toks & set(names)) and (not exts or ext in exts):
            return kws
    return []


WRAPPERS = {"sudo", "time", "nohup", "env", "exec", "command", "builtin", "nice", "stdbuf", "timeout", "xargs"}
LAUNCHERS = {"srun", "mpirun", "mpiexec", "salloc", "sbatch", "ssh"}   # the program they launch routes too
SEGMENT_RE = re.compile(r"[;&|()\n]+")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def program_tokens(command):
    """(programs, all_tokens): the program at each command position (after ; && || | ( ) and newlines, past env
    assignments and wrappers such as sudo/time/nohup/xargs) plus every lowercase token. Routing keys on the programs, so
    `.git` as a find argument or the word docker inside a quoted string never selects a route."""
    all_toks = [t.lower() for t in CMD_TOKEN_RE.findall(command or "")]
    programs = []

    def skip(words):
        while words and (ASSIGN_RE.match(words[0]) or words[0].lower() in WRAPPERS or words[0].startswith("-")
                         or words[0].replace(".", "").isdigit()):
            words.pop(0)
        return words

    for seg in SEGMENT_RE.split(command or ""):
        words = skip(seg.split())
        while words:
            prog = words.pop(0).lower().rsplit("/", 1)[-1]   # /usr/bin/python3 -> python3, ./all_reduce_perf -> all_reduce_perf
            if prog and CMD_TOKEN_RE.match(prog):
                programs.append(prog)
            if prog not in LAUNCHERS:
                break
            words = skip(words)                              # srun -N2 ./all_reduce_perf: the launched program too
    return programs, all_toks


def route_command(command):
    """(labels, keywords) for a Bash command from the PROGRAM at each command position (plus `aws <service>` and
    git/gh subcommands read from the following arguments). Empty when nothing matches, so the hook stays quiet on `ls -la`."""
    programs, toks = program_tokens(command)
    labels, kws = [], []

    def add(label, words):
        if label not in labels:
            labels.append(label)
        for w in words:
            if w not in kws:
                kws.append(w)
    progset = set(programs)
    for names, label, words in BASH_ROUTES:
        if progset & set(names):
            add(label, words)
    for i, t in enumerate(toks):
        if t not in progset:
            continue
        if t == "aws":
            svc = toks[i + 1] if i + 1 < len(toks) and toks[i + 1] in AWS_SERVICES else ""
            add(f"aws:{svc}" if svc else "aws", ["aws"] + ([svc] if svc else []))
        elif t in VCS_TOOLS:
            sub = next((x for x in toks[i + 1:i + 4] if x in VCS_SUBS[t]), "")   # first known subcommand after the tool
            extra = ["pull_request"] if (t == "gh" and sub == "pr") else []
            add(f"{t}:{sub}" if sub else t, VCS_TOOLS[t] + ([sub] if sub else []) + extra)
    if progset & SCRIPT_RUNNERS:
        add("script", ["script", "shell", "bash"])
    shell_tools = sorted(progset & SHELL_TOOLS)
    if shell_tools:
        family = [k for k, tools in SHELL_FAMILY.items() if progset & tools]
        add("shell", SHELL_KWS + family + shell_tools)
    return labels, kws


def content_tokens(content, limit=None):
    """Most frequent snake_case identifiers in the text being written — task context the filename lacks."""
    limit = limit or _env_int("CONTENT_TOKENS", 3)
    counts = {}
    toks = IDENT_RE.findall(content or "") + [m.replace("-", "_") for m in FLAG_RE.findall(content or "")]
    for t in toks:
        if len(t) >= 6:
            counts[t.lower()] = counts.get(t.lower(), 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


# Retired records are never delivered (knowledge-fabric review F6): the hub tombstones a superseded row by marking it, and
# a marked row must be skipped at delivery time. `status: negative` stays deliverable — a warning is not a tombstone.
UNSERVABLE_STATES = {"deprecated", "contested", "dormant"}                       # lifecycle module's unservable states
RETIRED_STATUS = {"deprecated", "retired", "superseded", "withdrawn", "tombstoned"}


def servable(r):
    if str(r.get("lifecycle_state") or "").lower() in UNSERVABLE_STATES:
        return False
    if str(r.get("status") or "").lower() in RETIRED_STATUS:
        return False
    return not (r.get("tombstone") or r.get("retired"))


_STORE_CACHE = {}
# Explicit correction link, form 1: the rule opens "CORRECTION ... to|of|for <id>". Form 2: a
# `corrects: [ids]` list on the record or its meta. Generic cross_ref is NEVER treated as a link.
CORR_RE = re.compile(r"^\s*CORRECTION\b[^\n]{0,120}?\b(?:to|of|for)\s+([A-Za-z][A-Za-z0-9_.-]*[A-Za-z0-9])", re.I)


def _fleet_rows():
    """[(mtime, line_no, fname, record)] for every parseable row in the fleet bank (read-only, memoized)."""
    if "fleet" in _STORE_CACHE:
        return _STORE_CACHE["fleet"]
    out, d = [], Path(FLEET_LEARNINGS_DIR)
    if d.is_dir():
        for f in d.glob("*.jsonl"):
            try:
                st = f.stat()
                if st.st_size > 20_000_000:
                    continue
                lines = f.read_text(errors="replace").splitlines()
            except Exception:
                continue
            for n, line in enumerate(lines):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and servable(r):
                    out.append((st.st_mtime, n, f.name, r))
    _STORE_CACHE["fleet"] = out
    return out


def _instinct_rows():
    if "inst" in _STORE_CACHE:
        return _STORE_CACHE["inst"]
    try:
        if str(INSTINCTS_JSON).endswith(".jsonl"):  # a bank file (one record per line) works as the store too
            rows = []
            for line in Path(INSTINCTS_JSON).read_text(errors="replace").splitlines():
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        else:
            rows = json.load(open(INSTINCTS_JSON))
            rows = rows if isinstance(rows, list) else rows.get("instincts", [])
    except Exception as e:
        print(f"hook: instincts store unreadable: {e}", file=sys.stderr)
        rows = []
    _STORE_CACHE["inst"] = [r for r in rows if isinstance(r, dict) and servable(r)]
    return _STORE_CACHE["inst"]


def _fleet_block(n, fname, r):
    rule = str(r.get("rule") or r.get("instinct") or "").strip()
    rid = str(r.get("id") or r.get("lid") or f"{fname}:{n}")
    return _block(rid, r.get("confidence", "?"), rule, r.get("evidence"), f"fleet-learnings/{fname}",
                  r.get("status") or "n/a", r.get("provenance"), r.get("version_scope"))


def _inst_block(r):
    return _block(r.get("id"), f"{r.get('confidence', '?')}/11", str(r.get("rule") or "").strip(),
                  r.get("evidence"), r.get("source"), r.get("status"), r.get("provenance"), r.get("version_scope"))


def _link_targets(r):
    """Ids this record explicitly corrects (both forms). Empty for ordinary records."""
    targets = set()
    m = CORR_RE.match(str(r.get("rule") or ""))
    if m:
        targets.add(m.group(1))
    meta = r.get("meta") if isinstance(r.get("meta"), dict) else {}
    for src in (r.get("corrects"), meta.get("corrects")):
        if isinstance(src, list):
            targets.update(str(x) for x in src if x)
    return targets


def corrections_index():
    """target_id -> [correction blocks] across BOTH stores, explicit links only."""
    idx = {}
    for _m, n, fname, r in _fleet_rows():
        for t in _link_targets(r):
            idx.setdefault(t, []).append(_fleet_block(n, fname, r))
    for r in _instinct_rows():
        for t in _link_targets(r):
            idx.setdefault(t, []).append(_inst_block(r))
    return idx


def _with_corrected_by(b, cids):
    """Mark a target block so the reader sees the link even before reaching the correction."""
    lines = b[3].rstrip("\n").split("\n")
    for i, l in enumerate(lines):
        if l.startswith("Evidence:"):
            lines.insert(i, f"Corrected-by: {', '.join(cids)} (appended correction shown alongside; this record's evidence is superseded where they conflict)")
            break
    return (b[0], b[1], b[2], "\n".join(lines) + "\n")


def _block(rid, conf, rule, evidence, source, status=None, provenance=None, scope=None):
    """One delivered record. Status, provenance and scope are passed through from the store verbatim
    (compacted), so the reader can weigh a measured run against an unmeasured design and knows WHERE the
    rule applies: a record's applicability (versions, instance types, conditions) lives in `version_scope`
    metadata, and a scoped rule must never reach the agent unscoped (validator finding, 2026-10-13)."""
    lines = [f"## {rid} [confidence: {conf}]", rule]
    if status:
        lines.append(f"Status: {status}")
    if provenance:
        prov = provenance if isinstance(provenance, str) else json.dumps(provenance, separators=(",", ":"))
        lines.append(f"Provenance: {prov[:200]}")
    if scope:
        lines.append(f"Scope: {str(scope)[:200]}")
    lines += [f"Evidence: {str(evidence or 'N/A')[:300]}", f"Source: {source or 'N/A'}"]
    return (str(rid), str(conf), rule, "\n".join(lines) + "\n")


def search_instincts(keywords, limit, exclude=frozenset()):
    """In-process equivalent of load-instincts.sh (word-boundary match over rule/evidence/source/
    category; rule-text hits first, then more distinct keywords hit, then confidence, then age).
    `exclude` (ids delivered recently) is applied BEFORE the limit so lower ranks can rotate in."""
    if not keywords or limit <= 0:
        return []
    kws = [k for k in keywords if k]
    pats = [(_wb(k), specificity(k)) for k in kws]
    scored = []
    for idx, r in enumerate(_instinct_rows()):
        rule = str(r.get("rule") or "")
        if not rule or str(r.get("id")) in exclude:
            continue
        rule_score = sum(w for p, w in pats if p.search(rule))          # specificity-weighted rule-text hits
        others_score = 0
        if not rule_score:
            others = " ".join(str(r.get(f) or "") for f in ("evidence", "source", "category", "title"))
            others += " " + " ".join(str(t) for t in (r.get("tags") or []) if t)
            others_score = sum(w for p, w in pats if p.search(others))
            if not others_score:
                continue
        stamp, pos = recency(r, idx)
        # rank: rule-text hits before tag/evidence hits; more specific and more numerous hits first; then confidence
        # (strings ranked, not zeroed); then recency (newest first) — never raw file position ascending.
        scored.append(((0 if rule_score else 1, -(rule_score or others_score), -conf_rank(r.get("confidence")), stamp, pos), r))
    def tie_key(t):   # ties break newest-first, never by input order
        return (t[0][0], t[0][1], t[0][2], _neg_stamp(t[0][3]), -t[0][4])
    scored.sort(key=tie_key)
    return [_inst_block(r) for _k, r in scored[:limit]]


def _neg_stamp(stamp):
    """Sort helper: a later ISO timestamp sorts first; records without one sort after dated ones."""
    return tuple(-ord(c) for c in stamp) if stamp else (1,)


def fleet_hits(tokens, limit=None, exclude=frozenset()):
    """Word-boundary match of content tokens against the fleet learnings bank, newest first. Read-only:
    retrieval from an already-banked store, not promotion. `exclude` is applied before the limit."""
    limit = limit or _env_int("FLEET_LIMIT", 3)
    if not tokens or limit <= 0:
        return []
    pats = [(_wb(t), specificity(t)) for t in tokens]
    hits = []
    for mtime, n, fname, r in _fleet_rows():
        rule = str(r.get("rule") or r.get("instinct") or "").strip()
        rid = str(r.get("id") or r.get("lid") or f"{fname}:{n}")
        hay = rule + " " + str(r.get("category") or "") + " " + " ".join(str(t) for t in (r.get("tags") or []) if t)
        score = sum(w for p, w in pats if p.search(hay)) if rule and rid not in exclude else 0
        if score:
            hits.append((score, conf_rank(r.get("confidence")), mtime, n, fname, r))
    # specificity first, then confidence, then newest (file mtime, then later line)
    hits.sort(key=lambda h: (-h[0], -h[1], -h[2], -h[3]))
    return [_fleet_block(n, fname, r) for _s, _c, _m, n, fname, r in hits[:limit]]


def bound(groups, budget, recent=frozenset()):
    """Greedy over GROUPS (a record plus its linked corrections); a group is kept whole, never
    truncated mid-rule. -> (kept_blocks, dropped_ids, omitted_uncorrected_ids, substituted).
    A target whose pair cannot fit is NEVER emitted alone: if its corrections alone fit they are
    delivered in its place (current evidence beats superseded evidence); if a correction was already
    delivered this session, or nothing fits, the target is omitted."""
    kept, dropped, omitted, substituted, used = [], [], [], {}, 0
    for blocks, has_correction in groups:
        size = sum(len(b[3]) for b in blocks)
        if used + size <= budget:
            kept.extend(blocks)
            used += size
            continue
        if not has_correction:
            dropped.extend(b[0] for b in blocks)
            continue
        target, corr = blocks[0], blocks[1:]
        omitted.append(target[0])
        csize = sum(len(c[3]) for c in corr)
        if corr and not any(c[0] in recent for c in corr) and used + csize <= budget:
            kept.extend(corr)
            used += csize
            substituted[target[0]] = [c[0] for c in corr]
    return kept, dropped, omitted, substituted


def delivery_cache(session_id):
    if not session_id:
        return None, {}
    p = UCB_DIR / "state" / "hook-delivered" / f"{re.sub(r'[^A-Za-z0-9._-]', '_', session_id)}.json"
    try:
        return p, json.loads(p.read_text())
    except Exception:
        return p, {}


def record_exposure(session_id, project, decision_ref, kept):
    if os.environ.get("HOOK_DISABLE_EXPOSURE") == "1" or not kept or not session_id:
        return 0
    try:
        sys.path.insert(0, LOGLEARNING)
        from loglearner import arms  # noqa: E402
        return arms.record_exposure(session_id, [arms.arm_id_for_rule(b[2]) for b in kept],
                                    project=project, decision_ref=decision_ref,
                                    instinct_ids=[b[0] for b in kept])
    except Exception as e:
        print(f"hook: exposure not recorded: {e}", file=sys.stderr)
        return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "")
    tp = payload.get("transcript_path", "")
    project = Path(tp).parent.name if tp else Path(payload.get("cwd", "")).name
    if tool == "Bash":
        command = str(ti.get("command") or "")
        if not command.strip():
            return
        labels, kws = route_command(command)
        route_label = ("bash:" + "+".join(labels)) if labels else "none"
        content, what, decision_ref = command, "command", f"Bash:{labels[0] if labels else 'cmd'}"
    else:
        file_path = ti.get("file_path") or os.environ.get("CLAUDE_FILE_PATH", "")
        kws = route(file_path)
        route_label = "agent-config" if kws is AGENT_KWS else (" ".join(kws) or "none")
        content = " ".join(str(x) for x in (
            ti.get("content", ""), ti.get("new_string", ""),
            *[e.get("new_string", "") for e in (ti.get("edits") or []) if isinstance(e, dict)]) if x)
        what, decision_ref = "edit", f"{tool}:{Path(file_path).name}"
    parts, manifest = [], {}

    cache_path, delivered = delivery_cache(session_id)
    ttl, now = _env_int("DELIVERY_TTL_S", 1800), time.time()
    recent = frozenset(rid for rid, ts in delivered.items() if now - float(ts) <= ttl)

    tokens = content_tokens(content)
    pass2 = (fleet_hits(tokens, exclude=recent)
             + search_instincts(tokens, _env_int("TOKEN_INSTINCT_LIMIT", 3), exclude=recent)) if tokens else []
    pass1 = search_instincts(kws, _env_int("INSTINCT_LIMIT", 6), exclude=recent) if kws else []
    # second tier for inspection commands: repository-state records, after the command-specific ones
    state_tier = (search_instincts(STATE_KWS, _env_int("STATE_LIMIT", 2), exclude=recent | {b[0] for b in pass2 + pass1})
                  if tool == "Bash" and inspection_command(content) else [])
    merged, seen_ids, seen_rules = [], set(), set()
    for b in pass2 + pass1 + state_tier:
        if b[0] in seen_ids or b[2][:80] in seen_rules:
            continue
        seen_ids.add(b[0]); seen_rules.add(b[2][:80]); merged.append(b)
    suppressed = sorted(recent)   # ids withheld this edit because they were delivered within the TTL
    # Correction-aware pairing: a selected record with an explicitly linked correction is never
    # emitted alone. The correction rides along regardless of query tokens or the TTL cache.
    cidx = corrections_index() if merged else {}
    groups, emitted, paired = [], set(), {}
    for b in merged:
        if b[0] in emitted:
            continue
        corr = [c for c in cidx.get(b[0], []) if c[0] != b[0]]
        if corr:
            cids = [c[0] for c in corr]
            paired[b[0]] = cids
            group = [_with_corrected_by(b, cids)] + [c for c in corr if c[0] not in emitted]
        else:
            group = [b]
        emitted.update(x[0] for x in group)
        groups.append((group, bool(corr)))
    kept, dropped, omitted_uncorrected, substituted = bound(groups, _env_int("INSTINCT_BUDGET_CHARS", 3600), recent)
    if kept:
        head = (f"Banked learnings matched to this {what} — {len(kept)} of {len(merged)} shown "
                f"(route: {route_label}; content tokens: {', '.join(tokens) or 'none'}"
                f"{'; ' + str(len(suppressed)) + ' withheld as delivered earlier this session' if suppressed else ''}"
                f"{'; ' + str(len([t for t in paired if t in {k[0] for k in kept}])) + ' shown with an appended correction' if paired else ''}"
                f"{'; ' + str(len(substituted)) + ' superseded record(s) replaced by their correction' if substituted else ''}). "
                f"Evidence strength varies by record: some are measured runs, some are source readings, and some "
                f"are unmeasured designs or hypotheses — weigh each by its own Status, Provenance and Evidence lines. "
                f"A 'negative' or REFUTED entry is a warning, not a recipe; a 'designed'/'unmeasured' entry is a "
                f"hypothesis, not a result. Advisory: the task's own instructions win on conflict. "
                f"Why these: ranked by match specificity (identifier tokens over generic words, rule text over tags), then "
                f"confidence, then recency; caps TOKEN_INSTINCT_LIMIT={_env_int('TOKEN_INSTINCT_LIMIT', 3)} "
                f"INSTINCT_LIMIT={_env_int('INSTINCT_LIMIT', 6)} FLEET_LIMIT={_env_int('FLEET_LIMIT', 3)} "
                f"INSTINCT_BUDGET_CHARS={_env_int('INSTINCT_BUDGET_CHARS', 3600)}.\n\n")
        parts.append(head + "\n".join(b[3] for b in kept))
        pass2_ids = {b[0] for b in pass2}
        kept_ids = {b[0] for b in kept}
        manifest = {"included": [b[0] for b in kept], "dropped_budget": dropped, "suppressed_recent": suppressed,
                    "omitted_uncorrected": omitted_uncorrected,
                    "correction_substituted": substituted,
                    "corrections_paired": {t: c for t, c in paired.items() if t in kept_ids},
                    "route": kws, "route_label": route_label, "content_tokens": tokens,
                    "state_tier": [b[0] for b in state_tier if b[0] in kept_ids],
                    "task_context_ids": [b[0] for b in kept if b[0] in pass2_ids]}
        manifest["exposure_rows"] = record_exposure(session_id, project, decision_ref, kept)
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                delivered.update({b[0]: now for b in kept})
                delivered.update({t: now for t in omitted_uncorrected})  # handled: correction shown/known
                tmp = cache_path.with_suffix(".tmp"); tmp.write_text(json.dumps(delivered)); tmp.replace(cache_path)
            except Exception as e:
                print(f"hook: delivery cache not written: {e}", file=sys.stderr)

    if content.strip() and Path(CAUTIONS).is_file():
        try:
            caut = subprocess.run(["bash", CAUTIONS], input=content, capture_output=True, text=True,
                                  timeout=_env_int("HOOK_TIMEOUT_S", 6)).stdout
        except Exception as e:
            print(f"hook: cautions failed: {e}", file=sys.stderr); caut = ""
    else:
        caut = ""
    if caut:
        caut = "\n".join(l for l in caut.splitlines()
                         if l.strip() not in ("<system-reminder>", "</system-reminder>")).strip()
    if caut:
        if True:
            if len(caut) <= _env_int("CAUTION_BUDGET_CHARS", 2000):
                parts.append("Caution rules matched by the text you are writing:\n" + caut)
            else:
                parts.append("Caution rules matched but exceeded the context budget; run "
                             "load-cautions.sh on your text before claiming resolution.")
    if not parts:
        return
    if manifest:
        parts.append("manifest: " + json.dumps(manifest, separators=(",", ":")))
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                        "additionalContext": "\n\n".join(parts)}}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block the tool
        print(f"hook: {e}", file=sys.stderr)
    sys.exit(0)
