# KNOWLEDGE-LIFECYCLE — states, conflict, decay, eviction, authority

*North-star gate N5, closed 2026-08-27. 2/3 skeptic-panel convergence
(gemini-3-pro + gpt-5.5-pro): the system mined new rules against a legacy base
with no specification for contradiction, staleness, invalidation, or authority.
This doc is that specification; `loglearner/lifecycle.py` implements it and
`tests/test_knowledge_lifecycle.py` proves the load-bearing transitions
(contradiction ⇒ conflict record; REVERT ⇒ out of the recall index).
Owner decisions D1/D2 below were made under the operator's 2026-08-27
autonomy grant.*

## 1. State machine

Every knowledge object (rule/instinct) carries `lifecycle_state` plus an
append-only `lifecycle_history` ledger (`{state, at, by, reason}` — states
change, entries never disappear: ledger over deletion).

```
                 mine (synthesizer)
                        │
                        ▼
                   ┌───────────┐   VerifyStep REVERT
                   │ candidate │──────────────────────────┐
                   └───────────┘                          │
                        │ VerifyStep KEEP                 ▼
                        ▼                          ┌────────────┐
                   ┌───────────┐  refuted reward / │ deprecated │
          ┌───────▶│ screened  │───REVERT──────────▶            │
          │        └───────────┘                   └────────────┘
   human restore /      │ retrieved into a session        ▲
   fresh evidence       ▼                                 │
          │        ┌───────────┐                          │
          ├────────│ recalled  │───refuted────────────────┤
          │        └───────────┘                          │
          │             │ reward = 1                      │
          │             ▼                                 │
          │        ┌───────────┐                          │
          ├────────│ rewarded  │───refuted────────────────┘
          │        └───────────┘
          │             │
          │   conflict loss (any serving state)
          │             ▼
          │        ┌───────────┐         decay (D2)   ┌─────────┐
          └────────│ contested │      serving ───────▶│ dormant │──┐
                   └───────────┘                      └─────────┘  │
                        ▲                                  ▲       │
                        └── human resolve ──── human restore ──────┘
```

**Serving set (the recall index):** `screened`, `recalled`, `rewarded` — plus
legacy rules with no `lifecycle_state` field, which are grandfathered as
`screened` (the entire shipped unified-v5 library predates this field).
`candidate` never reaches a serving surface: the verification gate stands
between synthesis and recall. `deprecated`, `dormant`, `contested` are
excluded from every recall surface: the Hindsight sink filters before retain
(and the bank is rebuilt from the filtered set), and deterministic retrieval
(`scripts/efficacy_eval.py`) filters at rule load. Eviction is therefore a
*state change that recall respects*, never a deletion — restore is always
possible and the history explains every bench.

## 2. Contradiction handling

Two rules **conflict** when their directive polarity differs (one says
never/don't/avoid, the other always/must/ensure) and their content-word
signatures overlap ≥ 0.5 (same subject, opposite instruction). Detection is
deterministic and lexical (invariant 6: no LLM inside a gate); it flags
*potential* semantic contradiction — double-negation agreements ("never skip
X" vs "always do X") can false-positive, which is why the conflict queue is
reviewable rather than auto-deleting anything.

On detection (runs inside `instinct_deduplicator.deduplicate_and_merge`, the
place merged libraries are born):

1. A **conflict record** is written to `conflicts.jsonl` beside the library —
   `{conflict_id, rule_a, rule_b, polarities, shared_terms, winner, loser,
   resolution_basis, pending_human_review, detected_at}`. Silently keeping
   both (the pre-#19 behavior) is the failure mode this kills.
2. **Deterministic resolution** picks a winner by total order:
   **evidence recency** (`provenance.added_date`; fresher corpus wins — the
   calibration audit showed evidence expires, so newer mining reflects the living
   corpus) → **confidence** → **merge_count** (independent discoveries) →
   **id** (pure tiebreak). The loser moves to `contested` and leaves the
   recall index; the winner keeps serving. Serving *both* directives would
   inject contradiction straight into an agent's prompt — worse than serving
   one defensible side.
3. A `contested` loser with confidence ≥ 8 sets `pending_human_review: true`
   (see authority matrix): the bench is immediate, the human review is the
   escalation, and every report surfaces the queue until it is empty.

## 3. Decay schedule (owner decision D2)

**Half-life = 180 days unexercised.** Effective confidence is computed, not
destructively written: `conf(t) = conf₀ // 2^(completed half-lives)` — step
decay, so nothing is lost until a full 180 days pass without exercise.
A rule whose effective confidence falls ≤ 2 is marked `dormant` by the
maintenance pass (`lifecycle.apply_decay`) and leaves the recall index.
The exercise clock starts at `provenance.added_date` and resets on retrieval
or reward (whichever is latest in `lifecycle_history`). Rules with no dated
provenance do not decay (unknown age is not evidence of staleness) — they are
instead subject to the evidence cap below.

**Evidence cap (from the calibration audit):** a rule whose cited
sessions are gone AND which has no mint-time evidence digest is unfalsifiable;
its effective confidence caps at **5** (the consensus-screened ceiling)
regardless of stored confidence.

*Why 180 days:* observed Claude Code session retention is ~3.5 months (49/55
shipped rules had already lost their cited evidence by audit time), and the
fleet's dependency-freshness doctrine draws the staleness line at 6 months.
One half-life ≈ one retention period past mining; a cross-project 10/10 rule
survives two half-lives (a year) before benching, a single-source 5 benches
after one. Half-life, not cliff: confidence degrades in proportion to disuse.

## 4. Invalidation triggers

| Trigger | Effect | Actor |
|---|---|---|
| VerifyStep REVERT verdict | `deprecated` (candidate never serves) | deterministic gate |
| Refuted reward (`meta.reward=0` + negative status) | serving → `deprecated` | deterministic signal |
| Conflict loss | serving → `contested` | deterministic resolution |
| Decay below threshold | serving → `dormant` | maintenance pass |
| Evidence expiry without digest | confidence capped at 5 (stays serving) | maintenance pass |
| Human judgment | any transition, incl. restore | human |

## 5. Authority matrix (owner decision D1)

| Action | Reward signal / deterministic gate | LLM (synthesis, consensus, VLM) | Human |
|---|---|---|---|
| Mint `candidate` | — | proposes | may author |
| Promote to `screened` | executes (verification gate) | proposes verdict inputs only | may override |
| Deprecate / bench any rule | **executes** (reversible state) | proposes only, never executes | executes; may veto/restore |
| Bench high-confidence (≥ 8) legacy rule | executes **+ sets `pending_human_review`** | proposes only | reviews the flag queue |
| Restore from deprecated/dormant/contested | — | — | **sole authority** |
| Physical deletion | **never** | **never** | **never** (archive only) |

**D1 — the reward signal is authoritative to *bench*, humans are authoritative
to *restore*.** A REVERT/refuted verdict deprecates any rule — including
high-confidence legacy — without waiting for sign-off, because deprecation is
a reversible state change while a known-bad rule left serving does live harm
in every session that recalls it (fail-safe direction). The safeguard is not
a pre-approval gate but a flagged, surfaced review queue: high-confidence
benches carry `pending_human_review: true` and appear in every loop report
until a human clears or restores them. Probabilistic critics (LLM verdicts)
never execute a transition — they feed the deterministic gates that do
(invariant 6).

## 6. Evidence archival

**Decision: digests live beside the library they attest.** At output time
(`WriteOutputsStep`), `loglearner/evidence_digest.py` extracts each shipped
rule's cited session window and persists a scrubbed digest to
`<output-dir>/evidence-digests.jsonl` next to `instincts.json` — so
verification survives Claude Code session retention (~3.5 months), which
already orphaned 49/55 shipped rules (calibration audit, coverage 0.109).

Fail-closed, same doctrine as every export surface: **no scrub config ⇒ no
digest persisted** (`status: no-scrub-config`); **detector hit on the
scrubbed digest ⇒ nothing persisted** (`status: quarantined`). Only
`status: ok` records carry text. The calibration audit's *local* digests
(`state/calibration/`, gitignored, unscrubbed, human-audit-only) are a
different artifact and stay local; both share one extractor implementation.
Invalidation and eviction reference digests by rule id + `ref`
(`<session8> <start>-<end>`), so a future re-audit can re-check a rule even
after its session is purged.

## 7. Surfaces bound by this doc

- `loglearner/lifecycle.py` — states, contradiction detection, resolution,
  verdict application, decay, `filter_servable` (THE recall-index filter).
- `loglearner/instinct_deduplicator.py` — emits `conflicts.jsonl` at merge.
- `loglearner/sinks/hindsight_sink.py` — retains only servable rules; the
  bank rebuild evicts benched ones.
- `scripts/efficacy_eval.py` — deterministic retrieval filters at load.
- `loglearner/evidence_digest.py` + `loglearner/steps.py` — mint-time
  archival.
- Gate: KR N5.1–N5.4 in `scripts/verify_okrs.sh`;
  tests: `tests/test_knowledge_lifecycle.py`.
