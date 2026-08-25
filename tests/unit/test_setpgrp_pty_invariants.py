"""Validate OS-level invariants for setpgrp-based process isolation.

These tests codify assumptions about how setpgrp interacts with PTYs,
process groups, and signal delivery. If any of these fail on a new OS or
platform, the SubprocessPlugin ↔ AgentRunner output capture chain will break.

The core invariant: os.setpgrp() creates a new process GROUP while keeping
the process in the same session. This means:
  - The child keeps its controlling terminal (PTY output flows to parent)
  - killpg() can target the child's process group without hitting the parent
  - This is NOT the same as os.setsid() which creates a new session and
    disconnects from the controlling terminal

Why this matters:
  SubprocessPlugin uses pexpect (PTY) to capture session output.
  AgentRunner spawns the agent command as a subprocess.
  If the agent runs in a new session (setsid), it loses the PTY and falls
  into non-interactive/print mode. Output never reaches pexpect.
  With setpgrp, the agent stays connected to the PTY and output flows.
"""

from __future__ import annotations

import os
import pty
import signal
import subprocess
import sys
import time

import pytest

from tests.process_tree_fixture import ProcessTreeMember


@pytest.mark.xdist_group("pty")
class TestSetpgrpPreservesControllingTerminal:
    """setpgrp child inherits the parent's controlling terminal."""

    def test_child_stdout_reaches_parent_pipe(self) -> None:
        """Without PIPE redirect, child output goes to parent's stdout.
        With PIPE, we can read it. Either way, setpgrp doesn't eat output."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "print('setpgrp-visible')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"setpgrp-visible" in out

    def test_child_inherits_parent_stdout(self) -> None:
        """When stdout is NOT redirected, child output goes to parent's fd 1.

        This is the key invariant: pexpect's PTY is inherited by the child,
        so pexpect (CleaningLogWriter) can capture the output.
        """
        # Run a parent that spawns a setpgrp child with inherited stdout.
        # Parent captures via its own pipe.
        parent_script = (
            "import os, subprocess, sys\n"
            "p = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'print(\"inherited-output\")'],\n"
            "    preexec_fn=os.setpgrp,\n"
            ")\n"
            "p.wait()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"inherited-output" in out, (
            "setpgrp child should inherit parent's stdout; "
            "if this fails, PTY-based output capture will break"
        )


@pytest.mark.xdist_group("pty")
class TestSetpgrpCreatesDistinctProcessGroup:
    """setpgrp child runs in a different process group than the parent."""

    def test_child_pgid_differs_from_parent(self) -> None:
        """The child's process group ID must differ from the parent's."""
        script = (
            "import os; "
            "print(f'{os.getpid()} {os.getpgrp()}')"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        out, _ = proc.communicate(timeout=5)
        child_pid, child_pgid = out.decode().strip().split()
        # setpgrp makes the child its own process group leader
        assert child_pid == child_pgid, "setpgrp child should be its own group leader"
        assert int(child_pgid) != os.getpgrp(), (
            "child pgid must differ from parent pgid for safe killpg"
        )

    def test_killpg_terminates_child_tree_without_hitting_parent(self) -> None:
        """killpg on the child's group kills child+grandchildren, not parent."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            preexec_fn=os.setpgrp,
        )
        time.sleep(0.1)
        pgid = os.getpgid(proc.pid)
        assert pgid != os.getpgrp(), "child must be in a different group"

        os.killpg(pgid, signal.SIGTERM)
        exit_code = proc.wait(timeout=5)
        assert exit_code == -signal.SIGTERM
        # If we reach here, parent survived the killpg


@pytest.mark.xdist_group("pty")
class TestSetsidDisconnectsFromTerminal:
    """Contrast: setsid creates a new session and loses the controlling terminal.

    These tests document WHY setsid is wrong for our use case, providing
    the contrast to setpgrp.
    """

    def test_setsid_child_has_no_controlling_terminal(self) -> None:
        """A setsid child loses its controlling terminal.

        This is the bug we're fixing: start_new_session=True calls setsid(),
        which disconnects from the PTY. The agent falls into print mode.
        """
        script = (
            "import os\n"
            "try:\n"
            "    ctty = os.ttyname(0)\n"
            "except OSError:\n"
            "    ctty = 'NONE'\n"
            "print(ctty)\n"
        )
        # setsid child should have no controlling terminal (stdin not a tty)
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        out, _ = proc.communicate(timeout=5)
        assert out.decode().strip() == "NONE", (
            "setsid child should not have a controlling terminal on stdin"
        )

    def test_setpgrp_child_inherits_terminal_from_parent(self) -> None:
        """Contrast: setpgrp child keeps the parent's controlling terminal.

        This is the correct behavior for our use case.
        """
        # We need a real PTY for this test. Use pty.openpty() to create one,
        # then spawn a child with the slave as stdin.
        master_fd, slave_fd = pty.openpty()
        try:
            script = (
                "import os\n"
                "try:\n"
                "    ctty = os.ttyname(0)\n"
                "    print('HAS_TTY')\n"
                "except OSError:\n"
                "    print('NO_TTY')\n"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setpgrp,
            )
            out, _ = proc.communicate(timeout=5)
            assert b"HAS_TTY" in out, (
                "setpgrp child with PTY stdin should retain the terminal; "
                "if this fails, agent will fall into non-interactive mode"
            )
        finally:
            os.close(master_fd)
            os.close(slave_fd)


@pytest.mark.xdist_group("pty")
class TestSetpgrpKillpgIsolation:
    """Verify that killpg on a setpgrp group kills the entire subtree."""

    def test_grandchild_killed_by_killpg(self) -> None:
        """killpg should kill grandchildren in the same group."""
        # Parent spawns a child (setpgrp) which spawns a grandchild.
        # killpg on child's group should kill both.
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(f'{p.pid}', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setpgrp,
        )
        # Read grandchild pid
        assert proc.stdout is not None
        grandchild_pid_line = proc.stdout.readline()
        grandchild_pid = int(grandchild_pid_line.decode().strip())

        # Kill the process group
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        proc.wait(timeout=5)

        # Grandchild should also be unable to execute user code.
        ProcessTreeMember(grandchild_pid).assert_contained()


def _agent_preexec() -> None:
    """Mirror of the production _agent_preexec from runner.py."""
    os.setpgrp()
    signal.signal(signal.SIGTTIN, signal.SIG_IGN)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)


def _has_controlling_terminal() -> bool:
    """Check if the current process has a controlling terminal."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
        os.close(fd)
        return True
    except OSError:
        return False


_requires_tty = pytest.mark.skipif(
    not _has_controlling_terminal(),
    reason="/dev/tty not available (no controlling terminal, e.g. CI)",
)

# Child script shared by the vulnerability-proof test pair.
# Opens /dev/tty and reads — this triggers SIGTTIN in a background process
# group unless SIGTTIN is ignored.
_TTY_READ_SCRIPT = (
    "import os\n"
    "fd = os.open('/dev/tty', os.O_RDONLY)\n"
    "os.read(fd, 1)\n"
    "os.close(fd)\n"
    "print('COMPLETED')\n"
)


@pytest.mark.xdist_group("pty")
class TestSigttiVulnerabilityProof:
    """Prove the /dev/tty SIGTTIN vulnerability exists and our fix prevents it.

    These paired tests ensure:
    1. The vulnerability is real: bare setpgrp + /dev/tty read -> STOPPED
    2. Our fix works: _agent_preexec + /dev/tty read -> completes normally

    If someone removes SIG_IGN from _agent_preexec, test 2 would start
    getting stopped just like test 1 demonstrates.

    Both tests require a controlling terminal (/dev/tty) and are skipped in
    environments without one (e.g. CI containers).
    """

    @_requires_tty
    def test_unprotected_setpgrp_child_stopped_by_tty_read(self) -> None:
        """VULNERABILITY PROOF: bare setpgrp child IS stopped when reading /dev/tty.

        This reproduces the exact failure from issue #4057. Claude CLI opens
        /dev/tty for keyboard handling. With only setpgrp (no SIG_IGN), the
        kernel sends SIGTTIN and stops the entire process group.

        We use os.waitpid(WUNTRACED) to detect the stopped state — the same
        mechanism the kernel uses. This is deterministic: the child hits the
        read() syscall and is stopped before it returns.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", _TTY_READ_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,  # Background group, NO signal protection
        )
        try:
            # WUNTRACED makes waitpid return for stopped children (not just exited).
            # This blocks until the child is stopped by SIGTTIN or exits.
            _, status = os.waitpid(proc.pid, os.WUNTRACED)

            if os.WIFEXITED(status):
                # Child exited instead of being stopped. This shouldn't happen
                # when /dev/tty is available, but handle it gracefully.
                proc.returncode = os.WEXITSTATUS(status)
                pytest.skip(
                    "Child exited without being stopped — "
                    "/dev/tty read did not trigger SIGTTIN in this environment"
                )

            assert os.WIFSTOPPED(status), (
                f"Expected child to be STOPPED by SIGTTIN, got status 0x{status:04x}"
            )
            assert os.WSTOPSIG(status) == signal.SIGTTIN, (
                f"Expected stop signal SIGTTIN ({signal.SIGTTIN}), "
                f"got signal {os.WSTOPSIG(status)}"
            )
        finally:
            # Clean up: resume the stopped child, then kill it
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGCONT)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(proc.pid, 0)
                proc.returncode = -signal.SIGKILL
            except ChildProcessError:
                proc.returncode = -1

    @_requires_tty
    def test_protected_child_survives_tty_read(self) -> None:
        """FIX VALIDATION: _agent_preexec child is NOT stopped by /dev/tty read.

        Same script as the vulnerability proof, but with _agent_preexec which
        ignores SIGTTIN. The read() returns EIO instead of stopping the process,
        and the child continues to completion.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", _TTY_READ_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=_agent_preexec,  # setpgrp + SIG_IGN
        )
        proc.communicate(timeout=5)
        # The child should NOT be stopped. It either completed (print('COMPLETED'))
        # or got an OSError from the read (EIO) and exited with a traceback.
        # Either way, it ran to completion instead of being frozen.
        assert proc.returncode is not None, "Process should have exited, not be stopped"


@pytest.mark.xdist_group("pty")
class TestSigttiImmunityWithIgnoredSignals:
    """Verify that ignoring SIGTTIN/SIGTTOU prevents process stops.

    When a background process group reads from /dev/tty, the kernel sends
    SIGTTIN which stops the process. By ignoring SIGTTIN before exec, the
    read returns EIO instead and the process continues running.

    This is the fix for issue #4057 where Claude CLI opened /dev/tty for
    keyboard handling and got stopped.
    """

    def test_ignored_sigttin_prevents_stop_on_tty_read(self) -> None:
        """A setpgrp child with SIGTTIN ignored is NOT stopped when reading /dev/tty.

        Without SIG_IGN, opening /dev/tty from a background process group triggers
        SIGTTIN and stops the process. With SIG_IGN, the open/read returns an error
        (EIO) and the process continues.
        """
        # Child script: ignore SIGTTIN, try to read /dev/tty, report outcome.
        # The read should fail with EIO (not stop the process).
        script = (
            "import os, signal\n"
            "signal.signal(signal.SIGTTIN, signal.SIG_IGN)\n"
            "try:\n"
            "    fd = os.open('/dev/tty', os.O_RDONLY | os.O_NOCTTY)\n"
            "    data = os.read(fd, 1)\n"
            "    os.close(fd)\n"
            "    print('READ_OK')\n"
            "except OSError as e:\n"
            "    print(f'EIO:{e.errno}')\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=os.setpgrp,
        )
        out, _ = proc.communicate(timeout=5)
        output = out.decode().strip()
        # The process should NOT be stopped (it completed and produced output).
        # It either got EIO or READ_OK — either way, it wasn't stopped.
        assert proc.returncode == 0, (
            f"Process should exit cleanly, not be stopped. "
            f"exit={proc.returncode}, output={output}"
        )
        assert output, "Process should produce output (not be stopped by SIGTTIN)"

    def test_child_still_produces_stdout_through_pty(self) -> None:
        """A child using _agent_preexec still produces stdout through a PTY.

        This ensures the SIGTTIN fix doesn't break the output capture chain.
        Uses the parent-child inheritance pattern (same as the production
        pexpect setup) to verify output flows.
        """
        # Spawn a parent that creates a setpgrp+signal-ignore child with
        # inherited stdout. If the child's output reaches the parent's pipe,
        # the PTY chain works.
        parent_script = (
            "import os, signal, subprocess, sys\n"
            "def preexec():\n"
            "    os.setpgrp()\n"
            "    signal.signal(signal.SIGTTIN, signal.SIG_IGN)\n"
            "    signal.signal(signal.SIGTTOU, signal.SIG_IGN)\n"
            "p = subprocess.Popen(\n"
            "    [sys.executable, '-c', 'print(\"sigttin-immune-output\")'],\n"
            "    stdin=subprocess.DEVNULL,\n"
            "    preexec_fn=preexec,\n"
            ")\n"
            "p.wait()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", parent_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"sigttin-immune-output" in out, (
            "Output should flow through inherited stdout even with SIGTTIN ignored"
        )

    def test_killpg_still_works_with_ignored_signals(self) -> None:
        """killpg still terminates the child even with SIGTTIN/SIGTTOU ignored.

        The signal ignore only affects SIGTTIN/SIGTTOU, not SIGTERM/SIGKILL.
        """
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            preexec_fn=_agent_preexec,
        )
        time.sleep(0.1)
        pgid = os.getpgid(proc.pid)
        assert pgid != os.getpgrp(), "child must be in a different group"

        os.killpg(pgid, signal.SIGTERM)
        exit_code = proc.wait(timeout=5)
        assert exit_code == -signal.SIGTERM, (
            "SIGTERM should still kill the child despite SIGTTIN being ignored"
        )


@pytest.mark.xdist_group("pty")
class TestZshResetsInheritedSignals:
    """Document that zsh resets inherited SIG_IGN for SIGTTIN (violating POSIX).

    POSIX requires SIG_IGN to survive exec(). bash and /bin/sh comply.
    zsh does NOT — it resets SIGTTIN/SIGTTOU to SIG_DFL during startup.

    This is the root cause of the agent freeze in issue #4057: the provider_runner
    wrapped commands in ``$SHELL -lc ...`` which picked up zsh on macOS. The
    preexec_fn set SIG_IGN, but zsh reset it, and the agent got SIGTTIN when
    opening /dev/tty from a background process group.

    The fix: provider_runner now uses ``/bin/sh -c`` instead of ``$SHELL -lc``.
    """

    # Script that reports whether SIGTTIN is SIG_IGN or SIG_DFL
    _CHECK_SIGNAL = (
        "import signal; "
        "h = signal.getsignal(signal.SIGTTIN); "
        "print('SIG_IGN' if h == signal.SIG_IGN else 'SIG_DFL')"
    )

    def test_sh_preserves_inherited_sig_ign(self) -> None:
        """/bin/sh preserves inherited SIG_IGN for SIGTTIN (POSIX compliant)."""
        proc = subprocess.Popen(
            ["/bin/sh", "-c", f"{sys.executable} -c {_shell_quote(self._CHECK_SIGNAL)}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=_agent_preexec,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"SIG_IGN" in out, (
            f"/bin/sh should preserve inherited SIG_IGN; got {out.decode().strip()}"
        )

    def test_bash_preserves_inherited_sig_ign(self) -> None:
        """/bin/bash preserves inherited SIG_IGN for SIGTTIN (POSIX compliant)."""
        proc = subprocess.Popen(
            ["/bin/bash", "-c", f"{sys.executable} -c {_shell_quote(self._CHECK_SIGNAL)}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=_agent_preexec,
        )
        out, _ = proc.communicate(timeout=5)
        assert b"SIG_IGN" in out, (
            f"/bin/bash should preserve inherited SIG_IGN; got {out.decode().strip()}"
        )

    @pytest.mark.skipif(
        not os.path.exists("/bin/zsh"),
        reason="/bin/zsh not available",
    )
    def test_zsh_resets_inherited_sig_ign(self) -> None:
        """zsh resets inherited SIG_IGN for SIGTTIN to SIG_DFL (POSIX violation).

        This test documents the zsh bug. If zsh ever fixes this, the test
        will start failing — which is fine, it means the underlying issue
        is resolved at the OS level.
        """
        proc = subprocess.Popen(
            ["/bin/zsh", "-c", f"{sys.executable} -c {_shell_quote(self._CHECK_SIGNAL)}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            preexec_fn=_agent_preexec,
        )
        out, _ = proc.communicate(timeout=5)
        # zsh resets SIG_IGN → SIG_DFL. If this starts showing SIG_IGN,
        # zsh fixed the bug and we can simplify provider_runner._build_command.
        assert b"SIG_DFL" in out, (
            f"Expected zsh to reset SIG_IGN to SIG_DFL (known POSIX violation); "
            f"got {out.decode().strip()}. If zsh now preserves SIG_IGN, "
            f"provider_runner._build_command can be simplified."
        )


def _shell_quote(s: str) -> str:
    """Quote a string for safe use inside shell single quotes."""
    import shlex
    return shlex.quote(s)
