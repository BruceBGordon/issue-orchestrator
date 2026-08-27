# HTCondor Validation Lanes (Opt-In)

Validation lanes (`typecheck`, `test-unit`, `test-simulated-core`,
`test-integration-core-local`) run through the `LaneExecutor` port. The
default backend executes them as direct subprocesses — exactly the
historical behavior, no scheduler involved. Opting in routes those lanes
through a personal [HTCondor](https://htcondor.org) pool instead, which
provides admission control, per-lane CPU accounting, wall-clock
deadlines that kill the whole process tree, and machine-wide
mutual-exclusion via concurrency limits.

Callers cannot tell the backends apart: identical exit codes (the lane's
own; `124` on deadline), streamed output, working directory, and
environment. That equivalence is enforced by a shared contract suite
(`tests/unit/lane_executor_contract.py`) that both adapters must pass.

## Opting in

```bash
# One-time (and after reboots): start the personal pool.
scripts/condor-personal.sh up

# Route wired lanes through the pool for one invocation:
LANE_EXECUTOR=condor make test-unit

# Or for a whole shell session:
export ISSUE_ORCHESTRATOR_LANE_EXECUTOR=condor
make validate-pr
```

There is **no silent fallback**: if the backend is opted in but the pool
is unreachable, lanes fail loudly with exit code 78 and a message
pointing here. `scripts/condor-personal.sh status` shows pool health.

## Scope: validation lanes only on macOS

The macOS personal pool tracks process **families**, not cgroups — a
double-forked, `setsid`-detached grandchild escapes removal (reproduced
live; see the executable boundary statement in
`tests/integration/test_condor_lane_executor.py` and
[ADR-0001](../architecture/execenv/adr/0001-linux-vm-for-execution-environment.md)).
Validation lanes are non-detaching and are safely contained; **agent
jobs are not in scope for the macOS pool** — they require the Linux
execution environment designed in
[docs/architecture/execenv/](../architecture/execenv/README.md).

## Platform notes

- **macOS**: upstream ships x86_64 binaries only; the helper downloads
  the tarball to `~/.local/share/issue-orchestrator/condor` and runs the
  daemons under Rosetta 2 (measured scheduling overhead ≈ 2s per lane).
- **Linux**: install the system package first
  (`sudo apt-get install htcondor`); the helper writes the lane tuning
  into `LOCAL_CONFIG_DIR` and restarts the service.

The helper applies low-latency tuning (`NEGOTIATOR_INTERVAL=1`, claim
reuse) plus two knobs the adapter depends on:

- `CONCURRENCY_LIMIT_DEFAULT = 1` — makes every named concurrency limit
  a machine-wide mutex, which is how exclusive lane resources (for
  example a provider account) are enforced.
- `PERIODIC_EXPR_INTERVAL = 5` — bounds how far past its deadline a lane
  can run before removal.

## Architecture

- `domain/lane_execution.py` — the typed contracts (the only vocabulary
  that crosses the port).
- `ports/lane_executor.py` — the port.
- `adapters/direct_lane_executor.py` — default backend.
- `adapters/condor/` — the anti-corruption layer: `submit_compiler.py`
  translates lane specs outbound into job descriptions;
  `event_classifier.py` translates job event logs inbound into typed
  lifecycle states. Scheduler vocabulary is forbidden outside this
  package by the `semgrep_condor_vocabulary` guardrail.
- `entrypoints/cli_tools/lane_run.py` — the composition root the
  Makefile invokes; selects the backend.

Interactive agent sessions (PTY) are out of scope for this backend —
see issue #7112. Multi-machine pools are a follow-on; everything here
assumes one machine and one shared filesystem.
