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

## Pool-policy self-check (runs at the head of every gate)

`up` verifies the pool the moment it starts it. Nothing verified it
again afterwards — and a pool is long-lived: files get hand-edited,
packages get reinstalled, an experiment gets left behind. A pool that
has lost one of the three settings above keeps accepting work and keeps
reporting lanes as completed, so the damage (exclusives that no longer
exclude, deadline overruns that surface as "backend unresponsive",
lanes held on their own working directory) shows up as flaky lanes
rather than as a configuration problem.

`make validate-pr LANE_EXECUTOR=condor` therefore preflights the pool
**once per gate**, before the lane fan:

```bash
make lane-preflight LANE_EXECUTOR=condor   # the same check, by hand
```

```
[lane-preflight] condor: 5 required setting(s) hold — …/etc/condor_config
```

It asserts the three settings above plus the two opt-in policy files
(below), exits **78** naming every drifted knob at once (no
warn-and-continue: a drifted pool stops the gate before a single lane
is dispatched), and exits 70 if the pool cannot be read at all — a
pool that will not answer is never reported as healthy. Cost is six
`condor_config_val` reads, about one second on the Rosetta macOS pool,
which is why the gate can afford it unconditionally and why it runs
once rather than once per lane.

Direct mode runs lanes in your own environment and has no external
policy, so the same target reports an empty invariant set and exits 0.

### Policy intent, and why `up` records it

The two opt-in policy files are asserted **present if and only if they
were intended** — and intent is something the pool has to remember.
`IO_CONDOR_LOAD_BACKOFF` and `IO_POOL_CAPACITY_PERCENT` are read once,
at `up` time, by the installer process. Nothing else used to record
that they were set, which left a pool that deliberately opted out
indistinguishable from one whose policy file had been deleted by hand:
the check passed on both.

So `up` now writes `90-io-policy-intent.conf` alongside the policies
themselves:

```
IO_INTENT_LOAD_BACKOFF = True          # or False
IO_INTENT_CAPACITY_PERCENT = 150       # omitted entirely when unset
```

It rides the identical staging/install/reconcile path as the files it
describes and is read over the same `condor_config_val` channel, so it
cannot be installed out of step with them. `IO_INTENT_LOAD_BACKOFF` is
written in *both* states deliberately: its presence is what proves a
pool has an intent record at all.

The record is validated against exactly that schema, because the
config tool returns macro values **verbatim** — it canonicalizes
nothing, so `true`, `Bogus` and `007` all reach the check as written.
The sentinel must be literally `True` or `False`, and the dial must be
absent or a positive integer with no leading zeros. Anything else did
not come from `up`; it came from a hand-edit, and it is drift naming
the macro and its value. There is deliberately no case or leading-zero
tolerance: `007` is a value whose meaning depends on who parses it,
and the pool was never sized with it.

**A pool started before this existed reads as a legacy pool and fails
preflight**, naming `IO_INTENT_LOAD_BACKOFF` with an empty value. That
is intentional — on such a pool "opted out" and "removed by hand" are
the same observation, so it cannot be judged and must not be trusted.
Fix it by re-running `scripts/condor-personal.sh up` with the opt-ins
the pool should carry. That restarts the startd, so do it **between**
gates, never during one.

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

## Per-lane verdict cache (gate re-runs pay only for what failed)

Within one tree SHA, the publish gate records each lane's GREEN verdict
(`.issue-orchestrator/validation/lanes/<sha>/`, worktree-local). A gate
re-run at the same SHA skips lanes already proven green — a loud
`[lane-verdict] <lane> cached-green-at-<sha>` line says so — and runs
only the lanes without a verdict, so a transient failure (a provider
stall, a flaky lane) costs one lane's re-run instead of the whole fan.
Failures are never cached; any commit invalidates everything
(whole-tree keying, deliberately naive); a corrupt verdict file fails
the lane loudly rather than ever counting as green. The layer lives in
`TIMED_RUN` + the `lane-verdict` CLI and is enabled only by the condor
publish gate; every other make path is untouched.

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

## Learned slice weights

The fat integration suite is split into slice lanes, and the split is
learned the same way the dispatch order is. Every green slice run
records what each test file it ran *whole* cost — every phase, summed
per file — into
`<git-common-dir>/issue-orchestrator/file-durations/history.json`, and
the next gate's partition balances on the rolling median of the last
five. Nothing is mined by hand, so there is no regeneration step and no
constant to go stale in the source.

- **Coverage never depends on the store.** The partition is computed
  from the Makefile's live file wildcard: every live file lands in
  exactly one slice whatever the store holds, and a file it has never
  heard of gets a default weight. Staleness can cost speed; it cannot
  drop a test.
- **The first run is naive by design.** An empty store weighs every
  file the same, which is exactly an equal split — no worse than the
  unweighted behavior, and sharper every run after.
- **Only successes teach, and only whole files.** An aborted run (`-x`)
  would teach every file it never reached that it is free. A file too
  fat to balance is run at test-node granularity, split across all
  slices; those partial measurements are deliberately *not* recorded,
  since storing a third of a file as the file's weight would make it
  look thin and the run after would fatten it again.
- **One gate, one set of weights.** The slices of a gate are admitted
  minutes apart and each teaches the store as it finishes, so a live
  read would hand the last slice different numbers than the first — and
  two different partitions of one file list can leave a file unrun in a
  gate that still goes green. The Makefile stamps one
  `SLICE_WEIGHTS_EPOCH` per gate (the flat fan is one make process, and
  the scheduler wrapper carries the stamp into the job); the first
  slice to ask publishes `pinned-<epoch>.json` and every other slice of
  that gate is answered from it.
- **A snapshot is never recomputed.** The store records every epoch it
  has pinned, and that record is made durable *before* the snapshot
  itself becomes observable. Dying between the two writes can therefore
  only leave a remembered epoch with no snapshot — which fails loudly —
  never a snapshot no ledger knows about, which would be adopted, later
  pruned, and then silently recomputed. If a slice asks for an epoch whose snapshot has since
  been reclaimed — a lane's deadline excludes time it spent suspended,
  so a frozen slice can legitimately return days later — the store
  **refuses**, naming the epoch and the remedy, and the lane fails.
  Recomputing would be the silent version of the same problem: weights
  derived now differ from the ones this gate's earlier slices already
  partitioned on, so some tests would run twice and others not at all.
  A rare honest red is the correct outcome; wrong weights never are.
- **Pin retention is housekeeping, not a safety mechanism.** Pins older
  than seven days are deleted so the directory stays small. Nothing
  about correctness rests on that number — outliving it produces the
  loud failure above, never a wrong partition. A pin whose age cannot
  be established at all (unreadable, not JSON, or carrying something
  that is not a date) is never deleted, and says so at `WARNING`.
- **Capture is backend-neutral.** It is a pytest plugin
  (`infra/pytest_file_durations.py`) enabled by the slice recipe, so a
  scheduler backend — which re-invokes that same recipe inside its job
  — captures identical durations by construction. It is enabled there
  and nowhere else: an always-on plugin would also learn from a
  developer running one test out of a file.
- **Nothing to invalidate.** To reset the weights, delete the file; the
  next gate seeds them again. A corrupt store fails loudly (the message
  names the file) rather than silently reverting to a guess.

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
  `adapters/json_lane_runtime_history.py` — the dispatch-order learning
  loop (backend-neutral: every backend reports runtime, every backend's
  submissions may consume the ordering).
- `ports/file_duration_history.py` +
  `adapters/json_file_duration_history.py` — the slice-weight learning
  loop, with `infra/file_duration_store.py` resolving its one shared
  home, `infra/pytest_file_durations.py` capturing, and
  `scripts/lane_slices.py` consuming.
- `adapters/direct_lane_executor.py` — default backend.
- `adapters/condor/` — the anti-corruption layer: `submit_compiler.py`
  translates lane specs outbound into job descriptions;
  `event_classifier.py` translates job event logs inbound into typed
  lifecycle states. Scheduler vocabulary is forbidden outside this
  package by the `semgrep_condor_vocabulary` guardrail.
- `execution/lane_backends.py` — the backend registry: one entry per
  backend carrying BOTH factories (executor and policy check), with the
  selectable names derived from it. A backend cannot be runnable
  without also being checkable.
- `ports/lane_policy_check.py` + `adapters/condor/pool_policy.py` —
  the pool-policy self-check (above).
- `entrypoints/cli_tools/lane_run.py` — the per-lane entrypoint the
  Makefile invokes; `entrypoints/cli_tools/lane_preflight.py` — the
  once-per-gate policy preflight. Both resolve their backend through
  the registry.

Interactive agent sessions (PTY) are out of scope for this backend —
see issue #7112. Multi-machine pools are a follow-on; everything here
assumes one machine and one shared filesystem.
