"""GitHub ref-backed adapter for issue claim management.

This implementation keeps the distributed coordination algorithm behind the
ClaimManager port. It uses one issue-specific Git ref as an atomic compare-and-
swap cell, with claim metadata stored in the referenced commit message.
"""

from __future__ import annotations

import dataclasses
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol as TypingProtocol

from ...domain.claim import (
    Claim,
    ClaimFetchError,
    ClaimResult,
    ClaimState,
)
from ...domain.lease_config import LeaseConfig
from ...domain.run_ledger import (
    RunLedger,
    RunLedgerDecodeError,
    RunLedgerOutcome,
    RunLedgerRequest,
    RunLedgerRequestKind,
    format_run_ledger,
    parse_run_ledger,
    resolve,
)
from ...infra import gh_audit
from ...ports.claim_manager import ClaimManager
from .claim_parser import format_claim_comment, parse_claim_comment
from .errors import GitHubHttpError

if TYPE_CHECKING:
    from ...ports.event_sink import EventSink
    from .http_client import GitHubHttpClient

logger = logging.getLogger(__name__)

CLAIM_REF_PREFIX = "refs/issue-orchestrator/claims"
CLAIM_COMMIT_PREFIX = "issue-orchestrator claim lock"
MAX_CAS_ATTEMPTS = 3


@dataclass(frozen=True)
class _ClaimRefSnapshot:
    """Current state of a claim ref.

    ``message`` is the raw commit message the ref points at. The store owns the
    compare-and-swap; the ADAPTER owns the record format, so the same CAS cell
    backs both key spaces (``issue-N`` and ``run-<slug>``) without the storage
    helper needing to know what a claim record means.
    """

    ref: str
    commit_sha: str
    tree_sha: str
    message: str


class _GitRefClaimStore:
    """CAS-oriented storage helper for one-record-per-ref GitHub refs.

    Keyed by an opaque ref key, not by issue number: an issue claim uses
    ``issue-42`` and the tech-lead run ledger uses ``tech-lead-runs``, which is
    what lets a whole-repository run be coordinated before any issue exists.
    """

    def __init__(
        self,
        client: "GitHubHttpClient",
        ref_prefix: str = CLAIM_REF_PREFIX,
    ) -> None:
        self._client = client
        self._ref_prefix = ref_prefix.rstrip("/")
        self._default_branch: str | None = None

    def claim_ref(self, key: str) -> str:
        return f"{self._ref_prefix}/{key}"

    def _default_branch_name(self) -> str:
        if self._default_branch is None:
            self._default_branch = self._client.get_default_branch()
        return self._default_branch

    def read(self, key: str) -> _ClaimRefSnapshot | None:
        ref = self.claim_ref(key)
        ref_payload = self._client.get_git_ref(ref)
        if ref_payload is None:
            return None

        commit_sha = _payload_commit_sha(ref_payload)
        commit_payload = self._client.get_git_commit(commit_sha)
        tree_sha = _payload_tree_sha(commit_payload)
        return _ClaimRefSnapshot(
            ref=ref,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            message=str(commit_payload.get("message") or ""),
        )

    def create(self, key: str, message: str) -> bool:
        default_branch = self._default_branch_name()
        base_ref = self._client.get_git_ref(f"refs/heads/{default_branch}")
        if base_ref is None:
            raise ClaimFetchError(
                f"Default branch ref refs/heads/{default_branch} was not found"
            )

        base_sha = _payload_commit_sha(base_ref)
        base_commit = self._client.get_git_commit(base_sha)
        base_tree_sha = _payload_tree_sha(base_commit)
        commit = self._client.create_git_commit(
            message=message,
            tree_sha=base_tree_sha,
            parents=[base_sha],
        )
        try:
            self._client.create_git_ref(
                ref=self.claim_ref(key),
                sha=_payload_sha(commit),
            )
            return True
        except GitHubHttpError as exc:
            if _is_ref_conflict(exc):
                return False
            raise

    def update(self, snapshot: _ClaimRefSnapshot, message: str) -> bool:
        commit = self._client.create_git_commit(
            message=message,
            tree_sha=snapshot.tree_sha,
            parents=[snapshot.commit_sha],
        )
        try:
            self._client.update_git_ref(
                ref=snapshot.ref,
                sha=_payload_sha(commit),
                force=False,
            )
            return True
        except GitHubHttpError as exc:
            if _is_ref_conflict(exc):
                return False
            raise

    def delete(self, snapshot: _ClaimRefSnapshot) -> bool:
        try:
            self._client.delete_git_ref(snapshot.ref)
            return True
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return True
            raise


def _issue_ref_key(issue_number: int) -> str:
    """Ref key for the ISSUE claim key space."""
    return f"issue-{issue_number}"


# The ONE ref that carries every live tech-lead run in the repository. A single
# cell, not one per run: the invariant being protected is a relationship BETWEEN
# runs, and independent per-key cells cannot decide a relationship atomically
# (#6994 round 2 F1/A1).
RUN_LEDGER_REF_KEY = "tech-lead-runs"


class GitHubRefRunLedgerAdapter:
    """:class:`TechLeadRunLedgerStore` over one ref compare-and-swap cell (#6994).

    The whole repository's tech-lead run ledger lives at
    ``refs/issue-orchestrator/claims/tech-lead-runs``. Reusing the issue claim's
    CAS mechanism (rather than inventing a second coordination primitive) is the
    point: one algorithm, one failure mode.

    The adapter owns exactly two things — the record format and the atomic
    read-decide-write cycle. It decides NO policy: the conflict matrix is
    :func:`...domain.run_ledger.resolve`, the same pure function the
    single-instance store evaluates, so the rule cannot differ by deployment.

    Every method absorbs transport failure into a typed ``UNAVAILABLE`` answer
    rather than raising, because this store's callers are admission and launch
    decisions: they must be able to tell "a peer owns it" from "we held it and
    lost it" from "we cannot tell", and the port's contract is that those three
    are always distinguishable.
    """

    def __init__(
        self,
        client: "GitHubHttpClient",
        claimant_id: str,
        config: LeaseConfig | None = None,
        ref_prefix: str = CLAIM_REF_PREFIX,
    ) -> None:
        self._claimant_id = claimant_id
        self._config = config or LeaseConfig()
        self._store = _GitRefClaimStore(client=client, ref_prefix=ref_prefix)

    def submit(self, request: "RunLedgerRequest") -> "RunLedgerOutcome":
        """One atomic read-decide-write against the shared ledger.

        A ledger this build cannot decode EXACTLY is treated as unreachable:
        the request is refused and NOTHING is written. Reading a partial ledger
        would make the repository look free — the one answer an exclusivity
        store must never invent (#6994 round 2 F7).
        """
        request = self._with_minted_lease(request)
        try:
            for _ in range(MAX_CAS_ATTEMPTS):
                snapshot = self._store.read(RUN_LEDGER_REF_KEY)
                resolution = resolve(
                    _ledger_of(snapshot),
                    request,
                    claimant=self._claimant_id,
                    now=datetime.now(),
                    lease_seconds=self._config.lease_seconds,
                )
                if resolution.ledger is None:
                    # A refusal needs no write, so a contended request never
                    # burns a GitHub write on the shared cell.
                    return resolution.outcome
                if self._commit(snapshot, resolution.ledger):
                    logger.info(
                        "Run ledger %s %s: run=%s claimant=%s",
                        request.kind.value,
                        resolution.outcome.status.value,
                        request.run_key,
                        self._claimant_id,
                    )
                    return resolution.outcome
                # Lost the CAS to a concurrent writer: re-read and re-decide
                # rather than assuming either outcome.
            return RunLedgerOutcome.unavailable(
                request.run_key,
                "the tech-lead run ledger kept changing during the update",
            )
        except RunLedgerDecodeError as exc:
            logger.error(
                "Refusing to %s run %s: the shared tech-lead run ledger is"
                " unreadable by this build (%s). No write was made.",
                request.kind.value,
                request.run_key,
                exc,
            )
            return RunLedgerOutcome.unavailable(
                request.run_key,
                f"the shared tech-lead run ledger could not be decoded: {exc}",
            )
        except Exception as exc:
            logger.warning(
                "Failed to %s run %s in the shared ledger: %s",
                request.kind.value,
                request.run_key,
                exc,
            )
            return RunLedgerOutcome.unavailable(request.run_key, str(exc))

    def read(self) -> "RunLedger | None":
        """The live ledger, or None when it is unreachable OR undecodable."""
        try:
            return _ledger_of(self._store.read(RUN_LEDGER_REF_KEY))
        except RunLedgerDecodeError as exc:
            logger.error("The shared tech-lead run ledger is unreadable: %s", exc)
            return None
        except Exception as exc:
            logger.warning("Failed to read the tech-lead run ledger: %s", exc)
            return None

    def _with_minted_lease(self, request: "RunLedgerRequest") -> "RunLedgerRequest":
        """Give a reservation a globally unique lease id.

        Minted by the ADAPTER because uniqueness is a property of the shared
        store's namespace, not of the control-layer owner that asks.
        """
        if request.kind is not RunLedgerRequestKind.RESERVE or request.lease_id:
            return request
        priority = int(datetime.now().timestamp() * 1000)
        return dataclasses.replace(
            request, lease_id=f"{uuid.uuid4().hex[:12]}-{priority}"
        )

    def _commit(
        self, snapshot: _ClaimRefSnapshot | None, ledger: "RunLedger"
    ) -> bool:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_WRITE,
            issue_key=RUN_LEDGER_REF_KEY,
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            message = _format_run_ledger_commit_message(ledger)
            if snapshot is None:
                return self._store.create(RUN_LEDGER_REF_KEY, message)
            return self._store.update(snapshot, message)


def _ledger_of(snapshot: _ClaimRefSnapshot | None) -> "RunLedger":
    """The ledger a ref snapshot carries; an absent ref is an empty ledger."""
    if snapshot is None:
        return RunLedger()
    return parse_run_ledger(snapshot.message)


def _format_run_ledger_commit_message(ledger: "RunLedger") -> str:
    return f"{CLAIM_COMMIT_PREFIX} tech-lead runs\n\n{format_run_ledger(ledger)}\n"


class GitHubRefClaimAdapter(ClaimManager):
    """GitHub ClaimManager implementation using Git ref compare-and-swap.

    An active claim is represented by the current commit at
    ``refs/issue-orchestrator/claims/issue-N``. Updates are non-force ref
    moves to a commit whose parent is the previously-read ref tip, so GitHub's
    fast-forward check acts as the compare-and-swap guard.

    Release deletes the owned claim ref after a fresh read confirms the ref
    still points at the releasing lease. The ``io:claimed`` label is advisory:
    if label updates fail around release, readers that need ownership must
    consult the ref rather than the label alone.
    """

    def __init__(
        self,
        client: "GitHubHttpClient",
        claimant_id: str,
        config: LeaseConfig | None = None,
        events: "EventSink | None" = None,
        label_adapter: "LabelSetProtocol | None" = None,
        io_claimed_label: str = "io:claimed",
        ref_prefix: str = CLAIM_REF_PREFIX,
    ) -> None:
        self._client = client
        self._claimant_id = claimant_id
        self._config = config or LeaseConfig()
        self._events = events
        self._labels = label_adapter
        self._io_claimed_label = io_claimed_label
        self._store = _GitRefClaimStore(client=client, ref_prefix=ref_prefix)

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        """Atomically attempt to acquire the issue claim ref."""
        now = datetime.now()
        priority = int(now.timestamp() * 1000)
        lease_id = f"{uuid.uuid4().hex[:12]}-{priority}"
        claim = self._build_claim(
            issue_number=issue_number,
            lease_id=lease_id,
            started_at=now,
            priority=priority,
        )

        try:
            for _ in range(MAX_CAS_ATTEMPTS):
                snapshot = self._store.read(_issue_ref_key(issue_number))
                current_claim = self._active_claim(snapshot, issue_number)
                if current_claim is not None:
                    return self._lost_result(issue_number, lease_id, current_claim)

                with gh_audit.context(
                    reason=gh_audit.AuditReason.GH_WRITE,
                    issue_key=str(issue_number),
                    scope=gh_audit.AuditScope.UNKNOWN,
                ):
                    message = _format_claim_commit_message(claim)
                    acquired = (
                        self._store.create(_issue_ref_key(issue_number), message)
                        if snapshot is None
                        else self._store.update(snapshot, message)
                    )
                if not acquired:
                    continue

                try:
                    self._add_claim_label(issue_number)
                except Exception as exc:
                    self._release_owned_claim(issue_number, lease_id, remove_label=False)
                    return ClaimResult.failed(
                        f"Acquired claim but failed to label issue #{issue_number}: {exc}"
                    )

                self._emit_event("CLAIM_ATTEMPTED", {
                    "issue_number": issue_number,
                    "lease_id": lease_id,
                    "claimant": self._claimant_id,
                    "priority": priority,
                    "storage": "git_ref",
                })
                logger.info(
                    "Acquired GitHub ref claim for issue #%d: lease_id=%s, claimant=%s",
                    issue_number,
                    lease_id,
                    self._claimant_id,
                )
                return ClaimResult.claimed(lease_id)

            return ClaimResult.failed(
                f"GitHub claim ref changed during acquisition for issue #{issue_number}"
            )
        except ClaimFetchError as exc:
            logger.error("Failed to acquire claim for issue #%d: %s", issue_number, exc)
            return ClaimResult.failed(str(exc))
        except Exception as exc:
            logger.error("Failed to acquire claim for issue #%d: %s", issue_number, exc)
            return ClaimResult.failed(str(exc))

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        """Confirm ownership.

        Ref CAS makes acquisition atomic, so this is a fresh ownership check
        with bounded retries for transient GitHub read failures.
        """
        deadline = time.monotonic() + self._config.convergence_timeout_seconds
        max_polls = self._config.convergence_max_polls
        poll_count = 0

        while time.monotonic() < deadline and poll_count < max_polls:
            poll_count += 1
            try:
                winner = self.get_current_claim(issue_number)
            except ClaimFetchError as exc:
                logger.warning(
                    "Issue #%d: failed to confirm GitHub ref claim ownership: %s",
                    issue_number,
                    exc,
                )
                self._sleep_before_next_convergence_poll()
                continue

            if winner is not None and winner.lease_id == lease_id:
                self._emit_event("CLAIM_CONVERGED", {
                    "issue_number": issue_number,
                    "lease_id": lease_id,
                    "storage": "git_ref",
                })
                return True

            if winner is not None:
                self._emit_event("CLAIM_CONTESTED", {
                    "issue_number": issue_number,
                    "our_lease_id": lease_id,
                    "winner_lease_id": winner.lease_id,
                    "storage": "git_ref",
                })
                return False

            self._sleep_before_next_convergence_poll()

        self._emit_event("CLAIM_CONTESTED", {
            "issue_number": issue_number,
            "our_lease_id": lease_id,
            "storage": "git_ref",
        })
        return False

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        """Renew the claim if the ref still points at our lease."""
        snapshot = self._read_snapshot(issue_number)
        current_claim = self._active_claim(snapshot, issue_number)
        if current_claim is None or current_claim.lease_id != lease_id:
            logger.warning(
                "Cannot renew claim for issue #%d: not the current winner",
                issue_number,
            )
            return False

        renewed_claim = self._build_claim(
            issue_number=issue_number,
            lease_id=lease_id,
            started_at=current_claim.started_at,
            priority=current_claim.priority,
        )

        assert snapshot is not None
        try:
            if not self._store.update(
                snapshot, _format_claim_commit_message(renewed_claim)
            ):
                return False
        except Exception as exc:
            raise ClaimFetchError(
                f"Failed to renew GitHub ref claim for issue #{issue_number}: {exc}"
            ) from exc

        self._emit_event("CLAIM_RENEWED", {
            "issue_number": issue_number,
            "lease_id": lease_id,
            "storage": "git_ref",
        })
        logger.info("Renewed GitHub ref claim for issue #%d, lease_id=%s", issue_number, lease_id)
        return True

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        """Release the claim by deleting the owned issue claim ref."""
        self._release_owned_claim(issue_number, lease_id, remove_label=True)

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        """Check whether the current active ref claim is our lease."""
        winner = self.get_current_claim(issue_number)
        return winner is not None and winner.lease_id == lease_id

    def get_current_claim(self, issue_number: int) -> Claim | None:
        """Read the current active claim from the issue claim ref."""
        snapshot = self._read_snapshot(issue_number)
        return self._active_claim(snapshot, issue_number)

    def _read_snapshot(self, issue_number: int) -> _ClaimRefSnapshot | None:
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                return self._store.read(_issue_ref_key(issue_number))
        except ClaimFetchError:
            raise
        except Exception as exc:
            raise ClaimFetchError(
                f"Failed to fetch GitHub ref claim for issue #{issue_number}: {exc}"
            ) from exc

    def _active_claim(
        self, snapshot: _ClaimRefSnapshot | None, issue_number: int
    ) -> Claim | None:
        if snapshot is None:
            return None
        claim = parse_claim_comment(snapshot.message, issue_number=issue_number)
        if claim is None or claim.is_expired(datetime.now()):
            return None
        return claim

    def _build_claim(
        self,
        *,
        issue_number: int,
        lease_id: str,
        started_at: datetime,
        priority: int,
    ) -> Claim:
        now = datetime.now()
        return Claim(
            lease_id=lease_id,
            claimant=self._claimant_id,
            issue_number=issue_number,
            started_at=started_at,
            expires_at=now + timedelta(seconds=self._config.lease_seconds),
            priority=priority,
        )

    def _release_owned_claim(
        self,
        issue_number: int,
        lease_id: str,
        *,
        remove_label: bool,
    ) -> None:
        try:
            snapshot = self._read_snapshot(issue_number)
            held = (
                None
                if snapshot is None
                else parse_claim_comment(snapshot.message, issue_number=issue_number)
            )
            if snapshot is None or held is None:
                if remove_label:
                    self._remove_claim_label(issue_number)
                return
            if held.lease_id != lease_id:
                return

            label_error: Exception | None = None
            if remove_label:
                try:
                    self._remove_claim_label(issue_number)
                except Exception as exc:
                    label_error = exc

            released = self._store.delete(snapshot)
            if not released:
                return

            self._emit_event("CLAIM_RELEASED", {
                "issue_number": issue_number,
                "lease_id": lease_id,
                "storage": "git_ref",
            })
            logger.info("Released GitHub ref claim for issue #%d, lease_id=%s", issue_number, lease_id)
            if label_error is not None:
                logger.warning(
                    "Released GitHub ref claim for issue #%d but failed to remove %s label: %s",
                    issue_number,
                    self._io_claimed_label,
                    label_error,
                )
        except Exception as exc:
            logger.error("Failed to release claim for issue #%d: %s", issue_number, exc)

    def _add_claim_label(self, issue_number: int) -> None:
        if self._labels:
            self._labels.add_label(issue_number, self._io_claimed_label)

    def _remove_claim_label(self, issue_number: int) -> None:
        if self._labels:
            self._labels.remove_label(issue_number, self._io_claimed_label)

    def _lost_result(
        self,
        issue_number: int,
        lease_id: str,
        winner: Claim,
    ) -> ClaimResult:
        return ClaimResult(
            success=False,
            lease_id=lease_id,
            state=ClaimState.CLAIM_LOST,
            competing_claims=[winner],
            error=(
                f"Issue #{issue_number} is already claimed by {winner.claimant} "
                f"until {winner.expires_at.isoformat()}"
            ),
        )

    def _sleep_before_next_convergence_poll(self) -> None:
        jitter_ms = random.randint(
            self._config.convergence_poll_min_ms,
            self._config.convergence_poll_max_ms,
        )
        time.sleep(jitter_ms / 1000)

    def _emit_event(self, event_name: str, data: dict) -> None:
        if not self._events:
            return

        try:
            from ...events.catalog import EventName
            from ...ports.event_sink import TraceEvent

            full_name = (
                f"CLAIM_{event_name}"
                if not event_name.startswith("CLAIM_")
                else event_name
            )
            event_enum = getattr(EventName, full_name, None)
            if event_enum:
                self._events.publish(TraceEvent(event_enum, data))
        except Exception as exc:
            logger.debug("Failed to emit event %s: %s", event_name, exc)


def _format_claim_commit_message(claim: Claim) -> str:
    return f"{CLAIM_COMMIT_PREFIX}\n\n{format_claim_comment(claim)}"


def _payload_sha(payload: dict) -> str:
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ClaimFetchError(f"GitHub payload missing sha: {payload}")
    return sha


def _payload_commit_sha(payload: dict) -> str:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise ClaimFetchError(f"GitHub ref payload missing object: {payload}")
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ClaimFetchError(f"GitHub ref payload missing object.sha: {payload}")
    return sha


def _payload_tree_sha(payload: dict) -> str:
    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise ClaimFetchError(f"GitHub commit payload missing tree: {payload}")
    sha = tree.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ClaimFetchError(f"GitHub commit payload missing tree.sha: {payload}")
    return sha


def _is_ref_conflict(exc: GitHubHttpError) -> bool:
    return exc.status_code in {409, 422}


class LabelSetProtocol(TypingProtocol):
    """Protocol for label operations (matches LabelSet port)."""

    def add_label(self, issue_number: int, label: str) -> None:
        ...

    def remove_label(self, issue_number: int, label: str) -> None:
        ...
