"""A tampered settings POST is refused BEFORE it touches live config (#6957 R3 F9).

``json_schema_extra={"enum": ...}`` shapes the generated ``<select>``; it does
not make Pydantic validate anything. So a hand-rolled request body — the exact
thing a browser control cannot produce but curl can — carried an unsupported
``tech_lead.findings.promote`` straight through ``model_validate``, was applied
to the running config by ``apply_to``, and was only caught afterwards by the
doctor pass, which then had to roll the live config back.

These drive the real ``update_settings`` handler and pin both halves: the
rejection is field-scoped (so the form can attach it to the offending input),
and it happens before the live config is mutated at all.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from issue_orchestrator.domain.tech_lead_findings import (
    VALID_FINDING_PROMOTION_MODES,
)
from issue_orchestrator.entrypoints.web_settings_routes import update_settings
from issue_orchestrator.infra.config import Config


def _config() -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_follow_up_agent = "agent:backend"
    return config


def _request(body: dict) -> types.SimpleNamespace:
    async def _json() -> dict:
        return body

    return types.SimpleNamespace(json=_json)


def _post(body: dict, config: Config):
    orchestrator = types.SimpleNamespace(config=config)
    return asyncio.run(update_settings(_request(body), orchestrator))


def _errors(response) -> list[dict]:
    import json

    return json.loads(bytes(response.body).decode())["errors"]


def test_an_unsupported_promotion_mode_is_rejected_field_scoped():
    config = _config()

    response = _post({"review": {"tech_lead_findings_promote": "sometimes"}}, config)

    assert response.status_code == 400
    [error] = [e for e in _errors(response) if "promote" in e["name"]]
    assert error["name"] == "tech_lead_findings_promote"


def test_the_rejected_value_never_reaches_the_live_config():
    """Rejected BEFORE apply_to, so there is nothing for doctor to roll back."""
    config = _config()
    before = config.tech_lead.findings.promote

    _post({"review": {"tech_lead_findings_promote": "sometimes"}}, config)

    assert config.tech_lead.findings.promote == before


@pytest.mark.parametrize("mode", VALID_FINDING_PROMOTION_MODES)
def test_every_supported_mode_round_trips_onto_live_config(mode, monkeypatch):
    """The gate must reject outsiders, not narrow the documented set.

    The doctor pass is stubbed out: it performs real git/GitHub work, and what
    this pins is the SCHEMA boundary — that each documented mode survives
    validation and lands on the config, which is exactly what the tampered
    value must not do.
    """
    from issue_orchestrator.infra import doctor as doctor_module

    monkeypatch.setattr(
        doctor_module, "run_doctor", lambda **_kwargs: doctor_module.DoctorResult([])
    )
    config = _config()

    response = _post({"review": {"tech_lead_findings_promote": mode}}, config)

    assert response.status_code == 200, bytes(response.body)
    assert config.tech_lead.findings.promote == mode
