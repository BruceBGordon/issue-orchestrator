"""Unit-level coverage for ``PersistentReviewExchangeRunner``.

Pins the contract the adapter has with ``run_persistent_session_exchange``
in B2:

- ``resolve_current_branch`` is called once per ``run`` to capture the
  coder's branch name (used by the inner round-loop's fast-forward).
- A ``reviewer_worktree_factory`` callable is passed to the inner
  runner — invoked at most once per pair, only on a registry cache
  miss inside ``run_persistent_session_exchange``'s spawn closure.
- ``coder_branch`` is threaded so the inner runner can fast-forward
  the reviewer worktree at the start of every reviewer round.
- The registry and ``session_output`` are passed through unchanged, while
  ``persistent_pair_root`` is derived from the coder worktree.

End-to-end behaviour against the real PTY runner is covered in
``tests/integration/test_persistent_review_exchange_integration.py``;
this file focuses on the adapter's policy on top of those helpers.

In B1 this file additionally pinned reviewer-worktree
creation/removal *inside* the runner. B2 ADR 0026 moved that
ownership: creation goes into the spawn closure (called only on
cache miss), removal goes into the registry's ``on_release`` hook
fired at issue-completion / reset / shutdown sites. The
corresponding "remove on every exit" assertions are gone; lifecycle
release is covered by ``test_persistent_exchange_pair_registry_inmemory``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.review_exchange import ReviewExchangeOutcome
from issue_orchestrator.domain.review_exchange_run import (
    ReviewExchangeRun,
    ReviewExchangeRunAssets,
)
from issue_orchestrator.domain.review_exchange_summary import ReviewExchangeSummaryV1
from issue_orchestrator.domain.runtime_config import RuntimeConfigReference
from issue_orchestrator.execution import persistent_review_exchange_runner as prer


@pytest.fixture
def stub_lifecycle(monkeypatch, tmp_path):
    """Replace the reviewer-worktree helpers with no-op stubs that record calls.

    Only ``resolve_current_branch`` and ``create_reviewer_worktree``
    are exposed on the runner module in B2 — fast-forward and remove
    moved to ``persistent_session_exchange`` and the registry's
    ``on_release`` hook respectively.
    """
    calls: dict[str, list[Any]] = {
        "create": [],
        "resolve_branch": [],
    }

    def _resolve_branch(wt: Path) -> str:
        calls["resolve_branch"].append(wt)
        return "feature/test"

    def _create(*, coder_worktree, coder_branch, timestamp):
        calls["create"].append(
            {
                "coder_worktree": coder_worktree,
                "coder_branch": coder_branch,
                "timestamp": timestamp,
            }
        )
        return SimpleNamespace(
            path=tmp_path / "reviewer-wt",
            coder_branch=coder_branch,
        )

    monkeypatch.setattr(prer, "resolve_current_branch", _resolve_branch)
    monkeypatch.setattr(prer, "create_reviewer_worktree", _create)
    return calls


def _make_agent(
    tmp_path: Path,
    *,
    provider: str | None = None,
    ai_system: str = "claude-code",
    provider_args: dict[str, Any] | None = None,
) -> AgentConfig:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello")
    return AgentConfig(
        prompt_path=prompt,
        provider=provider,
        ai_system=ai_system,
        provider_args=provider_args or {},
        command="echo",
    )


def _make_exchange_run(tmp_path: Path) -> ReviewExchangeRun:
    run_dir = tmp_path / ".issue-orchestrator" / "sessions" / "r1__review-exchange-42"
    return ReviewExchangeRun(
        session_name="review-exchange-42",
        run_id="r1",
        parent_session_name="coding-42",
        assets=ReviewExchangeRunAssets.from_run_dir(run_dir),
    )


def _runtime_config(tmp_path: Path) -> RuntimeConfigReference:
    config_path = tmp_path / "issue-orchestrator.test.yaml"
    if not config_path.exists():
        config_path.write_text(
            "validation:\n  quick:\n    cmd: 'true'\n", encoding="utf-8"
        )
    return RuntimeConfigReference.from_path(config_path)


def _canned_outcome(exchange_run: ReviewExchangeRun) -> ReviewExchangeOutcome:
    return ReviewExchangeOutcome(
        status="ok",
        rounds=2,
        reason="reviewer_ok",
        run_assets=exchange_run.assets,
        reviewer_response=None,
        summary=ReviewExchangeSummaryV1.from_payload(
            {
                "status": "ok",
                "reason": "reviewer_ok",
                "completed_rounds": 2,
                "response_text": None,
                "timestamp": "2026-02-01T00:00:00+00:00",
            }
        ),
    )


def _run(
    runner,
    tmp_path,
    *,
    coder_agent: AgentConfig | None = None,
    reviewer_agent: AgentConfig | None = None,
):
    exchange_run = _make_exchange_run(tmp_path)
    return runner.run(
        exchange_run=exchange_run,
        coder_worktree=tmp_path / "coder",
        issue_number=42,
        issue_title="t",
        coder_label="agent:coder",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent or _make_agent(tmp_path),
        reviewer_agent=reviewer_agent or _make_agent(tmp_path),
        runtime_config=_runtime_config(tmp_path),
        max_rounds=3,
        max_no_progress=3,
        require_validation=False,
    )


def _make_runner(tmp_path: Path) -> "prer.PersistentReviewExchangeRunner":
    return prer.PersistentReviewExchangeRunner(
        MagicMock(name="session_output"),
        MagicMock(name="pair_registry"),
    )


def test_response_channel_for_codex_workspace_write_uses_file(tmp_path: Path) -> None:
    agent = _make_agent(
        tmp_path,
        provider="codex",
        ai_system="codex",
        provider_args={
            "approval_mode": "full-auto",
            "sandbox": "workspace-write",
        },
    )

    assert prer.response_channel_for_agent(agent) == "file"


def test_response_channel_for_codex_explicit_workspace_write_uses_file(
    tmp_path: Path,
) -> None:
    agent = _make_agent(
        tmp_path,
        provider="codex",
        ai_system="codex",
        provider_args={
            "approval_mode": "default",
            "sandbox": "workspace-write",
        },
    )

    assert prer.response_channel_for_agent(agent) == "file"


def test_response_channel_for_codex_read_only_uses_file(tmp_path: Path) -> None:
    agent = _make_agent(
        tmp_path,
        provider="codex",
        ai_system="codex",
        provider_args={
            "approval_mode": "default",
            "sandbox": "read-only",
        },
    )

    assert prer.response_channel_for_agent(agent) == "file"


def test_response_channel_for_codex_yolo_uses_mailbox(tmp_path: Path) -> None:
    agent = _make_agent(
        tmp_path,
        provider="codex",
        ai_system="codex",
        provider_args={
            "approval_mode": "yolo",
            "sandbox": "danger-full-access",
        },
    )

    assert prer.response_channel_for_agent(agent) == "mailbox"


def test_run_passes_per_agent_response_channels(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = prer.PersistentReviewExchangeRunner(
        MagicMock(name="session_output"),
        MagicMock(name="pair_registry"),
        turn_mailbox=MagicMock(name="turn_mailbox"),
    )
    reviewer = _make_agent(
        tmp_path,
        provider="codex",
        ai_system="codex",
        provider_args={
            "approval_mode": "full-auto",
            "sandbox": "workspace-write",
        },
    )

    _run(runner, tmp_path, reviewer_agent=reviewer)

    channels = captured["response_channels"]
    assert channels.coder == "mailbox"
    assert channels.reviewer == "file"


def test_run_uses_file_channels_when_no_mailbox_is_wired(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = _make_runner(tmp_path)

    _run(runner, tmp_path)

    channels = captured["response_channels"]
    assert channels.coder == "file"
    assert channels.reviewer == "file"


def test_run_threads_pair_registry_and_persistent_root_into_inner_runner(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
):
    """The registry-owned pair lifecycle (B2 ADR 0026) hangs on the
    inner runner receiving the registry and pair-state root through
    every call. Pair filesystem state is worktree-scoped so deleting
    the issue worktree clears attempt-authoritative pair artifacts."""
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = _make_runner(tmp_path)

    outcome = _run(runner, tmp_path)

    assert outcome.status == "ok"
    assert captured["pair_registry"] is runner._pair_registry  # noqa: SLF001
    assert (
        captured["persistent_pair_root"]
        == tmp_path / "coder" / ".issue-orchestrator" / "persistent-pairs"
    )
    assert captured["coder_worktree_path"] == tmp_path / "coder"
    assert captured["session_output"] is runner._session_output  # noqa: SLF001
    assert captured["runtime_config"] == _runtime_config(tmp_path)


def test_run_resolves_coder_addendum_for_coder_worktree_only(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    provider = MagicMock(name="coder_prompt_addendum")
    provider.for_worktree.return_value = "INTERNAL-CODER-ONLY"
    runner = prer.PersistentReviewExchangeRunner(
        MagicMock(name="session_output"),
        MagicMock(name="pair_registry"),
        coder_prompt_addendum=provider,
    )

    _run(runner, tmp_path)

    provider.for_worktree.assert_called_once_with(tmp_path / "coder")
    assert captured["coder_prompt_addendum"] == "INTERNAL-CODER-ONLY"


def test_persistent_pair_root_helper_is_worktree_scoped(tmp_path: Path) -> None:
    coder_worktree = tmp_path / "coder"

    assert prer.persistent_pair_root_for_worktree(coder_worktree) == (
        coder_worktree / ".issue-orchestrator" / "persistent-pairs"
    )


def test_job_timeout_budget_includes_coder_protocol_retries(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("hello")
    coder = AgentConfig(prompt_path=prompt, timeout_minutes=10)
    reviewer = AgentConfig(prompt_path=prompt, timeout_minutes=10)
    runner = _make_runner(tmp_path)

    timeout = runner.job_timeout_seconds(
        coder_agent=coder,
        reviewer_agent=reviewer,
        max_rounds=1,
    )

    # One round can legitimately consume reviewer + coder + two coder
    # protocol retries before returning a protocol outcome. The supervisor
    # deadline should sit outside that runner-owned budget, with only a small
    # cleanup/drain grace window after it.
    max_legitimate_protocol_retry_duration = (10 * 60) + (3 * 10 * 60)
    assert timeout == max_legitimate_protocol_retry_duration + 300


def test_run_passes_reviewer_worktree_factory_invoked_lazily(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
):
    """The factory is the seam B2 uses to keep worktree creation lazy:
    the inner runner only invokes it on a registry cache miss inside
    its spawn closure. So the factory must be a *callable*, not a
    pre-resolved path; and ``create_reviewer_worktree`` must NOT
    have been called yet at the moment ``run`` returns control to
    the inner runner.
    """
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        # The fake inner runner deliberately does NOT call the factory;
        # we want to assert the runner passed a factory, not a path,
        # and that no worktree was created prematurely.
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = _make_runner(tmp_path)

    _run(runner, tmp_path)

    factory = captured["reviewer_worktree_factory"]
    assert callable(factory), "reviewer_worktree_factory must be a callable"
    assert stub_lifecycle["create"] == [], (
        "create_reviewer_worktree must not have been called yet — "
        "the inner runner only invokes the factory on a cache miss"
    )

    # Now invoke the factory and confirm the worktree gets created
    # with the right inputs.
    path = factory()
    assert path == tmp_path / "reviewer-wt"
    assert len(stub_lifecycle["create"]) == 1
    assert stub_lifecycle["create"][0]["coder_worktree"] == tmp_path / "coder"
    assert stub_lifecycle["create"][0]["coder_branch"] == "feature/test"


def test_run_threads_coder_branch_for_inner_fast_forward(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
):
    """The inner round-loop fast-forwards the reviewer worktree at the
    start of every reviewer round (including round 1 of any
    second-or-later exchange) using the coder's branch name. The
    runner is the only place that knows the branch, so it must
    thread it through; otherwise B2's "always FF" contract silently
    becomes a no-op on the cached-pair second-exchange path.
    """
    captured: dict[str, Any] = {}

    def _fake_inner(**kwargs):
        captured.update(kwargs)
        return _canned_outcome(kwargs["exchange_run"])

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _fake_inner)
    runner = _make_runner(tmp_path)

    _run(runner, tmp_path)

    assert captured["coder_branch"] == "feature/test"
    assert stub_lifecycle["resolve_branch"] == [tmp_path / "coder"]


def test_run_propagates_inner_exceptions_without_releasing_pair(
    monkeypatch,
    tmp_path: Path,
    stub_lifecycle,
):
    """A mid-exchange failure must NOT release the registry pair —
    that's the user-visible "1 process for the lifetime of the
    exchanges" contract from ADR 0026. Lifecycle release happens at
    issue-completion / reset / shutdown sites, not on every error
    path. (B1 had the opposite invariant; B2 inverts it.)
    """

    def _explode(**_):
        raise RuntimeError("simulated runner failure")

    monkeypatch.setattr(prer, "run_persistent_session_exchange", _explode)
    runner = _make_runner(tmp_path)

    with pytest.raises(RuntimeError, match="simulated runner failure"):
        _run(runner, tmp_path)

    # Registry must be untouched by the runner. The on-release hook
    # / reset path / shutdown_all are the canonical owners of the
    # pair's death.
    assert not runner._pair_registry.release.called  # noqa: SLF001
    assert not runner._pair_registry.shutdown_all.called  # noqa: SLF001
