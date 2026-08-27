# ADR-0015: Size the host VM deliberately; it is the real ceiling

- **Status:** Accepted
- **Date:** 2026-08-27 (rev 2)

## Context

ADR-0004 established that we pass no resource flags and let condor allocate
dynamically. That is true of the container and of condor — but not of the layer
below both.

Docker Desktop on macOS runs a single Linux VM with a memory allocation set in
its preferences. Condor sees that VM as "the machine". **VM size is pool size.**

Two behaviours matter:

- It is a **ceiling, not a reservation**. The VM boots small and grows as
  containers demand.
- But it **ratchets**. Memory freed inside the VM is not readily returned to
  macOS, so over a long session usage climbs toward the configured limit and
  stays there. Only quitting Docker Desktop reliably reclaims it.

An always-on execution point is exactly the workload that will settle at the
ceiling and live there.

## Decision

Size the VM to **peak concurrent working set**, not to "what can I spare".

Starting point on a 64 GB machine: **24 GB**. Leaves macOS, Chrome, and the IDE
comfortable, and can be raised once real job memory profiles exist.

Work backwards from: max concurrent jobs x typical `request_memory`, plus IO
itself, plus headroom for sibling test containers (ADR-0004 amendment).

## Consequences

- This is the one genuinely a-priori limit in the design. Everything above it is
  dynamic.
- Under-sizing shows up as jobs queuing behind a small pool, not as failures —
  relatively benign and easy to diagnose.
- Over-sizing shows up as macOS memory pressure after hours of use, because of
  the ratchet. Less obvious. Prefer starting lower and raising.
- **Apple's `container` does not have this layer** — each container gets its own
  independently sized VM rather than sharing one fixed pool. A point in its
  favour if ADR-0001 is ever revisited. Note it has the mirror-image problem
  though: partial memory ballooning means freed pages are not returned to the
  host either, so long-running machines want periodic restarts.
