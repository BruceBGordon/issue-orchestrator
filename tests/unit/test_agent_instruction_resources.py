"""Contract tests for the instruction docs injected into agent sessions.

These files are the orchestrator's only channel for telling a coding agent how
to sequence its work. The rule under test here is the commit-before-publish-gate
ordering: the publish suite is cached by HEAD SHA, and one green record is reused
by the agent, by the orchestrator's publish gate, and by the git pre-push hook.
An agent that validates before committing seeds the record against the parent
commit, so every later consumer misses and re-runs the whole suite.
"""

import re
from pathlib import Path

from issue_orchestrator.resources import (
    get_coding_done_instructions,
    get_completion_instructions,
    get_review_exchange_coder_instructions,
)


def _flat(text: str) -> str:
    """Collapse markdown line wrapping so assertions survive reflowing."""
    return re.sub(r"\s+", " ", text)


class TestCommitBeforePublishGate:
    def test_coding_done_instructions_order_commit_before_the_gate(self):
        text = _flat(get_coding_done_instructions())

        assert "Commit First, THEN Run the Publish Gate" in text
        assert "cached by **HEAD commit SHA**" in text
        assert "prepush-check -v" in text

        commit_step = text.index("**Commit** once you believe the change is done")
        gate_step = text.index("**Then** run the full publish gate")
        assert commit_step < gate_step, (
            "the instructions must present committing before running the gate"
        )

    def test_coding_done_instructions_explain_both_failure_modes(self):
        text = _flat(get_coding_done_instructions())

        # Running the gate on a dirty tree fails outright...
        assert "dirty-tree guard runs **first**" in text
        # ...and committing afterwards silently invalidates the record.
        assert "recorded against the *parent* commit" in text

    def test_instructions_name_the_consumer_that_actually_reuses_the_record(self):
        """Only the pre-push hook reuses the plain SHA record.

        The orchestrator's own publish gate runs attempt-scoped: given an
        AttemptKey it consults the attempt sidecar and never falls back to the
        SHA cache, by design (see
        tests/unit/test_validation.py, "does not fall back to sha cache for
        different attempt"). Telling an agent that the orchestrator will reuse
        its record would promise a saving the agent does not get.
        """
        for text in (
            _flat(get_coding_done_instructions()),
            _flat(get_review_exchange_coder_instructions()),
        ):
            assert "pre-push hook" in text
            assert "orchestrator's publish gate" not in text

    def test_coding_done_instructions_forbid_stashing_past_the_guard(self):
        text = _flat(get_coding_done_instructions())

        assert "Do not `git stash` to get past the dirty guard" in text
        assert "Commit instead." in text

    def test_review_exchange_coder_carries_the_same_ordering_rule(self):
        text = _flat(get_review_exchange_coder_instructions())

        assert "Commit before you run any publish/pre-push validation" in text
        assert "cached by HEAD commit SHA" in text
        # Rework adds commits every round, so the ordering is not a one-off.
        assert "this ordering matters every round" in text
        assert "Never `git stash` to get past the dirty guard" in text

    def test_rule_reaches_every_coding_side_task_kind(self):
        """code, rework, and exchange coders all receive the ordering rule."""

        for task_kind in ("code", "rework", "review_exchange_coder"):
            text = _flat(get_completion_instructions(task_kind))
            assert "HEAD commit SHA" in text, task_kind
            assert "git stash" in text, task_kind


class TestRepoCodingPromptOrdering:
    """This repo's own coding prompt must teach the same ordering.

    `repo-specific/prompts/simple-fix.md` is what code/rework/exchange agents
    read here. It previously ran "validate" as step 4 and "commit" as step 5,
    which is exactly the order that defeats the SHA-keyed publish-gate cache.
    """

    @staticmethod
    def _prompt() -> str:
        repo_root = Path(__file__).resolve().parents[2]
        return (repo_root / "repo-specific" / "prompts" / "simple-fix.md").read_text()

    def test_commit_step_precedes_the_pr_gate_step(self):
        text = _flat(self._prompt())

        commit_heading = text.index("### 5. Commit Your Changes")
        gate_heading = text.index("### 6. Run the Full PR Gate")
        assert commit_heading < gate_heading

    def test_checklist_puts_validate_pr_after_the_commit(self):
        text = _flat(self._prompt())

        commit_item = text.index("[ ] 2. Commit my changes")
        gate_item = text.index("[ ] 3. Run `make validate-pr` AT that commit")
        assert commit_item < gate_item

    def test_fast_loop_does_not_advertise_validate_pr(self):
        """Step 4 is the cheap inner loop; the expensive gate belongs in step 6."""
        text = _flat(self._prompt())

        fast_loop = text[
            text.index("### 4. Validate Your Changes") : text.index(
                "### 5. Commit Your Changes"
            )
        ]
        assert "make validate-quick" in fast_loop
        assert "make validate-pr" not in fast_loop

    def test_gate_section_warns_off_the_two_cache_defeating_moves(self):
        text = _flat(self._prompt())

        assert "Never `git stash` to satisfy the dirty guard" in text
        assert "Do **not** run `make validate-pr-raw` by hand" in text
