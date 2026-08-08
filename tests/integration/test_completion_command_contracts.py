"""Synthetic integration tests for completion command contract integrity.

These tests validate that completion command examples emitted by prompt
generators are executable by the real CLI entrypoints (`coding-done`
and `reviewer-done`).  This prevents drift where prompt text suggests
invalid command forms.
"""

from __future__ import annotations

import json
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
from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult
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
from issue_orchestrator.resources import get_coding_done_instructions, get_reviewer_done_instructions
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


def _run_completion_command(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
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
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True, text=True)
    (path / "README.md").write_text("test\n")
    (path / ".gitignore").write_text(".agent-done-marker\n.issue-orchestrator/\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, text=True)


_REVIEWER_STATUSES = {"approved", "changes_requested"}


def _bin_for_status(status: str) -> str:
    """Return the correct CLI binary name for a given status."""
    return "reviewer-done" if status in _REVIEWER_STATUSES else "coding-done"


def _run_completion_raw(argv: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
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

    def create_pr(self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None) -> object:
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

    def push(self, worktree: Path, remote: str = "origin", force_with_lease: bool = True, set_upstream: bool = True, skip_hooks: bool = False):
        return type("PushResult", (), {"success": True, "message": "ok", "branch": "issue-1"})()

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
        return type("PreflightResult", (), {"would_succeed": True, "error": None, "fix_hint": None})()


def test_setup_wizard_generated_prompts_have_valid_completion_commands(tmp_path: Path) -> None:
    work_prompt = tmp_path / "work-agent.md"
    tech_lead_prompt = tmp_path / "tech-lead-agent.md"

    create_starter_prompt("agent:backend", work_prompt)
    create_tech_lead_review_prompt(tech_lead_prompt, "needs-tech-lead-review", "tech-lead-reviewed")

    combined = work_prompt.read_text() + "\n" + tech_lead_prompt.read_text()
    commands = _extract_completion_commands(combined)
    _assert_commands_are_valid(commands, cwd=tmp_path)


def test_control_api_prompt_templates_have_valid_completion_commands(
    tmp_path: Path, lm: LabelManager,
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
            ["completed", "--implementation", "Implemented feature", "--problems", "None"],
            CompletionOutcome.COMPLETED.value,
            {"push_branch", "create_pr", "post_comment"},
        ),
        (
            ["blocked", "--reason", "Dependency unavailable", "--attempted", "Retried twice"],
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
            {"add_code_reviewed_label", "remove_needs_rework_label", "remove_code_review_label", "post_comment"},
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
    tech_lead_prompt = build_tech_lead_review_prompt_text("tech-lead-review", "tech-lead-reviewed")

    work_statuses = _extract_statuses(_extract_completion_commands(work_prompt))
    review_statuses = _extract_statuses(_extract_completion_commands(review_prompt))
    tech_lead_statuses = _extract_statuses(_extract_completion_commands(tech_lead_prompt))

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
    tmp_path: Path, lm: LabelManager,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _init_git_repo(worktree)
    (worktree / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
    run_assets = make_session_run_assets(worktree, session_name="review-100")
    completion_path = run_assets.run_dir / "completion-review-100.json"

    env = {
        **os.environ,
        "ISSUE_ORCHESTRATOR_COMPLETION_PATH": str(completion_path.relative_to(worktree)),
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
    assert any(target == 100 and label == lm.code_review for target, label in label_adapter.removed)
    assert any(target == 100 for target, _body in pr_adapter.comments)


def _make_test_session(issue: Issue, worktree: Path) -> Session:
    terminal_id = f"issue-{issue.number}"
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue.number)), task=TaskKind.CODE),
        issue=issue,
        terminal_id=terminal_id,
        branch_name=terminal_id,
        worktree_path=worktree,
        agent_config=AgentConfig(prompt_path=worktree / "prompt.md", timeout_minutes=30),
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


def test_publish_failure_multi_attempt_contract(tmp_path: Path, lm: LabelManager) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _init_git_repo(worktree)

    config = Config()
    config.repo = "owner/repo"

    issue = Issue(number=1, title="Synthetic publish-fail issue", labels=["agent:coder"])
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
        active_session_run_id=lambda _n: None,
        provider_availability=make_provider_availability(config),
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
            isinstance(action, AddLabelAction) and action.label in (lm.publish_failed, lm.needs_human)
            for action in result.actions
        )
        assert any(isinstance(action, RemoveLabelAction) and action.label == lm.in_progress for action in result.actions)
        issue = _apply_label_actions_to_issue(issue, result.actions)

    assert lm.publish_failed in issue.labels


def test_wrapper_and_git_guardrail_path_resolution(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    scripts_dir = Path(__file__).resolve().parents[2] / "src" / "issue_orchestrator" / "scripts"
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
        env={**os.environ, "ORCHESTRATOR_GH_AUTH": "agent-done-authorized"},
        text=True,
        capture_output=True,
        timeout=_CONTRACT_COMMAND_TIMEOUT_SECONDS,
    )
    # No remote is configured, so push may still fail — but wrapper block message
    # must not appear when auth bypass is set.
    assert "BLOCKED: 'git push' is not allowed" not in passthrough_push.stderr
