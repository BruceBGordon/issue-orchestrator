# Playwright Browser Tests (`tests/e2e_web/`)

These tests launch a real chromium and a real fixture web server. **Cost: ~7 s per test, plus flake risk.** Treat them as a scarce resource.

## Before adding a test here

**Ask first: can this be a JS-vm test instead?** See `tests/AGENTS.md` for the test pyramid and `tests/js/AGENTS.md` for the JS-vm pattern.

If the behavior you want to verify is any of:
- Which handler runs when a button is clicked → JS-vm
- HTML structure (class names, attributes, ordering) → JS-vm
- API URL or payload on a click → JS-vm with stubbed `fetch`
- Render shape per outcome / state combination → JS-vm
- Toggle expand/collapse, caret update, aria-expanded mirror → JS-vm

…write a JS-vm test, not a Playwright test.

## When Playwright IS the right tool

- **Cross-page navigation** — click in modal A, verify drawer B opens with the right context
- **Real DOM events** — focus management, scroll, `<details>` native toggle, drag/drop, file pickers
- **End-to-end smoke** — one test per major surface that proves the entire stack (Python view-model → API → JS render → real DOM) actually wires up
- **Browser-only timing** — e.g. SSE reconnect after a network blip, layout-dependent assertions

## What to assert in a Playwright test

When you do write one, push as much as possible into the *setup* (which fixtures, which run id, which seeded data) and keep the assertions *behavioral* and *minimal*:

```python
# GOOD — one focused observable per test
def test_run_row_expands_when_clicked(page, fixture_web_server):
    page.goto(fixture_web_server["url"])
    run_id = fixture_web_server["run_id"]
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)
    expect(row.locator(".cvv-root")).to_be_visible()

# BAD — verifies HTML structure that JS-vm could verify in 5 ms
def test_run_row_has_correct_pill_classes(page, fixture_web_server):
    ...
    pills = row.locator(".test-result-pill")
    expect(pills.first).to_have_class(...)
    expect(pills.nth(1)).to_have_text("Tracked")
```

Move pill-class / text-content assertions to `tests/js/`. Leave Playwright to verify the row *opens and mounts the canonical viewer at all*.

(Issue #6334 retired ``#e2eDiagnosisModal``; the canonical viewer mounts inline in each run's ``<details class="e2e-run-row">`` row.)

## When migrating: shrink, don't delete

If a Playwright test is doing N things and only one of them is truly browser-dependent:

1. Add JS-vm tests covering the non-browser things (one per concern)
2. Trim the Playwright test down to the one browser-dependent assertion
3. Don't delete the file — keep it as the smoke that proves end-to-end wiring

Example: `test_run_modal_filter_chips_and_per_row_expand_show_correct_content` originally verified filter-chip behavior, expand/collapse, error-pre content, lifecycle button JSON, *and* expandability. Most of those are now JS-vm tests in `tests/js/e2e_run_view_actions.test.js`. The Playwright test stays as the smoke that proves the modal renders and basic interactions work.

## Never seed `window.dashboardData` from a test

`dashboard/core.js` replaces that whole object on **every** `refreshViewModel` — at boot, when the SSE stream opens, and on each refresh event after that. Anything a test writes into it survives only until the next refresh lands, so any UI rendered on the far side of that refresh silently reverts to the server's answer.

That is #7140: the per-row agent `<select>` lost its injected `agent:vscode` option between two row expansions and `select_option` burned a 30 s timeout with *"did not find some options"*. It reproduced whenever `/api/view-model` took as little as 200 ms longer than usual — which is why it only showed up under host load, and why re-running "fixed" it every time.

To give a test different dashboard state, **configure the fixture orchestrator** (`FlowWebMockOrchestrator` in `conftest.py`) so the first render and every refresh agree. `test_browser_tests_do_not_seed_the_server_owned_dashboard_data_global` in `tests/unit/test_dashboard_ui_guardrails.py` enforces this.

The same reasoning applies to any page state the app re-derives from the server: waiting for `window.dashboardBundleLoaded` proves the bundle *parsed*, not that boot finished — and no signal can promise "no further refresh", because a live dashboard refreshes forever.

## Fixtures

- `web_server` — generic dashboard server (no auth)
- `authed_web_server` — adds admin token configuration
- `fixture_web_server` (in `test_e2e_timeline_fixture_browser.py`) — seeds an E2E run with linked-issue lifecycle

When adding new fixtures, prefer the smallest seed that exercises the assertion. Heavy fixtures slow every test that uses them.

## Evidence on failure

Failing browser tests write a trace, a screenshot, and a console/network log to
`.issue-orchestrator/diagnostics/e2e-web/<nodeid>/`, and the failure output prints the path. Green runs write nothing. Open a trace with:

```bash
playwright show-trace .issue-orchestrator/diagnostics/e2e-web/<nodeid>/trace.zip
```

This is on by default for the whole lane: `pytest_configure` in `conftest.py` moves pytest-playwright's `--tracing`/`--screenshot` to their failure-only modes, but **only when you did not supply them**. Supplied-ness is read from the token stream pytest parsed (ini `addopts`, `PYTEST_ADDOPTS`, argv), never from the resolved value — both options *default* to `off`, so `--tracing=off` and "no `--tracing`" are indistinguishable by value.

`--tracing=off --screenshot=off` therefore means exactly that: nothing is written, not even the console sidecar. The two options resolve independently.

## Running

```bash
pytest tests/e2e_web/ -q                                # full suite
pytest tests/e2e_web/test_x.py::test_y -q               # one test
pytest tests/e2e_web/ -q --headed                       # see the browser
```
