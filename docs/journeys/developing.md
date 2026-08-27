# Developing

You're modifying Issue Orchestrator — adding features, fixing bugs, or extending the system.

## Dev setup

```bash
git clone https://github.com/BruceBGordon/issue-orchestrator.git
cd issue-orchestrator
make venv
source .venv/bin/activate
pytest tests/unit/ -x -q    # Verify the unit suite passes
```

If you're working on a feature branch, use a git worktree to keep the base repo clean:

```bash
make worktree-create BRANCH=my-feature
```

This one-shot command creates `../issue-orchestrator-wt-my-feature` from
`HEAD` and runs the complete worktree setup there. It does not depend on a
shell `cd` carrying over between commands. To choose a different starting ref
or destination, use:

```bash
make worktree-create \
  BRANCH=my-feature \
  BASE_REF=origin/main \
  WORKTREE_PATH=/absolute/path/to/my-feature-worktree
```

If Git creates the worktree but setup fails, the worktree is kept and the
command prints an explicit `make -C <path> worktree-setup` retry command.

## Understand the architecture

**[Architecture diagram](../architecture/README.md)** — The hex diagram shows how everything connects: entry points, control plane, ports, adapters, external systems.

**[AGENTS.md](../../AGENTS.md)** — This is the primary conventions guide for contributors. It's written to be directly actionable for coding agents, but the architecture rules and workflow constraints apply equally to humans. The key sections:

| Section | What you'll learn |
|---------|-------------------|
| Architecture Principles | Hexagonal, DI, layered separation, labels as truth, agent intent vs orchestrator authority |
| Key Ports | The foundational Protocol interfaces and a pointer to the full port set in `ports/` |
| Events vs Logs | Structured events drive the UI; logs are for humans. Never parse log text in code. |
| Fail-Fast Design | No fallbacks, no silent degradation. Crash on unexpected state. |
| Conventions | Where ports live, where adapters live, how to test, how to emit events |

## Find what you need to change

The codebase follows strict layered separation:

| Layer | Directory | Responsibility |
|-------|-----------|----------------|
| **Control** | `control/` | Decisions, policy, state advancement. Pure logic, no I/O. |
| **Observation** | `observation/` | Gather facts. No decisions, no mutations. |
| **Adapters** | `adapters/` | Concrete external-system integrations. |
| **Execution** | `execution/` | Runtime services, provider factories, and orchestration support code. |
| **Ports** | `ports/` | Protocol interfaces. Contracts between layers. |
| **Domain** | `domain/` | Models, state machines, events. |

If you're adding a new external integration, you'll add a Protocol in `ports/` and a concrete adapter in `adapters/`, then wire it through the composition root and execution/provider layer as needed. If you're changing decision logic, that's `control/`. The runtime facade lives in `infra/orchestrator.py` and should keep delegating rather than owning policy.

## Testing

```bash
pytest tests/unit/ -v              # Full unit suite
pytest tests/unit/test_foo.py -v   # Single file
pytest tests/e2e/ -v               # E2E tests (requires gh auth)
```

**Mock at port boundaries, not internal functions.** Create a mock that implements the Protocol, inject it, test the control logic in isolation. See [Testing Guide](../development/TESTING.md) for patterns and fixtures.

**Import-linter enforces architecture.** Control cannot import execution. Ports cannot import outer layers. If your change violates a boundary, `make validate` will catch it.

## Key workflows to understand

| Topic | Doc | When you need it |
|-------|-----|------------------|
| How agents complete work | `entrypoints/cli_tools/coding_done.py` / `entrypoints/cli_tools/reviewer_done.py` + AGENTS "Agent Intent, Orchestrator Authority" | Modifying completion processing |
| Code review loop | [Review Workflow](../development/REVIEW_WORKFLOW.md) | Modifying review, rework, or tech lead |
| Hook enforcement | [Hooks Architecture](../architecture/hooks.md) | Modifying safety guardrails |
| State machines | `domain/state_machines/` | Changing issue or review lifecycle |
| Events and observability | AGENTS "Events vs Logs" | Adding new observable behavior |

## Submitting changes

1. **Commit first, then run `make validate-pr`.** The gate records its green result against the current `HEAD` SHA, and the git pre-push hook reuses that record on push. Validate on an uncommitted tree and the dirty guard rejects it outright; commit *after* a green run and the record points at the parent commit, so the whole suite re-runs at push time
2. If the gate fails, fix it, commit the fix, and re-run `make validate-pr` — the green must land on the commit that actually gets pushed. Never `git stash` work that belongs in this push; stashing leaves `HEAD` on the commit you are about to replace and the stashed change never gets pushed. Classify each dirty file before staging anything: commit what belongs in the push, delete or `.gitignore` only artifacts you created and can positively identify as disposable, and preserve everything else. Never delete or revert a file you did not create, and never commit one just to clear the guard
3. `make validate-pr` is a superset of `make validate` (`validate-pr-raw` runs the same `_validate-impl` suite plus the agent lane), so run one or the other — running both validates the standard suite twice
4. CI mirrors `make validate-pr` by splitting the fast validate job and the agent-backed simulated/integration slices across separate required jobs
5. Use `make validate-pr-raw` only when you intentionally need to force the full uncached local suite at the same HEAD; it records nothing, so the pre-push hook will re-run the same suite
6. Tests must pass. If tests fail, fix them — don't defer.
7. [CONTRIBUTING.md](../../CONTRIBUTING.md) covers running tests from forks

## Development docs reference

These docs in `docs/development/` cover specific topics in depth:

| Doc | When to read |
|-----|--------------|
| [Testing Guide](../development/TESTING.md) | Test patterns, fixtures, mocking |
| [Troubleshooting](../development/TROUBLESHOOTING.md) | Debugging sessions, hooks, common issues |
| [Review Workflow](../development/REVIEW_WORKFLOW.md) | Code review pipeline, exchange mechanisms |
| [Debugging](../development/debugging.md) | Event system debugging |
| [Caching & ETags](../development/CACHING_ETAGS.md) | GitHub API caching implementation |
| [GitHub Auth Setup (Dev)](../development/GITHUB_TOKEN_SETUP.md) | Token resolution chain internals and GitHub App auth |
