"""Contract strictness and actor attribution for the pause vocabulary.

Covers review findings 2, 4 and 7: the provenance contract must reject the
empty payload it replaced, each operator surface must be journaled under its
own identity, and the facade must refuse a pause that does not name why and
on whose behalf.
"""

from __future__ import annotations

import inspect

import pydantic
import pytest

from issue_orchestrator.contracts.public import (
    OrchestratorPausedPayload,
    OrchestratorResumedPayload,
)
from issue_orchestrator.domain.pause_state import PauseActor, PauseReason, PauseState
from issue_orchestrator.execution.orchestrator_http_api import OrchestratorAsyncHttpApi
from issue_orchestrator.infra.orchestrator import Orchestrator


class TestProvenanceContractsRejectEmptyPayloads:
    """Finding 4: optional fields let the old broken payload keep validating."""

    def test_paused_payload_rejects_the_empty_payload(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            OrchestratorPausedPayload.model_validate({})

    def test_resumed_payload_rejects_the_empty_payload(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            OrchestratorResumedPayload.model_validate({})

    @pytest.mark.parametrize(
        "missing",
        ["pause_reason", "pause_actor", "paused_since", "paused_held_seconds"],
    )
    def test_paused_payload_requires_each_provenance_field(self, missing: str) -> None:
        payload = {
            "paused": True,
            "pause_reason": "operator",
            "pause_actor": "web_api",
            "paused_since": "2026-08-17T09:37:19+00:00",
            "paused_held_seconds": 0.0,
            "pause_is_incident": False,
        }
        payload.pop(missing)
        with pytest.raises(pydantic.ValidationError):
            OrchestratorPausedPayload.model_validate(payload)

    def test_paused_payload_rejects_paused_false(self) -> None:
        """A "paused" event asserting paused=False is incoherent."""
        with pytest.raises(pydantic.ValidationError):
            OrchestratorPausedPayload.model_validate({
                "paused": False,
                "pause_reason": "operator",
                "pause_actor": "web_api",
                "paused_since": "2026-08-17T09:37:19+00:00",
                "paused_held_seconds": 0.0,
                "pause_is_incident": False,
            })

    def test_a_real_pause_payload_validates(self) -> None:
        """The producer's own serialization must satisfy the stricter contract."""
        state = PauseState.paused_now(
            reason=PauseReason.LOOP_ERROR_THRESHOLD,
            actor=PauseActor.SYSTEM,
            detail="3 consecutive tick errors",
        )
        OrchestratorPausedPayload.model_validate(state.to_payload())


class TestActorAttribution:
    """Finding 2: Control Center pauses were journaled as `mcp`."""

    def test_control_center_and_mcp_are_distinct_identities(self) -> None:
        assert PauseActor.CONTROL_CENTER != PauseActor.MCP
        assert str(PauseActor.CONTROL_CENTER) == "control_center"

    def test_async_client_requires_an_explicit_actor(self) -> None:
        """No default: one client class serves both surfaces."""
        sig = inspect.signature(OrchestratorAsyncHttpApi.__init__)
        param = sig.parameters["pause_actor"]
        assert param.default is inspect.Parameter.empty
        assert param.kind is inspect.Parameter.KEYWORD_ONLY

    @pytest.mark.parametrize(
        "actor", [PauseActor.MCP, PauseActor.CONTROL_CENTER]
    )
    @pytest.mark.asyncio
    async def test_each_surface_sends_its_own_actor(self, actor: PauseActor) -> None:
        sent: list[dict] = []

        class RecordingClient:
            async def request(self, method, url, **kwargs):  # noqa: ANN001
                sent.append(kwargs.get("json") or {})
                return _OkResponse()

            async def aclose(self) -> None:
                return None

        api = OrchestratorAsyncHttpApi(
            base_url_provider=lambda: "http://test",
            client=RecordingClient(),
            pause_actor=actor,
        )
        await api.pause()
        await api.resume()

        assert [body["actor"] for body in sent] == [str(actor), str(actor)]


class _OkResponse:
    status_code = 200
    text = "{}"

    def json(self) -> dict:
        return {"status": "ok"}

    def raise_for_status(self) -> None:
        return None


class TestFacadeRequiresProvenance:
    """Finding 7: defaults let a call site invent operator/control_api silently."""

    @pytest.mark.parametrize("name", ["reason", "actor"])
    def test_pause_has_no_default_provenance(self, name: str) -> None:
        param = inspect.signature(Orchestrator.pause).parameters[name]
        assert param.default is inspect.Parameter.empty, (
            f"Orchestrator.pause({name}=...) has a default; a caller could pause "
            "without naming why or on whose behalf."
        )

    def test_resume_has_no_default_actor(self) -> None:
        param = inspect.signature(Orchestrator.resume).parameters["actor"]
        assert param.default is inspect.Parameter.empty
