"""Unit tests for the exchange-respond CLI payload construction.

Network delivery is exercised end-to-end by the integration suite; here we
pin the payload shaping and validation, which is the part that has to match
the orchestrator's verdict parser exactly.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.entrypoints.cli_tools.exchange_respond import (
    _api_port,
    _deliver,
    build_parser,
    build_verdict,
)


def _parse(argv: list[str]):
    return build_parser().parse_args(argv)


def _wire(argv: list[str]) -> dict:
    return dict(build_verdict(_parse(argv)).to_wire())


class TestBuildVerdict:
    def test_minimal_ok(self) -> None:
        assert _wire(["ok", "--text", "Looks good."]) == {
            "response_type": "ok",
            "response_text": "Looks good.",
        }

    def test_getting_closer_flag(self) -> None:
        verdict = build_verdict(
            _parse(["changes_requested", "--text", "Fix X.", "--getting-closer"])
        )
        assert verdict.getting_closer is True

    def test_not_getting_closer_flag(self) -> None:
        verdict = build_verdict(
            _parse(["disagree", "--text", "Wrong.", "--not-getting-closer"])
        )
        assert verdict.getting_closer is False

    def test_decision_json_merged_under_decision_key(self) -> None:
        verdict = build_verdict(
            _parse(
                [
                    "changes_requested",
                    "--text",
                    "See F1.",
                    "--decision-json",
                    '{"verdict":"changes_requested","risk":"medium"}',
                ]
            )
        )
        assert verdict.decision == {
            "verdict": "changes_requested",
            "risk": "medium",
        }

    def test_full_json_overrides_positional_form(self) -> None:
        assert _wire(["--json", '{"response_type":"ok","response_text":"hi"}']) == {
            "response_type": "ok",
            "response_text": "hi",
        }

    def test_text_required_without_full_json(self) -> None:
        with pytest.raises(ValueError, match="--text is required"):
            build_verdict(_parse(["ok"]))

    def test_response_type_required_without_full_json(self) -> None:
        with pytest.raises(ValueError, match="response_type is required"):
            build_verdict(_parse(["--text", "orphan text"]))

    def test_full_json_must_be_object(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            build_verdict(_parse(["--json", "[1, 2, 3]"]))

    def test_full_json_must_be_valid_json(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            build_verdict(_parse(["--json", "{not json}"]))

    def test_decision_json_must_be_object(self) -> None:
        with pytest.raises(ValueError, match="--decision-json must be a JSON object"):
            build_verdict(
                _parse(["ok", "--text", "t", "--decision-json", '"a string"'])
            )


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    def read(self) -> bytes:
        return self._body


def test_deliver_reports_malformed_success_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def _urlopen(_req, timeout):  # noqa: ANN001, ANN202
        assert timeout == 60
        return _Response(b"not-json")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)

    accepted, message = _deliver("key", "8765", build_verdict(_parse(["ok", "--text", "x"])))

    assert accepted is False
    assert "malformed response JSON" in message


class TestApiPortResolution:
    """``control_api_port: 0`` means "bind any free port".

    A literal ``"0"`` in the environment is therefore a request, never a
    reachable destination. Because ``"0"`` is a truthy string, the old
    ``or`` chain returned it and the CLI dialled ``http://localhost:0``,
    shadowing the live port the review exchange injects as
    ``ORCHESTRATOR_API_PORT``. That made every verdict undeliverable
    (#6913).
    """

    def test_sentinel_zero_falls_through_to_live_port(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        assert _api_port() == "59957"

    def test_sentinel_zero_alone_is_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "0")
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert _api_port() is None

    def test_real_prefixed_port_still_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "8080")
        monkeypatch.setenv("ORCHESTRATOR_API_PORT", "59957")
        assert _api_port() == "8080"

    def test_unset_everywhere_is_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ISSUE_ORCHESTRATOR_API_PORT", raising=False)
        monkeypatch.delenv("ORCHESTRATOR_API_PORT", raising=False)
        assert _api_port() is None
