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

import ast
import importlib.util
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.claim_quarantine import (
    QuarantineCause,
    QuarantineSubject,
)
from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim
from issue_orchestrator.control.provider_availability import ProviderAvailabilityPolicy
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.control.tech_lead_reaction import (
    record_completed_session_problem,
)
from issue_orchestrator.domain.models import DiscoveredFailure, Issue, SessionStatus
from issue_orchestrator.domain.pending_work import PendingWorkKind
from issue_orchestrator.ports.pending_work_claim_store import (
    ClaimState,
    QuarantineLabelState,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.agent_runner_errors import (
    classify_provider_error,
    classify_provider_output,
)
from issue_orchestrator.execution.agent_runner_providers import (
    CLIProvider,
    ClaudeCodeProvider,
    CodexProvider,
)
from issue_orchestrator.execution.provider_readiness_probe import (
    CLIProviderReadinessProbe,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.hooks.hooks import evaluate_claude_ai_gate_result
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


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "issue_orchestrator"

# The banner an expired Claude Code login renders, verbatim from the terminal
# recordings in the incident (offset 2740 ms for #6463, 522 ms for #5336).
EXPIRED_LOGIN_BANNER = "Login expired · Please run /login"

# The banner the startup AI gate sees, verbatim from tests/unit/test_hooks.py.
# It reaches a different surface than the TUI banner above — a one-shot
# ``claude --print`` rather than a live session — which is precisely why it
# ended up in a second, private marker table.
AI_GATE_OAUTH_BANNER = (
    "Failed to authenticate: OAuth session expired and could not be refreshed"
)

# Every real auth banner any surface of this system has seen. Classification is
# parametrized over the whole corpus so a banner learned for one surface cannot
# stay unknown to another.
CLAUDE_AUTH_BANNERS = (
    EXPIRED_LOGIN_BANNER,
    AI_GATE_OAUTH_BANNER,
    "Invalid API key · Please run /login",
)
CODEX_AUTH_BANNERS = ("Not logged in. Please run `codex login`.",)
PROVIDER_AUTH_BANNERS = CLAUDE_AUTH_BANNERS + CODEX_AUTH_BANNERS


def _arch_guardrails():
    """Load the CI guardrail checker so tests assert the same rule CI runs."""
    name = "_arch_guardrails_under_test"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    path = REPO_ROOT / "tools" / "check_arch_guardrails.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: the checker defines dataclasses, and
    # ``dataclasses`` resolves their annotations through ``sys.modules``.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _synthetic_package_module(root: Path, relpath: str, source: str) -> Path:
    """Write one module into a throwaway ``issue_orchestrator`` package tree.

    The vocabulary half of the guardrail reads the owner's table out of the
    tree it is scanning, so the real one is copied in rather than restated —
    a guardrail whose expectations are a duplicate of the thing it guards is
    the same defect it exists to catch.
    """
    package = root / "src" / "issue_orchestrator"
    owner_relpath = "execution/agent_runner_errors.py"
    owner = package / owner_relpath
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text((SRC_ROOT / owner_relpath).read_text(encoding="utf-8"))

    module = package / relpath
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(textwrap.dedent(source).lstrip())
    return module


# The provider adapter intentionally checks PATH before asking CommandRunner to
# execute its credential probe.  Developer machines normally have both CLIs,
# but validate-fast intentionally does not.  Keep these adapter tests hermetic:
# PATH owns the installation fact and FakeCommandRunner below owns probe output.
@pytest.fixture(scope="module")
def provider_cli_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    fake_bin = tmp_path_factory.mktemp("provider-bin")
    for executable in ("claude", "codex"):
        path = fake_bin / executable
        path.touch()
        path.chmod(0o755)
    return fake_bin


@pytest.fixture(autouse=True)
def provider_clis_on_path(
    provider_cli_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "PATH", f"{provider_cli_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    )


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

    @pytest.mark.parametrize(
        "provider",
        [ClaudeCodeProvider(), CodexProvider()],
        ids=["claude-code", "codex"],
    )
    def test_missing_cli_reports_not_installed_without_probing(
        self,
        provider: CLIProvider,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path))
        runner = FakeCommandRunner(
            CommandResult(returncode=0, stdout='{"loggedIn": true}', stderr="")
        )

        readiness = provider.check_readiness(runner)

        assert readiness.state is ProviderReadinessState.NOT_INSTALLED
        assert runner.commands == []

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
        self,
        tmp_path: Path,
        probe,
        *,
        manager=None,
        events=None,
        config=None,
        create_session=None,
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
        # Kept rather than passed inline: a launch's label mutations all go
        # through this applier, so it is the only place a test can see whether
        # a failed launch nevertheless committed a destructive transition.
        self.action_applier = MagicMock()
        self.launcher = SessionLauncher(
            config=config,
            events=self.events,
            repository_host=MockRepositoryHost(),
            action_applier=self.action_applier,
            session_manager=MagicMock(),
            worktree_manager=MockWorktreeManager(tmp_path),
            working_copy=MockWorkingCopy(),
            command_runner=MockCommandRunner(),
            session_output=FileSystemSessionOutput(),
            manifest_downloader=NullManifestDownloader(),
            tech_lead_authority=SqliteTechLeadAuthorityStore.for_repo(tmp_path),
            session_exists_fn=lambda name: False,
            # Injected rather than patched so a test can observe what the world
            # looked like at the exact moment the terminal was spawned.
            create_session_fn=create_session
            or (lambda name, cmd, wd, title: self.created.append(name) or True),
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

    @pytest.mark.parametrize("banner", PROVIDER_AUTH_BANNERS)
    def test_every_banner_any_surface_has_seen_classifies_as_auth(
        self, banner: str
    ) -> None:
        """One corpus, so no surface can know a banner the others don't.

        The startup AI gate used to keep its own markers, and the two tables
        had already drifted: the gate read ``Failed to authenticate: OAuth
        session expired`` and this table did not. Same expiry, two answers.
        """
        assert classify_provider_output(banner) is ProviderErrorType.AUTH

    def test_no_second_provider_output_classifier_exists_in_src(self) -> None:
        """Guardrail: one owner decides what an auth failure looks like.

        Text search for one banner was the old form of this check, and it was
        evadable by any second table that chose different words — which is
        exactly how the AI gate's markers hid in plain sight. The checker below
        pins both the vocabulary and the *shape*, and runs in CI over all of
        ``src`` via ``tools/check_arch_guardrails.py``.
        """
        checker = _arch_guardrails()

        violations = [
            v.fmt()
            for path in SRC_ROOT.rglob("*.py")
            for v in checker.check_provider_output_classification(
                path, ast.parse(path.read_text(encoding="utf-8"))
            )
        ]

        assert violations == []

    @pytest.mark.parametrize(
        ("label", "source"),
        [
            (
                "token table",
                '''
                _MY_OWN_MARKERS = ("please re-login now", "handshake rejected")


                def looks_dead(output: str) -> bool:
                    return any(m in output.lower() for m in _MY_OWN_MARKERS)
                ''',
            ),
            (
                "direct literal",
                '''
                def looks_dead(output: str) -> bool:
                    return "please re-login now" in output.lower()
                ''',
            ),
            (
                "token table behind a normalized local",
                '''
                _MY_OWN_MARKERS = ("please re-login now", "handshake rejected")


                def looks_dead(output: str) -> bool:
                    lowered = output.lower()
                    return any(m in lowered for m in _MY_OWN_MARKERS)
                ''',
            ),
            (
                "casefold instead of lower",
                '''
                def looks_dead(output: str) -> bool:
                    folded = output.casefold()
                    return "please re-login now" in folded
                ''',
            ),
        ],
    )
    def test_the_guardrail_catches_a_second_classifier_however_it_is_spelled(
        self, label: str, source: str, tmp_path: Path
    ) -> None:
        """Proof the guardrail is not vacuous, and not evadable by rewording.

        None of these repeat a single word the owner's table knows. The rule
        keys on *normalization* instead — lowering or casefolding text before
        matching it is what reading a banner looks like, and a matcher that
        skips it is matching case-sensitively, which no banner reader survives.
        """
        module = _synthetic_package_module(tmp_path, "control/watcher.py", source)
        checker = _arch_guardrails()

        kinds = [
            v.kind
            for v in checker.check_provider_output_classification(
                module, ast.parse(module.read_text(encoding="utf-8"))
            )
        ]

        assert kinds == ["provider-output-classifier"], label

    def test_an_exempt_function_does_not_shelter_the_rest_of_its_module(
        self, tmp_path: Path
    ) -> None:
        """The exemption is per function, and that is the whole point.

        ``_ai_gate.py`` legitimately classifies hook-block text, and it is
        also where the deleted auth table lived. A module-level exemption
        would let that table walk back in beside the classifier that earned
        the exemption.
        """
        module = _synthetic_package_module(
            tmp_path,
            "infra/hooks/_ai_gate.py",
            '''
            _CLAUDE_AUTH_FAILURE_MARKERS = ("session gone", "creds stale")


            def _detect_blocked_from_output(output: str) -> bool:
                output_lower = output.lower()
                return any(ind in output_lower for ind in ("blocked", "denied"))


            def _is_claude_auth_failure(output: str) -> bool:
                lowered = output.lower()
                return any(m in lowered for m in _CLAUDE_AUTH_FAILURE_MARKERS)
            ''',
        )
        checker = _arch_guardrails()

        offenders = [
            (v.kind, v.detail.split(" matches")[0])
            for v in checker.check_provider_output_classification(
                module, ast.parse(module.read_text(encoding="utf-8"))
            )
        ]

        assert offenders == [("provider-output-classifier", "_is_claude_auth_failure")]

    def test_the_guardrail_catches_a_copy_of_the_owners_banner_text(
        self, tmp_path: Path
    ) -> None:
        """Re-listing a banner is caught on its own, with no matching at all."""
        module = _synthetic_package_module(
            tmp_path,
            "observation/watcher.py",
            '''
            _KNOWN_STALLS = ("login expired", "disk full")


            def describe(index: int) -> str:
                return _KNOWN_STALLS[index]
            ''',
        )
        checker = _arch_guardrails()

        kinds = [
            v.kind
            for v in checker.check_provider_output_classification(
                module, ast.parse(module.read_text(encoding="utf-8"))
            )
        ]

        assert kinds == ["provider-banner-vocabulary"]

    def test_even_an_exempt_function_may_not_list_the_owners_banners(
        self, tmp_path: Path
    ) -> None:
        """The vocabulary rule has no exemption, by design.

        A function exempted for classifying non-provider text must not be able
        to quietly acquire provider banner knowledge under that cover.
        """
        module = _synthetic_package_module(
            tmp_path,
            "infra/hooks/_ai_gate.py",
            '''
            def _detect_blocked_from_output(output: str) -> bool:
                output_lower = output.lower()
                return any(
                    ind in output_lower for ind in ("blocked", "login expired")
                )
            ''',
        )
        checker = _arch_guardrails()

        kinds = [
            v.kind
            for v in checker.check_provider_output_classification(
                module, ast.parse(module.read_text(encoding="utf-8"))
            )
        ]

        assert kinds == ["provider-banner-vocabulary"]

    def test_the_provider_adapters_may_still_interpret_their_own_output(
        self, tmp_path: Path
    ) -> None:
        """The owner surface is the table plus the provider adapters.

        A provider adapter reading its own CLI's banners is the boundary
        working, not a violation — that is where raw interpretation belongs.
        """
        module = _synthetic_package_module(
            tmp_path,
            "execution/agent_runner_providers/newcli.py",
            '''
            _BANNERS = ("login expired", "please run /login")


            def looks_dead(output: str) -> bool:
                return any(b in output.lower() for b in _BANNERS)
            ''',
        )
        checker = _arch_guardrails()

        assert (
            checker.check_provider_output_classification(
                module, ast.parse(module.read_text(encoding="utf-8"))
            )
            == []
        )


# ---------------------------------------------------------------------------
# 2b. Every consumer of provider output reads the same owner
# ---------------------------------------------------------------------------


class TestEveryConsumerReadsTheSameOwner:
    """The startup AI gate and the live-session probe agree on every banner.

    These two surfaces are where the drift actually happened: the gate knew
    ``Failed to authenticate: OAuth session expired`` and the shared table did
    not, so the same expired credential was a clean startup failure on one path
    and a 90-minute timeout on the other. Both paths are exercised with the
    same corpus here, so a banner cannot be learned by one alone again.
    """

    @pytest.mark.parametrize("banner", CLAUDE_AUTH_BANNERS)
    def test_the_gate_reports_auth_remediation_for_every_claude_banner(
        self, banner: str, tmp_path: Path
    ) -> None:
        success, message = evaluate_claude_ai_gate_result(
            returncode=1, stdout=banner, stderr="", work_repo=tmp_path
        )

        assert success is False
        assert message.startswith(
            "Claude is not authenticated; run 'claude auth login'\n"
            "Verify with 'claude auth status', then retry startup."
        )

    @pytest.mark.parametrize("banner", CLAUDE_AUTH_BANNERS)
    def test_a_live_session_fails_on_every_banner_the_gate_knows(
        self, banner: str
    ) -> None:
        """The other consumer, same corpus, confirmed by the credential probe."""
        probe = CLIProviderReadinessProbe(
            FakeCommandRunner(
                CommandResult(returncode=0, stdout='{"loggedIn": false}', stderr="")
            )
        )

        readiness = probe.diagnose_session_output("claude-code", banner)

        assert readiness.state is ProviderReadinessState.AUTH_EXPIRED
        assert readiness.human_fixable

    def test_an_exit_zero_auth_banner_still_reaches_remediation(
        self, tmp_path: Path
    ) -> None:
        """A provider that prints its banner and exits clean is still dead."""
        success, message = evaluate_claude_ai_gate_result(
            returncode=0, stdout=AI_GATE_OAUTH_BANNER, stderr="", work_repo=tmp_path
        )

        assert success is False
        assert message.startswith("Claude is not authenticated")

    def test_a_gate_pass_is_never_re_read_as_an_auth_failure(
        self, tmp_path: Path
    ) -> None:
        """The shared table is broader than the gate's four markers were.

        It knows generic auth words like "forbidden", and Claude reporting a
        blocked push is free to use them. Evidence that the gate did its job
        outranks a word in the report.
        """
        report = "The push was forbidden: a hook blocked git push --no-verify."

        success, message = evaluate_claude_ai_gate_result(
            returncode=0, stdout=report, stderr="", work_repo=tmp_path
        )

        assert success is True
        assert "AI gate test passed" in message

    def test_a_non_auth_provider_failure_keeps_the_generic_remediation(
        self, tmp_path: Path
    ) -> None:
        success, message = evaluate_claude_ai_gate_result(
            returncode=1,
            stdout="Cannot connect to the Anthropic API\n",
            stderr="",
            work_repo=tmp_path,
        )

        assert success is False
        assert message.startswith("Claude AI gate could not run (exit 1)")
        assert "Resolve the Claude CLI error below" in message


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

    def test_a_provider_success_cannot_erase_a_live_auth_outage(self) -> None:
        """An ordinary completed call is not the READY probe recovery requires.

        ``record_success`` used to delete the whole provider row. With
        concurrent sessions an older, in-flight success lands AFTER another
        session (or a readiness sample) has opened ``auth_open_until``, and
        deleting the row took the auth deadline, the auth counter and the sample
        identity with it - re-admitting the entire fleet to a provider that
        refuses every launch. That is the 90-minute burn this boundary exists to
        end, and a successful old call is no evidence at all about the
        credential (#6999 F3).
        """
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1", now=start
        )
        opened = manager.get_state("claude-code")
        assert opened is not None
        auth_deadline = opened.auth_open_until
        assert auth_deadline is not None

        manager.record_success("claude-code", now=start + timedelta(seconds=1))

        state = manager.get_state("claude-code")
        assert state is not None, "the auth outage must survive a service success"
        assert state.auth_open_until == auth_deadline
        assert state.consecutive_auth_failures == 1
        assert state.last_auth_sample_id == "s1"
        assert manager.is_open("claude-code", start + timedelta(seconds=2))
        # ...and nothing announced a recovery that has not happened.
        assert EventName.PROVIDER_OUTAGE_EXITED.value not in events.names()

    def test_a_success_retires_the_transient_cause_and_only_that(self) -> None:
        """Both causes coexisting: the success clears its own half, exactly."""
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_transient_failure(
            "claude-code", error_summary="503", now=start
        )
        manager.record_auth_failure(
            "claude-code",
            error_summary="not logged in",
            sample_id="s1",
            now=start + timedelta(seconds=1),
        )

        manager.record_success("claude-code", now=start + timedelta(seconds=2))

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.transient_open_until is None
        assert state.consecutive_outages == 0
        assert state.auth_open_until is not None
        assert manager.is_open("claude-code", start + timedelta(seconds=3))
        assert EventName.PROVIDER_OUTAGE_EXITED.value not in events.names()

        # Only the confirmed READY probe ends it, and then the aggregate
        # transition is announced exactly once.
        manager.clear_auth_failures("claude-code", now=start + timedelta(seconds=4))
        assert not manager.is_open("claude-code", start + timedelta(seconds=5))
        assert events.names().count(EventName.PROVIDER_OUTAGE_EXITED.value) == 1

    def test_a_success_between_auth_failures_does_not_reset_the_threshold(
        self,
    ) -> None:
        """``auth_failure_threshold > 1`` has to be able to accumulate.

        Zeroing ``consecutive_auth_failures`` on any successful call meant a
        configured threshold above one could never be reached: every unrelated
        call in between refunded the count, so the circuit never tripped and the
        fleet kept launching into an expired credential.
        """
        events = RecordingEvents()
        manager = _manager(events, threshold=2)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)

        manager.record_auth_failure(
            "claude-code", error_summary="not logged in", sample_id="s1", now=start
        )
        assert not manager.is_open("claude-code", start + timedelta(seconds=1))

        manager.record_success("claude-code", now=start + timedelta(seconds=1))
        manager.record_auth_failure(
            "claude-code",
            error_summary="not logged in",
            sample_id="s2",
            now=start + timedelta(seconds=2),
        )

        state = manager.get_state("claude-code")
        assert state is not None
        assert state.consecutive_auth_failures == 2
        assert manager.is_open("claude-code", start + timedelta(seconds=3))

    def test_a_success_on_a_healthy_transient_circuit_still_removes_the_row(
        self,
    ) -> None:
        """With no auth cause left, "a healthy circuit has no row" still holds."""
        events = RecordingEvents()
        manager = _manager(events)
        start = datetime(2026, 8, 4, 22, 0, tzinfo=timezone.utc)
        manager.record_transient_failure(
            "claude-code", error_summary="503", now=start
        )

        manager.record_success("claude-code", now=start + timedelta(seconds=1))

        assert manager.get_state("claude-code") is None
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

    def __init__(
        self,
        tmp_path: Path,
        readiness: ProviderReadiness,
        *,
        threshold: int,
        create_session=None,
    ):
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
            create_session=create_session,
        )
        self.manager = manager
        self.claims = _claims(tmp_path)
        self.quarantine_actions: list = []


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


def _route(queue: str, state, harness, restorer=None):
    """Drive the production routing function that owns ``queue``.

    ``restorer`` is a caller-supplied spy rather than a hidden local so a test
    can assert what the settlement did NOT do. A provider deferral must not
    attempt terminal adoption, and that is only observable from outside if the
    mock belongs to the caller (#6999 F12).

    The claim store comes from the harness so launch and settlement share the
    one orchestrator-owned store, exactly as the composition root wires it.
    """
    from unittest.mock import MagicMock

    from issue_orchestrator.control import session_routing

    if restorer is None:
        restorer = MagicMock()
    restorer.restore_session.return_value = None
    claims = harness.claims
    if queue == "review":
        return session_routing.orchestrator_launch_review_session(
            state.pending_reviews[0], state, harness.launcher, restorer, claims
        )
    if queue == "retrospective_review":
        return session_routing.orchestrator_launch_retrospective_review_session(
            state.pending_retrospective_reviews[0], state, harness.launcher,
            restorer, claims,
        )
    if queue == "rework":
        return session_routing.orchestrator_launch_rework_session(
            state.pending_reworks[0], state, harness.launcher, restorer, claims
        )
    if queue == "validation_retry":
        return session_routing.orchestrator_launch_validation_retry_session(
            state.pending_validation_retries[0], state, harness.launcher,
            restorer, claims,
        )
    if queue == "tech_lead":
        return session_routing.orchestrator_launch_tech_lead_session(
            state.pending_tech_lead_reviews[0],
            state,
            harness.launcher.config,
            harness.launcher,
            restorer,
            claims,
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

    def test_the_refusal_attempts_no_terminal_restoration(
        self, queue, refusal, tmp_path: Path
    ) -> None:
        """PROVIDER_DEFERRED is not EXISTING_TERMINAL — there is nothing to adopt.

        Adoption starts by asking the runner what is running, so a single
        discovery call is enough to prove the settlement wandered into the
        restoration path. Both collaborators are asserted silent (#6999 F12).
        """
        from unittest.mock import MagicMock

        readiness, threshold = _REFUSALS[refusal]
        harness = _RefusingLauncherHarness(tmp_path, readiness, threshold=threshold)
        state = _pending_state(queue)
        restorer = MagicMock()

        _route(queue, state, harness, restorer)

        runner = harness.launcher.session_manager.runner
        runner.discover_running_sessions.assert_not_called()
        # Nothing at all was asked of the restorer — not discovery matching, not
        # canonical-id derivation, not restore_known_terminal.
        assert restorer.mock_calls == []

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
    from issue_orchestrator.control.pending_session_queues import (
        PendingSessionQueues,
        TechLeadRetentionOutcome,
    )

    harness = _RefusingLauncherHarness(
        tmp_path, ProviderReadiness.not_installed(PROVIDER, "not on PATH"), threshold=1
    )
    state = _pending_state("tech_lead")

    for _ in range(5):
        _route("tech_lead", state, harness)

    assert len(state.pending_tech_lead_reviews) == 1
    # Not "an outcome was returned" — five refusals must count as zero. Asserting
    # the counter itself is what makes this a regression test: any deferral that
    # started spending the budget would show up here immediately (#6999 F12).
    assert state.pending_tech_lead_reviews[0].retryable_launch_failures == 0

    # And the allowance is genuinely intact rather than merely unread: the first
    # REAL input failure is the first one counted, and it is retained.
    queues = PendingSessionQueues(state)
    spend = queues.plan_tech_lead_retry(7)
    assert spend.outcome is TechLeadRetentionOutcome.RETAINED
    queues.apply_tech_lead_retry(spend)
    assert state.pending_tech_lead_reviews[0].retryable_launch_failures == 1


def test_a_provider_deferral_touches_neither_restoration_nor_the_retry_budget(
    tmp_path: Path,
) -> None:
    """The two side effects F10 required the new disposition to exclude.

    Asserted at the owner itself, where both collaborators are directly
    observable. The production matrix can only show that restoration was never
    *reached*; this pins the contract of the settlement branch itself, so a
    future edit that reorders the branch cannot spend the only queued
    failure-investigation record's budget (#6999 F12).
    """
    from issue_orchestrator.control.launch_transaction import (
        LaunchSettlement,
        RetryPlan,
    )
    from issue_orchestrator.control.session_launch_types import (
        LaunchDisposition,
        LaunchResult,
    )
    from issue_orchestrator.domain.models import OrchestratorState

    calls: list[str] = []

    def _spy_restore_existing():
        calls.append("restore_existing")
        return None

    def _spy_plan_retry(claim):
        calls.append("plan_retry")
        return RetryPlan(
            spent=claim,
            exhausted=False,
            apply=lambda: calls.append("apply_retry"),
            commit_exhaustion=lambda: False,
        )

    owner = LaunchSettlement(
        work=_launch_work("tech_lead", _pending_state("tech_lead"), tmp_path),
        remove=lambda: calls.append("remove"),
        restore_existing=_spy_restore_existing,
        plan_retry=_spy_plan_retry,
    )

    settled = owner.settle(
        LaunchResult(
            session=None,
            success=False,
            reason="claude not on PATH",
            disposition=LaunchDisposition.PROVIDER_DEFERRED,
        ),
        OrchestratorState(),
    )

    assert settled is None
    assert calls == []  # not removed, not restored, no budget spent


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


def test_an_unhandled_launch_disposition_never_silently_drops_the_work(
    tmp_path: Path,
) -> None:
    """The destructive branch must be reached deliberately, never by default.

    Dropping the pending item is the one irreversible thing the queue owner
    does. A disposition added later without a decision here would otherwise
    land in it silently — which is how the provider refusal deleted work in the
    first place (#6999 A1).
    """
    from issue_orchestrator.control.launch_transaction import LaunchSettlement
    from issue_orchestrator.control.session_launch_types import (
        LaunchDisposition,
        LaunchResult,
    )
    from issue_orchestrator.domain.models import OrchestratorState

    removed: list[str] = []
    owner = LaunchSettlement(
        work=_launch_work("review", _pending_state("review"), tmp_path),
        remove=lambda: removed.append("removed"),
    )
    result = LaunchResult(session=None, success=False, reason="new kind of failure")
    object.__setattr__(result, "disposition", "not-a-disposition")

    with pytest.raises(ValueError, match="unhandled launch disposition"):
        owner.settle(result, OrchestratorState())

    assert removed == []


# ---------------------------------------------------------------------------
# A live auth termination returns the work it consumed at launch (#6999 F2/A1)
# ---------------------------------------------------------------------------


def _pending_items(state, queue: str) -> list:
    return {
        "review": state.pending_reviews,
        "retrospective_review": state.pending_retrospective_reviews,
        "rework": state.pending_reworks,
        "validation_retry": state.pending_validation_retries,
        "tech_lead": state.pending_tech_lead_reviews,
    }[queue]


def _claim(queue: str, state):
    """The typed claim a launch off ``queue`` would take from ``state``."""
    from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind

    return PendingWorkClaim(PendingWorkKind(queue), _pending_items(state, queue)[0])


def _ready_harness(tmp_path: Path, *, create_session=None):
    """The same production launcher, with a provider that is authenticated."""
    return _RefusingLauncherHarness(
        tmp_path,
        ProviderReadiness.ready(PROVIDER),
        threshold=1,
        create_session=create_session,
    )


def _launch_work(queue: str, state, tmp_path: Path):
    """The launch-spanning claim owner the routing functions build (#6999 A2)."""
    from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim

    return PendingWorkLaunchClaim(
        claim=_claim(queue, state), claims=_claims(tmp_path)
    )


def _unrestorable_subject(
    run_key: str, started_at: str, *, session_name: str = "issue-7", issue_number: int = 7
):
    """A live run whose session assets could not be rebuilt (#6999 F14)."""
    return QuarantineSubject(
        quarantine_key=f"{run_key}@{started_at}",
        run_key=run_key,
        session_name=session_name,
        issue_number=issue_number,
        error="the run's session assets could not be rebuilt",
        cause=QuarantineCause.RUN_UNRESTORABLE,
    )


class _RecordingLabels:
    """Typed blocking-label ops that record what they were asked to do.

    Mirrors the production adapter's contract (#6999 F12): acquiring a label
    reports whether it was already present, and every op can be made to fail so
    retry behaviour is observable.
    """

    def __init__(self, harness, *, applies: bool = True, acquire=None):
        from issue_orchestrator.ports.pending_work_claim_store import (
            QuarantineLabelState,
        )

        self._harness = harness
        self._applies = applies
        self._acquire = acquire or QuarantineLabelState.ACQUIRED

    def acquire_block(self, issue_number: int):
        from issue_orchestrator.control.actions import AddLabelAction
        from issue_orchestrator.ports.pending_work_claim_store import (
            QuarantineLabelState,
        )

        self._harness.quarantine_actions.append(
            AddLabelAction(
                issue_number=issue_number,
                label="needs-human",
                reason="pending-work claim unreadable",
            )
        )
        return self._acquire if self._applies else QuarantineLabelState.UNKNOWN

    def release_block(self, issue_number: int) -> bool:
        from issue_orchestrator.control.actions import RemoveLabelAction

        self._harness.quarantine_actions.append(
            RemoveLabelAction(
                issue_number=issue_number,
                label="needs-human",
                reason="pending-work claim quarantine resolved",
            )
        )
        return self._applies

    def announce(self, issue_number: int, comment: str) -> bool:
        from issue_orchestrator.control.actions import AddCommentAction

        self._harness.quarantine_actions.append(
            AddCommentAction(
                number=issue_number,
                comment=comment,
                reason="pending-work claim unreadable",
            )
        )
        return self._applies


def _quarantine_with(harness, *, applies: bool = True, acquire=None):
    from issue_orchestrator.control.claim_quarantine import ClaimQuarantineOwner

    return ClaimQuarantineOwner(
        store=harness.claims,
        labels=_RecordingLabels(harness, applies=applies, acquire=acquire),
        events=harness.events,
    )


def _quarantine(harness):
    """The real bounded quarantine owner, sharing the harness's store."""
    return _quarantine_with(harness)


def _claims(tmp_path: Path):
    """The real orchestrator-owned store, so the durable claim path is exercised.

    Rooted at the test's repo root rather than any worktree: the whole point of
    #6999 F7 is that this record lives where the agent cannot reach it.
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        SqlitePendingWorkClaimStore,
    )

    return SqlitePendingWorkClaimStore.for_repo(tmp_path)


def _ledger(state, harness):
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger

    return InFlightWorkLedger(state, harness.claims)


def _terminate_on_provider(state, session, error_type, harness, *, drop_active=True):
    """Settle a running session the way terminal completion does.

    ``drop_active`` mirrors ``handle_session_completion``, which drops the
    session from ``active_sessions`` before settling. One test flips it off to
    prove the restore does not silently depend on that order.
    """
    from issue_orchestrator.control.in_flight_work import SettlementOutcome

    if drop_active:
        state.active_sessions = [
            s for s in state.active_sessions if s.terminal_id != session.terminal_id
        ]
    return _ledger(state, harness).settle(
        session, SettlementOutcome.for_provider_error(error_type)
    )


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
class TestALiveAuthTerminationReturnsTheWorkItConsumed:
    """Launching spends the request; only finishing the WORK should.

    The provider refusal path (above) never removed the queue item because the
    session never started. This is the other half: the session DID start, ran
    on a credential that then expired, and ended BLOCKED. Nothing in that path
    held the request, so queue-only work — a failure investigation's typed
    DiscoveredFailure, a validation retry's prompt/error/count, a rework whose
    needs-rework trigger was stripped at launch — was lost outright (#6999 F2).
    """

    def test_the_launch_hands_the_claim_to_the_in_flight_owner(
        self, queue, tmp_path: Path
    ) -> None:
        """Dequeued is not the same as spent: someone still holds it."""
        harness = _ready_harness(tmp_path)
        state = _pending_state(queue)
        original = _pending_items(state, queue)[0]

        session = _route(queue, state, harness)

        assert session is not None
        assert _pending_count(state, queue) == 0  # off the queue...
        held = _ledger(state, harness).holds(session.terminal_id)
        assert held is not None  # ...but not gone
        assert held.request is original

    def test_a_confirmed_auth_failure_returns_the_original_request(
        self, queue, tmp_path: Path
    ) -> None:
        """The SAME object, not a reconstruction.

        Identity is the assertion that matters: a rebuilt stand-in would lose
        the typed context that exists nowhere else once the item left its queue.
        """
        harness = _ready_harness(tmp_path)
        state = _pending_state(queue)
        original = _pending_items(state, queue)[0]
        session = _route(queue, state, harness)
        assert session is not None

        # Still listed as active on purpose: whether completion has dropped the
        # session yet is an ordering detail, and the work must come back either
        # way.
        _terminate_on_provider(
            state, session, ProviderErrorType.AUTH, harness, drop_active=False
        )

        assert _pending_count(state, queue) == 1
        assert _pending_items(state, queue)[0] is original

    def test_the_recovered_provider_relaunches_the_same_work(
        self, queue, tmp_path: Path
    ) -> None:
        """The whole point: after a human re-authenticates, the work still runs."""
        harness = _ready_harness(tmp_path)
        state = _pending_state(queue)
        first = _route(queue, state, harness)
        assert first is not None
        harness.probe.readiness = ProviderReadiness.auth_expired(
            PROVIDER, "not logged in"
        )
        _terminate_on_provider(state, first, ProviderErrorType.AUTH, harness)

        harness.probe.readiness = ProviderReadiness.ready(PROVIDER)
        second = _route(queue, state, harness)

        assert second is not None
        assert len(harness.created) == 2  # a real second spawn
        assert _pending_count(state, queue) == 0

    def test_a_terminal_work_outcome_still_consumes_the_request(
        self, queue, tmp_path: Path
    ) -> None:
        """The retention rule is scoped to provider verdicts, nothing wider.

        An agent that reported BLOCKED on the substance of the work has spent
        its request exactly as it always did; re-queueing that would relaunch
        the same doomed session forever.
        """
        harness = _ready_harness(tmp_path)
        state = _pending_state(queue)
        session = _route(queue, state, harness)
        assert session is not None

        _terminate_on_provider(state, session, None, harness)

        assert _pending_count(state, queue) == 0

    def test_a_returned_request_is_not_double_queued(
        self, queue, tmp_path: Path
    ) -> None:
        """Discovery may have re-queued the work while the session was dying.

        Each queue applies its own duplicate rule on the way back in, so a
        restore can add at most one item.
        """
        harness = _ready_harness(tmp_path)
        state = _pending_state(queue)
        original = _pending_items(state, queue)[0]
        session = _route(queue, state, harness)
        assert session is not None
        _pending_items(state, queue).append(original)  # rediscovered meanwhile

        _terminate_on_provider(state, session, ProviderErrorType.AUTH, harness)

        assert _pending_count(state, queue) == 1


def test_a_returned_failure_investigation_keeps_its_typed_trigger(
    tmp_path: Path,
) -> None:
    """The context F2 said was lost, asserted as context rather than as a count.

    The queued item is the only carrier of the typed DiscoveredFailure once the
    per-tick buffer is cleared, so a restore that produced a bare
    PendingTechLeadReview would leave the investigation with nothing to
    investigate.
    """
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    _terminate_on_provider(state, session, ProviderErrorType.AUTH, harness)

    returned = state.pending_tech_lead_reviews[0]
    assert returned.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
    assert returned.failure is not None
    assert returned.failure.issue_number == 7
    assert returned.failure.blocking_label == "blocked-failed"
    # And the launch that died on the provider cost it nothing.
    assert returned.retryable_launch_failures == 0


def test_a_returned_validation_retry_keeps_its_attempt_budget(
    tmp_path: Path,
) -> None:
    """A credential outage must not count as a validation attempt."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("validation_retry")
    session = _route("validation_retry", state, harness)
    assert session is not None

    _terminate_on_provider(state, session, ProviderErrorType.AUTH, harness)

    returned = state.pending_validation_retries[0]
    assert returned.retry_count == 1  # unchanged by the outage
    assert returned.validation_error == "boom"
    assert returned.original_prompt is None


def test_a_transient_provider_outage_also_returns_the_work(tmp_path: Path) -> None:
    """AUTH is not the only verdict that means "the work was never attempted"."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    _terminate_on_provider(state, session, ProviderErrorType.TRANSIENT, harness)

    assert len(state.pending_tech_lead_reviews) == 1


def test_an_issue_session_holds_no_claim_to_return(
    make_session, tmp_path: Path
) -> None:
    """Issue work is claimed by label, not by dequeuing; settling is a no-op.

    The store raises on every call on purpose: a claimless path must never
    touch it, so a read, defer or consume here fails the test rather than
    quietly succeeding.
    """
    from issue_orchestrator.control.in_flight_work import (
        InFlightWorkLedger,
        SettlementOutcome,
    )
    from issue_orchestrator.domain.models import OrchestratorState

    state = OrchestratorState()

    settled = InFlightWorkLedger(state, _UntouchableClaimStore()).settle(
        make_session(), SettlementOutcome.PROVIDER_DEFERRED
    )

    assert settled is None
    assert state.pending_reviews == []
    assert state.pending_tech_lead_reviews == []


def test_an_unhandled_pending_work_kind_never_silently_drops_the_work() -> None:
    """The mirror of the launch-disposition guard, on the admission side.

    A queue kind added without a decision in ``restore_deferred`` would
    otherwise return None-ish and silently discard the only record of its work.
    """
    from issue_orchestrator.control.pending_session_queues import PendingSessionQueues
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.domain.pending_work import PendingWorkClaim

    state = _pending_state("review")
    claim = _claim("review", state)
    object.__setattr__(claim, "kind", "not-a-kind")

    with pytest.raises(ValueError, match="unhandled pending work kind"):
        PendingSessionQueues(OrchestratorState()).restore_deferred(claim)

    assert isinstance(claim, PendingWorkClaim)


# ---------------------------------------------------------------------------
# Terminal completion is the seam that invokes the owner
# ---------------------------------------------------------------------------


def _complete_session(session, state, claims, *, provider_error_type):
    """Drive the production completion entrypoint for one session."""
    from unittest.mock import MagicMock

    from issue_orchestrator.control.completion_handler import CleanupDecision
    from issue_orchestrator.control.session_completion import handle_session_completion
    from issue_orchestrator.domain.models import SessionStatus
    from issue_orchestrator.ports.session_output import SessionOutput

    config = MagicMock()
    config.code_review_agent = "agent:reviewer"
    config.cleanup.without_tech_lead.close_ai_session_tabs = False
    completion_handler = MagicMock()
    completion_handler.process_completion.return_value = MagicMock(
        actions=[],
        history_entry=None,
        cleanup=CleanupDecision.immediate(),
        should_queue_review=False,
        pr_url=None,
        pr_number=None,
    )

    handle_session_completion(
        session=session,
        status=SessionStatus.BLOCKED,
        state=state,
        completion_handler=completion_handler,
        action_applier=MagicMock(),
        observer=MagicMock(),
        worktree_manager=None,
        kill_session_fn=lambda name: None,
        config=config,
        session_output=MagicMock(spec=SessionOutput),
        provider_error_type=provider_error_type,
        pending_work_claims=claims,
    )


def test_completion_control_returns_the_work_on_a_provider_block(
    tmp_path: Path,
) -> None:
    """The other half of F2: the ledger is useless if nothing settles it.

    Asserted through ``handle_session_completion`` rather than the ledger, so a
    completion path that stopped calling the owner would fail here even while
    every owner-level test above stayed green.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    assert state.pending_tech_lead_reviews == []

    _complete_session(session, state, harness.claims, provider_error_type=ProviderErrorType.AUTH)

    assert len(state.pending_tech_lead_reviews) == 1
    assert state.pending_tech_lead_reviews[0].failure is not None


def test_completion_control_consumes_the_work_on_an_ordinary_block(
    tmp_path: Path,
) -> None:
    """An agent-reported block is still a spent request."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    _complete_session(session, state, harness.claims, provider_error_type=None)

    assert state.pending_tech_lead_reviews == []


# ---------------------------------------------------------------------------
# An auth banner written after the head of the log (#6999 F3)
# ---------------------------------------------------------------------------


@dataclass
class _BannerConfirmingProbe:
    """A probe shaped like the real one: signature first, then confirmation.

    ``StubReadinessProbe`` answers the same way whatever it is handed, which
    cannot show whether the banner actually REACHED the probe. This one runs the
    real classification table over the output it was given, so a scan window
    that never included the banner — or that mangled it — fails the test.
    """

    confirms: bool = True
    seen: list[str] = field(default_factory=list)

    def check_launch_readiness(self, provider: str) -> ProviderReadiness:
        return ProviderReadiness.ready(provider)

    def diagnose_session_output(self, provider: str, output: str) -> ProviderReadiness:
        self.seen.append(output)
        matched = classify_provider_output(output) is ProviderErrorType.AUTH
        if not matched or not self.confirms:
            return ProviderReadiness.unknown(provider, "no confirmed auth failure")
        return ProviderReadiness.auth_expired(provider, "not logged in")


class TestAnAuthBannerPastTheHeadOfTheLog:
    """A credential can expire long after the session started working.

    The observer used to read only the first 8 KiB of the terminal log. Once
    ordinary output passed that mark, a banner appended later could never reach
    the probe, and the session fell through to the generic timeout path — the
    90-minute burn this issue exists to remove (#6999 F3).
    """

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

    def _session_with_lines(self, make_session, lines: list[str]):
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
            "".join(
                json.dumps({"kind": "output", "data": line}) + "\n" for line in lines
            ),
            encoding="utf-8",
        )
        return session

    @staticmethod
    def _ordinary_output(total_bytes: int) -> list[str]:
        """Believable agent chatter, comfortably past the head window."""
        chunk = "reading src/issue_orchestrator/control/planner.py ... ok"
        return [chunk] * (total_bytes // len(chunk) + 1)

    def test_a_banner_after_8kib_of_output_still_fails_the_session(
        self, sample_config, make_session
    ) -> None:
        probe = _BannerConfirmingProbe()
        observer = self._observer(sample_config, probe)
        session = self._session_with_lines(
            make_session,
            self._ordinary_output(64 * 1024) + [EXPIRED_LOGIN_BANNER],
        )

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED
        assert result.provider_readiness is not None
        assert result.provider_readiness.human_fixable

    def test_a_banner_after_8kib_of_output_outranks_timeout(
        self, sample_config, make_session
    ) -> None:
        """The exact misdirection F3 described: TIMED_OUT mints an investigation."""
        probe = _BannerConfirmingProbe()
        observer = self._observer(sample_config, probe)
        session = self._session_with_lines(
            make_session,
            self._ordinary_output(64 * 1024) + [EXPIRED_LOGIN_BANNER],
        )
        session.started_at = datetime.now() - timedelta(
            minutes=sample_config.session_timeout_minutes + 30
        )

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED

    def test_the_launch_time_banner_is_still_read_from_the_head(
        self, sample_config, make_session
    ) -> None:
        """Widening the window must not cost the case that already worked."""
        probe = _BannerConfirmingProbe()
        observer = self._observer(sample_config, probe)
        session = self._session_with_lines(
            make_session,
            [EXPIRED_LOGIN_BANNER] + self._ordinary_output(64 * 1024),
        )

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.PROVIDER_AUTH_FAILED

    def test_the_scan_window_stays_bounded(
        self, sample_config, make_session
    ) -> None:
        """Bounded at both ends, not "read the whole log".

        An unbounded read is O(session length) on every observation of every
        session, every tick. Two 8 KiB edges plus a separator is the ceiling.
        """
        from issue_orchestrator.observation.observer import (
            PROVIDER_AUTH_CHECK_MAX_BYTES,
        )

        probe = _BannerConfirmingProbe()
        observer = self._observer(sample_config, probe)
        session = self._session_with_lines(
            make_session,
            self._ordinary_output(512 * 1024) + [EXPIRED_LOGIN_BANNER],
        )

        observer.observe_session(session)

        assert probe.seen
        assert len(probe.seen[0].encode("utf-8")) <= 2 * PROVIDER_AUTH_CHECK_MAX_BYTES + 1

    def test_an_unconfirmed_late_banner_leaves_the_session_running(
        self, sample_config, make_session
    ) -> None:
        """The false-positive guard is unchanged by the wider window.

        The tail is where an agent echoing a provider's auth banner while
        working shows up, so probe confirmation matters more here, not less.
        """
        probe = _BannerConfirmingProbe(confirms=False)
        observer = self._observer(sample_config, probe)
        session = self._session_with_lines(
            make_session,
            self._ordinary_output(64 * 1024) + [EXPIRED_LOGIN_BANNER],
        )

        result = observer.observe_session(session)

        assert result.observation is SessionObservation.RUNNING


# ---------------------------------------------------------------------------
# The claim survives a restart (#6999 F4)
# ---------------------------------------------------------------------------


def _restart(state, session, harness):
    """Rebuild orchestrator state the way startup does, and rehydrate claims.

    Returns a FRESH OrchestratorState: the pending queues and the in-flight
    ledger are in-memory, so after a restart the launched request exists only
    beside the session's run assets. Restoration goes through the real
    SessionRestorer and the real run-artifact adapter, so nothing here can
    accidentally hand the claim over in memory.
    """
    from issue_orchestrator.control.session_restorer import SessionRestorer
    from issue_orchestrator.control.session_routing import restore_running_sessions
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.ports.session_runner import DiscoveredSession
    from tests.unit.test_session_restorer import MockRepositoryHost, MockWorkingCopy

    del state  # the old process's state is gone; that is the whole point
    restarted = OrchestratorState()
    repo_host = MockRepositoryHost()
    repo_host.issues[session.issue.number] = session.issue
    working_copy = MockWorkingCopy()
    working_copy.branches[session.worktree_path] = session.branch_name or "branch"
    restorer = SessionRestorer(
        harness.launcher.config, repo_host, working_copy
    )
    discovered = [
        DiscoveredSession(
            issue_number=session.issue.number,
            tab_name="",
            is_review=session.terminal_id.startswith(
                ("review-", "retrospective-review-")
            ),
            session_name=session.terminal_id,
            run_dir=str(session.run_assets.run_dir),
        )
    ]
    restored = restore_running_sessions(
        discovered, restarted, restorer, harness.claims, _quarantine(harness)
    )
    assert restored, "the terminal itself must restore, or the test proves nothing"
    return restarted, restored[0]


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_a_restart_still_returns_the_work_on_a_provider_failure(
    queue, tmp_path: Path
) -> None:
    """The gap F4 named: the claim was process-local.

    Launch removes the item from its queue, so between launch and settlement
    the claim is the only record. If the orchestrator restarted while the
    terminal was live and THEN saw the credential die, completion found no
    claim and the work was gone - permanently, for a failure investigation,
    which auth outcomes deliberately never re-mint.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state(queue)
    session = _route(queue, state, harness)
    assert session is not None

    restarted, restored = _restart(state, session, harness)
    assert _pending_count(restarted, queue) == 0  # nothing recovered it in memory

    _terminate_on_provider(restarted, restored, ProviderErrorType.AUTH, harness)

    assert _pending_count(restarted, queue) == 1


def test_a_restarted_failure_investigation_keeps_its_typed_trigger(
    tmp_path: Path,
) -> None:
    """The DiscoveredFailure has to survive the disk round trip, not just exist."""
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    restarted, restored = _restart(state, session, harness)

    _terminate_on_provider(restarted, restored, ProviderErrorType.AUTH, harness)

    returned = restarted.pending_tech_lead_reviews[0]
    assert returned.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
    assert returned.failure is not None
    assert returned.failure.issue_number == 7
    assert returned.failure.issue_title == "Test Issue"
    assert returned.failure.failure_reason == "failed"
    assert returned.failure.blocking_label == "blocked-failed"
    assert returned.retryable_launch_failures == 0


def test_a_restarted_validation_retry_keeps_its_prompt_and_budget(
    tmp_path: Path,
) -> None:
    """Prompt, error and attempt count cannot be rebuilt from a terminal alone."""
    from issue_orchestrator.domain.session_key import TaskKind

    harness = _ready_harness(tmp_path)
    state = _pending_state("validation_retry")
    state.pending_validation_retries[0].original_prompt = "the original prompt"
    session = _route("validation_retry", state, harness)
    assert session is not None
    restarted, restored = _restart(state, session, harness)

    _terminate_on_provider(restarted, restored, ProviderErrorType.AUTH, harness)

    returned = restarted.pending_validation_retries[0]
    assert returned.original_prompt == "the original prompt"
    assert returned.validation_error == "boom"
    assert returned.retry_count == 1
    assert returned.source_task is TaskKind.CODE


def test_a_restarted_rework_can_still_restore_its_durable_label(
    tmp_path: Path,
) -> None:
    """The second half of the rework case F4 named.

    A restored ``rework-*`` terminal comes back as generic CODE work with no PR
    number, and the provider-blocked planner keys the ``needs-rework`` restore
    on the PR. Without the claim the label is unrecoverable too, so BOTH the
    queue item and its crash-safe trigger were lost.
    """
    from issue_orchestrator.domain.session_key import TaskKind

    harness = _ready_harness(tmp_path)
    state = _pending_state("rework")
    state.pending_reworks[0].pr_number = 70
    state.pending_reworks[0].rework_cycle = 3
    session = _route("rework", state, harness)
    assert session is not None

    _restarted, restored = _restart(state, session, harness)

    # Identity the terminal name could not supply, taken from the claim.
    assert restored.pr_number == 70
    assert restored.key.task is TaskKind.REWORK
    assert restored.rework_cycle == 3


def test_a_restart_leaves_a_claimless_terminal_alone(tmp_path: Path) -> None:
    """Issue sessions hold no claim; rehydration must not invent one."""
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
    from issue_orchestrator.domain.models import Issue, OrchestratorState

    harness = _ready_harness(tmp_path)
    result = harness.launcher.launch_issue_session(
        Issue(number=123, title="Test Issue", labels=["agent:backend"], repo="test/repo"),
        [],
    )
    assert result.session is not None
    restarted, restored = _restart(OrchestratorState(), result.session, harness)

    assert InFlightWorkLedger(restarted, harness.claims).holds(
        restored.terminal_id
    ) is None
    assert restarted.in_flight_work == []


def test_a_settled_claim_does_not_come_back_after_a_restart(
    tmp_path: Path,
) -> None:
    """Consuming the work must clear the durable record too.

    A stale artifact would be re-taken at the next restart and re-queue work
    that had already been done.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    _terminate_on_provider(state, session, None, harness)  # a real terminal work outcome

    restarted, restored = _restart(state, session, harness)
    _terminate_on_provider(restarted, restored, ProviderErrorType.AUTH, harness)

    assert restarted.pending_tech_lead_reviews == []


# ---------------------------------------------------------------------------
# Ledger invariants: atomic settlement, unique ownership (#6999 F5)
# ---------------------------------------------------------------------------


def test_a_failing_restoration_leaves_the_claim_held(tmp_path: Path) -> None:
    """Releasing before the queue accepts destroys the only record of the work.

    Settlement runs when something has ALREADY gone wrong, so it is exactly
    where a destructive-first ordering is least affordable: the tick raises,
    the claim is gone, and no later attempt can find it.
    """
    from issue_orchestrator.control.in_flight_work import (
        InFlightWorkLedger,
        SettlementOutcome,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    ledger = InFlightWorkLedger(state, harness.claims)
    held = ledger.holds(session.terminal_id)
    assert held is not None
    # A queue kind with no entry in the admission table - the failure mode the
    # restoration decision table raises on by design.
    object.__setattr__(held, "kind", "not-a-kind")

    with pytest.raises(ValueError, match="unhandled pending work kind"):
        ledger.settle(session, SettlementOutcome.PROVIDER_DEFERRED)

    assert ledger.holds(session.terminal_id) is held  # still held in memory
    # ...and still in the ledger, so a fresh process can enumerate and
    # re-admit it. The row is DEFERRED by then - the durable half of the
    # transition committed first on purpose - so it is no longer "held by this
    # run", but it is very much still there (#6999 F8).
    unresolved = harness.claims.list_unresolved_claims()
    assert [u.deferred for u in unresolved] == [True]


def test_a_conflicting_second_claim_is_refused(tmp_path: Path) -> None:
    """Two claims for one terminal is registry drift, not a launch.

    Replacing the first would convert a caller bug into silent work loss -
    precisely what this boundary exists to prevent - so it fails fast.
    """
    from issue_orchestrator.control.in_flight_work import (
        DuplicateClaimError,
        InFlightWorkLedger,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    ledger = InFlightWorkLedger(state, harness.claims)
    original = ledger.holds(session.terminal_id)
    other = _claim("review", _pending_state("review"))

    with pytest.raises(DuplicateClaimError, match="already holds"):
        ledger.take(session, other)

    assert ledger.holds(session.terminal_id) is original
    assert len(state.in_flight_work) == 1


def test_re_taking_the_same_claim_is_idempotent(tmp_path: Path) -> None:
    """A repeated adoption of the terminal it already recorded changed nothing."""
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    ledger = InFlightWorkLedger(state, harness.claims)
    held = ledger.holds(session.terminal_id)
    assert held is not None

    ledger.take(session, held)

    assert len(state.in_flight_work) == 1
    assert ledger.holds(session.terminal_id) is held


def test_a_refused_claim_leaves_the_queue_item_alone(tmp_path: Path) -> None:
    """Ordering inside the launch settlement, asserted at the settlement.

    The ledger is the collaborator that can refuse, so it is asked first. If
    removal came first, a refused claim would leave the work dequeued and
    unheld - lost by the very check meant to protect it.
    """
    from issue_orchestrator.control.in_flight_work import (
        DuplicateClaimError,
        InFlightWorkLedger,
    )
    from issue_orchestrator.control.launch_transaction import LaunchSettlement
    from issue_orchestrator.control.session_launch_types import LaunchResult

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    review_state = _pending_state("review")
    removed: list[str] = []

    settlement = LaunchSettlement(
        work=PendingWorkLaunchClaim(
            claim=_claim("review", review_state), claims=harness.claims
        ),
        remove=lambda: removed.append("removed"),
    )

    with pytest.raises(DuplicateClaimError):
        settlement.settle(LaunchResult(session, True), state)

    assert removed == []  # the review item is still queued
    ledger = InFlightWorkLedger(state, harness.claims)
    assert ledger.holds(session.terminal_id) is not None


# ---------------------------------------------------------------------------
# The authoritative claim store (#6999 F4/F7)
# ---------------------------------------------------------------------------


class _UntouchableClaimStore:
    """A claim store that must never be called (#6999 F9)."""

    def __getattr__(self, name: str):
        def _refuse(*args, **kwargs):
            raise AssertionError(f"claimless settlement called {name}")

        return _refuse


def _run(tmp_path: Path, name: str = "issue-7", run_id: str = "run-1"):
    """A minimal owned run root, the way the launcher would allocate one."""
    from tests.unit.session_run_helpers import make_session_run_assets

    worktree = tmp_path / f"wt-{name}-{run_id}"
    worktree.mkdir(parents=True, exist_ok=True)
    return make_session_run_assets(worktree, session_name=name, run_id=run_id)


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_every_claim_kind_round_trips_through_the_authoritative_store(
    queue, tmp_path: Path
) -> None:
    """Encoding is explicit per kind, so every kind needs proving."""
    from issue_orchestrator.domain.pending_work import PendingWorkKind

    state = _pending_state(queue)
    claim = _claim(queue, state)
    store = _claims(tmp_path)
    run = _run(tmp_path)

    store.hold_pending_work_claim(run, issue_number=7, claim=claim)
    restored = store.look_up_pending_work_claim(run).held

    assert restored is not None
    assert restored.kind is PendingWorkKind(queue)
    original_key = getattr(claim.request, "issue_key", None)
    restored_key = getattr(restored.request, "issue_key", None)
    if original_key is not None:
        # An IssueKey returns as a GitHubIssueKey, which is the protocol's own
        # definition of the same work item: identity is structural over scope
        # and stable id. (Production only ever stores GitHubIssueKey, so this
        # is exact there; these fixtures use FakeIssueKey.)
        assert restored_key is not None
        assert restored_key.scope() == original_key.scope()
        assert restored_key.stable_id() == original_key.stable_id()
        assert replace(restored.request, issue_key=original_key) == claim.request
    else:
        assert restored.request == claim.request  # every field, structurally


def test_reading_a_run_that_holds_no_claim_is_absent(tmp_path: Path) -> None:
    assert (
        _claims(tmp_path).look_up_pending_work_claim(_run(tmp_path)).state
        is ClaimState.ABSENT
    )


def test_consuming_a_claim_is_idempotent(tmp_path: Path) -> None:
    store = _claims(tmp_path)
    run = _run(tmp_path)

    store.consume_pending_work_claim(run)  # nothing to consume
    store.hold_pending_work_claim(run, issue_number=7, claim=_claim("review", _pending_state("review")))
    store.consume_pending_work_claim(run)
    store.consume_pending_work_claim(run)

    assert store.look_up_pending_work_claim(run).state is ClaimState.ABSENT


def test_re_holding_the_same_claim_is_idempotent(tmp_path: Path) -> None:
    """A retried adoption of a run that already recorded this claim is a no-op."""
    store = _claims(tmp_path)
    run = _run(tmp_path)
    claim = _claim("tech_lead", _pending_state("tech_lead"))

    store.hold_pending_work_claim(run, issue_number=7, claim=claim)
    store.hold_pending_work_claim(run, issue_number=7, claim=claim)

    assert store.look_up_pending_work_claim(run).held is not None


def test_a_conflicting_authoritative_write_fails_loudly(tmp_path: Path) -> None:
    """Create-once: one run does one piece of work.

    Overwriting would destroy the only record of the first claim, which is the
    failure this whole boundary exists to prevent (#6999 F7).
    """
    from issue_orchestrator.ports.pending_work_claim_store import (
        ConflictingPendingWorkClaimError,
    )

    store = _claims(tmp_path)
    run = _run(tmp_path)
    first = _claim("tech_lead", _pending_state("tech_lead"))
    store.hold_pending_work_claim(run, issue_number=7, claim=first)

    with pytest.raises(ConflictingPendingWorkClaimError, match="already holds"):
        store.hold_pending_work_claim(run, issue_number=7, claim=_claim("review", _pending_state("review")))

    restored = store.look_up_pending_work_claim(run).held
    assert restored is not None
    assert restored.kind is first.kind


def test_a_rewritten_run_identity_is_refused_rather_than_read_as_absent(
    tmp_path: Path,
) -> None:
    """Rows are validated against the identity they were asked for.

    The identity comes from the worktree manifest, which the agent can write.
    Refusing here is what turns a rewritten manifest into a quarantined
    terminal rather than a silently claimless one.
    """
    from dataclasses import replace as dc_replace

    from issue_orchestrator.execution.pending_work_codec import (
        PendingWorkClaimDecodeError,
    )

    store = _claims(tmp_path)
    run = _run(tmp_path)
    store.hold_pending_work_claim(
        run, issue_number=7, claim=_claim("tech_lead", _pending_state("tech_lead"))
    )
    renamed = dc_replace(
        run, identity=dc_replace(run.identity, session_name="rework-9")
    )

    with pytest.raises(PendingWorkClaimDecodeError, match="rework-9"):
        store.look_up_pending_work_claim(renamed)


def test_an_undecodable_claim_is_never_read_as_absent(tmp_path: Path) -> None:
    """"No claim" and "a claim I cannot rebuild" are different facts.

    Returning None for the second would drop the only record of the work while
    looking like a clean restart.
    """
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
    from issue_orchestrator.execution.pending_work_codec import (
        PendingWorkClaimDecodeError,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    store = _claims(tmp_path)
    run = _run(tmp_path)
    store.hold_pending_work_claim(run, issue_number=7, claim=_claim("review", _pending_state("review")))
    corrupt = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    corrupt.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        ("{not json", os.path.normpath(str(run.run_dir))),
    )
    corrupt.commit()
    corrupt.close()

    with pytest.raises(PendingWorkClaimDecodeError):
        _claims(tmp_path).look_up_pending_work_claim(run)


# ---------------------------------------------------------------------------
# The claim ledger is not on the agent's side of the boundary (#6999 F7)
# ---------------------------------------------------------------------------


def test_the_authoritative_claim_is_not_written_into_the_agent_worktree(
    tmp_path: Path,
) -> None:
    """The run directory is handed to the agent and is writable by it.

    A claim stored there would let the launched agent rewrite which queue its
    own session is holding, which PR a restored rework targets, and which paths
    a tech-lead investigation admits as evidence roots - all of which
    restoration accepts as orchestrator truth.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    run_dir = session.run_assets.run_dir
    assert run_dir.is_relative_to(session.worktree_path)  # agent-reachable
    planted = [p.name for p in run_dir.rglob("*") if "claim" in p.name.lower()]
    assert planted == []
    # And it really was recorded - just somewhere the agent cannot reach.
    assert harness.claims.look_up_pending_work_claim(session.run_assets).held is not None


def test_a_worktree_side_claim_file_cannot_change_what_is_restored(
    tmp_path: Path,
) -> None:
    """An agent-planted shadow is inert: restoration never reads the worktree."""
    from issue_orchestrator.domain.pending_work import PendingWorkKind

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    # The agent writes a claim of its own choosing into its own run directory.
    forged = _claim("rework", _pending_state("rework"))
    (session.run_assets.run_dir / "pending-work-claim.json").write_text(
        json.dumps(
            {"schema_version": 1, "kind": "rework", "request": {"pr_number": 999}}
        ),
        encoding="utf-8",
    )
    assert forged.kind is PendingWorkKind.REWORK

    restarted, restored = _restart(state, session, harness)

    # Still the tech-lead investigation the orchestrator recorded, untouched.
    held = InFlightWorkLedger(restarted, harness.claims).holds(restored.terminal_id)
    assert held is not None
    assert held.kind is PendingWorkKind.TECH_LEAD
    assert restored.pr_number is None  # the forged PR identity never applied


# ---------------------------------------------------------------------------
# An unreadable claim quarantines its terminal, not its neighbours (#6999 F6)
# ---------------------------------------------------------------------------


def _corrupt_stored_claim(tmp_path: Path, run) -> None:
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
    from issue_orchestrator.infra.repo_identity import state_dir

    conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        ("{not json", os.path.normpath(str(run.run_dir))),
    )
    conn.commit()
    conn.close()


def _restore_pair(state, sessions, harness):
    """Run the REAL restoration seam over several live terminals at once."""
    from issue_orchestrator.control.session_restorer import SessionRestorer
    from issue_orchestrator.control.session_routing import restore_running_sessions
    from issue_orchestrator.domain.models import OrchestratorState
    from issue_orchestrator.ports.session_runner import DiscoveredSession
    from tests.unit.test_session_restorer import MockRepositoryHost, MockWorkingCopy

    del state
    restarted = OrchestratorState()
    repo_host = MockRepositoryHost()
    working_copy = MockWorkingCopy()
    discovered = []
    for session in sessions:
        repo_host.issues[session.issue.number] = session.issue
        working_copy.branches[session.worktree_path] = session.branch_name or "b"
        discovered.append(
            DiscoveredSession(
                issue_number=session.issue.number,
                tab_name="",
                is_review=False,
                session_name=session.terminal_id,
                run_dir=str(session.run_assets.run_dir),
            )
        )
    added = restore_running_sessions(
        discovered,
        restarted,
        SessionRestorer(harness.launcher.config, repo_host, working_copy),
        harness.claims,
        _quarantine(harness),
    )
    quarantined = [
        e for e in harness.event_names() if e == EventName.SESSION_CLAIM_UNREADABLE.value
    ]
    return restarted, added, quarantined


def test_an_unreadable_claim_quarantines_only_its_own_terminal(
    tmp_path: Path,
) -> None:
    """One bad record must not stop an orchestrator restarting.

    The healthy neighbour restores with its claim intact; the corrupt one is
    reported for a human instead.
    """
    harness = _ready_harness(tmp_path)
    healthy_state = _pending_state("review")
    healthy = _route("review", healthy_state, harness)
    corrupt_state = _pending_state("tech_lead")
    corrupt = _route("tech_lead", corrupt_state, harness)
    assert healthy is not None and corrupt is not None
    _corrupt_stored_claim(tmp_path, corrupt.run_assets)

    restarted, added, quarantined = _restore_pair(
        None, [healthy, corrupt], harness
    )

    assert [s.terminal_id for s in added] == [healthy.terminal_id]
    assert len(quarantined) == 1  # announced exactly once, for the corrupt one
    ledger = InFlightWorkLedger(restarted, harness.claims)
    assert ledger.holds(healthy.terminal_id) is not None


def test_a_quarantined_terminal_never_settles_as_claimless(tmp_path: Path) -> None:
    """The loss F6 named: an active terminal with no ledger entry.

    Its completion would take the ordinary claimless path and discard the
    queued request the unreadable record described. Keeping it out of
    active_sessions is what makes that unreachable.
    """
    harness = _ready_harness(tmp_path)
    corrupt_state = _pending_state("tech_lead")
    corrupt = _route("tech_lead", corrupt_state, harness)
    assert corrupt is not None
    _corrupt_stored_claim(tmp_path, corrupt.run_assets)

    restarted, added, quarantined = _restore_pair(None, [corrupt], harness)

    assert added == []
    assert len(quarantined) == 1  # a human was told
    # Never entered ordinary processing, so nothing can settle it away.
    assert restarted.active_sessions == []
    assert restarted.in_flight_work == []


# ---------------------------------------------------------------------------
# A crash during settlement is recoverable with no live terminal (#6999 F8)
# ---------------------------------------------------------------------------


def _recover_with_no_terminals(state, harness):
    """Startup with the ledger intact but NOTHING discoverable.

    The crash case: the run ended (or the process died) while its settlement
    was in flight, so no discovery will ever surface that terminal again. Only
    the enumerable ledger can bring the work back.
    """
    from unittest.mock import MagicMock

    from issue_orchestrator.control.session_routing import restore_running_sessions
    from issue_orchestrator.domain.models import OrchestratorState

    del state
    restarted = OrchestratorState()
    restorer = MagicMock()
    restorer.restore_sessions.return_value = []
    restore_running_sessions(
        [], restarted, restorer, harness.claims, _quarantine(harness)
    )
    return restarted


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_a_crash_before_the_deferral_lands_still_recovers_the_work(
    queue, tmp_path: Path
) -> None:
    """The run took the work and then the process died. Nothing settled it.

    Discovery cannot help - the terminal is gone - so without an enumerable
    ledger the request would sit in the store forever. For a tech-lead failure
    investigation that row is the only record of the investigation there is.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state(queue)
    session = _route(queue, state, harness)
    assert session is not None
    assert _pending_count(state, queue) == 0

    restarted = _recover_with_no_terminals(state, harness)

    assert _pending_count(restarted, queue) == 1


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_a_crash_after_the_deferral_lands_still_recovers_the_work(
    queue, tmp_path: Path
) -> None:
    """The durable half committed, the in-memory re-queue did not survive.

    Deferral marks the row and keeps it. If it deleted the row at that moment
    the only surviving copy of the request would be an in-memory list, and this
    restart would lose it.
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state(queue)
    session = _route(queue, state, harness)
    assert session is not None
    # Real settlement: the durable transition AND the in-memory re-queue.
    _terminate_on_provider(state, session, ProviderErrorType.AUTH, harness)
    assert _pending_count(state, queue) == 1

    # ...and now the process dies, taking the in-memory queue with it.
    restarted = _recover_with_no_terminals(state, harness)

    assert _pending_count(restarted, queue) == 1


def test_a_recovered_failure_investigation_keeps_its_typed_trigger(
    tmp_path: Path,
) -> None:
    """Recovery must return the work, not a husk of it."""
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    restarted = _recover_with_no_terminals(state, harness)

    returned = restarted.pending_tech_lead_reviews[0]
    assert returned.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
    assert returned.failure is not None
    assert returned.failure.blocking_label == "blocked-failed"
    assert returned.retryable_launch_failures == 0


def test_a_recovered_validation_retry_keeps_its_prompt_and_budget(
    tmp_path: Path,
) -> None:
    harness = _ready_harness(tmp_path)
    state = _pending_state("validation_retry")
    state.pending_validation_retries[0].original_prompt = "the original prompt"
    session = _route("validation_retry", state, harness)
    assert session is not None

    restarted = _recover_with_no_terminals(state, harness)

    returned = restarted.pending_validation_retries[0]
    assert returned.original_prompt == "the original prompt"
    assert returned.validation_error == "boom"
    assert returned.retry_count == 1


def test_recovery_leaves_live_work_alone(tmp_path: Path) -> None:
    """A running session's claim is not queued work; re-admitting it would
    launch a second session for work already in flight."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    InFlightWorkLedger(state, harness.claims).recover_unresolved(_quarantine(harness))

    assert state.pending_tech_lead_reviews == []
    assert harness.claims.look_up_pending_work_claim(session.run_assets).held is not None


def test_recovered_work_is_not_recovered_twice(tmp_path: Path) -> None:
    """The sweep runs on every orphan reconcile, not only at startup."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    restarted = _recover_with_no_terminals(state, harness)
    assert len(restarted.pending_tech_lead_reviews) == 1

    InFlightWorkLedger(restarted, harness.claims).recover_unresolved(
        _quarantine(harness)
    )

    assert len(restarted.pending_tech_lead_reviews) == 1
    # The row STAYS, deferred: an in-memory queue is not a durable destination,
    # so only a relaunch taking the same work may retire it (#6999 F8).
    unresolved = harness.claims.list_unresolved_claims()
    assert [u.deferred for u in unresolved] == [True]


def test_a_relaunch_supersedes_its_own_deferred_row(tmp_path: Path) -> None:
    """Otherwise the ledger would accumulate one stale row per attempt, and the
    next sweep would re-queue work that is already running again."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    first = _route("tech_lead", state, harness)
    assert first is not None
    _terminate_on_provider(state, first, ProviderErrorType.AUTH, harness)
    assert len(harness.claims.list_unresolved_claims()) == 1

    second = _route("tech_lead", state, harness)

    assert second is not None
    unresolved = harness.claims.list_unresolved_claims()
    assert len(unresolved) == 1
    assert not unresolved[0].deferred  # held by the new run, not deferred


# ---------------------------------------------------------------------------
# Worktree-controlled identity cannot bypass the authoritative row (#6999 F11)
# ---------------------------------------------------------------------------


def test_a_rewritten_started_at_is_refused(tmp_path: Path) -> None:
    """``started_at`` is part of the run identity and is NOT decorative.

    It comes from the worktree manifest and later becomes trusted tech-lead
    evidence chronology, so a store that recorded it without checking it would
    launder agent-controlled data into orchestrator authority.
    """
    from dataclasses import replace as dc_replace

    from issue_orchestrator.execution.pending_work_codec import (
        PendingWorkClaimDecodeError,
    )

    store = _claims(tmp_path)
    run = _run(tmp_path)
    store.hold_pending_work_claim(run, issue_number=7, claim=_claim("tech_lead", _pending_state("tech_lead")))
    retimed = dc_replace(
        run, identity=dc_replace(run.identity, started_at="1999-01-01T00:00:00+00:00")
    )

    with pytest.raises(PendingWorkClaimDecodeError, match="1999-01-01"):
        store.look_up_pending_work_claim(retimed)


def test_the_run_key_is_not_recomputed_through_symlinks(tmp_path: Path) -> None:
    """The run root lives in the agent-writable worktree.

    Resolving it on every access would let an agent retarget the key by turning
    its own run root into a symlink: the lookup would land on another run,
    return "no claim", and the terminal would be admitted as claimless rather
    than quarantined - the exact silent loss this ledger exists to prevent.
    """
    import shutil

    from tests.unit.session_run_helpers import make_session_run_assets

    store = _claims(tmp_path)
    run = _run(tmp_path)
    store.hold_pending_work_claim(run, issue_number=7, claim=_claim("review", _pending_state("review")))
    decoy = make_session_run_assets(
        run.worktree_path, session_name="issue-9", run_id="run-2"
    )
    store.hold_pending_work_claim(
        decoy, issue_number=9, claim=_claim("tech_lead", _pending_state("tech_lead"))
    )
    key_before = store.run_key_for(run)

    # The agent replaces its own run root with a symlink to the other run.
    shutil.rmtree(run.run_dir)
    run.run_dir.symlink_to(decoy.run_dir, target_is_directory=True)

    assert store.run_key_for(run) == key_before  # lexical, unmoved
    restored = store.look_up_pending_work_claim(run).held
    assert restored is not None
    assert restored.kind is PendingWorkKind.REVIEW  # still its OWN claim


# ---------------------------------------------------------------------------
# Quarantine has its own owner and its own rules (#6999 F12)
# ---------------------------------------------------------------------------


def _quarantined(harness, tmp_path: Path):
    from issue_orchestrator.control.in_flight_work import QuarantinedSession

    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    return QuarantinedSession(
        session,
        "payload unreadable",
        harness.claims.run_key_for(session.run_assets),
        harness.claims.quarantine_key_for(session.run_assets),
    )


def test_a_repeated_orphan_scan_quarantines_once(tmp_path: Path) -> None:
    """The orphan scan rediscovers an untracked terminal every 30 seconds.

    Each rediscovery must not add another comment to the issue.
    """
    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)
    owner = _quarantine_with(harness)

    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    comments = [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert len(comments) == 1  # told a human exactly once
    assert (
        harness.event_names().count(EventName.SESSION_CLAIM_UNREADABLE.value) == 1
    )
    # The LABEL is reasserted on every scan on purpose: it is shared with owners
    # that remove it when a session for the issue looks active, and a
    # quarantined terminal is deliberately not one of those (#6999 F12).
    labels = [a for a in harness.quarantine_actions if isinstance(a, AddLabelAction)]
    assert len(labels) == 3


def test_a_quarantine_survives_a_restart_without_re_commenting(
    tmp_path: Path,
) -> None:
    """The marker is durable, so a fresh process does not re-announce it."""
    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)
    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    harness.quarantine_actions.clear()

    # A brand-new owner over the SAME durable store, as after a restart.
    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    assert [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ] == []  # not re-announced
    # ...but the block is still reasserted, because it may have been removed.
    assert [a.label for a in harness.quarantine_actions] == ["needs-human"]


def test_a_failed_durable_escalation_is_retried_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    """A label/comment that did not land must not become the final state.

    Publishing the event anyway would show an operator a warning that vanishes
    on restart, and marking it escalated would mean nobody is ever told.
    """
    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)

    _quarantine_with(harness, applies=False).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    assert EventName.SESSION_CLAIM_UNREADABLE.value not in harness.event_names()
    harness.quarantine_actions.clear()
    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))  # next sweep
    assert len(harness.quarantine_actions) == 2  # really retried
    assert EventName.SESSION_CLAIM_UNREADABLE.value in harness.event_names()


def test_a_quarantine_does_not_reach_through_the_tech_lead_lifecycle(
    tmp_path: Path,
) -> None:
    """It has different provenance, identity and clearing rules (#6999 F12).

    The tech-lead needs-human lifecycle clears its marker as soon as ANY
    session for the issue is active - but a quarantined terminal is
    deliberately absent from active_sessions while still running, so borrowing
    that owner let a healthy sibling session retract the warning. The comment
    also has to describe the right kind of work: most quarantined claims are
    review, rework or validation-retry, not a failure investigation.
    """
    from issue_orchestrator.control.actions import AddCommentAction, AddLabelAction

    harness = _ready_harness(tmp_path)
    launcher_escalations: list = []
    harness.launcher.escalate_issue_needs_human = (
        lambda **kwargs: launcher_escalations.append(kwargs) or True
    )

    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(_quarantined(harness, tmp_path)))
    assert launcher_escalations == []  # its own owner, not that one
    labels = [a for a in harness.quarantine_actions if isinstance(a, AddLabelAction)]
    comments = [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert len(labels) == 1 and len(comments) == 1
    body = comments[0].comment
    assert "pending-work claim is unreadable" in body
    # Names every queue it could be, rather than a tech-lead investigation.
    assert "review, rework, validation retry or tech-lead investigation" in body


def test_two_runs_of_one_issue_quarantine_independently(tmp_path: Path) -> None:
    """A quarantine belongs to a RUN, not to an issue."""
    from issue_orchestrator.control.in_flight_work import QuarantinedSession

    harness = _ready_harness(tmp_path)
    first = _quarantined(harness, tmp_path)
    second_state = _pending_state("tech_lead")
    second_session = _route("tech_lead", second_state, harness)
    assert second_session is not None
    second = QuarantinedSession(
        second_session,
        "payload unreadable",
        harness.claims.run_key_for(second_session.run_assets),
        harness.claims.quarantine_key_for(second_session.run_assets),
    )
    assert first.quarantine_key != second.quarantine_key
    owner = _quarantine_with(harness)

    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(first))
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(second))
    assert (
        harness.event_names().count(EventName.SESSION_CLAIM_UNREADABLE.value) == 2
    )


# ---------------------------------------------------------------------------
# An upgrade must not delete in-flight claims (#6999 F13)
# ---------------------------------------------------------------------------


def _write_legacy_claim_row(
    db_path: Path, *, run_key: str, claim, identity=None
) -> None:
    """A row in the shape the previous release wrote."""
    import sqlite3

    from issue_orchestrator.execution.pending_work_codec import encode_claim

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_work_claim (
            run_key TEXT PRIMARY KEY,
            session_name TEXT NOT NULL,
            run_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO pending_work_claim VALUES (?, ?, ?, ?, ?)",
        (
            run_key,
            identity.session_name if identity else "issue-7",
            identity.run_id if identity else "run-1",
            identity.started_at if identity else "2026-08-07T00:00:00+00:00",
            json.dumps(encode_claim(claim), sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()


def test_an_upgrade_teaches_an_older_quarantine_table_the_typed_cause(
    tmp_path: Path,
) -> None:
    """A quarantine row written before the cause was durable still works.

    ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as it was,
    so the columns #6999 F6 added arrive only if something adds them. Without
    that, every read and write of a quarantine on an upgraded database raises
    and the orphan sweep stops escalating anything at all.

    A carried-forward row has no recorded cause, which must read as *different
    from whatever is observed next* so the next scan re-announces under a cause
    it can vouch for, rather than standing on a story nothing recorded.
    """
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir
    from issue_orchestrator.ports.pending_work_claim_store import QuarantineCause

    db_path = state_dir(tmp_path) / STORE_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pending_work_claim_quarantine (
            quarantine_key TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            session_name TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            error TEXT NOT NULL,
            label_state TEXT NOT NULL DEFAULT 'unknown',
            announced INTEGER NOT NULL DEFAULT 0,
            releasing INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO pending_work_claim_quarantine
            (quarantine_key, run_key, session_name, issue_number, error,
             label_state, announced, releasing)
        VALUES ('/runs/a@t1', '/runs/a', 'issue-7', 7, 'legacy', 'acquired', 1, 0);
        """
    )
    conn.commit()
    conn.close()

    store = SqlitePendingWorkClaimStore(db_path)

    (carried,) = store.list_quarantines()
    assert carried.issue_number == 7
    assert carried.announced  # the old flag survived...
    assert carried.cause is None  # ...but says nothing about which story
    assert not carried.announces(QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN)

    store.record_quarantine(
        "/runs/a@t1",
        run_key="/runs/a",
        session_name="issue-7",
        issue_number=7,
        error="still unreadable",
        cause=QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN,
        work_kind=None,
    )

    refreshed = store.read_quarantine("/runs/a@t1")
    assert refreshed is not None
    assert refreshed.cause is QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN
    assert not refreshed.announced  # re-announced under the cause it now holds
    assert refreshed.block_is_ours  # ...without disturbing the block it owns


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_an_upgrade_carries_every_in_flight_claim_forward(
    queue, tmp_path: Path
) -> None:
    """Dropping the old table would delete the only copy of live work.

    That branch existed because terminal discovery CANNOT reconstruct a typed
    queued request - which is precisely why the rows must survive the upgrade
    (#6999 F13).
    """
    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    state = _pending_state(queue)
    claim = _claim(queue, state)
    run = _run(tmp_path)
    _write_legacy_claim_row(
        state_dir(tmp_path) / STORE_FILENAME,
        run_key=os.path.normpath(str(run.run_dir)),
        claim=claim,
        identity=run.identity,
    )

    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    unresolved = store.list_unresolved_claims()
    assert len(unresolved) == 1
    assert unresolved[0].claim.kind is claim.kind
    assert not unresolved[0].deferred  # carried over as held, as written
    assert store.look_up_pending_work_claim(run).held is not None


def test_an_unmigratable_row_fails_startup_and_keeps_the_old_table(
    tmp_path: Path,
) -> None:
    """Migration is all-or-nothing, and the legacy table stays authoritative.

    Archiving the bad row and carrying on was worse than the drop it replaced:
    every lookup, enumeration and recovery path queries only the live table, so
    an archived row is operationally invisible - a surviving terminal for it
    reads ABSENT and can be admitted claimless (#6999 F13).
    """
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        PendingWorkClaimMigrationError,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    db_path = state_dir(tmp_path) / STORE_FILENAME
    _write_legacy_claim_row(
        db_path, run_key="/runs/a", claim=_claim("review", _pending_state("review"))
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pending_work_claim VALUES (?, ?, ?, ?, ?)",
        ("/runs/broken", "issue-8", "run-2", "2026-08-07T00:00:00+00:00", "{not json"),
    )
    conn.commit()
    conn.close()

    with pytest.raises(PendingWorkClaimMigrationError, match="/runs/broken"):
        SqlitePendingWorkClaimStore.for_repo(tmp_path)

    # Nothing was renamed, created or dropped: the original authority is intact
    # and still holds BOTH rows for a human to work with.
    survivors = sqlite3.connect(db_path).execute(
        "SELECT run_key FROM pending_work_claim ORDER BY run_key"
    ).fetchall()
    assert [row[0] for row in survivors] == ["/runs/a", "/runs/broken"]
    tables = sqlite3.connect(db_path).execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    assert [t[0] for t in tables] == ["pending_work_claim"]


def test_a_repaired_legacy_row_migrates_on_the_next_start(tmp_path: Path) -> None:
    """Failing startup is recoverable, not a dead end."""
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        PendingWorkClaimMigrationError,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    db_path = state_dir(tmp_path) / STORE_FILENAME
    _write_legacy_claim_row(
        db_path, run_key="/runs/a", claim=_claim("review", _pending_state("review"))
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pending_work_claim VALUES (?, ?, ?, ?, ?)",
        ("/runs/broken", "issue-8", "run-2", "2026-08-07T00:00:00+00:00", "{not json"),
    )
    conn.commit()
    conn.close()
    with pytest.raises(PendingWorkClaimMigrationError):
        SqlitePendingWorkClaimStore.for_repo(tmp_path)

    # A human removes the unreadable row.
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM pending_work_claim WHERE run_key = '/runs/broken'")
    conn.commit()
    conn.close()

    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    assert [u.run_key for u in store.list_unresolved_claims()] == ["/runs/a"]


# ---------------------------------------------------------------------------
# Recovery reaches the real seams, and survives a second restart (#6999 F8)
# ---------------------------------------------------------------------------


def test_the_ledger_sweep_runs_when_nothing_is_discovered(tmp_path: Path) -> None:
    """The branch that returns early is exactly where the sweep matters.

    A run whose terminal is already gone is invisible to discovery, so "nothing
    untracked was found" is the case where its ledger row is the only remaining
    record of the work. Driven through the shared operation; the two REAL
    callers are pinned separately, in test_startup_manager and test_orchestrator.
    """
    from issue_orchestrator.control.session_routing import recover_unresolved_work

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    state.active_sessions.clear()  # the terminal is gone
    state.pending_tech_lead_reviews.clear()

    recover_unresolved_work(state, harness.claims, _quarantine(harness))

    assert len(state.pending_tech_lead_reviews) == 1


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_a_second_restart_before_relaunch_still_has_the_work(
    queue, tmp_path: Path
) -> None:
    """Recovery re-admits into an in-memory queue, which is not durable.

    So the row must stay authoritative until a relaunch takes the same work
    again; otherwise the second restart before that relaunch loses it (#6999 F8).
    """
    harness = _ready_harness(tmp_path)
    state = _pending_state(queue)
    session = _route(queue, state, harness)
    assert session is not None
    once = _recover_with_no_terminals(state, harness)
    assert _pending_count(once, queue) == 1

    twice = _recover_with_no_terminals(once, harness)

    assert _pending_count(twice, queue) == 1


def test_a_still_discovered_run_whose_claim_is_deferred_is_not_admitted(
    tmp_path: Path,
) -> None:
    """Its work is back on a queue; admitting it would let it settle that work.

    The lookup keeps deferred distinct from absent precisely so this terminal
    cannot be treated as carrying nothing (#6999 F8).
    """
    from issue_orchestrator.control.in_flight_work import InFlightWorkLedger

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    harness.claims.defer_pending_work_claim(session.run_assets)

    restoration = InFlightWorkLedger(state, harness.claims).rehydrate([session])

    assert restoration.admitted == ()
    assert [s.session.terminal_id for s in restoration.stale] == [
        session.terminal_id
    ]


# ---------------------------------------------------------------------------
# A quarantined run stays ineligible for orphan recovery (#6999 F11)
# ---------------------------------------------------------------------------


def test_repeated_scans_never_admit_a_mismatched_identity_terminal(
    tmp_path: Path,
) -> None:
    """The bypass F11 named: quarantine once, then recover-and-delete the row.

    A quarantined run is deliberately absent from active_sessions, so without
    passing every OBSERVED run key into recovery its row looks orphaned, gets
    re-queued and retired - after which the same terminal reads as claimless
    and is admitted normally on the next scan.
    """
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
    from issue_orchestrator.infra.repo_identity import state_dir

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    # The agent rewrites its manifest identity; the row no longer matches.
    conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    conn.execute(
        "UPDATE pending_work_claim SET started_at = ? WHERE run_key = ?",
        ("1999-01-01T00:00:00+00:00", harness.claims.run_key_for(session.run_assets)),
    )
    conn.commit()
    conn.close()

    for _ in range(3):
        restarted, added, _ = _restore_pair(None, [session], harness)
        assert added == []  # never admitted, on any scan
        assert restarted.active_sessions == []
        # ...and its row is still authoritative, never re-queued beside it.
        assert len(harness.claims.list_unresolved_claims()) == 1
        assert restarted.pending_tech_lead_reviews == []


def test_an_unresolved_review_claim_escalates_its_ISSUE_not_its_PR(
    tmp_path: Path,
) -> None:
    """The trusted issue number comes from the ledger, not the terminal name.

    A review terminal is named ``review-<pr_number>``, so deriving the issue
    from that name labels and comments on the PR instead of the issue that
    owns the work - and the payload, the other place it lives, is exactly what
    has become unreadable (#6999 F12).
    """
    from issue_orchestrator.control.actions import AddCommentAction, AddLabelAction
    from issue_orchestrator.ports.pending_work_claim_store import UnreadableClaim

    harness = _ready_harness(tmp_path)

    _quarantine_with(harness).quarantine(
        QuarantineSubject.ended_run_with_unreadable_claim(
            UnreadableClaim(
                run_key="/runs/review-70",
                session_name="review-70",  # named for the PR
                issue_number=7,  # recorded at hold time, from the session's issue
                error="payload unreadable",
                started_at="2026-08-07T00:00:00+00:00",
            )
        )
    )

    labels = [a for a in harness.quarantine_actions if isinstance(a, AddLabelAction)]
    comments = [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert [a.issue_number for a in labels] == [7]
    assert [a.number for a in comments] == [7]


def test_an_unresolved_claim_quarantine_is_idempotent_across_sweeps(
    tmp_path: Path,
) -> None:
    """The ledger sweep runs on every reconcile, not only at startup."""
    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.ports.pending_work_claim_store import UnreadableClaim

    harness = _ready_harness(tmp_path)
    unreadable = UnreadableClaim(
        run_key="/runs/review-70",
        session_name="review-70",
        issue_number=7,
        error="payload unreadable",
        started_at="2026-08-07T00:00:00+00:00",
    )
    owner = _quarantine_with(harness)

    owner.quarantine(QuarantineSubject.ended_run_with_unreadable_claim(unreadable))
    owner.quarantine(QuarantineSubject.ended_run_with_unreadable_claim(unreadable))

    assert len(
        [a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)]
    ) == 1


def test_a_released_quarantine_stops_holding_the_issue_open(
    tmp_path: Path,
) -> None:
    """The explicit clear seam, including the block it owns.

    A quarantine ends when its cause is gone - never because another session
    for the issue happened to start - and the needs-human label it applied
    comes off with it.
    """
    from issue_orchestrator.control.actions import RemoveLabelAction

    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)
    owner = _quarantine_with(harness)
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    assert harness.claims.quarantined_issue_numbers() == frozenset({7})
    harness.quarantine_actions.clear()

    owner.release(quarantined.quarantine_key)

    assert harness.claims.quarantined_issue_numbers() == frozenset()
    removed = [
        a for a in harness.quarantine_actions if isinstance(a, RemoveLabelAction)
    ]
    assert [a.label for a in removed] == ["needs-human"]


def test_a_repaired_claim_releases_its_quarantine_on_the_next_restore(
    tmp_path: Path,
) -> None:
    """The production caller F12 said release() was missing.

    A human repairs the unreadable row; the next restoration reads it cleanly,
    so the marker and the block it owns must come off by themselves rather than
    holding the issue forever.
    """
    from issue_orchestrator.control.actions import RemoveLabelAction

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    _corrupt_stored_claim(tmp_path, session.run_assets)
    _restore_pair(None, [session], harness)
    assert harness.claims.quarantined_issue_numbers() == frozenset({7})
    harness.quarantine_actions.clear()

    # The human repairs the stored payload so the claim reads again.
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
    from issue_orchestrator.execution.pending_work_codec import encode_claim
    from issue_orchestrator.infra.repo_identity import state_dir

    conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
    conn.execute(
        "UPDATE pending_work_claim SET payload = ? WHERE run_key = ?",
        (
            json.dumps(
                encode_claim(_claim("tech_lead", _pending_state("tech_lead"))),
                sort_keys=True,
            ),
            harness.claims.run_key_for(session.run_assets),
        ),
    )
    conn.commit()
    conn.close()
    _restore_pair(None, [session], harness)

    assert harness.claims.quarantined_issue_numbers() == frozenset()
    removed = [
        a for a in harness.quarantine_actions if isinstance(a, RemoveLabelAction)
    ]
    assert [a.label for a in removed] == ["needs-human"]


def test_a_release_leaves_another_quarantines_block_alone(tmp_path: Path) -> None:
    """Two runs of one issue: clearing one must not unblock the other."""
    from issue_orchestrator.control.actions import RemoveLabelAction
    from issue_orchestrator.control.in_flight_work import QuarantinedSession

    harness = _ready_harness(tmp_path)
    first = _quarantined(harness, tmp_path)
    second_state = _pending_state("tech_lead")
    second_session = _route("tech_lead", second_state, harness)
    assert second_session is not None
    second = QuarantinedSession(
        second_session,
        "payload unreadable",
        harness.claims.run_key_for(second_session.run_assets),
        harness.claims.quarantine_key_for(second_session.run_assets),
    )
    assert first.quarantine_key != second.quarantine_key
    owner = _quarantine_with(harness)
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(first))
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(second))
    harness.quarantine_actions.clear()

    owner.release(first.quarantine_key)

    assert harness.claims.quarantined_issue_numbers() == frozenset({7})
    assert [
        a for a in harness.quarantine_actions if isinstance(a, RemoveLabelAction)
    ] == []



def test_a_replacement_run_reusing_the_directory_quarantines_independently(
    tmp_path: Path,
) -> None:
    """Run roots are named to the second and created with ``exist_ok``.

    So a replacement run of one session really can land on the same directory.
    An escalated marker from the previous generation must not suppress the new
    run's comment and event - the two are different terminals holding different
    work (#6999 F12). Forced deterministically: the same run root, a different
    recorded start instant, exactly as a same-second replacement produces.
    """
    from dataclasses import replace as dc_replace

    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.control.in_flight_work import QuarantinedSession

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    first = QuarantinedSession(
        session,
        "payload unreadable",
        harness.claims.run_key_for(session.run_assets),
        harness.claims.quarantine_key_for(session.run_assets),
    )
    owner = _quarantine_with(harness)
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(first))  # The first run finishes and its row goes; a replacement lands on the SAME
    # directory with its own start instant.
    harness.claims.consume_pending_work_claim(session.run_assets)
    replacement_assets = dc_replace(
        session.run_assets,
        identity=dc_replace(
            session.run_assets.identity, started_at="2026-08-07T12:00:00.500000+00:00"
        ),
    )
    assert harness.claims.run_key_for(replacement_assets) == first.run_key
    harness.claims.hold_pending_work_claim(
        replacement_assets,
        _claim("tech_lead", _pending_state("tech_lead")),
        issue_number=7,
    )
    second = QuarantinedSession(
        session,
        "payload unreadable",
        harness.claims.run_key_for(replacement_assets),
        harness.claims.quarantine_key_for(replacement_assets),
    )
    assert first.run_key == second.run_key  # the collision is real
    assert first.quarantine_key != second.quarantine_key

    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(second))
    comments = [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert len(comments) == 2  # each generation told a human on its own account
    assert (
        harness.event_names().count(EventName.SESSION_CLAIM_UNREADABLE.value) == 2
    )
    recorded = [
        q for q in harness.claims.list_quarantines() if q.run_key == first.run_key
    ]
    assert len(recorded) == 2  # two independent rows, one per generation


# ---------------------------------------------------------------------------
# Quarantine escalation/release is a durable state machine (#6999 F12)
# ---------------------------------------------------------------------------


def test_a_preexisting_block_is_never_removed_on_release(tmp_path: Path) -> None:
    """Adding a label that is already there SUCCEEDS.

    So "the apply worked" is not evidence this quarantine put it there, and
    removing it on release would silently retract a human's block.
    """
    from issue_orchestrator.control.actions import RemoveLabelAction
    from issue_orchestrator.ports.pending_work_claim_store import (
        QuarantineLabelState,
    )

    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)
    owner = _quarantine_with(harness, acquire=QuarantineLabelState.PREEXISTING)
    owner.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    assert (
        harness.claims.read_quarantine(quarantined.quarantine_key).label_state
        is QuarantineLabelState.PREEXISTING
    )
    harness.quarantine_actions.clear()

    owner.release(quarantined.quarantine_key)

    assert [
        a for a in harness.quarantine_actions if isinstance(a, RemoveLabelAction)
    ] == []
    assert harness.claims.read_quarantine(quarantined.quarantine_key) is None


def test_a_landed_label_with_a_failed_comment_is_retried_not_stranded(
    tmp_path: Path,
) -> None:
    """The two halves fail separately, so they are recorded separately.

    A single ``escalated`` bit cannot say "the label landed but the comment did
    not"; releasing on that state would delete the row and leave the label the
    quarantine really did add (#6999 F12).
    """
    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.ports.pending_work_claim_store import (
        QuarantineLabelState,
    )

    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)

    class _LabelOkCommentFails(_RecordingLabels):
        def announce(self, issue_number: int, comment: str) -> bool:
            super().announce(issue_number, comment)
            return False

    from issue_orchestrator.control.claim_quarantine import ClaimQuarantineOwner

    failing = ClaimQuarantineOwner(
        store=harness.claims,
        labels=_LabelOkCommentFails(harness),
        events=harness.events,
    )
    failing.quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    record = harness.claims.read_quarantine(quarantined.quarantine_key)
    assert record.label_state is QuarantineLabelState.ACQUIRED  # it really landed
    assert not record.announced  # ...and the comment did not
    assert EventName.SESSION_CLAIM_UNREADABLE.value not in harness.event_names()

    harness.quarantine_actions.clear()
    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))  # the next sweep

    assert [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert EventName.SESSION_CLAIM_UNREADABLE.value in harness.event_names()


def test_a_failed_block_removal_is_retried_by_the_next_reconciliation(
    tmp_path: Path,
) -> None:
    """Deleting the row first would leave nothing to retry from.

    The block would then stay on the issue forever, which is the failure the
    row's survival exists to prevent (#6999 F12).
    """
    from issue_orchestrator.control.actions import RemoveLabelAction
    from issue_orchestrator.control.claim_quarantine import ClaimQuarantineOwner

    harness = _ready_harness(tmp_path)
    quarantined = _quarantined(harness, tmp_path)
    _quarantine_with(harness).quarantine(QuarantineSubject.live_run_with_unreadable_claim(quarantined))
    class _RemovalFails(_RecordingLabels):
        def release_block(self, issue_number: int) -> bool:
            super().release_block(issue_number)
            return False

    failing = ClaimQuarantineOwner(
        store=harness.claims, labels=_RemovalFails(harness), events=harness.events
    )
    failing.release(quarantined.quarantine_key)

    # Still recorded, so there is something to retry from.
    assert harness.claims.read_quarantine(quarantined.quarantine_key) is not None
    harness.quarantine_actions.clear()

    _quarantine_with(harness).reconcile_released(frozenset())

    assert [
        a for a in harness.quarantine_actions if isinstance(a, RemoveLabelAction)
    ]
    assert harness.claims.read_quarantine(quarantined.quarantine_key) is None


class _LiveLabelApplier:
    """An ``ActionApplier``-shaped fake holding real live labels per issue.

    Production semantics, which the provenance rule reads (#6999 F12): adding a
    label that is already present is a SUCCESS carrying ``no_op=True``.
    """

    def __init__(self) -> None:
        self.live: dict[int, set[str]] = {}

    def apply(self, action):
        from issue_orchestrator.control.action_results import ActionResult
        from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction

        if isinstance(action, AddLabelAction):
            labels = self.live.setdefault(action.issue_number, set())
            already = action.label in labels
            labels.add(action.label)
            return ActionResult.ok(action, no_op=already)
        if isinstance(action, RemoveLabelAction):
            self.live.setdefault(action.issue_number, set()).discard(action.label)
        return ActionResult.ok(action)


def _factory_owner(harness, applier, tmp_path: Path):
    """The owner as the composition roots build it, over a live label set."""
    from issue_orchestrator.control.claim_quarantine import (
        build_claim_quarantine_owner,
    )
    from issue_orchestrator.control.label_manager import LabelManager

    return build_claim_quarantine_owner(
        store=harness.claims,
        action_applier=applier,
        label_manager=LabelManager(_routing_config(tmp_path)),
        events=harness.events,
    )


def test_a_second_quarantine_on_one_issue_does_not_strand_the_block(
    tmp_path: Path,
) -> None:
    """Two quarantines, one issue, one shared label - and only one owner of it.

    The second to escalate finds the label already there and records itself
    PREEXISTING, so it will never take it off. If the first one then dropped
    its row on release just because the issue was still quarantined, nothing
    would be left holding the obligation and the block would stay forever
    (#6999 F12).
    """
    from issue_orchestrator.control.label_manager import LabelManager

    harness = _ready_harness(tmp_path)
    applier = _LiveLabelApplier()
    owner = _factory_owner(harness, applier, tmp_path)
    needs_human = LabelManager(_routing_config(tmp_path)).needs_human
    for key, run in (("/runs/a@t1", "/runs/a"), ("/runs/b@t2", "/runs/b")):
        owner.quarantine(_unrestorable_subject(run, key.split("@", 1)[1]))
    states = {q.quarantine_key: q.label_state for q in harness.claims.list_quarantines()}
    assert states["/runs/a@t1"] is QuarantineLabelState.ACQUIRED
    assert states["/runs/b@t2"] is QuarantineLabelState.PREEXISTING
    assert needs_human in applier.live[7]

    # The one that actually acquired the label is repaired first.
    owner.release("/runs/a@t1")

    assert needs_human in applier.live[7]  # the surviving quarantine still blocks
    assert harness.claims.read_quarantine("/runs/a@t1") is not None  # obligation kept

    # Now the survivor is repaired too, and the block finally comes off.
    owner.reconcile_released(frozenset())

    assert needs_human not in applier.live[7]
    assert harness.claims.list_quarantines() == ()


def test_a_block_whose_presence_could_not_be_checked_is_not_treated_as_ours(
    tmp_path: Path,
) -> None:
    """``AddLabel`` still succeeds when the presence check itself failed.

    It reports ``presence_unknown``, and an unprovable acquisition must not
    become a licence to remove the label later (#6999 F12).
    """
    from issue_orchestrator.control.action_results import ActionResult
    from issue_orchestrator.control.actions import RemoveLabelAction

    class _CannotCheckPresence:
        def __init__(self) -> None:
            self.applied: list = []

        def apply(self, action):
            self.applied.append(action)
            return ActionResult.ok(action, presence_unknown=True)

    harness = _ready_harness(tmp_path)
    applier = _CannotCheckPresence()
    owner = _factory_owner(harness, applier, tmp_path)
    owner.quarantine(_unrestorable_subject("/runs/a", "t1"))
    assert (
        harness.claims.read_quarantine("/runs/a@t1").label_state
        is QuarantineLabelState.PREEXISTING
    )
    applier.applied.clear()

    owner.release("/runs/a@t1")

    assert [a for a in applier.applied if isinstance(a, RemoveLabelAction)] == []
    assert harness.claims.read_quarantine("/runs/a@t1") is None


def test_a_rewritten_manifest_timestamp_does_not_mint_a_new_quarantine(
    tmp_path: Path,
) -> None:
    """Generation identity is anchored in the orchestrator's own record.

    Taking it from the run assets would let an agent rewrite its manifest and
    mint a fresh quarantine key on every scan, re-commenting forever (#6999
    F12).
    """
    from dataclasses import replace as dc_replace

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    original_key = harness.claims.quarantine_key_for(session.run_assets)

    # The agent rewrites the manifest's start instant between scans.
    for stamp in ("2030-01-01T00:00:00+00:00", "1999-01-01T00:00:00+00:00"):
        rewritten = dc_replace(
            session.run_assets,
            identity=dc_replace(session.run_assets.identity, started_at=stamp),
        )
        assert harness.claims.quarantine_key_for(rewritten) == original_key


def test_a_rewritten_manifest_across_real_scans_comments_once(
    tmp_path: Path,
) -> None:
    """The same thing, driven through the real restoration seam."""
    import sqlite3

    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.execution.pending_work_claim_store import STORE_FILENAME
    from issue_orchestrator.infra.repo_identity import state_dir

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    _corrupt_stored_claim(tmp_path, session.run_assets)

    for stamp in ("2030-01-01T00:00:00+00:00", "1999-01-01T00:00:00+00:00"):
        # Restoration rebuilds identity from the manifest each scan; rewrite it.
        conn = sqlite3.connect(state_dir(tmp_path) / STORE_FILENAME)
        conn.execute("UPDATE pending_work_claim SET started_at = started_at")
        conn.commit()
        conn.close()
        session.run_assets.manifest.path.write_text(
            json.dumps(
                {
                    **json.loads(session.run_assets.manifest.path.read_text()),
                    "started_at": stamp,
                }
            ),
            encoding="utf-8",
        )
        _restore_pair(None, [session], harness)

    comments = [
        a for a in harness.quarantine_actions if isinstance(a, AddCommentAction)
    ]
    assert len(comments) == 1


# ---------------------------------------------------------------------------
# A live run that cannot be rebuilt is protected, not requeued (#6999 F14)
# ---------------------------------------------------------------------------


def _restore_with_raw_discovery(harness, discovered):
    """The real restoration seam, given raw discovery records."""
    from issue_orchestrator.control.session_restorer import SessionRestorer
    from issue_orchestrator.control.session_routing import restore_running_sessions
    from issue_orchestrator.domain.models import OrchestratorState
    from tests.unit.test_session_restorer import MockRepositoryHost, MockWorkingCopy

    restarted = OrchestratorState()
    added = restore_running_sessions(
        discovered,
        restarted,
        SessionRestorer(
            harness.launcher.config, MockRepositoryHost(), MockWorkingCopy()
        ),
        harness.claims,
        _quarantine(harness),
    )
    return restarted, added


@pytest.mark.parametrize(
    "break_manifest",
    ["missing", "malformed", "identity_mismatch"],
    ids=["missing", "malformed", "identity-mismatch"],
)
def test_an_unverifiable_live_run_aborts_restore_and_keeps_its_claim(
    break_manifest, tmp_path: Path
) -> None:
    """Startup fails before unverifiable live work can be reinterpreted."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )
    from issue_orchestrator.ports.session_runner import DiscoveredSession

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    manifest = session.run_assets.manifest.path
    if break_manifest == "missing":
        manifest.unlink()
    elif break_manifest == "malformed":
        manifest.write_text("{not json", encoding="utf-8")
    else:
        payload = json.loads(manifest.read_text())
        payload["run_dir"] = str(manifest.parent.parent / "somewhere-else")
        manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        SessionConfigurationIdentityVerificationError,
        match="Cannot verify configuration identity",
    ):
        _restore_with_raw_discovery(
            harness,
            [
                DiscoveredSession(
                    issue_number=7,
                    tab_name="",
                    is_review=False,
                    session_name=session.terminal_id,
                    run_dir=str(session.run_assets.run_dir),
                )
            ],
        )

    # The durable work remains claimed while startup stops for operator repair.
    assert len(harness.claims.list_unresolved_claims()) == 1


def test_an_unverifiable_run_fails_every_restore_without_requeueing(
    tmp_path: Path,
) -> None:
    """Repeated restore attempts fail fast and preserve the durable claim."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )
    from issue_orchestrator.ports.session_runner import DiscoveredSession

    harness = _ready_harness(tmp_path)
    state = _pending_state("review")
    session = _route("review", state, harness)
    assert session is not None
    session.run_assets.manifest.path.unlink()
    discovered = [
        DiscoveredSession(
            issue_number=7,
            tab_name="",
            is_review=True,
            session_name=session.terminal_id,
            run_dir=str(session.run_assets.run_dir),
        )
    ]

    for _ in range(3):
        with pytest.raises(
            SessionConfigurationIdentityVerificationError,
            match="Cannot verify configuration identity",
        ):
            _restore_with_raw_discovery(harness, discovered)
        assert len(harness.claims.list_unresolved_claims()) == 1


def test_the_factory_reports_an_already_present_label_as_preexisting(
    tmp_path: Path,
) -> None:
    """The production mapping the release rule depends on (#6999 F12).

    ``ActionApplier`` reports adding an already-present label as a SUCCESS with
    ``no_op=True``. Collapsing that to a boolean is what let a quarantine
    believe it had acquired a label a human applied - and then remove it.
    """
    from issue_orchestrator.control.action_results import ActionResult
    from issue_orchestrator.control.claim_quarantine import (
        build_claim_quarantine_owner,
    )
    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.ports.pending_work_claim_store import (
        QuarantineLabelState,
    )

    class _AlreadyLabelled:
        def apply(self, action):
            return ActionResult.ok(action, no_op=True)

    class _NotYetLabelled:
        def apply(self, action):
            return ActionResult.ok(action)

    class _Failing:
        def apply(self, action):
            return ActionResult.fail(action, "github said no")

    labels = LabelManager(harness_config := _routing_config(tmp_path))
    del harness_config

    def _ops(applier):
        return build_claim_quarantine_owner(
            store=_claims(tmp_path),
            action_applier=applier,
            label_manager=labels,
            events=RecordingEvents(),
        ).labels

    assert _ops(_AlreadyLabelled()).acquire_block(7) is (
        QuarantineLabelState.PREEXISTING
    )
    assert _ops(_NotYetLabelled()).acquire_block(7) is QuarantineLabelState.ACQUIRED
    assert _ops(_Failing()).acquire_block(7) is QuarantineLabelState.UNKNOWN


def test_a_migration_interrupted_mid_copy_leaves_the_legacy_authority(
    tmp_path: Path,
) -> None:
    """Rename, create, copy and drop are one transaction (#6999 F13).

    ``executescript`` would commit before running its script, so a stop after
    the rename could leave a current-shaped table beside the renamed original -
    after which the column check returns early on the next start and the real
    rows are invisible. Injected mid-copy, then the database is REOPENED.
    """
    import sqlite3

    from issue_orchestrator.execution import pending_work_claim_store
    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    db_path = state_dir(tmp_path) / STORE_FILENAME
    run = _run(tmp_path)
    _write_legacy_claim_row(
        db_path,
        run_key=os.path.normpath(str(run.run_dir)),
        claim=_claim("tech_lead", _pending_state("tech_lead")),
        identity=run.identity,
    )
    # Fail inside the copy, after the rename and the new table exist.
    real_open = pending_work_claim_store.open_sqlite

    class _DiesMidCopy:
        def __init__(self, conn):
            self._conn = conn

        def executemany(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk went away mid-copy")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _wrapped(*args, **kwargs):
        return _DiesMidCopy(real_open(*args, **kwargs))

    pending_work_claim_store.open_sqlite = _wrapped
    try:
        with pytest.raises(sqlite3.OperationalError):
            SqlitePendingWorkClaimStore.for_repo(tmp_path)
    finally:
        pending_work_claim_store.open_sqlite = real_open

    # Reopened: the legacy table is intact under its own name, nothing was left
    # half-swapped, and the migration simply completes.
    store = SqlitePendingWorkClaimStore.for_repo(tmp_path)

    unresolved = store.list_unresolved_claims()
    assert [u.run_key for u in unresolved] == [os.path.normpath(str(run.run_dir))]
    assert store.look_up_pending_work_claim(run).held is not None


def test_a_half_swapped_database_refuses_to_start(tmp_path: Path) -> None:
    """A surviving ``_old`` table means authoritative rows nothing will read.

    The transaction makes this state unreachable, but if a database ever
    reaches it, starting quietly is the exact failure F13 exists to prevent:
    the column check passes on the current-shaped table, and the real claims
    sit in a table no lookup, enumeration or recovery path ever queries.
    """
    import sqlite3

    from issue_orchestrator.execution.pending_work_claim_store import (
        STORE_FILENAME,
        PendingWorkClaimMigrationError,
        SqlitePendingWorkClaimStore,
    )
    from issue_orchestrator.infra.repo_identity import state_dir

    db_path = state_dir(tmp_path) / STORE_FILENAME
    run = _run(tmp_path)
    _write_legacy_claim_row(
        db_path,
        run_key=os.path.normpath(str(run.run_dir)),
        claim=_claim("tech_lead", _pending_state("tech_lead")),
        identity=run.identity,
    )
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE pending_work_claim RENAME TO pending_work_claim_old")
    conn.execute(
        "CREATE TABLE pending_work_claim (run_key TEXT PRIMARY KEY, "
        "work_key TEXT NOT NULL, deferred INTEGER NOT NULL DEFAULT 0, "
        "session_name TEXT NOT NULL, run_id TEXT NOT NULL, "
        "started_at TEXT NOT NULL, issue_number INTEGER NOT NULL, "
        "payload TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    with pytest.raises(PendingWorkClaimMigrationError):
        SqlitePendingWorkClaimStore.for_repo(tmp_path)

    # ...and it said so without touching the rows a human still needs.
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM pending_work_claim_old").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# A live run that can be neither rebuilt nor identified (#6999 F2)
# ---------------------------------------------------------------------------


def _discovered(session, *, is_review: bool):
    from issue_orchestrator.ports.session_runner import DiscoveredSession

    return DiscoveredSession(
        issue_number=7,
        tab_name="",
        is_review=is_review,
        session_name=session.terminal_id,
        run_dir=str(session.run_assets.run_dir),
    )


def test_an_unverifiable_live_run_with_an_unreadable_claim_fails_fast(
    tmp_path: Path,
) -> None:
    """Configuration identity is verified before an unreadable claim is handled."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    session.run_assets.manifest.path.unlink()  # cannot be rebuilt...
    _corrupt_stored_claim(tmp_path, session.run_assets)  # ...and cannot be read

    with pytest.raises(
        SessionConfigurationIdentityVerificationError,
        match="Cannot verify configuration identity",
    ):
        _restore_with_raw_discovery(
            harness, [_discovered(session, is_review=False)]
        )

    assert harness.quarantine_actions == []
    assert not harness.claims.list_quarantines()


def test_the_unverifiable_combined_state_fails_every_repeated_scan(
    tmp_path: Path,
) -> None:
    """Every scan refuses to reinterpret a run whose identity is unverifiable."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("review")
    session = _route("review", state, harness)
    assert session is not None
    session.run_assets.manifest.path.unlink()
    _corrupt_stored_claim(tmp_path, session.run_assets)
    discovered = [_discovered(session, is_review=True)]

    for _ in range(3):
        with pytest.raises(
            SessionConfigurationIdentityVerificationError,
            match="Cannot verify configuration identity",
        ):
            _restore_with_raw_discovery(harness, discovered)

    assert harness.quarantine_actions == []
    assert not harness.claims.list_quarantines()


def _quarantine_narratives(harness) -> list[str]:
    """What a reader of the timeline is told, for each quarantine event.

    Projected through the production fan-out and timeline projection rather
    than read off the registry, because "the operator's story changed" is only
    true if the USER-visible narrative changed (#6999 F6).
    """
    from issue_orchestrator.events.fan_out_pipeline import produce_external_records
    from issue_orchestrator.timeline import project_timeline

    narratives: list[str] = []
    for index, event in enumerate(harness.events.published):
        name = (
            event.event_type.value
            if hasattr(event.event_type, "value")
            else str(event.event_type)
        )
        if not name.startswith("session.claim_unreadable") and not name.startswith(
            "session.run_unrestorable"
        ):
            continue
        records = produce_external_records(
            internal_event_name=name,
            enriched_data=dict(event.data),
            base_event_id=f"evt-{index}",
            timestamp_iso="2026-05-04T00:00:00+00:00",
        )
        narratives.extend(
            projected.narrative or ""
            for projected in project_timeline(list(records), issue_number=7)
        )
    return narratives


def _comment_texts(harness) -> list[str]:
    return [
        a.comment
        for a in harness.quarantine_actions
        if isinstance(a, AddCommentAction)
    ]


def _break_both_halves(harness, tmp_path: Path, queue: str):
    """One run that can be neither rebuilt nor identified, launched for real."""
    state = _pending_state(queue)
    session = _route(queue, state, harness)
    assert session is not None
    session.run_assets.manifest.path.unlink()  # cannot be rebuilt...
    _corrupt_stored_claim(tmp_path, session.run_assets)  # ...and cannot be read
    return session


def test_an_unverifiable_live_run_does_not_rewrite_an_existing_quarantine(
    tmp_path: Path,
) -> None:
    """A live run without configuration identity stops before quarantine mutation."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )
    from issue_orchestrator.ports.pending_work_claim_store import QuarantineCause

    harness = _ready_harness(tmp_path)
    session = _break_both_halves(harness, tmp_path, "tech_lead")

    # Pass 1: nothing discoverable, so the ledger sweep calls the run ended.
    _recover_with_no_terminals(None, harness)
    assert "already ended" in _comment_texts(harness)[0]
    assert EventName.SESSION_CLAIM_UNREADABLE.value in harness.event_names()

    # Pass 2: the terminal is discovered alive, but restoration cannot verify
    # which configuration owns it. Startup stops without reinterpreting the
    # existing quarantine or telling the operator a contradictory story.
    for _ in range(2):
        with pytest.raises(
            SessionConfigurationIdentityVerificationError,
            match="Cannot verify configuration identity",
        ):
            _restore_with_raw_discovery(
                harness, [_discovered(session, is_review=False)]
            )

    assert len(_comment_texts(harness)) == 1
    (record,) = harness.claims.list_quarantines()
    assert record.cause is QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN
    assert record.announced
    assert len(_quarantine_narratives(harness)) == 1


def test_an_unverifiable_live_run_can_be_reconciled_after_the_terminal_ends(
    tmp_path: Path,
) -> None:
    """Once the unverifiable terminal ends, the unreadable claim is quarantined."""
    from issue_orchestrator.control.session_restorer import (
        SessionConfigurationIdentityVerificationError,
    )
    from issue_orchestrator.ports.pending_work_claim_store import QuarantineCause

    harness = _ready_harness(tmp_path)
    session = _break_both_halves(harness, tmp_path, "review")

    with pytest.raises(
        SessionConfigurationIdentityVerificationError,
        match="Cannot verify configuration identity",
    ):
        _restore_with_raw_discovery(harness, [_discovered(session, is_review=True)])
    assert _comment_texts(harness) == []

    # The terminal is gone: discovery returns nothing at all.
    _recover_with_no_terminals(None, harness)

    comments = _comment_texts(harness)
    assert len(comments) == 1
    assert "already ended" in comments[0]
    assert "still running" not in comments[0]
    (record,) = harness.claims.list_quarantines()
    assert record.cause is QuarantineCause.CLAIM_UNREADABLE_ENDED_RUN
    assert len(_quarantine_narratives(harness)) == 1


def test_the_two_quarantine_causes_do_not_borrow_each_others_story(
    tmp_path: Path,
) -> None:
    """An unrestorable run's work is KNOWN, so it must not read as unknown.

    The shared escalation used to publish ``claim_unreadable`` and comment that
    the queued request could not be read for every cause. For a run whose claim
    is intact that is false, and it points the operator straight at a manual
    re-queue - a second session beside the one still running (#6999 A1).
    """
    harness = _ready_harness(tmp_path)
    owner = _quarantine_with(harness)

    owner.quarantine(_unrestorable_subject("/runs/a", "t1"))

    comment = next(
        a.comment
        for a in harness.quarantine_actions
        if isinstance(a, AddCommentAction)
    )
    assert "could not be rebuilt" in comment
    assert "Do not re-queue it by hand" in comment
    assert "is unknown" not in comment
    assert EventName.SESSION_RUN_UNRESTORABLE.value in harness.event_names()
    assert EventName.SESSION_CLAIM_UNREADABLE.value not in harness.event_names()


# ---------------------------------------------------------------------------
# A re-applied block becomes ours to remove (#6999 F3)
# ---------------------------------------------------------------------------


def test_a_reasserted_block_is_cleared_on_release(tmp_path: Path) -> None:
    """Provenance is re-decided on every pass, not frozen at the first one.

    A quarantine that first found ``needs-human`` already present records
    PREEXISTING and will not remove it. If a human then takes the label off, the
    next sweep re-applies it - and that apply is demonstrably OURS. Leaving the
    row saying PREEXISTING stranded the issue in ``needs-human`` forever: on
    release the row was deleted without removing the label it had re-added.
    """
    from issue_orchestrator.control.label_manager import LabelManager

    harness = _ready_harness(tmp_path)
    applier = _LiveLabelApplier()
    owner = _factory_owner(harness, applier, tmp_path)
    needs_human = LabelManager(_routing_config(tmp_path)).needs_human
    applier.live.setdefault(7, set()).add(needs_human)  # a human got there first
    subject = _unrestorable_subject("/runs/a", "t1")

    owner.quarantine(subject)
    assert not harness.claims.read_quarantine(subject.quarantine_key).block_is_ours

    applier.live[7].discard(needs_human)  # the human's block is lifted
    owner.quarantine(subject)  # the next sweep puts OUR block back

    assert harness.claims.read_quarantine(subject.quarantine_key).block_is_ours
    assert needs_human in applier.live[7]

    owner.reconcile_released(frozenset())

    assert needs_human not in applier.live[7]  # not stranded
    assert harness.claims.list_quarantines() == ()


def test_a_preexisting_block_that_is_never_reasserted_stays_preexisting(
    tmp_path: Path,
) -> None:
    """The upgrade is evidence-driven: only a real apply changes provenance.

    Re-scanning a quarantine whose block is still present must keep reporting
    PREEXISTING, or every repeat sweep would quietly claim a human's label.
    """
    from issue_orchestrator.control.label_manager import LabelManager

    harness = _ready_harness(tmp_path)
    applier = _LiveLabelApplier()
    owner = _factory_owner(harness, applier, tmp_path)
    needs_human = LabelManager(_routing_config(tmp_path)).needs_human
    applier.live.setdefault(7, set()).add(needs_human)
    subject = _unrestorable_subject("/runs/a", "t1")

    for _ in range(3):
        owner.quarantine(subject)

    assert not harness.claims.read_quarantine(subject.quarantine_key).block_is_ours

    owner.reconcile_released(frozenset())

    assert needs_human in applier.live[7]  # the human's block is left alone


def test_an_acquired_block_is_not_downgraded_by_a_later_pass(
    tmp_path: Path,
) -> None:
    """A later sweep finding the label present is finding the one we applied."""
    harness = _ready_harness(tmp_path)
    applier = _LiveLabelApplier()
    owner = _factory_owner(harness, applier, tmp_path)
    subject = _unrestorable_subject("/runs/a", "t1")

    owner.quarantine(subject)  # ACQUIRED: nothing was there before
    owner.quarantine(subject)  # a no-op apply must not demote it

    assert harness.claims.read_quarantine(subject.quarantine_key).block_is_ours


# ---------------------------------------------------------------------------
# The claim is durable BEFORE the terminal exists (#6999 A2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_the_claim_is_already_durable_when_the_terminal_spawns(
    queue, tmp_path: Path
) -> None:
    """The crash window A2 named: a live terminal with no durable claim.

    The claim used to be written only after a live ``Session`` came back, so a
    crash between the spawn and that write left an agent running work whose only
    other record was an in-memory queue. For a failure investigation that queue
    IS the only other record, so a restart could not recover it at all.
    """
    at_spawn: list[tuple[str, ...]] = []
    harness = _ready_harness(
        tmp_path,
        create_session=lambda name, cmd, wd, title: (
            at_spawn.append(
                tuple(u.claim.kind.value for u in harness.claims.list_unresolved_claims())
            )
            or True
        ),
    )
    state = _pending_state(queue)

    session = _route(queue, state, harness)

    assert session is not None
    # Observed from INSIDE the spawn: the ledger already knew.
    assert at_spawn == [(queue,)]


# Every event a launch publishes only once its terminal is genuinely running,
# plus the label transition a rework commits on the same assumption. None of
# them may appear when ``create_session`` said no (#6999 F5).
_SPAWN_ONLY_EFFECTS = (
    EventName.SESSION_STARTED.value,
    EventName.REVIEW_STARTED.value,
    EventName.REWORK_STARTED.value,
    EventName.PR_VIEW_CHANGED.value,
)

# Labels whose removal is what tells the orchestrator "this work is under way".
# Retiring one for a terminal that never started is how a rework loses the only
# thing that could re-queue it.
_LAUNCH_TRIGGER_LABELS = frozenset({"needs-rework", "review-first"})


@pytest.mark.parametrize("queue", _PENDING_QUEUES)
def test_a_launch_that_never_spawns_hands_the_work_back(
    queue, tmp_path: Path
) -> None:
    """Compensation, not a stranded row - on every queue (#6999 F5/A2).

    ``review`` and ``rework`` used to be exempt from this, because they reported
    success no matter what ``create_session`` answered. The consequences were
    not cosmetic: a phantom ``Session`` joined ``active_sessions``, its durable
    claim stayed HELD against a terminal that does not exist, the pending item
    was removed as though the work had started, and the rework path went on to
    publish REWORK_STARTED and strip the ``needs-rework`` trigger from the PR -
    deleting the one label that could have re-queued it.

    What every queue does instead: no terminal started, so the request is
    untouched and waiting to be relaunched - which is exactly what a DEFERRED
    row means. Deleting the row instead would be the failure this whole boundary
    exists to prevent: a relaunch of already-deferred work supersedes the old
    row as it takes the claim, so a failed relaunch that then deleted its own
    row would leave nothing durable behind at all.
    """
    harness = _ready_harness(
        tmp_path, create_session=lambda name, cmd, wd, title: False
    )
    state = _pending_state(queue)

    assert _route(queue, state, harness) is None

    # No phantom session, and nothing claiming the work began.
    assert state.active_sessions == []
    assert [n for n in harness.event_names() if n in _SPAWN_ONLY_EFFECTS] == []
    # ...and no trigger label was retired on behalf of a terminal that does not
    # exist. For rework that label IS the recovery path: removing it while the
    # launch failed would leave the PR with nothing to re-queue it from.
    applied = [c.args[0] for c in harness.action_applier.apply.call_args_list]
    assert [
        a
        for a in applied
        if isinstance(a, RemoveLabelAction) and a.label in _LAUNCH_TRIGGER_LABELS
    ] == []

    unresolved = harness.claims.list_unresolved_claims()
    assert [u.deferred for u in unresolved] == [True]
    assert [u.claim.kind.value for u in unresolved] == [queue]

    # ...and a fresh process recovers exactly that work, exactly once.
    restarted = _recover_with_no_terminals(state, harness)
    assert _pending_count(restarted, queue) == 1


# ---------------------------------------------------------------------------
# The launch transaction settles the queue AND the ledger together (#6999 F4)
# ---------------------------------------------------------------------------


def _failing_spawn_harness(tmp_path: Path):
    return _ready_harness(
        tmp_path, create_session=lambda name, cmd, wd, title: False
    )


def test_a_permanently_dropped_request_is_not_resurrected_by_a_restart(
    tmp_path: Path,
) -> None:
    """PERMANENT_FAILURE has to mean permanent on BOTH sides of the transaction.

    The queue side and the durable side used to settle independently: the
    settlement removed the item, while the unspawned-launch compensation had
    already deferred the row and never heard about the drop. A deferred row is
    exactly what the startup sweep is built to re-admit, so the very next
    restart put the dropped request back on its queue and launched it - a
    launcher that gave up, undone by the recovery that protects work it did not
    give up on.
    """
    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("review")

    # A transient spawn failure first, so a durable deferred row really exists.
    assert _route("review", state, harness) is None
    assert len(harness.claims.list_unresolved_claims()) == 1

    # Then the launcher gives up for good.
    harness.launcher.config.repo = None
    assert _route("review", state, harness) is None

    assert _pending_count(state, "review") == 0  # dropped from its queue...
    assert harness.claims.list_unresolved_claims() == ()  # ...and from the ledger
    restarted = _recover_with_no_terminals(state, harness)
    assert _pending_count(restarted, "review") == 0


def test_an_exhausted_investigation_is_not_resurrected_by_a_restart(
    tmp_path: Path,
) -> None:
    """An escalated, exhausted budget must not be refunded by recovery.

    The third failure commits a durable ``needs-human`` transition and drops the
    queued investigation - the bounded-retry policy's whole point. Leaving its
    deferred row behind let the next restart re-admit the investigation with the
    escalation still standing on the issue, so the "this will not retry on its
    own" comment was false and the bound was not a bound (#6999 F4).
    """
    from issue_orchestrator.control.pending_session_queues import (
        TECH_LEAD_LAUNCH_RETRY_LIMIT,
    )

    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("tech_lead")

    for _ in range(TECH_LEAD_LAUNCH_RETRY_LIMIT):
        assert _route("tech_lead", state, harness) is None

    assert state.pending_tech_lead_reviews == []
    assert harness.claims.list_unresolved_claims() == ()
    restarted = _recover_with_no_terminals(state, harness)
    assert restarted.pending_tech_lead_reviews == []


def test_a_restart_before_exhaustion_keeps_the_budget_already_spent(
    tmp_path: Path,
) -> None:
    """The other half: retained work must come back with its budget as spent.

    The durable payload is written when the claim is HELD - before the launch
    fails and therefore before the queue owner increments the retry count. A
    restart that read that payload refunded every attempt, so an investigation
    whose input is permanently broken could relaunch forever, two failures at a
    time, and never reach the escalation that tells a human (#6999 F4).
    """
    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("tech_lead")

    assert _route("tech_lead", state, harness) is None
    assert _route("tech_lead", state, harness) is None
    assert state.pending_tech_lead_reviews[0].retryable_launch_failures == 2

    restarted = _recover_with_no_terminals(state, harness)

    (recovered,) = restarted.pending_tech_lead_reviews
    assert recovered.retryable_launch_failures == 2
    # ...and the typed trigger context survived the round trip with it.
    assert recovered.failure is not None
    assert recovered.failure.issue_number == 7


def test_a_claim_store_that_cannot_record_stops_the_launch(tmp_path: Path) -> None:
    """A store fault must be survivable, which means failing BEFORE the spawn.

    Recorded after the terminal, the write had nowhere left to fail: the
    terminal was already irreversible. Before it, the launch simply does not
    happen, the queue item keeps its full budget, and the next tick retries.
    """
    from issue_orchestrator.control.launch_transaction import PendingWorkLaunchClaim
    from issue_orchestrator.control.session_launch_types import LaunchDisposition

    class _RefusingStore:
        def __init__(self, real):
            self._real = real
            self.deferred: list = []

        def hold_pending_work_claim(self, run, claim, *, issue_number):
            raise RuntimeError("ledger is unwritable")

        def defer_pending_work_claim(self, run):
            self.deferred.append(run)

        def __getattr__(self, name):
            return getattr(self._real, name)

    harness = _ready_harness(tmp_path)
    store = _RefusingStore(harness.claims)
    state = _pending_state("tech_lead")
    work = PendingWorkLaunchClaim(
        claim=_claim("tech_lead", state), claims=store
    )

    result = harness.launcher.launch_issue_session(
        Issue(7, "Investigate: session failed", ["agent:tech-lead"]),
        state.active_sessions,
        work_claim=work,
    )

    assert not result.success
    # Not a retryable failure: that disposition spends a unit of the queue's
    # bounded budget and makes the spend durable by rewriting a deferred row
    # this very write failed to create (#6999 F1 round 2).
    assert result.disposition is LaunchDisposition.CLAIM_UNRECORDED
    assert harness.created == []  # no terminal was spawned
    assert store.deferred == []  # nothing was held, so nothing to hand back
    assert len(state.pending_tech_lead_reviews) == 1


# ---------------------------------------------------------------------------
# The durable decision lands BEFORE the queue projection (#6999 F2)
# ---------------------------------------------------------------------------
#
# The queue and the ledger are not equally durable: a ledger row survives a
# restart, an in-memory queue does not. So the settlement commits its decision
# to the ledger first and then brings the queue into line with it. These tests
# interrupt the transaction in exactly that gap - the queue projection dies -
# and then restart, because a restart is what turns "the queue never heard" into
# either a resurrection or a refund.


class _Interrupted(RuntimeError):
    """The process died between the durable decision and its projection."""


def _die() -> None:
    raise _Interrupted("process died")


def _settlement_over(queue: str, state, harness, *, remove, plan_retry=None,
                     drop_on_permanent_failure: bool = True):
    """A settlement over the harness's own store, so the ledger is the real one."""
    from issue_orchestrator.control.launch_transaction import (
        LaunchSettlement,
        PendingWorkLaunchClaim,
        unbounded_retry,
    )

    return LaunchSettlement(
        work=PendingWorkLaunchClaim(
            claim=_claim(queue, state), claims=harness.claims
        ),
        remove=remove,
        plan_retry=plan_retry or unbounded_retry,
        drop_on_permanent_failure=drop_on_permanent_failure,
    )


def _unspawned(disposition: str):
    """An unspawned launch outcome, named by its disposition."""
    from issue_orchestrator.control.session_launch_types import (
        LaunchDisposition,
        LaunchResult,
    )

    return LaunchResult(
        session=None,
        success=False,
        reason="nothing started",
        disposition=LaunchDisposition[disposition],
    )


def test_a_drop_interrupted_before_the_queue_hears_is_still_permanent(
    tmp_path: Path,
) -> None:
    """Permanent drop, interrupted in the gap, then restarted.

    The queue removal used to run FIRST and the ledger second, so a death in
    between left the deferred row the startup sweep is built to re-admit - and
    the queue mutation it was protecting had evaporated with the process
    anyway. Committing the drop durably first inverts that: the only thing lost
    to the interruption is state a restart rebuilds from the ledger.
    """
    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("review")

    # A transient spawn failure first, so a real deferred row exists.
    assert _route("review", state, harness) is None
    assert len(harness.claims.list_unresolved_claims()) == 1

    settlement = _settlement_over("review", state, harness, remove=_die)
    with pytest.raises(_Interrupted):
        settlement.settle(_unspawned("PERMANENT_FAILURE"), state)

    # The projection never ran, so this process still shows the item queued...
    assert _pending_count(state, "review") == 1
    # ...but the durable decision had already committed, and the restart that
    # rebuilds every queue from the ledger re-admits nothing.
    assert harness.claims.list_unresolved_claims() == ()
    restarted = _recover_with_no_terminals(state, harness)
    assert _pending_count(restarted, "review") == 0


def test_a_store_that_cannot_retire_a_dropped_claim_leaves_the_item_queued(
    tmp_path: Path,
) -> None:
    """The store fault case: no durable decision, so no projection either.

    The ordering has to hold under a store that refuses, not only under a crash.
    A drop whose ledger write fails must not remove the queue item, or the work
    is gone from the only two places that hold it.
    """
    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("review")
    assert _route("review", state, harness) is None

    class _RefusingRetire:
        def __init__(self, real):
            self._real = real

        def retire_deferred_claim(self, work_key):
            raise RuntimeError("ledger is unwritable")

        def __getattr__(self, name):
            return getattr(self._real, name)

    removed: list[str] = []
    harness.claims = _RefusingRetire(harness.claims)
    settlement = _settlement_over(
        "review", state, harness, remove=lambda: removed.append("remove")
    )
    with pytest.raises(RuntimeError, match="ledger is unwritable"):
        settlement.settle(_unspawned("PERMANENT_FAILURE"), state)

    assert removed == []  # the queue was never told
    assert _pending_count(state, "review") == 1


def test_a_spent_retry_interrupted_before_the_queue_hears_is_not_refunded(
    tmp_path: Path,
) -> None:
    """Retained retry-budget advancement, interrupted in the gap.

    The budget lives in the queued request, so until the advanced request is in
    the ledger the attempt is not spent at all. It used to be spent on the queue
    object first and serialised afterwards, which meant an interruption anywhere
    in between refunded it: the restart read the pre-launch payload and gave the
    investigation its attempts back, so a permanently broken input could
    relaunch forever without ever reaching the escalation that tells a human.
    """
    from dataclasses import replace

    from issue_orchestrator.control.launch_transaction import RetryPlan
    from issue_orchestrator.domain.pending_work import (
        PendingWorkClaim,
        PendingWorkKind,
    )

    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("tech_lead")
    assert _route("tech_lead", state, harness) is None
    (queued,) = state.pending_tech_lead_reviews
    assert queued.retryable_launch_failures == 1

    spent = PendingWorkClaim(
        PendingWorkKind.TECH_LEAD,
        replace(queued, retryable_launch_failures=2),
    )
    settlement = _settlement_over(
        "tech_lead",
        state,
        harness,
        remove=lambda: pytest.fail("a retained item must never be dropped"),
        plan_retry=lambda claim: RetryPlan(
            spent=spent, exhausted=False, apply=_die, commit_exhaustion=lambda: False
        ),
    )
    with pytest.raises(_Interrupted):
        settlement.settle(_unspawned("RETRYABLE_FAILURE"), state)

    # This process never projected the spend onto its queue...
    assert queued.retryable_launch_failures == 1
    # ...but the ledger already had it, so the restart does not hand it back.
    restarted = _recover_with_no_terminals(state, harness)
    (recovered,) = restarted.pending_tech_lead_reviews
    assert recovered.retryable_launch_failures == 2
    # ...and the typed trigger context came back with it.
    assert recovered.failure is not None


def test_an_escalation_that_commits_is_never_refunded_by_the_restart(
    tmp_path: Path,
) -> None:
    """Committed exhausted escalation, interrupted before the ledger retire.

    The escalation is a GitHub transition and the retire is a SQLite write, so
    the two cannot be one atomic act; the ordering is a choice of which way to
    fail. Retiring first would let a death in the window discard an
    investigation with nobody told. Escalating first means the work comes back
    - but at its BOUND, never with a fresh budget, so the very next settlement
    re-asserts the same idempotent escalation and drops it rather than starting
    three more launches. One redundant attempt is recoverable; a silently
    discarded investigation is not.
    """
    from issue_orchestrator.control.pending_session_queues import (
        TECH_LEAD_LAUNCH_RETRY_LIMIT,
    )

    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("tech_lead")

    retired: list[str] = []

    class _DyingRetire:
        def __init__(self, real):
            self._real = real

        def retire_deferred_claim(self, work_key):
            retired.append(work_key)
            raise _Interrupted("process died")

        def __getattr__(self, name):
            return getattr(self._real, name)

    real_claims = harness.claims
    for _ in range(TECH_LEAD_LAUNCH_RETRY_LIMIT - 1):
        assert _route("tech_lead", state, harness) is None

    harness.claims = _DyingRetire(real_claims)
    with pytest.raises(_Interrupted):
        _route("tech_lead", state, harness)

    # The escalation DID commit - the human has been told - and the retire is
    # what died.
    assert retired == ["tech_lead:7"]
    assert EventName.ISSUE_NEEDS_HUMAN.value in harness.event_names()

    harness.claims = real_claims
    restarted = _recover_with_no_terminals(state, harness)
    (recovered,) = restarted.pending_tech_lead_reviews
    assert recovered.retryable_launch_failures == TECH_LEAD_LAUNCH_RETRY_LIMIT, (
        "the budget must come back spent; a refund would restart the whole bound"
    )

    # And the next settlement finishes what the interruption left: no third
    # bound to work through, just the same escalation and the drop.
    assert _route("tech_lead", restarted, harness) is None
    assert restarted.pending_tech_lead_reviews == []
    assert harness.claims.list_unresolved_claims() == ()


def test_an_unwritable_ledger_never_spends_a_budget_it_cannot_record(
    tmp_path: Path,
) -> None:
    """The whole production router, against a store that refuses every hold.

    The launcher's own contract has always been that a failed hold costs the
    request nothing: no terminal, no queue mutation, full budget, retry next
    tick. But the launcher does not settle - the router does - and settlement
    read a failed hold as an ordinary retryable failure. That spent a unit of
    the bounded budget on the queue object and "made it durable" by rewriting
    this work's deferred row, which the failed hold had never created. The
    UPDATE matched zero rows and said so to nobody.

    Two losses followed, and only the production path shows either: a death
    after that projection lost freshly queued work outright, because the ledger
    held nothing; and TECH_LEAD_LAUNCH_RETRY_LIMIT store faults in a row
    exhausted the bound and escalated an investigation that had never actually
    failed to launch.
    """
    from issue_orchestrator.control.pending_session_queues import (
        TECH_LEAD_LAUNCH_RETRY_LIMIT,
    )

    class _RefusingHoldStore:
        """Every durable hold fails; everything else is the real store."""

        def __init__(self, real):
            self._real = real
            self.refusals = 0

        def hold_pending_work_claim(self, run, claim, *, issue_number):
            self.refusals += 1
            raise RuntimeError("ledger is unwritable")

        def __getattr__(self, name):
            return getattr(self._real, name)

    harness = _ready_harness(tmp_path)
    real_claims = harness.claims
    harness.claims = _RefusingHoldStore(real_claims)
    state = _pending_state("tech_lead")

    # More attempts than the bound, so an over-spent budget would have
    # escalated and dropped the investigation by now.
    for _ in range(TECH_LEAD_LAUNCH_RETRY_LIMIT + 1):
        assert _route("tech_lead", state, harness) is None

    assert harness.claims.refusals == TECH_LEAD_LAUNCH_RETRY_LIMIT + 1
    assert harness.created == []  # nothing was ever spawned
    (queued,) = state.pending_tech_lead_reviews
    assert queued.retryable_launch_failures == 0, (
        "a ledger fault is not the work failing; the budget must be untouched"
    )
    assert EventName.ISSUE_NEEDS_HUMAN.value not in harness.event_names()

    # Nothing half-written: the ledger has no row for work it never accepted,
    # so a restart cannot resurrect a phantom claim either. The request itself
    # is not recoverable from the ledger and must not pretend to be - it was
    # never taken off anything durable, and the queue that holds it is
    # rebuilt by discovery.
    harness.claims = real_claims
    assert harness.claims.list_unresolved_claims() == ()
    assert harness.claims.list_unreadable_claims() == ()
    restarted = _recover_with_no_terminals(state, harness)
    assert restarted.pending_tech_lead_reviews == []

    # ...and once the store is writable again the very next launch proceeds
    # with the full budget the fault never spent.
    assert _route("tech_lead", state, harness) is not None
    assert harness.created == ["issue-7"]


def test_a_settlement_will_not_project_a_spend_the_ledger_did_not_take(
    tmp_path: Path,
) -> None:
    """The backstop, independent of which disposition got us here.

    ``refresh_deferred_claim`` is an UPDATE of this work's deferred row, so it
    reports whether one existed. Any settlement that spends a bounded budget
    has to consult that: a spend the ledger did not take is a spend that lives
    only in memory, and the ordering this transaction is built on says the
    queue may never move ahead of the ledger.
    """
    from issue_orchestrator.control.launch_transaction import RetryPlan

    harness = _failing_spawn_harness(tmp_path)
    state = _pending_state("tech_lead")
    (queued,) = state.pending_tech_lead_reviews
    projected: list[str] = []

    # No deferred row exists: nothing was ever held for this work.
    assert harness.claims.list_unresolved_claims() == ()

    settlement = _settlement_over(
        "tech_lead",
        state,
        harness,
        remove=lambda: pytest.fail("nothing may be dropped here"),
        plan_retry=lambda claim: RetryPlan(
            spent=claim,
            exhausted=True,
            apply=lambda: projected.append("apply"),
            commit_exhaustion=lambda: pytest.fail(
                "an uncommitted spend must never reach the escalation"
            ),
        ),
    )
    settlement.settle(_unspawned("RETRYABLE_FAILURE"), state)

    assert projected == []
    assert queued.retryable_launch_failures == 0
    assert _pending_count(state, "tech_lead") == 1
