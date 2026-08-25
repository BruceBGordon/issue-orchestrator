# Host Executor

The host executor is a deep module for coordinating local commands across every
repository, worktree, and issue session owned by one OS user. It is processless:
each invocation coordinates through strict state records and advisory file
locks, so no orchestrator daemon has to be running.

## Public contract

Callers need only the domain vocabulary in `domain/executor.py` and the
`Executor` port:

```python
specification = ExecutorRunSpecification(
    work_key=ExecutorWorkKey("io:unit"),
    fairness_group=ExecutorFairnessGroup("io-validate-pr-1042"),
    concurrency_range=ExecutorConcurrencyRange(1, 24),
    exclusive_resources=(),
)
result = executor.run(
    specification,
    ExecutorCommand(
        ("pytest", "-n", "auto", "tests/unit"),
        ExecutorUnboundedDeadline(),
    ),
)
```

`ExecutorRunSpecification` is scheduling intent. `ExecutorCommand` is the exact
invocation. The split lets future adapters, including a repository mutation
queue, add admission policy without mixing process details into the declarative
contract. Required identities are never synthesized: missing work keys or
fairness groups fail at the boundary. Every command also makes its termination
contract explicit: `ExecutorUnboundedDeadline` means the application owns it;
`ExecutorBoundedDeadline` supplies an active budget and an independent absolute
safety bound.

## Boundaries

- `domain/executor.py` defines frozen, slot-backed public values.
- `ports/executor.py` defines the narrow behavior-level port.
- `control/executor_admission.py` contains pure demand learning, fairness, and
  grant selection.
- `execution/host_executor/` is the deep adapter. It hides Git identity,
  Pydantic persistence contracts, queue transactions, file descriptors,
  process supervision, resource measurement, and typed event collection.
- `entrypoints/bootstrap.py` remains the sole composition root. Executor CLI
  handlers import its builders lazily, so unrelated commands do not load the
  POSIX adapter and remain portable.
- `entrypoints/cli_executor_commands.py` translates CLI arguments into the
  public domain contract and invokes the port.

Callers must not read private executor state or coordinate its locks directly.

## Admission model

Each work key declares only the concurrency interval its command can use. After a
successful invocation, the adapter records child CPU seconds divided by wall
time and granted concurrency. The pure estimator maintains a bounded,
repository-scoped estimate of occupied cores per concurrency unit. Failed
commands remain diagnostic evidence but do not update the successful-demand
estimate.

At admission time the policy:

1. gives concurrently launched commands a bounded internal coalescing window so
   sibling queue records are visible together;
2. samples native cumulative CPU counters over that interval and pauses new
   admissions while the host is at or above the internal saturation threshold;
3. excludes requests whose named resource is already leased;
4. chooses the least-served live fairness group, then its oldest eligible
   request; if that request cannot fit, capacity drains rather than allowing a
   stream of newer work to starve it;
5. subtracts active internal CPU-slot leases from machine capacity;
6. reserves the learned charge of compatible queued requests' minimum
   concurrency before expanding the selected request toward its maximum;
7. chooses the largest accepted concurrency that fits the remainder; and
8. scales every learned charge by the machine aggressiveness percentage.

If even the declared minimum is wider than a small host's learned capacity,
the executor may admit exactly that minimum while charging the whole host. It
never makes a larger opportunistic grant fit by truncating its estimated
charge. Repositories that support narrower execution should therefore declare
the smallest genuinely useful minimum.

Internal CPU-slot capacity is the detected CPU count and default
aggressiveness is 100%. Operators use the percentage as the single machine
dial: lower it when unmanaged local work needs headroom, or raise it to exploit
I/O and agent think time. Repositories never declare an admission charge.

Adaptation has two horizons. Successful commands teach repository-and-work-key
CPU occupancy, which determines later concurrency grants. Independently, the
executor reads Mach per-processor counters on macOS or `/proc/stat` on Linux
over each admission interval. At 95% observed busy it stops admitting new work
until the host recovers; it records that exact observation and rationale. The
threshold is an internal safety mechanism, not another operator dial.

Load averages remain diagnostic evidence because they lag both starts and
finishes. They do not change a grant. Memory is observed per command but is not
an admission dimension today. The coalescing and polling intervals are also
internal mechanisms. Minimum reservation makes a declared range meaningful:
the minimum is useful service protected across a visible burst, while the
maximum is opportunistic expansion.

An opaque running subprocess is not resized or suspended. Its inherited lease
file descriptors are part of crash safety, so closing only the supervisor's
copy or sending an OS stop signal would create false capacity. Command
completion is therefore the current safe cooperative yield boundary. Native
pressure feedback attenuates additional admissions while existing commands
drain.

Issue-orchestrator uses that boundary directly. Code, validation-retry, rework,
review, and retrospective-review terminal sessions are complete application
phases; each is submitted independently through a typed
`AgentPhaseRunSpecification`. Finishing one phase releases its lease. A later
phase joins the fair queue as new work instead of inheriting a stale grant.
There is no signal-based pseudo-preemption and no scheduler-selected point
inside an agent process.

Agent phases use two monotonic watchdogs. Their active timeout starts only after
admission, so time deliberately yielded to the queue does not consume the
agent's existing work budget. A fixed absolute timeout of twice that budget
runs from submission and wins when necessary, preventing a saturated or broken
queue from extending a session forever. The terminal/session observer receives
the same absolute bound, so it cannot race the executor and kill a correctly
queued phase early.

## Crash and data behavior

The queue transaction and resource leases are separate ownership objects. A
queued record is removed on every exit from admission. Capacity, exclusive, and
lease-record file descriptors are inherited by the child, so a killed executor
parent cannot release resources while its command still runs. A later
invocation prunes a record only after its ownership lock is no longer held.
Capacity discovery/reconfiguration and admission share one cross-process guard,
so a migrated pool can adopt a new machine's CPU count only while every old
capacity lease is idle.

All persisted records are strict, versioned Pydantic contracts with unknown
fields rejected. Corrupt state fails loudly. Work-history filenames use an
internal hash of canonical Git common-directory identity plus the explicit work
key, while the history record and event store retain human-readable repository and work
names.

The bounded typed executor event store records enqueue facts, the coalescing
interval, wait-reason transitions, grants, minimum reservations, policy
changes, command observations, learned-demand changes, successful sample
counts, native CPU samples, decision reasons, admission/command deadline
expirations, and host load. `executor-events`
queries it through the read-only
`ExecutorMonitor` port; the CLI does not parse persistence or executor locks. A
live status projection and UI remain deferred to #7105.

## Deferred extensions

The deterministic virtual-time pressure DSL can model additional
application-declared safe boundaries inside resumable work. That is a pressure
experiment, not a production timer or scheduler-selected preemption point. A
future client could call such a boundary after persisting enough state to stop
and restart safely; implementing it would require a typed resume contract and
deadline accounting. The current production contract deliberately stops at
whole issue-orchestrator lifecycle phases.

A future typed batch-submission contract could provide atomic knowledge of a
complete command set and optimize startup order as well as minimum service. The
current bounded coalescing mechanism deliberately does less: it recognizes
ordinary concurrent bursts without adding a manifest, semantic lane priority,
or public timing option. Work keys remain opaque.

A future non-native merge queue can use a repository-scoped exclusive mutation
resource after validation reaches its publish boundary. The present separation
between specification, policy, and invocation permits that extension without
exposing executor internals or turning validation work keys into merge-policy
names. It is intentionally not implemented here.
