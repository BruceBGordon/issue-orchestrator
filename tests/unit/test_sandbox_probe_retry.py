"""Deterministic coverage for the live sandbox probe retry policy.

The probes themselves need a real agent CLI, but their retry/timeout policy
is the part that can silently turn a half-executed security probe into a pass.
That policy lives in ``tests/sandbox_probe_retry`` precisely so it can be
exercised here with no subprocess at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.sandbox_probe_retry import (
    TIMEOUT_RETURNCODE,
    AbsentContent,
    AbsentPath,
    AllEvidence,
    CreatedPaths,
    PresentContent,
    ProbeBreach,
    ProbeTimeout,
    UnchangedBytes,
    run_until_evidence,
)


def _completed(stdout: str = "ok") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["sandbox-probe"], 0, stdout=stdout, stderr="")


def _timeout(*, output: bytes = b"partial", stderr: bytes = b"err") -> Exception:
    return subprocess.TimeoutExpired(
        cmd=["sandbox-probe"], timeout=1, output=output, stderr=stderr
    )


def test_snapshots_preserve_first_attempt_breach_before_later_overwrite(
    tmp_path: Path,
) -> None:
    """A breach seen only on attempt 1 must survive attempt 2 overwriting it."""
    network_status = tmp_path / "network-status.txt"
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        network_status.write_text(
            "OPENED" if attempts == 1 else "CLOSED", encoding="utf-8"
        )
        if attempts == 2:
            completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(network_status,),
    )

    assert [snapshot[network_status] for snapshot in probe.snapshots] == [
        b"OPENED",
        b"CLOSED",
    ]
    probe.require_completed()


def test_timeout_then_success_retries_and_completes(tmp_path: Path) -> None:
    """A first-attempt timeout is retried, and the completed retry wins."""
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _timeout()
        completed.write_text("done", encoding="utf-8")
        return _completed("second attempt ok")

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed,),
    )

    assert attempts == 2
    assert not probe.timed_out
    assert probe.result.stdout == "second attempt ok"
    probe.require_completed()  # must not raise
    # The timed-out attempt's evidence is still reported.
    assert "probe timed out after 1s" in probe.combined_output


def test_timed_out_attempt_with_all_paths_present_is_not_success(
    tmp_path: Path,
) -> None:
    """A killed attempt that already created every path must not stop the retry.

    This is the false-pass the retry originally introduced: the probe was
    killed mid-run, so its side effects prove nothing about the boundary even
    though every expected file exists.
    """
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        completed.write_text(f"attempt {attempts}", encoding="utf-8")
        if attempts == 1:
            raise _timeout()
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed,),
    )

    assert attempts == 2, "a timed-out attempt must never satisfy the success check"
    assert not probe.timed_out
    probe.require_completed()
    # The accepted evidence is attempt 2's own, not the killed attempt's.
    assert probe.completed_attempt is not None
    assert probe.completed_attempt.number == 2
    assert completed.read_text(encoding="utf-8") == "attempt 2"


def test_retry_cannot_inherit_the_timed_out_attempt_s_files(tmp_path: Path) -> None:
    """The stale-artifact false pass: attempt 2 completes but redoes nothing.

    Attempt 1 creates every expected path and is then killed. Attempt 2 exits
    normally without touching anything — a live agent CLI can return without
    reissuing the tool calls. The leftover files must NOT be accepted as that
    attempt's evidence.
    """
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            completed.write_text("written by the killed attempt", encoding="utf-8")
            raise _timeout()
        return _completed("second attempt did nothing")

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed,),
    )

    assert attempts == 2
    assert not probe.timed_out  # it did not end on a timeout...
    assert probe.completed_attempt is None, (
        "no attempt produced complete evidence, so the run must not be accepted"
    )
    # The killed attempt's file was cleared, so the caller's positive control
    # (`assert path.exists()`) fails instead of passing on a stale artifact.
    assert not completed.exists()
    assert probe.attempts[0].produced_required_evidence
    assert not probe.attempts[1].produced_required_evidence


def test_clearing_is_limited_to_the_attempt_owned_outputs(tmp_path: Path) -> None:
    """Planted fixture files and breach markers survive the reset.

    ``observed_paths`` covers evidence the probe must NOT have touched (a
    planted policy file) and evidence that must never appear (an escaped
    write). Clearing those between attempts would destroy the fixture and hide
    a breach from the caller's post-run assertions.
    """
    completed = tmp_path / "completed.txt"
    planted = tmp_path / "policy.json"
    planted.write_text("ORIGINAL", encoding="utf-8")
    escaped = tmp_path / "escaped.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            escaped.write_text("ESCAPED", encoding="utf-8")
            raise _timeout()
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed, planted, escaped),
    )

    probe.require_completed()
    assert planted.read_text(encoding="utf-8") == "ORIGINAL"
    # The breach from attempt 1 is still on disk for the caller's final check.
    assert escaped.read_text(encoding="utf-8") == "ESCAPED"
    assert probe.snapshots[0][escaped] == b"ESCAPED"


def test_two_timeouts_exhaust_and_fail_loudly(tmp_path: Path) -> None:
    """Exhausting the retries on timeouts fails, even with every path present."""
    completed = tmp_path / "completed.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        completed.write_text("done", encoding="utf-8")
        raise _timeout()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed,),
    )

    assert attempts == 2
    assert probe.timed_out
    assert probe.result.returncode == TIMEOUT_RETURNCODE

    with pytest.raises(ProbeTimeout) as excinfo:
        probe.require_completed()

    message = str(excinfo.value)
    assert "timed out on all 2 attempt(s)" in message
    # The captured evidence must survive into the failure report.
    assert "probe timed out after 1s" in message
    assert "partial" in message


def test_exhausted_run_still_exposes_every_attempt_snapshot(tmp_path: Path) -> None:
    """Breach evidence from timed-out attempts is still available to assert on."""
    escaped = tmp_path / "escaped.txt"
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        escaped.write_text("ESCAPED", encoding="utf-8")
        raise _timeout()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        observed_paths=(escaped,),
    )

    assert [snapshot[escaped] for snapshot in probe.snapshots] == [
        b"ESCAPED",
        b"ESCAPED",
    ]


def test_missing_expected_paths_without_timeout_does_not_raise(tmp_path: Path) -> None:
    """A completed-but-incomplete run is the caller's assertion to make.

    ``require_completed`` only guards the timeout case; "the probe ran but did
    not produce its files" is reported by the caller's own positive-control
    assertion, which carries a far more specific message.
    """
    completed = tmp_path / "completed.txt"

    probe = run_until_evidence(
        lambda: _completed(),
        evidence=CreatedPaths((completed,)),
        observed_paths=(completed,),
    )

    assert not probe.timed_out
    assert probe.snapshots == ({completed: None}, {completed: None})
    assert probe.completed_attempt is None
    probe.require_completed()


class _StdoutContains:
    """Evidence that lives in the process output, not on disk.

    Stands in for the native-tool probes, whose proof is a ``tool_use`` in the
    CLI's event stream: there is nothing on disk to check, and nothing to clear
    between attempts.
    """

    def __init__(self, needle: str) -> None:
        self.needle = needle
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def missing_from(self, result: subprocess.CompletedProcess[str]) -> str | None:
        if self.needle in (result.stdout or ""):
            return None
        return f"the output never contained {self.needle!r}"


def test_completed_attempt_without_its_evidence_is_retried() -> None:
    """A clean exit that did not do the work must not end the run.

    Path existence cannot express this: the native-tool probes write nothing,
    so before evidence became the caller's declaration they ran with no retry
    at all and one short-circuited interaction failed the gate.
    """
    streams = ["I declined to do that", "tool_use: Write"]

    def run_attempt() -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["probe"], 0, stdout=streams.pop(0))

    evidence = _StdoutContains("tool_use: Write")
    probe = run_until_evidence(
        run_attempt, evidence=evidence, observed_paths=(), max_attempts=3
    )

    assert len(probe.attempts) == 2
    assert probe.attempts[0].missing_evidence == (
        "the output never contained 'tool_use: Write'"
    )
    assert probe.completed_attempt is not None
    assert probe.completed_attempt.number == 2
    assert evidence.resets == 1, "reset runs before each retry, never before the first"


def test_evidence_never_produced_exhausts_without_masking_the_caller() -> None:
    """Exhaustion leaves no completed attempt and does not raise as a timeout.

    ``require_completed`` guards timeouts only, so the caller's own assertion —
    which names the boundary that went unexercised — is what fails.
    """

    def run_attempt() -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["probe"], 0, stdout="no tool call")

    probe = run_until_evidence(
        run_attempt,
        evidence=_StdoutContains("tool_use: Write"),
        observed_paths=(),
        max_attempts=3,
    )

    assert len(probe.attempts) == 3
    assert probe.completed_attempt is None
    assert not probe.timed_out
    assert probe.missing_evidence == "the output never contained 'tool_use: Write'"
    probe.require_completed()  # must not raise: this is not a timeout


def test_all_evidence_requires_every_part_and_resets_every_part(
    tmp_path: Path,
) -> None:
    """A conjunction reports the first unmet part and clears all of them.

    The in-worktree positive control needs both: the agent attempted the write
    AND the content landed, with the sink cleared so a retry cannot inherit it.
    """
    sink = tmp_path / "landed.txt"
    stream_evidence = _StdoutContains("tool_use: Write")
    evidence = AllEvidence((stream_evidence, CreatedPaths((sink,))))
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Landed the file but never called the tool — an inherited artifact
            # from somewhere else, not this probe's proof.
            sink.write_text("stale", encoding="utf-8")
            return subprocess.CompletedProcess(["probe"], 0, stdout="declined")
        sink.write_text("fresh", encoding="utf-8")
        return subprocess.CompletedProcess(["probe"], 0, stdout="tool_use: Write")

    probe = run_until_evidence(
        run_attempt, evidence=evidence, observed_paths=(sink,), max_attempts=3
    )

    assert probe.attempts[0].missing_evidence == (
        "the output never contained 'tool_use: Write'"
    ), "the first unmet part is the one reported"
    assert stream_evidence.resets == 1
    assert sink.read_text(encoding="utf-8") == "fresh", (
        "the stale artifact must have been cleared before the retry"
    )
    assert probe.completed_attempt is not None
    assert probe.completed_attempt.number == 2


def test_timed_out_attempt_never_counts_even_with_its_evidence_present() -> None:
    """A killed attempt proves nothing, whatever its output contained."""
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise subprocess.TimeoutExpired(
                cmd=["probe"], timeout=1, output=b"tool_use: Write", stderr=b""
            )
        return subprocess.CompletedProcess(["probe"], 0, stdout="tool_use: Write")

    probe = run_until_evidence(
        run_attempt,
        evidence=_StdoutContains("tool_use: Write"),
        observed_paths=(),
        max_attempts=3,
    )

    assert attempts == 2
    assert probe.attempts[0].produced_required_evidence, (
        "the evidence was present in the killed attempt's captured output..."
    )
    assert not probe.attempts[0].is_complete_evidence, (
        "...but a timed-out attempt is never usable evidence"
    )
    assert probe.completed_attempt is not None
    assert probe.completed_attempt.number == 2


def test_a_breach_is_reported_ahead_of_the_timeout_that_followed_it(
    tmp_path: Path,
) -> None:
    """Breach-first ordering, owned here so no call site can reverse it.

    Two probes had ``require_completed()`` before their snapshot assertions,
    and a run that escaped the worktree on its final attempt and then timed out
    reported the timeout — burying the breach. ``require_intact`` evaluates
    every attempt's snapshot first, so the finding that surfaces is the leak.
    """
    escaped = tmp_path / "escaped.txt"
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        escaped.write_text("ESCAPED", encoding="utf-8")
        raise _timeout()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(AbsentPath(escaped, "a write escaped the worktree"),),
    )

    assert probe.timed_out, "the run really did end on a timeout..."
    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()
    assert "a write escaped the worktree" in str(excinfo.value)

    # ...and the timeout is still there for a run with nothing to report.
    with pytest.raises(ProbeTimeout):
        probe.require_completed()


def test_declaring_a_breach_check_is_what_captures_its_path(tmp_path: Path) -> None:
    """A breach path cannot be omitted from the capture set.

    The other half of the same review finding: the leak-bearing sinks were
    breach-relevant but absent from ``observed_paths``, so nothing was captured
    to check. Naming a path in a check is now the only declaration needed.
    """
    leaky = tmp_path / "secret-read.txt"
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        leaky.write_text("TOPSECRET", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(AbsentContent(leaky, b"TOPSECRET", "the secret was read"),),
        # Deliberately NOT listed here.
        observed_paths=(),
    )

    assert probe.snapshots[0][leaky] == b"TOPSECRET"
    with pytest.raises(ProbeBreach):
        probe.require_intact()


def test_a_reset_sink_that_recorded_a_breach_is_still_reported(tmp_path: Path) -> None:
    """The retry's reset cannot erase what an earlier attempt recorded.

    A path can be both the probe's result sink and its breach record. Clearing
    it for the retry is correct — the retry must redo the work — but only
    because the capture already happened.
    """
    sink = tmp_path / "secret-read.txt"
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        sink.write_text("TOPSECRET" if attempts == 1 else "", encoding="utf-8")
        if attempts == 1:
            raise _timeout()
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((sink,)),
        breach_checks=(AbsentContent(sink, b"TOPSECRET", "the secret was read"),),
    )

    assert attempts == 2
    assert sink.read_text(encoding="utf-8") == "", "final disk state looks clean"
    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()
    assert "on attempt 1" in str(excinfo.value)


def test_present_and_unchanged_checks_detect_deletion_and_append(
    tmp_path: Path,
) -> None:
    """Planted fixtures: a marker must survive, and a ref must not grow."""
    planted = tmp_path / "settings.json"
    ref = tmp_path / "ref"

    deleted: dict[Path, bytes | None] = {planted: None, ref: b"abc\n"}
    appended: dict[Path, bytes | None] = {planted: b"MARKER", ref: b"abc\nextra"}
    intact: dict[Path, bytes | None] = {planted: b"MARKER", ref: b"abc\n"}

    marker_check = PresentContent(planted, b"MARKER", "the policy file was replaced")
    ref_check = UnchangedBytes(ref, b"abc\n", "the base ref was modified")

    assert marker_check.violated_by(deleted) is not None, "a deleted file has no marker"
    assert marker_check.violated_by(intact) is None
    assert ref_check.violated_by(appended) is not None, "appending is modifying"
    assert ref_check.violated_by(intact) is None


def test_no_breach_checks_still_requires_completion(tmp_path: Path) -> None:
    """A probe with no breach surface declares that, and still must complete."""
    completed = tmp_path / "completed.txt"

    probe = run_until_evidence(
        lambda: (_ for _ in ()).throw(_timeout()),
        evidence=CreatedPaths((completed,)),
        breach_checks=(),
    )

    with pytest.raises(ProbeTimeout):
        probe.require_intact()


def test_created_paths_rejects_an_empty_requirement() -> None:
    """Zero required paths is satisfied by an attempt that did nothing."""
    with pytest.raises(ValueError, match="at least one path"):
        CreatedPaths(())


def test_all_evidence_rejects_no_parts() -> None:
    """A conjunction of nothing is true — the same vacuity, one layer up."""
    with pytest.raises(ValueError, match="at least one part"):
        AllEvidence(())
