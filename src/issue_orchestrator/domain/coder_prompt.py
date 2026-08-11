"""Pure composition rules for coder-only prompt instructions."""

from __future__ import annotations


def append_coder_prompt_addendum(prompt: str, addendum: str | None) -> str:
    """Append one owned coder addendum without changing disabled prompts."""
    if addendum is None:
        return prompt
    return f"{prompt.rstrip()}\n\n---\n\n{addendum.strip()}\n"


def build_internal_review_addendum(
    *,
    instructions: str,
    max_rounds: int,
    source: str,
) -> str:
    """Wrap repository instructions in the mandatory internal-review contract."""
    return f"""## Mandatory internal review loop

You are still the coder. Before reporting successful completion, follow the
repository-owned internal-review instructions below.

The purpose of this fast, coder-owned loop is to improve the implementation
that the orchestrator's independent external reviewer will see. It supplements
that external review; it does not replace or weaken it.

- Spawn exactly one internal reviewer; that reviewer must approve the current
  worktree.
- You may use at most {max_rounds} internal reviewer verdict(s) in this coder turn.
- Iterate with the same internal reviewer after addressing its findings.
- Any code change after approval requires another internal review.
- If approval cannot be reached within the limit, report the coder turn as
  blocked instead of reporting successful completion.

Instructions source: `{source}`

<internal-review-instructions>
{instructions.strip()}
</internal-review-instructions>"""
