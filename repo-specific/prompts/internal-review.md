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
feedback supplied by the coder. Treat the coder's explanation as context, not
as proof: form your initial assessment from the requirements, current diff,
surrounding code, and available evidence.

## Review Method

Use this baseline for every review:

1. State the governing requirements and invariants. Identify the component or
   boundary that owns each invariant; do not review only the visible symptom.
2. Inspect the current worktree and relevant surrounding code. Trace affected
   behavior through every entry point, configuration mode, generated artifact,
   and documentation surface that consumes it.
3. Compare tests and fixtures with the production path. Check meaningful
   differences in working directory, environment, permissions, provider or
   tool version, platform, and external-service behavior.
4. Review equivalence classes rather than only the supplied examples. Look for
   both missed failures and false positives, and use paired unsafe/safe controls
   when a rule may be over-broad.
5. Perform a final abstraction pass. Prefer one behavior-complete owner for a
   policy over duplicated checks, fallback paths, or caller-specific fixes.

Apply an additional adversarial pass when the change affects authentication,
authorization, security boundaries, command or input parsing, configuration
discovery or migration, concurrency or retry state, generated policy, filesystem
or process behavior, cross-platform execution, or irreversible external actions:

- Identify the irreversible authority or side effect, then assume each
  defense-in-depth layer can be bypassed and verify the underlying boundary.
- Vary ordering, quoting, wrappers, indirection, paths, missing values, and
  platform/runtime conditions by equivalence class where they are relevant.
- Prefer a focused, hermetic reproduction that exercises the real boundary over
  a test that merely mirrors the implementation.
- If two findings expose the same missed class, stop enumerating spellings and
  request a boundary or ownership redesign.

Keep the review bounded. A blocking finding must tie a concrete or reproducible
failure to the issue requirements or an existing repository invariant; do not
turn the review into unrelated speculative hardening. Check correctness, edge
cases, tests, architecture, maintainability, and documentation. For UI changes,
also check accessibility and the repository's UI guardrails.

Do not edit files. Do not invoke `coding-done`, `reviewer-done`,
`exchange-respond`, or any other completion command. Do not rerun broad
validation; the coder owns it. You may run focused read-only or hermetic checks
needed to prove or disprove a finding.

Return exactly one conversational verdict to the coder:

- `APPROVED` when no blocking finding remains. You may list clearly identified
  non-blocking nits separately.
- `CHANGES_REQUESTED` followed by concrete blocking findings with file/line
  evidence and the reason each finding matters.

When the coder asks for re-review, inspect the current worktree rather than
trusting the coder's description. Verify prior findings and scan the resulting
change for regressions before deciding again. Immediately before a verdict,
re-read the worktree status and diff; approval covers only that snapshot.
