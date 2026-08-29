"""Attributed shutdown-signal handling.

When an orchestrator dies on a SIGTERM, operators need to know **who** sent it,
not just that one arrived. Python's asyncio ``add_signal_handler`` does not
expose the sender, so a bare ``kill`` from outside the orchestrator system is
unattributable — the handler can only log ``os.getppid()`` (the *parent*), which
is a red herring: the parent is rarely the sender, and blaming it has sent real
investigations down the wrong path.

POSIX *does* report the sender via ``siginfo.si_pid`` — but only through
``sigwaitinfo``/``sigtimedwait``, not through a normal handler. So this module:

1. blocks SIGTERM/SIGINT process-wide (``block_shutdown_signals``, called once at
   the entry point before any threads start, so every thread inherits the block
   and the signals stay pending),
2. starts the consumer immediately (``begin_shutdown_watch``) so the window
   before the event loop exists stays stoppable — a signal there is attributed
   and forces exit rather than hanging until startup completes, and
3. once the loop is live, attaches the graceful callback
   (``install_attributed_shutdown``): the consumer dequeues signals on a
   dedicated thread via ``signal.sigwaitinfo``, logs the real sender pid/uid +
   resolved command line, then runs the caller's shutdown callback on the loop.

On platforms without ``sigwaitinfo``/``pthread_sigmask`` (e.g. Windows) it falls
back to asyncio signal handlers — still honest: the fallback message states the
sender is not reported rather than implying the parent sent it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# Signals that mean "shut down". SIGTERM is what supervisors/`kill` send;
# SIGINT is Ctrl+C in a foreground terminal.
_SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)

# Exit code when a signal forces termination during the startup window (before
# the orchestrator/loop exist). 0: an operator-requested stop is not a failure.
_STARTUP_SIGNAL_EXIT_CODE = 0


def supports_sender_attribution() -> bool:
    """True if this platform can report the signal sender (POSIX sigwaitinfo)."""
    return hasattr(signal, "sigwaitinfo") and hasattr(signal, "pthread_sigmask")


def block_shutdown_signals() -> bool:
    """Block SIGTERM/SIGINT in this thread (and threads spawned afterward).

    Must be called FIRST in the process entry point, before any threads are
    created, so every thread inherits the block. Then the signals stay pending
    until the dedicated ``sigwaitinfo`` thread dequeues them — which is what lets
    us read the sender. Returns ``True`` if blocking was applied, ``False`` on
    platforms without ``pthread_sigmask`` (caller should use the asyncio
    fallback).
    """
    if not supports_sender_attribution():
        return False
    signal.pthread_sigmask(signal.SIG_BLOCK, set(_SHUTDOWN_SIGNALS))
    return True


def unblock_shutdown_signals_in_child() -> None:
    """Reset the shutdown-signal mask in a forked child, before ``exec``.

    ``block_shutdown_signals`` blocks SIGTERM/SIGINT process-wide so the
    ``sigwaitinfo`` consumer can read the sender. POSIX children inherit that
    mask, and Python's ``subprocess`` does NOT reset it (``restore_signals`` only
    restores signal *dispositions*, not the mask) — so without this, processes
    the orchestrator launches (agent sessions, validation commands, …) inherit
    the block and ignore a normal SIGTERM, needing SIGKILL. In particular the
    agent kill path is ``killpg(pgid, SIGTERM)`` → grace → SIGKILL, so a blocked
    agent would always burn the grace period and be force-killed. Safe to call
    from ``preexec_fn`` (``pthread_sigmask`` is async-signal-safe). No-op where
    the signals were never blocked.
    """
    if not supports_sender_attribution():
        return
    signal.pthread_sigmask(signal.SIG_UNBLOCK, set(_SHUTDOWN_SIGNALS))


def child_signal_reset_preexec() -> Callable[[], None] | None:
    """A ``preexec_fn`` that resets the shutdown-signal mask, or ``None``.

    Returns ``None`` when the signals were never blocked (e.g. macOS, Windows),
    so a spawn site can pass it straight through as ``preexec_fn=...`` without
    forcing fork+exec or tripping platforms that reject ``preexec_fn``. On the
    blocking platform it returns ``unblock_shutdown_signals_in_child``.
    """
    if not supports_sender_attribution():
        return None
    return unblock_shutdown_signals_in_child


def describe_sender(pid: int) -> str:
    """Best-effort command line for a sender pid (it may already be gone)."""
    if pid <= 0:
        return "unknown (not reported)"
    try:
        result = subprocess.run(
            # -ww: procps truncates COMMAND to $COLUMNS even into a pipe,
            # which would silently shorten the attribution this returns.
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "(sender lookup failed)"
    cmd = result.stdout.strip()
    return cmd or "(sender already exited)"


def format_shutdown_signal_log(signum: int, sender_pid: int, sender_uid: int) -> str:
    """Human/operator log line naming the real sender of a shutdown signal."""
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = f"signal {signum}"
    return (
        f"Received {name} (signum={signum}) from pid={sender_pid} "
        f"(uid={sender_uid}, cmd={describe_sender(sender_pid)!r}); requesting "
        f"shutdown. For attributable stops prefer POST /api/shutdown with a "
        f"'reason'."
    )


class _GracefulTarget:
    """Where an attributed shutdown signal is routed.

    Before the event loop and orchestrator exist there is nothing to stop
    gracefully, so a signal in that window is logged (with its sender, by the
    consumer) and then forces an immediate process exit — keeping a slow or hung
    startup stoppable with SIGTERM instead of leaving the *blocked* signal pending
    until the build finishes (which would force an operator to escalate to
    SIGKILL). Once ``attach`` is called from ``run()`` — when the loop is live —
    signals route to the graceful callback on that loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_shutdown: Callable[[], None] | None = None

    def attach(
        self,
        loop: asyncio.AbstractEventLoop,
        on_shutdown: Callable[[], None],
    ) -> None:
        with self._lock:
            self._loop = loop
            self._on_shutdown = on_shutdown

    def dispatch(self) -> None:
        with self._lock:
            loop = self._loop
            on_shutdown = self._on_shutdown
        if loop is not None and on_shutdown is not None:
            loop.call_soon_threadsafe(on_shutdown)
            return
        # Startup window: no loop yet, nothing to unwind gracefully. The sender
        # was just logged; exit now so SIGTERM still stops a slow/hung engine
        # build rather than being swallowed by the process-wide signal block.
        # (logging flushes per-emit, so the sender line is already written.)
        logger.warning(
            "Shutdown signal arrived before the orchestrator was ready; exiting "
            "now — no graceful shutdown is possible yet, but a slow/hung startup "
            "stays stoppable with SIGTERM."
        )
        _force_exit(_STARTUP_SIGNAL_EXIT_CODE)


def _force_exit(code: int) -> None:
    """Terminate the whole process from a non-main thread (test seam)."""
    os._exit(code)


# Process-singleton consumer state. Signals are inherently process-global, and
# the entry point starts the consumer early in ``main`` (``begin_shutdown_watch``)
# but only learns the loop + graceful callback later in ``run``
# (``install_attributed_shutdown``) — so the routing target and the
# "is the watch thread started" flag live here rather than being threaded through.
_target = _GracefulTarget()
_watch_started = False
_watch_lock = threading.Lock()


def begin_shutdown_watch() -> bool:
    """Start the ``sigwaitinfo`` consumer EARLY, right after blocking signals.

    Closes the window that blocking opens: with SIGTERM/SIGINT blocked
    process-wide but no consumer running yet, a signal delivered during config
    load or ``build_orchestrator`` would stay pending — the engine couldn't be
    stopped without SIGKILL. Starting the consumer here means a startup-window
    signal is attributed and forces exit (see ``_GracefulTarget.dispatch``).

    Returns ``False`` on platforms using the asyncio fallback: there signals are
    never blocked, so the OS default still stops a hung startup, and the consumer
    is installed later in ``install_attributed_shutdown``.
    """
    if not supports_sender_attribution():
        return False
    _ensure_watch_thread()
    return True


def install_attributed_shutdown(
    *,
    loop: asyncio.AbstractEventLoop,
    on_shutdown: Callable[[], None],
) -> None:
    """Route SIGTERM/SIGINT to ``on_shutdown`` on ``loop``, naming the sender.

    On the ``sigwaitinfo`` path this attaches the loop + callback to the consumer
    that ``begin_shutdown_watch`` already started (and starts it if, for some
    reason, it was not) — so before this call a signal forces an attributed
    startup exit, and after it a signal runs ``on_shutdown`` gracefully on the
    loop. On platforms without ``sigwaitinfo`` it installs asyncio handlers with
    an honest "sender not reported" message. ``on_shutdown`` always runs on the
    event loop thread, so callers can touch loop state safely.
    """
    if supports_sender_attribution():
        _target.attach(loop, on_shutdown)
        _ensure_watch_thread()
    else:
        _install_asyncio_fallback(loop, on_shutdown)


def _ensure_watch_thread() -> None:
    """Start the single sigwait consumer thread, at most once per process."""
    global _watch_started
    with _watch_lock:
        if _watch_started:
            return
        _watch_started = True
    _start_sigwait_thread(_target)


def _start_sigwait_thread(target: _GracefulTarget) -> None:
    sigset = set(_SHUTDOWN_SIGNALS)

    def _watch() -> None:
        # Resolve dynamically (not at import): ``sigwaitinfo`` is present on POSIX
        # but absent on macOS/Windows, where this thread is never started. Looking
        # it up via ``getattr`` keeps the type checker from flagging the attribute
        # on stubs that omit it, and respects test monkeypatching of the symbol.
        sigwaitinfo = getattr(signal, "sigwaitinfo", None)
        assert sigwaitinfo is not None  # guaranteed by supports_sender_attribution()
        # Loop so a second signal still re-triggers shutdown (matching the old
        # handler, which fired on every signal) rather than being swallowed.
        while True:
            try:
                info = sigwaitinfo(sigset)
            except (InterruptedError, OSError):
                continue
            logger.warning(
                "%s",
                format_shutdown_signal_log(info.si_signo, info.si_pid, info.si_uid),
            )
            target.dispatch()

    threading.Thread(target=_watch, name="shutdown-signal-watch", daemon=True).start()


def _install_asyncio_fallback(
    loop: asyncio.AbstractEventLoop,
    on_shutdown: Callable[[], None],
) -> None:
    def make_handler(sig: signal.Signals) -> Callable[[], None]:
        def handle_signal() -> None:
            try:
                ppid = os.getppid()
            except OSError:
                ppid = -1
            logger.warning(
                "Received %s (signum=%d); this platform does not report the "
                "signal sender (no sigwaitinfo — e.g. macOS), so the source is "
                "unknown. My parent is pid=%d (cmd=%r) — that is NOT necessarily "
                "the sender. For an attributable stop, use POST /api/shutdown "
                "with a 'reason'.",
                sig.name, int(sig), ppid, describe_sender(ppid),
            )
            on_shutdown()
        return handle_signal

    for sig in _SHUTDOWN_SIGNALS:
        loop.add_signal_handler(sig, make_handler(sig))
