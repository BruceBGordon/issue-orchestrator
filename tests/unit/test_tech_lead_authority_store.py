"""Contract tests for the SQLite tech_lead launch-authority adapter (#6761 rr F1)."""

import sqlite3
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import DiscoveredFailure
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_findings import (
    PatternClassificationConflictError,
    PendingCaseFile,
    PendingPromotion,
)
from issue_orchestrator.domain.tech_lead_session import (
    StoredTechLeadOp,
    TechLeadLaunchAuthority,
    TechLeadSessionGeneration,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
    TechLeadAuthorityConflictError,
    TechLeadOpConflictError,
    TechLeadPatternConflictError,
    TechLeadPendingIntentConflictError,
    TechLeadShippedFixConflictError,
    TechLeadStormCohortConflictError,
)
from issue_orchestrator.infra.tech_lead_authority_store import (
    SqliteTechLeadAuthorityStore,
)


def _batch(prs: tuple[int, ...] = (101, 102)) -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=prs,
    )


def test_round_trip_keyed_by_run_identity(tmp_path: Path) -> None:
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch())

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded == _batch()
    # Other runs of the same session (and other sessions) see nothing.
    assert store.load(run_id="r2", session_name="issue-7") is None
    assert store.load(run_id="r1", session_name="issue-8") is None


@pytest.mark.parametrize(
    "make_store",
    [
        lambda tmp_path: SqliteTechLeadAuthorityStore.for_repo(tmp_path),
        lambda _tmp_path: InMemoryTechLeadAuthorityStore(),
    ],
)
def test_record_identical_payload_is_noop(tmp_path: Path, make_store) -> None:
    """Create-once: re-recording the same payload is silently accepted."""
    store = make_store(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded is not None
    assert loaded.manifest_pr_numbers == (1,)


@pytest.mark.parametrize(
    "make_store",
    [
        lambda tmp_path: SqliteTechLeadAuthorityStore.for_repo(tmp_path),
        lambda _tmp_path: InMemoryTechLeadAuthorityStore(),
    ],
)
def test_record_conflicting_payload_fails_loudly(tmp_path: Path, make_store) -> None:
    """The authority constrains mutation scope: it must never silently
    change or expand for an existing (run_id, session_name) (#6769 r4)."""
    store = make_store(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))

    with pytest.raises(TechLeadAuthorityConflictError):
        store.record(run_id="r1", session_name="issue-7", authority=_batch((2, 3)))

    loaded = store.load(run_id="r1", session_name="issue-7")
    assert loaded is not None
    assert loaded.manifest_pr_numbers == (1,)


def test_store_lives_in_orchestrator_state_dir(tmp_path: Path) -> None:
    """The record must live OUTSIDE any agent-writable worktree."""
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )
    assert (state_dir(tmp_path) / "tech_lead_authority.sqlite").exists()


def test_survives_reopen(tmp_path: Path) -> None:
    """A restart constructs a fresh handle over the same durable file."""
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert reopened.load(run_id="r1", session_name="issue-7") == _batch()


def test_investigation_authority_requires_focus() -> None:
    with pytest.raises(ValueError, match="focus_issue_number"):
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=7,
        )


def test_health_review_authority_rejects_focus_and_manifest() -> None:
    """Health-review scope is the anchor only (ADR-0031 §4) — a launch that
    records a focus issue or manifest PRs for it is a producer bug."""
    with pytest.raises(ValueError, match="health_review"):
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=7,
            focus_issue_number=7,
        )
    with pytest.raises(ValueError, match="health_review"):
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=7,
            manifest_pr_numbers=(101,),
        )


def test_allowed_targets_by_flavor() -> None:
    investigation = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=7,
        focus_issue_number=7,
    )
    assert investigation.allowed_targets() == frozenset({7})
    assert _batch().allowed_targets() == frozenset({7, 101, 102})


def test_allowed_act_level_targets_are_issue_only() -> None:
    """Act-level (reset_retry/kill_hung_session) scope is the STRICTER
    issue-only set (#6764 re-review F1, #6780): an investigation owns its focus;
    a health review owns its immutable launch-granted problem cohort; a batch owns no
    resettable issue. Tech Lead anchors and manifest PRs are never act targets."""
    investigation = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=7,
        focus_issue_number=7,
    )
    assert investigation.allowed_act_level_targets() == frozenset({7})
    # Batch manifest PRs are addressable for comments but NOT for act-level work.
    assert _batch().allowed_act_level_targets() == frozenset()
    assert 101 not in _batch().allowed_act_level_targets()
    health = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        anchor_issue_number=9,
        problem_issue_numbers=(12, 14),
    )
    assert health.allowed_act_level_targets() == frozenset({12, 14})
    assert 9 not in health.allowed_act_level_targets()


def test_observed_session_generation_round_trips_and_resolves_unambiguously() -> None:
    generation = TechLeadSessionGeneration(
        issue_number=14,
        task_kind=TaskKind.CODE,
        terminal_id="issue-14",
        run_id="RUN-14",
    )
    authority = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        anchor_issue_number=9,
        problem_issue_numbers=(14,),
        observed_session_generations=(generation,),
    )

    restored = TechLeadLaunchAuthority.from_dict(authority.to_dict())

    assert restored == authority
    assert restored.observed_kill_target(14) == generation
    assert restored.observed_kill_target(99) is None

    ambiguous = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        anchor_issue_number=9,
        problem_issue_numbers=(14,),
        observed_session_generations=(
            generation,
            TechLeadSessionGeneration(
                issue_number=14,
                task_kind=TaskKind.REWORK,
                terminal_id="rework-14",
                run_id="RUN-R",
            ),
        ),
    )
    assert ambiguous.observed_kill_target(14) is None


def test_health_problem_cohort_round_trips_and_is_validated() -> None:
    health = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        anchor_issue_number=9,
        problem_issue_numbers=(12, 14),
    )

    assert TechLeadLaunchAuthority.from_dict(health.to_dict()) == health
    with pytest.raises(ValueError, match="sorted and unique"):
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            anchor_issue_number=9,
            problem_issue_numbers=(14, 12, 12),
        )
    with pytest.raises(ValueError, match="only for a health review"):
        TechLeadLaunchAuthority(
            flavor=TechLeadSessionFlavor.BATCH_REVIEW,
            anchor_issue_number=9,
            problem_issue_numbers=(12,),
        )


def test_discard_removes_only_the_named_run(tmp_path: Path) -> None:
    """Retention (#6769 F3): discard drops one run's row and nothing else."""
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch())
    store.record(run_id="r2", session_name="issue-7", authority=_batch((5,)))

    store.discard(run_id="r1", session_name="issue-7")

    assert store.load(run_id="r1", session_name="issue-7") is None
    assert store.load(run_id="r2", session_name="issue-7") == _batch((5,))


def test_discard_is_a_noop_when_absent(tmp_path: Path) -> None:
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.discard(run_id="never-recorded", session_name="issue-7")
    assert store.load(run_id="never-recorded", session_name="issue-7") is None


def test_sqlite_adapter_satisfies_the_port() -> None:
    """The adapter must implement every method the port declares."""
    from issue_orchestrator.ports.tech_lead_authority import (
        TechLeadAuthorityStore as TechLeadAuthorityStorePort,
    )

    for method in ("record", "load", "discard"):
        assert callable(getattr(SqliteTechLeadAuthorityStore, method))
        assert callable(getattr(TechLeadAuthorityStorePort, method))


def test_health_review_authority_targets_only_its_anchor() -> None:
    """HEALTH_REVIEW scope: targeted proposals may address only the anchor."""
    health = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        anchor_issue_number=9,
    )
    assert health.allowed_targets() == frozenset({9})


# --- Gated proposal ops (#6778) ------------------------------------------


def _op(
    target: int = 13, *, op_type: str = "reset_retry", rationale: str = "r"
) -> "StoredTechLeadOp":
    return StoredTechLeadOp(
        op_type=op_type,
        target_issue_number=target,
        rationale=rationale,
        source_run_id="run-1",
        source_session_name="issue-7",
        source_action_id="A2",
        created_at="2026-07-11T00:00:00+00:00",
    )


OP_STORES = [
    lambda tmp_path: SqliteTechLeadAuthorityStore.for_repo(tmp_path),
    lambda _tmp_path: InMemoryTechLeadAuthorityStore(),
]


@pytest.mark.parametrize("make_store", OP_STORES)
def test_op_round_trip(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_op(issue_number=500, op=_op())

    assert store.load_op(issue_number=500) == _op()
    assert store.load_op(issue_number=501) is None
    assert store.list_ops() == ((500, _op()),)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_op_identical_payload_is_noop(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_op(issue_number=500, op=_op())
    store.record_op(issue_number=500, op=_op())

    assert store.load_op(issue_number=500) == _op()


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_op_conflicting_payload_fails_loudly(tmp_path: Path, make_store) -> None:
    """The approver's consent binds to exactly one recorded payload; it must
    never silently change after the proposal issue exists (#6778)."""
    store = make_store(tmp_path)
    store.record_op(issue_number=500, op=_op(13))

    with pytest.raises(TechLeadOpConflictError):
        store.record_op(issue_number=500, op=_op(14))

    loaded = store.load_op(issue_number=500)
    assert loaded is not None and loaded.target_issue_number == 13


@pytest.mark.parametrize("make_store", OP_STORES)
def test_discard_op_removes_only_the_named_issue(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_op(issue_number=500, op=_op(13))
    store.record_op(issue_number=501, op=_op(14, op_type="kill_hung_session"))

    store.discard_op(issue_number=500)

    assert store.load_op(issue_number=500) is None
    assert store.load_op(issue_number=501) is not None
    assert [n for n, _ in store.list_ops()] == [501]
    # No-op when absent (once-only owner).
    store.discard_op(issue_number=500)


def test_op_survives_reopen(tmp_path: Path) -> None:
    """Crash-safety: an unexecuted op outlives the recording process."""
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record_op(
        issue_number=500, op=_op()
    )

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert reopened.load_op(issue_number=500) == _op()


def test_stored_op_rejects_unknown_op_type() -> None:
    with pytest.raises(ValueError, match="op_type"):
        _op(op_type="merge_pr")


def test_stored_op_rejects_blank_source_identity() -> None:
    with pytest.raises(ValueError, match="source_run_id"):
        StoredTechLeadOp(
            op_type="reset_retry",
            target_issue_number=13,
            rationale="r",
            source_run_id=" ",
            source_session_name="issue-7",
            source_action_id="A2",
            created_at="2026-07-11T00:00:00+00:00",
        )


def test_stored_op_dict_round_trip() -> None:
    op = _op(42, op_type="kill_hung_session", rationale="hung 90m")
    assert StoredTechLeadOp.from_dict(op.to_dict()) == op


# --- Pattern case-file ledger (#6781) ------------------------------------


@pytest.mark.parametrize("make_store", OP_STORES)
def test_pattern_round_trip(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout",
        issue_number=600,
        observation_id="run-1:sess:A1",
        diagnosis="Mechanism: leaked DB connection. Suggested fix: close it.",
    )

    assert store.lookup_pattern(signature="db-timeout") == 600
    assert store.lookup_pattern(signature="absent") is None
    assert store.list_patterns() == (("db-timeout", 600),)
    [evidence] = store.list_pattern_evidence()
    assert evidence.diagnosis == (
        "Mechanism: leaked DB connection. Suggested fix: close it."
    )


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_pattern_identical_issue_is_noop(tmp_path: Path, make_store) -> None:
    """Create-once: re-recording the SAME case-file issue for a signature is
    silently accepted — the case file IS the accumulating artifact (#6781)."""
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout",
        issue_number=600,
        observation_id="run-1:sess:db-timeout",
    )
    store.record_pattern(
        signature="db-timeout",
        issue_number=600,
        observation_id="run-1:sess:db-timeout",
    )

    assert store.lookup_pattern(signature="db-timeout") == 600
    assert store.list_patterns() == (("db-timeout", 600),)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_pattern_conflicting_issue_fails_loudly(
    tmp_path: Path, make_store
) -> None:
    """A signature keys exactly one evidence trail; it must never silently
    move to a different case-file issue (#6781)."""
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout",
        issue_number=600,
        observation_id="run-1:sess:db-timeout",
    )

    with pytest.raises(TechLeadPatternConflictError):
        store.record_pattern(
            signature="db-timeout",
            issue_number=601,
            observation_id="run-1:sess:db-timeout",
        )

    assert store.lookup_pattern(signature="db-timeout") == 600


@pytest.mark.parametrize("make_store", OP_STORES)
def test_list_patterns_is_signature_sorted(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="zeta",
        issue_number=3,
        observation_id="run-1:sess:zeta",
    )
    store.record_pattern(
        signature="alpha",
        issue_number=1,
        observation_id="run-1:sess:alpha",
    )
    store.record_pattern(
        signature="mu",
        issue_number=2,
        observation_id="run-1:sess:mu",
    )

    assert store.list_patterns() == (("alpha", 1), ("mu", 2), ("zeta", 3))


def test_pattern_survives_reopen(tmp_path: Path) -> None:
    """The evidence-trail ledger outlives the recording process (#6781)."""
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record_pattern(
        signature="db-timeout", issue_number=600, observation_id="run-1:sess:A1"
    )

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert reopened.lookup_pattern(signature="db-timeout") == 600


# --- Observation identity: create-once counting (#6957 review F1) ---------


@pytest.mark.parametrize("make_store", OP_STORES)
def test_distinct_observations_each_advance_the_count(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="r1:s:A1"
    )

    assert store.note_pattern_observation(
        signature="db-timeout", observation_id="r2:s:A1"
    )
    assert store.note_pattern_observation(
        signature="db-timeout", observation_id="r2:s:A2"
    )

    [evidence] = store.list_pattern_evidence()
    assert evidence.observation_count == 3


@pytest.mark.parametrize("make_store", OP_STORES)
def test_replaying_one_observation_never_counts_it_twice(
    tmp_path: Path, make_store
) -> None:
    """The #6957 review F1 defect: a blind increment inflated min_evidence.

    Replaying a completed decision action after a crash reproduces the same
    observation identity, so the count must not move — otherwise a two-
    observation action could reach count 4 after one retry and promote a
    signature that never had distinct evidence.
    """
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="r1:s:A1"
    )

    assert store.note_pattern_observation(
        signature="db-timeout", observation_id="r2:s:A1"
    )
    assert not store.note_pattern_observation(
        signature="db-timeout", observation_id="r2:s:A1"
    )
    # ...including the observation the case-file BODY already recorded.
    assert not store.note_pattern_observation(
        signature="db-timeout", observation_id="r1:s:A1"
    )

    [evidence] = store.list_pattern_evidence()
    assert evidence.observation_count == 2


@pytest.mark.parametrize("make_store", OP_STORES)
def test_has_pattern_observation_reports_what_is_recorded(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout", issue_number=600, observation_id="r1:s:A1"
    )

    assert store.has_pattern_observation(
        signature="db-timeout", observation_id="r1:s:A1"
    )
    assert not store.has_pattern_observation(
        signature="db-timeout", observation_id="r2:s:A1"
    )
    assert not store.has_pattern_observation(
        signature="absent", observation_id="r1:s:A1"
    )


@pytest.mark.parametrize("make_store", OP_STORES)
def test_observation_identity_is_required(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.record_pattern(signature="s", issue_number=1, observation_id="  ")
    store.record_pattern(signature="s", issue_number=1, observation_id="r1:s:A1")
    with pytest.raises(ValueError):
        store.note_pattern_observation(signature="s", observation_id="")


def test_observation_identities_survive_reopen(tmp_path: Path) -> None:
    """Replay safety must outlive the process, or a restart re-counts (#6957 F1)."""
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.record_pattern(signature="s", issue_number=1, observation_id="r1:s:A1")
    store.note_pattern_observation(signature="s", observation_id="r2:s:A1")

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert not reopened.note_pattern_observation(
        signature="s", observation_id="r2:s:A1"
    )
    [evidence] = reopened.list_pattern_evidence()
    assert evidence.observation_count == 2


def test_legacy_pattern_rows_keep_their_count_and_accept_new_observations(
    tmp_path: Path,
) -> None:
    """Migration: a pre-#6957 row has a count but no observation identities."""
    db = state_dir(tmp_path) / "tech_lead_authority.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(db)
    legacy.execute(
        "CREATE TABLE tech_lead_patterns (signature TEXT PRIMARY KEY,"
        " issue_number INTEGER NOT NULL, recorded_at TEXT NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO tech_lead_patterns VALUES ('legacy', 42, '2026-01-01T00:00:00Z')"
    )
    legacy.commit()
    legacy.close()

    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    [before] = store.list_pattern_evidence()
    assert before.observation_count == 1

    assert store.note_pattern_observation(signature="legacy", observation_id="r1:s:A1")

    [after] = store.list_pattern_evidence()
    assert after.observation_count == 2


# --- Classification immutability (#6957 review F3) -------------------------


@pytest.mark.parametrize("make_store", OP_STORES)
def test_unclassified_row_is_upgraded_by_a_later_observation(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_pattern(signature="s", issue_number=1, observation_id="r1:s:A1")

    store.note_pattern_observation(
        signature="s", observation_id="r2:s:A1", fix_class="code", area="db"
    )

    [evidence] = store.list_pattern_evidence()
    assert (evidence.fix_class, evidence.area) == ("code", "db")


@pytest.mark.parametrize("make_store", OP_STORES)
def test_empty_incoming_classification_preserves_what_is_recorded(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="s",
        issue_number=1,
        observation_id="r1:s:A1",
        fix_class="code",
        area="db",
    )

    store.note_pattern_observation(signature="s", observation_id="r2:s:A1")

    [evidence] = store.list_pattern_evidence()
    assert (evidence.fix_class, evidence.area) == ("code", "db")


@pytest.mark.parametrize("make_store", OP_STORES)
@pytest.mark.parametrize(
    "recorded,incoming",
    (
        ("human", "code"),
        ("code", "human"),
    ),
)
def test_conflicting_fix_class_fails_loudly(
    tmp_path: Path, make_store, recorded: str, incoming: str
) -> None:
    """#6957 review F3: observation order must not decide promotability.

    ``human -> code`` would make a human-gated finding runnable; ``code ->
    human`` would silently retire established promotable work. Both are a
    reviewed reclassification, not a side effect of the next observation.
    """
    store = make_store(tmp_path)
    store.record_pattern(
        signature="s", issue_number=1, observation_id="r1:s:A1", fix_class=recorded
    )

    with pytest.raises(PatternClassificationConflictError):
        store.note_pattern_observation(
            signature="s", observation_id="r2:s:A1", fix_class=incoming
        )

    [evidence] = store.list_pattern_evidence()
    assert evidence.fix_class == recorded
    # The rejected observation was not counted either.
    assert evidence.observation_count == 1


@pytest.mark.parametrize("make_store", OP_STORES)
def test_conflicting_area_fails_loudly(tmp_path: Path, make_store) -> None:
    """Area decides which repository a promotion routes to (#6957 review F3)."""
    store = make_store(tmp_path)
    store.record_pattern(
        signature="s", issue_number=1, observation_id="r1:s:A1", area="db"
    )

    with pytest.raises(PatternClassificationConflictError):
        store.note_pattern_observation(
            signature="s", observation_id="r2:s:A1", area="ui"
        )

    [evidence] = store.list_pattern_evidence()
    assert evidence.area == "db"


@pytest.mark.parametrize("make_store", OP_STORES)
def test_identical_classification_is_idempotent(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="s",
        issue_number=1,
        observation_id="r1:s:A1",
        fix_class="code",
        area="DB",
    )

    store.note_pattern_observation(
        signature="s", observation_id="r2:s:A1", fix_class="code", area="db"
    )

    [evidence] = store.list_pattern_evidence()
    # GitHub folds label case, so an area respelled in another case is the same.
    assert (evidence.fix_class, evidence.area, evidence.observation_count) == (
        "code",
        "DB",
        2,
    )


# --- In-flight creation intents (#6957 round-3 review F10/F11) -------------


def _pending_case_file(**overrides) -> PendingCaseFile:
    fields = dict(
        signature="db-timeout",
        title="Pattern case file: db-timeout",
        idempotency_marker="<!-- marker -->",
        body_observation_id="r1:s:A1",
        fix_class="human",
        area="database",
        diagnosis="Mechanism: leaked DB connection.",
    )
    fields.update(overrides)
    return PendingCaseFile(**fields)


def _pending_promotion(**overrides) -> PendingPromotion:
    fields = dict(
        signature="db-timeout",
        case_file_issue_number=600,
        target_repo="owner/upstream",
        title="[tech-lead:repo] db-timeout",
        idempotency_marker="<!-- marker -->",
        area="database",
        body_observations=2,
    )
    fields.update(overrides)
    return PendingPromotion(**fields)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_pending_case_file_round_trip(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_pending_case_file(pending=_pending_case_file())

    assert store.load_pending_case_file(signature="db-timeout") == _pending_case_file()
    assert store.load_pending_case_file(signature="absent") is None

    store.discard_pending_case_file(signature="db-timeout")
    assert store.load_pending_case_file(signature="db-timeout") is None
    store.discard_pending_case_file(signature="db-timeout")  # idempotent


@pytest.mark.parametrize("make_store", OP_STORES)
def test_pending_case_file_is_create_once(tmp_path: Path, make_store) -> None:
    """A later command must never silently replace an earlier one's authority."""
    store = make_store(tmp_path)
    store.record_pending_case_file(pending=_pending_case_file())
    store.record_pending_case_file(pending=_pending_case_file())  # identical: no-op

    with pytest.raises(TechLeadPendingIntentConflictError):
        store.record_pending_case_file(
            pending=_pending_case_file(body_observation_id="r2:s:B1", fix_class="code")
        )

    assert store.load_pending_case_file(signature="db-timeout") == _pending_case_file()


@pytest.mark.parametrize("make_store", OP_STORES)
def test_pending_promotion_round_trip_and_create_once(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_pending_promotion(pending=_pending_promotion())
    store.record_pending_promotion(pending=_pending_promotion())

    assert store.load_pending_promotion(signature="db-timeout") == _pending_promotion()

    with pytest.raises(TechLeadPendingIntentConflictError):
        store.record_pending_promotion(pending=_pending_promotion(body_observations=3))

    store.discard_pending_promotion(signature="db-timeout")
    assert store.load_pending_promotion(signature="db-timeout") is None


def test_pending_intents_survive_reopen(tmp_path: Path) -> None:
    """They only help if they outlive the process that crashed."""
    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.record_pending_case_file(pending=_pending_case_file())
    store.record_pending_promotion(pending=_pending_promotion())

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert reopened.load_pending_case_file(signature="db-timeout") == (
        _pending_case_file()
    )
    assert reopened.load_pending_promotion(signature="db-timeout") == (
        _pending_promotion()
    )


@pytest.mark.parametrize("make_store", OP_STORES)
def test_load_pattern_evidence_reads_one_signature(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_pattern(
        signature="db-timeout",
        issue_number=600,
        observation_id="r1:s:A1",
        fix_class="code",
        area="database",
    )

    row = store.load_pattern_evidence(signature="db-timeout")

    assert row is not None
    assert (row.case_file_issue_number, row.fix_class, row.area) == (
        600,
        "code",
        "database",
    )
    assert store.load_pattern_evidence(signature="absent") is None


def test_pending_intent_methods_satisfy_the_port() -> None:
    from issue_orchestrator.ports.tech_lead_authority import (
        TechLeadAuthorityStore as TechLeadAuthorityStorePort,
    )

    for method in (
        "record_pending_case_file",
        "load_pending_case_file",
        "discard_pending_case_file",
        "record_pending_promotion",
        "load_pending_promotion",
        "discard_pending_promotion",
        "load_pattern_evidence",
    ):
        assert callable(getattr(SqliteTechLeadAuthorityStore, method))
        assert callable(getattr(InMemoryTechLeadAuthorityStore, method))
        assert callable(getattr(TechLeadAuthorityStorePort, method))


def test_pattern_methods_satisfy_the_port() -> None:
    from issue_orchestrator.ports.tech_lead_authority import (
        TechLeadAuthorityStore as TechLeadAuthorityStorePort,
    )

    for method in (
        "record_pattern",
        "lookup_pattern",
        "list_patterns",
        "note_pattern_observation",
        "has_pattern_observation",
    ):
        assert callable(getattr(SqliteTechLeadAuthorityStore, method))
        assert callable(getattr(InMemoryTechLeadAuthorityStore, method))
        assert callable(getattr(TechLeadAuthorityStorePort, method))


# --- Problem-storm cohort ledger (#6780) ---------------------------------


def _cohort(numbers: tuple[int, ...] = (41, 42)) -> tuple[DiscoveredFailure, ...]:
    return tuple(
        DiscoveredFailure(
            issue_number=number,
            issue_title=f"Problem {number}",
            failure_reason="failed",
            artifact_hints=(f"/runs/{number}/failure-diagnostic.json",),
            observed_at=1_000.0 + number,
            blocking_label="blocked-failed",
            issue_body=f"body {number}",
            issue_milestone="M1",
        )
        for number in numbers
    )


@pytest.mark.parametrize("make_store", OP_STORES)
def test_storm_cohort_round_trip_preserves_every_field(
    tmp_path: Path, make_store
) -> None:
    """The WHOLE typed fact must survive, hints included: a recovered anchor
    hands these to the board snapshot verbatim (#6780)."""
    store = make_store(tmp_path)
    store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort())

    assert store.load_storm_cohort(anchor_issue_number=999) == _cohort()
    assert store.load_storm_cohort(anchor_issue_number=1000) is None
    assert store.list_storm_cohorts() == ((999, _cohort()),)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_storm_cohort_identical_payload_is_noop(
    tmp_path: Path, make_store
) -> None:
    """Create-once: a retried intake for the same anchor is accepted."""
    store = make_store(tmp_path)
    store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort())
    store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort())

    assert store.list_storm_cohorts() == ((999, _cohort()),)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_conflicting_storm_cohort_fails_loudly(
    tmp_path: Path, make_store
) -> None:
    """The cohort is act-level authority AND artifact-retention scope; it must
    never silently change or expand after the anchor exists (#6780)."""
    store = make_store(tmp_path)
    store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort())

    with pytest.raises(TechLeadStormCohortConflictError):
        store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort((41, 42, 43)))

    assert store.load_storm_cohort(anchor_issue_number=999) == _cohort()


@pytest.mark.parametrize("make_store", OP_STORES)
def test_discard_storm_cohort_is_idempotent(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_storm_cohort(anchor_issue_number=999, cohort=_cohort())

    store.discard_storm_cohort(anchor_issue_number=999)
    store.discard_storm_cohort(anchor_issue_number=999)

    assert store.load_storm_cohort(anchor_issue_number=999) is None
    assert store.list_storm_cohorts() == ()


@pytest.mark.parametrize("make_store", OP_STORES)
def test_list_storm_cohorts_is_anchor_sorted(tmp_path: Path, make_store) -> None:
    store = make_store(tmp_path)
    store.record_storm_cohort(anchor_issue_number=30, cohort=_cohort((3,)))
    store.record_storm_cohort(anchor_issue_number=10, cohort=_cohort((1,)))
    store.record_storm_cohort(anchor_issue_number=20, cohort=_cohort((2,)))

    assert [anchor for anchor, _ in store.list_storm_cohorts()] == [10, 20, 30]


def test_storm_cohort_survives_reopen(tmp_path: Path) -> None:
    """The whole point: the cohort outlives the process that discovered it."""
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record_storm_cohort(
        anchor_issue_number=999, cohort=_cohort()
    )

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert reopened.load_storm_cohort(anchor_issue_number=999) == _cohort()


def test_storm_cohort_methods_satisfy_the_port() -> None:
    from issue_orchestrator.ports.tech_lead_authority import (
        TechLeadAuthorityStore as TechLeadAuthorityStorePort,
    )

    for method in (
        "record_storm_cohort",
        "load_storm_cohort",
        "discard_storm_cohort",
        "list_storm_cohorts",
    ):
        assert callable(getattr(SqliteTechLeadAuthorityStore, method))
        assert callable(getattr(InMemoryTechLeadAuthorityStore, method))
        assert callable(getattr(TechLeadAuthorityStorePort, method))


# --- Shipped-fix operational memory (#6781 amendment) -------------------


@pytest.mark.parametrize("make_store", OP_STORES)
def test_shipped_fix_round_trip_is_newest_first_and_bounded(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )
    store.record_shipped_fix(
        issue_number=601,
        title="Repair queue seam",
        pr_url="https://github.com/o/r/pull/701",
        area="queue",
    )

    [newest] = store.list_recent_shipped_fixes(limit=1)

    assert newest.issue_number == 601
    assert newest.title == "Repair queue seam"
    assert newest.pr_url == "https://github.com/o/r/pull/701"
    assert newest.area == "queue"
    assert newest.merged_at


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_shipped_fix_identical_evidence_is_noop(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )
    [original] = store.list_recent_shipped_fixes(limit=10)

    store.record_shipped_fix(
        issue_number=600,
        title="Renamed DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )

    # Mutable issue titles are not evidence identity; the first observed title
    # remains in the durable fact without blocking a crash-retry.
    assert store.list_recent_shipped_fixes(limit=10) == (original,)


@pytest.mark.parametrize("make_store", OP_STORES)
def test_record_shipped_fix_conflicting_evidence_fails_loudly(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)
    store.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )

    with pytest.raises(TechLeadShippedFixConflictError):
        store.record_shipped_fix(
            issue_number=600,
            title="Repair DB seam",
            pr_url="https://github.com/o/r/pull/700",
            area="queue",
        )


@pytest.mark.parametrize("make_store", OP_STORES)
def test_list_recent_shipped_fixes_rejects_nonpositive_limit(
    tmp_path: Path, make_store
) -> None:
    store = make_store(tmp_path)

    with pytest.raises(ValueError, match="positive"):
        store.list_recent_shipped_fixes(limit=0)


def test_shipped_fix_survives_reopen(tmp_path: Path) -> None:
    SqliteTechLeadAuthorityStore.for_repo(tmp_path).record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )

    reopened = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    [fix] = reopened.list_recent_shipped_fixes(limit=10)
    assert (fix.issue_number, fix.area) == (600, "db")


def test_existing_authority_database_adds_shipped_fix_ledger(tmp_path: Path) -> None:
    """Opening a pre-feature database applies the additive CREATE TABLE."""
    db_path = state_dir(tmp_path) / "tech_lead_authority.sqlite"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE tech_lead_patterns ("
            "signature TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, "
            "recorded_at TEXT NOT NULL)"
        )

    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)
    store.record_shipped_fix(
        issue_number=600,
        title="Repair DB seam",
        pr_url="https://github.com/o/r/pull/700",
        area="db",
    )

    assert store.list_recent_shipped_fixes(limit=10)[0].issue_number == 600
    assert store.list_pattern_evidence() == ()


def test_existing_pattern_ledger_adds_empty_diagnosis_column(tmp_path: Path) -> None:
    db_path = state_dir(tmp_path) / "tech_lead_authority.sqlite"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE tech_lead_patterns ("
            "signature TEXT PRIMARY KEY, issue_number INTEGER NOT NULL, "
            "recorded_at TEXT NOT NULL, observation_count INTEGER NOT NULL, "
            "fix_class TEXT NOT NULL, area TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO tech_lead_patterns VALUES "
            "('legacy', 65, 'now', 2, 'code', 'queue')"
        )

    [evidence] = SqliteTechLeadAuthorityStore.for_repo(tmp_path).list_pattern_evidence()

    assert evidence.signature == "legacy"
    assert evidence.diagnosis == ""


def test_existing_promotion_ledger_adds_reported_observation_watermark(
    tmp_path: Path,
) -> None:
    """Opening an earlier #6957 database applies the additive column migration."""
    db_path = state_dir(tmp_path) / "tech_lead_authority.sqlite"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE tech_lead_promoted_findings ("
            "signature TEXT PRIMARY KEY, case_file_issue_number INTEGER NOT NULL, "
            "target_repo TEXT NOT NULL, target_issue_number INTEGER NOT NULL, "
            "state TEXT NOT NULL, area TEXT NOT NULL DEFAULT '', "
            "title TEXT NOT NULL DEFAULT '', shipped_pr_url TEXT NOT NULL DEFAULT '', "
            "recorded_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO tech_lead_promoted_findings VALUES "
            "('sig', 65, 'o/r', 99, 'promoted', '', '', '', 'now')"
        )

    store = SqliteTechLeadAuthorityStore.for_repo(tmp_path)

    assert store.load_promotion(signature="sig").reported_observations == 0


def test_shipped_fix_methods_satisfy_the_port() -> None:
    from issue_orchestrator.ports.tech_lead_authority import (
        TechLeadAuthorityStore as TechLeadAuthorityStorePort,
    )

    for method in ("record_shipped_fix", "list_recent_shipped_fixes"):
        assert callable(getattr(SqliteTechLeadAuthorityStore, method))
        assert callable(getattr(InMemoryTechLeadAuthorityStore, method))
        assert callable(getattr(TechLeadAuthorityStorePort, method))
