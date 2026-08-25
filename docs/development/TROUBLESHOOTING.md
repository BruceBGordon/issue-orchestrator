# Troubleshooting

## Quick Debugging

**Check what's running:**
```bash
issue-orchestrator status
```

**See session output:**
```bash
issue-orchestrator output <issue_number>
```

**Attach to session:**
```bash
issue-orchestrator attach <issue_number>
```

**Check web dashboard:**
```bash
curl -s http://localhost:8080/api/status | jq
curl -s http://localhost:8080/api/state | jq
```

## Audit Surfaces

The repo has multiple things called "audit". They answer different questions.

**Queue audit:** why an issue is queued, skipped, blocked, or already in progress.
```bash
issue-orchestrator audit
curl -s "http://localhost:8080/control/tools/audit?repo_root=$(pwd)" | jq
```

**Issue audit:** force a fresh failure diagnosis for one issue or stalled run.
This is the right tool when a coding/review session timed out, never wrote
`coding-done`, or looks off relative to the timeline.
```bash
curl -s -X POST "http://localhost:8080/api/issues/4057/audit" | jq
curl -s "http://localhost:8080/api/failure-diagnosis/4057" | jq
```

**Session diagnostics:** inspect the run-scoped manifest and artifact actions for
the latest run or a specific `run_dir`.
```bash
curl -s "http://localhost:8080/api/dialog/session-diagnostics/4057" | jq
curl -s "http://localhost:8080/api/session/manifest/4057" | jq
```

Use them in this order:
1. Queue audit when the issue never started.
2. Issue audit when a specific run failed or timed out.
3. Session diagnostics when you need exact run-scoped files and replay paths.

## "Why is the orchestrator paused?"

A paused engine keeps ticking and logging, so it looks alive while doing nothing.
Answer this in one step — the pause journal is durable and survives restarts:

```bash
# Newest transition last: why, who, when, and how long the last pause held.
tail -5 .issue-orchestrator/state/pause-journal.jsonl | jq
```

Or ask the running engine, which reports the same provenance:

```bash
curl -s http://localhost:8080/api/status | jq '{paused, pause_reason, pause_actor,
  paused_since, paused_held_seconds, pause_is_incident, pause_detail}'
```

The log tail also carries it — every `[LOOP]` line names the reason while paused:

```
[LOOP] Iteration 11169 - active=0 paused=True [reason=loop_error_threshold by=system
  since=2026-08-17T09:37:19+00:00 held=196255s detail=3 consecutive tick errors; ...]
```

**Reading the reason:**

| `pause_reason` | Meaning | Clears itself? |
|---|---|---|
| `operator` | Someone clicked Pause (`pause_actor` says which surface) | No — resume it |
| `startup` | Started with `--start-paused` | No — resume it |
| `tech_lead_investigation` / `tech_lead_health_review` | The planner is halted for the length of a tech-lead run | Yes, when the run ends |
| `loop_error_threshold` | **Incident.** Three consecutive tick errors tripped the breaker | Yes — half-open retry on a 60s/5m/15m/1h ladder |

Only `loop_error_threshold` is a fault (`pause_is_incident: true`). Its
`pause_detail` carries the last exception, which is usually the whole answer —
`GitHubAuthError: [Errno 8] nodename nor servname provided` means DNS was gone,
typically because the host slept.

Resume sooner than the backoff with:

```bash
curl -s -X POST http://localhost:8080/api/resume | jq
```

An incident pause that keeps recurring is the signal to chase — the ladder only
resets after a healthy tick, so a climbing backoff means the fault is real and
not a blip.

## Session Output Directory

All session artifacts are centralized in a run directory per session:

```
<worktree>/.issue-orchestrator/sessions/
├── <run_id>__<session_name>/     # e.g., 20260120-143052Z__issue-42
│   ├── manifest.json             # Session metadata (start time, paths, outcome)
│   ├── terminal-recording.jsonl  # Terminal output (NDJSON with base64 PTY events)
│   ├── validation-record.json    # Validation pass/fail result
│   ├── validation-stdout.log     # Validation command stdout
│   ├── validation-stderr.log     # Validation command stderr
│   ├── validation-errors.txt     # Human-readable validation errors
│   ├── orchestrator-tail.log     # Filtered orchestrator log for this session
│   └── claude-session.jsonl      # Symlink to Claude session log
├── <session_name>                # Symlink to latest run for this session
├── latest.json                   # Pointer to most recent run
└── index.json                    # List of all runs
```

**Quick navigation:**
```bash
WORKTREE="/path/to/worktree"

# Find the latest run
RUN_DIR=$(ls -td $WORKTREE/.issue-orchestrator/sessions/*__* 2>/dev/null | head -1)

# Check manifest for session metadata
cat $RUN_DIR/manifest.json | jq

# Check terminal recording (NDJSON format — use orchestrator replay, not cat)
ls -lh $RUN_DIR/terminal-recording.jsonl

# Check validation errors
cat $RUN_DIR/validation-errors.txt

# List all runs in a worktree
cat $WORKTREE/.issue-orchestrator/sessions/index.json | jq '.runs'
```

## Common Issues

### Dependency Changes Not Reflected Locally

**Symptom:** You updated `pyproject.toml`, but dependencies or `uv.lock` are out of sync.

**Fix:** Run `make upgrade-deps` to re-resolve and sync, then commit `uv.lock` alongside
the `pyproject.toml` change.

### Sessions Failing Without Completion

**Symptom:** Sessions end with "without completion markers", marked as FAILED.

**Causes:**
1. Agent prompt doesn't include `coding-done`/`reviewer-done` instructions
2. Pre-push hook blocking push
3. Agent crashing/timeout before completion

**Fix:** Ensure agent prompts include `coding-done`/`reviewer-done` usage in "When Done" section.

### Pre-Push Validation Failed

**Symptom:** `git push` fails with validation errors.

**Finding the output:** When validation fails, the full output is saved to a known location. The exact location depends on how validation was run:

1. **Orchestrator-managed sessions**: Output goes to the session directory
   ```
   <worktree>/.issue-orchestrator/sessions/<run_id>__<session>/validation-output.log
   ```

2. **Direct runs** (human running `make validate`): Falls back to diagnostics
   ```
   <worktree>/.issue-orchestrator/diagnostics/validation-output.log
   ```

The failure message always prints the path to the output file:
```
============================================================
Validation FAILED (exit code 1) in 45.2s
============================================================

Full output saved to:
  /path/to/worktree/.issue-orchestrator/diagnostics/validation-output.log

To view: cat /path/to/worktree/.issue-orchestrator/diagnostics/validation-output.log
============================================================
```

**How it works:** The `make validate` target runs validation through a Python wrapper (`validate_runner.py`) that captures all output while also streaming it to the terminal. This ensures agents can find failure details without re-running tests.

**Explicit diagnostic path:** `make validate-raw` runs directly without output
capture. It is never selected automatically when the Python wrapper fails.

**Tuning local PR validation parallelism:** `make validate-pr` launches static
analysis and six test commands concurrently. Each command declares an accepted
concurrency range under a human-readable IO work key. The measured defaults on
the 18-core host are static 1–3, unit 8–24, simulated 4–8, local integration
2–4, Claude 1–2, Codex 2–3, and browser 4–12. Those are accepted ranges, not
simultaneous reservations. The host executor learns CPU occupancy for each key
and grants the largest value that fits the active machine-wide leases.

Every range-based pytest command uses `-n auto`. This is required: xdist reads the
executor grant from `PYTEST_XDIST_AUTO_NUM_WORKERS`, while a numeric `-n N`
would bypass the grant. `--dist=loadgroup` preserves serial tests within each
declared provider interaction group without collapsing the entire provider lane
to one xdist worker.

For a repeatable experiment, set a numeric `UNIT_PARALLEL`,
`SIMULATED_PARALLEL`, `INTEGRATION_PARALLEL`, `CLAUDE_PROVIDER_PARALLEL`,
`CODEX_PROVIDER_PARALLEL`, or `WEB_PARALLEL`. The Make target converts that
number to a fixed minimum/maximum executor grant while keeping xdist on `auto`.
Set a value to `0` to disable xdist for that command. `PROVIDER_PARALLEL`
remains the shared override for both provider commands. `VALIDATE_LANE_JOBS`
only controls how many commands Make may submit; the executor owns admission.

All repositories and worktrees under the same OS user share the host pool. The
executor detects its internal CPU-slot capacity; the aggressiveness percentage
is the one host-pressure dial. Provider and browser lanes also hold named
exclusive leases so two
orchestrated issues cannot run the same scarce local integration concurrently.
Every top-level IO validation gives all its lanes one fairness group. The pool
accounts admitted CPU slots per live group, so a newly waiting light Porchpin
validation runs before more lanes from an already-served heavy IO validation.
An old request that needs several CPU slots drains enough capacity to run
instead of being starved by a stream of small work.
See [Client Test Integrations](../user/test-integrations.md#bound-validation-concurrency-across-repositories)
for the generic repository wrapper.

Each queued command also samples native host CPU busy time over its admission
interval. A `waiting reason=host-pressure` event means the sample reached the
internal 95% threshold, so the executor stopped admitting new commands until a
later sample showed recovery. Use `executor-events` to distinguish this from
capacity, fairness, exclusive-resource, and lease-race waits. Load average is
recorded for context but does not drive this decision.

The 2026-08-24 backtracking calibration on the 18-core, 64 GiB host measured the
full validation-result-cache-miss gate at 95.86s with 100% aggressiveness,
84.24s at 125%, and 96.76s at 150%. The 125% point improved on 100% by about
12%, while 150% gave the gain back and slowed local static and unit work as well
as the variable provider path. That places 125% at the measured knee; more
parallelism was not a credible further 5% win. Use
`issue-orchestrator executor-policy --aggressiveness 125` on this host.

Two exact clean committed `make validate-pr` runs then passed in **83.888s** at
`443ecdd` and **81.71s** at `5b10693` (82.80s mean). The latter's seven-lane
phase took 79s: browser 20s, static 38s, simulated scenarios 42s, unit 48s,
local integration 55s, Codex 55s, and Claude 79s; VS Code took 1s afterwards.
The executor granted xdist-aware work between 2 and 12 workers according to
learned demand, rather than the historical accidental single-worker behavior.
These are the normal-response confirmations for the approximately 85-second
goal.

After process-group, restart-watchdog, profiler-cleanup, and crash-safe
persistence hardening, an exact clean committed gate at `4e35b20` passed in
**85.09s** retained validation time (**85.41s** measured wall time), with zero
swaps. This is the final post-hardening confirmation rather than an earlier
cache-only or pre-review measurement.

Timing rows live in the repository's shared Git directory and can therefore
survive a machine migration. For calibration, select records by the captured
host name, OS, architecture, CPU count, and physical-memory bytes; a date alone
does not prove which machine produced a row. Records without the new host
identity, or with the previous machine's identity, are historical context
rather than evidence for this calibration.

Across 222 resource samples recorded on this 18-core/64 GiB host, memory-free
pressure never fell below 86% and swap use remained 0 MiB. The largest observed
single lane was static at about 1.2 GiB RSS. There is therefore no evidence that
64 GiB makes this gate faster than 48 GiB would; the measured workload had ample
headroom at 48 GiB. The extra memory is still useful headroom for simultaneous
repositories, browsers, worktrees, IDEs, and agent sessions, but current data
does not justify memory-aware admission or another RAM upgrade for this use
case.

This is repeatable scheduler behavior, not a hard wall-clock upper bound on
remote services. A later exact fresh-pool profile of clean commit `b38378e`
passed both aggregates but took 146.39s cold and 148.09s learned. The retained
evidence attributes the result: Claude was admitted after at most 0.124s, then
spent 141.18s cold and 143.36s learned in its command while using only
30.93–33.36 child CPU seconds. Learning changed aggregate time by just 1.70s,
well inside provider variance. The same profile's isolated lanes were Claude
77.02s, Codex 62.70s, unit 41.37s, integration 27.47s, static 27.80s, web
21.16s, simulated 18.82s, and VS Code 2.40s. More CPU concurrency cannot remove
an external response tail.

One otherwise-identical earlier confirmation took 128.8s because the real
interactive Codex smoke alone varied to 122.02s; its executor admission took
0.12s and its entire lane consumed only about 20 child CPU seconds. An
instrumented isolated run split 42.5s into 9.0s of interactive startup, 4.1s of
safe prompt submission, and 29.4s awaiting the review response. Faster-model
experiments were not retained: Spark was fast but emitted an invalid protocol
value in one of three runs, while Luna and Terra were slower in measured
samples. A larger future optimization could start an idle interactive provider
and make the first real review specification its first model turn, instead of
paying for a bootstrap "waiting" turn; that changes the production session
contract and belongs outside executor tuning.

Removing any remaining lane-arrival-order component requires an explicit
batch-submission contract; do not simulate one with lane priorities or startup
sleeps. "Uncached" here bypasses the validation-result cache only. Normal OS
filesystem, installed dependency, browser, CLI, and external-service caches
remain part of the real-world measurement.

`make -f repo-specific/Makefile validate-profile` resolves one exact commit SHA
before discovery, then measures detached fresh worktrees pinned to that SHA.
Moving `HEAD` or dirtying the source worktree during the profile cannot change
its target inventory or measured code. Each profile invocation creates one
fresh executor-learning pool shared by its cold aggregate, isolated lane
training, and learned aggregate, while normal external caches remain enabled. Its
`VALIDATE_JOBS` value controls both aggregate GNU make fan-out and the inner
validation-lane fan-out; the JSON report records both facts explicitly. The
headline serial sum includes the aggregate static lane exactly once; nested
typecheck, architecture, and quality components are not double-counted as
independent execution lanes. A sibling `*-artifacts` directory retains one
combined-output log per command and the exact fairness-group event suffix for
each aggregate, serialized as explicitly discriminated typed executor events.
`total_matching_event_count` says how many events belonged to that aggregate;
check `possibly_truncated` before treating the retained suffix as complete.
The profiler stops on its first failed stage and writes a typed partial report
with the failed command and every completed predecessor. It never prints or
persists learned/cold comparisons after an incomplete experiment.

Use `issue-orchestrator executor-events --limit 100` to inspect the durable
typed decision trail after a run. It reports human repository/work identities,
wait reasons, grants, internal CPU-slot arithmetic, native CPU busy samples and
sample intervals, resource observations, and learned-estimate changes. It
reads through the executor monitoring port rather than parsing logs or private
state.

To exercise an IO lane that has no exclusive resource without the host pool,
use the standalone direct executor with the same command contract:

```bash
make test-unit EXECUTOR_RUN=./scripts/executor-run-direct
```

The direct executor grants the declared maximum but performs no cross-process
coordination or learning. It fails rather than silently ignoring an exclusive
resource, so it cannot replace the pooled executor for IO's complete PR gate.

**Environment variable:** The orchestrator sets
`ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR` to direct output to the session
directory. Direct runs use the repository diagnostics directory by default.

### Pre-Push Hook Infinite Recursion

**Symptom:** Push hangs forever, hook log shows repeated "Pre-push hook started".

**Cause:** When worktrees reused, `install_hooks()` reads `core.hooksPath` from worktree config (which has our override), copies the chained wrapper as "project hook".

**Fix:** Code now reads `core.hooksPath` from main repo only. To repair existing worktrees:
```bash
MAIN_HOOK="/path/to/repo/.githooks/pre-push"
for dir in /path/to/repo-*/; do
  HOOKS_DIR="/path/to/repo/.git/worktrees/$(basename $dir)/hooks"
  if grep -q "Chained pre-push" "$HOOKS_DIR/pre-push.project" 2>/dev/null; then
    cp "$MAIN_HOOK" "$HOOKS_DIR/pre-push.project"
  fi
done
```

### Main Repo hooksPath Corrupted

**Symptom:** Pushes from main repo fail, `git config core.hooksPath` shows worktree path.

**Fix:**
```bash
cd /path/to/main/repo
git config --unset core.hooksPath
git config core.hooksPath .githooks
```

### Missing Labels

**Symptom:** Warnings about labels not found.

**Fix:**
```bash
gh label create "failed" -R owner/repo --description "Agent session failed" --color "B60205"
```

### Lock Cleanup

Locks stored in `.issue-orchestrator/locks/` (per-instance JSON files). Cleanup runs at startup.

Manual cleanup:
```bash
rm .issue-orchestrator/locks/*.json
```

## E2E Timeline: Missing Issue Affordances on Test Rows

**Symptom:** The dashboard's E2E run drawer shows test rows with no clickable
`#N` issue affordances even though agents clearly ran for issues during the
run window.

**Root cause class:** The view-model pipeline that attaches `issue_numbers`
to `e2e.test_started` / `e2e.test_completed` events is brittle. Bugs have
included: a placeholder `issue_number=0` collapsing all events, view
filtering dropping debug-only events before matching, narrow time-window
boundaries missing in-progress tests.

### Fast iteration loop (no PR/restart cycle)

Use `scripts/debug_e2e_timeline.py` to replay the production matcher
against your real DBs in-process. It loads `e2e.db` + the base-repo
`timeline.sqlite` + the e2e-worktree `timeline.sqlite` from a checkout,
runs the same code as the live endpoint, and prints which test windows
have issue numbers attached and which agent events are unmatched:

```bash
.venv/bin/python scripts/debug_e2e_timeline.py --run-id 87
```

Edit code → re-run → see effect immediately. To cross-validate against
the live endpoint (catches endpoint-vs-helper drift):

```bash
# Terminal 1
issue-orchestrator start  # or restart your running instance

# Terminal 2
PORT=$(lsof -p $(pgrep -f run_orchestrator) | awk '/LISTEN/{sub(".*:","",$9); print $9; exit}')
diff <(curl -s http://localhost:$PORT/api/e2e-run-detail/87 | python3 -c '...') \
     <(.venv/bin/python scripts/debug_e2e_timeline.py --run-id 87)
```

A diff there means the live endpoint and the helper disagree — usually
because something in the endpoint pipeline (filtering, projection,
view-model) is mutating data the helper test bypasses.

### Pin the regression with a captured fixture

Once you've reproduced and fixed a bug, capture the run as a fixture so
it can never regress silently again:

```bash
.venv/bin/python scripts/snapshot_e2e_run.py --run-id 87
```

This writes a sanitized, self-contained snapshot to
`tests/fixtures/e2e_runs/run_87/` (e2e.db row, base timeline, worktree
timeline, expected.json). The integration test
`tests/integration/test_e2e_timeline_real_fixture.py` discovers every
fixture under that directory and replays each through the live
`/api/e2e-run-detail/{id}` endpoint, asserting per-test
`issue_numbers` against the captured ground truth.

If a fixture starts failing because the contract LEGITIMATELY changed
(matcher logic, view-model shape), re-bless it by re-running the
snapshot script against the same run.

## Claude Session Logs

Each Claude Code session creates logs useful for debugging:

**Log Locations:**
```
~/.claude/
├── projects/<escaped-path>/     # Per-project session history
│   └── <session-id>.jsonl       # Conversation history
├── debug/<session-id>.txt       # Debug logs
├── history.jsonl                # Global command history
└── todos/<session-id>-*.json    # Todo lists per session
```

**Path Escaping:** `/Users/bruce/dev/myproject` -> `-Users-bruce-dev-myproject`

**Quick access via run directory:**
```bash
# The run directory has a symlink to the Claude log
ls -la $RUN_DIR/claude-session.jsonl

# Or get the path from manifest
cat $RUN_DIR/manifest.json | jq -r '.claude_log_path'
```

**Legacy method (finding sessions for a worktree):**
```bash
WORKTREE="/path/to/worktree"
ESCAPED=$(echo "$WORKTREE" | sed 's|^/|-|' | tr '/' '-')
ls -la ~/.claude/projects/$ESCAPED/

# View most recent session log
ls -t ~/.claude/projects/$ESCAPED/*.jsonl | head -1 | xargs head -100
```
