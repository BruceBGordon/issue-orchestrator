from pathlib import Path

import pytest

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.ports.command_runner import OutputNewlines
from issue_orchestrator.ports.git import GitError


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.newlines = []
        self.next_result = (0, "OK\n", "")
        self.timed_out = False

    def run(
        self,
        command,
        *,
        cwd=None,
        env=None,
        timeout_seconds=None,
        shell=False,
        newlines=OutputNewlines.TRANSLATED,
    ):
        self.calls.append((command, cwd, env, timeout_seconds, shell))
        self.newlines.append(newlines)
        return type(
            "Result",
            (),
            {
                "returncode": self.next_result[0],
                "stdout": self.next_result[1],
                "stderr": self.next_result[2],
                "timed_out": self.timed_out,
            },
        )()


def test_git_cli_builds_git_c_command():
    runner = FakeRunner()
    git = GitCLI(runner=runner)
    git.status_porcelain(Path("/tmp/repo"))
    command, cwd, env, timeout_seconds, shell = runner.calls[0]
    assert command[:3] == ["git", "-C", "/tmp/repo"]
    assert command[3:] == ["status", "--porcelain"]
    assert cwd is None
    assert shell is False
    assert timeout_seconds == git.default_timeout_s
    assert "GIT_DIR" not in env
    assert runner.newlines == [OutputNewlines.TRANSLATED]


def test_git_cli_forwards_requested_newline_policy():
    runner = FakeRunner()
    git = GitCLI(runner=runner)

    git.run(Path("/tmp/repo"), ["show", "HEAD:a.txt"], newlines=OutputNewlines.PRESERVED)

    assert runner.newlines == [OutputNewlines.PRESERVED]


def test_git_cli_raises_typed_error_on_failure():
    runner = FakeRunner()
    runner.next_result = (1, "", "boom")
    git = GitCLI(runner=runner)
    with pytest.raises(GitError) as exc:
        git.fetch(Path("/tmp/repo"))
    assert "git command failed" in str(exc.value)


def test_git_cli_reports_timeout():
    runner = FakeRunner()
    runner.timed_out = True
    git = GitCLI(runner=runner)
    with pytest.raises(GitError) as exc:
        git.current_branch(Path("/tmp/repo"))
    assert "timed out" in str(exc.value)
