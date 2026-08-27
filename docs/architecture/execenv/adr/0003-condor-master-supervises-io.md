# ADR-0003: `condor_master` supervises IO; no second supervisor

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

ADR-0002 puts two long-lived processes in one container. The reflex is to add
supervisord or s6. But `condor_master` is already a process supervisor, and it is
already PID 1's natural occupant here.

`condor_master` can supervise arbitrary non-HTCondor daemons: name them in
`DAEMON_LIST`, define the path, and leave them out of `DC_DAEMON_LIST` so the master
knows not to speak DaemonCore protocol to them. It starts them, restarts them on
unexpected exit, and shuts them down on `SIGTERM`.

## Decision

`condor_master -f` is the container entrypoint and PID 1. IO is registered as a
non-DaemonCore daemon in `DAEMON_LIST`.

## Consequences

- One supervisor, no extra dependency, no second config language.
- `docker stop` sends `SIGTERM` to `condor_master`, which drains and shuts down the
  pool cleanly rather than having jobs killed out from under the schedd.
- IO gets restarted if it crashes, for free.
- `condor_master` is not a full init and does not do general zombie reaping. If we
  ever see orphaned zombies accumulating, add `tini` as PID 1 with `condor_master`
  as its child. Not doing so pre-emptively.
- IO's stdout/stderr goes to a condor-managed log file rather than the container's
  stdout. `docker logs` will show master output, not IO output. If we want IO logs
  on container stdout, that needs explicit handling.
