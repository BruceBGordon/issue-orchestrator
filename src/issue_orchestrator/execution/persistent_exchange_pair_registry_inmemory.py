"""In-memory adapter for ``PersistentExchangePairRegistry``.

Owned by the composition root (one instance per orchestrator process)
and shared across all review-exchange invocations. Tear-down is wired
into orchestrator shutdown so no PTY-attached agent processes leak
when the orchestrator stops.

This module also defines :class:`PersistentExchangePair`, the value
type the adapter caches and returns from :meth:`acquire`. The
dataclass and ``acquire`` live in execution/ on purpose: they
reference :class:`PersistentSession` (a subprocess-backed handle),
which the port boundary forbids exposing. Control-layer callers
that only need the ``release`` / ``shutdown_all`` lifecycle verbs
depend on the narrow port; execution-layer callers that need
``acquire`` import this concrete adapter directly.

ADR 0026 documents the design and migration plan.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from ..ports.persistent_exchange_pair_registry import PersistentExchangePairRegistry
from .exchange_kill_evidence import ExchangeKillEvidenceRecorder
from .persistent_round_runner import PersistentSession, close_persistent_session

logger = logging.getLogger(__name__)


@dataclass
class PersistentExchangePair:
    """One issue's coder + reviewer subprocess pair plus its reviewer worktree.

    Lives in execution/ so the dataclass can name
    :class:`PersistentSession` directly without violating the port
    boundary. Returned by :meth:`InMemoryPersistentExchangePairRegistry.acquire`
    to execution-layer callers.

    Pair write paths are pair-lifetime scoped. Agent-written response/report
    paths live inside the role worktrees so sandboxed agents can write them;
    recordings and coder completion paths live under the persistent pair root.
    ``exchange_run_id`` and ``run_dir`` record the typed exchange run whose
    environment spawned the role processes. The pair contract owner releases,
    rather than rebinds, a cached pair from any other exchange run.

    ``created_at`` is wall-clock seconds since epoch, set once at
    construction. ``last_used_at`` is updated on every cache hit so
    future diagnostics / idle-reaping can see when the pair was
    last touched. The dataclass is mutable so the registry can
    update ``last_used_at`` in place.

    The registry owns cache membership and pair teardown via
    ``release`` / ``shutdown_all``. Once a pair is acquired for a
    single exchange, the execution-layer role-session owner may
    replace ``coder_session`` or ``reviewer_session`` after a role
    process exits following a valid turn. That owner preserves the
    pair-scoped worktree/path contract while swapping the active
    subprocess handle. Registry snapshots are diagnostics-only and
    may observe either side of that atomic reference swap during the
    handoff.
    """

    coder_session: PersistentSession
    reviewer_session: PersistentSession
    reviewer_worktree_path: Path
    issue_key: Hashable
    exchange_run_id: str
    run_dir: Path
    created_at: float
    coder_response_path: Path
    reviewer_response_path: Path
    reviewer_report_path: Path
    coder_recording_path: Path
    reviewer_recording_path: Path
    coder_completion_path: Path
    validation_record_path: Path
    last_used_at: float = field(default=0.0)


def _pair_is_alive(pair: PersistentExchangePair) -> bool:
    """Both role sessions can still accept another round prompt."""
    return pair.coder_session.is_live and pair.reviewer_session.is_live


class InMemoryPersistentExchangePairRegistry(PersistentExchangePairRegistry):
    """Thread-safe issue-keyed cache of live coder/reviewer pairs.

    Concurrent acquires for *different* keys are allowed; concurrent
    acquires for the *same* key are serialized through the registry's
    lock so a slow ``spawn`` callable can't be invoked twice.

    The implementation deliberately does not own the reviewer
    worktree's filesystem layout — ``release`` invokes the
    caller-supplied ``on_release`` hook (registered at construction)
    so worktree removal can call into the existing reviewer-worktree
    helpers without this module growing a dependency on them.

    ``acquire`` is execution-only: callers that need it import this
    concrete class instead of the narrow port. Control-layer callers
    only see ``release`` / ``shutdown_all`` via the port.
    """

    def __init__(
        self,
        *,
        on_release: Callable[[PersistentExchangePair, str], None] | None = None,
        clock: Callable[[], float] = time.time,
        kill_evidence: ExchangeKillEvidenceRecorder | None = None,
    ) -> None:
        self._cache: dict[Hashable, PersistentExchangePair] = {}
        self._lock = RLock()
        self._on_release = on_release
        self._clock = clock
        # This registry destroys the evidence — closing the sessions ends the
        # recording and the release hook reclaims the reviewer worktree — so it
        # owns the recorder that has to run first. Owning it here also makes
        # "one recorder shared by the round loop and the teardown" structural:
        # the exchange runner reads it back off the registry it already holds,
        # so the two halves cannot end up wired to different instances.
        self._kill_evidence = kill_evidence or ExchangeKillEvidenceRecorder()

    @property
    def kill_evidence(self) -> ExchangeKillEvidenceRecorder:
        """The kill-evidence owner for pairs this registry manages."""
        return self._kill_evidence

    def acquire(
        self,
        *,
        issue_key: Hashable,
        spawn: Callable[[], PersistentExchangePair],
    ) -> PersistentExchangePair:
        """Return a live pair for ``issue_key``.

        On cache hit (and pair still alive): returns the cached pair
        with ``last_used_at`` refreshed.

        On cache miss or evicted-dead-pair: invokes ``spawn`` to
        build a new pair, caches it, and returns it.

        ``spawn`` is supplied by the caller because *how* to spawn
        (which command, env, recording paths) is a policy the
        registry deliberately does not own — the registry only owns
        *when* and *for how long*. ``spawn`` is invoked at most
        once per acquire.
        """
        with self._lock:
            existing = self._cache.get(issue_key)
            if existing is not None:
                if _pair_is_alive(existing):
                    existing.last_used_at = self._clock()
                    logger.debug(
                        "[exchange-pair-registry] cache hit issue_key=%s "
                        "coder_pid=%d reviewer_pid=%d age=%.1fs",
                        issue_key,
                        existing.coder_session.proc.pid,
                        existing.reviewer_session.proc.pid,
                        self._clock() - existing.created_at,
                    )
                    return existing
                logger.warning(
                    "[exchange-pair-registry] cached pair for issue_key=%s is "
                    "dead (coder_alive=%s reviewer_alive=%s); evicting and respawning",
                    issue_key,
                    existing.coder_session.is_live,
                    existing.reviewer_session.is_live,
                )
                self._tear_down(existing, reason="dead-on-acquire")
                del self._cache[issue_key]

            pair = spawn()
            pair.last_used_at = self._clock()
            self._cache[issue_key] = pair
            logger.info(
                "[exchange-pair-registry] spawned issue_key=%s "
                "coder_pid=%d reviewer_pid=%d reviewer_worktree=%s",
                issue_key,
                pair.coder_session.proc.pid,
                pair.reviewer_session.proc.pid,
                pair.reviewer_worktree_path,
            )
            return pair

    def release(self, issue_key: Hashable, *, reason: str) -> None:
        with self._lock:
            pair = self._cache.pop(issue_key, None)
            if pair is None:
                return
            self._tear_down(pair, reason=reason)

    def shutdown_all(self, *, reason: str) -> None:
        with self._lock:
            keys = list(self._cache.keys())
            logger.info(
                "[exchange-pair-registry] shutdown_all reason=%s "
                "logical_pairs=%d logical_agents=%d issue_keys=%s",
                reason,
                len(keys),
                len(keys) * 2,
                ",".join(str(key) for key in keys) if keys else "none",
            )
            for issue_key in keys:
                pair = self._cache.pop(issue_key)
                self._tear_down(pair, reason=reason)

    def has_active_pair(self, issue_key: Hashable) -> bool:
        """Return True while a pair is cached for ``issue_key``.

        Reads the exact cache membership ``release`` pops, so a lifecycle
        boundary's activity check never diverges from what a release would tear
        down. Liveness is deliberately not re-probed here: a cached-but-dead pair
        is still a resource ``release`` would reclaim, so treating it as active
        keeps the boundary fail-safe.
        """
        with self._lock:
            return issue_key in self._cache

    def _tear_down(self, pair: PersistentExchangePair, *, reason: str) -> None:
        """Close subprocesses and notify the worktree-removal hook.

        Errors during close are logged but never re-raised — release
        runs in ``finally`` blocks where a raise would mask the
        original exception. Errors are still surfaced in the log so
        leaks don't go unnoticed.
        """
        # BEFORE anything is closed: a round still in flight here is a wedged
        # worker being killed out from under itself (supervisor wall-clock
        # deadline, operator cancel, orchestrator shutdown). Its recording dies
        # with the sessions below, so this is the last moment that evidence
        # exists (#7141 finding 2). Never raises; see the recorder's docstring.
        self._kill_evidence.capture_abandoned_rounds(pair.issue_key, reason=reason)
        coder_pid = pair.coder_session.proc.pid
        reviewer_pid = pair.reviewer_session.proc.pid
        logger.info(
            "[exchange-pair-registry] releasing issue_key=%s reason=%s "
            "coder_pid=%d reviewer_pid=%d age=%.1fs",
            pair.issue_key,
            reason,
            coder_pid,
            reviewer_pid,
            self._clock() - pair.created_at,
        )
        exit_codes: dict[str, int | None] = {}
        for session_label, session in (
            ("reviewer", pair.reviewer_session),
            ("coder", pair.coder_session),
        ):
            try:
                exit_codes[session_label] = close_persistent_session(session)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                logger.exception(
                    "[exchange-pair-registry] %s session close raised "
                    "issue_key=%s pid=%d reason=%s",
                    session_label,
                    pair.issue_key,
                    session.proc.pid,
                    reason,
                )
                exit_codes[session_label] = None

        logger.info(
            "[exchange-pair-registry] released issue_key=%s reason=%s "
            "coder_pid=%d coder_exit=%s reviewer_pid=%d reviewer_exit=%s",
            pair.issue_key,
            reason,
            coder_pid,
            exit_codes.get("coder"),
            reviewer_pid,
            exit_codes.get("reviewer"),
        )

        if self._on_release is not None:
            try:
                self._on_release(pair, reason)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                logger.exception(
                    "[exchange-pair-registry] on_release hook raised "
                    "issue_key=%s reason=%s reviewer_worktree=%s",
                    pair.issue_key,
                    reason,
                    pair.reviewer_worktree_path,
                )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a debug snapshot of cached pairs (for diagnostics).

        Used by future control-API endpoints (B3); returned dicts are
        plain JSON-serializable values so the caller can render them
        without importing this module.
        """
        with self._lock:
            now = self._clock()
            return [
                {
                    "issue_key": str(pair.issue_key),
                    "coder_pid": pair.coder_session.proc.pid,
                    "reviewer_pid": pair.reviewer_session.proc.pid,
                    "reviewer_worktree": str(pair.reviewer_worktree_path),
                    "alive": _pair_is_alive(pair),
                    "age_seconds": now - pair.created_at,
                    "idle_seconds": now - pair.last_used_at,
                }
                for pair in self._cache.values()
            ]
