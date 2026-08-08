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
- a validation record that record points at, with `passed=true`, carrying its own
  `head_sha`;
- that exact `head_sha` resolving to a commit in the repository's object store.

Agent prose, GitHub issue bodies, comments, the agent-writable tech-lead assignment
file, and agent-chosen filesystem paths are never authority (ADR-0016, ADR-0031's
trust boundary). Section 5 defines admission precisely.

### 1.1 The publishable commit is the validated commit — exactly

`validated_head_sha` is **always** the validation record's `head_sha`. It is never
the worktree HEAD, and a worktree HEAD is never promoted to "validated" by being a
descendant of a validated commit. **Ancestry is not identity.** If validation passed
at `V` and the worktree has since advanced to `L`, then `L` is unvalidated work;
publishing `L` under `V`'s validation record would put unvalidated code behind a
"validated" disposition — the precise inversion of the invariant this contract
exists to hold.

The rule is enforced at three separate moments, because a single admission-time
check cannot see a worktree that advances afterwards:

| Moment | Rule |
|---|---|
| Admission (§5) | The candidate commit *is* `validation.head_sha`. The observed worktree HEAD is recorded separately as `worktree_head_sha` — an observation, never the target. |
| Escrow (§6) | `refs/issue-orchestrator/validated/<issue>/<evidence_id>` pins `validated_head_sha` **only**. When `worktree_head_sha` differs, it is pinned separately at `refs/issue-orchestrator/observed/<issue>/<evidence_id>` so the unvalidated commits also survive — preserved, never published. |
| Submission (§4.3 check 4) | Immediately before handing off to the publisher, the pinned validated ref, the publishing worktree's HEAD, and the command's `target_head_sha` must **all** equal `validated_head_sha`. Any inequality aborts before any external write. |

A worktree ahead of its validation is not a defect in the evidence — the validated
commit is real and worth saving — but it is never *automatic*. It forces `PARKED`
with `WORKTREE_AHEAD_OF_VALIDATION`. The only two exits are an explicit operator or
tech-lead decision to publish `V` as-is, or a fresh validation run at `L` admitted
as **new evidence with its own `evidence_id`**. No path publishes `L` under `V`'s
record.

When `validated_head_sha` is not reachable in the object store at all (rewritten
history, pruned objects, a foreign clone), the evidence is unverifiable:
`VALIDATION_SHA_MISMATCH` ⇒ `FAILED`, artifacts preserved.

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
    NONE       = "none"       # resolved: no completed+validated work at this edge
    QUEUED     = "queued"     # recovery admitted automatically; drain will execute
    PARKED     = "parked"     # durable, awaiting approval (gated op or operator)
    PUBLISHING = "publishing" # admitted to the publisher; submission in flight
    RECOVERED  = "recovered"  # published + review routed; resolved
    FAILED     = "failed"     # fail-closed; artifacts preserved; UNRESOLVED
    ABANDONED  = "abandoned"  # operator explicitly accepted the loss; resolved


# Two DIFFERENT state sets. Conflating them is exactly the bug F5 found.

UNRESOLVED_STATES = frozenset({          # work still exists and is not safe to lose
    ValidatedWorkState.QUEUED,
    ValidatedWorkState.PARKED,
    ValidatedWorkState.PUBLISHING,
    ValidatedWorkState.FAILED,           # <- FAILED IS UNRESOLVED, not "terminal"
})

RESOLVED_STATES = frozenset({            # nothing is at risk; reset/teardown may proceed
    ValidatedWorkState.NONE,
    ValidatedWorkState.RECOVERED,
    ValidatedWorkState.ABANDONED,
})

OPEN_STATES = frozenset({                # at most one of these per dedup key (§4.1)
    ValidatedWorkState.QUEUED,
    ValidatedWorkState.PARKED,
    ValidatedWorkState.PUBLISHING,
})

assert UNRESOLVED_STATES | RESOLVED_STATES == set(ValidatedWorkState)
assert not (UNRESOLVED_STATES & RESOLVED_STATES)
assert OPEN_STATES < UNRESOLVED_STATES


class ValidatedWorkFailure(StrEnum):
    """Precise, enumerable reasons. Never a free-text-only failure."""
    ESCROW_WRITE_FAILED           = "escrow_write_failed"
    ARTIFACT_MISSING              = "artifact_missing"
    ARTIFACT_HASH_MISMATCH        = "artifact_hash_mismatch"
    ARTIFACT_UNTRUSTED_PATH       = "artifact_untrusted_path"
    VALIDATION_SHA_MISMATCH       = "validation_sha_mismatch"   # head_sha unreachable
    WORKTREE_AHEAD_OF_VALIDATION  = "worktree_ahead_of_validation"  # §1.1; parks
    WORKSPACE_INTEGRITY           = "workspace_integrity"      # #7017 detached HEAD etc.
    REF_PIN_LOST                  = "ref_pin_lost"
    PUBLISH_TARGET_MISMATCH       = "publish_target_mismatch"  # §4.3 check 4 tripped
    REMOTE_DIVERGED               = "remote_diverged"          # not a fast-forward
    REMOTE_HEAD_CHANGED           = "remote_head_changed"      # third, unexpected sha
    REMOTE_UNREADABLE             = "remote_unreadable"        # read failed != "absent"
    PR_CLOSED_OR_MERGED           = "pr_closed_or_merged"
    PR_BRANCH_MISMATCH            = "pr_branch_mismatch"
    ISSUE_UNREADABLE              = "issue_unreadable"
    RUNTIME_ACTIVE                = "runtime_active"
    PUSH_FAILED                   = "push_failed"
    SUBMISSION_LOST               = "submission_lost"          # token has no live job
    REVIEW_ROUTING_FAILED         = "review_routing_failed"


class ReviewDisposition(StrEnum):
    ROUTE_TO_PR_REVIEW  = "route_to_pr_review"   # normal review discovery on new head
    RESUME_REVIEW       = "resume_review"        # PR already under review; update head
    EXCHANGE_APPROVED   = "exchange_approved"    # exchange reached OK/REVIEWER_OK


@dataclass(frozen=True, slots=True)
class AdmittedArtifact:
    """One admitted artifact. IDENTITY-BEARING — every field enters evidence_id."""
    relative_path: str      # POSIX, relative to escrow root; never absolute/'..'
    sha256: str             # lowercase hex
    byte_size: int


@dataclass(frozen=True, slots=True)
class ValidatedWorkIdentity:
    """The stable semantic facts that define WHICH work this is.

    Every field is either immutable for the life of the work or an admitted
    content hash. Nothing here can change between two captures of the same work,
    which is what makes ``evidence_id`` re-derivable after a crash (§2.1.1).
    """
    schema_version: int                  # bumped when the identity field set changes
    repo_slug: str                       # target repository identity
    issue_number: int
    branch_name: str
    validated_head_sha: str              # == validation record head_sha (§1.1)
    worktree_head_sha: str               # observed HEAD at capture; may differ
    run_identity: SessionRunIdentity     # session_name, run_id, started_at
    completion_artifact: AdmittedArtifact
    validation_artifact: AdmittedArtifact
    exchange_summary_artifact: AdmittedArtifact | None
    requested_actions: tuple[RequestedAction, ...]
    review_disposition: ReviewDisposition
    exchange_terminal: ReviewExchangeTerminalState | None  # e.g. STOPPED/MAX_ROUNDS
    branch_binding_verified: bool        # False when HEAD was detached (#7017)


@dataclass(frozen=True, slots=True)
class ValidatedWorkEvidence:
    """Identity plus the mutable observations a recovery reconciles against."""
    identity: ValidatedWorkIdentity

    # --- OBSERVED, MUTABLE, DELIBERATELY OUTSIDE evidence_id -----------------
    captured_at: str                     # ISO-8601 UTC
    expected_remote_head_sha: str | None # remote branch head at capture; None = absent
    pr_number: int | None
    observed_blocking_labels: tuple[str, ...]  # exactly what this op may later clear

    @property
    def evidence_id(self) -> str:
        """Canonical identity hash. The dedup key. Depends on `identity` only."""
        return canonical_evidence_id(self.identity)
```

#### 2.1.1 `evidence_id` must survive the crash it deduplicates

The whole idempotency story is that a repeated capture of the *same* work
re-derives the *same* id and converges on the existing record. That only holds if
the hash covers nothing that moves between captures. Two classes of field are
therefore excluded by construction:

- **Capture timestamps.** `captured_at` is when we looked, not what we found.
- **Mutable external observations.** `expected_remote_head_sha`, `pr_number`, and
  `observed_blocking_labels` are reconciliation *inputs* read fresh from GitHub;
  a label added by a human between two captures must not mint a second record.

```python
IDENTITY_SCHEMA_VERSION = 1

def canonical_evidence_id(identity: ValidatedWorkIdentity) -> str:
    return "v%d:%s" % (
        identity.schema_version,
        hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest(),
    )
```

`_canonical_json` normalization, specified so two processes agree byte-for-byte:

| Kind | Normalization |
|---|---|
| JSON encoding | `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, UTF-8 |
| `StrEnum` | its `.value` |
| SHAs | lowercase, full 40-hex; a short or mixed-case sha is a hard admission error |
| Paths | POSIX separators, relative to the escrow root |
| `requested_actions` | sorted by `.value`, duplicates removed — request order is not identity |
| `None` | JSON `null` (never omitted — an omitted key and a null key must not collide) |
| `bool`/`int` | native JSON |
| Nested dataclasses | ordered dict of their own fields, same rules recursively |

The `v<schema_version>:` prefix means a future identity-field change produces
visibly different ids instead of silently colliding with v1 records.

**Reconciliation-observation refresh.** When a capture converges on an existing
record, the observed fields are refreshed **only while the record is in `QUEUED` or
`PARKED`** — pre-submission, where a newer remote head is simply better
information. Once the record is `PUBLISHING`, its `expected_remote_head_sha` is
frozen: it is the compare-and-set baseline the phase-aware checks (§4.3) depend on,
and overwriting it mid-flight would destroy the ability to tell "push landed" from
"someone else pushed".

#### 2.1.2 Atomic capture, and repair of a partial one

Capture writes three things that cannot be committed in one transaction. The order
is chosen so that every prefix is *repairable* and no prefix is *misleading*:

1. **Escrow** — artifacts written to `<escrow_root>/.tmp/<evidence_id>.<pid>/`,
   fsynced, then `os.replace`d onto `<escrow_root>/<issue>/<evidence_id>/`.
2. **Ref pin** — `refs/issue-orchestrator/validated/<issue>/<evidence_id>` (and
   `.../observed/...` when the worktree head differs).
3. **Record** — `INSERT INTO validated_work_records ... ON CONFLICT(evidence_id)
   DO NOTHING`, then read back the row that now exists.

Steps 1 and 2 are content-addressed by `evidence_id`, so re-running them is a
no-op rather than a duplicate. A crash between any two steps leaves an *orphan*
escrow directory and/or ref with no record — inert, never admissible on its own,
and never confused with real state because admission reads the **record**, never
the directory.

`reconcile_escrow_orphans()` runs once per orchestrator start, on the owner's first
`drain()` — the same seam that already performs restart reconciliation (§3.4), so
no new scheduler is introduced. For each escrowed `evidence_id` with no record it
re-runs step 3 from the escrowed identity, restoring the record whose insert was
lost. It **never deletes**: an orphan it cannot re-admit (issue closed, identity
unparseable) is left in place and reported — alongside the store's registry entry
in `infra/sqlite_registry.py` — by a doctor check. Deleting escrow is the one thing
this contract exists to prevent.

**Convergence onto a resolved record.** If a fresh capture re-derives an
`evidence_id` whose record is already `RECOVERED` or `ABANDONED`, the owner returns
that existing terminal disposition. It does not insert a second record, and it does
not reopen a resolved one — the `ON CONFLICT DO NOTHING` primary key is what makes
that structural rather than a race-prone read-then-write.

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
class OperatorResolution:
    """The ONLY way unresolved work becomes safe to lose. Never inferred."""
    actor: str            # operator identity or approved tech-lead op id
    reason: str           # non-empty; recorded verbatim in the durable row
    resolved_at: str      # ISO-8601 UTC


@dataclass(frozen=True, slots=True)
class ValidatedWorkDisposition:
    """The typed result. Reported on IssueRuntimeTermination and to the UI."""
    state: ValidatedWorkState
    reason: str
    evidence_id: str | None = None
    failure: ValidatedWorkFailure | None = None
    pr_number: int | None = None
    published_head_sha: str | None = None
    resolution: OperatorResolution | None = None

    @property
    def unresolved(self) -> bool:
        """True while work still exists that must not be destroyed."""
        return self.state in UNRESOLVED_STATES

    def __post_init__(self) -> None:
        # fail-closed shape rules
        if self.state is ValidatedWorkState.FAILED and self.failure is None:
            raise ValueError("FAILED disposition requires an enumerated failure")
        if self.state is not ValidatedWorkState.NONE and not self.evidence_id:
            raise ValueError("non-NONE disposition requires an evidence_id")
        if self.state is ValidatedWorkState.ABANDONED and self.resolution is None:
            raise ValueError("ABANDONED requires an explicit OperatorResolution")
        if self.state is not ValidatedWorkState.ABANDONED and self.resolution is not None:
            raise ValueError("only ABANDONED carries an OperatorResolution")
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

    def has_unresolved_work(self, issue_number: int) -> bool: ...
    """Activity probe for has_active_issue_runtime (fifth owner).

    True while ANY record for the issue is in UNRESOLVED_STATES — which
    includes FAILED. Named for the question the callers actually ask
    ("is there work here that must not be destroyed?"), not for a
    workflow phase; a `pending`-shaped predicate is what let a FAILED
    record read as inactive and become scratch-reset eligible.
    """

    def abandon(
        self, issue_number: int, evidence_id: str, resolution: OperatorResolution
    ) -> ValidatedWorkDisposition: ...
    """Operator explicitly accepts the loss: UNRESOLVED -> ABANDONED.

    The single modeled route out of FAILED/PARKED without a recovery.
    Requires an actor and a reason, is refused for QUEUED/PUBLISHING (stop
    the in-flight work first), and retains escrow + refs for the retention
    window regardless. Never callable by an agent.
    """

    def snapshot(self, issue_number: int) -> ValidatedWorkSnapshot | None: ...
    """Read model for the UI/view-model layer. Never re-derives policy."""
```

`ports/validated_work_store.py` (new) — durable state, section 4.
`ports/validated_head_publication.py` (new) — the execution boundary, section 4.4.

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
    lambda: validated_work is not None and validated_work.has_unresolved_work(n),  # NEW
)
```

The existing fail-safe wrapper (`_owner_active_or_unverifiable`) applies: a probe
that raises counts as active. Consequence: `has_active_reset_retry_runtime()` in
`entrypoints/web_retry_history_routes.py` — which the tech-lead reset executor and
the dashboard reset both consult through `_reset_retry_runtime_owners()` — now
stale-downgrades any scratch reset while **unresolved** work exists. That is the
concrete mechanism by which the stuck sweep can no longer discard validated work.

**Which states block, and why `FAILED` is one of them.** ADR-0035 promises that a
failed disposition never becomes scratch-reset eligible. A predicate phrased as
"pending" would break that promise the moment a record failed: `FAILED` is a
resting state, so "pending" reads false, the reset boundary sees no owner, and the
nuclear reset removes the worktree, branch, PR and labels of work that is *still
sitting in escrow because we could not publish it*. The predicate is therefore
phrased as **unresolved**, and the state sets from §2.1 are the definition:

| State | `has_unresolved_work` | Reset / termination | Escrow + ref retention |
|---|---|---|---|
| `NONE` | false | proceeds | n/a |
| `QUEUED` | **true** | stale-downgrades | indefinite |
| `PARKED` | **true** | stale-downgrades | indefinite |
| `PUBLISHING` | **true** | stale-downgrades | indefinite |
| `FAILED` | **true** | stale-downgrades | indefinite |
| `RECOVERED` | false | proceeds | `escrow_retention_days` after `terminal_at` |
| `ABANDONED` | false | proceeds | `escrow_retention_days` after `terminal_at` |

Unresolved work becomes resolvable by exactly two routes: a durable recovery
(`RECOVERED`), or an explicit, separately modeled operator decision
(`ABANDONED`, §2.2 — actor + reason, recorded, never inferred, never an agent).
There is no timeout, no sweep, and no "it has been failed long enough" heuristic
that resolves it, because every such heuristic is a fresh way to lose the work.

### 3.3 Consumers that stop losing the work

| Seam today | Change |
|---|---|
| `completion_review_exchange.py` — any non-ok outcome is a bare halt | Build a disposition request from the exchange terminal state. `STOPPED/MAX_ROUNDS_EXCEEDED` and `STOPPED/REVIEWER_REPORTS_NO_PROGRESS` carry `ReviewDisposition.ROUTE_TO_PR_REVIEW`; `ERROR/*` carries it too when completion+validation are conclusive. Publish-or-park, never discard (#7018 is this row). |
| `domain/completion_finalization.py` — matrix sees runtime facts only | `CompletionFinalizationCommand` gains `validated_work_present: bool`. `TERMINAL_REVIEW_EXCHANGE_TIMEOUT` may only be returned once disposition is recorded; the matrix stays pure — the caller gathers the fact. |
| `session_controller.py` / `session_completion.py` / `completion_action_planner.py` — classify to `TIMED_OUT`/`FAILED` | Consult `IssueRuntimeTermination.validated_work`. When it is not `NONE`, the recorded session outcome and the emitted event carry the disposition state; generic `timed_out` is no longer a legal classification for an issue whose disposition is in `UNRESOLVED_STATES` (`QUEUED`/`PARKED`/`PUBLISHING`/`FAILED`). |
| `stuck_sweep.py` — "`failure_reason` is always `timed_out`" | Reads the disposition snapshot; a stranded-with-disposition issue is reported as owned recovery, not as an undiagnosed timeout, and is excluded from scratch-reset proposals. |
| `control/tech_lead_reset_retry.py` + the dashboard reset — act on a boolean freshness check and then tear down | The returned `IssueRuntimeTermination.validated_work` is **consumed, not discarded**: a result with `.unresolved` true aborts the reset with a typed stale-downgrade carrying `state`/`failure`/`evidence_id`, so the operator is told *which* record blocked and what would resolve it. Ignoring the field is the failure mode F5 describes; §9's guardrail asserts every call site binds it. |
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
    worktree_head_sha     TEXT NOT NULL,              -- observed at capture (§1.1)
    expected_remote_head  TEXT NOT NULL DEFAULT '',   -- '' = no remote branch expected
    pr_number             INTEGER,
    state                 TEXT NOT NULL,              -- ValidatedWorkState
    failure               TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure
    reason                TEXT NOT NULL DEFAULT '',
    identity              TEXT NOT NULL,              -- ValidatedWorkIdentity JSON
    observations          TEXT NOT NULL,              -- mutable half of the evidence
    escrow_dir            TEXT NOT NULL,              -- relative to escrow root
    pinned_ref            TEXT NOT NULL,              -- validated commit
    observed_ref          TEXT NOT NULL DEFAULT '',   -- unvalidated worktree head, if any
    submission_token      TEXT NOT NULL DEFAULT '',   -- set while PUBLISHING
    published_head_sha    TEXT NOT NULL DEFAULT '',
    resolved_by           TEXT NOT NULL DEFAULT '',   -- OperatorResolution.actor
    resolution_reason     TEXT NOT NULL DEFAULT '',
    resolved_at           TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    terminal_at           TEXT NOT NULL DEFAULT ''    -- entry into a RESOLVED state
);

-- Dedup: at most one OPEN record per target+branch+validated head.
-- OPEN_STATES only (§2.1) — 'failed' is deliberately outside this index because a
-- failed record is resolved by transitioning THAT record (FAILED -> PARKED via
-- operator re-submit, or FAILED -> ABANDONED), never by minting a rival one.
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_open
    ON validated_work_records (repo_slug, issue_number, branch_name, validated_head_sha)
    WHERE state IN ('queued', 'parked', 'publishing');

-- The activity/reset probe's index: unresolved includes 'failed'.
CREATE INDEX IF NOT EXISTS ix_validated_work_unresolved
    ON validated_work_records (issue_number, state)
    WHERE state IN ('queued', 'parked', 'publishing', 'failed');

CREATE INDEX IF NOT EXISTS ix_validated_work_issue
    ON validated_work_records (issue_number, state);
```

Records are **append-then-transition**, never deleted by normal operation. The
unique partial index is the deduplication rule from #6914 expressed as a database
constraint rather than as caller discipline: *target repository + issue + branch +
validated HEAD*, with `evidence_id` (the canonical identity hash of §2.1.1) as the
primary key covering the evidence-identity half.

The two partial indexes are deliberately different, and the difference is the F5
fix made structural: `ux_validated_work_open` answers "may I admit another record
for this key?" (`OPEN_STATES`), while `ix_validated_work_unresolved` answers "is
there work here that must not be destroyed?" (`UNRESOLVED_STATES`). `failed`
appears in the second and not the first.

### 4.2 State machine

```
  termination ──► admission ──► NONE                        (resolved: nothing found)
        │
        ├── conclusive + branch binding verified
        │   + worktree head == validated head ─────────────► QUEUED
        │                                                      │
        └── incomplete / stale / unverifiable / worktree       │ drain:
            ahead of validation ──────────────────► PARKED     │ phase-aware
                                                      │        │ checks pass
                            approval (tech-lead op ───┘        │
                             or operator command)              ▼
                                                          PUBLISHING
                                                               │
                                    publish + review routed    │
                                    reconciled ────────────────┴──► RECOVERED
                                                                       (resolved)

   any step, fail-closed ──► FAILED  ── operator re-submit ──► PARKED
                              (UNRESOLVED — blocks reset)
                                 │
                                 └── explicit OperatorResolution ──► ABANDONED
                                                                      (resolved)
```

Legal transitions only; every other pair raises. `PARKED` and `FAILED` are durable
resting states — artifacts are retained, the issue stays blocked, scratch reset
stale-downgrades (§3.2), and there are exactly three exits:

| From | To | Trigger | Guard |
|---|---|---|---|
| `PARKED` | `PUBLISHING` | approved tech-lead op or operator command | full §4.3 pre-submission phase |
| `FAILED` | `PARKED` | operator re-submits after fixing the external condition (e.g. reopening a closed PR) | re-runs every §4.3 check from scratch |
| `PARKED`/`FAILED` | `ABANDONED` | `abandon()` with an `OperatorResolution` | actor + non-empty reason; refused for `QUEUED`/`PUBLISHING` |

`ABANDONED` is the only modeled way unresolved work becomes safe to lose, and it
still retains escrow and refs for `escrow_retention_days` — an operator saying "I
accept this loss" resolves the *lifecycle*, not the *bytes*.

### 4.3 Stale checks, revalidated immediately before every mutation

Executed by the owner in `drain()`/`recover()`, never by an initiator.

**The checks are phase-aware.** A single fixed expectation cannot be right on both
sides of a non-atomic external write: before the push, `expected_remote_head_sha`
is the only legal remote state; *after* a push that landed, the remote is at
`validated_head_sha` — which is the success we asked for. A check that demands
`expected_remote_head_sha` unconditionally therefore reports the successful push as
a divergence and fails the record, which is the exact non-atomic failure mode this
design exists to survive. Every remote comparison below is a **compare-and-set**
against a per-phase allowed set, not an equality test against one sha.

```python
class DispositionPhase(StrEnum):
    PRE_SUBMISSION = "pre_submission"   # QUEUED/PARKED -> PUBLISHING
    RECONCILING    = "reconciling"      # record is already PUBLISHING
```

Phase-independent checks:

1. `repo_slug` matches the running orchestrator's repository identity.
2. Issue is readable and open; the recorded `observed_blocking_labels` are re-read
   fresh ("could not read" is not "not blocked" — the rule
   `publish_retry_admission.board_block_reason()` already encodes).
   Unreadable ⇒ `ISSUE_UNREADABLE`, retried next drain, never published past.
3. Every escrowed artifact exists and its sha256 matches
   (`ARTIFACT_MISSING` / `ARTIFACT_HASH_MISMATCH`).
4. **Publication target identity (§1.1).** `pinned_ref` resolves and equals
   `validated_head_sha`; the publishing worktree's HEAD resolves to
   `validated_head_sha`; and the command's `target_head_sha` equals it. All three,
   checked in the same drain step that submits. Any inequality ⇒
   `PUBLISH_TARGET_MISMATCH` (or `REF_PIN_LOST` when the ref is gone) ⇒ `FAILED`
   **before any external write**. This is what stops an advanced worktree from
   being published under an older validation.
5. `has_active_issue_runtime()` (excluding this owner's own probe) is false.
6. No other record in `OPEN_STATES` shares the dedup key.

Phase-dependent remote checks — the allowed-state tables:

**7. Remote branch head.**

| Recorded expectation | Phase | Allowed observed state | Action | Anything else |
|---|---|---|---|---|
| sha `R` | `PRE_SUBMISSION` | `R` | push `R → L` | `REMOTE_HEAD_CHANGED` ⇒ `FAILED` |
| sha `R` | `RECONCILING` | `R` | submission never landed ⇒ resubmit | `REMOTE_HEAD_CHANGED` ⇒ `FAILED` |
| sha `R` | `RECONCILING` | `L` (== `validated_head_sha`) | **push landed** ⇒ reconcile only, no second push | — |
| absent (`''`) | `PRE_SUBMISSION` | branch absent | push creates it; no ancestry requirement | branch present ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED` |
| absent (`''`) | `RECONCILING` | branch absent | resubmit | — |
| absent (`''`) | `RECONCILING` | branch at `L` | **push landed** ⇒ reconcile only | any other sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED` |

A third sha is always a hard divergence — never "probably fine". A *failed read* is
`REMOTE_UNREADABLE` and leaves the record where it is for the next drain; it is
never collapsed into "absent", because "absent" authorizes a branch-creating push.

**8. Fast-forward legality** (`PRE_SUBMISSION` with a present remote branch only):
`validated_head_sha` must be a **descendant** of the observed remote head
(`merge-base --is-ancestor`). Not a descendant ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`.
**Fast-forward only. Never force-push.** Skipped when the branch is absent (nothing
to fast-forward from) and when reconciling a landed push (the remote already *is*
the target).

**9. PR head**, when `pr_number` is set — the same allowed-set shape, so a PR
whose head advanced to the target by our own push is recognized rather than
rejected:

| Phase | PR state | Allowed head sha | Action |
|---|---|---|---|
| `PRE_SUBMISSION` | open, head ref == `branch_name` | `expected_remote_head_sha` | publish |
| `RECONCILING` | open, head ref == `branch_name` | `expected_remote_head_sha` **or** `validated_head_sha` | resubmit / reconcile respectively |
| either | closed or merged | — | `PR_CLOSED_OR_MERGED` ⇒ `FAILED` |
| either | head ref != `branch_name` | — | `PR_BRANCH_MISMATCH` ⇒ `FAILED` |

The PR head and the branch head are checked as one decision, not two independent
equalities: the publisher (§4.4) receives the observed pair and returns
`ALREADY_AT_TARGET` when both are at `validated_head_sha`, `SUBMITTED` when both
are at the expectation, and refuses on any mixture it cannot classify.

### 4.4 The publication boundary — `ValidatedHeadPublisher`

This is the single named boundary the design review is required to specify, and it
cannot be "call `PublishRecoveryService.retry_publish()` unchanged".

**Why the existing entry point cannot serve.** `retry_publish()` is a *manual
admission* owner: it runs `_retry_decision()` (board/locator admission policy), and
then — before any push — takes the existing-PR branch. `_matching_open_pr()` scopes
open PRs to the expected branch and returns the first match; if one exists,
`retry_publish()` calls `RetrySuccessFinalizer.finalize(...)`, clears retry terminal
state, and returns `recovered_existing_pr` **without ever calling
`_submit_republish`**. Nothing in that path compares the PR's head with the commit
we are trying to land. In the Porchpin #5 proof — open PR *P* at remote head `R`,
validated local head `L` — that is precisely the wrong branch: the PR exists, so
the service declares victory and finalizes, and `L` is never pushed. The scenario
this contract is built to fix would silently produce a green-looking recovery with
the validated commits still local-only.

So the design defines the **execution-only** command the disposition owner actually
needs, and lets both callers compose it. `ports/validated_head_publication.py`:

```python
class RemoteHeadExpectation(StrEnum):
    EXACT         = "exact"          # disposition: remote must be at expected_sha
    ABSENT        = "absent"         # disposition: branch must not exist
    UNCONSTRAINED = "unconstrained"  # manual retry: no captured expectation


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadCommand:
    """Execution only. Carries no admission policy and makes no board decisions."""
    issue_number: int
    repo_slug: str
    branch_name: str
    target_head_sha: str                  # the commit that MUST end up published
    expectation: RemoteHeadExpectation
    expected_remote_head_sha: str | None  # required iff expectation is EXACT
    pr_number: int | None
    review_disposition: ReviewDisposition
    submission_token: str                 # idempotency key; see below
    locators: PublishRetryLocators        # what the executor pushes from


class PublishValidatedHeadStatus(StrEnum):
    SUBMITTED         = "submitted"           # push job in flight; poll by token
    ALREADY_AT_TARGET = "already_at_target"   # branch+PR already at target: reconcile only
    RECONCILED        = "reconciled"          # finalize + review routing completed
    REJECTED          = "rejected"            # preconditions unmet; nothing written
    DIVERGED          = "diverged"            # third-party head; nothing written


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadOutcome:
    status: PublishValidatedHeadStatus
    submission_token: str
    observed_remote_head_sha: str | None
    observed_pr_head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    failure: ValidatedWorkFailure | None
    message: str


class ValidatedHeadPublisher(Protocol):
    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome: ...

    def submission_status(self, token: str) -> PublishSubmissionStatus: ...
    """Durable, owner-observable: PENDING | SUCCEEDED | FAILED | UNKNOWN.

    UNKNOWN (no live job, no recorded terminal) is what a crash looks like;
    the disposition owner re-runs §4.3 in the RECONCILING phase rather than
    assuming either outcome.
    """
```

**The decision the publisher owns** (and `retry_publish()` today does not make):

| Observed branch head | Observed PR head | Verdict |
|---|---|---|
| == `expected_remote_head_sha` (or branch absent under `ABSENT`) | == expected, or no PR | **publish** — push `→ target_head_sha`, then finalize + route |
| == `target_head_sha` | == `target_head_sha` | **`ALREADY_AT_TARGET`** — the push already landed; finalize + route, no second push |
| == `target_head_sha` | == expected (PR view stale) | re-read once, then treat as `ALREADY_AT_TARGET` |
| anything else | — | **`DIVERGED`** — no write; `REMOTE_HEAD_CHANGED`/`REMOTE_DIVERGED` |
| under `UNCONSTRAINED` (manual retry) | — | publish when `target_head_sha` is a descendant of the observed head; `DIVERGED` otherwise |

"An open PR exists" is never on its own a reason to skip the push. Finalization is
reached through `RetrySuccessFinalizer` + `RetryReviewRouting` exactly as today, but
only after the head is confirmed at the target.

**`submission_token` is derived, not generated:**
`token = "vwd:" + evidence_id + ":" + target_head_sha`. A crash-and-retry re-derives
the same token, so `submission_status()` finds the prior submission instead of
starting a rival one, and duplicate submits collapse structurally rather than by
timing.

**Composition (A1).** The execution half of `PublishRecoveryService` — locator
push, `_submit_republish`, finalization, review routing — moves behind this port.
Both callers then compose one executor with their own admission policy:

| Caller | Admission policy (stays with the caller) | Command it builds |
|---|---|---|
| `PublishRecoveryService.retry_publish()` (manual, board/UI) | `_retry_decision()`, `board_block_reason()`, `locator_block_reason()` — unchanged | `expectation=UNCONSTRAINED`, `target_head_sha` = locator worktree HEAD, token = existing retry job key |
| `ValidatedWorkDispositionService` | §4.3 phase-aware checks, escrow/ref identity, dedup | `expectation=EXACT`/`ABSENT`, `target_head_sha=validated_head_sha`, token derived from `evidence_id` |

Manual Retry Publish keeps its current admission semantics and its current
observable behaviour for the case it was built for; what changes is that the
"an open PR already exists" shortcut becomes a head comparison instead of an
existence check, which is a bug fix for that path too.

**Handoff performed by the disposition owner:**

1. If the original worktree is gone, the owner **rehydrates** it: create a worktree
   at the canonical issue path checked out at `pinned_ref` (so its HEAD *is*
   `validated_head_sha`, satisfying §4.3 check 4), then restore the escrowed
   completion and validation records into the run directory. This preserves
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
4. The owner calls `ValidatedHeadPublisher.publish_or_reconcile(...)`, stores the
   returned `submission_token`, and transitions to `PUBLISHING` **before** the
   external write is observable — so a crash mid-push resumes in the `RECONCILING`
   phase, which accepts both the pre-push and post-push remote states.
5. On `SUBMITTED`, subsequent drains poll `submission_status(token)`;
   `ALREADY_AT_TARGET`/`RECONCILED` with the head verified at `validated_head_sha`
   marks `RECOVERED`. `UNKNOWN` re-runs §4.3 in `RECONCILING` rather than
   resubmitting blind.

No other caller may reconstruct locators or call the publisher. This is enforced
mechanically (§9).

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
the run directory to a validation record with `passed=true` whose `head_sha`
resolves to a commit in the object store.** Recency alone never selects; validity
gates it. An unparseable canonical artifact is simply not a candidate — it is not
"the" record that wins by being canonical.

**The selected record fixes the target (§1.1).** `validated_head_sha` is taken from
the selected validation record's `head_sha` — never from the worktree. The worktree
HEAD is read separately into `worktree_head_sha` and compared:

| Relationship | Meaning | Disposition |
|---|---|---|
| `worktree_head == validated_head` | validation covers exactly what is checked out | eligible for `QUEUED` |
| `validated_head` is an ancestor of `worktree_head` | unvalidated commits sit on top | `PARKED` + `WORKTREE_AHEAD_OF_VALIDATION`; both commits pinned (§6); nothing auto-publishes |
| `validated_head` unreachable from `worktree_head` and from the branch | history rewritten or objects pruned | `FAILED` + `VALIDATION_SHA_MISMATCH`; artifacts preserved |
| HEAD detached (#7017) | commits are real, branch binding is not | `branch_binding_verified=False` ⇒ `PARKED` (as before) |

Note the second row is the one an ancestor-or-equal rule would have swallowed: it
looks like success (validation passed, work exists, the worktree is "ahead") and is
exactly the case where publishing the worktree would ship unvalidated code.

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
| Validated commits | pinned by `refs/issue-orchestrator/validated/<issue>/<evidence_id>` in the shared object store | ref deletion on a RESOLVED record + retention window |
| Unvalidated commits on top of them (§1.1) | pinned by `refs/issue-orchestrator/observed/<issue>/<evidence_id>` when the worktree head differs | same window as the validated ref |
| Live run directory | inside the worktree (unchanged) | worktree removal (now non-fatal) |

Rules:

- Escrow writes go to a sibling temp directory and are moved into place with an
  atomic rename, so a partially written escrow is never admissible (§2.1.2).
- A git ref in the common `.git` directory survives `git worktree remove`, which is
  what makes the commits durable without copying a bundle. Pinning also protects
  the objects from `gc`.
- The `observed` ref exists so that "we refuse to publish this" never means "we let
  this be collected". Unvalidated work is preserved for the human, and is only ever
  publishable by being validated and admitted as new evidence.
- `_delete_issue_branches()` in `control/maintenance.py` must skip
  `refs/issue-orchestrator/validated/*` and `refs/issue-orchestrator/observed/*`,
  and `reset_issue(from_scratch=True)` is already blocked upstream by the §3.2
  unresolved-work probe.
- `session_output_retention_days` cleanup must not traverse the escrow root.
- New setting `validated_work.escrow_retention_days` (default `30`, §8.2): escrow
  and both refs are released only after a record has been in a **`RESOLVED_STATES`**
  state (`RECOVERED` or `ABANDONED`) for that long, measured from `terminal_at`.
  Every `UNRESOLVED_STATES` record — `QUEUED`, `PARKED`, `PUBLISHING`, and
  `FAILED` — is retained **indefinitely**. The whole point is that unresolved work
  is never garbage, and a failed disposition is unresolved work, not a closed case.

---

## 7. Label transitions

Applied **only after their corresponding effect succeeds** (ADR-0013: labels are
crash-safe truth, so a label must never claim an effect that did not happen).

| Transition point | Effect that must succeed first | Labels |
|---|---|---|
| Evidence recorded (`QUEUED`/`PARKED`) | durable record + escrow committed | add `recovery-pending` (new, `LabelCategory.BLOCKING`). Existing blocking label is **kept** — the issue is not unblocked by being owned. |
| Admission to publish (`PUBLISHING`) | locators stored | add `publish-failed` if absent (the executor's board precondition); the pre-existing set is already recorded in `observed_blocking_labels`. |
| Publication + review routing succeeded (`RECOVERED`) | remote head == `validated_head_sha`, PR open, review routed | remove `recovery-pending`; remove **only** the labels in `observed_blocking_labels` plus the `publish-failed` this op added. Then normal routing applies `pr-pending`/review labels via `RetrySuccessFinalizer`. |
| Disposition failed (`FAILED`) | — | keep `recovery-pending`; add `tech-lead-needs-human`. Never leave the issue in a plain `blocked-failed` state, never scratch-eligible — `recovery-pending` stays *because* `FAILED` is unresolved (§3.2). |
| Operator abandoned (`ABANDONED`) | durable `OperatorResolution` recorded | remove `recovery-pending` (the work is now formally resolved and reset may proceed); keep `tech-lead-needs-human` and the pre-existing blocking labels untouched. Escrow and refs are still retained for the retention window. |

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

Two independent config changes. The first rides an existing section; the second
introduces a **new top-level section**, which in this codebase is a fixed set of
surfaces — naming a settings field alone would produce either a `config_attr`
pointing at an attribute that does not exist or YAML that
`ALLOWED_TOP_LEVEL_FIELDS` rejects as unknown at load time.

**(a) Tech-lead authority action** — existing `tech_lead` section, no new surfaces:

- `infra/config_models_tech_lead.py`: `TechLeadAuthorityConfig.recover_validated_work`
  defaults to `"propose"`; add the key to `TECH_LEAD_AUTHORITY_CONFIGURABLE_ACTIONS`.
- `infra/settings_schema.py`: `tech_lead_authority_recover_validated_work`.

**(b) `Config.validated_work: ValidatedWorkConfig`** — the owner shape for
`escrow_retention_days`. `merge_queue` is the closest precedent (a small, optional,
defaults-off top-level section) and the slice below follows it file-for-file:

| Surface | File | Change |
|---|---|---|
| Model | `infra/config_models.py` | `@dataclass ValidatedWorkConfig` with `escrow_retention_days: int = 30` |
| Config attribute | `infra/config.py` | `validated_work: ValidatedWorkConfig = field(default_factory=ValidatedWorkConfig)` |
| Allowed YAML key | `infra/config_sections.py` | add `"validated_work"` to `_TOP_LEVEL_SECTION_KEYS` (which *derives* `ALLOWED_TOP_LEVEL_FIELDS`, so this one edit is what makes the key legal) |
| Parser | `infra/config_sections.py` | `parse_validated_work_config()` — rejects non-int and `< 1` with the section-qualified message style the other parsers use |
| Parser registration | `infra/config_sections.py` | entry in `_OPTIONAL_SECTION_PARSERS`, so `apply_optional_sections()` picks it up with no new branch |
| Shape validation | `infra/config_schema.py` | `"validated_work": dataclass_config_shape(ValidatedWorkConfig)` |
| `to_dict()` | `infra/config.py` | emit `{"validated_work": {"escrow_retention_days": ...}}` |
| YAML round-trip | `infra/config_serialization.py` | `validated_work_section(config)` emitting only non-default values, added to the section list `to_yaml_dict` composes |
| Settings field | `infra/settings_schema.py` | `validated_work_escrow_retention_days` with `config_attr`/`yaml_path` both `validated_work.escrow_retention_days` |
| Settings section | `infra/settings_schema.py` | `ValidatedWorkSettings` model + `{"key": "validated_work", "label": "Validated Work", "model": ValidatedWorkSettings}` in the settings-section list |
| Generated reference | `docs/user/configuration_reference.md` | regenerated from the settings schema |
| Example | `examples/config.example.yaml` | `validated_work:` block with the commented default |
| Tests | `tests/unit/test_config.py` | defaults, YAML parse, unknown-key rejection, and `to_dict`/`to_yaml_dict` round-trip omitting the default |
| Tests | `tests/unit/test_settings_schema.py` | drift + `config_attr` resolvability against a real `Config` |

Drift between the settings schema and the config model is caught by
`tests/unit/test_settings_schema.py`; drift in the generated reference by the same
suite. (Follow the `configuration` skill's file checklist — all config-touching
files move together.) §11 slice 2 owns this whole slice, so the retention setting
cannot ship as a UI-visible field that startup is unable to consume.

### 8.3 Events — `events/catalog.py`

New `validated_work.*` domain:

| EventName | Payload highlights |
|---|---|
| `VALIDATED_WORK_DETECTED` | `issue_number`, `evidence_id`, `validated_head_sha`, `exchange_terminal` |
| `VALIDATED_WORK_QUEUED` | `evidence_id`, `branch_name`, `pr_number` |
| `VALIDATED_WORK_PARKED` | `evidence_id`, `failure` (why approval is required) |
| `VALIDATED_WORK_RECOVERED` | `evidence_id`, `published_head_sha`, `pr_number` |
| `VALIDATED_WORK_DISPOSITION_FAILED` | `evidence_id`, `failure`, `reason` |
| `VALIDATED_WORK_ABANDONED` | `evidence_id`, `actor`, `reason` — the audit trail for the one action that makes unresolved work resolvable |

The UI-visible subset is added to the public timeline event enum in the same module,
per the `schema-updates` skill.

### 8.4 Public contract + Control Center command

- `contracts/public.py`: the issue-detail view model gains a `validated_work` block
  — `state`, `unresolved`, `evidence_id`, `validated_head_sha`, `worktree_head_sha`,
  `pr_number`, `failure`, `escrow_retained`, and the available operator actions
  (`recover`, and `abandon` for a `PARKED`/`FAILED` record). Regenerate
  `contracts/public/*.json` with `scripts/generate_public_contracts.py`; drift is
  enforced by `tests/unit/test_public_contract_schemas.py`.
- Availability comes from `ValidatedWorkDispositionOwner.snapshot()`, not from the
  route re-deriving policy — the same shape as the existing
  `can_retry_publish()`-gated `retry_publish` action in
  `entrypoints/web_issue_detail_routes.py`.
- New endpoints in `entrypoints/web_retry_history_routes.py` (beside
  `retry-publish`), each delegating to the owner and returning the typed result:
  `POST /api/issues/{issue_number}/recover-validated-work` →
  `deps.validated_work.recover(...)`, and
  `POST /api/issues/{issue_number}/abandon-validated-work` →
  `deps.validated_work.abandon(...)` with the operator's identity and a
  **required** non-empty reason from the request body. Register both endpoints and
  their payloads in the UI OpenAPI contract per the `ui-openapi` skill.
- Accessibility for the new action buttons: native `<button>`, keyboard reachable,
  visible focus ring, accessible name that includes the issue number, and a
  non-colour status signal (text + icon) for `PARKED`/`FAILED`/`ABANDONED` in both
  themes. The error toast for a `FAILED` disposition must not auto-dismiss. Abandon
  is destructive-by-consequence (it makes the issue reset-eligible), so it needs a
  confirm step whose dialog states what becomes possible, keeps focus trapped, and
  is dismissible by `Escape`.

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

- Every `ValidatedWorkState` — all seven — renders the correct issue-detail view
  model and the correct operator action availability.
- The tech-lead board renders a `recover_validated_work` gated op; removing
  `proposed-tech-lead` and the Control Center command reach the same owner and
  produce the same typed result.
- `session_controller`/`session_completion` classification: an issue with a non-`NONE`
  disposition is never recorded as generic `timed_out`.
- The reset path binds the returned disposition: a reset attempted against every
  `UNRESOLVED_STATES` value stale-downgrades with zero effects and surfaces the
  blocking `state`/`evidence_id`.

**Owner behaviour**: one test per stale check in §4.3; one per crash point in §10;
dedup (repeat request converges, never double-publishes); restart drain; escrow
atomicity; fast-forward-only refusal on divergence.

**Per-finding regression cases** — each of these fails against the pre-review
contract, which is why they are named individually rather than folded into the
sweeps above:

| Guards | Case |
|---|---|
| §1.1 target identity | Validation passes at `V`; the worktree advances to `L` (`V` an ancestor of `L`). Assert the record's `validated_head_sha == V`, the state is `PARKED`/`WORKTREE_AHEAD_OF_VALIDATION`, **no push occurs**, and `L` is never published under `V`'s record. A second case validates at `L` and asserts a *new* `evidence_id`. |
| §1.1 submission check | A record reaches submission while the rehydrated worktree HEAD has been moved off `validated_head_sha`: `PUBLISH_TARGET_MISMATCH`, `FAILED`, zero external writes. |
| §4.4 publisher | Open PR *P* at remote head `R`, target `L`: the publisher **pushes** and does not take an existing-PR shortcut; end state has remote head `L`. |
| §4.4 publisher | Remote branch and PR already at `L`: `ALREADY_AT_TARGET`, finalize + route, **no second push**. |
| §4.4 publisher | Remote head is a third sha `X`: `DIVERGED`, no push, no finalize, no label writes. |
| §4.4 publisher | Manual `retry_publish()` with `UNCONSTRAINED` keeps its existing admission behaviour, and its existing-PR path now compares heads. |
| §2.1.1 identity | Capture the same work twice at different `captured_at`, with different remote heads, and after a human adds a blocking label: identical `evidence_id`, exactly one row. |
| §2.1.1 identity | Requested actions supplied in a different order produce the same id; a different completion artifact hash produces a different id. |
| §2.1.2 atomicity | Crash after escrow rename, after ref pin, and after insert; startup `reconcile_escrow_orphans()` converges to one record and deletes nothing. Re-capture converging on a `RECOVERED`/`ABANDONED` id returns that record and inserts nothing. |
| §4.3 phases | One test per crash row in §10, asserting the allowed-state table: `RECONCILING` accepts remote at `expected` **and** remote at `validated_head_sha`; both branch-absent variants; a third sha fails in both phases. |
| §3.2 / F5 | Scratch-reset freshness for **every** state, with `FAILED` asserted reset-**ineligible**, and `ABANDONED` asserted eligible only after a recorded `OperatorResolution`. |
| §6 retention | The retention sweep releases escrow/refs only for `RESOLVED_STATES` past the window, and never for a `FAILED` record regardless of age. |
| §8.2 config | `validated_work.escrow_retention_days` parses from YAML, rejects `0`/non-int, survives a `to_dict`/`to_yaml_dict` round-trip, is omitted at default, and its `config_attr` resolves against a real `Config`. |

**Non-regression**: Retry Publish, `request_rework` (#7008), reset-from-scratch, and
kill-session behaviour unchanged apart from the head-comparison fix above; scratch
reset now stale-downgrades while unresolved work exists.

**Mechanical guardrails** (ADR-0012), added to the existing AST guardrail suite:

- No module outside the disposition owner constructs `PublishRetryLocators`.
- No module outside the owner adds/removes `recovery-pending`.
- `IssueRuntimeTermination` cannot be constructed without `validated_work`
  (enforced by the type, verified by a test).
- Every `terminate_issue_runtime()` call site **binds** the returned
  `validated_work` — a discarded result is the F5 failure mode in miniature.
- Only `ValidatedWorkDispositionService` and `PublishRecoveryService` call
  `ValidatedHeadPublisher.publish_or_reconcile`.

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
| Admission | Both records are run-scoped and therefore admissible *sources*. Selection (§5) rejects the invalid one because it does not parse, and selects the valid one because its `validation_record_path` resolves in-run to `passed=true` with `head_sha == L`. The worktree HEAD is also exactly `L`, so the target is fixed **by identity, not by ancestry**. The canonical path holds no privileged status. |
| Evidence | Identity: `validated_head_sha=L`, `worktree_head_sha=L`, `branch_name=b`, `review_disposition=RESUME_REVIEW`, `branch_binding_verified=True`. Observations (outside `evidence_id`): `expected_remote_head_sha=R`, `pr_number=P`, `observed_blocking_labels=("blocked-failed",)`. |
| Escrow | Completion + validation records copied to `<state_dir>/validated-work/N/<evidence_id>/` (temp dir + atomic rename); `L` pinned at `refs/issue-orchestrator/validated/N/<evidence_id>`. No `observed` ref — the two heads agree. |
| Disposition | Evidence conclusive ⇒ `QUEUED`. `recovery-pending` added; `blocked-failed` kept. `IssueRuntimeTermination.validated_work.state == QUEUED`, so the session is **not** classified `timed_out`. |
| Drain | §4.3 in `PRE_SUBMISSION`: remote head is `R` as expected, PR *P*'s head is `R`, pinned ref == rehydrated worktree HEAD == `L`, and `L` is a descendant of `R` ⇒ fast-forward legal. Locators reconstructed (worktree rehydrated at the pinned ref if it is gone), `publish-failed` added, `publish_or_reconcile` called with `target_head_sha=L`, `expectation=EXACT(R)`; state `PUBLISHING` before the write is observable. |
| Publish | The publisher sees branch and PR at `R` — the expectation, **not** the target — so it pushes. Fast-forward `b → L`; PR *P*'s head becomes `L`. This is the row the old contract got wrong: `retry_publish()` would have found an open PR for `b` and finalized it without pushing, leaving `L` local-only. No new PR, no supersede, no force-push, no reset. |
| Review | `RetryReviewRouting` routes the new head through normal review discovery — review resumes on `L`. No approval label is applied, so there is no false ready-to-merge state. |
| Finalize | `recovery-pending`, `blocked-failed`, and the op's own `publish-failed` removed; `pr-pending` applied by the normal finalizer; record `RECOVERED`; escrow + ref retained for `escrow_retention_days`. |
| Divergence variant | If `L` were **not** a descendant of `R`, step 6 of §4.3 fails ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`, artifacts preserved, `tech-lead-needs-human` added. A human resolves the divergence; nothing is force-pushed and nothing is deleted. |

**Crash points around the non-atomic GitHub writes:**

The phase column is load-bearing: every row after submission runs §4.3 in
`RECONCILING`, where **both** `R` and `L` are allowed remote states. That is what
makes a successful-but-unacknowledged push a recoverable state rather than a
`REMOTE_HEAD_CHANGED` failure.

| Crash after | Phase on restart | On restart, `drain()` does |
|---|---|---|
| Nothing (before the record is written) | — | Re-derives the same `evidence_id` (§2.1.1 — `captured_at` and the observed remote head are outside the hash) at the next termination and converges on one record. |
| Partial escrow write | — | The temp directory was never renamed ⇒ no admissible escrow ⇒ re-escrow from the worktree if present, else `FAILED(ESCROW_WRITE_FAILED)`. Never a half-admitted record. |
| Escrow renamed / ref pinned, insert lost | — | `reconcile_escrow_orphans()` re-inserts the record from the escrowed identity. Nothing is deleted; the orphan is inert until then because admission reads records, not directories. |
| Record `QUEUED`, before locators | `PRE_SUBMISSION` | Re-runs the checks and re-admits. Remote is still at `R`. Idempotent. |
| Locators stored, before submission | `PRE_SUBMISSION` | Sees `QUEUED` with locators present; `PublishRetryLocatorStore` writes are keyed by issue, so the rewrite is a no-op. |
| `PUBLISHING`, submission token orphaned | `RECONCILING` | `submission_status(token)` is `UNKNOWN`. Remote at `R` ⇒ allowed ⇒ resubmit with the **same derived token**, so no rival submission. Remote at `L` ⇒ allowed ⇒ `ALREADY_AT_TARGET`, reconcile without pushing. Remote at any third sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| Push succeeded, PR update/link failed | `RECONCILING` | Remote head and PR head are `L` — an **allowed** state for this phase, not a divergence. The publisher returns `ALREADY_AT_TARGET`; finalize + review routing run, no second push. |
| PR reconciled, labels not applied | `RECONCILING` | Label add/remove through `ActionApplier` are idempotent; re-applies the §7 transition. |
| Labels applied, record not marked `RECOVERED` | `RECONCILING` | Verifies remote head == `L`, PR open, labels correct ⇒ marks `RECOVERED` without re-writing GitHub. |
| Branch-absent variant, push landed | `RECONCILING` | The recorded expectation was "absent", but the branch now exists at `L` — allowed for this phase (that is our push), so reconcile. A branch at any other sha ⇒ `FAILED`. A *read failure* is `REMOTE_UNREADABLE` and retries; it never re-authorizes a branch-creating push. |

Every row converges on exactly one published head, one PR, one review routing, and
one terminal record — which is what the derived submission token, the canonical
`evidence_id`, the phase-aware allowed sets, and the fast-forward-only rule buy.

---

## 11. Implementation plan (#6914)

Ordered so each slice is independently shippable and leaves the tree green.

1. **Domain + store.** `domain/validated_work.py` (states, state sets,
   `ValidatedWorkIdentity`, `canonical_evidence_id`), `ports/validated_work_store.py`,
   `infra/validated_work_store.py` (+ sqlite registry entry). Pure unit tests,
   including the identity-stability cases from §9.
2. **Escrow + ref pinning + config.** Filesystem escrow with atomic rename and
   `reconcile_escrow_orphans()`; `WorkingCopy` extension for creating/resolving/
   deleting the `validated`/`observed` refs; **the whole §8.2(b) config slice ships
   here** — `ValidatedWorkConfig` model, section key, parser + registration, shape
   validation, `to_dict`, YAML round-trip, settings field + section, generated
   reference, example, and the config/settings tests. The retention sweep has a
   real setting to read on the same day it exists.
3. **Owner, admission-only.** `dispose_at_termination()` returning `NONE`/`PARKED`
   plus evidence capture and the §1.1 target-identity rules. Wire into
   `terminate_issue_runtime()` as a required parameter; add the fifth activity probe
   (`has_unresolved_work`) and make the reset call sites consume the result. At this
   point nothing recovers, but **nothing is destroyed** — scratch reset already
   stale-downgrades, including for `FAILED`.
4. **Publisher extraction + automatic recovery.** Extract
   `ValidatedHeadPublisher` (§4.4) from `PublishRecoveryService`'s execution half,
   including the existing-PR head comparison, and re-point manual `retry_publish()`
   at it with `UNCONSTRAINED` — a standalone, independently testable fix. Then the
   `QUEUED` → `PUBLISHING` → `RECOVERED` drain with the phase-aware check set and
   locator reconstruction. Route `STOPPED/MAX_ROUNDS_EXCEEDED` in (this is #7018).
5. **Classification cleanup.** Session/failure paths and the stuck sweep consume
   `IssueRuntimeTermination.validated_work`; generic `timed_out` becomes illegal for
   an issue with a disposition.
6. **Gated tech-lead op.** `recover_validated_work` through the existing
   `StoredTechLeadOp` lifecycle; config + settings schema; move out of
   `UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS` once the executor is wired.
7. **Operator commands + contracts.** View model, public contract regeneration, UI
   OpenAPI, the `recover-validated-work` and `abandon-validated-work` endpoints, and
   the dashboard actions (with the §8.4 accessibility requirements). `abandon()` is
   the last piece, deliberately: until it exists, unresolved work has no exit at all,
   which is the safe direction to be incomplete in.
8. **Backfill the stranded cohort.** #6327/#6335/#6337 (#6914) and #5204/#5561
   (#7011) admitted through the owner as `PARKED` records — via the same admission
   path, not a one-off script.

Slice 3 is the one that stops the bleeding; slices 4–8 recover what is already lost.
