# ADR-0014: OPEN — bind-mount paths do not agree between job and host daemon

- **Status:** Open
- **Date:** 2026-08-27
- **Revised:** 2026-08-27 (rev 2) — scope narrowed, likely answer identified

## Context

ADR-0008 mounts the host Docker socket, so job-launched containers are
**siblings** created by the *host* daemon. ADR-0005 puts work trees in a named
volume inside our container.

When a repo's test does something like:

```java
.withFileSystemBind("./fixtures/seed.sql", "/docker-entrypoint-initdb.d/seed.sql")
```

the path is resolved by the **host daemon**, which has a different filesystem
view than our container. It either does not exist or resolves elsewhere.

## What changed in rev 2

**This is not a suspicion — it is a documented prerequisite.** Testcontainers'
own documentation states that the source directory must be volume-mounted at the
*same path* inside the container that Testcontainers runs in, so that it can set
up correct volume mounts for the containers it spawns.

We do not satisfy that, and under ADR-0005 (reaffirmed) we cannot: a named volume
lives at `/var/lib/docker/volumes/io-work/_data` in the VM, not at `/work`.

Same-path bind mounting *would* satisfy it, and was considered and rejected in the
ADR-0005 amendment — small-file build performance outweighs it for this workload.

**But the scope is narrower than first feared.** This is per-repo, not
architectural:

- Repos that pull an image and expose a port — the common case — work fine.
- Only repos that bind-mount fixture files into a test container break.

## Options

1. **Same-path bind mount.** Rejected — see ADR-0005 amendment.
2. **Per-repo `containerAccess: dind`.** ADR-0008 already defines the knob. Inside
   a nested daemon, paths resolve correctly. Costs `--privileged` and startup
   time, but scoped to the few repos that need it rather than shaping the design.
   **Current front-runner.**
3. **Copy instead of mount.** Wrapper intercepts bind-mount requests and stages
   files via `docker cp`. Invasive; needs per-framework support.
4. **Testcontainers' own host-path configuration.** Investigate what the library
   offers for sibling setups before building anything.

## Current position

Ship with the socket mount. Expect this to be the first thing that breaks in real
use. Option 2 is the likely answer, but **do not pre-build it** — wait for a
concrete failing repo, because the right fix depends on what the failure looks
like.

## Revisit when

The first repo fails with a bind-mount-related error from a test dependency.
