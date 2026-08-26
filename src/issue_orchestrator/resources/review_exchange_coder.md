# Review Exchange Protocol — Coder

You are participating in an automated coder↔reviewer exchange.

## How to respond

Read the task-specific prompt file for what to fix. Rework prompts include the
reviewer's full current-round markdown report; treat that report as the source
of review details.

1. **Make the requested changes** in the worktree.
2. **Commit your changes** - the working tree must be clean.
3. **Run `prepush-check --dirty-only -v`** and fix any dirty-worktree failure before continuing. That check is the gate for *completing*; an escalation under rung 3 goes straight to the escalation command, which accepts the dirty tree on purpose.
4. **Run `coding-done completed --implementation "..." --problems "..."`** to record your completion and run validation.
5. **Then submit your verdict** by running the `exchange-respond` command:

**Applied fixes:**
```
exchange-respond ok --text "Fixed X, Y, Z as requested."
```

**Disagree with the feedback:**
```
exchange-respond disagree --text "This change is wrong because..."
```

## CRITICAL rules

- You MUST call `coding-done` first (this creates completion and validation artifacts).
- You MUST also run `exchange-respond` after coding-done succeeds to submit your verdict.
- **Commit before you run any publish/pre-push validation.** That suite is cached
  by HEAD commit SHA, and the green result is reused by the git pre-push hook.
  Validating first and committing after records the result against the parent
  commit, so the whole suite re-runs later. Each rework round adds commits, so
  this ordering matters every round. Never `git stash` work that belongs in this
  push - stashing leaves HEAD on the commit you are about to replace, and the
  stashed change never reaches the push.
- **Classify each dirty file before staging anything.** Stage the paths that
  belong in this push explicitly by name; never stage every changed file at
  once. A file you created yourself and can positively identify as disposable
  may be deleted or added to `.gitignore`. For anything else - pre-existing
  edits, files you did not create, anything you cannot positively classify -
  preserve it. Never delete or revert a file you did not create. It may be
  operator or user work that cannot be recovered; add an untracked path to
  `.gitignore` to clear the guard without touching it, or report
  `coding-done blocked --reason "cannot classify dirty file <path>" --attempted "inspected the file and its history"`.
- Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, and schemas that belong to this push must still be committed.
- Do NOT skip, disable, quarantine, or weaken failing tests. For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
- **DO NOT** call `reviewer-done`. That command is for reviewers, not coders.
- Both steps are required. Missing either one will cause a protocol error.
