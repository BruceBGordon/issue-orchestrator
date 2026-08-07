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
from dataclasses import dataclass, field, replace
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


def _resilience_config(*, threshold: int = 1, auth_cooldown: int = 21600):
    from issue_orchestrator.infra.config_models import (
        ProviderCircuitBreakerConfig,
        ProviderResilienceConfig,
    )

    return ProviderResilienceConfig(
        circuit_breaker=ProviderCircuitBreakerConfig(
            auth_failure_threshold=threshold,
            auth_cooldown_seconds=auth_cooldown,
        )
    )


def _manager(events, *, threshold: int = 1, auth_cooldown: int = 21600):
    return ProviderResilienceManager(
        config=_resilience_config(threshold=threshold, auth_cooldown=auth_cooldown),
        store=InMemoryProviderCircuitStore(),
        events=events,
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

        outcome = policy.assess_launch("claude-code")

        assert not outcome.may_launch
        assert outcome.blocked_by_readiness
        assert probe.launch_calls == ["claude-code"]
        # The circuit — not the caller — decided to pause the fleet.
        assert outcome.circuit_open
        assert manager.is_open("claude-code")
        assert EventName.PROVIDER_AUTH_FAILED.value in events.names()

    def test_ready_provider_leaves_a_healthy_circuit_untouched(self) -> None:
        events = RecordingEvents()
        manager = _manager(events)
        policy = ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=manager,
            readiness_probe=StubReadinessProbe(ProviderReadiness.ready("claude-code")),
        )

        assert policy.assess_launch("claude-code").may_launch
        assert not manager.is_open("claude-code")
        assert events.names() == []

    def test_a_re_authenticated_provider_is_released_by_the_probe(self) -> None:
        """The deadlock guard: no session can run to report the good news.

        While the auth circuit is open nothing launches, so recovery has to be
        observable from the probe alone — otherwise the fleet stays parked for
        the whole (deliberately long) auth cooldown.
        """
        events = RecordingEvents()
        manager = _manager(events)
        outage = ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=manager,
            readiness_probe=StubReadinessProbe(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
        )
        outage.assess_launch("claude-code")
        assert manager.is_open("claude-code")

        recovered = ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=manager,
            readiness_probe=StubReadinessProbe(ProviderReadiness.ready("claude-code")),
        )
        assert recovered.assess_launch("claude-code").may_launch

        assert not manager.is_open("claude-code")

    def test_the_gate_reopens_launches_after_re_authentication(self) -> None:
        """End to end through the launcher: parked, then flowing again."""
        from issue_orchestrator.control.provider_launch_gate import ProviderLaunchGate

        events = RecordingEvents()
        manager = _manager(events)

        def gate_for(readiness: ProviderReadiness) -> ProviderLaunchGate:
            return ProviderLaunchGate(
                policy=ProviderAvailabilityPolicy(
                    config=_config(),
                    provider_resilience=manager,
                    readiness_probe=StubReadinessProbe(readiness),
                ),
                events=events,
                apply_actions=lambda actions, context: True,
            )

        parked = gate_for(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        ).check("claude-code", 123)
        assert parked is not None and not parked.success

        proceeded = gate_for(ProviderReadiness.ready("claude-code")).check(
            "claude-code", 123
        )
        assert proceeded is None

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
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value in harness.event_names()

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
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in harness.event_names()

    def test_default_policy_never_claims_a_provider_is_authenticated(self) -> None:
        """With no probe wired, readiness is UNKNOWN — not READY, not blocked."""
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=_manager(RecordingEvents())
        )

        outcome = policy.assess_launch("claude-code")

        assert outcome.readiness.state is ProviderReadinessState.UNKNOWN
        assert not outcome.readiness.authenticated
        assert outcome.may_launch


def _config():
    from issue_orchestrator.infra.config import Config

    return Config(repo="test/repo", repo_root=Path("/tmp/does-not-matter"))


class _LauncherHarness:
    """A real SessionLauncher wired with a real circuit owner and a stub probe.

    Everything below the provider gate is mocked: these tests are about whether
    the gate spawns a session, not about worktree mechanics.
    """

    def __init__(
        self, tmp_path: Path, probe, *, manager=None, events=None, config=None
    ) -> None:
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

        if config is None:
            prompt_path = tmp_path / "prompt.md"
            prompt_path.write_text("Test prompt")
            config = Config(repo="test/repo", repo_root=tmp_path)
            config.agents = {
                "agent:backend": AgentConfig(
                    prompt_path=prompt_path, provider="claude-code", model="sonnet"
                )
            }

        self.created: list[str] = []
        self.events = events if events is not None else MockEventSink()
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
            provider_resilience=manager if manager is not None else _manager(self.events),
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
        return self.launch_issue(issue)

    def launch_issue(self, issue):
        return self.launcher.launch_issue_session(issue, [])

    def event_names(self) -> list[str]:
        if hasattr(self.events, "names"):
            return self.events.names()
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

    @pytest.mark.parametrize(
        "output", ["working...", "rate limit exceeded", "503 service unavailable"]
    )
    def test_timeout_without_an_auth_signature_is_still_transient(
        self, output: str
    ) -> None:
        """Only AUTH overrides the timeout; other retry behaviour is untouched."""
        classified = classify_provider_error(
            stdout=output, stderr="", exit_code=None, timed_out=True
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
        # The live-session story, not the launch-gate one — and no raw
        # provider-blocked label rides along (#6999 F5).
        assert decision.blocked_label is None
        names = events.names()
        assert names.count(EventName.SESSION_PROVIDER_AUTH_TERMINATED.value) == 1
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in names

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

        first = manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )
        assert first is not None
        assert first.consecutive_auth_failures == 1
        assert not manager.is_open("claude-code")

        second = manager.record_auth_failure(
            "claude-code", error_summary="still not logged in", sample_id="s2"
        )
        assert second is not None
        assert second.consecutive_auth_failures == 2
        assert manager.is_open("claude-code")

        third = manager.record_auth_failure(
            "claude-code", error_summary="still not logged in", sample_id="s3"
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
            "claude-code", error_summary="not logged in", sample_id="s1", now=now
        )

        assert state is not None
        assert state.open_until == now + timedelta(seconds=7200)

    def test_a_confirmed_probe_clears_the_auth_circuit(self) -> None:
        """Recovery does not wait out the long cooldown."""
        events = RecordingEvents()
        manager = _manager(events)
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )
        assert manager.is_open("claude-code")

        cleared = manager.clear_auth_failures("claude-code")

        assert cleared is not None
        assert cleared.consecutive_auth_failures == 0
        assert not manager.is_open("claude-code")
        assert EventName.PROVIDER_OUTAGE_EXITED.value in events.names()

    def test_clearing_a_healthy_provider_is_a_no_op(self) -> None:
        """Nothing to retire means no write and no event."""
        events = RecordingEvents()
        manager = _manager(events)

        assert manager.clear_auth_failures("claude-code") is None
        assert events.names() == []

    def test_clearing_auth_leaves_a_transient_outage_count_intact(self) -> None:
        """Only the auth half is retired; the transient ladder keeps its place."""
        manager = _manager(RecordingEvents())
        manager.record_transient_failure("claude-code", error_summary="503")
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )

        manager.clear_auth_failures("claude-code")

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 0
        assert state.consecutive_outages == 1

    def test_transient_failures_do_not_disturb_the_auth_count(self) -> None:
        manager = _manager(RecordingEvents(), threshold=2)
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )

        manager.record_transient_failure("claude-code", error_summary="502")

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 1
        assert state.consecutive_outages == 1

    def test_no_provider_means_no_circuit_write(self) -> None:
        manager = _manager(RecordingEvents())

        assert (
            manager.record_auth_failure(
                "", error_summary="not logged in", sample_id="s1"
            )
            is None
        )
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


# ---------------------------------------------------------------------------
# The production planning path (#6999 F1 / A1)
#
# The launch gate alone cannot end an auth outage: every queue is filtered by
# the planner first, so if planning consults the raw circuit the gate is never
# reached, no probe runs, and the fleet waits out the whole auth cooldown. These
# tests start from an OPEN auth circuit and a provider that is now READY, and
# prove a launch is planned — through the real Planner, for every queue.
# ---------------------------------------------------------------------------


PROVIDER = "claude-code"


@dataclass
class _RecordingProbe:
    """A probe handing out one fixed sample, recording every launch question.

    The sample carries a stable id, exactly as the real probe's short-lived
    cache does within its TTL: the tick sampler and the launch-gate recheck see
    ONE physical observation, so the circuit counts it once.
    """

    readiness: ProviderReadiness
    sample_id: str = "sample-1"
    launch_calls: list[str] = field(default_factory=list)

    def _sample(self) -> ProviderReadiness:
        return replace(self.readiness, sample_id=self.sample_id)

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        self.launch_calls.append(provider)
        return self._sample()

    def diagnose_session_output(self, provider: str, output: str) -> ProviderReadiness:
        del output
        return self._sample()


def _recovery_config(tmp_path: Path):
    from issue_orchestrator.infra.config import AgentConfig, Config

    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    config = Config(repo="test/repo", repo_root=tmp_path, max_concurrent_sessions=4)
    config.agents = {
        label: AgentConfig(prompt_path=prompt, provider=PROVIDER)
        for label in ("agent:backend", "agent:reviewer", "agent:tech-lead")
    }
    config.code_review_agent = "agent:reviewer"
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead.max_concurrent = 1
    return config


def _queue_snapshot(queue: str):
    """One pending item on ``queue``, with everything else empty."""
    from unittest.mock import Mock

    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import (
        PendingRetrospectiveReview,
        PendingReview,
        PendingRework,
        PendingTechLeadReview,
        PendingValidationRetry,
    )
    from issue_orchestrator.domain.session_key import TaskKind
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
    from tests.unit.test_planner import make_issue, make_snapshot

    issue_key = FakeIssueKey(name="7")
    if queue == "coding":
        return make_snapshot(issues=[make_issue(7, labels=["agent:backend"])]), {}
    if queue == "review":
        review = PendingReview(
            issue_key=issue_key,
            pr_number=70,
            pr_url="url",
            branch_name="branch",
            _issue_number=7,
            agent_label="agent:backend",
        )
        workflow = Mock()
        workflow.is_configured.return_value = True
        workflow.should_launch_reviews.return_value = Mock(
            should_launch=True, skip_reason=None, reviews_to_launch=[review]
        )
        return make_snapshot(pending_reviews=[review]), {"review_workflow": workflow}
    if queue == "retrospective_review":
        review = PendingRetrospectiveReview(
            issue_key=issue_key,
            issue_number=7,
            issue_title="Retro",
            agent_label="agent:backend",
            trigger_label="review-first",
        )
        workflow = Mock()
        workflow.is_configured.return_value = True
        workflow.should_launch_reviews.return_value = Mock(
            should_launch=True, skip_reason=None, reviews_to_launch=[review]
        )
        return (
            make_snapshot(pending_retrospective_reviews=[review]),
            {"retrospective_review_workflow": workflow},
        )
    if queue == "rework":
        rework = PendingRework(
            issue_key=issue_key, agent_type="agent:backend", issue_number=7
        )
        workflow = Mock()
        workflow.should_launch_reworks.return_value = Mock(
            should_launch=True, skip_reason=None, reworks_to_launch=[rework]
        )
        workflow.should_escalate.return_value = Mock(should_escalate=False)
        return make_snapshot(pending_reworks=[rework]), {"rework_workflow": workflow}
    if queue == "validation_retry":
        retry = PendingValidationRetry(
            issue_number=7,
            issue_title="Retry",
            agent_label="agent:backend",
            worktree_path="/tmp/wt",
            branch_name="branch",
            original_prompt=None,
            validation_error="boom",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
        )
        return make_snapshot(pending_validation_retries=[retry]), {}
    if queue == "tech_lead":
        item = PendingTechLeadReview(
            issue_number=7,
            title="Health Review",
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        )
        return make_snapshot(pending_tech_lead=[item]), {}
    raise AssertionError(f"unknown queue {queue!r}")


_LAUNCH_FOR_QUEUE = {
    "coding": "issue",
    "review": "review",
    "retrospective_review": "retrospective-review",
    "rework": "rework",
    "validation_retry": "validation_retry",
    "tech_lead": "tech-lead",
}


def _planned_launch_kinds(actions) -> set[str]:
    from issue_orchestrator.control.actions import (
        LaunchSessionAction,
        LaunchValidationRetryAction,
    )

    kinds = {
        action.session_type.value
        for action in actions
        if isinstance(action, LaunchSessionAction)
    }
    if any(isinstance(action, LaunchValidationRetryAction) for action in actions):
        kinds.add("validation_retry")
    return kinds


def _sample_and_plan(config, manager, probe, workflows, snapshot):
    """Run the real tick order: sample readiness, then plan against the fact.

    Deliberately mirrors ``run_planning_cycle``: the sampler probes and feeds
    the circuit BEFORE planning, and the planner only reads the resulting fact.
    A planner that probed for itself would not be a pure function of its
    snapshot (#6999 F6/A3).
    """
    from dataclasses import replace

    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.provider_availability import (
        ProviderAvailabilityPolicy,
    )
    from issue_orchestrator.control.provider_launch_readiness import (
        ProviderLaunchReadinessSampler,
    )
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.workflows import TechLeadWorkflow

    sampler = ProviderLaunchReadinessSampler(
        config=config,
        policy=ProviderAvailabilityPolicy(
            config, manager, readiness_probe=probe
        ),
    )
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, RecordingEvents()),
        provider_resilience=manager,
        **workflows,
    )
    return planner.plan(replace(snapshot, provider_launch=sampler.sample()))


def _apply_impact_actions(actions, events) -> list[int]:
    """Apply every provider-impact command the plan produced.

    The plan is only half the story: the blocked label and the durable
    issue-scoped record are applied by the command, and that is where the
    user-visible event comes from.
    """
    from issue_orchestrator.control.actions import ActionResult
    from issue_orchestrator.control.provider_impact import (
        ApplyProviderImpactAction,
        apply_provider_impact,
    )

    labelled: list[int] = []

    def _apply_label(action):
        labelled.append(action.issue_number)
        return ActionResult.ok(action)

    for action in actions:
        if isinstance(action, ApplyProviderImpactAction):
            apply_provider_impact(
                action, apply_label=_apply_label, publish=events.publish
            )
    return labelled


@pytest.mark.parametrize("queue", sorted(_LAUNCH_FOR_QUEUE))
class TestPlanningReleasesTheAuthOutage:
    """Every planner queue must be able to observe re-authentication."""

    def test_open_auth_circuit_parks_the_queue_with_a_durable_record(
        self, queue, tmp_path
    ) -> None:
        """While the provider really is dead: no launch, and the issue is parked.

        Parking is not just an absent launch action. The issue gets the
        provider-impact transition — blocked label plus the issue-scoped
        record that survives the label being shed — so an operator can see why
        nothing happened (#6999 F6).
        """
        config = _recovery_config(tmp_path)
        manager = _manager(RecordingEvents())
        probe = _RecordingProbe(
            ProviderReadiness.auth_expired(PROVIDER, "not logged in")
        )
        snapshot, workflows = _queue_snapshot(queue)

        plan = _sample_and_plan(config, manager, probe, workflows, snapshot)

        assert _LAUNCH_FOR_QUEUE[queue] not in _planned_launch_kinds(plan.actions)
        assert manager.is_open(PROVIDER)
        applied_events = RecordingEvents()
        assert _apply_impact_actions(plan.actions, applied_events) == [7]
        assert (
            applied_events.names().count(EventName.PROVIDER_ISSUE_BLOCKED.value) == 1
        )

    def test_a_ready_probe_reopens_the_queue_before_the_cooldown(
        self, queue, tmp_path
    ) -> None:
        """The deadlock guard, on the real production path.

        The circuit is open on a six-hour auth cooldown and nothing has
        expired. The pre-planning sample still asks the provider, sees READY,
        and the launch flows — which is only possible because the sample is
        taken before the circuit is consulted (#6999 F1).
        """
        config = _recovery_config(tmp_path)
        manager = _manager(RecordingEvents(), auth_cooldown=21600)
        manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="outage"
        )
        assert manager.is_open(PROVIDER)
        probe = _RecordingProbe(ProviderReadiness.ready(PROVIDER))
        snapshot, workflows = _queue_snapshot(queue)

        plan = _sample_and_plan(config, manager, probe, workflows, snapshot)

        assert probe.launch_calls, "the tick never asked the provider"
        assert not manager.is_open(PROVIDER)
        assert _LAUNCH_FOR_QUEUE[queue] in _planned_launch_kinds(plan.actions)
        assert _apply_impact_actions(plan.actions, RecordingEvents()) == []


def test_planning_never_probes_or_writes_the_circuit(tmp_path: Path) -> None:
    """Planner purity: it is a pure function of its snapshot (#6999 F6/A3).

    Given a snapshot whose sampled fact already says the provider is fine, a
    plan must not touch the probe or the circuit — even with an unauthenticated
    provider sitting behind that probe.
    """
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.scheduler import Scheduler

    config = _recovery_config(tmp_path)
    events = RecordingEvents()
    manager = _manager(events)
    probe = _RecordingProbe(
        ProviderReadiness.auth_expired(PROVIDER, "not logged in")
    )
    snapshot, workflows = _queue_snapshot("coding")
    planner = Planner(
        config=config,
        scheduler=Scheduler(config),
        provider_resilience=manager,
        **workflows,
    )
    # Hand planning a policy that CAN probe, so a regression that reintroduces
    # sampling inside the planner is observable rather than silently inert.
    planner.provider_policy = ProviderAvailabilityPolicy(
        config, manager, readiness_probe=probe
    )

    plan = planner.plan(snapshot)

    assert probe.launch_calls == []
    assert manager.snapshot() == []
    assert events.names() == []
    assert "issue" in _planned_launch_kinds(plan.actions)


def test_a_test_composition_never_shells_out_to_a_provider_cli(tmp_path: Path) -> None:
    """The default test orchestrator must not depend on an installed CLI."""
    from unittest.mock import MagicMock

    from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.ports.provider_readiness import (
        StaticProviderReadinessProbe,
    )

    orchestrator = build_orchestrator_for_testing(
        Config(repo="test/repo", repo_root=tmp_path), github=MagicMock()
    )

    assert isinstance(
        orchestrator.deps.provider_readiness_probe, StaticProviderReadinessProbe
    )


# ---------------------------------------------------------------------------
# One physical probe sample = one circuit input (#6999 F2)
# ---------------------------------------------------------------------------


class _StepClock:
    """A monotonic clock that only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _logged_out_probe(clock: _StepClock) -> tuple[CLIProviderReadinessProbe, FakeCommandRunner]:
    runner = FakeCommandRunner(
        CommandResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
    )
    return (
        CLIProviderReadinessProbe(runner, ttl_seconds=60.0, clock=clock),
        runner,
    )


class TestOneSampleCountsOnce:
    """A configurable threshold must mean observations, not call sites."""

    def test_many_launch_checks_on_one_sample_count_once(self) -> None:
        """A tick gating five launches on one cached probe is ONE failure.

        Counting per call turned a single physical observation into N failures
        and blew through any ``auth_failure_threshold > 1`` immediately, which
        is exactly what the knob exists to prevent (#6999 F2).
        """
        events = RecordingEvents()
        manager = _manager(events, threshold=3)
        clock = _StepClock()
        probe, runner = _logged_out_probe(clock)
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=manager, readiness_probe=probe
        )

        for _ in range(5):
            policy.assess_launch("claude-code")

        assert len(runner.commands) == 1  # one physical probe...
        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 1  # ...counted once
        assert not manager.is_open("claude-code")  # threshold 3 not reached
        assert events.names().count(EventName.PROVIDER_AUTH_FAILED.value) == 1

    def test_distinct_samples_advance_the_threshold(self) -> None:
        """Genuinely new observations still march the circuit toward tripping."""
        events = RecordingEvents()
        manager = _manager(events, threshold=3)
        clock = _StepClock()
        probe, runner = _logged_out_probe(clock)
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=manager, readiness_probe=probe
        )

        for _ in range(3):
            policy.assess_launch("claude-code")
            policy.assess_launch("claude-code")  # same sample, must not count
            clock.advance(61.0)  # the cached sample expires => a NEW observation

        assert len(runner.commands) == 3
        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 3
        assert manager.is_open("claude-code")
        assert events.names().count(EventName.PROVIDER_AUTH_FAILED.value) == 3

    def test_a_fresh_process_does_not_collide_with_the_persisted_sample(
        self, tmp_path: Path
    ) -> None:
        """Restart: the first real observation after a reboot must still count.

        The circuit persists the last sample it counted, so sample identity has
        to be unique across process lifetimes. A per-process counter restarts at
        the same value every boot, collides with the stored id, and gets dropped
        as a replay — which with a threshold above 1 could stop the circuit ever
        tripping (#6999 F2).
        """
        from issue_orchestrator.execution.provider_circuit_store import (
            SQLiteProviderCircuitStore,
        )

        store = SQLiteProviderCircuitStore(tmp_path / "circuit.sqlite")

        def policy_over(store) -> ProviderAvailabilityPolicy:
            manager = ProviderResilienceManager(
                config=_resilience_config(threshold=3),
                store=store,
                events=RecordingEvents(),
            )
            probe, _runner = _logged_out_probe(_StepClock())
            return ProviderAvailabilityPolicy(
                config=_config(), provider_resilience=manager, readiness_probe=probe
            )

        # First process: one physical sample, deduplicated within itself.
        first = policy_over(store)
        first.assess_launch("claude-code")
        first.assess_launch("claude-code")
        assert store.get("claude-code").consecutive_auth_failures == 1

        # Second process, same database: a genuinely new sample.
        second = policy_over(SQLiteProviderCircuitStore(tmp_path / "circuit.sqlite"))
        second.assess_launch("claude-code")
        second.assess_launch("claude-code")

        state = store.get("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 2

    def test_a_live_session_death_reuses_its_confirming_sample(self) -> None:
        """The session's verdict came from the same probe result, so it counts once."""
        from issue_orchestrator.control.session_decision import ProviderAuthOutcome

        events = RecordingEvents()
        manager = _manager(events, threshold=3)
        clock = _StepClock()
        probe, _runner = _logged_out_probe(clock)
        policy = ProviderAvailabilityPolicy(
            config=_config(), provider_resilience=manager, readiness_probe=probe
        )
        policy.assess_launch("claude-code")

        diagnosis = probe.diagnose_session_output("claude-code", EXPIRED_LOGIN_BANNER)
        auth_failure = ProviderAuthOutcome.from_readiness(diagnosis)
        manager.record_auth_failure(
            auth_failure.provider,
            error_summary=auth_failure.detail,
            sample_id=auth_failure.sample_id,
        )

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 1


class TestAuthCircuitSettingsRoundTrip:
    """The two new knobs must survive YAML in both directions."""

    def test_yaml_values_reach_the_circuit_config(self) -> None:
        from issue_orchestrator.infra.config_sections import (
            parse_provider_resilience_config,
        )

        parsed = parse_provider_resilience_config(
            {
                "circuit_breaker": {
                    "auth_failure_threshold": 4,
                    "auth_cooldown_seconds": 900,
                }
            }
        )

        assert parsed.circuit_breaker.auth_failure_threshold == 4
        assert parsed.circuit_breaker.auth_cooldown_seconds == 900

    def test_defaults_are_one_confirmed_failure_and_six_hours(self) -> None:
        from issue_orchestrator.infra.config_sections import (
            parse_provider_resilience_config,
        )

        parsed = parse_provider_resilience_config({})

        assert parsed.circuit_breaker.auth_failure_threshold == 1
        assert parsed.circuit_breaker.auth_cooldown_seconds == 21600

    def test_non_default_values_serialize_back_out(self) -> None:
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.infra.config_models import (
            ProviderCircuitBreakerConfig,
            ProviderResilienceConfig,
        )

        config = Config(repo="test/repo", repo_root=Path("/tmp/does-not-matter"))
        config.provider_resilience = ProviderResilienceConfig(
            circuit_breaker=ProviderCircuitBreakerConfig(
                auth_failure_threshold=4, auth_cooldown_seconds=900
            )
        )

        circuit = config.to_dict()["provider_resilience"]["circuit_breaker"]

        assert circuit["auth_failure_threshold"] == 4
        assert circuit["auth_cooldown_seconds"] == 900

    def test_default_values_stay_out_of_serialized_yaml(self) -> None:
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo", repo_root=Path("/tmp/does-not-matter"))

        assert "provider_resilience" not in config.to_dict()


# ---------------------------------------------------------------------------
# Auth and transient outages are independent causes (#6999 F3)
# ---------------------------------------------------------------------------


class TestIndependentOutageCauses:
    """A credential probe is evidence about credentials and nothing else."""

    def test_auth_recovery_does_not_release_a_live_transient_outage(self) -> None:
        """The provider is still 503ing; re-authenticating must not unpark it."""
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_transient_failure(
            "claude-code", error_summary="503", now=start
        )
        transient_state = manager.get_state("claude-code")
        assert transient_state is not None
        transient_deadline = transient_state.transient_open_until
        assert transient_deadline is not None
        manager.record_auth_failure(
            "claude-code",
            error_summary="not logged in",
            sample_id="s1",
            now=start + timedelta(seconds=1),
        )

        manager.clear_auth_failures("claude-code", now=start + timedelta(seconds=2))

        just_before = transient_deadline - timedelta(seconds=1)
        assert manager.is_open("claude-code", just_before)
        assert not manager.is_open(
            "claude-code", transient_deadline + timedelta(seconds=1)
        )
        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 0
        assert state.auth_open_until is None
        assert state.transient_open_until == transient_deadline

    def test_no_recovery_is_announced_while_the_provider_is_still_down(self) -> None:
        """``provider.outage_exited`` describes the aggregate, not one half."""
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_transient_failure("claude-code", error_summary="503", now=start)
        manager.record_auth_failure(
            "claude-code",
            error_summary="not logged in",
            sample_id="s1",
            now=start + timedelta(seconds=1),
        )

        manager.clear_auth_failures("claude-code", now=start + timedelta(seconds=2))

        assert EventName.PROVIDER_OUTAGE_EXITED.value not in events.names()

    def test_recovery_is_announced_once_the_last_cause_is_gone(self) -> None:
        """With no transient outage in play, auth recovery IS the recovery."""
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1", now=start
        )

        manager.clear_auth_failures("claude-code", now=start + timedelta(seconds=1))

        assert events.names().count(EventName.PROVIDER_OUTAGE_EXITED.value) == 1

    def test_a_ready_probe_cannot_launch_work_into_a_transient_outage(self) -> None:
        """End to end through the assessment: healthy credentials are not enough."""
        manager = _manager(RecordingEvents())
        manager.record_transient_failure("claude-code", error_summary="503")
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )
        policy = ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=manager,
            readiness_probe=StubReadinessProbe(ProviderReadiness.ready("claude-code")),
        )

        outcome = policy.assess_launch("claude-code")

        assert not outcome.blocked_by_readiness  # credentials are fine...
        assert outcome.circuit_open  # ...but the service outage still holds
        assert not outcome.may_launch

    def test_a_transient_failure_leaves_the_auth_deadline_alone(self) -> None:
        manager = _manager(RecordingEvents(), auth_cooldown=21600)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1", now=start
        )

        manager.record_transient_failure(
            "claude-code", error_summary="503", now=start + timedelta(seconds=1)
        )

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.auth_open_until == start + timedelta(seconds=21600)
        assert state.open_until == start + timedelta(seconds=21600)


def test_split_deadlines_survive_a_single_open_until_database(tmp_path: Path) -> None:
    """A store written against the one-deadline schema opens and reads as transient."""
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
            ('claude-code', '2026-08-04T23:00:00+00:00', 2, 'boom',
             '2026-08-04T22:00:00+00:00');
        """
    )
    legacy.commit()
    legacy.close()

    store = SQLiteProviderCircuitStore(db_path)
    state = store.get("claude-code")

    assert state is not None
    assert state.transient_open_until == datetime(
        2026, 8, 4, 23, 0, tzinfo=timezone.utc
    )
    assert state.auth_open_until is None
    assert state.open_until == state.transient_open_until


# ---------------------------------------------------------------------------
# Diagnosis is not gated on when we happened to look (#6999 F4)
# ---------------------------------------------------------------------------


class TestAuthDiagnosisOutranksTimeAndTimeout:
    """A session-age or timeout ordering rule re-opens the 90-minute burn."""

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

    def _auth_dead_session(self, make_session, *, age_seconds: float):
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
            json.dumps({"kind": "output", "data": EXPIRED_LOGIN_BANNER}) + "\n",
            encoding="utf-8",
        )
        session.started_at = datetime.now() - timedelta(seconds=age_seconds)
        return session

    @pytest.mark.parametrize(
        "age_seconds",
        [
            10,  # observed immediately, as in the happy path
            6 * 60,  # first observation delayed past the old five-minute window
            3 * 60 * 60,  # orchestrator restarted hours into the session
        ],
    )
    def test_a_late_first_observation_still_diagnoses_the_auth_failure(
        self, sample_config, make_session, age_seconds
    ) -> None:
        """The head of the log belongs to THIS launch however late we read it.

        A restart or a delayed first tick used to skip the check entirely and
        let the session burn to its full timeout (#6999 F4).
        """
        probe = StubReadinessProbe(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )
        observer = self._observer(sample_config, probe)
        session = self._auth_dead_session(make_session, age_seconds=age_seconds)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED
        assert probe.diagnose_calls == ["claude-code"]

    def test_an_auth_dead_session_past_its_timeout_is_not_timed_out(
        self, sample_config, make_session
    ) -> None:
        """The credential outage is the cause; TIMED_OUT would mint an investigation."""
        sample_config.session_timeout_minutes = 1
        probe = StubReadinessProbe(
            ProviderReadiness.auth_expired("claude-code", "not logged in")
        )
        observer = self._observer(sample_config, probe)
        session = self._auth_dead_session(make_session, age_seconds=90 * 60)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED
        assert result.observation is not SessionObservation.TIMED_OUT

    def test_a_timed_out_session_without_an_auth_failure_still_times_out(
        self, sample_config, make_session
    ) -> None:
        """The reordering is scoped to confirmed auth failures, nothing wider."""
        sample_config.session_timeout_minutes = 1
        probe = StubReadinessProbe(
            ProviderReadiness.unknown("claude-code", "not confirmed")
        )
        observer = self._observer(sample_config, probe)
        session = self._auth_dead_session(make_session, age_seconds=90 * 60)

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.TIMED_OUT


# ---------------------------------------------------------------------------
# The issue-impact owner is not bypassed, and the story is told once (#6999 F5)
# ---------------------------------------------------------------------------


class TestLiveAuthFailureRoutesThroughTheImpactOwner:
    """The provider-blocked label and its durable record are one transition."""

    def _planner(self, config, manager):
        from issue_orchestrator.control.completion_action_planner import (
            CompletionActionPlanner,
        )
        from issue_orchestrator.control.label_manager import LabelManager
        from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
        from issue_orchestrator.control.provider_availability import (
            ProviderAvailabilityPolicy,
        )
        from issue_orchestrator.ports.open_issue_corpus_store import (
            InMemoryOpenIssueCorpusStore,
        )
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        class _NoRepositoryReads:
            def get_prs_for_branch(self, branch):
                return []

            def get_issue(self, issue_number):
                return None

        host = _NoRepositoryReads()
        return CompletionActionPlanner(
            config,
            host,
            LabelManager(config),
            InMemoryTechLeadAuthorityStore(),
            OpenIssueCorpusManager(
                host, InMemoryOpenIssueCorpusStore(), is_enabled=lambda: False
            ),
            lambda _n: None,
            ProviderAvailabilityPolicy(config, manager, LabelManager(config)),
        )

    def _session(self, make_session, terminal_id: str):
        from issue_orchestrator.infra.config import AgentConfig

        session = make_session()
        session.agent_config = AgentConfig(
            prompt_path=session.agent_config.prompt_path, provider="claude-code"
        )
        session.terminal_id = terminal_id
        return session

    @pytest.mark.parametrize(
        "terminal_id", ["issue-123", "review-123", "rework-123"]
    )
    def test_every_session_kind_records_the_provider_impact(
        self, sample_config, make_session, terminal_id
    ) -> None:
        """A dead credential impacts the issue whichever session hit it."""
        from issue_orchestrator.control.actions import AddLabelAction
        from issue_orchestrator.control.provider_impact import (
            ApplyProviderImpactAction,
        )

        manager = _manager(RecordingEvents())
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )
        planner = self._planner(sample_config, manager)

        actions = planner.generate_completion_actions(
            self._session(make_session, terminal_id),
            SessionStatus.BLOCKED,
            blocked_reason="not logged in",
            provider_error_type=ProviderErrorType.AUTH,
        )

        impacts = [a for a in actions if isinstance(a, ApplyProviderImpactAction)]
        assert len(impacts) == 1
        assert impacts[0].assessment.open_providers == ("claude-code",)
        # ...and never as a bare label mutation that would strand the history.
        assert not [a for a in actions if isinstance(a, AddLabelAction)]

    def test_an_issue_session_still_releases_its_claim(
        self, sample_config, make_session
    ) -> None:
        from issue_orchestrator.control.actions import RemoveLabelAction
        from issue_orchestrator.control.label_manager import LabelManager

        manager = _manager(RecordingEvents())
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )

        actions = self._planner(sample_config, manager).generate_completion_actions(
            self._session(make_session, "issue-123"),
            SessionStatus.BLOCKED,
            provider_error_type=ProviderErrorType.AUTH,
        )

        removed = {
            a.label for a in actions if isinstance(a, RemoveLabelAction)
        }
        assert LabelManager(sample_config).in_progress in removed

    def test_a_transient_provider_block_takes_the_same_route(
        self, sample_config, make_session
    ) -> None:
        """The rule is about provider causes, not about which one (#5980 F1)."""
        from issue_orchestrator.control.actions import AddLabelAction
        from issue_orchestrator.control.provider_impact import (
            ApplyProviderImpactAction,
        )

        manager = _manager(RecordingEvents())
        manager.record_transient_failure("claude-code", error_summary="503")

        actions = self._planner(sample_config, manager).generate_completion_actions(
            self._session(make_session, "issue-123"),
            SessionStatus.BLOCKED,
            provider_error_type=ProviderErrorType.TRANSIENT,
        )

        assert any(isinstance(a, ApplyProviderImpactAction) for a in actions)
        assert not [a for a in actions if isinstance(a, AddLabelAction)]

    def test_an_ordinary_agent_block_keeps_the_generic_route(
        self, sample_config, make_session
    ) -> None:
        """Only a typed provider verdict diverts; agent-reported blocks are untouched."""
        from issue_orchestrator.control.actions import AddLabelAction
        from issue_orchestrator.control.provider_impact import (
            ApplyProviderImpactAction,
        )

        actions = self._planner(
            sample_config, _manager(RecordingEvents())
        ).generate_completion_actions(
            self._session(make_session, "issue-123"),
            SessionStatus.BLOCKED,
            blocked_label="blocked:needs-human",
            blocked_reason="I cannot find the spec",
        )

        assert any(isinstance(a, AddLabelAction) for a in actions)
        assert not [a for a in actions if isinstance(a, ApplyProviderImpactAction)]

    def test_the_impact_command_applies_the_label_and_records_the_outage(
        self, sample_config, make_session
    ) -> None:
        """Command to label/event: one apply moves both halves together."""
        from issue_orchestrator.control.provider_impact import (
            ApplyProviderImpactAction,
        )

        manager = _manager(RecordingEvents())
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )
        actions = self._planner(sample_config, manager).generate_completion_actions(
            self._session(make_session, "issue-123"),
            SessionStatus.BLOCKED,
            provider_error_type=ProviderErrorType.AUTH,
        )
        [impact] = [a for a in actions if isinstance(a, ApplyProviderImpactAction)]
        from issue_orchestrator.control.actions import ActionResult
        from issue_orchestrator.control.label_manager import LabelManager
        from issue_orchestrator.control.provider_impact import apply_provider_impact

        events = RecordingEvents()
        applied: list[tuple[int, str]] = []

        def _apply_label(action):
            applied.append((action.issue_number, action.label))
            return ActionResult.ok(action)

        result = apply_provider_impact(
            impact, apply_label=_apply_label, publish=events.publish
        )

        assert result.success
        assert applied == [
            (123, LabelManager(sample_config).provider_unavailable)
        ]
        assert EventName.PROVIDER_ISSUE_BLOCKED.value in events.names()


class TestLiveAuthEventIsToldOnce:
    """Two publishers meant the same failure appeared twice, worded wrongly."""

    def test_the_observer_publishes_nothing(
        self, sample_config, make_session
    ) -> None:
        from issue_orchestrator.infra.config import AgentConfig
        from issue_orchestrator.infra.terminal_recording import (
            TERMINAL_RECORDING_FILENAME,
        )
        from issue_orchestrator.observation.observer import SessionObserver

        class _AlwaysRunning:
            def session_exists_by_name(self, name: str) -> bool:
                return True

            def send_to_session_by_name(self, name: str, text: str) -> bool:
                return True

            def get_session_output(self, issue_number, lines=100, session_name=None):
                return ""

        events = RecordingEvents()
        session = make_session()
        session.agent_config = AgentConfig(
            prompt_path=session.agent_config.prompt_path, provider="claude-code"
        )
        recording = session.run_assets.run_dir / TERMINAL_RECORDING_FILENAME
        recording.parent.mkdir(parents=True, exist_ok=True)
        recording.write_text(
            json.dumps({"kind": "output", "data": EXPIRED_LOGIN_BANNER}) + "\n",
            encoding="utf-8",
        )
        observer = SessionObserver(
            config=sample_config,
            session_output=FileSystemSessionOutput(),
            events=events,
            session_runner=_AlwaysRunning(),
            provider_readiness_probe=StubReadinessProbe(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
        )

        observer.observe_session(session)

        assert EventName.SESSION_PROVIDER_AUTH_TERMINATED.value not in events.names()
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in events.names()

    def test_the_controller_announces_a_termination_not_a_parked_launch(
        self, tmp_path: Path
    ) -> None:
        """A session that DID launch must not be reported as a parked launch."""
        events = RecordingEvents()
        controller = SessionController(
            completion_processor=MockCompletionProcessor(),
            events=events,
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        decide_with_run_assets(
            controller,
            observation=SessionObservationResult.provider_auth_failed(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
            worktree_path=tmp_path / "worktree",
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        names = events.names()
        assert names.count(EventName.SESSION_PROVIDER_AUTH_TERMINATED.value) == 1
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in names

    def test_the_launch_gate_still_owns_the_parked_launch_story(self) -> None:
        """The two concepts stay distinct: nothing ran, versus something was stopped."""
        from issue_orchestrator.control.provider_launch_gate import ProviderLaunchGate

        events = RecordingEvents()
        gate = ProviderLaunchGate(
            policy=ProviderAvailabilityPolicy(
                config=_config(),
                provider_resilience=_manager(RecordingEvents()),
                readiness_probe=StubReadinessProbe(
                    ProviderReadiness.auth_expired("claude-code", "not logged in")
                ),
            ),
            events=events,
            apply_actions=lambda actions, context: True,
        )

        gate.check("claude-code", 123)

        names = events.names()
        assert names.count(EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value) == 1
        assert EventName.SESSION_PROVIDER_AUTH_TERMINATED.value not in names


class TestAuthVerdictNeverDiscardsFinishedWork:
    """Removing the age cutoff must not let a late verdict strand a record."""

    def test_a_completion_record_outranks_the_auth_verdict(
        self, tmp_path: Path
    ) -> None:
        """completion.json is the agent's reported intent and it finished.

        An auth outage that becomes visible *after* the work was written says
        nothing about that work. Discarding it would be the stranded-work
        failure mode, traded for the burn this issue removes.
        """
        from issue_orchestrator.domain.models import CompletionOutcome

        from tests.unit.test_session_controller import make_record

        processor = MockCompletionProcessor()
        processor.completion_record = make_record(
            CompletionOutcome.COMPLETED, implementation="did the work"
        )
        controller = SessionController(
            completion_processor=processor,
            events=RecordingEvents(),
            session_output=FileSystemSessionOutput(),
            working_copy=StubWorkingCopy(),
        )

        decision = decide_with_run_assets(
            controller,
            observation=SessionObservationResult.provider_auth_failed(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
            worktree_path=tmp_path / "worktree",
            issue_number=123,
            issue_title="Test Issue",
            session_name="issue-123",
        )

        assert decision.status is SessionStatus.COMPLETED
        assert decision.completion_processed
        assert decision.provider_auth_failure is None


class TestAuthCircuitSettingsAreValidatedAtStartup:
    """Raw YAML bypasses the settings schema, so startup must re-check."""

    def _config_with(self, **circuit):
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.infra.config_models import (
            ProviderCircuitBreakerConfig,
            ProviderResilienceConfig,
        )

        config = Config(repo="test/repo", repo_root=Path("/tmp/does-not-matter"))
        config.provider_resilience = ProviderResilienceConfig(
            circuit_breaker=ProviderCircuitBreakerConfig(**circuit)
        )
        return config

    @pytest.mark.parametrize("threshold", [0, -1, 11])
    def test_out_of_range_threshold_is_rejected(self, threshold: int) -> None:
        errors = self._config_with(auth_failure_threshold=threshold).validate()

        assert any("auth_failure_threshold" in error for error in errors)

    @pytest.mark.parametrize("cooldown", [0, -1, 59, 604801])
    def test_out_of_range_cooldown_is_rejected(self, cooldown: int) -> None:
        """A zero/negative cooldown yields an already-expired auth deadline.

        The circuit would then stop protecting the fleet the instant it opened,
        which is the long-burn behaviour this issue exists to remove (#6999 F7).
        """
        errors = self._config_with(auth_cooldown_seconds=cooldown).validate()

        assert any("auth_cooldown_seconds" in error for error in errors)

    @pytest.mark.parametrize("threshold,cooldown", [(1, 60), (10, 604800), (3, 21600)])
    def test_in_range_values_are_accepted(self, threshold: int, cooldown: int) -> None:
        errors = self._config_with(
            auth_failure_threshold=threshold, auth_cooldown_seconds=cooldown
        ).validate()

        assert not [e for e in errors if "auth_" in e]

    def test_yaml_out_of_range_fails_the_normal_load_path(self, tmp_path: Path) -> None:
        """The values arrive as raw YAML, so the check must be on that path."""
        from issue_orchestrator.infra.config import Config

        config_path = tmp_path / ".issue-orchestrator" / "config" / "default.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "repo:\n  name: owner/repo\n"
            "provider_resilience:\n"
            "  circuit_breaker:\n"
            "    auth_failure_threshold: 0\n"
            "    auth_cooldown_seconds: 0\n",
            encoding="utf-8",
        )

        config = Config.load(config_path)

        assert config.provider_resilience.circuit_breaker.auth_failure_threshold == 0
        errors = config.validate()
        assert any("auth_failure_threshold" in e for e in errors)
        assert any("auth_cooldown_seconds" in e for e in errors)

    def test_the_circuit_owner_no_longer_clamps_a_bad_threshold(self) -> None:
        """Fail-fast: the config gate owns the range, not a silent max(1, ...)."""
        manager = ProviderResilienceManager(
            config=_resilience_config(threshold=2),
            store=InMemoryProviderCircuitStore(),
            events=RecordingEvents(),
        )

        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1"
        )

        assert not manager.is_open("claude-code")  # honours the configured 2


# Every way a PROVIDER_AUTH_FAILED observation can be malformed. Built by
# DIRECT dataclass construction, not through the convenience factory: the
# invariant belongs to the type, and a regression that moved it back into the
# factory would reopen the bypass while leaving factory-only tests green.
_MALFORMED_READINESS = {
    "missing": None,
    "ready": ProviderReadiness.ready("claude-code"),
    "unknown": ProviderReadiness.unknown("claude-code", "probe could not run"),
    "unnamed": ProviderReadiness.auth_expired("", "not logged in"),
}


class TestMalformedAuthObservationFailsLoudly:
    """A partial auth outcome would end a session with the outage unrecorded."""

    @pytest.mark.parametrize("variant", sorted(_MALFORMED_READINESS))
    def test_direct_construction_rejects_a_malformed_readiness(
        self, variant: str
    ) -> None:
        with pytest.raises(ValueError, match="auth-expired"):
            SessionObservationResult(
                observation=SessionObservation.PROVIDER_AUTH_FAILED,
                session_exists=True,
                provider_readiness=_MALFORMED_READINESS[variant],
            )

    def test_direct_construction_accepts_a_named_auth_expired_readiness(self) -> None:
        """The invariant is a guard, not a ban: the well-formed case still builds."""
        observation = SessionObservationResult(
            observation=SessionObservation.PROVIDER_AUTH_FAILED,
            session_exists=True,
            provider_readiness=ProviderReadiness.auth_expired(
                "claude-code", "not logged in"
            ),
        )

        assert observation.provider_readiness is not None
        assert observation.provider_readiness.provider == "claude-code"
        assert observation.is_terminal

    def test_other_observations_are_unaffected_by_the_invariant(self) -> None:
        """Only PROVIDER_AUTH_FAILED carries the requirement."""
        assert (
            SessionObservationResult(
                observation=SessionObservation.RUNNING, session_exists=True
            ).provider_readiness
            is None
        )

    def test_the_convenience_factory_inherits_the_same_guard(self) -> None:
        with pytest.raises(ValueError, match="auth-expired"):
            SessionObservationResult.provider_auth_failed(
                ProviderReadiness.ready("claude-code")
            )

    @pytest.mark.parametrize("variant", sorted(_MALFORMED_READINESS))
    def test_the_consumer_boundary_also_refuses_a_malformed_readiness(
        self, variant: str
    ) -> None:
        """Separate coverage: an observation built by any other means still fails.

        The controller converts this into a circuit write and a provider-impact
        route, so it must not accept a value the observation type would reject.
        """
        from issue_orchestrator.control.session_decision import ProviderAuthOutcome

        with pytest.raises(ValueError, match="auth-expired"):
            ProviderAuthOutcome.from_readiness(_MALFORMED_READINESS[variant])

    def test_a_well_formed_observation_still_reaches_the_circuit_owner(self) -> None:
        """The happy path is unchanged: provider, detail and sample all carried."""
        from issue_orchestrator.control.session_decision import ProviderAuthOutcome

        readiness = ProviderReadiness(
            provider="claude-code",
            state=ProviderReadinessState.AUTH_EXPIRED,
            detail="not logged in",
            sample_id="sample-1",
        )

        decision = ProviderAuthOutcome.from_readiness(readiness).as_decision()

        assert decision.provider_auth_failure is not None
        assert decision.provider_auth_failure.provider == "claude-code"
        assert decision.provider_auth_failure.sample_id == "sample-1"


# ---------------------------------------------------------------------------
# The production tick boundary (#6999 F6)
#
# Everything above tests a seam. This exercises the real chain a running
# orchestrator uses — run_planning_cycle -> sampler -> FactGatherer -> Planner
# -> ActionApplier -> SessionLauncher/ProviderLaunchGate — so a regression that
# forgets to carry the sample into the snapshot, or plans an action nothing
# applies, cannot pass.
# ---------------------------------------------------------------------------


class _ProductionTick:
    """One real planning cycle over a real applier and a real launcher."""

    def __init__(
        self,
        tmp_path: Path,
        readiness: ProviderReadiness,
        *,
        threshold: int = 1,
        auth_cooldown: int = 21600,
    ) -> None:
        from unittest.mock import MagicMock

        from issue_orchestrator.control.action_applier import ActionApplier
        from issue_orchestrator.control.fact_gatherer import FactGatherer
        from issue_orchestrator.control.label_manager import LabelManager
        from issue_orchestrator.control.planner import Planner
        from issue_orchestrator.control.provider_availability import (
            ProviderAvailabilityPolicy,
        )
        from issue_orchestrator.control.provider_launch_readiness import (
            ProviderLaunchReadinessSampler,
        )
        from issue_orchestrator.control.scheduler import Scheduler
        from issue_orchestrator.control.session_manager import SessionType
        from issue_orchestrator.domain.models import OrchestratorState
        from tests.conftest import MockGitHubAdapter
        from tests.unit.test_planner import make_issue

        self.config = _recovery_config(tmp_path)
        # No fetch this tick: the seeded queue IS the queue, so label mutations
        # applied by the real applier are what the next tick sees.
        self.config.fetch_layer_network_sync_seconds = 3600
        self.events = RecordingEvents()
        self.manager = ProviderResilienceManager(
            config=_resilience_config(threshold=threshold, auth_cooldown=auth_cooldown),
            store=InMemoryProviderCircuitStore(),
            events=self.events,
        )
        self.probe = _RecordingProbe(readiness)
        self.labels = LabelManager(self.config)

        self.github = MockGitHubAdapter()
        self.github.issues = [make_issue(7, labels=["agent:backend"])]

        self.launched: list[int] = []
        self.launcher = _LauncherHarness(
            tmp_path,
            self.probe,
            manager=self.manager,
            events=self.events,
            config=self.config,
        )

        def _launch(session_type, number):
            if session_type is not SessionType.ISSUE:
                return None
            issue = self.github.get_issue(number)
            result = self.launcher.launch_issue(issue)
            if result.success:
                self.launched.append(number)
            return result.session

        self.applier = ActionApplier(
            labels=self.github,
            sessions=MagicMock(),
            events=self.events,
            repository_host=self.github,
            label_manager=self.labels,
            session_launcher=_launch,
        )
        self.sampler = ProviderLaunchReadinessSampler(
            config=self.config,
            policy=ProviderAvailabilityPolicy(
                self.config, self.manager, self.labels, readiness_probe=self.probe
            ),
        )
        self.fact_gatherer = FactGatherer(
            config=self.config, repository_host=self.github, events=self.events
        )
        self.planner = Planner(
            config=self.config,
            scheduler=Scheduler(self.config),
            provider_resilience=self.manager,
            label_manager=self.labels,
        )
        self.state = OrchestratorState()
        self.state.cached_queue_issues = self.github.issues
        self.state.cached_scope_issues = self.github.issues

    def tick(self) -> None:
        import time
        from unittest.mock import Mock

        from issue_orchestrator.control.orchestrator_support import (
            IssueFetchResilience,
            run_planning_cycle,
        )

        run_planning_cycle(
            config=self.config,
            events=self.events,
            event_context=Mock(enrich=lambda payload: payload),
            state=self.state,
            fact_gatherer=self.fact_gatherer,
            planner=self.planner,
            repository_host=self.github,
            scheduler=Mock(),
            github_workflow=Mock(),
            apply_plan_fn=self._apply,
            clear_discovered_facts_fn=Mock(),
            last_network_sync=time.time(),
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            provider_launch_sampler=self.sampler,
        )

    def _apply(self, plan) -> None:
        for action in plan.actions:
            self.applier.apply(action)

    def issue_labels(self) -> set[str]:
        return set(self.github.get_issue_labels(7))

    def event_names(self) -> list[str]:
        return self.events.names()


class TestTheProductionTickExplainsEveryProviderRefusal:
    """Every non-launchable sample must leave one issue-scoped consequence."""

    def test_a_sub_threshold_auth_sample_is_refused_at_the_launch_gate(
        self, tmp_path: Path
    ) -> None:
        """Threshold 2: the first sample does not open the circuit.

        Planning must NOT silently suppress the work here — the provider-impact
        command has no open circuit to record, so the issue would be dropped
        with nothing on it to explain why. The launch gate owns this case and
        says so per issue (#6999 F6).
        """
        tick = _ProductionTick(
            tmp_path,
            ProviderReadiness.auth_expired(PROVIDER, "not logged in"),
            threshold=2,
        )

        tick.tick()

        assert not tick.manager.is_open(PROVIDER)  # sub-threshold
        assert tick.launched == []  # ...but nothing was spawned
        names = tick.event_names()
        assert names.count(EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value) == 1
        assert tick.labels.provider_unavailable not in tick.issue_labels()

    def test_a_not_installed_provider_is_refused_without_an_auth_failure(
        self, tmp_path: Path
    ) -> None:
        """A missing CLI is not a credential problem and must not be counted as one."""
        tick = _ProductionTick(
            tmp_path, ProviderReadiness.not_installed(PROVIDER, "claude not on PATH")
        )

        tick.tick()

        assert tick.launched == []
        assert tick.manager.get_state(PROVIDER) is None  # no auth failure recorded
        assert EventName.PROVIDER_AUTH_FAILED.value not in tick.event_names()
        assert (
            tick.event_names().count(EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value)
            == 1
        )

    def test_an_open_circuit_parks_the_issue_once_across_repeated_ticks(
        self, tmp_path: Path
    ) -> None:
        """Two parked ticks, one issue-scoped event and one label.

        The second tick re-plans against the label the first tick applied, so
        the impact command's mutation is a no-op and records nothing further.
        Anything else would re-announce the same outage every tick forever.
        """
        tick = _ProductionTick(
            tmp_path, ProviderReadiness.auth_expired(PROVIDER, "not logged in")
        )

        tick.tick()
        tick.tick()

        assert tick.manager.is_open(PROVIDER)
        assert tick.launched == []
        assert tick.labels.provider_unavailable in tick.issue_labels()
        names = tick.event_names()
        # Both ticks really ran a full cycle — otherwise "exactly once" would be
        # satisfied by the second tick doing nothing at all.
        assert names.count(EventName.PLAN_COMPUTED.value) == 2
        assert names.count(EventName.PROVIDER_ISSUE_BLOCKED.value) == 1
        # Planning parked the work, so the launch gate was never reached.
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in names

    def test_a_ready_probe_reaches_the_launcher_before_the_cooldown(
        self, tmp_path: Path
    ) -> None:
        """Recovery, end to end: the session actually starts.

        The circuit is open on a six-hour auth cooldown with nothing expired.
        The tick's sample sees READY, the circuit closes, planning queues the
        launch, the applier calls the launcher, and the gate lets it through.
        """
        tick = _ProductionTick(
            tmp_path, ProviderReadiness.ready(PROVIDER), auth_cooldown=21600
        )
        tick.manager.record_auth_failure(
            PROVIDER, error_summary="not logged in", sample_id="earlier-outage"
        )
        assert tick.manager.is_open(PROVIDER)

        tick.tick()

        assert not tick.manager.is_open(PROVIDER)
        assert tick.launched == [7]
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in tick.event_names()

    def test_a_healthy_provider_launches_without_any_provider_event(
        self, tmp_path: Path
    ) -> None:
        """The gate is scoped to refusals; it blocks and announces nothing else."""
        tick = _ProductionTick(tmp_path, ProviderReadiness.ready(PROVIDER))

        tick.tick()

        assert tick.launched == [7]
        names = tick.event_names()
        assert EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value not in names
        assert EventName.PROVIDER_ISSUE_BLOCKED.value not in names


# ---------------------------------------------------------------------------
# A provider refusal must not consume the pending work (#6999 F10 / A1)
# ---------------------------------------------------------------------------


def _routing_config(tmp_path: Path):
    config = _recovery_config(tmp_path)
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


class _RefusingLauncherHarness(_LauncherHarness):
    """A real SessionLauncher whose provider gate refuses every launch."""

    def __init__(self, tmp_path: Path, readiness: ProviderReadiness, *, threshold: int):
        events = RecordingEvents()
        manager = ProviderResilienceManager(
            config=_resilience_config(threshold=threshold),
            store=InMemoryProviderCircuitStore(),
            events=events,
        )
        self.probe = _RecordingProbe(readiness)
        super().__init__(
            tmp_path,
            self.probe,
            manager=manager,
            events=events,
            config=_routing_config(tmp_path),
        )
        self.manager = manager


def _pending_state(queue: str):
    """Orchestrator state holding exactly one pending item on ``queue``."""
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.models import (
        OrchestratorState,
        PendingRetrospectiveReview,
        PendingReview,
        PendingRework,
        PendingTechLeadReview,
        PendingValidationRetry,
    )
    from issue_orchestrator.domain.session_key import TaskKind
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    state = OrchestratorState()
    issue_key = FakeIssueKey(name="7")
    if queue == "review":
        state.pending_reviews.append(
            PendingReview(
                issue_key=issue_key,
                pr_number=70,
                pr_url="url",
                branch_name="branch",
                _issue_number=7,
                agent_label="agent:backend",
            )
        )
    elif queue == "retrospective_review":
        state.pending_retrospective_reviews.append(
            PendingRetrospectiveReview(
                issue_key=issue_key,
                issue_number=7,
                issue_title="Retro",
                agent_label="agent:backend",
                trigger_label="review-first",
            )
        )
    elif queue == "rework":
        state.pending_reworks.append(
            PendingRework(
                issue_key=issue_key, agent_type="agent:backend", issue_number=7
            )
        )
    elif queue == "validation_retry":
        state.pending_validation_retries.append(
            PendingValidationRetry(
                issue_number=7,
                issue_title="Retry",
                agent_label="agent:backend",
                worktree_path="/tmp/wt",
                branch_name="branch",
                original_prompt=None,
                validation_error="boom",
                validation_error_file=None,
                retry_count=1,
                source_task=TaskKind.CODE,
            )
        )
    elif queue == "tech_lead":
        from issue_orchestrator.domain.models import DiscoveredFailure

        state.pending_tech_lead_reviews.append(
            PendingTechLeadReview(
                issue_number=7,
                title="Investigate: session failed",
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=DiscoveredFailure(
                    7, "Test Issue", "failed", blocking_label="blocked-failed"
                ),
            )
        )
    else:
        raise AssertionError(f"unknown queue {queue!r}")
    return state


def _pending_count(state, queue: str) -> int:
    return len(
        {
            "review": state.pending_reviews,
            "retrospective_review": state.pending_retrospective_reviews,
            "rework": state.pending_reworks,
            "validation_retry": state.pending_validation_retries,
            "tech_lead": state.pending_tech_lead_reviews,
        }[queue]
    )


def _route(queue: str, state, harness):
    """Drive the production routing function that owns ``queue``."""
    from unittest.mock import MagicMock

    from issue_orchestrator.control import session_routing

    restorer = MagicMock()
    restorer.restore_session.return_value = None
    if queue == "review":
        return session_routing.orchestrator_launch_review_session(
            state.pending_reviews[0], state, harness.launcher, restorer
        )
    if queue == "retrospective_review":
        return session_routing.orchestrator_launch_retrospective_review_session(
            state.pending_retrospective_reviews[0], state, harness.launcher, restorer
        )
    if queue == "rework":
        return session_routing.orchestrator_launch_rework_session(
            state.pending_reworks[0], state, harness.launcher, restorer
        )
    if queue == "validation_retry":
        return session_routing.orchestrator_launch_validation_retry_session(
            state.pending_validation_retries[0], state, harness.launcher, restorer
        )
    if queue == "tech_lead":
        return session_routing.orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            harness.launcher.config,
            harness.launcher,
            restorer,
        )
    raise AssertionError(f"unknown queue {queue!r}")


_PENDING_QUEUES = [
    "review",
    "retrospective_review",
    "rework",
    "validation_retry",
    "tech_lead",
]

_REFUSALS = {
    # Sub-threshold: the sample counts, but the circuit is not open yet.
    "sub_threshold_auth": (
        ProviderReadiness.auth_expired(PROVIDER, "not logged in"),
        2,
    ),
    # Never opens a circuit at all.
    "not_installed": (
        ProviderReadiness.not_installed(PROVIDER, "claude not on PATH"),
        1,
    ),
}


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
@pytest.mark.parametrize("refusal", sorted(_REFUSALS))
class TestAProviderRefusalNeverConsumesPendingWork:
    """A refused launch is not a failed one; the work must survive it.

    The routing layer drops a pending item on any launch result that is not
    explicitly retained, so a provider refusal used to delete the request. For a
    failure-investigation tech-lead item the queue is the only record that
    exists, so that lost the investigation outright (#6999 F10).
    """

    def test_the_pending_item_survives_the_refusal(
        self, queue, refusal, tmp_path: Path
    ) -> None:
        readiness, threshold = _REFUSALS[refusal]
        harness = _RefusingLauncherHarness(tmp_path, readiness, threshold=threshold)
        state = _pending_state(queue)

        session = _route(queue, state, harness)

        assert session is None
        assert harness.created == []  # nothing spawned
        assert _pending_count(state, queue) == 1  # still queued for a healthy tick
        assert state.active_sessions == []

    def test_the_refusal_is_announced_for_the_issue(
        self, queue, refusal, tmp_path: Path
    ) -> None:
        """Retained is not the same as silent: the issue still gets the story."""
        readiness, threshold = _REFUSALS[refusal]
        harness = _RefusingLauncherHarness(tmp_path, readiness, threshold=threshold)
        state = _pending_state(queue)

        _route(queue, state, harness)

        assert (
            harness.event_names().count(
                EventName.SESSION_LAUNCH_BLOCKED_PROVIDER.value
            )
            == 1
        )

    def test_a_later_healthy_tick_launches_the_retained_item(
        self, queue, refusal, tmp_path: Path
    ) -> None:
        """The whole point of retaining it: the work still runs afterwards."""
        readiness, threshold = _REFUSALS[refusal]
        harness = _RefusingLauncherHarness(tmp_path, readiness, threshold=threshold)
        state = _pending_state(queue)
        _route(queue, state, harness)

        harness.probe.readiness = ProviderReadiness.ready(PROVIDER)
        harness.probe.sample_id = "recovered"
        session = _route(queue, state, harness)

        assert session is not None
        assert harness.created  # a session really started this time
        assert _pending_count(state, queue) == 0  # and the item was consumed


def test_a_refused_tech_lead_launch_keeps_its_full_retry_budget(
    tmp_path: Path,
) -> None:
    """A provider refusal must not spend the bounded required-input budget.

    That budget exists for transient failures of the request itself (an
    unreadable log or database). Nothing about the investigation failed here,
    so burning a retry against it would eventually drop the item for a reason
    that was never its fault (#6999 F10).
    """
    from issue_orchestrator.control.session_routing import PendingSessionQueues

    harness = _RefusingLauncherHarness(
        tmp_path, ProviderReadiness.not_installed(PROVIDER, "not on PATH"), threshold=1
    )
    state = _pending_state("tech_lead")

    for _ in range(5):
        _route("tech_lead", state, harness)

    assert len(state.pending_tech_lead_reviews) == 1
    # The retry budget is untouched, so a genuine input failure later still has
    # its full allowance.
    queues = PendingSessionQueues(state)
    assert queues.retain_tech_lead_for_retry(7) is not None


def test_the_launch_gate_reports_a_provider_deferral(tmp_path: Path) -> None:
    """The typed disposition, at the seam that produces it."""
    from issue_orchestrator.control.provider_launch_gate import ProviderLaunchGate
    from issue_orchestrator.control.session_launch_types import LaunchDisposition

    gate = ProviderLaunchGate(
        policy=ProviderAvailabilityPolicy(
            config=_config(),
            provider_resilience=_manager(RecordingEvents()),
            readiness_probe=StubReadinessProbe(
                ProviderReadiness.auth_expired("claude-code", "not logged in")
            ),
        ),
        events=RecordingEvents(),
        apply_actions=lambda actions, context: True,
    )

    result = gate.check("claude-code", 123)

    assert result is not None
    assert not result.success
    assert result.disposition is LaunchDisposition.PROVIDER_DEFERRED
    assert result.defers_to_provider


def test_an_unhandled_launch_disposition_never_silently_drops_the_work() -> None:
    """The destructive branch must be reached deliberately, never by default.

    Dropping the pending item is the one irreversible thing the queue owner
    does. A disposition added later without a decision here would otherwise
    land in it silently — which is how the provider refusal deleted work in the
    first place (#6999 A1).
    """
    from issue_orchestrator.control import session_routing
    from issue_orchestrator.control.session_launch_types import (
        LaunchDisposition,
        LaunchResult,
    )
    from issue_orchestrator.domain.models import OrchestratorState

    removed: list[str] = []
    owner = session_routing._PendingQueueOwner(  # noqa: SLF001 - owner contract
        remove=lambda: removed.append("removed")
    )
    result = LaunchResult(session=None, success=False, reason="new kind of failure")
    object.__setattr__(result, "disposition", "not-a-disposition")

    with pytest.raises(ValueError, match="unhandled launch disposition"):
        owner.settle(result, OrchestratorState())

    assert removed == []
