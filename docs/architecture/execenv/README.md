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

```
Dockerfile                      the image
docker/condor/00-io-execenv.config
docker/entrypoint.sh
ci/test-execenv.sh              the detached-grandchild kill test
.github/workflows/ci.yml
adr/                       every decision and why
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

## Before this builds

Deliberate placeholders, all in the Dockerfile (ADR-0007):

1. `BASE_DIGEST` — resolve with `docker buildx imagetools inspect ubuntu:24.04`.
2. `CONDOR_VERSION` — confirm it is a current LTS point release with `aarch64`
   packages for this distro.
3. `requirements.lock` and `job-wrapper/requirements.lock` — not included here;
   generate from IO's actual dependencies.
4. `IO` / `IO_ARGS` in the condor config assume an `io-server` entry point on
   `--port` and `--work-root`. Adjust to match reality.

## Running it

```bash
docker volume create io-work
docker volume create io-spool

docker run -d --name io \
  -p 8080:8080 \
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
