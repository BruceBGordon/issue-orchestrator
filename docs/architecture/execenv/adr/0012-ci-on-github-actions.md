# ADR-0012: CI builds the image and exercises the kill path

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

The version pinning in ADR-0007 is only useful if something checks the pins. And the
original motivating problem — HTCondor cannot reliably kill a job's process tree —
deserves a test that would actually catch a regression.

GitHub-hosted runners are ephemeral, firewalled, and time-capped. They are a fine
place to build and test the image; they are not a place to run a real execution point.

## Decision

GitHub Actions builds the image and runs an execution-environment suite inside it.

The central test: submit a job that **double-forks a detached grandchild**, `condor_rm`
it, and assert the grandchild is gone. That is what proves cgroup tracking is working,
and it is the exact case that fails under the old macOS user install.

- Poll `condor_status` until a slot appears, with a timeout. Never a fixed `sleep`.
- Dump `StarterLog` and `ProcLog` on failure — condor failures are opaque without them
  and there is no debugger on a CI run.
- Build `amd64` on every push. ~~Add the `arm64` leg only on release tags.~~
  **Amended by #7119 (rev 3):** there is no `arm64` leg to add — htcondor.org
  ships amd64 only, and the distro's arm64 package lacks working cgroup-v2
  family tracking (ADR-0007 amendment). The image is `linux/amd64` on every
  platform; Apple Silicon runs it emulated.

## Consequences

- A condor version bump that breaks process tracking fails CI instead of surprising a
  user.
- Feedback stays around two minutes rather than fifteen, because multi-arch is off the
  PR path.
- **Expect environment differences.** GitHub runners are themselves containerized, and
  cgroup delegation inside a container inside a runner does not always behave like a
  laptop. If the kill test passes locally and fails in CI, look there before assuming
  a code bug.
