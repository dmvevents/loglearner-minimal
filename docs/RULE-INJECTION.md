# Rule→prompt injection contract

The hypothesis an external validation panel raised: unbudgeted, conflict-blind
injection of learned rules into prompts.
Executable half: `loglearner/rule_injection.py`; suite:
`tests/test_rule_injection.py`.

**The contract: any surface that puts learned rules into an LLM prompt
routes through `render_rules_block(rules, budget)`.** It returns
`(block_text | None, manifest)`; `None` means "inject nothing" (no empty
headers). The manifest is the audit trail: included ids and every dropped
id with the reason (`benched` / `conflict` / `budget`).

## 1. Lifecycle gate first (defense in depth)

`filter_servable` runs inside the renderer even though every recall
surface already filters (KNOWLEDGE-LIFECYCLE §7): injection is the last
hop before an agent acts on a rule, so a benched (deprecated / contested
/ dormant) rule must be unable to reach a prompt through ANY caller path,
including a caller that assembled its rule list by hand.

## 2. Conflict handling — read-only resolution at the boundary

Mint-time conflict resolution (KNOWLEDGE-LIFECYCLE §2) benches losers, so
opposite-directive pairs should never both be servable — that removes the
worst instruction-conflict case at the source. Injection re-checks the
*candidate set* anyway: `detect_contradictions` over what is about to be
injected; if a pair survives, only the winner under the same total order
(evidence recency → confidence → merge_count → id, via the shared
`_resolution_key`) is injected. Unlike the mint-time pass this is
**read-only**: the library is never mutated by rendering a prompt — a
prompt build must not change lifecycle state.

Rationale: a prompt carrying both "always X" and "never X" is worse than
no injection — the model resolves the contradiction arbitrarily and the
reward signal attributes the outcome to both rules.

## 3. Token budget

- Estimator: `len(text) // 4` (chars-per-token heuristic; deterministic,
  offline, model-agnostic — the budget is a guardrail, not an exact bill).
- Default `DEFAULT_TOKEN_BUDGET = 1200` — small next to any model context
  but enough for ~25 typical rules; callers pass their own.
- Packing is greedy **with skip** in rank order: a rule that does not fit
  whole is dropped whole (id → `dropped_budget`) and packing continues,
  so a shorter lower-ranked rule can still use the room. **A rule is
  never truncated mid-text** — a half-directive ("never use sed on" …)
  can invert meaning.
- Ranking is `effective_confidence` (decay + unverifiable cap, so a stale
  or uncheckable rule outranks nothing) desc → merge_count desc → id asc.

## 4. Precedence: injected rules are advisory

The header line states it in-band: rules are *context distilled from past
failures*; on any conflict **the task instructions win**. Learned rules
must never be able to override what the operator asked for — that is the
instruction-conflict failure mode at the prompt level, distinct from
rule-vs-rule conflicts (§2).

## 5. Format (stable, attribution-carrying)

One header line containing `RULES RECALLED` + one `- [<id>] <rule>` line
per rule. Rule ids in-prompt make reward attribution and telemetry
possible (which recalled rule did the responder act on?). The A/B
harness's arms-differ-only-by-block property depends on exactly this
shape: header line + `- ` lines, nothing else.

## 6. Bound surfaces

| surface | status |
|---|---|
| `scripts/efficacy_eval.py` `build_prompt` (the surface #21 proved) | routed (KR N5.5 greps it) |
| synthesis prompts (`example_synthesizer`) | adopts at next mining run — synthesis injects *examples*, not rules; when rule blocks land there, they route here |

Note: the committed efficacy report (`state/efficacy/latest-report.json`)
predates this contract; its numbers stand (N1.1 reads the committed
artifact). The next live efficacy run inherits budgeted injection.
