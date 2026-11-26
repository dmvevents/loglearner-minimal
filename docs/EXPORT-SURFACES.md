# Export surfaces — the audited inventory of every path bytes can leave this host

The north star requires a standing inventory: every outbound
surface, its content class, and the gate that covers it. Rule of the house:
**a surface with no fail-closed gate is a finding, not a footnote.**
Audited 2026-08-27 by grepping every `subprocess`/`urllib`/HTTP call site in
`loglearner/`, `scripts/`, `dashboard/` (see `tests/test_synthesizer_egress_gate.py`
for the enforcement of the LLM-prompt class).

| # | Surface | Content class | Destination | Gate | Status |
|---|---------|--------------|-------------|------|--------|
| 1 | `example_synthesizer._query_model` (synthesis + consensus prompts; also imported by `dashboard/app.py` LLM-abstract) | **harvested session windows** (highest risk) | external LLM APIs via query-model.sh | `egress_gate.scrub_for_egress` — scrub + detect on scrubbed, fail-closed; refusal → heuristic synthesis | **GATED 2026-08-27** (was an open finding) |
| 2 | `instinct_deduplicator._query_model` (similarity prompts) | rule + evidence text (session-derived) | external LLM APIs | same gate (`dedup-prompt`) | **GATED 2026-08-27** |
| 3 | `scripts/mine_from_labels.call_llm` (rule-mining prompts) | gap summaries + user-message excerpts | external LLM API (gpt-5.4) | same gate (`mine-prompt`) | **GATED 2026-08-27** |
| 4 | `scripts/derive_labeling_profile.run_claude` | corpus evidence sample | `claude -p` (Anthropic API) | input-side fail-closed: `--texts` refuses without scrub config, detector hit quarantines the text, all samples scrubbed before the prompt is built; index path reads only the scrubbed index | GATED (at source) |
| 5 | Gist/GitHub pushes (`export_gate.scrubbed_tarball`, index/report writers) | knowledge artifacts | github.com (PRIVATE repo/gists) | `export_gate` + whole-file `verify_clean()` + `detect()`, fail-closed; `tests/test_export_gate.py` | GATED (KR5.2) |
| 6 | Plane board cards (`route_learnings_to_boards.py`, board publishers) | facets + URLs only | loopback Plane (127.0.0.1) | per-card scrub gate before any write; bodies never include gist content | GATED |
| 7 | North-star skeptic prompts (`~/.loglearner/issues/bin/north-star-check.sh`) | abstracted product summary | external LLM APIs | redact-scan BLOCK tier, refuses to dispatch; mutation-tested (`test_north_star_check.sh`) | GATED (issues tooling) |
| 8 | Dashboard → Hindsight proxy (`dashboard/app.py` `hindsight_get/post`) | recall queries / retained notes | 127.0.0.1:8888, **but Hindsight itself calls OpenAI for extraction** | loopback bind; content policy: only scrubbed/abstract notes are retained (hindsight-memory skill caveat) | INDIRECT — policy-gated, not code-gated; carded under #19 lifecycle if Hindsight use grows |
| 9 | VLM screenshot lane (`/gists` self-improve loop) | dashboard pixels | none today — PNGs read locally by Claude-in-session | boundary documented in `docs/SELF-IMPROVING-VIEW.md`; MUST adopt the egress gate before any external vision API | LOCAL-ONLY (no egress) |
| 10 | Telemetry / caches / logs (`output*/`, `state/`, `scratch/`) | pipeline artifacts | none — local disk only; `state/` knowledge files are scrub-gated at write time (surface 5 applies on push) | write-time scrub for indexes; repo private | LOCAL-ONLY |
| 11 | `gh` CLI reads (gist LIST metadata, issue bodies) | inbound only | github.com | n/a (no payload leaves beyond API routing) | INBOUND |

## The doctrine (applies to any NEW surface)

1. No scrub config resolvable ⇒ the external call does not happen (the local
   fallback runs instead). Privacy degrades capability, never the reverse.
2. Scrub, then run detectors **on the scrubbed text**; any hit ⇒ refuse.
   Built-in replacement classes ship with the engine and need no config entry: a user's home directory, plain
   (`/home/<user>`, `/Users/<user>`) or in Claude Code's project-directory encoding (`-home-<user>-…`, the directory
   names under `~/.claude/projects/`), becomes `~` or `-HOME`; the residual scan covers them like any configured class.
3. The gate must have a test that FAILS if the gate call is removed
   (planted-secret + no-config-no-call), like
   `tests/test_synthesizer_egress_gate.py`.
4. Adding an outbound call without a row in this table fails review.
