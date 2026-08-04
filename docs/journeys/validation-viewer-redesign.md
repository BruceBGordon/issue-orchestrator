# Validation viewer redesign

> **Design history, not setup documentation.** This page records why the
> validation viewer and its plugin registry have the shape they do. It is not a
> guide to integrating a target repo's tests, and the plugin registry it
> describes is an internal extension point — not a supported client one.
>
> If you own a target repo and want your tests visible in Issue-Orchestrator,
> read [Client Test Integrations](../user/test-integrations.md) instead. The
> supported surface is JUnit XML plus artifacts.

Status: design + mockups committed. Implementation in progress across four
PRs (foundation → drawer integration → E2E view → accessibility).

This document captures the architectural decisions behind the redesign so a
future maintainer reads "why this shape" alongside the mockup HTML.

## Problem

The validation dialog today is a modal that opens over the issue-detail
drawer when the user clicks a cycle's validation badge.  It surfaces a
flat summary (started/ended, failing-test names as plain strings, three
groups of artifact buttons, and a `<pre>` stdout dump).  Three frictions
fall out:

1. **No per-test drill-down.**  Failed tests appear as bare names; passed
   tests aren't represented at all.  Anyone wanting to see *why a
   specific test failed* — stdout, traceback, source line, duration —
   has to leave the dialog and open a file on disk.
2. **Cycle ↔ validation are spatially disconnected.**  Modal-over-drawer
   loses the journey context.  Comparing "did Cycle 1 also fail this
   test?" requires close-modal, click-different-badge, reopen-modal.
3. **Artifact grouping conflates phases.**  "Session Recording", "Claude
   Log", "Validation Record", "Reviewer Log" all sit in one
   undifferentiated list under the validation dialog — which session's
   recording is *Session Recording*?  Ambiguous when a cycle has both a
   coder and reviewer agent.

The E2E view has its own version of the same problem: it carries
orchestrator-specific workflow UI (quarantine, triage-state soup, flake
analytics) that doesn't apply when a casual user simply wants to see
which pytest cases failed and why.

## Design

### One idiom

The entire UI uses a single interaction primitive: **rows that expand to
reveal child rows**.  Expander caret `▸` rotates to `▾`.  Tree spine on
the left of the children for visual nesting.  Rows without further
content show no caret — the presence of a caret is the affordance.

This idiom appears at every depth: a Run expands to Cycles, a Cycle
expands to a timeline of Events, an Event (e.g. "Validation passed")
expands to its content (test results, session artifacts, transcript), a
test expands to its source/traceback/stdout.

The same pattern carries to the E2E view: an E2E Run expands to its
pytest cases; a failed case expands to a failure card; if the case drove
the orchestrator on an issue, an extension panel within the case reveals
the issue's journey.

### Canonical JUnit viewer

The "rich" body of the validation dialog is reframed as the **canonical
JUnit viewer**, a JS component that any consumer can mount.  It accepts
typed `cases: TestCase[]` and renders:

- A header summary (suite, total, pass/fail/error/skip counts, duration).
- Failed/errored tests as **triage cards** at the top — outcome chip,
  type chip (`AssertionError`, `TimeoutError`, …), duration, optional
  flake badge, optional tracked-issue chip, sparkline of recent
  pass/fail history, **source snippet** at the failure point, **folded
  traceback** with framework frames behind a separate expander,
  stdout/stderr expanders, action buttons (Open in editor, Copy run
  command, Open JUnit record).
- A `▸ + N passed, N skipped` row beneath the failures that opens a
  **file-grouped browse view**.  Each file row shows pass count +
  duration; each test row within shows outcome icon + duration; each
  test expands to stdout (no traceback, no triage on passed tests).
- A `▸ Validation artifacts` row at the bottom (record / output /
  stderr).

The viewer ships once.  The per-issue drawer uses it inside cycle-event
expansions ("Validation passed/failed").  The E2E view uses it as the
entire body.  External consumers (tixmeup et al.) can use it standalone
with no additional UI.

### Plugin registry — Phase 0 minimal

Each test case may optionally carry `extras: [{namespace, payload}]`.
The viewer iterates extras; for each one it looks up a registered
renderer by namespace and embeds the rendered HTML below the test's
detail.  Unknown namespaces silently skipped.

```js
// One JS object, one register fn, one dispatch fn.
const VALIDATION_PLUGINS = {};
function registerValidationPlugin(namespace, renderer) { ... }
function renderPluginExtras(testCase) { ... }  // iterates case.extras
```

**The first and only Phase-0 plugin**: `io.agent-context`.  When a test
case was driven by the orchestrator on an issue (currently: E2E tests in
our own suite), the parser attaches
`{namespace: "io.agent-context", payload: {issue_number, run_id, ...}}`
to that case's `extras`.  The plugin renders the linked-issue subtree —
runs → cycles → events with full nested expansion.

**Generic consumers don't populate `extras`.** tixmeup's validation
parser sets `extras = []` on every case, so `renderPluginExtras` is a
no-op there.  The viewer is unaware that "linked issues" exist.

### What the plugin registry is NOT (Phase 0 scope hard limits)

- **No stdout marker protocol.**  Extras live as a structured field on
  the typed case payload, not as `<<<plugin:foo:begin>>>` markers in
  `<system-out>`.  The marker protocol is the upgrade we'd make when we
  have writers we don't own (third-party runners injecting extensions
  without modifying our parser).  Not needed when our parser is the
  single producer.
- **No plugin manifest.**  Plugin modules are statically imported and
  call `registerValidationPlugin()` at boot.  No YAML config, no dynamic
  loading, no per-tenant registries.
- **No version negotiation.**  Namespaces are flat strings.  When a
  plugin's payload shape changes incompatibly, we'll add `:v2` to the
  namespace and let the renderer dispatch — but not until we have an
  external consumer of the v1 shape.
- **No fallback debug UI for unknown namespaces.**  Just skip.  When we
  need it, it's one expander.

When we hit a second plugin (screenshot, video, profiler trace, AI
failure explainer, …), revisit these limits — but defer until.

### Per-issue drawer: single-idiom-everywhere

The drawer (today: cycles list + modal validation dialog) restructures
to:

```
Issue #N (drawer header)
├─ Run 2 (current, expanded)
│  ├─ Cycle 1 ✓ (collapsed by default for green cycles)
│  ├─ Cycle 2 ✕ (auto-expanded on failure)
│  │  ├─ ▸ Coding session started   → coder identity, recording, claude log
│  │  ├─ ▸ Agent finished coding    → completion record
│  │  ├─ ▾ Validation failed        → CANONICAL JUNIT VIEWER (auto-expanded)
│  │  └─ ▸ Reviewer requested changes → review feedback, transcript
│  │  ▸ Orchestrator log (cycle-wide)
│  └─ Cycle 3 ✓
└─ Run 1 (superseded, collapsed)
```

Per-event expansion replaces the cycle-level "Run artifacts (13 actions)"
lump.  Each session's artifacts live under its own session event.
Cycle-wide artifacts (orchestrator log, full log) get a slim expander at
the bottom of the cycle's children — the only thing that doesn't fit
under any single event.

When validation runs more than once in a cycle (e.g. orchestrator reruns
after a SHA change), each "Validation passed/failed" event is its own
independently-expandable row with its own canonical-viewer content.  No
special case in rendering — just two events with the same shape.

### E2E view

Drops orchestrator-workflow chrome (quarantine, triage-state categories
like "untriaged" / "has_issue" / "flaky" / "fixed" / "quarantined" — too
many states for generic users to model; replaces with JUnit-canonical
"passed/failed/skipped/errored").

Mounts the canonical viewer as its body.  Registers the
`io.agent-context` plugin.  Failed E2E tests that drove the orchestrator
embed the journey via the plugin; everything else is generic JUnit.

For our own debugging of agent loops gone wrong, the embedded journey
under a failed E2E test is exactly the right diagnostic surface — and we
get it for free because the same plugin we built for the per-issue
drawer renders the same content.

## Schema

```typescript
// New optional field on the typed test case
interface TestCase {
  // ...existing fields (nodeid, name, outcome, duration, stdout, stderr, traceback)...
  extras?: ValidationExtra[];
}

interface ValidationExtra {
  namespace: string;        // e.g. "io.agent-context"
  payload: unknown;         // plugin-specific
}
```

OpenAPI: `TestCaseResultPayload.extras` is added as `Array<{namespace,
payload}>` with `payload` accepting `{type: object, additionalProperties:
true}`.  Generic JUnit consumers either omit the field or send `[]`.

## Accessibility

The single-idiom-expander tree is a textbook screen-reader target IF the
ARIA is correct.  The implementation contract:

- Each expandable `row` is `role="treeitem"`.
- The `children` container is `role="group"`.
- The top-level drawer body is `role="tree"`.
- Each treeitem has `aria-expanded="true|false"`.
- Each treeitem has `aria-level="N"` (1-indexed depth).
- Each treeitem has `aria-setsize` and `aria-posinset` when siblings are
  known.
- Keyboard nav: arrow up/down moves focus among siblings; arrow right
  expands and focuses first child; arrow left collapses and returns
  focus to parent; Enter/Space toggle the current item.
- Focus is visually indicated (outline + ring).
- Default tab order: header close button → body's first treeitem →
  footer.  Within the tree, arrow-key nav, not tab.

Playwright a11y assertion: the validation viewer renders a non-empty
tree of `treeitem`s with consistent depth labels.

## Phasing

| PR | Scope |
|---|---|
| A. Foundation | Canonical viewer JS component, plugin registry, linked-issue plugin module, OpenAPI schema for `case.extras`, backend parser populates extras, JS-vm + Python unit tests. The new viewer replaces the old `renderValidationDialog` body so the modal entry point exercises the new code without UI restructuring yet. |
| B. Drawer integration | Per-issue drawer's cycle rows restructure to the single-idiom tree.  Cycle events become individually expandable.  Validation event hosts the canonical viewer inline.  Modal entry point deleted. |
| C. E2E view | Strip orchestrator-workflow chrome.  Mount canonical viewer.  Failed-test linked-issue subtree via the plugin. |
| D. Accessibility | ARIA + keyboard nav + focus styling + Playwright a11y assertions. |

## Mockup artifacts

- `validation-dialog-mockup.html` — first iteration, triage-first within a
  modal dialog (failure cards, browse-by-file, smart triage).  Useful
  reference for the canonical viewer's content shape.
- `validation-inline-mockup.html` — second iteration, the per-issue drawer
  with single-idiom expander tree.  Shows the per-event expansion,
  validation as a cycle-event, phase-grouped artifacts.  This is the
  Phase-B target.
- `validation-e2e-mockup.html` — third iteration, the E2E view with the
  canonical viewer + linked-issue plugin.  This is the Phase-C target.

All three open standalone in any browser; their interactivity is
sufficient to exercise the design decisions.

## Decisions worth re-reading before implementing

- **Single idiom**: every expander is `▸`/`▾`, regardless of depth or
  content type.  No mixing of `<details>` with custom expand/collapse
  buttons.  No tabs.  No modals (the validation modal goes away in Phase
  B).
- **Event-driven, not subsection-driven**: cycle events ARE the
  artifacts; expanding a Validation event shows the JUnit results
  directly.  Don't introduce a separate "Test Results" subsection that
  duplicates the event.
- **Phase-grouped artifacts**: per-session artifacts live under their
  session's event, not in a cycle-level lump.
- **Plugin gating by data presence**: a plugin's content renders if and
  only if its case has an entry in `extras` for that namespace.  No
  config to "turn the plugin on" — the data presence is the switch.
- **Accessibility is structural**: ARIA tree pattern fits the design
  naturally because the design IS a tree.  Don't bolt on ARIA at the
  end; build it from row #1.
