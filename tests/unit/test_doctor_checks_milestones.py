"""Unit tests for milestone doctor checks."""

import pytest

from issue_orchestrator.adapters.github.errors import (
    GitHubHttpError,
    GitHubTransportError,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.doctor.checks import milestones as milestone_checks


def _client_returning(titles, *, record=None):
    """Client stub whose all-milestones read returns *titles*.

    Mirrors the real contract: ``list_all_milestones`` is the exhaustive,
    all-state reader, so the stub deliberately exposes only that method.
    """
    class _Client:
        def __init__(self, _config):
            pass

        def list_all_milestones(self):
            if record is not None:
                record.append("list_all_milestones")
            return [{"title": t, "number": i} for i, t in enumerate(titles, 1)]

        def close(self):
            pass

    return _Client


def _client_raising(exc):
    class _Client:
        def __init__(self, _config):
            pass

        def list_all_milestones(self):
            raise exc

        def close(self):
            pass

    return _Client


def _by_name(checks, name):
    return next((c for c in checks if c.name == name), None)


# --------------------------------------------------------------- order rule

def test_skips_entirely_when_nothing_configured():
    cfg = Config()
    cfg.milestone_order = []
    cfg.foundation_milestone = ""

    assert milestone_checks.check_milestones(cfg) == []


def test_order_errors_without_repo(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.foundation_milestone = ""
    cfg.repo = None

    def _raise_repo_error():
        raise milestone_checks.GitRepoError("missing")

    monkeypatch.setattr(milestone_checks, "get_repo_from_git", _raise_repo_error)

    checks = milestone_checks.check_milestones(cfg)

    assert checks[0].status == "error"
    assert "without a repository" in checks[0].detail


def test_order_errors_when_missing(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1", "M2"]
    cfg.foundation_milestone = ""
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["M1"]))

    checks = milestone_checks.check_milestones(cfg)

    assert checks[0].status == "error"
    assert "M2" in checks[0].detail


def test_order_ok_when_all_found(monkeypatch):
    cfg = Config()
    cfg.milestone_order = ["M1"]
    cfg.foundation_milestone = ""
    cfg.repo = "owner/repo"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["M1", "M2"]))

    checks = milestone_checks.check_milestones(cfg)

    assert checks[0].status == "ok"


# ---------------------------------------------------------- foundation rule

def test_foundation_ok_when_exact_match(monkeypatch):
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["M0", "M1"]))

    assert _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone").status == "ok"


def test_foundation_ok_when_milestone_is_closed(monkeypatch):
    """A CLOSED foundation is still valid.

    Milestone scope is evaluated before issue state, so dependency edges into a
    closed foundation still resolve. Reading only open milestones would report a
    correct configuration as broken.
    """
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    class _ClosedOnly:
        def __init__(self, _config):
            pass

        def list_all_milestones(self):
            return [{"title": "M0", "number": 1, "state": "closed"}]

        def close(self):
            pass

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _ClosedOnly)

    assert _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone").status == "ok"


def test_foundation_found_beyond_first_page(monkeypatch):
    """The reader is exhaustive, so a title on page 2 must still be found."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0 - Foundation"

    # 100 fillers (a full first page) followed by the real foundation.
    titles = [f"Filler {i}" for i in range(100)] + ["M0 - Foundation"]

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(titles))

    assert _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone").status == "ok"


def test_foundation_warns_and_suggests_on_suffix_mismatch(monkeypatch):
    """The realistic failure: config says 'M0', the milestone is 'M0 - Foundation'."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(
        milestone_checks, "GitHubHttpClient",
        _client_returning(["M0 - Foundation", "M1 - Surfaces"]),
    )

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert "Did you mean 'M0 - Foundation'?" in check.detail
    assert check.expandable["suggestion"] == "M0 - Foundation"


def test_foundation_presents_ambiguity_instead_of_guessing(monkeypatch):
    """Several prefix matches must not become a confident wrong recommendation."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(
        milestone_checks, "GitHubHttpClient",
        _client_returning(["M0 - Foundation", "M0 - Platform"]),
    )

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert "Did you mean" not in check.detail
    assert "Closest matches:" in check.detail
    assert check.expandable["suggestion"] is None
    assert check.expandable["ambiguous_candidates"] == ["M0 - Foundation", "M0 - Platform"]


def test_foundation_suggestion_is_deterministic_across_set_orderings(monkeypatch):
    """Set iteration order must not leak into the suggestion."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    results = set()
    for ordering in (
        ["M0 - Foundation", "M0 - Platform", "Z"],
        ["Z", "M0 - Platform", "M0 - Foundation"],
        ["M0 - Platform", "Z", "M0 - Foundation"],
    ):
        monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(ordering))
        check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")
        results.add((check.expandable["suggestion"], tuple(check.expandable["ambiguous_candidates"])))

    assert len(results) == 1


def test_foundation_warns_without_suggestion_when_nothing_close(monkeypatch):
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["Backlog", "Icebox"]))

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert "Did you mean" not in check.detail
    assert check.expandable["suggestion"] is None


def test_foundation_silent_when_repo_has_no_milestones(monkeypatch):
    """A repo that does not use milestones cannot be affected, so stay quiet."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning([]))

    assert milestone_checks.check_milestones(cfg) == []


# ------------------------------------------------------------ failure paths

@pytest.mark.parametrize("exc", [
    GitHubHttpError("boom", method="GET", url="/milestones", status_code=500),
    GitHubTransportError("network down", method="GET", url="/milestones"),
])
def test_expected_github_failures_are_reported_not_swallowed(monkeypatch, exc):
    """Returning [] would be indistinguishable from 'configuration is fine'."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_raising(exc))

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert "Could not verify" in check.detail


def test_auth_error_is_reported(monkeypatch):
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    def _raise_auth(**_kw):
        raise milestone_checks.GitHubAuthError("no token")

    monkeypatch.setattr(milestone_checks, "build_github_auth", _raise_auth)

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert "no token" in check.detail


def test_unexpected_exception_propagates(monkeypatch):
    """A defect in this diagnostic must not masquerade as 'check inapplicable'.

    The whole point of this check is to expose a silent failure; swallowing a
    TypeError here would make the exposer itself fail silently.
    """
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = "M0"

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(
        milestone_checks, "GitHubHttpClient",
        _client_raising(TypeError("programming error")),
    )

    with pytest.raises(TypeError):
        milestone_checks.check_milestones(cfg)


# ----------------------------------------------------------- single snapshot

def test_both_rules_share_one_fetch(monkeypatch):
    """A1: one bounded owner, one authoritative snapshot, both rules."""
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.milestone_order = ["M1"]
    cfg.foundation_milestone = "M0"

    calls: list[str] = []
    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(
        milestone_checks, "GitHubHttpClient",
        _client_returning(["M0", "M1"], record=calls),
    )

    checks = milestone_checks.check_milestones(cfg)

    assert calls == ["list_all_milestones"]
    assert _by_name(checks, "Milestone Order").status == "ok"
    assert _by_name(checks, "Foundation Milestone").status == "ok"


def test_foundation_is_not_normalized_before_comparison(monkeypatch):
    """A padded config value must NOT be reported ok against an unpadded title.

    Runtime scoping compares ``config.foundation_milestone`` by exact equality,
    so stripping here would make the doctor bless a configuration runtime
    rejects: ' M0 ' against a real 'M0' would report ok while every dependency
    edge still evaluated CROSS_MILESTONE. A diagnostic that normalises
    differently from the code it diagnoses is worse than no diagnostic.
    """
    cfg = Config()
    cfg.repo = "owner/repo"
    cfg.foundation_milestone = " M0 "

    monkeypatch.setattr(milestone_checks, "build_github_auth", lambda **_kw: object())
    monkeypatch.setattr(milestone_checks, "GitHubHttpClient", _client_returning(["M0"]))

    check = _by_name(milestone_checks.check_milestones(cfg), "Foundation Milestone")

    assert check.status == "warning"
    assert check.expandable["configured"] == " M0 "
    assert check.expandable["suggestion"] == "M0"


# ------------------------------------------- config contract (#6939 B7 / A2)

def _errs(foundation):
    cfg = Config()
    cfg.foundation_milestone = foundation
    return [e for e in cfg.validate() if "milestones.foundation" in e]


def test_config_rejects_padded_foundation_milestone():
    """Padding is a configuration error, not an alternate value.

    Runtime compares this to GitHub milestone titles by exact equality, so a
    padded value does not degrade — it silently disables the foundation
    exemption and blocks every cross-milestone dependent.
    """
    assert any("whitespace" in e for e in _errs(" M0 "))
    assert any("whitespace" in e for e in _errs("M0\t"))


def test_config_rejects_blank_or_missing_foundation_milestone():
    for value in ("", "   ", None):
        errors = _errs(value)
        assert errors, f"expected an error for {value!r}"
        assert any("non-empty" in e for e in errors)


def test_config_accepts_interior_spacing():
    """'M0 - Foundation' is an ordinary title; only padding is rejected."""
    assert _errs("M0 - Foundation") == []
    assert _errs("M0") == []


@pytest.mark.parametrize("literal, kind", [
    ("123", "int"),
    ("1.5", "float"),
    ("true", "bool"),
    ("[a, b]", "list"),
    ("{k: v}", "dict"),
])
def test_yaml_non_string_foundation_is_reported_not_crashed(tmp_path, literal, kind):
    """A wrong TYPE must be reported, not raised.

    Config.load keeps the raw YAML scalar, so `foundation: 123` reaches the
    validator as an int. Calling .strip() on it raised AttributeError out of
    Config.validate() — the validator crashing on exactly the input it exists to
    reject, which takes down every other check in the same pass.
    """
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(
        "repo:\n"
        "  name: owner/repo\n"
        "agents:\n"
        "  agent:backend:\n"
        "    prompt: p.md\n"
        "    provider: claude-code\n"
        "milestones:\n"
        f"  foundation: {literal}\n"
    )

    cfg = Config.load(cfg_path)

    assert not isinstance(cfg.foundation_milestone, str)
    errors = cfg.validate()  # must not raise
    assert any("milestones.foundation" in e and "must be a string" in e for e in errors)


def test_config_reports_non_string_foundation_without_raising():
    for value in (123, 1.5, True, ["a"], {"k": "v"}, object()):
        errors = _errs(value)
        assert errors, f"expected an error for {value!r}"
        assert any("must be a string" in e for e in errors)


def test_yaml_loaded_padded_foundation_is_rejected(tmp_path):
    """The contract holds through real YAML loading, not just attribute set."""
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(
        "repo:\n"
        "  name: owner/repo\n"
        "agents:\n"
        "  agent:backend:\n"
        "    prompt: p.md\n"
        "    provider: claude-code\n"
        "milestones:\n"
        '  foundation: "  M0  "\n'
    )

    cfg = Config.load(cfg_path)

    assert cfg.foundation_milestone == "  M0  "  # not silently trimmed on load
    assert any(
        "milestones.foundation" in e and "whitespace" in e for e in cfg.validate()
    )
