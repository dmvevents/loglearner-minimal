# RULE-DELIVERY — how a learned rule actually reaches a session

*Written 2026-09-27 after measuring that it did not. Companion to `docs/RULE-INJECTION.md` (which
governs rules injected into prompts we build) — this doc governs rules delivered by the Claude Code
hook into someone else's live session.*

## The two defects this replaced

The global `PreToolUse` `Edit|Write` hook installed 2026-05 delivered **nothing**, for two
independent reasons:

1. **Dead route.** It branched on `case "$CLAUDE_FILE_PATH"`. That variable is not defined by Claude
   Code (0 occurrences in the v2.1.283 binary). Hooks receive JSON on **stdin**; the field is
   `tool_input.file_path`. With the variable unset, every branch fell through and the loader never ran.
2. **Wrong output channel.** Even when the route did fire (env var supplied by hand), it printed the
   rules as **plain text**. Per the hook reference, for `PreToolUse` *"plain stdout on exit 0 goes to
   the debug log, not the transcript"*; context reaches the model only as
   `hookSpecificOutput.additionalContext`. So the one path that appeared to work still delivered
   nothing to the model.

Measured (probe = JSON on stdin, `CLAUDE_FILE_PATH` unset, i.e. how Claude Code really invokes hooks):

| file shape | old hook | new hook |
|---|---|---|
| `internode_ll.cu` | 0 instincts, invalid hook JSON | valid JSON, 4-8 rules |
| `utils.cuh`, `config.hpp` | 0 | valid, 6 |
| `proxy_efa.cpp` | 0 | valid, 6 |
| `buffer.py` | 0 | valid, 6 |
| Neuron-named `.py` | 0 | valid, 6 |
| `README.md` | 0 | caution rules only (correct) |

The header/source forms (`.cuh/.hpp/.cpp/.h/.c`) were also **absent from the old glob table** — the
CUDA and header files this fleet edits most had no route at all, even in principle.

## What the hook does now

`scripts/hooks/pretooluse-instinct-context.py` (installed at `~/.claude/hooks/`), two retrieval
passes, task context first:

- **pass 2 — content tokens.** The most frequent snake_case identifiers in the text being written,
  matched word-boundary against **both** stores: the fleet learnings bank (the JSONL banks, newest first)
  and `instincts.json`. This is the fix for the audit's second-order finding that *filename is not
  task context*: an edit that sets `num_sms` now retrieves `num_sms` knowledge.
- **pass 1 — file route.** The 2026-05 glob table plus the header/source forms it missed.

Ranking inside `instincts.json` mirrors `load-instincts.sh` (rule-text hits before
evidence/source-only hits, then confidence) with one refinement: **more distinct keywords hit in the
rule ranks higher**, so a specific token can outrank an older generic rule of equal confidence.

Output is one JSON object with `hookSpecificOutput.additionalContext`, or nothing at all.

## Bounds (context is not free)

| bound | env | default | behaviour |
|---|---|---|---|
| route rules considered | `INSTINCT_LIMIT` | 6 | |
| total instinct text | `INSTINCT_BUDGET_CHARS` | 3600 (~900 tokens) | whole rules dropped, **never truncated mid-rule**; dropped ids listed in the manifest |
| caution text | `CAUTION_BUDGET_CHARS` | 2000 | over budget → a pointer, not the text |
| content tokens | `CONTENT_TOKENS` | 3 | |
| per-session repeat | `DELIVERY_TTL_S` | 1800 s | a rule delivered within the TTL is withheld; suppression is applied **before** each store's limit, so lower-ranked matches rotate in on later edits until the matched set is exhausted (then the hook goes silent for that edit shape). Tested with a four-rule fixture and a three-rule limit on both stores — the first implementation truncated before suppressing, which made the 4th rule undeliverable |

Every delivered block passes through the record's own `Status:` and `Provenance:` when the store has
them (both stores — `instincts.json` rows carry `status`/`provenance` too). The header uses neutral
mixed-evidence wording: the matched set deliberately spans measured runs, source readings and
**unmeasured designs** (the fleet pass retrieves NH-A3-class design records), so the reader is told to
weigh each record by its own Status/Provenance/Evidence lines, that a `negative`/REFUTED entry is a
warning not a recipe, and that a `designed`/`unmeasured` entry is a hypothesis not a result.

## Correction-aware retrieval (post-resume review, 2026-09-27)

A correction appended to the bank is useless if retrieval keeps re-injecting the record it corrects.
That happened: for content `num_sms = 64; num_qps = 6` the hook delivered the superseded record (whose
evidence text carries a false arithmetic string) but not its appended correction, because the
correction's rule uses the C++ template names and matched neither token.

Rule now enforced: **a selected record with an explicitly linked correction is never emitted alone as
current evidence.** Links are explicit only — a rule that opens `CORRECTION … to|of|for <id>`, or a
`corrects: [ids]` list on the record or its `meta`. Generic `cross_ref` is *not* a link (it marks
related records, not superseding ones). Given a link:

1. the correction rides along with its target regardless of query tokens and of the TTL cache;
2. the target gets a `Corrected-by:` line; the pair is bounded **atomically**;
3. if the pair cannot fit the budget, the correction alone is delivered in the target's place
   (manifest `correction_substituted`), because it is self-contained current evidence and the target's
   evidence is superseded where they conflict;
4. if nothing fits, or the correction was already delivered this session, the target is omitted
   (manifest `omitted_uncorrected`) — never emitted alone;
5. targets handled through this path are marked in the session cache so they are not re-selected on
   every edit (no repeat injection of the correction).

Budgets are unchanged. Proven on an isolated fixture where the correction lacks the query tokens:
pairing, tight-budget substitution, neither-fits omission, `meta.corrects` form, cross_ref non-link,
TTL exemption, correction-alone, no re-selection. On the real store the reviewer's fresh-cache probe
now delivers the correction on the first edit and never re-injects the superseded string.

## Exposure, and what it is not

Each delivery appends one row per rule to `output/loglearner_exposures.jsonl`:
`{session_id, arm, instinct_id, project, decision_ref, ts}`. `decision_ref` is the tool and file, so
the row records *which session received which rule before which edit*.

This is the join `arms.measure_arms` requires, and it is **delivery only**:

- it is **not** a reward — no observation, no bandit pull, nothing in `loglearner_rewards.jsonl`;
- an exposed rule with no outcome evidence stays **unmeasured**, which is the fail-closed default;
- the hook never reads `state/candidates/` — **no candidate can be promoted as a side effect of delivery.**

Disable with `HOOK_DISABLE_EXPOSURE=1`. The ledger lives under gitignored `output/`.

## Safety properties

Always exits 0 and never blocks a tool; stdout is empty or exactly one JSON object; diagnostics go to
stderr only; every knowledge store is opened read-only; a missing store degrades to silence. Runs in
~0.2-0.7 s (the old shell path spent ~4 s in `jq`; matching is now in-process).

## Verification

- `tests/test_pretooluse_hook.py` — 24 tests: delivery as `additionalContext`, env var not required,
  all five file forms, content-token retrieval, status/provenance passthrough + neutral header,
  caution routing preserved, budget bounds with whole rules, exposure == delivered set, cache
  suppression, TTL expiry, no-route silence, missing stores, exposure disable, rotation on an isolated
  four-rule fixture with a three-rule limit for both stores (watched RED before the fix), and the
  nine correction-aware cases above (watched RED before the fix).
- **FAILS-if-removed:** replacing the JSON wrapper with plain text makes the output invalid hook JSON
  (3,689 bytes that would land in the debug log) — the wrapper is the delivery mechanism.
- `scripts/hooks/probe_hook.py <command|@file> <path> [tool] [content]` probes any hook command
  offline; exposure writes are redirected so probes never touch the real ledger.
- **Live end-to-end:** writing a `.cu` file through the Write tool in a real session delivered 3 of 11
  matched rules with a manifest and wrote 3 exposure rows (`docs/reviews/2026-09-27-hook-route-probe/`).

## Installing / rolling back

`scripts/hooks/install_pretooluse_hook.py [--dry-run]` re-reads the live `~/.claude/settings.json`,
and is idempotent: when the command is already installed it refreshes only the script copy under
`~/.claude/hooks/` and leaves settings untouched. Otherwise it
replaces **only** the one Edit|Write hook command, and aborts unless the JSON diff is exactly that one
leaf — other agents own `model`, `enabledPlugins`, `statusLine`, `mcpServers`, `env`. It backs up to
`settings.json.bak-hookfix-<ts>` and re-reads immediately before writing to shrink the race window.
Roll back by restoring that backup; settings edits apply to running sessions without a restart.

## Bash routing and token-based paths (0.1.1, 2026-10-13)

- A Bash tool call is routed on the command's program-name tokens (`docker`, `kubectl`, `aws <service>`, `fi_info`,
  `srun`, `git`, `pip`, `vllm`, ...) and gets the same content-token pass over the command text, the same budget,
  delivery cache and exposure ledger (`decision_ref` = `Bash:<route>`); it is quiet when nothing matches. The settings
  matcher becomes `Edit|Write|Bash`.
- File routes match path TOKENS (segments and words split on `/ _ - .`), never substrings: `defaults/Dockerfile` no
  longer routes to the EFA rules because "defaults" contains "efa" (consumer-found defect).
- A record's `tags`, `category` and `title` are part of the match haystack, so pack records tagged with the routing
  vocabulary are found even when the rule text uses other words.
- `python3 -m loglearner.cli recall` applies the same semantics from a Bash call; `bank` is the sanctioned write path.
  See `docs/AGENT-USAGE.md`.

## Agent-engineering class (operator direction, 2026-10-13)

Records with category `agent-engineering` (skills and triggers, hooks, settings and permission modes, model fallbacks and
safety stops, sandboxing, lane operating rules, brief patterns, the autonomy contract) are routed when the tool call touches
an agent's own configuration: `settings.json`/`settings.local.json`/`keybindings.json`, `AGENTS.md`, `CLAUDE.md`, `SKILL.md`,
any file under a `claude`/`codex` directory (including `.claude/rules/*.md`), `hooks/*.{py,sh,mjs,js}`, `skills/*.md`, a codex `config.toml`, agent memory files (`*/memory/{feedback,reference,project,user}_*.md`, `MEMORY.md`); and Bash calls
to `claude`, `codex` or `tmux`. The route's keywords are the class id plus its tag vocabulary, so every class record is a
candidate and the ranking picks within the cap. The taxonomy itself is data: category + tags on the records.

## Retired records are never delivered (knowledge-fabric review F6, 2026-10-13)

A row whose `lifecycle_state` is deprecated, contested or dormant, whose `status` is deprecated, retired, superseded,
withdrawn or tombstoned, or that carries a truthy `tombstone`/`retired` flag is skipped at delivery time — in both stores
the hook reads, in the correction index (a retired correction never rides along) and in `loglearner.cli recall`. `status:
negative` stays deliverable: a warning is not a tombstone. A hub that tombstones a superseded row by marking it can rely on
this instead of removing the row from the distribution.
