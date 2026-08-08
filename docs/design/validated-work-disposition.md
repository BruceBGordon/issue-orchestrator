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
    NONE       = "none"       # resolved: no completed+validated work at this edge
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
    ValidatedWorkState.NONE,
    ValidatedWorkState.RECOVERED,
    ValidatedWorkState.ABANDONED,
})

# NONE is a DISPOSITION state, never a ROW state. "No completed+validated work at
# this edge" means no evidence was admitted, so no row exists to hold it; a stored
# NONE would be a row asserting its own absence. Only the six remaining states are
# ever persisted, and §2.1.3's `row.state in RESOLVED_STATES` branch therefore
# reaches only RECOVERED and ABANDONED. The store rejects an attempt to write NONE.
PERSISTED_STATES = frozenset(ValidatedWorkState) - {ValidatedWorkState.NONE}

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
      INSERT evidence (:evidence_id, role := CURRENT, observation_revision := 0)
      classify_lineage(row)              # §2.1.4, same transaction
      -> ADMITTED
  if row.state == PUBLISHING:
      INSERT evidence (:evidence_id, role := ATTACHED)   # DURABLE, not just escrow
      -> ATTACHED                        # do not disturb an in-flight submission
  if row.state == RECOVERED:
      # This validated head is already published, so this evidence's work is safe.
      # RETAIN it rather than dropping it: it is still addressable, still escrowed.
      INSERT evidence (:evidence_id, role := SUPERSEDED)
      -> ALREADY_RECOVERED
  if row.state == ABANDONED:
      # New evidence for abandoned work is exactly the signal that made it
      # recoverable again.
      demote(current -> SUPERSEDED) ; INSERT evidence (:evidence_id, role := CURRENT)
      row.state := PARKED ; refresh observations
      -> REOPENED
  # row is QUEUED / PARKED / FAILED with DIFFERENT evidence for the SAME work
  demote(current -> SUPERSEDED) ; INSERT evidence (:evidence_id, role := CURRENT)
  row.state := initial_state ; refresh observations
  classify_lineage(row)                  # the head may have moved
  -> SUPERSEDES
COMMIT
```

Every branch is one transaction over both tables, so a role can never be observed
without its record and no evidence can exist in two roles. Every branch is also
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

| Record reached | Every attached row becomes | Why |
|---|---|---|
| `RECOVERED` | `SUPERSEDED` (record untouched) | attached evidence is for the **same** `ValidatedWorkKey`, so its validated head is exactly the head that just published. Its work is safe; retain the bytes, act on nothing. |
| `FAILED` | the **oldest** attached row becomes `CURRENT`, the failed current becomes `SUPERSEDED`, and the record re-opens to that evidence's initial state (`QUEUED`/`PARKED`); remaining attached rows stay attached | this is the "new evidence resolves a `FAILED` row in place" rule (§2.1.3), reached from the other direction. A failure must never be left blocking behind evidence that could clear it. |
| `ABANDONED` | as `FAILED`, re-opening to `PARKED` | new evidence for abandoned work is the signal that made it recoverable. |

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

**Admission classifies the new head against every unresolved row on the key**, using
`merge-base --is-ancestor` against the **pinned refs** (§6) so a pruned object can
never make the comparison unanswerable:

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
| `RECOVERED` at `H` | a descendant of `H` | `HEAD`, `QUEUED` — **and its compare-and-set baseline is advanced to `H`**, but only under the proof below |
| `RECOVERED` at `H` | an ancestor of `H` | `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` if its escrow still verifies, else `FAILED(ARTIFACT_HASH_MISMATCH)` — never resolved by inference |
| `RECOVERED` at `H` | divergent from `H` | `PARKED(DIVERGENT_VALIDATED_HEADS)` |
| `FAILED` or `ABANDONED` | any | classified by §2.1.4's ordinary admission rules against the predecessor's **unpublished** head. No baseline moves — nothing was published, so the waiter's captured expectation still stands. |
| still `PUBLISHING` (an attempt merely expired and reconciled) | any | stays `PENDING`. Reconciliation is not resolution. |

**The baseline advance is proven, not observed.** A descendant waiter captured its
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

**Publication resolves the ancestors it contains.** In the same transaction that
marks the `HEAD` row `RECOVERED` at published head `H`, every unresolved row on the
lineage key whose `validated_head_sha` is an ancestor of `H` **and whose escrowed
artifacts still verify** is marked `RECOVERED` with
`resolution_kind = CONTAINED_IN_PUBLISHED_HEAD`, `published_head_sha = H`, and its
own escrow and refs retained for the window. Its work is *in* the published head;
holding it unresolved would be a deadlock in defence of nothing.

The artifact re-verification is not ceremony. Resolving an ancestor is the one place
this contract marks work safe without publishing it, so it is gated on the same
evidence check as a real publication: an ancestor whose escrow no longer verifies
becomes `FAILED(ARTIFACT_HASH_MISMATCH)` — still unresolved — rather than being
resolved by inference. **Divergent rows are never resolved by another row's
publication**, because their commits are genuinely not contained; publishing one
leaves the others parked for an explicit choice, which is the correct answer to
"these two validated heads are incompatible".

**At most one drainable row per lineage key**, enforced durably:

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
is a forbidden fallback: return `NONE` (a completed+validated run passes through
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


@dataclass(frozen=True, slots=True)
class ValidatedWorkDispositionBatch:
    """EVERY disposition one termination produced. One per distinct ValidatedWorkKey.

    Singular was a data-loss path, not merely an imprecise type. A ledger
    holding run A validated at ``V`` and run B validated at a divergent ``L``
    has two units of work; a result that can hold one meant capture had to pick
    one, and teardown then removed the worktree of the other. The lineage
    machinery cannot protect a record that admission never created.
    """
    issue_number: int
    dispositions: tuple[ValidatedWorkDisposition, ...]

    def __post_init__(self) -> None:
        if not self.dispositions:
            raise ValueError("a batch always reports at least NONE")
        states = [d.state for d in self.dispositions]
        if ValidatedWorkState.NONE in states and len(states) != 1:
            raise ValueError("NONE means nothing was found; it is never one of several")
        ids = [d.evidence_id for d in self.dispositions if d.evidence_id]
        if len(set(ids)) != len(ids):
            raise ValueError("one disposition per distinct unit of work")

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
    publish_attempts: int             # §4.4e attempt rows; a retry loop is visible
    finalization_phase: FinalizationPhase      # §4.5b; where a resumed finalize restarts
    updated_at: str
    can_recover: bool                 # PARKED, or FAILED after the condition is fixed
    can_abandon: bool                 # PARKED/FAILED only — never QUEUED/PUBLISHING
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
    ) -> ValidatedWorkDisposition: ...
    """Operator explicitly accepts the loss: UNRESOLVED -> ABANDONED.

    The single modeled route out of FAILED/PARKED without a recovery.
    Requires an actor and a reason, is refused for QUEUED/PUBLISHING (stop
    the in-flight work first), and retains escrow + refs for the retention
    window regardless. Never callable by an agent.
    """

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

`validated_work` **and `run_evidence`** become **required keyword parameters** of
`terminate_issue_runtime()` — not `| None = None`. A required parameter is the
mechanical guardrail (ADR-0012) that stops a future terminal path from opting out.
The boundary takes the *source* rather than a prebuilt `IssueRunEvidence` for one
reason: three of the four call sites hold only an issue number, and a caller forced
to build the evidence itself is a caller that will eventually build it from a
worktree scan. Step 0 is inside the boundary, so there is exactly one place that
knows how run evidence is obtained.

There are **three direct call sites and one typed-callable seam**, and every one of
them must now carry both owners:

| Path | Site | Today | Source of `run_evidence` |
|---|---|---|---|
| Orchestrator facade (also how the tech-lead kill wiring and shutdown arrive) | `infra/orchestrator.py:240`, inside `Orchestrator.terminate_issue_runtime_for_issue` | returns `IssueRuntimeTermination` | `deps.issue_run_evidence` |
| Action applier | `control/action_applier.py:1134` | returns it | constructor-injected, beside its other owners |
| Dashboard / tech-lead reset | `entrypoints/web_retry_history_routes.py:554` | **result discarded** | new field on `_ResetRetryRuntimeOwners` (§3.2) |
| Awaiting-merge reconciliation | `control/history_reconciliation.py:85`, through the injected `IssueRuntimeTerminator` | **result discarded, and its type is erased** | bound into the terminator closure by its builder (§3.5) |

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
    if kind not in exclude_owners                           # §4.3 check 5 only
)
```

The tuple becomes a keyed mapping for one reason: §4.3 check 5 needs to ask "is any
owner *other than me* active?", and a positional tuple cannot express which element
to skip. `exclude_owners` defaults to empty, so this is a no-op for every existing
caller; the rule that keeps it a no-op for every *future* one is in §4.3.

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
| `session_controller.py` / `session_completion.py` / `completion_action_planner.py` — classify to `TIMED_OUT`/`FAILED` | Consult `IssueRuntimeTermination.validated_work`. When the batch is anything other than a lone `NONE`, the recorded session outcome and the emitted event carry **every** member disposition; generic `timed_out` is no longer a legal classification for an issue whose batch reports any state in `UNRESOLVED_STATES` (`QUEUED`/`PARKED`/`PUBLISHING`/`FAILED`). |
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
GitValidatedHeadPublisher(working_copy, worktree_manager, repository_host)   # §4.4
StagedPublishedWorkFinalizer(                                                # §4.5b
    review_routing=RetryReviewRouting, phase_recorder=store,
    fresh_issue_reader=..., action_applier=..., label_manager=...,
)
ValidatedWorkDispositionService(
    store, escrow, working_copy, worktree_manager, repository_host,
    publisher, finalizer, action_applier, label_manager, needs_human_block, events,
)
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
| PR **merged**, and `validated_head_sha` is contained in the merged head | `RECOVERED` with `resolution_kind = CONTAINED_IN_PUBLISHED_HEAD` — the same lineage resolution §2.1.4 defines, reached by a different route. The work shipped; nothing is stranded. |
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

Three tables, because there are three lifetimes: the **work** (one row per
`record_id`, forever), the **evidence** for that work (many rows, each with a role),
and the **publish attempts** against it (many rows, append-only).

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
    active_fence          INTEGER NOT NULL DEFAULT 0, -- §4.4e; monotonic, never reused
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
    fence                 INTEGER NOT NULL,           -- must equal records.active_fence to act
    owner_host            TEXT NOT NULL,              -- liveness evidence for reclamation
    owner_pid             INTEGER NOT NULL,
    owner_started_at      TEXT NOT NULL,              -- pid reuse guard
    started_at            TEXT NOT NULL,
    lease_expires_at      TEXT NOT NULL,
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
```

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


def has_active_issue_runtime(
    *,
    ...,                                                   # unchanged owner params
    validated_work: ValidatedWorkDispositionOwner,         # required (§3.2)
    exclude_owners: frozenset[IssueRuntimeOwnerKind] = frozenset(),
) -> bool: ...
```

The probe tuple becomes a `Mapping[IssueRuntimeOwnerKind, Callable[[], bool]]`, and
the predicate evaluates every kind **not** in `exclude_owners`. Three properties keep
this from becoming a fresh way to stop looking at an owner:

- **Empty by default.** Every existing caller keeps today's behaviour without
  naming the parameter, so the freshness/teardown symmetry is unchanged.
- **Exclusion is narrower than omission.** The owner is still a *required*
  parameter; `exclude_owners` only suppresses one probe for one call. A caller
  cannot use it to avoid knowing about an owner — it has to name the owner to skip
  it, which is the opposite of forgetting.
- **Exactly one caller may exclude anything.** §9's guardrail asserts that
  `exclude_owners` is non-empty only inside `ValidatedWorkDispositionService`, and
  only as `{IssueRuntimeOwnerKind.VALIDATED_WORK}`. Every other call site passes the
  default. A second exclusion is a guardrail failure, not a code review judgement
  call.

So check 5 reads, in full: `has_active_issue_runtime(..., exclude_owners={VALIDATED_WORK})`
is false. Another issue-scoped owner being live still blocks the drain (`RUNTIME_ACTIVE`,
retried next drain) — publication must never race a live session — but the owner's
own record no longer blocks itself.

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


# PublishAttemptClaim (§4.4e) is the durable identity of ONE attempt at ONE
# operation: record, attempt number, and the fence that authorizes it. The
# publisher receives it so it can re-check the fence immediately before each
# external write; it never invents one.


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
    attempt: PublishAttemptClaim          # claimed before this call; fenced (§4.4e)


class PublishValidatedHeadStatus(StrEnum):
    PUBLISHED         = "published"           # remote ref is now at target
    ALREADY_AT_TARGET = "already_at_target"   # remote ref was ALREADY at target
    REJECTED          = "rejected"            # preconditions unmet; nothing written
    DIVERGED          = "diverged"            # lease/FF refused; nothing written
    TRANSIENT_FAILURE = "transient_failure"   # network/5xx/auth; retryable, nothing written


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


class ValidatedHeadPublisher(Protocol):
    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome: ...
    """Branch write (§4.4b) then PR ensure/reconcile. Synchronous and idempotent.

    Returns only when the remote work for this attempt has finished or
    definitively failed. There is no in-flight state to poll: a process that
    dies mid-call leaves an attempt row with no recorded outcome, and §4.4e
    reconciles that against the remote rather than asking the publisher.

    Re-checks ``holds_fence(command.attempt)`` immediately before the push and
    again before PR ensure, and performs no external write when it is False —
    a superseded caller must be unable to touch the remote.
    """
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

#### 4.4e Attempt identity and durable ordering

**The operation is stable; the attempts are numbered.** The operation this record
is trying to perform is "publish `target_head_sha` to `branch_name`", identified by
`(record_id, target_head_sha)` and unchanging for the life of the record. Each
*attempt* at it is a row in `validated_work_publish_attempts` (§4.1) with a
contiguous `attempt_no`, and **the row is written before the external call, in the
same transaction that claims the right to make it**:

```python
def begin_publish_attempt(
    self,
    record_id: str,
    *,
    expected_states: frozenset[ValidatedWorkState],   # {QUEUED, PARKED} or {PUBLISHING}
    expected_attempt_no: int,        # the highest attempt this drainer observed
    evidence_id: str,                # must still be the CURRENT evidence
    target_head_sha: str,
    expected_remote_head: str,
    phase: DispositionPhase,
    started_at: str,
    lease_expires_at: str,
    owner: ProcessIdentity,          # host + pid + process start time
) -> PublishAttemptClaim | None: ...
    """One transaction: CAS the record into PUBLISHING (or keep it there),
    bump `active_fence`, and INSERT attempt `expected_attempt_no + 1` with the
    new fence and owner. Returns None if it lost, or if an outcome-less attempt
    is still held by a live owner."""

def record_attempt_outcome(
    self,
    claim: PublishAttemptClaim,
    *,
    outcome: PublishValidatedHeadStatus,
    failure: ValidatedWorkFailure | None,
    finished_at: str,
) -> bool: ...
    """CAS on `active_fence == claim.fence`. Returns False for a stale claim,
    whose outcome is DISCARDED — a late-returning publisher must not overwrite
    the outcome of the attempt that superseded it."""

def holds_fence(self, claim: PublishAttemptClaim) -> bool: ...
    """Re-read immediately before every external mutation. False aborts it."""
```

#### The fence: an expired timestamp is not proof the old caller is dead

The attempt row alone does **not** give "exactly one active attempt". A lease is a
timestamp, and a timestamp expiring proves only that time passed — a slow process
can still be inside `publish_or_reconcile()` and return from it afterwards. If the
reclaiming drainer starts attempt *n+1* while attempt *n* is still executing, two
callers can reach PR ensure and finalization, and the stale one can record a late
outcome over the winner's. The exact ref lease (§4.4b) bounds the damage to the
branch write; it says nothing about PR creation, label routing, history mutation, or
outcome recording.

So the correctness mechanism is a **fence**, and the lease is only a hint about when
to consider reclaiming:

```python
@dataclass(frozen=True, slots=True)
class PublishAttemptClaim:
    record_id: str
    attempt_no: int
    fence: int          # monotonic per record; NEVER reused, never decreases
    evidence_id: str
```

`validated_work_records.active_fence` is bumped by every claim and every
reclamation, and the claim's `fence` must equal it for anything to happen:

- **Every durable write takes the claim and compare-and-sets on `active_fence` in
  the same transaction** — `record_attempt_outcome()`, `record_pr_number()`,
  `record_finalization_phase()`, `resolve_attached_evidence()`, and the transition
  to `RECOVERED`/`FAILED`. A stale claim's write is **rejected**, not merged. That
  is the property the reviewer's test asks for: the stale attempt cannot change
  durable state, so a late-returning publisher cannot overwrite the winner's
  outcome.
- **The fence is re-read and re-checked immediately before each external
  mutation** — before the push, before PR ensure, and before each finalization
  stage. A stale claim aborts there and performs no remote write at all.
- **Reclamation bumps the fence**, so it permanently invalidates the previous claim
  in the same act that authorizes the new one. There is no window in which both are
  valid, regardless of what the old process is doing.

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

**Reclamation requires evidence, not just elapsed time.** The attempt row records
the owning `process_identity` (host, pid, process start time). A reclaiming drainer
may take an outcome-less attempt only when the recorded owner is provably gone —
same host, and no live process with that pid *and* start time — or, when liveness
cannot be established (a different host, an unreadable process table), after the
lease has been expired for a full `PUBLISH_RECLAIM_GRACE`, which is an order of
magnitude longer than the call timeout. This is a *politeness* rule: it avoids
reclaiming a healthy slow publisher and burning an attempt. It is emphatically not
the safety argument — the fence is, and the fence holds even when the liveness
probe is wrong.

Within one process the drain is single-threaded and `publish_or_reconcile()` is
called from it, so a second concurrent attempt on one record is only ever
cross-process. That is what makes a durable fence the right shape rather than an
in-process lock.

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
  **expired** lease is exactly "we called out and never learned what happened". The
  owner does **not** resubmit blind: it re-runs §4.3 in `RECONCILING`, where the
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

| Attempt state at drain | Owner does |
|---|---|
| outcome `''`, lease **live** | another drainer owns it. No writes, re-check next drain. |
| outcome `''`, lease expired, owner **provably alive** | do not reclaim; re-check next drain. Reclaiming would be safe (the fence protects it) but would waste an attempt. |
| outcome `''`, lease expired, owner **gone or unprovable past the grace** | bump the fence, re-run §4.3 in `RECONCILING`, and claim a new attempt only if a write is still required. The previous claim is now inert. |
| a stale claim tries to record anything | the CAS returns False; nothing is written and the attempt is logged as superseded. |
| `PUBLISHED` / `ALREADY_AT_TARGET` | re-read the branch; require it at `target_head_sha`; ensure the PR (§4.4d); finalize (§4.5); mark `RECOVERED`. A success whose branch is *not* at target is a hard `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| `TRANSIENT_FAILURE` | stay `PUBLISHING`; claim attempt *n+1* next drain while `COUNT(attempts) < PUBLISH_ATTEMPT_LIMIT`; exhaustion ⇒ durable `FAILED`. |
| `DIVERGED` / `REJECTED` | durable `FAILED` with the mapped `ValidatedWorkFailure`. Definitive: no retry. |

The transient/definitive split is `PublishValidatedHeadOutcome.retryable` — an enum
comparison over `PublishValidatedHeadStatus`, mapped from the enumerated
`ExactPushOutcome` and typed host errors. Never a substring match on an error
string.

The lease duration is derived from the existing publish job timeout (config-free,
for the same reason the attempt limit is), and an expired lease is never treated as
a failure — only as "reconcile before deciding".

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
| 3 | Remove `recovery-pending` and **only** the labels in `observed_blocking_labels`. | `RECOVERY_CLEARED` |
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
    claim: PublishAttemptClaim            # §4.4e fence; every stage is gated on it
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
        self, claim: PublishAttemptClaim, phase: FinalizationPhase
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
| Durable state | `ValidatedWorkStore` | records, evidence roles, publish attempts, finalization phase | **no** |
| Remote execution | `ValidatedHeadPublisher` | exact-object/exact-lease branch write, PR ensure | **no** |
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
   insert attempt *n+1* (§4.4e). Abort silently if it loses or the lease is live.
5. `publisher.publish_or_reconcile(command)` — exact write, then PR ensure.
6. `store.record_attempt_outcome(...)` — before anything is read from the outcome.
7. `finalizer.finalize(request)` with the live state and `resume_from` — staged
   routing, observation, then recovery clear (§4.5b).
8. Mark `RECOVERED` and resolve contained ancestors (§2.1.4) in one transaction;
   remove the workspace; retain escrow and refs for the window.

Only `ValidatedWorkDispositionService` claims disposition publish attempts. This is
enforced mechanically (§9).

---

## 5. Trusted artifact admission

Admission answers: *which bytes on disk may become executable authority?*

**The candidate set is bounded by the command, not by the filesystem.** Admission
considers exactly the runs in `AutomaticCaptureCommand.run_evidence.runs` (§2.5) —
each an exact `SessionRunAssets` recorded by the owner that allocated it. It never
enumerates run directories, never picks a "latest run", and never resolves a run
from a worktree or a session name. `NO_RUNS_RECORDED` yields `NONE`; an unreadable
ledger raised before admission was ever reached.

**Admissible sources within each such run, in this order:**

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

| Transition point | Effect that must succeed first | Labels |
|---|---|---|
| Evidence recorded (`QUEUED`/`PARKED`) | durable record + escrow committed | add `recovery-pending` (new, `LabelCategory.BLOCKING`). Existing blocking label is **kept** — the issue is not unblocked by being owned. |
| Admission to publish (`PUBLISHING`) | durable CAS to `PUBLISHING` succeeded | **no label change.** The previous draft added `publish-failed` here purely to satisfy the manual path's `board_block_reason()` precondition; §4.6 removes that round-trip, so the issue is no longer marked with a failure it did not have. |
| Review routing (stage 1–2 of §4.5b) | remote head == `validated_head_sha`, PR open | apply `pr-pending`/the review-queue transition **first**, and observe it. `recovery-pending` is still present throughout. |
| Publication + review routing durable (`RECOVERED`) | `finalization_phase == REVIEW_ROUTED` recorded | *then* remove `recovery-pending`; remove **only** the labels in `observed_blocking_labels`. |
| Disposition failed (`FAILED`) | — | keep `recovery-pending`, and register `NeedsHumanCause.VALIDATED_WORK_DISPOSITION` through the needs-human owner's API (see below). Never scratch-eligible — `recovery-pending` stays *because* `FAILED` is unresolved (§3.2). |
| Operator abandoned (`ABANDONED`) | durable `OperatorResolution` recorded | remove `recovery-pending` (the work is now formally resolved and reset may proceed); **withdraw** the needs-human cause; leave pre-existing blocking labels untouched. Escrow and refs are still retained for the retention window. |
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
  — `state`, `unresolved`, `evidence_id`, `validated_head_sha`, `worktree_head_sha`,
  `pr_number`, `failure`, `escrow_retained`, `lineage_role` (so an ancestor parked
  behind a descendant reads as *waiting*, not *stuck*), `publish_attempts`,
  `finalization_phase`, and the available operator actions (`recover`, and `abandon`
  for a `PARKED`/`FAILED` record). A `DIVERGENT` row surfaces the sibling heads so
  the operator's choice is informed rather than blind. Regenerate
  `contracts/public/*.json` with `scripts/generate_public_contracts.py`; drift is
  enforced by `tests/unit/test_public_contract_schemas.py`.
- The block is a **list**, because an issue can hold several records (§2.1.4): a
  descendant head plus the ancestors parked behind it, or two divergent heads
  awaiting a choice. Rendering one and hiding the rest is the same collapse-to-a-winner
  the batch exists to prevent, and it is worse in the UI than in the owner, because
  the operator is the one being asked to choose.
- An operator action posts back the `ValidatedWorkAuthoritySnapshot` fields the UI
  displayed — evidence id, observation revision, PR number, expected remote head —
  and the endpoint builds the `StoredEvidenceCommand` from them. So the operator,
  like the tech lead, approves *specific facts*: if the record moved between render
  and click, check 0 refuses with zero writes and the UI re-renders the new facts
  rather than acting on them silently.
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
- `NO_RUNS_RECORDED` for an issue that genuinely never launched produces `NONE` and
  a clean teardown — the positive fact is distinguishable from the failure.
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
- `ValidatedWorkDispositionBatch` refuses an empty tuple, refuses `NONE` alongside
  other members, and refuses duplicate evidence ids.

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
| §4.4e ordering | The record is `PUBLISHING` **and its attempt row exists** before the publisher is invoked — asserted by a publisher double that reads the store when called. A crash inside the publisher leaves `PUBLISHING` with an outcome-less attempt, never `QUEUED`-with-a-completed-push. |
| §4.4e ordering | Two concurrent drains on one row, **and two drains in separate processes over one database file**: exactly one `begin_publish_attempt` wins, exactly one remote call is made, and the loser makes no writes. A rival arriving while the lease is live also gets `None`. |
| §4.4e fencing | **Pause attempt 1 inside the publisher past its lease; let a second process reclaim and run attempt 2 to completion; then release attempt 1.** Assert exactly one effective PR ensure, one routing, one history finalization, and one accepted outcome; assert attempt 1's `record_attempt_outcome`, `record_pr_number`, and `record_finalization_phase` all return False and write nothing; and assert attempt 1's pre-mutation `holds_fence()` checks abort it before any remote call. |
| §4.4e fencing | The stale attempt's push is rejected by the exact ref lease, and its PR create is rejected by the remote's one-open-PR-per-(head,base) rule and mapped to adopt — asserted against a fake that records every remote call, so a second PR or a second push fails the test. Two marked open PRs for the branch produce `FAILED(DUPLICATE_OPEN_PR)`, never a silent pick. |
| §4.4e reclamation | An expired lease whose owner is **provably alive** is not reclaimed; one whose owner is gone is reclaimed with a fence bump; one on an unreachable host is reclaimed only after `PUBLISH_RECLAIM_GRACE`. In all three the fence — not the liveness answer — is what the safety assertions are made against. |
| §4.4e attempts | Restart at **every** attempt boundary: after the claim/before the call, after the call/before `record_attempt_outcome`, and after the outcome. Each resumes without a duplicate remote write. |
| §4.4e attempts | A transient failure is followed by a **real second attempt**: attempt 2 is a distinct durable row with its own identity, the operation identity `(record_id, target_head_sha)` is unchanged, and the published head is still exactly one commit. |
| §4.4e attempts | An outcome-less attempt with an **expired** lease re-runs `RECONCILING` and never resubmits blind; the same row with a **live** lease produces zero writes and a re-check next drain. |
| §4.4e outcomes | One test per `PublishValidatedHeadStatus` — `PUBLISHED`/`ALREADY_AT_TARGET` (branch verified, PR ensured, `RECOVERED`), a success whose branch is *not* at target (`FAILED`), `TRANSIENT_FAILURE` (stays `PUBLISHING`, bounded retries, exhaustion ⇒ durable `FAILED`), `DIVERGED`/`REJECTED` (durable `FAILED`, no retry). |
| §2.1.1 identity | Capture the same work twice at different `captured_at`, with different remote heads, after a human adds a blocking label, **and after the worktree advances**: identical `record_id` *and* `evidence_id`, exactly one row. |
| §2.1.1 identity | Requested actions supplied in a different order produce the same id; a different completion artifact hash produces a different `evidence_id` but the **same** `record_id`. |
| §2.1.3 admission | Different evidence for the same key supersedes in place: one record row, the prior evidence row at role `superseded`, both escrows and both ref sets retained. Superseding a `FAILED` row transitions *that* row; no rival row exists at any point. Recovery against a superseded-from-`FAILED` key leaves nothing unresolved. |
| §4.1 evidence relation | Exact lookup by `evidence_id` resolves **current, attached and superseded** evidence to its role and owning record, with no JSON scanned. An approval naming a non-`current` id is refused, and the refusal names the current id. |
| §4.1 evidence relation | The `ux_validated_work_current_evidence` index makes two `current` rows for one record unrepresentable; a bug that tries produces an integrity error, not a silent second current. |
| §2.1.3 attached | A capture arriving while the row is `PUBLISHING` durably records role `attached` and returns `ATTACHED` without disturbing the submission. **In the same process, without a restart**, the drain that resolves the submission then queries `attached_evidence()` and converges or supersedes. A second case restarts between the two and reaches the same end state. |
| §2.1.3 attached | Attached evidence resolved after the current submission **fails**: `resolve_attached_evidence()` promotes the oldest attached row to `CURRENT`, supersedes the failed current, and the record leaves `FAILED` rather than staying blocked behind it. Multiple attached rows are processed oldest-first, deterministically. |
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
| §3.2 owner symmetry | `has_active_issue_runtime()` cannot be called without the disposition owner (required parameter), and `_ResetRetryRuntimeOwners` carries it into both the freshness check and the teardown. |
| §4.3 check 5 liveness | A `QUEUED` record with **no** other active owner drains to `RECOVERED`. This is the regression test for the self-deadlock: without `exclude_owners`, the owner's own `has_unresolved_work()` reads true, check 5 fails, and the record never publishes on this or any later drain. A companion case adds a live pair and asserts the drain *is* blocked with `RUNTIME_ACTIVE` and zero writes, so the exclusion did not disable the check it narrows. |
| §4.3 check 5 scope | `exclude_owners` suppresses **only** `VALIDATED_WORK`: with the flag set, an active session, pair, supervised job, or publish retry each still block the drain. |
| §3.1 terminator seam | The narrowed `IssueRuntimeTerminator` return type makes a terminator that discards the disposition a type error, and the seam is enumerated by the call-site guardrail rather than being invisible to it. |
| §4.4e durability | The attempt rows and `publishing_started_at` survive a simulated restart: a record that has burned N transient attempts resumes at N (not 0) and reaches durable `FAILED` at `PUBLISH_ATTEMPT_LIMIT` overall, not per process. |
| §2.1.4 lineage | Two validated heads `V` then `L` (descendant) on one issue+branch, admitted in **both orders**: exactly one row is drainable, `V`'s row is `ANCESTOR`/`PARKED`, and publishing `L` resolves `V` as `RECOVERED(CONTAINED_IN_PUBLISHED_HEAD)` — leaving **nothing** unresolved and reset unblocked. |
| §2.1.4 lineage | Divergent heads (neither an ancestor of the other): both `PARKED(DIVERGENT_VALIDATED_HEADS)`, nothing auto-publishes, and publishing one after an explicit choice leaves the other parked — never silently resolved. |
| §2.1.4 lineage | An ancestor whose escrowed artifacts no longer verify is **not** resolved by the descendant's publication: it becomes `FAILED(ARTIFACT_HASH_MISMATCH)` and stays unresolved. |
| §2.1.4 waiters | Admit a **descendant**, an **ancestor**, and a **divergent** record while a predecessor is actively `PUBLISHING`. Each becomes its own `PARKED`/`PENDING` record with `waits_on_record_id` set — never an evidence role on the predecessor — and each blocks reset from the moment it exists. |
| §2.1.4 waiters | Predecessor **success**: the descendant waiter is classified `HEAD` and its baseline advances to the published head, so it does **not** fail merely because the predecessor moved the remote to the exact contained head this owner published — the specific regression. The ancestor waiter resolves by containment; the divergent waiter parks. |
| §2.1.4 waiters | The baseline advance is refused when the waiter's recorded expectation differs from the predecessor's recorded pre-push expectation: `PARKED(REMOTE_BASELINE_UNPROVEN)`, with **no remote read** involved in the decision. |
| §2.1.4 waiters | Predecessor **definitive failure** and **abandonment**: waiters are classified against the unpublished head by the ordinary §2.1.4 rules and no baseline moves. Predecessor **attempt expiry/reconciliation**: waiters stay `PENDING`, because reconciliation is not resolution. |
| §2.1.4 lineage | `ux_validated_work_lineage_head` makes two simultaneously drainable rows on one lineage key unrepresentable, under concurrent admission. |
| §3.5 history ordering | The selected ordering and **all** side effects, for a merged PR and a closed one: shipped-fix capture precedes the history mutation, the events are emitted, the terminator runs, and the bound disposition appears on the `ActionResult` and the `HISTORY_RECONCILED` payload. A merged PR containing `validated_head_sha` resolves the row; a merged PR that does not, and a closed PR, leave it unresolved with escrow intact. |
| §3.5 history ordering | An escrow failure inside the terminator: the history mutation stands, the action reports the failure, and the **retry reaches the terminator through the no-op branch** — the regression for a runtime that is never released and a disposition that is never captured. |
| §3.5 event payloads | For merged and closed PRs, on both the mutation and the no-op retry paths: `HISTORY_RECONCILED`/`REVIEW_MERGED` carry **no** disposition field and are emitted only on the mutation branch, and `VALIDATED_WORK_DISPOSITION_OBSERVED` is emitted after the terminator returns carrying the **actual bound batch**. Asserted by event order against a recording sink, so a payload promising data produced later fails the test. |
| §2.1 state sets | Writing `NONE` to the store raises; `dispose_at_termination()` returning `NONE` creates no row; `PERSISTED_STATES` and the `ValidatedWorkState` enum do not drift. |
| §4.5b finalization | The disposition path reaches `RetryReviewRouting` through `StagedPublishedWorkFinalizer` with the live state on the request; a `FreshIssueReadError` is a retry-next-drain transient carrying `phase_reached`, not `FAILED`. |
| §4.5b staging | Crash/restart after **every** label and routing mutation — before routing, after routing but before the phase record, after `REVIEW_ROUTED`, after `RECOVERY_CLEARED`. For each: the issue is asserted **not scheduler-eligible** at any point between publication and durable review routing (`recovery-pending` present), routing and completed history occur exactly once, and the label writes are idempotent. |
| §4.5b staging | `RECOVERED` is refused when `finalization_phase` is below `RECOVERY_CLEARED`, **even with every label already correct** — the regression for inferring the routing mutation from cleanup labels. |
| §6 retention | The retention sweep releases escrow/refs only for `RESOLVED_STATES` past the window, never for a `FAILED` record regardless of age, and never for superseded evidence of an unresolved row. |
| §4.3 check 0 authority | Approve at PR `P` / remote `R`, then refresh the same `evidence_id` to a different PR **or** a different expected remote head (or simply bump `observation_revision`). Execution is refused as stale with **zero** effects — no push, no PR write, no label write, no finalization, no state change — and the refusal names the fact that moved. Repeat for both the tech-lead op and the Control Center command. |
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
- Every `terminate_issue_runtime()` **and** `has_active_issue_runtime()` call site
  passes the disposition owner, and every `terminate_issue_runtime()` call site
  **binds** the returned `validated_work` — a discarded result, or an activity
  check that skipped the owner, is the whole failure mode in miniature. The guardrail
  enumerates the seam in §3.1 as a call site, so the injected
  `IssueRuntimeTerminator` path is covered rather than invisible, and it fails if
  the alias is ever widened back to an `object` return.
- `exclude_owners` is non-empty only inside `ValidatedWorkDispositionService`, and
  only as `{IssueRuntimeOwnerKind.VALIDATED_WORK}` (§4.3 check 5). Any other
  exclusion, anywhere, is a guardrail failure — that parameter is the one lever in
  this contract that can make an activity probe stop looking at an owner.
- Only `ValidatedWorkDispositionService` and `PublishRecoveryService` call
  `ValidatedHeadPublisher.publish_or_reconcile`.
- The disposition publish path never calls `fetch_for_push()` or
  `build_push_args()`; its only remote write is `push_exact()` (§4.4b). A guardrail
  over the publisher module keeps the bare-`--force-with-lease` path from creeping
  back in.
- `ValidatedWorkDispositionService` does not reference `PublishRecoveryService` —
  the two admission owners are siblings, not a chain.
- Every `terminate_issue_runtime()` call site passes an `IssueRunEvidenceSource`,
  and no disposition or termination module imports `RecordedSessionRunLookup`,
  `SessionOutput.find_run_dir`, or any latest-run/session-name/worktree-scan
  helper. Run assets reach the boundary by injection or not at all.
- No module outside the needs-human owner adds or removes `needs-human` or
  `tech-lead-needs-human` (§7). The disposition owner's only label is
  `recovery-pending`.
- Only `ValidatedWorkDispositionService` calls `begin_publish_attempt()`, and no
  module calls `ValidatedHeadPublisher.publish_or_reconcile()` without a claimed
  attempt.
- Every `ValidatedWorkStore` mutation on a `PUBLISHING` record takes a
  `PublishAttemptClaim` — a store write that takes a bare `record_id` on that path
  is a guardrail failure, because it is a write nothing fenced.
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
| Run evidence | `terminate_issue_runtime()` asks `IssueRunEvidenceSource` for issue *N* and gets `RUNS_RECORDED` with the coding session's exact `SessionRunAssets` — the row its launch transaction wrote (§2.5). No worktree is scanned and no "latest run" is chosen. |
| Admission | Both records are run-scoped and therefore admissible *sources*. Selection (§5) rejects the invalid one because it does not parse, and selects the valid one because its `validation_record_path` resolves in-run to `passed=true` with `head_sha == L`. The worktree HEAD is also exactly `L`, so the target is fixed **by identity, not by ancestry**. The canonical path holds no privileged status. |
| Evidence | Identity: `validated_head_sha=L`, `worktree_head_sha=L`, `branch_name=b`, `review_disposition=RESUME_REVIEW`, `branch_binding_verified=True`. Observations (outside `evidence_id`): `expected_remote_head_sha=R`, `pr_number=P`, `observed_blocking_labels=("blocked-failed",)`. |
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
| Attempt 1 claimed, before the publisher was invoked | `RECONCILING` | The attempt row has no outcome and its lease has expired. Remote is still at `R` ⇒ allowed ⇒ claim attempt 2 and publish. This state exists *because* the attempt row precedes the call (§4.4e) — the alternative ordering produces a `QUEUED` row with a completed push, which no phase can safely interpret. |
| Push landed, process died before the outcome was recorded | `RECONCILING` | The attempt row has no outcome and an expired lease — the crash window, not a failure. Remote at `L` ⇒ allowed ⇒ `ALREADY_AT_TARGET`, reconcile without pushing and record the outcome on a new attempt. Remote at `R` ⇒ allowed ⇒ publish. Any third sha ⇒ `REMOTE_HEAD_CHANGED` ⇒ `FAILED`. |
| **Push succeeded, no PR created** (`pr_number` is `None`, no PR exists) | `RECONCILING` | The branch decision returns `ALREADY_AT_TARGET` on the **branch ref alone**, so the missing PR is not a gap in the table. PR-ensure (§4.4d) finds no open PR for `b`, creates exactly one, persists its number, then routes review. No re-push. |
| PR created, its number not yet persisted | `RECONCILING` | PR-ensure discovers the open PR for `b` (scoped to the active issue branch, matched by the orchestrator body marker) and adopts it. No duplicate PR. |
| Push succeeded, PR reconcile/link failed | `RECONCILING` | Remote head and PR head are `L` — an **allowed** state for this phase, not a divergence. `ALREADY_AT_TARGET`, then finalize; no second push. |
| PR reconciled, finalization not started (`NOT_STARTED`) | `RECONCILING` | `recovery-pending` is still present, so the issue was never scheduler-eligible. Replays §4.5b from stage 1: apply and observe the routing label, re-append the in-memory review candidate and history. |
| Routing label applied, `REVIEW_ROUTED` not yet recorded | `RECONCILING` | Replays stage 1; label add/remove through `ActionApplier` are idempotent and the in-memory append dedupes on `(issue, pr)`. The observe step in stage 2 then finds the label already present and records the phase. |
| `REVIEW_ROUTED` recorded, crash before `recovery-pending` was removed | `RECONCILING` | The recovery block is **still on the issue** — this is the window the old cleanup-first ordering left open, and it is now closed by construction. Re-runs stage 1's in-memory half (memory did not survive), then stages 3–4. |
| `RECOVERY_CLEARED` recorded, row not marked `RECOVERED` | `RECONCILING` | Verifies remote head == `L` and the PR is open, then marks `RECOVERED` and resolves contained ancestors (§2.1.4). **`RECOVERED` is never inferred from "labels correct"** — the durable phase, not the label state, is what proves the routing mutation happened. |
| Branch-absent variant, push landed | `RECONCILING` | The recorded expectation was "absent", but the branch now exists at `L` — allowed for this phase (that is our push), so reconcile. A branch at any other sha ⇒ `FAILED`. A *read failure* is `REMOTE_UNREADABLE` and retries; it never re-authorizes a branch-creating push. |
| Anywhere, with a rival drainer live | either | Its `begin_publish_attempt()` sees an unexpired lease on an outcome-less attempt and returns `None`; it makes no remote call and no state change. |
| Anywhere, with the publication workspace lost | either | Recreated idempotently from the pinned ref at the top of the next drain (§4.4a). The workspace holds no unique state. |

Every row converges on exactly one published head, one PR, one review routing, and
one terminal row — which is what the derived submission token, the `record_id`
primary key, the self-describing capture envelope, the CAS-before-invoke ordering,
the phase-aware allowed sets, and the exact-object/exact-lease write buy.

---

## 11. Implementation plan (#6914)

Ordered so each slice is independently shippable and leaves the tree green.

1. **Domain + store.** `domain/validated_work.py` (states, state sets,
   `ValidatedWorkKey`, `ValidatedWorkIdentity`, `LineageRole`, `EvidenceRole`,
   `FinalizationPhase`, `canonical_record_id`, `canonical_evidence_id`),
   `ports/validated_work_store.py`, `infra/validated_work_store.py` with the
   three §4.1 tables — `record_id`-keyed records, the **evidence relation** with its
   one-current partial index, and the append-only **attempt** table — the §2.1.3
   transactional admission, and the §2.1.4 lineage classification and
   containment-resolution (+ sqlite registry entry). Pure unit tests, including the
   identity-stability, evidence-role, attached-drain, lineage and attempt-CAS cases
   from §9. Ancestry is injected as a typed predicate here so the store is testable
   without a repository.

1a. **Batch + authority types.** `ValidatedWorkDispositionBatch`,
   `ValidatedWorkAuthoritySnapshot`, `PublishAttemptClaim` and the fence column ship
   with slice 1, because the store API is shaped by them: a fence-less store method
   would have to be re-signed later, and a singular disposition would have to be
   widened through every consumer.

1b. **Run evidence.** `ports/issue_run_evidence.py`, `SqliteIssueRunLedger`, and
   `IssueRunEvidenceService` (§2.5), with `record_run()` wired into the launch
   transaction beside the existing `SessionRunAssets` construction. Independently
   shippable and independently useful: it makes "which runs did this issue have,
   exactly" answerable without a worktree scan, which nothing else in the system
   can currently do. Fakes that raise on rediscovery ship with it.
2. **Escrow + ref pinning + config.** Filesystem escrow with the capture envelope,
   atomic rename, and `reconcile_escrow_orphans()`; `WorkingCopy` extensions for the
   `validated`/`observed` refs **and `push_exact()`** (§4.4b); **the whole §8.2(b)
   config slice ships here** — `ValidatedWorkConfig` model, section key, parser +
   registration, shape validation, `to_dict`, YAML round-trip, settings field +
   section, generated reference, example, and the config/settings tests. The
   retention sweep has a real setting to read on the same day it exists.
3. **Owner, admission-only.** `dispose_at_termination()` returning `NONE`/`PARKED`
   plus evidence capture and the §1.1 target-identity rules, taking the
   `AutomaticCaptureCommand` built from slice 1b's evidence. Wire the disposition
   owner **and the run-evidence source** into `terminate_issue_runtime()`, and the
   disposition owner into `has_active_issue_runtime()`, as required parameters; add
   the fifth activity probe (`has_unresolved_work`), convert the probe tuple to the
   `IssueRuntimeOwnerKind`-keyed mapping with `exclude_owners`, narrow
   `IssueRuntimeTerminator` to return `IssueRuntimeTermination`, and make **every**
   call site — including `history_reconciliation`, whose no-op branch must also
   reach the terminator (§3.5) — consume the result. The typing changes ship here
   rather than with the publisher because they are what makes this slice's guarantee
   mechanical instead of conventional. At this point nothing recovers, but
   **nothing is destroyed** — scratch reset already stale-downgrades, including for
   `FAILED`.
4. **Publisher + finalizer, then automatic recovery.** Introduce
   `ValidatedHeadPublisher` (§4.4c) over `push_exact()` plus PR ensure, and
   `StagedPublishedWorkFinalizer` (§4.5b) composing the existing `RetryReviewRouting`
   policy; re-point manual `retry_publish()` at the publisher with `UNCONSTRAINED`,
   which independently fixes its existing-PR shortcut and is shippable on its own.
   The manual path keeps `RetrySuccessFinalizer` unchanged — only the disposition
   path needs the staging. Then the publication workspace, the
   `begin_publish_attempt` CAS, and the `QUEUED` → `PUBLISHING` → `RECOVERED` drain with
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
