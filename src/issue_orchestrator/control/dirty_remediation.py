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

# Marker sentences. Runtime messages render these, and the markdown contract
# test requires them verbatim, so a reworded copy in one surface fails loudly
# instead of silently drifting from the others.
CLASSIFY_BEFORE_STAGING = "Classify each dirty file before staging anything."
NEVER_DESTROY_UNKNOWN = "Never delete or revert a file you did not create."
COMMIT_WHAT_BELONGS = "Commit the changes that belong in this push"
NEVER_STASH_WHAT_BELONGS = "Do not stash work that belongs in this push"


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
        action="delete it, or add its path to .gitignore",
        detail=(
            "Only take this path when you created the file during this session "
            "and can positively identify it as disposable, such as build output "
            "or a generated artifact you produced."
        ),
    ),
    RemediationRung(
        classification=(
            "Anything else -- pre-existing edits, files you did not create, "
            "anything you cannot positively classify"
        ),
        action="preserve it and clear the guard without touching its contents",
        detail=(
            f"{NEVER_DESTROY_UNKNOWN} It may be operator or user work that "
            "cannot be recovered. An untracked path can be added to .gitignore, "
            "which clears the guard and leaves the file on disk untouched -- "
            "that edit makes .gitignore itself dirty, and it belongs in your "
            "commit. If the file cannot be cleared that way, stop and report "
            "coding-done blocked --reason \"cannot classify dirty file "
            "<path>\" --attempted \"inspected the file and its history\", "
            "rather than destroying it."
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


def retry_prompt_steps() -> str:
    """The numbered remediation block injected into the dirty-worktree retry."""
    rungs = "\n".join(f"   {line}" for line in _rung_lines())
    return (
        f"2. {CLASSIFY_BEFORE_STAGING} For each one:\n"
        f"{rungs}\n"
        "3. Stage the paths from step 2 explicitly by name and commit them. "
        "Do not stage every changed file at once."
    )
