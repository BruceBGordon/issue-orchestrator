# IO execution environment — design record

**rev 2** — see [REVISIONS.md](REVISIONS.md) for the full arc, including decisions
that were challenged and reversed back.

> **Provenance:** design record authored 2026-08-27 alongside PR #7113 (the
> LaneExecutor port and opt-in condor backend). The implementation scaffolding
> it describes (Dockerfile, entrypoint, condor config, CI kill test) ships with
> the implementation issue, not this PR. The escape scenario in ADR-0001 was
> reproduced empirically on the macOS Rosetta pool on the same day: a
> setsid-detached double-forked grandchild survived condor_rm (see
> tests/integration/test_condor_lane_executor.py).


Design artefacts for running Issue-Orchestrator's opt-in HTCondor execution
environment in a container.

> **Trust scope:** this harness assumes you trust the repos it runs. It is not
> designed for untrusted multi-tenant execution. See ADR-0009 and ADR-0013.

## The one-line version

HTCondor's reliable process tracking and resource enforcement are Linux cgroup
features. They do not exist on macOS at any privilege level. So the execution
environment runs on Linux in a container, with IO alongside it, and
`condor_master` supervises both.

## Contents

As shipped by #7119 (the design-artifact paths this section previously
listed were placeholders and never existed in-repo):

```
docker/execenv/Dockerfile              the image (pins: base digest, condor LTS, uv digest)
docker/execenv/condor/00-io-execenv.config
docker/execenv/entrypoint.sh           fail-hard cgroup-v2 delegation + PID 1
docker/execenv/selftest.sh             in-container proofs (incl. the
                                       detached-grandchild containment case)
scripts/condor-execenv.sh              host driver: build|up|preflight|selftest|diagnose|down
.github/workflows/execenv.yml          path-scoped CI running the proofs natively
adr/                                   every decision and why
```

## Decisions

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](adr/0001-linux-vm-for-execution-environment.md) | Linux, not macOS — cgroups | Accepted |
| [0002](adr/0002-single-container-io-and-condor.md) | IO and condor in one container | Accepted |
| [0003](adr/0003-condor-master-supervises-io.md) | `condor_master` is PID 1 and supervises IO | Accepted |
| [0004](adr/0004-partitionable-slots-no-runtime-caps.md) | Partitionable slots, no resource caps | Accepted |
| [0005](adr/0005-work-trees-in-named-volume.md) | Work trees in a named volume | Accepted |
| [0006](adr/0006-single-image-with-capability-probe.md) | One image; IO probes for `condor_submit` | Accepted |
| [0007](adr/0007-version-pinning.md) | Pin by digest; HTCondor LTS | Accepted |
| [0008](adr/0008-docker-socket-for-containerized-test-deps.md) | Mount the Docker socket | Accepted |
| [0009](adr/0009-trust-model.md) | Trusted repos only | Accepted |
| [0010](adr/0010-python-layering.md) | Three Python layers, isolated | Accepted |
| [0011](adr/0011-pty-for-console-capture.md) | pty, not pipe, for console capture | Accepted |
| [0012](adr/0012-ci-on-github-actions.md) | CI exercises the kill path | Accepted |
| [0013](adr/0013-OPEN-privilege-boundary-is-not-real.md) | Job privilege boundary is not a security boundary | **Open** |
| [0014](adr/0014-OPEN-testcontainers-path-mismatch.md) | Bind-mount paths disagree with the host daemon | **Open** |
| [0015](adr/0015-host-vm-sizing.md) | Size the host VM deliberately — it is the real ceiling | Accepted |

Amended in rev 2: ADR-0004, ADR-0008, ADR-0014. Challenged and reaffirmed: ADR-0005.

## Before this builds — resolution status

The validation-lane increment (#7119) resolved the Dockerfile
placeholders; what remains open belongs to the io-in-container half:

1. `BASE_DIGEST` — **resolved**: ubuntu:24.04 pinned by digest in
   `docker/execenv/Dockerfile`.
2. `CONDOR_VERSION` — **resolved, amended by evidence**: pinned as the
   `CONDOR_PACKAGE_VERSION` build arg (htcondor.org 24.0 LTS point
   release). Two empirical amendments to the original design: the
   distro's own package (23.4) silently declines cgroup-v2 family
   tracking and cannot be used, and htcondor.org ships **amd64 only**,
   so the image is built and run `linux/amd64` (native on CI; emulated
   under Rosetta on Apple Silicon, matching the macOS pool's posture).
   There are no aarch64 packages to confirm.
3. `requirements.lock` — **superseded for this increment**: the
   container builds io's Linux venv from the repo's own `uv.lock`
   (`uv sync --frozen`); a separate lock set returns with the
   job-wrapper work if that layer materializes (ADR-0010).
4. `IO` / `IO_ARGS` in the condor config — **still open**: io does not
   run in-container yet; this entry-point reconciliation lands with the
   io-supervision half (ADR-0003) after ADR-0016.

## Running it

**What exists today (the #7115 validation-lane increment):** the image,
entrypoint, pool config, and in-container proofs live at
`docker/execenv/`, driven by `scripts/condor-execenv.sh
build|up|selftest|down`. It runs the lane contract suite (including the
Linux escape-*containment* branch of the boundary test) and proves the
hard `request_memory` ceiling inside cgroup v2. io itself does not run
in-container yet — that half gates on ADR-0016 (credential
provisioning), and the recipe below is its target shape:

```bash
docker volume create io-work
docker volume create io-spool

docker run -d --name io \
  -p 8080:8080 \
  --stop-timeout 60 \
  --add-host=host.docker.internal:host-gateway \
  -v io-work:/work \
  -v io-spool:/var/lib/condor \
  -v /var/run/docker.sock:/var/run/docker.sock \
  io:latest
```

No `--memory` or `--cpus` — condor allocates dynamically (ADR-0004).

`--add-host` is **required**, not optional: without it, testcontainers hands tests
a connection string that will not resolve from inside the container (ADR-0008
amendment).

**Set Docker Desktop's VM memory first.** That is the real ceiling and condor
treats it as the machine size. 24 GB is the starting point on a 64 GB Mac
(ADR-0015).

## What will break first

ADR-0014. The socket-mount path handling, on the first repo that bind-mounts a
fixture file into a Postgres container. Confirmed in rev 2 as a documented
testcontainers prerequisite rather than a hunch — but scoped to those repos only,
not to all testcontainers usage. `containerAccess: dind` is the likely per-repo
answer. Do not pre-build it; wait for a concrete failing repo.

The core shape — Linux VM, one container, condor supervising IO, partitionable
slots — is boring and well-trodden and should hold.
