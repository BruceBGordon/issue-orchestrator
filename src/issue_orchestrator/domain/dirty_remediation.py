"""Single owner for what a coding agent must do with a dirty working-tree file.

Several surfaces tell an agent how to clear the dirty guard: the
``prepush-check`` CLI, the ``coding-done`` rejection reason, the orchestrator's
dirty-worktree retry prompt, and the markdown instruction resources injected
into every coding session. Each used to carry its own wording, so a policy
correction had to be re-applied by hand at every site. That drift is not
hypothetical: the guard came to demonstrate bulk staging in one paragraph and
forbid it sixteen lines later, and "remove the file" survived as advice for
files the agent never created and cannot prove are disposable.

The policy lives here as data and is rendered per surface, so the rungs cannot
diverge. The markdown surfaces cannot import Python; they are held to the same
contract by table in ``tests/unit/test_agent_instruction_resources.py``, which
checks the shared marker sentences below plus the forbidden-command list.

Safety rule that shapes the whole ladder: *unrelated is not the same as
disposable*. A file the agent did not create may be pre-existing operator work,
and deleting or reverting it destroys data the orchestrator cannot recover. Only
a file the agent created itself, and can positively identify as disposable, may
be removed. Everything else is preserved, ignored in place, or escalated.
"""

from dataclasses import dataclass
from enum import Enum

# Marker sentences. Runtime messages render these, and the markdown contract
# test requires them verbatim, so a reworded copy in one surface fails loudly
# instead of silently drifting from the others.
CLASSIFY_BEFORE_STAGING = "Classify each dirty file before staging anything."
NEVER_DESTROY_UNKNOWN = "Never delete or revert a file you did not create."
COMMIT_WHAT_BELONGS = "Commit the changes that belong in this push"
NEVER_STASH_WHAT_BELONGS = "Do not stash work that belongs in this push"

#: The escalation an agent runs when a dirty file cannot be classified. It is a
#: complete, runnable invocation on purpose: ``blocked`` requires both --reason
#: and --attempted, so a bare "run coding-done blocked" exits 1, and
#: test_completion_command_contracts executes any command it finds in an
#: instruction surface.
ESCALATION_COMMAND = (
    'coding-done blocked --reason "cannot classify dirty file <path>" '
    '--attempted "inspected the file and its history"'
)


@dataclass(frozen=True)
class RemediationRung:
    """One classification of a dirty file and the only action allowed for it."""

    classification: str
    action: str
    detail: str


#: The complete ladder, most-specific first. A file that does not positively
#: match rung 1 or rung 2 falls to rung 3, which never destroys anything.
REMEDIATION_LADDER: tuple[RemediationRung, ...] = (
    RemediationRung(
        classification="Part of the work you are pushing",
        action="stage that path explicitly and commit it",
        detail=(
            f"{NEVER_STASH_WHAT_BELONGS} -- stashing leaves HEAD on the commit "
            "you are about to replace, and the stashed change never reaches "
            "the push."
        ),
    ),
    RemediationRung(
        classification="A disposable artifact you created yourself",
        action="delete it",
        detail=(
            "Only take this path when you created the file during this session "
            "and can positively identify it as disposable, such as build output "
            "or a generated artifact you produced. Do not reach for .gitignore "
            "to clear the guard: an ignore rule is a repository policy change, "
            "and using one to hide a file is what this ladder exists to "
            "prevent. If an artifact genuinely should be ignored from now on, "
            "that rule is part of your change -- commit it under rung 1 on its "
            "own merits."
        ),
    ),
    RemediationRung(
        classification=(
            "Anything else -- pre-existing edits, files you did not create, "
            "anything you cannot positively classify"
        ),
        action="leave it exactly as it is and escalate",
        detail=(
            f"{NEVER_DESTROY_UNKNOWN} It may be operator or user work that "
            "cannot be recovered. Do not add it to .gitignore either: that "
            "commits repository policy about a path you just said you cannot "
            "classify, and it would hide a file someone else may be working "
            f"on. Report it instead: {ESCALATION_COMMAND} (or the needs_human "
            "status). Those two statuses are accepted on a dirty tree "
            "precisely so this path is reachable -- the file stays on disk, "
            "untouched, for a human to resolve."
        ),
    ),
)


def _rung_lines(prefix: str = "") -> tuple[str, ...]:
    return tuple(
        f"{prefix}{i}. {rung.classification}: {rung.action}. {rung.detail}"
        for i, rung in enumerate(REMEDIATION_LADDER, start=1)
    )


def guard_hint_lines(mode: str) -> tuple[str, ...]:
    """Lines the ``prepush-check`` dirty guard prints after the dirty list.

    ``mode`` is the configured ``validation.publish.dirty_check`` value. Only
    ``all`` enumerates untracked paths, so only ``all`` gets the sentence about
    them; the tracked modes must not tell an agent to act on files their dirty
    list never contained.
    """
    lines = [
        f"{COMMIT_WHAT_BELONGS} before running this gate: the gate records its "
        "result against HEAD and the pre-push hook reuses that record, so it "
        "must be recorded at the commit that gets pushed.",
        CLASSIFY_BEFORE_STAGING,
        *_rung_lines(),
    ]
    if mode == "all":
        lines.append(
            "This mode also lists untracked paths, which routinely include "
            "build output, local configuration, and secrets. Never commit one "
            "just to clear this gate."
        )
    return tuple(lines)


def blocked_reason(override_hint: str) -> str:
    """The reason ``coding-done`` reports when the tree is dirty."""
    return (
        f"Working tree is dirty; {COMMIT_WHAT_BELONGS.lower()} "
        f"(stashing leaves HEAD stale). {CLASSIFY_BEFORE_STAGING} "
        f"{NEVER_DESTROY_UNKNOWN} {override_hint}"
    )


def remediation_prompt_steps() -> str:
    """The numbered remediation block for any prompt that must clear a dirty tree.

    Shared verbatim by the orchestrator's dirty-worktree retry prompt and the
    review-exchange coder prompt. Rework rounds add commits every round, so the
    exchange prompt is re-issued each round and must not restate the ladder in
    its own words.
    """
    rungs = "\n".join(f"   {line}" for line in _rung_lines())
    return (
        f"2. {CLASSIFY_BEFORE_STAGING} For each one:\n"
        f"{rungs}\n"
        "3. Stage the paths from step 2 explicitly by name and commit them. "
        "Do not stage every changed file at once."
    )


def rejection_hint_lines(phase: str) -> tuple[str, ...]:
    """Lines the ``coding-done`` dirty rejection prints back to the agent.

    This is the surface an agent actually reads at the moment it is blocked, so
    it is the one most likely to be acted on literally. ``phase`` is
    ``"post-validation"`` when the validation run itself dirtied the tree, which
    changes only the framing sentence -- the ladder is identical, because the
    classification question is the same one either way.
    """
    opening = (
        "Validation modified the working tree (auto-formatter, generated "
        "artifacts, integration-test side effects, ...)."
        if phase == "post-validation"
        else "Commit the work that belongs in this push before calling coding-done."
    )
    return (opening, CLASSIFY_BEFORE_STAGING, *_rung_lines())


class DirtyTreeDisposition(Enum):
    """What a completion status is allowed to do with a dirty working tree."""

    REJECT = "reject"
    PRESERVE_AND_ESCALATE = "preserve_and_escalate"


#: Statuses where the agent is *reporting* a problem rather than publishing
#: work. These are rung 3's escalation, so a dirty tree is frequently the very
#: thing being reported: rejecting them would leave an agent that cannot
#: classify a file with no legal move at all -- unable to commit it, forbidden
#: to destroy it, and unable to hand it over -- which is the pressure that gets
#: someone else's uncommitted work deleted.
#:
#: These values are deliberately the shared vocabulary of two layers: they are
#: both ``AgentStatus`` strings at the completion CLI and ``CompletionOutcome``
#: values on the record the orchestrator reads. Both consult this one set, so
#: the CLI cannot accept an escalation the orchestrator will then reject.
#:
#: Accepting a dirty tree here does not publish anything unclean. Both statuses
#: do request PUSH_BRANCH (``STATUS_TO_ACTIONS``), and that is correct: a push
#: sends committed history, so the preserved files -- which are by definition
#: not in HEAD -- cannot ride along, while any work the agent *did* commit
#: before hitting the unresolvable file still reaches the remote instead of
#: being stranded in a worktree that later gets cleaned up. What the dirty gate
#: protects against is an agent believing uncommitted work was published; an
#: escalation says the opposite out loud, and names the files it left behind.
ESCALATION_STATUSES: frozenset[str] = frozenset({"blocked", "needs_human"})


def dirty_tree_disposition(status: str) -> DirtyTreeDisposition:
    """Decide how a dirty tree must be handled for ``status``.

    ``status`` is an ``AgentStatus`` string at the CLI boundary and a
    ``CompletionOutcome`` value at the orchestrator boundary; the two
    vocabularies share these spellings on purpose. Both callers ask this
    function rather than deciding for themselves, so a tree the CLI accepts
    cannot be rejected one boundary later.
    """
    if status in ESCALATION_STATUSES:
        return DirtyTreeDisposition.PRESERVE_AND_ESCALATE
    return DirtyTreeDisposition.REJECT
