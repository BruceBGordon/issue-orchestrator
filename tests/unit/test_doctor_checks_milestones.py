"""Unit tests for milestone doctor checks."""

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import milestones as milestone_checks


def test_check_milestone_order_skips_when_empty():
    cfg = Config()
    cfg.milestone_order = []

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks == []


def test_check_milestone_order_errors_without_repo(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.repo = None

    def _raise_repo_error():
        raise milestone_checks.GitRepoError("missing")

    monkeypatch.setattr(milestone_checks, "get_repo_from_git", _raise_repo_error)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "error"
    assert "milestones.order" in checks[0].detail


def test_check_milestone_order_errors_when_missing(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1", "M2"]
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())

    class _Client:
        def __init__(self, _config):
            pass

        def list_milestones(self, state="open"):
            assert state == "open"
            return [{"title": "M1", "number": 1}]

        def close(self):
            pass

    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _Client)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "error"
    assert "M2" in checks[0].detail


def test_check_milestone_order_ok_when_all_found(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())

    class _Client:
        def __init__(self, _config):
            pass

        def list_milestones(self, state="open"):
            return [{"title": "M1", "number": 1}]

        def close(self):
            pass

    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _Client)

    checks = milestone_checks.check_milestone_order(cfg)

    assert checks[0].status == "ok"


def _client_returning(titles):
    class _Client:
        def __init__(self, _config):
            pass

        def list_milestones(self, state="open"):
            assert state == "open"
            return [{"title": t, "number": i} for i, t in enumerate(titles, 1)]

        def close(self):
            pass

    return _Client


def test_check_foundation_milestone_ok_when_exact_match(monkeypatch):
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["M0", "M1"]))

    checks = milestone_checks.check_foundation_milestone(cfg)

    assert checks[0].status == "ok"


def test_check_foundation_milestone_warns_and_suggests_on_suffix_mismatch(monkeypatch):
    """The realistic failure: config says 'M0', the milestone is 'M0 - Foundation'.

    Exact-equality scoping means this silently blocks every cross-milestone
    dependency on the intended foundation, so the check must both fire and name
    the milestone the user probably meant.
    """
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(
        milestone_checks, "GitHubHttpClient",
        _client_returning(["M0 - Foundation", "M1 - Surfaces"]),
    )

    checks = milestone_checks.check_foundation_milestone(cfg)

    assert checks[0].status == "warning"
    assert "M0 - Foundation" in checks[0].detail
    assert "Did you mean" in checks[0].detail
    assert checks[0].expandable["suggestion"] == "M0 - Foundation"


def test_check_foundation_milestone_warns_without_suggestion_when_nothing_close(monkeypatch):
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["Backlog", "Icebox"]))

    checks = milestone_checks.check_foundation_milestone(cfg)

    assert checks[0].status == "warning"
    assert "Did you mean" not in checks[0].detail
    assert checks[0].expandable["suggestion"] is None


def test_check_foundation_milestone_skips_when_repo_has_no_milestones(monkeypatch):
    """A repo that does not use milestones cannot be affected, so stay quiet."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning([]))

    assert milestone_checks.check_foundation_milestone(cfg) == []


def test_check_foundation_milestone_silent_without_repo(monkeypatch):
    cfg = Config()
    cfg.repo = None
    cfg.foundation_milestone = "M0"

    def _raise_repo_error():
        raise milestone_checks.GitRepoError("missing")

    monkeypatch.setattr(milestone_checks, "get_repo_from_git", _raise_repo_error)

    assert milestone_checks.check_foundation_milestone(cfg) == []


def test_check_foundation_milestone_silent_on_auth_error(monkeypatch):
    """Auth problems are reported by the GitHub checks; do not duplicate them."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    def _raise_auth(**_kw):
        raise milestone_checks.GitHubAuthError("no token")

    monkeypatch.setattr(milestone_checks, "build_github_auth", _raise_auth)

    assert milestone_checks.check_foundation_milestone(cfg) == []
