# ADR-0008: Mount the Docker socket for testcontainers-style dependencies

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Initially assumed jobs would not need Docker. That was wrong. Repos under test
routinely spin up containerized service dependencies — Postgres, Neon, Redis — via
testcontainers or equivalent. Any client repo may want one. This is the norm, not an
edge case.

Options:

1. **Socket mount (sibling containers).** Mount `/var/run/docker.sock`. Job-launched
   containers become siblings on the host daemon. Fast, no nesting, and the pattern
   testcontainers is designed for. Costs: socket access is root-equivalent on the host,
   and bind mounts requested by a job are resolved by the *host* daemon, not our
   container's filesystem.
2. **Docker-in-Docker (`--privileged`).** Real nested daemon, correct isolation
   semantics, paths behave. Costs `--privileged` (its own security surrender), slow
   startup, storage-driver friction.
3. **Rootless Podman inside.** Speaks the Docker API so testcontainers works; avoids
   both the socket and `--privileged`. More setup, own rough edges in nested rootless.

## Decision

Socket mount, exposed as a configuration knob rather than a hardcoded flag:

```
execEnv.containerAccess: socket | none | dind    # default: socket
```

## Consequences

- Testcontainers workflows work, which is the requirement.
- The knob costs about an hour now and means the multi-tenant question has a place to
  land later instead of forcing a refactor.
- **This punches through the containment boundary.** See ADR-0013 — it directly
  contradicts the unprivileged-job-user posture and must not be described as isolation.
- Bind-mount path resolution is unresolved. See ADR-0014.
- Valid because of the trust model in ADR-0009. It would not be valid otherwise.

---

## Amendment — 2026-08-27 (rev 2): the socket alone is not enough

The original decision under-specified this. Mounting the socket makes the daemon
*reachable*; it does not make the resulting containers *usable*.

When testcontainers starts Postgres as a sibling, it returns a connection string
pointing at `localhost:<mapped-port>`. Inside our container, `localhost` is our
container — not the host — so the connection fails.

Testcontainers' own documentation covers this: with Docker Desktop, set
`TESTCONTAINERS_HOST_OVERRIDE` to `host.docker.internal`, the DNS name Docker
Desktop provides for reaching the host from inside a container.

**Amendment — required, not optional:**

- Image sets `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal`.
- Run with `--add-host=host.docker.internal:host-gateway`.
- Ryuk, the testcontainers reaper, also needs socket access. Satisfied by the
  existing mount.

The same docs state a second prerequisite — the source directory must be mounted
at the *same path* inside the container. That one we do **not** satisfy. See
ADR-0014.

Sibling containers also consume host VM memory that condor cannot see. See the
ADR-0004 amendment.
