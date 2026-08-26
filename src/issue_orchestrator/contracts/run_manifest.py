"""Typed contract for run-scoped manifest.json artifacts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, model_validator


class RunManifestArtifact(BaseModel):
    """Typed descriptor for one run-scoped artifact."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "session_log",
        "terminal_recording",
        "provider_runner_stdout",
        "claude_jsonl",
        "session_prompt",
        "completion_record",
        "validation_record",
        "validation_stdout",
        "validation_stderr",
        "junit_xml",
        "diagnostic",
        "orchestrator_tail",
        "review_exchange_summary",
        "review_exchange_transcript",
    ]
    path: str = Field(min_length=1)
    content_type: str | None = None


class RunManifestContract(BaseModel):
    """Contract for run-scoped ``manifest.json`` files."""

    model_config = ConfigDict(extra="allow")

    session_name: str
    run_id: str
    run_dir: str
    artifacts: dict[str, RunManifestArtifact] = Field(default_factory=dict)
    scheduled_outer_watchdog_timeout_minutes: int | None = Field(
        default=None,
        gt=0,
    )

    @model_validator(mode="after")
    def _validate_required_artifacts(self, info: ValidationInfo) -> "RunManifestContract":
        strict_required = bool((info.context or {}).get("strict_required_artifacts", False))
        if strict_required:
            for required_name in ("terminal_recording",):
                if required_name not in self.artifacts:
                    raise ValueError(f"manifest missing required artifact: {required_name}")
                artifact = self.artifacts[required_name]
                if not artifact.path.strip():
                    raise ValueError(f"manifest required artifact has empty path: {required_name}")
        return self


def validate_run_manifest_payload(
    payload: dict[str, Any],
    *,
    strict_required_artifacts: bool = False,
) -> dict[str, Any]:
    """Validate and normalize run manifest payload."""
    model = RunManifestContract.model_validate(
        payload,
        context={"strict_required_artifacts": strict_required_artifacts},
    )
    return model.model_dump(mode="python", exclude_none=True)


def run_manifest_json_schema() -> dict[str, Any]:
    """Return JSON schema for run manifest contract."""
    return RunManifestContract.model_json_schema()


def is_valid_run_manifest_payload(
    payload: dict[str, Any],
    *,
    strict_required_artifacts: bool = False,
) -> bool:
    """Return True when payload validates against run manifest contract."""
    try:
        RunManifestContract.model_validate(
            payload,
            context={"strict_required_artifacts": strict_required_artifacts},
        )
    except ValidationError:
        return False
    return True
