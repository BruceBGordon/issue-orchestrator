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

2. `IssueRuntimeTermination` gains a required
   `validated_work: ValidatedWorkDispositionBatch` field — **every** disposition the
   capture produced, one per distinct unit of work. Singular was itself a data-loss
   path: an issue whose durable ledger holds two runs validated at different commits
   has two units of work, and a result that can hold one forces capture to pick a
   winner and tear the loser's worktree down un-escrowed. Lineage cannot protect a
   record admission never created. Recency selects *within* a unit of work; the
   choice *between* units is the lineage decision, made durably after all of them
   are admitted.

   Each member names its durable record and carries one of **six** states,
   partitioned into two sets that the contract keeps distinct because conflating
   them is how work gets destroyed. "No work here" is the empty batch, not a
   seventh state — a disposition of nothing has no record to name:

   | Set | States | Meaning |
   |---|---|---|
   | Unresolved | `QUEUED` (automatic recovery), `PARKED` (awaiting approval), `PUBLISHING` (submission in flight), `FAILED` (fail-closed) | work exists and must not be lost; blocks teardown-with-destruction and scratch reset |
   | Resolved | `RECOVERED` (published + routed), `ABANDONED` (operator explicitly accepted the loss) | nothing is at risk |

   An unverifiable or failed disposition preserves artifacts and must never
   collapse into generic `timed_out`, cleanup success, or scratch-reset
   eligibility. **`FAILED` is unresolved, not terminal** — the only routes out are a
   durable recovery or an explicit `ABANDONED` carrying an operator identity and
   reason. No timeout, sweep, or age heuristic resolves it.

   The publishable commit is the validation record's `head_sha` **exactly**. A
   worktree that has advanced past its validation is preserved and parked, never
   published: ancestry is not identity.

3. **One owner, three initiators, two commands.** Automatic recovery at the
   boundary, the gated tech-lead `recover_validated_work` op, and the operator
   Control Center command all call the same owner and receive the same typed result.
   The owner — not the initiators — owns trusted artifact admission, stale checks,
   idempotency, durable state, the publication workspace, and partial-write
   reconciliation, and it *composes* (rather than reimplements) the publisher and
   the finalizer of decision 8.

   The initiators split across **two command types, not one optional-laden
   request**: automatic capture carries the exact run assets, stored-evidence
   recovery carries an evidence id, and neither has an optional or sentinel field.
   A single union request would force the owner to decide what to do when the run
   assets are absent, and every available answer is a forbidden fallback — report
   "nothing found", scan for the latest run, or rediscover paths from a worktree.

   The exact assets reach the boundary through `IssueRunEvidenceSource`, a
   behavior-level port written by the owner that **allocates** the run (the launch
   transaction, in the same step that constructs `SessionRunAssets`) and read by
   the termination boundary, which holds only an issue number. Its result is an
   explicit fact about which runs were considered, and "no runs recorded" is a
   distinct type from "the ledger could not be read" — the latter raises and aborts
   teardown. A live session with no ledger row also raises: two owners disagreeing
   about what exists is never resolved in favour of destroying it.

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
   evidence* carries it. Neither hash covers capture timestamps, mutable external
   observations, the moving worktree head, or filesystem paths, so a crash-and-retry
   re-derives both and converges.

   Evidence is a **normalized relation**, not a column plus a JSON array: each
   `evidence_id` is a row related to its record with an explicit role — `current`,
   `attached` (admitted while a submission was in flight), or `superseded`. Exact
   lookup, approval-identity refusal, the post-submission drain of attached
   candidates, and retention are all queries against that relation. As a JSON array
   they were unqueryable: a superseded handle resolved to nothing, and an attached
   capture existed only as a directory on disk that no same-process drain could find.

   Because `record_id` includes the validated head, two validations on one branch
   are two records — so records on one issue+branch also carry a **lineage role**.
   A descendant validation becomes the single drainable head, ancestors park behind
   it and are resolved as recovered when the head publishes and their evidence still
   verifies, and genuinely divergent heads park for an explicit choice. Without that
   relation, whichever record published first left the other failing against a
   remote it no longer recognised — blocking reset over work that was already
   shipped.

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
   the nuclear reset. The owner is not a *parameter* of either boundary:
   both are methods on one immutable `IssueRuntimeLifecycleOwners` value that also
   carries the run-evidence source, so the freshness predicate and the teardown read
   the same owner set because they are two methods on one object — not because every
   call site remembered to pass the same arguments. A parameter list can always be
   satisfied with a different set; here there is nothing to pass and nothing to
   omit. Callers must also **consume** the returned disposition rather
   than discard it, so scratch reset can never discard work the predicate did not
   observe. Two mechanical consequences the contract makes explicit rather than
   leaving to the implementer:

   - The disposition owner needs a **narrower question**, because the record being
     drained is itself unresolved: consulting the full predicate would make the
     owner's own probe block its own drain on every pass, and automatic recovery
     would never publish anything. That narrowing is done by **scope, not by an
     argument** — a separate core bundle without the validated-work owner, behind
     its own port — because an exclusion parameter is a lever any future caller
     could use to stop looking at sessions, pairs, jobs, or publish retry. The full
     predicate keeps evaluating all five owners, always.
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
   | Remote execution | `ValidatedHeadExecutor` — exact-object/exact-lease branch write, then ensure exactly one PR, as two separately callable steps with **stage-specific result types** composed by one named composer, plus a separate constructor for the two claim-loss points that fabricates no branch result | no |
   | Fenced publication | `FencedValidatedHeadPublisher` — the disposition owner's claim re-checked before and between those steps | no |
   | Finalization | `PublishedWorkFinalizer` — staged review routing, then recovery-block clearing, composing the existing `RetryReviewRouting` policy | yes, on the request |
   | Admission | `PublishRecoveryService` (manual) and `ValidatedWorkDispositionService` (validated work), as **siblings** | yes |

   The executor is shared, so it must carry **no admission-specific authorization
   type**. Requiring the validated-work claim on its command while forbidding the
   manual owner to acquire one would have made the shared port unusable by one of
   its two allowed callers. Authority lives one layer up instead: the disposition
   owner wraps the executor in a fenced publisher that holds its claim, and the
   manual owner calls the executor under its existing locator/background-job
   authority. Neither forges the other's, and both end in the same composer — so
   the combined and two-step paths produce identical results by construction rather
   than by convention.

   The disposition owner reaches the *other* runtime owners the same way it reaches
   the remote: through one narrow seam. Without it the disposition service would
   have had to gather the session manager, pair registry, job supervisor and
   publish-retry owner itself — reaching into four siblings, one of which this ADR
   forbids it to depend on at all.

   Those bundles are the **call boundary**, not advice about what to pass:
   `terminate_issue_runtime()` and `has_active_issue_runtime()` become methods on
   the full lifecycle value rather than free functions with one parameter per
   owner, and both views evaluate the same single `core.probe()`. A callable
   surface that still accepts the pieces can always be handed a different set, and
   no bootstrap assertion can catch that; removing the pieces from the surface can.
   The run-evidence source rides the same bundle, because termination needs it at
   every call site and three of the four hold only an issue number.

   The seam is scoped by **construction, not by an argument**: an immutable
   `CoreIssueRuntimeOwners` bundle holds the four pre-existing owners;
   `OtherRuntimeActivity` wraps it and exposes one method with no exclusion
   parameter; the disposition service consumes that; and the full
   `IssueRuntimeLifecycleOwners` for teardown and reset is then built from the same
   core bundle plus the completed validated-work owner. That ordering is acyclic —
   a single value serving both would have to exist before itself — and the shared
   four owners are the same objects, so the two boundaries cannot drift. An
   exclusion parameter would have been a lever every future caller could pull to
   stop looking at sessions or pairs; a bundle that does not contain an owner cannot
   be asked to skip anything else.

   **Publication is fenced, synchronous, and its durability lives in numbered
   attempt rows.** A lease expiring proves only that time passed, never that the
   previous caller is dead, so an expired timestamp cannot by itself authorize a
   second attempt: a slow publisher can still return and record a late outcome over
   the winner's, and the Git ref lease bounds only the branch write, not PR
   creation, label routing, history mutation, or outcome recording. Correctness
   therefore rests on a **monotonic fence** per record, bumped by every claim and
   every takeover. Every durable write compare-and-sets on it (so a stale claim's
   write is discarded, not merged), the fence is re-checked immediately before each
   external mutation, and the two remote writes are each independently fenced by the
   remote itself — the exact ref lease for the push, and GitHub's one-open-PR-per
   (head, base) rule for the create. Ownership is **record-scoped and spans
   finalization**, not attempt-scoped: routing, history and the staged label
   sequence happen after the push outcome is durable and are exactly as
   single-owner as the push.

   The claim is a **capability, not a struct**: `acquire_claim()` mints a random
   secret and stores only its hash, so no reader of the row can construct a value
   the store's predicates accept, and every call additionally checks that the
   presenting process *is* the recorded owner. A fence bump alone could not do
   this — the bumped value is as readable as the old one.

   **A claim changes hands only when its owner stopped existing.** `holds_claim()`
   and the mutation it guards are not atomic, so a fence cannot by itself stop a
   former owner from performing one more effect — and while remote writes converge
   and label writes are idempotent, the staged finalizer also mutates process-local
   `OrchestratorState`, which no durable fence reaches. There are therefore exactly
   two paths to a new owner, and neither can overlap an in-flight effect: the owner
   **died**, or the owner **relinquished** at a stage boundary where nothing was in
   flight. There is deliberately no operation that takes a claim from a live
   process — an audit record does not create mutual exclusion, so the operator's
   remedy is to stop the owning engine (which makes death provable), not to seize
   the record beside it.

   Death must be proven from the **exclusion primitive**, which is the `flock`
   gate — not from `lock.json`, which `repo_lock.py` states is an advertisement and
   not the primitive. A typed `OrchestratorLivenessPort` answers it from the
   relationship between the recorded owner and the current process: a gate this
   process already holds (its own instance's, or the exclusive repo gate) was
   admitted by the kernel only because the previous holder released it, so the
   startup acquisition *is* the proof and that gate is never probed; a *different*
   named instance's gate is probed non-blockingly, which distinguishes gone from
   alive without disturbing it; a different host is never provable and therefore
   never taken over. Elapsed time authorizes nothing anywhere in this path, so
   there is no lease on the record at all.

   **The death proof is part of the safety argument, not a politeness
   optimization.** A fence cannot make a false-positive safe: deciding a live owner
   is dead is precisely what would let it apply a late, unfenced process-local
   history/routing effect beside its successor. The predicate therefore answers
   `False` whenever evidence is absent, and every `True` is backed by an exclusion
   the kernel already enforced.

   The publisher itself makes one call and returns what happened; there is no
   "submitted, poll later" state, because nothing in this design owns a job runner
   or a submission store to make such a state durable. What *is* durable is the
   attempt: the operation is identified by record and target commit and never
   changes, while each attempt at it is an append-only row written **before** the
   external call, under the owner's claim. A retry is therefore an explicit new
   attempt rather than a re-use of an idempotency key that already names a failed
   one, the retry budget survives restart by counting rows, and a crash mid-call
   leaves an outcome-less row that the next proven owner reconciles against the
   remote rather than resubmitting blind.

   **Finalization is staged and durable, and it routes before it clears.** The
   review-routing transition is applied and observed, and only then is the
   `recovery-pending` block removed. The reverse order — the manual retry path's
   order, which is correct for *its* job — has a crash window in which the block is
   gone and the routing, which lived only in process memory, is not: an issue
   scheduler-eligible again with a published head nobody will review. The phase is
   persisted, restart replays the missing stages idempotently, and `RECOVERED` is
   never inferred from labels looking correct, because cleanup labels do not prove
   the routing mutation happened.

9. **The wedged-owner escape needs two owners, and one of them does not exist yet.**
   Because a claim is never taken from a live owner, an owner that wedges is
   resolved by stopping its engine — which must therefore be reachable. There is no
   Repository Engine lifecycle owner for *stopping* today: `ControlCenterActions`
   owns pause/resume/refresh and friends but no stop command, and the stop routes
   call `SupervisorOps` directly, one of them with the default `instance_id=None`,
   which cannot target the named instance holding a claim. So this ADR defines
   `RepositoryEngineLifecycle` over `SupervisorOps` as a **new boundary with one new
   caller**, owning exactly one behaviour: the targeted stop of one named or
   single-instance engine. It does **not** migrate the existing stop surfaces, and
   an earlier draft claiming it would was a scoping error — those routes also carry
   bulk, force, graceful-timeout, port-fallback and shutdown-operation semantics,
   and the tree has stop consumers in MCP, CLI, repository removal, restart and
   reconciliation. They are pre-existing and untouched here, so nothing about them
   is being deferred; the guardrail is scoped to the surfaces this design
   introduces.

   **Its stop policy is stated, not inherited.** `SupervisorOps.stop()` defaults
   `force_if_graceful_fails=True` and a graceful timeout, so a call that named
   neither would silently own "graceful, then SIGKILL after an unstated timeout"
   while the prose claimed force was out of scope. The command carries both
   explicitly, and force-on-timeout is deliberately enabled: the case being solved
   is an engine that has stopped responding, so a graceful-only stop would leave the
   only recovery path a no-op. Forcing is safe on this contract's terms — the
   record, evidence and escrow are durable, the fence refuses the dying process's
   late writes, and a killed process has its gate released by the kernel, which is
   what makes death provable.

   The recovery-specific guard is a *separate*, smaller owner. A plain
   `StopEngineCommand` carries no record binding — engine lifecycle has no business
   knowing about validated work — so "stop this engine only if it still owns this
   exact record" becomes a bounded coordinator that re-reads the owner by exact
   `record_id`, refuses on any mismatch or a non-local target with zero effect, and
   delegates a plain stop only on an exact match.

   **Both live in the Control Center, because the Repository Engine is the thing
   that is stuck.** Hosting the coordinator or its endpoint in the engine that owns
   the claim would make the escape hatch unavailable in exactly the failure it
   exists for. The Control Center composes the write half over `SupervisorOps` and
   the read half over an out-of-process reader that opens the target repository's
   own `validated_work.sqlite` read-only — which is why that state is a file in the
   repository's state directory rather than engine memory. Neither half requires the
   target engine to be responsive. The issue detail, served by that engine, only
   reports the owner and links to the Control Center.

   The disposition owner stays read-only with respect to engine lifecycle: it
   publishes the claim holder as a fact, with **stop availability computed by the
   owner and carried as data** rather than by a read-model property consulting
   process-global host state. An issue-detail handler must never reach into
   supervisor state or build a stop call from persisted PID fields, and an engine on
   another host is presented as unavailable rather than offered.

10. **Publication resolution is one atomic store command per verified route.**
   Marking a record recovered, advancing the forward-only published-lineage fact
   with its provenance, resolving verified contained ancestors, and classifying
   waiting successors are four effects of one decision, and splitting them across
   port calls put the invariant in the caller's memory: resolve-then-advance with a
   crash in between recreates the arrival-order stranding the fact exists to
   prevent. Both routes — our own push, and an observed merged PR — are therefore
   single store-owned commands returning a typed resolution, with no transaction
   handle crossing the port, and the observed-merge route refuses outright while a
   live claim owns the record.

11. **An approved operation binds the facts it was approved against.** The evidence
   id binds the immutable half — repo, issue, branch, validated head, run identity,
   artifact hashes — and deliberately excludes the mutable observations, which is
   what makes it crash-stable. That exclusion means the id alone cannot authorize:
   the PR and the expected remote baseline are refreshed under the same id while a
   record is unresolved, so an operation approved against one PR and one remote
   state could execute against another with the immutable approval unchanged.
   Revalidation cannot close this — it proves the current facts are internally safe,
   not that they are the facts a human authorized. The immutable
   `StoredTechLeadOp` (and the operator command) therefore carries a
   `ValidatedWorkAuthoritySnapshot`, checked for exact equality against the durable
   record before anything else runs; a changed observation stale-downgrades the
   approval with zero writes and asks for a fresh one. The snapshot is
   authorization input, never a competing source of truth: every stale check still
   runs against freshly read state after it matches.

12. **This owner writes exactly one label of its own.** A failed disposition keeps
   `recovery-pending` and registers its escalation through the needs-human owner's
   behavior API as its own enumerated cause. It does not write
   `tech-lead-needs-human`, which ADR-0013 defines as provenance for a *different*
   lifecycle: a marker written here would be read on restart as an interrupted
   tech-lead escalation and re-blocked as one, and — because it would be added after
   the recovery observed its blocking labels — would survive a successful recovery
   and reassert `needs-human` over work that was already published. Ownership of a
   label includes ownership of its removal.

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
- Two new registered SQLite stores — the validated-work store and the issue run
  ledger — and an escrow directory join the orchestrator-owned
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
