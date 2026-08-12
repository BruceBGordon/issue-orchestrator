---
name: troubleshooting
description: General orchestrator diagnostics, hook problems, performance issues, and infrastructure debugging. Use for "orchestrator behaving unexpectedly", hook hangs, or lock/state issues. For failed issues, use session-debugging skill instead.
---

# Troubleshooting

General diagnostics for orchestrator infrastructure issues.

## When to Use

- Orchestrator behaving unexpectedly
- Hooks hanging or failing
- Terminal/subprocess performance issues
- Lock or state problems
- General "something's wrong" scenarios

**For investigating failed issues/sessions, use the `session-debugging` skill instead.**
**For startup/launch issues (doctor checks, hooks, launcher), use the `startup` skill instead.**

---

## Session Output Directory Structure

All session artifacts are centralized in a run directory per session:

```
<worktree>/.issue-orchestrator/sessions/
├── <run_id>__<session_name>/     # e.g., 20260120-143052Z__issue-42
│   ├── manifest.json             # Session metadata
│   ├── session-identity.json     # Stable issue/PR/role identity
│   ├── session-prompt.txt        # Rendered prompt sent to the agent
│   ├── terminal-recording.jsonl  # Terminal output (NDJSON with base64 PTY events)
│   ├── validation-*.{json,log}   # Validation artifacts
│   ├── orchestrator-tail.log     # Filtered orch log for this session
│   ├── review-exchange/          # Local-loop review exchange artifacts, when present
│   └── claude-session.jsonl      # Symlink to Claude log
├── <session_name>                # Symlink to latest run
├── latest.json                   # Pointer to most recent run
└── index.json                    # List of all runs
```

**Quick access:**
```bash
# Latest run for any session
cat $WORKTREE/.issue-orchestrator/sessions/latest.json | jq

# List all runs
cat $WORKTREE/.issue-orchestrator/sessions/index.json | jq '.runs'

# Find run for specific issue
ls -td $WORKTREE/.issue-orchestrator/sessions/*__issue-<N> | head -1
```

---

## Quick Diagnostics

```bash
# Check orchestrator status
issue-orchestrator status

# Check orchestrator log (filter out loop spam)
LOG=".issue-orchestrator/state/logs/orchestrator.log"
tail -f "$LOG" | grep -v "LOOP.*Iteration"

# See ONLY events
grep "\[EVENT\]" "$LOG" | tail -100

# See events + key transitions
grep -E "\[EVENT\]|\[STATE_MACHINE\]|Launched|Queued|review" "$LOG" | tail -100

# Check for errors
grep -i -E "error|exception|traceback" "$LOG" | tail -30

# List running subprocess sessions
ps aux | grep -E "claude|issue-orchestrator" | grep -v grep

# Check web API (if running with --web-ui)
curl -s http://localhost:8080/api/status | jq
```

### Dashboard Auth / Reconnect Loops

If the embedded dashboard loops through messages like `Event stream disconnected... reconnecting in 30s` or the older `Engine restarting... reconnecting in 30s` while `/api/info` or the dashboard HTML still loads, suspect the browser-session auth path before assuming the engine is down.

Expected browser auth contract:
- Dashboard and Control Center both load `static/js/browser_auth.js`.
- Authenticated dashboard HTML includes `<meta name="io-csrf-token" ...>`.
- HTML includes `<meta name="io-browser-auth-required" content="1|0">`; no-auth test/dev pages should use `0`.
- Mutating dashboard fetches include `X-CSRF-Token`.
- `/api/events` uses a fresh `/api/sse-token` value on every EventSource connect.
- Tests can exercise this without real operator tokens via `fake_browser_auth`, `auth_enabled_control_client`, `auth_enabled_dashboard_client`, and `logged_in_dashboard_client`.

Check the engine log for auth rejections:

```bash
rg "Auth rejected|/api/events|/api/resume|/api/pause" .issue-orchestrator/state/logs/orchestrator.log | tail -80
```

### Quick Session Inspection

```bash
WORKTREE="/path/to/worktree"

# Find the latest session run
RUN_DIR=$(ls -td $WORKTREE/.issue-orchestrator/sessions/*__* 2>/dev/null | head -1)

# Check manifest for session metadata
cat $RUN_DIR/manifest.json | jq '{session_name, started_at, ended_at, outcome}'

# Check terminal recording (NDJSON format — use orchestrator replay, not cat)
ls -lh $RUN_DIR/terminal-recording.jsonl

# Check orchestrator-filtered log for this session
cat $RUN_DIR/orchestrator-tail.log | tail -50
```

### Diagnostic Feedback Loop

If the trace or session artifacts do not quickly explain what happened, finish
the investigation by improving future diagnostics. Prefer adding focused
logging/events at the component that owns the decision, not extra ad hoc
parsing in the UI or CLI. Include the identifiers and artifact paths an
operator needs to answer the same question next time, such as issue/session,
status/reason, cached-vs-fresh decisions, head SHA, validation result, summary
or manifest path, and concise human-facing response text.

Keep diagnostic output one-line and bounded so `issue-orchestrator trace <N>`
remains readable. Add or update a focused test that proves the new log/event
contains the missing fact.

---

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Pre-push hook hangs | Recursive/corrupt hook chain or credential prompt | Check `pre-push`, `pre-push.project`, and `pre-push.orchestrator`; run `issue-orchestrator doctor`; use `GIT_TERMINAL_PROMPT=0` |
| Labels not applied | Wrong label_target (issue vs PR) | Check completion_processor logs |
| Sessions cycling/retry loop | `blocked-failed` label not added | Check `failed_this_cycle` mechanism |
| Orchestrator won't start | Lock file exists | Check `.issue-orchestrator/locks/` |
| GitHub API errors | Rate limiting | Check `gh api rate_limit` |
| Session artifacts missing | Run dir not created | Check manifest.json exists in run dir |
| Gradle daemons linger after worktree removal | Gradle daemons are host processes, even when their per-worktree registry is gone | Usually wait for Gradle idle timeout; inspect with `pgrep -fl GradleDaemon` before stopping matching processes |

---

## Event System Architecture

Events flow through pluggy hooks:
```
TraceEvent(name, data) --> PluggyEventSink --> on_trace_event hook
                                                    |
                            +-----------------------+-----------------------+
                            |                       |                       |
                     LifecycleIPCPlugin    LifecycleSSEPlugin    LifecycleLoggingPlugin
                            |                       |                       |
                            v                       v                       v
                       IPC socket              SSE stream              Python logger
```

### Expected Events

| Event | When | Key Data |
|-------|------|----------|
| `session.started` | Issue session launched | issue_number, session_id |
| `session.completed` | Session finished | issue_number, outcome, pr_url |
| `pr.created` | PR created | issue_number, pr_url |
| `review.queued` | PR queued for review | pr_number, issue_number |
| `review.started` | Review session launched | pr_number |

---

## Hook Debugging

### Pre-push Hook Hangs

If the pre-push hook hangs in worktrees:

```bash
# Run just the dirty-tree guard
python -m issue_orchestrator.entrypoints.cli_tools.prepush_check --dirty-only -v

# Check if it's a git prompt issue
GIT_TERMINAL_PROMPT=0 git push

# Check hook chain
cat .git/hooks/pre-push
ls -la .git/hooks/pre-push*

# Look for recursive wrappers or push calls
grep -r "setup-guardrails: pre-push\\|git push" .git/hooks/
```

**Common causes:**
- Hook calls `git push` which triggers the hook again
- Hook waits for user input (credential prompt)
- `pre-push.project` contains the managed wrapper instead of the preserved project hook
- Hook runs validation that hangs

**Fix:** Run `issue-orchestrator setup-guardrails` in the target repo. If a worktree has stale hooks, let the orchestrator recreate the worktree hooks or remove the worktree-specific hook files and relaunch the session.

### Stop Hook Issues

The stop hook checks for the completion marker (written by `coding-done`/`reviewer-done`):

```bash
# Check if marker exists
ls -la $WORKTREE/.agent-done-marker

# View marker content
cat $WORKTREE/.agent-done-marker
```

---

## Worktree Issues

### Stale Completion Files

If sessions complete immediately with old data:

```bash
# Check completion files in worktree
ls -la /path/to/worktree/.issue-orchestrator/completion*.json

# View content (check timestamp)
cat /path/to/worktree/.issue-orchestrator/completion*.json | jq '.timestamp'
```

**Fix:** `Worktree.prepare_for_session()` in `control/worktree.py` should delete stale files.

### Session Run Directory Issues

If session artifacts are missing or incomplete:

```bash
# Check if run directory was created
ls -la $WORKTREE/.issue-orchestrator/sessions/

# Check manifest for this run
RUN_DIR=$(ls -td $WORKTREE/.issue-orchestrator/sessions/*__* | head -1)
cat $RUN_DIR/manifest.json | jq

# Check if symlink is correct
ls -la $WORKTREE/.issue-orchestrator/sessions/<session_name>
```

---

## Performance Issues

### Too Many Concurrent Sessions

Too many subprocess sessions can exhaust system resources.

```bash
# Count running agent processes
ps aux | grep -E "claude|coding-done|reviewer-done" | grep -v grep | wc -l
```

**Prevention:** Configure `max_concurrent_sessions` in config to limit parallel sessions.

---

## Lock and State Issues

### Orchestrator Lock

If orchestrator won't start due to lock:

```bash
# Check lock files
ls -la .issue-orchestrator/locks/

# Check if process is actually running
ps aux | grep issue-orchestrator

# If not running, remove stale locks
rm .issue-orchestrator/locks/*.json
```

### State Corruption

If state seems wrong:

```bash
# Let doctor run SQLite quick checks and backup checks
issue-orchestrator doctor

# List local state databases
find .issue-orchestrator/state -maxdepth 1 \( -name "*.sqlite" -o -name "*.db" \) -print

# Check the subprocess session registry
sqlite3 .issue-orchestrator/state/session_registry.sqlite "PRAGMA quick_check;"
sqlite3 .issue-orchestrator/state/session_registry.sqlite \
  "SELECT session_name, issue_number, pid, started_at, is_review FROM sessions;"

# Check for orphaned subprocess sessions
ps aux | grep -E "claude|issue-orchestrator" | grep -v grep

# Check legacy registry files only when investigating migration issues
ls -la .issue-orchestrator/state/subprocess_sessions* 2>/dev/null
```

---

## Kill Stuck Processes

```bash
# Kill stuck orchestrators
pkill -f "issue-orchestrator.*start"

# Kill specific e2e worker
pkill -f "e2e_worker"
```

---

## Claude Session Logs

Claude Code session logs are useful for understanding agent behavior:

**Log Locations:**
```
~/.claude/
├── projects/<escaped-path>/     # Per-project session history
│   └── <session-id>.jsonl       # Conversation history
├── debug/<session-id>.txt       # Debug logs
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

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `.issue-orchestrator/state/logs/orchestrator.log` | Main orchestrator log |
| `.issue-orchestrator/state/session_registry.sqlite` | Current subprocess session registry |
| `.issue-orchestrator/state/subprocess_sessions.json` | Legacy subprocess registry, migration fallback only |
| `.issue-orchestrator/locks/` | Lock files (per instance) |
| `.issue-orchestrator/config/modes/*/*.yaml` | Launch configuration files |
| `.issue-orchestrator/sessions/latest.json` | Pointer to most recent session run |
| `.issue-orchestrator/sessions/index.json` | List of all session runs |
| `.issue-orchestrator/sessions/<run>/manifest.json` | Session metadata |
| `.issue-orchestrator/sessions/<run>/terminal-recording.jsonl` | Terminal output |
| `.issue-orchestrator/sessions/<run>/orchestrator-tail.log` | Filtered orch log |
| `execution/terminal_subprocess.py` | Subprocess-based terminal session management |
| `control/session_launcher.py` | Session launch/completion handling |
