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

## Calling `lane-run` from another repository

The pool is shared, so the dispatcher has to be callable from repos that
are not this one — including repos with no Python environment of their
own. Installing this package puts `lane-run` on `PATH`:

```bash
# From a clone of this repo (not on PyPI); pipx install works the same way.
uv tool install /path/to/issue-orchestrator

cd /path/to/your-repo
lane-run --work-key test-unit --timeout-seconds 900 -- npm test
```

Nothing about the invocation is issue-orchestrator-specific. `lane-run`
resolves `.issue-orchestrator/lanes.yaml` relative to its **working
directory**, so the calling repo declares its own lanes, and everything
after `--` is the calling repo's own command. A work key with no row in
that file fails with exit 78 rather than running unscheduled — there is
no policy by absence.

Depend on the exit codes, not the flags (the flag surface is
`Experimental`, see [Stability](stability.md)):

| Exit | What the dispatcher means by it |
|---|---|
| `124` | The lane exceeded `--timeout-seconds` |
| `78` | Configuration: undeclared work key, unusable command, backend opted in but unavailable |
| `70` | The dispatcher broke: a backend fault mid-run, or an unclassified crash |
| anything else | The lane's own exit code |

**These three codes are not reserved.** A lane's own exit code is
passed through unchanged, `70`, `78` and `124` included: a lane owns
the whole 0-255 space, so no code the dispatcher picks can be disjoint
from it, and `lane-run` will not lie about what your command returned.

**No in-band signal distinguishes a dispatcher fault from a lane
result** — not the exit code, not the journal, not the stderr prefix:

- Exit codes collide, as above.
- Journal rows are best-effort in both directions. A fault after the
  lane finishes and before its row is written leaves a completed lane
  with no row; a fault after the write exits `70` over a row that says
  `"exit_code": 0`.
- The `lane-run: …` stderr prefix is neither guaranteed nor
  unforgeable. A stderr that cannot be written emits nothing, and the
  lane inherits stderr, so `echo "lane-run: …" >&2` from inside a lane
  produces the prefix with no dispatcher fault anywhere.

So: **treat any non-zero exit as a failed lane.** Whether re-running is
safe is a property of your command, not of `lane-run`. Everything
`lane-run` writes is diagnostic — read it when it is there, never test
for its absence.

Do not re-derive a discriminator from these signals. Each of the three
above was documented on this page as one, and each was falsified.
Doing it properly needs an invocation-correlated lifecycle record with
an explicit indeterminate state, which `lane-run` does not have.

What the mapping being total does buy you is that an unclassified crash
in the dispatcher exits `70` rather than the `1` an uncaught Python
exception would otherwise produce — `1` being the commonest
test-failure code of all, and so the one collision that would mislead
every caller rather than a rare one.

Prefer the console script to `python -m` outside this repo: `-m` puts
the **caller's** working directory on `sys.path`, so a repo holding a
top-level module named after one of this package's dependencies breaks
the dispatcher — with exit 1, a lane result code. The console script
imports from the install, and is unaffected.

This repo's own callers — the Makefile and `docker/execenv/selftest.sh` —
deliberately keep using
`$(PYTHON) -m issue_orchestrator.entrypoints.cli_tools.lane_run`: naming
the interpreter pins the gate to its own virtualenv, where a bare
`lane-run` would resolve through `PATH` and could be another install —
including one produced by the `uv tool install` above. The gate's
working directory is this repo's root, which holds nothing that shadows
one of this package's imports, so the `sys.path` hazard does not apply
to it.
`tests/unit/test_console_script_entry_points.py` asserts both forms name
the same module, so the two cannot drift apart.

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
| `.issue-orchestrator/lanes.yaml` rows | **Measured** per-lane demand (request_cpus, memory_mb), three-valued suspendability (`never`/`anywhere`/`cooperative`), exclusives — one schema-validated home, resolved by `lane-run` per work key | Scheduling backend admission only; the direct backend accepts-and-ignores |
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
phantom.

The declared `request_cpus` is the **seed and the ceiling**, not the
truth — lanes measure themselves and the request follows (see
[Learned CPU requests](#learned-cpu-requests)). To seed a brand-new
lane by hand, measure with
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
- **Only lanes whose classification permits it.** Every lane
  declares one of three `suspendability` values in
  `.issue-orchestrator/lanes.yaml`: `anywhere` (hermetic — freeze and
  thaw safely at any point), `never` (live provider exchanges: frozen
  mid-turn, the response window expires and the thaw manufactures a
  provider-outage failure), or `cooperative` — a lane that CAN
  advertise safe interruption points (between test items, via the
  opt-in plugin `-p issue_orchestrator.entrypoints.pytest_cooperative_yield`
  and `condor_chirp`). **Cooperative lanes are currently never
  frozen**: the pool's policy deliberately holds their eligibility
  closed, because a live experiment (2026-08-29) proved runtime chirp
  updates reach the schedd's job ad but not the startd copy that
  evaluates SUSPEND — the advertisement machinery ships and is
  exercised end-to-end (acknowledged transitions, hard errors when an
  unsafe state cannot be restored), and #7139 tracks the
  startd-visible channel that will open eligibility. The fail-safe
  direction is built in at every layer regardless: no advertisement,
  failed transport, xdist workers (silent by design — they share one
  job ad), stale state, or a pre-migration job all mean never-frozen.
  The field is schema-required — a lane nobody classified fails
  validation loudly instead of defaulting.
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

## Learned CPU requests

Lanes measure their own CPU demand the same way they measure their own
duration. Each scheduled lane's exec shim reports the CPU its process
tree burned (the POSIX `times` built-in, written to `lane.rusage` in
the run directory and collected like the event log); busy cores =
CPU-seconds / observed runtime. Successes record it in
`lane-runtime-history/busy-cores/<work-key>.json`, and the next
submission requests `ceil(rolling median)`, floored at one core.

The measurement is taken in the shim rather than read from the
scheduler because the scheduler's own CPU attributes report a flat 0.0
on the macOS pool, which has no cgroups to account against. Doing it
in the shim makes the mechanism identical on every platform.

Measuring costs the shim its `exec` — a process that has replaced
itself cannot report anything afterwards — so the shim runs the lane
and re-raises its status. Two things make that invisible, and both are
load-bearing: the lane's stderr is routed around the shell so a
surviving shell's "Killed: 9" notice never lands in the lane's error
file, and the shim ignores the soft-kill signals so it outlives a
deadline removal exactly as an `exec`-ed lane did. Without the second,
the scheduler sees the job's primary process vanish on the soft kill
and never sends the hard kill that reaps a signal-resistant
descendant — the lane's tree survives its own deadline. The lane
itself keeps default signal dispositions.

The CPU dimension lives in a *sibling* file rather than beside the
runtimes in `<work-key>.json`, because the history is shared by every
worktree of the repository and a worktree checked out before this
feature still runs gates: its writer rewrites `<work-key>.json`
wholesale with runtimes only, which would erase a CPU key stored
inside it. Both files are still written under the one per-key lock, so
old and new writers serialize rather than race. To reset one lane's
CPU evidence, delete its file from the `busy-cores/` directory; the
runtime history is untouched.

If a completed lane produces no report, the gate log carries a
`[lane-cpu] WARNING <lane>: ...` line naming the lane and the missing
file — instrumentation never fails a lane that ran correctly, but it
must not fail silently either. The same fact is queryable afterwards:
a `lane-dispatch.jsonl` row with `"backend": "condor"` and a null
`observed_busy_cores` is a scheduled lane that measured nothing.

The declaration governs in both directions, asymmetrically:

- **Empty history submits the declared value**, unchanged. A lane
  nobody has measured behaves exactly as before.
- **Evidence may only lower the request.** A lane measuring under its
  declaration hands capacity back to the pool.
- **Evidence may never raise it.** A lane suddenly "measuring" sixteen
  cores is far likelier to be a broken measurement than a lane that
  got eight times hungrier, and granting that would drain the pool.
  The divergence is recorded (`cpu_request_capped` in the dispatch
  journal) so a *sustained* rise shows up as evidence for a human to
  act on by editing `lanes.yaml` — deliberately, not automatically.
- **Only the scheduling backend measures.** The direct backend runs
  lanes concurrently out of make's own job graph, which inflates wall
  time without changing CPU time and so under-measures busy cores
  systematically. Since evidence may only lower a request, feeding
  those numbers in would quietly shrink every request and
  oversubscribe the pool. Only a backend whose measuring conditions
  match the consumer of the number reports one.
- **Short lanes abstain.** The scheduler's event log carries
  whole-second timestamps, so a lane finishing inside a second has no
  denominator to divide by and teaches nothing.

Known limitation while only the downward path exists: a measurement
taken while the lane was starved of CPU looks exactly like a lane that
wants less, and lowers the request permanently — a lane that needed
eight cores but got four on a busy host learns "four", and four is
then all it can ever measure. Watch for it in
`lane-dispatch.jsonl`: a lane whose `request_cpus` has walked away
from its `declared_cpus` over successive runs while
`observed_busy_cores` tracks the request rather than sitting below it
is stuck, not tuned. Delete that lane's file from the history
directory to reset it to the declared seed.

A completed lane also reports its dispatch facts — the priority it ran
with, how long it queued, how long it executed, what it requested
against what was declared, and what it actually used — as a
`[lane-dispatch]` line in the gate log, then a row in
`<git-common-dir>/issue-orchestrator/lane-dispatch.jsonl`, so dispatch
quality is checkable without pool archaeology. Both are diagnostic and
neither is guaranteed: a failed journal write exits `70` with the line
already printed and no row behind it, so read these records when they
are present and never infer anything from a missing one (see
[exit codes](#calling-lane-run-from-another-repository)). Jobs additionally carry
their submitting worktree (`LaneSubmitter` in the queue), since the
pool is shared and concurrent gates from different worktrees are
normal.

## Forensics: what every record carries

A duration on its own cannot be read. Two overlapping gates produce
contention-inflated samples that look exactly like regressions, and the
covariate that separates them is gone the moment nobody had a terminal
open. So every row of both
`<git-common-dir>/issue-orchestrator/validate-timings.jsonl` and
`lane-dispatch.jsonl` carries a `machine_state` envelope:

```json
"machine_state": {
  "sampled_at": "2026-08-29T12:00:00+00:00",
  "loadavg_1m": 7.91, "loadavg_5m": 12.51, "loadavg_15m": 9.0,
  "cpu_idle_percent": 85.68,
  "cpu_idle_source": "host_statistics(HOST_CPU_LOAD_INFO) over 0.1s",
  "physical_cores": 18, "probe_error": null
}
```

- **CPU idle is not derivable from load average**, especially on macOS
  where load counts parked threads: a host reading 12.5 can be 85%
  idle. Both platforms expose cumulative CPU tick counters, so both are
  read the same way — two reads a window apart, idle share of the
  delta. Linux reads `/proc/stat`; darwin reads the kernel counters
  `top` itself prints, without paying for `top` (measured at ~1-1.5s of
  CPU per probe, rising with load). `cpu_idle_source` always names
  which probe answered, or why none did.
- **The window is a floor, not a fixed wait.** Darwin's aggregate
  refreshes on a cadence that coarsens under load, so the probe
  re-reads until the counters move (bounded at 2s). A "no measurement"
  answer exactly when the host is pegged would be the worst possible
  failure for this envelope.
- **The host is probed on a bounded cadence**, not once per record: one
  sampler per process holds its reading for a few seconds, and
  `sampled_at` makes the reuse visible.
- **A failed probe never fails the work.** The envelope keeps its shape
  with nulls and a `probe_error`; an observability probe that could turn
  a green lane red would manufacture the failures this exists to
  explain. This is the one deliberate exception to the repository's
  fail-fast stance, owned in `infra/machine_state.py`. It is a
  `BaseException` boundary, not an `Exception` one: a sampler raising
  `SystemExit` — or a signal handler raising one mid-sample — is
  contained and recorded, because the alternative is a probe replacing
  the gate's own exit code. Only teardown signals get out
  (`KeyboardInterrupt`, `GeneratorExit`, `CancelledError`, listed once
  in `infra/containment.py` and shared with the lane executor's
  cancellation path): the operator's Ctrl-C must win. Rendering the
  failure is itself contained, so an exception whose `__str__` or
  `__repr__` raises degrades to its type name instead of escaping.
- **Concurrency is derivable, not sampled.** A running-job count would
  cost a scheduler subprocess per record; instead, each dispatch row's
  end instant, runtime and queue wait let overlap be reconstructed from
  the journal itself.

The pool is also configured with `PER_JOB_HISTORY_DIR`
(`$(SPOOL)/per-job-history`), so the scheduler writes every finished
job's complete final ClassAd to `history.<cluster>.<proc>`. When a lane
does **not** end cleanly, its retained run directory collects that file
as `lane.classad` beside `lane.sub`, `lane.events`, `lane.out` and
`lane.err` — memory and CPU usage, slot, hold reason and every
timestamp travel with the diagnostics instead of staying in a rotating
global history. Collection runs on **every** path that retains the run
directory, cancellation included — a lane killed by Ctrl-C is exactly the
one whose final ClassAd a reader wants, and the removal is what makes it
appear. It is best-effort by construction: it runs while a lane is
already ending badly, so a pool without the knob costs the ClassAd and a
stderr line, never the lane's own result.

Two properties make that safe to do while a lane is being cancelled.
Both start at the first instruction of the wind-down and cover all of
it — job removal, stream draining, configuration lookup, the wait for
the file, and the copy:

- **One budget spans the whole wind-down** (a couple of seconds there
  rather than the usual ten), not a per-stage timeout. A pool whose
  tools have gone slow cannot spend an interrupted lane's allowance on
  the removal or the lookup and then start waiting: whatever a stage
  spends is gone, and a stage with nothing left is skipped rather than
  started — including the `stat` and the copy. Spending the budget costs
  the ClassAd, and says so.
- **A second Ctrl-C wins, from the first instruction.** Ordinary
  failures and `SystemExit` stay contained so a diagnostic cannot
  rewrite why the lane ended — and are *recorded*, since a containment
  that reports nothing is indistinguishable from a bug. A teardown
  signal arriving *during* cleanup means the operator is no longer
  willing to wait for it: it propagates, with the original ending
  chained as `__cause__` rather than discarded.

The primitives both boundaries share — which exceptions are teardown
signals, and how to render a contained one without trusting it — live in
`infra/containment.py`, so the sampler and the wind-down cannot drift
apart.

**That knob is a loaded gun, and the pool helper treats it as one.** A
*missing* per-job history directory is safe — condor logs `must point to
a valid directory; disabling per-job history output` and carries on. A
directory the scheduler cannot *write* is not: it EXCEPTs the schedd
(`error 13 (Permission denied) opening per-job history file`,
`classadHistory.cpp:262`), and the master then restarts a schedd that
immediately re-EXCEPTs on the same queued job, forever. So:

- `scripts/condor-personal.sh up` creates the directory mode **1777**
  (sticky, world-writable) rather than guessing which uid the daemons
  run as — the submitting user on the tarball pools, `condor` on a
  system install. It refuses to touch the path at all if something
  other than a plain directory is already there (a symlink would send
  the privileged `chmod` at an unrelated target), then *verifies the
  outcome* — a real non-symlink directory carrying sticky, other-write
  and other-execute — and writes the `PER_JOB_HISTORY_DIR` knob into a
  managed optional config (`93-io-per-job-history.conf`) **only** if
  that check passed, removing a previously written one otherwise.
- The worst case is therefore per-job accounting silently off, never a
  dead pool. `condor-personal.sh up` prints why when it turns off.
- The execenv image makes the same guarantee at build time, and the
  build fails if the directory is not world-writable.

## Seeing what the pool is doing: `executor-status`

```bash
issue-orchestrator executor-status              # summarize the last 400 records
issue-orchestrator executor-status --scan 20    # only the most recent 20
issue-orchestrator executor-status --backend condor   # report on a named backend
```

Read-only, from any directory in the repository. It answers one
question — *why is validation work running or waiting?* — by joining
three things that are otherwise checked separately:

```
Executor pool — backend condor (from repository validation command), captured …

POOL: online — 1 machine, 18 cpus, 2 in use; 1 running, 1 queued
  STATE      FOR  CPUS  PRI  LANE       SUBMITTED BY             EXCLUSIVE
  running  27.0s     2   71  test-unit  issue-orchestrator-wt-a  codexlogin
  queued   24.0s     2   72  test-web   issue-orchestrator-wt-b  codexlogin

LANES: /repo/.issue-orchestrator/lanes.yaml
  dispatch journal: …/lane-dispatch.jsonl (30 record(s) scanned), highest first
  LANE          CPUS  MEM MB  FREEZE  EXCLUSIVE  RUNS  LAST RUNTIME  …  BACKEND  WHEN
  test-unit        8    6144  ok      -             3         1m21s  …  condor   …
  execenv.memory-oom  1   128  never  -             0             —  …  —        never
```

The mental model, top to bottom:

- **The header** names the backend *and what established it*: an
  explicit `--backend`, `$ISSUE_ORCHESTRATOR_LANE_EXECUTOR`, or this
  repository's own gate command (which is where `LANE_EXECUTOR=condor`
  lives). If nothing establishes it, the header says
  `backend UNKNOWN` and the command exits `78` — it will not assume one,
  because a status tool confidently naming the wrong backend is worse
  than one that admits it cannot tell.
- **POOL** is *now*. `online` is a claim that the pool can run work:
  it has execute resources and the collector heard from each of them
  recently. Anything else reads `health UNKNOWN` with the reason —
  no execute resources, records the collector is still serving from
  cache after a daemon died, or liveness it could not establish. The
  numbers it did report are still shown, labelled as its claim.
  Every job row names the lane, the **worktree that submitted it** (the
  pool is machine-wide), how long it has been **in that state**, and any
  exclusive token it holds.
- **LANES** is every lane that exists, from the declarations, joined to
  what the journal says each one last cost. A declared lane that has
  never run still gets a row (`never`), and a lane in the journal that
  the declarations no longer describe is marked `undeclared` — it cannot
  run again until it is declared. `PRI` is the learned dispatch priority
  the **next** run will carry, so the table is printed in the order the
  next gate will dispatch. `BACKEND` is the backend each lane actually
  last ran on, so history that contradicts the header is visible.
- **IDLE** is how idle the host was when that runtime was measured, from
  the `machine_state` envelope above — a duration read without its
  contention is the ambiguity that envelope exists to end. Rows written
  before a column the record now requires (the envelope, or the cpu
  request beside it) cannot be read back — and a worktree on older code
  is still appending such rows to the shared journal — so they are
  skipped and the count is printed beside the scan total: the history
  is thinner than the window, and saying so is the point. The count is
  deliberately one number across every such schema epoch rather than
  one per epoch: those rows are gone either way, newer ones accrue, and
  the remedy does not differ by which column was missing.
- **FAULTS**, when present, means an input is broken rather than empty.

Nothing is ever silently omitted. A machine with no pool prints
`POOL: unavailable` and the reason, and still prints the lane table. A
repository with no journal prints the path records will appear at.
Absence exits `0`; a *broken* input (a corrupt journal row, an
untranslatable answer from the pool, a missing or malformed
`lanes.yaml`) is printed under `FAULTS` and exits `70`; a backend that
nothing establishes exits `78`.

The command never prints command lines, arguments, environments, or
output paths: the pool query does not even ask for those attributes, so
a prompt or a token cannot reach the terminal through it.
## Architecture

- `domain/lane_execution.py` — the typed contracts (the only vocabulary
  that crosses the port).
- `ports/lane_executor.py` — the execution port;
  `ports/lane_policy_check.py` — the backend's policy self-check;
  `ports/executor_pool.py` — the read-only pool-inspection port. All
  three speak the same backend-neutral vocabulary.
- `ports/lane_runtime_history.py` +
  `adapters/json_lane_runtime_history.py` — the dispatch-order learning
  loop (backend-neutral: every backend reports runtime, every backend's
  submissions may consume the ordering).
- `ports/file_duration_history.py` +
  `adapters/json_file_duration_history.py` — the slice-weight learning
  loop, with `infra/file_duration_store.py` resolving its one shared
  home, `infra/pytest_file_durations.py` capturing, and
  `scripts/lane_slices.py` consuming.
- `domain/lane_cpu_request.py` — the one home of the seed/ceiling
  policy: declared value when nothing is measured, learned evidence
  only ever downward.
- `observation/executor_status.py` — joins the pool, the dispatch
  journal, and the learning loop into one snapshot, and owns what
  happens when a source is absent or broken.
- `adapters/direct_lane_executor.py` — default backend, including the
  policy check whose honest answer is an empty invariant set and the
  inspector that states precisely why it has no pool.
- `ports/machine_state.py` + `infra/machine_state.py` — the forensics
  envelope every timing and dispatch record carries (backend-neutral,
  and the single owner of probe-failure semantics, in both directions:
  it writes the envelope and reads it back).
- `adapters/condor/` — the anti-corruption layer: `submit_compiler.py`
  translates lane specs outbound into job descriptions;
  `event_classifier.py` translates job event logs inbound into typed
  lifecycle states; `pool_policy.py` reads the pool's effective
  configuration into a typed policy report; `pool_inspector.py`
  translates queue and slot answers inbound into pool contracts;
  `lane_executor.py` holds `CondorTools`, which locates the scheduler's
  command-line tools and is the single boundary through which this
  package invokes them. Scheduler vocabulary is forbidden outside this
  package by the `semgrep_condor_vocabulary` guardrail.
- `execution/lane_backends.py` — the backend registry: one entry per
  backend carrying EVERY factory (executor, policy check, pool
  inspector), with the selectable names derived from it. A backend
  cannot be runnable without also being checkable and inspectable. It
  is the guardrail's only exemption outside `adapters/condor`.
- `ports/lane_policy_check.py` + `adapters/condor/pool_policy.py` —
  the pool-policy self-check (above).
- `entrypoints/cli_tools/lane_run.py` — the per-lane entrypoint the
  Makefile invokes (it also wires the lane runner's own persistence
  adapters); `lane_preflight.py` — the once-per-gate policy preflight;
  `executor_status.py` — renders the operator snapshot for
  `issue-orchestrator executor-status`. All three resolve their backend
  through the registry.

Interactive agent sessions (PTY) are out of scope for this backend —
see issue #7112. Multi-machine pools are a follow-on; everything here
assumes one machine and one shared filesystem.
