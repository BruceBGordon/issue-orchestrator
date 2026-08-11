"""Round-trip + state-table tests for the typed turn-packet/turn-result.

The packet is the typed bundle the orchestrator threads into the
prompt builder; the result is the typed parsed agent response. Both
have manifest-symmetric ``to_manifest_fields`` / ``from_manifest``
round-trip behavior, plus state-table coverage of every parser branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.review_exchange import (
    build_coder_prompt,
    build_reviewer_prompt,
)
from issue_orchestrator.domain.review_exchange_turn import (
    ReviewExchangePromptFiles,
    ReviewExchangeTurnPacket,
    ReviewExchangeTurnResult,
    Role,
    TurnResultKind,
)


# ---------------------------------------------------------------------------
# ReviewExchangeTurnPacket — round-trip + field validation
# ---------------------------------------------------------------------------


class TestReviewExchangeTurnPacketRoundTrip:
    def test_full_round_trip_preserves_every_field(self) -> None:
        original = ReviewExchangeTurnPacket(
            issue_number=359,
            issue_title="Make it right",
            round_index=2,
            role=Role.REVIEWER,
            require_validation=True,
            run_dir=Path("/wt/.issue-orchestrator/sessions/r1"),
            prompt_files=ReviewExchangePromptFiles(
                validation_record=Path(
                    "/wt/.issue-orchestrator/sessions/r1/validation.json",
                ),
            ),
            last_coder_text="Applied the fix.",
            last_reviewer_text="Still failing on edge case.",
            reviewer_feedback=None,
            coder_prompt_addendum="Internal coder instructions",
        )
        recovered = ReviewExchangeTurnPacket.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered == original

    def test_coder_packet_with_reviewer_feedback_round_trip(self) -> None:
        original = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Add feature",
            round_index=1,
            role=Role.CODER,
            require_validation=False,
            run_dir=Path("/wt/r1"),
            reviewer_feedback="Please add tests for X.",
        )
        recovered = ReviewExchangeTurnPacket.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered == original
        assert recovered is not None
        assert recovered.role is Role.CODER

    def test_optional_fields_omitted_when_unset(self) -> None:
        packet = ReviewExchangeTurnPacket(
            issue_number=1,
            issue_title="t",
            round_index=1,
            role=Role.REVIEWER,
            require_validation=False,
            run_dir=Path("/r"),
        )
        fields = packet.to_manifest_fields()
        # Optional fields go missing rather than serialize as None — the
        # reader detects "unset" by absence (consistent with the manifest
        # header pattern).
        assert "last_coder_text" not in fields
        assert "last_reviewer_text" not in fields
        assert "reviewer_feedback" not in fields
        assert "coder_prompt_addendum" not in fields
        assert "prompt_files" not in fields

    @pytest.mark.parametrize("missing_key", [
        "issue_number",
        "issue_title",
        "round_index",
        "role",
        "require_validation",
        "run_dir",
    ])
    def test_from_manifest_returns_none_when_required_field_missing(
        self, missing_key: str,
    ) -> None:
        full = {
            "issue_number": 1,
            "issue_title": "t",
            "round_index": 1,
            "role": "reviewer",
            "require_validation": True,
            "run_dir": "/r",
        }
        del full[missing_key]
        assert ReviewExchangeTurnPacket.from_manifest(full) is None

    def test_from_manifest_returns_none_for_unknown_role(self) -> None:
        # The role enum is closed; any string outside the enum is
        # rejected wholesale rather than coerced to one of the two
        # valid values.
        assert ReviewExchangeTurnPacket.from_manifest({
            "issue_number": 1,
            "issue_title": "t",
            "round_index": 1,
            "role": "supervisor",  # not a valid Role
            "require_validation": True,
            "run_dir": "/r",
        }) is None

    def test_from_manifest_returns_none_for_wrong_typed_fields(self) -> None:
        for bad_field, bad_value in [
            ("issue_number", "not-an-int"),
            ("issue_title", 42),
            ("round_index", "1"),
            ("role", 42),
            ("require_validation", "true"),  # str, not bool
            ("run_dir", 42),
            ("run_dir", ""),  # empty string rejected too
        ]:
            full = {
                "issue_number": 1,
                "issue_title": "t",
                "round_index": 1,
                "role": "reviewer",
                "require_validation": True,
                "run_dir": "/r",
            }
            full[bad_field] = bad_value
            assert ReviewExchangeTurnPacket.from_manifest(full) is None, (
                f"expected None for bad {bad_field}={bad_value!r}"
            )

    def test_from_manifest_returns_none_for_wrong_typed_prompt_files(self) -> None:
        assert ReviewExchangeTurnPacket.from_manifest({
            "issue_number": 1,
            "issue_title": "t",
            "round_index": 1,
            "role": "reviewer",
            "require_validation": True,
            "run_dir": "/r",
            "prompt_files": {"validation_record": ""},
        }) is None

    def test_from_manifest_ignores_wrong_typed_optional_fields(self) -> None:
        # A wrong-typed optional field is treated as "unset" rather
        # than as a hard rejection of the whole packet.
        recovered = ReviewExchangeTurnPacket.from_manifest({
            "issue_number": 1,
            "issue_title": "t",
            "round_index": 1,
            "role": "reviewer",
            "require_validation": True,
            "run_dir": "/r",
            "last_coder_text": 42,  # wrong type
            "reviewer_feedback": ["a", "b"],  # wrong type
        })
        assert recovered is not None
        assert recovered.last_coder_text is None
        assert recovered.reviewer_feedback is None


# ---------------------------------------------------------------------------
# Reviewer prompt contract
# ---------------------------------------------------------------------------


class TestBuildReviewerPrompt:
    def test_coder_addendum_never_changes_reviewer_prompt(self) -> None:
        packet = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Make it right",
            round_index=1,
            role=Role.REVIEWER,
            require_validation=False,
            run_dir=Path("/wt/.issue-orchestrator/sessions/review-exchange-run"),
            coder_prompt_addendum="INTERNAL-CODER-ONLY",
        )

        prompt = build_reviewer_prompt(packet)

        assert "INTERNAL-CODER-ONLY" not in prompt

    def test_validation_note_uses_injected_validation_record_path(self) -> None:
        run_dir = Path("/wt/.issue-orchestrator/sessions/review-exchange-run")
        validation_record = Path("/explicit/artifacts/validation-record.json")
        packet = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Make it right",
            round_index=1,
            role=Role.REVIEWER,
            require_validation=True,
            run_dir=run_dir,
            prompt_files=ReviewExchangePromptFiles(
                validation_record=validation_record,
            ),
        )

        prompt = build_reviewer_prompt(packet)

        assert str(validation_record) in prompt
        assert str(run_dir / "validation-record.json") not in prompt
        assert "passed=true" in prompt
        # The reviewer must trust validation-record.json and not re-run build/
        # test tooling itself. Wrapper downloads (services.gradle.org,
        # repo1.maven.org, npm registry, ...) hang on restricted networks and
        # eat the per-round budget for no benefit.
        assert "do NOT run build, test, or validation commands" in prompt
        assert "./gradlew" in prompt

    def test_validation_required_without_injected_record_fails_fast(self) -> None:
        packet = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Make it right",
            round_index=1,
            role=Role.REVIEWER,
            require_validation=True,
            run_dir=Path("/wt/.issue-orchestrator/sessions/review-exchange-run"),
        )

        with pytest.raises(ValueError, match="prompt_files.validation_record"):
            build_reviewer_prompt(packet)


class TestBuildCoderPrompt:
    def test_internal_review_addendum_is_appended_after_outer_feedback(self) -> None:
        packet = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Make it right",
            round_index=2,
            role=Role.CODER,
            require_validation=False,
            run_dir=Path("/wt/.issue-orchestrator/sessions/review-exchange-run"),
            reviewer_feedback="External reviewer finding.",
            coder_prompt_addendum="MANDATORY-INTERNAL-REVIEW",
        )

        prompt = build_coder_prompt(packet)

        assert prompt.index("External reviewer finding.") < prompt.index(
            "MANDATORY-INTERNAL-REVIEW"
        )

    def test_dirty_worktree_prevalidation_instruction_is_explicit(self) -> None:
        packet = ReviewExchangeTurnPacket(
            issue_number=42,
            issue_title="Make it right",
            round_index=1,
            role=Role.CODER,
            require_validation=True,
            run_dir=Path("/wt/.issue-orchestrator/sessions/review-exchange-run"),
            reviewer_feedback="Fix the tests and commit the result.",
        )

        prompt = build_coder_prompt(packet)

        assert "clean working tree required" in prompt
        assert "prepush-check --dirty-only -v" in prompt
        assert "Tracked project files" in prompt
        assert ".issue-orchestrator/" in prompt
        assert "Reviewer report:" in prompt

# ---------------------------------------------------------------------------
# ReviewExchangeTurnResult.from_agent_dict — every parser branch
# ---------------------------------------------------------------------------


class TestReviewExchangeTurnResultFromAgentDict:
    def test_ok_response_parses_to_ok_kind(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "ok",
            "response_text": "Looks good.",
            "getting_closer": True,
        })
        assert result.kind is TurnResultKind.OK
        assert result.response_text == "Looks good."
        assert result.getting_closer is True
        assert result.protocol_error_reason is None

    def test_changes_requested_response_parses_to_changes_requested_kind(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "changes_requested",
            "response_text": "Fix the null check.",
            "getting_closer": False,
        })
        assert result.kind is TurnResultKind.CHANGES_REQUESTED
        assert result.getting_closer is False
        assert result.protocol_error_reason is None

    def test_disagree_response_parses_to_disagree_kind(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "disagree",
            "response_text": "This is wrong because X.",
        })
        assert result.kind is TurnResultKind.DISAGREE
        assert result.getting_closer is None  # absent in the dict

    def test_response_text_is_stripped_of_surrounding_whitespace(self) -> None:
        # Agents often emit trailing whitespace; the orchestrator
        # downstream concatenates the text into prompts where extra
        # whitespace shifts indentation in unhelpful ways.
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "ok",
            "response_text": "   Trim me   ",
        })
        assert result.response_text == "Trim me"

    def test_none_input_is_protocol_error_missing_response(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict(None, raw_output="raw")
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "missing_response"
        assert result.raw_output == "raw"
        assert result.getting_closer is False

    def test_missing_response_type_is_protocol_error(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_text": "I forgot to declare myself.",
        })
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "missing_response_type"

    def test_empty_response_type_is_protocol_error(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "   ",
            "response_text": "ok",
        })
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "missing_response_type"

    def test_missing_response_text_is_protocol_error(self) -> None:
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "ok",
        })
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "missing_response_text"

    def test_unknown_response_type_is_protocol_error(self) -> None:
        # The set of valid response_type strings is closed; an agent
        # writing "wat" gets a named protocol error rather than
        # silently being treated as a known kind.
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "wat",
            "response_text": "I've gone rogue.",
        })
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "unknown_response_type"
        # The raw input is preserved on the result so downstream
        # diagnostics can report exactly what the agent said.
        assert result.raw_json is not None
        assert result.raw_json.get("response_type") == "wat"

    def test_non_bool_getting_closer_is_dropped(self) -> None:
        # An agent emitting ``getting_closer="true"`` (string) is not
        # a protocol error — the field is optional, so we drop it
        # rather than rejecting the whole response.
        result = ReviewExchangeTurnResult.from_agent_dict({
            "response_type": "ok",
            "response_text": "ok",
            "getting_closer": "yes-please",
        })
        assert result.kind is TurnResultKind.OK
        assert result.getting_closer is None


class TestReviewExchangeTurnResultForNoCompletion:
    """Exception-path factory: the runner uses this when a turn
    timed out or the role process died, so a typed result artifact
    still lands on disk for replay/forensics."""

    def test_returns_typed_protocol_error_with_named_reason(self) -> None:
        result = ReviewExchangeTurnResult.for_no_completion(
            "PersistentRoundTimeoutError: 60s elapsed",
        )
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "no_completion"
        # The detail surfaces in response_text so an operator
        # inspecting the on-disk artifact sees the same root cause
        # the REVIEW_EXCHANGE_ROLE_TIMEOUT event reports.
        assert "PersistentRoundTimeoutError" in result.response_text
        assert result.raw_output == "PersistentRoundTimeoutError: 60s elapsed"
        assert result.getting_closer is False

    def test_empty_detail_falls_back_to_generic_message(self) -> None:
        # A detail-less call still produces a usable response_text
        # rather than an empty string the manifest reader would
        # reject as wrong-typed.
        result = ReviewExchangeTurnResult.for_no_completion("")
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "no_completion"
        assert result.response_text == "Agent produced no response"
        assert result.raw_output is None

    def test_preserves_precise_failure_reason_when_supplied(self) -> None:
        result = ReviewExchangeTurnResult.for_no_completion(
            "Agent exited unexpectedly (code=0) before responding",
            protocol_error_reason="process_exited_before_response",
        )
        assert result.kind is TurnResultKind.PROTOCOL_ERROR
        assert result.protocol_error_reason == "process_exited_before_response"

    def test_round_trips_through_manifest(self) -> None:
        original = ReviewExchangeTurnResult.for_no_completion(
            "process exited with signal 9",
        )
        recovered = ReviewExchangeTurnResult.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered is not None
        assert recovered.kind is TurnResultKind.PROTOCOL_ERROR
        assert recovered.protocol_error_reason == "no_completion"
        assert "process exited with signal 9" in recovered.response_text


# ---------------------------------------------------------------------------
# ReviewExchangeTurnResult — round-trip
# ---------------------------------------------------------------------------


class TestReviewExchangeTurnResultRoundTrip:
    @pytest.mark.parametrize("kind", list(TurnResultKind))
    def test_every_kind_round_trips(self, kind: TurnResultKind) -> None:
        original = ReviewExchangeTurnResult(
            kind=kind,
            response_text="payload",
            getting_closer=True if kind is not TurnResultKind.PROTOCOL_ERROR else False,
            protocol_error_reason=(
                "missing_response_type"
                if kind is TurnResultKind.PROTOCOL_ERROR
                else None
            ),
        )
        recovered = ReviewExchangeTurnResult.from_manifest(
            original.to_manifest_fields(),
        )
        assert recovered is not None
        assert recovered.kind is kind
        assert recovered.response_text == "payload"
        assert recovered.protocol_error_reason == original.protocol_error_reason
        # raw_json / raw_output are intentionally not persisted; they
        # are sibling-file concerns. The recovered result has them None.
        assert recovered.raw_json is None
        assert recovered.raw_output is None

    def test_unset_optional_fields_omitted_from_manifest(self) -> None:
        result = ReviewExchangeTurnResult(
            kind=TurnResultKind.OK,
            response_text="ok",
        )
        fields = result.to_manifest_fields()
        assert "getting_closer" not in fields
        assert "protocol_error_reason" not in fields
        assert fields == {"kind": "ok", "response_text": "ok"}

    def test_from_manifest_returns_none_when_kind_unknown(self) -> None:
        assert ReviewExchangeTurnResult.from_manifest({
            "kind": "celebrate",
            "response_text": "yay",
        }) is None

    def test_from_manifest_returns_none_when_required_field_missing(self) -> None:
        assert ReviewExchangeTurnResult.from_manifest({
            "kind": "ok",
            # response_text missing
        }) is None
        assert ReviewExchangeTurnResult.from_manifest({
            "response_text": "ok",
            # kind missing
        }) is None
