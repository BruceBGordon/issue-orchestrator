"""Tech-lead attention sweep for terminally-stuck issues (#6823).

These tests drive the real ``stuck_sweep`` policy owner and its ``fact_gatherer``
wiring through fake ports (a recording repository host and a real
``LabelManager``), covering: the pure timer/config due-gate (zero GitHub calls
when not due), the scan/injection that re-injects a stuck issue as a
``timed_out`` recovered failure, the done guard, dedup/ownership, the bounded
recovery counter + exhaustion escalation, machinery/human exclusions, config
parsing, and the end-to-end wiring into the reactive-tech-lead reaction model.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.stuck_sweep import (
    run_stuck_sweep,
    stuck_sweep_due,
)
from issue_orchestrator.control.tech_lead_reaction import TechLeadReactionPolicy
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_sections import parse_tech_lead_config
from issue_orchestrator.ports.event_sink import InMemoryEventSink


def _config(
    *,
    enabled: bool = True,
    interval_minutes: int = 15,
    max_recovery_attempts: int = 3,
    tech_lead_agent: str | None = "agent:tech-lead",
    on_failure: bool = True,
    filter_label: str | None = None,
) -> Config:
    config = Config()
    config.tech_lead_review_agent = tech_lead_agent
    config.tech_lead_review_on_failure = on_failure
    config.filtering.label = filter_label
    config.tech_lead.stuck_sweep.enabled = enabled
    config.tech_lead.stuck_sweep.interval_minutes = interval_minutes
    config.tech_lead.stuck_sweep.max_recovery_attempts = max_recovery_attempts
    return config


class _RecordingHost:
    """RepositoryHost fake honoring GitHub's label AND-filter and state."""

    def __init__(self, issues) -> None:
        self._issues = list(issues)
        self.calls: list[dict] = []

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.calls.append(
            {
                "labels": list(labels or []),
                "state": state,
                "limit": limit,
                "exhaustive": kwargs.get("exhaustive", False),
            }
        )
        wanted = {name.casefold() for name in (labels or [])}
        matched = [
            issue
            for issue in self._issues
            if state == "all" or issue.state == state
            if wanted <= {name.casefold() for name in issue.labels}
        ]
        return matched[:limit]

    def get_prs_with_label(self, *args, **kwargs):
        return []


def _issue(number: int, *, labels, state: str = "open", title: str | None = None):
    return Issue(
        number=number,
        title=title or f"Issue {number}",
        labels=list(labels),
        state=state,
        repo="test/repo",
        body="body",
        milestone="M1",
    )


# ---------------------------------------------------------------------------
# Due gate (pure state/config math, zero GitHub calls)
# ---------------------------------------------------------------------------


def test_due_false_when_disabled():
    config = _config(enabled=False)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_due_false_when_not_yet_elapsed():
    config = _config(interval_minutes=15)
    state = OrchestratorState()
    state.last_stuck_sweep_at = 10_000.0
    # 10 minutes later — under the 15-minute interval.
    assert stuck_sweep_due(config, state, now=10_000.0 + 600) is False


def test_due_true_when_elapsed():
    config = _config(interval_minutes=15)
    state = OrchestratorState()
    state.last_stuck_sweep_at = 10_000.0
    assert stuck_sweep_due(config, state, now=10_000.0 + 15 * 60) is True


def test_due_false_without_tech_lead_agent():
    config = _config(tech_lead_agent=None)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_due_false_without_tech_lead_on_failure():
    config = _config(on_failure=False)
    assert stuck_sweep_due(config, OrchestratorState(), now=10_000.0) is False


def test_not_due_sweep_makes_zero_github_calls():
    """A configured-but-not-due sweep must not touch the network."""
    config = _config(interval_minutes=60)
    state = OrchestratorState()
    # create_snapshot uses real time.time(); a just-now sweep is not yet due.
    state.last_stuck_sweep_at = time.time()
    host = _RecordingHost([_issue(1, labels=["blocked-failed"])])
    gatherer = FactGatherer(config=config, repository_host=host)
    gatherer.create_snapshot(state, issues=[])
    assert host.calls == []
    assert state.discovered_failures == []


# ---------------------------------------------------------------------------
# Scan + injection
# ---------------------------------------------------------------------------


def test_scan_injects_recovered_failure_as_timed_out():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(7, labels=["agent:web", "blocked-failed"])])
    result = run_stuck_sweep(
        config, state, host, LabelManager(config), now=50_000.0
    )
    assert len(result.recovered) == 1
    failure = result.recovered[0]
    # CRITICAL: timed_out (never blocked) so the reaction model INVESTIGATES.
    assert failure.failure_reason == "timed_out"
    # The real terminal label rides along for context.
    assert failure.blocking_label == "blocked-failed"
    assert failure.issue_number == 7
    assert failure.observed_at == 50_000.0
    # F1 (#6824): first detection records an OUTSTANDING recovery (0 failed
    # cycles yet) — injection alone never spends the budget.
    assert state.recovery_attempts == {7: 0}
    assert result.exhausted == ()


def test_scan_requests_exhaustive_to_avoid_starvation():
    # F3 (#6823): the recovery scan must be exhaustive. A fixed non-exhaustive page
    # returns the same newest N every cadence, so any eligible issue beyond page N
    # is starved forever. Exhaustive fails loud on truncation (#6779 R17) instead
    # of silently dropping older stuck issues.
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(7, labels=["agent:web", "blocked-failed"])])
    run_stuck_sweep(config, state, host, LabelManager(config), now=50_000.0)
    assert host.calls and host.calls[0]["exhaustive"] is True


def test_scan_prefers_blocked_failed_label_when_multiple():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost(
        [_issue(8, labels=["blocked", "blocked-failed", "blocked-cross-milestone"])]
    )
    result = run_stuck_sweep(
        config, state, host, LabelManager(config), now=1.0
    )
    assert result.recovered[0].blocking_label == "blocked-failed"


def test_scan_scopes_query_to_filter_label():
    config = _config(filter_label="agent:web")
    state = OrchestratorState()
    host = _RecordingHost([_issue(9, labels=["agent:web", "blocked"])])
    run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert host.calls[0]["labels"] == ["agent:web"]
    assert host.calls[0]["state"] == "open"


# ---------------------------------------------------------------------------
# Done guard
# ---------------------------------------------------------------------------


def test_done_guard_skips_closed_issue():
    config = _config()
    state = OrchestratorState()
    # Closed issue that still (stale) carries a blocking label.
    host = _RecordingHost(
        [_issue(1, labels=["blocked-failed"], state="closed")]
    )
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()
    assert state.recovery_attempts == {}


def test_done_guard_skips_issue_without_blocking_label():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(1, labels=["agent:web", "in-progress"])])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()


def test_excludes_machinery_and_reconciler_owned_labels():
    # F2 (#6824): only proposed-tech-lead/tech-lead-observation are BLANKET machinery
    # exclusions. provider-unavailable is owned while its circuit is open (the
    # default None predicate treats it as owned), and the tech-lead-needs-human
    # MARKER is reconciler-owned — both skipped, but not permanently blind.
    config = _config()
    state = OrchestratorState()
    labels = LabelManager(config)
    host = _RecordingHost(
        [
            _issue(2, labels=[labels.tech_lead_needs_human]),   # marker: reconciler owns
            _issue(3, labels=[labels.provider_unavailable]),  # circuit owns (default)
            _issue(4, labels=["proposed-tech-lead"]),            # machinery
            _issue(5, labels=["tech-lead-observation"]),         # machinery
        ]
    )
    result = run_stuck_sweep(config, state, host, labels, now=1.0)
    assert result.recovered == ()
    assert state.recovery_attempts == {}


def test_bare_needs_human_is_eligible_for_reexamination():
    # F2 (#6824): a BARE needs-human (operator escalation, no tech_lead marker) is
    # eligible — re-injecting it is how a superseding investigation is created.
    config = _config()
    state = OrchestratorState()
    labels = LabelManager(config)
    host = _RecordingHost([_issue(1, labels=[labels.needs_human])])
    result = run_stuck_sweep(config, state, host, labels, now=1.0)
    assert {f.issue_number for f in result.recovered} == {1}


def test_provider_unavailable_eligible_when_circuit_closed():
    # F2 (#6824): once the provider circuit CLOSES, an orphaned provider-unavailable
    # issue (label never cleared because it fell out of active work) is re-examined.
    config = _config()
    state = OrchestratorState()
    labels = LabelManager(config)
    host = _RecordingHost([_issue(3, labels=[labels.provider_unavailable])])
    result = run_stuck_sweep(
        config, state, host, labels, now=1.0,
        provider_circuit_open=lambda _issue: False,  # circuit closed -> orphaned
    )
    assert {f.issue_number for f in result.recovered} == {3}


def test_provider_unavailable_skipped_while_circuit_open():
    # F2 (#6824): while the circuit is OPEN the resilience manager owns the issue.
    config = _config()
    state = OrchestratorState()
    labels = LabelManager(config)
    host = _RecordingHost([_issue(3, labels=[labels.provider_unavailable])])
    result = run_stuck_sweep(
        config, state, host, labels, now=1.0,
        provider_circuit_open=lambda _issue: True,  # circuit open -> reconciler owns
    )
    assert result.recovered == ()
    assert state.recovery_attempts == {}


def test_open_proposal_target_is_not_reinjected_or_charged():
    # F1 (#6824): an issue with an OPEN gated proposal (propose mode) is owned by
    # the human who must delabel it — the sweep must not re-investigate it or
    # spend its budget (the propose-mode budget-burn the review flagged).
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([_issue(42, labels=["blocked-failed"])])
    result = run_stuck_sweep(
        config, state, host, LabelManager(config), now=1.0,
        open_proposal_targets=frozenset({42}),
    )
    assert result.recovered == ()
    assert state.recovery_attempts == {}


# ---------------------------------------------------------------------------
# Dedup / ownership
# ---------------------------------------------------------------------------


def test_dedup_skips_active_pending_cohort_and_discovered():
    config = _config()
    state = OrchestratorState()
    # 11 is in an active session; 12 is a pending tech_lead review; 13 is a cohort
    # member of a pending storm review; 14 was discovered this tick.
    state.active_sessions = [SimpleNamespace(issue=SimpleNamespace(number=11))]
    state.pending_tech_lead_reviews = [
        SimpleNamespace(issue_number=12, problem_cohort=()),
        SimpleNamespace(
            issue_number=99,
            problem_cohort=(SimpleNamespace(issue_number=13),),
        ),
    ]
    state.discovered_failures = [SimpleNamespace(issue_number=14)]
    host = _RecordingHost(
        [
            _issue(11, labels=["blocked-failed"]),
            _issue(12, labels=["blocked-failed"]),
            _issue(13, labels=["blocked-failed"]),
            _issue(14, labels=["blocked-failed"]),
            _issue(15, labels=["blocked-failed"]),
        ]
    )
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    recovered_numbers = {failure.issue_number for failure in result.recovered}
    assert recovered_numbers == {15}


# ---------------------------------------------------------------------------
# Bounded attempts / escalation
# ---------------------------------------------------------------------------


def test_final_failed_cycle_escalates_once_then_skips_at_ceiling():
    # F1 (#6824): escalation fires ONCE, on the failing cycle that reaches the
    # ceiling; a subsequent sweep leaves the already-escalated issue for the
    # human (no re-inject, no re-escalate — the comment is never re-posted).
    config = _config(max_recovery_attempts=3)
    labels = LabelManager(config)

    # Transition: 2 prior failures + this one == max -> escalate once.
    state = OrchestratorState()
    state.recovery_attempts = {5: 2}
    host = _RecordingHost([_issue(5, labels=["blocked-failed"])])
    result = run_stuck_sweep(config, state, host, labels, now=1.0)
    assert result.recovered == ()
    assert result.exhausted == (5,)
    assert state.recovery_attempts == {5: 3}

    # Ceiling: already escalated -> skipped entirely on the next sweep.
    result2 = run_stuck_sweep(config, state, host, labels, now=2.0)
    assert result2.recovered == ()
    assert result2.exhausted == ()
    assert state.recovery_attempts == {5: 3}  # capped, no unbounded growth


def test_failed_cycle_spends_one_unit_and_reinjects():
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()
    state.recovery_attempts = {6: 1}  # 1 prior failed cycle
    host = _RecordingHost([_issue(6, labels=["blocked-failed"])])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered[0].issue_number == 6
    assert state.recovery_attempts == {6: 2}  # a second failed cycle


def test_recovered_issue_clears_its_budget():
    # F1 (#6824): an issue that RECOVERED (no longer carries any blocking label,
    # or was closed) has its lifetime budget cleared so a later unrelated
    # incident on the same number starts fresh instead of inheriting a stale
    # (possibly already-exhausted) count.
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()
    state.recovery_attempts = {8: 2, 9: 3}
    host = _RecordingHost(
        [
            _issue(8, labels=["agent:web"]),           # recovered: no blocking label
            # 9 is absent from the scan entirely (closed) -> also cleared.
        ]
    )
    run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert state.recovery_attempts == {}


def test_owned_stuck_issue_keeps_its_budget():
    # The clear must NOT drop an issue that is still blocked but OWNED mid-recovery
    # (an open proposal): it has not recovered, so its budget is preserved.
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()
    state.recovery_attempts = {42: 1}
    host = _RecordingHost([_issue(42, labels=["blocked-failed"])])
    run_stuck_sweep(
        config, state, host, LabelManager(config), now=1.0,
        open_proposal_targets=frozenset({42}),
    )
    assert state.recovery_attempts == {42: 1}


def test_empty_scan_returns_empty_result():
    config = _config()
    state = OrchestratorState()
    host = _RecordingHost([])
    result = run_stuck_sweep(config, state, host, LabelManager(config), now=1.0)
    assert result.recovered == ()
    assert result.exhausted == ()


# ---------------------------------------------------------------------------
# fact_gatherer wiring -> reactive-tech-lead reaction
# ---------------------------------------------------------------------------


def test_due_sweep_populates_snapshot_and_reaction_investigates():
    config = _config()
    state = OrchestratorState()  # last_stuck_sweep_at defaults to 0.0 -> due
    host = _RecordingHost([_issue(21, labels=["agent:web", "blocked-failed"])])
    events = InMemoryEventSink()
    gatherer = FactGatherer(config=config, repository_host=host, events=events)

    snapshot = gatherer.create_snapshot(state, issues=[])

    # The recovered failure is in THIS tick's snapshot (reorder guarantee).
    numbers = {failure.issue_number for failure in snapshot.discovered_failures}
    assert 21 in numbers
    assert state.last_stuck_sweep_at > 0.0

    # The reaction model classifies the timed_out failure as INVESTIGATE.
    reaction = TechLeadReactionPolicy(
        config=config,
        labels=LabelManager(config),
        dependency_evaluator=None,
        clock=lambda: 1.0,
    ).assess(snapshot)
    assert 21 in {p.issue_number for p in reaction.investigations}

    # An observation event was emitted for the sweep.
    names = {event.name for event in events.events}
    assert "tech_lead.stuck_sweep" in names


def test_exhausted_issue_flows_to_snapshot_for_planner_escalation():
    # F1 (#6824): an exhausted issue is routed to the planner for an authoritative
    # needs-human escalation via state.stuck_sweep_escalations -> the snapshot.
    config = _config(max_recovery_attempts=3)
    state = OrchestratorState()  # due (last_stuck_sweep_at == 0.0)
    state.recovery_attempts = {50: 2}  # one failed cycle short of the ceiling
    host = _RecordingHost([_issue(50, labels=["agent:web", "blocked-failed"])])
    gatherer = FactGatherer(config=config, repository_host=host)

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert 50 in state.stuck_sweep_escalations
    assert 50 in snapshot.stuck_sweep_escalations
    # Not re-injected for investigation once exhausted.
    assert 50 not in {f.issue_number for f in snapshot.discovered_failures}


class _InMemoryQueueCache:
    """Minimal QueueCacheStore for the durable stuck-sweep meta (#6824 R1)."""

    def __init__(self) -> None:
        self._at = 0.0
        self._attempts: dict[int, int] = {}
        self._pending: set[int] = set()

    def load_last_stuck_sweep_at(self) -> float:
        return self._at

    def save_last_stuck_sweep_at(self, value: float) -> None:
        self._at = value

    def load_recovery_attempts(self) -> dict[int, int]:
        return dict(self._attempts)

    def save_recovery_attempts(self, value: dict[int, int]) -> None:
        self._attempts = dict(value)

    def load_pending_escalations(self) -> set[int]:
        return set(self._pending)

    def save_pending_escalations(self, value: set[int]) -> None:
        self._pending = set(value)


def test_escalation_persists_until_needs_human_observed():
    # R1 (#6824): an exhausted issue's escalation stays in the durable pending set
    # until its needs-human label is observed — surviving apply failures / crashes
    # in between (no repeat comment, just an idempotent label retry).
    config = _config(max_recovery_attempts=3)
    labels = LabelManager(config)
    state = OrchestratorState()
    state.recovery_attempts = {50: 2}
    host = _RecordingHost([_issue(50, labels=["blocked-failed"])])

    r1 = run_stuck_sweep(config, state, host, labels, now=1.0)
    assert r1.exhausted == (50,)
    assert state.pending_stuck_sweep_escalations == {50}

    # Next sweep: the label never landed (apply failed) — still blocked, no
    # needs-human. The escalation is RETAINED for retry, and NOT re-counted as
    # newly exhausted (so the comment is not re-posted).
    r2 = run_stuck_sweep(config, state, host, labels, now=2.0)
    assert r2.exhausted == ()
    assert state.pending_stuck_sweep_escalations == {50}


def test_escalation_acknowledged_when_needs_human_present():
    # R1 (#6824): once needs-human is observed on the issue, the escalation is
    # acknowledged and drops out of the durable pending set (stops retrying).
    config = _config(max_recovery_attempts=3)
    labels = LabelManager(config)
    state = OrchestratorState()
    state.recovery_attempts = {50: 3}
    state.pending_stuck_sweep_escalations = {50}
    host = _RecordingHost(
        [_issue(50, labels=["blocked-failed", labels.needs_human])]
    )

    run_stuck_sweep(config, state, host, labels, now=1.0)

    assert state.pending_stuck_sweep_escalations == set()


def test_pending_escalation_dropped_when_issue_recovers():
    # R1 (#6824): a recovered issue (no longer blocked) supersedes its pending
    # escalation — the durable set does not accumulate resolved issues.
    config = _config(max_recovery_attempts=3)
    labels = LabelManager(config)
    state = OrchestratorState()
    state.pending_stuck_sweep_escalations = {50}
    host = _RecordingHost([_issue(50, labels=["agent:web"])])  # no blocking label

    run_stuck_sweep(config, state, host, labels, now=1.0)

    assert state.pending_stuck_sweep_escalations == set()


def test_pending_escalations_persist_and_rehydrate_across_restart():
    # R1 (#6824): the pending-escalation set survives a restart via the store, so
    # an unacknowledged escalation is re-planned after a crash.
    from issue_orchestrator.control.stuck_sweep import (
        hydrate_stuck_sweep_state,
        persist_stuck_sweep_state,
    )

    store = _InMemoryQueueCache()
    state = OrchestratorState()
    state.pending_stuck_sweep_escalations = {50, 51}
    persist_stuck_sweep_state(state, store)

    restarted = OrchestratorState()
    hydrate_stuck_sweep_state(restarted, store)
    assert restarted.pending_stuck_sweep_escalations == {50, 51}


def test_disabled_sweep_injects_nothing_via_fact_gatherer():
    config = _config(enabled=False)
    state = OrchestratorState()
    host = _RecordingHost([_issue(30, labels=["blocked-failed"])])
    gatherer = FactGatherer(config=config, repository_host=host)
    gatherer.create_snapshot(state, issues=[])
    assert state.discovered_failures == []
    assert host.calls == []


# ---------------------------------------------------------------------------
# Config parse / defaults
# ---------------------------------------------------------------------------


def test_stuck_sweep_config_defaults():
    tech_lead = parse_tech_lead_config({})
    assert tech_lead.stuck_sweep.enabled is False
    # 4h backstop cadence by default (a broad exhaustive scan for a rare-miss
    # reconcile, not a hot path).
    assert tech_lead.stuck_sweep.interval_minutes == 240
    assert tech_lead.stuck_sweep.max_recovery_attempts == 3


def test_config_rejects_zero_interval_minutes():
    # F4 (#6823): interval_minutes: 0 makes stuck_sweep_due true EVERY tick — an
    # unthrottled GitHub scan on every loop. A zero cadence is meaningless; reject
    # it at config validation (disable via enabled: false instead).
    for enabled in (True, False):
        cfg = parse_tech_lead_config(
            {"stuck_sweep": {"enabled": enabled, "interval_minutes": 0}}
        ).stuck_sweep
        assert any("interval_minutes" in e for e in cfg.startup_errors()), (
            f"interval 0 must be rejected (enabled={enabled})"
        )
    # A positive interval validates.
    ok = parse_tech_lead_config(
        {"stuck_sweep": {"enabled": True, "interval_minutes": 1}}
    ).stuck_sweep
    assert ok.startup_errors() == []


def test_due_false_for_zero_interval_even_if_elapsed():
    # Defense-in-depth for F4: a 0-interval config that bypassed validation must
    # NOT be treated as always-due (which would every-tick scan).
    config = _config(enabled=True, interval_minutes=0)
    state = OrchestratorState()
    state.last_stuck_sweep_at = 0.0
    assert stuck_sweep_due(config, state, now=1_000_000.0) is False


def test_stuck_sweep_config_parsed_from_yaml_dict():
    tech_lead = parse_tech_lead_config(
        {
            "stuck_sweep": {
                "enabled": True,
                "interval_minutes": 30,
                "max_recovery_attempts": 5,
            }
        }
    )
    assert tech_lead.stuck_sweep.enabled is True
    assert tech_lead.stuck_sweep.interval_minutes == 30
    assert tech_lead.stuck_sweep.max_recovery_attempts == 5
    assert tech_lead.to_event_dict()["stuck_sweep"] == {
        "enabled": True,
        "interval_minutes": 30,
        "max_recovery_attempts": 5,
    }
