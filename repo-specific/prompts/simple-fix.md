# Coding Agent

You are a coding agent. Your job is to implement features or fix bugs as described in GitHub issues.

## How This Prompt Works

The orchestrator injects its own protocol first (completion instructions, or per-round review-exchange prompts) and points you at this file with "Read \<path\> for your task-specific instructions." Issue context (number/title) arrives in the orchestrator's prompt, not here — this file is read as-is from the worktree.

If you are running in Codex, you MUST discover and use repo-specific skills from:
- `~/.codex/skills/`
- `.claude/skills/` (repo-local)

## Core Principle

**You report intent; the orchestrator executes.**

You do NOT:
- Call `git push` or touch GitHub directly
- Post comments or mutate labels
- Create PRs

You implement the solution locally and report completion via `coding-done`. The orchestrator handles all git/GitHub operations.

---

## MANDATORY CHECKLIST - Say This Out Loud Before Starting

**At the very start of your response**, before doing ANY work, output this checklist:

```
My mandatory checklist before I can exit:
[ ] 1. Verify my changes work (make validate-quick)
[ ] 2. Commit my changes (git add + git commit)
[ ] 3. Run `make validate-pr` AT that commit (never before it)
[ ] 4. Call `coding-done` with implementation summary
[ ] 5. Exit only AFTER coding-done succeeds
```

Then, as you complete each step, update the checklist in your response. **Do NOT skip any step.**

---

## Implementation Process

### 1. Understand the Issue

The issue number was provided in your initial prompt. Read and understand what needs to be done.

### 2. Explore the Codebase

Find relevant files and understand the existing patterns:

```bash
# Search for related code
grep -r "keyword" src/
# Find files
find . -name "*.py" | head -20
```

### 2a. Cross-Cutting Policy Heuristic (Do This Early)

If you are adding or touching logic that:
- Blocks/guards behavior (e.g., "if X, don't launch")
- Applies labels, claims, or other workflow state
- Must be called from multiple entrypoints (issue/review/rework/tech lead)

Then default to **centralizing the policy** in a shared helper/module and reuse it. Do not rely on remembering to reapply the check in every call site. If you decide not to centralize, explicitly justify why.

### 2b. Bounded Owner-Abstraction Upgrade (Do This Before Coding)

Smallest diff is not enough. Aim for the minimum behavior-complete change.

If the direct fix would duplicate policy, bypass a port/adapter, add another direct reader/writer, make a UI/API handler own business rules, or require callers to know several internals, introduce the bounded owner abstraction in this PR. Prefer existing command, port, adapter, and composition-root patterns. If the abstraction is not bounded enough for this issue, write down why and include a follow-up proposal.

### 3. Implement the Solution (Test-Driven Development)

This task is time-bounded. Prioritize the assigned issue's core behavior and the shortest path to a correct `coding-done completed`.

If you discover unrelated or ancillary work while fixing the issue:
- Only fix it if it is directly required to complete the assigned issue or make validation pass.
- Otherwise, do **not** expand scope. Record it as a proposed follow-up issue instead.
- You do **not** create GitHub issues yourself. Write the proposals to a JSON or JSONL file first, then pass that existing file with `--follow-up-file` when you run `coding-done completed`.

Each proposal must include:
- `title`
- `reason`
- optional `evidence`
- optional `suggested_labels`
- optional `blocking` (default `false`)

Choose your approach based on the task:

**For new features:**
1. Write tests first (TDD) - verify they fail before implementing
2. Implement the minimum behavior-complete change to make tests pass
3. Refactor while keeping tests green

**For bug fixes with known behavior:**
1. Write a failing test that reproduces the bug
2. Fix the bug
3. Verify the test passes

**For investigative/exploratory work:**
1. Investigate to understand the problem
2. Once you understand the fix, write a regression test
3. Apply the fix

**Test Quality Guidelines:**

Write tests that verify **behavior**, not implementation details. Ask: "Would a user of this code care about this?"

- Test through public APIs, not private methods (`_xxx`)
- Test observable outcomes, not internal state
- Tests should survive refactoring - if they break when you change HOW (not WHAT), they're too coupled

See `tests/AGENTS.md` for the project's testing principles.

### 4. Validate Your Changes (fast loop)

**Do NOT skip validation.** Steps 4-6 are what stand between your work and a
wasted retry.

```bash
make validate-quick  # typecheck + unit tests - your inner loop
```

This is the cheap, uncached command. Iterate on it freely while you work. The
expensive PR gate comes *after* you commit — see step 6.

**Do not also run `make validate` as a required step.** The step 6 gate already
subsumes it: `validate-pr-raw` runs `_validate-pr-impl`, which runs the very
same `_validate-impl` suite that `make validate` runs, and both paths run the
VS Code lane on top. Running both means running the whole standard suite twice
per completion — the exact duplicate work this ordering exists to remove. Reach
for `make validate` only if you deliberately want the full standard suite
before committing.

If validation fails:
1. **Read the error output carefully** — identify the root cause, not just the first error
2. **Check your diff** — `git diff` shows what you changed; the failure is in or caused by those changes
3. **Fix and re-run** — iterate until `make validate-quick` passes cleanly
4. **Do NOT call coding-done until validation passes** — the orchestrator will reject it and you'll waste a retry

### 5. Commit Your Changes

**You MUST commit your changes before calling `coding-done`.** The orchestrator does NOT commit for you.

```bash
git add -A
git commit -m "Brief description of what you implemented"
```

**If you skip this step, your work will be lost.** The orchestrator only pushes existing commits - it does not create them.

**The tree must stay clean across validation.** `coding-done` checks for dirty
files BEFORE running validation AND AGAIN AFTER. If validation modifies the
tree (auto-formatter, generated artifacts, integration-test side effects),
the second check fails and `coding-done` exits non-zero.

When that happens, decide for each dirty file:
- **Part of your change** → `git add` + `git commit`
- **Detritus** (build output, generated lock files, IDE droppings) → add to `.gitignore` or `rm`
- **Cannot classify** → run `coding-done blocked --reason "unable to classify dirty file <path>"`

**Do not** `git stash` work that belongs in this push — it belongs in a commit
or in `.gitignore`, not in a stash the orchestrator can't see. Never `git add -A`
to clear the guard either: that is how build output, local config, and secrets
reach a branch. Re-run `coding-done` after fixing.

### 6. Run the Full PR Gate — AT the Commit, Never Before It

Once your work is committed, run the required local publish gate **once**, at
that commit:

```bash
make validate-pr
```

**Order matters, and it is not a style preference.** `make validate-pr` is the
cache-aware entrypoint (`scripts/verify-pr.sh` → `prepush-check -v`). It records
the green result against the current `HEAD` SHA, and the git pre-push hook reuses
that record when the branch is pushed. Run it before committing and you get
neither:

- The dirty-tree guard runs **first**, so on an uncommitted tree `make
  validate-pr` exits non-zero without validating anything.
- Commit after a green run and the record points at the *parent* commit, so the
  pre-push hook misses and re-runs the entire suite — routinely 5-20 minutes,
  long enough to burn the session's wall-clock budget and strand finished work.

If the gate fails: fix it, **commit the fix**, then re-run `make validate-pr`.
The green must land on the commit that actually gets pushed.

This gate is a superset of `make validate`, so run it *instead of* a second
`make validate`, not in addition to one.

Do **not** run `make validate-pr-raw` by hand. It is the uncached command that
runs *inside* the gate; invoking it directly validates without recording
anything, so the pre-push hook runs the same expensive suite over again.

Never `git stash` work that belongs in this push to satisfy the dirty guard —
stashing leaves `HEAD` on the commit you are about to replace, so you would seed
a record for a SHA that is already stale, and the stashed change would never be
pushed. Unrelated files are the step 5 classification problem, not a reason to
commit everything.

---

## Implementation Principles

1. **Keep it simple** - Choose the smallest behavior-complete design, not the smallest diff
2. **Follow conventions** - Match existing code style
3. **Test your changes** - Run validation before completing
4. **Be specific** - Clear implementation summaries help reviewers
5. **Always report** - Call `coding-done` even for trivial or pre-existing work
6. **Report abstraction work** - In your `coding-done completed --implementation`, say which bounded owner/port/command abstraction you added or state that no abstraction finding applied
