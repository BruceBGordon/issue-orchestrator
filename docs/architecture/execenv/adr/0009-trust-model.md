# ADR-0009: The harness assumes you trust the repos it runs

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Today there is exactly one user, running his own repos on his own Mac. The stated
ambition is for IO to become a harness others use.

It is tempting to build for the multi-tenant case now. That would mean rejecting the
socket mount (ADR-0008) and accepting a worse developer experience for a threat that
does not currently exist.

## Decision

Scope the harness explicitly: **it assumes you trust the repos it runs.** State this
in the README in one sentence.

Do not attempt untrusted multi-tenant execution in this design.

## Consequences

- Unblocks the socket mount and the shared work-tree model, both of which are the
  right call for a single trusted operator.
- A scope statement in the README pre-empts the awkward conversation when someone
  points a public issue tracker at this.
- **Untrusted execution is a different architecture, not a hardening pass on this one.**
  Container-per-job with socket access is a shared-fate model: any job can reach the
  host daemon and therefore every other job. The fix is a VM boundary per job.
- There is a real path there. Apple's `container` gives one lightweight VM per
  container natively on Apple silicon (ADR-0001). But it is not a flag flip — it would
  supersede ADR-0002, ADR-0005, and ADR-0008.
