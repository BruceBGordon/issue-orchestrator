# Validated-work disposition contract

**Design review for #7024.** Implementation slice: #6914. Decision record:
[ADR-0035](../architecture/ADR/0035-validated-work-disposition.md).

This document is the implementable owner contract the design review is required to
produce. It names the owning port/command/result types, composition-root wiring,
durable store schema and recovery semantics, authority/config/schema changes,
public UI/event contracts, label transitions, the artifact-retention boundary, and
the producer-to-command plus command-to-handler test surface. It closes with the
Porchpin #5 proof walkthrough including crash points around every non-atomic
GitHub write.

---

## 1. The invariant

> `terminate_issue_runtime()` and every abnormal review-exchange/completion terminal
> path must be unable to finish teardown until the disposition of any
> completed-and-validated work is durably recorded.

"Completed and validated" means, and only means, orchestrator-owned evidence:

- a completion record the orchestrator itself preserved (the run-scoped copy under
  `SessionRunAssets.run_dir`, or a record referenced by the run manifest) that
  parses as `CompletionOutcome.COMPLETED` and requests `PUSH_BRANCH`/`CREATE_PR`;
- a validation record that record points at, with `passed=true`, produced at a
  commit that is an ancestor-or-equal of the current worktree HEAD;
- a resolvable commit sha for that HEAD.

Agent prose, GitHub issue bodies, comments, the agent-writable tech-lead assignment
file, and agent-chosen filesystem paths are never authority (ADR-0016, ADR-0031's
trust boundary). Section 5 defines admission precisely.

### 1.1 Why the existing owner, extended

`control/review_exchange_lifecycle.py` already owns issue terminal boundaries and
already maintains the hard-won symmetry between `terminate_issue_runtime()` and
`has_active_issue_runtime()` — the teardown and the freshness predicate read the
*same* owner set through one contract, which is what stops a stale proposal from
tearing down live work it never observed. A sibling disposition owner would break
that symmetry: callers would have to remember to coordinate two boundaries, and the
activity predicate, teardown, and disposition would drift apart exactly the way the
case files describe. The disposition owner is therefore **composed inside** the
existing owner, and it becomes a fifth probe in the activity predicate.

---

## 2. Types

### 2.1 Domain — `domain/validated_work.py` (new)

```python
class ValidatedWorkState(StrEnum):
    NONE      = "none"       # terminal: no completed+validated work at this edge
    QUEUED    = "queued"     # recovery admitted automatically; drain will execute
    PARKED    = "parked"     # durable, awaiting approval (gated op or operator)
    PUBLISHING = "publishing" # admitted to the publish executor; submission in flight
    RECOVERED = "recovered"  # published + review routed; terminal
    FAILED    = "failed"     # fail-closed; artifacts preserved; operator-terminal


class ValidatedWorkFailure(StrEnum):
    """Precise, enumerable reasons. Never a free-text-only failure."""
    ESCROW_WRITE_FAILED       = "escrow_write_failed"
    ARTIFACT_MISSING          = "artifact_missing"
    ARTIFACT_HASH_MISMATCH    = "artifact_hash_mismatch"
    ARTIFACT_UNTRUSTED_PATH   = "artifact_untrusted_path"
    VALIDATION_SHA_MISMATCH   = "validation_sha_mismatch"
    WORKSPACE_INTEGRITY       = "workspace_integrity"      # #7017 detached HEAD etc.
    REF_PIN_LOST              = "ref_pin_lost"
    REMOTE_DIVERGED           = "remote_diverged"          # not a fast-forward
    REMOTE_HEAD_CHANGED       = "remote_head_changed"
    PR_CLOSED_OR_MERGED       = "pr_closed_or_merged"
    PR_BRANCH_MISMATCH        = "pr_branch_mismatch"
    ISSUE_UNREADABLE          = "issue_unreadable"
    RUNTIME_ACTIVE            = "runtime_active"
    PUSH_FAILED               = "push_failed"
    REVIEW_ROUTING_FAILED     = "review_routing_failed"


class ReviewDisposition(StrEnum):
    ROUTE_TO_PR_REVIEW  = "route_to_pr_review"   # normal review discovery on new head
    RESUME_REVIEW       = "resume_review"        # PR already under review; update head
    EXCHANGE_APPROVED   = "exchange_approved"    # exchange reached OK/REVIEWER_OK


@dataclass(frozen=True, slots=True)
class EscrowedArtifactRef:
    """One admitted artifact, addressed relative to the escrow root."""
    relative_path: str      # never absolute, never '..'; validated by path_guards
    sha256: str
    byte_size: int
    captured_at: str        # ISO-8601 UTC


@dataclass(frozen=True, slots=True)
class ValidatedWorkEvidence:
    """The orchestrator-owned facts a recovery is allowed to act on."""
    repo_slug: str                       # target repository identity
    issue_number: int
    branch_name: str
    validated_head_sha: str              # commit validation passed at
    expected_remote_head_sha: str | None # remote branch head at capture; None = absent
    pr_number: int | None
    run_identity: SessionRunIdentity     # session_name, run_id, started_at
    completion_artifact: EscrowedArtifactRef
    validation_artifact: EscrowedArtifactRef
    exchange_summary_artifact: EscrowedArtifactRef | None
    requested_actions: tuple[RequestedAction, ...]
    review_disposition: ReviewDisposition
    exchange_terminal: ReviewExchangeTerminalState | None  # e.g. STOPPED/MAX_ROUNDS
    observed_blocking_labels: tuple[str, ...]  # exactly what this op may later clear
    branch_binding_verified: bool        # False when HEAD was detached (#7017)

    @property
    def evidence_id(self) -> str:
        """Content hash over every field above. The dedup key."""
```

`evidence_id` being a pure content hash is what makes the whole contract
idempotent: the same abnormal edge re-derives the same id, so a crash-and-retry
converges on the existing record instead of minting a second one.

### 2.2 Command / result

```python
@dataclass(frozen=True, slots=True)
class ValidatedWorkDispositionRequest:
    """Typed command. Built by the initiator; policy lives in the owner."""
    issue_number: int
    reason: str                       # the terminate_issue_runtime reason string
    initiator: DispositionInitiator    # AUTOMATIC | TECH_LEAD | OPERATOR
    worktree_path: Path | None        # None once the worktree is already gone
    run_assets: SessionRunAssets | None
    evidence_id: str = ""             # required for TECH_LEAD/OPERATOR, empty for AUTOMATIC


@dataclass(frozen=True, slots=True)
class ValidatedWorkDisposition:
    """The typed result. Reported on IssueRuntimeTermination and to the UI."""
    state: ValidatedWorkState
    reason: str
    evidence_id: str | None = None
    failure: ValidatedWorkFailure | None = None
    pr_number: int | None = None
    published_head_sha: str | None = None

    def __post_init__(self) -> None:
        # fail-closed shape rules
        if self.state is ValidatedWorkState.FAILED and self.failure is None:
            raise ValueError("FAILED disposition requires an enumerated failure")
        if self.state is not ValidatedWorkState.NONE and not self.evidence_id:
            raise ValueError("non-NONE disposition requires an evidence_id")
```

### 2.3 Ports

`ports/validated_work_disposition.py` (new) — the behavior-level owner:

```python
class ValidatedWorkDispositionOwner(Protocol):
    def dispose_at_termination(
        self, request: ValidatedWorkDispositionRequest
    ) -> ValidatedWorkDisposition: ...
    """Automatic initiator. Called by terminate_issue_runtime BEFORE teardown."""

    def recover(
        self, request: ValidatedWorkDispositionRequest
    ) -> ValidatedWorkDisposition: ...
    """Explicit initiator: approved tech-lead op or operator command."""

    def drain(self) -> tuple[ValidatedWorkDisposition, ...]: ...
    """Tick-driven progression of durable records; restart reconciliation."""

    def has_pending_disposition(self, issue_number: int) -> bool: ...
    """Activity probe for has_active_issue_runtime (fifth owner)."""

    def snapshot(self, issue_number: int) -> ValidatedWorkSnapshot | None: ...
    """Read model for the UI/view-model layer. Never re-derives policy."""
```

`ports/validated_work_store.py` (new) — durable state, section 4.

### 2.4 Extended existing type

```python
@dataclass(frozen=True)
class IssueRuntimeTermination:
    issue_number: int
    review_exchange: ReviewExchangeCancellation
    stopped_session_ids: tuple[str, ...]
    cleared_active_session_ids: tuple[str, ...]
    validated_work: ValidatedWorkDisposition   # NEW — required, never defaulted
```

Making the field required (no default) is deliberate: every construction site must
produce a disposition, so a new terminal path cannot be added that silently omits it.

---

## 3. Composition and control flow

### 3.1 Inside `terminate_issue_runtime()`

The order is load-bearing. Disposition must observe the pair, the worktree, and the
run directory *before* any of them is released.

```
1. disposition = validated_work.dispose_at_termination(request)   # may raise -> teardown aborts
2. cancel_issue_review_exchange(...)      # pair release + supervised job cancel
3. publish_recovery.abandon_issue(...)    # unchanged
4. stop issue-N / rework-N terminals; clear stale active-session records
5. return IssueRuntimeTermination(..., validated_work=disposition)
```

`validated_work` becomes a **required keyword parameter** of
`terminate_issue_runtime()` — not `| None = None`. There are four production call
sites (`infra/orchestrator.py`, `control/action_applier.py`,
`entrypoints/web_retry_history_routes.py`, and the tech-lead kill wiring via
`Orchestrator.terminate_issue_runtime_for_issue`); each must pass the owner. A
required parameter is the mechanical guardrail (ADR-0012) that stops a future
terminal path from opting out.

Step 1 raising is the invariant's teeth: if evidence exists but escrow cannot be
written, the boundary refuses to tear down. The caller surfaces a hard failure and
the work stays exactly where it is.

### 3.2 `has_active_issue_runtime()` — fifth probe

```python
probes = (
    ...,                                                    # sessions
    ...,                                                    # pair registry
    ...,                                                    # supervised jobs
    lambda: publish_recovery is not None and publish_recovery.has_active_retry(n),
    lambda: validated_work is not None and validated_work.has_pending_disposition(n),  # NEW
)
```

The existing fail-safe wrapper (`_owner_active_or_unverifiable`) applies: a probe
that raises counts as active. Consequence: `has_active_reset_retry_runtime()` in
`web_retry_history_routes.py` — which the tech-lead reset executor and the dashboard
reset both consult — now stale-downgrades any scratch reset while a non-terminal
disposition record exists. **That is the concrete mechanism by which the stuck
sweep can no longer discard validated work.**

### 3.3 Consumers that stop losing the work

| Seam today | Change |
|---|---|
| `completion_review_exchange.py` — any non-ok outcome is a bare halt | Build a disposition request from the exchange terminal state. `STOPPED/MAX_ROUNDS_EXCEEDED` and `STOPPED/REVIEWER_REPORTS_NO_PROGRESS` carry `ReviewDisposition.ROUTE_TO_PR_REVIEW`; `ERROR/*` carries it too when completion+validation are conclusive. Publish-or-park, never discard (#7018 is this row). |
| `domain/completion_finalization.py` — matrix sees runtime facts only | `CompletionFinalizationCommand` gains `validated_work_present: bool`. `TERMINAL_REVIEW_EXCHANGE_TIMEOUT` may only be returned once disposition is recorded; the matrix stays pure — the caller gathers the fact. |
| `session_controller.py` / `session_completion.py` / `completion_action_planner.py` — classify to `TIMED_OUT`/`FAILED` | Consult `IssueRuntimeTermination.validated_work`. When it is not `NONE`, the recorded session outcome and the emitted event carry the disposition state; generic `timed_out` is no longer a legal classification for an issue with `QUEUED`/`PARKED`/`FAILED` work. |
| `stuck_sweep.py` — "`failure_reason` is always `timed_out`" | Reads the disposition snapshot; a stranded-with-disposition issue is reported as owned recovery, not as an undiagnosed timeout, and is excluded from scratch-reset proposals. |
| Workspace freshness / detached HEAD (#7017) | A `WorkspaceIntegrity` precondition is evaluated **at exchange start** (don't run rounds on a broken workspace) and **at evidence capture**. At capture, a detached HEAD does not invalidate the commits — the resolved sha is pinned — but sets `branch_binding_verified=False`, which forces `PARKED` instead of `QUEUED`. |

### 3.4 Composition root

`entrypoints/bootstrap.py` constructs, in order: `SqliteValidatedWorkStore(state_dir/…)`
→ `FilesystemValidatedWorkEscrow(state_dir/"validated-work")` → `ValidatedWorkDispositionService`
(injected with the store, escrow, `WorkingCopy`, `WorktreeManager`, `RepositoryHost`,
`PublishRetryLocatorStore`, `PublishRecoveryService`, `ActionApplier`, `LabelManager`,
`EventSink`). It is exposed on `control/orchestrator_deps.py` as
`validated_work: ValidatedWorkDispositionOwner`, mirroring how `publish_recovery` is
carried today. The store is registered in `infra/sqlite_registry.py` so doctor
checks, backups, and startup maintenance cover it (the precedent set by
`tech_lead_authority.sqlite`).

`drain()` is called from the same tick drain point that already calls
`PublishRecoveryService.drain_completed_retries()`, so restart reconciliation needs
no new scheduler.

---

## 4. Durable state

### 4.1 Store schema — `validated_work.sqlite` in `<repo>/.issue-orchestrator/state/`

```sql
CREATE TABLE IF NOT EXISTS validated_work_records (
    evidence_id           TEXT PRIMARY KEY,
    repo_slug             TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    branch_name           TEXT NOT NULL,
    validated_head_sha    TEXT NOT NULL,
    expected_remote_head  TEXT NOT NULL DEFAULT '',   -- '' = no remote branch expected
    pr_number             INTEGER,
    state                 TEXT NOT NULL,              -- ValidatedWorkState
    failure               TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure
    reason                TEXT NOT NULL DEFAULT '',
    evidence              TEXT NOT NULL,              -- ValidatedWorkEvidence JSON
    escrow_dir            TEXT NOT NULL,              -- relative to escrow root
    pinned_ref            TEXT NOT NULL,
    submission_token      TEXT NOT NULL DEFAULT '',   -- set while PUBLISHING
    published_head_sha    TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    terminal_at           TEXT NOT NULL DEFAULT ''
);

-- Dedup: at most one non-terminal record per target+branch+validated head.
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_open
    ON validated_work_records (repo_slug, issue_number, branch_name, validated_head_sha)
    WHERE state NOT IN ('recovered', 'failed', 'none');

CREATE INDEX IF NOT EXISTS ix_validated_work_issue
    ON validated_work_records (issue_number, state);
```

Records are **append-then-transition**, never deleted by normal operation. The
unique partial index is the deduplication rule from #6914 expressed as a database
constraint rather than as caller discipline: *target repository + issue + branch +
validated HEAD*, with `evidence_id` (the content hash) as the primary key covering
the evidence-identity half.

### 4.2 State machine

```
                      ┌──────────────── (no evidence) ───────────────┐
                      ▼                                              │
  termination ──► admission ──► NONE                                 │
        │                                                            │
        ├── evidence conclusive + branch binding verified ──► QUEUED ─┤
        │                                                            │
        └── evidence incomplete / stale / unverifiable ────► PARKED   │
                                                              │      │
                                approval (tech-lead op or ────┘      │
                                 operator command)                    │
                                          ▼                           │
   QUEUED ──drain: stale checks pass──► PUBLISHING ──publish ok──► RECOVERED
      │                                     │
      └── stale checks fail ──► FAILED ◄────┘  (any step, fail-closed)
```

Legal transitions only; every other pair raises. `FAILED` and `PARKED` are durable
resting states — artifacts are retained, the issue stays blocked, and the operator
or an approved tech-lead op is the only way out. `FAILED → PARKED` is permitted when
an operator explicitly re-submits after fixing the external condition (e.g. reopening
a closed PR); the re-submission runs every stale check again.

### 4.3 Stale checks, revalidated immediately before every mutation

Executed by the owner in `drain()`/`recover()`, never by an initiator:

1. `repo_slug` matches the running orchestrator's repository identity.
2. Issue is readable and open; the recorded `observed_blocking_labels` are re-read
   fresh ("could not read" is not "not blocked" — the rule
   `publish_retry_admission.board_block_reason()` already encodes).
3. Every escrowed artifact exists and its sha256 matches.
4. `pinned_ref` resolves and equals `validated_head_sha`.
5. Remote branch head equals `expected_remote_head_sha` (or the branch is absent
   when `''` was recorded).
6. `validated_head_sha` is a **descendant** of the current remote head
   (`merge-base --is-ancestor`). Not a descendant ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`.
   **Fast-forward only. Never force-push.**
7. When `pr_number` is set: PR is open, its head ref is `branch_name`, and its head
   sha equals `expected_remote_head_sha`.
8. `has_active_issue_runtime()` (excluding this owner's own probe) is false.
9. No other non-terminal record shares the dedup key.

### 4.4 Publish-retry composition boundary

This is the single named boundary the design review is required to specify.

`PublishRecoveryService` remains the **publication executor** and gains no admission
policy. The disposition owner is the **only** component permitted to synthesise
`PublishRetryLocators` for a failure that never reached the publish stage. Handoff:

1. If the original worktree is gone, the owner **rehydrates** it: create a worktree
   at the canonical issue path from `pinned_ref`, then restore the escrowed
   completion record and validation record into the run directory. This preserves
   `locator_block_reason()` unchanged — the owner satisfies its preconditions rather
   than weakening them.
2. The owner writes `PublishRetryLocators` (worktree, branch, completion path, run
   assets, `pr_number`, and the `skip_review`/`review_exchange_completed`/
   `review_exchange_halted` routing flags derived from `ReviewDisposition`) through
   `PublishRetryLocatorStore`.
3. The owner applies the admission label transition (§7) so the board precondition
   `board_block_reason()` enforces — the `publish-failed` label — is genuinely true
   before submission. A max-rounds strand carries `blocked-failed`, not
   `publish-failed`; the owner records the observed label set first so §7's targeted
   clear can restore it exactly.
4. The owner calls `PublishRecoveryService.retry_publish(...)` and stores the
   returned submission token, transitioning to `PUBLISHING`.
5. Success reconciliation reuses `RetrySuccessFinalizer` + `RetryReviewRouting`
   unchanged, then the owner marks `RECOVERED`.

No other caller may reconstruct locators. This is enforced mechanically (§9).

---

## 5. Trusted artifact admission

Admission answers: *which bytes on disk may become executable authority?*

**Admissible sources, in this order:**

1. The run-scoped durable completion copy at
   `SessionRunAssets.completion_record_copy.path` (orchestrator-written).
2. A completion record referenced by the orchestrator-owned run manifest
   (`SessionRunAssets.manifest`), whose resolved path is contained by `run_dir`.

**Inadmissible, always:** any path the agent chose that is not one of the above; any
path outside the run directory; symlinks escaping the run directory; the
agent-writable tech-lead assignment file; issue bodies; comments; agent prose.

**Selection rule when several admissible records exist** (Porchpin #124's
canonical-vs-sidecar problem): choose the **latest admissible record that parses,
whose `outcome` is `COMPLETED`, and whose `validation_record_path` resolves inside
the run directory to a validation record with `passed=true` at a commit that is an
ancestor-or-equal of the current HEAD.** Recency alone never selects; validity
gates it. An unparseable canonical artifact is simply not a candidate — it is not
"the" record that wins by being canonical.

**Containment** is enforced with the existing `domain/path_guards.py`
(`require_absolute_path`, `require_path_under`) plus symlink resolution before the
containment test. A violation is `ARTIFACT_UNTRUSTED_PATH` ⇒ `FAILED`, never a
silent skip.

**Hashing**: every admitted artifact is sha256-hashed at capture and re-hashed at
execution. A mismatch means the worktree was mutated after capture ⇒
`ARTIFACT_HASH_MISMATCH` ⇒ `FAILED`.

---

## 6. Artifact-retention boundary

Today `SessionRunAssets` requires `run_dir` to live **under** `worktree_path`, so
worktree removal deletes the completion record, the validation record, the exchange
summary, and the terminal recording together. `reset_issue(from_scratch=True)`
removes the worktree with `force=True`, and the branch commits exist only in the
worktree's checkout lineage. That single fact is why Porchpin #126 ("recovery
artifacts deleted with the tech-lead worktree") and #6914's three rotting branches
are the same bug.

The contract moves the retention boundary off the worktree:

| Asset | Location | Deleted by |
|---|---|---|
| Escrowed completion/validation/exchange-summary copies | `<state_dir>/validated-work/<issue>/<evidence_id>/` | escrow retention sweep only |
| Validated commits | pinned by `refs/issue-orchestrator/validated/<issue>/<evidence_id>` in the shared object store | ref deletion on `RECOVERED` + retention window |
| Live run directory | inside the worktree (unchanged) | worktree removal (now non-fatal) |

Rules:

- Escrow writes go to a sibling temp directory and are moved into place with an
  atomic rename, so a partially written escrow is never admissible.
- A git ref in the common `.git` directory survives `git worktree remove`, which is
  what makes the commits durable without copying a bundle. Pinning also protects
  the objects from `gc`.
- `_delete_issue_branches()` in `control/maintenance.py` must skip
  `refs/issue-orchestrator/validated/*`, and `reset_issue(from_scratch=True)` is
  already blocked upstream by the §3.2 activity probe.
- `session_output_retention_days` cleanup must not traverse the escrow root.
- New setting `validated_work.escrow_retention_days` (default `30`): escrow and the
  pinned ref are released only after a record has been `RECOVERED` for that long.
  `PARKED` and `FAILED` records are retained indefinitely — the whole point is that
  unresolved work is never garbage.

---

## 7. Label transitions

Applied **only after their corresponding effect succeeds** (ADR-0013: labels are
crash-safe truth, so a label must never claim an effect that did not happen).

| Transition point | Effect that must succeed first | Labels |
|---|---|---|
| Evidence recorded (`QUEUED`/`PARKED`) | durable record + escrow committed | add `recovery-pending` (new, `LabelCategory.BLOCKING`). Existing blocking label is **kept** — the issue is not unblocked by being owned. |
| Admission to publish (`PUBLISHING`) | locators stored | add `publish-failed` if absent (the executor's board precondition); the pre-existing set is already recorded in `observed_blocking_labels`. |
| Publication + review routing succeeded (`RECOVERED`) | remote head == `validated_head_sha`, PR open, review routed | remove `recovery-pending`; remove **only** the labels in `observed_blocking_labels` plus the `publish-failed` this op added. Then normal routing applies `pr-pending`/review labels via `RetrySuccessFinalizer`. |
| Disposition failed (`FAILED`) | — | keep `recovery-pending`; add `tech-lead-needs-human`. Never leave the issue in a plain `blocked-failed` state, never scratch-eligible. |

The targeted clear is important: a human who adds `blocked-needs-human` *after*
admission must not have it wiped by a recovery that never saw it. Only the observed
set is cleared.

`recovery-pending` is added to `control/label_manager.py`'s `LabelEntry` table as a
blocking-category label, which automatically makes it (a) excluded from agent pickup
by the scheduler's blocking classification and (b) rejected as an agent-proposed
workflow label.

---

## 8. Authority, config, events, and UI contracts

### 8.1 Tech-lead authority — extend, do not fork

- `domain/tech_lead_artifacts.py`: add `"recover_validated_work"` to the action
  vocabulary and to `ACT_LEVEL_TECH_LEAD_ACTIONS`; add it to
  `UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS` until the executor is wired, so config
  `execute` is a startup error rather than a silent no-op (the rule
  `TechLeadAuthorityConfig.startup_errors()` already enforces for
  `kill_hung_session`).
- `domain/tech_lead_session.py`: `StoredTechLeadOp` gains **one** field,
  `target_evidence_id: str = ""`, with the mirror of the existing
  `target_session_id` rule — **required non-empty for `recover_validated_work`,
  required empty for every other op type**. The op binds the evidence *identity*;
  the disposition store holds the facts (repo, issue, PR, branch, expected remote
  head, validated local head, artifact identities). Two sources of truth are thereby
  avoided, and execution revalidates all of §4.3 regardless.
- Storage: `tech_lead_proposal_ops.op` is already a JSON blob, so this is a
  `to_dict`/`from_dict` extension with **no DDL change**. The immutable create-once
  ledger, the proposal-approval scanner (`ApprovedTechLeadOp` classification from the
  same open-issue scan), execute-once dispatch, stale downgrade, and terminal-ledger
  cleanup are all reused as-is.
- Executor wiring goes in `entrypoints/tech_lead_reset_retry_wiring.py` alongside the
  kill and reset executors — the established home for closures that need the live
  orchestrator — and calls `deps.validated_work.recover(...)`. It does **not**
  reimplement any part of the owner.

### 8.2 Config / settings schema

- `infra/config_models_tech_lead.py`: `TechLeadAuthorityConfig.recover_validated_work`
  defaults to `"propose"`; add the key to `TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS`.
- `infra/settings_schema.py`: `tech_lead_authority_recover_validated_work` and
  `validated_work_escrow_retention_days`, which regenerate
  `docs/user/configuration_reference.md`; mirror both in
  `examples/config.example.yaml`. Drift is caught by
  `tests/unit/test_settings_schema.py`. (Follow the `configuration` skill's file
  checklist — all config-touching files move together.)

### 8.3 Events — `events/catalog.py`

New `validated_work.*` domain:

| EventName | Payload highlights |
|---|---|
| `VALIDATED_WORK_DETECTED` | `issue_number`, `evidence_id`, `validated_head_sha`, `exchange_terminal` |
| `VALIDATED_WORK_QUEUED` | `evidence_id`, `branch_name`, `pr_number` |
| `VALIDATED_WORK_PARKED` | `evidence_id`, `failure` (why approval is required) |
| `VALIDATED_WORK_RECOVERED` | `evidence_id`, `published_head_sha`, `pr_number` |
| `VALIDATED_WORK_DISPOSITION_FAILED` | `evidence_id`, `failure`, `reason` |

The UI-visible subset is added to the public timeline event enum in the same module,
per the `schema-updates` skill.

### 8.4 Public contract + Control Center command

- `contracts/public.py`: the issue-detail view model gains a `validated_work` block
  — `state`, `evidence_id`, `validated_head_sha`, `pr_number`, `failure`,
  `escrow_retained`, and the available operator action. Regenerate
  `contracts/public/*.json` with `scripts/generate_public_contracts.py`; drift is
  enforced by `tests/unit/test_public_contract_schemas.py`.
- Availability comes from `ValidatedWorkDispositionOwner.snapshot()`, not from the
  route re-deriving policy — the same shape as the existing
  `can_retry_publish()`-gated `retry_publish` action in
  `entrypoints/web_issue_detail_routes.py`.
- New endpoint `POST /api/issues/{issue_number}/recover-validated-work` in
  `entrypoints/web_retry_history_routes.py` (beside `retry-publish`), delegating to
  `orchestrator.deps.validated_work.recover(...)` and returning the typed result.
  Register the endpoint and payloads in the UI OpenAPI contract per the
  `ui-openapi` skill.
- Accessibility for the new action button: native `<button>`, keyboard reachable,
  visible focus ring, accessible name that includes the issue number, and a
  non-colour status signal (text + icon) for `PARKED`/`FAILED` in both themes. The
  error toast for a `FAILED` disposition must not auto-dismiss.

---

## 9. Test surface

**Producer → command** (the fact-gathering half):

- Each abnormal edge builds the correct `ValidatedWorkDispositionRequest`:
  exchange `STOPPED/MAX_ROUNDS_EXCEEDED`, `STOPPED/REVIEWER_REPORTS_NO_PROGRESS`,
  every `ERROR/*` reason, outer session timeout, orchestrator shutdown,
  hold/retry/cancel races (#6960), respawn-during-cleanup (#6986), detached HEAD
  (#7017).
- `terminate_issue_runtime()` calls disposition **before** pair release, job cancel,
  and session stop — asserted by call ordering against a recording fake.
- Escrow failure raises and **no** teardown occurs.

**Command → handler** (the consumption half):

- Every `ValidatedWorkState` renders the correct issue-detail view model and the
  correct operator action availability.
- The tech-lead board renders a `recover_validated_work` gated op; removing
  `proposed-tech-lead` and the Control Center command reach the same owner and
  produce the same typed result.
- `session_controller`/`session_completion` classification: an issue with a non-`NONE`
  disposition is never recorded as generic `timed_out`.

**Owner behaviour**: one test per stale check in §4.3; one per crash point in §10;
dedup (repeat request converges, never double-publishes); restart drain; escrow
atomicity; fast-forward-only refusal on divergence.

**Non-regression**: Retry Publish, `request_rework` (#7008), reset-from-scratch, and
kill-session behaviour unchanged; scratch reset now stale-downgrades while a
disposition is pending.

**Mechanical guardrails** (ADR-0012), added to the existing AST guardrail suite:

- No module outside the disposition owner constructs `PublishRetryLocators`.
- No module outside the owner adds/removes `recovery-pending`.
- `IssueRuntimeTermination` cannot be constructed without `validated_work`
  (enforced by the type, verified by a test).

---

## 10. Proof: the Porchpin #5 sequence

Required walkthrough. Sequence: invalid canonical completion artifact → corrected
valid side artifact → validation at a newer local HEAD → old remote PR head →
failed ingestion → repeated tech-lead diagnosis → no executable recovery action.

**Setup.** Coding session for issue *N* on branch `b`. PR *P* exists with head `R`.
The coder's first `coding-done` writes a completion record that fails schema
validation. A second `coding-done` writes a valid record; the orchestrator preserves
it as the run-scoped durable copy. `make validate` passes at local HEAD `L`, three
commits ahead of `R`.

**Today.** Ingestion consumes the invalid record, the session is classified failed,
the issue gets `blocked-failed`, no publish locators are ever written (publish never
ran), the tech lead re-diagnoses the same issue every sweep, and the only available
remedy — scratch reset — would delete `L`.

**Under the contract:**

| Step | Behaviour |
|---|---|
| Admission | Both records are run-scoped and therefore admissible *sources*. Selection (§5) rejects the invalid one because it does not parse, and selects the valid one because its `validation_record_path` resolves in-run to `passed=true` at a commit ancestor-or-equal of `L`. The canonical path holds no privileged status. |
| Evidence | `validated_head_sha=L`, `expected_remote_head_sha=R`, `pr_number=P`, `review_disposition=RESUME_REVIEW`, `observed_blocking_labels=("blocked-failed",)`, `branch_binding_verified=True`. |
| Escrow | Completion + validation records copied to `<state_dir>/validated-work/N/<evidence_id>/` (temp dir + atomic rename); `L` pinned at `refs/issue-orchestrator/validated/N/<evidence_id>`. |
| Disposition | Evidence conclusive ⇒ `QUEUED`. `recovery-pending` added; `blocked-failed` kept. `IssueRuntimeTermination.validated_work.state == QUEUED`, so the session is **not** classified `timed_out`. |
| Drain | Stale checks pass; `L` is a descendant of `R` ⇒ fast-forward legal. Locators reconstructed (worktree rehydrated from the pinned ref if it is gone), `publish-failed` added, `retry_publish` submitted, state `PUBLISHING`. |
| Publish | Fast-forward push `b → L`. PR *P*'s head becomes `L`. No new PR, no supersede, no force-push, no reset. |
| Review | `RetryReviewRouting` routes the new head through normal review discovery — review resumes on `L`. No approval label is applied, so there is no false ready-to-merge state. |
| Finalize | `recovery-pending`, `blocked-failed`, and the op's own `publish-failed` removed; `pr-pending` applied by the normal finalizer; record `RECOVERED`; escrow + ref retained for `escrow_retention_days`. |
| Divergence variant | If `L` were **not** a descendant of `R`, step 6 of §4.3 fails ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`, artifacts preserved, `tech-lead-needs-human` added. A human resolves the divergence; nothing is force-pushed and nothing is deleted. |

**Crash points around the non-atomic GitHub writes:**

| Crash after | On restart, `drain()` does |
|---|---|
| Nothing (before the record is written) | Re-derives the same `evidence_id` at the next termination and converges on one record. |
| Partial escrow write | The temp directory was never renamed ⇒ no admissible escrow ⇒ re-escrow from the worktree if present, else `FAILED(ESCROW_WRITE_FAILED)`. Never a half-admitted record. |
| Record `QUEUED`, before locators | Re-runs stale checks and re-admits. Idempotent. |
| Locators stored, before submission | Sees `QUEUED` with locators present; `PublishRetryLocatorStore` writes are keyed by issue, so the rewrite is a no-op. |
| `PUBLISHING`, submission token orphaned | No live job for the token ⇒ re-runs stale checks and resubmits. The push is a fast-forward to `L`; if it already landed, remote head already equals `L` and the push is a no-op. |
| Push succeeded, PR update/link failed | Remote head equals `L` ⇒ skip push, take `PublishRecoveryService`'s existing already-created-PR recovery path. |
| PR reconciled, labels not applied | Label add/remove through `ActionApplier` are idempotent; re-applies the §7 transition. |
| Labels applied, record not marked `RECOVERED` | Verifies remote head == `L`, PR open, labels correct ⇒ marks `RECOVERED` without re-writing GitHub. |

Every row converges on exactly one published head, one PR, one review routing, and
one terminal record — which is what the dedup key and the fast-forward-only rule buy.

---

## 11. Implementation plan (#6914)

Ordered so each slice is independently shippable and leaves the tree green.

1. **Domain + store.** `domain/validated_work.py`, `ports/validated_work_store.py`,
   `infra/validated_work_store.py` (+ sqlite registry entry). Pure unit tests.
2. **Escrow + ref pinning.** Filesystem escrow with atomic rename; `WorkingCopy`
   extension for creating/resolving/deleting the pinned ref; retention setting.
3. **Owner, admission-only.** `dispose_at_termination()` returning `NONE`/`PARKED`
   plus evidence capture. Wire into `terminate_issue_runtime()` as a required
   parameter; add the fifth activity probe. At this point nothing recovers, but
   **nothing is destroyed** — scratch reset already stale-downgrades.
4. **Automatic recovery.** `QUEUED` → `PUBLISHING` → `RECOVERED` drain with the full
   stale-check set and publish-retry reconstruction. Route
   `STOPPED/MAX_ROUNDS_EXCEEDED` in (this is #7018).
5. **Classification cleanup.** Session/failure paths and the stuck sweep consume
   `IssueRuntimeTermination.validated_work`; generic `timed_out` becomes illegal for
   an issue with a disposition.
6. **Gated tech-lead op.** `recover_validated_work` through the existing
   `StoredTechLeadOp` lifecycle; config + settings schema; move out of
   `UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS` once the executor is wired.
7. **Operator command + contracts.** View model, public contract regeneration, UI
   OpenAPI, endpoint, dashboard action (with the §8.4 accessibility requirements).
8. **Backfill the stranded cohort.** #6327/#6335/#6337 (#6914) and #5204/#5561
   (#7011) admitted through the owner as `PARKED` records — via the same admission
   path, not a one-off script.

Slice 3 is the one that stops the bleeding; slices 4–8 recover what is already lost.
