# ADR-0005: Work trees live in a named volume, not a bind-mounted macOS checkout

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Work trees could be bind-mounted from the Mac (`-v ~/work:/work`) so they are
editable in a macOS editor, or kept in a Docker named volume inside the VM.

The macOS-to-container filesystem bridge is slow for many small files. Git checkouts,
`node_modules`, and build trees are precisely many small files. A named volume lives
in the VM's own filesystem and is dramatically faster.

## Decision

IO clones into a named volume. Repos are not bind-mounted from macOS.

Condor's spool (`/var/lib/condor`) also gets a named volume.

## Consequences

- Build and test performance is materially better.
- Repos are **not** editable from a macOS editor. Accepted deliberately: IO is driving
  agents, not hosting hand-editing. Reversing this later means moving data, so it is a
  real commitment.
- The spool volume is not optional. Without it, `docker rm` silently discards the job
  queue — queued and running jobs vanish.
- Interacts badly with ADR-0008; see ADR-0014.

## Alternative not taken

HTCondor's own file transfer would stage work into each job's scratch directory,
giving isolated per-job sandboxes and automatic cleanup, and removing any need for
path identity between IO and jobs. Slower for large repos, but much cleaner semantics
— and arguably the better default for agent jobs that write unpredictably. Worth
revisiting if the shared work-tree model causes cross-job interference.

---

## Amendment — 2026-08-27 (rev 2): challenged, reaffirmed

This decision was challenged and then reinstated. Recorded because the reasoning
is now better than it was, and because a future reader will have the same
instinct to flip it.

### The challenge

Three arguments for bind-mounting `/Users/bruce/work` at the same absolute path
on both sides instead:

1. **IntelliJ.** The author browses code in IntelliJ on macOS. A named volume is
   invisible to it.
2. **Backup.** Repos in a named volume live inside Docker's VM disk image. Time
   Machine sees one opaque multi-gigabyte `Docker.raw`, backs up the whole thing
   on every change, and offers no file-level recovery. A bind mount keeps the
   existing backup strategy working untouched.
3. **Path parity.** Same-path bind mounting would satisfy the testcontainers
   prerequisite in ADR-0014 for free.

Three problems, one change. Compelling on its face.

### Why it was reinstated

The usage weighting is lopsided and the challenge mis-weighted it. Small-file
churn — checkouts, `node_modules`, build output — happens on **every build of
every job, continuously**. IDE browsing is **occasional** and has cheap
workarounds: `docker cp` a tree out, or mount the volume into a throwaway
container to look at it.

Optimising the constant case over the rare one is correct. The original decision
stands.

### What survives the reversal

- **Path parity does not come free.** A named volume lives at
  `/var/lib/docker/volumes/io-work/_data` inside the VM, not at `/work`, so a
  job's bind-mount request still will not resolve for the host daemon.
  ADR-0014 remains genuinely open.
- **Backup story is now explicit.** Repos are on GitHub; what a bind mount would
  have protected is *uncommitted* work, which agents produce a lot of. Accepted
  risk. Mitigate by having IO push branches rather than relying on host backup.
- The condor spool volume stays on the Linux side regardless. It holds transient
  queue state and does not belong in backups.
