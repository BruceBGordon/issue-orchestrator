"""Compatibility exports for execution-owned setup prompt builders."""

from ..execution.setup_wizard_prompts import (
    build_code_review_prompt_text,
    build_starter_prompt_text,
    build_tech_lead_review_prompt_text,
)

__all__ = [
    "build_code_review_prompt_text",
    "build_starter_prompt_text",
    "build_tech_lead_review_prompt_text",
]
