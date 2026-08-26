"""Synthetic integration tests for completion command contract integrity.

These tests validate that completion command examples emitted by prompt
generators are executable by the real CLI entrypoints (`coding-done`
and `reviewer-done`).  This prevents drift where prompt text suggests
invalid command forms.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.control.actions import AddLabelAction, RemoveLabelAction
from issue_orchestrator.control.completion_handler import CompletionHandler
from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.control.tech_lead_run_activity import in_memory_run_activity
from issue_orchestrator.control.session_controller import SessionController
from issue_orchestrator.domain.models import AgentConfig, Issue, Session, SessionStatus
from issue_orchestrator.domain.models import CompletionOutcome
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
)
from issue_orchestrator.observation.observation import (
    SessionObservation,
    SessionObservationResult,
)
from issue_orchestrator.entrypoints.cli_tools.setup_wizard import (
    create_starter_prompt,
    create_tech_lead_review_prompt,
)
from issue_orchestrator.entrypoints.setup_wizard_prompts import (
    build_code_review_prompt_text,
    build_starter_prompt_text,
    build_tech_lead_review_prompt_text,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.resources import (
    get_coding_done_instructions,
    get_reviewer_done_instructions,
)
from tests.git_push_authorization import authorized_local_fixture_git_env
from tests.conftest import make_provider_availability
from tests.unit.session_run_helpers import make_session_run_assets

from .conftest import xdist_timeout
from tests.callback_endpoint_helpers import ready_callback_endpoint


@pytest.fixture(scope="module")
def lm() -> LabelManager:
    """Module-scoped label manager.

    Deferring construction out of module-import time avoids concurrent
    ``Config()`` initialization races observed under full-suite xdist runs
    (see issue #4391).
    """
    return LabelManager(Config())


_COMPLETION_CMDS = ("coding-done", "reviewer-done")
_CONTRACT_COMMAND_TIMEOUT_SECONDS = xdist_timeout(60)

# Match any fenced block (bash, json, bare, ...) so language-tagged fences
# keep open/close pairing intact; non-command content is filtered later by
# the startswith(_COMPLETION_CMDS) check.
_FENCED_BLOCK_RE = re.compile(r"```(?:[a-z]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*(?:coding-done|reviewer-done)[^`]*)`")


def _extract_completion_commands(text: str) -> list[str]:
    commands: list[str] = []

    for block in _FENCED_BLOCK_RE.findall(text):
        logical_lines: list[str] = []
        current = ""
        for raw_line in block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if not current:
                current = line
            else:
                current = f"{current} {line}"
            if current.endswith("\\"):
                current = current[:-1].strip()
                continue
            logical_lines.append(current)
            current = ""
        if current:
            logical_lines.append(current)

        for line in logical_lines:
            if any(line.startswith(f"{cmd} ") for cmd in _COMPLETION_CMDS):
                commands.append(line)

    for inline in _INLINE_CODE_RE.findall(text):
        line = inline.strip()
        if any(line.startswith(f"{cmd} ") for cmd in _COMPLETION_CMDS):
            commands.append(line)

    # Preserve order while deduping
    deduped: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        if cmd in seen:
            continue
        seen.add(cmd)
        deduped.append(cmd)
    return deduped


def _run_completion_command(
    command: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(command)
    if "--help" not in argv and "--dry-run" not in argv:
        argv.append("--dry-run")

    bin_name = argv[0]  # coding-done or reviewer-done
    cli_bin = Path(sys.executable).parent / bin_name
    assert cli_bin.exists(), f"{bin_name} not found at {cli_bin}"

    return subprocess.run(
        [str(cli_bin), *argv[1:]],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init"], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    (path / "README.md").write_text("test\n")
    (path / ".gitignore").write_text(".agent-done-marker\n.issue-orchestrator/\n")
    subprocess.run(
        ["git", "add", "."], cwd=path, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


_REVIEWER_STATUSES = {"approved", "changes_requested"}


def _bin_for_status(status: str) -> str:
    """Return the correct CLI binary name for a given status."""
    return "reviewer-done" if status in _REVIEWER_STATUSES else "coding-done"


def _run_completion_raw(
    argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    bin_name = _bin_for_status(argv[0])
    cli_bin = Path(sys.executable).parent / bin_name
    assert cli_bin.exists(), f"{bin_name} not found at {cli_bin}"
    return subprocess.run(
        [str(cli_bin), *argv],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )


def _assert_commands_are_valid(commands: list[str], cwd: Path) -> None:
    assert commands, "No completion commands found in prompt text"

    failures: list[str] = []
    for command in commands:
        result = _run_completion_command(command, cwd=cwd)
        if result.returncode != 0:
            failures.append(
                f"Command failed: {command}\n"
                f"exit={result.returncode}\n"
                f"stderr={result.stderr.strip()}\n"
                f"stdout={result.stdout.strip()}"
            )

    assert not failures, "\n\n".join(failures)


def _extract_statuses(commands: list[str]) -> set[str]:
    statuses: set[str] = set()
    for command in commands:
        argv = shlex.split(command)
        if len(argv) >= 2 and argv[0] in _COMPLETION_CMDS:
            statuses.add(argv[1])
    return statuses


class _RecordingLabelAdapter:
    def __init__(self) -> None:
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []

    def add_label(self, target: int, label: str) -> None:
        self.added.append((target, label))

    def remove_label(self, target: int, label: str) -> None:
        self.removed.append((target, label))


class _RecordingPRAdapter:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[object]:
        return []

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[object]:
        return []

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> object:
        return type("PR", (), {"url": "https://example.test/pr/1"})()

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        self.comments.append((issue_or_pr_number, body))
        return "https://example.test/comment/1"


class _NoopGitAdapter:
    def get_current_branch(self, worktree: Path) -> str:
        return "issue-1"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False

    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool:
        return False

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        force_with_lease: bool = True,
        set_upstream: bool = True,
        skip_hooks: bool = False,
        *,
        extra_env: dict[str, str] | None = None,
    ):
        return type(
            "PushResult", (), {"success": True, "message": "ok", "branch": "issue-1"}
        )()

    def get_branch_status(self, worktree: Path):
        return None

    def get_head_sha(self, worktree: Path):
        return "deadbeef"

    def rebase_on_branch(self, worktree: Path, target: str = "origin/main"):
        return type("RebaseResult", (), {"success": True, "message": "ok"})()

    def list_branch_names(self, worktree: Path) -> list[str]:
        return ["issue-1"]

    def create_branch_from_current(self, worktree: Path, branch: str) -> None:
        return None

    def push_preflight(self, worktree: Path, remote: str = "origin"):
        return type(
            "PreflightResult",
            (),
            {"would_succeed": True, "error": None, "fix_hint": None},
        )()


def test_setup_wizard_generated_prompts_have_valid_completion_commands(
    tmp_path: Path,
) -> None:
    work_prompt = tmp_path / "work-agent.md"
    tech_lead_prompt = tmp_path / "tech-lead-agent.md"

    create_starter_prompt("agent:backend", work_prompt)
    create_tech_lead_review_prompt(
        tech_lead_prompt, "needs-tech-lead-review", "tech-lead-reviewed"
    )

    combined = work_prompt.read_text() + "\n" + tech_lead_prompt.read_text()
    commands = _extract_completion_commands(combined)
    _assert_commands_are_valid(commands, cwd=tmp_path)


def test_control_api_prompt_templates_have_valid_completion_commands(
    tmp_path: Path,
    lm: LabelManager,
) -> None:
    prompts = [
        build_starter_prompt_text("backend"),
        build_code_review_prompt_text(lm.code_review, lm.code_reviewed),
        build_tech_lead_review_prompt_text("tech-lead-review", "tech-lead-reviewed"),
    ]
    commands = _extract_completion_commands("\n".join(prompts))
    _assert_commands_are_valid(commands, cwd=tmp_path)


def test_canonical_completion_instructions_have_valid_commands(tmp_path: Path) -> None:
    combined = get_coding_done_instructions() + "\n" + get_reviewer_done_instructions()
    commands = _extract_completion_commands(combined)
    _assert_commands_are_valid(commands, cwd=tmp_path)


def test_completion_record_schema_contract_for_all_statuses(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    completion_path = repo / ".issue-orchestrator" / "completion.json"
    common_env = {
        **os.environ,
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": ".issue-orchestrator/completion.json",
        "ISSUE_ORCHESTRATOR_SESSION_ID": "issue-1",
    }

    cases = [
        (
            [
                "completed",
                "--implementation",
                "Implemented feature",
                "--problems",
                "None",
            ],
            CompletionOutcome.COMPLETED.value,
            {"push_branch", "create_pr", "post_comment"},
        ),
        (
            [
                "blocked",
                "--reason",
                "Dependency unavailable",
                "--attempted",
                "Retried twice",
            ],
            CompletionOutcome.BLOCKED.value,
            {"push_branch", "add_blocked_label", "post_comment"},
        ),
        (
            ["needs_human", "--question", "Pick API strategy"],
            CompletionOutcome.NEEDS_HUMAN.value,
            {"push_branch", "add_needs_human_label", "post_comment"},
        ),
        (
            ["approved", "--summary", "Looks good", "--risk", "low"],
            CompletionOutcome.REVIEW_APPROVED.value,
            {
                "add_code_reviewed_label",
                "remove_needs_rework_label",
                "remove_code_review_label",
                "post_comment",
            },
        ),
        (
            ["changes_requested", "--issues", "Missing tests", "--risk", "medium"],
            CompletionOutcome.REVIEW_CHANGES_REQUESTED.value,
            {"add_needs_rework_label", "remove_code_review_label", "post_comment"},
        ),
    ]

    for argv, expected_outcome, expected_actions in cases:
        if completion_path.exists():
            completion_path.unlink()
        result = _run_completion_raw(argv, cwd=repo, env=common_env)
        assert result.returncode == 0, result.stderr
        assert completion_path.exists()
        payload = json.loads(completion_path.read_text())
        assert payload["outcome"] == expected_outcome
        actions = {a.lower() for a in payload["requested_actions"]}
        assert actions == expected_actions


def test_prompt_role_status_contracts(lm: LabelManager) -> None:
    work_prompt = build_starter_prompt_text("backend")
    review_prompt = build_code_review_prompt_text(lm.code_review, lm.code_reviewed)
    tech_lead_prompt = build_tech_lead_review_prompt_text(
        "tech-lead-review", "tech-lead-reviewed"
    )

    work_statuses = _extract_statuses(_extract_completion_commands(work_prompt))
    review_statuses = _extract_statuses(_extract_completion_commands(review_prompt))
    tech_lead_statuses = _extract_statuses(
        _extract_completion_commands(tech_lead_prompt)
    )

    assert {"blocked", "needs_human"} <= work_statuses
    assert review_statuses == {"approved", "changes_requested"}
    # Tech Lead sessions run on the coding-done contract: the orchestrator labels
    # manifest PRs on COMPLETED and publishes any committed worktree changes.
    # reviewer-done would skip push_branch/create_pr and mis-target review
    # labels at the tech_lead tracking issue.
    assert tech_lead_statuses == {"completed", "blocked"}
    assert "reviewer-done" not in tech_lead_prompt
    assert "gh pr comment" not in tech_lead_prompt
    assert "gh issue create" not in tech_lead_prompt
    # ADR-0031: tech_lead completion requires the decision artifact pair; the
    # prompt must name both files the orchestrator validates on completion.
    assert "tech-lead-decision.json" in tech_lead_prompt
    assert "tech-lead-report.md" in tech_lead_prompt


def test_completion_record_drives_expected_review_actions(
    tmp_path: Path,
    lm: LabelManager,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _init_git_repo(worktree)
    (worktree / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
    run_assets = make_session_run_assets(worktree, session_name="review-100")
    completion_path = run_assets.run_dir / "completion-review-100.json"

    env = {
        **os.environ,
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": str(
            completion_path.relative_to(worktree)
        ),
        "ISSUE_ORCHESTRATOR_SESSION_ID": "review-100",
        "ISSUE_ORCHESTRATOR_RUN_DIR": str(run_assets.run_dir),
    }
    write_result = _run_completion_raw(
        ["approved", "--summary", "LGTM", "--risk", "low"],
        cwd=worktree,
        env=env,
    )
    assert write_result.returncode == 0, write_result.stderr

    label_adapter = _RecordingLabelAdapter()
    pr_adapter = _RecordingPRAdapter()
    processor = CompletionProcessor(
        agent_callback_endpoint=ready_callback_endpoint(),
        label_adapter=label_adapter,
        pr_adapter=pr_adapter,
        git_adapter=_NoopGitAdapter(),
        session_output=FileSystemSessionOutput(),
        label_config=lm.to_label_config_dict(),
    )
    controller = SessionController(
        completion_processor=processor,
        events=type("Sink", (), {"publish": lambda self, event: None})(),
        session_output=FileSystemSessionOutput(),
        working_copy=_NoopGitAdapter(),
    )

    decision = controller.decide_outcome(
        observation=SessionObservationResult(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
        ),
        worktree_path=worktree,
        issue_number=1,
        issue_title="Test issue",
        session_name="review-100",
        completion_path=str(completion_path.relative_to(worktree)),
        session_run_assets=run_assets,
    )

    assert decision.status.name == "COMPLETED"
    assert (100, lm.code_reviewed) in label_adapter.added
    assert (100, lm.needs_rework) in label_adapter.removed
    assert any(
        target == 100 and label == lm.code_review
        for target, label in label_adapter.removed
    )
    assert any(target == 100 for target, _body in pr_adapter.comments)


def _make_test_session(issue: Issue, worktree: Path) -> Session:
    terminal_id = f"issue-{issue.number}"
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        terminal_id=terminal_id,
        branch_name=terminal_id,
        worktree_path=worktree,
        agent_config=AgentConfig(
            prompt_path=worktree / "prompt.md", timeout_minutes=30
        ),
        run_assets=make_session_run_assets(worktree, session_name=terminal_id),
    )


def _apply_label_actions_to_issue(issue: Issue, actions: list[object]) -> Issue:
    labels = set(issue.labels)
    for action in actions:
        if isinstance(action, AddLabelAction):
            labels.add(action.label)
        if isinstance(action, RemoveLabelAction):
            labels.discard(action.label)
    return Issue(number=issue.number, title=issue.title, labels=sorted(labels))


def test_publish_failure_multi_attempt_contract(
    tmp_path: Path, lm: LabelManager
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _init_git_repo(worktree)

    config = Config()
    config.repo = "owner/repo"

    issue = Issue(
        number=1, title="Synthetic publish-fail issue", labels=["agent:coder"]
    )
    repository_host = type(
        "RepoHost",
        (),
        {
            "get_prs_for_branch": lambda self, branch: [],
            "get_pr": lambda self, pr_number: None,
            "get_issue": lambda self, issue_number: None,
            "set_pr_draft": lambda self, pr_number, draft: None,
        },
    )()
    handler = CompletionHandler(
        config=config,
        events=type("Sink", (), {"publish": lambda self, event: None})(),
        repository_host=repository_host,
        get_issue_machine_fn=lambda _issue: None,
        get_session_machine_fn=lambda _terminal: None,
        get_review_machine_fn=lambda _pr: None,
        session_output=FileSystemSessionOutput(),
        tech_lead_authority=InMemoryTechLeadAuthorityStore(),
        open_issue_corpus=OpenIssueCorpusManager(
            repository_host,
            InMemoryOpenIssueCorpusStore(),
            is_enabled=lambda: config.tech_lead.dedup.enabled,
        ),
        provider_availability=make_provider_availability(config),
        tech_lead_run_activity=in_memory_run_activity(),
    )

    for _ in range(3):
        session = _make_test_session(issue, worktree)
        result = handler.process_completion(
            session,
            SessionStatus.COMPLETED,
            processing_errors=["publish_blocked: simulated push failure"],
        )
        # Each attempt either adds publish-failed or escalates to needs-human
        assert any(
            isinstance(action, AddLabelAction)
            and action.label in (lm.publish_failed, lm.needs_human)
            for action in result.actions
        )
        assert any(
            isinstance(action, RemoveLabelAction) and action.label == lm.in_progress
            for action in result.actions
        )
        issue = _apply_label_actions_to_issue(issue, result.actions)

    assert lm.publish_failed in issue.labels


def test_wrapper_and_git_guardrail_path_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    scripts_dir = (
        Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator" / "scripts"
    )
    agent_done_wrapper = scripts_dir / "agent-done"
    git_wrapper = scripts_dir / "git"

    wrapper_result = subprocess.run(
        [str(agent_done_wrapper), "--help"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )
    assert wrapper_result.returncode == 0
    assert "agent work" in wrapper_result.stdout.lower()

    blocked_push = subprocess.run(
        [str(git_wrapper), "push"],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )
    assert blocked_push.returncode != 0
    assert "BLOCKED: 'git push' is not allowed" in blocked_push.stderr

    passthrough_push = subprocess.run(
        [str(git_wrapper), "push"],
        cwd=repo,
        env=authorized_local_fixture_git_env(),
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )
    # No remote is configured, so push may still fail — but wrapper block message
    # must not appear when auth bypass is set.
    assert "BLOCKED: 'git push' is not allowed" not in passthrough_push.stderr


class TestEscalationIsReachableOnADirtyTree:
    """The rung 3 escalation must actually run against the tree it describes.

    The remediation ladder's last rung tells an agent that cannot classify a
    dirty file to preserve it and report `blocked` / `needs_human`. That advice
    is worthless if the command itself rejects a dirty tree -- the agent would
    be left with no legal move at all, which is the pressure that gets a
    stranger's uncommitted work deleted.

    The rest of this module cannot catch that: `_run_completion_command`
    appends `--dry-run`, which returns before the dirty check ever runs. These
    tests deliberately invoke the real path with a genuinely dirty worktree.
    """

    @staticmethod
    def _dirty_repo(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        # A file the agent did not create and cannot classify -- rung 3.
        (repo / "operator_notes.py").write_text("pre-existing operator work\n")
        return repo

    def _run(self, argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert "--dry-run" not in argv, "this suite must exercise the real path"
        return _run_completion_raw(argv, cwd=cwd)

    def test_blocked_is_accepted_while_the_tree_is_dirty(self, tmp_path):
        repo = self._dirty_repo(tmp_path)

        result = self._run(
            [
                "blocked",
                "--reason",
                "cannot classify dirty file operator_notes.py",
                "--attempted",
                "inspected the file and its history",
            ],
            cwd=repo,
        )

        assert result.returncode == 0, (
            "rung 3 escalation must be reachable on a dirty tree; got:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_needs_human_is_accepted_while_the_tree_is_dirty(self, tmp_path):
        repo = self._dirty_repo(tmp_path)

        result = self._run(
            ["needs_human", "--question", "who owns operator_notes.py?"],
            cwd=repo,
        )

        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    def test_the_unclassified_file_survives_the_escalation(self, tmp_path):
        """Escalating must not be a euphemism for cleaning up."""
        repo = self._dirty_repo(tmp_path)
        target = repo / "operator_notes.py"
        before = target.read_text()

        self._run(
            [
                "blocked",
                "--reason",
                "cannot classify dirty file operator_notes.py",
                "--attempted",
                "inspected the file and its history",
            ],
            cwd=repo,
        )

        assert target.exists(), "the escalation deleted the file it was preserving"
        assert target.read_text() == before

    def test_the_escalation_records_what_was_left_behind(self, tmp_path):
        """A human picking this up must see the files, not just the reason."""
        repo = self._dirty_repo(tmp_path)

        self._run(
            [
                "blocked",
                "--reason",
                "cannot classify dirty file operator_notes.py",
                "--attempted",
                "inspected the file and its history",
            ],
            cwd=repo,
        )

        record = json.loads(
            (repo / ".issue-orchestrator" / "completion.json").read_text()
        )
        body = record.get("comment_body") or ""
        assert "operator_notes.py" in body
        assert "preserved" in body

    def test_completed_still_requires_a_clean_tree(self, tmp_path):
        """Only the escalation statuses are exempt; publishing is not."""
        repo = self._dirty_repo(tmp_path)

        result = self._run(
            [
                "completed",
                "--implementation",
                "did the thing",
                "--problems",
                "None",
            ],
            cwd=repo,
        )

        assert result.returncode != 0


class TestEscalationRecordSurvivesTheOrchestrator:
    """The record the CLI writes must be accepted by the consumer that reads it.

    The CLI-side tests above stop at exit code and file contents. Round 4 showed
    that is not enough: `coding-done blocked` exited 0 and wrote a record, then
    `CompletionRecordValidator.validate_worktree_state` rejected it because
    `STATUS_TO_ACTIONS` gives escalations `PUSH_BRANCH` and the dirty policy
    fired. The agent believed it had escalated; the human was never told. This
    drives the real artifact across that boundary.
    """

    @staticmethod
    def _escalate(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)
        (repo / "operator_notes.py").write_text("pre-existing operator work\n")
        result = _run_completion_raw(
            [
                "blocked",
                "--reason",
                "cannot classify dirty file operator_notes.py",
                "--attempted",
                "inspected the file and its history",
            ],
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr
        return repo

    def test_the_written_record_is_accepted_with_the_tree_still_dirty(self, tmp_path):
        from issue_orchestrator.control.completion_record_validation import (
            CompletionRecordValidator,
        )
        from issue_orchestrator.infra.config import Config

        repo = self._escalate(tmp_path)

        class _DirtyGit:
            """Reports exactly the state the escalation left behind."""

            def get_current_branch(self, worktree):
                return "issue-123"

            def has_uncommitted_changes(self, worktree, **kwargs):
                return True

            def has_tracked_changes(self, worktree, **kwargs):
                return True

            def list_dirty_files(self, worktree, mode):
                return ["operator_notes.py"]

        config = Config()
        config.validation.publish.dirty_check = "tracked"
        validator = CompletionRecordValidator(config=config, git_adapter=_DirtyGit())

        record = validator.read_completion_record(repo)
        assert record is not None, "the CLI wrote no readable record"

        result = validator.validate_worktree_state(repo, record)

        assert result.ok, (
            "the orchestrator rejected the escalation the CLI accepted: "
            f"{getattr(result, 'reason', None)}"
        )

    def test_the_record_still_asks_to_publish_committed_history(self, tmp_path):
        """Escalating must not strand whatever the agent did manage to commit."""
        from issue_orchestrator.domain.models import RequestedAction

        repo = self._escalate(tmp_path)
        record = json.loads(
            (repo / ".issue-orchestrator" / "completion.json").read_text()
        )

        assert RequestedAction.PUSH_BRANCH.value in record["requested_actions"]
        assert record["outcome"] == "blocked"

    def test_a_completed_record_on_a_dirty_tree_is_still_rejected(self, tmp_path):
        """The exemption is scoped to escalations, not to dirty trees generally.

        The record is produced on a clean tree so the CLI actually writes one,
        then presented against a dirty worktree -- the temporal variance the
        post-validation re-check exists for. It must still be rejected.
        """
        from issue_orchestrator.control.completion_record_validation import (
            CompletionRecordValidator,
        )
        from issue_orchestrator.infra.config import Config

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        result = _run_completion_raw(
            ["completed", "--implementation", "did it", "--problems", "None"],
            cwd=repo,
        )
        assert result.returncode == 0, result.stderr

        class _DirtyGit:
            def get_current_branch(self, worktree):
                return "issue-123"

            def has_uncommitted_changes(self, worktree, **kwargs):
                return True

            def has_tracked_changes(self, worktree, **kwargs):
                return True

            def list_dirty_files(self, worktree, mode):
                return ["src.py"]

        config = Config()
        config.validation.publish.dirty_check = "tracked"
        validator = CompletionRecordValidator(config=config, git_adapter=_DirtyGit())
        record = validator.read_completion_record(repo)
        assert record is not None, "the CLI wrote no record on a clean tree"

        outcome = validator.validate_worktree_state(repo, record)
        assert not outcome.ok, "a completed record must still require a clean tree"


class TestEscalationSurvivesTheEffectiveHookPath:
    """The real pre-push hook must agree with the record validator.

    Round 5: production wires `CompletionProcessor` with a `PrePublishGate`
    whenever `enforce_hooks` is on (the default), and that gate runs the
    worktree's *effective* pre-push hook, which is nothing but
    `prepush-check --dirty-only`. The real push then runs the same hook again.
    So fixing only `validate_worktree_state` left the escalation rejected two
    boundaries later -- the third and fourth places the same policy is applied.

    These tests run the actual hook script, with the actual interpreter, over a
    real dirty repository.
    """

    @staticmethod
    def _repo_with_real_hook(tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo)

        # A tracked file, committed, then modified by someone else.
        tracked = repo / "operator_notes.py"
        tracked.write_text("operator work\n")
        subprocess.run(
            ["git", "add", "operator_notes.py"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "add notes"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        tracked.write_text("operator work\nedited after the agent started\n")

        config_dir = repo / ".issue-orchestrator" / "config" / "modes" / "default"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text(
            'validation:\n  publish:\n    cmd: "echo ok"\n    dirty_check: "tracked"\n'
        )

        # Install the real hook exactly as production does: substitute the
        # interpreter placeholder, make it executable.
        source = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "issue_orchestrator"
            / "hooks"
            / "pre-push"
        )
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook = hooks_dir / "pre-push"
        hook.write_text(
            source.read_text().replace(
                "@@ORCHESTRATOR_PYTHON@@", shlex.quote(sys.executable)
            )
        )
        hook.chmod(0o755)
        return repo

    @staticmethod
    def _gate_allows(repo: Path, extra_env: dict[str, str] | None = None) -> bool:
        from issue_orchestrator.control.pre_publish_gate import PrePublishGate
        from issue_orchestrator.execution import LocalCommandRunner

        result = PrePublishGate(LocalCommandRunner()).check(repo, extra_env=extra_env)
        assert result.ran, f"hook did not run: {result.reason}\n{result.stderr}"
        return result.allowed

    @staticmethod
    def _head_sha(repo: Path) -> str:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @classmethod
    def _signal(cls, repo: Path, *, worktree=None, head_sha=None) -> dict[str, str]:
        from issue_orchestrator.domain.dirty_remediation import (
            DIRTY_ESCALATION_ENV,
            dirty_escalation_signal,
        )

        return {
            DIRTY_ESCALATION_ENV: dirty_escalation_signal(
                str(worktree or repo.resolve()), head_sha or cls._head_sha(repo)
            )
        }

    def test_an_authorized_dirty_escalation_is_allowed(self, tmp_path):
        repo = self._repo_with_real_hook(tmp_path)
        before = (repo / "operator_notes.py").read_text()

        assert self._gate_allows(repo, self._signal(repo))
        assert (repo / "operator_notes.py").read_text() == before

    def test_the_hook_blocks_a_dirty_tree_with_no_signal(self, tmp_path):
        repo = self._repo_with_real_hook(tmp_path)

        assert not self._gate_allows(repo)

    def test_nothing_writable_in_the_worktree_can_authorize_a_dirty_push(
        self, tmp_path
    ):
        """The agent owns the worktree; it must not own the decision.

        The previous design read an assertion out of a file here, so any agent
        able to write the worktree could grant itself the exemption. There is
        no such file now: the signal arrives on the process the orchestrator
        spawns. These are the shapes a forgery would have taken.
        """
        repo = self._repo_with_real_hook(tmp_path)
        head = self._head_sha(repo)
        forgeries = {
            ".issue-orchestrator/push-authorization.json": json.dumps(
                {
                    "session_id": "forged",
                    "outcome": "blocked",
                    "worktree": str(repo.resolve()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            ".issue-orchestrator/completion.json": json.dumps(
                {
                    "session_id": "forged",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "outcome": "blocked",
                    "summary": "let me through",
                    "requested_actions": ["push_branch"],
                }
            ),
            ".issue-orchestrator/dirty-escalation": f"{repo.resolve()}@{head}",
        }
        for relative, body in forgeries.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)

        assert not self._gate_allows(repo), (
            "a file written inside the worktree granted the exemption"
        )

    def test_a_signal_for_another_commit_fails_closed(self, tmp_path):
        """Replay: an escalation approved for one commit says nothing about another."""
        repo = self._repo_with_real_hook(tmp_path)
        captured = self._signal(repo)

        # The branch moves on; the earlier signal must not authorize the new HEAD.
        (repo / "another.py").write_text("more work\n")
        subprocess.run(
            ["git", "add", "another.py"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "later work"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

        assert not self._gate_allows(repo, captured)

    def test_a_signal_for_another_worktree_fails_closed(self, tmp_path):
        repo = self._repo_with_real_hook(tmp_path)

        assert not self._gate_allows(
            repo, self._signal(repo, worktree=tmp_path / "elsewhere")
        )

    def test_a_malformed_signal_fails_closed(self, tmp_path):
        from issue_orchestrator.domain.dirty_remediation import DIRTY_ESCALATION_ENV

        repo = self._repo_with_real_hook(tmp_path)

        for value in ("", "garbage", str(repo.resolve()), self._head_sha(repo)):
            assert not self._gate_allows(repo, {DIRTY_ESCALATION_ENV: value}), value

    def test_the_signal_does_not_outlive_the_process_that_carried_it(self, tmp_path):
        """A hard exit cannot leave an exemption behind, because none is stored.

        The previous design wrote a file and removed it in a ``finally``, which
        ``os._exit`` skips entirely -- leaving a standing exemption after a
        crash. An environment variable dies with the process by construction,
        so the only thing to assert is that the flow persists nothing.
        """
        repo = self._repo_with_real_hook(tmp_path)

        assert self._gate_allows(repo, self._signal(repo))

        leftovers = [
            path
            for path in (repo / ".issue-orchestrator").rglob("*")
            if path.is_file() and "authoriz" in path.name.lower()
        ]
        assert not leftovers, f"authorization artifact persisted: {leftovers}"
        # And a later gate run, without the signal, is blocked again.
        assert not self._gate_allows(repo)


class TestEscalationReachesTheHumanThroughTheProductionGate:
    """End to end: real hook, real record, real gate, real processor wiring.

    The round-4 processor test omitted `pre_publish_gate`, so it proved the
    escalation survived record validation and nothing more. Production always
    supplies that gate when `enforce_hooks` is on, which is the default. This
    composes the processor the way production does and asserts the escalation
    reaches its human-routing label with the uncommitted file untouched.
    """

    def test_a_dirty_escalation_reaches_its_label_through_the_real_gate(self, tmp_path):
        from unittest.mock import Mock

        from issue_orchestrator.control.completion_processor import (
            CompletionProcessor,
        )
        from issue_orchestrator.control.pre_publish_gate import PrePublishGate
        from issue_orchestrator.domain.events import EventBus
        from issue_orchestrator.execution import LocalCommandRunner
        from issue_orchestrator.execution.session_output_adapter import (
            FileSystemSessionOutput,
        )
        from issue_orchestrator.infra.config import Config
        from tests.callback_endpoint_helpers import ready_callback_endpoint
        from tests.unit.session_run_helpers import make_session_run_assets

        repo = TestEscalationSurvivesTheEffectiveHookPath._repo_with_real_hook(tmp_path)
        preserved = repo / "operator_notes.py"
        before = preserved.read_text()

        assert (
            _run_completion_raw(
                [
                    "blocked",
                    "--reason",
                    "cannot classify dirty file operator_notes.py",
                    "--attempted",
                    "inspected the file and its history",
                ],
                cwd=repo,
            ).returncode
            == 0
        )

        from issue_orchestrator.control.completion_processor import GitAdapter
        from issue_orchestrator.ports.working_copy import (
            BranchPathsResult,
            BranchTextFilesResult,
            DiffResult,
            PushResult,
        )

        git_adapter = Mock(spec=GitAdapter)
        git_adapter.push = Mock(
            return_value=PushResult(
                success=True, branch="issue-123", remote="origin", message="Pushed"
            )
        )
        git_adapter.get_current_branch = Mock(return_value="issue-123")
        # The signal the orchestrator issues is bound to this exact commit.
        git_adapter.get_head_sha = Mock(
            return_value=TestEscalationSurvivesTheEffectiveHookPath._head_sha(repo)
        )
        git_adapter.default_branch = Mock(return_value="main")
        git_adapter.list_branch_names = Mock(return_value=["issue-123"])
        # The tree really is dirty, and stays that way.
        git_adapter.has_uncommitted_changes = Mock(return_value=True)
        git_adapter.has_tracked_changes = Mock(return_value=True)
        git_adapter.list_dirty_files = Mock(return_value=["operator_notes.py"])
        git_adapter.diff_against_base = Mock(
            return_value=DiffResult(success=True, diff_text="")
        )
        git_adapter.read_branch_text_files = Mock(
            return_value=BranchTextFilesResult(success=True)
        )
        git_adapter.branch_post_image_paths_against_base = Mock(
            return_value=BranchPathsResult(success=True, paths=())
        )

        label_adapter = Mock()
        pr_adapter = Mock()

        config = Config()
        config.validation.publish.dirty_check = "tracked"

        processor = CompletionProcessor(
            agent_callback_endpoint=ready_callback_endpoint(),
            label_adapter=label_adapter,
            pr_adapter=pr_adapter,
            git_adapter=git_adapter,
            event_bus=EventBus(),
            session_output=FileSystemSessionOutput(),
            label_config={"blocked": "blocked"},
            config=config,
            pre_publish_gate=PrePublishGate(LocalCommandRunner()),
        )

        result = processor.process(
            repo,
            run_assets=make_session_run_assets(repo),
            issue_number=123,
            issue_title="Test",
        )

        assert result.success, (
            f"escalation blocked at the publish gate: "
            f"{result.failure_kind}: {result.message}"
        )
        label_adapter.add_label.assert_any_call(123, "blocked")
        # Only committed history was published, and the preserved file survived.
        git_adapter.push.assert_called_once()
        assert preserved.read_text() == before
        # The authorization reached the hook through the environment of the
        # process the orchestrator spawned, so nothing was written into the
        # worktree that a later push could reuse.
        assert not [
            path
            for path in (repo / ".issue-orchestrator").rglob("*")
            if path.is_file() and "authoriz" in path.name.lower()
        ]
