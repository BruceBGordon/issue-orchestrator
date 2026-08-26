"""Only an escalation, at a known HEAD, may push a dirty worktree."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.dirty_escalation import dirty_escalation_env
from issue_orchestrator.domain.dirty_remediation import (
    DIRTY_ESCALATION_ENV,
    dirty_escalation_signal,
    signal_authorizes_dirty_push,
)
from issue_orchestrator.domain.models import CompletionOutcome


class _Record:
    def __init__(self, outcome):
        self.outcome = outcome
        self.session_id = "s-1"


def _git(head_sha):
    adapter = Mock()
    adapter.get_head_sha = Mock(return_value=head_sha)
    return adapter


class TestDirtyEscalationEnv:
    @pytest.mark.parametrize(
        "outcome", [CompletionOutcome.BLOCKED, CompletionOutcome.NEEDS_HUMAN]
    )
    def test_an_escalation_is_authorized_at_its_current_head(self, tmp_path, outcome):
        env = dirty_escalation_env(_git("abc123"), tmp_path, _Record(outcome))

        assert env == {
            DIRTY_ESCALATION_ENV: dirty_escalation_signal(
                str(tmp_path.resolve()), "abc123"
            )
        }

    @pytest.mark.parametrize(
        "outcome",
        [
            CompletionOutcome.COMPLETED,
            CompletionOutcome.REVIEW_APPROVED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED,
        ],
    )
    def test_no_other_outcome_is_authorized(self, tmp_path, outcome):
        assert dirty_escalation_env(_git("abc123"), tmp_path, _Record(outcome)) == {}

    def test_an_unresolvable_head_authorizes_nothing(self, tmp_path):
        """An unbound signal would authorize any commit; refuse to make one."""
        assert (
            dirty_escalation_env(
                _git(None), tmp_path, _Record(CompletionOutcome.BLOCKED)
            )
            == {}
        )

    def test_the_signal_it_issues_is_the_signal_the_hook_accepts(self, tmp_path):
        """Issuer and verifier must agree, not merely both exist."""
        env = dirty_escalation_env(
            _git("abc123"), tmp_path, _Record(CompletionOutcome.BLOCKED)
        )
        value = env[DIRTY_ESCALATION_ENV]
        worktree = str(tmp_path.resolve())

        assert signal_authorizes_dirty_push(value, worktree, "abc123")
        # ...and only that pairing.
        assert not signal_authorizes_dirty_push(value, worktree, "def456")
        assert not signal_authorizes_dirty_push(value, "/elsewhere", "abc123")
        assert not signal_authorizes_dirty_push(None, worktree, "abc123")

    def test_a_moved_head_re_derives_rather_than_reusing(self, tmp_path):
        """A rebase retry must not push under the pre-rebase authorization."""
        record = _Record(CompletionOutcome.BLOCKED)
        before = dirty_escalation_env(_git("abc123"), tmp_path, record)
        after = dirty_escalation_env(_git("def456"), tmp_path, record)

        assert before != after
        assert not signal_authorizes_dirty_push(
            before[DIRTY_ESCALATION_ENV], str(tmp_path.resolve()), "def456"
        )

    def test_nothing_is_written_to_the_worktree(self, tmp_path):
        """The decision travels as environment; it leaves no artifact to replay."""
        before = {p for p in Path(tmp_path).rglob("*")}

        dirty_escalation_env(
            _git("abc123"), tmp_path, _Record(CompletionOutcome.BLOCKED)
        )

        assert {p for p in Path(tmp_path).rglob("*")} == before
