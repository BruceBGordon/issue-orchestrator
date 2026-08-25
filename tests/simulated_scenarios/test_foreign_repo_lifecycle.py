"""Foreign repo lifecycle integration tests.

Verifies the orchestrator works correctly against a *foreign* repository —
one that has no orchestrator source tree, no ``.venv``, no ``Makefile``.

Tests use the **real** ``GitWorktreeManager`` adapter so worktrees are created
via ``git worktree add``, and verify:
- Orchestrator-internal setup (hooks, Claude settings, coding-done, worktree-id)
- PATH resolution uses the orchestrator's own package, not the target repo
- sync_cli_tools copies from the orchestrator package, not repo_root
- setup_worktree defaults to empty (no orchestrator-specific commands)
- The full lifecycle (coder -> completion -> review -> PR) succeeds
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import sync_cli_tools
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalShell,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_terminal_session_terminator,
)
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.env import ENV_PREFIX
from tests.git_push_authorization import authorized_local_fixture_git_env

from .conftest import (
    ScriptSessionRunner,
    StubWorkingCopy,
    build_config,
    build_orchestrator,
    run_until,
)
from .scenario_dsl import script


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


def _init_foreign_repo(tmp_path: Path) -> Path:
    """Create a minimal foreign git repo with an ``origin`` remote.

    Layout::

        tmp_path/
          origin.git/    <- bare repo (acts as remote)
          foreign-repo/  <- working clone with just a README
    """
    bare = tmp_path / "origin.git"
    clone = tmp_path / "foreign-repo"

    # Create bare repo as origin
    subprocess.run(
        ["git", "init", "--bare", str(bare)],
        check=True,
        capture_output=True,
    )

    # Clone it to get a working copy
    subprocess.run(
        ["git", "clone", str(bare), str(clone)],
        check=True,
        capture_output=True,
    )

    # Ensure the default branch is named "main" (-B tolerates init.defaultBranch=main)
    subprocess.run(
        ["git", "checkout", "-B", "main"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )

    # Create initial commit so main branch exists
    readme = clone / "README.md"
    readme.write_text("# Foreign repo\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "Initial commit"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    push_env = authorized_local_fixture_git_env()
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(clone),
        check=True,
        capture_output=True,
        env=push_env,
    )

    return clone


def _run_shell_with_timeout_retry(
    full_cmd: str,
    *,
    executable: str = "/bin/bash",
    timeout_seconds: int,
    retry_timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command with one timeout retry under loaded CI/pre-push runs."""
    try:
        return subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            executable=executable,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return subprocess.run(
            full_cmd,
            shell=True,
            capture_output=True,
            text=True,
            executable=executable,
            timeout=retry_timeout_seconds,
        )


@pytest.fixture()
def foreign_repo(tmp_path: Path) -> Path:
    return _init_foreign_repo(tmp_path)


@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    """Returned by the ``make_worktree`` factory fixture."""

    path: Path
    repo: Path


@dataclass(frozen=True, slots=True)
class ForeignSessionContract:
    """Typed env inputs for simulated foreign-repo agent sessions."""

    issue_number: int
    session_name: str
    completion_rel: str
    run_dir: Path
    worktree_path: Path


@pytest.fixture()
def make_worktree(foreign_repo: Path, tmp_path: Path):
    """Factory fixture that creates real foreign-repo worktrees and cleans up."""
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir(exist_ok=True)
    mgr = GitWorktreeManager()
    created: list[WorktreeHandle] = []

    def _make(issue_number: int, issue_title: str) -> WorktreeHandle:
        info = mgr.create(
            repo_root=foreign_repo,
            issue_number=issue_number,
            issue_title=issue_title,
            worktree_base=worktree_base,
            enforce_hooks=False,
        )
        handle = WorktreeHandle(path=info.path, repo=foreign_repo)
        created.append(handle)
        return handle

    yield _make

    # Cleanup: force-remove all created worktrees then prune
    for handle in created:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(handle.path)],
            cwd=str(handle.repo),
            capture_output=True,
        )
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=str(foreign_repo),
        capture_output=True,
    )


def _coder_session_contract(
    issue_number: int,
    worktree_path: Path,
    completion_rel: str = ".issue-orchestrator/completion.json",
) -> ForeignSessionContract:
    session_name = f"coder-{issue_number}"
    return ForeignSessionContract(
        issue_number=issue_number,
        session_name=session_name,
        completion_rel=completion_rel,
        run_dir=worktree_path / ".issue-orchestrator" / "sessions" / session_name,
        worktree_path=worktree_path,
    )


def _build_session_exports(contract: ForeignSessionContract) -> str:
    """Build the env-export string that session_launcher produces."""
    orch_bin = Path(sys.executable).parent
    return (
        f"export {ENV_PREFIX}COMPLETION_PATH='{contract.completion_rel}'"
        f" {ENV_PREFIX}SESSION_ID='{contract.session_name}'"
        f" {ENV_PREFIX}AGENT_LABEL='agent:coder'"
        f" {ENV_PREFIX}ISSUE_NUMBER='{contract.issue_number}'"
        f" {ENV_PREFIX}VALIDATION_OUTPUT_DIR='{contract.run_dir}'"
        f" {ENV_PREFIX}RUN_DIR='{contract.run_dir}'"
        f" {ENV_PREFIX}WORKTREE='{contract.worktree_path}'"
        f' PATH="{orch_bin}:$PATH"'
    )


# ---------------------------------------------------------------------------
# Full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(
    reason="Persistent-session cutover deleted the spawn-per-phase "
    "capture path these scenarios were tightly coupled to. The "
    "persistent runner is exhaustively unit-tested in "
    "test_persistent_session_exchange.py + test_persistent_round_runner.py; "
    "migrating this harness to drive the persistent runner natively "
    "is tracked as a follow-up."
)
def test_foreign_repo_full_lifecycle(foreign_repo: Path, tmp_path: Path) -> None:
    """Full orchestrator lifecycle against a repo with no orchestrator files."""
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()

    config = build_config(
        foreign_repo,
        coder_command=script("coder_dual_mode.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        review_exchange_mode="via-local-loop",
        validation_cmd=script("validate_pass.sh"),
    )
    config.worktree_base = worktree_base

    issue = Issue(
        number=1,
        title="Foreign repo test issue",
        labels=["simulated-scenario", "agent:coder"],
    )

    orch, repo_host, events, _ = build_orchestrator(
        foreign_repo,
        [issue],
        config,
        worktree_manager=GitWorktreeManager(),  # type: ignore[arg-type]  # duck-typed
        working_copy=StubWorkingCopy(),
        runner=ScriptSessionRunner(),
    )

    try:
        run_until(orch, lambda: not orch.state.active_sessions, max_ticks=15)

        emitted = {e.name for e in events.events}
        assert EventName.SESSION_STARTED in emitted
        assert EventName.SESSION_COMPLETED in emitted
        assert EventName.REVIEW_EXCHANGE_STARTED in emitted
        assert EventName.REVIEW_EXCHANGE_COMPLETED in emitted
        assert EventName.ISSUE_PR_CREATED in emitted

        assert repo_host.get_pr(100) is not None, "Expected PR #100 to be created"

        # The worktree was a real git worktree
        history = orch.state.session_history
        assert history, "Expected session history to be non-empty"
        worktree_path = history[0].worktree_path
        assert worktree_path is not None
        git_entry = Path(worktree_path) / ".git"
        assert git_entry.exists(), ".git should exist in worktree"
        assert git_entry.is_file(), ".git should be a file (worktree), not a directory"
        git_content = git_entry.read_text().strip()
        assert git_content.startswith("gitdir:"), (
            f".git file should start with 'gitdir:', got: {git_content!r}"
        )

        # Orchestrator-internal setup was applied
        wt = Path(worktree_path)

        assert (wt / ".claude" / "settings.json").exists(), (
            "Claude settings should be installed in worktree"
        )
        assert (wt / ".issue-orchestrator" / "worktree-id").exists(), (
            "Worktree identity marker should be installed"
        )

        # CLI tools synced from orchestrator package (not repo_root)
        cli_tools_dir = wt / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"
        assert cli_tools_dir.exists(), (
            "CLI tools should be synced from orchestrator package to foreign repo worktree"
        )
        assert (cli_tools_dir / "agent_done.py").exists(), (
            "agent_done.py should be synced to worktree"
        )

        # Foreign repo should NOT have the full orchestrator source tree
        assert not (wt / "src" / "issue_orchestrator" / "control").exists(), (
            "Foreign repo worktree should not contain orchestrator control layer"
        )
        assert not (wt / "src" / "issue_orchestrator" / "domain").exists(), (
            "Foreign repo worktree should not contain orchestrator domain layer"
        )
    finally:
        close = getattr(orch, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Unit-level / small integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_foreign_repo_sync_cli_tools_from_package(make_worktree, foreign_repo: Path) -> None:
    """sync_cli_tools copies from the orchestrator package, not repo_root."""
    handle = make_worktree(99, "sync-test")
    wt = handle.path

    # The foreign repo has no src/issue_orchestrator/ at all
    assert not (foreign_repo / "src" / "issue_orchestrator").exists()

    # But sync_cli_tools should still work (finds source from package)
    sync_cli_tools(wt)

    # CLI tools were synced
    cli_tools = wt / "src" / "issue_orchestrator" / "entrypoints" / "cli_tools"
    assert cli_tools.exists(), "CLI tools dir should be created"
    assert (cli_tools / "agent_done.py").exists(), "agent_done.py should be synced"


@pytest.mark.integration
def test_foreign_repo_agent_done_on_path() -> None:
    """coding-done and reviewer-done are findable from the orchestrator's own venv."""
    orch_bin = Path(sys.executable).parent
    coding_done = orch_bin / "coding-done"
    assert coding_done.exists(), (
        f"coding-done should be in orchestrator venv bin at {coding_done}"
    )
    reviewer_done = orch_bin / "reviewer-done"
    assert reviewer_done.exists(), (
        f"reviewer-done should be in orchestrator venv bin at {reviewer_done}"
    )


def test_foreign_repo_default_setup_commands_empty() -> None:
    """Config defaults to no setup commands (foreign-repo safe)."""
    config = Config()
    assert config.setup_worktree == [], (
        "Default setup_worktree should be empty list, not orchestrator-specific commands"
    )


@pytest.mark.integration
def test_foreign_repo_scripts_dir_from_package() -> None:
    """terminal_subprocess uses package-relative scripts dir, not repo_root.

    Instead of mirroring the implementation's path arithmetic, we verify the
    scripts directory referenced in the command actually exists and contains
    the expected wrapper scripts.
    """
    plugin = SubprocessPlugin(build_terminal_session_terminator())
    cmd = plugin._build_process_command("echo test", Path("/tmp/fake-worktree"))  # noqa: SLF001

    # Extract the PATH value from the generated command
    # Format: ... export PATH="<venv_bin>:<scripts_dir>:<system_path>" ...
    import re

    match = re.search(r'export PATH="([^"]+)"', cmd)
    assert match, f"Could not find PATH export in command: {cmd}"
    path_entries = match.group(1).split(":")

    # The second entry (after worktree .venv/bin) should be the scripts dir
    # and it should actually exist with the expected wrappers
    scripts_entry = path_entries[1]
    scripts_dir = Path(scripts_entry)
    assert scripts_dir.exists(), (
        f"Scripts dir from command PATH does not exist: {scripts_dir}"
    )
    assert (scripts_dir / "coding-done").exists(), (
        f"Expected coding-done wrapper in scripts dir: {scripts_dir}"
    )
    assert (scripts_dir / "reviewer-done").exists(), (
        f"Expected reviewer-done wrapper in scripts dir: {scripts_dir}"
    )
    assert (scripts_dir / "exchange-respond").exists(), (
        f"Expected exchange-respond wrapper in scripts dir: {scripts_dir}"
    )


@pytest.mark.integration
@pytest.mark.skip(
    reason="Persistent-session cutover deleted the spawn-per-phase "
    "capture path these scenarios were tightly coupled to. The "
    "persistent runner is exhaustively unit-tested in "
    "test_persistent_session_exchange.py + test_persistent_round_runner.py; "
    "migrating this harness to drive the persistent runner natively "
    "is tracked as a follow-up."
)
def test_foreign_repo_with_setup_commands(foreign_repo: Path, tmp_path: Path) -> None:
    """Setup commands run in worktree during session launch."""
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()

    marker = "foreign-setup-ran.marker"

    config = build_config(
        foreign_repo,
        coder_command=script("coder_dual_mode.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        review_exchange_mode="via-local-loop",
        validation_cmd=script("validate_pass.sh"),
    )
    config.worktree_base = worktree_base
    config.setup_worktree = [f"touch {marker}"]

    issue = Issue(
        number=2,
        title="Setup commands test",
        labels=["simulated-scenario", "agent:coder"],
    )

    orch, _, _, _ = build_orchestrator(
        foreign_repo,
        [issue],
        config,
        worktree_manager=GitWorktreeManager(),  # type: ignore[arg-type]
        working_copy=StubWorkingCopy(),
        runner=ScriptSessionRunner(),
    )

    try:
        run_until(orch, lambda: not orch.state.active_sessions, max_ticks=15)

        history = orch.state.session_history
        assert history, "Expected session history"
        worktree_path = history[0].worktree_path
        assert worktree_path is not None
        wt = Path(worktree_path)
        assert (wt / marker).exists(), (
            f"Setup command should have created {marker} in worktree"
        )
    finally:
        close = getattr(orch, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Real PATH chain tests — prove coding-done/reviewer-done are findable in a
# foreign repo worktree through the actual command construction used in production.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_foreign_repo_real_path_chain_finds_coding_done(make_worktree) -> None:
    """The full PATH chain (session_launcher + terminal_subprocess) finds coding-done."""
    handle = make_worktree(77, "path-chain-test")
    wt = handle.path

    # Sanity: foreign repo worktree has NO full orchestrator source tree
    assert not (wt / "src" / "issue_orchestrator" / "control").exists()
    assert not (wt / "src" / "issue_orchestrator" / "domain").exists()
    assert not (wt / ".venv").exists()

    plugin = SubprocessPlugin(build_terminal_session_terminator())
    exports = _build_session_exports(_coder_session_contract(77, wt))
    full_cmd = plugin._build_process_command(  # noqa: SLF001
        f"{exports} && which coding-done", wt
    )

    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True, executable="/bin/bash",
    )

    assert result.returncode == 0, (
        f"coding-done not found via real PATH chain in foreign repo worktree.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\ncommand: {full_cmd}"
    )
    assert "coding-done" in result.stdout, (
        f"Expected 'coding-done' in which output, got: {result.stdout!r}"
    )


@pytest.mark.integration
def test_foreign_repo_real_path_chain_coding_done_executes(make_worktree) -> None:
    """coding-done actually executes (not just findable) through the real PATH chain."""
    handle = make_worktree(78, "coding-done-exec-test")
    wt = handle.path

    plugin = SubprocessPlugin(build_terminal_session_terminator())
    exports = _build_session_exports(_coder_session_contract(78, wt))
    full_cmd = plugin._build_process_command(  # noqa: SLF001
        f"{exports} && coding-done --help", wt
    )

    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True, executable="/bin/bash",
    )

    assert result.returncode == 0, (
        f"coding-done --help failed in foreign repo worktree.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\ncommand: {full_cmd}"
    )
    assert "completed" in result.stdout.lower() or "usage" in result.stdout.lower(), (
        f"Expected coding-done help output, got: {result.stdout!r}"
    )


@pytest.mark.integration
def test_foreign_repo_real_path_chain_validation_runs(make_worktree) -> None:
    """A validation command executes successfully through the real PATH chain."""
    handle = make_worktree(79, "validation-test")
    wt = handle.path

    val_script = wt / "validate.sh"
    val_script.write_text("#!/bin/bash\necho VALIDATION_OK\nexit 0\n")
    val_script.chmod(0o755)

    plugin = SubprocessPlugin(build_terminal_session_terminator())
    exports = _build_session_exports(_coder_session_contract(79, wt))
    full_cmd = plugin._build_process_command(  # noqa: SLF001
        f"{exports} && ./validate.sh", wt
    )

    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True, executable="/bin/bash",
    )

    assert result.returncode == 0, (
        f"Validation command failed in foreign repo worktree.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "VALIDATION_OK" in result.stdout


# ---------------------------------------------------------------------------
# Real agent invocation — prove coding-done writes completion in a foreign repo
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_foreign_repo_coding_done_writes_completion(make_worktree) -> None:
    """coding-done completed writes a completion record in a foreign repo worktree."""
    handle = make_worktree(80, "coding-done-completion-test")
    wt = handle.path
    completion_rel = ".issue-orchestrator/completion.json"

    # coding-done enforces clean working tree — commit setup files first
    subprocess.run(
        ["git", "add", "."], cwd=wt, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "worktree setup", "--allow-empty"], cwd=wt, capture_output=True, check=True,
    )

    exports = _build_session_exports(_coder_session_contract(80, wt, completion_rel))
    agent_cmd = (
        "coding-done completed"
        " --implementation 'Foreign repo test implementation'"
        " --problems 'None'"
    )

    plugin = SubprocessPlugin(build_terminal_session_terminator())
    full_cmd = plugin._build_process_command(  # noqa: SLF001
        f"{exports} && {agent_cmd}", wt
    )

    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True,
        executable="/bin/bash", timeout=30,
    )

    assert result.returncode == 0, (
        f"coding-done completed failed in foreign repo worktree.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}\ncommand: {full_cmd}"
    )

    completion_file = wt / completion_rel
    assert completion_file.exists(), (
        f"coding-done should have written {completion_rel} in worktree.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    record = json.loads(completion_file.read_text())
    assert record["outcome"] == "completed", (
        f"Expected outcome 'completed', got: {record}"
    )


@pytest.mark.integration
@pytest.mark.xdist_group("pty")
def test_foreign_repo_real_pty_agent_invocation(
    make_worktree, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real PTY session (pexpect) runs coding-done in a foreign repo worktree."""
    handle = make_worktree(81, "pty-agent-test")
    wt = handle.path
    completion_rel = ".issue-orchestrator/completion.json"

    # Write a test agent script that calls coding-done
    agent_script = wt / "test-agent.sh"
    agent_script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        "coding-done completed"
        " --implementation 'PTY foreign repo test'"
        " --problems 'None'\n"
    )
    agent_script.chmod(0o755)

    # Commit setup files so coding-done's dirty-file check passes
    # (orchestrator runtime files in .issue-orchestrator/ are auto-excluded)
    subprocess.run(
        ["git", "add", "."], cwd=wt, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "worktree setup", "--allow-empty"], cwd=wt, capture_output=True, check=True,
    )

    session_contract = _coder_session_contract(81, wt, completion_rel)
    exports = _build_session_exports(session_contract)
    command = f"{exports} && ./test-agent.sh"

    # Use monkeypatch for clean env manipulation (auto-restores on teardown)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(handle.repo))

    plugin = SubprocessPlugin(build_terminal_session_terminator())
    session_name = session_contract.session_name

    created = plugin.create_session(
        session_id=81,
        command=command,
        interaction_intent=TerminalInteractionIntent.NONE,
        shell=TerminalShell.BASH,
        working_dir=str(wt),
        title="PTY agent test",
        session_name=session_name,
    )
    assert created is True, "create_session should return True"

    # Poll until the session finishes (coding-done is fast, but system load
    # under parallel test execution can delay process startup significantly)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if not plugin.session_exists(81, session_name):
            break
        time.sleep(0.2)
    else:
        plugin.kill_session(81, session_name)
        pytest.fail("PTY session did not complete within 60 seconds")

    completion_file = wt / completion_rel
    write_deadline = time.monotonic() + 5
    while time.monotonic() < write_deadline and not completion_file.exists():
        time.sleep(0.1)
    # Debug: show session output
    output = plugin.get_session_output(81, 100, session_name)
    assert completion_file.exists(), (
        f"coding-done should have written {completion_rel} via PTY session.\n"
        f"Worktree contents: {list(wt.iterdir())}\n"
        f"Session output: {output}"
    )

    record = json.loads(completion_file.read_text())
    assert record["outcome"] == "completed", (
        f"Expected outcome 'completed', got: {record}"
    )


# ---------------------------------------------------------------------------
# Real AI agent tests — prove Claude Code / Codex can run coding-done in a
# foreign repo worktree through the full production PATH chain.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.provider_claude
@pytest.mark.xdist_group("pty")
@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="Claude Code CLI not installed",
)
def test_foreign_repo_claude_code_agent_done(make_worktree) -> None:
    """Claude Code invokes coding-done in a foreign repo worktree."""
    handle = make_worktree(90, "claude-foreign-test")
    wt = handle.path
    completion_rel = ".issue-orchestrator/completion.json"

    # coding-done enforces clean working tree — commit setup files first
    subprocess.run(["git", "add", "."], cwd=wt, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "setup", "--allow-empty"], cwd=wt, capture_output=True, check=True,
    )

    exports = _build_session_exports(_coder_session_contract(90, wt, completion_rel))

    prompt = (
        "This is an authorized integration test in a disposable local git "
        "worktree. The command below only records test completion in "
        ".issue-orchestrator/completion.json. Execute it now with Bash: "
        'coding-done completed --implementation "claude foreign repo test" '
        '--problems "none". '
        "After it succeeds, reply done."
    )

    escaped_prompt = prompt.replace('"', '\\"')
    plugin = SubprocessPlugin(build_terminal_session_terminator())
    inner_cmd = (
        f'{exports} && claude -p --model haiku --permission-mode bypassPermissions '
        f'"{escaped_prompt}"'
    )
    full_cmd = plugin._build_process_command(inner_cmd, wt)  # noqa: SLF001

    result = _run_shell_with_timeout_retry(
        full_cmd,
        timeout_seconds=180,
        retry_timeout_seconds=420,
    )

    print(f"Claude stdout: {result.stdout[:1000]}")
    print(f"Claude stderr: {result.stderr[:1000]}")
    print(f"Return code: {result.returncode}")

    completion_file = wt / completion_rel
    assert completion_file.exists(), (
        f"Claude Code did not produce completion.json in foreign repo worktree.\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}\n"
        f"returncode: {result.returncode}"
    )

    record = json.loads(completion_file.read_text())
    assert record["outcome"] == "completed", (
        f"Expected outcome 'completed', got: {record}"
    )


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.provider_codex
@pytest.mark.xdist_group("codex-exec")
@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="Codex CLI not installed",
)
def test_foreign_repo_codex_agent_done(make_worktree) -> None:
    """Codex invokes coding-done in a foreign repo worktree."""
    handle = make_worktree(91, "codex-foreign-test")
    wt = handle.path
    completion_rel = ".issue-orchestrator/completion.json"

    # coding-done enforces clean working tree — commit setup files first
    subprocess.run(["git", "add", "."], cwd=wt, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "setup", "--allow-empty"], cwd=wt, capture_output=True, check=True,
    )

    exports = _build_session_exports(_coder_session_contract(91, wt, completion_rel))

    prompt = (
        "You are in a test. Run this exact bash command and nothing else: "
        'coding-done completed --implementation "codex foreign repo test" '
        '--problems "none". '
        "Do not explain anything, just run the command above using the shell tool."
    )

    escaped_prompt = prompt.replace('"', '\\"')
    plugin = SubprocessPlugin(build_terminal_session_terminator())
    inner_cmd = (
        f'{exports} && codex exec '
        f'--dangerously-bypass-approvals-and-sandbox '
        f'"{escaped_prompt}"'
    )
    full_cmd = plugin._build_process_command(inner_cmd, wt)  # noqa: SLF001

    result = _run_shell_with_timeout_retry(
        full_cmd,
        timeout_seconds=180,
        retry_timeout_seconds=300,
    )

    print(f"Codex stdout: {result.stdout[:1000]}")
    print(f"Codex stderr: {result.stderr[:1000]}")
    print(f"Return code: {result.returncode}")

    completion_file = wt / completion_rel
    assert completion_file.exists(), (
        f"Codex did not produce completion.json in foreign repo worktree.\n"
        f"stdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}\n"
        f"returncode: {result.returncode}"
    )

    record = json.loads(completion_file.read_text())
    assert record["outcome"] == "completed", (
        f"Expected outcome 'completed', got: {record}"
    )
