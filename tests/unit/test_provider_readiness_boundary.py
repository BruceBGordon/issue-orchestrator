"""The typed provider-readiness / auth-failure boundary (#6999).

On 2026-08-04 an expired Claude Code login produced four consecutive
90-minute zero-work sessions and four misdirected failure investigations.
These tests pin the boundary that makes that impossible, and — just as
importantly — pin that the boundary has exactly ONE owner per concern:

* one classification table (``execution/agent_runner_errors.py``),
* one credential probe per provider (the provider adapter),
* one circuit-state owner (``ProviderResilienceManager``),
* one launch gate (``ProviderAvailabilityPolicy`` / ``SessionLauncher``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.control.provider_availability import ProviderAvailabilityPolicy
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.control.tech_lead_reaction import (
    record_completed_session_problem,
)
from issue_orchestrator.domain.models import DiscoveredFailure, SessionStatus
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.agent_runner_errors import (
    classify_provider_error,
    classify_provider_output,
)
from issue_orchestrator.execution.agent_runner_providers import (
    ClaudeCodeProvider,
    CodexProvider,
)
from issue_orchestrator.execution.provider_readiness_probe import (
    CLIProviderReadinessProbe,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from issue_orchestrator.ports.command_runner import CommandResult, OutputNewlines
from issue_orchestrator.ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadiness,
    ProviderReadinessState,
)
from issue_orchestrator.ports.provider_resilience import ProviderErrorType

from tests.unit.test_session_controller import (
    MockCompletionProcessor,
    StubWorkingCopy,
    decide_with_run_assets,
)


SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator"

# The banner an expired Claude Code login renders, verbatim from the terminal
# recordings in the incident (offset 2740 ms for #6463, 522 ms for #5336).
EXPIRED_LOGIN_BANNER = "Login expired · Please run /login"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeCommandRunner:
    """A CommandRunner that replays one canned result and records argv."""

    result: CommandResult
    commands: list[list[str]] = field(default_factory=list)

    def run(
        self,
        command,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        self.commands.append(list(command))
        return self.result


@dataclass
class StubReadinessProbe:
    """A probe returning a fixed readiness, recording who asked."""

    readiness: ProviderReadiness
    launch_calls: list[str] = field(default_factory=list)
    diagnose_calls: list[str] = field(default_factory=list)

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        self.launch_calls.append(provider)
        return self.readiness

    def diagnose_session_output(self, provider: str, output: str) -> ProviderReadiness:
        self.diagnose_calls.append(provider)
        return self.readiness


class RecordingEvents:
    """EventSink capturing published events for assertions."""

    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def names(self) -> list[str]:
        return [
            e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type)
            for e in self.published
        ]


def _manager(events, *, threshold: int = 1, auth_cooldown: int = 21600):
    from issue_orchestrator.infra.config_models import (
        ProviderCircuitBreakerConfig,
        ProviderResilienceConfig,
    )

    config = ProviderResilienceConfig(
        circuit_breaker=ProviderCircuitBreakerConfig(
            auth_failure_threshold=threshold,
            auth_cooldown_seconds=auth_cooldown,
        )
    )
    return ProviderResilienceManager(
        config=config, store=InMemoryProviderCircuitStore(), events=events
    )


# ---------------------------------------------------------------------------
# 1. Claude expired-login preflight
# ---------------------------------------------------------------------------


class TestClaudeExpiredLoginPreflight:
    """The provider adapter answers "am I logged in" without spawning a TUI."""

    def test_logged_out_probe_reports_auth_expired(self) -> None:
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
        )

        readiness = ClaudeCodeProvider().check_readiness(runner)

        assert readiness.state is ProviderReadinessState.AUTH_EXPIRED
        assert readiness.error_type is ProviderErrorType.AUTH
        assert readiness.human_fixable
        assert not readiness.launchable

    def test_logged_in_probe_reports_ready(self) -> None:
        runner = FakeCommandRunner(
            CommandResult(
                returncode=0,
                stdout='{"loggedIn": true, "authMethod": "claude.ai"}',
                stderr="",
            )
        )

        readiness = ClaudeCodeProvider().check_readiness(runner)

        assert readiness.state is ProviderReadinessState.READY
        assert readiness.authenticated
        assert readiness.launchable
        assert readiness.error_type is None

    def test_probe_is_local_and_non_interactive(self) -> None:
        """The probe must be affordable on every launch: no prompt, no TUI."""
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": true}', stderr="")
        )

        ClaudeCodeProvider().check_readiness(runner)

        assert runner.commands == [["claude", "auth", "status", "--json"]]

    def test_probe_timeout_is_unknown_not_authenticated(self) -> None:
        """A probe that could not answer must never read as "credentials fine"."""
        runner = FakeCommandRunner(
            CommandResult(returncode=None, stdout="", stderr="", timed_out=True)
        )

        readiness = ClaudeCodeProvider().check_readiness(runner)

        assert readiness.state is ProviderReadinessState.UNKNOWN
        assert not readiness.authenticated
        # ...but still launchable: an unprobeable provider behaves as before.
        assert readiness.launchable

    def test_codex_not_logged_in_reports_auth_expired(self) -> None:
        runner = FakeCommandRunner(
            CommandResult(returncode=1, stdout="Not logged in", stderr="")
        )

        readiness = CodexProvider().check_readiness(runner)

        assert readiness.state is ProviderReadinessState.AUTH_EXPIRED

    def test_codex_logged_in_reports_ready(self) -> None:
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        )

        readiness = CodexProvider().check_readiness(runner)

        assert readiness.state is ProviderReadinessState.READY

    def test_policy_parks_the_launch_and_feeds_the_circuit(self) -> None:
        """Launch control gets the typed outcome; the circuit owner gets the AUTH fact."""
        events = RecordingEvents()
        manager = _manager(events)
        probe = StubReadinessProbe(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=manager, readiness_probe=probe
        )

        readiness = policy.probe_launch_readiness("claude-code")

        assert not readiness.launchable
        assert probe.launch_calls == ["claude-code"]
        # The circuit — not the caller — decided to pause the fleet.
        assert manager.is_open("claude-code")
        assert EventName.PROVIDER_AUTH_FAILED.value in events.names()

    def test_ready_provider_leaves_the_circuit_untouched(self) -> None:
        events = RecordingEvents()
        manager = _manager(events)
        policy = ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=manager,
            readiness_probe=StubReadinessProbe(ProviderReadiness.ready("claude-code")),
        )

        assert policy.probe_launch_readiness("claude-code").launchable
        assert not manager.is_open("claude-code")
        assert events.names() == []

    def test_launcher_parks_the_launch_without_spawning_a_session(
        self, tmp_path: Path
    ) -> None:
        """The whole point: an unauthenticated provider spawns nothing."""
        harness = _LauncherHarness(
            tmp_path,
            StubReadinessProbe(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
        )

        result = harness.launch()

        assert not result.success
        assert "not ready" in result.reason
        assert harness.created == []
        assert EventName.SESSION_LAUNCH_FAILED_AUTH.value in harness.event_names()

    def test_launcher_proceeds_past_the_gate_when_the_provider_is_ready(
        self, tmp_path: Path
    ) -> None:
        """The gate is scoped to unready providers; it blocks nothing else."""
        probe = StubReadinessProbe(ProviderReadiness.ready("claude-code"))
        harness = _LauncherHarness(tmp_path, probe)

        harness.launch()

        # The gate really ran (not skipped by an earlier precondition) and let
        # the launch through.
        assert probe.launch_calls == ["claude-code"]
        assert EventName.SESSION_LAUNCH_FAILED_AUTH.value not in harness.event_names()

    def test_default_policy_never_claims_a_provider_is_authenticated(self) -> None:
        """With no probe wired, readiness is UNKNOWN — not READY, not blocked."""
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=_manager(RecordingEvents())
        )

        readiness = policy.probe_launch_readiness("claude-code")

        assert readiness.state is ProviderReadinessState.UNKNOWN
        assert not readiness.authenticated
        assert readiness.launchable


def _config():
    from issue_orchestrator.infra.config import Config

    return Config(repo="test/repo", repo_root=Path("/tmp/does-not-matter"))


class _LauncherHarness:
    """A real SessionLauncher wired with a real circuit owner and a stub probe.

    Everything below the provider gate is mocked: these tests are about whether
    the gate spawns a session, not about worktree mechanics.
    """

    def __init__(self, tmp_path: Path, probe) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.session_launcher import SessionLauncher
        from issue_orchestrator.domain.state_machines.issue_machine import (
            IssueStateMachine,
        )
        from issue_orchestrator.domain.state_machines.review_machine import (
            ReviewStateMachine,
        )
        from issue_orchestrator.domain.state_machines.session_machine import (
            SessionStateMachine,
        )
        from issue_orchestrator.infra.config import AgentConfig, Config
        from issue_orchestrator.infra.tech_lead_authority_store import (
            SqliteTechLeadAuthorityStore,
        )
        from issue_orchestrator.ports import (
            NullBoardSnapshotProvider,
            NullManifestDownloader,
        )
        from tests.callback_endpoint_helpers import ready_callback_endpoint
        from tests.unit.test_session_launcher import (
            MockCommandRunner,
            MockEventSink,
            MockRepositoryHost,
            MockWorkingCopy,
            MockWorktreeManager,
        )

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("Test prompt")
        config = Config(repo="test/repo", repo_root=tmp_path)
        config.agents = {
            "agent:backend": AgentConfig(
                prompt_path=prompt_path, provider="claude-code", model="sonnet"
            )
        }

        self.created: list[str] = []
        self.events = MockEventSink()
        self.launcher = SessionLauncher(
            config=config,
            events=self.events,
            repository_host=MockRepositoryHost(),
            action_applier=MagicMock(),
            session_manager=MagicMock(),
            worktree_manager=MockWorktreeManager(tmp_path),
            working_copy=MockWorkingCopy(),
            command_runner=MockCommandRunner(),
            session_output=FileSystemSessionOutput(),
            manifest_downloader=NullManifestDownloader(),
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(tmp_path),
            session_exists_fn=lambda name: False,
            create_session_fn=lambda name, cmd, wd, title: (
                self.created.append(name) or True
            ),
            get_issue_machine=lambda issue: IssueStateMachine(issue),
            get_session_machine=lambda name, n, timeout: SessionStateMachine(
                name, n, timeout_minutes=timeout
            ),
            get_review_machine=lambda pr, issue: ReviewStateMachine(pr, issue),
            provider_resilience=_manager(self.events),
            board_snapshot_provider=NullBoardSnapshotProvider(),
            agent_callback_endpoint=ready_callback_endpoint(),
            provider_readiness_probe=probe,
        )

    def launch(self):
        from issue_orchestrator.domain.models import Issue

        issue = Issue(
            number=123,
            title="Test Issue",
            labels=["agent:backend"],
            repo="test/repo",
        )
        return self.launcher.launch_issue_session(issue, [])

    def event_names(self) -> list[str]:
        return [
            e.event_type.value if hasattr(e.event_type, "value") else str(e.event_type)
            for e in self.events.events
        ]


# ---------------------------------------------------------------------------
# 2. Early TUI banner classifies through the shared table
# ---------------------------------------------------------------------------


class TestSingleClassificationTable:
    """The expired-login banner is auth — and only one module knows that."""

    def test_expired_login_banner_classifies_as_auth(self) -> None:
        assert classify_provider_output(EXPIRED_LOGIN_BANNER) is ProviderErrorType.AUTH

    def test_provider_adapters_delegate_to_the_shared_table(self) -> None:
        """No provider keeps a private copy: both route to the same function."""
        for provider in (ClaudeCodeProvider(), CodexProvider()):
            assert (
                provider.classify_output(EXPIRED_LOGIN_BANNER)
                is ProviderErrorType.AUTH
            )

    def test_timeout_no_longer_masks_an_auth_failure(self) -> None:
        """The observed failure used to classify TRANSIENT and get retried."""
        classified = classify_provider_error(
            stdout=EXPIRED_LOGIN_BANNER,
            stderr="",
            exit_code=None,
            timed_out=True,
        )

        assert classified is ProviderErrorType.AUTH

    def test_timeout_without_an_auth_signature_is_still_transient(self) -> None:
        classified = classify_provider_error(
            stdout="working...", stderr="", exit_code=None, timed_out=True
        )

        assert classified is ProviderErrorType.TRANSIENT

    def test_exactly_one_module_knows_the_banner_text(self) -> None:
        """Guardrail: a watcher-local token list would show up here.

        The whole design constraint of #6999 is one classification table. If a
        second module starts matching provider banner text, this fails and
        names the file.
        """
        owners = sorted(
            path.relative_to(SRC_ROOT).as_posix()
            for path in SRC_ROOT.rglob("*.py")
            if "login expired" in path.read_text(encoding="utf-8").lower()
        )

        assert owners == ["execution/agent_runner_errors.py"]


# ---------------------------------------------------------------------------
# 3. The typed provider -> launch-control boundary
# ---------------------------------------------------------------------------


class TestTypedBoundary:
    """Only typed values cross into control; raw interpretation stays in adapters."""

    def test_control_layer_never_imports_the_raw_classifier(self) -> None:
        offenders = sorted(
            path.relative_to(SRC_ROOT).as_posix()
            for layer in ("control", "observation")
            for path in (SRC_ROOT / layer).rglob("*.py")
            if "agent_runner_errors" in path.read_text(encoding="utf-8")
        )

        assert offenders == []

    def test_diagnosis_confirms_a_signature_against_the_real_probe(self) -> None:
        """An echoed banner is a trigger, not a verdict.

        This orchestrator routinely prints provider auth banners while working
        on its own auth tooling. Confirmation by the provider's own credential
        probe is what makes acting on the signature safe.
        """
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": true}', stderr="")
        )
        probe = CLIProviderReadinessProbe(runner)

        readiness = probe.diagnose_session_output("claude-code", EXPIRED_LOGIN_BANNER)

        assert readiness.state is ProviderReadinessState.UNKNOWN
        assert not readiness.human_fixable

    def test_confirmed_signature_is_reported_as_auth_expired(self) -> None:
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
        )
        probe = CLIProviderReadinessProbe(runner)

        readiness = probe.diagnose_session_output("claude-code", EXPIRED_LOGIN_BANNER)

        assert readiness.state is ProviderReadinessState.AUTH_EXPIRED

    def test_output_without_a_signature_never_probes(self) -> None:
        """Ordinary agent output must not cost a subprocess every tick."""
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
        )
        probe = CLIProviderReadinessProbe(runner)

        readiness = probe.diagnose_session_output("claude-code", "reading files...")

        assert readiness.state is ProviderReadinessState.UNKNOWN
        assert runner.commands == []

    def test_repeat_launch_checks_share_one_probe_result(self) -> None:
        """A tick gating several launches on one provider probes once."""
        clock_values = iter([0.0, 1.0, 2.0, 3.0])
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": true}', stderr="")
        )
        probe = CLIProviderReadinessProbe(
            runner, ttl_seconds=60.0, clock=lambda: next(clock_values)
        )

        probe.check_launch_readiness("claude-code")
        probe.check_launch_readiness("claude-code")

        assert len(runner.commands) == 1

    def test_unknown_provider_name_is_reported_not_raised(self) -> None:
        probe = CLIProviderReadinessProbe(
            FakeCommandRunner(CommandResult(returncode=0, stdout="", stderr=""))
        )

        readiness = probe.check_launch_readiness("not-a-provider")

        assert readiness.state is ProviderReadinessState.UNKNOWN
        assert readiness.launchable


# ---------------------------------------------------------------------------
# 4. Distinct non-timeout outcome, excluded from investigation minting
# ---------------------------------------------------------------------------


class TestDistinctAuthOutcome:
    def test_auth_dead_session_is_not_timed_out(self, tmp_path: Path) -> None:
        events = RecordingEvents()
        controller = SessionController(
            completion_processor=MockCompletionProcessor(),
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )
        observation = SessionObservationResult.provider_auth_failed(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )

        decision = decide_with_run_assets(
            controller,
            observation=observation,
            worktree_path=tmp_path / "worktree",
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status is not SessionStatus.TIMED_OUT
        assert decision.status is SessionStatus.BLOCKED
        assert decision.provider_error_type is ProviderErrorType.AUTH
        assert decision.provider_auth_failure is not None
        assert decision.provider_auth_failure.provider == "claude-code"
        assert EventName.SESSION_LAUNCH_FAILED_AUTH.value in events.names()

    def test_auth_observation_is_terminal(self) -> None:
        observation = SessionObservationResult.provider_auth_failed(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )

        assert observation.observation is SessionObservation.PROVIDER_AUTH_FAILED
        assert observation.is_terminal

    def test_auth_outcome_mints_no_failure_investigation(self, make_session) -> None:
        recorded: list[DiscoveredFailure] = []

        record_completed_session_problem(
            status=SessionStatus.BLOCKED,
            session=make_session(issue_labels=["agent:backend"]),
            tech_lead_agent="agent:tech-lead",
            blocking_label="blocked:provider-unavailable",
            artifact_hints=lambda: (),
            record=recorded.append,
            provider_error_type=ProviderErrorType.AUTH,
        )

        assert recorded == []

    def test_an_ordinary_blocked_session_still_mints_one(self, make_session) -> None:
        """The exclusion is scoped to the typed AUTH verdict, nothing wider."""
        recorded: list[DiscoveredFailure] = []

        record_completed_session_problem(
            status=SessionStatus.BLOCKED,
            session=make_session(issue_labels=["agent:backend"]),
            tech_lead_agent="agent:tech-lead",
            blocking_label="blocked:needs-human",
            artifact_hints=lambda: (),
            record=recorded.append,
            provider_error_type=None,
        )

        assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 5. Circuit-state transitions
# ---------------------------------------------------------------------------


class TestCircuitOwnership:
    def test_consecutive_auth_failures_produce_one_transition(self) -> None:
        events = RecordingEvents()
        manager = _manager(events, threshold=2)

        first = manager.record_auth_failure("claude-code", error_summary="not logged in")
        assert first is not None
        assert first.consecutive_auth_failures == 1
        assert not manager.is_open("claude-code")

        second = manager.record_auth_failure(
            "claude-code", error_summary="still not logged in"
        )
        assert second is not None
        assert second.consecutive_auth_failures == 2
        assert manager.is_open("claude-code")

        third = manager.record_auth_failure(
            "claude-code", error_summary="still not logged in"
        )
        assert third is not None
        assert third.consecutive_auth_failures == 3

        names = events.names()
        assert names.count(EventName.PROVIDER_AUTH_FAILED.value) == 3
        # One circuit transition, not one per failure.
        assert names.count(EventName.PROVIDER_OUTAGE_ENTERED.value) == 1

    def test_auth_cooldown_is_its_own_window(self) -> None:
        """A credential outage must not retry on the transient ladder."""
        manager = _manager(RecordingEvents(), auth_cooldown=7200)
        now = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)

        state = manager.record_auth_failure(
            "claude-code", error_summary="not logged in", now=now
        )

        assert state is not None
        assert state.open_until == now + timedelta(seconds=7200)

    def test_a_successful_launch_clears_the_auth_circuit(self) -> None:
        """Recovery does not wait out the long cooldown."""
        manager = _manager(RecordingEvents())
        manager.record_auth_failure("claude-code", error_summary="not logged in")

        manager.record_success("claude-code")

        assert not manager.is_open("claude-code")
        assert manager.get_state("claude-code") is None

    def test_transient_failures_do_not_disturb_the_auth_count(self) -> None:
        manager = _manager(RecordingEvents(), threshold=2)
        manager.record_auth_failure("claude-code", error_summary="not logged in")

        manager.record_transient_failure("claude-code", error_summary="502")

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 1
        assert state.consecutive_outages == 1

    def test_no_provider_means_no_circuit_write(self) -> None:
        manager = _manager(RecordingEvents())

        assert manager.record_auth_failure("", error_summary="not logged in") is None
        assert manager.snapshot() == []


# ---------------------------------------------------------------------------
# Live-session observation
# ---------------------------------------------------------------------------


class TestLiveSessionObservation:
    """A live session dies on its provider's verdict, not on a token match."""

    def _observer(self, config, probe):
        from issue_orchestrator.observation.observer import SessionObserver

        class _AlwaysRunning:
            def session_exists_by_name(self, name: str) -> bool:
                return True

            def send_to_session_by_name(self, name: str, text: str) -> bool:
                return True

            def get_session_output(self, issue_number, lines=100, session_name=None):
                return ""

        return SessionObserver(
            config=config,
            session_output=FileSystemSessionOutput(),
            events=RecordingEvents(),
            session_runner=_AlwaysRunning(),
            provider_readiness_probe=probe,
        )

    def _session_with_log(self, make_session, text: str):
        """A live session whose terminal recording already holds ``text``."""
        from issue_orchestrator.infra.config import AgentConfig
        from issue_orchestrator.infra.terminal_recording import (
            TERMINAL_RECORDING_FILENAME,
        )

        session = make_session()
        session.agent_config = AgentConfig(
            prompt_path=session.agent_config.prompt_path, provider="claude-code"
        )
        recording = session.run_assets.run_dir / TERMINAL_RECORDING_FILENAME
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            json.dumps({"kind": "output", "data": text}) + "\n", encoding="utf-8"
        )
        return session

    def test_confirmed_auth_banner_fails_the_session_immediately(
        self, sample_config, make_session
    ) -> None:
        probe = StubReadinessProbe(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )
        observer = self._observer(sample_config, probe)
        session = self._session_with_log(make_session, EXPIRED_LOGIN_BANNER)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED
        assert result.provider_readiness is not None
        assert result.provider_readiness.provider == "claude-code"

    def test_unconfirmed_signature_leaves_the_session_running(
        self, sample_config, make_session
    ) -> None:
        """The false-positive guard: the probe says the credentials are fine."""
        probe = StubReadinessProbe(
            ProviderReadiness.unknown("claude-code", "not confirmed")
        )
        observer = self._observer(sample_config, probe)
        session = self._session_with_log(make_session, EXPIRED_LOGIN_BANNER)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.RUNNING

    def test_an_old_session_is_not_auth_checked(
        self, sample_config, make_session
    ) -> None:
        """Beyond the launch window the banner cannot be about this launch."""
        from issue_orchestrator.observation.observer import (
            PROVIDER_AUTH_CHECK_WINDOW_SECONDS,
        )

        probe = StubReadinessProbe(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )
        observer = self._observer(sample_config, probe)
        session = self._session_with_log(make_session, EXPIRED_LOGIN_BANNER)
        session.started_at = datetime.now() - timedelta(
            seconds=PROVIDER_AUTH_CHECK_WINDOW_SECONDS + 60
        )

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.RUNNING
        assert probe.diagnose_calls == []

    def test_default_observer_never_reports_an_auth_failure(
        self, sample_config, make_session
    ) -> None:
        observer = self._observer(sample_config, NO_PROVIDER_READINESS_PROBE)
        session = self._session_with_log(make_session, EXPIRED_LOGIN_BANNER)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.RUNNING


# ---------------------------------------------------------------------------
# Circuit-state persistence
# ---------------------------------------------------------------------------


def test_auth_counter_survives_a_pre_existing_database(tmp_path: Path) -> None:
    """A store written before the auth counter existed must still open."""
    import sqlite3

    from issue_orchestrator.execution.provider_circuit_store import (
        SQLiteProviderCircuitStore,
    )

    db_path = tmp_path / "circuit.sqlite"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        """
        CREATE TABLE provider_circuit (
            provider TEXT PRIMARY KEY,
            open_until TEXT,
            consecutive_outages INTEGER NOT NULL,
            last_error_summary TEXT,
            updated_at TEXT NOT NULL
        );
        INSERT INTO provider_circuit VALUES
            ('claude-code', NULL, 2, 'boom', '2026-08-04T22:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    store = SQLiteProviderCircuitStore(db_path)
    state = store.get("claude-code")

    assert state is not None
    assert state.consecutive_outages == 2
    assert state.consecutive_auth_failures == 0


@pytest.mark.parametrize(
    "state,launchable",
    [
        (ProviderReadinessState.READY, True),
        (ProviderReadinessState.UNKNOWN, True),
        (ProviderReadinessState.AUTH_EXPIRED, False),
        (ProviderReadinessState.NOT_INSTALLED, False),
    ],
)
def test_launchability_is_decided_by_the_typed_state(
    state: ProviderReadinessState, launchable: bool
) -> None:
    readiness = ProviderReadiness(provider="claude-code", state=state)

    assert readiness.launchable is launchable
