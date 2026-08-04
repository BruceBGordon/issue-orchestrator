# Feature List

Issue-Orchestrator helps you use AI coding agents on a real repository without
giving up the controls that make software maintainable: scoped work, isolated
branches, validation, review, recovery, and human merge authority.

This page is organized by the problems an operator or engineering team needs to
solve when agents start doing more than one-off patches.

## Run Multiple Agents Without Branch Chaos

Use this when you want several agents working at once, but you still need each
change to be isolated, reviewable, and safe to publish.

- Creates a separate git worktree and branch for each issue so agents do not
  share a working copy.
- Lets you set the maximum number of concurrent agent sessions.
- Detects non-fast-forward push failures when another change landed first.
- Rebases and retries a stale branch push when the update is mechanical and the
  worktree is clean.
- Routes branch-behind and merge-conflict cases back to rework with explicit
  instructions when the update is not safe to fix automatically.
- Keeps issue state, branch state, PR state, and labels connected so parallel
  work does not silently overwrite or hide another agent's result.

## Keep Agents Inside a Defined Workflow

Use this when you want agents to contribute code, but not decide for themselves
that the work is finished.

- Claims eligible GitHub issues and moves them through queued, running,
  blocked, awaiting-merge, and completed states.
- Routes work to configured agent roles such as coder, reviewer, or tech_lead.
- Requires agents to finish through structured commands such as `coding-done`
  and `reviewer-done`.
- Treats the agent's completion message as a claim, then checks the work before
  advancing the issue.
- Supports completed, blocked, needs-human, review-approved, and
  changes-requested outcomes.
- Keeps humans responsible for merge decisions; agents cannot merge PRs.

## Catch Bad or Incomplete Agent Work Before It Reaches a PR

Use this when the important question is not "did the agent stop?" but "is the
result ready to review or publish?"

- Runs your repository's configured validation commands before work advances.
- Supports quick validation during coding/review loops and deeper publish
  validation before push or PR publication.
- Blocks publish when the worktree is dirty according to your configured
  policy, including tracked-only or all-file modes.
- Fails closed when dirty-file enumeration itself fails, instead of assuming the
  tree is safe.
- Re-runs validation when a cached validation record is missing or corrupt.
- Captures validation output, stdout/stderr paths, and structured test results
  when JUnit XML is configured.
- Shows validation failures as failure rows in the timeline, with links to the
  diagnostic artifacts.

## Turn Review Feedback Into Bounded Rework

Use this when you want an agent review loop, but still need the loop to stop
when it is not converging.

- Supports reviewer agents that approve work, request changes, report risk, and
  produce human-readable review reports.
- Sends reviewer feedback back into coder rework sessions.
- Tracks rework cycles so you can see how many times an issue has bounced.
- Bounds review and rework cycles with configurable limits.
- Supports local review loops, MCP-mediated loops, and draft-PR-mediated review
  when configured.
- Handles reviewer nits separately from blocking findings, with configurable
  policies for surfacing, addressing, or ignoring nits.
- Escalates repeated review-exchange timeouts or no-progress loops instead of
  burning agent time indefinitely.

## Recover From Crashes, Stuck Sessions, and Human Edits

Use this when you need the orchestrator to survive normal operational mess:
process restarts, label edits, stale claims, timed-out agents, and partial
state changes.

- Uses GitHub labels and observed worktree/PR state as durable external truth.
- Re-observes GitHub and local worktrees before important state changes.
- Cleans up stale in-progress labels when no matching session is running.
- Detects stale claims left by crashed or stopped orchestrator instances.
- Recovers awaiting-merge issues when PRs are merged, closed, behind, blocked,
  or failing checks.
- Waits for pending GitHub checks after publication, then escalates when checks
  stay pending too long.
- Moves ambiguous or unrecoverable states to blocked or needs-human instead of
  silently guessing.

## See What Happened in Every Agent Run

Use this when you need to debug an agent failure, audit a completed issue, or
explain why an issue moved to rework or needs-human.

- Provides a dashboard with queued, running, blocked, awaiting-merge, and
  completed columns.
- Shows an issue timeline with session starts, validation results, review
  decisions, rework rounds, publish attempts, and failure states.
- Captures terminal transcripts and session recordings for agent runs.
- Provides replayable session views so you can inspect what the agent actually
  did.
- Links timeline rows to validation artifacts, review reports, completion
  summaries, diagnostics, and run directories.
- Captures run audits for long-running or timed-out sessions when configured.
- Separates structured events for the UI from human-readable logs for
  debugging.

## Enforce Project Standards Instead of Prompting for Them

Use this when your repo already has standards and you want agents held to the
same checks as humans.

- Runs project-defined tests, linters, type checks, architecture checks,
  complexity checks, coverage gates, or any other configured validation command.
- Installs AI-agent hooks and git hooks that block supported bypass paths.
- Runs the effective pre-push hook before the orchestrator performs an
  authenticated push.
- Verifies hook installation so startup can fail fast when guardrails are
  missing or stale.
- Keeps orchestrator credentials separate from agent execution paths.
- Provides doctor checks for configuration, GitHub access, hook state, and
  local readiness before startup.

## Keep E2E Failures Connected to the Work That Caused Them

Use this when a target repo has meaningful end-to-end tests and you want those
results visible in the same control surface as issue work.

- Runs an async E2E runner in a configured worktree.
- Supports pytest-based and command-based runner modes.
- Captures native runner artifacts such as reports, traces, screenshots, and
  JUnit XML outputs.
- Supports quarantine files for known-flaky tests.
- Shows E2E run history and run details in the dashboard.
- Links E2E evidence back to related issue sessions when the run was driven by
  orchestrated work.

## Work From the Control Surface That Fits You

Use this when you want orchestration to be visible from your browser, editor,
automation, or AI assistant.

- Provides a local Control Center for managing repository engines and runtime
  state.
- Provides a browser dashboard for monitoring and controlling issue flow.
- Provides a VS Code integration for queues, sessions, diagnostics, worktree
  links, and dashboard access.
- Exposes a contracted subset of HTTP routes, described by
  `docs/api/ui-openapi.json`, for clients and automations. The remaining
  `/api/*` and `/control/*` routes are internal engine transport with no
  compatibility promise — see [Stability & API Surface](stability.md).
- Provides an MCP server entry point for editor and agent integrations. This is
  the intended external route for automation, but it is still **experimental**
  during `0.x` — tool names, arguments, and return shapes may change in any
  release.
- Supports pause, resume, stop, diagnostics, and guardrail repair controls from
  configured clients.

## Set Up and Operate Repositories Deliberately

Use this when you want setup to be explicit enough that agents inherit the same
repo context every time.

- Provides setup wizard flows for first-time configuration.
- Uses YAML configuration for agents, validation, review, worktrees, E2E,
  GitHub access, and UI behavior.
- Provides a generated configuration reference for supported settings.
- Seeds agent worktrees from the configured branch or seed ref.
- Supports local-only evaluation with `worktrees.seed_ref: HEAD`.
- Protects the local Control API with an admin token.
- Supports guardrail setup and repair flows for target repositories.
