"""Unit tests for the exchange-respond CLI payload construction.

Network delivery is exercised end-to-end by the integration suite; here we
pin the payload shaping and validation, which is the part that has to match
the orchestrator's verdict parser exactly.
"""

from __future__ import annotations

import pytest

from issue_orchestrator.entrypoints.cli_tools.exchange_respond import (
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
