"""HTTP GitHub client for orchestrator operations (sync httpx)."""

from __future__ import annotations

import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, Iterator, Literal, cast
from urllib.parse import quote

import httpx

from ...events import EventName
from ...infra import gh_audit
from ... import __version__
from .auth import (
    GitHubAppInstallationTokenProvider,
    GitHubAuth,
    build_github_auth,
    build_github_token_provider,
)
from .errors import GitHubAuthError, GitHubHttpError, GitHubTransportError
from .tokens import (
    KEYRING_SERVICE,
    KEYRING_USERNAME,
    GitHubTokenProvider,
    StaticGitHubTokenProvider,
    TokenValidationResult,
    clear_keyring_token,
    describe_github_token_sources,
    resolve_github_token,
    store_keyring_token,
)

logger = logging.getLogger(__name__)

# Operational backstop for the comment-marker pagination loop. At 100 comments
# per page this covers 2,000 comments; hitting it means GitHub never returned a
# final (short/empty) page, so the scan fails loud rather than report "absent"
# from a truncated read.
_MARKER_SCAN_PAGE_CAP = 20


# Completed check-run conclusions that GitHub treats as acceptable for a
# required status check: only `success`, `skipped`, and `neutral` clear merge.
# Everything else completed — `failure`, `timed_out`, `action_required`,
# `startup_failure`, and notably `cancelled`/`stale` — is non-passing and must
# block, so the REST fallback cannot report it as SUCCESS. Modeling the PASS set
# (rather than a FAIL set) is fail-safe: any unknown/new conclusion is treated
# as non-passing. We only read the PR head commit, so `cancelled`/`stale` here
# mean the head's own required check did not pass, not a superseded commit.
# See https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
_PASSING_CHECK_CONCLUSIONS: frozenset[str] = frozenset(
    {"success", "skipped", "neutral"}
)

# A check-state contribution from one source: (failure, pending, present).
# `present` distinguishes "no checks at all" (→ rollup None) from "all green".
_RollupSignal = tuple[bool, bool, bool]

# Upper bound on check-run pages walked per commit. GitHub returns 100 runs per
# page; a head commit with >2000 check runs is implausible, so this is purely a
# runaway-loop backstop, not an expected limit.
_MAX_CHECK_RUN_PAGES = 20

# Why one REST rollup source did (not) yield a trustworthy reading. ``ok`` =
# read in full; ``permission_denied`` = the token lacks the read scope;
# ``transient_error`` = a retryable GitHub blip (5xx / rate-limit / timeout);
# ``capped`` = the source was readable but truncated at the pagination safety
# cap, so later pages are unread. Distinct causes so the gate that owns
# permission backoff can apply the right policy (retry vs escalate).
_SourceStatus = Literal["ok", "permission_denied", "transient_error", "capped"]

# Aggregate read capability for a commit's rollup, mirroring the port's
# ``StatusCheckRollupCapability`` (kept local to avoid an adapter→port import).
_RollupCapability = Literal["ok", "permission_denied", "transient_error"]


@dataclass(frozen=True)
class CommitCheckRollup:
    """REST-fallback rollup for a commit SHA, with a typed read capability.

    ``state`` is the aggregate ``StatusState``-like rollup (``FAILURE`` /
    ``PENDING`` / ``SUCCESS`` / ``None`` when the commit has no checks).

    ``capability`` carries WHY the read is (un)trustworthy so the caller does
    not collapse distinct facts:

    - ``ok``: ``state`` is trustworthy — either every source was read in full,
      or a readable source reported ``FAILURE`` (the top of the rollup
      precedence ``FAILURE`` > ``PENDING`` > ``SUCCESS``/none, which no unread
      source can outrank).
    - ``permission_denied``: a source could not be read for lack of scope and no
      readable ``FAILURE`` overrides it — a persistent operator problem.
    - ``transient_error``: a source failed with a retryable blip (5xx /
      rate-limit / timeout) OR was truncated at the pagination cap, and no
      readable ``FAILURE`` overrides it — safe to retry next tick and, crucially,
      must NOT arm the repo-wide permission backoff.

    When ``capability`` is not ``ok`` the caller must NOT treat ``state`` as
    truthful: an unread source could hold a failed required check/status that
    outranks the non-failure aggregate that was read.
    """

    state: str | None
    capability: _RollupCapability

    @property
    def complete(self) -> bool:
        """Whether ``state`` is trustworthy (every source read, or a readable
        FAILURE). Convenience over ``capability == "ok"``."""
        return self.capability == "ok"


@dataclass(frozen=True)
class _SourceReadout:
    """One REST rollup source's contribution: its signal plus a typed outcome.

    ``outcome`` is the typed read result (see ``_SourceStatus``). Anything other
    than ``ok`` means the (possibly partial) ``signal`` must NOT be mistaken for
    "this source found nothing"; the caller folds the outcome into
    ``CommitCheckRollup.capability``.
    """

    signal: _RollupSignal
    outcome: _SourceStatus


@dataclass(frozen=True)
class _CommitStatusReadout:
    """Outcome of a best-effort legacy combined-status read for one commit."""

    payload: dict[str, Any] | None
    outcome: _SourceStatus


def _aggregate_check_runs(payload: object) -> _RollupSignal:
    """Reduce a REST `/check-runs` response to a `_RollupSignal`."""
    failure = pending = present = False
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        present = True
        if run.get("status") != "completed":
            pending = True
        elif str(run.get("conclusion") or "").lower() not in _PASSING_CHECK_CONCLUSIONS:
            # Completed but not success/skipped/neutral (incl. cancelled/stale)
            # → non-passing, so it blocks merge like any other failure.
            failure = True
    return failure, pending, present


def _aggregate_combined_status(payload: object) -> _RollupSignal:
    """Reduce a REST `/commits/{sha}/status` response to a `_RollupSignal`.

    A commit with zero statuses reports ``state="pending"``, so only fold this
    in when there are real statuses — otherwise it would mask a clean check-run
    result as pending.
    """
    if not isinstance(payload, dict) or not (payload.get("statuses") or []):
        return False, False, False
    state = str(payload.get("state") or "").lower()
    return state in ("failure", "error"), state == "pending", True


# Markers in a GitHub error body that name a genuine missing-capability failure
# (as opposed to a retryable throttle/5xx). GitHub returns HTTP 403 for BOTH a
# missing scope and "secondary rate limit"/"API rate limit exceeded"; only the
# former names a scope/permission, so we sniff the body rather than the status.
_ROLLUP_PERMISSION_MARKERS: tuple[str, ...] = (
    "forbidden",
    "not accessible",
    "scope",  # "...has not been granted the required scopes..."
    "permission",
    "must have",  # "...must have read access..."
)


def classify_github_http_failure(exc: GitHubHttpError) -> _RollupCapability:
    """Tell a missing-permission rollup failure from a transient one.

    A 401 is an authentication failure: the token cannot identify itself at all,
    which is always an operator problem rather than a retryable blip.

    Every other status (403, 429, 5xx, GraphQL-200-with-errors) is decided by
    the response body, NOT the status code. A genuine missing-capability failure
    names the gap — ``forbidden`` / ``not accessible`` / a required
    ``scope``/``permission`` — so we sniff for those markers. GitHub also returns
    HTTP 403 for retryable throttling ("API rate limit exceeded", "secondary
    rate limit"); those bodies name no scope, so they fall through to
    ``transient_error`` and are retried next tick instead of arming the repo-wide
    permission backoff and escalating a bogus missing-scope error.
    """
    if exc.status_code == 401:
        return "permission_denied"
    haystack = f"{exc} {getattr(exc, 'response_text', '') or ''}".lower()
    if any(marker in haystack for marker in _ROLLUP_PERMISSION_MARKERS):
        return "permission_denied"
    return "transient_error"


def _aggregate_rollup_capability(*outcomes: _SourceStatus) -> _RollupCapability:
    """Fold per-source read outcomes into one rollup capability.

    Only reached for a NON-conclusive aggregate (no readable ``FAILURE``; a
    readable failure short-circuits to ``ok`` before this). Precedence is biased
    toward never falsely escalating a missing scope:

    - any ``transient_error`` wins → retry next tick (a real but unread
      permission gap will resurface once the transient clears);
    - else any ``permission_denied`` → escalate the genuine scope problem;
    - else any ``capped`` → ``transient_error`` (a truncated read is not a
      permission failure, so it must not arm the permission backoff);
    - else every source was read in full → ``ok``.
    """
    if "transient_error" in outcomes:
        return "transient_error"
    if "permission_denied" in outcomes:
        return "permission_denied"
    if "capped" in outcomes:
        return "transient_error"
    return "ok"


def _combine_rollup_signals(*signals: _RollupSignal) -> str | None:
    """Fold per-source signals into one `StatusState`-like rollup."""
    failure = any(sig[0] for sig in signals)
    pending = any(sig[1] for sig in signals)
    present = any(sig[2] for sig in signals)
    if failure:
        return "FAILURE"
    if pending:
        return "PENDING"
    return "SUCCESS" if present else None


@dataclass
class GitHubRateLimitSnapshot:
    core_remaining: int | None
    core_limit: int | None
    core_reset: int | None
    search_remaining: int | None
    search_limit: int | None
    search_reset: int | None
    graphql_remaining: int | None
    graphql_limit: int | None
    graphql_reset: int | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "core": {
                "remaining": self.core_remaining,
                "limit": self.core_limit,
                "reset": self.core_reset,
            },
            "search": {
                "remaining": self.search_remaining,
                "limit": self.search_limit,
                "reset": self.search_reset,
            },
            "graphql": {
                "remaining": self.graphql_remaining,
                "limit": self.graphql_limit,
                "reset": self.graphql_reset,
            },
        }


@dataclass
class GitHubHttpConfig:
    repo: str
    token: str | None = None
    base_url: str = "https://api.github.com"
    timeout_seconds: float = 20.0
    token_provider: GitHubTokenProvider | None = None
    auth: GitHubAuth | None = None


@dataclass
class _ETagEntry:
    etag: str
    payload: Any


def _truncate_one_line(text: str, max_len: int) -> str:
    snippet = text.strip().replace("\n", " ")
    return snippet[:max_len] + ("..." if len(snippet) > max_len else "")


def _format_error_entry(err: object) -> str:
    """Format a single entry from a GitHub `errors[]` array."""
    if isinstance(err, dict):
        bits = [
            str(err[k])
            for k in ("resource", "field", "code", "message")
            if err.get(k)
        ]
        return "/".join(bits)
    return str(err) if err else ""


def _format_error_details(errors: object) -> str:
    if not isinstance(errors, list):
        return ""
    chunks = [c for c in (_format_error_entry(e) for e in errors) if c]
    return "[" + "; ".join(chunks) + "]" if chunks else ""


def _summarize_github_error(response_text: str, max_len: int = 280) -> str:
    """Extract GitHub's `message` and `errors[]` codes from a response body.

    GitHub error bodies look like:
        {"message": "Validation Failed",
         "errors": [{"resource": "PullRequest", "code": "invalid", "field": "state"}],
         "documentation_url": "..."}

    Returns a compact one-line summary suitable for embedding in an exception
    message or surfacing in a UI toast. Falls back to a truncated raw body
    when JSON parsing fails or fields are absent.
    """
    if not response_text:
        return ""
    try:
        body = _json.loads(response_text)
    except (ValueError, TypeError):
        return _truncate_one_line(response_text, max_len)
    if not isinstance(body, dict):
        return str(body)[:max_len]
    parts = [
        str(body.get("message") or "").strip(),
        _format_error_details(body.get("errors")),
    ]
    summary = " ".join(p for p in parts if p)
    if not summary:
        return _truncate_one_line(response_text, max_len)
    return _truncate_one_line(summary, max_len)


def _extract_rate_limit_headers(response: httpx.Response) -> dict[str, Any] | None:
    """Extract X-RateLimit-* headers from GitHub response.

    Returns a dict with remaining, limit, used, reset if available.
    """
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    used = response.headers.get("X-RateLimit-Used")
    reset = response.headers.get("X-RateLimit-Reset")
    resource = response.headers.get("X-RateLimit-Resource")

    if remaining is None and limit is None:
        return None

    result: dict[str, Any] = {}
    if remaining is not None:
        result["remaining"] = int(remaining)
    if limit is not None:
        result["limit"] = int(limit)
    if used is not None:
        result["used"] = int(used)
    if reset is not None:
        result["reset"] = int(reset)
    if resource is not None:
        result["resource"] = resource
    return result if result else None


class GitHubHttpClient:
    """Minimal GitHub REST client for issue-orchestrator."""

    def __init__(self, config: GitHubHttpConfig) -> None:
        self._config = config
        self._etag_cache: dict[str, _ETagEntry] = {}
        if config.auth is not None:
            self._auth = config.auth
        elif config.token_provider is not None:
            self._token_provider = config.token_provider
            self._auth = GitHubAuth(
                token_provider=config.token_provider,
                source_descriptions=(),
                api_url=config.base_url,
                repo=config.repo,
            )
        elif config.token:
            self._token_provider = StaticGitHubTokenProvider(config.token)
            self._auth = GitHubAuth(
                token_provider=self._token_provider,
                source_descriptions=(),
                api_url=config.base_url,
                repo=config.repo,
            )
        else:
            raise GitHubAuthError("GitHub HTTP client requires a token provider.")
        self._token_provider = self._auth.token_provider
        self._config.auth = self._auth
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"issue-orchestrator/{__version__}",
        }
        self._client = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
        )

    @property
    def config(self) -> GitHubHttpConfig:
        """Expose config for use by adapters that need to create temporary clients."""
        return self._config

    @property
    def auth_kind(self) -> str:
        return self._auth.auth_kind

    def close(self) -> None:
        self._client.close()

    def _auth_headers(self) -> dict[str, str]:
        return self._auth.authorization_headers()

    def _cache_key(self, method: str, url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return f"{method} {url}"
        ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{method} {url}?{ordered}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        use_cache: bool = True,
        caller: str = "github_http",
    ) -> Any:
        url = path
        headers = self._auth_headers()
        cache_key = self._cache_key(method, url, params)
        if use_cache and method.upper() == "GET":
            cached = self._etag_cache.get(cache_key)
            if cached:
                headers["If-None-Match"] = cached.etag

        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code = None
        payload: Any = None
        was_304 = False
        rate_limit_info: dict[str, int] | None = None
        try:
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                error = f"transport_error: {exc}"
                raise GitHubTransportError(
                    f"GitHub transport error for {method} {url}",
                    method=method,
                    url=url,
                    original=exc,
                ) from exc
            status_code = response.status_code
            response_text = response.text
            # Extract rate limit headers from every response
            rate_limit_info = _extract_rate_limit_headers(response)
            if status_code == 304 and use_cache:
                cached = self._etag_cache.get(cache_key)
                if cached is not None:
                    payload = cached.payload
                    was_304 = True
                    return payload
            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                summary = _summarize_github_error(response_text)
                detail = f" — {summary}" if summary else ""
                raise GitHubHttpError(
                    (
                        f"GitHub {method.upper()} {path} failed: "
                        f"{status_code}{detail}"
                    ),
                    method=method,
                    url=str(response.url),
                    status_code=status_code,
                    response_text=response_text,
                )
            if response_text:
                payload = response.json()
            else:
                payload = {}
            if use_cache and method.upper() == "GET":
                etag = response.headers.get("ETag")
                if etag:
                    self._etag_cache[cache_key] = _ETagEntry(etag=etag, payload=payload)
            return payload
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            # 304 Not Modified: no data was transferred, so items_returned=0
            items_count = 0 if was_304 else _count_items(payload)
            gh_audit.record_live_call(
                command=f"{method.upper()} {url}",
                caller=caller,
                error=error,
                rate_limit=rate_limit_info,
            )
            gh_audit.record(
                args=[method.upper(), url],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller=caller,
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=items_count,
                full_scan=_is_full_scan(method, path),
                rate_limit=rate_limit_info,
            )

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        caller: str = "graphql",
    ) -> dict[str, Any]:
        """Execute a GraphQL query or mutation.

        Args:
            query: The GraphQL query or mutation string.
            variables: Variables to pass to the query.
            caller: Identifier for audit logging.

        Returns:
            The parsed JSON response containing 'data' and optionally 'errors'.

        Raises:
            GitHubHttpError: If the request fails or returns GraphQL errors.
        """
        json_body: dict[str, Any] = {"query": query}
        if variables:
            json_body["variables"] = variables

        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code: int | None = None
        payload: dict[str, Any] = {}
        response: httpx.Response | None = None
        try:
            try:
                response = self._client.request(
                    "POST",
                    "/graphql",
                    json=json_body,
                    headers=self._auth_headers(),
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                error = f"transport_error: {exc}"
                raise GitHubTransportError(
                    "GitHub GraphQL transport error",
                    method="POST",
                    url="/graphql",
                    original=exc,
                ) from exc

            status_code = response.status_code
            response_text = response.text

            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                raise GitHubHttpError(
                    f"GitHub GraphQL request failed: {status_code}",
                    method="POST",
                    url="/graphql",
                    status_code=status_code,
                    response_text=response_text,
                )

            payload = response.json()

            # Check for GraphQL-level errors
            if "errors" in payload and payload["errors"]:
                error_messages = [e.get("message", str(e)) for e in payload["errors"]]
                error = f"GraphQL errors: {error_messages}"
                raise GitHubHttpError(
                    f"GitHub GraphQL error: {error_messages[0]}",
                    method="POST",
                    url="/graphql",
                    status_code=status_code,
                    response_text=response_text,
                )

            return payload
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            rate_limit_info = _extract_rate_limit_headers(response) if response is not None else None
            gh_audit.record_live_call(
                command="POST /graphql",
                caller=caller,
                error=error,
                rate_limit=rate_limit_info,
            )
            gh_audit.record(
                args=["POST", "/graphql", caller],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller=caller,
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=1 if payload.get("data") else 0,
                full_scan=False,
                rate_limit=rate_limit_info,
            )

    # -------------------- Issues --------------------

    def list_issues(
        self,
        *,
        labels: list[str] | None = None,
        state: str = "open",
        milestone: str | None = None,
        limit: int = 100,
        use_cache: bool = True,
        exhaustive: bool = False,
    ) -> list[dict[str, Any]]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            state: Filter by issue state. Can be "open", "closed", or "all".
            milestone: Filter by milestone title.
            limit: Maximum number of issues to return.
            use_cache: If True (default), use ETag cache. If False, bypass cache
                and force a fresh request (used when required IDs are missing).
            exhaustive: If True, the multi-page walk enforces the fail-loud
                completeness contract (#6779 R17) — a later-page non-200 or
                transport failure raises, and hitting the scan cap raises —
                so an AUTHORITATIVE caller (the open-issue anchor scan) can
                never consume a silently partial result. If False (default),
                ``limit`` is a deliberate bound and later pages are best-effort.
        """
        params: dict[str, Any] = {
            "state": state,
            "per_page": min(100, max(1, limit)),
        }
        if labels:
            params["labels"] = ",".join(labels)
        if milestone:
            milestone_number = self._get_milestone_number(milestone)
            if milestone_number is not None:
                params["milestone"] = milestone_number
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues",
            params=params,
            caller="list_issues",
            use_cache=use_cache,
        )
        if not isinstance(payload, list):
            return []
        issues = [item for item in payload if "pull_request" not in item]
        # Exhaustive discovery (#6779 R4): a single page caps at 100, so a
        # caller asking for more than that and receiving a FULL first page may
        # be truncating the matching set. Walk subsequent pages until a short
        # page or the requested limit is reached. The common case (limit<=100)
        # keeps the cached single-page path untouched.
        if limit > 100 and len(payload) >= int(params["per_page"]):
            issues.extend(
                self._list_issues_remaining_pages(
                    params=params, limit=limit, exhaustive=exhaustive
                )
            )
        return issues[:limit]

    def _list_issues_remaining_pages(
        self, *, params: dict[str, Any], limit: int, exhaustive: bool = False
    ) -> list[dict[str, Any]]:
        """Fetch issue pages beyond the first until short page / limit (#6779 R4).

        When ``exhaustive`` the fail-loud contract applies (#6779 R17): the walk
        shares :meth:`_paginate_fresh` with the all-labels pager, so a later-page
        non-200/transport failure or a cap-exhausted scan RAISES instead of
        returning a silently partial anchor set. Otherwise ``limit`` is a
        deliberate bound and later-page failures are best-effort (the general
        ``fetch_limit`` fetch, where a truncated window is expected, not a bug).

        Uncached (mirrors :meth:`list_all_labels`): later pages are read fresh
        rather than ETag-cached per page. Bounded by a page cap so an
        unbounded scan fails loud rather than looping.
        """
        if exhaustive:
            return self._list_open_issues_exhaustive_pages(params=params, limit=limit)
        collected: list[dict[str, Any]] = []
        page = 2
        while True:
            page_params = {**params, "page": page}
            try:
                response = self._client.get(
                    f"/repos/{self._config.repo}/issues",
                    params=page_params,
                    headers=self._auth_headers(),
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                raise GitHubTransportError(
                    "GitHub transport error for list_issues pagination",
                    method="GET",
                    url=f"/repos/{self._config.repo}/issues",
                    original=exc,
                ) from exc
            if response.status_code != 200:
                break
            batch = response.json()
            if not isinstance(batch, list) or not batch:
                break
            collected.extend(item for item in batch if "pull_request" not in item)
            if len(batch) < int(params["per_page"]) or len(collected) >= limit:
                break
            page += 1
            if page > 20:  # Safety limit: 20 * 100 = 2000 issues (list_all_labels parity)
                break
        return collected

    def _list_open_issues_exhaustive_pages(
        self, *, params: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        """Fail-loud remaining-page walk for the AUTHORITATIVE open-issue scan.

        ``discover_open_tech_lead_anchor_issues`` consumes this as EXHAUSTIVE: an
        anchor or approved op hidden on a dropped/uncrawled page causes
        duplicate anchor creation, missed startup recovery, or an indefinitely
        delayed approved op. So this reuses the SAME fail-loud pager as the
        all-labels scan (#6779 R8/R17) — one completeness contract, no
        cross-path drift — rather than accepting a partial list. ``limit`` sets
        the page cap (``ceil(limit/per_page)`` pages, page 1 already read); a
        scan that would exceed it cannot prove completeness and raises, exactly
        as the label cap does.
        """
        per_page = int(params["per_page"])
        page_cap = max(2, -(-limit // per_page))
        collected: list[dict[str, Any]] = []
        for batch in self._paginate_fresh(
            f"/repos/{self._config.repo}/issues",
            params=params,
            start_page=2,
            page_cap=page_cap,
            what="repository issues",
        ):
            collected.extend(item for item in batch if "pull_request" not in item)
        return collected

    def list_issues_since(
        self,
        *,
        since: str,
        state: str = "all",
        limit: int = 100,
        use_cache: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List issues updated since a watermark using repo-wide delta feed.

        Returns:
            (issues, next_watermark_hint) where next watermark is the oldest
            updated_at value from the fetched batch to avoid skipping updates
            when results are truncated by limit.
        """
        per_page = min(100, max(1, limit))
        page = 1
        collected: list[dict[str, Any]] = []
        oldest_updated_at: str | None = None

        while len(collected) < limit:
            params: dict[str, Any] = {
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "since": since,
                "per_page": per_page,
                "page": page,
            }
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/issues",
                params=params,
                caller="list_issues_delta",
                use_cache=use_cache,
            )
            if not isinstance(payload, list) or not payload:
                break

            for item in payload:
                if not isinstance(item, dict):
                    continue
                if "pull_request" in item:
                    continue
                collected.append(item)
                updated_at = item.get("updated_at")
                if isinstance(updated_at, str):
                    if oldest_updated_at is None or updated_at < oldest_updated_at:
                        oldest_updated_at = updated_at
                if len(collected) >= limit:
                    break

            if len(payload) < per_page:
                break
            page += 1

        return collected[:limit], oldest_updated_at

    def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            caller="get_issue",
        )
        return payload if isinstance(payload, dict) else None

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict[str, Any] | None:
        """Create a new issue.

        Returns the full issue data including number and html_url.
        """
        json_body: dict[str, Any] = {"title": title, "body": body, "labels": labels or []}
        if milestone is not None:
            json_body["milestone"] = milestone

        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues",
            json_body=json_body,
            use_cache=False,
            caller="create_issue",
        )
        if isinstance(payload, dict):
            return payload
        return None

    def add_label(self, issue_number: int, label: str) -> None:
        self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels",
            json_body={"labels": [label]},
            use_cache=False,
            caller="add_label",
        )

    def remove_label(self, issue_number: int, label: str) -> None:
        encoded = quote(label, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels/{encoded}",
            use_cache=False,
            caller="remove_label",
        )

    def get_issue_labels(self, issue_number: int, *, use_cache: bool = True) -> list[str]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels",
            params={"per_page": 100},
            use_cache=use_cache,
            caller="get_issue_labels",
        )
        if isinstance(payload, list):
            names: list[str] = []
            for label in payload:
                if not isinstance(label, dict):
                    continue
                name = label.get("name")
                if isinstance(name, str):
                    names.append(name)
            return names
        return []

    def list_labels(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/labels",
            params={"per_page": 100},
            caller="list_labels",
        )
        labels = payload if isinstance(payload, list) else []
        # The port promises ALL labels (#6779 R8): page 1 keeps its ETag cache
        # for the common (<=100 labels) case, but a FULL first page means more
        # may exist, so continue paging. Without this a gate label sorted onto a
        # later page (e.g. proposed-tech-lead in a repo with 100+ labels) is missed
        # and valid proposal creation is falsely refused. Mirrors list_issues.
        if len(labels) >= 100:
            labels = list(labels) + self._paginate_all_labels(start_page=2)
        return labels

    # 20 pages * 100/page = 2000 labels. An exhaustive label scan that would
    # exceed this cannot prove completeness, so it fails loud instead (R8).
    _LABEL_PAGE_CAP = 20

    # 10 pages * 100/page = 1000 milestones. Same fail-loud contract: a scan
    # that cannot prove completeness must not answer a negative-existence
    # question. Lower than labels because milestones are coarse planning units
    # and a repo with 1000 of them has a different problem.
    _MILESTONE_PAGE_CAP = 10

    def _paginate_fresh(
        self,
        path: str,
        *,
        params: dict[str, Any],
        start_page: int,
        page_cap: int,
        what: str,
    ) -> Iterator[list[Any]]:
        """Yield fresh (uncached) page batches for an AUTHORITATIVE full scan,
        failing loud when completeness cannot be proven (#6779 R8/R17).

        The SINGLE exhaustive-pagination contract shared by the all-labels scan
        and the open-issue anchor scan: a pre-response failure raises
        ``GitHubTransportError``, a later-page non-200 raises ``GitHubHttpError``,
        and exceeding ``page_cap`` full pages raises ``GitHubHttpError``.
        Iteration stops only on the first empty/short/non-list page — the true
        final page — so no caller can mistake a truncated read for a complete
        one. ``params`` must carry ``per_page`` (the short-page threshold); later
        pages are read fresh rather than ETag-cached per page.
        """
        per_page = int(params["per_page"])
        page = start_page
        while True:
            try:
                response = self._client.get(
                    path,
                    params={**params, "page": page},
                    headers=self._auth_headers(),
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                raise GitHubTransportError(
                    f"GitHub transport error while paging {what}",
                    method="GET",
                    url=path,
                    original=exc,
                ) from exc
            if response.status_code != 200:
                raise GitHubHttpError(
                    f"GitHub returned status {response.status_code} while paging"
                    f" {what} (page {page}); refusing to treat the partial"
                    f" {what} as complete",
                    method="GET",
                    url=path,
                    status_code=response.status_code,
                    response_text=response.text,
                )
            batch = response.json()
            if not isinstance(batch, list):
                # A 2xx carrying a non-list body is a contract violation, NOT
                # exhaustion. Treating it as the end of the list would hand the
                # caller a silently truncated result under a method that
                # promises completeness — the exact failure this pager exists to
                # make impossible. Only an empty list is valid exhaustion.
                raise GitHubHttpError(
                    f"GitHub returned a non-list body while paging {what}"
                    f" (page {page}); refusing to treat it as an exhausted list",
                    method="GET",
                    url=path,
                    status_code=response.status_code,
                    response_text=response.text,
                )
            if not batch:
                return
            yield batch
            if len(batch) < per_page:  # short page => exhausted, list is complete
                return
            page += 1
            if page > page_cap:
                raise GitHubHttpError(
                    f"{what} scan exceeded the {page_cap * per_page}-item page"
                    " cap; cannot prove the list is complete",
                    method="GET",
                    url=path,
                )

    def _paginate_all_labels(self, *, start_page: int) -> list[dict[str, Any]]:
        """Exhaustively page repo labels from ``start_page``, failing loud
        when completeness cannot be established (#6779 R8).

        The port promises ALL labels, and control/tech_lead_proposals.py makes a
        gate-ABSENT decision from this list — a truncated scan that misses the
        ``proposed-tech-lead`` gate would falsely refuse valid proposals. So this
        never returns a silently partial result: it drains the shared fail-loud
        pager (:meth:`_paginate_fresh`), which raises on a transport failure, a
        later-page non-200, or a cap-exhausted scan. Uncached: later pages are
        read fresh rather than ETag-cached per page.
        """
        collected: list[dict[str, Any]] = []
        for batch in self._paginate_fresh(
            f"/repos/{self._config.repo}/labels",
            params={"per_page": 100},
            start_page=start_page,
            page_cap=self._LABEL_PAGE_CAP,
            what="repository labels",
        ):
            collected.extend(batch)
        return collected

    def list_all_labels(self) -> list[dict[str, Any]]:
        """Fetch all labels with pagination (for cleanup operations).

        Unlike list_labels(), which ETag-caches the first page, this reads
        every page fresh. Shares the exhaustive fail-loud pager so both
        all-labels paths enforce completeness identically (#6779 R8).
        """
        return self._paginate_all_labels(start_page=1)

    def invalidate_labels_etag(self) -> None:
        """Invalidate ETag cache for the labels endpoint.

        Call after POST/PATCH/DELETE on repo labels to ensure
        subsequent GETs fetch fresh data.
        """
        url = f"/repos/{self._config.repo}/labels"
        key = self._cache_key("GET", url, {"per_page": 100})  # Match list_labels params
        self._etag_cache.pop(key, None)

    def invalidate_pr_etag(self, pr_number: int) -> None:
        """Invalidate ETag cache for a PR endpoint."""
        url = f"/repos/{self._config.repo}/pulls/{pr_number}"
        key = self._cache_key("GET", url, None)
        self._etag_cache.pop(key, None)

    def list_milestones(self, state: str = "open") -> list[dict[str, Any]]:
        """List milestones in the repository.

        Args:
            state: Filter by milestone state ('open', 'closed', 'all')

        Returns:
            List of milestone dictionaries.
        """
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/milestones",
            params={"state": state, "per_page": 100},
            caller="list_milestones",
        )
        return payload if isinstance(payload, list) else []

    def list_all_milestones(self) -> list[dict[str, Any]]:
        """Fetch EVERY milestone, all states, with exhaustive pagination.

        Unlike :meth:`list_milestones`, which reads a single page of one state,
        this promises completeness — because its callers make *negative*
        decisions from the result ("this configured milestone does not exist").
        A truncated or state-filtered read turns a correct configuration into a
        false report, which is worse than no check at all.

        ``state="all"`` because milestone scope is evaluated independently of
        issue state, so a closed milestone is still a valid dependency target.

        Shares the fail-loud pager (:meth:`_paginate_fresh`), which raises on a
        transport failure, a later-page non-200, or a cap-exhausted scan, so no
        caller can mistake a partial read for a complete one.
        """
        collected: list[dict[str, Any]] = []
        for batch in self._paginate_fresh(
            f"/repos/{self._config.repo}/milestones",
            params={"state": "all", "per_page": 100},
            start_page=1,
            page_cap=self._MILESTONE_PAGE_CAP,
            what="repository milestones",
        ):
            collected.extend(batch)
        return collected

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict[str, Any] | None:
        """Create a milestone."""
        json_body: dict[str, Any] = {"title": title, "state": state}
        if description:
            json_body["description"] = description
        if due_on:
            json_body["due_on"] = due_on
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/milestones",
            json_body=json_body,
            use_cache=False,
            caller="create_milestone",
        )
        return payload if isinstance(payload, dict) else None

    def create_label(
        self,
        name: str,
        *,
        color: str = "ededed",
        description: str | None = None,
        force: bool = False,
    ) -> None:
        body = {"name": name, "color": color}
        if description:
            body["description"] = description
        try:
            self._request_json(
                "POST",
                f"/repos/{self._config.repo}/labels",
                json_body=body,
                use_cache=False,
                caller="create_label",
            )
        except GitHubHttpError as exc:
            if exc.status_code == 422 and force:
                encoded = quote(name, safe="")
                self._request_json(
                    "PATCH",
                    f"/repos/{self._config.repo}/labels/{encoded}",
                    json_body=body,
                    use_cache=False,
                    caller="update_label",
                )
                return
            if exc.status_code == 422:
                return
            raise

    def delete_label(self, name: str) -> None:
        encoded = quote(name, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/labels/{encoded}",
            use_cache=False,
            caller="delete_label",
        )

    def add_comment(self, issue_number: int, body: str) -> str:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues/{issue_number}/comments",
            json_body={"body": body},
            use_cache=False,
            caller="add_comment",
        )
        if isinstance(payload, dict):
            return payload.get("html_url", f"https://github.com/{self._config.repo}/issues/{issue_number}")
        return f"https://github.com/{self._config.repo}/issues/{issue_number}"

    def get_issue_comments(
        self,
        issue_number: int,
        *,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
            caller="get_issue_comments",
            use_cache=use_cache,
        )
        return payload if isinstance(payload, list) else []

    def issue_comment_marker_present(self, issue_number: int, marker: str) -> bool:
        """Return True if any comment on the issue/PR contains ``marker``.

        Paginates the issue/PR comments endpoint (unlike
        ``get_issue_comments``, which returns only the first page) and
        short-circuits as soon as a comment body containing ``marker`` is
        found, so a marker posted beyond the first 100 comments is still
        detected. The scan terminates only when GitHub returns a short or
        empty page (the true final page); a returned ``False`` therefore means
        the marker is genuinely absent from every page. An operational page cap
        guards against an unbounded loop, but hitting it **fails loud**
        (raises ``GitHubHttpError``) rather than reporting "absent" from a
        truncated scan -- a truncated read is not evidence the marker is
        missing, and silently returning ``False`` would let a dedupe caller
        post a duplicate comment. A malformed (non-list) 2xx body is likewise
        treated as a fail-loud read error, not as "absent". Not ETag-cached:
        dedupe reads are correctness-critical and must not return a stale page.
        Transport/HTTP errors propagate as ``RepositoryHostError`` so callers
        can fail loud.
        """
        page = 1
        while True:
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/issues/{issue_number}/comments",
                params={"per_page": 100, "page": page},
                caller="issue_comment_marker_present",
                use_cache=False,
            )
            if not isinstance(payload, list):
                # A non-list 2xx body is a contract violation (proxy/mock drift
                # or malformed GitHub response), not evidence of "no marker".
                # Fail loud so a dedupe caller never posts a duplicate from a
                # response we could not actually scan.
                raise GitHubHttpError(
                    f"Comment listing for #{issue_number} page {page} was not "
                    f"a list ({type(payload).__name__}); cannot confirm marker "
                    f"absence",
                    issue_number=issue_number,
                )
            if not payload:
                # Empty page = the true final page with nothing on it: the
                # marker is genuinely absent.
                return False
            for comment in payload:
                if not isinstance(comment, dict):
                    continue
                body = comment.get("body")
                if isinstance(body, str) and marker in body:
                    return True
            if len(payload) < 100:
                return False
            page += 1
            if page > _MARKER_SCAN_PAGE_CAP:
                # The cap exists only to bound a pathological loop, not to
                # define "marker absent". Fail loud so the dedupe caller never
                # mistakes a truncated scan for a clean one.
                raise GitHubHttpError(
                    f"Comment marker scan for #{issue_number} exceeded "
                    f"{_MARKER_SCAN_PAGE_CAP} pages without reaching the final "
                    f"page; cannot confirm marker absence",
                    issue_number=issue_number,
                )

    def issue_closed_on_or_after(self, issue_number: int, timestamp: str) -> bool:
        """Return True if the issue has a ``closed`` event at/after ``timestamp``.

        Paginates ``/issues/{n}/events`` with the same fail-loud contract as
        ``issue_comment_marker_present``: a short/empty page is the only clean
        "no such event" answer; a malformed body or a scan that exceeds the
        page cap raises rather than reporting absence from a truncated read —
        the caller decides a destructive write (close-on-merge fallback) on
        this fact. Timestamps are GitHub's own ISO-8601 UTC strings on both
        sides, so lexicographic comparison is chronological. Not ETag-cached:
        transition evidence must not come from a stale page.
        """
        page = 1
        while True:
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/issues/{issue_number}/events",
                params={"per_page": 100, "page": page},
                caller="issue_closed_on_or_after",
                use_cache=False,
            )
            if not isinstance(payload, list):
                raise GitHubHttpError(
                    f"Event listing for #{issue_number} page {page} was not a "
                    f"list ({type(payload).__name__}); cannot confirm close-"
                    f"event absence",
                    issue_number=issue_number,
                )
            if not payload:
                return False
            for event in payload:
                if not isinstance(event, dict):
                    continue
                if event.get("event") != "closed":
                    continue
                created_at = event.get("created_at")
                if isinstance(created_at, str) and created_at >= timestamp:
                    return True
            if len(payload) < 100:
                return False
            page += 1
            if page > _MARKER_SCAN_PAGE_CAP:
                raise GitHubHttpError(
                    f"Issue event scan for #{issue_number} exceeded "
                    f"{_MARKER_SCAN_PAGE_CAP} pages without reaching the final "
                    f"page; cannot confirm close-event absence",
                    issue_number=issue_number,
                )

    # -------------------- Git refs / commits --------------------

    def get_default_branch(self) -> str:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}",
            caller="get_default_branch",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub repository payload was not an object")
        default_branch = payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubHttpError("GitHub repository payload missing default_branch")
        return default_branch

    def get_git_ref(self, ref: str) -> dict[str, Any] | None:
        encoded = quote(_api_ref_path(ref), safe="/")
        try:
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/git/ref/{encoded}",
                use_cache=False,
                caller="get_git_ref",
            )
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def create_git_ref(self, *, ref: str, sha: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/git/refs",
            json_body={"ref": ref, "sha": sha},
            use_cache=False,
            caller="create_git_ref",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub create ref payload was not an object")
        return payload

    def update_git_ref(self, *, ref: str, sha: str, force: bool = False) -> dict[str, Any]:
        encoded = quote(_api_ref_path(ref), safe="/")
        payload = self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/git/refs/{encoded}",
            json_body={"sha": sha, "force": force},
            use_cache=False,
            caller="update_git_ref",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub update ref payload was not an object")
        return payload

    def delete_git_ref(self, ref: str) -> None:
        encoded = quote(_api_ref_path(ref), safe="/")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/git/refs/{encoded}",
            use_cache=False,
            caller="delete_git_ref",
        )

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        encoded = quote(sha, safe="")
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/git/commits/{encoded}",
            use_cache=False,
            caller="get_git_commit",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub commit payload was not an object")
        return payload

    def create_git_commit(
        self,
        *,
        message: str,
        tree_sha: str,
        parents: list[str],
    ) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/git/commits",
            json_body={"message": message, "tree": tree_sha, "parents": parents},
            use_cache=False,
            caller="create_git_commit",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub create commit payload was not an object")
        return payload

    def update_issue_state(self, issue_number: int, state: str) -> None:
        self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            json_body={"state": state},
            use_cache=False,
            caller="update_issue_state",
        )

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> dict[str, Any] | None:
        payload = self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            json_body={"milestone": milestone},
            use_cache=False,
            caller="update_issue_milestone",
        )
        return payload if isinstance(payload, dict) else None

    # -------------------- PRs --------------------

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls/{pr_number}",
            caller="get_pr",
        )
        return payload if isinstance(payload, dict) else None

    def get_pr_status_check_rollup(self, pr_number: int) -> str | None:
        """Fetch the aggregated status-check rollup for a PR's head commit.

        Returns one of GitHub's `StatusState` values (`SUCCESS`, `FAILURE`,
        `PENDING`, `EXPECTED`, `ERROR`) or `None` if the PR / commit has no
        rollup (no checks configured). Used by the awaiting-merge reconciler
        to disambiguate `mergeable_state=unstable|blocked` between
        "checks running" and "check actually failed".
        """
        owner, repo = self._config.repo.split("/", 1)
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    commits(last: 1) {
                        nodes {
                            commit {
                                statusCheckRollup { state }
                            }
                        }
                    }
                }
            }
        }
        """
        result = self._graphql(
            query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller="get_pr_status_check_rollup",
        )
        pr_data = (
            result.get("data", {}).get("repository", {}).get("pullRequest")
        )
        if not pr_data:
            return None
        nodes = (pr_data.get("commits") or {}).get("nodes") or []
        if not nodes:
            return None
        commit = nodes[0].get("commit") or {}
        rollup = commit.get("statusCheckRollup")
        if not rollup:
            return None
        state = rollup.get("state")
        return state if isinstance(state, str) else None

    def get_commit_check_rollup(self, sha: str) -> CommitCheckRollup:
        """Aggregate REST check-runs + combined commit status into a single
        `StatusState`-like rollup for a commit SHA.

        This is the REST fallback for `get_pr_status_check_rollup` when the
        GraphQL `statusCheckRollup` query is inaccessible to the running token.
        The returned `CommitCheckRollup.state` is one of `FAILURE` / `PENDING`
        / `SUCCESS`, or `None` when the commit has no checks or statuses at all.

        The check-runs API is paginated. Pagination short-circuits only once a
        FAILURE run is seen (the top of the rollup precedence, which no later
        page can change); a pending run is remembered but pagination continues,
        because a completed failing run on a later page must still override an
        earlier in-progress one. Pagination is bounded by a safety cap of
        `_MAX_CHECK_RUN_PAGES` full pages: if the cap is reached before a final
        short page without finding a failure, the unread later pages could still
        hold a failed required run, so the check-runs source is reported
        unreadable (incomplete) rather than a conclusive success/pending.

        The check-runs and combined-status sources are read INDEPENDENTLY: if
        the check-runs API is inaccessible, the legacy combined-status source is
        still consulted (and vice versa), so a conclusive failure/pending from
        either readable source is honored even when the other cannot be read.

        `CommitCheckRollup.capability` is `ok` only when `state` is trustworthy:
        either every source was read in full, or a readable source produced a
        `FAILURE`. `FAILURE` is the top of the rollup precedence (`FAILURE` >
        `PENDING` > `SUCCESS`/none), so no signal an unread source could ADD
        outranks it — a readable failure stays conclusive. A readable `PENDING`
        is NOT conclusive when a source is unread, because that unread source
        could hold a failed required check/status that outranks pending; the
        capability then carries WHY the source was unread (`permission_denied`
        vs `transient_error`) so the caller can escalate a real scope gap but
        merely retry a transient/throttle/cap-truncated read. This method does
        not raise on source access failures — the typed capability carries them.
        """
        encoded = quote(sha, safe="")
        checks = self._read_check_run_signal(encoded)
        status_readout = self._best_effort_commit_status(sha, encoded)
        statuses = _aggregate_combined_status(status_readout.payload)
        state = _combine_rollup_signals(checks.signal, statuses)
        # A readable FAILURE is conclusive: it is the top of the rollup
        # precedence (FAILURE > PENDING > SUCCESS/none), so no signal an unread
        # source could ADD outranks it. Otherwise the aggregate is only as
        # trustworthy as its least-read source, so fold the per-source outcomes
        # into the read capability (which preserves permission vs transient vs
        # cap-truncated, instead of collapsing every gap to permission-denied).
        if state == "FAILURE":
            return CommitCheckRollup(state="FAILURE", capability="ok")
        capability = _aggregate_rollup_capability(checks.outcome, status_readout.outcome)
        return CommitCheckRollup(state=state, capability=capability)

    def _read_check_run_signal(self, encoded_sha: str) -> _SourceReadout:
        """Fold the paginated check-runs API into one `_SourceReadout`.

        Stops early only once a failure run is seen: failure is the top of the
        rollup precedence (`_combine_rollup_signals` returns FAILURE before
        PENDING), so no later page can change a FAILURE. A pending signal is NOT
        conclusive — a completed failing run on a later page must still override
        an earlier in-progress one — so pending is remembered but pagination
        continues until a failure is found or the final page is read.

        If the API is inaccessible, the source's `outcome` records WHY —
        `permission_denied` (token lacks `checks:read`) vs `transient_error` (a
        retryable 5xx/rate-limit/timeout) — with an empty signal rather than
        raising, so the caller can still honor the combined-status source and
        apply the right policy (escalate vs retry). A partial read that errored
        mid-pagination before finding a failure is reported the same way (its
        in-progress signal is discarded), because an unread page could still
        hold a failure.

        If pagination reaches the `_MAX_CHECK_RUN_PAGES` safety cap on a full
        page without finding a failure, the unread later pages could likewise
        hold a failed required run, so the source's `outcome` is `capped` — the
        partial pending/present signal is preserved only as non-conclusive
        evidence. A cap is NOT a permission failure, so the caller retries/waits
        rather than arming the permission backoff, unless another readable
        source already produced a FAILURE.
        """
        failure = pending = present = False
        page = 1
        while True:
            try:
                payload = self._request_json(
                    "GET",
                    f"/repos/{self._config.repo}/commits/{encoded_sha}/check-runs",
                    params={"per_page": 100, "page": page},
                    caller="get_commit_check_rollup_checks",
                )
            except GitHubTransportError as exc:
                logger.debug(
                    "check-runs read failed for %s (page %d, transient): %s",
                    encoded_sha, page, exc,
                )
                return _SourceReadout((False, False, False), outcome="transient_error")
            except GitHubHttpError as exc:
                outcome = classify_github_http_failure(exc)
                logger.debug(
                    "check-runs unreadable for %s (page %d, %s): %s",
                    encoded_sha, page, outcome, exc,
                )
                return _SourceReadout((False, False, False), outcome=outcome)
            page_failure, page_pending, page_present = _aggregate_check_runs(payload)
            failure = failure or page_failure
            pending = pending or page_pending
            present = present or page_present
            if failure:
                break  # FAILURE is conclusive — no later page can change it
            runs = payload.get("check_runs") if isinstance(payload, dict) else None
            if not isinstance(runs, list) or len(runs) < 100:
                break  # last (or only) page
            page += 1
            if page > _MAX_CHECK_RUN_PAGES:
                logger.warning(
                    "check-runs pagination hit safety cap (%d pages) for %s "
                    "without a failure; treating the check-runs source as "
                    "truncated so an unread later-page failure cannot be "
                    "masked as a conclusive success/pending",
                    _MAX_CHECK_RUN_PAGES, encoded_sha,
                )
                # Cap reached on a full page (a further page exists) with no
                # failure yet. The unread pages could hold a failed required
                # run, which outranks the pending/present we have, so the source
                # is truncated: outcome="capped" keeps the partial signal only
                # as non-conclusive evidence. Only another readable FAILURE can
                # still make the aggregate conclusive.
                return _SourceReadout((failure, pending, present), outcome="capped")
        return _SourceReadout((failure, pending, present), outcome="ok")

    def _best_effort_commit_status(
        self, sha: str, encoded_sha: str
    ) -> _CommitStatusReadout:
        """Fetch the legacy combined commit status, reporting access failures.

        GitHub Actions checks live only in the Checks API, so an inaccessible
        combined-status call must NOT poison a readable check-run result — it
        is purely supplementary for repos using the older Status API. The
        readout's typed `outcome` records WHY the source was unread
        (`permission_denied` vs `transient_error`) so the caller preserves the
        failure cause instead of silently treating an unreadable status source
        as "no statuses".
        """
        try:
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/commits/{encoded_sha}/status",
                caller="get_commit_check_rollup_status",
            )
        except GitHubTransportError as exc:
            logger.debug(
                "Combined commit status read failed for %s (transient): %s", sha, exc
            )
            return _CommitStatusReadout(payload=None, outcome="transient_error")
        except GitHubHttpError as exc:
            outcome = classify_github_http_failure(exc)
            logger.debug(
                "Combined commit status unavailable for %s (%s): %s", sha, outcome, exc
            )
            return _CommitStatusReadout(payload=None, outcome=outcome)
        normalized = payload if isinstance(payload, dict) else None
        return _CommitStatusReadout(payload=normalized, outcome="ok")

    def list_prs(self, *, state: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls",
            params={"state": state, "per_page": min(100, max(1, limit))},
            caller="list_prs",
        )
        if not isinstance(payload, list):
            return []
        return payload[:limit]

    def close_pr(self, pr_number: int) -> None:
        self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/pulls/{pr_number}",
            json_body={"state": "closed"},
            use_cache=False,
            caller="close_pr",
        )

    def update_pr_base(self, pr_number: int, base: str) -> dict[str, Any] | None:
        """Retarget a PR onto a new base branch via the REST PATCH endpoint."""
        payload = self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/pulls/{pr_number}",
            json_body={"base": base},
            use_cache=False,
            caller="update_pr_base",
        )
        self.invalidate_pr_etag(pr_number)
        return payload if isinstance(payload, dict) else None

    # -------------------- Merge queue (GraphQL) --------------------

    def _pr_node_id(self, pr_number: int, *, caller: str) -> str:
        """Resolve a PR's GraphQL node ID, required by the queue mutations."""
        owner, repo = self._config.repo.split("/", 1)
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) { id }
            }
        }
        """
        result = self._graphql(
            query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller=caller,
        )
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_data or not pr_data.get("id"):
            raise GitHubHttpError(
                f"PR #{pr_number} not found",
                method="POST",
                url="/graphql",
                status_code=200,
                response_text=str(result),
            )
        return pr_data["id"]

    def get_merge_queue_entry(self, pr_number: int) -> dict[str, Any] | None:
        """Return ``{"state", "position"}`` for a PR's merge queue entry.

        Returns ``None`` when the PR is not currently in the merge queue.
        """
        owner, repo = self._config.repo.split("/", 1)
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    mergeQueueEntry { state position }
                }
            }
        }
        """
        result = self._graphql(
            query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller="get_merge_queue_entry",
        )
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_data:
            return None
        entry = pr_data.get("mergeQueueEntry")
        if not entry:
            return None
        return entry

    def enqueue_pull_request(self, pr_number: int) -> None:
        """Add a PR to the repository's merge queue via GraphQL."""
        node_id = self._pr_node_id(pr_number, caller="enqueue_pull_request_get_id")
        mutation = """
        mutation($pullRequestId: ID!) {
            enqueuePullRequest(input: {pullRequestId: $pullRequestId}) {
                mergeQueueEntry { state position }
            }
        }
        """
        self._graphql(
            mutation,
            {"pullRequestId": node_id},
            caller="enqueue_pull_request",
        )

    def list_branches(self) -> list[str]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/branches",
            params={"per_page": 100},
            caller="list_branches",
        )
        if not isinstance(payload, list):
            return []
        names = []
        for item in payload:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    names.append(name)
        return names

    def delete_branch(self, branch: str) -> None:
        encoded = quote(branch, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/git/refs/heads/{encoded}",
            use_cache=False,
            caller="delete_branch",
        )

    def branch_exists(self, branch: str) -> bool:
        encoded = quote(branch, safe="")
        try:
            self._request_json(
                "GET",
                f"/repos/{self._config.repo}/git/refs/heads/{encoded}",
                use_cache=False,
                caller="branch_exists",
            )
            return True
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return False
            raise

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[dict[str, Any]]:
        owner = self._config.repo.split("/")[0]
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": state, "per_page": 100},
            caller="get_prs_for_branch",
        )
        return payload if isinstance(payload, list) else []

    def _valid_pr_search_items(
        self,
        payload: dict[str, Any],
        label: str,
        state: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in _search_items(payload):
            number = item.get("number")
            if not isinstance(number, int):
                logger.warning(
                    "Skipping malformed PR search item: label=%s state=%s url=%s",
                    label,
                    state,
                    item.get("html_url") or item.get("url"),
                )
                gh_audit.emit_event(EventName.GH_SEARCH_ITEM_MALFORMED, {
                    "label": label,
                    "state": state,
                    "item": {
                        "html_url": item.get("html_url"),
                        "url": item.get("url"),
                        "title": item.get("title"),
                    },
                })
                continue
            items.append(item)
        return items

    def get_prs_with_label(self, label: str, state: str = "open") -> list[dict[str, Any]]:
        if state == "all":
            items: list[dict[str, Any]] = []
            seen: set[int] = set()
            for st in ("open", "closed"):
                query = f"repo:{self._config.repo} is:pr label:{label} state:{st}"
                payload = self._request_json(
                    "GET",
                    "/search/issues",
                    params={"q": query, "per_page": 100},
                    caller="get_prs_with_label",
                )
                for item in self._valid_pr_search_items(payload, label=label, state=st):
                    number = cast(int, item.get("number"))
                    if number in seen:
                        continue
                    seen.add(number)
                    items.append(item)
            return items
        query = f"repo:{self._config.repo} is:pr label:{label} state:{state}"
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": 100},
            caller="get_prs_with_label",
        )
        return self._valid_pr_search_items(payload, label=label, state=state)

    def get_prs_with_label_graphql(
        self,
        label: str,
        *,
        state: str = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch PRs with a label using GraphQL (single request, includes head branch).

        Unlike get_prs_with_label (search API + N individual get_pr calls),
        this returns full PR info including head.ref in 1-2 GraphQL requests.

        Returns dicts with keys matching the REST PR API shape (number, title,
        html_url, head.ref, body, state, labels, draft) so _pr_info_from_api
        can parse them directly.
        """
        # Map state filter to GraphQL enum values
        states: list[str]
        if state == "all":
            states = ["OPEN", "CLOSED", "MERGED"]
        elif state == "closed":
            states = ["CLOSED", "MERGED"]
        else:
            states = [state.upper()]

        owner, repo_name = self._config.repo.split("/", 1)

        query = """
        query($owner: String!, $repo: String!, $label: String!, $states: [PullRequestState!], $first: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequests(labels: [$label], states: $states, first: $first, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                number
                title
                url
                headRefName
                body
                state
                isDraft
                labels(first: 20) {
                  nodes { name }
                }
              }
            }
          }
        }
        """
        all_prs: list[dict[str, Any]] = []
        cursor: str | None = None
        remaining = limit

        while remaining > 0:
            batch = min(remaining, 100)
            variables: dict[str, Any] = {
                "owner": owner,
                "repo": repo_name,
                "label": label,
                "states": states,
                "first": batch,
            }
            if cursor:
                variables["after"] = cursor

            result = self._graphql(query, variables, caller="get_prs_with_label_graphql")
            data = result.get("data", {})
            repo_data = data.get("repository", {})
            prs_data = repo_data.get("pullRequests", {})
            nodes = prs_data.get("nodes", [])

            for node in nodes:
                # Reshape to match REST API format for _pr_info_from_api
                labels = [{"name": l["name"]} for l in (node.get("labels", {}).get("nodes", []))]
                pr_dict: dict[str, Any] = {
                    "number": node["number"],
                    "title": node.get("title", ""),
                    "html_url": node.get("url", ""),
                    "head": {"ref": node.get("headRefName", "")},
                    "body": node.get("body", "") or "",
                    "state": node.get("state", "OPEN").lower(),
                    "labels": labels,
                    "draft": node.get("isDraft"),
                }
                all_prs.append(pr_dict)

            page_info = prs_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            remaining -= len(nodes)

        return all_prs

    def get_prs_for_issue(self, issue_number: int) -> list[dict[str, Any]]:
        # GitHub's search API rejects queries without an `is:` qualifier with
        # 422 ("Query must include 'is:issue' or 'is:pull-request'"). The
        # parens are required so `OR` binds within the disjunction rather than
        # across the `is:pr` qualifier.
        query = (
            f"repo:{self._config.repo} is:pr "
            f"(head:{issue_number} OR #{issue_number})"
        )
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": 100},
            caller="get_prs_for_issue",
        )
        return _search_items(payload)

    def search_issues_by_title(
        self,
        query_terms: list[str],
        *,
        limit: int = 30,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Search issues whose title contains any of the given quoted terms.

        Terms are OR'd inside parens; `in:title` scopes the match to titles;
        `is:issue` is required for fine-grained PATs (search API otherwise
        returns 422). Returns raw search result items; caller filters.
        """
        if not query_terms:
            return []
        quoted = " OR ".join(f'"{t}"' for t in query_terms)
        query = f"repo:{self._config.repo} is:issue ({quoted}) in:title"
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": min(100, max(1, limit))},
            caller="search_issues_by_title",
            use_cache=use_cache,
        )
        return _search_items(payload)

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get all reviews on a pull request.

        Args:
            pr_number: The PR number to get reviews for.

        Returns:
            List of review dicts with 'state', 'body', 'user' etc.
        """
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
            caller="get_pr_reviews",
        )
        return payload if isinstance(payload, list) else []

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> dict[str, Any] | None:
        body_payload: dict[str, Any] = {"title": title, "body": body, "head": head, "base": base}
        if draft is not None:
            body_payload["draft"] = draft
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/pulls",
            json_body=body_payload,
            use_cache=False,
            caller="create_pr",
        )
        return payload if isinstance(payload, dict) else None

    def set_pr_draft(self, pr_number: int, draft: bool) -> dict[str, Any] | None:
        """Set draft status on a pull request using GraphQL.

        The REST API does not support changing draft status, so we must use
        GraphQL mutations: markPullRequestReadyForReview or convertPullRequestToDraft.
        """
        owner, repo = self._config.repo.split("/")

        # First, get the PR's GraphQL node ID
        node_id_query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    id
                }
            }
        }
        """
        result = self._graphql(
            node_id_query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller="set_pr_draft_get_id",
        )
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_data:
            raise GitHubHttpError(
                f"PR #{pr_number} not found",
                method="POST",
                url="/graphql",
                status_code=200,
                response_text=str(result),
            )
        node_id = pr_data["id"]

        # Use the appropriate mutation based on desired draft status
        if draft:
            mutation = """
            mutation($pullRequestId: ID!) {
                convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
                    pullRequest {
                        id
                        number
                        isDraft
                    }
                }
            }
            """
            mutation_name = "convertPullRequestToDraft"
        else:
            mutation = """
            mutation($pullRequestId: ID!) {
                markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                    pullRequest {
                        id
                        number
                        isDraft
                    }
                }
            }
            """
            mutation_name = "markPullRequestReadyForReview"

        result = self._graphql(
            mutation,
            {"pullRequestId": node_id},
            caller="set_pr_draft_mutate",
        )
        mutation_result = result.get("data", {}).get(mutation_name, {})
        return mutation_result.get("pullRequest")

    # -------------------- Sub-issues (GraphQL) --------------------

    def get_issue_node_id(self, issue_number: int) -> str | None:
        """Get the GraphQL node ID for an issue.

        Args:
            issue_number: Issue number

        Returns:
            Node ID string (e.g., "I_kwDOGK5N...") or None if not found
        """
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                    id
                }
            }
        }
        """
        owner, repo = self._config.repo.split("/")
        try:
            result = self._graphql(
                query,
                {"owner": owner, "repo": repo, "number": issue_number},
                caller="get_issue_node_id",
            )
            issue = result.get("data", {}).get("repository", {}).get("issue")
            if issue:
                return issue.get("id")
            return None
        except GitHubHttpError:
            return None

    def add_sub_issue(
        self,
        parent_node_id: str,
        child_issue_number: int,
    ) -> bool:
        """Link an issue as a sub-issue of a parent.

        Uses GitHub's sub-issues API (addSubIssue mutation).

        Args:
            parent_node_id: GraphQL node ID of the parent issue
            child_issue_number: Issue number of the child to link

        Returns:
            True if linked successfully, False otherwise
        """
        # First get the child's node ID
        child_node_id = self.get_issue_node_id(child_issue_number)
        if not child_node_id:
            logger.warning(
                "[github] Could not get node ID for issue #%d",
                child_issue_number,
            )
            return False

        mutation = """
        mutation($parentId: ID!, $childId: ID!) {
            addSubIssue(input: {issueId: $parentId, subIssueId: $childId}) {
                issue {
                    id
                    number
                }
                subIssue {
                    id
                    number
                }
            }
        }
        """
        try:
            result = self._graphql(
                mutation,
                {"parentId": parent_node_id, "childId": child_node_id},
                caller="add_sub_issue",
            )
            add_result = result.get("data", {}).get("addSubIssue")
            if add_result and add_result.get("subIssue"):
                logger.info(
                    "[github] Linked issue #%d as sub-issue of parent",
                    child_issue_number,
                )
                return True
            return False
        except GitHubHttpError as e:
            logger.warning(
                "[github] Failed to link sub-issue #%d: %s",
                child_issue_number,
                e,
            )
            return False

    # -------------------- Rate limits --------------------

    def get_rate_limit_snapshot(self) -> GitHubRateLimitSnapshot | None:
        payload = self._request_json("GET", "/rate_limit", caller="rate_limit", use_cache=False)
        if not isinstance(payload, dict):
            return None
        resources = payload.get("resources", {})
        core = resources.get("core", {})
        search = resources.get("search", {})
        graphql = resources.get("graphql", {})
        return GitHubRateLimitSnapshot(
            core_remaining=core.get("remaining"),
            core_limit=core.get("limit"),
            core_reset=core.get("reset"),
            search_remaining=search.get("remaining"),
            search_limit=search.get("limit"),
            search_reset=search.get("reset"),
            graphql_remaining=graphql.get("remaining"),
            graphql_limit=graphql.get("limit"),
            graphql_reset=graphql.get("reset"),
        )

    def get_token_scopes(self) -> list[str]:
        """Return OAuth scopes for the configured token (if available)."""
        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code = None
        scopes: list[str] = []
        try:
            response = self._client.request(
                "GET",
                "/user",
                headers=self._auth_headers(),
            )
            status_code = response.status_code
            response_text = response.text
            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                summary = _summarize_github_error(response_text)
                detail = f" — {summary}" if summary else ""
                raise GitHubHttpError(
                    f"GitHub GET /user failed: {status_code}{detail}",
                    method="GET",
                    url=str(response.url),
                    status_code=status_code,
                    response_text=response_text,
                )
            header = response.headers.get("X-OAuth-Scopes", "")
            scopes = [scope.strip() for scope in header.split(",") if scope.strip()]
            return scopes
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            gh_audit.record(
                args=["GET", "/user"],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller="get_token_scopes",
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=0,
                full_scan=False,
            )

    # -------------------- Helpers --------------------

    def _get_milestone_number(self, title: str) -> int | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/milestones",
            params={"state": "all", "per_page": 100},
            caller="list_milestones",
        )
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("title") == title:
                return item.get("number")
        return None


def validate_github_token(
    token: str | None = None,
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
    repo: str | None = None,
    api_url: str = "https://api.github.com",
    timeout_seconds: float = 10.0,
) -> TokenValidationResult:
    """Validate a GitHub token by calling the API.

    Token source resolution lives in tokens.py; validation stays here because
    this module owns GitHub HTTP transport and import-linter allows httpx here.
    """
    try:
        auth = (
            GitHubAuth(
                token_provider=StaticGitHubTokenProvider(token),
                source_descriptions=("provided token",),
                api_url=api_url,
                repo=repo,
            )
            if token is not None
            else build_github_auth(
                configured_token=configured_token,
                configured_env=configured_env,
                configured_keyring_service=configured_keyring_service,
                configured_keyring_username=configured_keyring_username,
                configured_app_client_id=configured_app_client_id,
                configured_app_id=configured_app_id,
                configured_app_installation_id=configured_app_installation_id,
                configured_app_private_key_path=configured_app_private_key_path,
                configured_app_private_key_env=configured_app_private_key_env,
                repo=repo,
                api_url=api_url,
                timeout_seconds=timeout_seconds,
            )
        )
    except GitHubAuthError as exc:
        return TokenValidationResult(valid=False, error=str(exc))
    return auth.validate(repo=repo, timeout_seconds=timeout_seconds)


def _api_ref_path(ref: str) -> str:
    if ref.startswith("refs/"):
        return ref.removeprefix("refs/")
    return ref


def _search_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items", [])
    return items if isinstance(items, list) else []


def _count_items(payload: Any) -> int | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        if "items" in payload and isinstance(payload["items"], list):
            return len(payload["items"])
        return 1
    return None


def _is_full_scan(method: str, path: str) -> bool:
    if method.upper() != "GET":
        return False
    return path.startswith("/repos/") and (path.endswith("/issues") or path.endswith("/pulls") or path.endswith("/milestones"))


__all__ = [
    "GitHubAppInstallationTokenProvider",
    "GitHubAuthError",
    "GitHubHttpClient",
    "GitHubHttpConfig",
    "GitHubHttpError",
    "GitHubRateLimitSnapshot",
    "GitHubTransportError",
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "TokenValidationResult",
    "build_github_token_provider",
    "clear_keyring_token",
    "describe_github_token_sources",
    "resolve_github_token",
    "store_keyring_token",
    "validate_github_token",
]
