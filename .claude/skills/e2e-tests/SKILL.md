---
name: e2e-tests
description: Run end-to-end tests against a target repository. Use when running e2e tests, debugging e2e failures, or setting up e2e test infrastructure.
---

# E2E Test Runner

Async E2E test runner that executes tests locally with dashboard visibility and resume support.

## When to Use

- Running or debugging E2E tests
- Configuring E2E auto-trigger
- Investigating E2E failures
- Managing quarantined tests
- Understanding E2E progress/status

## Configuration

E2E settings are defined in `src/issue_orchestrator/infra/settings_schema.py` (`E2ESettings` model) and drive the web settings dialog, API, and wizard defaults. The schema is the single source of truth.

```yaml
# .issue-orchestrator/config/modes/<mode>/*.yaml
e2e:
  enabled: true
  role: "auto"                     # auto | executor | reader | disabled
  auto_run_interval_minutes: 30    # 0 = manual only
  pytest_args: ["tests/e2e", "-v"]
  allow_retry_once: true           # Retry failing tests once
  quarantine_file: "tests/e2e/quarantine.txt"
  stop_on_first_failure: false     # Add -x when true
  auto_quarantine: true            # Auto-add failing tests to quarantine list
  auto_create_issues: true         # Create GitHub issues for failures
  issue_agent_label: "agent:backend"
  flake_threshold: 20              # Flip-rate percent for flaky classification
  flake_window_runs: 10
  run_retention_count: 50
  survive_restart: true            # Let worker continue if orchestrator restarts
```

Only instances whose resolved role is `executor` auto-trigger runs. `reader` and `disabled` instances ignore `auto_run_interval_minutes`.

## Key Files

| File | Purpose |
|------|---------|
| `infra/e2e_db.py` | SQLite persistence for runs and results |
| `infra/e2e_runner.py` | Worker manager, auto-trigger logic |
| `entrypoints/e2e_worker.py` | Pytest subprocess with result plugin |
| `entrypoints/control_api_e2e_runs.py` | Run/status/log/quarantine endpoints at `/control/e2e/*` |
| `entrypoints/control_api_e2e_triage.py` | E2E failure triage endpoints |

## Database Schema

Results stored in `.issue-orchestrator/e2e.db`:

```sql
-- Runs table
e2e_runs: id, status, started_at, finished_at, total_tests, current_test, worker_pid, ...

-- Per-test results
e2e_test_results: run_id, nodeid, outcome, duration_seconds, longrepr, retry_outcome, is_quarantined

-- Failure issue tracking and stability
e2e_failure_issues: nodeid, issue_number, issue_url, status, resolved_at
e2e_flake_history: nodeid, run_id, outcome, retry_outcome
```

## Progress Tracking

The runner tracks progress in real-time:
- `total_tests`: Set after pytest collection phase
- `current_test`: Updated as each test starts
- `completed/passed/failed/skipped`: Counted from results table

Dashboard polls `/control/e2e/status` every 2 seconds while running.

## Linked Issue Lifecycles

When an E2E run exercises orchestrator issues, the issue drill-in should expose the same run-scoped evidence as the dashboard: coder/reviewer session recordings, review transcript, validation details, review report, and decision JSON. Review report is the primary review artifact action; decision JSON is secondary/menu evidence. Pin this with `tests/unit/test_e2e_timeline_convergence.py` when changing lifecycle artifacts or timeline actions.

## Resume Support

When orchestrator restarts mid-run:

1. **Orphan detection**: `start_run()` checks if existing "running" run has dead `worker_pid`
2. **Mark interrupted**: Dead runs marked as `status='interrupted'`
3. **Resume**: `start_or_resume()` finds interrupted runs, gets passed nodeids
4. **Skip passed**: Passes `--deselect <nodeid>` to pytest for each passed test

**Test structure for resumability:**
```python
# Good - each function is a checkpoint
def test_create_issue(): ...
def test_create_pr(): ...
def test_review_cycle(): ...

# Bad - monolithic, no partial progress
def test_entire_workflow(): ...
```

## API Endpoints

If you have older curl snippets saved locally, add `config_name=default.yaml` (or the config file you are actually running). The E2E control endpoints now require it.

```bash
API_TOKEN="$(cat ~/.issue-orchestrator/api-token)"

# Start E2E (or resume interrupted)
curl -X POST http://localhost:8080/control/e2e/start \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"repo_root": "'$(pwd)'", "config_name": "default.yaml"}'

# Stop running E2E
curl -X POST http://localhost:8080/control/e2e/stop \
  -H "Authorization: Bearer ${API_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"repo_root": "'$(pwd)'", "config_name": "default.yaml"}'

# Get status with progress
curl "http://localhost:8080/control/e2e/status?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq

# List recent runs
curl "http://localhost:8080/control/e2e/runs?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq

# Get run details with test results
curl "http://localhost:8080/control/e2e/run/1?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq

# Get run timeline/logs/failed tests
curl "http://localhost:8080/control/e2e/run/1/timeline?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq
curl "http://localhost:8080/control/e2e/logs/1?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}"
curl "http://localhost:8080/control/e2e/failed/1?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq

# View or update quarantine list
curl "http://localhost:8080/control/e2e/quarantine?repo_root=$(pwd)&config_name=default.yaml" \
  -H "Authorization: Bearer ${API_TOKEN}" | jq
```

## Debugging E2E Failures

### Check Status
```bash
# Via API
curl -s "http://localhost:8080/control/e2e/status?repo_root=$(pwd)&config_name=default.yaml" | jq

# Check if worker is running
ps aux | grep e2e_worker

# View database
sqlite3 .issue-orchestrator/e2e.db "SELECT id, status, total_tests, started_at FROM e2e_runs ORDER BY id DESC LIMIT 5"
```

### View Logs
```bash
# Find latest log
ls -lt .issue-orchestrator/logs/e2e/ | head -5

# Tail log
tail -f .issue-orchestrator/logs/e2e/run_*.log
```

### View Failed Tests
```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT nodeid, outcome, longrepr
  FROM e2e_test_results
  WHERE run_id = (SELECT MAX(id) FROM e2e_runs)
    AND outcome = 'failed'
"
```

### Check Progress Mid-Run
```bash
sqlite3 .issue-orchestrator/e2e.db "
  SELECT
    (SELECT COUNT(*) FROM e2e_test_results WHERE run_id = r.id) as completed,
    r.total_tests,
    r.current_test
  FROM e2e_runs r
  WHERE r.status = 'running'
"
```

## Quarantine Management

Quarantine file lists known flaky tests excluded from failure count:

```
# tests/e2e/quarantine.txt
# Known flaky tests - excluded from required runs
tests/e2e/test_slow_network.py::test_timeout_handling
tests/e2e/test_race_condition.py::test_concurrent_updates
```

Quarantined tests still run but:
- Marked with `is_quarantined=1` in results
- Excluded from `get_failed_tests()`
- Don't affect pass/fail status

## Common Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| E2E not auto-triggering | `auto_run_interval_minutes: 0` | Set to positive value |
| E2E not auto-triggering on this machine | role resolved to `reader` or `disabled` | Set `e2e.role: executor` on the runner |
| Worker exits immediately | Invalid pytest args | Check `pytest_args` path exists |
| Worker exits with `No module named 'pytest'` | Repo sync did not install pytest | Re-run on current code; the E2E worktree now bootstraps fallback pytest |
| Worker exits with missing `uv.lock` while using `uv sync --frozen` | Old orchestrator code path | Re-run on current code; repos without `uv.lock` are supported |
| "AlreadyRunning" error | Previous worker still running | Stop via API or kill process |
| No progress shown | Old schema without `total_tests` | Delete e2e.db, will recreate |
| Tests not resuming | Monolithic test structure | Split into discrete test functions |
| Quarantine not working | Wrong `quarantine_file` path | Check path relative to repo root |

## Auto-Trigger Logic

E2E auto-triggers when ALL conditions met:
1. `e2e.enabled: true`
2. `auto_run_interval_minutes > 0`
3. The resolved E2E role is `executor`
4. Enough time passed since last E2E run
5. Main branch HEAD changed since the last tested commit
6. No E2E currently running

Check auto-trigger in logs:
```bash
grep "E2E auto-trigger" .issue-orchestrator/state/logs/orchestrator.log
```
