# ADR-0007: Pin versions explicitly; target the HTCondor LTS series

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

An unpinned build — `FROM ubuntu:24.04`, `apt-get install python3`,
`curl get.htcondor.org | bash` — means "whatever is current when the build runs". The
same Dockerfile produces a different image next month. When the execution environment
breaks, we cannot tell whether it was our code or a condor point release.

HTCondor ships two channels: an LTS series with a stable API surface, and a feature
series that moves faster and carries newer cgroup handling and better defaults.

## Decision

- Base image pinned **by digest**, not tag — tags move as patches land.
- HTCondor pinned to a specific version in the **LTS** series.
- Python dependencies in a lockfile with hashes.
- `apt` packages not individually pinned: the base digest fixes the archive state,
  and per-package pinning is high-effort for little marginal determinism.
- Versions we expect to sweep are `ARG`s, so testing against a new condor series is
  `--build-arg CONDOR_VERSION=...` rather than a Dockerfile edit.

## Consequences

- Reproducible builds. CI tells us when a version bump breaks IO before users find out.
- LTS is the right promise for an opt-in feature in an OSS project: a stable API
  surface for about a year. We give up newer cgroup improvements in the feature series.
- Digests must be refreshed deliberately, which is the point — a visible commit rather
  than a silent drift.
- ~~TODO before first build: resolve the actual base image digest and confirm the
  chosen HTCondor LTS point release has `aarch64` packages for the chosen distro.~~
  **Resolved by #7119 (rev 3):** the base image is digest-pinned and the LTS point
  release is the `CONDOR_PACKAGE_VERSION` build arg. The `aarch64` confirmation
  resolved NEGATIVELY: htcondor.org publishes amd64 debs only, so the image is
  built and run `linux/amd64` everywhere (native on CI, Rosetta-emulated on Apple
  Silicon — the same posture as the macOS pool's x86_64 tarball). An empirical
  addendum with teeth: the distro's own arm64 htcondor (23.4) silently declines
  cgroup-v2 family tracking, so "use the distro package for arm64" is not a
  fallback — containment is the point, and only the LTS delivers it.
