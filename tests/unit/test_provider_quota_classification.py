"""Provider quota exhaustion is a typed, human-fixable outage (#7096).

Every string in this file is a real banner or typed error code emitted by a
provider CLI, not a paraphrase. That matters: the defect these tests pin was
that ``classify_provider_output`` returned ``None`` for the exact words Codex
prints when an account runs out of credits, so the failure was invisible to the
retry loop, the circuit, the claim ledger, and the reaction model at once.

The layers are tested separately because they failed separately: the classifier
had no verdict to give, and every consumer downstream of it then defaulted to
the wrong one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.control.in_flight_work import SettlementOutcome
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.execution.agent_runner_errors import (
    classify_provider_error,
    classify_provider_output,
)
from issue_orchestrator.execution.provider_circuit_store import (
    SQLiteProviderCircuitStore,
)
from issue_orchestrator.ports.provider_resilience import (
    InMemoryProviderCircuitStore,
    ProviderCircuitState,
    ProviderErrorType,
)

# Codex's typed error codes and the prose banner it renders around them.
CODEX_QUOTA_OUTPUT = (
    "usage_limit_exceeded",
    "workspace_owner_usage_limit_reached",
    "workspace_member_credits_depleted",
    "You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/"
    "pricing), visit https://chatgpt.com/codex/settings/usage to purchase more "
    "credits or try again later.",
    "Your workspace is out of credits.",
)

# The monthly spend cap, which renders as its own modal rather than as a quota
# error. It was previously read as a plain "the agent did not accept the
# prompt" and respawned four times against a provider that could not answer.
SPEND_CAP_OUTPUT = (
    "Monthly spend limit reached",
    "You have reached your spending limit",
)

# Codex refresh tokens are single-use and rotate, and the CLI takes no
# cross-process lock around the rewrite. Only the first of these four sibling
# failures was previously known to the table.
CODEX_REFRESH_FAILURES = (
    "Failed to refresh your session because your refresh token has expired. "
    "Please log out and sign in again.",
    "Failed to refresh your session because your refresh token was already "
    "used. Please log out and sign in again.",
    "Failed to refresh your session because your refresh token was revoked. "
    "Please log out and sign in again.",
    "Failed to refresh your session because you have since logged out or "
    "signed in to another account. Please log out and sign in again.",
)

# Claude subscription windows. These *do* reopen on a clock, so they are rate
# limits — but neither phrase contained a rate-limit token either, so both
# also classified as ``None``.
CLAUDE_WINDOW_LIMITS = (
    "You've hit your session limit",
    "You've hit your weekly limit",
)


class TestQuotaClassification:
    """The strings that had no verdict now have the right one."""

    @pytest.mark.parametrize("output", CODEX_QUOTA_OUTPUT + SPEND_CAP_OUTPUT)
    def test_exhaustion_classifies_as_quota(self, output: str) -> None:
        assert classify_provider_output(output) is ProviderErrorType.QUOTA

    @pytest.mark.parametrize("output", CODEX_REFRESH_FAILURES)
    def test_every_refresh_failure_classifies_as_auth(self, output: str) -> None:
        """All four need the same human re-login, so all four are AUTH.

        Only "has expired" was matched before. The other three are what
        concurrent sessions actually produce, because the losing process of a
        token rotation is told its token was *used*, not that it expired.
        """
        assert classify_provider_output(output) is ProviderErrorType.AUTH

    @pytest.mark.parametrize("output", CLAUDE_WINDOW_LIMITS)
    def test_subscription_windows_are_rate_limits_not_quota(
        self, output: str
    ) -> None:
        """A window that reopens on a clock is not an exhausted balance."""
        assert classify_provider_output(output) is ProviderErrorType.RATE_LIMIT

    def test_ordinary_rate_limits_are_unchanged(self) -> None:
        """The quota table is additive: it must not capture existing verdicts."""
        assert classify_provider_output("rate_limit_reached") is (
            ProviderErrorType.RATE_LIMIT
        )
        assert classify_provider_output("429 Too Many Requests") is (
            ProviderErrorType.RATE_LIMIT
        )

    def test_healthy_output_still_classifies_as_nothing(self) -> None:
        assert classify_provider_output("Reading files, running tests...") is None


# Text an AGENT plausibly writes while working on billing or rate-limit code.
# This matters because the table is matched against the whole PTY transcript,
# which includes everything the agent itself produced — and this repository's
# own agents work on exactly this subject matter.
AGENT_PROSE_ABOUT_QUOTA = (
    "I will add a usage limit check to the billing module",
    "The spend limit should be configurable per workspace",
    "Consider showing purchase more credits in the empty state",
    # Gemini's /stats output on a healthy session, which the bare phrase
    # "usage limit" would otherwise have matched.
    "Usage limit: 1,000",
    "Usage limits span all sessions and reset daily.",
)


class TestQuotaTokensDoNotFireOnAgentProse:
    """A false positive here parks work until a success, so the bar is high.

    Quota trips on the first observation, and the next successful provider call
    may not arrive promptly. A phrase common enough to appear in an agent's own
    reasoning must not be in the table, however plausible it looks as a provider
    banner.
    """

    @pytest.mark.parametrize("output", AGENT_PROSE_ABOUT_QUOTA)
    def test_discussing_quota_is_not_hitting_one(self, output: str) -> None:
        assert classify_provider_output(output) is None


class TestHumanInterventionPolicy:
    """Cause and policy are separable, and call sites branch on policy."""

    @pytest.mark.parametrize(
        "error_type", [ProviderErrorType.AUTH, ProviderErrorType.QUOTA]
    )
    def test_human_fixable_causes(self, error_type: ProviderErrorType) -> None:
        assert error_type.requires_human_intervention

    @pytest.mark.parametrize(
        "error_type",
        [
            ProviderErrorType.TRANSIENT,
            ProviderErrorType.RATE_LIMIT,
            ProviderErrorType.FATAL,
        ],
    )
    def test_time_healed_and_fatal_causes_are_not(
        self, error_type: ProviderErrorType
    ) -> None:
        assert not error_type.requires_human_intervention


class TestTimeoutDoesNotMaskExhaustion:
    """The manufactured-timeout path, which is where work got destroyed."""

    @pytest.mark.parametrize("output", CODEX_QUOTA_OUTPUT)
    def test_quota_survives_a_wall_clock_timeout(self, output: str) -> None:
        """An exhausted account waits out the clock exactly as a dead login does.

        Without this the session is recorded as TIMED_OUT — the classification
        under which cleanup is entitled to treat an unpushed worktree as
        abandoned work.
        """
        classified = classify_provider_error(
            stdout=output, stderr="", exit_code=1, timed_out=True
        )

        assert classified is ProviderErrorType.QUOTA

    @pytest.mark.parametrize(
        "output", ["working...", "rate limit exceeded", "503 service unavailable"]
    )
    def test_time_healed_output_still_degrades_to_transient(
        self, output: str
    ) -> None:
        """Only the human-fixable causes override a timeout; retry is untouched."""
        classified = classify_provider_error(
            stdout=output, stderr="", exit_code=None, timed_out=True
        )

        assert classified is ProviderErrorType.TRANSIENT


class TestClaimSettlement:
    """A session the provider never let run has not spent its request."""

    def test_quota_defers_the_claim(self) -> None:
        assert SettlementOutcome.for_provider_error(ProviderErrorType.QUOTA) is (
            SettlementOutcome.PROVIDER_DEFERRED
        )

    @pytest.mark.parametrize(
        "error_type",
        [ProviderErrorType.AUTH, ProviderErrorType.TRANSIENT],
    )
    def test_existing_deferrals_are_unchanged(
        self, error_type: ProviderErrorType
    ) -> None:
        assert SettlementOutcome.for_provider_error(error_type) is (
            SettlementOutcome.PROVIDER_DEFERRED
        )

    @pytest.mark.parametrize(
        "error_type", [None, ProviderErrorType.FATAL, ProviderErrorType.RATE_LIMIT]
    )
    def test_everything_else_still_consumes_the_claim(
        self, error_type: ProviderErrorType | None
    ) -> None:
        assert SettlementOutcome.for_provider_error(error_type) is (
            SettlementOutcome.CONSUMED
        )


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def names(self) -> list[str]:
        return [
            e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type)
            for e in self.published
        ]


def _manager(events, *, auth_cooldown: int = 21600) -> ProviderResilienceManager:
    from issue_orchestrator.infra.config_models import (
        ProviderCircuitBreakerConfig,
        ProviderResilienceConfig,
    )

    return ProviderResilienceManager(
        config=ProviderResilienceConfig(
            circuit_breaker=ProviderCircuitBreakerConfig(
                auth_failure_threshold=3,
                auth_cooldown_seconds=auth_cooldown,
            )
        ),
        store=InMemoryProviderCircuitStore(),
        events=events,
    )


class TestQuotaCircuit:
    """Exhaustion gets its own dimension and evidence-based recovery."""

    def test_one_observation_opens_the_circuit(self) -> None:
        """No threshold ladder: a second observation costs a second session.

        The auth threshold exists because one cached probe sample answers every
        launch in a tick. A quota verdict has no probe behind it — it is read
        from a session that really ran and really failed.
        """
        events = RecordingEvents()
        manager = _manager(events)

        state = manager.record_quota_failure(
            "codex", error_summary="usage_limit_exceeded"
        )

        assert state is not None
        assert state.consecutive_quota_failures == 1
        assert state.quota_open_until is not None
        assert "provider.quota_exhausted" in events.names()
        assert "provider.outage_entered" in events.names()

    def test_the_aggregate_circuit_is_open(self) -> None:
        manager = _manager(RecordingEvents())
        manager.record_quota_failure("codex", error_summary="out of credits")

        statuses = {s.provider: s for s in manager.snapshot()}
        assert statuses["codex"].is_open

    def test_a_successful_call_retires_the_quota_outage(self) -> None:
        """A completed call proves the account has usable allowance again."""
        events = RecordingEvents()
        manager = _manager(events)
        quota_at = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        success_at = quota_at + timedelta(seconds=1)
        manager.record_quota_failure(
            "codex",
            error_summary="out of credits",
            now=quota_at,
        )

        updated = manager.record_success(
            "codex",
            observed_at=success_at,
            now=success_at,
        )

        assert updated is None
        assert manager.store.get("codex") is None
        assert events.names().count("provider.outage_exited") == 1

    def test_a_success_clears_quota_without_erasing_an_auth_outage(self) -> None:
        """Success is quota evidence, but only a READY probe can clear auth."""
        events = RecordingEvents()
        manager = _manager(events)
        quota_at = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        manager.record_quota_failure(
            "codex",
            error_summary="out of credits",
            now=quota_at,
        )
        for sample_id in ("s1", "s2", "s3"):
            manager.record_auth_failure(
                "codex",
                error_summary="not logged in",
                sample_id=sample_id,
                now=quota_at,
            )

        manager.record_success(
            "codex",
            observed_at=quota_at + timedelta(seconds=1),
            now=quota_at + timedelta(seconds=1),
        )

        state = manager.store.get("codex")
        assert state is not None
        assert state.auth_open_until is not None
        assert state.consecutive_auth_failures == 3
        assert state.last_auth_sample_id == "s3"
        assert state.quota_open_until is None
        assert state.consecutive_quota_failures == 0
        assert events.names().count("provider.outage_exited") == 0

    def test_an_older_success_cannot_retire_a_newer_quota_outage(self) -> None:
        """Thread-finish order must not let stale recovery erase newer evidence."""
        events = RecordingEvents()
        manager = _manager(events)
        older_success_at = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        quota_at = older_success_at + timedelta(seconds=1)
        applied_at = quota_at + timedelta(minutes=2)
        manager.record_quota_failure(
            "codex",
            error_summary="out of credits",
            now=quota_at,
        )

        updated = manager.record_success(
            "codex",
            observed_at=older_success_at,
            now=applied_at,
        )

        assert updated is not None
        assert updated.quota_observed_at == quota_at
        assert updated.quota_open_until is not None
        assert updated.consecutive_quota_failures == 1
        assert manager.is_open("codex", applied_at)
        assert events.names().count("provider.outage_exited") == 0

    def test_a_newer_success_retires_an_older_quota_outage(self) -> None:
        """Chronologically newer recovery clears quota despite delayed apply."""
        events = RecordingEvents()
        manager = _manager(events)
        quota_at = datetime(2026, 8, 26, 0, 10, tzinfo=timezone.utc)
        newer_success_at = quota_at + timedelta(seconds=1)
        applied_at = newer_success_at + timedelta(minutes=2)
        manager.record_quota_failure(
            "codex",
            error_summary="out of credits",
            now=quota_at,
        )

        updated = manager.record_success(
            "codex",
            observed_at=newer_success_at,
            now=applied_at,
        )

        assert updated is None
        assert manager.store.get("codex") is None
        assert events.names().count("provider.outage_exited") == 1

    def test_clearing_auth_leaves_the_quota_outage_standing(self) -> None:
        """A READY credential probe is evidence about credentials alone.

        No provider CLI reports a balance, so re-authenticating cannot be
        allowed to release an exhausted account.
        """
        manager = _manager(RecordingEvents())
        manager.record_quota_failure("codex", error_summary="out of credits")
        manager.record_auth_failure("codex", error_summary="not logged in", sample_id="s1")

        manager.clear_auth_failures("codex")

        state = manager.store.get("codex")
        assert state is not None
        assert state.auth_open_until is None
        assert state.consecutive_auth_failures == 0
        assert state.quota_open_until is not None

    def test_a_transient_outage_does_not_disturb_the_quota_dimension(self) -> None:
        manager = _manager(RecordingEvents())
        manager.record_quota_failure("codex", error_summary="out of credits")

        manager.record_transient_failure("codex", error_summary="503")

        state = manager.store.get("codex")
        assert state is not None
        assert state.quota_open_until is not None
        assert state.consecutive_quota_failures == 1

    def test_an_elapsed_deadline_retires_the_cause_and_its_counter(self) -> None:
        """The only recovery path quota has, so it must reset cleanly.

        Keeping the counter would make the next exhaustion trip on a stale
        count from a budget cycle that has already been paid for.
        """
        manager = _manager(RecordingEvents(), auth_cooldown=1)
        manager.record_quota_failure("codex", error_summary="out of credits")

        closed = manager.close_expired(now=datetime.now(timezone.utc) + timedelta(hours=2))

        assert [s.provider for s in closed] == ["codex"]
        state = manager.store.get("codex")
        assert state is not None
        assert state.quota_open_until is None
        assert state.consecutive_quota_failures == 0


class TestQuotaPersistence:
    """The new dimension survives a restart, including from an older database."""

    def test_state_round_trips(self, tmp_path) -> None:
        store = SQLiteProviderCircuitStore(tmp_path / "circuit.sqlite")
        deadline = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

        store.save(
            ProviderCircuitState(
                provider="codex",
                consecutive_outages=0,
                last_error_summary="out of credits",
                updated_at=deadline,
                quota_open_until=deadline,
                consecutive_quota_failures=2,
                quota_observed_at=deadline - timedelta(hours=1),
            )
        )

        loaded = store.get("codex")
        assert loaded is not None
        assert loaded.quota_open_until == deadline
        assert loaded.consecutive_quota_failures == 2
        assert loaded.quota_observed_at == deadline - timedelta(hours=1)

    def test_a_database_written_before_quota_existed_is_migrated(
        self, tmp_path
    ) -> None:
        """A live orchestrator has one of these on disk right now."""
        db_path = tmp_path / "legacy.sqlite"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE provider_circuit (
                provider TEXT PRIMARY KEY,
                transient_open_until TEXT,
                consecutive_outages INTEGER NOT NULL,
                last_error_summary TEXT,
                updated_at TEXT NOT NULL,
                consecutive_auth_failures INTEGER NOT NULL DEFAULT 0,
                auth_open_until TEXT,
                last_auth_sample_id TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO provider_circuit VALUES
                ('codex', NULL, 2, 'boom', '2026-08-21T12:00:00+00:00', 0, NULL, '');
            """
        )
        conn.commit()
        conn.close()

        store = SQLiteProviderCircuitStore(db_path)

        loaded = store.get("codex")
        assert loaded is not None
        assert loaded.consecutive_outages == 2
        assert loaded.quota_open_until is None
        assert loaded.consecutive_quota_failures == 0
        assert loaded.quota_observed_at is None

    def test_an_active_quota_row_gains_a_conservative_observation_watermark(
        self, tmp_path
    ) -> None:
        """The pre-watermark schema remains protected after an upgrade."""
        db_path = tmp_path / "pre-watermark.sqlite"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE provider_circuit (
                provider TEXT PRIMARY KEY,
                transient_open_until TEXT,
                consecutive_outages INTEGER NOT NULL,
                last_error_summary TEXT,
                updated_at TEXT NOT NULL,
                consecutive_auth_failures INTEGER NOT NULL DEFAULT 0,
                auth_open_until TEXT,
                last_auth_sample_id TEXT NOT NULL DEFAULT '',
                quota_open_until TEXT,
                consecutive_quota_failures INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO provider_circuit VALUES (
                'codex', NULL, 0, 'out of credits',
                '2026-08-26T00:10:00+00:00', 0, NULL, '',
                '2026-08-26T06:10:00+00:00', 1
            );
            """
        )
        conn.commit()
        conn.close()

        store = SQLiteProviderCircuitStore(db_path)

        loaded = store.get("codex")
        assert loaded is not None
        assert loaded.quota_observed_at == datetime(
            2026, 8, 26, 0, 10, tzinfo=timezone.utc
        )
        evidence = store.get_evidence("codex")
        assert evidence is not None
        assert evidence.quota_failure_observed_at == loaded.quota_observed_at
