"""The shared block is governed by the COMPOSITION, not by any one class.

The decorator's own tests prove it refuses a value it was handed. They cannot
prove the composition root hands it to everybody — and every bypass this closed
was a writer that simply never received it. A future omission in ``bootstrap``
would leave those tests green and the hole wide open (#6999 F2 round 6).

So these drive the REAL builder and the REAL public surfaces, against a
CONFIGURED label rather than the default spelling: an AST rule can see
``"blocked-needs-human"``, but nothing static can see a value that only exists
once a repo's configuration is loaded.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.control.actions import SyncLabelsAction
from issue_orchestrator.control.label_sync import DesiredLabels
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.execution import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config

#: Nothing in the codebase spells this. It exists only in the config below, so
#: a writer that refuses it can only have got it from the composition root.
CONFIGURED_BLOCK = "escalate-to-a-person"
ORDINARY = "size:small"
ISSUE = 903


@pytest.fixture
def config(tmp_path) -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path
    config.worktree_base = tmp_path / "worktrees"
    config.label_needs_human = CONFIGURED_BLOCK
    return config


@pytest.fixture
def github() -> MagicMock:
    adapter = MagicMock()
    adapter.get_issue_labels.return_value = []
    adapter.get_issue_labels_fresh.return_value = []
    return adapter


@pytest.fixture
def orchestrator(config, github):
    with patch("issue_orchestrator.entrypoints.bootstrap.install_gh_guard"):
        return build_orchestrator_for_testing(config=config, github=github)


def _added(github: MagicMock) -> list[tuple[int, str]]:
    return [(call.args[0], call.args[1]) for call in github.add_label.call_args_list]


def test_the_composed_label_manager_really_uses_the_configured_value(
    orchestrator,
) -> None:
    """Guard the guard: these tests are worthless if the config did not take."""
    assert orchestrator.deps.label_manager.needs_human == CONFIGURED_BLOCK


def test_the_composed_action_applier_refuses_the_configured_block(
    orchestrator, github
) -> None:
    """``SyncLabelsAction`` carries a planner-assembled collection.

    Its values are computed, so no spelling rule can govern them; only the
    capability the applier was composed with can.
    """
    result = orchestrator.deps.action_applier.apply(
        SyncLabelsAction(
            issue_number=ISSUE,
            add_labels=(CONFIGURED_BLOCK, ORDINARY),
            remove_labels=(),
            reason="composed sync",
        )
    )

    assert not result.success
    assert (ISSUE, CONFIGURED_BLOCK) not in _added(github)
    # ...and the ordinary label in the same collection still landed, so the
    # refusal is about the governed value and not about the action failing.
    assert (ISSUE, ORDINARY) in _added(github)


def test_the_composed_label_sync_refuses_the_configured_block(
    orchestrator, github
) -> None:
    """``LabelSync`` computes its own add/remove sets — same exposure."""
    result = orchestrator.deps.label_sync.sync(
        ISSUE,
        current=set(),
        desired=DesiredLabels.add(CONFIGURED_BLOCK, ORDINARY),
    )

    assert CONFIGURED_BLOCK in result.errors
    assert (ISSUE, CONFIGURED_BLOCK) not in _added(github)
    assert (ISSUE, ORDINARY) in _added(github)


def test_the_composed_completion_processor_rejects_it_in_pr_labels(
    orchestrator, github, tmp_path
) -> None:
    """The agent-authored surface, through the composed processor.

    Proves BOTH halves of the completion wiring at once: the block owner it
    consults at the door knows the configured label, and the record is refused
    before anything external happens.
    """
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    session_output = FileSystemSessionOutput()
    run_assets = session_output.start_run(
        worktree.resolve(),
        f"issue-{ISSUE}",
        issue_number=ISSUE,
        agent_label="agent:test",
        backend="subprocess",
    )
    (run_assets.run_dir / "completion.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "timestamp": "2026-08-08T00:00:00Z",
                "outcome": "completed",
                "summary": "done",
                "requested_actions": ["create_pr"],
                "pr_labels": [CONFIGURED_BLOCK, ORDINARY],
            }
        )
    )

    result = orchestrator.deps.completion_processor.process(
        worktree.resolve(),
        ISSUE,
        "Test issue",
        run_assets=run_assets,
        completion_path=(
            f".issue-orchestrator/sessions/{run_assets.run_dir.name}/completion.json"
        ),
        agent_label="agent:test",
    )

    assert not result.success
    assert any(CONFIGURED_BLOCK in error for error in result.errors)
    assert _added(github) == [], "rejected at the door, so nothing was labelled"
