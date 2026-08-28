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

## Which parameters belong to which mode

The concurrency controls are three separate layers; each parameter
belongs to exactly one, and its name says which:

| Parameter family | What it is | Consumed by |
|---|---|---|
| `.issue-orchestrator/lanes.yaml` rows | **Measured** per-lane demand (request_cpus, memory_mb), suspendability, exclusives — one schema-validated home, resolved by `lane-run` per work key | Scheduling backend admission only; the direct backend accepts-and-ignores |
| `LANE_WORKERS_*`, `*_PARALLEL` (Makefile) | How parallel the suite itself runs (xdist `-n`) — part of the command text | The suite, in every mode |
| `VALIDATE_*_JOBS` phase widths (Makefile) | Host protection: keeps xdist-heavy suites from trampling each other | Direct mode only — the flat fan + pool admission replaces this structure |
| `IO_POOL_CAPACITY_PERCENT` | The one throughput dial (below) | The pool |

The Makefile speaks only logical work names and commands; everything
scheduling-shaped is a `lanes.yaml` row, and a drift test holds the
two together bidirectionally (an undeclared lane cannot run, a
declared lane no target submits is a dead row).

Workers and requests are different numbers on purpose: most lanes are
I/O-bound (an integration slice keeps 4 workers busy on 0.85 cores),
so requesting worker-count cores rations capacity that is mostly
phantom. Measure with
`/usr/bin/time -l gmake <lane-target> LANE_EXECUTOR=direct` — busy
cores = (user+sys)/wall — and record the result in `lanes.yaml`.

## Pool capacity dial (opt-in)

One number scales the whole pool's admission capacity as a percentage
of physical cores:

```bash
IO_POOL_CAPACITY_PERCENT=150 scripts/condor-personal.sh up   # oversubscribe
IO_POOL_CAPACITY_PERCENT=60 scripts/condor-personal.sh up    # throttle
scripts/condor-personal.sh up                                # unset: physical cores
```

Raising it admits more concurrent lanes — sound when requests are
honest and the mix is I/O-bound; lowering it throttles everything
uniformly. This is the static half of load control; the reactive half
is the machine-load backoff below.

## Machine-load backoff (opt-in)

The pool can defer to the machine's real owner: when load that condor's
own jobs did not cause climbs, eligible running lanes are frozen
(SIGSTOP) and thawed when it clears. Off by default; enable at pool
start:

```bash
IO_CONDOR_LOAD_BACKOFF=1 scripts/condor-personal.sh up
# thresholds (owner load average): IO_CONDOR_SUSPEND_LOAD (default 5.0),
# IO_CONDOR_CONTINUE_LOAD (default 2.0)
```

Three rules are built in, each load-bearing:

- **Owner load, never total load.** The policy subtracts the load
  condor's own jobs cause; suspending on total load would trip on the
  gate's own lane fan and oscillate against its own reflection.
- **Only lanes that declared it safe.** Hermetic lanes declare
  `suspendable: true` in `.issue-orchestrator/lanes.yaml`; lanes
  holding live provider exchanges declare `suspendable: false`
  (frozen mid-turn, their response window expires and the thaw
  manufactures a provider-outage failure). The field is
  schema-required — a lane nobody classified fails validation
  loudly instead of defaulting either way.
- **Frozen time is charged to nothing.** The compiled lane deadline
  subtracts suspension time (a freeze must not manufacture a timeout),
  and observed runtime excludes it (a freeze must not teach the
  learning loop that a lane got slower).

## Learned dispatch order

Lanes carry no tuning knobs: the system orders its own queue. Every
successful run's observed execution time (queue wait excluded) is
recorded per work key under
`<git-common-dir>/issue-orchestrator/lane-runtime-history/`, and the
next submission's dispatch priority is the rolling median of the last
five — longest lanes first (the LPT makespan heuristic). Priority
decides which of the *simultaneously eligible queued* lanes matches
first; a large lane can still wait on slot shape (its cpu/memory
request needs a big enough hole) or on lanes that arrived while it was
not yet submitted. The properties to know:

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

Every completed lane also reports its dispatch facts — the priority it
ran with, how long it queued, how long it executed — as a
`[lane-dispatch]` line in the gate log and a row in
`<git-common-dir>/issue-orchestrator/lane-dispatch.jsonl`, so dispatch
quality is checkable without pool archaeology. Jobs additionally carry
their submitting worktree (`LaneSubmitter` in the queue), since the
pool is shared and concurrent gates from different worktrees are
normal.

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
