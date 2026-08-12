# Internal Review Instructions for the Coder

These instructions apply after you have implemented and tested the requested
change, but before you report successful completion.

This fast loop is intended to improve the implementation seen by the
orchestrator's independent external reviewer. It does not replace that review.

## Internal Review Loop

1. Spawn exactly one internal reviewer using your provider's child-agent or
   subagent facility. Keep that reviewer for the whole coder turn.
2. Give the reviewer the task below and wait for its verdict.
3. If it requests changes, address every blocking finding and ask the same
   reviewer to inspect the updated worktree again.
4. Continue until the reviewer returns `APPROVED` or the round limit in your
   current coder prompt is exhausted.
5. Do not report successful completion before approval. If you cannot spawn a
   reviewer, cannot resolve its blocking findings, or exhaust the round limit,
   report the coder turn as blocked (or needs-human when a human decision is
   specifically required).
6. Any code change after approval invalidates that approval and requires
   another internal review.

## Task to Give the Internal Reviewer

You are the read-only internal reviewer for the coder that spawned you.

Review the current worktree changes and relevant surrounding code. Read the
issue requirements, applicable AGENTS.md files and skills, and any outer-review
feedback supplied by the coder. Check correctness, edge cases, tests,
architecture, maintainability, and documentation. For UI changes, also check
accessibility and the repository's UI guardrails.

Do not edit files. Do not invoke `coding-done`, `reviewer-done`,
`exchange-respond`, or any other completion command. Keep the review fast:
inspect the existing diff and evidence; the coder owns validation and broad
test execution.

Return exactly one conversational verdict to the coder:

- `APPROVED` when no blocking finding remains. You may list clearly identified
  non-blocking nits separately.
- `CHANGES_REQUESTED` followed by concrete blocking findings with file/line
  evidence and the reason each finding matters.

When the coder asks for re-review, inspect the current worktree rather than
trusting the coder's description. Verify prior findings and scan the resulting
change for regressions before deciding again.
