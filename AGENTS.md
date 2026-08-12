# AI Assistant Guide for issue-orchestrator

---

## STOP - Read This First

**Before making ANY code changes, create a git worktree.**

```bash
make worktree-create BRANCH=my-branch-name
```

This one-shot target creates `../issue-orchestrator-wt-my-branch-name` from
`HEAD` and runs the complete setup there (venv, VS Code extensions, and
Playwright). Use `BASE_REF=<ref>` or `WORKTREE_PATH=<path>` to override its
defaults. If setup fails after Git creates the worktree, the command preserves
it and prints the exact `make -C ... worktree-setup` command to retry.

This is not optional. Do not edit files in the base repo. If the user explicitly says "edit directly" or "no worktree", then and only then may you skip this. Otherwise: **worktree first, then work.**

This instruction has been ignored multiple times. Don't be the next one.

---

## No‑Surprise Rule (Required)

Before starting any work, post a short “Plan & Scope” message:
- Goals: what you believe the user wants
- Will do: concrete deliverables
- Will not do: explicit exclusions
- Unknowns: assumptions or areas needing confirmation

If scope changes while working, post an updated Plan & Scope before proceeding.

## Alignment Check Trigger

If a user says “discuss” or “explore”, do not implement until you:
- Summarize the proposed approach in 3–6 bullets
- Ask for confirmation to proceed

## UI Changes

For UI-facing changes, follow `.claude/skills/frontend-design/SKILL.md`; task is incomplete without required non-UI behavior tests and UI guardrail tests.

Accessibility is required scope for every UI change:
- Preserve semantic controls first (native buttons, links, inputs, `details`/`summary`) before adding ARIA.
- All interactive controls must be keyboard reachable, have a visible focus state, and expose an accessible name.
- Expanded/collapsed content must keep its semantic relationship (`aria-controls`, labelled regions, or native disclosure semantics as appropriate).
- Layout changes must not clip text, focus rings, controls, or expanded content at supported viewport sizes.
- Color cannot be the only status signal; maintain text/icons and sufficient contrast in light and dark themes.

Reviewers must explicitly check accessibility for UI changes. If issues exist, list them as implementation-required findings; if none exist, state: `Accessibility review: no issues found.`

## What This Is

**issue-orchestrator** orchestrates multiple AI agents working on GitHub issues in parallel. It fetches issues, creates git worktrees, launches agent sessions, enforces structured completion via `coding-done`/`reviewer-done`, and manages the full lifecycle including code review and tech_lead.

## Architecture Principles

1. **Hexagonal (Ports & Adapters)** - Core defines Protocol interfaces (ports), adapters implement them. External systems are abstracted behind ports.

2. **Dependency Injection** - Orchestrator receives dependencies via constructor, not globals. `entrypoints/bootstrap.py` is the composition root that wires everything.

3. **Layered Separation**:
   - **Observation** (`observation/`) - Gathers facts, no decisions
   - **Control** (`control/`, `orchestrator.py`) - Makes decisions, advances state
   - **Execution** (`execution/`) - Talks to external systems, no policy

4. **Labels as Source of Truth** - Crash-safe: GitHub labels persist, orchestrator recovers state from labels on restart.

5. **Agent Intent, Orchestrator Authority** - Agents write a `CompletionRecord` expressing intent (what they did, what they want). The orchestrator validates it as untrusted input, decides what to do, and executes via adapters. Agents cannot push code or create PRs directly.

## Key Ports (Protocols)

| Port | Purpose | Adapter |
|------|---------|---------|
| `EventSink` | Fire-and-forget trace events | `PluggyEventSink` |
| `SessionRunner` | Terminal session management | `PluggySessionRunner` |
| `IssueTracker` | GitHub issue operations | `GitHubAdapter` |
| `SessionStore` | Persist session state | `JsonSessionStore` |

These are the foundational ports. See `ports/` for the full set, including `WorkingCopy`, `WorktreeManager`, `RepositoryHost`, `CommandRunner`, and others.

## Composition Root

```python
# entrypoints/bootstrap.py - the ONLY place that wires dependencies
from .entrypoints.bootstrap import build_orchestrator

orchestrator = build_orchestrator(config)  # Production
orchestrator = build_orchestrator_for_testing(config, events=mock, runner=mock)  # Tests
```

## Core Files

| File | Purpose |
|------|---------|
| `src/issue_orchestrator/entrypoints/bootstrap.py` | Composition root - wires all dependencies |
| `src/issue_orchestrator/infra/orchestrator.py` | Main facade, session lifecycle, delegates to control/services |
| `src/issue_orchestrator/entrypoints/cli.py` | CLI commands, calls `build_orchestrator()` |
| `src/issue_orchestrator/entrypoints/cli_tools/agent_done.py` | Shared completion core (used by `coding-done` and `reviewer-done`) |
| `src/issue_orchestrator/entrypoints/cli_tools/coding_done.py` | `coding-done` CLI - coding/rework agent completion |
| `src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py` | `reviewer-done` CLI - review agent completion |
| `src/issue_orchestrator/ports/` | Protocol definitions (EventSink, SessionRunner, etc.) |
| `src/issue_orchestrator/adapters/` | Concrete external-system adapters |
| `src/issue_orchestrator/execution/` | Runtime services, provider factories, and orchestration support code |

## Completion Commands

Agents MUST use `coding-done` or `reviewer-done` to complete. Direct `git push` is blocked by hooks.

```bash
# Coding/rework agents:
coding-done completed --implementation "What was done" --problems "None"
coding-done blocked --reason "Why" --attempted "What was tried"
coding-done needs_human --question "Need clarification on X"

# Review agents:
reviewer-done approved --summary "Code looks good" --risk low
reviewer-done changes_requested --issues "Missing tests" --risk medium
```

## Detailed Documentation

| Topic | File | When to Read |
|-------|------|--------------|
| Architecture overview | [docs/architecture/README.md](docs/architecture/README.md) | System diagram, core principles |
| Review workflow | [docs/development/REVIEW_WORKFLOW.md](docs/development/REVIEW_WORKFLOW.md) | Code review, tech lead, rework cycles |
| Troubleshooting | [docs/development/TROUBLESHOOTING.md](docs/development/TROUBLESHOOTING.md) | Debugging, common problems |
| Hook enforcement | [docs/architecture/hooks.md](docs/architecture/hooks.md) | Guardrails, agent restrictions |

## Skills (Auto-Invoked)

Skills in `.claude/skills/` are automatically invoked when working on relevant areas:

| Skill | Triggers When |
|-------|---------------|
| `architecture` | Working on ports, adapters, DI, bootstrap |
| `troubleshooting` | Debugging sessions, hooks, locks |
| `review-workflow` | Code review pipeline, tech lead, rework |
| `session-replay` | Session artifact capture, replay endpoints, timeline/session correlation, and emulator-backed session viewing |
| `schema-updates` | Updating UI contracts, SSE payloads, or config schemas |
| `github-token-rotation` | Rotating expiring GitHub PATs or identifying the repo-scoped auth source issue-orchestrator uses |

## Directory Context (AGENTS.md)

Test and source directories have local `AGENTS.md` files auto-read when working there. `CLAUDE.md` remains as a compatibility symlink to the same content.
- `tests/unit/AGENTS.md` - Unit test patterns, fixtures, mocking
- `tests/e2e/AGENTS.md` - E2E setup, gh auth, test-data isolation
- `tests/integration/AGENTS.md` - Integration test patterns

## Quick Reference

**Run tests:**
```bash
pytest tests/unit/ -v          # unit test suite
pytest tests/e2e/ -v           # Live e2e tests (requires gh auth)
```

## Async E2E Runner

The orchestrator includes an async E2E test runner. See [docs/user/e2e.md](docs/user/e2e.md) for details.

**Configuration:** `.issue-orchestrator/config/modes/<mode>/<name>.yaml` - see `examples/config.example.yaml`

## Events vs Logs (Key Patterns)

**Events** (machine-consumable):
- Audience: Web UI, tests, automations
- Stable names from `EventName` enum (`events/catalog.py`)
- Structured payloads with `run_id`, `tick_id`, `schema` version
- Emit via `self.events.publish(TraceEvent(EventName.TICK_STARTED, ctx.enrich({...})))`

**Logs** (human-consumable):
- Audience: Developers reading console/CI logs
- Use `logging.getLogger(__name__)` with levels DEBUG/INFO/WARNING/ERROR
- Include context via `extra=log_context(tick_id=1, issue_key="M1-011")`
- Can change freely - UI/tests must NOT parse logs

**Golden Rule**: UI and tests react to events, never parse log text.

```python
# Events - for UI/test synchronization
from .events import EventName, EventContext
self.events.publish(TraceEvent(EventName.TICK_COMPLETED, ctx.enrich({"idle": True})))

# Logs - for debugging
logger.info("[PLAN] %d action(s)", count, extra=log_context(tick_id=5))
```

## Schema Contracts (Public UI + Settings)

- **UI contracts** live in `src/issue_orchestrator/contracts/public.py`.
- **Generated JSON schemas** live in `contracts/public/*.json` (regen via `python scripts/generate_public_contracts.py`).
- **Settings schema** lives in `src/issue_orchestrator/infra/settings_schema.py` and drives `docs/user/configuration_reference.md`.
- Drift is enforced by `tests/unit/test_public_contract_schemas.py` and `tests/unit/test_settings_schema.py`.

## Fail-Fast Design

**Fallbacks are strongly discouraged.** While there may be rare cases where appropriate, the default stance is:

- **Strong typing over optionals** - If a value must exist, don't make it `Optional`. Let the type system enforce it.
- **Runtime failures for None** - Prefer crashing on unexpected `None` over silently returning defaults. A crash tells you exactly where the bug is.
- **No silent degradation** - Don't hide problems behind fallback behavior. If something is wrong, fail loudly.
- **Less to test** - Fallback paths are code paths. Every fallback doubles your test matrix. Fail-fast means fewer branches to cover.
- **Don't hide bugs** - A fallback that "works" masks the real issue. The bug still exists, you just can't see it anymore.

```python
# BAD - hides bugs, more to test
def get_session(self, id: str) -> Session | None:
    return self._sessions.get(id)  # Caller might forget to check None

# GOOD - fails fast, less to test
def get_session(self, id: str) -> Session:
    if id not in self._sessions:
        raise SessionNotFoundError(id)  # Bug is immediately visible
    return self._sessions[id]
```

## Conventions

- Ports in `ports/`, concrete integrations in `adapters/`, runtime composition/support in `execution/`
- Tests mock at port boundaries, not internal functions
- Events via `self.events.publish(TraceEvent(...))`, never direct pluggy
- Session ops via `self.runner.*`, never direct plugin manager
- All orchestrator dependencies injected via constructor

## Abstraction Heuristics

- Favor higher-level abstractions when they improve clarity, conciseness, or testability.
- If callers must rummage across disparate classes/fields to accomplish a task, consider introducing a higher-level port or helper.
- Entry points should depend on behavior-level ports, not storage or transport details.
- Abstraction trigger: if implementing one policy requires touching multiple internal fields/classes, stop and introduce or extend a behavior-level abstraction first.
- Shared state rule: do not mutate shared state collections directly from entrypoints/controllers when policy enforcement is required; route through an owner abstraction with explicit outcomes.
- Review classification: if this rule is violated, classify as `Correctness Risk` when a concrete invariant can be bypassed, otherwise classify as `Design Smell`.
- Smallest diff is not a sufficient design goal. Prefer the minimum behavior-complete owner/port/command abstraction when the direct fix would scatter policy or duplicate access rules.

## Command Surface Testing

When behavior is exposed to the UI, route it through the existing typed command / owner-port pattern instead of ad hoc handlers. Tests must cover both sides of the boundary: producer/base system to command payload or port request, and command payload to UI handler/rendered output. Reviewers should treat missing coverage on either side as implementation-required, not a nit.

## GitHub API Discipline

GitHub CLI/API calls are a limited resource. Be mindful of command volume and avoid unnecessary scans or polling. Inefficient usage forces expensive systemic tuning later, so prefer cached data, targeted reads, and minimal refreshes whenever possible.
Direct `gh` CLI usage from Python is forbidden; token resolution must use explicit config/env or OS keychain/hosts.yml.

## Think Like a Long-Term Owner

**You are not here to complete tasks. You are here to build lasting value.**

The difference between a contractor and an owner: a contractor finishes the task and moves on. An owner asks "will I be proud of this in six months?" Don't just make it work—make it right.

### Seek the Right Abstractions

When you're about to add a few lines to an already bloated method, stop. That's the contractor move. The owner move is:

1. **Recognize the smell** - If a method is already long and complex, adding more lines makes it worse
2. **Find the hidden concept** - There's usually an abstraction trying to emerge. Name it.
3. **Extract and refactor** - Create the abstraction, then migrate existing code to use it
4. **Leave it better** - The next person should find clean seams, not more spaghetti

```python
# Contractor: adds to the pile
def process_session(self, session):
    # ... 80 lines of existing code ...
    # NEW: handle the edge case
    if session.state == "weird":
        do_thing_a()
        do_thing_b()

# Owner: finds the abstraction
def process_session(self, session):
    handler = self._get_state_handler(session.state)
    handler.process(session)
```

### Be Relentlessly Thorough

"It looks right" is not the same as "it is right." Don't stop at the first thing that works.

- **Testing UI?** The row headers look good—now validate every list item. Check the empty state. Check overflow.
- **Writing a function?** The happy path works—now what about nulls? Empty collections? Concurrency?
- **Fixing a bug?** The symptom is gone—did you fix the root cause? Are there similar bugs elsewhere?

The work isn't done when it *appears* done. The work is done when you've verified every assumption and edge case.

### Own the Tests

**Goal**: Actually test the system, not just get tests passing in name only.

**Anti-patterns:**
- Using `@pytest.mark.skip` to hide infrastructure requirements - tests should FAIL if prerequisites are missing
- Treating test failures as "not my problem" - if tests fail, investigate and fix
- Writing tests that pass but don't verify important behavior
- Skipping tests because they're hard to set up - that difficulty is valuable feedback

**Owner mindset:**
- Tests exist to catch problems BEFORE production. A skipped test catches nothing.
- If a test requires infrastructure (GitHub token, Claude CLI, etc.), failing clearly tells someone to set it up.
- The goal isn't green CI - it's a system that works correctly under real conditions.

**When tests fail:**
1. Investigate the root cause
2. Fix the underlying issue (not just the symptom)
3. Verify the fix actually solves the problem
4. Never dismiss failures as "someone else's job"

### Final Abstraction Pass (Required)

Before finishing any review or code change, run one final pass focused on abstraction integrity.

Check for:
1. Policy scattered across multiple call sites that should have one owner abstraction.
2. Entry points/controllers touching storage/state internals directly instead of owner APIs.
3. Shared mutable state writes outside the owning boundary.
4. Callers requiring knowledge of multiple internals to complete one task.
5. Cross-path rule drift where the same rule is enforced differently by path.

Decision rule:
- If an abstraction issue is found, implement the abstraction fix in this PR by default.
- Deferral is allowed only when the abstraction fix is a substantial undertaking that would materially expand scope or risk.
- If deferred, a follow-up issue must be created immediately with clear scope, named owner, due milestone/date, risk statement, and link from the PR/review.

Review output requirement:
- Reviewers must list abstraction findings as implementation-required unless explicitly deferred under the decision rule above.
- If none exist, state: `Final abstraction pass: no issues found.`

Coding output requirement:
- Coders must state which abstraction findings were implemented in this PR.
- If any were deferred, include the follow-up issue link and required metadata listed above.
