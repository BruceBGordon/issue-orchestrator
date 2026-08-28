# Revision log

Cumulative. Each revision records what changed **and what was challenged and
held**, because a decision that survived an argument is better understood than
one that was never questioned.

Nothing here is final. Revisions are expected.

---

## rev 2 — 2026-08-27

Driven by two questions: how a client actually sets up a containerized test
dependency, and how the Docker Desktop memory setting relates to "no a-priori
limits".

### Added

- **ADR-0015 — host VM sizing.** The one genuinely a-priori limit in the design.
  VM size is pool size. Ceiling not reservation, but it ratchets. 24 GB starting
  point on a 64 GB machine.

### Amended

- **ADR-0004** — two corrections. The host VM *is* an a-priori cap (layer 1 of
  three; only layers 2 and 3 are dynamic). And sibling test containers consume
  the same VM memory pool invisibly to condor, so `RESERVED_MEMORY = 4096` is
  now set.
- **ADR-0008** — the socket mount alone was under-specified. Testcontainers
  returns `localhost:<port>` connection strings that do not resolve from inside
  our container. `TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal` plus
  `--add-host=host.docker.internal:host-gateway` are now required, not optional.
- **ADR-0014** — upgraded from suspicion to documented fact, then narrowed in
  scope. Path parity is a stated testcontainers prerequisite, not a maybe. But it
  breaks only repos that bind-mount fixture files, not all testcontainers usage.
  Per-repo `containerAccess: dind` is the front-runner. Still not pre-building it.

### Challenged and reaffirmed

- **ADR-0005 — work trees in a named volume.** Challenged on three grounds:
  IntelliJ cannot see a named volume; repos inside Docker's disk image break the
  existing Time Machine strategy; and same-path bind mounting would have closed
  ADR-0014 for free. Three problems, one change — compelling, and briefly
  adopted.

  **Reversed back.** The usage weighting was wrong. Small-file build churn happens
  on every job continuously; IDE browsing is occasional and has cheap workarounds
  (`docker cp`, throwaway container). Optimising the constant case is correct.

  Recorded in full rather than quietly reverted, because the next reader will have
  the same instinct — and because the reversal left two live consequences:
  ADR-0014 stays open, and uncommitted work has no host-side backup (mitigate by
  having IO push branches).

### Unchanged

ADR-0001, 0002, 0003, 0006, 0007, 0009, 0010, 0011, 0012, 0013. The core shape —
Linux VM, one container, `condor_master` supervising IO, partitionable slots — has
not moved and is not expected to.

---

## rev 1 — 2026-08-27

Initial capture. ADR-0001 through ADR-0014.

Origin: HTCondor installed on macOS as an unprivileged user program could not
reliably kill a job's process tree. Investigation established that the capability
is a Linux cgroup feature with no macOS equivalent at any privilege level, which
determined essentially everything else.

Two items opened rather than resolved: ADR-0013 (the job privilege boundary is not
a security boundary once the Docker socket is mounted) and ADR-0014 (bind-mount
path mismatch).

---

## rev 3 (2026-08-28, with #7119)

The validation-lane increment shipped and CI runs the proofs natively.
Amendments forced by evidence: ADR-0007's aarch64 confirmation resolved
negatively (htcondor.org is amd64-only; the distro's arm64 23.4 silently
declines cgroup-v2 family tracking) — the image is linux/amd64
everywhere; ADR-0012's release-tag arm64 leg is removed for the same
reason. The README contents inventory now lists the shipped paths.
Still open, unchanged: ADR-0013 and ADR-0014. ADR-0016 was recorded
and accepted in rev 4.

## Rev 4 — 2026-08-29

ADR-0016 recorded and **Accepted** on the strength of a live spike
(2026-08-28/29): claude authenticates in-container via a read-only
volume-mounted setup token (non-root, root refused
`--dangerously-skip-permissions`); codex authenticates via a device-auth
master whose jobs receive per-job copies — mandatory, because the spike
demonstrated that a directly-mounted master is destroyed by first-touch
working-state churn and failed use. Host sessions proven untouched by
both new token families. Agent jobs' credential prerequisite (#7112) is
closed; the admission probe becomes the provider-outage sentinel.
