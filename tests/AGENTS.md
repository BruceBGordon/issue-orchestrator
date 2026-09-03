# Testing Principles

## Determinism Required

**Deterministic tests only.** Any timing-based coordination is forbidden.

- Do **not** use `sleep` (including `asyncio.sleep`) to “wait for” work.
- Do **not** rely on “eventually” semantics, background timing, or real clocks.
- Do use deterministic control points: explicit callbacks, single-tick helpers, mocked time, or injected schedulers.
- If a test can flake under load or on CI, it’s wrong—fix the test, not the timeout.
- Exception: tests that must wait on **real external systems** may use bounded waits, but should still prefer explicit readiness/ack signals over sleeps.

---

## Spawned Processes Must Not Outlive the Run

Whoever spawns a real process owns its death. On 2026-08-29 twenty leaked CPU
burners spun for nine hours, flaked seven gates across four unrelated branches,
and were misattributed twice (#7142). The cost lands on whoever runs the *next*
gate, who cannot see the cause.

Reach for `tests/load_fixture`:

```python
from tests.load_fixture import cpu_load, reap_marked_processes

with cpu_load(workers=8, max_lifetime_seconds=60.0):
    ...  # load is up here, and provably gone after the block

try:
    ...  # the system under test spawns a tree of its own
finally:
    reap_marked_processes(str(tmp_path))  # identity is the argv, not a remembered pid
```

For a live CLI under a timeout, `tests/process_group_run` is the owner.

The rule, whichever helper you reach for:

1. Own the group (`start_new_session=True`) so one signal reaches descendants.
2. Reap in `finally` — not after the asserts, not in `except TimeoutExpired`. A
   failing assert is exactly when the fixture is still alive.
3. Escalate to SIGKILL. Fixtures worth writing often ignore SIGTERM on purpose.
4. Give the spawned process its own deadline, so a killed harness still caps the
   damage.

Ad-hoc verification counts too: `python -m tests.load_fixture --workers 20
--seconds 60`, never a shell loop of `python -c 'while True: pass'`.

---

## Test Behavior, Not Implementation

**The cardinal rule**: Tests should verify WHAT the code does, not HOW it does it.

When you test implementation details (private methods, internal state), you:
- Couple tests to code structure, so refactoring breaks tests
- Miss actual bugs because you're testing the wrong thing
- Create maintenance burden as implementation evolves

### The Litmus Test

Before writing a test, ask: **"Would a user of this code care about this?"**

- If yes → test it through the public API
- If no → don't test it directly (it's an implementation detail)

## Typed Run Assets in Tests

- Active session/completion/review-exchange tests must construct and inject
  typed run assets such as `SessionRunAssets` or `ReviewExchangeRunAssets`.
- Do not omit required run assets from positive-path test sessions. Missing
  `run_dir` belongs only in explicit negative tests that assert fail-fast
  behavior.
- Prefer builders/fakes that fail when production code attempts fallback
  discovery. A passing test must not depend on "latest run" lookup,
  session-name scans, alternate-name recovery, or worktree rummaging.

---

## Never Access Private Members (`_xxx`)

**Do not access `_private` members in tests.** This is enforced by ruff's SLF001 rule.

If you find yourself needing to access a private member, stop and ask why:

| Smell | What It Means | Fix |
|-------|---------------|-----|
| `obj._method()` | Testing implementation | Test through public API instead |
| `obj._cache.set(...)` | Missing dependency injection | Inject the collaborator |
| `assert obj._state == X` | Testing internal state | Test observable outcome |
| `obj._flag = False` | Missing configuration surface | Make it a constructor param |

### Example: Testing Cache Behavior

```python
# BAD - Reaches into internals
def test_cache_stores_labels(self, adapter):
    adapter.get_issue_labels(42)
    cached = adapter._cache.get_issue_labels(42)  # SLF001 violation!
    assert cached == ["bug"]

# GOOD - Tests observable behavior
def test_subsequent_calls_use_cache(self, adapter, mock_http):
    adapter.get_issue_labels(42)  # First call hits API
    adapter.get_issue_labels(42)  # Second call should use cache
    assert mock_http.get_issue_labels.call_count == 1  # Only one API call
```

### Example: Testing Helper Methods

```python
# BAD - Tests private helper directly
def test_build_labels(self, orchestrator):
    labels = orchestrator._build_labels("agent:web")  # SLF001 violation!
    assert labels == ["agent:web", "test-data"]

# GOOD - Tests through public behavior
def test_session_labels_include_filter(self, orchestrator, mock_github):
    orchestrator.start_session(issue)
    # Verify the labels that were actually applied
    call_args = mock_github.add_labels.call_args
    assert "test-data" in call_args[0][1]
```

---

## Dependency Injection Over Internal Access

If a test needs to control or observe a collaborator, that collaborator should be **injected**, not accessed internally.

```python
# BAD - Test manipulates internal collaborator
def test_with_cache_disabled(self, adapter):
    adapter._cache_enabled = False  # Reaching into internals
    ...

# GOOD - Collaborator is injected
def test_with_cache_disabled(self):
    adapter = GitHubAdapter(cache_enabled=False)  # Configuration at construction
    ...

# BETTER - Cache is a separate injectable dependency
def test_cache_behavior(self):
    cache = InMemoryCache()
    cache.set_issue_labels(42, ["bug"])
    adapter = GitHubAdapter(cache=cache)  # Inject the collaborator
    ...
```

---

## What to Test

### DO Test
- **Public method behavior** - Given inputs, verify outputs/effects
- **Error handling** - Given bad inputs, verify appropriate errors
- **State transitions** - Given action, verify observable state change
- **Integration points** - Verify collaborators are called correctly (via mocks)

### DON'T Test
- **Private methods directly** - They're implementation details
- **Internal state** - Test observable outcomes instead
- **How something works** - Test what it does

---

## Mock at Port Boundaries

This codebase uses hexagonal architecture. Mock at the **port** level, not inside implementations.

```python
# BAD - Patching deep internals
with patch('issue_orchestrator.execution.github_http.GitHubHttpClient'):
    ...

# GOOD - Mock the port interface
mock_github_adapter.get_issue_labels.return_value = ["bug"]
orchestrator = create_orchestrator(github=mock_github_adapter)
```

See `tests/unit/conftest.py` for pre-built mock adapters.

---

## When You Can't Test Without Private Access

If you genuinely can't verify behavior without accessing internals, this signals a design issue:

1. **Extract a collaborator** - The private thing should be its own class with a public interface
2. **Add observability** - The class needs a way to report its state publicly
3. **Question the test** - Maybe this behavior doesn't need explicit testing

**Never "fix" this by making `_private` → `public`.** The author marked it private for a reason. Fix the design, not the visibility.

---

## Test Pyramid: Prefer Integration over Browser

**Default to the cheapest layer that can prove the behavior.** Playwright tests are slow (~7s/test) and flaky-prone; treat them as a scarce resource.

| Layer | Cost | Use for |
|-------|------|---------|
| Python unit | <50 ms | Pure logic, parsers, view-model shape, port behavior |
| **JS-vm** (`tests/js/`, `node:test` + `vm`) | **<5 ms** | **JS rendering, click dispatch, fetch behavior, command-pattern handlers** |
| Python integration | <500 ms | HTTP endpoints (`TestClient`), SQLite-backed flows, multi-component wiring |
| Playwright (`tests/e2e_web/`) | ~7 s | True browser concerns only |

### When to use Playwright (and when NOT to)

**Use Playwright for:**
- Cross-page navigation, modal lifecycle, focus management
- Real DOM events the JS-vm can't simulate (paint timing, layout, drag/drop, file pickers)
- One end-to-end smoke per major surface, proving the wiring (view-model → API → JS → DOM) actually works in a real browser

**Do NOT use Playwright for:**
- Verifying which handler fires when a button is clicked → JS-vm test of the dispatch function
- Verifying HTML structure (pills, classes, attributes) → JS-vm test asserting on rendered string
- Verifying API request shape on click → JS-vm test with stubbed `fetch`
- Verifying error/empty states → JS-vm test with the matching response stub

### The command pattern lets you skip Playwright

Click handlers in this codebase route through dispatch contracts (`data-e2e-action`, `data-lifecycle-command`, `data-artifact-path`) rather than inline closures. Tests at the JS-vm layer call the dispatcher directly with a synthetic button (`{ dataset: {...} }`) and assert on which window-scoped helper got called and with what arguments — no DOM, no browser.

Reference: `tests/js/e2e_run_view_actions.test.js` covers every clickable surface in `e2e_run_view.js` (row actions, lifecycle commands, artifact buttons, filter chips, row toggle, lazy-load) at the dispatch layer. New click surfaces should be tested there first; only escalate to Playwright if the test genuinely needs a browser.

### Substring guardrails are a backstop, not a primary

`tests/unit/test_dashboard_ui_guardrails.py` greps function bodies for specific strings ("must contain `data-needs-fetch`"). It catches accidental deletion of structural patterns but doesn't verify behavior — a typo or wrong wiring still passes substring checks. Use it sparingly for cross-cutting structural rules ("filter dispatch is panel-scoped, not global"); per-handler behavior belongs in `tests/js/`.

---

## Subdirectory Guides

- `unit/AGENTS.md` - Unit test patterns, fixtures, mocking
- `integration/AGENTS.md` - Integration test patterns
- `js/AGENTS.md` - JS-vm command-pattern tests (the preferred middle layer)
- `e2e/AGENTS.md` - E2E test setup, GitHub requirements
- `e2e_web/AGENTS.md` - Playwright browser tests (use sparingly)
