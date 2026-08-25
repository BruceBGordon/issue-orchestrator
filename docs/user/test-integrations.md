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

### Bound validation concurrency across repositories

Test frameworks can limit one process, but they do not coordinate separate
repositories, worktrees, or issue sessions. The executor is a processless,
per-user host facility that provides that missing boundary. It has three levels:

- The **machine** detects its CPU capacity internally and exposes one
  aggressiveness dial.
- A **validation run** owns a unique fairness group.
- A repository describes each independently runnable command with an opaque,
  human-readable work key and the concurrency range it can accept.

The public contract is deliberately small: construct an
`ExecutorRunSpecification`, then invoke `Executor.run(specification, command)`.
CLI clients express the same contract directly:

```bash
export ISSUE_ORCHESTRATOR_EXECUTOR_GROUP="porchpin-validate-$$"

# xdist-aware work: the executor chooses from 1 through 8 workers.
issue-orchestrator executor-run \
  --work-key porchpin:unit \
  --min-concurrency 1 \
  --max-concurrency 8 \
  -- pytest -n auto tests/unit

# A command without xdist still learns its CPU demand under concurrency 1.
issue-orchestrator executor-run \
  --work-key porchpin:checks \
  --min-concurrency 1 \
  --max-concurrency 1 \
  -- make checks
```

`--work-key` is required and repository-local. Use a stable name such as
`io:unit`, not a generated hash: it is the identity under which resource history
is learned and the name shown in diagnostics. Printable Unicode and embedded
spaces are valid, so existing human-facing names do not require a lossy alias.
The fairness group is also
required, either through `--group` or
`ISSUE_ORCHESTRATOR_EXECUTOR_GROUP`. Give all commands from one top-level
validation the same unique group. No global meanings such as "unit",
"integration", or "browser" are imposed; those words are only local names a
repository may choose.

The executor learns successful commands' occupied CPU cores per granted
concurrency unit. It combines that repository-and-work-specific estimate with
currently active leases. A bounded internal coalescing window lets concurrently
launched work become visible as a burst. The scheduler reserves each compatible
queued command's learned minimum demand before expanding earlier commands
toward their maxima. Thus a heavy IO xdist worker and a light Porchpin command
are not assumed to cost the same, and one early wide command cannot consume the
whole machine when every visible minimum fits. Suite duration is not the
normalization unit; observed CPU occupancy is. Failed commands are logged but
do not teach the scheduler that an early failure is a cheap successful run.

Before each admission decision the executor also samples current host CPU busy
time from native counters: Mach per-processor ticks on macOS and `/proc/stat`
on Linux. At the internal 95% saturation threshold, it holds new work and
retries after the host has had time to drain. This instantaneous feedback is
how unmanaged work such as an editor, another agent process, or a repository
that has not adopted the executor can attenuate new admissions. It does not
kill or resize commands already running.

For xdist, `-n auto` is essential. The executor exports its grant as
`PYTEST_XDIST_AUTO_NUM_WORKERS`; an explicit `pytest -n 8` bypasses that grant.
Framework-neutral clients can read
`ISSUE_ORCHESTRATOR_EXECUTOR_CONCURRENCY`. Commands without an internal worker
pool declare a range of `1` through `1`; their CPU demand is still observed and
learned. Repositories never estimate or declare admission capacity.

The executor derives its internal CPU-slot capacity from the machine. Every
repository and worktree for the same OS user shares the pool automatically.
The percentage below is the only host-pressure control exposed to users.

One user-facing percentage scales the learned recommendation:

```bash
# Inspect the effective value and its source.
issue-orchestrator executor-policy

# 100% is the learned recommendation; 75% is more conservative;
# 125% permits more overlap.
issue-orchestrator executor-policy --aggressiveness 125
```

The accepted range is 25–400%. The persisted value is machine-wide. The
environment variable
`ISSUE_ORCHESTRATOR_EXECUTOR_AGGRESSIVENESS_PERCENT` is an explicit override;
the policy command reports when it overrides the saved value.

Repeated `--exclusive NAME` declarations serialize correctness-sensitive host
resources such as one provider CLI identity, browser fixture, emulator, or
future repository mutation boundary. These locks are independent of work-key
names. CPU and exclusive leases transfer to a transient guardian and survive an
executor-wrapper crash until the complete opaque command group is contained.
The command itself receives no lease descriptors.

There are no new YAML settings. Capacity is machine policy in the environment;
work specifications belong to repository commands. The orchestrator does not
need to be running: `executor-run` is a local command that coordinates through
locks and strict state records.

Issue-orchestrator's own code, validation-retry, rework, review, and
retrospective-review agent sessions participate automatically; repository
owners do not configure them. Each complete terminal session is one safe phase.
When it ends, its lease is released, and any later lifecycle phase re-enters
the fair host queue. Queue time is excluded from the agent's active timeout,
while a fixed submission-to-exit deadline prevents unbounded waiting. The
planner persists that exact outer watchdog before terminal creation, and a
restart requires the persisted value instead of recomputing from current
configuration. The executor never suspends a live agent at an arbitrary
instruction.

An agent process launched outside issue-orchestrator is unmanaged work: it has
no lease, but its CPU use is visible to native host-pressure sampling and can
hold back later managed admissions. To receive fairness and bounded admission,
launch a generic command through `executor-run` with its own human work key and
fairness group.

The executor writes a bounded typed event store at
`<pool>/executor-events-v4.jsonl`; on macOS the default pool is under
`~/Library/Application Support/issue-orchestrator/executor-pools/host-v2`.
Enqueue, changing wait reasons, grants, policy source, command observations and
lifecycle failures, learned-demand changes, successful learning-sample counts,
native CPU busy samples and their intervals, admission/command watchdog
expirations, and host load averages are recorded with human work and repository
names. Inspect recent activity without reading that persistence directly:

```bash
issue-orchestrator executor-events --limit 50
```

`issue-orchestrator executor-status` shows the detected host CPU slots, the
effective percentage and its source, global successful/excluded sample totals,
the exact global learning fingerprint, and a bounded page of human-readable
repository/work profiles. Use `--repository`, `--offset`, and `--limit` to
navigate retained profiles. Failed commands are recorded in `executor-events`
by repository and work key but never enter learning history; the status
failure total exists for valid migrated history that contains explicit failed
samples. This is the compact view; `executor-events` is the detailed
after-the-fact decision trail.

The CLI queries the typed `ExecutorMonitor` port used as the future UI seam.
Accurate live-state projection and a polished UI remain deferred to the
executor visibility TODO (#7105).

Host *load average* is diagnostic rather than an admission input because it
lags both the start and end of work. Short-interval native CPU busy percentage
is the immediate admission input: saturation pauses new commands without
changing an already selected grant. Managed work also adapts across runs from
learned CPU occupancy and current live leases. If the automatic attenuation
still leaves too little interactive headroom, turn down the one percentage
explicitly.

Repositories can keep the same lane commands usable without installing the
issue-orchestrator package by checking in the standalone
[`scripts/executor-run-direct`](../../scripts/executor-run-direct) adapter and
selecting it as their executor command:

```make
EXECUTOR_RUN ?= issue-orchestrator executor-run

# Standalone invocation, with no issue-orchestrator package or service:
# make test-unit EXECUTOR_RUN=./scripts/executor-run-direct
```

The direct adapter executes the command itself and exports the largest accepted
concurrency as `PYTEST_XDIST_AUTO_NUM_WORKERS`, so
`pytest -n auto` remains bounded within that invocation. It requires the same
work key, group, and demand declaration, but applies no cross-process fairness
or learning. It fails if an exclusive resource is requested because it cannot
honor that safety contract. Selecting direct mode must be explicit; it is never
an automatic degradation path. Neither executor requires a running
orchestrator service; the pooled form only requires the `issue-orchestrator`
CLI to be installed.

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

One rule governs every recipe below. `artifact_paths` is evaluated as a single
group, so **if you configure it at all, at least one pattern in it must be
guaranteed to match on a green run.** Most framework artifacts are conditional —
screenshots and traces usually exist only for failures, videos only when
recording is enabled — and a group where every pattern is conditional turns a
passing suite into a failed run. Anchor the group with something written
unconditionally (an HTML report is the usual choice), or leave `artifact_paths`
unset until you have such an output. The same applies to `junit_xml_paths`, but
a report you always write is the point of that field anyway.

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
    # Requires video: true — see the warning below before keeping this field.
    - "cypress/videos/**/*.mp4"
    - "cypress/screenshots/**/*.png"
```

Cypress is the recipe most likely to trip the artifact-group rule. It writes
screenshots **only for failing tests**, and writes videos **only when `video:
true` is set** in `cypress.config.js`. With video off, a green run matches
neither pattern, the whole artifact group is empty, and the run fails even though
the JUnit group resolved perfectly. Either enable video so the group always has a
member, or drop `artifact_paths` and rely on the JUnit report alone —
screenshots on a failing run are not worth a false failure on every passing one.

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

Three rules make wrapper scripts behave:

1. **Do not swallow the exit code.** Capture it before any cleanup and re-exit
   with it. The exit code is an *input* to run status, not the status itself —
   the orchestrator combines it with the parsed JUnit outcomes and the quarantine
   list. Two overrides are worth knowing:
   - A command that exits `0` while its JUnit report contains failing cases still
     produces a **failed** run, annotated with "Command exited 0 but JUnit XML
     reported failing tests". You cannot make a run pass by inventing a zero.
   - When the only failing cases are quarantined, a non-zero exit is cleared to
     `0` and the run completes as **warning**. So a non-zero command does not
     always mean a failed run either.

   A discovery or parser error finishes the run as **error** regardless of exit
   code. Reporting the true exit code is what keeps all of these decisions
   correct.
2. **Always write the reports, even on failure.** A script that short-circuits on
   failure before copying reports produces a run with a loud "did not resolve to
   any files" error instead of the failure detail you wanted.
3. **Make the artifact group survivable.** The `cp ... || true` lines above
   deliberately tolerate a missing file, which means a run that fails early can
   leave `artifact_paths` matching nothing and fail on ingestion rather than on
   the real problem. Either guarantee one of those artifacts (write a stub HTML
   report when the real one is absent) or leave `artifact_paths` unset.

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

- **Untracked dependency trees inside the worktree are wiped between runs.**
  Repo-local `node_modules`, `.gradle`, build caches, and browsers installed into
  the tree do not survive. The clean runs only inside the E2E worktree, so
  user-level caches outside it — `~/.gradle`, `~/.cache/ms-playwright`, a global
  npm cache — are untouched and will still speed up your bootstrap. Your command
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

### Path and glob rules

Resolution and deduplication work the same way on both surfaces:

- Paths are **relative to the worktree root** for that surface. Absolute paths are
  accepted but tie the config to one machine.
- `**` recursive globs are supported (`test-results/**/*.zip`).
- Directories that match a glob are skipped; only files are ingested.
- Duplicate matches across patterns are deduplicated.
- One path or glob per line in the YAML list; blank lines are ignored, so a
  trailing newline does not read as "configured".
- Matching is evaluated **per field, not per entry.** `junit_xml_paths` resolves
  as one group and `artifact_paths` as another. A single entry that matches
  nothing is tolerated as long as some other entry in the same field matched.
  That is what makes optional trace/screenshot/video globs safe to list next to
  a report that is always produced.

What a discovery problem *does*, however, differs by surface:

| Condition | Validation (`validation.junit_xml_paths`) | E2E (`e2e.junit_xml_paths`, `e2e.artifact_paths`) |
|---|---|---|
| The whole `junit_xml_paths` group matches no files | No structured cases; the validation command's own pass/fail outcome is unchanged | Run fails |
| The whole group matches only files older than the run | No structured cases; outcome unchanged | Run fails |
| The whole `artifact_paths` group matches no files | n/a — validation has no `artifact_paths` | Run fails |
| One entry matches nothing, another in the same field matches | Tolerated | Tolerated |
| A matched path resolves outside the worktree root | No structured cases; outcome unchanged | Run fails |
| A matched report is malformed or rejected by the parser | No structured cases; outcome unchanged | Run fails |

Read that as two different jobs. On **E2E**, report discovery is load-bearing, so
it is loud by design — a silently missing report looks identical to a passing
suite. On **validation**, structured cases are best-effort evidence layered on
top of a command that already has its own exit status: a validation command that
fails during typecheck, before its test step ever writes a report, still reports
that real failure. It just has no per-case view to render.

Path escapes and malformed reports therefore fail *the run* only on E2E. Both
surfaces still refuse to ingest them.

---

## What the JUnit parser accepts

The orchestrator reads any `.//testcase` elements, so single-suite and
multi-suite reports both work.

| Element / attribute | Becomes |
|---------------------|---------|
| `testcase@name` (required, non-empty) | Display name |
| `testcase@classname` | Suite name; combined as `classname::name` for the case ID |
| `testcase@time` | Duration in seconds. Optional, but when present and non-empty it must be float-like — a non-numeric value is a parse rejection, not a dropped duration |
| `<failure>` child | Outcome `failed`, with `message` + body as failure details |
| `<error>` child | Outcome `error` |
| `<skipped>` child | Outcome `skipped` |
| none of the above | Outcome `passed` |
| `<system-out>` / `<system-err>` | Captured output, truncated at 100,000 characters per channel |

A report is rejected when the file is missing, is not well-formed XML, contains
zero `<testcase>` entries, has a `testcase` with an empty `name`, or carries a
typed attribute it cannot parse — a non-numeric `testcase@time` is the one you
are most likely to hit. Treat that list as illustrative rather than exhaustive:
anything the parser cannot turn into a typed case is a rejection.

What a rejection costs you depends on the surface, per the matrix above. On the
E2E runner it fails the run. On validation it yields no structured cases and
leaves the validation command's own outcome alone.

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
- There is no marker protocol or payload contract for third-party runners to
  inject extras. Today `extras` is populated only by Issue-Orchestrator-owned
  payload/view-model translators — the built-in `io.agent-context` entry is
  synthesized by the dashboard's E2E canonical-payload translator from
  linked-issue data the orchestrator already holds. Generic JUnit ingestion
  produces none: the JUnit parser has no `extras` concept at all, and view-model
  normalization emits `extras: []` for cases that arrive without one. Nothing a
  client runner can write into a report reaches this slot.
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
5. Confirm artifact buttons appear for the files your patterns actually matched.
   Count files, not config lines: one `artifact_paths` entry is a path or glob
   and may produce zero, one, or many buttons. What matters is that the group
   produced something and that the artifacts you rely on are among them.

For per-run SQL checks against `.issue-orchestrator/e2e.db`, see the debugging
section of [E2E Test Runner](e2e.md#debugging).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Results tab shows only `Raw Output` | No JUnit emitted or path did not resolve | Verify the file exists after a manual run; mirror the exact path into `junit_xml_paths` |
| E2E run fails: "Configured JUnit XML paths did not resolve to any files" | No entry in `e2e.junit_xml_paths` matched | Fix the emitted path or the glob. Check the path is worktree-relative |
| E2E run fails: "…did not resolve to any fresh files" | Every match predates this run | Your command is not rewriting the report. Delete stale reports at start-of-run |
| E2E run fails: "Configured artifact paths did not resolve to any files" | No entry in `e2e.artifact_paths` matched | Remove the entry, or make the command always produce it |
| E2E run fails: "JUnit XML did not contain any `<testcase>` entries" | The suite collected zero tests, or the reporter wrote a stub | Fix test selection; confirm the reporter ran |
| E2E run fails: "E2E report path resolves outside repo root" | The path escapes the worktree (`../`, or a symlink out) | Write reports inside the worktree |
| One optional E2E glob never matches, but the run still passes | Matching is per field, not per entry — another entry in that field matched | Working as designed. Config cannot mark a single entry required; there is exactly one `junit_xml_paths` group and one `artifact_paths` group. If a specific file is mandatory, check for it in your command or wrapper script and exit non-zero when it is missing |
| E2E run fails on a green suite, but passes when tests fail | Every `artifact_paths` pattern is conditional (failure-only screenshots, disabled video), so a passing run leaves the group empty | Anchor the group with an artifact written unconditionally, or unset `artifact_paths` |
| Validation event has no structured cases | Validation exited before the test step, the report was stale, or the report was unreadable | Check the validation stdout log for where the command stopped. Validation never fails *because of* discovery — the command's own exit status stands |
| Dependencies missing on every E2E run | The worktree is `git clean -fdx`'d between runs | Install inside your command or wrapper script |
| Auto-quarantine entries keep disappearing | The forced checkout restores tracked files | Commit quarantine entries to the repository |

---

## Related documentation

- [E2E Test Runner](e2e.md) — async runner lifecycle, retry/resume, quarantine, API, debugging
- [Configuration Reference](configuration_reference.md) — every `validation.*` and `e2e.*` field
- [Configuration](configuration.md) — getting started with config files
- [Validation System](../architecture/validation.md) — publish gate design
- [Feature List](features.md) — capabilities grouped by user problem
