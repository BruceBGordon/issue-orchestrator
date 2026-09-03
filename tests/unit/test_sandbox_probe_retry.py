"""Deterministic coverage for the live sandbox probe retry policy.

The probes themselves need a real agent CLI, but their retry/timeout policy
is the part that can silently turn a half-executed security probe into a pass.
That policy lives in ``tests/sandbox_probe_retry`` precisely so it can be
exercised here with no subprocess at all.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from issue_orchestrator.infra.containment import MAX_RENDERED_CHARS

from tests.sandbox_probe_retry import (
    TIMEOUT_RETURNCODE,
    AbsentContent,
    AbsentPath,
    AllEvidence,
    CreatedPaths,
    PresentContent,
    ProbeBreach,
    ProbeRun,
    ProbeTimeout,
    UnchangedBytes,
    UndeclaredBreachPath,
    _DeclaredPaths,
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
    probe.require_intact()


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
    probe.require_intact()  # must not raise
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
    probe.require_intact()
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

    probe.require_intact()
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
        probe.require_intact()

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

    ``require_intact`` only guards the timeout case beyond the breach checks; "the probe ran but did
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
    probe.require_intact()


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

    ``require_intact`` adds only the timeout guard, so the caller's own assertion —
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
    probe.require_intact()  # must not raise: this is not a timeout


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

    Two probes had the completion guard before their snapshot assertions,
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

    # ...and the same timeout IS what surfaces when there is no breach to
    # outrank it, so the breach is being preferred rather than the timeout lost.
    clean = run_until_evidence(
        lambda: (_ for _ in ()).throw(_timeout()),
        evidence=CreatedPaths((completed,)),
        breach_checks=(AbsentPath(tmp_path / "never-written.txt", "escaped"),),
    )
    with pytest.raises(ProbeTimeout):
        clean.require_intact()


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


class _ReachesBeyondItsDeclaration:
    """A conforming check that declares one path and inspects a second.

    Not a strawman: ``BreachCheck`` is an open Protocol, so this satisfies it
    completely. While ``violated_by`` received the whole snapshot, this shape
    reported a clean run over a path nothing had captured. It declares a real
    path because a check declaring none is now refused outright.
    """

    def __init__(self, declared: Path, undeclared: Path) -> None:
        self.declared = declared
        self.undeclared = undeclared
        self.saw: bytes | None | str = "never ran"

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        self.saw = snapshot.get(self.undeclared)
        return None


def test_a_check_cannot_inspect_a_path_it_did_not_declare(tmp_path: Path) -> None:
    """Declare-is-capture is structural, not a rule implementers must follow.

    A check is handed only the entries it declared, so reaching past them
    raises instead of silently reading ``None`` and reporting intact — which
    is what a check with ``paths == ()`` would otherwise do to a real leak.
    """
    secret_sink = tmp_path / "secret-read.txt"
    completed = tmp_path / "completed.txt"
    rogue = _ReachesBeyondItsDeclaration(completed, secret_sink)

    def run_attempt() -> subprocess.CompletedProcess[str]:
        secret_sink.write_text("TOPSECRET", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(rogue,),
    )

    with pytest.raises(UndeclaredBreachPath) as excinfo:
        probe.require_intact()

    assert "did not declare" in str(excinfo.value)
    assert rogue.saw == "never ran", "the undeclared read must not have returned"


def test_the_scoped_view_refuses_every_undeclared_access_shape(
    tmp_path: Path,
) -> None:
    """``[]``, ``get()`` and ``in`` all refuse; declared entries all work.

    ``get()`` matters most: ``Mapping.get`` swallows ``KeyError`` to return its
    default, so an undeclared read would have come back as ``None`` — exactly
    the silent "no leak here" this prevents. That is why the error is not a
    ``KeyError``.
    """
    declared = tmp_path / "declared.txt"
    other = tmp_path / "other.txt"
    view = _DeclaredPaths({declared: b"value"})

    assert view[declared] == b"value"
    assert view.get(declared) == b"value"
    assert list(view) == [declared]
    assert len(view) == 1

    with pytest.raises(UndeclaredBreachPath):
        view[other]
    with pytest.raises(UndeclaredBreachPath):
        view.get(other)
    with pytest.raises(UndeclaredBreachPath):
        other in view  # noqa: B015


def test_a_declared_but_absent_path_reads_as_none_not_an_error(
    tmp_path: Path,
) -> None:
    """Absence is data, not an undeclared access — a sink may legitimately be gone."""
    declared = tmp_path / "declared.txt"

    assert _DeclaredPaths({declared: None})[declared] is None


def test_mutating_the_check_list_mid_run_cannot_desync_the_capture_set(
    tmp_path: Path,
) -> None:
    """The checks are frozen at entry, before the capture set is derived.

    While the sequence was read twice — once to build the capture set, once to
    build the run — a caller still holding the list could append during
    ``run_attempt`` and leave the run evaluating a check whose path nothing
    captured.
    """
    completed = tmp_path / "completed.txt"
    late = tmp_path / "late.txt"
    checks: list[object] = [AbsentPath(tmp_path / "escaped.txt", "escaped")]

    def run_attempt() -> subprocess.CompletedProcess[str]:
        checks.append(AbsentPath(late, "a late arrival nothing captured"))
        late.write_text("APPEARED", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=checks,  # type: ignore[arg-type]
    )

    assert len(checks) == 2, "the caller really did mutate its list"
    assert len(probe.breach_checks) == 1, "the run kept the list it was given"
    probe.require_intact()  # must not raise KeyError on the uncaptured path


def test_require_intact_is_the_only_completion_entry_point() -> None:
    """There is nothing to call that completes a run without its breach checks.

    A separate public timeout guard was callable on a run that HAD checks, and
    returned without evaluating them. Folding it in removed the bypass rather
    than documenting against it.
    """
    entry_points = {
        name
        for name in vars(ProbeRun)
        if name.startswith("require") or name.startswith("assert")
    }

    assert entry_points == {"require_intact"}


class _BrokenCheck:
    """A check whose own contract is wrong: it reads a path it never declared."""

    def __init__(self, declared: Path, undeclared: Path) -> None:
        self.declared = declared
        self.undeclared = undeclared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        return snapshot[self.undeclared] and None


class _RecordingCheck:
    """A real check that reports a breach, and remembers whether it ran."""

    def __init__(self, path: Path, marker: bytes, detail: str) -> None:
        self.path = path
        self.marker = marker
        self.detail = detail
        self.ran = False

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.path,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        self.ran = True
        if self.marker in (snapshot[self.path] or b""):
            return self.detail
        return None


def test_a_broken_check_cannot_hide_a_breach_a_later_check_finds(
    tmp_path: Path,
) -> None:
    """A contract error of ours must never outrank the breach it precedes.

    Evaluation used to abort on the first raising check, so a real breach that
    a later check would have caught was hidden behind an earlier check's own
    bug — the same family as the completion-before-breach ordering already
    fixed. Every check now runs; the outcomes are ranked afterwards.
    """
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    broken = _BrokenCheck(completed, tmp_path / "never-declared.txt")
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    def run_attempt() -> subprocess.CompletedProcess[str]:
        leaked.write_text("TOPSECRET", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        # The broken one is FIRST, so it is what aborted evaluation before.
        breach_checks=(broken, real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "the secret was read" in str(excinfo.value)
    assert real.ran, "the check after the broken one must still have run"


def test_a_broken_check_is_still_loud_when_there_is_no_breach(
    tmp_path: Path,
) -> None:
    """Ranking below a breach is not the same as being swallowed.

    With nothing to outrank it, a check that cannot inspect what it claims to
    is the most serious thing in the run: it means coverage that looks present
    is absent.
    """
    completed = tmp_path / "completed.txt"
    clean = tmp_path / "secret-read.txt"
    broken = _BrokenCheck(completed, tmp_path / "never-declared.txt")
    real = _RecordingCheck(clean, b"TOPSECRET", "the secret was read")

    def run_attempt() -> subprocess.CompletedProcess[str]:
        clean.write_text("", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(broken, real),
    )

    with pytest.raises(UndeclaredBreachPath) as excinfo:
        probe.require_intact()

    assert real.ran, "the other check still ran before the error was reported"
    notes = getattr(excinfo.value, "__notes__", [])
    assert any("attempt 1 of 1" in note for note in notes), notes


def test_a_broken_check_outranks_a_timeout(tmp_path: Path) -> None:
    """Breach, then broken check, then timeout — the full order."""
    completed = tmp_path / "completed.txt"
    broken = _BrokenCheck(completed, tmp_path / "never-declared.txt")

    probe = run_until_evidence(
        lambda: (_ for _ in ()).throw(_timeout()),
        evidence=CreatedPaths((completed,)),
        breach_checks=(broken,),
    )

    assert probe.timed_out
    with pytest.raises(UndeclaredBreachPath):
        probe.require_intact()


def test_a_breach_outranks_a_broken_check_on_an_earlier_attempt(
    tmp_path: Path,
) -> None:
    """Rank beats order of discovery, across attempts as well as within one."""
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    broken = _BrokenCheck(completed, tmp_path / "never-declared.txt")
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")
    attempts = 0

    def run_attempt() -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Attempt 1: the broken check raises, nothing has leaked yet.
            leaked.write_text("", encoding="utf-8")
            raise _timeout()
        leaked.write_text("TOPSECRET", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(broken, real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "on attempt 2" in str(excinfo.value)


def test_a_check_declaring_no_paths_is_refused(tmp_path: Path) -> None:
    """Zero declared paths is the empty-evidence vacuity, one layer over.

    ``paths`` is the check's entire input, so a check declaring none is handed
    an empty view and can only report intact — coverage that is not there.
    Refused where CreatedPaths(()) and AllEvidence(()) are refused.
    """

    class _DeclaresNothing:
        @property
        def paths(self) -> tuple[Path, ...]:
            return ()

        def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
            return None

    completed = tmp_path / "completed.txt"

    with pytest.raises(ValueError, match="declares no paths"):
        run_until_evidence(
            lambda: _completed(),
            evidence=CreatedPaths((completed,)),
            breach_checks=(_DeclaresNothing(),),
        )

    # Also refused on a directly constructed run, so the rule holds wherever
    # the checks are held rather than only on the path that spawns a probe.
    with pytest.raises(ValueError, match="declares no paths"):
        ProbeRun(attempts=(), breach_checks=(_DeclaresNothing(),))


def test_no_breach_checks_is_still_how_a_probe_says_it_has_no_surface(
    tmp_path: Path,
) -> None:
    """Refusing a zero-path CHECK must not refuse zero CHECKS.

    The in-worktree positive control legitimately has nothing to guard, and
    says so with ``breach_checks=()``. That stays valid.
    """
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt, evidence=CreatedPaths((completed,)), breach_checks=()
    )

    probe.require_intact()


class _UnannotatableError(Exception):
    """An exception whose ``add_note`` raises.

    ``add_note`` is a method the exception object is free to override, and the
    exception object comes from a check — caller code. Annotating it inside the
    collection loop meant this shape aborted evaluation.
    """

    def add_note(self, note: str) -> None:
        raise RuntimeError("note annotation failed")


class _HostileReprCheck:
    """A check that raises, and whose ``__repr__`` raises while being blamed."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    def __repr__(self) -> str:
        raise RuntimeError("repr exploded")

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        raise ValueError("this check is broken")


class _RaisesUnannotatable:
    """A check that raises an exception which cannot be annotated."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        raise _UnannotatableError("this check is broken")


class _HostileReasonCheck:
    """A check that reports a breach whose description cannot be rendered."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        class _Unrenderable:
            def __str__(self) -> str:
                raise RuntimeError("str exploded")

            __repr__ = __str__

        return _Unrenderable()  # type: ignore[return-value]


def _leaking_run(
    leaked: Path, completed: Path
) -> Callable[[], subprocess.CompletedProcess[str]]:
    def run_attempt() -> subprocess.CompletedProcess[str]:
        leaked.write_text("TOPSECRET", encoding="utf-8")
        completed.write_text("done", encoding="utf-8")
        return _completed()

    return run_attempt


def test_a_failing_annotation_cannot_hide_a_breach(tmp_path: Path) -> None:
    """The masking bug, recreated one level up by the code that annotates it.

    The first check raises an exception whose ``add_note`` raises. Annotating
    ran uncontained inside the collection loop, so that ``RuntimeError``
    escaped, evaluation died, and the second check — holding a real breach —
    was never invoked.
    """
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    probe = run_until_evidence(
        _leaking_run(leaked, completed),
        evidence=CreatedPaths((completed,)),
        breach_checks=(_RaisesUnannotatable(completed), real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "the secret was read" in str(excinfo.value)
    assert real.ran, "the check after the unannotatable failure must still run"


def test_a_raising_repr_cannot_hide_a_breach(tmp_path: Path) -> None:
    """Naming the check must not call the check's own ``__repr__``.

    ``safe_type_name`` gets the type's name without running caller code, so a
    check that explodes while being blamed no longer takes the evaluation with
    it.
    """
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    probe = run_until_evidence(
        _leaking_run(leaked, completed),
        evidence=CreatedPaths((completed,)),
        breach_checks=(_HostileReprCheck(completed), real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "the secret was read" in str(excinfo.value)
    assert real.ran


def test_an_unrenderable_breach_description_is_reported_not_discarded(
    tmp_path: Path,
) -> None:
    """A breach whose description will not render is still a breach.

    Rendering a check's reason is caller code, so it goes through the
    containment owner: a hostile ``__str__`` degrades to the type name instead
    of aborting the loop. Reporting it beats the older behaviour, where the
    explosion was collected as a check error and the breach the check had
    ACTUALLY reported was thrown away. Evaluation still reaches the checks
    after it.
    """
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    probe = run_until_evidence(
        _leaking_run(leaked, completed),
        evidence=CreatedPaths((completed,)),
        breach_checks=(_HostileReasonCheck(completed), real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    message = str(excinfo.value)
    assert "_Unrenderable" in message, "the breach degrades to a safe name"
    assert real.ran, "the checks after it still ran"


def test_a_breach_reported_by_a_later_check_is_unaffected(tmp_path: Path) -> None:
    """The ordinary case, kept explicit next to the degrading one."""
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    quiet = _RecordingCheck(completed, b"NEVER-PRESENT", "not this one")
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    probe = run_until_evidence(
        _leaking_run(leaked, completed),
        evidence=CreatedPaths((completed,)),
        breach_checks=(quiet, real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "the secret was read" in str(excinfo.value)
    assert quiet.ran and real.ran


def test_a_broken_check_is_still_reported_when_its_note_could_not_be_written(
    tmp_path: Path,
) -> None:
    """Losing the note is acceptable; losing the failure is not.

    With no breach to outrank it, the unannotatable exception is still raised
    in full — same object, same type — just without its context note.
    """
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_RaisesUnannotatable(completed),),
    )

    with pytest.raises(_UnannotatableError):
        probe.require_intact()


def test_annotation_still_lets_a_teardown_signal_out(tmp_path: Path) -> None:
    """Containment is for failures, never for "the caller is going away".

    A ``KeyboardInterrupt`` raised while annotating must win, exactly as the
    containment owner requires — the operator's Ctrl-C is not a contained
    diagnostic failure.
    """

    class _InterruptingError(Exception):
        def add_note(self, note: str) -> None:
            raise KeyboardInterrupt

    class _RaisesInterrupting:
        def __init__(self, declared: Path) -> None:
            self.declared = declared

        @property
        def paths(self) -> tuple[Path, ...]:
            return (self.declared,)

        def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
            raise _InterruptingError("broken")

    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_RaisesInterrupting(completed),),
    )

    with pytest.raises(KeyboardInterrupt):
        probe.require_intact()


def test_a_hostile_repr_does_not_cost_the_diagnostic_note(tmp_path: Path) -> None:
    """Containing the explosion is not enough; the note must survive it.

    Rendering the check as ``{check!r}`` and catching what that throws keeps
    the loop alive but leaves the contained failure with NO context — the
    annotation died before it was written. ``safe_type_name`` never runs the
    check's ``__repr__``, so the note is still written and still says which
    check broke.
    """
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_HostileReprCheck(completed),),
    )

    with pytest.raises(ValueError) as excinfo:
        probe.require_intact()

    notes = getattr(excinfo.value, "__notes__", [])
    assert any("_HostileReprCheck" in note for note in notes), notes
    assert any("attempt 1 of 1" in note for note in notes), notes


class _ExitingReasonCheck:
    """A check whose breach description raises ``SystemExit`` while rendering."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        class _Exits:
            def __str__(self) -> str:
                raise SystemExit(91)

            __repr__ = __str__

        return _Exits()  # type: ignore[return-value]


class _EnormousReasonCheck:
    """A check reporting a breach described in 100,000 characters."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        return "x" * 100_000


class _ExitingCheck:
    """A check that raises ``SystemExit`` outright."""

    def __init__(self, declared: Path) -> None:
        self.declared = declared

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.declared,)

    def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
        raise SystemExit(91)


def test_a_system_exit_from_a_check_cannot_hide_a_breach(tmp_path: Path) -> None:
    """SystemExit is containable here, exactly as the owner documents.

    ``except Exception`` was not a boundary: a check raising ``SystemExit``
    went straight out, aborting evaluation before the check after it ran — so
    a broken check could both substitute its own exit status for the run's
    outcome AND hide a real breach behind it. Containing it (and re-raising
    only TEARDOWN_SIGNALS) puts SystemExit under the same ranking as every
    other check failure.
    """
    completed = tmp_path / "completed.txt"
    leaked = tmp_path / "secret-read.txt"
    real = _RecordingCheck(leaked, b"TOPSECRET", "the secret was read")

    probe = run_until_evidence(
        _leaking_run(leaked, completed),
        evidence=CreatedPaths((completed,)),
        breach_checks=(_ExitingCheck(completed), real),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "the secret was read" in str(excinfo.value)
    assert real.ran, "the check after the SystemExit must still have run"


def test_a_system_exit_from_a_check_is_still_reported_when_nothing_outranks_it(
    tmp_path: Path,
) -> None:
    """Contained is not swallowed: with no breach, the SystemExit is raised."""
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_ExitingCheck(completed),),
    )

    with pytest.raises(SystemExit):
        probe.require_intact()


def test_a_breach_description_raising_system_exit_degrades(tmp_path: Path) -> None:
    """Rendering contains SystemExit too, so the breach is still reported."""
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_ExitingReasonCheck(completed),),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    assert "_Exits" in str(excinfo.value)


def test_a_teardown_signal_from_a_check_still_wins(tmp_path: Path) -> None:
    """Containing SystemExit must not mean containing a Ctrl-C."""

    class _InterruptingCheck:
        def __init__(self, declared: Path) -> None:
            self.declared = declared

        @property
        def paths(self) -> tuple[Path, ...]:
            return (self.declared,)

        def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
            raise KeyboardInterrupt

    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_InterruptingCheck(completed),),
    )

    with pytest.raises(KeyboardInterrupt):
        probe.require_intact()


def test_an_enormous_breach_description_is_capped(tmp_path: Path) -> None:
    """A hostile diagnostic must not become a 100,000-character failure.

    Only the exception rendering was capped, so a check could put its whole
    payload into the ProbeBreach message.
    """
    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_EnormousReasonCheck(completed),),
    )

    with pytest.raises(ProbeBreach) as excinfo:
        probe.require_intact()

    message = str(excinfo.value)
    assert "x" * MAX_RENDERED_CHARS in message, "the description survives, capped"
    assert "x" * (MAX_RENDERED_CHARS + 1) not in message, "and no further"
    assert len(message) < MAX_RENDERED_CHARS + 200, len(message)


def test_an_enormous_check_type_name_is_capped_in_the_note(tmp_path: Path) -> None:
    """The same cap on the other diagnostic path: the annotation."""

    class _VerboseMeta(type):
        @property
        def __name__(cls) -> str:
            return "n" * 100_000

    class _VerboseCheck(metaclass=_VerboseMeta):
        def __init__(self, declared: Path) -> None:
            self.declared = declared

        @property
        def paths(self) -> tuple[Path, ...]:
            return (self.declared,)

        def violated_by(self, snapshot) -> str | None:  # noqa: ANN001
            raise ValueError("broken")

    completed = tmp_path / "completed.txt"

    def run_attempt() -> subprocess.CompletedProcess[str]:
        completed.write_text("done", encoding="utf-8")
        return _completed()

    probe = run_until_evidence(
        run_attempt,
        evidence=CreatedPaths((completed,)),
        breach_checks=(_VerboseCheck(completed),),
    )

    with pytest.raises(ValueError) as excinfo:
        probe.require_intact()

    notes = getattr(excinfo.value, "__notes__", [])
    assert notes
    assert "n" * MAX_RENDERED_CHARS in notes[0], "the type name survives, capped"
    assert "n" * (MAX_RENDERED_CHARS + 1) not in notes[0], "and no further"
    assert len(notes[0]) < MAX_RENDERED_CHARS + 200, len(notes[0])
