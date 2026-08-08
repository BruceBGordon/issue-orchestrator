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

assert UNRESOLVED_STATES | RESOLVED_STATES == set(ValidatedWorkState)
assert not (UNRESOLVED_STATES & RESOLVED_STATES)

# There is deliberately no third "open" set. Uniqueness is not state-scoped:
# `record_id` is the table's primary key (§4.1), so there is at most ONE row per
# unit of work in every state. A state-scoped uniqueness rule is what let a rival
# row be admitted beside an unresolved FAILED one.


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


class ArtifactSlot(StrEnum):
    """The fixed vocabulary of escrowed artifacts. A slot, not a path."""
    COMPLETION        = "completion"
    VALIDATION        = "validation"
    EXCHANGE_SUMMARY  = "exchange_summary"


@dataclass(frozen=True, slots=True)
class AdmittedArtifact:
    """One admitted artifact. IDENTITY-BEARING — every field enters evidence_id.

    Deliberately carries NO path. The escrow filename is derived from the slot
    (``<evidence_dir>/<slot>.json``); the source path the bytes were admitted
    from is recorded in the observations for audit. A path that contains
    ``<evidence_id>`` cannot participate in the hash that produces
    ``<evidence_id>`` — the artifact's identity is its content, and its slot
    says what role that content plays.
    """
    slot: ArtifactSlot
    sha256: str             # lowercase hex
    byte_size: int


@dataclass(frozen=True, slots=True)
class ValidatedWorkKey:
    """WHICH WORK this is: one commit, on one branch, of one issue, in one repo.

    This is the natural key of the durable row (§4.1) and the unit the
    "at most one unresolved record" invariant is stated over.
    """
    repo_slug: str
    issue_number: int
    branch_name: str
    validated_head_sha: str              # == validation record head_sha (§1.1)

    @property
    def record_id(self) -> str:
        """Stable primary key. Independent of which evidence carries the work."""


@dataclass(frozen=True, slots=True)
class ValidatedWorkIdentity:
    """WHICH EVIDENCE this is: the key plus the admitted content that proves it.

    Every field is either part of the key or an admitted content hash. Nothing
    here can change between two captures of the same evidence, which is what
    makes ``evidence_id`` re-derivable after a crash (§2.1.1). In particular no
    observation of external or mutable state appears here — see the exclusion
    list below.
    """
    schema_version: int                  # bumped when the identity field set changes
    key: ValidatedWorkKey
    run_identity: SessionRunIdentity     # session_name, run_id, started_at
    completion_artifact: AdmittedArtifact
    validation_artifact: AdmittedArtifact
    exchange_summary_artifact: AdmittedArtifact | None
    requested_actions: tuple[RequestedAction, ...]
    review_disposition: ReviewDisposition
    exchange_terminal: ReviewExchangeTerminalState | None  # e.g. STOPPED/MAX_ROUNDS
    branch_binding_verified: bool        # False when HEAD was detached (#7017)


@dataclass(frozen=True, slots=True)
class ValidatedWorkObservations:
    """Everything read from mutable state. NONE of it enters evidence_id."""
    captured_at: str                     # ISO-8601 UTC
    worktree_head_sha: str               # issue worktree HEAD at capture; may move
    expected_remote_head_sha: str | None # remote branch head at capture; None = absent
    pr_number: int | None
    observed_blocking_labels: tuple[str, ...]  # exactly what this op may later clear
    admitted_from_paths: Mapping[ArtifactSlot, str]  # audit only, never re-read


@dataclass(frozen=True, slots=True)
class ValidatedWorkEvidence:
    """Identity plus the mutable observations a recovery reconciles against."""
    identity: ValidatedWorkIdentity
    observations: ValidatedWorkObservations

    @property
    def evidence_id(self) -> str:
        """Canonical identity hash. Depends on `identity` only."""
        return canonical_evidence_id(self.identity)

    @property
    def record_id(self) -> str:
        return self.identity.key.record_id
```

**`worktree_head_sha` is an observation, not identity.** It moves — that is the
entire premise of §1.1 — so hashing it would mean the same validated commit `V`
re-captured after the worktree advanced to `L` derives a *different* `evidence_id`,
which is precisely the crash-retry divergence §2.1.1 exists to prevent. It is
recorded (and pinned, §6) so the unvalidated work survives and the operator can see
it, and it is read **once**, at first capture, to choose the initial state (§5). It
has no role after that, because publication no longer reads any worktree HEAD: the
publisher pushes the pinned immutable object (§4.4). A worktree that moves after
capture therefore cannot change what gets published, which is why it does not need
to change the identity either.

#### 2.1.1 `evidence_id` must survive the crash it deduplicates

The whole idempotency story is that a repeated capture of the *same* work
re-derives the *same* ids and converges on the existing record. That only holds if
neither hash covers anything that moves between captures. **Two** ids are derived,
and the difference between them is what makes convergence structural:

| Id | Over | Answers | Changes when |
|---|---|---|---|
| `record_id` | `ValidatedWorkKey` | *which work* | never, for a given commit on a given branch |
| `evidence_id` | `ValidatedWorkIdentity` | *which evidence for that work* | the admitted artifacts, run, or routing differ |

Excluded from both, by construction:

- **Capture timestamps.** `captured_at` is when we looked, not what we found.
- **Mutable external observations.** `expected_remote_head_sha`, `pr_number`, and
  `observed_blocking_labels` are reconciliation *inputs* read fresh from GitHub;
  a label added by a human between two captures must not change any id.
- **Mutable local observations.** `worktree_head_sha` — see the note above.
- **Filesystem paths.** Artifacts are identified by `ArtifactSlot` + content hash,
  never by path. Escrow paths contain `<evidence_id>`, so a path inside the hash
  that produces `<evidence_id>` would be circular; `admitted_from_paths` keeps the
  source locations for audit, outside both hashes.

```python
IDENTITY_SCHEMA_VERSION = 1

def canonical_record_id(key: ValidatedWorkKey) -> str:
    return "r%d:%s" % (
        IDENTITY_SCHEMA_VERSION,
        hashlib.sha256(_canonical_json(key).encode("utf-8")).hexdigest(),
    )

def canonical_evidence_id(identity: ValidatedWorkIdentity) -> str:
    return "e%d:%s" % (
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
| `repo_slug` / `branch_name` | exact bytes, case-sensitive; no normalization (git refs are case-sensitive) |
| `requested_actions` | sorted by `.value`, duplicates removed — request order is not identity |
| `None` | JSON `null` (never omitted — an omitted key and a null key must not collide) |
| `bool`/`int` | native JSON |
| Nested dataclasses | ordered dict of their own fields, same rules recursively |

The `r1:`/`e1:` prefixes mean a future key- or identity-field change produces
visibly different ids instead of silently colliding with v1 rows.

**Reconciliation-observation refresh.** When a capture converges on an existing
record, the observations are refreshed **only while the record is in `QUEUED`,
`PARKED`, or `FAILED`** — all pre-submission, where a newer remote head is simply
better information. Once the record is `PUBLISHING`, its `expected_remote_head_sha`
is frozen: it is the compare-and-set baseline that both the phase-aware checks
(§4.3) and the push lease (§4.4) are bound to, and overwriting it mid-flight would
destroy the ability to tell "our push landed" from "someone else pushed".

#### 2.1.2 Atomic capture, and repair of a partial one

Capture writes three things that cannot be committed in one transaction. The order
is chosen so that every prefix is *repairable* and no prefix is *misleading*:

1. **Escrow** — the evidence directory is materialised in
   `<escrow_root>/.tmp/<evidence_id>.<pid>/`, fsynced (files, then the directory),
   then `os.replace`d onto `<escrow_root>/<issue>/<evidence_id>/`.
2. **Ref pin** — `refs/issue-orchestrator/validated/<issue>/<evidence_id>` (and
   `.../observed/...` when the worktree head differs).
3. **Record** — the transactional admission below.

**The escrow directory is self-describing.** Repairing a lost row requires more
than artifact bytes, so the directory holds a versioned **capture envelope**
alongside them:

```
<escrow_root>/<issue>/<evidence_id>/
    capture.json          # the envelope — fsynced inside the atomic rename
    completion.json       # ArtifactSlot.COMPLETION
    validation.json       # ArtifactSlot.VALIDATION
    exchange-summary.md   # ArtifactSlot.EXCHANGE_SUMMARY (optional)
```

`capture.json` carries, in one document: `envelope_version`, `record_id`,
`evidence_id`, the full `ValidatedWorkIdentity`, the full
`ValidatedWorkObservations` (remote expectation, PR number, observed blocking
labels, worktree head, admitted-from paths), the `initial_state` the owner chose
and why, the pinned ref names, and a `checksum` over the rest of the document. It
is **self-validating**: repair recomputes `canonical_evidence_id(identity)` and
requires it to equal the directory name, and recomputes each artifact's sha256 and
requires it to equal the envelope's. An envelope that fails either check is not
repaired — it is reported, never guessed at.

Steps 1 and 2 are content-addressed by `evidence_id`, so re-running them is a
no-op rather than a duplicate. A crash between any two steps leaves an *orphan*
escrow directory and/or ref with no row — inert, never admissible on its own, and
never confused with real state because admission reads the **row**, never the
directory.

`reconcile_escrow_orphans()` runs once per orchestrator start, on the owner's first
`drain()` — the same seam that already performs restart reconciliation (§3.4), so
no new scheduler is introduced. For each escrowed `evidence_id` with no row it
re-runs step 3 from the envelope, restoring the row whose insert was lost —
including the remote expectation, PR, labels and routing disposition that no amount
of artifact hashing could recover. It needs **no worktree**: everything it reads is
in the escrow directory and the pinned refs. It **never deletes**: an orphan it
cannot re-admit (issue closed, envelope invalid, ref missing) is left in place and
reported — alongside the store's registry entry in `infra/sqlite_registry.py` — by a
doctor check. Deleting escrow is the one thing this contract exists to prevent.

#### 2.1.3 Transactional admission: one unresolved row per work, always

Convergence cannot rest on `ON CONFLICT(evidence_id)` alone, because two *different*
evidences — a second completion record at the same commit, a re-run with a new
`run_id`, a different review disposition — legitimately produce different
`evidence_id`s for the same `ValidatedWorkKey`. Admission is therefore keyed on
`record_id` and runs in one `BEGIN IMMEDIATE` transaction:

```
BEGIN IMMEDIATE
  row := SELECT * FROM validated_work_records WHERE record_id = :record_id
  if row is NULL:
      INSERT the new row (state := initial_state, evidence_id := new)
      -> ADMITTED
  if row.evidence_id == :evidence_id:
      refresh observations iff row.state in (QUEUED, PARKED, FAILED)
      -> CONVERGED                      # the crash-retry case
  if row.state in RESOLVED_STATES:
      # RECOVERED: this work is already published. ABANDONED: reopen, because new
      # evidence for abandoned work is exactly the signal that made it recoverable.
      -> RETURN row (RECOVERED) | REOPEN as PARKED (ABANDONED)
  if row.state == PUBLISHING:
      -> ATTACHED                        # do not disturb an in-flight submission;
                                         # escrow is retained as an alternate
  # row is QUEUED / PARKED / FAILED with DIFFERENT evidence for the SAME work
  append row.evidence_id to row.superseded_evidence_ids
  row.evidence_id := :evidence_id ; row.state := initial_state ; refresh observations
  -> SUPERSEDED
COMMIT
```

Three consequences, each closing a hole a partial-index scheme leaves open:

- **A rival can never be minted beside an unresolved row.** `record_id` is the
  table's primary key, so "two open records for the same work" is not a rule the
  code has to remember — it is unrepresentable. This is what the §4.1 index tried
  and failed to express by excluding `FAILED`.
- **A `FAILED` row is always resolved by transitioning *that* row.** New evidence
  supersedes it in place (`FAILED → QUEUED/PARKED`), so a recovery can never leave
  the original failure unresolved-forever, still blocking reset.
- **Superseded evidence is retained, not discarded.** Every superseded
  `evidence_id` keeps its escrow directory and its pinned refs for the retention
  window, and is listed on the row. Superseding changes which evidence is *acted
  on*; it never deletes bytes.

`ATTACHED` is the one case that defers: a capture arriving while a submission is in
flight escrows itself and returns the in-flight disposition, and the next `drain()`
after the submission resolves re-runs admission — where it either converges (same
evidence) or supersedes (different evidence) against a row that is no longer
`PUBLISHING`.

**Operator handles resolve through the row.** The tech-lead op's
`target_evidence_id` and the Control Center actions name an `evidence_id`, which
resolves to its row through `ux_validated_work_evidence`. A handle naming
*superseded* evidence resolves to the same row and is **refused** with an explicit
"this evidence was superseded by `<evidence_id>`" message rather than silently
acting on the current evidence — an approval is consent to publish a specific
commit and a specific set of artifacts, and consent does not transfer.

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
        self, request: ValidatedWorkDispositionRequest, state: OrchestratorState
    ) -> ValidatedWorkDisposition: ...
    """Explicit initiator: approved tech-lead op or operator command."""

    def drain(self, state: OrchestratorState) -> tuple[ValidatedWorkDisposition, ...]: ...
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

`recover()` and `drain()` take `OrchestratorState` for the same reason
`PublishRecoveryService.retry_publish(issue_number, state)` does today: publication
ends in success finalization and review routing, which are control-layer policies
over live orchestrator state (§4.5). `dispose_at_termination()` deliberately does
**not** take it — capture and escrow touch no orchestrator state, which is what
lets the termination boundary call it while everything else is still being torn
down.

`ports/validated_work_store.py` (new) — durable state, section 4.
`ports/validated_head_publication.py` (new) — remote execution only, section 4.4.
`ports/published_work_finalization.py` (new) — labels + review routing, section 4.5.

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
    lambda: validated_work.has_unresolved_work(n),          # NEW — not optional
)
```

`validated_work` is a **required keyword parameter of `has_active_issue_runtime()`,
exactly as it is of `terminate_issue_runtime()`** — not `| None = None`. The two
boundaries are only safe because they read the same owner set; an owner that is
mandatory on one side and optional on the other is not the same set, it is the same
set *by convention*, and the convention is what a future caller forgets. An
optional probe would let a new activity call site pass every existing owner, get
`False`, and authorize a scratch reset over unresolved work without ever mentioning
the disposition owner. The parameter being required makes that a `TypeError` at
import time rather than data loss at 3am.

Concretely: `_ResetRetryRuntimeOwners` in
`entrypoints/web_retry_history_routes.py` gains a `validated_work` field resolved by
`_reset_retry_runtime_owners()`, so `has_active_reset_retry_runtime()` and
`_terminate_reset_retry_runtime()` — the tech-lead reset executor and the dashboard
reset — keep passing one identical owner set to both functions. The existing
fail-safe wrapper (`_owner_active_or_unverifiable`) still applies: a probe that
raises counts as active. §9's guardrail covers **every** call site of both
functions, not only termination.

That is the concrete mechanism by which the stuck sweep can no longer discard
validated work.

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

`entrypoints/bootstrap.py` constructs, in order:

```
SqliteValidatedWorkStore(state_dir/"validated_work.sqlite")
FilesystemValidatedWorkEscrow(state_dir/"validated-work")
GitValidatedHeadPublisher(working_copy, worktree_manager, repository_host)   # §4.4
PublishedWorkFinalizer(RetrySuccessFinalizer, RetryReviewRouting)            # §4.5
ValidatedWorkDispositionService(
    store, escrow, working_copy, worktree_manager, repository_host,
    publisher, finalizer, action_applier, label_manager, events,
)
```

**The disposition owner does not depend on `PublishRecoveryService`.** That is the
point of A1: the manual Retry Publish service is a *sibling* admission owner, not a
collaborator. Both it and the disposition owner are constructed here and both are
injected with the same two lower-level owners — the publisher and the finalizer —
so neither reaches through the other. `PublishedWorkFinalizer` wraps the existing
`RetrySuccessFinalizer`/`RetryReviewRouting` pair rather than reimplementing them,
which is what keeps one review-routing policy in the system.

The service is exposed on `control/orchestrator_deps.py` as
`validated_work: ValidatedWorkDispositionOwner`, mirroring how `publish_recovery` is
carried today. The store is registered in `infra/sqlite_registry.py` so doctor
checks, backups, and startup maintenance cover it (the precedent set by
`tech_lead_authority.sqlite`).

`drain(state)` is called from the same tick drain point that already calls
`PublishRecoveryService.drain_completed_retries()` and already holds
`OrchestratorState`, so restart reconciliation needs no new scheduler.

---

## 4. Durable state

### 4.1 Store schema — `validated_work.sqlite` in `<repo>/.issue-orchestrator/state/`

```sql
CREATE TABLE IF NOT EXISTS validated_work_records (
    record_id             TEXT PRIMARY KEY,           -- canonical ValidatedWorkKey
    evidence_id           TEXT NOT NULL,              -- currently admitted evidence
    superseded_evidence_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array, retained
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

-- The activity/reset probe's index. UNRESOLVED includes 'failed'.
CREATE INDEX IF NOT EXISTS ix_validated_work_unresolved
    ON validated_work_records (issue_number, state)
    WHERE state IN ('queued', 'parked', 'publishing', 'failed');

-- Evidence lookup for orphan repair and the tech-lead op's target_evidence_id.
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_evidence
    ON validated_work_records (evidence_id);

CREATE INDEX IF NOT EXISTS ix_validated_work_issue
    ON validated_work_records (issue_number, state);
```

Rows are **append-then-transition**, never deleted by normal operation.

**`record_id` as the primary key is the deduplication rule made unrepresentable to
violate.** The rule from #6914 — one recovery per *target repository + issue +
branch + validated HEAD* — is the definition of `ValidatedWorkKey`, so "two rows
for the same work" cannot exist at any state, in any order, under any race. A
partial unique index over a subset of states cannot say this: whichever states it
excludes become a hole through which a second row for the same work arrives, and
the excluded row then sits unresolved forever behind the rival that replaced it.
`evidence_id` moves to a plain unique column because it identifies *which evidence
the row currently carries*, and §2.1.3 defines exactly how it changes (supersede,
audited, escrow retained) rather than by minting a new row.

`ix_validated_work_unresolved` is the index behind `has_unresolved_work()`, and it
covers `failed` because a failed disposition is work that still exists (§3.2).

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
   `validated_head_sha`; the **publication workspace** (§4.4a — never the issue
   worktree) is bound to that same ref; and the command's `target_head_sha` equals
   it. All three, checked in the same drain step that submits. Any inequality ⇒
   `PUBLISH_TARGET_MISMATCH` (or `REF_PIN_LOST` when the ref is gone) ⇒ `FAILED`
   **before any external write**. Note that this check is a belt-and-braces
   assertion, not the primary defence: the publisher pushes the immutable object
   `target_head_sha` by explicit refspec, so no worktree HEAD — moving or
   otherwise — can change which commit is published.
5. `has_active_issue_runtime()` (excluding this owner's own probe) is false.
6. The row's `record_id` still matches the key being acted on and its
   `evidence_id` is still the one this drain admitted — i.e. no supersession
   (§2.1.3) landed underneath us. Re-read inside the same transaction that
   compare-and-sets the state; a mismatch aborts the drain step with no writes.

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
`validated_head_sha` must be a **descendant of `expected_remote_head_sha`**
(`merge-base --is-ancestor`) — of the *recorded expectation*, not of "whatever the
remote looked like just now". The distinction matters because that same expectation
value is bound into the push lease (§4.4b): proving descent from `E` and then
writing under a lease that requires the remote to *be* `E` makes the write a
fast-forward by construction, with no window in between where a third party's
commit could be silently accepted as the new baseline. Not a descendant ⇒
`REMOTE_DIVERGED` ⇒ `FAILED`. **Fast-forward only. Never force-push.** Skipped when
the branch is absent (nothing to fast-forward from) and when reconciling a landed
push (the remote already *is* the target).

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

So the design defines the narrow remote-execution port the disposition owner
actually needs, and composes finalization policy *above* it (§4.5) rather than
inside it.

#### 4.4a The publication workspace is never the issue worktree

The disposition owner **always** publishes from a dedicated per-evidence workspace
bound to the pinned validated ref:

```
git worktree add --detach \
    <state_dir>/validated-work/<issue>/<evidence_id>/workspace  <pinned_validated_ref>
```

Not "rehydrate if the original is gone" — *always*. The §1.1 exit that matters most
is the one where validation passed at `V` and the issue worktree is still sitting at
`L`: an operator or approved tech-lead op chooses to publish `V` as-is. If the
publisher's source were the issue worktree, that approved recovery would read HEAD
`L`, trip the target-identity check, and deterministically land in
`FAILED(PUBLISH_TARGET_MISMATCH)` — the contract would promise an exit it can never
take, and only in the case where recovery matters most.

Properties that make the dedicated workspace the right answer rather than a second
copy of the problem:

- It is **detached at the pinned ref**, so its HEAD *is* `validated_head_sha` by
  construction — not by a check that could have been forgotten.
- It is **detached**, so git never refuses it for "branch already checked out
  elsewhere", and it never competes with the issue worktree for `branch_name`.
- It is **outside the issue worktree**, so `reset_issue(from_scratch=True)` and
  agent activity cannot mutate or remove it, and it is never the working copy any
  agent was given.
- It is **per-evidence**, so a superseded evidence's workspace cannot be mistaken
  for the current one.
- It is disposable: removed when the row reaches a resolved state, recreated
  idempotently by the next drain if missing. Its absence is never a data-loss
  event, because the escrow and the pinned ref are the durable artifacts.

`L` stays exactly where it is — in the issue worktree and pinned at the `observed`
ref (§6) — preserved and unpublished.

#### 4.4b The remote write: exact object, exact expectation, one atomic step

Carrying `target_head_sha` in the command is not enough if the write still goes
through the generic push path. Today `git_working_copy` runs `fetch_for_push()` to
refresh tracking refs and then `build_push_args()` returns
`["push", "--force-with-lease", ...]` with **no explicit expectation**
(`execution/git_push_operations.py:143-156`). Bare `--force-with-lease` leases
against the tracking ref that the immediately preceding fetch just updated, so if a
third party moved the branch from `E` to `X` in the interim, the fetch adopts `X` as
the lease and the push happily replaces `X`. That is a force-push in everything but
name, and it contradicts both "a third sha means no write" and "never force-push".
It also pushes the *current branch HEAD of the worktree*, so a worktree that moved
after admission would publish a different commit than the one that was checked.

The disposition path therefore does **not** reuse that path. A new `WorkingCopy`
capability performs the write in one step:

```python
class ExactPushOutcome(StrEnum):
    PUSHED            = "pushed"             # remote ref now == target
    LEASE_REJECTED    = "lease_rejected"     # remote was NOT at the expectation
    NOT_FAST_FORWARD  = "not_fast_forward"
    AUTH_FAILED       = "auth_failed"
    TRANSIENT         = "transient"          # network/5xx; retry, no state change

def push_exact(
    *, remote: str, branch: str, target_sha: str, expected_sha: str | None
) -> ExactPushResult: ...
```

implemented as a single git invocation:

```
git push --atomic <remote> \
    --force-with-lease=refs/heads/<branch>:<expected_sha_or_empty> \
    <target_sha>:refs/heads/<branch>
```

Three properties, each closing one hole:

- **The object is explicit.** `<target_sha>:refs/heads/<branch>` publishes an
  immutable commit id. No worktree HEAD is consulted, so a worktree that advances
  between admission and push cannot change what is published. (The §4.4a workspace
  is still bound to the ref — it is where run assets live and a defence in depth —
  but it is no longer load-bearing for *which commit* ships.)
- **The expectation is explicit.** The `:<expected_sha>` form of
  `--force-with-lease` compares against the value we pass, **not** against a
  tracking ref, so no fetch can refresh the lease out from under the check. The
  publisher must not call `fetch_for_push()` on this path; the absence of that call
  is the fix, and §9 guards it.
- **Absent-branch is expressible.** An empty expectation
  (`--force-with-lease=refs/heads/<branch>:`) requires the ref not to exist, so
  "create this branch, and only if nobody else did" is the same atomic primitive
  rather than a check-then-push.

Combined with §4.3 check 8 — `target` proven a descendant of the *same*
`expected_sha` — a lease-matching write is necessarily a fast-forward. Divergence
and interference both surface as `LEASE_REJECTED`/`NOT_FAST_FORWARD` with **zero
remote writes**, which the owner maps to `REMOTE_HEAD_CHANGED`/`REMOTE_DIVERGED`.

#### 4.4c The port

`ports/validated_head_publication.py` — remote execution only. No labels, no review
routing, no `OrchestratorState`, no `PublishRetryLocators`:

```python
class RemoteHeadExpectation(StrEnum):
    EXACT         = "exact"          # remote must be at expected_remote_head_sha
    ABSENT        = "absent"         # branch must not exist
    UNCONSTRAINED = "unconstrained"  # manual retry: no captured expectation


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadCommand:
    """Execution only. Carries no admission policy and makes no board decisions."""
    issue_number: int
    repo_slug: str
    branch_name: str
    target_head_sha: str                  # the immutable commit to publish
    expectation: RemoteHeadExpectation
    expected_remote_head_sha: str | None  # required iff expectation is EXACT
    source_workspace: Path                # §4.4a; holds the objects, not the target
    pr_number: int | None
    pr_base_branch: str
    submission_token: str                 # idempotency key; see below


class PublishValidatedHeadStatus(StrEnum):
    SUBMITTED         = "submitted"           # write in flight; poll by token
    PUBLISHED         = "published"           # remote ref is now at target
    ALREADY_AT_TARGET = "already_at_target"   # remote ref was ALREADY at target
    REJECTED          = "rejected"            # preconditions unmet; nothing written
    DIVERGED          = "diverged"            # lease/FF refused; nothing written


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadOutcome:
    status: PublishValidatedHeadStatus
    submission_token: str
    observed_remote_head_sha: str | None
    pr_number: int | None                 # the ONE PR for this branch, ensured
    pr_url: str | None
    pr_head_sha: str | None
    failure: ValidatedWorkFailure | None
    message: str


class ValidatedHeadPublisher(Protocol):
    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome: ...
    """Branch write (§4.4b) followed by PR ensure/reconcile. Idempotent."""

    def submission_status(self, token: str) -> PublishSubmissionStatus: ...
    """Durable, owner-observable: PENDING | SUCCEEDED | FAILED | UNKNOWN."""
```

**The branch decision** (which `retry_publish()` does not make today):

| Observed branch head | Verdict |
|---|---|
| == `expected_remote_head_sha`, or absent under `ABSENT` | **push** the exact object under the exact lease |
| == `target_head_sha` | **`ALREADY_AT_TARGET`** — our push landed; no second write |
| anything else | **`DIVERGED`** — no write |
| `UNCONSTRAINED` (manual retry) | push when `target_head_sha` descends from the observed head; `DIVERGED` otherwise |

Crucially, the branch decision does **not** consult the PR. "An open PR exists" is
never a reason to skip the push, and `ALREADY_AT_TARGET` is decided on the branch
ref alone — which is what makes the crash-after-push-before-PR state (§4.4d)
recognisable instead of an unreachable gap.

#### 4.4d PR ensure/reconcile — one PR, idempotently

After the branch is confirmed at `target_head_sha`, the publisher ensures exactly
one PR for it:

| Observed | Action |
|---|---|
| `pr_number` set, PR open, head ref == `branch_name` | reconcile: report its head; no create |
| `pr_number` set, PR closed or merged | `PR_CLOSED_OR_MERGED` ⇒ no write |
| `pr_number` set, head ref != `branch_name` | `PR_BRANCH_MISMATCH` ⇒ no write |
| `pr_number` **None**, an open PR exists for `branch_name` | adopt it — do not create a second |
| `pr_number` **None**, no open PR for `branch_name` | **create one**, then persist its number before anything else |

The fourth and fifth rows are the crash the previous draft had no state for: the
process dies after the push and before PR creation, leaving branch at `L` with
`pr_number=None` and no PR. Restart re-enters `RECONCILING`, the branch decision
returns `ALREADY_AT_TARGET` **without requiring a PR**, and PR-ensure discovers or
creates the single expected PR. Discovery uses the existing active-issue-branch
scoping (`scope_prs_to_active_issue_branch`) so a prior-attempt PR is never adopted.
Creation is keyed by the orchestrator body marker, so a crash *between* creation and
persisting the number is repaired by discovery on the next drain rather than by
opening a duplicate.

#### 4.4e Submission identity and durable ordering

**`submission_token` is derived, not generated:**
`token = "vwd:" + evidence_id + ":" + target_head_sha`. A crash-and-retry re-derives
the same token, so `submission_status()` finds the prior submission instead of
starting a rival one.

**The record reaches `PUBLISHING` before the executor is invoked, not after.** A
synchronous call cannot promise "the transition happens before the write is
observable" — the callee may push before returning, and a crash inside the call
would leave a `QUEUED` row with a completed push. Because the token is *derived*,
there is nothing to wait for the executor to return, so the order is simply:

```
1. store.begin_publishing(record_id, expected_state in {QUEUED, PARKED},
                          token, expected_remote_head)   # durable CAS; false -> abort
2. publisher.publish_or_reconcile(command)               # only if the CAS won
```

The CAS is also the concurrency guard: two drains racing the same row produce one
winner, and the loser does nothing.

**Every `submission_status` value has a defined transition** — the owner never has
an unhandled state:

| `submission_status(token)` | Owner does |
|---|---|
| `PENDING` | stay `PUBLISHING`, no writes, re-check next drain. Past `publish_submission_timeout` (bounded, config-free — derived from the existing publish job timeout), treat as `UNKNOWN`. |
| `SUCCEEDED` | re-read the branch; require it at `target_head_sha`; ensure the PR (§4.4d); finalize (§4.5); mark `RECOVERED`. A `SUCCEEDED` token whose branch is *not* at target is a hard `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| `FAILED` — transient (`TRANSIENT`, `AUTH_FAILED` on a rotatable token, `REMOTE_UNREADABLE`, issue unreadable) | stay `PUBLISHING`, no state change, retry next drain, up to a bounded attempt count; exhaustion ⇒ durable `FAILED` (which is *unresolved*, so nothing is lost). |
| `FAILED` — definitive (`LEASE_REJECTED`, `NOT_FAST_FORWARD`, `PR_CLOSED_OR_MERGED`, `PR_BRANCH_MISMATCH`, `ARTIFACT_*`) | durable `FAILED` with the mapped `ValidatedWorkFailure`. |
| `UNKNOWN` | re-run §4.3 in `RECONCILING` and act on the allowed-state tables — never resubmit blind. |

Transient and definitive are distinguished by an explicit mapping, not by a
substring match on an error string: only the enumerated `ExactPushOutcome` values
and typed host errors reach this table.

### 4.5 The finalization boundary — `PublishedWorkFinalizer`

Labels and review routing are **control-layer policy over live orchestrator state**,
not remote execution. `RetrySuccessFinalizer.finalize()` already requires
`OrchestratorState` (`control/publish_retry_finalize.py:73-83`), so folding it into
the publisher would force either a hidden global or a back-reference from the
publisher to `PublishRecoveryService` — reintroducing exactly the cross-owner
coupling A1 exists to remove. It stays a separate, explicitly composed port:

```python
@dataclass(frozen=True, slots=True)
class PublishedWorkFinalizationRequest:
    state: OrchestratorState              # the caller supplies it; no hidden state
    issue_number: int
    issue_title: str
    agent_label: str | None
    branch_name: str
    pr_number: int
    pr_url: str
    published_head_sha: str
    review_disposition: ReviewDisposition  # -> skip_review / exchange completed|halted
    history_reason: str
    worktree_path: str | None


class PublishedWorkFinalizer(Protocol):
    def finalize(
        self, request: PublishedWorkFinalizationRequest
    ) -> FinalizationOutcome: ...
```

The implementation wraps the existing `RetrySuccessFinalizer` + `RetryReviewRouting`
pair unchanged — one review-routing policy in the system, reached by two admission
owners. `FreshIssueReadError` surfaces as a typed transient outcome (it already does
in `retry_publish()`), which the disposition owner treats as retry-next-drain rather
than `FAILED`.

### 4.6 Composition, and who owns what

| Layer | Owner | Responsibility | Needs `OrchestratorState`? |
|---|---|---|---|
| Admission (manual) | `PublishRecoveryService` | `_retry_decision()`, board/locator gates, `PublishRetryLocators` | yes (already) |
| Admission (validated work) | `ValidatedWorkDispositionService` | evidence admission, escrow/refs, §4.3 checks, state machine | yes, via `drain(state)`/`recover(request, state)` |
| Remote execution | `ValidatedHeadPublisher` | exact-object/exact-lease branch write, PR ensure | **no** |
| Finalization | `PublishedWorkFinalizer` | labels, history, review routing | yes, carried on the request |

Each row is one implementable responsibility, and neither admission owner depends
on the other.

**`PublishRetryLocators` stays entirely with the manual path.** The disposition
owner does not synthesise them: it has its own escrowed artifacts, its own
workspace, and its own publisher and finalizer, so the locator round-trip bought it
nothing but a dependency on another owner's admission preconditions. Dropping it
also removes the ugliest step of the previous draft — adding a `publish-failed`
label the issue never earned, purely to satisfy `board_block_reason()` on a path
that no longer runs (§7 loses that transition).

Construction of `PublishRetryLocators` is centralised in a named
`PublishRetryLocatorFactory` owned by `PublishRecoveryService`; the guardrail (§9)
is that **no other module constructs them**, which the manual path satisfies by
construction and the disposition path satisfies by not needing them.

**Handoff performed by the disposition owner:**

1. Create or refresh the §4.4a publication workspace at the pinned validated ref;
   restore the escrowed completion and validation records into its run directory.
2. Run §4.3 in `PRE_SUBMISSION` (or `RECONCILING` for a row already `PUBLISHING`).
3. `store.begin_publishing(...)` — durable CAS to `PUBLISHING` with the derived
   token. Abort silently if it loses.
4. `publisher.publish_or_reconcile(command)` — exact write, then PR ensure.
5. `finalizer.finalize(request)` with the live state — labels, history, review
   routing.
6. Mark `RECOVERED`; remove the workspace; retain escrow and refs for the window.

No other caller may call the publisher with a disposition token. This is enforced
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

This comparison is made **once, at capture**, to choose the initial state. It is
not a standing precondition, because publication does not read the issue worktree
at all: the publisher pushes the pinned immutable object from the dedicated
workspace (§4.4a/§4.4b). A worktree that advances *after* capture therefore changes
nothing about what can be published — which is also why `worktree_head_sha` is an
observation rather than part of the evidence identity (§2.1).

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
| Escrowed capture envelope + completion/validation/exchange-summary copies | `<state_dir>/validated-work/<issue>/<evidence_id>/` | escrow retention sweep only |
| Superseded evidence (§2.1.3) | its own `<evidence_id>/` directory and refs | same window, measured from the row's `terminal_at` — superseding never deletes |
| Publication workspace (§4.4a) | `<state_dir>/validated-work/<issue>/<evidence_id>/workspace/` | removed on a resolved row; recreated idempotently from the pinned ref, so its loss is never a data-loss event |
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
- Worktree maintenance (`git worktree prune`, issue-worktree cleanup, the stale
  worktree sweep) must skip the escrow root. The publication workspace is
  registered with git like any other worktree, so a blanket prune would remove it
  mid-publish; it is recoverable, but a prune racing a push is a failure mode worth
  not having.
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
| Admission to publish (`PUBLISHING`) | durable CAS to `PUBLISHING` succeeded | **no label change.** The previous draft added `publish-failed` here purely to satisfy the manual path's `board_block_reason()` precondition; §4.6 removes that round-trip, so the issue is no longer marked with a failure it did not have. |
| Publication + review routing succeeded (`RECOVERED`) | remote head == `validated_head_sha`, PR open, review routed | remove `recovery-pending`; remove **only** the labels in `observed_blocking_labels`. Then normal routing applies `pr-pending`/review labels via the finalizer (§4.5). |
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
| §4.4a workspace | **The original worktree still exists at `L`** and an approved recovery chooses `V`: the publication workspace is a distinct path detached at the pinned ref, the pushed object is exactly `V`, the issue worktree is neither moved nor removed, and `L` remains pinned at the `observed` ref and unpublished. |
| §4.4b TOCTOU | The worktree advances from `V` to `L` *after* admission and before the push: the published object is still `V` (the refspec names the object, not a branch HEAD). |
| §4.4b TOCTOU | The remote moves from `E` to `X` after observation and before the push: `LEASE_REJECTED` ⇒ `REMOTE_HEAD_CHANGED`, **zero remote writes**, and `X` is still the remote head afterwards. Asserted against a fake that records every git invocation, so a bare `--force-with-lease` or a preceding `fetch_for_push()` fails the test. |
| §4.4b absent branch | Expectation `ABSENT` with the branch created by a third party between observation and push: empty-lease rejection, zero writes. |
| §4.4 publisher | Open PR *P* at remote head `R`, target `L`: the publisher **pushes** and does not take an existing-PR shortcut; end state has remote head `L`. |
| §4.4 publisher | Remote branch already at `L`: `ALREADY_AT_TARGET`, **no second push**, decided on the branch ref alone (no PR consulted). |
| §4.4 publisher | Remote head is a third sha `X`: `DIVERGED`, no push, no PR write, no label writes. |
| §4.4d PR ensure | Branch at `L`, `pr_number=None`, **no PR** (crashed after push, before create): restart creates exactly one PR, routes review, and does not re-push. |
| §4.4d PR ensure | Branch at `L`, `pr_number=None`, an open PR for the branch already exists (crashed after create, before persisting the number): it is adopted, not duplicated; a prior-attempt PR on another branch is not adopted. |
| §4.4e ordering | The record is `PUBLISHING` with the derived token **before** the publisher is invoked — asserted by a publisher double that reads the store when called. A crash inside the publisher leaves `PUBLISHING`, never `QUEUED`-with-a-completed-push. |
| §4.4e ordering | Two concurrent drains on one row: exactly one `begin_publishing` CAS wins and exactly one submission is made. |
| §4.4e statuses | One test per `submission_status` value — `PENDING` (no writes, re-check), `SUCCEEDED` (branch verified, PR ensured, `RECOVERED`), `SUCCEEDED` with the branch *not* at target (`FAILED`), transient `FAILED` (stays `PUBLISHING`, bounded retries, exhaustion ⇒ `FAILED`), definitive `FAILED` (durable `FAILED`), `UNKNOWN` (re-runs `RECONCILING`). |
| §2.1.1 identity | Capture the same work twice at different `captured_at`, with different remote heads, after a human adds a blocking label, **and after the worktree advances**: identical `record_id` *and* `evidence_id`, exactly one row. |
| §2.1.1 identity | Requested actions supplied in a different order produce the same id; a different completion artifact hash produces a different `evidence_id` but the **same** `record_id`. |
| §2.1.3 admission | Different evidence for the same key supersedes in place: one row, prior `evidence_id` in `superseded_evidence_ids`, both escrows and both ref sets retained. Superseding a `FAILED` row transitions *that* row; no rival row exists at any point. Recovery against a superseded-from-`FAILED` key leaves nothing unresolved. |
| §2.1.3 admission | A capture arriving while the row is `PUBLISHING` returns `ATTACHED` without disturbing the submission, and the next drain converges or supersedes. |
| §2.1.2 atomicity | Crash after escrow rename, after ref pin, and after insert; startup `reconcile_escrow_orphans()` rebuilds the row from `capture.json` — **with the original worktree deleted** — restoring remote expectation, PR, labels and routing, and deletes nothing. An envelope whose recomputed `evidence_id` or artifact hash mismatches is reported, not repaired. |
| §2.1.2 atomicity | Re-capture converging on a `RECOVERED` id returns that record and inserts nothing; on an `ABANDONED` id it reopens as `PARKED`. |
| §4.3 phases | One test per crash row in §10, asserting the allowed-state table: `RECONCILING` accepts remote at `expected` **and** remote at `validated_head_sha`; both branch-absent variants; a third sha fails in both phases. |
| §4.3 check 8 | Fast-forward legality is proven against the **recorded expectation**, not a freshly observed head: a remote that moved to a descendant of `E` after observation still yields zero writes. |
| §3.2 / F5 | Scratch-reset freshness for **every** state, with `FAILED` asserted reset-**ineligible**, and `ABANDONED` asserted eligible only after a recorded `OperatorResolution`. |
| §3.2 / F5 | `has_active_issue_runtime()` cannot be called without the disposition owner (required parameter), and `_ResetRetryRuntimeOwners` carries it into both the freshness check and the teardown. |
| §4.5 finalization | The disposition path reaches `RetrySuccessFinalizer`/`RetryReviewRouting` through `PublishedWorkFinalizer` with the live state on the request; a `FreshIssueReadError` is a retry-next-drain transient, not `FAILED`. |
| §6 retention | The retention sweep releases escrow/refs only for `RESOLVED_STATES` past the window, never for a `FAILED` record regardless of age, and never for superseded evidence of an unresolved row. |
| §8.2 config | `validated_work.escrow_retention_days` parses from YAML, rejects `0`/non-int, survives a `to_dict`/`to_yaml_dict` round-trip, is omitted at default, and its `config_attr` resolves against a real `Config`. |

**Non-regression**: Retry Publish, `request_rework` (#7008), reset-from-scratch, and
kill-session behaviour unchanged apart from the head-comparison fix above; scratch
reset now stale-downgrades while unresolved work exists.

**Mechanical guardrails** (ADR-0012), added to the existing AST guardrail suite:

- `PublishRetryLocators` is constructed only by `PublishRetryLocatorFactory`
  (§4.6). The disposition owner never constructs or stores them.
- No module outside the owner adds/removes `recovery-pending`.
- `IssueRuntimeTermination` cannot be constructed without `validated_work`
  (enforced by the type, verified by a test).
- Every `terminate_issue_runtime()` **and** `has_active_issue_runtime()` call site
  passes the disposition owner, and every `terminate_issue_runtime()` call site
  **binds** the returned `validated_work` — a discarded result, or an activity
  check that skipped the owner, is the F5 failure mode in miniature.
- Only `ValidatedWorkDispositionService` and `PublishRecoveryService` call
  `ValidatedHeadPublisher.publish_or_reconcile`.
- The disposition publish path never calls `fetch_for_push()` or
  `build_push_args()`; its only remote write is `push_exact()` (§4.4b). A guardrail
  over the publisher module keeps the bare-`--force-with-lease` path from creeping
  back in.
- `ValidatedWorkDispositionService` does not reference `PublishRecoveryService` —
  the two admission owners are siblings, not a chain.

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
| Escrow | `capture.json` + completion + validation records written to `<state_dir>/validated-work/N/<evidence_id>/` (temp dir, fsync, atomic rename); `L` pinned at `refs/issue-orchestrator/validated/N/<evidence_id>`. No `observed` ref — the two heads agree. |
| Admission | `record_id` derived from (repo, N, `b`, `L`); no existing row ⇒ `ADMITTED`. |
| Disposition | Evidence conclusive ⇒ `QUEUED`. `recovery-pending` added; `blocked-failed` kept. `IssueRuntimeTermination.validated_work.state == QUEUED`, so the session is **not** classified `timed_out`. |
| Drain | Publication workspace created detached at the pinned ref. §4.3 in `PRE_SUBMISSION`: remote head is `R` as expected, PR *P*'s head is `R`, pinned ref == workspace HEAD == `L`, and `L` descends from the **recorded expectation** `R` ⇒ fast-forward legal. `begin_publishing()` CAS to `PUBLISHING` with the derived token, **then** `publish_or_reconcile(target_head_sha=L, expectation=EXACT(R))`. |
| Publish | The publisher sees the branch at `R` — the expectation, **not** the target — so it writes: `git push --atomic origin --force-with-lease=refs/heads/b:R L:refs/heads/b`. The object published is `L` itself, and the lease guarantees the remote was still at `R`. PR *P* is then ensured: it is open on `b`, so it is reconciled, and its head is now `L`. This is the row the old contract got wrong twice — `retry_publish()` would have found an open PR for `b` and finalized without pushing, and a bare `--force-with-lease` would have re-leased to whatever the preceding fetch saw. No new PR, no supersede, no force-push, no reset. |
| Review | `PublishedWorkFinalizer` composes `RetrySuccessFinalizer` + `RetryReviewRouting` with the live state — review resumes on `L` through normal discovery. No approval label is applied, so there is no false ready-to-merge state. |
| Finalize | `recovery-pending` and `blocked-failed` removed (no `publish-failed` was ever added); `pr-pending` applied by the finalizer; record `RECOVERED`; workspace removed; escrow + ref retained for `escrow_retention_days`. |
| Divergence variant | If `L` were **not** a descendant of `R`, step 6 of §4.3 fails ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`, artifacts preserved, `tech-lead-needs-human` added. A human resolves the divergence; nothing is force-pushed and nothing is deleted. |

**Crash points around the non-atomic GitHub writes:**

The phase column is load-bearing: every row after submission runs §4.3 in
`RECONCILING`, where **both** `R` and `L` are allowed remote states. That is what
makes a successful-but-unacknowledged push a recoverable state rather than a
`REMOTE_HEAD_CHANGED` failure.

| Crash after | Phase on restart | On restart, `drain()` does |
|---|---|---|
| Nothing (before the row is written) | — | Re-derives the same `record_id` and `evidence_id` (§2.1.1 — `captured_at`, the observed remote head, and the worktree head are all outside the hashes) at the next termination and converges on one row. |
| Partial escrow write | — | The temp directory was never renamed ⇒ no admissible escrow ⇒ re-escrow from the worktree if present, else `FAILED(ESCROW_WRITE_FAILED)`. Never a half-admitted row. |
| Escrow renamed / ref pinned, insert lost | — | `reconcile_escrow_orphans()` rebuilds the row from `capture.json` — identity, observations, initial state and ref names all persisted in the envelope — after validating that the recomputed `evidence_id` and artifact hashes match. Works with the worktree already deleted. Nothing is deleted; the orphan is inert until then because admission reads rows, not directories. |
| Row `QUEUED`, before the publishing CAS | `PRE_SUBMISSION` | Re-runs the checks and re-admits. Remote is still at `R`. Idempotent. |
| CAS to `PUBLISHING` won, before the publisher was invoked | `RECONCILING` | `submission_status(token)` is `UNKNOWN`; remote is still at `R` ⇒ allowed ⇒ publish with the **same derived token**. This state exists *because* the CAS precedes the call (§4.4e) — the alternative ordering produces a `QUEUED` row with a completed push, which no phase can safely interpret. |
| Push landed, process died before the outcome was recorded | `RECONCILING` | `submission_status(token)` is `UNKNOWN`. Remote at `L` ⇒ allowed ⇒ `ALREADY_AT_TARGET`, reconcile without pushing. Remote at `R` ⇒ allowed ⇒ publish. Any third sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| **Push succeeded, no PR created** (`pr_number` is `None`, no PR exists) | `RECONCILING` | The branch decision returns `ALREADY_AT_TARGET` on the **branch ref alone**, so the missing PR is not a gap in the table. PR-ensure (§4.4d) finds no open PR for `b`, creates exactly one, persists its number, then routes review. No re-push. |
| PR created, its number not yet persisted | `RECONCILING` | PR-ensure discovers the open PR for `b` (scoped to the active issue branch, matched by the orchestrator body marker) and adopts it. No duplicate PR. |
| Push succeeded, PR reconcile/link failed | `RECONCILING` | Remote head and PR head are `L` — an **allowed** state for this phase, not a divergence. `ALREADY_AT_TARGET`, then finalize; no second push. |
| PR reconciled, labels not applied | `RECONCILING` | Label add/remove through `ActionApplier` are idempotent; re-applies the §7 transition. |
| Labels applied, row not marked `RECOVERED` | `RECONCILING` | Verifies remote head == `L`, PR open, labels correct ⇒ marks `RECOVERED` without re-writing GitHub. |
| Branch-absent variant, push landed | `RECONCILING` | The recorded expectation was "absent", but the branch now exists at `L` — allowed for this phase (that is our push), so reconcile. A branch at any other sha ⇒ `FAILED`. A *read failure* is `REMOTE_UNREADABLE` and retries; it never re-authorizes a branch-creating push. |
| Anywhere, with the publication workspace lost | either | Recreated idempotently from the pinned ref at the top of the next drain (§4.4a). The workspace holds no unique state. |

Every row converges on exactly one published head, one PR, one review routing, and
one terminal row — which is what the derived submission token, the `record_id`
primary key, the self-describing capture envelope, the CAS-before-invoke ordering,
the phase-aware allowed sets, and the exact-object/exact-lease write buy.

---

## 11. Implementation plan (#6914)

Ordered so each slice is independently shippable and leaves the tree green.

1. **Domain + store.** `domain/validated_work.py` (states, state sets,
   `ValidatedWorkKey`, `ValidatedWorkIdentity`, `canonical_record_id`,
   `canonical_evidence_id`), `ports/validated_work_store.py`,
   `infra/validated_work_store.py` with the `record_id`-keyed table and the §2.1.3
   transactional admission (+ sqlite registry entry). Pure unit tests, including the
   identity-stability and supersession cases from §9.
2. **Escrow + ref pinning + config.** Filesystem escrow with the capture envelope,
   atomic rename, and `reconcile_escrow_orphans()`; `WorkingCopy` extensions for the
   `validated`/`observed` refs **and `push_exact()`** (§4.4b); **the whole §8.2(b)
   config slice ships here** — `ValidatedWorkConfig` model, section key, parser +
   registration, shape validation, `to_dict`, YAML round-trip, settings field +
   section, generated reference, example, and the config/settings tests. The
   retention sweep has a real setting to read on the same day it exists.
3. **Owner, admission-only.** `dispose_at_termination()` returning `NONE`/`PARKED`
   plus evidence capture and the §1.1 target-identity rules. Wire into
   `terminate_issue_runtime()` **and** `has_active_issue_runtime()` as a required
   parameter; add the fifth activity probe (`has_unresolved_work`) and make the
   reset call sites consume the result. At this point nothing recovers, but
   **nothing is destroyed** — scratch reset already stale-downgrades, including for
   `FAILED`.
4. **Publisher + finalizer, then automatic recovery.** Introduce
   `ValidatedHeadPublisher` (§4.4c) over `push_exact()` plus PR ensure, and
   `PublishedWorkFinalizer` (§4.5) wrapping the existing
   `RetrySuccessFinalizer`/`RetryReviewRouting`; re-point manual `retry_publish()`
   at both with `UNCONSTRAINED`, which independently fixes its existing-PR shortcut
   and is shippable on its own. Then the publication workspace, the
   `begin_publishing` CAS, and the `QUEUED` → `PUBLISHING` → `RECOVERED` drain with
   the phase-aware check set. Route `STOPPED/MAX_ROUNDS_EXCEEDED` in (this is #7018).
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
