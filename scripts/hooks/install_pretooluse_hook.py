#!/usr/bin/env python3
"""Install the JSON-emitting PreToolUse instinct hook by changing ONLY its own entry in the live
~/.claude/settings.json: the hook command and, since 0.1.1, the matcher
('Edit|Write' -> 'Edit|Write|Bash', so Bash calls are routed too). Other fields (model, enabledPlugins,
statusLine, mcpServers, env, ...) are owned by other agents and are preserved byte-for-byte at the JSON level.

Steps: fresh read -> locate the single PreToolUse entry with matcher 'Edit|Write' or 'Edit|Write|Bash' whose
command references load-instincts.sh or this hook -> copy the hook script to ~/.claude/hooks/ -> back up
settings.json -> atomic write -> JSON-level diff must touch ONLY that entry's command and matcher leaves,
else roll back. The entry itself is never created here: a harness adds it once (see START.md).
Usage: install_pretooluse_hook.py [--dry-run] [--matcher 'Edit|Write|Bash'|'Edit|Write']
"""
import json, os, shutil, sys, hashlib, datetime, tempfile
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
SRC = Path(__file__).resolve().parent / "pretooluse-instinct-context.py"
DST = HOME / ".claude" / "hooks" / "pretooluse-instinct-context.py"
NEW_CMD = f"python3 {DST}"
MARKERS = ("load-instincts.sh", "pretooluse-instinct-context.py")
MATCHERS = ("Edit|Write", "Edit|Write|Bash")
DEFAULT_MATCHER = "Edit|Write|Bash"


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def plan(before, new_matcher=DEFAULT_MATCHER):
    """Return (after, changed_leaf_paths, (i, j)) for the one hook entry this installer owns. Raises on ambiguity."""
    if new_matcher not in MATCHERS:
        raise SystemExit(f"refusing: matcher must be one of {MATCHERS}")
    targets = [(i, j) for i, h in enumerate(before.get("hooks", {}).get("PreToolUse", []))
               if h.get("matcher") in MATCHERS
               for j, hh in enumerate(h.get("hooks", [])) if any(m in str(hh.get("command", "")) for m in MARKERS)]
    if len(targets) != 1:
        raise SystemExit(f"refusing: expected exactly one Edit|Write[|Bash] instinct hook entry, found {len(targets)}")
    i, j = targets[0]
    after = json.loads(json.dumps(before))
    after["hooks"]["PreToolUse"][i]["hooks"][j]["command"] = NEW_CMD
    after["hooks"]["PreToolUse"][i]["matcher"] = new_matcher
    b, a = dict(leaves(before)), dict(leaves(after))
    if set(b) != set(a):
        raise SystemExit("refusing: leaf set changed")
    changed = [p for p in b if b[p] != a[p]]
    allowed = {f"/hooks/PreToolUse[{i}]/hooks[{j}]/command", f"/hooks/PreToolUse[{i}]/matcher"}
    if any(p not in allowed for p in changed):
        raise SystemExit(f"refusing: diff touches leaves outside the hook entry: {changed}")
    return after, changed, (i, j)


def main():
    dry = "--dry-run" in sys.argv
    matcher = sys.argv[sys.argv.index("--matcher") + 1] if "--matcher" in sys.argv else DEFAULT_MATCHER
    raw = SETTINGS.read_text()
    before = json.loads(raw)
    after, changed, (i, j) = plan(before, matcher)
    old_cmd = before["hooks"]["PreToolUse"][i]["hooks"][j]["command"]
    print(f"settings sha256 before: {hashlib.sha256(raw.encode()).hexdigest()[:16]}  leaves={len(list(leaves(before)))}")
    print(f"changed leaves: {len(changed)} -> {changed}")
    print(f"old command: {old_cmd[:90]}\nnew command: {NEW_CMD}\nmatcher: {before['hooks']['PreToolUse'][i].get('matcher')} -> {matcher}")
    if not changed:  # already installed: refresh only the script copy, never touch settings
        if dry:
            print("dry-run: already installed; would refresh the script copy only"); return
        DST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC, DST); os.chmod(DST, 0o755)
        print(f"settings unchanged (already installed). hook refreshed: {DST} "
              f"sha256={hashlib.sha256(DST.read_bytes()).hexdigest()[:16]} (== source: "
              f"{hashlib.sha256(SRC.read_bytes()).hexdigest()[:16]})")
        return
    if dry:
        print("dry-run: nothing written"); return
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SRC, DST); os.chmod(DST, 0o755)
    print(f"hook installed: {DST} sha256={hashlib.sha256(DST.read_bytes()).hexdigest()[:16]} (== source: {hashlib.sha256(SRC.read_bytes()).hexdigest()[:16]})")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = SETTINGS.with_name(f"settings.json.bak-hookfix-{ts}")
    shutil.copy2(SETTINGS, bak); print(f"backup: {bak}")
    # re-read immediately before writing to shrink the race window with other writers
    latest = json.loads(SETTINGS.read_text())
    if latest != before:
        sys.exit("refusing: settings.json changed underneath us between read and write; re-run")
    tmp = tempfile.NamedTemporaryFile("w", dir=SETTINGS.parent, delete=False, prefix=".settings.", suffix=".tmp")
    json.dump(after, tmp, indent=2); tmp.write("\n"); tmp.close()
    os.replace(tmp.name, SETTINGS)
    verify = json.loads(SETTINGS.read_text())
    assert verify == after, "post-write verification failed"
    print(f"written: {SETTINGS} sha256={hashlib.sha256(SETTINGS.read_bytes()).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
