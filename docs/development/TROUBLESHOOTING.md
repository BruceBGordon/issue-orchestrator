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

## Retained Kill Evidence (review-exchange round failures)

Run directories live in homes that disappear — a torn-down agent worktree, or a
pytest tmp directory the async E2E runner rotates within the hour. When a
review-exchange round is declared failed (`prompt_not_accepted`, timeout,
process exit), the evidence is copied *out* of that home at declaration time
into the repository's retained diagnostics:

```
<repo-root>/.issue-orchestrator/diagnostics/exchange-kills/
├── index.jsonl                                # one line per capture, grep-able
└── <ts>__issue-<n>__<role>__round-R-attempt-A-respawn-K/
    ├── terminal-recording.jsonl               # copy of the role recording (tail-capped)
    ├── idle-trace.json                        # window config + bytes_drained trajectory
    └── run-identity.json                      # branch, HEAD SHA, session, run/exchange dirs
```

The exchange directory gets a matching
`round-R-<role>-attempt-A-respawn-K.kill-evidence.json` back-pointer, so the
cross-reference runs both ways and correlation never needs mtime archaeology.

Two kinds of kill land here:

- `failure_reason` other than `abandoned_by_teardown` — the round declared its
  own failure (prompt not accepted, timeout, process exit).
- `failure_reason: abandoned_by_teardown` — the round was *still running* when
  the pair was released (supervisor wall-clock deadline, operator cancel,
  orchestrator shutdown). The capture is taken by the pair registry before it
  closes the sessions. If the wedged round had reached its poll loop the live
  idle trajectory comes with it; otherwise `idle_trace_unavailable` says why
  there is none.

```bash
KILLS=.issue-orchestrator/diagnostics/exchange-kills

# Every capture for one branch, newest last
jq -c 'select(.branch == "my-branch") | {captured_at, role, failure_reason, composer_state: .composer_state.state, retained_dir}' $KILLS/index.jsonl

# Did the prompt ever submit? composer_stranded = the injected text never left
# the composer (injection/settle race); composer_emptied = the submit
# registered and the provider then went silent; undetermined = the recording
# could not be reconstructed faithfully, so no verdict was guessed.
jq '.composer_state' $KILLS/<capture>/run-identity.json

# Did the agent produce anything at all after the prompt?
jq '.idle_trace | {window_seconds, idle_for_seconds, bytes_drained_total}' $KILLS/<capture>/idle-trace.json
```

A frozen `bytes_drained_total` across the whole `samples` trajectory means the
agent never engaged with the prompt. Combine that with `composer_stranded` and
you are looking at the PR #6484 injection/settle family, not a provider stall.

`composer_state` is read off the **rendered final viewport**, not the raw byte
history, so an erased footer cannot support a verdict. That viewport reproduces
the *bundled* xterm's screen — including its width model, under which an emoji
is one cell and a ZWJ family is four — so the screen a verdict rests on is the
screen the session viewer draws. Regenerate the measurements behind that with
`node tools/measure_xterm_widths.js`. Anything that makes the
reconstruction untrustworthy — a half-written recording, an unparseable row, an
undecodable payload, an implausible `resize`, a replay the capture budget cut
short, or a grid-affecting terminal mode the viewport does not model — yields
`undetermined` rather than a guess. Autowrap (DECAWM) *is* modelled, because
TUIs toggle it constantly and a footer's row depends on it. `matched_marker` names
the affordance that decided it and `evidence_snippet` is the screen row it came
from — check them before acting on the classification. `replayed_from_start:
false` means only a trailing window of the recording was replayed; the verdict
is still sound (only rows the replay wrote are searched) but the screen above
the footer band may be incomplete. A holding marker outranks a busy marker when
both are visible: "tab to queue message" is direct evidence of unsent composer
text, while "esc to interrupt" only says the agent is busy.

`recording_copy_error` is set when the recording could not be copied whole.
One cause is the capture budget: the teardown capture runs while the pair
registry holds its lock, so it is bounded by wall clock as well as by size. A
stalled filesystem shows up as an abandoned stage with a recorded reason (and a
`capture budget ... exhausted` warning naming the rounds it dropped) rather
than a teardown that never returns.

Captures accumulate; prune the directory manually when it gets large.

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

**Fallback:** If the Python wrapper fails, use `make validate-raw` for direct execution (no output capture).

**Tuning local PR validation parallelism:** `make validate-pr` is phased so
pytest suites that already use xdist do not also compete with each other at the
Makefile level. `VALIDATE_JOBS` still controls the static-check phase by
default. Use `VALIDATE_TEST_JOBS`, `VALIDATE_WEB_JOBS`,
`VALIDATE_AGENT_JOBS`, or `VALIDATE_E2E_JOBS` when debugging local contention;
the default `1` for these heavier phases is a deliberate stability/walltime
tradeoff.

**Environment variable:** The orchestrator sets `ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR` to direct output to the session directory. For direct runs, this is unset and output goes to the diagnostics fallback.

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
