"""Newline fidelity from real Git output through to the test-skip guard.

The guard reads two Git outputs -- unified diff text and branch-tip blob
content -- and applies Git's own LF-delimited physical-line rule to both. Tests
that hand-build those two strings prove the parser, not the transport: they
cannot see that a universal-newline capture rewrote a bare carriage return to a
line feed before the parser ever ran, splitting one patch record in two and
stripping the ``+`` from the half that carried the banned call. These tests
drive the production wiring instead -- ``GitWorkingCopy`` over ``GitCLI`` and
``LocalCommandRunner`` -- against a real repository.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.control.test_skip_guard import (
    added_test_paths,
    scan_added_test_skip_guards,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.ports.command_runner import OutputNewlines


GUARD_PATH = "src/test/java/GuardTest.java"

# Committed byte-for-byte, mixing every terminator shape the guard has to keep
# straight:
#   line 2  inert -- the banned text sits inside a string literal
#   line 3  VIOLATION -- a bare CR ends the Java line comment, so the call that
#           follows it executes, yet Git keeps the whole thing in one record
#   line 4  inert under CRLF
#   line 5  VIOLATION under CRLF, one line after the inert fixture
GUARD_SOURCE = (
    b"class GuardTest {\n"
    b'    String fixture = "assumeTrue(false);";\n'
    b"    // note\rassumeTrue(false);\n"
    b'    String crlfFixture = "@Disabled";\r\n'
    b"    @Disabled\r\n"
    b"}\n"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _echo_bytes(payload: bytes) -> list[str]:
    """Return a command that writes *payload* to stdout with no translation."""

    return [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.buffer.write({payload!r})",
    ]


@pytest.fixture
def repo_with_guard_source(tmp_path: Path) -> tuple[Path, str]:
    """Commit GUARD_SOURCE on a branch; return the repo and its base ref."""

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    # Git itself must not rewrite terminators on the way in or out; this test is
    # about the capture, not about checkout filters.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "feature")
    source = repo / GUARD_PATH
    source.parent.mkdir(parents=True)
    source.write_bytes(GUARD_SOURCE)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add guard test")
    return repo, base


def test_real_git_reads_keep_the_terminators_git_emitted(repo_with_guard_source):
    repo, base = repo_with_guard_source
    working_copy = GitWorkingCopy()

    diff_result = working_copy.diff_against_base(repo, base)
    branch_files = working_copy.read_branch_text_files(repo, (GUARD_PATH,))

    assert diff_result.success
    assert branch_files.success
    # One patch record, carriage return intact -- not two records with the
    # banned half demoted to context.
    assert "+    // note\rassumeTrue(false);\n" in diff_result.diff_text
    assert branch_files.files[0].content == GUARD_SOURCE.decode()


def test_guard_sees_skip_hidden_behind_bare_carriage_return_through_real_git(
    repo_with_guard_source,
):
    repo, base = repo_with_guard_source
    working_copy = GitWorkingCopy()

    diff_result = working_copy.diff_against_base(repo, base)
    test_paths = added_test_paths(diff_result.diff_text)
    branch_files = working_copy.read_branch_text_files(repo, test_paths)

    result = scan_added_test_skip_guards(diff_result.diff_text, branch_files.files)

    assert test_paths == (GUARD_PATH,)
    # Lines 2 and 4 are inert fixture text; the CRLF violation lands on 5, so
    # the two sides agree on line numbering under every terminator here.
    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(3, "JUnit assumeTrue"), (5, "JUnit @Disabled")]


def test_local_command_runner_translates_newlines_by_default():
    result = LocalCommandRunner().run(_echo_bytes(b"a\rb\r\nc\n"))

    assert result.stdout == "a\nb\nc\n"


def test_local_command_runner_preserves_newlines_on_request():
    result = LocalCommandRunner().run(
        _echo_bytes(b"a\rb\r\nc\n"), newlines=OutputNewlines.PRESERVED
    )

    assert result.stdout == "a\rb\r\nc\n"
