# ADR 0035: Validated work has a disposition at every issue-terminal boundary

**Status:** Proposed
**Date:** 2026-08-08
**Tracking issue:** #7024 (design review) — implementation slice #6914
**Related:** ADR-0026 (persistent exchange pair), ADR-0027 (exchange resume state machine),
ADR-0031 (tech lead / graduated authority), ADR-0013 (labels as crash-safe truth),
ADR-0019 (agent-done completion protocol), ADR-0016 (orchestrator as mediator)
**Full contract:** [docs/design/validated-work-disposition.md](../../design/validated-work-disposition.md)

## Context

`control/review_exchange_lifecycle.py` already declares itself the behavior owner
for issue terminal boundaries. `terminate_issue_runtime()` centralises teardown of
every issue-scoped runtime owner — the persistent coder/reviewer pair, supervised
review-exchange jobs, visible `issue-N`/`rework-N` terminals, stale active-session
records, and publish-retry — and `has_active_issue_runtime()` is its symmetric
activity predicate over the *same* owner set. Teardown is not ad hoc.

The gap is narrower than "teardown is scattered". The owner's inputs and its typed
`IssueRuntimeTermination` result carry only job and session identifiers
(`cancelled_job_ids`, `stopped_session_ids`, `cleared_active_session_ids`). The
boundary can therefore release every runtime owner cleanly while being *blind* to
whether completed-and-validated work exists at the instant it fires. It cannot
enforce the one invariant that all of the open review-exchange case files violate:

> Work an agent completed and validation passed must survive abnormal termination.

Three open case-file patterns and a constellation of incidents converge here:
#7012 (max-rounds strands validated work — #5204 14 commits, #5561 34 commits, local
only), #7017 (detached HEAD invalidates review evidence), #6913 (coder no-completion),
plus #6987, #6960, #6986, #6672, and the after-the-fact rescue lanes #6914/#7011.

Four concrete mechanisms in today's code destroy or strand the work:

1. **Non-ok exchange outcomes are a bare halt.** The exchange already produces a
   distinct terminal — `ReviewExchangeStatus.STOPPED` /
   `ReviewExchangeReason.MAX_ROUNDS_EXCEEDED`. `completion_review_exchange.py`
   treats *any* non-ok outcome as `halt=True`: publish never runs, no disposition
   of the validated work is recorded, and the outer failure path
   (`session_controller`/`session_completion`/`stuck_sweep`) later re-surfaces the
   incident as generic `timed_out`.
2. **The finalization matrix cannot see the work.**
   `domain/completion_finalization.py` decides cancel-vs-defer from runtime facts
   only (`review_exchange_running`, `review_exchange_within_deadline`); completion,
   validation, and branch facts are not inputs.
3. **Every durable artifact lives inside the worktree.** `SessionRunAssets` requires
   `run_dir` to be *under* `worktree_path`, and `PublishRetryLocators` stores
   `worktree_path` + `completion_path` + `run_assets`. `reset_issue(from_scratch=True)`
   removes the worktree with `force=True`. The worktree is therefore the de-facto
   retention boundary for the only evidence a recovery could use —
   `locator_block_reason()` correctly fails closed with "Retry worktree no longer
   exists", but by then the work is unrecoverable (Porchpin #126).
4. **Recovery is bolted on per incident.** #6914 and #7011 are hand-built rescue
   lanes for already-stranded branches; nothing prevents the next one.

## Decision

**Extend the existing issue-terminal owner. Do not mint a sibling.**

1. `terminate_issue_runtime()` composes exactly one new behavior-level owner,
   `ValidatedWorkDispositionOwner`, and **cannot finish teardown until the
   disposition of any completed-and-validated work is durably recorded.**
   Disposition runs *before* any runtime owner is released, while the pair,
   worktree, and run directory still exist. A failure to record fails closed and
   raises; teardown does not proceed.

2. `IssueRuntimeTermination` gains a required `validated_work: ValidatedWorkDisposition`
   field with **seven** states, partitioned into two sets that the contract keeps
   distinct because conflating them is how work gets destroyed:

   | Set | States | Meaning |
   |---|---|---|
   | Unresolved | `QUEUED` (automatic recovery), `PARKED` (awaiting approval), `PUBLISHING` (submission in flight), `FAILED` (fail-closed) | work exists and must not be lost; blocks teardown-with-destruction and scratch reset |
   | Resolved | `NONE` (no work here), `RECOVERED` (published + routed), `ABANDONED` (operator explicitly accepted the loss) | nothing is at risk |

   An unverifiable or failed disposition preserves artifacts and must never
   collapse into generic `timed_out`, cleanup success, or scratch-reset
   eligibility. **`FAILED` is unresolved, not terminal** — the only routes out are a
   durable recovery or an explicit `ABANDONED` carrying an operator identity and
   reason. No timeout, sweep, or age heuristic resolves it.

   The publishable commit is the validation record's `head_sha` **exactly**. A
   worktree that has advanced past its validation is preserved and parked, never
   published: ancestry is not identity.

3. **One owner, three initiators.** Automatic recovery at the boundary, the gated
   tech-lead `recover_validated_work` op, and the operator Control Center command
   all call the same owner and receive the same typed result. The owner — not the
   initiators — owns trusted artifact admission, stale checks, idempotency,
   durable state, publish-retry reconstruction, fast-forward publication, review
   routing, label finalization, and partial-write reconciliation.

4. **Escrow outside the worktree; commits pinned by refs.** Admitted evidence is
   copied to `<state_dir>/validated-work/<issue>/<evidence_id>/` and the validated
   commit is pinned by `refs/issue-orchestrator/validated/<issue>/<evidence_id>` in
   the shared object store; unvalidated commits sitting on top of it are pinned
   separately under `refs/issue-orchestrator/observed/...` so that refusing to
   publish them never means allowing them to be collected. Worktree removal stops
   being a data-loss event. `evidence_id` is a canonical hash over stable semantic
   facts and admitted content hashes only — capture timestamps and mutable
   observations are excluded — so a crash-and-retry re-derives the same identity
   and converges on one record instead of minting a second.

5. **`has_active_issue_runtime()` gains a fifth probe** — `has_unresolved_work()`,
   true for every state in the unresolved set above. The predicate is named for the
   question its callers ask ("is there work here that must not be destroyed?")
   rather than for a workflow phase, because a `pending`-shaped predicate reads
   false for a `FAILED` record and would hand exactly the work this ADR protects to
   the nuclear reset. The reset-freshness predicate and the teardown boundary keep
   reading the same owner set, and callers must **consume** the returned
   disposition rather than discard it, so scratch reset can never discard work the
   predicate did not observe.

6. **Authority rides the existing gated lane.** `recover_validated_work` is added
   to `ACT_LEVEL_TECH_LEAD_ACTIONS` and bound through the existing immutable
   `StoredTechLeadOp` / `tech_lead_proposal_ops` lifecycle. No second approval
   table, no second scanner. Recovery preserves branch/PR/review/rework lineage;
   it may never call the reset-from-scratch owner, force-push, delete work, or
   clear blocking state before publication and review routing succeed.

7. **Publication is an execution-only port, composed by both callers.** The
   existing `PublishRecoveryService.retry_publish()` entry point cannot serve the
   disposition owner: it treats *any* matching open PR as an already-recovered
   state and finalizes without ever pushing, never comparing the PR's head with the
   commit to be landed — so in the canonical strand (open PR at the old remote head,
   validated commits local-only) it would report success while the work stayed
   unpublished. The execution half is therefore extracted behind
   `ValidatedHeadPublisher.publish_or_reconcile()`, a narrow command carrying
   evidence identity, expected remote head, target validated head, branch/PR
   identity, review disposition, and a derived submission token, and returning a
   durable owner-observable outcome that distinguishes "publish", "already at
   target — reconcile only", and "diverged — write nothing".

   Admission policy stays with each caller: manual Retry Publish keeps its board
   and locator admission and the Control Center surface; the disposition owner
   keeps evidence admission and its stale checks. Neither duplicates the other's
   finalization. This also fixes the existing-PR shortcut for the manual path.

## Consequences

- The termination boundary becomes the one place that answers "what happened to
  the finished work?", so the `STOPPED`/`MAX_ROUNDS_EXCEEDED` outcome, the
  timed-out finalization matrix, detached-HEAD/workspace-freshness (#7017), and
  shutdown races all route through one contract instead of four policies.
- Teardown gains a failure mode it did not have: escrow failure blocks teardown.
  That is deliberate and fail-closed — losing validated work is the worse outcome.
- A new registered SQLite store and an escrow directory join the orchestrator-owned
  state dir, with their own retention boundary that `session_output_retention_days`
  and worktree cleanup must not cross. That boundary is driven by a new top-level
  config section, `Config.validated_work`, which brings the full set of surfaces a
  new section requires in this codebase (model, section key, parser, shape
  validation, serialization, settings schema, generated reference, example, tests);
  escrow and refs are released only for resolved records past the window, and never
  for unresolved ones at any age.
- Unresolved work has no automatic exit. That is a deliberate cost: an issue can sit
  blocked indefinitely on a `FAILED` disposition until a human recovers or
  explicitly abandons it. The alternative — any time- or sweep-based resolution — is
  a rediscovery of the bug this ADR closes.
- #7018 (route `MAX_ROUNDS_EXCEEDED` into disposition) becomes a consumer of this
  contract rather than a standalone point fix; #6914 becomes its behavior-complete
  implementation.

## Scope boundary

ADR/design #6965 covers verdict **delivery** — how a verdict gets home across
deployment shapes. This ADR covers lifecycle **termination** and work preservation.
Together they bound the two ways the exchange loses finished work: in transit
(#6965) and at shutdown (here). #7008 `request_rework` is the *semantic* lane — a
finding says the implementation needs more coding; `recover_validated_work` disposes
work that is already completed and validated.
