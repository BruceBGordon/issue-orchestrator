# ADR-0002: IO and the HTCondor daemons run in one container

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Given ADR-0001, condor runs inside a Linux container. Where does IO run?

**Shape A — IO native on macOS, condor in the container.** IO reaches the schedd
either by installing the HTCondor client tarball on the Mac and pointing
`CONDOR_HOST` at a published port, or by shelling out to `docker exec`. Work trees
are bind-mounted so both sides see them, but at *different paths*: IO sees
`/Users/you/work/repo-42`, condor sees `/work/repo-42`. Every submit file, status
report, and log path needs translation.

**Shape B — both inside.** One filesystem, one UID namespace, one set of paths.

IO's UI is served over HTTP and viewed in Chrome. Chrome does not care whether the
server process is macOS-native or Linux-in-a-VM; it connects to `localhost`. So
there is no user-visible benefit to Shape A.

## Decision

Shape B. IO and the condor daemons share a single container.

Publish only IO's HTTP port. The condor daemons' ports stay internal — nothing
outside the container needs to reach them.

## Consequences

- No path translation layer. This is the main win; path mapping between host and
  container is where the bugs would have lived.
- `condor_submit` works directly from IO with no client install, no `docker exec`,
  no `CONDOR_HOST` configuration.
- Attack surface is one published port.
- IO is a Linux process. Fine today — it is a headless server. This decision would
  have to be revisited if IO ever grew a native Mac UI.
- Two processes in one container needs a supervisor. See ADR-0003.
