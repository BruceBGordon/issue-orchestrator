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
  (`sudo apt-get install htcondor`); the plain package boots only a
  master, so the helper also writes an explicit personal-role overlay
  (all five daemons on loopback) and restarts the service. An ambient
  config that is already a full pool keeps its own topology.

On every pool it manages, the helper applies low-latency tuning
(`NEGOTIATOR_INTERVAL=1`, claim reuse) plus three lane-compatibility
settings the adapter depends on:

- `CONCURRENCY_LIMIT_DEFAULT = 1` — makes every named concurrency limit
  a machine-wide mutex, which is how exclusive lane resources (for
  example a provider account) are enforced.
- `PERIODIC_EXPR_INTERVAL = 5` — bounds how far past its deadline a lane
  can run before removal.
- `MOUNT_UNDER_SCRATCH =` — disables HTCondor's per-job private `/tmp`
  so lanes can use working directories under the real `/tmp` (pytest
  temp dirs, notably); without it every such lane holds with "Cannot
  access initial working directory".

### Isolation tradeoff — read before pointing this at a repo

The personal pool intentionally has **no job isolation**: on Linux the
role overlay sets run-as-owner execution (`TRUST_UID_DOMAIN`,
`STARTER_ALLOW_RUNAS_OWNER`), and on macOS the tarball pool already
runs jobs as the invoking user. Lanes execute **as you, on your real
filesystem, with your environment** — that is what lets them read your
worktrees, and it is only acceptable under the trusted-repo scope
([execenv ADR-0009](../architecture/execenv/adr/0009-trust-model.md)).
The containerized execution environment
([ADR-0013](../architecture/execenv/adr/0013-OPEN-privilege-boundary-is-not-real.md))
is where a separate job user and any future isolation posture live;
this helper deliberately does not attempt one.

`scripts/condor-personal.sh up` verifies all of this at startup: after
the daemons report, it asserts the personal role's daemon list and then
runs a probe job in a fresh submitter-owned directory — readiness means
"a lane can actually execute", and a failure prints the effective
identity configuration and hold reason instead of leaving you nine
held lanes later.

## Learned dispatch order

Lanes carry no tuning knobs: the system orders its own queue. Every
successful run's observed execution time (queue wait excluded) is
recorded per work key under
`<git-common-dir>/issue-orchestrator/lane-runtime-history/`, and the
next submission's dispatch priority is the rolling median of the last
five — longest lanes first (the LPT makespan heuristic), so a long lane
can never be stranded behind short ones. The properties to know:

- **The first run is naive by design.** No history means no priority —
  identical to pre-learning behavior. One gate run seeds everything.
- **Only successes teach.** A failed run's duration is the failure's,
  not the lane's, so provider stalls never poison the ordering.
- **Nothing to invalidate.** The rolling window re-converges by itself
  when a lane's cost drifts or the hardware changes. To reset one lane
  anyway, delete its file from the history directory; delete the
  directory to reset everything.
- History is shared across all worktrees of a repository (it lives in
  the git common dir, like the validation timings).

## Architecture

- `domain/lane_execution.py` — the typed contracts (the only vocabulary
  that crosses the port).
- `ports/lane_executor.py` — the port.
- `ports/lane_runtime_history.py` +
  `adapters/json_lane_runtime_history.py` — the learning loop
  (backend-neutral: every backend reports runtime, every backend's
  submissions may consume the ordering).
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
