# ADR-0006: One image for now; IO capability-probes for HTCondor

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

The execution environment is opt-in, but baking condor into the default image makes
every user pay for it in download size (roughly 250 MB).

The clean answer is a multi-stage build with two targets — `io:latest` from a `base`
stage and `io:htcondor` from a stage layered on top — built from one Dockerfile in one
CI job, so they cannot drift.

But the execution environment is still being proven. Carrying two build targets while
the thing underneath is in flux costs more than the megabytes do.

## Decision

Ship a single image containing condor. Defer the split.

**Do now, because it is expensive to retrofit:** IO probes for `condor_submit` at
startup and registers the HTCondor execution environment only if present. Capability
detection, not a compile-time assumption.

## Consequences

- One tag, one support path, one thing to test while the design is still moving.
- The split stays cheap to add: a `FROM base AS htcondor` line and a second buildx
  invocation. Nothing here forecloses it.
- The probe means `io:latest` (post-split) will not advertise a broken execution
  environment in the UI. Same binary, gated at runtime.
- When we do split: anything shared must live in `base` and be unmodified by the
  condor stage, or layers diverge and users pulling both tags download twice.
