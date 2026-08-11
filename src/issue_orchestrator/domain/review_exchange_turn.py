"""Typed turn-packet and turn-result for the review-exchange protocol.

Replaces three loose seams:

1. **Per-turn input args**. ``_drive_rounds`` previously threaded eight
   keyword args (``issue_number``, ``round_index``, ``last_coder_text``,
   ``last_reviewer_text``, ``require_validation``, ``run_dir``, ``role``,
   file paths, ``issue_title``) into ``build_reviewer_prompt`` /
   ``build_coder_prompt``. A drift between writer and prompt builder was
   silent — the type checker had nothing to grab onto.
   ``ReviewExchangeTurnPacket`` is the one typed bundle.

2. **Per-turn result parsing**. ``_normalize_role_response`` consumed a
   free-form ``dict[str, Any]`` and produced a ``ReviewExchangeResponse``
   with ``response_type='protocol_error'`` for any malformed input.
   Callers then matched on the magic string. ``ReviewExchangeTurnResult``
   makes the kind a ``TurnResultKind`` enum so downstream dispatch is
   type-safe and exhaustive.

3. **Per-turn artifact replay**. Session debugging required walking the
   recording stream to recover what the orchestrator gave the agent and
   what the agent returned. ``to_manifest_fields`` /``from_manifest``
   on both types let the runner persist them as JSON artifacts under
   the exchange dir; the writer and reader share field-set symmetry,
   pinned by round-trip tests.

Public API:
    ReviewExchangePromptFiles — injected file references for prompts
    ReviewExchangeTurnPacket — typed inputs for one role's round
    TurnResultKind — enum naming each result variant
    ReviewExchangeTurnResult — typed parsed result (kind + payload)
    Role — the two roles, named (no string typo path)
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class Role(str, enum.Enum):
    """The two roles in the review exchange.

    Subclassing ``str`` so existing code that accepts ``str`` for role
    keeps working during the migration; new call sites should pass /
    accept ``Role`` directly. Compare with ``Role.REVIEWER`` etc., not
    the literal string.
    """

    REVIEWER = "reviewer"
    CODER = "coder"


class TurnResultKind(str, enum.Enum):
    """The set of outcomes the orchestrator parses from one role's turn.

    - ``OK``: reviewer approved (only meaningful for ``Role.REVIEWER``).
    - ``CHANGES_REQUESTED``: reviewer asked for changes.
    - ``DISAGREE``: coder pushed back on the request.
    - ``PROTOCOL_ERROR``: response missing required fields, malformed,
      or otherwise unparseable. Carries
      ``ReviewExchangeTurnResult.protocol_error_reason`` so operators
      can distinguish "missing response_type" from "invalid JSON" from
      "completion artifact missing" etc.
    """

    OK = "ok"
    CHANGES_REQUESTED = "changes_requested"
    DISAGREE = "disagree"
    PROTOCOL_ERROR = "protocol_error"


# The set of strings the agent is allowed to write into its JSON
# response's ``response_type`` field, mapped to the enum the
# orchestrator uses internally. Anything outside this map is a
# protocol error.
_VALID_RESPONSE_TYPES: dict[str, TurnResultKind] = {
    "ok": TurnResultKind.OK,
    "changes_requested": TurnResultKind.CHANGES_REQUESTED,
    "disagree": TurnResultKind.DISAGREE,
}


@dataclass(frozen=True)
class ReviewExchangePromptFiles:
    """Explicit file dependencies a review-exchange prompt may reference.

    Prompt builders consume these injected paths rather than deriving
    file locations from layout fields such as ``run_dir``. Add new file
    references here when another prompt needs a concrete artifact path.
    """

    validation_record: Path | None = None

    def to_manifest_fields(self) -> dict[str, Any]:
        manifest: dict[str, Any] = {}
        if self.validation_record is not None:
            manifest["validation_record"] = str(self.validation_record)
        return manifest

    @classmethod
    def from_manifest(
        cls,
        manifest: Any,
    ) -> "ReviewExchangePromptFiles | None":
        if manifest is None:
            return cls()
        if not isinstance(manifest, Mapping):
            return None
        validation_record_raw = manifest.get("validation_record")
        validation_record = None
        if validation_record_raw is not None:
            if not isinstance(validation_record_raw, str) or not validation_record_raw:
                return None
            validation_record = Path(validation_record_raw)
        return cls(validation_record=validation_record)


@dataclass(frozen=True)
class ReviewExchangeTurnPacket:
    """Typed input bundle for one role's turn in the exchange.

    Replaces the ad-hoc keyword-arg threading from ``_drive_rounds`` to
    the prompt builders. The packet is the single source the prompt is
    built from, the orchestrator records it as an artifact, and round-
    trip tests pin the writer/reader symmetry.
    """

    issue_number: int
    issue_title: str
    round_index: int
    role: Role
    require_validation: bool
    run_dir: Path
    prompt_files: ReviewExchangePromptFiles = field(
        default_factory=ReviewExchangePromptFiles,
    )
    last_coder_text: str | None = None
    last_reviewer_text: str | None = None
    reviewer_feedback: str | None = None
    """The coder's packet carries the reviewer's prior-round report here.

    The persisted manifest field keeps its historical ``reviewer_feedback``
    name, but the value is the full ``review-report.md`` text. This is
    distinct from ``last_reviewer_text`` because that remains the
    reviewer's one-line JSON summary for reviewer-to-reviewer context.
    """
    coder_prompt_addendum: str | None = None
    """Coder-only instructions composed outside the pure exchange builder."""

    def to_manifest_fields(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for artifact persistence.

        Optional fields are omitted when unset (consistent with the
        manifest header pattern), so readers detect "unset" by absence
        rather than by sentinel value.
        """
        manifest: dict[str, Any] = {
            "issue_number": self.issue_number,
            "issue_title": self.issue_title,
            "round_index": self.round_index,
            "role": self.role.value,
            "require_validation": self.require_validation,
            "run_dir": str(self.run_dir),
        }
        prompt_files = self.prompt_files.to_manifest_fields()
        if prompt_files:
            manifest["prompt_files"] = prompt_files
        if self.last_coder_text is not None:
            manifest["last_coder_text"] = self.last_coder_text
        if self.last_reviewer_text is not None:
            manifest["last_reviewer_text"] = self.last_reviewer_text
        if self.reviewer_feedback is not None:
            manifest["reviewer_feedback"] = self.reviewer_feedback
        if self.coder_prompt_addendum is not None:
            manifest["coder_prompt_addendum"] = self.coder_prompt_addendum
        return manifest

    @classmethod
    def from_manifest(
        cls, manifest: Mapping[str, Any],
    ) -> "ReviewExchangeTurnPacket | None":
        """Recover a packet from its persisted manifest dict.

        Returns ``None`` if any required field is missing or wrong-typed
        — the caller treats ``None`` as "this artifact is unusable" and
        falls through to whatever recovery path applies (typically: log
        and continue without replay).

        Scalar optional text fields are ignored when malformed. The
        structured ``prompt_files`` object is stricter: malformed file
        references reject the packet so prompt dependencies never silently
        degrade to "unset."
        """
        issue_number = manifest.get("issue_number")
        issue_title = manifest.get("issue_title")
        round_index = manifest.get("round_index")
        role_raw = manifest.get("role")
        require_validation = manifest.get("require_validation")
        run_dir_raw = manifest.get("run_dir")
        if not isinstance(issue_number, int):
            return None
        if not isinstance(issue_title, str):
            return None
        if not isinstance(round_index, int):
            return None
        if not isinstance(role_raw, str):
            return None
        try:
            role = Role(role_raw)
        except ValueError:
            return None
        if not isinstance(require_validation, bool):
            return None
        if not isinstance(run_dir_raw, str) or not run_dir_raw:
            return None
        last_coder_text = manifest.get("last_coder_text")
        last_reviewer_text = manifest.get("last_reviewer_text")
        reviewer_feedback = manifest.get("reviewer_feedback")
        coder_prompt_addendum = manifest.get("coder_prompt_addendum")
        prompt_files = ReviewExchangePromptFiles.from_manifest(
            manifest.get("prompt_files"),
        )
        if prompt_files is None:
            return None
        return cls(
            issue_number=issue_number,
            issue_title=issue_title,
            round_index=round_index,
            role=role,
            require_validation=require_validation,
            run_dir=Path(run_dir_raw),
            prompt_files=prompt_files,
            last_coder_text=last_coder_text if isinstance(last_coder_text, str) else None,
            last_reviewer_text=last_reviewer_text if isinstance(last_reviewer_text, str) else None,
            reviewer_feedback=reviewer_feedback if isinstance(reviewer_feedback, str) else None,
            coder_prompt_addendum=(
                coder_prompt_addendum
                if isinstance(coder_prompt_addendum, str)
                else None
            ),
        )


@dataclass(frozen=True)
class ReviewExchangeTurnResult:
    """Typed parsed result of one role's turn.

    The kind is an enum, not a magic string. Protocol errors carry a
    distinct ``protocol_error_reason`` so the orchestrator can tell
    "missing response_type" apart from "wrong response_type value"
    apart from "JSON decode failure" without re-parsing the raw output.
    """

    kind: TurnResultKind
    response_text: str
    getting_closer: bool | None = None
    raw_json: dict[str, Any] | None = None
    raw_output: str | None = None
    protocol_error_reason: str | None = None

    @classmethod
    def for_no_completion(
        cls,
        detail: str,
        *,
        protocol_error_reason: str = "no_completion",
    ) -> "ReviewExchangeTurnResult":
        """Construct a typed result for a turn that never produced a response.

        Used when the persistent-session round timed out, the role
        process died, or any other condition prevented the agent from
        writing its JSON response file. Distinct from
        ``from_agent_dict(None)`` (which is also a missing-response,
        but the caller did not have an exception detail to surface).

        ``protocol_error_reason`` names the exact current-turn failure
        while the exchange summary may still use the broader
        ``*_no_completion`` retry bucket. ``detail`` typically carries
        the underlying exception's ``str(exc)`` so an operator inspecting
        the persisted ``round-<n>-<role>-attempt-<m>.result.json`` sees
        the same root cause the ``REVIEW_EXCHANGE_ROLE_TIMEOUT`` event
        reports — without having to cross-reference event logs and the
        recording stream.
        """
        return cls(
            kind=TurnResultKind.PROTOCOL_ERROR,
            response_text=detail or "Agent produced no response",
            getting_closer=False,
            raw_json=None,
            raw_output=detail or None,
            protocol_error_reason=protocol_error_reason or "no_completion",
        )

    @classmethod
    def from_agent_dict(
        cls,
        parsed: Mapping[str, Any] | None,
        *,
        raw_output: str | None = None,
    ) -> "ReviewExchangeTurnResult":
        """Construct from the agent's JSON response dict.

        Centralises the protocol contract: the agent must write
        ``{response_type, response_text, [getting_closer]}`` where
        ``response_type`` is one of ``ok`` / ``changes_requested`` /
        ``disagree``. Anything else is a protocol error with a named
        reason.
        """
        if parsed is None:
            return cls(
                kind=TurnResultKind.PROTOCOL_ERROR,
                response_text="Agent produced no parseable response",
                getting_closer=False,
                raw_json=None,
                raw_output=raw_output,
                protocol_error_reason="missing_response",
            )
        response_type_raw = parsed.get("response_type")
        response_text_raw = parsed.get("response_text")
        getting_closer = parsed.get("getting_closer")
        if not isinstance(response_type_raw, str) or not response_type_raw.strip():
            return cls(
                kind=TurnResultKind.PROTOCOL_ERROR,
                response_text="Agent response missing required response_type field",
                getting_closer=False,
                raw_json=dict(parsed),
                raw_output=raw_output,
                protocol_error_reason="missing_response_type",
            )
        if not isinstance(response_text_raw, str) or not response_text_raw.strip():
            return cls(
                kind=TurnResultKind.PROTOCOL_ERROR,
                response_text="Agent response missing required response_text field",
                getting_closer=False,
                raw_json=dict(parsed),
                raw_output=raw_output,
                protocol_error_reason="missing_response_text",
            )
        kind = _VALID_RESPONSE_TYPES.get(response_type_raw.strip())
        if kind is None:
            return cls(
                kind=TurnResultKind.PROTOCOL_ERROR,
                response_text=(
                    f"Agent wrote unrecognized response_type "
                    f"{response_type_raw!r}"
                ),
                getting_closer=False,
                raw_json=dict(parsed),
                raw_output=raw_output,
                protocol_error_reason="unknown_response_type",
            )
        return cls(
            kind=kind,
            response_text=response_text_raw.strip(),
            getting_closer=getting_closer if isinstance(getting_closer, bool) else None,
            raw_json=dict(parsed),
            raw_output=raw_output,
        )

    def to_manifest_fields(self) -> dict[str, Any]:
        """Render to a JSON-safe dict for artifact persistence."""
        fields: dict[str, Any] = {
            "kind": self.kind.value,
            "response_text": self.response_text,
        }
        if self.getting_closer is not None:
            fields["getting_closer"] = self.getting_closer
        if self.protocol_error_reason is not None:
            fields["protocol_error_reason"] = self.protocol_error_reason
        # raw_json / raw_output are deliberately not persisted by
        # default — callers that want those should write them to a
        # sibling file (the recording slice already captures them).
        return fields

    @classmethod
    def from_manifest(
        cls, fields: Mapping[str, Any],
    ) -> "ReviewExchangeTurnResult | None":
        """Recover a result from its persisted manifest dict.

        Returns ``None`` if any required field is missing or wrong-typed.
        """
        kind_raw = fields.get("kind")
        response_text = fields.get("response_text")
        if not isinstance(kind_raw, str):
            return None
        try:
            kind = TurnResultKind(kind_raw)
        except ValueError:
            return None
        if not isinstance(response_text, str):
            return None
        getting_closer = fields.get("getting_closer")
        protocol_error_reason = fields.get("protocol_error_reason")
        return cls(
            kind=kind,
            response_text=response_text,
            getting_closer=getting_closer if isinstance(getting_closer, bool) else None,
            raw_json=None,
            raw_output=None,
            protocol_error_reason=(
                protocol_error_reason
                if isinstance(protocol_error_reason, str)
                else None
            ),
        )
