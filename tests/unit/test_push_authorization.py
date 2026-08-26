"""The authorization must exist only while a push it describes is happening."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.control.push_authorization import authorized_push
from issue_orchestrator.domain.dirty_remediation import (
    PUSH_AUTHORIZATION_PATH,
    PushAuthorization,
)
from issue_orchestrator.domain.models import CompletionOutcome


class _Record:
    def __init__(self, outcome, session_id="s-1"):
        self.outcome = outcome
        self.session_id = session_id


def _read(worktree):
    return json.loads((worktree / PUSH_AUTHORIZATION_PATH).read_text())


class TestAuthorizedPush:
    @pytest.mark.parametrize(
        "outcome", [CompletionOutcome.BLOCKED, CompletionOutcome.NEEDS_HUMAN]
    )
    def test_an_escalation_is_authorized_for_the_body_only(self, tmp_path, outcome):
        with authorized_push(tmp_path, _Record(outcome)):
            written = _read(tmp_path)
            assert written["outcome"] == outcome.value
            assert written["worktree"] == str(tmp_path.resolve())

        assert not (tmp_path / PUSH_AUTHORIZATION_PATH).exists()

    def test_a_completed_record_is_never_authorized(self, tmp_path):
        with authorized_push(tmp_path, _Record(CompletionOutcome.COMPLETED)):
            assert not (tmp_path / PUSH_AUTHORIZATION_PATH).exists()

        assert not (tmp_path / PUSH_AUTHORIZATION_PATH).exists()

    def test_the_authorization_is_revoked_even_when_the_push_raises(self, tmp_path):
        """A crash mid-publish must not leave a standing exemption behind."""
        with pytest.raises(RuntimeError):
            with authorized_push(tmp_path, _Record(CompletionOutcome.BLOCKED)):
                assert (tmp_path / PUSH_AUTHORIZATION_PATH).exists()
                raise RuntimeError("push blew up")

        assert not (tmp_path / PUSH_AUTHORIZATION_PATH).exists()

    def test_what_is_written_actually_authorizes_the_push_it_describes(self, tmp_path):
        """The issuer and the reader must agree, not merely both exist."""
        with authorized_push(tmp_path, _Record(CompletionOutcome.BLOCKED)):
            authorization = PushAuthorization.from_dict(_read(tmp_path))
            assert authorization is not None
            issued_at = datetime.fromisoformat(authorization.issued_at)
            age = (datetime.now(timezone.utc) - issued_at).total_seconds()

            assert authorization.authorizes_dirty_push(str(tmp_path.resolve()), age)
            # ...and nothing else.
            assert not authorization.authorizes_dirty_push("/elsewhere", age)
            assert not authorization.authorizes_dirty_push(
                str(tmp_path.resolve()), timedelta(hours=2).total_seconds()
            )

    def test_an_unwritable_worktree_leaves_the_guard_armed(self, tmp_path):
        """Failing to authorize must fail closed, not raise into the publish."""
        blocked = tmp_path / "ro"
        blocked.mkdir()
        (blocked / ".issue-orchestrator").write_text("not a directory")

        with authorized_push(blocked, _Record(CompletionOutcome.BLOCKED)):
            assert not (blocked / PUSH_AUTHORIZATION_PATH).exists()
