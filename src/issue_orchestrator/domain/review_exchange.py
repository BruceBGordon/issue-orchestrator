"""Pure review-exchange domain helpers shared across exchange runners.

These types and functions describe the coder↔reviewer protocol without
touching infrastructure (no file I/O, no subprocess, no event sink).
They live in the domain layer so both the active in-process exchange
runner (`control/review_exchange_loop.py`) and the upcoming persistent-
session runner (`execution/persistent_session_exchange.py`) can build
on the same types and prompt/response semantics.

Public API:
    ReviewExchangeResponse, ReviewExchangeOutcome — dataclasses
    build_reviewer_prompt, build_coder_prompt — prompt construction
    parse_exchange_response — recover a response from agent output
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .coder_prompt import append_coder_prompt_addendum
from .review_exchange_run import ReviewExchangeRunAssets
from .review_exchange_summary import (
    ReviewExchangeReason,
    ReviewExchangeStatus,
    ReviewExchangeSummaryV1,
    ReviewExchangeTerminalState,
)

if TYPE_CHECKING:
    from .review_exchange_turn import ReviewExchangeTurnPacket


# Stable marker embedded in reviewer-worktree checkout/fast-forward failure
# messages. The reviewer-worktree checkout is the operation that committed
# ``.issue-orchestrator`` runtime artifacts break (#6659). The completion
# failure-reporting layer keys its runtime-artifact recovery guidance off this
# marker so that note is attached ONLY to this failure class — never to the
# many unrelated halts that share the ``review_exchange:`` error prefix
# (max-rounds/no-progress, missing exchange outcome, background-job
# cancellation, invalid exchange config, timeout cancellation).
REVIEWER_WORKTREE_CHECKOUT_FAILURE_MARKER = "[reviewer-worktree-checkout-failure]"


@dataclass(frozen=True)
class ReviewExchangeResponse:
    """One response produced by either role during a review-exchange round."""

    response_type: str
    response_text: str
    getting_closer: bool | None = None
    raw_json: dict[str, Any] | None = None
    raw_output: str | None = None


@dataclass(frozen=True)
class ReviewExchangeCacheMetadata:
    """Typed metadata for a cached review-exchange outcome."""

    summary_path: Path
    validation_record_path: Path
    head_sha: str = ""

    def to_event_fields(self) -> dict[str, str]:
        fields = {"review_cache_summary_path": str(self.summary_path)}
        fields["review_cache_validation_record_path"] = str(
            self.validation_record_path,
        )
        if self.head_sha:
            fields["review_cache_head_sha"] = self.head_sha
        return fields


@dataclass(frozen=True)
class ReviewExchangeOutcome:
    """Terminal outcome of a complete review-exchange run."""

    status: ReviewExchangeStatus
    rounds: int
    reason: ReviewExchangeReason
    run_assets: ReviewExchangeRunAssets
    reviewer_response: ReviewExchangeResponse | None = None
    summary: ReviewExchangeSummaryV1 | None = None
    cache_metadata: ReviewExchangeCacheMetadata | None = None

    def __post_init__(self) -> None:
        terminal = ReviewExchangeTerminalState.from_values(
            self.status,
            self.reason,
        )
        object.__setattr__(self, "status", terminal.status)
        object.__setattr__(self, "reason", terminal.reason)
        if (
            self.summary is not None
            and type(self.summary) is not ReviewExchangeSummaryV1
        ):
            raise TypeError("summary must be ReviewExchangeSummaryV1")

    @property
    def run_dir(self) -> Path:
        return self.run_assets.run_dir

    @property
    def exchange_dir(self) -> Path:
        return self.run_assets.exchange_dir

    @property
    def summary_path(self) -> Path:
        return self.run_assets.summary_path

    @property
    def validation_record_path(self) -> Path:
        return self.run_assets.validation_record_path


def build_reviewer_prompt(packet: "ReviewExchangeTurnPacket") -> str:
    """Build the reviewer's prompt for one round of the exchange.

    Consumes a ``ReviewExchangeTurnPacket`` (must have
    ``role == Role.REVIEWER``); the caller is responsible for
    constructing the packet so all per-turn inputs go through one
    typed seam rather than a free keyword-arg signature.
    """
    from .review_exchange_turn import Role

    if packet.role is not Role.REVIEWER:
        raise ValueError(
            f"build_reviewer_prompt requires Role.REVIEWER packet, got {packet.role!r}"
        )
    validation_note = ""
    if packet.require_validation:
        validation_record = packet.prompt_files.validation_record
        if validation_record is None:
            raise ValueError(
                "build_reviewer_prompt requires "
                "packet.prompt_files.validation_record when validation is required"
            )
        validation_note = (
            "Validation is required. Check "
            f"{validation_record}. Only respond ok if that file exists and has "
            "passed=true. Trust this file as authoritative — do NOT run build, "
            "test, or validation commands yourself (no ./gradlew, ./scripts/"
            "validate*, make, npm/pnpm/yarn test, cargo test, pytest, mvn, "
            "bazel test, or similar). Running them wastes the round's budget "
            "and can hang on restricted networks where wrapper downloads or "
            "package fetches fail. If the file is missing or shows "
            "passed=false, respond changes_requested asking the coder to run "
            "validation and fix any failures."
        )
    prior = ""
    if packet.last_coder_text:
        prior += f"\nCoder response:\n{packet.last_coder_text}\n"
    if packet.last_reviewer_text:
        prior += f"\nPrevious review feedback:\n{packet.last_reviewer_text}\n"
    return (
        f"You are the reviewer in a coder↔reviewer exchange for issue #{packet.issue_number}: {packet.issue_title}.\n"
        f"Round {packet.round_index}.\n"
        f"{validation_note}\n"
        "Review the current worktree changes.\n"
        "Consider:\n"
        "A) the changes for this issue\n"
        "B) relevant context in the broader codebase\n"
        "C) any applicable .claude/skills guidance\n"
        "D) docs/ if needed for intended behavior\n"
        "E) whether the bounded owner/port/command abstraction is strong enough, "
        "not merely whether the diff works\n"
        f"{prior}\n"
        "Submit your verdict by running the `exchange-respond` command "
        "(do not write a response file):\n"
        "  exchange-respond ok --getting-closer --text \"Looks good.\"\n"
        "  exchange-respond changes_requested --getting-closer --text \"Fix X.\"\n"
        "  exchange-respond disagree --not-getting-closer --text \"Wrong approach.\"\n"
    )


def build_coder_prompt(packet: "ReviewExchangeTurnPacket") -> str:
    """Build the coder's prompt for one round of the exchange.

    Consumes a ``ReviewExchangeTurnPacket`` with
    ``role == Role.CODER`` and ``reviewer_feedback`` set. Runner is
    expected to copy the most-recent persisted reviewer report into the
    packet's ``reviewer_feedback`` slot.
    """
    from .review_exchange_turn import Role

    if packet.role is not Role.CODER:
        raise ValueError(
            f"build_coder_prompt requires Role.CODER packet, got {packet.role!r}"
        )
    if packet.reviewer_feedback is None:
        raise ValueError(
            "build_coder_prompt requires packet.reviewer_feedback to be set"
        )
    prompt = (
        f"You are the coder in a review exchange for issue #{packet.issue_number}: {packet.issue_title}.\n"
        f"Round {packet.round_index}.\n"
        "Review the full reviewer report below and update the worktree accordingly.\n"
        "\n"
        "Steps:\n"
        "1. Make the requested changes (or prepare a disagreement).\n"
        "2. Commit all changes (clean working tree required).\n"
        "3. Run `prepush-check --dirty-only -v` and fix any dirty-worktree failure.\n"
        "4. Run `coding-done completed --implementation '...' --problems '...'`\n"
        "5. Submit your verdict by running `exchange-respond`\n"
        "Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` "
        "is ignored by the orchestrator dirty guard. Tracked project files, "
        "generated sources, lock files, schemas, and other repo changes must "
        "still be committed or removed.\n"
        "\n"
        f"Session output dir: {packet.run_dir}\n"
        f"\nReviewer report:\n{packet.reviewer_feedback}\n"
        "\n"
        "After coding-done succeeds, submit your verdict with `exchange-respond`:\n"
        "  exchange-respond ok --text \"Applied fixes...\"\n"
        "  exchange-respond disagree --text \"This is wrong because...\"\n"
    )
    return append_coder_prompt_addendum(prompt, packet.coder_prompt_addendum)


def parse_exchange_response(stdout: str) -> ReviewExchangeResponse | None:
    """Recover a structured response from raw agent output.

    Tries the response in three places, in order: a strict last-line JSON
    object; a multiline JSON string with raw newlines we know how to repair;
    embedded JSON objects in non-JSON wrapper output. Falls through to a
    JSON-line-envelope walk for agents that wrap output in a result/message
    structure (e.g. Claude's tool-call envelope).
    """
    if not stdout:
        return None
    direct = _parse_protocol_json_from_text(stdout)
    if direct is not None:
        return _review_exchange_response_from_dict(direct, stdout)

    for envelope in _iter_json_line_envelopes(stdout):
        embedded = _parse_embedded_protocol_from_envelope(envelope)
        if embedded is not None:
            return _review_exchange_response_from_dict(embedded, stdout)
    return None


def _parse_protocol_json_from_text(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    line_match = _parse_protocol_json_from_lines(stripped)
    if line_match is not None:
        return line_match
    repaired = _parse_protocol_json_with_repaired_multiline_strings(stripped)
    if repaired is not None:
        return repaired
    return _parse_protocol_json_from_embedded_objects(stripped)


def _review_exchange_response_from_dict(
    parsed: dict[str, Any],
    raw_output: str,
) -> ReviewExchangeResponse:
    return ReviewExchangeResponse(
        response_type=parsed["response_type"],
        response_text=parsed["response_text"],
        getting_closer=parsed["getting_closer"],
        raw_json=parsed["raw_json"],
        raw_output=raw_output,
    )


def _iter_json_line_envelopes(stdout: str) -> list[dict[str, Any]]:
    envelopes: list[dict[str, Any]] = []
    for line in reversed(stdout.strip().splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            envelope = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(envelope, dict):
            envelopes.append(envelope)
    return envelopes


def _parse_embedded_protocol_from_envelope(
    envelope: dict[str, Any],
) -> dict[str, Any] | None:
    result_payload = envelope.get("result")
    if isinstance(result_payload, str):
        embedded = _parse_protocol_json_from_text(result_payload)
        if embedded is not None:
            return embedded
    return _parse_embedded_protocol_from_message(envelope.get("message"))


def _parse_embedded_protocol_from_message(
    message: object,
) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        embedded = _parse_protocol_json_from_text(text)
        if embedded is not None:
            return embedded
    return None


def _parse_protocol_json_from_lines(stripped: str) -> dict[str, Any] | None:
    for line in reversed(stripped.splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        normalized = _normalize_protocol_response(data)
        if normalized is not None:
            return normalized
    return None


def _parse_protocol_json_from_embedded_objects(stripped: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    matches: list[dict[str, Any]] = []
    for idx, ch in enumerate(stripped):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(stripped[idx:])
        except json.JSONDecodeError:
            continue
        if end <= 0:
            continue
        normalized = _normalize_protocol_response(obj)
        if normalized is not None:
            matches.append(normalized)
    return matches[-1] if matches else None


def _parse_protocol_json_with_repaired_multiline_strings(
    stripped: str,
) -> dict[str, Any] | None:
    """Recover common malformed JSON where agents emit raw newlines inside strings.

    Review exchange prompts ask for one-line JSON, but interactive agents
    sometimes write multi-line prose directly inside ``response_text``.  The
    content is still structurally useful, so normalize raw newlines inside JSON
    strings and try one more strict parse before declaring a protocol error.
    """
    repaired = _escape_raw_newlines_inside_json_strings(stripped)
    if repaired == stripped:
        return None
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError:
        return None
    return _normalize_protocol_response(data)


def _escape_raw_newlines_inside_json_strings(text: str) -> str:
    """Escape literal CR/LF characters that appear inside quoted JSON strings."""
    chars: list[str] = []
    in_string = False
    escaped = False

    for ch in text:
        if in_string:
            if escaped:
                chars.append(ch)
                escaped = False
                continue
            if ch == "\\":
                chars.append(ch)
                escaped = True
                continue
            if ch == '"':
                chars.append(ch)
                in_string = False
                continue
            if ch == "\n":
                chars.append("\\n")
                continue
            if ch == "\r":
                chars.append("\\r")
                continue
            if ch == "\t":
                chars.append("\\t")
                continue
            chars.append(ch)
            continue

        chars.append(ch)
        if ch == '"':
            in_string = True

    return "".join(chars)


def _normalize_protocol_response(obj: object) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    response_type = str(obj.get("response_type", "")).strip()
    response_text = str(obj.get("response_text", "")).strip()
    if not response_type or not response_text:
        return None
    getting_closer = obj.get("getting_closer")
    return {
        "response_type": response_type,
        "response_text": response_text,
        "getting_closer": bool(getting_closer) if getting_closer is not None else None,
        "raw_json": obj,
    }
