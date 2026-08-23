# CRITICAL: You MUST call coding-done before exiting

There is NO other way to complete this session. If you exit without calling `coding-done`, your work is LOST and the session will time out, requiring human intervention.

Read the task-specific prompt file for what to do. Return here for how to signal completion.

---

## IMPORTANT: Clean Working Tree Required

Before calling `coding-done`, ensure your working tree is clean:

1. Run `git status --short` — if there are uncommitted files, **commit them**.
2. This includes generated artifacts from schema/contract changes, lock file updates, or any other files you modified.
3. Run `prepush-check --dirty-only -v`; it must pass before `coding-done`.
4. `coding-done` will **reject a dirty working tree** and exit non-zero.

Runtime-managed metadata under `.issue-orchestrator/` and `.claude/` is ignored by the orchestrator dirty guard. Tracked project files, generated sources, lock files, schemas, and other repo changes must still be committed or removed.

If you genuinely cannot commit certain files (e.g., they shouldn't be tracked), explain why in the `--problems` field.

---

## IMPORTANT: Commit First, THEN Run the Publish Gate

The publish validation suite is cached by **HEAD commit SHA**. A green result is
reused by the git pre-push hook that actually publishes your branch — and by any
later re-run of the gate itself — but only if it was recorded at the *exact
commit that gets pushed*.

So the order is:

1. Iterate with the fast/quick validation command while you work.
2. **Commit** once you believe the change is done.
3. **Then** run the full publish gate: `prepush-check -v` (or this repo's
   cache-aware wrapper around it, if it has one).
4. If it fails: fix, **commit again**, and re-run `prepush-check -v`.

Running the publish gate before committing is wasted work, twice over:

- The dirty-tree guard runs **first**, so on an uncommitted tree the gate exits
  non-zero without validating anything at all.
- If you commit after a green run, the result is recorded against the *parent*
  commit. Every later consumer misses the cache and re-runs the whole suite,
  which on a large repo can exceed the session's time budget and strand your
  work.

**Do not `git stash` to get past the dirty guard.** Stashing leaves HEAD on the
parent commit, so the gate records a result for a SHA you are about to
invalidate. Commit instead.

---

## IMPORTANT: Do Not Skip Tests

Do not disable, skip, quarantine, or weaken failing tests to make validation pass.
For JUnit/Kotlin/Java this includes `assumeTrue`, `assumeFalse`, `@Disabled`, and `@Ignore`.
Fix the code, improve the fixture, or report blocked with the specific reason.

---

## Completion Protocol

When your work is done (or you cannot proceed), call `coding-done` with the appropriate status:

**Completed successfully:**
```bash
coding-done completed \
  --implementation "What you did" \
  --problems "Any issues encountered, or 'None'"
```

If you discovered unrelated ancillary work while staying focused on the assigned issue, write those proposals to a JSON or JSONL file first, then add `--follow-up-file path` to the completed command above.
Each entry should include `title` and `reason`, and may include `evidence`, `suggested_labels`, and `blocking`.

**Cannot proceed - external blocker:**
```bash
coding-done blocked \
  --reason "Why you're blocked" \
  --attempted "What you tried" \
  --blocked-by 123 456 \
  --when-unblocked "Hint for resolution"
```
The `--blocked-by` and `--when-unblocked` options are optional.

**Cannot proceed - gave up:**
```bash
coding-done blocked \
  --reason "Could not complete: <why>" \
  --attempted "Tried X, Y, Z - none worked"
```

**Need human decision:**
```bash
coding-done needs_human \
  --question "What do you need answered?" \
  --context "Background info" \
  --options "Option A" "Option B" \
  --default "What to do if no response"
```
The `--context`, `--options`, and `--default` options are optional.

### Additional options

All statuses support:
- `--pr-labels label1 label2` - Extra labels to add to the PR
- `--dry-run` - Show what would be written without writing
- `--verbose` - Show detailed output

Completed status also supports:
- `--follow-up-file path` - Structured proposals for ancillary follow-up issues discovered during the work

## What happens after coding-done

1. **Dirty-file check** - coding-done verifies your working tree is clean
2. **Quick validation runs** (if configured) - fast tests, linting, type checks
3. **If validation fails**: coding-done exits non-zero. Fix the issues and run coding-done again.
4. **Preflight push check** - verifies the push will succeed
5. **If all checks pass**: Completion record is written
6. **Orchestrator takes over**: runs publish validation, pushes code, creates PR, posts comments, updates labels

You do NOT push code or touch GitHub directly. The orchestrator handles all external operations.

## If validation keeps failing

If you genuinely cannot fix the validation errors after multiple attempts:

```bash
coding-done blocked \
  --reason "Validation failing: <specific error>" \
  --attempted "Tried to fix by X, Y, Z"
```

This signals you need help without pretending the work is complete.
