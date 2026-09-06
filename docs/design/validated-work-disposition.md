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

"Completed and validated" means, and only means, orchestrator-owned evidence
registered by §5's required intake/validator producer, not mere path ownership:

- an intake-attested completion record the orchestrator itself preserved (the run-scoped copy under
  `SessionRunAssets.run_dir`, or a record referenced by the run manifest) that
  parses as `CompletionOutcome.COMPLETED` and requests `PUSH_BRANCH`/`CREATE_PR`;
- a validator-attested validation record that record points at, with `passed=true`, carrying its own
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

### 1.2 Why the existing owner, extended

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
    QUEUED     = "queued"     # recovery admitted automatically; drain will execute
    PARKED     = "parked"     # durable, awaiting approval (gated op or operator)
    PUBLISHING = "publishing" # admitted to the publisher; submission in flight
    RECOVERED  = "recovered"  # published + review routed; resolved
    FAILED     = "failed"     # fail-closed; artifacts preserved; UNRESOLVED
    ABANDONED  = "abandoned"  # operator explicitly accepted the loss; resolved


# Two DIFFERENT state sets. Conflating "resting" with "resolved" is the bug.

UNRESOLVED_STATES = frozenset({          # work still exists and is not safe to lose
    ValidatedWorkState.QUEUED,
    ValidatedWorkState.PARKED,
    ValidatedWorkState.PUBLISHING,
    ValidatedWorkState.FAILED,           # <- FAILED IS UNRESOLVED, not "terminal"
})

RESOLVED_STATES = frozenset({            # nothing is at risk; reset/teardown may proceed
    ValidatedWorkState.RECOVERED,
    ValidatedWorkState.ABANDONED,
})

# There is deliberately NO "none" state. "No completed+validated work at this edge"
# is not a property of a record — it is the ABSENCE of records, which an empty
# ValidatedWorkDispositionBatch says directly (§2.2). A `NONE` member would be a
# disposition of nothing, carrying no record_id and no key, and every consumer
# would have to special-case it out of a collection whose whole purpose is that
# each member names a unit of work. Every state here is persisted; every
# disposition names a row.
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
    ANCESTOR_OF_PENDING_HEAD      = "ancestor_of_pending_head"  # §2.1.4; parks behind a descendant
    DIVERGENT_VALIDATED_HEADS     = "divergent_validated_heads" # §2.1.4; parks for a choice
    AWAITING_LINEAGE_PREDECESSOR  = "awaiting_lineage_predecessor"  # §2.1.4; parks behind PUBLISHING
    REMOTE_BASELINE_UNPROVEN      = "remote_baseline_unproven"  # §2.1.4; parks rather than guess
    AUTHORITY_SNAPSHOT_STALE      = "authority_snapshot_stale"  # §8.1; approved facts moved
    DUPLICATE_OPEN_PR             = "duplicate_open_pr"         # §4.4d; two marked PRs, never guess
    PUBLISHED_HEAD_LACKS_VALIDATED_WORK = "published_head_lacks_validated_work"  # §3.5; parks
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


class ResolutionKind(StrEnum):
    """HOW a record became resolved. Recorded on the row; never inferred later."""
    PUBLISHED                    = "published"      # this record's own publication
    CONTAINED_IN_PUBLISHED_HEAD  = "contained_in_published_head"   # §2.1.4 / §3.5
    OPERATOR_ABANDONED           = "operator_abandoned"


class LineageRole(StrEnum):
    """This record's position among the validated heads of one issue+branch (§2.1.4)."""
    HEAD      = "head"       # the only drainable role
    ANCESTOR  = "ancestor"   # contained by a HEAD; parks, resolves when the HEAD lands
    DIVERGENT = "divergent"  # not comparable with the others; parks for an explicit choice
    PENDING   = "pending"    # admitted behind a PUBLISHING sibling; classified when it resolves


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
        return canonical_record_id(self)


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
    reviewer_proof_digest: str | None    # required for EXCHANGE_APPROVED (§5)


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

**Reconciliation-observation refresh, and the revision that makes it auditable.**
When a capture converges on an existing record, the observations are refreshed
**only while the record is in `QUEUED`, `PARKED`, or `FAILED`** — all
pre-submission, where a newer remote head is simply better information. Once the
record is `PUBLISHING`, its `expected_remote_head_sha` is frozen: it is the
compare-and-set baseline that both the phase-aware checks (§4.3) and the push lease
(§4.4) are bound to, and overwriting it mid-flight would destroy the ability to tell
"our push landed" from "someone else pushed".

Every refresh increments `observation_revision` on the evidence row, in the same
transaction. Deliberately keeping mutable observations *out* of `evidence_id` is
what makes the id crash-stable — but it also means an `evidence_id` alone cannot
say **which** PR and **which** remote baseline it currently carries. An approval is
consent to publish a specific commit onto a specific remote state, so §8.1 binds
`(evidence_id, observation_revision)` and the observed facts into the immutable
approval, and execution requires exact equality before it will act. The revision is
the cheap, monotonic way to detect that the facts moved under a standing approval;
it is authorization bookkeeping, never an input to identity.

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
no new scheduler is introduced. It covers the crash case **only**: an attached
capture is a durable evidence row from the moment it is admitted (§2.1.3), so it is
never an orphan and never waits on this scan. For each escrowed `evidence_id` with
no **evidence row** it rebuilds the identity and observations from the envelope and
calls **the §2.1.3 admission transaction itself** — it does not insert a row of its
own and it does not derive a role from the envelope. That matters because the
envelope records what was true when it was written, and an orphan can be discovered
long after a *different* capture created the record and a current evidence: a
repair that wrote `current` from stale envelope state would demote or collide with
the live one. Routed through admission, the same orphan lands on whichever branch
is correct now — `ADMITTED` if the record is genuinely missing, `ATTACHED` if a
submission is in flight, `SUPERSEDES` if the record moved on, `ALREADY_RECOVERED`
if the head shipped. The envelope's `initial_state` is consulted **only** on the
`ADMITTED` branch, where there is no live state to contradict it. Repair thereby
restores the remote expectation, PR, labels and routing disposition that no amount
of artifact hashing could recover, without ever being the thing that decides a
role. It needs **no worktree**: everything it reads is
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

**The transaction opens with an exact lookup of the incoming id across *every*
role.** Keying the first read on `role = 'current'` made replay unsafe in both
directions: a repeated capture of an already-`attached` id fell through to the
insert and hit the evidence primary key, and a repeated capture of a `superseded`
id would re-admit evidence the record had already moved past. Admission is a
convergence operation, so its first question must be "do I already know this exact
evidence, in any role?".

```
BEGIN IMMEDIATE
  ev  := SELECT * FROM validated_work_evidence WHERE evidence_id = :evidence_id
  row := SELECT * FROM validated_work_records  WHERE record_id  = :record_id

  # --- 1. Replay. This exact evidence is already known; every role is idempotent.
  if ev is not NULL:
      # evidence_id hashes the identity, which contains the key, so this always holds.
      assert ev.record_id == :record_id
      if ev.role == CURRENT:
          refresh observations iff row.state in (QUEUED, PARKED, FAILED)
          -> CONVERGED                   # the crash-retry case
      if ev.role == ATTACHED:
          -> ATTACHED                    # already waiting; no insert, no disturbance
      if ev.role == SUPERSEDED:
          -> RETAINED                    # known, kept, deliberately not acted on

  # --- 2. New evidence.
  if row is NULL:
      INSERT record (state := initial_state)
      INSERT evidence (:evidence_id, role := CURRENT, observation_revision := 0,
                       initial_state, initial_failure, initial_reason)   # §5
      classify_lineage(row)              # §2.1.4, same transaction
      -> ADMITTED
  if row.state == PUBLISHING:
      INSERT evidence (:evidence_id, role := ATTACHED,
                       initial_state, initial_failure, initial_reason)   # §5; §2.1.3
      -> ATTACHED                        # do not disturb an in-flight submission
  if row.state == RECOVERED:
      # This validated head is already published, so this evidence's work is safe.
      # RETAIN it rather than dropping it: it is still addressable, still escrowed.
      INSERT evidence (:evidence_id, role := SUPERSEDED,
                       initial_state, initial_failure, initial_reason)
      -> ALREADY_RECOVERED
  if row.state == ABANDONED:
      # New evidence for abandoned work is exactly the signal that made it
      # recoverable again.
      demote(current -> SUPERSEDED)
      INSERT evidence (:evidence_id, role := CURRENT,
                       initial_state, initial_failure, initial_reason)
      row.state := initial_state ; refresh observations
      -> REOPENED
  # row is QUEUED / PARKED / FAILED with DIFFERENT evidence for the SAME work
  demote(current -> SUPERSEDED)
  INSERT evidence (:evidence_id, role := CURRENT,
                   initial_state, initial_failure, initial_reason)
  row.state := initial_state ; refresh observations
  classify_lineage(row)                  # the head may have moved
  -> SUPERSEDES
COMMIT
```

Every branch is one transaction over both tables, so a role can never be observed
without its record and no evidence can exist in two roles. **Every branch that
inserts an evidence row writes `initial_state`/`initial_failure`/`initial_reason`**
— the columns are `NOT NULL` precisely so a branch that forgot them fails loudly
instead of defaulting to the dangerous value (§2.1.3's promotion rule). The values
are §5's admission judgement for *this* evidence, computed once at capture and never
re-derived. Every branch is also
**total**: each of the three roles and each of the six record states has a named
outcome, and every outcome is idempotent under repetition.

Three consequences, each closing a hole a partial-index scheme leaves open:

- **A rival can never be minted beside an unresolved row.** `record_id` is the
  table's primary key, so "two open records for the same work" is not a rule the
  code has to remember — it is unrepresentable. This is what the §4.1 index tried
  and failed to express by excluding `FAILED`.
- **A `FAILED` row is always resolved by transitioning *that* row.** New evidence
  supersedes it in place (`FAILED → QUEUED/PARKED`), so a recovery can never leave
  the original failure unresolved-forever, still blocking reset.
- **Superseded evidence is retained, not discarded.** A superseded evidence row
  keeps its escrow directory and its pinned refs for the retention window, and stays
  addressable by its `evidence_id`. Superseding changes which evidence is *acted
  on*; it never deletes bytes and never makes a handle unresolvable.

`ATTACHED` is the one case that defers, and it is now a **row**, not a directory on
disk. A capture arriving while a submission is in flight inserts its evidence with
role `attached` in the same transaction that reads the record, and returns the
in-flight disposition.

**Attached evidence is resolved by an explicit transition, not by re-running
admission.** Re-admitting it cannot work, and the reason is worth stating because it
is the subtle half: once the submission succeeds the record is `RECOVERED`, so a
re-admission of the attached id would take the replay branch (`ATTACHED` → still
attached) or the `ALREADY_RECOVERED` branch, and in neither case does the attached
row ever leave its role. It would be re-selected on every subsequent drain, forever.
So `drain()` calls `resolve_attached_evidence(record_id)` — its own transaction —
the moment the record leaves `PUBLISHING`:

**It runs on every drain for any record that is not `PUBLISHING`** — not only on
the edge where a record leaves that state. Triggering on the edge alone left
attached rows stranded: a record that fails, promotes one row and still holds
others never crosses that edge again, so the remaining rows would sit attached
forever while the retention sweep eventually released them.

| Record state at drain | Attached rows | Why |
|---|---|---|
| `RECOVERED` | **all** become `SUPERSEDED`; record untouched | attached evidence is for the **same** `ValidatedWorkKey`, so its validated head is exactly the head that just published. Its work is safe; retain the bytes, act on nothing. |
| `QUEUED` / `PARKED` / `FAILED` | the **oldest** becomes `CURRENT`, the previous current becomes `SUPERSEDED`, and the record moves to **that row's persisted `initial_state`, `initial_failure` and `initial_reason`**; remaining rows stay attached and are promoted one per drain until none remain | this is the "new evidence resolves a row in place" rule (§2.1.3), reached from the other direction. A failure must never be left blocking behind evidence that could clear it, and a parked record must not hide newer evidence a human would want to see. |
| `ABANDONED` | — | unreachable: `abandon()` refuses while attached evidence is unresolved (below), so a record cannot enter `ABANDONED` holding any. |

**The promoted row's disposition is read from the relation, never re-derived or
defaulted, and that is a safety property rather than a convenience.** Evidence is
admitted `PARKED` whenever §5's comparison says approval is required —
`WORKTREE_AHEAD_OF_VALIDATION`, `branch_binding_verified=False` after a detached
HEAD (#7017), and every other approval-required condition. An attached row can be
promoted arbitrarily long after that judgement was made, in a different process,
with the worktree long gone. If the store had to guess, default, or re-inspect,
the plausible default is `QUEUED` — and `QUEUED` means the next drain publishes it
automatically. Work that was deliberately parked for a human would ship without one.

So `initial_state`/`initial_failure`/`initial_reason` are written on the evidence
row **at admission**, in the same transaction that inserts it (§2.1.3), and
`resolve_attached_evidence()` copies them onto the record verbatim. No envelope is
read: §2.1.2's `capture.json` is consulted only on orphan repair's `ADMITTED`
branch, and this transition must work without a filesystem and without a restart.
There is no clamping rule and therefore no invented failure/reason: the promoted
row's disposition is copied verbatim, and a `QUEUED` row promotes to `QUEUED`. The
case that made clamping look necessary — an operator abandoning work while newer
evidence sat attached — is closed at the other end instead.

**`abandon()` refuses while unresolved attached evidence exists.** It returns
`AbandonValidatedWorkOutcome(status=ATTACHED_EVIDENCE_PENDING, ...)` with zero
effect, naming the waiting evidence ids in `pending_evidence_ids` — and the same
condition makes `can_abandon` false on the snapshot, with
`abandon_unavailable = ATTACHED_EVIDENCE_PENDING`, so the UI never offers an action
the owner is specified to refuse. The
reason is that abandonment is an operator accepting a loss, and accepting a loss
requires seeing what is being lost: attached evidence may be a *better* capture of
the same work — one that would have published — and it has not yet been considered
because the record was `PUBLISHING` when it arrived. The next drain promotes it
(rows above), the operator sees the record's new state and reason, and may abandon
then if they still wish to. This also makes the state total: a record can only reach
`ABANDONED` with no attached rows outstanding, so no attached evidence is ever
resolved away by an abandonment nobody evaluated it against, and the retention sweep
never releases a row that was never looked at.

The transition is idempotent and runs under the record's fence (§4.4e), so a stale
publisher cannot drive it. Rows are processed oldest-first by `admitted_at`, so the
order is deterministic.

The durable row is what makes the promise real. As an escrow directory alone, an
attached capture was discoverable only by `reconcile_escrow_orphans()`, which runs
**once per process start** (§2.1.2) — so within a single long-lived orchestrator
process, the common case where the in-flight submission resolves seconds later had
no query that could ever find it, and the alternate evidence sat inert until the
next restart.

#### 2.1.4 Lineage: two validated heads of one branch must not strand each other

`ValidatedWorkKey` includes `validated_head_sha`, so validating at `V` and later at
`L` on the same issue and branch produces two `record_id`s — deliberately, because
they are two different commits and §1.1 forbids treating one as the other. But two
*rows* with no relationship between them is a deadlock generator:

- If `L` publishes first, the remote leaves `V`'s recorded expectation behind. `V`'s
  row then sees a third sha and fails — even though `V` is an ancestor of `L` and its
  work is already published and safe.
- If `V` publishes first, `L`'s row can fail against the intermediate `V`.
- Either way an unresolved row survives, `has_unresolved_work()` stays true, and
  reset is blocked until a human abandons work that is either already shipped or
  perfectly publishable.

Drain order must not decide this. The relationship is a property of the commits, so
it is computed **in the admission transaction** and stored, and every row on one
issue+branch carries a `LineageRole` (§2.1). The lineage key is canonical over
`(repo_slug, issue_number, branch_name)` — deliberately *not* the head sha, which is
what makes it the thing several records share.

**The lineage's published head is a durable fact, not a derived one.** A separate
row per lineage key records what this owner has actually published:

```sql
CREATE TABLE IF NOT EXISTS validated_work_lineage (
    lineage_key                 TEXT PRIMARY KEY,
    published_head_sha          TEXT NOT NULL DEFAULT '',
    published_by_record_id      TEXT NOT NULL DEFAULT '',
    published_via               TEXT NOT NULL DEFAULT '',  -- PublicationProvenance
    published_pre_push_expected TEXT NOT NULL DEFAULT '',  -- '' unless we pushed it
    published_at                TEXT NOT NULL DEFAULT ''
);
```

**Every verified publication route advances it — there is more than one.** Writing
it only from `RECOVERED(PUBLISHED)` left a hole exactly as wide as the one it
closed: §3.5 independently resolves a record `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)`
after verifying that a *merged PR* contains the validated head, and that is the
**ordinary** ending for most work. With no fact recorded there, a later ancestor —
ordinary capture, orphan repair, or slice-8 backfill — again saw no unresolved peer
and no published fact, and the arrival-order stranding recurred on the common path.

The two routes prove different things, so the fact records which:

```python
class PublicationProvenance(StrEnum):
    PUSHED_BY_OWNER = "pushed_by_owner"   # we pushed it; the pre-push baseline is known
    OBSERVED_MERGE  = "observed_merge"    # a merged PR contains it; no baseline of ours
```

| Route | `published_head_sha` | `published_pre_push_expected` |
|---|---|---|
| `RECOVERED(PUBLISHED)` — §4.4 drain | the pushed `validated_head_sha` | the recorded pre-push expectation |
| `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` — §3.5 merged PR | the observed merged head, verified to contain the validated head | `''` — we did not push, so we have no baseline to prove |

Both are written in the same transaction as their record's resolution, and only when
the new head is a descendant of (or equal to) the recorded one, so the fact moves
forward and never backward. Neither is inferred from a label or from a bare remote
read; each is written only after the containment/publication it describes was
verified.

**The missing baseline is modelled, not glossed.** Under `OBSERVED_MERGE` there is
no proven pre-push expectation, so §2.1.4's descendant rule has nothing to compare
against: a late descendant is admitted `PARKED(REMOTE_BASELINE_UNPROVEN)` unless its
own captured expectation is exactly the recorded published head. Late **ancestors**
are unaffected — containment needs only the head — and late **divergent** heads park
as always. Recording the head with an honest provenance is strictly better than
omitting it: the ancestor case (the stranding one) is resolved either way, and the
descendant case parks for a decision instead of failing against a remote it cannot
explain.

**Admission classifies the new head against the published fact AND every unresolved
row on the key**, using `merge-base --is-ancestor` against the **pinned refs** (§6)
so a pruned object can never make the comparison unanswerable. Consulting only
unresolved peers was a stranding path in its own right: once a descendant `H` is
`RECOVERED`, it is resolved and therefore invisible to that comparison, so a later
admission of ancestor `V` — an ordinary backfill (§11 slice 8) or an orphan repair
that surfaced late — saw no peer at all, became the drainable `HEAD`, captured a
fresh expectation of `H`, and then failed check 8 for not descending from `H`.
Work demonstrably contained in the published head became durable
`FAILED(REMOTE_DIVERGED)` and blocked reset until a human abandoned it.

So classification is a **total** function of (new head, published fact, unresolved
peers). When a published head `H` exists for the lineage key:

| New head vs. published `H` | Result |
|---|---|
| ancestor of `H`, or equal to it | escrow re-verified ⇒ `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` with `published_head_sha = H`, immediately and without ever being `QUEUED`; escrow fails to verify ⇒ `FAILED(ARTIFACT_HASH_MISMATCH)`, unresolved |
| descendant of `H` | `HEAD`, subject to its persisted evidence admission gate, **sequenced from the proven baseline**: `expected_remote_head := H`, permitted only when the new row's captured expectation is `H` itself or the lineage's `published_pre_push_expected`. This baseline proof never overrides required approval. Anything else ⇒ `PARKED(REMOTE_BASELINE_UNPROVEN)` |
| divergent from `H` | `PARKED(DIVERGENT_VALIDATED_HEADS)`. A published divergent head is never published over automatically |
| ancestry unanswerable | `FAILED(VALIDATION_SHA_MISMATCH)` |

Only after that gate does the comparison against unresolved peers run:

| New head vs. existing unresolved rows | Result |
|---|---|
| Descendant of them | new row is `HEAD`; each ancestor row becomes `ANCESTOR`, is forced from `QUEUED` to `PARKED(ANCESTOR_OF_PENDING_HEAD)`, and records `superseded_by_record_id` |
| Ancestor of an existing `HEAD` | new row is admitted directly as `ANCESTOR` / `PARKED(ANCESTOR_OF_PENDING_HEAD)` |
| Neither (divergent) | **every** row on the key becomes `DIVERGENT` / `PARKED(DIVERGENT_VALIDATED_HEADS)`. Nothing auto-publishes; an operator or approved tech-lead op promotes exactly one to `HEAD` |
| Ancestry unanswerable (a sha unreachable) | the *unreachable* row becomes `FAILED(VALIDATION_SHA_MISMATCH)`; the readable rows are untouched |

**A newer head arriving during a publication gets its own durable state.** An
existing row that is already `PUBLISHING` is never reclassified — the in-flight
submission owns the remote expectation, and the §4.4e fence exists precisely so
nothing rewrites its baseline mid-flight. But the arriving record has a *different*
`record_id`, so `ATTACHED` — an evidence role **within** one record — cannot
represent it. It is admitted as its own record in `PARKED`, with
`lineage_role = PENDING`, `failure = AWAITING_LINEAGE_PREDECESSOR`, and
`waits_on_record_id` set to the publishing record (§4.1). `PARKED` is unresolved, so
the waiter blocks reset and retains its escrow from the moment it exists, and
`PENDING` is outside `ux_validated_work_lineage_head`, so it cannot race the
publication for the drainable slot.

When the predecessor resolves, `classify_lineage_waiters(predecessor_record_id)`
runs **in the same transaction as the predecessor's own resolution**, so a waiter is
never observable in a state its predecessor has already left:

| Predecessor reached | Waiter is | Waiter becomes |
|---|---|---|
| `RECOVERED` at `H` | a descendant of `H` | `HEAD`, with its persisted evidence admission gate restored — **and its compare-and-set baseline is advanced to `H`**, but only under the proof below. Only evidence originally eligible for automatic publication becomes `QUEUED`; approval-required evidence stays `PARKED`. |
| `RECOVERED` at `H` | an ancestor of `H` | `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` if its escrow still verifies, else `FAILED(ARTIFACT_HASH_MISMATCH)` — never resolved by inference |
| `RECOVERED` at `H` | divergent from `H` | `PARKED(DIVERGENT_VALIDATED_HEADS)` |
| `FAILED` or `ABANDONED` | any | classified by §2.1.4's ordinary admission rules against the predecessor's **unpublished** head. No baseline moves — nothing was published, so the waiter's captured expectation still stands. |
| still `PUBLISHING` (an attempt was merely reconciled by a successor, or is still in flight) | any | stays `PENDING`. Neither reconciliation nor a change of owner is a resolution. |

**The baseline advance is proven, not observed**, and it reads the same
`validated_work_lineage` fact the post-resolution rules do. A descendant waiter captured its
`expected_remote_head_sha` before the predecessor pushed — typically `R` — and the
remote is now at the predecessor's `H`. Under the §4.3 allowed-state table `H` is a
third sha, so without this rule the waiter would fail against the very publication
that contains its own base. Advancing the baseline is therefore necessary, but it
must not become "re-read the remote and believe it". The advance is permitted
**only when the waiter's recorded expectation equals the predecessor's recorded
pre-push expectation** — both observed the same remote state, and the owner's own
durable `published_head_sha` proves the only change since was its own push. That
is a comparison of two durable records, with no remote read involved. When the two
expectations differ, something else moved the branch: the waiter goes to
`PARKED(REMOTE_BASELINE_UNPROVEN)` for an explicit decision rather than inheriting a
baseline nobody proved.

**Publication resolves the ancestors it contains, and records why it may.** In the
same transaction that marks the `HEAD` row `RECOVERED` at published head `H`, the
`validated_work_lineage` fact is advanced to `H` — and every unresolved row on the
lineage key whose `validated_head_sha` is an ancestor of `H` **and whose escrowed
artifacts still verify** is marked `RECOVERED` with
`resolution_kind = CONTAINED_IN_PUBLISHED_HEAD`, `published_head_sha = H`, and its
own escrow and refs retained for the window. Its work is *in* the published head;
holding it unresolved would be a deadlock in defence of nothing.

Rows admitted *after* that transaction take the same decision from the same fact
(the table above), so the answer does not depend on whether a record happened to
exist when its descendant shipped. That symmetry is the point: containment is a
property of the commits, and it must not become a property of arrival order.

The artifact re-verification is not ceremony. Resolving an ancestor is the one place
this contract marks work safe without publishing it, so it is gated on the same
evidence check as a real publication: an ancestor whose escrow no longer verifies
becomes `FAILED(ARTIFACT_HASH_MISMATCH)` — still unresolved — rather than being
resolved by inference. **Divergent rows are never resolved by another row's
publication**, because their commits are genuinely not contained; publishing one
leaves the others parked for an explicit choice, which is the correct answer to
"these two validated heads are incompatible".

**At most one drainable row per lineage key**, enforced durably:

**Lineage eligibility never grants publication authority.** The persisted
`initial_state`/`initial_failure`/`initial_reason` on the CURRENT evidence are the
admission gate; lineage classification is an additional gate. Every descendant
promotion, waiter release, attached-evidence promotion and late admission restores
that evidence gate before applying the lineage restriction. `HEAD` means the row
may be considered, not that it is `QUEUED`. In particular, a waiter captured with
`WORKTREE_AHEAD_OF_VALIDATION`, detached branch binding, or historical operator
intake remains `PARKED` after its predecessor succeeds. Neither a proven baseline
advance nor removal of a lineage restriction is an approval. An explicit recovery
command may override an approval-required gate only after §4.3 check 0; automatic
reclassification cannot reuse such consent after the evidence or observations
change. All transitions call this same gate-composition rule, including
`resolve_attached_evidence()`, so copying an initial `QUEUED` state cannot bypass an
existing divergent/ancestor restriction either. Tests exercise the cross-product
of admission gate and lineage outcome, not just ancestry alone.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_lineage_head
    ON validated_work_records (lineage_key)
    WHERE state IN ('queued', 'publishing');
```

This is a partial unique index over a state subset, which §4.1 argues against for
the *record* uniqueness rule — the difference is worth stating because it looks like
a contradiction. There, the excluded states were a hole: a second row for the same
work could be minted beside an excluded one. Here, multiple rows per lineage key are
the **intended** shape (ancestors and divergents are supposed to coexist), and the
index constrains only how many may be *acted on at once*. The uniqueness that
matters for work identity is still the `record_id` primary key, which no state can
escape.

**Operator handles resolve through the evidence relation.** The tech-lead op's
`ValidatedWorkAuthoritySnapshot` and the Control Center actions name an `evidence_id`, which
`evidence_for_id()` resolves by primary key to its row, its **role**, and its owning
record (§4.1) — no JSON is scanned, and a superseded or attached id resolves just as
exactly as the current one. A handle whose role is not `CURRENT` is **refused** with
an explicit "this evidence is `<role>`; the record is now acting on `<evidence_id>`"
message rather than silently acting on the current evidence — an approval is consent
to publish a specific commit and a specific set of artifacts, and consent does not
transfer.

### 2.2 Command / result

#### 2.2.1 Two commands, because there are two different jobs

Automatic capture and stored-evidence recovery need *disjoint* inputs. Capture
needs the exact run artifacts of a run that just ended and has no evidence id yet;
recovery needs an evidence id and must never re-capture. A single request with
`run_assets: SessionRunAssets | None` and `evidence_id: str = ""` describes their
*union*, and a union type has to be narrowed at runtime by the owner — which is
the moment a missing `run_assets` becomes a decision, and every available decision
is a forbidden fallback: report no work (a completed+validated run passes through
teardown unseen), scan for the latest run, or rediscover paths from a worktree.
The last two are explicitly outlawed by the repository's active-run contract
(`control/AGENTS.md`, "Strongly Typed Session Run Ownership"). So the command is a
**discriminated union of two frozen types, each of whose required fields are
non-optional**:

```python
class DispositionInitiator(StrEnum):
    """WHO asked. Recorded on the row; never widens what the owner will do.

    The three initiators of the "one owner, three initiators" rule. This
    selects the *authority path* (and therefore what may be approved without a
    human), not the policy: every §4.3 check runs identically for all three.
    """
    AUTOMATIC = "automatic"   # terminate_issue_runtime, at the boundary
    TECH_LEAD = "tech_lead"   # approved recover_validated_work gated op (§8.1)
    OPERATOR  = "operator"    # Control Center command (§8.4)


@dataclass(frozen=True, slots=True)
class AutomaticCaptureCommand:
    """Capture at a terminal boundary. Carries the runs, not a hint of them."""
    issue_number: int
    reason: str                       # the terminate_issue_runtime reason string
    run_evidence: IssueRunEvidence    # §2.5 — required; proves what was considered

    initiator: ClassVar[DispositionInitiator] = DispositionInitiator.AUTOMATIC

    def __post_init__(self) -> None:
        if self.run_evidence.issue_number != self.issue_number:
            raise ValueError("run evidence must belong to the issue being terminated")


@dataclass(frozen=True, slots=True)
class ValidatedWorkAuthoritySnapshot:
    """The facts an approval was given AGAINST. Immutable once approved.

    ``evidence_id`` binds the immutable half — repo, issue, branch, validated
    head, run identity, artifact hashes, routing. It deliberately excludes the
    mutable observations (§2.1.1), which is exactly what makes it crash-stable
    — and exactly why it cannot stand alone as authorization. Those excluded
    observations are refreshed under the same id while a record is ``QUEUED``,
    ``PARKED`` or ``FAILED``, so an op approved against PR ``P`` and remote
    baseline ``R`` could otherwise execute later against ``P2``/``R2`` with the
    approval unchanged.

    Revalidation does not close that: §4.3 proves the CURRENT facts are
    internally safe, not that they are the facts a human authorized.
    """
    record_id: str
    evidence_id: str
    observation_revision: int             # §2.1.1; bumped by every refresh
    validated_head_sha: str
    branch_name: str
    repo_slug: str
    issue_number: int
    pr_number: int | None                 # the PR the approver saw
    expected_remote_head_sha: str | None   # the remote baseline the approver saw


@dataclass(frozen=True, slots=True)
class StoredEvidenceCommand:
    """Recover or re-submit evidence that is ALREADY durable. Never captures."""
    issue_number: int
    reason: str
    initiator: DispositionInitiator   # TECH_LEAD or OPERATOR only
    evidence_id: str                  # non-empty, always
    actor: str                        # operator identity or approved tech-lead op id
    authority: ValidatedWorkAuthoritySnapshot   # what was approved; checked for equality

    def __post_init__(self) -> None:
        if self.initiator is DispositionInitiator.AUTOMATIC:
            raise ValueError("stored-evidence recovery is never the automatic initiator")
        if not self.evidence_id:
            raise ValueError("stored-evidence recovery requires an evidence_id")
        if not self.actor:
            raise ValueError("stored-evidence recovery requires an actor")
        if self.authority.evidence_id != self.evidence_id:
            raise ValueError("the approval must name the evidence it is executing")
        if self.authority.issue_number != self.issue_number:
            raise ValueError("the approval must name the issue it is executing")


ValidatedWorkDispositionCommand = AutomaticCaptureCommand | StoredEvidenceCommand
```

Neither type has a field that can be absent, and neither has a sentinel. There is
no `worktree_path`: capture reads artifacts through `IssueRunEvidence`
(§2.5), which carries `SessionRunAssets.worktree_path` per run, and publication
never reads a worktree at all (§4.4a). There is no `evidence_id=""`: the automatic
command *derives* the id from what it captured (§2.1.1), and the stored-evidence
command requires a real one.

```python
@dataclass(frozen=True, slots=True)
class OperatorResolution:
    """The ONLY way unresolved work becomes safe to lose. Never inferred."""
    actor: str            # operator identity or approved tech-lead op id
    reason: str           # non-empty; recorded verbatim in the durable row
    resolved_at: str      # ISO-8601 UTC


@dataclass(frozen=True, slots=True)
class ValidatedWorkDisposition:
    """The disposition OF ONE RECORD. It always names the work it disposes.

    ``record_id`` and ``key`` are required, not decorative. A disposition that
    carried only ``evidence_id`` could not identify its unit of work at all:
    §2.1.1 deliberately allows several evidence ids per record, so a batch
    validated for distinct evidence ids can still hold two members for one unit
    — the exact invariant the batch exists to state. The same omission made
    §8.3's event contract unbuildable from the returned value, forcing the
    emitter back into the store for identity the typed boundary had claimed to
    return.
    """
    record_id: str                # required, non-empty; the durable row's key
    key: ValidatedWorkKey         # repo, issue, branch, validated head
    evidence_id: str              # required, non-empty; the CURRENT evidence
    state: ValidatedWorkState     # every state names a persisted row
    lineage_role: LineageRole
    reason: str
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
        if not self.record_id or not self.evidence_id:
            raise ValueError("a disposition must name its record and its evidence")
        if self.record_id != self.key.record_id:
            raise ValueError("record_id must be the canonical id of the key it carries")
        if self.state is ValidatedWorkState.FAILED and self.failure is None:
            raise ValueError("FAILED disposition requires an enumerated failure")
        if self.state is ValidatedWorkState.ABANDONED and self.resolution is None:
            raise ValueError("ABANDONED requires an explicit OperatorResolution")
        if self.state is not ValidatedWorkState.ABANDONED and self.resolution is not None:
            raise ValueError("only ABANDONED carries an OperatorResolution")


class AbandonStatus(StrEnum):
    """Total. Every abandon() call ends on exactly one of these."""
    ABANDONED                 = "abandoned"
    NO_SUCH_RECORD            = "no_such_record"
    ALREADY_RESOLVED          = "already_resolved"       # RECOVERED or ABANDONED
    REFUSED_STATE             = "refused_state"          # QUEUED / PUBLISHING: stop it first
    ATTACHED_EVIDENCE_PENDING = "attached_evidence_pending"


@dataclass(frozen=True, slots=True)
class AbandonValidatedWorkOutcome:
    """Typed result, because a refusal has to carry its reason to the UI.

    Returning a bare `ValidatedWorkDisposition` gave callers no way to tell
    success from refusal except by re-reading state or parsing an exception,
    and no carrier at all for the evidence ids an
    `ATTACHED_EVIDENCE_PENDING` refusal must name.
    """
    status: AbandonStatus
    disposition: ValidatedWorkDisposition | None   # set iff ABANDONED
    pending_evidence_ids: tuple[str, ...]          # non-empty iff ATTACHED_EVIDENCE_PENDING
    message: str


@dataclass(frozen=True, slots=True)
class ValidatedWorkDispositionBatch:
    """EVERY disposition one termination produced. One per distinct ValidatedWorkKey.

    Singular was a data-loss path, not merely an imprecise type. A ledger
    holding run A validated at ``V`` and run B validated at a divergent ``L``
    has two units of work; a result that can hold one meant capture had to pick
    one, and teardown then removed the worktree of the other. The lineage
    machinery cannot protect a record that admission never created.

    **Empty is the no-work answer**, and it is the only one. There is no `NONE`
    member (§2.1): "nothing admissible was found here" is the absence of
    records, and modelling it as a member would mean a disposition with no
    record and no key inside a collection whose invariant is that every member
    names one.
    """
    issue_number: int
    dispositions: tuple[ValidatedWorkDisposition, ...]   # empty == no work found
    reason: str                                          # why, especially when empty

    @classmethod
    def no_work(cls, issue_number: int, reason: str) -> "ValidatedWorkDispositionBatch":
        """The explicit no-work result. Reads at call sites as what it is."""
        return cls(issue_number=issue_number, dispositions=(), reason=reason)

    def __post_init__(self) -> None:
        record_ids = [d.record_id for d in self.dispositions]
        if len(set(record_ids)) != len(record_ids):
            raise ValueError("one disposition per unit of work: duplicate record_id")
        for d in self.dispositions:
            if d.key.issue_number != self.issue_number:
                raise ValueError("every member must belong to the terminated issue")

    @property
    def found_work(self) -> bool:
        return bool(self.dispositions)

    @property
    def unresolved(self) -> bool:
        """True while ANY member still holds work that must not be destroyed."""
        return any(d.unresolved for d in self.dispositions)

    @property
    def unresolved_dispositions(self) -> tuple[ValidatedWorkDisposition, ...]:
        return tuple(d for d in self.dispositions if d.unresolved)


@dataclass(frozen=True, slots=True)
class ValidatedWorkSnapshot:
    """Read model for the view-model layer (§8.4). Facts + availability only.

    One per record; ``snapshot()`` returns every record for the issue. The UI
    renders these; it never re-derives
    policy, which is why the two ``can_*`` flags are computed by the owner
    from the state machine (§4.2) rather than by the route from ``state``.
    """
    disposition: ValidatedWorkDisposition
    record_id: str
    validated_head_sha: str
    worktree_head_sha: str
    branch_name: str
    expected_remote_head_sha: str | None
    superseded_evidence_ids: tuple[str, ...]   # from the evidence relation (§4.1)
    attached_evidence_ids: tuple[str, ...]     # admitted during PUBLISHING, not yet drained
    lineage_role: LineageRole                  # §2.1.4; ANCESTOR/DIVERGENT never drain
    escrow_retained: bool
    observation_revision: int                  # §2.1.1; posted back as authority (§8.4)
    waits_on_record_id: str                    # §2.1.4; '' unless PENDING behind a sibling
    owner: ClaimOwnerFact | None               # §4.4e claim holder + stop availability;
                                               # a FACT the owner computes, not a control
    publish_attempts: int             # §4.4e attempt rows; a retry loop is visible
    finalization_phase: FinalizationPhase      # §4.5b; where a resumed finalize restarts
    updated_at: str
    can_recover: bool                 # PARKED, or FAILED after the condition is fixed
    can_abandon: bool                 # PARKED/FAILED **and** no unresolved attached evidence
    abandon_unavailable: AbandonStatus | None   # why not, when can_abandon is false
```

### 2.3 Ports

`ports/validated_work_disposition.py` (new) — the behavior-level owner:

```python
class ValidatedWorkDispositionOwner(Protocol):
    def dispose_at_termination(
        self, command: AutomaticCaptureCommand
    ) -> ValidatedWorkDispositionBatch: ...
    """Automatic initiator. Called by terminate_issue_runtime BEFORE teardown.

    Returns EVERY disposition the capture produced — one per distinct
    ``ValidatedWorkKey`` across every run in the command's evidence (§5).
    Raises if any candidate cannot be durably admitted, so teardown never
    proceeds over a partially captured batch.
    """

    def recover(
        self, command: StoredEvidenceCommand, state: OrchestratorState
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
    ) -> AbandonValidatedWorkOutcome: ...
    """Operator explicitly accepts the loss: UNRESOLVED -> ABANDONED.

    The single modeled route out of FAILED/PARKED without a recovery.
    Requires an actor and a reason, is refused for QUEUED/PUBLISHING (stop
    the in-flight work first), and retains escrow + refs for the retention
    window regardless. Never callable by an agent.
    """

    def snapshot_record(self, record_id: str) -> ValidatedWorkSnapshot | None: ...
    """Exact read by durable primary key (§8.4). None when no such record."""

    def snapshot(self, issue_number: int) -> tuple[ValidatedWorkSnapshot, ...]: ...
    """Read model for the UI/view-model layer. Never re-derives policy.

    A tuple, not an optional: one issue can hold several records (§2.1.4
    lineage), and an Optional would force the UI to show one and hide the rest
    — the same collapse-to-a-winner that made the batch necessary. Empty means
    no records, which is the honest rendering of "nothing to dispose".
    """
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
    validated_work: ValidatedWorkDispositionBatch   # NEW — required, never defaulted
```

Making the field required (no default) is deliberate: every construction site must
produce a disposition, so a new terminal path cannot be added that silently omits it.
Making it a **batch** is equally deliberate: a singular field would have forced
capture to choose one unit of work per termination, and the unchosen one would have
been torn down un-escrowed. `.unresolved` on the batch is what every consumer
already asks for, so the seams in §3.3 read the same as before.

### 2.5 `IssueRunEvidence` — the typed source of the runs capture must consider

`AutomaticCaptureCommand.run_evidence` has to come from somewhere, and today the
termination boundary has nothing to build it from: `terminate_issue_runtime()`
takes `issue_number` and `reason` (`control/review_exchange_lifecycle.py:118-128`)
and three of its four call sites have no more than that. Without a source, the
required field is unsatisfiable and the contract collapses back to an optional.

The source is a **behavior-level port whose result is an explicit fact about which
runs were considered** — not a search:

```python
class IssueRunEvidenceOrigin(StrEnum):
    """Where the runs came from. Recorded for audit; never a policy input."""
    LIVE_REGISTRY = "live_registry"   # sessions still in OrchestratorState
    RUN_LEDGER    = "run_ledger"      # durable rows written at launch
    BOTH          = "both"


class IssueRunEvidenceStatus(StrEnum):
    RUNS_RECORDED    = "runs_recorded"
    NO_RUNS_RECORDED = "no_runs_recorded"   # a POSITIVE fact, not a missing answer


@dataclass(frozen=True, slots=True)
class IssueRunRecord:
    """One run of one issue, with the exact assets its creating owner allocated."""
    session_key: SessionKey
    run: SessionRunAssets              # exact; never rediscovered, never optional
    recorded_at: str


@dataclass(frozen=True, slots=True)
class IssueRunEvidence:
    """Proof of WHICH runs this termination looked at."""
    issue_number: int
    status: IssueRunEvidenceStatus
    runs: tuple[IssueRunRecord, ...]
    origin: IssueRunEvidenceOrigin
    observed_at: str

    def __post_init__(self) -> None:
        if self.status is IssueRunEvidenceStatus.RUNS_RECORDED and not self.runs:
            raise ValueError("RUNS_RECORDED requires at least one run")
        if self.status is IssueRunEvidenceStatus.NO_RUNS_RECORDED and self.runs:
            raise ValueError("NO_RUNS_RECORDED cannot carry runs")


class IssueRunEvidenceUnavailable(RuntimeError):
    """The ledger could not be read. Never a synonym for "no runs"."""


class IssueRunEvidenceSource(Protocol):
    def record_run(self, issue_number: int, record: IssueRunRecord) -> None: ...
    """Called by the owner that ALLOCATED the run, in its launch transaction."""

    def evidence_for_issue(self, issue_number: int) -> IssueRunEvidence: ...
    """Every run recorded for the issue and not yet released. Raises on read failure."""

    def release_runs(self, issue_number: int, *, before: str) -> None: ...
    """Drop rows once their disposition is RESOLVED (§6 retention window)."""
```

**`NO_RUNS_RECORDED` is a fact, `IssueRunEvidenceUnavailable` is a failure, and
they are different types.** That split is the whole point. A conflated
`runs: tuple[...] = ()` cannot distinguish "this issue genuinely never launched a
run" from "the ledger is unreadable" — and the second, read as the first, is
precisely how validated work passes through teardown unseen. An unreadable ledger
raises, and §3.1's step 1 turns the raise into an aborted teardown, the same teeth
escrow failure already has.

**Missing ownership fails closed.** If a run is live in the registry (a `Session`
in `OrchestratorState.active_sessions`, which carries a required
`SessionRunAssets` — `domain/models.py:1208`) but has **no** ledger row, the source
raises `IssueRunEvidenceUnavailable`: the two owners disagree about what exists,
and the safe reading of a disagreement is "we do not know what we would be
destroying". It is not silently unioned into a `RUNS_RECORDED` answer.

**Who writes it.** `record_run()` is called by the owner that constructs
`SessionRunAssets` — the launch transaction in `control/launch_transaction.py` /
`control/session_launcher.py`, in the same step that creates the `Session`. That is
the "exact run identity/assets from the owner that created the run" requirement,
discharged at the only place the exact values exist.

**Why a durable ledger and not just the live registry.** Every case file this
design exists for describes a termination that happens *after* the session record
is gone: a crash and restart, a stuck sweep, a reset proposal, a
`history_reconciliation` on an `issue-completed` PR. The live registry is empty by
then, and an empty registry is a truthful `NO_RUNS_RECORDED` — truthfully wrong.
`SqliteIssueRunLedger` (`issue_run_ledger.sqlite`, registered in
`infra/sqlite_registry.py`) keeps the rows until the disposition resolves.
`SqlitePendingWorkClaimStore` is the exact precedent: it already takes
`SessionRunAssets` and derives a stable `run_key` from it
(`execution/pending_work_claim_store.py:515`), so this is a second table in an
established shape, not a new mechanism.

**What the source may never do.** No `SessionOutput.find_run_dir()`, no
"latest run", no session-name search, no worktree scan, no manifest rediscovery.
`RecordedSessionRunLookup` is inspection, not ownership, and §9's guardrail forbids
the disposition modules from importing it. Test fakes raise on any such call, so an
ownership regression fails the suite rather than degrading quietly.

---

## 3. Composition and control flow

### 3.1 Inside `terminate_issue_runtime()`

The order is load-bearing. Disposition must observe the pair, the worktree, and the
run directory *before* any of them is released.

```
0. evidence = run_evidence.evidence_for_issue(n)                  # may raise -> teardown aborts
1. batch = validated_work.dispose_at_termination(
       AutomaticCaptureCommand(n, reason, evidence))              # may raise -> teardown aborts
   # batch holds ONE disposition per distinct ValidatedWorkKey across every run (§5).
   # Any group that cannot be escrowed raises here, so teardown never runs over a
   # partially captured batch.
2. cancel_issue_review_exchange(...)      # pair release + supervised job cancel
3. publish_recovery.abandon_issue(...)    # unchanged
4. stop issue-N / rework-N terminals; clear stale active-session records
5. return IssueRuntimeTermination(..., validated_work=batch)
```

`validated_work` **and `run_evidence`** become **fields of the owning bundle**
(§3.2) rather than per-call parameters, and `terminate_issue_runtime()` becomes a
method on it. That is the mechanical guardrail (ADR-0012) that stops a future
terminal path from opting out: there is no parameter to omit, because there is no
parameter. The bundle holds the run-evidence *source* rather than a prebuilt
`IssueRunEvidence` for one reason: three of the four call sites hold only an issue number, and a caller forced
to build the evidence itself is a caller that will eventually build it from a
worktree scan. Step 0 is inside the boundary, so there is exactly one place that
knows how run evidence is obtained.

There are **three direct call sites and one typed-callable seam**, and every one of
them now holds the same `IssueRuntimeLifecycleOwners` value instead of a list of
owners it assembled:

| Path | Site | Today | How it reaches the bundle |
|---|---|---|---|
| Orchestrator facade (also how the tech-lead kill wiring and shutdown arrive) | `infra/orchestrator.py:240`, inside `Orchestrator.terminate_issue_runtime_for_issue` | returns `IssueRuntimeTermination` | `deps.issue_runtime` |
| Action applier | `control/action_applier.py:1134` | returns it | constructor-injected, one field |
| Dashboard / tech-lead reset | `entrypoints/web_retry_history_routes.py:554` | **result discarded** | `_ResetRetryRuntimeOwners` becomes a holder of the same value (§3.2) |
| Awaiting-merge reconciliation | `control/history_reconciliation.py:85`, through the injected `IssueRuntimeTerminator` | **result discarded, and its type is erased** | the bundle's bound method is the terminator (§3.5) |

**`IssueRuntimeTerminator` is a hole in the guardrail, and it must be closed by this
contract.** It is declared as `Callable[[int, str], object]`
(`control/history_reconciliation.py:25`) — a positional alias whose return type is
`object`. Three things follow, none of them visible from the module function's
signature:

- A required keyword parameter on `terminate_issue_runtime()` does **not** reach
  this path. Whoever *builds* the terminator supplies the owner; the alias itself
  can be satisfied by any two-argument callable, including one that never heard of
  the disposition owner.
- `object` erases `IssueRuntimeTermination`, so the §9 rule "every call site
  **binds** the returned `validated_work`" is not merely unenforced here — it is
  unstatable. `history_reconciliation.py:85` discards the result today.
- This is a genuine terminal edge, not a bookkeeping one. It fires on
  `issue-completed` when the PR was **closed** as well as when it was merged, so an
  issue can be torn down through it while a `PARKED` or `FAILED` record is sitting
  in escrow.

The fix is part of the slice, not a follow-up: narrow the alias to
`Callable[[int, str], IssueRuntimeTermination]`, have `history_reconciliation.py`
bind the result and carry it (§3.5 defines exactly what "carry" means there, and
why it is *not* a refusal), and add the seam to the §9 call-site guardrail. An
alias that returns `object` is exactly how a fifth terminal path gets added next
year without anyone noticing it never disposed anything.

Steps 0 and 1 raising are the invariant's teeth: if evidence exists but escrow cannot be
written, the boundary refuses to tear down. The caller surfaces a hard failure and
the work stays exactly where it is.

**`ESCROW_WRITE_FAILED` is therefore a raise at capture and a state everywhere
else, and the difference is whether a row exists.** At capture there is nothing
durable yet — a `FAILED` row would have to point at escrow that was never written,
which is a record asserting the existence of evidence it cannot produce. So capture
raises, teardown aborts, and the worktree (still present, because teardown stopped)
remains the source for the next attempt. Once a row exists, the same condition
reached from `drain()` or `reconcile_escrow_orphans()` is a durable
`FAILED(ESCROW_WRITE_FAILED)`: the row is the thing that keeps the work from being
lost, and it is already there to be transitioned. §10's "partial escrow write" row
is the second case, not a contradiction of the first.

### 3.2 `has_active_issue_runtime()` — fifth probe

```python
probes: Mapping[IssueRuntimeOwnerKind, Callable[[], bool]] = {
    IssueRuntimeOwnerKind.SESSIONS:       ...,              # unchanged
    IssueRuntimeOwnerKind.PAIR_REGISTRY:  ...,              # unchanged
    IssueRuntimeOwnerKind.SUPERVISED_JOB: ...,              # unchanged
    IssueRuntimeOwnerKind.PUBLISH_RETRY:
        lambda: publish_recovery is not None and publish_recovery.has_active_retry(n),
    IssueRuntimeOwnerKind.VALIDATED_WORK:
        lambda: validated_work.has_unresolved_work(n),      # NEW — not optional
}
return any(
    _owner_active_or_unverifiable(probe)
    for kind, probe in probes.items()
)
```

The tuple becomes a keyed mapping so the *result* can name which owners were
active — a blocked drain reports `RUNTIME_ACTIVE: pair_registry` rather than a bare
false. It gains no exclusion parameter: §4.3 check 5's "is any owner *other than
me* active?" is answered by a differently-scoped object, not by asking this one to
look away.

`has_active_issue_runtime()` is the **other method on the same bundle**, so the
freshness predicate and the teardown cannot read different owner sets — not by
convention, but because they are two methods on one value. An earlier draft made
`validated_work` a required *parameter* of both free functions, which is weaker: a
parameter list can be satisfied with a different set, and a future activity call
site could pass every existing owner, get `False`, and authorize a scratch reset
over unresolved work. There is now nothing to pass.

Concretely: `_ResetRetryRuntimeOwners` in
`entrypoints/web_retry_history_routes.py` becomes a holder of that one
`IssueRuntimeLifecycleOwners` value, so `has_active_reset_retry_runtime()` and
`_terminate_reset_retry_runtime()` — the tech-lead reset executor and the dashboard
reset — call the same two methods every other caller does. The existing fail-safe
wrapper (`_owner_active_or_unverifiable`) still applies inside `core.probe()`: a
probe that raises counts as active. §9's guardrail covers **every** call site of
both methods, not only termination.

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
| `session_controller.py` / `session_completion.py` / `completion_action_planner.py` — classify to `TIMED_OUT`/`FAILED` | Consult `IssueRuntimeTermination.validated_work`. When the batch `found_work`, the recorded session outcome and the emitted event carry **every** member disposition; generic `timed_out` is no longer a legal classification for an issue whose batch reports any state in `UNRESOLVED_STATES` (`QUEUED`/`PARKED`/`PUBLISHING`/`FAILED`). |
| `stuck_sweep.py` — "`failure_reason` is always `timed_out`" | Reads the disposition snapshot; a stranded-with-disposition issue is reported as owned recovery, not as an undiagnosed timeout, and is excluded from scratch-reset proposals. |
| `control/tech_lead_reset_retry.py` + the dashboard reset — act on a boolean freshness check and then tear down | The returned `IssueRuntimeTermination.validated_work` is **consumed, not discarded**: a batch whose `.unresolved` is true aborts the reset with a typed stale-downgrade listing **every** blocking member's `state`/`failure`/`evidence_id`, so the operator is told which records blocked and what would resolve each. Ignoring the field is how a reset tears down work the boundary had already recorded; §9's guardrail asserts every call site binds it. |
| Workspace freshness / detached HEAD (#7017) | A `WorkspaceIntegrity` precondition is evaluated **at exchange start** (don't run rounds on a broken workspace) and **at evidence capture**. At capture, a detached HEAD does not invalidate the commits — the resolved sha is pinned — but sets `branch_binding_verified=False`, which forces `PARKED` instead of `QUEUED`. |

### 3.4 Composition root

`entrypoints/bootstrap.py` constructs, in order:

```
SqliteIssueRunLedger(state_dir/"issue_run_ledger.sqlite")                    # §2.5
IssueRunEvidenceService(run_ledger, active_sessions_view)                    # §2.5
SqliteValidatedWorkStore(state_dir/"validated_work.sqlite")
FilesystemValidatedWorkEscrow(state_dir/"validated-work")
GitValidatedHeadExecutor(working_copy, worktree_manager, repository_host)    # §4.4
FencedValidatedHeadPublisher(executor, store)                                # §4.4f
StagedPublishedWorkFinalizer(                                                # §4.5b
    review_routing=RetryReviewRouting, phase_recorder=store,
    fresh_issue_reader=..., action_applier=..., label_manager=...,
)
CoreIssueRuntimeOwners(                                      # the four pre-existing owners
    session_manager, active_sessions, pair_registry, job_supervisor, publish_recovery,
)
OtherRuntimeActivity(core)                                   # §4.3 check 5; no exclusion knob
RepoLockLiveness(repo_root, instance_id)                     # §4.4e gate-backed death proof
ValidatedWorkDispositionService(
    store, escrow, working_copy, worktree_manager, repository_host,
    publisher, finalizer, other_activity, liveness, action_applier, label_manager,
    needs_human_block, events,
)
IssueRuntimeLifecycleOwners(core, validated_work, issue_run_evidence)
    # the FULL five PLUS the run-evidence source; the one value teardown and reset use.
    # `issue_run_evidence` is the SAME instance injected into the launch owner above,
    # so the ledger read at teardown is the ledger written at launch (§2.5).
```

`IssueRunEvidenceService` is also injected into the **launch** owner, which calls
`record_run()` in the same transaction that constructs `SessionRunAssets` — the
write side of §2.5. Both stores are registered in `infra/sqlite_registry.py`.

**The disposition owner does not depend on `PublishRecoveryService`.** That is the
point: the manual Retry Publish service is a *sibling* admission owner, not a
collaborator. Both it and the disposition owner are constructed here and both are
injected with the same two lower-level owners — the publisher and the finalizer —
so neither reaches through the other. `StagedPublishedWorkFinalizer` composes the
existing `RetryReviewRouting` decision rather than reimplementing it, which is what
keeps one review-routing policy in the system; it owns only the *ordering* that
policy is applied in (§4.5b), because the two admission owners genuinely need
different orderings.

The services are exposed on `control/orchestrator_deps.py` as
`validated_work: ValidatedWorkDispositionOwner` and
`issue_run_evidence: IssueRunEvidenceSource`, mirroring how `publish_recovery` is
carried today. Both stores are registered in `infra/sqlite_registry.py` so doctor
checks, backups, and startup maintenance cover them (the precedent set by
`tech_lead_authority.sqlite`).

`drain(state)` is called from the same tick drain point that already calls
`PublishRecoveryService.drain_completed_retries()` and already holds
`OrchestratorState`, so restart reconciliation needs no new scheduler.

### 3.5 History reconciliation carries the disposition; it does not refuse

An earlier draft of this contract required `history_reconciliation` to treat an
unresolved disposition as "a refusal to finish the terminal transition". That
requirement is **withdrawn**, for two independent reasons.

*It is not implementable in that ordering.* `apply_history_reconciliation()`
mutates session history, records the area-tagged shipped fix, and emits
`HISTORY_RECONCILED`/`REVIEW_MERGED` **before** it calls the terminator
(`control/history_reconciliation.py:40-85`). Binding the return value afterwards
cannot un-emit an event or un-append a history entry, so a "refusal" there would be
a refusal that had already happened.

*It is also the wrong policy.* This path fires on an externally observed terminal
PR — merged or closed. Under ADR-0013 that observation is the truth, and the
orchestrator's own history refusing to record it would leave local state
permanently disagreeing with GitHub while the action re-planned every tick. The
terminal transition releases **runtime**, not **evidence**; it was never the thing
that destroyed work. What protects the work here is `has_unresolved_work()` keeping
the issue out of scratch reset (§3.2) and the retention boundary keeping escrow and
refs (§6) — both unchanged by the history mutation.

So the ordering is specified as: **shipped-fix capture → history mutation →
`HISTORY_RECONCILED`/`REVIEW_MERGED` → terminate (capture the batch) →
`VALIDATED_WORK_DISPOSITION_OBSERVED` → return the action result.** Today's order is
preserved exactly; the only change is that the result is no longer discarded.

**The disposition is carried on a post-termination event, not on the history
event.** An earlier draft kept the existing event order *and* put the bound
disposition in the `HISTORY_RECONCILED` payload — which is a payload built and
published before the terminator runs
(`control/history_reconciliation.py:59-85`). That is data that does not exist yet;
no ordering makes it true.

Of the two executable shapes, this design takes the second one:

- *Move the terminal events after termination.* Rejected. It requires a durable
  "these terminal events were already emitted" marker, because the no-op retry
  branch must emit events the failed attempt never got to and must **not** re-emit
  them for an action that merely got re-planned after succeeding. The only candidate
  marker is the session-history entry, which is process-local
  (`OrchestratorState.session_history`) and therefore gone across exactly the
  restart this has to survive. It would trade a stated contradiction for an
  unstated one, and it would regress the current behaviour where a no-op emits
  nothing.
- *Keep the event order; carry the disposition on its own typed event.* Taken.
  `HISTORY_RECONCILED` and `REVIEW_MERGED` keep their present payloads and their
  present mutation-branch-only emission — no consumer changes.
  `VALIDATED_WORK_DISPOSITION_OBSERVED` (§8.3) is emitted after the terminator
  returns, on **both** branches, carrying the batch. It is an observation keyed by
  record and evidence ids, so a re-planned no-op re-emitting it is harmless, which
  is precisely what the history events could not tolerate.

The `ActionResult` also carries the batch — it is built and returned after the
terminator, so there is no contradiction there.

**The terminal PR is then reconciled by the owner** — this, not a refusal, is what
resolves the row:

| Observed | Row transition |
|---|---|
| PR **merged**, and `validated_head_sha` is contained in the merged head | one `resolve_observed_merge()` command (§4.1): `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)`, the `validated_work_lineage` fact advanced to the merged head as `OBSERVED_MERGE`, contained ancestors resolved, and waiters classified — **all in one store transaction**, so no crash can leave the resolution without its fact (§2.1.4). A record that is `PUBLISHING` under a live claim is refused (`PUBLICATION_IN_FLIGHT`) and left to its owner's drain. Without that write this ordinary ending left no published fact, and a later ancestor stranded exactly as it did before the fact existed. The work shipped; nothing is stranded. |
| PR **merged**, `validated_head_sha` **not** contained | `PARKED(PUBLISHED_HEAD_LACKS_VALIDATED_WORK)`. Something else was merged; this work was not. Unresolved, escrow retained, surfaced for a human. |
| PR **closed**, not merged | `QUEUED`/`PUBLISHING` ⇒ `PARKED(PR_CLOSED_OR_MERGED)` — the automatic exit is gone, so only an explicit decision may proceed. `PARKED`/`FAILED` are left where they are. Never auto-resolved. |

**One gap this ordering exposes, closed here.** The terminator can raise (an
unreadable run ledger, an escrow write failure), and today a raise after a
successful mutation is unrecoverable: the retry re-plans the action, the history
owner reports the entry is *already* at the terminal status, and `_log_noop`
returns at `control/history_reconciliation.py:47-57` **without ever reaching the
terminator**. The runtime is never released and the disposition is never captured.
The terminal transition is therefore redefined as "history at terminal status
**and** runtime released **and** disposition captured", and the terminator — which
is idempotent on all three counts — runs on the no-op branch as well as the
mutation branch, followed by `VALIDATED_WORK_DISPOSITION_OBSERVED`. The
`ActionResult` reports `no_op=True` for the history half and still carries the
batch. The history events stay on the mutation branch only, exactly as today.

---

## 4. Durable state

### 4.1 Store schema — `validated_work.sqlite` in `<repo>/.issue-orchestrator/state/`

Four tables, because there are four lifetimes: the **work** (one row per
`record_id`, forever), the **evidence** for that work (many rows, each with a role),
the **publish attempts** against it (many rows, append-only), and the **lineage
fact** (one row per issue+branch, recording what has actually been published there).
The lineage table is defined with its policy in §2.1.4 and repeated here as part of
the schema the store owns:

```sql
CREATE TABLE IF NOT EXISTS validated_work_records (
    record_id             TEXT PRIMARY KEY,           -- canonical ValidatedWorkKey
    repo_slug             TEXT NOT NULL,
    issue_number          INTEGER NOT NULL,
    branch_name           TEXT NOT NULL,
    validated_head_sha    TEXT NOT NULL,
    lineage_key           TEXT NOT NULL,              -- canonical (repo, issue, branch) §2.1.4
    lineage_role          TEXT NOT NULL,              -- LineageRole
    superseded_by_record_id TEXT NOT NULL DEFAULT '', -- the HEAD this ancestor waits on
    waits_on_record_id    TEXT NOT NULL DEFAULT '',   -- §2.1.4 PENDING successor
    owner_fence           INTEGER NOT NULL DEFAULT 0, -- §4.4e; monotonic, never reused
    owner_host            TEXT NOT NULL DEFAULT '',   -- current claim holder
    owner_pid             INTEGER NOT NULL DEFAULT 0,
    owner_started_at      TEXT NOT NULL DEFAULT '',   -- pid reuse guard
    owner_claim_hash      TEXT NOT NULL DEFAULT '',   -- sha256 of the claim secret
    owner_instance_id     TEXT NOT NULL DEFAULT '',   -- repo-lock instance, for the death proof
    owner_claimed_at      TEXT NOT NULL DEFAULT '',   -- diagnostics/UI only, never authority
    stop_reserved_fence   INTEGER NOT NULL DEFAULT -1, -- §8.4; -1 = no reservation
    stop_reserved_engine  TEXT NOT NULL DEFAULT '',    -- the engine the stop targets
    stop_reservation_id   TEXT NOT NULL DEFAULT '',    -- unique, never shared by callers
    stop_reserved_at      TEXT NOT NULL DEFAULT '',
    state                 TEXT NOT NULL,              -- ValidatedWorkState
    failure               TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure
    reason                TEXT NOT NULL DEFAULT '',
    finalization_phase    TEXT NOT NULL DEFAULT 'not_started',  -- FinalizationPhase (§4.5)
    publishing_started_at TEXT NOT NULL DEFAULT '',   -- set by the first attempt claim
    published_head_sha    TEXT NOT NULL DEFAULT '',
    resolution_kind       TEXT NOT NULL DEFAULT '',   -- ResolutionKind
    resolved_by           TEXT NOT NULL DEFAULT '',   -- OperatorResolution.actor
    resolution_reason     TEXT NOT NULL DEFAULT '',
    resolved_at           TEXT NOT NULL DEFAULT '',
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    terminal_at           TEXT NOT NULL DEFAULT ''    -- entry into a RESOLVED state
);

-- One row per admitted evidence, keyed by the id operators and ops name.
CREATE TABLE IF NOT EXISTS validated_work_evidence (
    evidence_id           TEXT PRIMARY KEY,
    record_id             TEXT NOT NULL
                          REFERENCES validated_work_records(record_id),
    role                  TEXT NOT NULL,              -- EvidenceRole
    identity              TEXT NOT NULL,              -- ValidatedWorkIdentity JSON
    observations          TEXT NOT NULL,              -- mutable half of the evidence
    worktree_head_sha     TEXT NOT NULL,              -- observed at capture (§1.1)
    expected_remote_head  TEXT NOT NULL DEFAULT '',   -- '' = no remote branch expected
    pr_number             INTEGER,
    escrow_dir            TEXT NOT NULL,              -- relative to escrow root
    pinned_ref            TEXT NOT NULL,              -- validated commit
    observed_ref          TEXT NOT NULL DEFAULT '',   -- unvalidated worktree head, if any
    observation_revision  INTEGER NOT NULL DEFAULT 0, -- §2.1.1; bumped by every refresh
    -- The disposition THIS evidence was admitted with (§5). Durable because an
    -- attached row may be promoted long after capture, and promoting it as QUEUED
    -- when it was captured PARKED would auto-publish approval-required work.
    -- QUEUED, PARKED **or FAILED**: §5 admits evidence FAILED for
    -- VALIDATION_SHA_MISMATCH and ARTIFACT_*, and that verdict is as durable as
    -- the others. Restricting the column to two states would force a promotion
    -- to invent a third answer.
    initial_state         TEXT NOT NULL,              -- ValidatedWorkState (any admitted state)
    initial_failure       TEXT NOT NULL DEFAULT '',   -- ValidatedWorkFailure, e.g. WORKTREE_AHEAD_OF_VALIDATION
    initial_reason        TEXT NOT NULL DEFAULT '',
    admitted_at           TEXT NOT NULL,
    role_changed_at       TEXT NOT NULL,
    released_at           TEXT NOT NULL DEFAULT ''    -- retention release (§6)
);

-- Exactly one CURRENT evidence per record. Roles are exhaustive and mutually
-- exclusive, so this partial index has no complement to leak through.
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_current_evidence
    ON validated_work_evidence (record_id) WHERE role = 'current';

CREATE INDEX IF NOT EXISTS ix_validated_work_evidence_role
    ON validated_work_evidence (record_id, role, admitted_at);

-- Append-only publish attempts (§4.4e). The row is written BEFORE the remote call.
CREATE TABLE IF NOT EXISTS validated_work_publish_attempts (
    record_id             TEXT NOT NULL
                          REFERENCES validated_work_records(record_id),
    attempt_no            INTEGER NOT NULL,           -- 1-based, contiguous
    evidence_id           TEXT NOT NULL,
    target_head_sha       TEXT NOT NULL,
    expected_remote_head  TEXT NOT NULL DEFAULT '',
    phase                 TEXT NOT NULL,              -- DispositionPhase at claim time
    fence                 INTEGER NOT NULL,           -- the owner_fence that claimed it
    started_at            TEXT NOT NULL,
    outcome               TEXT NOT NULL DEFAULT '',   -- '' = no outcome yet (in flight or crashed)
    failure               TEXT NOT NULL DEFAULT '',
    finished_at           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (record_id, attempt_no)
);

-- The activity/reset probe's index. UNRESOLVED includes 'failed'.
CREATE INDEX IF NOT EXISTS ix_validated_work_unresolved
    ON validated_work_records (issue_number, state)
    WHERE state IN ('queued', 'parked', 'publishing', 'failed');

-- At most one drainable row per issue+branch lineage (§2.1.4).
CREATE UNIQUE INDEX IF NOT EXISTS ux_validated_work_lineage_head
    ON validated_work_records (lineage_key)
    WHERE state IN ('queued', 'publishing');

CREATE INDEX IF NOT EXISTS ix_validated_work_lineage
    ON validated_work_records (lineage_key, state);

-- What has actually been published for this issue+branch (§2.1.4). Advanced by
-- BOTH verified publication routes: our own push, and an observed merged PR.
CREATE TABLE IF NOT EXISTS validated_work_lineage (
    lineage_key                 TEXT PRIMARY KEY,
    published_head_sha          TEXT NOT NULL DEFAULT '',
    published_by_record_id      TEXT NOT NULL DEFAULT '',
    published_via               TEXT NOT NULL DEFAULT '',  -- PublicationProvenance
    published_pre_push_expected TEXT NOT NULL DEFAULT '',  -- '' under OBSERVED_MERGE
    published_at                TEXT NOT NULL DEFAULT ''
);

-- Successors parked behind a publishing predecessor (§2.1.4).
CREATE INDEX IF NOT EXISTS ix_validated_work_waiters
    ON validated_work_records (waits_on_record_id)
    WHERE waits_on_record_id != '';

CREATE INDEX IF NOT EXISTS ix_validated_work_issue
    ON validated_work_records (issue_number, state);
```

Rows are **append-then-transition**, never deleted by normal operation.

**Why evidence is a relation and not two columns.** The previous shape stored the
current `evidence_id` on the record and everything else in a JSON array, which
cannot express what the contract promises:

- `ux_validated_work_evidence` indexed only the *current* id, so an operator handle
  naming superseded evidence resolved to nothing at all — the refusal message §2.1.3
  promises ("this evidence was superseded by `<id>`") had no row to read it from.
- An `ATTACHED` capture had **no durable representation whatsoever**: it existed only
  as an escrow directory on disk. The orphan scan that could have found it runs once
  per process start, so the "next drain re-admits it" promise had no query behind it
  inside a single process — the common case, since the in-flight submission usually
  resolves seconds later.
- Retention and approval identity could not be enforced relationally for anything
  but the current evidence.

With the relation, all three are ordinary queries by primary key or by
`(record_id, role)`, and no JSON is scanned on any policy path.

```python
class EvidenceRole(StrEnum):
    CURRENT    = "current"     # what the record is acting on; exactly one per record
    ATTACHED   = "attached"    # admitted during PUBLISHING; drained after it resolves
    SUPERSEDED = "superseded"  # was current; retained, never acted on again
```

Store API over the relation — every one of these is exact, none scans:

```python
def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None: ...
    """By primary key. Returns the evidence row, its role, AND its owning record."""

def attached_evidence(self, record_id: str) -> tuple[EvidenceRow, ...]: ...
    """role='attached', oldest first. Queried by drain() once the record leaves PUBLISHING."""

def evidence_for_retention(self, *, released_before: str) -> tuple[EvidenceRow, ...]: ...
    """Every role, for records whose terminal_at is past the window (§6)."""

def lineage_publication(self, lineage_key: str) -> LineagePublication | None: ...
    """The durable published fact (§2.1.4). Read by EVERY classification."""
```

**Resolution is one store-owned command per verified route, not a sequence the
caller is trusted to complete.** An earlier draft exposed
`advance_lineage_publication(claim_or_txn, ...)` beside the record transition. That
handed the invariant to the consumer: it could resolve the record and then advance
the fact (or the reverse), and a crash between the two recreated the exact
arrival-order stranding §2.1.4 exists to close — a resolved record with no published
fact. It also left ancestor resolution and waiter classification as two further
mutations somebody had to remember. And `claim_or_txn` leaked a transaction handle
through the port, which means the store no longer owns its own atomicity.

So there is no standalone advance, and no transaction handle crosses the boundary.
Each route is a single command that performs **all four** effects in the store's own
transaction:

```python
@dataclass(frozen=True, slots=True)
class PublicationResolution:
    """Everything the four effects produced — no re-read needed to emit events."""
    record: ValidatedWorkDisposition                    # the subject, now RECOVERED
    lineage: LineagePublication                         # the fact after advancing
    resolved_ancestors: tuple[ValidatedWorkDisposition, ...]
    failed_ancestors: tuple[ValidatedWorkDisposition, ...]   # escrow no longer verifies
    classified_waiters: tuple[ValidatedWorkDisposition, ...]


class LineageResolutionRefusal(StrEnum):
    STALE_CLAIM          = "stale_claim"           # fence lost; nothing written
    PUBLICATION_IN_FLIGHT = "publication_in_flight" # a live claim owns this record
    NOT_A_DESCENDANT     = "not_a_descendant"      # the fact only moves forward
    CONTAINMENT_UNPROVEN = "containment_unproven"   # merged head does not contain it


def resolve_published(
    self,
    claim: ValidatedWorkClaim,          # the §4.4e owner; fence-gated
    *,
    record_id: str,
    published_head_sha: str,
    pre_push_expected: str,
    finalized_at: str,
) -> PublicationResolution | LineageResolutionRefusal: ...
    """Route 1: we pushed it. ONE transaction: RECOVERED(PUBLISHED) + advance
    the fact as PUSHED_BY_OWNER + resolve verified contained ancestors + classify
    waiters (§2.1.4)."""


def resolve_observed_merge(
    self,
    *,
    record_id: str,
    merged_head_sha: str,
    observed_at: str,
) -> PublicationResolution | LineageResolutionRefusal: ...
    """Route 2: a merged PR contains it (§3.5). Same four effects in one
    transaction, advancing the fact as OBSERVED_MERGE with no pre-push baseline.

    Takes no claim because no publication attempt was made — and therefore
    REFUSES with `PUBLICATION_IN_FLIGHT` when the record is `PUBLISHING`, so it
    can never resolve a record out from under a live owner. The drain reconciles
    that case itself against the merged PR.
    """
```

Both commands verify ancestry through the injected typed predicate (the same one
slice 1 uses, so the store stays testable without a repository) *inside* the
transaction, and both refuse rather than write when the head is not a descendant of
the recorded fact. Because the four effects share one transaction, neither "a
resolved record without its lineage fact" nor "a lineage fact without its matching
resolution" is an observable state — not merely an unlikely one.

An approval (tech-lead op or Control Center command) naming an `evidence_id` whose
role is not `CURRENT` is **refused** by `evidence_for_id()`'s role, with the current
id named in the refusal — consent is to publish a specific commit and a specific set
of artifacts, and consent does not transfer.

**`record_id` as the primary key is the deduplication rule made unrepresentable to
violate.** The rule from #6914 — one recovery per *target repository + issue +
branch + validated HEAD* — is the definition of `ValidatedWorkKey`, so "two rows
for the same work" cannot exist at any state, in any order, under any race. A
partial unique index over a subset of states cannot say this: whichever states it
excludes become a hole through which a second row for the same work arrives, and
the excluded row then sits unresolved forever behind the rival that replaced it.
Evidence moves out of the record entirely, into its own relation, because it
identifies *which evidence the row is acting on* — a role that changes over the
record's life (supersede, attach, retain) without the work's identity changing at
all. §2.1.3 defines exactly how the role moves; §2.1.4 defines the one other
multi-row relationship, between different validated heads of the same branch.

`ix_validated_work_unresolved` is the index behind `has_unresolved_work()`, and it
covers `failed` because a failed disposition is work that still exists (§3.2).

### 4.2 State machine

```
  termination ──► admission ──► (empty batch)               (nothing admissible found)
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

Lineage adds one gate in front of this machine rather than a state to it: only a
row whose `lineage_role` is `HEAD` (§2.1.4) is ever drained, an `ANCESTOR` sits in
`PARKED(ANCESTOR_OF_PENDING_HEAD)` until its head publishes (then `RECOVERED` by
containment), and a `DIVERGENT` row sits in `PARKED(DIVERGENT_VALIDATED_HEADS)`
until an operator promotes one. Every such row is unresolved throughout, so nothing
becomes reset-eligible by waiting.

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

**Check 0 — the authority gate, and why it precedes everything.** A
`StoredEvidenceCommand` (approved tech-lead op or operator command) carries a
`ValidatedWorkAuthoritySnapshot` (§2.2.1). Before any other check runs, every field
of that snapshot must be **exactly equal** to the current durable record and
evidence — including `observation_revision`, `pr_number`, and
`expected_remote_head_sha`. Any inequality is a **stale downgrade with zero
writes**: `FAILED`-free, state unchanged, `AUTHORITY_SNAPSHOT_STALE` reported to the
initiator, and the operator is told which fact moved so they can re-approve against
the new one.

This is a different question from the checks below it, which is why it cannot be
folded into them. Checks 1–9 ask *is it safe to publish these facts now*; check 0
asks *are these the facts someone authorized*. A refreshed observation can be
perfectly safe and still not be what was approved — the approver consented to
landing a commit onto a specific remote baseline, against a specific PR. The
snapshot is authorization input only: once it matches, every check below still runs
against freshly read state, so it never becomes a competing source of truth or an
excuse to skip revalidation.

The automatic initiator has no snapshot to check, because it has no approver — its
authority is the boundary itself, and check 0 is skipped for `AutomaticCaptureCommand`.

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
5. **No other runtime owner is active** — see the self-exclusion rule below.
6. The row's `record_id` still matches the key being acted on, the evidence this
   drain admitted still holds role `CURRENT` (§4.1), and the row's `lineage_role`
   is still `HEAD` (§2.1.4) — i.e. no supersession and no newer descendant landed
   underneath us. Re-read inside the same transaction that claims the publish
   attempt; a mismatch aborts the drain step with no writes.

**Check 5 must exclude the caller's own probe, and the predicate must be able to
say so.** A record being drained is by definition in `UNRESOLVED_STATES`, so
`has_unresolved_work()` — the fifth probe this design adds (§3.2) — returns `True`
for it. A check 5 that consulted the whole probe set would therefore observe the
owner's own record, abort the drain, and do so again on every subsequent drain: the
contract would never publish anything, and the "automatic recovery" initiator would
be dead on arrival. The self-exclusion is not an implementation detail to be left to
the implementer — today `has_active_issue_runtime()` builds its probes as a fixed
local tuple with no way to express it (`control/review_exchange_lifecycle.py:216-229`),
so the capability has to be part of this contract:

```python
class IssueRuntimeOwnerKind(StrEnum):
    SESSIONS       = "sessions"
    PAIR_REGISTRY  = "pair_registry"
    SUPERVISED_JOB = "supervised_job"
    PUBLISH_RETRY  = "publish_retry"
    VALIDATED_WORK = "validated_work"


class IssueRuntimeLifecycleOwners:                # §3.2; the FULL five, as a value
    def has_active_issue_runtime(self, issue_number: int) -> bool: ...
```

The probe tuple becomes a `Mapping[IssueRuntimeOwnerKind, Callable[[], bool]]` and
the predicate evaluates **every** kind, always. It has no exclusion parameter,
because there is no caller that wants a narrower answer from *this* function: the
one that does asks `OtherRuntimeActivityPort` instead, an object whose owner bundle
simply lacks the validated-work probe.

That is deliberately not a constrained parameter. A parameter guarded by "only one
caller may pass it" is a lever every future caller can still reach and a guardrail
that has to keep catching them; a bundle that does not contain the owner cannot be
asked to skip anything else in the first place.

**The disposition service must not gather that owner set itself.** The full
predicate needs the session manager, the active-session registry, the pair registry,
the background job supervisor, and the publish-retry owner. The disposition service is
injected with none of them (§3.4), `drain()` receives only `OrchestratorState`, and
§4.6 explicitly forbids it from depending on `PublishRecoveryService` at all. Left
as "call `has_active_issue_runtime(...)`", an implementer's only options were to
reach into four sibling owners, silently drop part of check 5, or invent a closure
in bootstrap that nobody reviewed — each of which defeats the bounded-owner rule the
rest of this contract is built on, and the second of which lets publication race a
live session.

So the capability is a **behavior-level seam owned by the issue-runtime lifecycle
boundary** — the module that already owns both `terminate_issue_runtime()` and
`has_active_issue_runtime()` — and the disposition service is injected with that
seam and nothing else:

```python
@dataclass(frozen=True, slots=True)
class IssueRuntimeActivity:
    """Which issue-runtime owners are live. Facts only; no policy."""
    issue_number: int
    active_owners: tuple[IssueRuntimeOwnerKind, ...]
    unverifiable_owners: tuple[IssueRuntimeOwnerKind, ...]   # probe raised (fail-safe)

    @property
    def any_active(self) -> bool:
        return bool(self.active_owners or self.unverifiable_owners)


class OtherRuntimeActivityPort(Protocol):
    """The activity of every issue-runtime owner OTHER than validated work.

    Deliberately has **no** ``excluding`` parameter. An exclusion argument is
    the one lever in this contract that can make an activity probe stop looking
    at an owner, and a public parameter offers it to every future caller — a
    caller who suppressed SESSIONS here would let publication race a live agent.
    The self-exclusion is structural instead: this port is constructed from a
    bundle that does not contain the validated-work owner, so there is nothing
    to exclude and no way to ask for more.
    """

    def other_runtime_activity(self, issue_number: int) -> IssueRuntimeActivity: ...
```

**The bundles are the call boundary, not a convention about what to pass.** Leaving
`terminate_issue_runtime()` and `has_active_issue_runtime()` as free functions with
one parameter per owner means a future call site can still assemble its own set —
or omit one — and a bootstrap identity assertion cannot prevent that, because the
callable surface still accepts the pieces. So the free functions are replaced by
methods on the bundles, and the pieces stop being separately passable:

```python
@dataclass(frozen=True, slots=True)
class CoreIssueRuntimeOwners:
    """The four pre-existing issue-runtime owners. The ONLY place they are read."""

    session_manager: "SessionManager | None"
    active_sessions: list["Session"] | None
    pair_registry: "PersistentExchangePairRegistry | None"
    job_supervisor: "BackgroundJobSupervisor | None"
    publish_recovery: "IssuePublishRetryRuntime | None"

    def probe(self, issue_number: int) -> IssueRuntimeActivity: ...
    """Evaluate all four probes. Both views below derive from THIS method."""

    def terminate(self, issue_number: int, reason: str) -> CoreTermination: ...
    """Release all four. The teardown counterpart of `probe`, same owner set."""


@dataclass(frozen=True, slots=True)
class OtherRuntimeActivity:                      # implements OtherRuntimeActivityPort
    core: CoreIssueRuntimeOwners

    def other_runtime_activity(self, issue_number: int) -> IssueRuntimeActivity:
        return self.core.probe(issue_number)     # no exclusion argument exists


@dataclass(frozen=True, slots=True)
class IssueRuntimeLifecycleOwners:
    """The FULL five, plus the run-evidence source teardown needs at step 0."""

    core: CoreIssueRuntimeOwners
    validated_work: ValidatedWorkDispositionOwner
    run_evidence: IssueRunEvidenceSource

    def has_active_issue_runtime(self, issue_number: int) -> bool: ...
    def terminate_issue_runtime(self, issue_number: int, reason: str) -> IssueRuntimeTermination: ...
```

Three consequences:

- **`core.probe()` is the single evaluation site.** The "other runtime" view and the
  full predicate both call it; neither unpacks the bundle. Adding a sixth core owner
  is one edit, and it cannot reach one view and miss the other.
- **`IssueRunEvidenceSource` lives on the bundle deliberately**, not outside it.
  §3.1 step 0 needs it at every termination, and three of the four call sites hold
  only an issue number — leaving it a separate parameter is exactly the
  reconstruction problem in miniature, one that would end in a worktree scan.
- **`_ResetRetryRuntimeOwners`** becomes a thin holder of the same
  `IssueRuntimeLifecycleOwners` value rather than its own parallel owner list, so
  the dashboard reset and the tech-lead reset call the same two methods every other
  caller does.

The §9 guardrail is updated to match: no module calls a free
`terminate_issue_runtime`/`has_active_issue_runtime`, and no module outside
bootstrap constructs `CoreIssueRuntimeOwners` or `IssueRuntimeLifecycleOwners` —
rejecting *individual-owner reconstruction*, not merely an exclusion parameter.

**The construction is layered, because the naive single value is cyclic.**
`has_active_issue_runtime()` requires the validated-work owner (§3.2), and the
validated-work service requires an activity seam — so one `IssueRuntimeLifecycleOwners`
serving both would have to exist before itself. Bootstrap therefore builds three
things in order, each immutable:

```
1. core = CoreIssueRuntimeOwners(session_manager, active_sessions,
                                 pair_registry, job_supervisor, publish_recovery)
2. other_activity = OtherRuntimeActivity(core)        # implements the port above
3. validated_work = ValidatedWorkDispositionService(..., other_activity, ...)
4. lifecycle = IssueRuntimeLifecycleOwners(core, validated_work, run_evidence)
                                                      # the FULL five + evidence source
```

`lifecycle` is what `terminate_issue_runtime()`, `has_active_issue_runtime()` and
`_ResetRetryRuntimeOwners` (§3.2) use; `other_activity` is what the disposition
service uses. Both are built from **the same `core` value**, passed by reference and
never copied, so the four shared owners cannot drift — the property §1.2 says the
whole design rests on — while the fifth is added exactly where it belongs and
nowhere else. The disposition service never sees a pair registry, a supervisor, a
session manager, or the publish-retry owner; it sees one port returning one typed
fact.

So check 5 reads, in full: `other_runtime_activity(issue).any_active` is false. A
raising probe still counts as active (`unverifiable_owners`, fail-safe). Another
issue-scoped owner being live blocks the drain (`RUNTIME_ACTIVE`, retried next
drain, and the blocking kinds are *named* in the result so the reason is legible) —
publication must never race a live session — but the owner's own record cannot block
itself, because the bundle it is asking about does not contain it.

With the self-exclusion made structural, `has_active_issue_runtime()` needs no
`exclude_owners` parameter at all: it always evaluates all five owners, and the one
caller that needed a narrower question now asks a different, narrower object. The
parameter is removed rather than merely constrained — a lever that does not exist
cannot be pulled by a future caller.

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
`ALREADY_AT_TARGET` when both are at `validated_head_sha`, `PUBLISHED` when they
were at the expectation and the write landed, and refuses on any mixture it cannot
classify.

### 4.4 The publication boundary — `ValidatedHeadExecutor`

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
routing, no `OrchestratorState`, no `PublishRetryLocators`.

**Publication is synchronous.** An earlier draft had `publish_or_reconcile()` also
able to return `SUBMITTED`, with a `submission_status(token)` whose answer was
"durable across restart" — but nothing in the design owned that durability. There
was no job runner, no submission table, and no claim owner, so the port promised an
asynchronous lifecycle that no component implemented. The retry story contradicted
itself for the same reason: `submission_token` was *derived and stable* so a retry
would find the prior submission, yet a transient failure was supposed to re-invoke
the publisher with that same token — an idempotency key that permanently identifies
the failed submission cannot also identify the new one.

Both problems have one root: two identities were doing one job. The fix separates
them. The **operation** is identified by `(record_id, target_head_sha)` and never
changes; each **attempt** at that operation is a durable, numbered row written
*before* the external call. So a retry is a genuinely new attempt with its own
identity, the record still converges on one published head, and the port itself
needs no lifecycle at all — it makes one call and returns what happened:

```python
class RemoteHeadExpectation(StrEnum):
    EXACT         = "exact"          # remote must be at expected_remote_head_sha
    ABSENT        = "absent"         # branch must not exist
    UNCONSTRAINED = "unconstrained"  # manual retry: no captured expectation


# The executor below is deliberately free of admission-specific authority types.
# See §4.4f for why, and for the fenced wrapper the disposition owner puts above it.


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


class PublishValidatedHeadStatus(StrEnum):
    PUBLISHED         = "published"           # remote ref is now at target
    ALREADY_AT_TARGET = "already_at_target"   # remote ref was ALREADY at target
    REJECTED          = "rejected"            # preconditions unmet; nothing written
    DIVERGED          = "diverged"            # lease/FF refused; nothing written
    TRANSIENT_FAILURE = "transient_failure"   # network/5xx/auth; retryable, nothing written
    SUPERSEDED        = "superseded"          # claim lost mid-sequence (§4.4f); this caller stops


@dataclass(frozen=True, slots=True)
class PublishValidatedHeadOutcome:
    status: PublishValidatedHeadStatus
    observed_remote_head_sha: str | None
    pr_number: int | None                 # the ONE PR for this branch, ensured
    pr_url: str | None
    pr_head_sha: str | None
    push_outcome: ExactPushOutcome | None  # None when no push was attempted
    failure: ValidatedWorkFailure | None
    message: str

    @property
    def retryable(self) -> bool:
        """The transient/definitive split, by enum — never by substring match."""
        return self.status is PublishValidatedHeadStatus.TRANSIENT_FAILURE


class BranchWriteStatus(StrEnum):
    PUSHED            = "pushed"              # we moved the ref to the target
    ALREADY_AT_TARGET = "already_at_target"   # it was there; no write
    DIVERGED          = "diverged"            # lease/FF refused; nothing written
    REJECTED          = "rejected"            # preconditions unmet; nothing written
    TRANSIENT_FAILURE = "transient_failure"   # network/5xx/auth; nothing written


@dataclass(frozen=True, slots=True)
class BranchWriteOutcome:
    status: BranchWriteStatus
    observed_remote_head_sha: str | None
    push_outcome: ExactPushOutcome | None    # None when no push was attempted
    failure: ValidatedWorkFailure | None
    message: str

    @property
    def at_target(self) -> bool:
        """The branch IS at the target commit — the only gate to PR ensure."""
        return self.status in (
            BranchWriteStatus.PUSHED, BranchWriteStatus.ALREADY_AT_TARGET
        )


class PrEnsureStatus(StrEnum):
    RECONCILED        = "reconciled"          # the recorded PR is open on this branch
    ADOPTED           = "adopted"             # an existing open PR was adopted
    CREATED           = "created"             # exactly one PR was created
    REFUSED           = "refused"             # closed/merged, branch mismatch, duplicate
    TRANSIENT_FAILURE = "transient_failure"


@dataclass(frozen=True, slots=True)
class PrEnsureOutcome:
    status: PrEnsureStatus
    pr_number: int | None
    pr_url: str | None
    pr_head_sha: str | None
    failure: ValidatedWorkFailure | None
    message: str


class ValidatedHeadExecutor(Protocol):
    """Remote execution. Two steps, each with its OWN result type."""

    def push_validated_head(
        self, command: PublishValidatedHeadCommand
    ) -> BranchWriteOutcome: ...
    """The branch write of §4.4b, and nothing else."""

    def ensure_pull_request(
        self, command: PublishValidatedHeadCommand
    ) -> PrEnsureOutcome: ...
    """The PR ensure/adopt/create of §4.4d, and nothing else.

    Called only when the branch is proven at the target: it neither observes
    nor reports branch status, which is why its result cannot carry one.
    """

    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome: ...
    """Branch write (§4.4b) then PR ensure/reconcile. Synchronous and idempotent.

    DEFINED as the composition below, not merely "does both" — so the manual
    combined path and the fenced two-step path (§4.4f) cannot diverge:

        branch = self.push_validated_head(command)
        pr = self.ensure_pull_request(command) if branch.at_target else None
        return compose_publication_outcome(branch, pr)

    Returns only when the remote work for this attempt has finished or
    definitively failed. There is no in-flight state to poll: a process that
    dies mid-call leaves an attempt row with no recorded outcome, and §4.4e
    reconciles that against the remote rather than asking the publisher.
    """


class SupersededStage(StrEnum):
    """WHERE the claim was lost. Both points are real; both need a result."""
    BEFORE_BRANCH_WRITE = "before_branch_write"   # nothing was attempted at all
    BETWEEN_STEPS       = "between_steps"         # the branch effect stands


def superseded_outcome(
    stage: SupersededStage,
    branch: BranchWriteOutcome | None,   # required iff stage is BETWEEN_STEPS
) -> PublishValidatedHeadOutcome: ...
    """The stale-owner result, WITHOUT fabricating a branch outcome.

    Loss before the branch write has no branch result to report — there was no
    branch write — so `BranchWriteStatus` gains no phantom `NOT_ATTEMPTED`
    member to satisfy a composer that should never have been called. The two
    cases differ in exactly one observable way, and the type says so:
    `observed_remote_head_sha`/`push_outcome` are None before the write and
    carry the branch stage's values after it.
    """


def compose_publication_outcome(
    branch: BranchWriteOutcome,
    pr: PrEnsureOutcome | None,          # None when the branch is not at target
) -> PublishValidatedHeadOutcome: ...
    """Composes two REAL stage results. Supersession is not one of its inputs."""
```

**The composer is the single place stage results become a publication result**, and
it is total over the pairs that can occur:

| `branch.status` | `pr` | Composed `PublishValidatedHeadStatus` |
|---|---|---|
| `PUSHED` | `RECONCILED`/`ADOPTED`/`CREATED` | `PUBLISHED` |
| `ALREADY_AT_TARGET` | `RECONCILED`/`ADOPTED`/`CREATED` | `ALREADY_AT_TARGET` |
| `PUSHED`/`ALREADY_AT_TARGET` | `REFUSED` | `REJECTED`, carrying the PR `failure` — the branch is at target, the PR is not usable |
| `PUSHED`/`ALREADY_AT_TARGET` | `TRANSIENT_FAILURE` | `TRANSIENT_FAILURE` — retried; the next attempt's branch step returns `ALREADY_AT_TARGET` and only the PR step re-runs |

| `DIVERGED` | `None` | `DIVERGED` |
| `REJECTED` | `None` | `REJECTED` |
| `TRANSIENT_FAILURE` | `None` | `TRANSIENT_FAILURE` |

The table is total over the pairs `compose_publication_outcome()` can actually
receive: `pr` is non-`None` exactly when `branch.at_target`, and supersession never
reaches it. `observed_remote_head_sha` and `push_outcome` always come from the branch stage;
`pr_number`/`pr_url`/`pr_head_sha` from the PR stage when it ran, and from the
command's recorded `pr_number` otherwise. `PublishValidatedHeadStatus` gains
`SUPERSEDED` for the row above — the fenced wrapper needs a real member of the
result type rather than an ad-hoc value, since its caller maps every status to a
`ValidatedWorkFailure` or to a state transition.

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

#### 4.4e Attempt identity and durable ordering

**The operation is stable; the attempts are numbered.** The operation this record
is trying to perform is "publish `target_head_sha` to `branch_name`", identified by
`(record_id, target_head_sha)` and unchanging for the life of the record. Each
*attempt* at it is a row in `validated_work_publish_attempts` (§4.1) with a
contiguous `attempt_no`, and **the row is written before the external call, in the
same transaction that claims the right to make it**:

```python
def acquire_claim(
    self,
    record_id: str,
    *,
    expected_states: frozenset[ValidatedWorkState],  # {QUEUED, PARKED} or {PUBLISHING}
    evidence_id: str,                # must still be the CURRENT evidence
    liveness: OrchestratorLivenessPort,   # gate-backed death proof (see below)
) -> ValidatedWorkClaim | None: ...
    """Take exclusive ownership of the record for its whole PUBLISHING phase.

    One transaction: admit the caller only when the record is unowned or its
    recorded owner is ``liveness.is_provably_dead``. Then generate a fresh
    ``ClaimSecret``, store only its sha256, CAS `owner_fence = owner_fence + 1`,
    record ``liveness.current()`` as the owner, and return the claim carrying
    the new fence and the secret. Returns None whenever death is not proven —
    including "this process already owns it", which is impossible in practice
    because a live owner keeps its claim in memory (below).

    The secret — not the fence — is what makes the result a capability: the row
    stores only its hash, so no reader of the row can construct a claim the
    predicates accept. There is no `now`/`lease` parameter: elapsed time never
    authorizes anything here.
    """

def relinquish_claim(self, claim: ValidatedWorkClaim) -> bool: ...
    """Called BY THE OWNER at a stage boundary — graceful shutdown, or a drain
    that finished a tick — so nothing can be in flight. Clears the owner and
    bumps the fence, letting the next drain proceed without waiting for a death
    proof. There is no counterpart that takes a claim from someone else.

    Returns False while a §8.4 stop reservation is live for
    this fence: an operator is stopping this engine *because* it owns this
    record, and letting it hand the claim off mid-flight would make that stop
    hit an engine that no longer owns it."""

def begin_publish_attempt(
    self,
    claim: ValidatedWorkClaim,
    *,
    expected_attempt_no: int,        # the highest attempt this drainer observed
    target_head_sha: str,
    expected_remote_head: str,
    phase: DispositionPhase,
    started_at: str,
) -> PublishAttempt | None: ...
    """Fence-gated. INSERT attempt `expected_attempt_no + 1` under the claim
    and, on the first attempt, CAS the record into PUBLISHING. Returns None for
    a stale claim or a lost race."""

def record_attempt_outcome(
    self,
    claim: ValidatedWorkClaim,
    attempt: PublishAttempt,
    *,
    outcome: PublishValidatedHeadStatus,
    failure: ValidatedWorkFailure | None,
    finished_at: str,
) -> bool: ...
    """CAS on `owner_fence == claim.fence`. Returns False for a stale claim,
    whose outcome is DISCARDED — a late-returning publisher must not overwrite
    the outcome of the owner that superseded it."""

def holds_claim(self, claim: ValidatedWorkClaim) -> bool: ...
    """Verify secret hash, fence, recorded owner, AND that the calling process
    IS that owner. Re-read immediately before every external mutation and
    before every finalization stage. False aborts, with no remote write and no
    in-memory routing mutation."""

def owner_of(self, record_id: str) -> ProcessIdentity | None: ...
    """Diagnostics and the UI's "owned by engine X, still running" state.
    Never an authorization input."""

# NOTE: `relinquish_claim()` above is the ONLY release command. There is no
# separate `release_claim()`: terminal cleanup and graceful hand-back are the same
# transition — the owner, at a quiescent point, giving up a record it is done
# with — and two commands for it would be two places to forget. Ownership is NOT
# released by a successful attempt outcome; finalization is still ahead.
```

#### The fence, and why a timestamp could never have been the proof

The attempt row alone does **not** give "exactly one active attempt", and an
earlier draft's answer — a lease on the record — could not either. A lease is a
timestamp, and a timestamp expiring proves only that time passed: a slow process can
still be inside `publish_or_reconcile()` and return from it afterwards. If a rival
drainer starts attempt *n+1* while attempt *n* is still executing, two callers can
reach PR ensure and finalization, and the stale one can record a late outcome over
the winner's. The exact ref lease (§4.4b) bounds the damage to the branch write; it
says nothing about PR creation, label routing, history mutation, or outcome
recording. **No lease exists anywhere on this path** — not on the record, not on the
attempt — and the takeover rule below replaces it with proof.

So the correctness mechanism is a **fence plus a proof of death**, and there is no
lease on the record at all — elapsed time authorizes nothing here (see the takeover
rule below). Two properties make it work, and the second is the one an
attempt-scoped fence was missing:

```python
@dataclass(frozen=True, slots=True)
class ValidatedWorkClaim:
    """Exclusive ownership of ONE record for the WHOLE of its PUBLISHING phase.

    Record-scoped, not attempt-scoped: ownership must survive from the first
    attempt through `FinalizationPhase.COMPLETE`, because publication is only
    half the mutation. Routing, history append and the staged label sequence
    happen after the push outcome is durable, and they are exactly as
    single-owner as the push.

    Obtainable ONLY from `acquire_claim()`, which is the only place the secret
    below exists in plaintext.
    """
    record_id: str
    fence: int                  # monotonic per record; never reused, never decreases
    secret: ClaimSecret         # opaque; only its HASH is stored (see below)
    owner: ProcessIdentity      # host, pid, process start time — checked on every call
```

**A fence bump alone is not a capability, and the previous draft was wrong to say
it was.** The claim's fields were `(record_id, fence)`, both plain columns; after
the bump the *new* fence is just as readable as the old one, so any process could
read the current value, construct the exact struct the store accepts, and pass. The
sentence "acquiring changes the value it would have copied" only defeats a rival
who copied *before* the bump — not one who reads after it. So the acquisition proof
has to be something the row does not disclose:

- `acquire_claim()` generates a random `ClaimSecret` (a 256-bit token), stores
  **only `sha256(secret)`** in `records.owner_claim_hash`, and returns the secret
  in the claim to the acquirer alone. The row therefore contains no value from
  which a valid claim can be built.
- `holds_claim(claim)` and every fenced mutation verify, in one transaction:
  `sha256(claim.secret) == owner_claim_hash` **and** `claim.fence == owner_fence`
  **and** `claim.owner == records.owner_*` **and** `claim.owner` equals the *real
  calling process* (`ProcessIdentity.current()`). A claim value that leaked into
  another process — through a bug, a serialized payload, a shared fixture — is
  refused by the last check even though it holds the secret.
- The fence remains, because it gives every takeover a monotonic generation to log
  and to reason about; it is no longer asked to be the secret.

This matters even though the drain is single-process (below): several processes do
open this state directory — the control API, CLI tools, every xdist worker — and
the store must be safe against any of them writing, not merely against the ones the
happy path expects.

That closes the window an attempt-scoped fence left open. Previously, once an
attempt recorded `PUBLISHED` the record stayed `PUBLISHING` with its fence
unchanged, so a second drainer could read the successful outcome, rebuild the same
claim, pass the fence check, and finalize alongside the original owner — two
processes appending review candidates, completed history and label sequences under
one fence. `record_finalization_phase()` accepting both was never exclusivity,
because both presented the same, still-current, publicly readable value.

- **Ownership spans claim → terminal.** It is taken before the first attempt and
  released only when the record reaches `RECOVERED`/`FAILED`/`ABANDONED` (or is
  taken over). Recording a successful attempt outcome does **not** release it.
- **Every durable write takes the claim and compare-and-sets on `owner_fence` in
  the same transaction** — `begin_publish_attempt()`, `record_attempt_outcome()`,
  `record_pr_number()`, `record_finalization_phase()`,
  `resolve_attached_evidence()`, `classify_lineage_waiters()`, and the terminal
  transition. A stale claim's write is **rejected**, not merged.
- **The fence is re-checked immediately before each external mutation** — before
  the push, before PR ensure, and before each finalization stage. A stale owner
  aborts there and performs no remote write and no in-memory routing mutation.
- **Ownership persists until death or relinquish.** There is nothing to renew: a
  slow external call cannot cost the owner its claim, because no clock is consulted.
  A drain that finishes a tick with the record still `PUBLISHING` keeps the claim in
  memory and continues on the next tick.
- **Takeover bumps the fence**, so it permanently invalidates the previous owner in
  the same act that authorizes the new one. There is no moment when both are valid,
  whatever the old process is doing.

The recheck-then-mutate window is still not atomic, so the two external mutations
are each independently fenced by the remote itself, and this is why those two and
no others are performed here:

| External write | What makes a stale duplicate harmless |
|---|---|
| Branch push | `--force-with-lease=refs/heads/<branch>:<expected>` (§4.4b). Whichever caller lands first moves the ref; the other's lease no longer matches and it writes nothing. Exactly one push lands, always. |
| PR create | GitHub permits at most one **open** PR per (head, base) pair. A racing create is rejected by the remote, which the publisher maps to "adopt the existing PR" (§4.4d) rather than to a failure. |
| Label add/remove | Idempotent by construction, and the *phase* that decides whether to apply them is fence-gated, so a stale caller cannot advance the staged machine. |

If PR-ensure ever observes **two** open PRs for the branch carrying the
orchestrator body marker, that is `FAILED(DUPLICATE_OPEN_PR)` — reported, never
silently resolved by picking one.

**Takeover requires positive proof the owner is gone. There is no grace timer, and
no way to take a claim from a living process.**

The earlier draft allowed takeover after a grace period and leaned on the fence for
safety. That was not sound: `holds_claim()` and the mutation it guards are not
atomic, so a former owner can pass the check, lose the fence a microsecond later,
and still perform the *next* effect. For the remote writes that is harmless — the
ref lease and GitHub's one-open-PR rule make them converge — and label writes are
idempotent. But §4.5b stage 1 also appends the review candidate and the
completed-history entry to `OrchestratorState`, and no durable fence reaches into
another process's memory. "Exactly one routing and one history append" was an
assertion, not a mechanism.

**So the only way a claim changes hands is that its owner stopped existing.** Two
paths, and neither can race an in-flight effect:

| Path | Why it cannot overlap |
|---|---|
| The owner **died** | A dead process performs no further effect. Proof is required (below), never inferred. |
| The owner **relinquished** | `relinquish_claim(claim)` is called *by the owner itself*, only at a stage boundary where nothing is in flight — graceful shutdown, or a drain that finished a tick. Quiescence is guaranteed because the caller is the thing that would otherwise be mid-effect. |

There is deliberately **no operation that takes a claim from a live owner.** An
earlier draft offered `release_ownership()` as an audited operator command, but an
audit record does not create mutual exclusion: the old process could pass
`holds_claim()`, be fenced a microsecond later by the operator, and still apply its
in-memory review/history append beside the new owner. That is precisely the window
the death rule exists to close, so the operation is removed. What the operator gets
instead is the honest sequence: the Control Center shows the record as owned by a
named, still-running engine, and the engine surface offers a record-guarded
**`Stop engine`** (§8.4). Stopping it releases the gate, death becomes provable, and
the next drain takes over automatically. The fence never moves while the owner is alive.

**That remedy has to be reachable, so §8.4 makes it a real, targeted command.** The
Control Center's existing stop route is repository-scoped and calls
`Supervisor.stop(..., instance_id=None)`, which cannot stop the *particular named
instance* holding a claim in multi-instance mode — so "stop that engine" would have
been advice the UI could not act on. §8.4 defines the three pieces this needs: the
disposition owner publishing the claim holder as a fact, a new targeted engine-stop
boundary, and a record-guarded coordinator between them.

#### Proving death from the gate, not from the advertisement

The proof must come from the **exclusion primitive**, which is the `flock` gate
(`repo.lock`, and `locks/{instance_id}.lock` in multi-instance mode). It must *not*
come from `lock.json`: `repo_lock.py` is explicit that the metadata file "remains
the on-disk advertisement (pid/port/heartbeat) … it is no longer the exclusion
primitive", and `read_lock()` reads only that file. A stale or hand-edited
advertisement must never be able to authorize a takeover.

So death proof is a typed behavior-level capability, injected like every other:

```python
@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Full identity. `pid` alone is reusable; `started_at` closes that."""
    host: str
    pid: int
    started_at: str            # process start time
    instance_id: str | None    # repo-lock instance; None = single-instance mode


class OrchestratorLivenessPort(Protocol):
    def current(self) -> ProcessIdentity: ...
    def is_provably_dead(self, owner: ProcessIdentity) -> bool: ...
    """True ONLY on positive evidence. Unknown, unreachable, or different host
    all return False — this predicate never guesses, and False costs visibility
    while True costs data."""
```

`RepoLockLiveness` implements it against the real gate:

**The governing rule is: never probe a gate we already hold — for those, our own
startup acquisition is the proof.** `_acquire_gate()` is non-blocking and `flock` is
bound to the open file description, so a process probing a gate it already holds
would conflict with *itself* and conclude the previous holder is alive forever. The
matrix is therefore written over the relationship between `owner` and `current()`,
not over "which mode are we in":

| # | `owner` vs `current()` | Answer | Evidence |
|---|---|---|---|
| 1 | identical `ProcessIdentity` | `False` | it is us |
| 2 | different `host` | `False` | we can observe neither that host's processes nor its gates. **No automatic takeover ever crosses a host**, by construction |
| 3 | same `instance_id`, different pid/`started_at` (**restart of the same instance**) | `True` | we acquired *that exact gate* at startup — `locks/{id}.lock` if named, `repo.lock` if single-instance — and `flock` only admitted us because the previous holder released it. **Never probe here**: the gate we would probe is the one in our hand |
| 4 | `current().instance_id is None` (we are single-instance), owner is any other instance | `True` | we hold `repo.lock` `LOCK_EX`, which excludes every named `LOCK_SH` holder and every other exclusive one. Nothing else can be running |
| 5 | we are named, `owner.instance_id is None` (**single→named restart**) | `True` | a named acquire takes `LOCK_SH` on the repo gate, which a live exclusive single-instance holder would have blocked; `acquire_lock()` additionally refuses when a live `lock.json` advertises one (`_conflicting_legacy_holder`). Our success proves it is gone |
| 6 | we are named, owner is a **different** named instance | probe `locks/{owner.instance_id}.lock` with `LOCK_EX|LOCK_NB`, releasing immediately on success | this is the only gate we do not hold. Success ⇒ that instance holds no gate ⇒ gone. `BlockingIOError` ⇒ alive ⇒ `False`. Non-blocking, so a live instance is never disturbed |

Rows 3 and 5 are the ones a naive "named mode ⇒ probe" rule got wrong, and row 3 is
the **ordinary restart** — the case §4.4e's crash recovery depends on. An instance
that died mid-publication restarts under the same `instance_id` with a new pid,
finds a record owned by its former self, and must be able to take it over; probing
`locks/A.lock` while holding `locks/A.lock` would have reported that former self
alive forever and stranded the record permanently.

Row 6 is the only case that needs a probe at all, and it is the case that makes the
capability necessary: named instances coexist by design (`LOCK_SH` on the repo
gate), so two draining processes against one repository *are* possible, and rows 4-5
alone would be wrong there.

Every row is decided from the **gate**, never from `lock.json`, and every `True` is
backed by a kernel-enforced exclusion that already happened. A row that cannot
produce such evidence answers `False` — the direction that costs visibility rather
than data.

#### The owner keeps its own secret; `acquire_claim()` never hands one back

The row stores only `sha256(secret)`, so nothing can reconstruct an existing claim —
including the store. `ValidatedWorkDispositionService` therefore holds the claims it
acquired in memory for the lifetime of the process and re-presents them on later
ticks; `acquire_claim()` is called only for a record it does not already hold.

There is no case that needs reconstruction. A process that lost its in-memory claim
has, by definition, a different `ProcessIdentity` — a restart changes the pid and
`started_at` — so it is not the recorded owner, and it reaches ownership through the
death path like any other successor: row 3 of the matrix above, which is exactly why
that row must answer `True` without probing. `acquire_claim()` accordingly has one behaviour:
mint a new secret, bump the fence, record the new owner. It never returns an
existing claim, because it cannot.

A durable claim is still the right shape rather than an in-process lock, because
the state it guards outlives the process: a record left `PUBLISHING` by a crash must
be resumable by the *next* orchestrator, and only a durable owner record can say
whether resuming is safe.

Four properties, each replacing something the token model claimed but did not own:

- **Exactly one *effective* attempt, across drainers and processes.** The insert
  must land at `expected_attempt_no + 1`, and `(record_id, attempt_no)` is the
  primary key, so two racing drainers produce one insert and one loser that does
  nothing. If the loser reclaims later, the fence bump makes the earlier claim inert
  — so "exactly one" is a statement about which attempt can have *effects*, which is
  the property that matters, rather than about which processes are still running.
- **Retry is an explicit attempt transition.** A transient failure records its
  outcome on attempt *n* and the next drain claims attempt *n+1*. Nothing is asked
  to be simultaneously "the same submission, so we find it" and "a new submission,
  so we retry it".
- **The crash window is representable.** An attempt row with `outcome = ''` and an
  no outcome, **once its owner is provably dead**, is exactly "we called out and
  never learned what happened". The successor does **not** resubmit blind: it re-runs §4.3 in `RECONCILING`, where the
  allowed-state tables decide whether the push landed (remote at
  `validated_head_sha`), never landed (remote at the expectation), or something
  else happened (a third sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED`). A new attempt row
  is claimed only if reconciliation concludes a write is still needed.
- **The budget is durable by construction.** `PUBLISH_ATTEMPT_LIMIT` (a module
  constant, 5 — the correct number of times to retry a transient push is not an
  operator decision) is compared against `COUNT(*)` of attempt rows, so a restart
  resumes the count instead of resetting it. Exhaustion is durable `FAILED`, which
  is *unresolved* (§3.2), so hitting the bound still loses nothing.

The first successful `begin_publish_attempt()` is also what stamps
`publishing_started_at` on the record and moves it out of `QUEUED`/`PARKED`, so a
record can never be `PUBLISHING` without an attempt row, and an attempt row can
never exist for a record that is not `PUBLISHING`.

**Every outcome has a defined transition** — the owner never has an unhandled state:

Every row below is reached only *after* `acquire_claim()` succeeded, so "the owner
does" always means the current fence holder:

| Record/attempt state at drain | Owner does |
|---|---|
| the recorded owner is **this** process | the service still holds the claim in memory; it continues where it left off without calling `acquire_claim()` at all. |
| the recorded owner is **alive** (gate held), any attempt state, any finalization phase | `acquire_claim()` returns None. No writes, re-check next drain. The normal case, including a slow owner mid-finalization. |
| the recorded owner is **provably dead** (`OrchestratorLivenessPort`) | `acquire_claim()` mints a new secret, bumps the fence, and takes ownership. The previous owner is gone, so it cannot apply a late in-memory effect — including one that died between a successful outcome and finalization. |
| the owner **relinquished** at a stage boundary | the record is unowned; the next drain acquires normally, with no death proof needed. |
| the owner is alive but wedged | nothing automatic and nothing forced. The record stays `PUBLISHING` (unresolved, escrow retained, reset blocked) and the Control Center shows which engine owns it, offering **stop that engine**. Stopping releases the gate, which makes death provable. |
| claim held, latest attempt outcome `''` | re-run §4.3 in `RECONCILING`; begin a new attempt only if a write is still required. |
| claim held, latest attempt outcome successful, `finalization_phase < COMPLETE` | resume §4.5b from the recorded phase. **This is a finalization resume, not a new attempt** — no push, no new attempt row. |
| a stale claim tries to record anything | the CAS returns False; nothing is written and the caller is logged as superseded. |
| `PUBLISHED` / `ALREADY_AT_TARGET` | re-read the branch; require it at `target_head_sha`; ensure the PR (§4.4d); finalize (§4.5); mark `RECOVERED`. A success whose branch is *not* at target is a hard `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| `TRANSIENT_FAILURE` | stay `PUBLISHING`; claim attempt *n+1* next drain while `COUNT(attempts) < PUBLISH_ATTEMPT_LIMIT`; exhaustion ⇒ durable `FAILED`. |
| `DIVERGED` / `REJECTED` | durable `FAILED` with the mapped `ValidatedWorkFailure`. Definitive: no retry. |
| `SUPERSEDED` | **stop, and write nothing further.** This caller is no longer the owner, so it records no outcome, advances no phase, and makes no state transition — every such write would be refused by the fence anyway, and attempting them would only produce misleading log noise. **Its attempt row stays**, outcome-less, and is reconciled by whoever now owns the record. Not a failure, and not something this caller may clean up. |

The transient/definitive split is `PublishValidatedHeadOutcome.retryable` — an enum
comparison over `PublishValidatedHeadStatus`, mapped from the enumerated
`ExactPushOutcome` and typed host errors. Never a substring match on an error
string.

**`SUPERSEDED` always has an attempt row, and that is correct.** §4.6's handoff
inserts attempt *n+1* **before** invoking the fenced publisher — the ordering that
stops a crash from leaving a `QUEUED` record with a completed push — and the wrapper
can lose the claim either before the branch write or between the two steps. So a row
exists at both supersession points, and an earlier draft's "no attempt recorded" was
not merely optimistic but unreachable under the mandated ordering.

Removing the row would also destroy exactly what the append-only attempt table is
for. In the between-steps case the branch **may already have moved**, and that row is
the only durable record that this owner attempted the write; deleting it would leave
the next owner reconciling a remote it has no local explanation for.

So the honest contract is:

- The stale caller records **no outcome** on its row and performs no further store or
  phase mutation. The row stays outcome-less.
- The new owner treats it exactly like the crash case (§4.4e's "no outcome, owner
  provably dead"): re-run §4.3 in `RECONCILING` and let the allowed-state tables
  decide whether the push landed.
- **The row counts against `PUBLISH_ATTEMPT_LIMIT`**, because the budget is a count
  of attempt rows and a superseded attempt may have had a real remote effect.
  Conservative counting matches how this contract already treats every other
  outcome-less row, and exhausting the budget is durable `FAILED`, which is
  *unresolved* — so counting an attempt that did nothing costs a retry, never the
  work.

An attempt whose owner died is never treated as a failure — only as "reconcile
before deciding". There is no duration to configure here, because no duration
participates: the successor acts on proven death, and the live owner is left alone
however long it takes.

#### 4.4f Two callers, one executor: where the fence lives

The executor above is shared by both admission owners — that is the point of §4.6,
and it is what fixes the manual path's existing-PR shortcut in the same stroke. But
the fence of §4.4e belongs to the **validated-work record**, and only
`ValidatedWorkDispositionService` has one. An earlier draft put a required
`ValidatedWorkClaim` on `PublishValidatedHeadCommand` and simultaneously forbade
every module but the disposition service from acquiring a claim, which left
`PublishRecoveryService` unable to construct the command of a port it is explicitly
allowed to call. That is an unimplementable boundary, not a typing detail: the
manual path owns `PublishRetryLocators` and a background-job lifecycle, and
`RemoteHeadExpectation.UNCONSTRAINED` names its *semantics* without granting it
*authority*.

The split is therefore by layer, not by parameter:

| Layer | Type | Who calls it | Authorization |
|---|---|---|---|
| Remote execution | `ValidatedHeadExecutor` | **both** admission owners | none of its own — it executes what it is told |
| Fenced validated-work publication | `FencedValidatedHeadPublisher` | `ValidatedWorkDispositionService` only | holds a `ValidatedWorkClaim`; re-checks `holds_claim()` before each executor step |
| Manual publication | `PublishRecoveryService`, calling the executor directly | manual retry only | its existing locator + background-job authority, unchanged |

`FencedValidatedHeadPublisher` is a thin wrapper constructed by the disposition
service around the shared executor and its own claim. Its whole body is
check-then-delegate:

```python
def publish(
    self, claim: ValidatedWorkClaim, command: PublishValidatedHeadCommand
) -> PublishValidatedHeadOutcome:
    if not self._store.holds_claim(claim):
        return superseded_outcome(SupersededStage.BEFORE_BRANCH_WRITE, None)
    branch = self._executor.push_validated_head(command)
    if not branch.at_target:
        return compose_publication_outcome(branch, None)
    if not self._store.holds_claim(claim):        # re-check BETWEEN the two writes
        return superseded_outcome(SupersededStage.BETWEEN_STEPS, branch)
    return compose_publication_outcome(branch, self._executor.ensure_pull_request(command))
```

Both paths end in the **same composer**, so "the manual combined path and the fenced
two-step path produce the same final result" is true by construction rather than by
review. The only difference the wrapper can introduce is `SUPERSEDED`, which the
manual path — having no claim — can never produce.

That is why the executor exposes its two steps separately: the interposition point
has to be *between* the branch write and the PR ensure, and putting it there must
not require the executor to know what a claim is.

Three properties this buys:

- **Neither caller forges the other's authority.** The manual owner never
  constructs a `ValidatedWorkClaim`; the disposition owner never constructs
  `PublishRetryLocators` (§4.6). Each reaches the same executor through its own.
- **The guardrails become statable.** "Only `ValidatedWorkDispositionService`
  acquires claims" and "both admission owners may call `ValidatedHeadExecutor`" are
  now compatible sentences, and "the disposition path reaches the executor only
  through `FencedValidatedHeadPublisher`" is mechanically checkable (§9).
- **The manual regression is preserved.** `retry_publish()` still goes through the
  executor with `UNCONSTRAINED`, so an open PR at `R` no longer short-circuits
  publishing target `L`, and its duplicate-submission/tombstone behaviour is
  untouched.

### 4.5 The finalization boundary — `PublishedWorkFinalizer`

Labels and review routing are **control-layer policy over live orchestrator state**,
not remote execution. `RetrySuccessFinalizer.finalize()` already requires
`OrchestratorState` (`control/publish_retry_finalize.py:73-83`), so folding it into
the publisher would force either a hidden global or a back-reference from the
publisher to `PublishRecoveryService` — reintroducing exactly the cross-owner
coupling this split exists to remove. It stays a separate, explicitly composed port.

#### 4.5a Why it cannot wrap `RetrySuccessFinalizer` unchanged

An earlier draft said the finalizer wraps the existing pair "unchanged". It cannot,
because that finalizer's internal ordering is the inverse of what this contract
needs — and the inversion is a data-loss window, not a style preference.

Today `RetrySuccessFinalizer.finalize()` applies the external label cleanup **first**
and only afterwards appends the review candidate and the completed history to
in-memory state (`control/publish_retry_finalize.py:104-126`). That order is right
for the manual retry path, which is undoing a `publish-failed` state. It is wrong
here, because this contract's cleanup step removes `recovery-pending` — the label
that is keeping the issue off the board. A crash in the window between the cleanup
and `state.discovered_reviews.append(...)` leaves:

- no `recovery-pending` (removed), and
- no durable `needs-code-review`/`pr-pending` transition (the routing lived only in
  process memory, which the crash took), and therefore
- an issue that is **scheduler-eligible again** with a published head nobody is
  going to review.

Labels are the restart source of truth (ADR-0013), so "labels correct" after that
crash means "correctly says nothing is pending". The §10 row that let a record be
marked `RECOVERED` on the strength of cleanup labels compounded it: cleanup labels
do not prove the routing mutation ever happened.

#### 4.5b Staged, durable, replayed

Finalization is therefore a **durable staged owner**, with the phase persisted on
the record (`finalization_phase`, §4.1) and the recovery block held until the
externally observable routing fact exists:

```python
class FinalizationPhase(StrEnum):
    NOT_STARTED      = "not_started"
    REVIEW_ROUTED    = "review_routed"      # routing label WRITTEN and OBSERVED
    RECOVERY_CLEARED = "recovery_cleared"   # recovery-pending + observed blockers removed
    COMPLETE         = "complete"           # record marked RECOVERED
```

The stages, in order — the inverse of today's:

| # | Stage | Durable phase after it |
|---|---|---|
| 1 | Apply the **review routing label** (`pr-pending`, or the review-queue transition the discovered review drives) and append the review candidate + completed history to `OrchestratorState`. | — |
| 2 | Re-read labels fresh and require the routing label present (write-then-observe, ADR-0002). | `REVIEW_ROUTED` |
| 3 | Release this record's recovery block through the issue-aggregate reconciliation in §7; remove `recovery-pending` only if no other record holds it, and clear only this record's observed blockers that no remaining owner holds. | `RECOVERY_CLEARED` |
| 4 | Mark the record `RECOVERED` (with §2.1.4's ancestor resolution in the same transaction). | `COMPLETE` |

A crash at any point leaves `recovery-pending` present until stage 3, so the issue
is never scheduler-eligible between publication and durable review routing. Restart
resumes from the recorded phase.

**In-memory state is replayed on every resume, regardless of phase.**
`state.discovered_reviews` and `state.session_history` do not survive a restart, so
a record resuming at `REVIEW_ROUTED` still re-runs stage 1's in-memory half
(idempotently — the review candidate dedupes on `(issue_number, pr_number)` and the
history entry on the same key). The durable phase governs only the **label** steps,
which are the ones that must not repeat against a human's later edits. Conflating
the two is what made "labels correct ⇒ `RECOVERED`" look sufficient.

**`RECOVERED` is never inferred from labels.** It requires
`finalization_phase == RECOVERY_CLEARED` durably recorded, plus a fresh observation
that the remote head is `validated_head_sha` and the PR is open. Cleanup labels are
an effect of the transition, never evidence of it.

```python
@dataclass(frozen=True, slots=True)
class PublishedWorkFinalizationRequest:
    state: OrchestratorState              # the caller supplies it; no hidden state
    claim: ValidatedWorkClaim             # §4.4e; every stage is gated on it
    resume_from: FinalizationPhase        # NOT_STARTED on the first pass
    issue_number: int
    issue_title: str
    agent_label: str | None
    branch_name: str
    pr_number: int
    pr_url: str
    published_head_sha: str
    review_disposition: ReviewDisposition  # -> skip_review / exchange completed|halted
    history_reason: str
    recovery_label: str                    # removed at stage 3, never earlier
    observed_blocking_labels: tuple[str, ...]   # the ONLY other labels stage 3 clears
    worktree_path: str | None


class FinalizationStatus(StrEnum):
    FINALIZED = "finalized"   # reached RECOVERY_CLEARED; the record may be RECOVERED
    TRANSIENT = "transient"   # retry next drain from `phase_reached`; record unchanged
    FAILED    = "failed"      # durable FAILED with the mapped failure


@dataclass(frozen=True, slots=True)
class FinalizationOutcome:
    """Why this is a value and not ``None``.

    ``RetrySuccessFinalizer.finalize()`` returns ``None`` today
    (``control/publish_retry_finalize.py:73-85``), so its caller learns nothing
    about *how* it ended and a ``FreshIssueReadError`` is indistinguishable
    from success. The disposition owner cannot work that way: the difference
    between "retry next drain" and "durable FAILED" is the difference between
    keeping the work and stranding it, and §4.4e's table has to be driven by an
    enum rather than by an exception escaping (or not escaping) a void call.
    """
    status: FinalizationStatus
    phase_reached: FinalizationPhase       # where a TRANSIENT resume starts
    review_disposition: ReviewDisposition
    labels_added: tuple[str, ...]
    labels_removed: tuple[str, ...]
    failure: ValidatedWorkFailure | None   # required iff status is FAILED
    message: str


class FinalizationPhaseRecorder(Protocol):
    """The narrow durability the staged owner needs. Implemented by the store."""
    def record_finalization_phase(
        self, claim: ValidatedWorkClaim, phase: FinalizationPhase
    ) -> bool: ...
    """Fence-gated (§4.4e): False for a stale claim, which then performs no
    further stage. A superseded publisher cannot drive the staged machine."""


class PublishedWorkFinalizer(Protocol):
    def finalize(
        self, request: PublishedWorkFinalizationRequest
    ) -> FinalizationOutcome: ...
```

The implementation **composes** `RetryReviewRouting` and the review-candidate
construction of `RetrySuccessFinalizer` — one review-routing policy in the system,
reached by two admission owners — but owns the staging and the label ordering
itself, and records each phase through the injected `FinalizationPhaseRecorder` as
it completes. A `FreshIssueReadError` is `TRANSIENT` with the phase reached so far
(the disposition owner retries next drain rather than failing the record), and a
review-routing failure is `FAILED(REVIEW_ROUTING_FAILED)`.

The manual retry path keeps `RetrySuccessFinalizer` and its current
exception-based, cleanup-first behaviour unchanged: it has no `recovery-pending` to
hold, so the window described above does not exist for it. The shared piece is the
routing decision, not the ordering.

### 4.6 Composition, and who owns what

| Layer | Owner | Responsibility | Needs `OrchestratorState`? |
|---|---|---|---|
| Admission (manual) | `PublishRecoveryService` | `_retry_decision()`, board/locator gates, `PublishRetryLocators` | yes (already) |
| Admission (validated work) | `ValidatedWorkDispositionService` | evidence admission, escrow/refs, lineage, §4.3 checks, state machine | yes, via `drain(state)`/`recover(command, state)` |
| Run evidence | `IssueRunEvidenceSource` (§2.5) | which runs exist for an issue, with exact assets | **no** |
| Runtime activity | `OtherRuntimeActivityPort` (§4.3 check 5) | which *other* issue-runtime owners are live, as one typed fact | **no** |
| Owner liveness | `OrchestratorLivenessPort` (§4.4e) | gate-backed proof that a claim's owner is dead | **no** |
| Durable state | `ValidatedWorkStore` | records, evidence roles, publish attempts, finalization phase | **no** |
| Remote execution | `ValidatedHeadExecutor` | exact-object/exact-lease branch write, PR ensure — two separately callable steps | **no** |
| Fenced publication | `FencedValidatedHeadPublisher` | re-checks the claim between and before executor steps (§4.4f) | **no** |
| Finalization | `PublishedWorkFinalizer` | staged labels → routing → recovery clear (§4.5b) | yes, carried on the request |

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

1. Select the drainable row for the lineage key (§2.1.4); an `ANCESTOR` or
   `DIVERGENT` row is never drained.
2. Create or refresh the §4.4a publication workspace at the pinned validated ref;
   restore the escrowed completion and validation records into its run directory.
3. Run §4.3 in `PRE_SUBMISSION` (or `RECONCILING` for a row already `PUBLISHING`).
4. `store.begin_publish_attempt(...)` — one transaction: CAS to `PUBLISHING` and
   insert attempt *n+1* under the claim (§4.4e). Abort silently on a stale claim.
5. `publisher.publish_or_reconcile(command)` — exact write, then PR ensure.
6. `store.record_attempt_outcome(...)` — before anything is read from the outcome.
7. `finalizer.finalize(request)` with the live state and `resume_from` — staged
   routing, observation, then recovery clear (§4.5b).
8. `store.resolve_published(claim, ...)` — one command marking `RECOVERED`,
   advancing the lineage fact, resolving contained ancestors and classifying
   waiters in a single transaction (§4.1). Then remove the workspace; retain
   escrow and refs for the window.
9. `store.relinquish_claim(claim)` — the record is terminal, so ownership ends at a
   quiescent point rather than waiting for this process to die (§4.4e).

Only `ValidatedWorkDispositionService` claims disposition publish attempts. This is
enforced mechanically (§9).

---

## 5. Trusted artifact admission

Admission answers: *which bytes on disk may become executable authority?*

`ReviewDisposition.EXCHANGE_APPROVED` requires separate owner-attested reviewer
evidence: the exact reviewed SHA equals `validated_head_sha`, reviewer session/run
identity and approved decision/report hashes match the completed exchange, and
both review and validation satisfy the active review-cache boundary (including a
scratch reset). An `OK`/`REVIEWER_OK` terminal enum or publication approval alone
does not prove any of those facts. Missing reviewer proof routes through ordinary
PR review; contradictory/corrupt proof fails admission rather than silently
granting approval. The immutable evidence includes the bound reviewer proof digest
when this disposition is selected, so changing the approval changes `evidence_id`.

**The candidate set is bounded by the command, not by the filesystem.** Admission
considers exactly the runs in `AutomaticCaptureCommand.run_evidence.runs` (§2.5) —
each an exact `SessionRunAssets` recorded by the owner that allocated it. It never
enumerates run directories, never picks a "latest run", and never resolves a run
from a worktree or a session name. `NO_RUNS_RECORDED` yields an empty batch; an
unreadable ledger raised before admission was ever reached.

**Admissible sources within each such run, in this order:**

1. An immutable completion intake entry registered by the orchestrator for the
   exact `SessionRunAssets`, with its content hash and validator attestation.
2. The run-scoped completion copy or manifest-referenced record, but only when
   matched to that same registered intake entry. Containment alone is not proof
   that the orchestrator admitted those bytes.

**The trusted producer is required implementation, not existing behavior.**
`CompletionRecord.validation_record_path` already exists
(`domain/models.py:456`), but `CompletionProcessor._attach_validation_artifacts()`
copies validation into `run_assets.validation_artifacts.record_path` and updates
the manifest without rewriting that completion field. Moreover,
`preserve_completion_record()` (`control/completion_result_artifacts.py:158`) runs
only after parsing and pre-action policies succeeded
(`control/completion_processor.py:824-853`); failed ingestion can therefore leave
no preserved copy at all. The existing best-effort audit copy cannot establish
this design's intake guarantee.

Extend the run-evidence owner with a typed `CompletionEvidenceIntake` boundary,
called by the orchestrator's completion intake before canonical-record selection
or any terminal failure can discard the submission. It registers every completion
submission for the exact allocated run, including a corrected side submission,
with an immutable entry id, receive order, raw artifact hash and source provenance.
The launch owner supplies the run identity; agent `session_id`, timestamps and
paths cannot choose it. Invalid JSON remains an inspectable rejected entry and
does not hide a later valid entry. CLI-written files remain untrusted until this
owner registers them; the CLI cannot write the authority ledger.

The concrete producer path is a new authenticated run-scoped
`POST /api/completion/submissions`: `coding-done` sends a typed
`SubmitCompletionEvidence(raw_bytes, content_sha256)` using the run-bound
capability provisioned at launch. The server resolves that capability to the
allocated `SessionRunAssets`; no payload field selects a run. Its typed
`CompletionIntakeReceipt(entry_id, content_sha256)` is returned only after durable
registration. The processing queue consumes the receipt id, never re-selects a
canonical filename. A later corrected submission receives a second receipt even
if the first failed schema validation. Unavailable intake returns an explicit
failure and leaves the raw candidate intact; the CLI cannot claim successful
submission without a receipt. Bootstrap injects the same intake owner into this
handler, completion processing and terminal capture; validation completion calls
`attest_validation` for the receipt that caused that validation. Endpoint auth,
run capability expiry and terminal intake closure share the run owner: capture
closes new submissions and drains already-accepted receipts before releasing
runtime, so a late accepted receipt cannot arrive after the candidate set freezes.

The intake ledger is part of `SqliteIssueRunLedger`, not a second filesystem
discovery mechanism. Its immutable authority rows and validator attestations live
under the repository-owned state directory outside all agent worktrees; manifests
inside a worktree are locators whose hashes must match, never authority. Its API is:

```python
class CompletionEvidenceIntake(Protocol):
    def register_submission(
        self, run: SessionRunAssets, submission: OwnedCompletionSubmission
    ) -> CompletionIntakeEntry: ...
    def attest_validation(
        self, entry_id: str, result: OwnedValidationResult
    ) -> CompletionIntakeEntry: ...
    def entries_for_run(self, run: SessionRunIdentity) -> tuple[CompletionIntakeEntry, ...]: ...
```

The additional tables belong to `issue_run_ledger.sqlite`; they do not change the
four disposition tables in §4.1:

| Table | Required durable fields / constraints |
|---|---|
| `completion_intake_entries` | `entry_id` primary key; `run_key` foreign key to the allocated run; `submission_key`, `receive_sequence`, `raw_sha256`, `byte_size`, state-directory artifact locator, parse status, normalization version, normalized completion hash/locator. Unique `(run_key, submission_key)` makes a retried POST return the same receipt; changed bytes under that key are rejected. Receipt order is owner-assigned, never the agent timestamp. |
| `completion_validation_attestations` | `entry_id` primary/foreign key; exact `run_key`, raw/normalized completion hashes, full `head_sha`, validator/config digest, validation-result hash/locator, outcome and recorded time. One immutable attestation per entry; another validation creates a new entry rather than rewriting authority. |

`SubmitCompletionEvidence` also carries the client's stable `submission_key`;
each intentional correction uses a new key. Nullable normalized fields exist
only for rejected/unprocessed raw entries and cannot pass admission. Startup
repairs durable intake envelopes before processing receipts or termination, checks
all hashes against ledger rows, resumes accepted-but-unprocessed entries and
refuses corrupt/missing authority. Run release and intake artifact retention are
one owner operation: no run or attestation is released while a referencing
disposition remains unresolved.

`OwnedCompletionSubmission` is the intake adapter's registered submission event
and immutable captured bytes, not a path supplied by a completion JSON field.
`OwnedValidationResult` comes only from the existing configured validation
execution boundary (`control/validation.py:ValidationRunner.run`), and binds the
exact run, completion hash, validator/config identity, full commit SHA and result
hash. A validation JSON with `passed=true` is not sufficient without that match.
The validator's output is durably copied before success is attested; failed copy
or ledger write aborts terminal teardown. Producer entries use fsync/atomic rename
followed by ledger insertion and self-describing orphan repair, as in §2.1.2.
No authority depends on an agent's ability to edit a run-directory manifest.

The intake owner retains raw completion bytes outside the worktree for audit and
writes a separate normalized admitted completion whose `validation_record_path`
names the immutable run-contained validation copy certified by the attestation.
Both hashes and the normalization version are recorded. It never follows or trusts
the raw field as authority. §5 selection and evidence hashing use the normalized
copy plus the attestation-bound validation bytes, with source hashes retained in
the intake entry. The completion still expresses untrusted intent: requested
actions must include `PUSH_BRANCH` or `CREATE_PR`, and all policy checks still run.

**Historical sidecars require explicit import and fresh validation.** An existing
unregistered side artifact does not become trustworthy because it is run-scoped,
newer, or named by an editable manifest. Add an operator-only historical-intake
command on the same owner: the operator identifies the repository, issue, branch,
exact commit and candidate artifact hash/path. The intake adapter reads it as
untrusted bytes, verifies the selected repository/branch/commit through
`WorkingCopy`, and allocates a fresh recovery run through the run-evidence owner.
It executes the repository's configured validation at that exact commit in an
isolated workspace, then produces the same normalized completion and validator
attestation through `CompletionEvidenceIntake`. No agent-supplied successful
validation result is imported as authority. Missing objects, invalid completion,
failed validation or unavailable prerequisites refuse admission and preserve the
candidate for inspection. A successful historical intake always admits `PARKED`
evidence; a separate snapshot-bound recovery approval authorizes publication.
This gives slice 8 an executable backfill route without pretending pre-ledger
artifacts were already registered, and without granting agent prose authority.

The concrete historical surface is the new operator-authenticated
`POST /api/validated-work/intake`, dispatched through the existing typed command
handler pattern into `import_historical(command) -> HistoricalIntakeOutcome` on
the intake owner. `HistoricalIntakeCommand` requires `repo_slug`, positive
`issue_number`, exact `branch_name`, full `target_head_sha`, absolute normalized
`candidate_path`, `candidate_sha256`, operator `actor` and reason. The selected
repository comes from configured repository identity, never from the file or
agent command text. The outcome is discriminated: `PARKED` carries the new
`record_id`/`evidence_id`, `REFUSED` carries an enumerated admission reason, and
`VALIDATION_FAILED` carries the owned failed validation reference. The handler
does not execute a caller-supplied shell command or grant publication approval.
Tests cover operator request-to-owner mapping and owner outcome-to-HTTP response,
including path/hash replacement, wrong repository, failed validation and a
successful `PARKED` intake followed by separate snapshot-bound recovery.

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

**Recency selects within a unit of work, never between units of work.** That
distinction is the whole of it, and getting it wrong is a data-loss path rather than
an imprecision. Capture therefore runs in two steps:

1. **Enumerate.** Collect *every* admissible candidate across *every* run in
   `run_evidence.runs` that parses, is `COMPLETED`, and resolves to a validation
   record with `passed=true` whose `head_sha` exists in the object store. Nothing is
   discarded for being older, from an earlier run, or non-canonical.
2. **Group, then select within each group.** Partition the candidates by their
   `ValidatedWorkKey` — repo, issue, branch, validated head. The recency rule above
   picks one candidate *within* a group, because those candidates are competing
   descriptions of the **same** commit on the same branch. It is never applied
   *across* groups, because different groups are different commits, and choosing
   between commits is not a capture decision at all — it is the lineage decision,
   made durably in §2.1.4 after every group has been admitted.

Every group is then escrowed, ref-pinned, and admitted (§2.1.2, §2.1.3) **before
teardown proceeds**, and `dispose_at_termination()` returns a
`ValidatedWorkDispositionBatch` with one disposition per group. `run_identity` on
each `ValidatedWorkIdentity` records which run its evidence came from.

If any group fails to escrow, capture raises and **no** teardown occurs — the
all-or-nothing rule of §3.1, now over a set. A partially captured batch would be
strictly worse than no capture, because the groups that succeeded would make the
issue look handled while the worktree holding the rest was removed.

Concretely, the case a single "winning record" destroyed: the ledger holds run A
validated at `V` and run B validated at a divergent `L`. Enumeration finds both,
grouping produces two `ValidatedWorkKey`s, both are escrowed and pinned, lineage
classifies them `DIVERGENT` (§2.1.4), and the batch reports two `PARKED`
dispositions. Under the previous shape, `L` won on recency, `V` was never admitted,
and teardown removed the worktree that was the only remaining copy of it.

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
| Superseded **and attached** evidence (§2.1.3) | its own `<evidence_id>/` directory and refs | same window, measured from the owning record's `terminal_at` — role is irrelevant to retention, and superseding never deletes |
| Run-ledger rows (§2.5) | `issue_run_ledger.sqlite` | `release_runs()`, only once the issue has no unresolved record — the ledger is what proves the runs were considered |
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
- The retention sweep drives off `evidence_for_retention()` (§4.1), so it releases
  every evidence row of a resolved record — current, attached and superseded — and
  can release none of an unresolved one, in one query rather than by parsing JSON.
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

**Labels belong to the issue; dispositions belong to records.** All label rows
below express release of the transitioning record's interest, not an unconditional
issue-wide removal. One issue can have several divergent records, or records on
different branches. Recovering or abandoning one must leave `recovery-pending`
present while any other record remains unresolved. A `FAILED` cause is likewise
owned per record: its durable cause source includes `record_id`, and withdrawal
names only that source. The needs-human owner still aggregates across all causes.

`ValidatedWorkDispositionService.reconcile_issue_block(issue_number)` is the one
behavior that derives and applies this projection. Stage 3 may exclude its own
claim-bound record only after its publication and `REVIEW_ROUTED` are proven;
every other unresolved record still holds the block, including ancestors not yet
resolved by the publication transaction. After resolution, abandonment, admission,
or failure, this same behavior runs again. It also runs on every drain for issues
with retained record rows, so a crash between a durable transition and its label
write converges without requiring a fresh recovery command. Failed writes remain
pending reconciliation; durable records are never discarded because the label
temporarily disagrees. `RECOVERY_CLEARED` means this record released its interest,
not that the issue's aggregate label is necessarily absent.

Admission and aggregate label writes are serialized by one issue-scoped mutation
gate owned by the disposition service. Its narrow `IssueDispositionMutationGate`
port provides a context-managed `try_acquire(repo_slug, issue_number)` lease; the
same-host adapter uses an exclusive kernel file lock under the repository state
directory, with no timeout takeover, inheritance into child processes, or network
filesystem support. Busy acquisition returns a typed retry/refusal and teardown
fails closed; it never blocks shutdown indefinitely. The fresh aggregate read and GitHub
write occur while that gate is held: a new sibling cannot be admitted between the
last-holder check and label removal. Acquisition order is record claim, then issue
gate. A finalizer retains its record claim throughout. No operation holding the
issue gate may wait for or acquire a record claim: admission attaches/refuses when
the record is owned, while abandon or reconciliation returns a retry/refusal and
may acquire the record first on its next invocation. This gate covers only admission
and short label/transition operations, not validation or publication subprocesses.
The projection handles resolution of contained ancestors in one pass after the
store transaction; entrypoints and the finalizer never assemble sibling counts.

Observed blocking labels are not a license to remove another owner's active
block. Shared `needs-human` labels are cleared only through their owner, and
recording/withdrawing `VALIDATED_WORK_DISPOSITION` cannot clear a separate cause.
Other captured blockers are retained while another unresolved disposition still
holds the issue; clearing is deferred to the aggregate's final release.

| Transition point | Effect that must succeed first | Labels |
|---|---|---|
| Evidence recorded (`QUEUED`/`PARKED`) | durable record + escrow committed | add `recovery-pending` (new, `LabelCategory.BLOCKING`). Existing blocking label is **kept** — the issue is not unblocked by being owned. |
| Admission to publish (`PUBLISHING`) | durable CAS to `PUBLISHING` succeeded | **no label change.** The previous draft added `publish-failed` here purely to satisfy the manual path's `board_block_reason()` precondition; §4.6 removes that round-trip, so the issue is no longer marked with a failure it did not have. |
| Review routing (stage 1–2 of §4.5b) | remote head == `validated_head_sha`, PR open | apply `pr-pending`/the review-queue transition **first**, and observe it. `recovery-pending` is still present throughout. |
| Publication + review routing durable (`RECOVERED`) | `finalization_phase == REVIEW_ROUTED` recorded | Release this record's interest through the aggregate projection; remove `recovery-pending` only on the last eligible release, and clear only observed blockers whose owners permit release. |
| Disposition failed (`FAILED`) | — | keep `recovery-pending`, and register `NeedsHumanCause.VALIDATED_WORK_DISPOSITION` through the needs-human owner's API (see below). Never scratch-eligible — `recovery-pending` stays *because* `FAILED` is unresolved (§3.2). |
| Operator abandoned (`ABANDONED`) | durable `OperatorResolution` recorded | release this record's recovery block through the aggregate rule; **withdraw only its record-scoped** needs-human cause; leave pre-existing blocking labels untouched. Reset remains blocked by any unresolved sibling. Escrow and refs are still retained for the retention window. |
| Recovered from a previous `FAILED` (`RECOVERED`) | as above | **withdraw** the needs-human cause in the same step that clears `recovery-pending`. |

The routing-before-clearing order is not cosmetic — §4.5b explains why the reverse
order opens a window where the issue is scheduler-eligible with an unrouted
published head.

The targeted clear is important: a human who adds `blocked-needs-human` *after*
admission must not have it wiped by a recovery that never saw it. Only the observed
set is cleared.

**This owner does not write another owner's provenance marker.** An earlier draft had
`FAILED` add `tech-lead-needs-human` directly, and left it in place after both
recovery and abandonment. That label is defined by ADR-0013 as provenance for a
`needs-human` escalation owned by the **tech-lead launch workflow**
(`docs/architecture/ADR/0013-labels-as-crash-safe-truth.md:35-48`), and its
reconciler treats a marker present on an issue with no active investigation as an
*interrupted tech-lead escalation*, re-adding `needs-human` with an explanatory
comment (`control/tech_lead_needs_human_reconcile.py:271-316`). Two failures
followed:

- a validated-work failure was **misclassified on restart** as a tech-lead
  investigation escalation, which is a different lifecycle with different exits; and
- the marker was **asymmetric** — recovery removed `recovery-pending` and the
  observed blocking labels, but not a marker added after those were observed, so a
  successfully `RECOVERED` record could have `needs-human` reasserted underneath it.

The escalation instead goes through the existing needs-human owner's behavior API:
`NeedsHumanCause` (`control/needs_human_block.py:63`) with a new
`VALIDATED_WORK_DISPOSITION` member, recorded and withdrawn through the durable
cause registry (`record_needs_human_cause` / `withdraw_needs_human_cause`,
`ports/pending_work_claim_store.py:389-445`). That owner decides whether and how
`needs-human` appears, releases the shared block only when no other cause holds it,
and gives this contract the symmetric removal the direct label write never had. The
disposition owner writes exactly one label of its own — `recovery-pending` — and
§9's guardrail asserts no module outside the needs-human owner adds or removes
`needs-human` or `tech-lead-needs-human`.

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
  `validated_work_authority: ValidatedWorkAuthoritySnapshot | None = None`, with the
  mirror of the existing `target_session_id` rule — **required non-None for
  `recover_validated_work`, required None for every other op type**.

  It carries the snapshot rather than a bare `target_evidence_id` because the
  evidence id alone cannot express what was approved. By design `evidence_id`
  excludes the mutable observations (§2.1.1) — that exclusion is what makes it
  crash-stable — and those observations are legitimately refreshed under the same id
  while the record is `QUEUED`, `PARKED` or `FAILED`. So an op approved against PR
  `P` and remote baseline `R` could execute against `P2`/`R2` with the immutable
  approval unchanged, which is a real authority escape: the approver consented to
  landing a commit onto a specific remote state, and neither of those facts is in
  the id they approved.

  Revalidation does not close it. §4.3 proves the *current* facts are internally
  safe; it cannot prove they are the facts a reviewer authorized. The snapshot is
  checked for **exact equality** before anything else runs (§4.3 check 0), and a
  changed observation stale-downgrades the op with **zero writes** and an explicit
  "this was approved against PR `P`/remote `R`; it is now `P2`/`R2`" message, so the
  operator re-approves deliberately. The store remains the source of truth for
  execution — the snapshot is authorization input only, and every §4.3 check still
  runs against freshly read state after it matches.
- Storage: `tech_lead_proposal_ops.op` is already a JSON blob, so the snapshot is a
  `to_dict`/`from_dict` extension with **no DDL change**. The ledger is
  create-once-immutable, which is what makes the snapshot an approval record rather
  than a cache that could be refreshed to match. The immutable create-once
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
| `VALIDATED_WORK_DISPOSITION_OBSERVED` | `issue_number`, `reason`, and the whole batch: one entry per record with `record_id`, `evidence_id`, `state`, `lineage_role`, `failure`. Emitted **after** a terminal boundary returns (§3.5), which is why it is a separate event rather than a field on `HISTORY_RECONCILED` — that payload is built before the terminator runs. Re-emission on a re-planned no-op is harmless: it is an observation, not a transition. |
| `VALIDATED_WORK_AUTHORITY_STALE` | `evidence_id`, `actor`, and which approved fact moved (`pr_number`, `expected_remote_head_sha`, `observation_revision`) — the audit trail for a refused approval (§4.3 check 0) |

The UI-visible subset is added to the public timeline event enum in the same module,
per the `schema-updates` skill.

### 8.4 Public contract + Control Center command

- `contracts/public.py`: the issue-detail view model gains a `validated_work` block
  whose items carry `state`, `unresolved`, `worktree_head_sha`, `failure`,
  `escrow_retained`, `lineage_role` (so an ancestor parked behind a descendant reads
  as *waiting*, not *stuck*), `publish_attempts`, `finalization_phase`, the
  available operator actions (`recover`, and `abandon` **only when `can_abandon`**,
  with `abandon_unavailable` carrying the typed reason otherwise), **and a nested
  `authority` object carrying every field of
  `ValidatedWorkAuthoritySnapshot` verbatim**: `record_id`, `evidence_id`,
  `observation_revision`, `validated_head_sha`, `branch_name`, `repo_slug`,
  `issue_number`, `pr_number`, `expected_remote_head_sha`.

  The `authority` object is not redundancy. The operator command is only an
  authorization if the browser can echo back **exactly what it rendered**, and a
  public payload that omitted `record_id`, `branch_name`, `expected_remote_head_sha`
  or `observation_revision` left the adapter no way to build the snapshot except by
  reading them from fresh server state — which is precisely the escape §4.3 check 0
  exists to close, moved one layer out. The endpoint therefore **must not populate
  any authority field from the store**: a request missing a field, or carrying one
  the client did not render, is rejected as malformed rather than completed.

  A `DIVERGENT` row surfaces the sibling heads so the operator's choice is informed
  rather than blind. Regenerate
  `contracts/public/*.json` with `scripts/generate_public_contracts.py`; drift is
  enforced by `tests/unit/test_public_contract_schemas.py`.
- The block is a **list**, because an issue can hold several records (§2.1.4): a
  descendant head plus the ancestors parked behind it, or two divergent heads
  awaiting a choice. Rendering one and hiding the rest is the same collapse-to-a-winner
  the batch exists to prevent, and it is worse in the UI than in the owner, because
  the operator is the one being asked to choose.
- An operator action posts the rendered `authority` object back **unchanged**, and
  the endpoint builds the `StoredEvidenceCommand` from the request body alone. So
  the operator, like the tech lead, approves *specific facts*: if the record moved
  between render and click, check 0 refuses with zero writes and the UI re-renders
  the new facts rather than acting on them silently. Registered in the UI OpenAPI
  contract as a required request-body object per the `ui-openapi` skill.
- The confirmation context must **show** what is being authorized — PR number,
  branch, validated target sha, and remote baseline — not merely transmit it. An
  approval the operator cannot read is not an informed one, and this is the dialog
  where the destructive-by-consequence `abandon` also lives (§8.4 accessibility).

**The wedged-owner escape (§4.4e), as a real command surface.** A record can sit
`PUBLISHING` under a live owner, and the only remedy is to stop that owner's engine.
That is a **Repository Engine** lifecycle action, not an issue action, so it is
modelled as one:

| Concern | Owner | Shape |
|---|---|---|
| Who holds the claim | `ValidatedWorkDispositionOwner.snapshot()` | `owner: ClaimOwnerFact or None` — engine identity plus owner-computed stop availability |
| Presenting it | Control Center issue detail | "Owned by engine `<label>` (`Running`)", with a link to that engine's surface. **No engine control is embedded in the issue view** |
| Guarding the stop | a control-layer coordinator | `StopValidatedWorkOwnerCommand(record_id, expected_engine, expected_owner_fence, actor, reason)` — reserves the exact rendered owner and refuses on any mismatch |
| Stopping it | `RepositoryEngineLifecycle` (new; §8.4) | a plain instance-targeted `StopEngineCommand(engine, actor, reason)` under the standard **`Stop engine`** label |

```python
@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """Stable, targetable name of a Repository Engine. Never a raw pid.

    Identity only. It deliberately has no `targetable_here` property: a frozen
    read-model fact that consults `local_host()` reads process-global state at
    an arbitrary later moment, which is both a boundary leak and a value that
    can disagree with the render it was shown in. Targetability is produced by
    the owner (below) and carried as data.
    """

    repo_root: str               # the repository this engine serves; the adapter needs it
    instance_id: str | None      # None == the single-instance engine
    host: str
    label: str                   # display name, e.g. "orchestrator-2"
    process: ProcessIdentity     # exact incarnation; §4.4e, not a reusable instance slot

    def __post_init__(self) -> None:
        if (self.host, self.instance_id) != (self.process.host, self.process.instance_id):
            raise ValueError("engine identity and process identity disagree")


class EngineStopAvailability(StrEnum):
    """Whether THIS Control Center can stop that engine. Owner-produced."""
    AVAILABLE   = "available"
    REMOTE_HOST = "remote_host"    # present it; never offer a control
    EXACT_TARGET_UNAVAILABLE = "exact_target_unavailable"  # platform cannot pin this process


@dataclass(frozen=True, slots=True)
class ClaimOwnerFact:
    """What the read model reports about who holds a record's claim."""
    engine: EngineIdentity
    owner_fence: int                          # render-time claim generation
    stop_availability: EngineStopAvailability   # computed at snapshot time
```

#### The engine lifecycle owner does not exist yet, so this design defines it

The earlier draft said this "extends the existing Repository Engine lifecycle
owner". There is no such owner for stopping. `ControlCenterActions` owns commands
for pause, resume, refresh, doctor, audit, trace, labels and stale worktrees
(`execution/control_center_actions.py:357-381`) — but not stop; the Control Center
stop route calls `SupervisorOps.stop_all_instances()` directly
(`entrypoints/control_api_orchestrator_routes.py:393`), and the legacy repository
route calls `SupervisorOps.stop()` with the default `instance_id=None`
(`entrypoints/control_api.py:540`). So there was nothing to extend, and the
instance-targeted stop this recovery needs had no home.

Two owners are defined, at two different altitudes, because two different questions
are being asked:

```python
@dataclass(frozen=True, slots=True)
class StopEngineCommand:
    """Plain engine lifecycle. Knows nothing about validated work.

    The stop policy is stated, not inherited. `SupervisorOps.stop()` defaults
    `graceful_timeout_seconds` and `force_if_graceful_fails=True`
    (`infra/supervisor.py:447-456`), so a call that omitted them would silently
    own "graceful, then SIGKILL after an unstated timeout" while the prose
    claimed force was out of scope. Both readings were dangerous: an
    implementation that disabled force could leave the only recovery path
    permanently wedged, and one that took the default would force-kill on a
    timeout nobody had chosen.
    """
    engine: EngineIdentity
    actor: str
    reason: str
    graceful_timeout_seconds: float = ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS
    force_on_timeout: bool = True


class StopEngineStatus(StrEnum):
    """Facts about the requested process incarnation, never its successor."""
    STOPPED        = "stopped"         # the expected process is provably gone
    TARGET_CHANGED = "target_changed"  # pre-stop target mismatch; zero effect
    REMOTE_HOST    = "remote_host"     # refused before calling: not ours to stop
    FAILED         = "failed"          # stop/identity proof failed; no success asserted


@dataclass(frozen=True, slots=True)
class StopEngineOutcome:
    status: StopEngineStatus
    engine: EngineIdentity
    message: str


class RepositoryEngineLifecycle(Protocol):
    """The targeted-stop boundary this design adds. Not a universal one:
    the pre-existing stop surfaces are untouched (see the scope note below)."""
    def stop_engine(self, command: StopEngineCommand) -> StopEngineOutcome: ...
    def engines(self) -> tuple[EngineIdentity, ...]: ...
    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability: ...


class IncarnationStopPort(Protocol):
    """New supervisor capability; the legacy Boolean stop API is insufficient."""
    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability: ...
    def stop_expected(self, command: StopEngineCommand) -> StopEngineOutcome: ...
```

`SupervisorRepositoryEngineLifecycle` implements it over `IncarnationStopPort`,
which is a **new** supervisor capability required by this slice, and calls

```python
incarnation_stop.stop_expected(command)  # includes repo, instance, process and stop policy
```

**A reusable instance name is not a process target.** The existing
`SupervisorOps.stop()` reads the current lock advertisement and returns a Boolean;
it neither accepts an expected incarnation nor carries an explanatory message.
It cannot implement this guarded path by checking the record and then calling
`stop(repo_root, instance_id)`. Between those steps the owner may die and another
engine may start under the same instance name, so that call could kill its
successor. The new capability must bind **every effect**, including graceful HTTP,
signals, force-on-timeout and lock cleanup, to `command.engine.process`.

The supported automatic backend is **Linux pidfd**: acquire `pidfd_open(pid)`,
validate the target's kernel start identity against the expected `ProcessIdentity`
while holding that handle, and use `pidfd_send_signal` for both SIGTERM and, after
the command timeout, SIGKILL. Wait for the handle to report exit. Once acquired,
that handle cannot redirect a signal to a reused pid. There is no HTTP request to
a reusable port and no process-group signal in this path. SIGTERM is the graceful
shutdown request; this engine's signal handler must be covered by the integration
test. The backend never removes the instance's current advertisement: exit releases
the kernel gate, and ordinary startup reconciliation owns stale metadata cleanup.

A replaced instance detected before a handle is accepted is `TARGET_CHANGED` with
zero effects; a provably dead expected process is `STOPPED`. After a handle was
accepted, the expected process exiting is `STOPPED` even if a replacement now
exists: no further effect can reach that replacement. A read of pid/start time
followed by an ordinary signal is **not** a lifetime pin. The Linux backend requires
real-process tests, including replacement before force escalation.

**macOS, and Linux without usable pidfd support, have no guarded automatic stop in
this design.** No macOS lifetime-pinning implementation is claimed or left as an
unspecified implementation prerequisite. The lifecycle capability probe reports
`EXACT_TARGET_UNAVAILABLE`; the read model carries that availability before render,
and the Control Center shows text explaining that exact-owner stop is unavailable
plus navigation to the existing independently authorized engine stop control. That
control keeps its own explicit engine/repository-wide confirmation and scope. It is
the actual recovery route on these platforms, without the exact-record guarantee.
No new guardian process or broad migration is introduced.

A capability disappearing between render and dispatch returns `FAILED` with a
diagnostic and zero effects; release the reservation normally. Neither platform
silently falls back to the ordinary `SupervisorOps.stop()` or its newer
`expected_pid`/tracked-instance helpers: a reusable pid or port is not a process
lifetime handle. This additional capability leaves existing callers on their
current APIs and behavior.

**For the supported exact-process backend, force-on-timeout is deliberately `True`, and that is a decision rather than
an inherited default.** The case this exists for is an engine that has stopped
responding while holding a claim; a graceful-only stop would leave exactly that
engine running and the record wedged forever, turning the only recovery path into a
no-op. Forcing is safe on this contract's own terms: the record, its evidence and
its escrow are durable and untouched by the kill, the fence refuses any late write
from the dying process, and — the point of the whole exercise — a killed process has
its `flock` released by the kernel, which is what makes death provable (§4.4e).
`ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS` is a module constant, not a setting, for the
same reason `PUBLISH_ATTEMPT_LIMIT` is.

Outcome mapping preserves the typed `IncarnationStopPort` result and its message:
`STOPPED` means the expected process is provably gone, `TARGET_CHANGED` refuses a
replacement, and `FAILED` reports a failed stop or unavailable identity proof.
The lifecycle owner refuses a non-local target as `REMOTE_HOST` before any call.
Graceful exit, successful force and already-gone share `STOPPED`; no consumer needs
to distinguish them. Messages are produced by this new adapter, not recovered from
the legacy Boolean return or parsed from logs. None of these outcomes promises that
the record is still unclaimed: another engine may already have acquired it.

The operator confirmation must say what will happen in those words: that the engine
will be asked to shut down, that it will be **forcibly terminated** if it has not
stopped within the timeout, and that stopping it halts **all** of that engine's
work, not only this record.

#### Scope: this boundary is an addition, not a consolidation

An earlier draft said the existing stop surfaces would be re-pointed through this
owner "so there is one stop boundary", paired that with a repo-wide guardrail, and
then deferred the migration — three positions that could not all hold. The primary
Control Center stop route carries materially more behaviour than a targeted stop
(bulk `stop_all_instances`, `force`, `force_if_timeout`, a graceful timeout, a
port-targeted fallback, and shutdown-operation admission/cleanup,
`entrypoints/control_api_orchestrator_routes.py:340-400`), and the tree has direct
stop consumers in MCP, CLI, repository removal, restart and reconciliation.

This design therefore **does not migrate any of them, and does not claim to.**
`RepositoryEngineLifecycle` is a new boundary with exactly one new caller — the
guarded coordinator below — owning exactly one behaviour: the targeted stop of one
engine under the policy above. The existing call sites are pre-existing and
untouched: this change neither improves nor regresses them, so nothing about them is
being *deferred*. The guardrail is scoped to match, rejecting direct
`SupervisorOps.stop`/`stop_all_instances` calls from the **validated-work,
disposition and issue-detail** modules this design introduces.

Consolidating the pre-existing surfaces behind this boundary is worthwhile cleanup
and is filed as a follow-up proposal, but it is not a prerequisite for anything
here and no part of this contract waits on it.

#### The recovery guard is its own bounded coordinator

`StopEngineCommand` deliberately carries no `record_id`: engine lifecycle has no
business knowing about validated work, and the generic engine surface has even less
context after a link from an issue. But the guard this recovery needs — *stop this
engine only if it still owns this exact record* — has to live somewhere, and neither
the route (which would re-derive policy) nor the disposition store (which would
gain a lifecycle dependency) may hold it. So it is a small control-layer command of
its own:

```python
@dataclass(frozen=True, slots=True)
class StopValidatedWorkOwnerCommand:
    """Stop the engine that owns this record — if it still does."""
    record_id: str
    expected_engine: EngineIdentity     # exactly what the operator was shown
    expected_owner_fence: int           # exactly the rendered claim generation
    actor: str
    reason: str


class StopOwnerStatus(StrEnum):
    """Total. Every dispatch ends on exactly one of these."""
    STOPPED           = "stopped"          # the engine is no longer running
    NO_SUCH_RECORD    = "no_such_record"   # record_id resolves to nothing
    NOT_OWNED         = "not_owned"        # the record has no claim holder now
    OWNER_CHANGED     = "owner_changed"    # someone else holds it; re-render
    STOP_IN_PROGRESS  = "stop_in_progress" # another reservation holds this generation
    REPO_MISMATCH     = "repo_mismatch"    # the engine serves a different repository
    REMOTE_HOST       = "remote_host"      # not ours to stop
    STOP_FAILED       = "stop_failed"      # the lifecycle owner reported failure


@dataclass(frozen=True, slots=True)
class StopOwnerOutcome:
    status: StopOwnerStatus
    observed_owner: ClaimOwnerFact | None   # what the re-read actually found
    message: str


class ValidatedWorkOwnerStopCoordinator(Protocol):
    def stop_owning_engine(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopOwnerOutcome: ...
```

The coordinator matches the record's current owner, refuses on **any** mismatch or a
non-local target with zero effect, and only on an exact match delegates a plain
`StopEngineCommand` to `RepositoryEngineLifecycle`. Three boundaries stay intact:
the disposition owner remains read-only with respect to engine lifecycle, the engine
owner remains ignorant of validated work, and the route dispatches a typed command
instead of implementing policy.

#### A read-then-stop is not a guard: the reservation interlock

Matching a snapshot and *then* calling the supervisor does not make the promise
true, it only narrows the window. This execution is legal under the rest of this
contract:

1. the coordinator reads record `R`, owned by engine `A`, and matches the rendered `A`;
2. `A` reaches a stage boundary and calls `relinquish_claim()` (§4.4e) — it stays
   alive and keeps doing unrelated work;
3. engine `B` acquires `R`;
4. the coordinator stops `A`, halting all of `A`'s work although `A` no longer owns `R`.

Zero-effect-on-mismatch has to hold at the **effect point**, not at the read. So the
guard is a durable, store-owned reservation, and it is owned by the disposition
store because the claim is. The Control Center uses a narrow writable port, not a
raw store dependency or the engine's HTTP API:

```python
@dataclass(frozen=True, slots=True)
class StopReservation:
    reservation_id: str          # fresh unpredictable id; never returned to another caller
    record_id: str
    engine: EngineIdentity       # includes the exact process incarnation
    owner_fence: int


@dataclass(frozen=True, slots=True)
class StopReservationRefusal:
    status: StopOwnerStatus       # one of the pre-stop refusals below
    observed_owner: ClaimOwnerFact | None
    message: str


class ValidatedWorkStopReservations(Protocol):
    def reserve_owner_stop(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopReservation | StopReservationRefusal: ...
        """CAS the repository, full process identity and rendered owner fence.

        One transaction: verify all expected facts and absence of a reservation
        for this fence; then persist a fresh reservation id and return its token.
        NO_SUCH_RECORD / NOT_OWNED / OWNER_CHANGED / REPO_MISMATCH / REMOTE_HOST
        refuse with no writes. ANY existing reservation for this fence returns
        STOP_IN_PROGRESS, including a second request for the identical owner.
        An old-fence reservation is invalid and never blocks a current owner.
        """

    def release_owner_stop(self, reservation: StopReservation) -> bool: ...
        """Compare-and-clear by (record_id, fence, reservation_id, incarnation).

        True only when this token's reservation was removed. False is a harmless
        stale/duplicate release; it can never remove another request's reservation.
        """
```

**What the reservation blocks is exactly one thing: `relinquish_claim()`.** While a
reservation is live for the current fence, relinquish returns `False` and
the owner keeps its claim. That closes the live hand-back race, because relinquish
is the only way a live engine loses a claim. The other way is death, and the
incarnation-bound lifecycle capability above closes that second race: it can never
redirect the pending stop to the dead owner's replacement.
Acquisition needs no separate block: `B` can only acquire when the record is
unowned or `A` is provably dead, and neither can happen while `A` is alive and
holding.

So the coordinator's sequence is reserve → stop → release. Every reservation
refusal happens before any lifecycle call; a lifecycle refusal performs no stop
effect, while a `FAILED` result may follow an unsuccessful stop attempt:

```python
reservation = reservations.reserve_owner_stop(command)   # CAS through the narrow port
if isinstance(reservation, StopReservationRefusal):
    return StopOwnerOutcome(reservation.status, reservation.observed_owner, reservation.message)
try:
    outcome = lifecycle.stop_engine(StopEngineCommand(
        engine=reservation.engine, actor=command.actor, reason=command.reason,
    ))
    return map_stop_outcome(outcome)  # TARGET_CHANGED -> OWNER_CHANGED; FAILED -> STOP_FAILED
finally:
    reservations.release_owner_stop(reservation)
```

**Concurrent requests never share a reservation.** Even two clicks with identical
rendered facts have distinct operation lifetimes. If they shared a token, the first
could fail and release it while the second had not stopped yet, allowing the owner
to relinquish before the second stop. Exclusive admission and conditional release
close that interleaving. No caller may adopt or release a token read from storage.

**A leaked reservation costs liveness until the owner exits.** If the Control Center
dies between reserve and release, the owner cannot relinquish and subsequent guarded
stops return `STOP_IN_PROGRESS`; escrow remains retained and unresolved work still
blocks reset. The surface must explain this state and direct the operator to the
existing explicit engine stop control, whose confirmation authorizes stopping that
engine independently of a record guard. No timer expires a reservation. Owner death
still permits acquisition; the acquisition transaction clears all old reservation
fields as it advances the fence. A later release carrying the old token cannot
erase a successor's reservation. This is an explicit recovery cost, not a claim that
a leaked reservation has no effect on the guarded path.

**The re-read needs an exact record lookup.** `snapshot(issue_number)` is the wrong
shape here: `record_id` is a canonical hash (§2.1.1), so a coordinator holding only
a record id cannot derive the issue number and would have to reach into the store —
the boundary this split exists to keep. An exact read is therefore part of the
contract:

```python
def snapshot_record(self, record_id: str) -> ValidatedWorkSnapshot | None: ...
    """Exact read by the durable primary key. `None` when no such record."""
```

`ValidatedWorkDispositionOwner` exposes it for in-process callers, and — see below —
the Control Center gets the same read through its own adapter.

#### This must live in the Control Center, because the Repository Engine is the thing that is stuck

An earlier draft said the coordinator was "constructed in bootstrap" and reached by
Control Center. Those are **different processes**. `entrypoints/bootstrap.py`
composes a Repository Engine and its `ValidatedWorkDispositionOwner`; the Control
Center is the separate shell that owns supervisor controls. A coordinator or
endpoint hosted by the owning Repository Engine is worthless as an escape hatch
precisely when it is needed, because the process serving it is the wedged one.

So the guarded stop is **Control Center-owned end to end**, and it reaches the
target repository's state without asking that repository's engine anything:

```python
class ValidatedWorkRecordReader(Protocol):
    """Read one record's disposition facts for a repository, out of process."""
    def snapshot_record(
        self, repo_root: str, record_id: str
    ) -> ValidatedWorkSnapshot | None: ...
```

`SqliteValidatedWorkRecordReader` implements it by opening that repository's
`state/validated_work.sqlite` **read-only**, through the shared connection helper
(`infra/sqlite_connection.py`). That is the whole reason the disposition state is a
file in the repository's own state directory rather than engine memory: a reader
that needed the engine's HTTP API would be unavailable in exactly the failure this
command exists for. It performs no writes and takes no claim.

`SqliteValidatedWorkStopReservations` separately implements the narrow
`ValidatedWorkStopReservations` port with a writable connection to the same file,
using the shared SQLite connection helper. It shares the store-owned transaction
implementation for reservation CAS, conditional release and fence invalidation;
it does not duplicate those predicates in the Control Center. Its only writes are
reservation fields: it cannot acquire/relinquish publication claims, change record
state, or run lifecycle effects. An inaccessible, locked or incompatible database
fails before any lifecycle call. The connection holds no transaction during stop.

**Control Center composition root:**

```
lifecycle = SupervisorRepositoryEngineLifecycle(incarnation_stop) # exact process effects
reader = SqliteValidatedWorkRecordReader(lifecycle.stop_availability)
reservations = SqliteValidatedWorkStopReservations()               # narrow durable CAS
coordinator = ValidatedWorkOwnerStopCoordinator(reader, reservations, lifecycle)
ControlCenterActions(..., stop_validated_work_owner_cmd=coordinator)
```

None of these collaborators requires the target engine to be responsive, which is the
property that makes this a real escape hatch on a supported backend. The Repository
Engine composition root supplies the same read-only availability capability to its
disposition snapshot producer, but never constructs the stop coordinator. The
Control Center recomputes availability for its own platform before offering the
guarded control. §8.4's issue-detail surface never posts to the coordinator.

**Where the control lives, and how the record context crosses processes.** The
control is a Control Center engine control, on a Control Center surface, served by
the Control Center API. The issue detail — which the wedged engine serves — only
ever *reports* and *links*.

There is no instance-addressed page today: the Control Center renders one repository
card keyed by `repo.path`, and its `Stop engine` action is repository-aggregate
(`static/js/control_center.js:1072-1098`, calling `stop_all_instances()`). Rather
than invent a new page, the existing repository card gains a **per-engine row** —
it already resolves per-instance status — and that row is addressable:

| Step | Surface | What it carries |
|---|---|---|
| 1 | Issue detail (served by the engine) | renders the `ClaimOwnerFact` as text — "Owned by engine `<label>` (`Running`)" — and a **link**, never a control, to the Control Center: `{cc_origin}/?repo={repo_key}&engine={instance_key}&stop_owner_of={record_id}`, where `cc_origin` is the embed-context parameter below — rendered as plain text with no anchor when it is absent |
| 2 | Control Center repository card | with that context, expands the named engine row and renders the native **`Stop engine`** button (accessible name includes the engine label) beside a summary of the record it would unblock. Without the context the card is unchanged |
| 3 | POST | `POST /api/control-center/repositories/{repo_key}/engines/{instance_key}/stop-validated-work-owner`, body `{record_id, engine: {repo_root, instance_id, host, label, process: {host, pid, started_at, instance_id}}, owner_fence, reason}` — the engine and fence echoed **verbatim** from what step 2 rendered, exactly as §4.3 check 0's authority snapshot is echoed |
| 4 | Handler | builds `StopValidatedWorkOwnerCommand` from the body **and the route**, checks they agree (below), and dispatches to the coordinator; it re-derives nothing and decides nothing |

**Single-instance identity is routable.** `instance_id is None` is the
single-instance engine, and `None` has no path representation, so `instance_key`
encodes it as the reserved literal `default`; every other value is the
`instance_id`. The encoding is one function used by both the link builder and the
route parser, so they cannot disagree.

**Four facts must agree before anything stops**, and the handler checks all four
rather than trusting the body it was handed:

1. the **route** identity (`repo_key`, `instance_key`),
2. the **rendered** engine identity in the body,
3. the record's **repository** (`snapshot_record().key.repo_slug` → its root),
4. the **current claim owner**, its exact process incarnation and fence on that record.

Any disagreement is a typed refusal with zero effect: `REPO_MISMATCH` for 1–3,
`OWNER_CHANGED` for 4. Missing process identity or fence is malformed; the handler
never fills it from fresh state. The reservation transaction repeats the owner/fence
comparison at admission. A duplicated path value that contradicts the body is
therefore a refusal, not an ignored field.

**The origin is supplied by the shell and preserved by embedded navigation.** There
is no `control_center_origin` setting today and this design does not add one: a new
top-level config surface for a value the shell already knows would be a worse
answer than passing it, and the engine has no way to discover it on its own.
Instead it rides the mechanism that already exists for exactly this purpose.

The Control Center shell builds repository dashboard URLs and stamps its embed
context onto them — today `embedded` and `theme`
(`static/js/control_center.js:901-906`) — and `embedded_nav.js` preserves that
context across in-dashboard navigation from a single frozen list,
`EMBEDDED_CONTEXT_PARAMS` (`static/js/embedded_nav.js:31-43`). This design adds one
member, `cc_origin`, to **both** places: the shell stamps its own origin, and
embedded navigation carries it just like `theme`. One list, one rule, no new
configuration surface, and the link builder reads it from the same place every other
embed-context consumer does.

Three cases, all specified:

| Context | Behaviour |
|---|---|
| Embedded in the shell | `cc_origin` is present; the link is built against it, and the target card is already in the shell |
| Standalone **with** `cc_origin` (navigated from an embedded session, or supplied explicitly) | same link, opened against that origin |
| Standalone **without** `cc_origin` | the issue detail renders the owner fact and the record id as **text**, with an explicit "open the Control Center for this repository to stop engine `<label>`" instruction. A link that cannot be built is never rendered as one — no dead href, no guessed origin |

The endpoint is registered in the UI OpenAPI contract per the `ui-openapi` skill
with a required request body and the typed `StopOwnerOutcome` as its response, and
it uses the **shared browser-session auth helper** for CSRF/SSE-token handling like
every other Control Center engine control (`control-center-lifecycle` rule 8) rather
than a bespoke path. `actor` comes from the authenticated session, never the body.

- **A different host is presented, not offered.** `EngineStopAvailability.REMOTE_HOST`
  renders the owner as informational text naming the host, with the action absent
  (not a disabled-looking control), because there is nothing this Control Center can
  do about it. Same conservatism as row 2 of §4.4e's liveness matrix, and the
  coordinator refuses it a second time server-side.
- **An unavailable exact-process backend is presented before action.**
  `EXACT_TARGET_UNAVAILABLE` renders explanatory text and navigation to the existing
  independent engine stop surface, with no guarded stop button. Availability comes
  from the injected lifecycle capability probe combined with the owner facts, not
  from platform checks in a template. Dispatch probes again before effects so stale
  capability data cannot trigger an ordinary pid/port stop.
- **A stale owner identity is refused, not guessed.** `expected_engine` is the
  render-time fact together with `expected_owner_fence`; if the claim moved before the click, the coordinator returns
  `OWNER_CHANGED` with zero effect and the surface re-renders — the same
  render-time-facts discipline as §4.3 check 0.
- The disposition owner exposes the holder and nothing else. It has no stop method,
  constructs no supervisor call, and reads no supervisor state; the issue-detail
  handler likewise never builds a stop call from persisted PID fields.
- Accessibility matches the rest of §8.4: native `<button>`, keyboard reachable,
  visible focus ring, accessible name including the engine label, `Running`/`Not
  running` and `REMOTE_HOST` conveyed as text rather than colour alone, and — since stopping an engine
  halts all of its work, not only this record — a confirm step stating that scope,
  focus-trapped and `Escape`-dismissible. Its failure toast does not auto-dismiss.
- Terminology follows the `control-center-lifecycle` skill exactly: **Control Center**
  is the shell, **Repository Engine** is the runtime, the label is `Stop engine`, and
  engine controls stay on engine surfaces.
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
  **required** non-empty reason from the request body.
- One endpoint on the **Control Center** API — not the Repository Engine's, because
  the engine may be the wedged process:
  `POST /api/control-center/repositories/{repo_key}/engines/{instance_key}/stop-validated-work-owner`
  → the Control Center-composed `ValidatedWorkOwnerStopCoordinator`, returning the
  typed `StopOwnerOutcome`. Register all three endpoints and their payloads in the
  UI OpenAPI contract per the `ui-openapi` skill.
- Accessibility for the new action buttons: native `<button>`, keyboard reachable,
  visible focus ring, accessible name that includes the issue number, and a
  non-colour status signal (text + icon) for `PARKED`/`FAILED`/`ABANDONED` in both
  themes. The error toast for a `FAILED` disposition must not auto-dismiss. Abandon
  is destructive-by-consequence (it makes the issue reset-eligible), so it needs a
  confirm step whose dialog states what becomes possible, keeps focus trapped, and
  is dismissible by `Escape`.

---

## 9. Test surface

Required admission and multi-record regressions (in addition to the tables below):

- Drive `coding-done` submission through the authenticated run-bound endpoint,
  durable receipt, validator callback and terminal capture. Invalid canonical
  submission followed by a corrected side submission still yields the valid
  attested group. Forged run identity, edited manifest, forged `passed=true`,
  mismatched hash/head and a raw validation path outside the run cannot create
  authority. Simulate crashes before/after every intake durable write and a
  submission racing intake closure; no acknowledged receipt is lost.
- Historical unregistered sidecars remain inadmissible automatically; operator
  import executes configured validation at the exact chosen commit, generates a
  new attestation and admits `PARKED` only. A normal completion whose raw validation
  pointer names an external cache is normalized through the validator's certified
  copy rather than rejected or trusted by path.
- `EXCHANGE_APPROVED` requires exact reviewed head, matching reviewer session and
  report/decision hashes, and the active review-cache boundary. Missing proof
  routes ordinary review; stale or forged approval never marks a new head reviewed.
- Cross admission gates (`QUEUED`, ahead/detached/historical `PARKED`, `FAILED`)
  with descendant waiter release, late descendant admission and attached evidence
  promotion. A lineage change never erases an approval requirement, and an initial
  `QUEUED` value never erases a divergent/ancestor lineage restriction.
- Recover or abandon one of two divergent or different-branch records; the other
  retains `recovery-pending` and its record-scoped needs-human cause. Only the last
  eligible release clears the label. Crash after resolution before the label write,
  then restart; projection converges. Interleave sibling admission with aggregate
  clear across processes and assert the issue gate prevents a stale clear. Busy
  gates retry/fail closed, never time out into takeover, and a finalizer's record
  claim remains valid through every stage. Ancestor resolution clears the aggregate
  after its transaction without requiring operator action.

**Producer → command** (the fact-gathering half):

- Each abnormal edge builds the correct `AutomaticCaptureCommand`:
  exchange `STOPPED/MAX_ROUNDS_EXCEEDED`, `STOPPED/REVIEWER_REPORTS_NO_PROGRESS`,
  every `ERROR/*` reason, outer session timeout, orchestrator shutdown,
  hold/retry/cancel races (#6960), respawn-during-cleanup (#6986), detached HEAD
  (#7017).
- `terminate_issue_runtime()` calls disposition **before** pair release, job cancel,
  and session stop — asserted by call ordering against a recording fake.
- Escrow failure raises and **no** teardown occurs.
- **Every** abnormal edge passes the **exact** `SessionRunAssets` its launch
  transaction recorded — asserted by identity against the injected value, not by
  path shape — through `terminate_issue_runtime`, `ActionApplier`, orchestrator
  shutdown, the dashboard/tech-lead reset, and `history_reconciliation`.
- Missing ownership fails closed: a live `Session` with no ledger row raises
  `IssueRunEvidenceUnavailable`, and teardown aborts. An unreadable ledger does the
  same. Neither is ever reported as `NO_RUNS_RECORDED`.
- `NO_RUNS_RECORDED` for an issue that genuinely never launched produces an empty
  batch and a clean teardown — the positive fact is distinguishable from the failure.
- The run-evidence fake **raises** on `find_run_dir`, latest-run selection,
  session-name search, and worktree traversal, so any rediscovery attempt fails the
  test rather than passing quietly.
- `AutomaticCaptureCommand` cannot be constructed without run evidence and
  `StoredEvidenceCommand` cannot be constructed with an empty `evidence_id`, actor,
  or a snapshot naming different evidence — `__post_init__` errors, not runtime
  branches.
- **One first-time termination whose evidence holds two exact run records at
  *descendant* heads, and a second at *divergent* heads.** For each: every distinct
  validated head is escrowed and ref-pinned, lineage is classified (`HEAD`+`ANCESTOR`
  / both `DIVERGENT`), the returned batch reports **every** disposition, and teardown
  is asserted not to have run until all of them were durably admitted. A capture that
  admitted only the latest head fails these.
- A group that cannot be escrowed raises and **nothing** is torn down — including the
  groups that had already been escrowed, which stay admitted and unresolved rather
  than being rolled back into invisibility.
- `ValidatedWorkDispositionBatch` refuses **two members for one `record_id`** —
  including the case that previously slipped through, two *different* evidence ids
  for the same record — refuses a member belonging to another issue, and accepts
  distinct record ids. `no_work()` produces an empty batch whose `found_work` is
  false and whose `unresolved` is false.
- `VALIDATED_WORK_DISPOSITION_OBSERVED` and the `ActionResult` are constructed
  **solely from the bound batch**, with no store lookup — asserted by building both
  against a store double that fails the test if it is read.

**Command → handler** (the consumption half):

- Every `ValidatedWorkState` — the cases are **derived from the enum**, not a
  pinned count, so adding or retiring a state changes the test surface
  automatically — renders the correct issue-detail view model and the correct
  operator action availability. An **empty batch** renders the no-work view with no
  operator actions offered (§2.2's `no_work()`; there is no `NONE` member to
  render).
- The tech-lead board renders a `recover_validated_work` gated op; removing
  `proposed-tech-lead` and the Control Center command reach the same owner and
  produce the same typed result.
- `session_controller`/`session_completion` classification: an issue whose batch
  `found_work` is never recorded as generic `timed_out`.
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
| §4.4 publisher | Open PR *P* at remote head `R`, target `L`: the publisher **pushes** and does not take an existing-PR shortcut; end state has remote head `L`. Run through **both** owners — `PublishRecoveryService` with `UNCONSTRAINED`, and the disposition path through `FencedValidatedHeadPublisher` — since the manual regression must survive the split. |
| §4.4f composition | The manual combined path and the fenced two-step path produce an **identical** `PublishValidatedHeadOutcome` — compared field by field, not by final remote head — for push success, already-at-target, PR reconcile, PR adoption, PR creation, PR refusal, PR transient failure, branch diverged, and branch transient failure. Driven through the composer table so every (branch status × PR status) pair named there has a case. |
| §4.4f composition | Both supersession points, field by field. **Before the branch write**: `SUPERSEDED` with `observed_remote_head_sha` and `push_outcome` None, and `push_validated_head` never called. **Between the steps**: `SUPERSEDED` carrying the branch stage's observed head and push outcome, the branch effect standing at the remote, and `ensure_pull_request` never called. Neither constructs a fabricated `BranchWriteStatus`. The manual path, having no claim, cannot produce either. |
| §4.4e outcomes | The owner's outcome cases are **derived from `PublishValidatedHeadStatus`**, not enumerated by hand, so a new member cannot silently drop out of coverage. `SUPERSEDED` asserts: no outcome recorded, no phase advance, no state transition — **and that the attempt row inserted before the call is still present and still outcome-less**, since the caller may already have moved the branch. A companion case proves the new owner reconciles that row rather than resubmitting blind, and that it counted against `PUBLISH_ATTEMPT_LIMIT`. |
| §4.4f owners | A valid publisher request is constructible through **each** allowed owner using only that owner's own authority: the manual path from its locator/background-job authority, the disposition path from an acquired `ValidatedWorkClaim`. Neither test may construct the other's authority type. |
| §4.4f owners | Guardrail: the disposition path reaches `ValidatedHeadExecutor` **only** through `FencedValidatedHeadPublisher`, and `PublishRecoveryService` never acquires a claim or constructs one. Manual duplicate-submission and tombstone behaviour is asserted unchanged. |
| §4.4 publisher | Remote branch already at `L`: `ALREADY_AT_TARGET`, **no second push**, decided on the branch ref alone (no PR consulted). |
| §4.4 publisher | Remote head is a third sha `X`: `DIVERGED`, no push, no PR write, no label writes. |
| §4.4d PR ensure | Branch at `L`, `pr_number=None`, **no PR** (crashed after push, before create): restart creates exactly one PR, routes review, and does not re-push. |
| §4.4d PR ensure | Branch at `L`, `pr_number=None`, an open PR for the branch already exists (crashed after create, before persisting the number): it is adopted, not duplicated; a prior-attempt PR on another branch is not adopted. |
| §4.4e ordering | The record is `PUBLISHING` **and its attempt row exists** before the publisher is invoked — asserted by a publisher double that reads the store when called. A crash inside the publisher leaves `PUBLISHING` with an outcome-less attempt, never `QUEUED`-with-a-completed-push. |
| §4.4e ordering | Two concurrent drains on one row, **and two drains in separate processes over one database file**: exactly one `acquire_claim` wins, exactly one remote call is made, and the loser makes no writes. A rival arriving while the owner is alive also gets `None`. |
| §4.4e fencing | **Pause attempt 1 inside the publisher; model its process as dead; let a second process take over and run attempt 2 to completion; then revive attempt 1.** Assert exactly one effective PR ensure, one routing, one history finalization, and one accepted outcome; assert attempt 1's `record_attempt_outcome`, `record_pr_number`, and `record_finalization_phase` all return False and write nothing; and assert attempt 1's pre-mutation `holds_claim()` checks abort it before any remote call. The takeover is driven by the modelled death, **not** by elapsed time — the earlier version of this case advanced past a lease, which the contract no longer honours. |
| §4.4e fencing | **Pause owner A *after* `record_attempt_outcome(PUBLISHED)` is durable and *before* finalization stage 1**, then run a second process's drain. While A is live, B's `acquire_claim()` returns None and B performs nothing — **however long A is paused**, since no clock participates. Then model A's death (its gate released): B takes over, bumps the fence, and resumes finalization from the recorded phase. The fixture then "revives" A and asserts **zero** accepted writes and zero effects. Exactly one routing, one history append, one label sequence, one phase progression, and one terminal transition across both processes. This is the window an attempt-scoped fence left open. |
| §4.4e claims | A `ValidatedWorkClaim` built from **everything the row discloses** — `record_id`, the current `owner_fence`, the recorded owner identity, and `owner_claim_hash` — is rejected by every fenced store call, because the row never contains the secret those calls verify. This is the regression that the previous `(record_id, fence)` shape could not pass. |
| §4.4e claims | A claim carrying the correct secret but presented from a **different process** is rejected by the calling-process equality check, so a leaked or serialized claim confers nothing. |
| §4.4e claims | The claim is held across the whole phase: a successful attempt outcome does **not** release it, and `relinquish_claim()` — the single release command — is called only at a quiescent point, in the terminal transition or on graceful shutdown. A live owner is never displaced mid-finalization no matter how long it takes, because nothing measures how long it takes. |
| §4.4e fencing | The stale attempt's push is rejected by the exact ref lease, and its PR create is rejected by the remote's one-open-PR-per-(head,base) rule and mapped to adopt — asserted against a fake that records every remote call, so a second PR or a second push fails the test. Two marked open PRs for the branch produce `FAILED(DUPLICATE_OPEN_PR)`, never a silent pick. |
| §4.4e takeover | Takeover is admitted **only** on gate-backed death proof. A live owner is never displaced, no matter how long it has been working (assert zero writes and no fence bump) — asserted at **two** points in the phase, mid-attempt and mid-finalization, since the rule is stated over the whole claim. **No test may advance a clock to obtain ownership; there is no clock in the path.** A modelled-dead owner never resumes: the fixture that "revives" it asserts every fenced call returns False. |
| §4.4e liveness | `RepoLockLiveness` proves death from the **gate**, never from `lock.json`: a hand-written or stale advertisement naming a dead pid does not authorize takeover. |
| §4.4e liveness matrix | One deterministic case per row, driven against real gate files: (1) self ⇒ False; (2) different host ⇒ False, with no filesystem access to that host attempted; (3) **same-instance restart** ⇒ True with **no probe issued** — asserted by a gate double that fails the test if the held gate is reopened, since probing it would self-conflict and strand the record forever; (4) single-instance current vs any other owner ⇒ True; (5) named current vs a former single-instance owner ⇒ True; (6) named current vs a *different* live named instance ⇒ False by non-blocking probe, and ⇒ True once that instance's gate is released, with the probe asserted not to disturb the live holder. |
| §4.4e relinquish | `relinquish_claim()` is callable only by the owner at a stage boundary, clears ownership and bumps the fence, and lets the next drain proceed with no death proof. There is **no** operation that takes a claim from a live owner — asserted by the absence of such a method on the port and by a guardrail. |
| §8.4 stop engine (producer) | Snapshot → rendered context → guarded request, both hops: the issue detail renders the `ClaimOwnerFact` as text plus a **link carrying `stop_owner_of={record_id}`** and asserts **no engine control in the issue view**; the engine surface with that context renders the native `Stop engine` button and posts a body whose `engine` object is byte-for-field what it rendered. Without the context, the engine surface's ordinary controls are unchanged. |
| §8.4 stop engine (handler) | Guarded request → reservation port → lifecycle → `IncarnationStopPort.stop_expected(command)`: assert exact repository, named or `default` instance, process incarnation, actor/reason and explicit timeout/force policy. One case per `StopOwnerStatus` enum member: pre-reservation refusals have zero lifecycle calls; `STOP_FAILED` follows the typed lifecycle failure; lifecycle `TARGET_CHANGED` maps to `OWNER_CHANGED` without a stop effect. Missing process identity/fence is rejected, never filled from current state. |
| §8.4 reservation | Pause after `reserve_owner_stop()` succeeds and attempt hand-back: `relinquish_claim()` returns `False`, the live owner keeps its claim, and a successor cannot acquire. Mirror case: move the claim before reservation, including relinquish/reacquire by the same process with a new fence; admission refuses `OWNER_CHANGED` with zero lifecycle calls. |
| §8.4 reservation concurrency | Two overlapping identical requests never share a reservation: the second returns `STOP_IN_PROGRESS` with zero writes/calls. After the first fails and releases, a new request gets a fresh id; replaying the old release returns `False` and leaves the new reservation intact. Concurrent transactions use the real SQLite store, not a serialized mock. |
| §8.4 reservation cleanup | Conditional release runs in `finally` after every successful reservation, including lifecycle failure, refusal and exception; reservation refusals have no token to release. A killed coordinator leaves `STOP_IN_PROGRESS` and prevents relinquish; the UI explains the existing independently authorized engine stop route. Owner death still permits acquisition, atomically clears the old reservation and advances the fence; a stale release cannot affect a successor's reservation. |
| §8.4 stop incarnation | Reserve A, pause before lifecycle dispatch, kill A and start A′ in the same instance without ownership of the record: result `OWNER_CHANGED`, zero effects on A′. Repeat after A's handle is accepted and between graceful request and force escalation: A's exit yields `STOPPED`, with zero effects on A′ despite reused port/pid advertisements and a successor claim/reservation. Every effect remains bound to A's incarnation. Real-process tests demonstrate the stop handle's lifetime binding; check-then-signal or ordinary `SupervisorOps.stop()` delegation fails the guardrail. |
| §8.4 stop policy | One case per `StopEngineStatus`: graceful exit, successful force and already-gone all yield `STOPPED`; replacement yields `TARGET_CHANGED`; stop/identity-proof failure yields `FAILED`; remote target yields `REMOTE_HOST` without a call. The new adapter produces each message explicitly; no log parsing or message extraction from a Boolean. Assert command timeout/force values and confirmation text stating graceful-then-force and engine-wide scope. |
| §8.4 stop capability | Linux pidfd integration covers SIGTERM handler shutdown, timeout SIGKILL and handle exit observation without HTTP, process-group effects or advertisement deletion. On macOS or unavailable pidfd, `EXACT_TARGET_UNAVAILABLE` reaches both snapshot and Control Center rendering: no guarded stop button, explanatory text and working navigation to the existing independent stop control with its own scope confirmation. Capability loss after render yields `FAILED` and zero effects, releases the reservation, and never delegates to pid/port-based legacy helpers. |
| §8.4 stop engine (wedged) | The Control Center succeeds while the Repository Engine serves no HTTP: read through `SqliteValidatedWorkRecordReader`, reserve/release through the writable `SqliteValidatedWorkStopReservations` adapter against the same real database, then stop through the exact-process capability. Database admission failure causes zero lifecycle calls. This proves the full interlock, not just the snapshot, works out of process. |
| §8.4 stop engine (identity) | All four identities must agree: route (`repo_key`/`instance_key`), rendered body engine, record repository, and current claim owner. A body whose engine contradicts the path is `REPO_MISMATCH` with zero effect — the duplicated path value is checked, not ignored — and `instance_key` round-trips `default` ↔ `None` through the one shared encoder used by both the link builder and the route parser. |
| §8.4 stop engine (navigation) | All three context cases: embedded (with `cc_origin` stamped by the shell), standalone carrying `cc_origin`, and standalone without it — the last asserted to render **text with instructions and no anchor at all**, never a dead href. Plus the transport itself: `cc_origin` is in `EMBEDDED_CONTEXT_PARAMS` and is stamped by the shell's URL builder, asserted by navigating two hops inside the dashboard and finding it preserved alongside `theme`. |
| §8.4 stop engine (transport) | The endpoint is registered in the UI OpenAPI contract with its required body and typed response; it uses the shared browser-session auth helper (CSRF/SSE token); `actor` comes from the session, never the body. The coordinator depends only on reader, reservation and lifecycle ports; a guardrail rejects raw store/SQLite access from it. Both store and Control Center adapters invoke one shared reservation transaction owner; the writable adapter cannot acquire/relinquish publication claims or transition record state. |
| §8.4 stop engine | `EngineIdentity` has no `targetable_here` property and consults no process-global host state; availability arrives as owner-computed data, asserted by constructing the identity in a process whose local host differs from the snapshot's and checking the rendered availability is unchanged. |
| §8.4 stop engine | `RepositoryEngineLifecycle` owns the targeted graceful stop and **only** that: the guardrail rejects direct `SupervisorOps.stop`/`stop_all_instances` calls from validated-work, disposition and issue-detail modules, and a companion assertion records that the existing bulk/force/port routes are deliberately unmigrated and unclaimed, so a later repo-wide widening is a visible change rather than a silent one. The disposition owner exposes no stop method and reads no supervisor state; the issue-detail handler builds no stop call from pid fields. |
| §8.4 stop engine | `EngineIdentity` carries `repo_root` and full `ProcessIdentity`; `stop_expected` uses these rather than ambient repository or the instance's current process. Assert with two repositories and with contradictory duplicated host/instance fields rejected by construction. |
| §4.4e takeover | There is **no** operation that displaces a live owner: the store port exposes none, and a guardrail asserts none is added. A wedged live owner leaves the record `PUBLISHING` — asserted unresolved, escrow retained, reset blocked — and the UI surfaces the owning engine. Stopping that engine (releasing its gate) is what lets the next drain acquire. |
| §4.4e attempts | Restart at **every** attempt boundary: after the claim/before the call, after the call/before `record_attempt_outcome`, and after the outcome. Each resumes without a duplicate remote write. |
| §4.4e attempts | A transient failure is followed by a **real second attempt**: attempt 2 is a distinct durable row with its own identity, the operation identity `(record_id, target_head_sha)` is unchanged, and the published head is still exactly one commit. |
| §4.4e attempts | An outcome-less attempt whose owner is **provably dead** is reconciled by the successor in `RECONCILING` and never resubmitted blind; the same row whose owner is **alive** produces zero writes and a re-check next drain, with no clock available to change that answer. |
| §4.4e outcomes | One case per `PublishValidatedHeadStatus` member — `PUBLISHED`/`ALREADY_AT_TARGET` (branch verified, PR ensured, `RECOVERED`), a success whose branch is *not* at target (`FAILED`), `TRANSIENT_FAILURE` (stays `PUBLISHING`, bounded retries, exhaustion ⇒ durable `FAILED`), `DIVERGED`/`REJECTED` (durable `FAILED`, no retry), `SUPERSEDED` (no writes at all). |
| §2.1.1 identity | Capture the same work twice at different `captured_at`, with different remote heads, after a human adds a blocking label, **and after the worktree advances**: identical `record_id` *and* `evidence_id`, exactly one row. |
| §2.1.1 identity | Requested actions supplied in a different order produce the same id; a different completion artifact hash produces a different `evidence_id` but the **same** `record_id`. |
| §2.1.3 admission | Different evidence for the same key supersedes in place: one record row, the prior evidence row at role `superseded`, both escrows and both ref sets retained. Superseding a `FAILED` row transitions *that* row; no rival row exists at any point. Recovery against a superseded-from-`FAILED` key leaves nothing unresolved. |
| §4.1 evidence relation | Exact lookup by `evidence_id` resolves **current, attached and superseded** evidence to its role and owning record, with no JSON scanned. An approval naming a non-`current` id is refused, and the refusal names the current id. |
| §4.1 evidence relation | The `ux_validated_work_current_evidence` index makes two `current` rows for one record unrepresentable; a bug that tries produces an integrity error, not a silent second current. |
| §2.1.3 attached | A capture arriving while the row is `PUBLISHING` durably records role `attached` and returns `ATTACHED` without disturbing the submission. **In the same process, without a restart**, the drain that resolves the submission then queries `attached_evidence()` and converges or supersedes. A second case restarts between the two and reaches the same end state. |
| §2.1.3 attached | Attached evidence resolved after the current submission **fails**: `resolve_attached_evidence()` promotes the oldest attached row to `CURRENT`, supersedes the failed current, and the record leaves `FAILED` rather than staying blocked behind it. Multiple attached rows are processed oldest-first, deterministically. |
| §2.1.3 attached | **Approval-required attached evidence stays approval-required.** Attach a single row admitted `PARKED(WORKTREE_AHEAD_OF_VALIDATION)`, fail the current publication, and assert the record re-opens `PARKED` with that row's original failure and reason and is **not** drained. |
| §2.1.3 attached | Initially **`FAILED`** attached evidence, as its own case: a row admitted `FAILED(VALIDATION_SHA_MISMATCH)` promotes to `FAILED` with that exact failure and reason — "copy verbatim" holds for **every** admitted state, not just the two that read as recoverable, and the promoted record stays unresolved. |
| §2.1.3 attached | The auto-eligible counterpart, as its own case: a single attached row admitted `QUEUED` (worktree head == validated head, branch binding verified) promotes to `QUEUED` and drains. Both this and the case above are then repeated **across a restart**, with the worktree deleted and no escrow envelope readable, so the values can only have come from the evidence relation. A store that defaults or re-derives publishes work a human was meant to approve. |
| §2.1.3 attached | **Multiple attached rows drain one per pass.** Attach a `QUEUED` row and a `PARKED` row, fail the current: the oldest promotes and the other stays attached; a second failure/resolution pass promotes the second. Assert the parked one never drains automatically whichever order it arrives in, and that the record is never observed with two `CURRENT` rows. |
| §2.1.3 attached | `resolve_attached_evidence()` runs on **every** drain for a non-`PUBLISHING` record, not only on the leaving edge: a record that failed, promoted one row and still holds another has the second promoted on a later drain — the regression for rows stranded until retention. |
| §2.1.3 attached | `abandon()` on a record with unresolved attached evidence returns `AbandonValidatedWorkOutcome(ATTACHED_EVIDENCE_PENDING)` with **zero** effect and the waiting ids in `pending_evidence_ids`; after the drain promotes them and none remain, the same call returns `ABANDONED` with its disposition. Retention is asserted to release **no** attached row while the owning record is unresolved. |
| §8.4 abandon contract | Both sides of the command boundary, one case per `AbandonStatus` member derived from the enum: the owner returns each status, the endpoint maps each to its response, and the **public contract** carries `can_abandon` plus `abandon_unavailable`. A record with unresolved attached evidence renders with the abandon action **absent** and the typed reason shown — the regression for a UI offering an action the owner refuses, with no response shape for why. |
| §2.1.3 attached | Every admission branch that inserts evidence writes `initial_state`/`initial_failure`/`initial_reason`: driven once per branch — new record, attached-during-publishing, retained-on-`RECOVERED`, reopened-from-`ABANDONED`, and ordinary supersede — with the `NOT NULL` constraint asserted to reject a branch that omits them. |
| §2.1.3 attached | Resolution after the current submission **succeeds**: every attached row becomes `SUPERSEDED` and the record stays `RECOVERED`. The regression is the row that stayed `attached` forever — assert it is not re-selected on the next drain, or on any drain after that. |
| §2.1.3 replay | **Repeating the same attached capture while the record is still `PUBLISHING`** returns `ATTACHED` with no insert and no integrity error. Repeating a `CURRENT` id converges; repeating a `SUPERSEDED` id returns `RETAINED` and changes nothing. Every role is replayed twice in a row. |
| §2.1.2 orphan | Repair of an orphan **whose owning record and current evidence were created in the meantime** routes through the §2.1.3 transaction and lands on the branch that is correct *now* — `SUPERSEDES` or `ATTACHED` or `ALREADY_RECOVERED` — never writing `CURRENT` from the stale envelope. Assert the live current evidence is not demoted by the repair. |
| §6 / §4.1 retention | Retention is enforced over the evidence relation for every role — current, attached and superseded — and releases nothing while the owning record is unresolved. |
| §2.1.2 atomicity | Crash after escrow rename, after ref pin, and after insert; startup `reconcile_escrow_orphans()` rebuilds the row from `capture.json` — **with the original worktree deleted** — restoring remote expectation, PR, labels and routing, and deletes nothing. An envelope whose recomputed `evidence_id` or artifact hash mismatches is reported, not repaired. |
| §2.1.2 atomicity | Re-capture converging on a `RECOVERED` id returns that record and inserts nothing; on an `ABANDONED` id it reopens as `PARKED`. |
| §4.3 phases | One test per crash row in §10, asserting the allowed-state table: `RECONCILING` accepts remote at `expected` **and** remote at `validated_head_sha`; both branch-absent variants; a third sha fails in both phases. |
| §4.3 check 8 | Fast-forward legality is proven against the **recorded expectation**, not a freshly observed head: a remote that moved to a descendant of `E` after observation still yields zero writes. |
| §7 label ownership | A validated-work `FAILED` writes **no** `tech-lead-needs-human` and **no** `needs-human`: it keeps `recovery-pending` and registers `NeedsHumanCause.VALIDATED_WORK_DISPOSITION` through the needs-human owner. Restarting the tech-lead reconciler over that issue fabricates **no** escalation and adds no explanatory comment. |
| §7 label ownership | `FAILED → RECOVERED` and `FAILED → ABANDONED` both withdraw the cause, and no stale marker or `needs-human` survives either transition. A second, independent needs-human cause keeps the block — the withdrawal is targeted, not a blanket clear. |
| §3.2 reset freshness | Scratch-reset freshness for **every** state, with `FAILED` asserted reset-**ineligible**, and `ABANDONED` asserted eligible only after a recorded `OperatorResolution`. |
| §3.2 owner symmetry | There is **no** way to call either boundary without the full owner set, because there is no per-owner surface to call: both are methods on one `IssueRuntimeLifecycleOwners` value, and `_ResetRetryRuntimeOwners` holds that same value. A test that tries to reconstruct the boundary from pieces has no function to call. |
| §4.3 check 5 liveness | A `QUEUED` record with **no** other active owner drains to `RECOVERED`. This is the regression test for the self-deadlock: had the drain consulted the *full* predicate, the owner's own `has_unresolved_work()` would read true, check 5 would fail, and the record would never publish on this or any later drain. A companion case adds a live pair and asserts the drain *is* blocked with `RUNTIME_ACTIVE` and zero writes, so the narrower bundle did not weaken the check it narrows. |
| §4.3 check 5 scope | Exercised against `OtherRuntimeActivity(core)` **with no exclusion argument, because none exists**: one case per core owner — active session, pair, supervised job, publish retry — each still blocks the drain with its kind named in the returned fact, and a probe that raises lands in `unverifiable_owners` and also blocks. Validated work is absent from the answer because it is absent from the bundle, not because it was filtered out. |
| §4.3 check 5 seam | Bootstrap wiring is acyclic and shares by identity: `CoreIssueRuntimeOwners` is constructed once and is the *same object* inside both `OtherRuntimeActivity` and `IssueRuntimeLifecycleOwners` (asserted with `is`), the full lifecycle value contains the validated-work owner while the core bundle does not, and the disposition service is constructed successfully with only the port. A guardrail asserts the service imports no runtime-owner module, and that neither activity API accepts an exclusion argument. |
| A10 boundary | Both views derive from **one** `core.probe()`: a fake core recording its calls proves the full predicate and `other_runtime_activity()` evaluate the same four probes, and adding a probe to the core reaches both without a second edit. A guardrail rejects any module that constructs `CoreIssueRuntimeOwners`/`IssueRuntimeLifecycleOwners` outside bootstrap, or that calls a per-owner termination/activity function — the individual-owner reconstruction path, not just the exclusion parameter. |
| A10 boundary | `_ResetRetryRuntimeOwners` holds the same `IssueRuntimeLifecycleOwners` value the terminator uses (asserted with `is`), so the dashboard reset and the tech-lead reset cannot consult a different owner set than the teardown they authorize. |
| §4.3 check 5 seam | The **full** path still sees validated work: `has_active_issue_runtime()` through `IssueRuntimeLifecycleOwners` reports an unresolved record as active, so reset/teardown freshness is unchanged by the split. |
| §3.1 terminator seam | The narrowed `IssueRuntimeTerminator` return type makes a terminator that discards the disposition a type error, and the seam is enumerated by the call-site guardrail rather than being invisible to it. |
| §4.4e durability | The attempt rows and `publishing_started_at` survive a simulated restart: a record that has burned N transient attempts resumes at N (not 0) and reaches durable `FAILED` at `PUBLISH_ATTEMPT_LIMIT` overall, not per process. |
| §2.1.4 lineage | Two validated heads `V` then `L` (descendant) on one issue+branch, admitted in **both orders**: exactly one row is drainable, `V`'s row is `ANCESTOR`/`PARKED`, and publishing `L` resolves `V` as `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` — leaving **nothing** unresolved and reset unblocked. |
| §2.1.4 lineage | Divergent heads (neither an ancestor of the other): both `PARKED(DIVERGENT_VALIDATED_HEADS)`, nothing auto-publishes, and publishing one after an explicit choice leaves the other parked — never silently resolved. |
| §2.1.4 lineage | An ancestor whose escrowed artifacts no longer verify is **not** resolved by the descendant's publication: it becomes `FAILED(ARTIFACT_HASH_MISMATCH)` and stays unresolved. |
| §2.1.4 waiters | Admit a **descendant**, an **ancestor**, and a **divergent** record while a predecessor is actively `PUBLISHING`. Each becomes its own `PARKED`/`PENDING` record with `waits_on_record_id` set — never an evidence role on the predecessor — and each blocks reset from the moment it exists. |
| §2.1.4 waiters | Predecessor **success**: the descendant waiter is classified `HEAD` and its baseline advances to the published head, so it does **not** fail merely because the predecessor moved the remote to the exact contained head this owner published — the specific regression. The ancestor waiter resolves by containment; the divergent waiter parks. |
| §2.1.4 waiters | The baseline advance is refused when the waiter's recorded expectation differs from the predecessor's recorded pre-push expectation: `PARKED(REMOTE_BASELINE_UNPROVEN)`, with **no remote read** involved in the decision. |
| §2.1.4 post-resolution | **Fully recover `H`, then admit ancestor `V` afterwards** — by ordinary capture, by orphan repair, and by the slice-8 backfill path. `V` resolves directly as `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` on verified escrow, is never `QUEUED`, never reaches check 8, and leaves nothing unresolved. With escrow that fails verification it is `FAILED(ARTIFACT_HASH_MISMATCH)` instead — never resolved by inference. |
| §2.1.4 post-resolution | Late **descendant** after `H` is recovered: sequenced from the durable published fact when its captured expectation is `H` or the recorded pre-push baseline, `PARKED(REMOTE_BASELINE_UNPROVEN)` otherwise. Late **divergent** after `H`: `PARKED(DIVERGENT_VALIDATED_HEADS)`, never auto-published over. Together with the ancestor row above, the post-resolution matrix is total. |
| §2.1.4 atomicity | Fault injection inside `resolve_published()` and `resolve_observed_merge()` — after the record transition, after the fact advance, after ancestor resolution — rolls the **whole** command back. Assert that neither a resolved record without its lineage fact nor a lineage fact without its matching resolution is ever observable, in either route. |
| §2.1.4 atomicity | `resolve_observed_merge()` refuses a `PUBLISHING` record with `PUBLICATION_IN_FLIGHT` and zero writes, so history reconciliation can never resolve a record out from under a live publisher; `resolve_published()` refuses a stale claim the same way. Neither command exposes a transaction handle. |
| §2.1.4 lineage fact | `validated_work_lineage` is advanced by **both** verified routes — the §4.4 drain (`PUSHED_BY_OWNER`) and §3.5's merged-PR containment (`OBSERVED_MERGE`) — never moves backward, and is the single source both the in-flight waiter rule and the post-resolution rules read. Asserted by driving the same two heads through both orderings and requiring identical end states. |
| §2.1.4 lineage fact | **Resolve `H` through history reconciliation** (merged PR, no push by this owner), then admit a late **ancestor**, **descendant** and **divergent** record through ordinary capture, orphan repair, and the slice-8 backfill path. The ancestor resolves by verified containment; the descendant parks `REMOTE_BASELINE_UNPROVEN` unless its captured expectation is the recorded published head, because `OBSERVED_MERGE` proves no baseline of ours; the divergent parks. Nothing is left unresolved by arrival order, and no route reaches check 8 with a stale expectation. |
| §2.1.4 waiters | Predecessor **definitive failure** and **abandonment**: waiters are classified against the unpublished head by the ordinary §2.1.4 rules and no baseline moves. Predecessor **outcome-less attempt taken over by a proven-dead successor**: the record stays `PUBLISHING` while the successor reconciles it, and waiters stay `PENDING` — a change of owner is not a resolution, and there is no expiry that could make it one. |
| §2.1.4 lineage | `ux_validated_work_lineage_head` makes two simultaneously drainable rows on one lineage key unrepresentable, under concurrent admission. |
| §3.5 history ordering | The selected ordering and **all** side effects, for a merged PR and a closed one: shipped-fix capture precedes the history mutation, the history events are emitted, the terminator runs, and the bound batch appears on the `ActionResult` and on `VALIDATED_WORK_DISPOSITION_OBSERVED` — with **no** disposition field on `HISTORY_RECONCILED` or `REVIEW_MERGED`, asserted positively on both. A merged PR containing `validated_head_sha` resolves the row; a merged PR that does not, and a closed PR, leave it unresolved with escrow intact. |
| §3.5 history ordering | An escrow failure inside the terminator: the history mutation stands, the action reports the failure, and the **retry reaches the terminator through the no-op branch** — the regression for a runtime that is never released and a disposition that is never captured. |
| §3.5 event payloads | For merged and closed PRs, on both the mutation and the no-op retry paths: `HISTORY_RECONCILED`/`REVIEW_MERGED` carry **no** disposition field and are emitted only on the mutation branch, and `VALIDATED_WORK_DISPOSITION_OBSERVED` is emitted after the terminator returns carrying the **actual bound batch**. Asserted by event order against a recording sink, so a payload promising data produced later fails the test. |
| §2.1 state sets | Every `ValidatedWorkState` member is persistable and every disposition names a record — there is no state that cannot be stored and no member without an identity. `UNRESOLVED_STATES ∪ RESOLVED_STATES` covers the enum exactly, asserted at import. |
| §4.5b finalization | The disposition path reaches `RetryReviewRouting` through `StagedPublishedWorkFinalizer` with the live state on the request; a `FreshIssueReadError` is a retry-next-drain transient carrying `phase_reached`, not `FAILED`. |
| §4.5b staging | Crash/restart after **every** label and routing mutation — before routing, after routing but before the phase record, after `REVIEW_ROUTED`, after `RECOVERY_CLEARED`. For each: the issue is asserted **not scheduler-eligible** at any point between publication and durable review routing (`recovery-pending` present), routing and completed history occur exactly once, and the label writes are idempotent. |
| §4.5b staging | `RECOVERED` is refused when `finalization_phase` is below `RECOVERY_CLEARED`, **even with every label already correct** — the regression for inferring the routing mutation from cleanup labels. |
| §6 retention | The retention sweep releases escrow/refs only for `RESOLVED_STATES` past the window, never for a `FAILED` record regardless of age, and never for superseded evidence of an unresolved row. |
| §4.3 check 0 authority | Approve at PR `P` / remote `R`, then refresh the same `evidence_id` to a different PR **or** a different expected remote head (or simply bump `observation_revision`). Execution is refused as stale with **zero** effects — no push, no PR write, no label write, no finalization, no state change — and the refusal names the fact that moved. Repeat for both the tech-lead op and the Control Center command. |
| §8.4 authority payload | Owner snapshot → public payload → rendered action → POST body, asserted **field by field**: every `ValidatedWorkAuthoritySnapshot` field survives the round trip unchanged. Then mutate `observation_revision`, the PR, or the expected remote head in the store and re-post the **unchanged** rendered command: it stale-downgrades with zero push, PR, label, finalization or state effects. Covered at the authenticated API level and through the UI wiring. |
| §8.4 authority payload | The endpoint **refuses** a request whose `authority` object is missing a field, rather than filling it from the current row — the regression for silently authorizing facts the operator never saw. |
| §4.3 check 0 authority | A snapshot that still matches proceeds, and **every** §4.3 check still runs afterwards against freshly read state — the snapshot is not a substitute for revalidation. A `StoredTechLeadOp` for `recover_validated_work` without a snapshot, or any other op type with one, is rejected at load. |
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
- `terminate_issue_runtime()` and `has_active_issue_runtime()` exist **only** as
  methods on `IssueRuntimeLifecycleOwners`; no free function with per-owner
  parameters remains, so a call site cannot assemble or omit its own owner set.
  `CoreIssueRuntimeOwners` and `IssueRuntimeLifecycleOwners` are constructed only in
  bootstrap. Every `terminate_issue_runtime()` call site **binds** the returned
  `validated_work` — a discarded result, or an activity
  check that skipped the owner, is the whole failure mode in miniature. The guardrail
  enumerates the seam in §3.1 as a call site, so the injected
  `IssueRuntimeTerminator` path is covered rather than invisible, and it fails if
  the alias is ever widened back to an `object` return.
- No activity predicate anywhere takes an exclusion parameter. `has_active_issue_runtime()`
  evaluates all five owners; `OtherRuntimeActivityPort` has one no-argument-beyond-issue
  method. A reintroduced `exclude_owners` — on either — is a guardrail failure.
- `CoreIssueRuntimeOwners` is constructed exactly once and passed by reference to
  both `OtherRuntimeActivity` and `IssueRuntimeLifecycleOwners`; neither copies it,
  so the four shared owners are the same objects.
- Only `ValidatedWorkDispositionService` (through `FencedValidatedHeadPublisher`)
  and `PublishRecoveryService` call `ValidatedHeadExecutor`.
- The disposition publish path never calls `fetch_for_push()` or
  `build_push_args()`; its only remote write is `push_exact()` (§4.4b). A guardrail
  over the publisher module keeps the bare-`--force-with-lease` path from creeping
  back in.
- `ValidatedWorkDispositionService` does not reference `PublishRecoveryService` —
  the two admission owners are siblings, not a chain.
- No call site passes an `IssueRunEvidenceSource`, a disposition owner, or any
  individual runtime owner to a termination or activity boundary: each receives one
  already-complete `IssueRuntimeLifecycleOwners` value and cannot supply or omit its
  parts. Bootstrap asserts that value's `run_evidence` **is** the same
  `IssueRunEvidenceSource` instance registered with the launch owner, so the ledger
  read at teardown is the ledger written at run creation. No disposition or
  termination module imports `RecordedSessionRunLookup`,
  `SessionOutput.find_run_dir`, or any latest-run/session-name/worktree-scan
  helper. Run assets reach the boundary by injection or not at all.
- No module outside the needs-human owner adds or removes `needs-human` or
  `tech-lead-needs-human` (§7). The disposition owner's only label is
  `recovery-pending`.
- Only `ValidatedWorkDispositionService` calls `acquire_claim()`/`begin_publish_attempt()`,
  and the disposition path reaches `ValidatedHeadExecutor` only through
  `FencedValidatedHeadPublisher` (§4.4f). `PublishRecoveryService` may call the
  executor directly and may never acquire a claim.
- `ValidatedWorkDispositionService` imports no runtime-owner module — not the
  session manager, pair registry, job supervisor, or publish-retry owner. Its only
  route to check 5 is `OtherRuntimeActivityPort`.
- Every `ValidatedWorkStore` mutation on a `PUBLISHING` record takes a
  `ValidatedWorkClaim` — a store write that takes a bare `record_id` on that path
  is a guardrail failure, because it is a write nothing fenced. No module
  constructs a `ValidatedWorkClaim`; it is only ever returned by `acquire_claim()`.
- `StoredTechLeadOp` for `recover_validated_work` cannot be constructed without a
  `ValidatedWorkAuthoritySnapshot`, and no module builds a `StoredEvidenceCommand`
  without one.
- `dispose_at_termination()` returns `ValidatedWorkDispositionBatch`; no call site
  indexes a single member out of it to stand for the whole result.

---

## 10. Proof: the Porchpin #5 sequence

Required walkthrough. Sequence: invalid canonical completion artifact → corrected
valid side artifact → validation at a newer local HEAD → old remote PR head →
failed ingestion → repeated tech-lead diagnosis → no executable recovery action.

**Setup.** Coding session for issue *N* on branch `b`. PR *P* exists with head `R`.
The coder's first `coding-done` writes a completion record that fails schema
validation. A second `coding-done` submits a valid record; the orchestrator preserves
both submissions through §5's intake receipts before processing them. The configured
orchestrator validator attests a passing run at local HEAD `L`, three
commits ahead of `R`.

**Today.** Ingestion consumes the invalid record, the session is classified failed,
the issue gets `blocked-failed`, no publish locators are ever written (publish never
ran), the tech lead re-diagnoses the same issue every sweep, and the only available
remedy — scratch reset — would delete `L`.

**Under the contract:**

| Step | Behaviour |
|---|---|
| Run evidence | `terminate_issue_runtime()` asks `IssueRunEvidenceSource` for issue *N* and gets `RUNS_RECORDED` with the coding session's exact `SessionRunAssets` — the row its launch transaction wrote (§2.5). No worktree is scanned and no "latest run" is chosen. |
| Admission | Both submissions have owner-registered intake receipts. Selection rejects invalid JSON and selects the valid normalized completion bound to the owner's validator attestation at `L`. Its pointer names the certified in-run copy. A historical sidecar without a receipt instead requires operator import/fresh validation and becomes `PARKED`; containment alone never admits it. |
| Evidence | Identity: `validated_head_sha=L`, `branch_name=b`, `review_disposition=RESUME_REVIEW`, `branch_binding_verified=True`. Observations (outside `evidence_id`): `worktree_head_sha=L`, `expected_remote_head_sha=R`, `pr_number=P`, `observed_blocking_labels=("blocked-failed",)`. |
| Escrow | `capture.json` + completion + validation records written to `<state_dir>/validated-work/N/<evidence_id>/` (temp dir, fsync, atomic rename); `L` pinned at `refs/issue-orchestrator/validated/N/<evidence_id>`. No `observed` ref — the two heads agree. |
| Admission | `record_id` derived from (repo, N, `b`, `L`); no existing row ⇒ `ADMITTED`. |
| Disposition | One group, so the batch holds one disposition. Evidence conclusive ⇒ `QUEUED`. `recovery-pending` added; `blocked-failed` kept. `IssueRuntimeTermination.validated_work` reports `QUEUED`, so the session is **not** classified `timed_out`. |
| Drain | Publication workspace created detached at the pinned ref. §4.3 in `PRE_SUBMISSION`: remote head is `R` as expected, PR *P*'s head is `R`, pinned ref == workspace HEAD == `L`, and `L` descends from the **recorded expectation** `R` ⇒ fast-forward legal. `begin_publish_attempt()` CASes to `PUBLISHING` and inserts attempt 1 in one transaction, **then** `publish_or_reconcile(target_head_sha=L, expectation=EXACT(R))`, **then** `record_attempt_outcome()`. |
| Publish | The publisher sees the branch at `R` — the expectation, **not** the target — so it writes: `git push --atomic origin --force-with-lease=refs/heads/b:R L:refs/heads/b`. The object published is `L` itself, and the lease guarantees the remote was still at `R`. PR *P* is then ensured: it is open on `b`, so it is reconciled, and its head is now `L`. This is the row the old contract got wrong twice — `retry_publish()` would have found an open PR for `b` and finalized without pushing, and a bare `--force-with-lease` would have re-leased to whatever the preceding fetch saw. No new PR, no supersede, no force-push, no reset. |
| Review | `StagedPublishedWorkFinalizer` composes `RetryReviewRouting` with the live state and routes **first**: the review transition is applied and then observed on a fresh read, and `REVIEW_ROUTED` is recorded. Review resumes on `L` through normal discovery; no approval label is applied, so there is no false ready-to-merge state. Throughout this stage `recovery-pending` is still on the issue. |
| Finalize | *Only now* are `recovery-pending` and `blocked-failed` removed (no `publish-failed` was ever added) and `RECOVERY_CLEARED` recorded; then the record is marked `RECOVERED` — from the durable phase, never from reading the labels back. Workspace removed; escrow + ref retained for `escrow_retention_days`. |
| Divergence variant | If `L` were **not** a descendant of `R`, §4.3 check 8 (fast-forward legality, proven against the recorded expectation `R`) fails ⇒ `REMOTE_DIVERGED` ⇒ `FAILED`, artifacts preserved, `recovery-pending` kept, and a `VALIDATED_WORK_DISPOSITION` needs-human cause registered through its owner (§7). A human resolves the divergence; nothing is force-pushed and nothing is deleted. |

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
| Attempt 1 claimed, before the publisher was invoked | `RECONCILING` | The attempt row has no outcome and its owner is provably dead (§4.4e). Remote is still at `R` ⇒ allowed ⇒ claim attempt 2 and publish. This state exists *because* the attempt row precedes the call (§4.4e) — the alternative ordering produces a `QUEUED` row with a completed push, which no phase can safely interpret. |
| Push landed, process died before the outcome was recorded | `RECONCILING` | The attempt row has no outcome and its owner is provably dead — the crash window, not a failure. Remote at `L` ⇒ allowed ⇒ `ALREADY_AT_TARGET`, reconcile without pushing and record the outcome on a new attempt. Remote at `R` ⇒ allowed ⇒ publish. Any third sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| **Push succeeded, no PR created** (`pr_number` is `None`, no PR exists) | `RECONCILING` | The branch decision returns `ALREADY_AT_TARGET` on the **branch ref alone**, so the missing PR is not a gap in the table. PR-ensure (§4.4d) finds no open PR for `b`, creates exactly one, persists its number, then routes review. No re-push. |
| PR created, its number not yet persisted | `RECONCILING` | PR-ensure discovers the open PR for `b` (scoped to the active issue branch, matched by the orchestrator body marker) and adopts it. No duplicate PR. |
| Push succeeded, PR reconcile/link failed | `RECONCILING` | Remote head and PR head are `L` — an **allowed** state for this phase, not a divergence. `ALREADY_AT_TARGET`, then finalize; no second push. |
| PR reconciled, finalization not started (`NOT_STARTED`) | `RECONCILING` | `recovery-pending` is still present, so the issue was never scheduler-eligible. Replays §4.5b from stage 1: apply and observe the routing label, re-append the in-memory review candidate and history. |
| Routing label applied, `REVIEW_ROUTED` not yet recorded | `RECONCILING` | Replays stage 1; label add/remove through `ActionApplier` are idempotent and the in-memory append dedupes on `(issue, pr)`. The observe step in stage 2 then finds the label already present and records the phase. |
| `REVIEW_ROUTED` recorded, crash before `recovery-pending` was removed | `RECONCILING` | The recovery block is **still on the issue** — this is the window the old cleanup-first ordering left open, and it is now closed by construction. Re-runs stage 1's in-memory half (memory did not survive), then stages 3–4. |
| `RECOVERY_CLEARED` recorded, row not marked `RECOVERED` | `RECONCILING` | Verifies remote head == `L` and the PR is open, then marks `RECOVERED` and resolves contained ancestors (§2.1.4). **`RECOVERED` is never inferred from "labels correct"** — the durable phase, not the label state, is what proves the routing mutation happened. |
| Branch-absent variant, push landed | `RECONCILING` | The recorded expectation was "absent", but the branch now exists at `L` — allowed for this phase (that is our push), so reconcile. A branch at any other sha ⇒ `FAILED`. A *read failure* is `REMOTE_UNREADABLE` and retries; it never re-authorizes a branch-creating push. |
| Anywhere, with a rival drainer live | either | Its `acquire_claim()` cannot prove the owner dead and returns `None`; it makes no remote call and no state change. |
| Anywhere, with the publication workspace lost | either | Recreated idempotently from the pinned ref at the top of the next drain (§4.4a). The workspace holds no unique state. |
| Record resolution/abandonment committed, aggregate block write lost | — | The next drain projects §7's issue block from retained durable rows. Other unresolved records retain `recovery-pending` and their own needs-human causes; only the last eligible interest releases the label, serialized with sibling admission by the issue gate. |
| Intake bytes persisted, ledger insert lost; or canonical ingestion fails | — | Intake orphan repair restores the immutable entry before teardown. Invalid bytes remain rejected; the corrected receipt and owner validation attestation remain selectable. An unregistered historical sidecar is never upgraded automatically. |

Every row converges on exactly one published head, one PR, one review routing, and
one terminal row — which is what the derived submission token, the `record_id`
primary key, the self-describing capture envelope, the CAS-before-invoke ordering,
the phase-aware allowed sets, and the exact-object/exact-lease write buy.

---

## 11. Implementation plan (#6914)

Ordered so each slice is independently shippable and leaves the tree green.

1. **Domain + store.** `domain/validated_work.py` (states, state sets,
   `ValidatedWorkKey`, `ValidatedWorkIdentity`, `LineageRole`, `EvidenceRole`,
   `PublicationProvenance`, `FinalizationPhase`, `canonical_record_id`,
   `canonical_evidence_id`),
   `ports/validated_work_store.py`, `infra/validated_work_store.py` with the
   **four** §4.1 tables — `record_id`-keyed records, the **evidence relation** with
   its one-current partial index, the append-only **attempt** table, and the
   **lineage publication fact** with its forward-only advance — the §2.1.3
   transactional admission, and the §2.1.4 lineage classification and
   containment-resolution (+ sqlite registry entry). Pure unit tests, including the
   identity-stability, evidence-role, attached-drain, lineage and attempt-CAS cases
   from §9. Ancestry is injected as a typed predicate here so the store is testable
   without a repository.

1a. **Batch + authority types.** `ValidatedWorkDispositionBatch`,
   `ValidatedWorkAuthoritySnapshot`, `ValidatedWorkClaim` and the owner-fence column ship
   with slice 1, because the store API is shaped by them: a fence-less store method
   would have to be re-signed later, and a singular disposition would have to be
   widened through every consumer.

1b. **Run evidence.** `ports/issue_run_evidence.py`, `SqliteIssueRunLedger`, and
   `IssueRunEvidenceService` (§2.5), with `record_run()` wired into the launch
   transaction beside the existing `SessionRunAssets` construction. Independently
   shippable and independently useful: it makes "which runs did this issue have,
   exactly" answerable without a worktree scan, which nothing else in the system
   can currently do. Fakes that raise on rediscovery ship with it.
1c. **Trustworthy completion intake.** Implement §5's `CompletionEvidenceIntake`,
   immutable submission/validator-attestation rows in the run ledger (separate
   from the four disposition tables), state-directory artifact retention, typed
   submission command/receipt and authenticated run-bound endpoint. Wire
   `coding-done`, the receipt-driven processing queue, `ValidationRunner` results,
   terminal intake closure and bootstrap to the same owner. Preserve original
   bytes and normalize admitted validation pointers; no manifest or copied agent
   JSON grants authority. Add the operator-only historical intake path with fresh
   configured validation and mandatory `PARKED` admission. This must precede slice
   3: without it failed ingestion has no trusted evidence for capture to recover.
2. **Escrow + ref pinning + config.** Filesystem escrow with the capture envelope,
   atomic rename, and `reconcile_escrow_orphans()`; `WorkingCopy` extensions for the
   `validated`/`observed` refs **and `push_exact()`** (§4.4b); **the whole §8.2(b)
   config slice ships here** — `ValidatedWorkConfig` model, section key, parser +
   registration, shape validation, `to_dict`, YAML round-trip, settings field +
   section, generated reference, example, and the config/settings tests. The
   retention sweep has a real setting to read on the same day it exists.
3. **Owner, admission-only.** `dispose_at_termination()` returning an empty batch or `PARKED` members
   plus evidence capture and the §1.1 target-identity rules, taking the
   `AutomaticCaptureCommand` built from slice 1b's evidence. Introduce
   `CoreIssueRuntimeOwners`/`OtherRuntimeActivity`/`IssueRuntimeLifecycleOwners` and
   move `terminate_issue_runtime()` and `has_active_issue_runtime()` onto it as
   methods, **deleting the free functions and their per-owner parameters** so no
   call site can assemble its own set; add the fifth activity probe
   (`has_unresolved_work`), convert the probe tuple to the
   `IssueRuntimeOwnerKind`-keyed mapping evaluated once in `core.probe()`, bind the
   single `IssueRunEvidenceSource` instance into the lifecycle value beside the one
   given to the launch owner, narrow
   `IssueRuntimeTerminator` to return `IssueRuntimeTermination`, and make **every**
   call site — including `history_reconciliation`, whose no-op branch must also
   reach the terminator (§3.5) — consume the result. The typing changes ship here
   rather than with the publisher because they are what makes this slice's guarantee
   mechanical instead of conventional. At this point nothing recovers, but
   **nothing is destroyed** — scratch reset already stale-downgrades, including for
   `FAILED`.
4. **Executor + finalizer, then automatic recovery.** Introduce
   `ValidatedHeadExecutor` (§4.4c) over `push_exact()` plus PR ensure — with its two
   steps separately callable so §4.4f's fenced wrapper can interpose — and
   `StagedPublishedWorkFinalizer` (§4.5b) composing the existing `RetryReviewRouting`
   policy; re-point manual `retry_publish()` at the publisher with `UNCONSTRAINED`,
   which independently fixes its existing-PR shortcut and is shippable on its own.
   The manual path keeps `RetrySuccessFinalizer` unchanged and calls the executor
   under its own authority — only the disposition path needs the staging and the
   fenced wrapper. Then the publication workspace, the
   `begin_publish_attempt` CAS, and the `QUEUED` → `PUBLISHING` → `RECOVERED` drain with
   the phase-aware check set. Route `STOPPED/MAX_ROUNDS_EXCEEDED` in (this is #7018).
4a. **Aggregate recovery blocks.** Implement §7's single issue-block projection and
   `IssueDispositionMutationGate`, per-record needs-human cause source, restart
   reconciliation and claim-before-issue-gate ordering. Wire admission, finalization,
   abandonment and failure through it. This ships with automatic recovery, since
   clearing a shared issue label per record is unsafe once a batch has siblings.
5. **Classification cleanup.** Session/failure paths and the stuck sweep consume
   `IssueRuntimeTermination.validated_work`; generic `timed_out` becomes illegal for
   an issue with a disposition.
6. **Gated tech-lead op.** `recover_validated_work` through the existing
   `StoredTechLeadOp` lifecycle; config + settings schema; move out of
   `UNWIRED_ACT_LEVEL_TECH_LEAD_ACTIONS` once the executor is wired.
7. **Operator commands + contracts.** View model, public contract regeneration, UI
   OpenAPI, the `recover-validated-work` and `abandon-validated-work` endpoints, and
   the dashboard actions (with the §8.4 accessibility requirements). **Also the
   wedged-owner recovery path**, which is its own body of work: the
   `RepositoryEngineLifecycle` boundary and its new `IncarnationStopPort` adapter
   with Linux pidfd exact-process tests and the explicit capability-unavailable
   navigation on macOS/unsupported Linux (no macOS automatic backend is claimed), the
   `ValidatedWorkOwnerStopCoordinator` with its `reserve_owner_stop` interlock,
   `snapshot_record()` on the owner port, the Control Center-side
   `ValidatedWorkRecordReader`, writable `ValidatedWorkStopReservations` adapter
   sharing store-owned transactions, and composition root, the
   `stop-validated-work-owner` endpoint with its OpenAPI/auth wiring, the
   `cc_origin` embed-context parameter, the engine-row control and the issue-detail
   link that carries `stop_owner_of`, and the scoped guardrail. This slice claims
   **no** consolidation of the pre-existing stop surfaces: they are untouched, so
   nothing here is deferred and nothing waits on a follow-up. `abandon()` is
   the last piece, deliberately: until it exists, unresolved work has no exit at all,
   which is the safe direction to be incomplete in.
8. **Backfill the stranded cohort.** #6327/#6335/#6337 (#6914) and #5204/#5561
   (#7011) admitted through §5's operator historical-intake and fresh-validation
   command as `PARKED` records. Existing sidecars need no fabricated old ledger
   entry; the new recovery run supplies trustworthy evidence through the same owner.

Slice 3 is the one that stops the bleeding; slices 4–8 recover what is already lost.
