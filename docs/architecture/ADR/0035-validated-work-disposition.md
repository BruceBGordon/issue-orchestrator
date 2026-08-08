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
   initiators — owns trusted artifact admission, stale checks, idempotency, durable
   state, the publication workspace, and partial-write reconciliation, and it
   *composes* (rather than reimplements) the publisher and the finalizer of
   decision 8.

4. **Escrow outside the worktree; commits pinned by refs.** Admitted evidence is
   copied to `<state_dir>/validated-work/<issue>/<evidence_id>/`, together with a
   self-validating capture envelope that carries everything needed to rebuild the
   durable row without the worktree, and the validated commit is pinned by
   `refs/issue-orchestrator/validated/<issue>/<evidence_id>` in the shared object
   store; unvalidated commits sitting on top of it are pinned separately under
   `refs/issue-orchestrator/observed/...` so that refusing to publish them never
   means allowing them to be collected. Worktree removal stops being a data-loss
   event.

   Identity is split in two. `record_id` hashes *which work* this is (repo, issue,
   branch, validated head) and is the durable row's **primary key**, so two rows for
   one unit of work are unrepresentable in any state. `evidence_id` hashes *which
   evidence* carries it; new evidence for the same work supersedes in place, with
   the prior evidence audited and its escrow retained. Neither hash covers capture
   timestamps, mutable external observations, the moving worktree head, or
   filesystem paths, so a crash-and-retry re-derives both and converges.

5. **Publication reads a dedicated workspace and writes an immutable object.**
   Recovery publishes from a per-evidence worktree detached at the pinned validated
   ref — never the issue worktree, which may have advanced — and the remote write
   names the commit explicitly under an explicit expected-value lease
   (`--force-with-lease=<ref>:<expected>`), never the fetch-refreshed bare lease the
   generic push path uses. A worktree that moves cannot change what is published,
   and a remote that moves produces zero writes rather than a force-push.

6. **`has_active_issue_runtime()` gains a fifth probe** — `has_unresolved_work()`,
   true for every state in the unresolved set above. The predicate is named for the
   question its callers ask ("is there work here that must not be destroyed?")
   rather than for a workflow phase, because a `pending`-shaped predicate reads
   false for a `FAILED` record and would hand exactly the work this ADR protects to
   the nuclear reset. The owner is a **required parameter of the activity predicate
   as well as of teardown** — optional on one side would mean the two boundaries
   read the same owner set only by convention, and convention is what a future
   caller forgets. Callers must also **consume** the returned disposition rather
   than discard it, so scratch reset can never discard work the predicate did not
   observe. Two mechanical consequences the contract makes explicit rather than
   leaving to the implementer:

   - The predicate must be able to **exclude one named owner**, because the record
     being drained is itself unresolved. Without that, the owner's own probe blocks
     its own drain on every pass and automatic recovery never publishes anything.
     Exclusion is narrower than omission — the owner stays required, and exactly
     one caller may exclude exactly one kind.
   - `IssueRuntimeTerminator`, the injected callable
     (`control/history_reconciliation.py`) through which awaiting-merge
     reconciliation reaches the boundary, is typed `Callable[[int, str], object]`.
     That erased return type makes "bind the disposition" unstatable on a real
     terminal edge — one that fires when a PR is **closed**, not only merged. It is
     narrowed to return `IssueRuntimeTermination` as part of this decision.

7. **Authority rides the existing gated lane.** `recover_validated_work` is added
   to `ACT_LEVEL_TECH_LEAD_ACTIONS` and bound through the existing immutable
   `StoredTechLeadOp` / `tech_lead_proposal_ops` lifecycle. No second approval
   table, no second scanner. Recovery preserves branch/PR/review/rework lineage;
   it may never call the reset-from-scratch owner, force-push, delete work, or
   clear blocking state before publication and review routing succeed.

8. **Publication splits into three owners, each with one responsibility.** The
   existing `PublishRecoveryService.retry_publish()` entry point cannot serve the
   disposition owner: it treats *any* matching open PR as an already-recovered
   state and finalizes without ever pushing, never comparing the PR's head with the
   commit to be landed — so in the canonical strand (open PR at the old remote head,
   validated commits local-only) it would report success while the work stayed
   unpublished.

   | Layer | Owner | Needs orchestrator state |
   |---|---|---|
   | Remote execution | `ValidatedHeadPublisher` — exact-object/exact-lease branch write, then ensure exactly one PR | no |
   | Finalization | `PublishedWorkFinalizer` — labels, history, review routing, wrapping the existing `RetrySuccessFinalizer`/`RetryReviewRouting` | yes, on the request |
   | Admission | `PublishRecoveryService` (manual) and `ValidatedWorkDispositionService` (validated work), as **siblings** | yes |

   The publisher is execution-only precisely because finalization needs
   `OrchestratorState` and the publisher does not; folding the two together would
   force a hidden global or a back-reference from the executor to the manual
   service, recreating the coupling this split removes. The two admission owners
   compose the same executor and the same finalizer and never call each other, and
   `PublishRetryLocators` stays entirely with the manual path — the disposition
   owner has its own escrow, workspace, publisher and finalizer, so it needs no
   locator round-trip and no borrowed board preconditions. This also fixes the
   existing-PR shortcut for the manual path.

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
