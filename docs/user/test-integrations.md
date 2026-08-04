# Client Test Integrations

This is the canonical guide for exposing **your repository's tests** inside
Issue-Orchestrator. Read this if you own a target repo and you want the
dashboard to show your test results, failures, reports, traces, and logs.

Issue-Orchestrator does not know your test framework. It knows three evidence
channels, and every integration is a matter of pointing those channels at files
your existing test command already produces:

| Channel | What it is | How you enable it |
|---------|-----------|-------------------|
| **Raw output** | The full stdout/stderr of your test command | Automatic. Nothing to configure. |
| **JUnit XML** | Structured per-case results (name, outcome, duration, failure text, captured output) | Emit a JUnit report and list it in `junit_xml_paths` |
| **Artifacts** | Native reports and debugging output: HTML reports, traces, screenshots, zips, logs | List them in `e2e.artifact_paths` (E2E runner only) |

JUnit XML plus artifacts is the **supported client integration surface**. Custom
JS rendering plugins are not — see
[Custom test rendering plugins are internal](#custom-test-rendering-plugins-are-internal).

- Every configuration field named here is documented field-by-field in the
  [Configuration Reference](configuration_reference.md).
- Async-runner behavior, lifecycle, API endpoints, retry/resume, quarantine, and
  debugging live in the [E2E Test Runner](e2e.md) guide.

---

## Step 1: Pick your surface

There are two independent places your tests can run. Most repos want both, and
they are configured separately.

| | **Validation gates** (`validation.*`) | **Async E2E runner** (`e2e.*`) |
|---|---|---|
| Question it answers | "Is this agent's commit good enough to advance?" | "Is the main branch still healthy?" |
| When it runs | On `coding-done`, during review exchange loops, and before push/publish | On a timer after the tracked branch head moves, or on demand |
| What it gates | Whether work advances, reworks, blocks, or becomes a PR | Nothing. It reports. |
| Where it runs | The per-issue agent worktree | A dedicated E2E worktree |
| Speed budget | Quick gate: seconds to a couple of minutes. Publish gate: your real PR suite. | Whatever your suite takes |
| Raw output | Yes | Yes |
| JUnit XML | Yes (`validation.junit_xml_paths`) | Yes (`e2e.junit_xml_paths`) |
| Artifact capture | **No** | Yes (`e2e.artifact_paths`) |
| Can open issues on failure | No (it routes the issue to rework/blocked) | Yes (`e2e.auto_create_issues`) |
| Quarantine support | No | Yes (`e2e.quarantine_file`) |

**Rule of thumb:** unit/lint/typecheck/fast-integration tests belong in the
validation gates. Long, flaky-prone, browser-driven, or environment-heavy suites
belong in the E2E runner.

---

## Step 2: Wire the validation gates

Validation gates are what actually hold the line on agent work. Configure the
commands first; structured results are an add-on.

```yaml
validation:
  quick:
    cmd: "./scripts/validate-fast.sh"
    timeout_seconds: 300
  publish:
    cmd: "./scripts/validate-pr.sh"
    timeout_seconds: 1800
    dirty_check: "tracked"
  junit_xml_paths:
    - "test-results.xml"
```

- `validation.quick.cmd` runs inside the agent/reviewer loop. Keep it fast — every
  round of coder/reviewer back-and-forth pays for it.
- `validation.publish.cmd` is the authoritative gate before push and PR
  publication. It should match your repo's real local PR/pre-push gate.
- `validation.junit_xml_paths` accepts one path or glob per line, resolved
  relative to the **issue worktree**.

### What you get in the dashboard

With no JUnit configured, a validation event still exposes:

- the validation record (`suite`, `command`, `exit_code`, `started_at`, `ended_at`)
- the captured stdout and stderr logs
- a heuristic list of failed test names scraped from pytest-style `FAILED ...`
  lines in stdout

With `validation.junit_xml_paths` set, the same event additionally renders the
canonical validation viewer: per-case rows with outcome, duration, failure
details, and captured `system-out` / `system-err`.

### Rules that bite

- **Validation captures no artifacts.** There is no `validation.artifact_paths`.
  If you need HTML reports, traces, or screenshots surfaced, that suite belongs in
  the E2E runner.
- **Missing JUnit is not a validation failure.** A validation command that exits
  before writing its report (a typecheck step failing ahead of the test step)
  simply produces no structured cases. Contrast with the E2E runner, which fails
  the run loudly.
- **Reports must be fresh.** A JUnit file whose mtime predates the validation run
  (minus a 2-second filesystem-granularity cushion) is ignored, so a stale report
  from a previous run cannot be misreported as this run's result.
- **Discovered paths are recorded, not re-discovered.** The paths found at
  validation time are written into the run manifest and are authoritative
  afterwards. If your test task wipes and rewrites its output directory (Gradle's
  `test` task does), a later run can leave the manifest pointing at deleted files;
  the UI then shows "no case data" instead of erroring.

---

## Step 3: Wire the async E2E runner

The E2E runner has two adapters. Pick by `e2e.runner_kind`.

### `runner_kind: pytest`

Use when the suite is already pytest-based and you want live per-test progress
events and retry/resume support.

```yaml
e2e:
  enabled: true
  role: "auto"
  runner_kind: "pytest"
  auto_run_interval_minutes: 30
  pytest_args:
    - "tests/e2e"
    - "-v"
    - "--junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml"
  junit_xml_paths:
    - ".issue-orchestrator/e2e-results/pytest-junit.xml"
  allow_retry_once: true
  quarantine_file: "tests/e2e/quarantine.txt"
  auto_quarantine: true
  auto_create_issues: true
  issue_agent_label: "agent:backend"
```

Mirror the `--junitxml=` path into `junit_xml_paths` — pytest writes the file,
`junit_xml_paths` tells the orchestrator to ingest it.

### `runner_kind: command`

Use for everything else: Playwright, Vitest, Jest, Cypress, Robot Framework,
Gradle, or a project-local wrapper script.

```yaml
e2e:
  enabled: true
  role: "auto"
  runner_kind: "command"
  auto_run_interval_minutes: 30
  command:
    - "./scripts/run-e2e-suite.sh"
  junit_xml_paths:
    - "test-results/junit.xml"
  artifact_paths:
    - "playwright-report/index.html"
    - "test-results/**/*.zip"
    - "test-results/**/*.png"
  auto_create_issues: true
  issue_agent_label: "agent:backend"
```

`allow_retry_once` and `stop_on_first_failure` are pytest-only. The command
adapter reports the original command result and ingests reports after the process
exits.

See [E2E Test Runner](e2e.md) for auto-trigger conditions, retry/resume
semantics, quarantine behavior, run lifecycle, and the control API.

---

## Framework recipes

Each recipe assumes `runner_kind: command` unless stated otherwise, and shows the
two things you must produce: a JUnit report and any native artifacts worth
keeping.

### pytest

`runner_kind: pytest` is the better fit here — you also get live per-test events,
retry-once, and resume-by-deselection.

```yaml
e2e:
  runner_kind: "pytest"
  pytest_args:
    - "tests/e2e"
    - "-v"
    - "--junitxml=.issue-orchestrator/e2e-results/pytest-junit.xml"
  junit_xml_paths:
    - ".issue-orchestrator/e2e-results/pytest-junit.xml"
```

pytest writes dotted module paths into the JUnit `classname` attribute
(`tests.e2e.test_basic`) while live runtime observation records filesystem-style
nodeids (`tests/e2e/test_basic.py::test_thing`). The orchestrator normalizes the
former onto the latter so one test produces one row rather than two.

For a validation gate, the same flag works — add `--junitxml=test-results.xml` to
your quick or publish command and list `test-results.xml` in
`validation.junit_xml_paths`.

### Playwright

```jsonc
// playwright.config.ts
reporter: [
  ['junit', { outputFile: 'test-results/junit.xml' }],
  ['html',  { outputFolder: 'playwright-report', open: 'never' }],
]
```

```yaml
e2e:
  runner_kind: "command"
  command: ["npx", "playwright", "test"]
  junit_xml_paths:
    - "test-results/junit.xml"
  artifact_paths:
    - "playwright-report/index.html"
    - "test-results/**/trace.zip"
    - "test-results/**/*.png"
    - "test-results/**/*.webm"
```

Trace zips are recognized as traces when `trace` appears in the filename;
`.png` / `.jpg` / `.jpeg` / `.webp` are recognized as images.

### Vitest / Jest

```yaml
e2e:
  runner_kind: "command"
  command: ["npm", "run", "test:e2e"]
  junit_xml_paths:
    - "reports/junit.xml"
  artifact_paths:
    - "reports/index.html"
    - "reports/results.json"
```

Vitest: `--reporter=junit --outputFile=reports/junit.xml`.
Jest: `jest-junit` with `JEST_JUNIT_OUTPUT_FILE=reports/junit.xml`.

Both accept multiple reporters, so keep your human-facing HTML or JSON reporter
and add JUnit alongside it — the HTML/JSON output becomes an artifact.

### Cypress

```yaml
e2e:
  runner_kind: "command"
  command: ["npx", "cypress", "run"]
  junit_xml_paths:
    - "cypress/results/*.xml"
  artifact_paths:
    - "cypress/screenshots/**/*.png"
    - "cypress/videos/**/*.mp4"
```

Configure `mocha-junit-reporter` with a per-spec filename
(`mochaFile: cypress/results/results-[hash].xml`) so parallel specs do not
overwrite each other. The glob ingests every file and merges the cases into one
run. Use `cypress-multi-reporters` if you want the spec reporter's console
output as well.

### Generic wrapper script

When a suite needs setup, environment wiring, or several tools, put it behind one
script and keep the orchestrator's view stable:

```bash
#!/usr/bin/env bash
# scripts/run-e2e-suite.sh
set -uo pipefail

mkdir -p test-results

./gradlew :app:e2eTest --continue
status=$?

# Copy every module's report to one predictable place.
cp build/test-results/e2eTest/*.xml test-results/ 2>/dev/null || true
cp -r build/reports/tests/e2eTest test-results/html-report 2>/dev/null || true

exit "$status"
```

```yaml
e2e:
  runner_kind: "command"
  command: ["./scripts/run-e2e-suite.sh"]
  junit_xml_paths:
    - "test-results/*.xml"
  artifact_paths:
    - "test-results/html-report/index.html"
    - "test-results/**/*.log"
```

Two rules make wrapper scripts behave:

1. **Do not swallow the exit code.** The run's status is the command's exit code.
   Capture it before any cleanup and re-exit with it.
2. **Always write the reports, even on failure.** A script that short-circuits on
   failure before copying reports produces a run with a loud "did not resolve to
   any files" error instead of the failure detail you wanted.

---

## Path and worktree rules

### Where commands run

| Surface | Working directory |
|---------|-------------------|
| Validation gates | The per-issue agent worktree |
| E2E runner | A dedicated sibling worktree: `<repo>/../<repo-name>-e2e-worktree` |

The E2E worktree is force-checked-out to the orchestrator's current `HEAD` and
`git clean -fdx`'d before **every** run, preserving only:

- `.venv`
- `.issue-orchestrator/state/timeline.sqlite*`
- `.issue-orchestrator/sessions`
- `.issue-orchestrator/e2e-results`

Consequences worth planning for:

- **Untracked dependency trees are wiped between runs.** `node_modules`,
  `.gradle`, build caches, and downloaded browsers do not survive. Your command
  must be able to bootstrap what it needs, or you should keep that bootstrap
  inside your wrapper script.
- **Writes to tracked files do not persist.** The next run's forced checkout
  restores them. This includes `e2e.auto_quarantine` additions to a tracked
  quarantine file — commit entries you want to keep.
- **Collected artifacts are copied out.** After ingestion, matched files are
  snapshotted into `.issue-orchestrator/e2e-results/run_<id>/` inside the E2E
  worktree, which is excluded from the clean. That is why the dashboard can still
  open an artifact from a run several cycles old.
- Raw run logs live in the **base repo**, not the worktree:
  `.issue-orchestrator/logs/e2e/run_<timestamp>.log`.

### Glob rules (both surfaces)

- Paths are **relative to the worktree root** for that surface. Absolute paths are
  accepted but tie the config to one machine.
- `**` recursive globs are supported (`test-results/**/*.zip`).
- Directories that match a glob are skipped; only files are ingested.
- A path that resolves outside the worktree root is a hard error.
- Duplicate matches across patterns are deduplicated.
- One path or glob per line in the YAML list; blank lines are ignored, so a
  trailing newline does not read as "configured".
- **E2E only:** a configured `junit_xml_paths` or `artifact_paths` entry that
  matches nothing fails the run loudly. This is deliberate — a silently missing
  report looks identical to a passing suite.

---

## What the JUnit parser accepts

The orchestrator reads any `.//testcase` elements, so single-suite and
multi-suite reports both work.

| Element / attribute | Becomes |
|---------------------|---------|
| `testcase@name` (required, non-empty) | Display name |
| `testcase@classname` | Suite name; combined as `classname::name` for the case ID |
| `testcase@time` | Duration in seconds |
| `<failure>` child | Outcome `failed`, with `message` + body as failure details |
| `<error>` child | Outcome `error` |
| `<skipped>` child | Outcome `skipped` |
| none of the above | Outcome `passed` |
| `<system-out>` / `<system-err>` | Captured output, truncated at 100,000 characters per channel |

A report is rejected — and, on the E2E runner, fails the run — when the file is
missing, is not well-formed XML, contains zero `<testcase>` entries, or has a
`testcase` with an empty `name`.

---

## Custom test rendering plugins are internal

The canonical validation viewer can render extra, non-JUnit content attached to a
test case through a plugin slot. **This is an internal extension point today, not
a supported client one.** Do not build a target-repo integration on it.

How it actually works:

- Each case in the typed payload carries `extras: [{namespace, payload}]`
  (`ValidationExtraPayload` / `JUnitCasePayload` in `docs/api/ui-openapi.json`).
- A renderer claims a namespace by calling
  `registerValidationPlugin(namespace, renderer)` at dashboard boot.
- The viewer iterates a case's `extras` in payload order and embeds each rendered
  fragment beneath the case detail. Unknown namespaces are silently skipped; a
  renderer that throws produces a one-line inline error instead of taking down
  the viewer.
- `io.agent-context` is the only built-in renderer. It activates when a case was
  driven by orchestrated work on an issue and renders the inline
  `▸ Attempts on issue #N` drill-in. Presence of the data is the switch — there
  is no config to turn a plugin on.

Why it is not a client extension point yet:

- Plugin modules are **statically imported** by the dashboard's own asset
  manifest and register at load time. There is no manifest, dynamic loading, or
  per-repo registry a target repo could hook into without patching
  Issue-Orchestrator's source.
- There is no marker protocol for third-party runners to inject extras. The
  orchestrator's own parser is the only producer, and generic JUnit ingestion
  always yields `extras: []`.
- Namespaces are flat strings with no version negotiation, so the payload shape
  is free to change without notice.

**What to do instead:** emit JUnit XML for structured per-case results and use
`e2e.artifact_paths` for anything richer — an HTML report, a trace, a screenshot,
a JSON summary. Those are stable, supported, and framework-neutral.

If plugin rendering becomes a supported extension point, it will be announced
here. The design history and the deliberate Phase-0 limits are recorded in
[docs/journeys/validation-viewer-redesign.md](../journeys/validation-viewer-redesign.md),
which is a design document, not setup documentation.

---

## Verify your integration

1. Run your test command by hand from a clean checkout and confirm the report and
   artifact files exist at the configured paths afterwards.
2. Break one test on purpose and confirm the report still gets written.
3. Trigger a run (`POST /control/e2e/start`, or the dashboard's E2E controls) and
   open the run's **Results** tab.
4. Confirm you see structured cases, not only `Raw Output`. Only-raw-output means
   your JUnit path did not resolve or the file was not fresh.
5. Confirm artifact buttons appear for each `artifact_paths` entry.

For per-run SQL checks against `.issue-orchestrator/e2e.db`, see the debugging
section of [E2E Test Runner](e2e.md#debugging).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Results tab shows only `Raw Output` | No JUnit emitted or path did not resolve | Verify the file exists after a manual run; mirror the exact path into `junit_xml_paths` |
| Run fails: "Configured JUnit XML paths did not resolve to any files" | The glob matched nothing | Fix the emitted path or the glob. Check the path is worktree-relative |
| Run fails: "…did not resolve to any fresh files" | The only matches predate this run | Your command is not rewriting the report. Delete stale reports at start-of-run |
| Run fails: "Configured artifact paths did not resolve to any files" | An `artifact_paths` glob matched nothing | Remove the entry, or make the command always produce it |
| Run fails: "JUnit XML did not contain any `<testcase>` entries" | The suite collected zero tests, or the reporter wrote a stub | Fix test selection; confirm the reporter ran |
| Run fails: "E2E report path resolves outside repo root" | The path escapes the worktree (`../`, or a symlink out) | Write reports inside the worktree |
| Validation event has no structured cases | Validation exited before the test step, or the report was stale | Check the validation stdout log for where the command stopped |
| Dependencies missing on every E2E run | The worktree is `git clean -fdx`'d between runs | Install inside your command or wrapper script |
| Auto-quarantine entries keep disappearing | The forced checkout restores tracked files | Commit quarantine entries to the repository |

---

## Related documentation

- [E2E Test Runner](e2e.md) — async runner lifecycle, retry/resume, quarantine, API, debugging
- [Configuration Reference](configuration_reference.md) — every `validation.*` and `e2e.*` field
- [Configuration](configuration.md) — getting started with config files
- [Validation System](../architecture/validation.md) — publish gate design
- [Feature List](features.md) — capabilities grouped by user problem
