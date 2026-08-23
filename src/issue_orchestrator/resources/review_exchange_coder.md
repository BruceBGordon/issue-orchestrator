# Review Exchange Protocol — Coder

You are participating in an automated coder↔reviewer exchange.

## How to respond

Read the task-specific prompt file for what to fix. Rework prompts include the
reviewer's full current-round markdown report; treat that report as the source
of review details.

1. **Make the requested changes** in the worktree.
2. **Commit your changes** - the working tree must be clean.
3. **Run `prepush-check --dirty-only -v`** and fix any dirty-worktree failure before continuing.
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
  this ordering matters every round. Never `git stash` to get past the dirty
  guard - stashing leaves HEAD on the commit you are about to replace.
- Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, schemas, and other repo changes must still be committed or removed.
- Do NOT skip, disable, quarantine, or weaken failing tests. For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
- **DO NOT** call `reviewer-done`. That command is for reviewers, not coders.
- Both steps are required. Missing either one will cause a protocol error.
