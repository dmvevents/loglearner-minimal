#!/usr/bin/env python3
"""Offline probe for a PreToolUse hook command: feeds the exact JSON Claude Code sends (no
CLAUDE_FILE_PATH), captures stdout, and reports whether it is a valid hookSpecificOutput JSON with
additionalContext, how many instinct ids it carries, and whether caution text is present.
Exposure writes are redirected to a scratch ledger via LOGLEARNER_UCB_DIR so probes never create
fake sessions in the real ledger. Usage: probe_hook.py <command-string-or-@file> <file_path> [tool]
"""
import json, os, re, subprocess, sys, tempfile
cmd, fp = sys.argv[1], sys.argv[2]
tool = sys.argv[3] if len(sys.argv) > 3 else "Write"
content = sys.argv[4] if len(sys.argv) > 4 else "// root cause identified and resolved\nint x = 1;"
if cmd.startswith("@"):
    cmd = open(cmd[1:]).read()
payload = {"session_id": "probe-offline", "transcript_path": "/tmp/probe/-probe-project/probe.jsonl",
           "cwd": "/tmp/probe", "hook_event_name": "PreToolUse", "tool_name": tool,
           "tool_input": {"file_path": fp, "content": content}}
env = {k: v for k, v in os.environ.items() if k != "CLAUDE_FILE_PATH"}
env.setdefault("LOGLEARNER_UCB_DIR", os.environ.get("PROBE_UCB_DIR", tempfile.mkdtemp(prefix="hook-probe-ucb-")))
r = subprocess.run(["bash", "-c", cmd], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
out = r.stdout.strip()
ids, ctx, caution, valid = [], "", False, False
if out:
    try:
        d = json.loads(out)
        ctx = d.get("hookSpecificOutput", {}).get("additionalContext", "")
        valid = d.get("hookSpecificOutput", {}).get("hookEventName") == "PreToolUse" and bool(ctx)
        ids = re.findall(r"^## (\S+) \[", ctx, re.M)
        caution = "Caution rules" in ctx
    except json.JSONDecodeError:
        valid = False
print(json.dumps({"file": os.path.basename(fp), "exit": r.returncode, "stdout_bytes": len(out),
                  "valid_hook_json": valid, "context_chars": len(ctx), "instincts": len(ids),
                  "first_ids": ids[:3], "ids": ids, "caution": caution, "ledger_dir": env["LOGLEARNER_UCB_DIR"]}))
