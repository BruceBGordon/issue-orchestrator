# Async E2E Test Runner

The Issue Orchestrator can run long-lived E2E suites in a background worker and surface the results in the dashboard without making the product pytest-shaped.

This page covers the **async runner itself**: its execution model, run lifecycle, retry/resume semantics, quarantine, control API, and debugging.

> **Setting up your repo's tests?** Start with
> [Client Test Integrations](test-integrations.md). That is the canonical guide for
> choosing between validation gates and the E2E runner, emitting JUnit XML,
> capturing artifacts, per-framework recipes (pytest, Playwright, Vitest/Jest,
> Cypress, wrapper scripts), and the path/worktree rules those paths must obey.

The reporting model is:

1. Raw run output is always captured.
2. Structured case results are ingested from JUnit XML when the runner emits it.
3. Native framework artifacts such as HTML reports, traces, screenshots, and logs are linked as artifacts.
4. Agentic issue lifecycles, logical cycles, validation, and session logs appear as linked evidence when the tests exercised the orchestrator itself.

That gives one UI that works for both of these cases:

- `issue-orchestrator` running pytest-based agentic E2E tests
- external repos such as `tixmeup` running arbitrary commands that emit JUnit XML and artifacts

## Dashboard Model

Each E2E run now has two primary surfaces:

- `Results`: framework-neutral case outcomes, raw output, JUnit-backed results, and native artifacts
- `Timeline`: chronological run events plus linked issue lifecycles when the suite created or exercised issues

When an E2E run includes orchestrator work, the Results tab also shows `Linked issue lifecycles`. Those rows keep the semantically projected cycles visible and expose:

- `Timeline`
- `Coder Session`
- `Review Session`
- `Review Transcript`
- `Review Report`
- `Decision JSON`
- `Validation`

That is the critical bridge for agentic tests: a non-agentic suite is still debuggable from raw output and JUnit results, while an agentic suite additionally exposes logical cycles and UI session logs.

## Test Tiers

There are now two useful layers for onboarding and orchestration journeys:

- regular `tests/e2e` live coverage for issue pickup, session execution, review, and PR paths
- live agent transport acceptance for provider-dependent TUI contracts such as persistent prompt injection into Claude and Codex
- `heavy_e2e` journey coverage for broader flows such as onboarding, where a test may create a temp repo, run the setup wizard, install guardrails, and validate local doctor/guardrail behavior end to end
- an opt-in live agent-guided onboarding acceptance that lets a real `codex` or `claude-code` session onboard a GitHub-backed repo, then proves the first issue can launch

Run the heavy tier with:

```bash
make test-e2e-heavy
```

Keep this tier out of normal fast validation. It is intended for explicit runs, nightly coverage, or future provider-acceptance journeys.

Run the live agent-guided onboarding acceptance explicitly:

```bash
make test-e2e-onboarding-live

# Default provider is codex. To include Claude too:
E2E_AGENT_GUIDED_ONBOARDING_PROVIDERS=codex,claude-code make test-e2e-onboarding-live
```

The live onboarding acceptance is collection-gated behind `E2E_AGENT_GUIDED_ONBOARDING=1` so normal `heavy_e2e` runs do not burn GitHub cleanup calls just to skip it.

## Runner Modes

`e2e.runner_kind` selects the execution adapter. The two modes differ in what the runner can observe while the suite is running, not in how results are ingested.

| | `pytest` | `command` |
|---|---|---|
| Live per-test progress events | Yes | No |
| `allow_retry_once` | Yes | Ignored; the original command result is reported |
| `stop_on_first_failure` | Yes (adds `-x`) | Ignored |
| Resume after interruption | Yes, by deselecting already-passing nodeids | No; interrupted runs restart fresh |
| Suite definition | `e2e.pytest_args` | `e2e.command` |
| Structured results | JUnit XML ingested after the run | JUnit XML ingested after the run |

Both modes execute inside the E2E worktree, always capture raw output, and ingest `junit_xml_paths` / `artifact_paths` after the process exits. Ingestion is loud on this surface: the run fails when the configured JUnit list as a whole resolves to no fresh files, or when the artifact list as a whole resolves to nothing. Matching is per field rather than per entry, so one non-matching glob is tolerated while another entry in the same field matches — see [the surface matrix](test-integrations.md#path-and-glob-rules) for the exact contract.

Pytest resume works best when long workflows are split into discrete test functions, so already-passing nodeids can be deselected after an interruption.

For the YAML for each mode, per-framework recipes, and the path rules those globs must obey, see [Client Test Integrations](test-integrations.md).

## Results, Artifacts, And Session Logs

The Results tab intentionally separates universal debugging evidence from orchestrator-specific evidence.

Universal run evidence:

- canonical command
- run status
- started time
- duration
- `Raw Output`
- structured reports and additional artifacts

Agentic evidence, when present:

- linked issue lifecycles
- logical cycle chips
- coder/reviewer session recordings
- review transcript
- review report and decision JSON artifacts
- validation details

This matters because many E2E suites will not create issues on every failing test. In that case:

- the run is still debuggable from `Results`
- the Timeline still shows chronology
- linked lifecycle/session controls simply remain absent

## Retry And Resume Semantics

`runner_kind=pytest`

- supports live per-test progress
- supports retry-once
- interrupted pytest runs can resume through deselection of already-passing tests

`runner_kind=command`

- is framework-neutral
- ingests results after the command completes
- does not attempt pytest-style resume semantics
- interrupted runs restart fresh

### Orchestrator restarts

The E2E worker is a detached subprocess, so it can outlive an orchestrator restart.

`e2e.survive_restart` (default `true`) leaves a running worker alone on shutdown: neither the worker nor its `running` row is touched, so the detached worker keeps going and finishes the run normally. A restart is not a run boundary. Set it to `false` to stop the worker on shutdown and mark the running row canceled instead.

Orphan recovery is a separate, conditional path. If a surviving worker dies before finishing — machine reboot, OOM kill, manual `kill` — the row stays `running` with a PID that no longer exists. The next attempt to start a run detects that dead PID, marks the stale row `interrupted`, and proceeds. For `runner_kind=pytest`, an `interrupted` run is the one that can resume by deselecting already-passing nodeids. So a restart alone does not end and resume the in-flight run; only a worker that actually died does.

## Quarantine

`e2e.quarantine_file` names a plain-text file of test node IDs, one per line, with `#` comment lines allowed. It is read from the E2E worktree, so the path is a normal repository-relative path such as `tests/e2e/quarantine.txt`.

Quarantined tests still run and still appear in the Results tab. What changes is how their failures are scored:

- they are excluded from the retry-once pass
- when *only* quarantined tests failed, the run completes as `warning` rather than `failed`, and a non-zero runner exit code is ignored with a note explaining why
- a single non-quarantined failure still fails the run

Matching is exact against the node ID, for both live pytest observation and JUnit-ingested cases. If your JUnit `classname`/`name` pair does not produce the nodeid you expect, the entry will not match.

`e2e.auto_quarantine: true` appends every failing node ID to that file after a failed run, preserving any leading comment header. Because the E2E worktree is force-checked-out to the orchestrator's `HEAD` before each run, additions to a **tracked** quarantine file do not survive to the next run — commit the entries you want to keep.

Doctor verifies the quarantine file exists when E2E is enabled.

## API Endpoints

The dashboard uses these endpoints:

Authenticated control API calls require a bearer token from
`~/.issue-orchestrator/api-token`, the target repo root, and `config_name`.
Older examples that omit `config_name` will no longer work.

- `POST /control/e2e/start`
- `POST /control/e2e/stop`
- `GET /control/e2e/status`
- `GET /control/e2e/runs`
- `GET /api/e2e-run-detail/{run_id}`
- `GET /control/e2e/run/{run_id}/timeline`
- `GET /api/e2e-run/{run_id}/issue-detail/{issue_number}`
- `GET /api/session/terminal-recording/{issue_number}`
- `GET /api/session/review-transcript/{issue_number}`
- `GET /api/session/review-artifact/{issue_number}`

`/api/e2e-run-detail/{run_id}` is the main typed payload for the run drawer. It carries:

- run metadata
- results summary
- categorized case results
- artifacts and reports
- lifecycle projection

## Database Model

Run metadata lives in `.issue-orchestrator/e2e.db`.

Key tables:

- `e2e_runs`
- `e2e_test_results`
- `e2e_run_artifacts`

Important run fields:

- `pytest_args`
- `command_json`
- `runner_kind`
- `log_path`
- `artifacts_dir`

Important case fields:

- `display_name`
- `suite_name`
- `result_source`
- `outcome`
- `longrepr`

`result_source` tells you whether a row came from live runtime observation or a structured external report such as JUnit XML.

## Debugging

### Check recent runs

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT id, runner_kind, status, started_at, command_json
  FROM e2e_runs
  ORDER BY id DESC
  LIMIT 5
"
```

### Check the latest structured case results

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT nodeid, suite_name, outcome, result_source
  FROM e2e_test_results
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
  ORDER BY nodeid
"
```

### Check captured artifacts

```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT kind, label, path
  FROM e2e_run_artifacts
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
  ORDER BY kind, label
"
```

### Tail raw output

```bash
ls -lt .issue-orchestrator/logs/e2e/ | head -5
tail -f .issue-orchestrator/logs/e2e/run_*.log
```

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| E2E not auto-triggering | `auto_run_interval_minutes: 0` | Set it to a positive value |
| Command runner cannot resume | Resume is pytest-only | Restart the command run; use raw output and JUnit for debugging |
| Linked issue lifecycle is missing | The suite did not create or exercise issues in-window | Debug from Results/Timeline instead; lifecycle/session controls are additive |
| Session Recording button does nothing | The lifecycle command lacked valid run-scoped recording context | This is a bug; the dashboard should only emit phase-scoped session parameters when both `round_index` and `session_role` are available |

For result-ingestion problems — only `Raw Output` in the Results tab, unresolved `junit_xml_paths` / `artifact_paths` globs, rejected JUnit reports, or artifacts that vanish between runs — see the troubleshooting table in [Client Test Integrations](test-integrations.md#troubleshooting).

## Auto-Trigger Logic

E2E auto-triggers when all conditions are met:

1. `e2e.enabled: true`
2. `auto_run_interval_minutes > 0`
3. enough time passed since the last run
4. the tracked main branch HEAD changed
5. no E2E run is already active
