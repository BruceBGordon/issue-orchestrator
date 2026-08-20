"""The stuck sweep must not spend the tick's error budget on a network blip.

Driven through ``gather_tech_lead_facts`` — the public seam that owns the
sweep — rather than the private helper, so these cover the boundary a real
tick crosses.

Regression cover for a real incident. The queue fetch runs its GitHub read under
``IssueFetchResilience``, so a transient DNS failure is absorbed there ("keeping
cached queue; will retry next cycle"). The stuck sweep issues its OWN exhaustive
``list_issues`` read, outside that guard — so the very same error, in the very
same tick, escaped into the loop-error path. Three of those in a row tripped the
breaker, and (before the half-open retry) the engine stayed paused for days.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.adapters.github.errors import (
    GitHubAuthError,
    GitHubHttpError,
    GitHubScanIncompleteError,
)
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.repository_host import (
    RepositoryHostError,
    RepositoryScanIncompleteError,
)

# The exact failure observed: a DNS drop while the host slept, surfaced as an
# App-installation-token request failure.
# Past the 240-minute default sweep interval, so the sweep is genuinely due.
# Picking a clock inside the interval makes every assertion here vacuous.
SWEEP_DUE_AT = 100_000.0

DNS_DROP = GitHubAuthError(
    "Failed to request GitHub App installation token: "
    "[Errno 8] nodename nor servname provided, or not known"
)


def _sweep_config() -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.tech_lead.stuck_sweep.enabled = True
    config.tech_lead_review_agent = "tech-lead"
    config.tech_lead_review_on_failure = True
    return config


def _gatherer(host) -> FactGatherer:
    return FactGatherer(config=_sweep_config(), repository_host=host)


class TestStuckSweepTransientFailure:
    def test_the_observed_dns_failure_is_a_repository_host_error(self) -> None:
        """The catch only helps if the real exception is actually covered by it."""
        assert isinstance(DNS_DROP, RepositoryHostError)

    def test_transient_host_failure_does_not_escape_the_sweep(self) -> None:
        """This exception used to reach the loop-error handler and trip the breaker."""
        host = MagicMock()
        host.list_issues.side_effect = DNS_DROP
        gatherer = _gatherer(host)
        state = OrchestratorState()

        # Must not raise.
        gatherer.gather_tech_lead_facts(state, now=SWEEP_DUE_AT)

    def test_failed_sweep_stays_due_so_the_next_tick_retries(self) -> None:
        """Not stamping the timer is what makes the skip a retry rather than a loss."""
        from issue_orchestrator.control.stuck_sweep import stuck_sweep_due

        host = MagicMock()
        host.list_issues.side_effect = DNS_DROP
        gatherer = _gatherer(host)
        state = OrchestratorState()
        before = state.last_stuck_sweep_at

        gatherer.gather_tech_lead_facts(state, now=SWEEP_DUE_AT)

        assert state.last_stuck_sweep_at == before
        assert stuck_sweep_due(_sweep_config(), state, SWEEP_DUE_AT) is True

    def test_a_real_bug_still_propagates(self) -> None:
        """Only repository-host failures are absorbed; genuine defects must not be."""
        host = MagicMock()
        host.list_issues.side_effect = ValueError("a real bug")
        gatherer = _gatherer(host)

        with pytest.raises(ValueError, match="a real bug"):
            gatherer.gather_tech_lead_facts(OrchestratorState(), now=SWEEP_DUE_AT)

    def test_a_sweep_that_is_not_due_never_touches_the_network(self) -> None:
        host = MagicMock()
        gatherer = _gatherer(host)
        state = OrchestratorState()
        state.last_stuck_sweep_at = SWEEP_DUE_AT

        gatherer.gather_tech_lead_facts(state, now=SWEEP_DUE_AT)

        host.list_issues.assert_not_called()


class TestCompletenessFailuresAreLoudButBounded:
    """An outage is skippable; an unprovable scan is neither skippable nor fatal.

    ``stuck_sweep`` passes ``exhaustive=True`` precisely so a truncated read
    fails loud, and swallowing that meant the sweep retried forever while never
    actually running. But propagating it out of the tick is the opposite
    mistake: the sweep is one recovery backstop, and a repo with more open
    issues than the scan cap would raise on EVERY cycle — tripping the breaker
    and stopping queue fetching, planning and applying, permanently.

    So the contract is: never silently "done", never fatal, always visible.
    """

    def _incomplete_host(self, message: str, **kw) -> MagicMock:
        host = MagicMock()
        host.list_issues.side_effect = GitHubScanIncompleteError(
            message, method="GET", url="/issues", **kw
        )
        return host

    def test_an_unprovable_scan_does_not_kill_the_tick(self) -> None:
        """It must not abort the snapshot the whole engine depends on."""
        gatherer = _gatherer(
            self._incomplete_host(
                "open issues scan exceeded the 1000-item page cap; "
                "cannot prove the list is complete"
            )
        )

        # Must not raise: every other subsystem keeps running.
        gatherer.gather_tech_lead_facts(OrchestratorState(), now=SWEEP_DUE_AT)

    def test_an_unprovable_scan_never_marks_the_sweep_done(self) -> None:
        """Not stamping the timer is what stops it counting as a clean sweep."""
        from issue_orchestrator.control.stuck_sweep import stuck_sweep_due

        gatherer = _gatherer(self._incomplete_host("cannot prove the list is complete"))
        state = OrchestratorState()
        before = state.last_stuck_sweep_at

        gatherer.gather_tech_lead_facts(state, now=SWEEP_DUE_AT)

        assert state.last_stuck_sweep_at == before
        assert stuck_sweep_due(_sweep_config(), state, SWEEP_DUE_AT) is True

    def test_an_unprovable_scan_is_surfaced_as_an_event(self) -> None:
        """A log line repeats every cadence and is easy to miss; the event is
        what a dashboard or alert can react to."""
        events = _RecordingEvents()
        gatherer = FactGatherer(
            config=_sweep_config(),
            repository_host=self._incomplete_host("cannot prove"),
            events=events,
        )

        gatherer.gather_tech_lead_facts(OrchestratorState(), now=SWEEP_DUE_AT)

        incomplete = [
            e for e in events.published if e.data.get("scan_incomplete") is True
        ]
        assert len(incomplete) == 1
        assert incomplete[0].data["recovered"] == []

    def test_completeness_error_is_a_repository_host_error(self) -> None:
        """It must stay catchable by generic handlers while defeating the skip."""
        err = GitHubScanIncompleteError("truncated")
        assert isinstance(err, RepositoryHostError)
        assert isinstance(err, RepositoryScanIncompleteError)
        # Still an HTTP error, so existing adapter-level handlers keep working.
        assert isinstance(err, GitHubHttpError)

    def test_the_outage_case_is_still_skipped_and_stays_due(self) -> None:
        """The transient half of the classification must not regress."""
        from issue_orchestrator.control.stuck_sweep import stuck_sweep_due

        host = MagicMock()
        host.list_issues.side_effect = DNS_DROP
        gatherer = _gatherer(host)
        state = OrchestratorState()

        gatherer.gather_tech_lead_facts(state, now=SWEEP_DUE_AT)

        assert state.last_stuck_sweep_at == 0.0
        assert stuck_sweep_due(_sweep_config(), state, SWEEP_DUE_AT) is True


class _RecordingEvents:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:  # noqa: ANN001
        self.published.append(event)
