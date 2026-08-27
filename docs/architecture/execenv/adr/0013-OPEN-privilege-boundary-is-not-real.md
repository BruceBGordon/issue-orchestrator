# ADR-0013: OPEN — the job privilege boundary is not a security boundary

- **Status:** Open
- **Date:** 2026-08-27

## Context

Two decisions taken in the same session contradict each other:

- Condor runs as root in the container and executes jobs as an unprivileged user
  (`SLOT1_USER`, `STARTER_ALLOW_RUNAS_OWNER = FALSE`), so a job cannot touch IO's own
  files or config.
- ADR-0008 mounts the Docker socket so jobs can start containerized test dependencies.

**Socket access is root-equivalent on the host.** A job that can talk to the daemon can
start a privileged container mounting `/` and read or write anything — including IO's
files, the condor config, and the host filesystem. The unprivileged job user therefore
protects against *accidents*, not against a *malicious or compromised job*.

This is recorded rather than resolved because under ADR-0009's trust model it is an
acceptable posture. What is not acceptable is describing it as isolation.

## Open question

Do we keep both and document the limit honestly, or drop one?

Sub-questions:

- If the socket knob is set to `none`, does the unprivileged job user become a real
  boundary? (Probably yes for filesystem access — but the shared work-tree volume of
  ADR-0005 still gives cross-job reach.)
- Is there value in making `containerAccess: none` the documented posture for anyone
  running repos they do not fully control, even before real multi-tenancy?

## Current position

Keep both. Keep the unprivileged job user — it is free and it catches accidents. But:

- **Never describe the job user as a security boundary** in docs, code comments, or
  the UI.
- The README trust statement (ADR-0009) is what carries the actual guarantee, and the
  guarantee is "none against hostile code".

## Revisit when

Anyone other than the author runs a repo the author did not write.
