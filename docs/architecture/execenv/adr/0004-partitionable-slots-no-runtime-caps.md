# ADR-0004: Partitionable slots; no a-priori resource caps on the container

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Concern raised: does containerizing mean fixing resources up front? We want HTCondor
to allocate dynamically, by its own judgement, per job.

Clarification that resolved it: a **Dockerfile cannot set resource limits at all**.
It is build-time only. Limits are runtime flags (`--memory`, `--cpus`) and are absent
unless passed. So the permissive default is already what we want.

The remaining question is how condor divides what it sees. Static slots partition the
machine in advance. **Partitionable slots** advertise one large slot that the negotiator
carves into dynamic slots sized to each job's `request_cpus` / `request_memory`, and
recombines on completion.

## Decision

Pass no resource flags at `docker run`. Configure one partitionable slot at 100% of
CPU, memory, and disk.

## Consequences

- Condor sees the whole machine and schedules against it dynamically. Exactly the
  behaviour asked for.
- On macOS the real ceiling is the *VM's* allocation, not the container's. Docker
  Desktop assigns a fixed slice of host RAM in its settings; condor sees that, not
  the host's 64 GB. Raising it is a Docker Desktop setting, not anything in our config.
- **Sharp edge:** if anyone ever does pass `--memory`, condor's resource detection
  reads the OS view and may not reflect an externally-imposed cgroup limit. The startd
  would advertise more memory than exists and overcommit, and jobs would be OOM-killed
  instead of held. If we add memory flags, we must set `MEMORY` in the condor config
  to match. CI should assert that advertised memory matches container reality.
- `CGROUP_MEMORY_LIMIT_POLICY = hard` means a job exceeding its own `request_memory`
  goes on hold with a clear message rather than being silently OOM-killed.

---

## Amendment — 2026-08-27 (rev 2)

Two things surfaced after this was first written.

### The host VM is an a-priori limit after all

There are three layers, and only the lower two are dynamic:

1. **Docker Desktop's VM** — a fixed slice of host RAM, set in preferences.
   This *is* an a-priori limit and it is the real ceiling.
2. **Our container** — no `--memory`, so it may use the whole VM.
3. **Condor** — carves that dynamically per job.

"No a-priori caps" was always about layers 2 and 3. Layer 1 is sized once by
the operator. See ADR-0015.

### Sibling containers eat condor's pool invisibly

ADR-0008 puts job-launched test containers (Postgres, Redis) on the *host*
daemon. On a Mac they run in the **same VM**, consuming from the same memory
pool — but condor has no idea they exist. It will advertise the full VM memory
and schedule against it while three Postgres containers are also resident.
Something then gets OOM-killed rather than held.

**Amendment:** reserve headroom explicitly.

```
RESERVED_MEMORY = 4096
```

This coupling only exists because of ADR-0008. Neither decision is wrong; the
interaction needed naming.
