"""Tests for deterministic session run artifact resolution."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.session_run_resolution import (
    resolve_run_dir,
    resolve_session_run_dir,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.domain.session_run import (
    SessionRunAssets,
    canonical_run_dir_name,
)
from issue_orchestrator.ports.session_output import SessionOutput
from tests.unit.session_run_helpers import make_session_run_assets


def _session(
    tmp_path: Path,
    run_dir: Path,
    *,
    completion_path: str = ".issue-orchestrator/completion.json",
) -> Session:
    run_id, session_name = run_dir.name.split("__", 1)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("prompt", encoding="utf-8")
    issue = Issue(number=123, title="Test issue", labels=["agent:test"])
    return Session(
        key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
        issue=issue,
        agent_config=AgentConfig(prompt_path=prompt_path, model="sonnet"),
        terminal_id="issue-123",
        worktree_path=tmp_path / "worktree",
        branch_name="123-test",
        completion_path=completion_path,
        run_assets=make_session_run_assets(
            run_dir.parents[2],
            run_id=run_id,
            session_name=session_name,
        ),
    )


def test_recorded_run_dir_is_authoritative(tmp_path: Path) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    run_dir.mkdir(parents=True)
    session = _session(tmp_path, run_dir)
    session_output = Mock(spec=SessionOutput)

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    session_output.find_run_dir.assert_not_called()
    session_output.read_manifest.assert_not_called()


def test_missing_recorded_run_dir_still_prevents_discovery_fallback(
    tmp_path: Path,
) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )
    session = _session(tmp_path, run_dir)
    session_output = Mock(spec=SessionOutput)

    resolved = resolve_session_run_dir(session_output, session)

    assert resolved == run_dir
    session_output.find_run_dir.assert_not_called()
    session_output.read_manifest.assert_not_called()


def test_active_session_requires_run_dir_at_construction(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        Session(  # type: ignore[call-arg]
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=Issue(number=123, title="Test issue", labels=["agent:test"]),
            agent_config=AgentConfig(prompt_path=tmp_path / "prompt.md", model="sonnet"),
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-test",
        )


def test_resolve_run_dir_has_no_discovery_fallback(
    tmp_path: Path,
) -> None:
    run_dir = (
        tmp_path
        / "worktree"
        / ".issue-orchestrator"
        / "sessions"
        / "20260525__coding-1"
    )

    run_id, session_name = run_dir.name.split("__", 1)
    run_assets = make_session_run_assets(
        run_dir.parents[2],
        run_id=run_id,
        session_name=session_name,
    )

    resolved = resolve_run_dir(
        session_name="issue-123",
        recorded_run_assets=run_assets,
    )

    assert resolved == run_assets.run_dir


def test_manifest_run_dir_must_match_injected_run_dir(tmp_path: Path) -> None:
    run_assets = make_session_run_assets(tmp_path / "worktree")
    manifest = json.loads(run_assets.manifest_path.read_text(encoding="utf-8"))
    wrong_run_dir = run_assets.run_dir.parent / "20260525__wrong-session"
    wrong_run_dir.mkdir()

    with pytest.raises(ValueError, match="run_dir mismatch"):
        SessionRunAssets.from_manifest_payload(
            run_dir=wrong_run_dir,
            manifest=manifest,
        )


def test_a_tampered_manifest_identity_is_refused_at_restoration(tmp_path: Path) -> None:
    """Restart restoration reloads ``run_id``/``session_name`` from the WORKTREE's
    manifest, which the agent can write (#6858 round 7 F17).

    Downstream owners trust the two halves for different things — the tech-lead
    archive names its durable destination from the identity while reading bytes
    from the directory — so an identity that disagrees with the directory it was
    loaded from would let one run's evidence be filed under another run's receipt,
    leave the real activity row stuck at Running, and never conclude it. The
    restorer skips a session whose assets raise, so refusing here is what turns a
    rewritten manifest into a skipped session rather than a crossed one.
    """
    run_assets = make_session_run_assets(tmp_path / "worktree")
    manifest = json.loads(run_assets.manifest_path.read_text(encoding="utf-8"))
    manifest["session_name"] = "rework-9"

    with pytest.raises(ValueError, match="does not name run"):
        SessionRunAssets.from_manifest_payload(
            run_dir=run_assets.run_dir,
            manifest=manifest,
        )


def test_a_tampered_manifest_run_id_is_refused_at_restoration(tmp_path: Path) -> None:
    run_assets = make_session_run_assets(tmp_path / "worktree")
    manifest = json.loads(run_assets.manifest_path.read_text(encoding="utf-8"))
    manifest["run_id"] = "run-someone-elses"

    with pytest.raises(ValueError, match="does not name run"):
        SessionRunAssets.from_manifest_payload(
            run_dir=run_assets.run_dir,
            manifest=manifest,
        )


def test_a_run_directory_naming_another_run_is_refused(tmp_path: Path) -> None:
    """The binding is on the pair, not on the manifest: assets assembled directly
    with a sibling run's directory are refused the same way."""
    run_assets = make_session_run_assets(tmp_path / "worktree")
    sibling = run_assets.run_dir.parent / canonical_run_dir_name("run-2", "issue-2")
    sibling.mkdir()

    with pytest.raises(ValueError, match="does not name run"):
        SessionRunAssets.from_paths(
            session_name=run_assets.identity.session_name,
            run_id=run_assets.identity.run_id,
            worktree_path=run_assets.worktree_path,
            run_dir=sibling,
            terminal_recording_path=sibling / "terminal-recording.jsonl",
            manifest_path=sibling / "manifest.json",
            started_at=run_assets.identity.started_at,
        )
