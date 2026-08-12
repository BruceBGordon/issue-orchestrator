---
name: startup
description: Understand the unified launcher, doctor checks, and startup sequence. Use when working on launcher.py, doctor checks, supervisor.py, run_orchestrator.py, startup_manager.py, or entry point startup code (cli.py, control_api.py, mcp_server.py).
---

# Startup & Launcher

Unified startup architecture for all entry points.

## When to Use

- Working on startup/launch code in any entry point
- Debugging doctor check failures
- Modifying pre-flight checks
- Understanding the supervisor process model
- Troubleshooting "orchestrator won't start" issues

**For general orchestrator runtime issues, use `troubleshooting` skill.**

---

## Unified Launcher Architecture

All entry points (CLI, Control Center, MCP) share the same pre-flight checks
via `infra/launcher.py`. Hook verification is part of the doctor checks, not
the runtime startup sequence.

```
CLI (cmd_start)         Control Center (/control/start)     MCP (start)
       |                           |                            |
       v                           v                            v
launch_preflight_only()    launch_subprocess()          launch_subprocess()
       |                           |                            |
       +----------+----------------+----------------------------+
                  |
                  v
         _run_preflight(config)  <-- shared doctor checks
                  |
                  v
         DoctorResult (ok / warning / error)
                  |
          +-------+-------+
          |               |
    CLI: in-process   CC/MCP: supervisor.start()
    build_orchestrator()    (subprocess)
```

### Key Functions

| Function | File | Purpose |
|----------|------|---------|
| `preflight()` | `infra/launcher.py` | Run doctor only, return readiness |
| `launch_preflight_only()` | `infra/launcher.py` | Alias for CLI (doctor only) |
| `launch_subprocess()` | `infra/launcher.py` | Doctor + supervisor.start() |
| `run_doctor()` | `infra/doctor/runner.py` | All diagnostic checks |
| `supervisor.start()` | `infra/supervisor.py` | Launch orchestrator subprocess |

### Control Center Auth Boundary

Control Center launches repository engines directly through `launch_subprocess()`
and `supervisor.start()`. It does not execute target-repo wrapper scripts. If a
target repo relies on a wrapper script to export a GitHub token from Keychain,
that export is bypassed for Control Center launches.

For repo-specific GitHub auth, inspect the selected repo config itself:

```yaml
repo:
  github:
    token_env: TIXMEUP_GITHUB_TOKEN
    keyring_service: tixmeup-github
    keyring_username: "${USER}"
```

`token_env` only works when the variable is already present in the Control
Center process environment. Add `keyring_service` and `keyring_username` when
Control Center should resolve the token from macOS Keychain without a manual env
export.

### LaunchResult

```python
@dataclass
class LaunchResult:
    doctor: DoctorResult       # All check results
    launched: bool             # Whether subprocess was started
    status: str                # "ok" | "doctor_error" | "doctor_warning" | "already_running" | "launch_error"
    error: str | None          # Error message if launch_error
    supervisor: dict | None    # Supervisor info (pid, port, instance_id or instances) when available
```

---

## Doctor Checks

The doctor runs a comprehensive set of checks. Hook verification uses
a 2-tier system: Installation and Verification.

### Check Categories

| Check | File | What It Tests |
|-------|------|---------------|
| GitHub Auth | `checks/github.py` | Configured token sources and repo access |
| AI Provider CLIs | `checks/ai.py` | Claude/agent CLIs installed |
| Config File | `checks/config.py` | Config loads, validates |
| Config Schema | `checks/config.py` | No unknown fields |
| Template Variables | `checks/config.py` | Valid template vars |
| Repository | `checks/config.py` | Repo set or auto-detected |
| Worktree Remediation | `checks/config.py` | Remediation settings |
| Milestones | `checks/milestones.py` | Configured milestone order exists |
| Hook Installation | `checks/hooks.py` | AI hook files present for configured agents |
| Hook Verification | `checks/hooks.py` | AI hooks/execpolicy actually block dangerous commands |
| AI Gate | `checks/hooks.py` | Periodic live agent hook gate when due |
| Repo Guardrails | `checks/hooks.py` | Repo-local pre-push guardrails from `setup-guardrails` |
| Worktree Hook Corruption | `checks/hooks.py` | Managed wrapper recursion/corruption detection |
| Workspace | `checks/workspace.py` | Working dir, agents |
| Settings Schema | `checks/schema.py` | Schema-driven path and agent reference checks |
| Guardrails | `checks/guardrails.py` | Safety checks pass |
| Code Review | `checks/review.py` | Review config valid |
| E2E Runner | `checks/e2e.py` | E2E config valid |
| Clock Sync | `checks/clock_sync.py` | Clock drift dangerous to claim coordination |

### Hook Verification

1. **Installation**: Are hook files present? (`is_installed()`)
2. **Verification**: Do hooks actually block dangerous commands? (`verify_hooks()`)
3. **AI Gate**: When due, spawn supported agent CLIs to prove the end-to-end hook gate works.

Verification only runs when installation succeeds. The AI gate respects `hooks.ai_gate.interval_days` and `hooks.ai_gate.dangerous_allow_failure`.

---

## Entry Point Flows

### CLI (`cmd_start` in `cli.py`)

```python
launch_result = launch_preflight_only(config, runner)
if launch_result.status == "doctor_error":
    # Print errors and exit
    return 1
# Continue with in-process orchestrator
orchestrator = build_orchestrator(config)
await orchestrator.startup()
await orchestrator.run_loop()
```

### Control Center (`control_start` in `control_api.py`)

```python
launch_result = launch_subprocess(repo_root, config, config_name)
if launch_result.status == "doctor_error":
    return JSONResponse({"error": "doctor_failed", ...}, status_code=422)
if launch_result.status == "already_running":
    return JSONResponse({"error": "already_running", ...}, status_code=409)
if not launch_result.launched:
    return JSONResponse({"error": "launch_failed", ...}, status_code=500)
return JSONResponse({"status": "started", **launch_result.supervisor})
```

### MCP (`start` in `mcp_server.py`)

```python
launch_result = launch_subprocess(repo_root, config, config_name)
return {"launch": launch_result.to_dict()}
```

---

## Runtime Startup (StartupManager)

After the orchestrator process starts (either in-process or via supervisor),
`StartupManager.run_startup()` handles runtime initialization:

1. Emit config event
2. Clean up stale in-progress labels
3. Clean up idle terminal sessions
4. Discover and restore running sessions
5. Check in-progress issues (orphaned labels, open PRs, partial work)
6. Recover pending code reviews
7. Recover pending tech lead reviews
8. Recover pending validation retries
9. Recover orphaned cleanups
10. Resume partial work
11. Audit and cache the queue

**Key file:** `control/startup_manager.py`

---

## Key Files

| File | Purpose |
|------|---------|
| `infra/launcher.py` | Unified launcher (preflight + subprocess launch) |
| `infra/doctor/runner.py` | Doctor check runner |
| `infra/doctor/types.py` | Check and DoctorResult dataclasses |
| `infra/doctor/checks/hooks.py` | Hook verification checks (2-tier) |
| `infra/doctor/checks/config.py` | Config and repo checks |
| `infra/supervisor.py` | Process supervisor (start/stop/status) |
| `control/startup_manager.py` | Runtime startup sequence |
| `entrypoints/cli.py` | CLI entry point (cmd_start) |
| `entrypoints/control_api.py` | Control Center API |
| `entrypoints/mcp_server.py` | MCP server entry point |
| `entrypoints/bootstrap.py` | Composition root (wires dependencies) |

---

## Troubleshooting Startup Failures

| Symptom | Check | Fix |
|---------|-------|-----|
| "Startup checks failed" | Doctor error | Run `issue-orchestrator doctor` |
| "Hooks not installed" | AI hook installation check | Run `issue-orchestrator setup-hooks` |
| "Repo guardrails not installed" | Repo Guardrails check | Run `issue-orchestrator setup-guardrails` |
| "Hook verification failed" | Hook Verification check | Run `issue-orchestrator verify` |
| "Config not found" | Config File check | Create `.issue-orchestrator/config/modes/default/default.yaml` |
| "Repository not configured" | Repository check | Set `repo.name` in config or run from git repo |
| "Already running" (409) | Lock file | Stop existing instance or use `force_restart` |
| "Config validation errors" | Config Validation check | Fix config per error messages |
| Orchestrator crashes on startup | StartupManager error | Check orchestrator log |
