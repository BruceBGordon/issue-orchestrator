# ADR-0001: The HTCondor execution environment runs on Linux, not macOS

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

Issue-Orchestrator (IO) gained an opt-in HTCondor execution environment. HTCondor
was initially installed on macOS as an unprivileged user program. In that mode it
is second-class: it cannot reliably kill a job's full process tree.

Investigating the options surfaced the real constraint. HTCondor's reliable process
tracking and resource enforcement — `BASE_CGROUP`, `CGROUP_MEMORY_LIMIT_POLICY`,
freezer-based atomic kill of a process family — are **Linux cgroup features**. They
do not exist on macOS at any privilege level.

Three options were considered:

1. **Reinstall as a privileged program on macOS.** The documented path creates a
   `condor` service account via `dscl` and registers a root `condor.plist`
   LaunchDaemon. This fixes the *privilege* half of the problem: daemons run as
   root, jobs run as a separate UID, and `condor_rm` can signal any job process
   regardless of owner. It does **not** fix the *tracking* half. With no cgroups,
   `condor_procd` falls back to PID-tree plus environment-marker scanning. A
   double-forked, `setsid`-detached child — precisely what agent jobs spawn, e.g.
   dev servers, file watchers, test runners — still escapes. There are also no
   memory limits.

2. **Docker-style container.** On macOS this is a Linux VM with extra steps, and
   that VM is exactly what we need. Real kernel, real cgroups.

3. **Apple's `container` / `container machine`** (macOS 26, stable 1.0.0 shipped
   2026-06-09). One lightweight VM per container. `container machine` boots
   `/sbin/init` as PID 1 and persists across stop/start, so it can host a
   supervised, long-lived execution point.

## Decision

Run the execution environment on Linux, obtained via a container runtime on the
developer's Mac. Install HTCondor as root inside that Linux.

Ship an **OCI image** as the distributable artifact. Docker is the near-term
runtime because it is mature and already installed. Apple's `container` is a
documented alternative recipe against the same image, not a separate build target.

macOS-native HTCondor is **not supported**.

## Consequences

- We get cgroup v2 tracking, hard memory limits, and atomic freeze-and-kill. This
  is the whole point.
- A wedged job that outruns condor still dies when the VM is torn down. Backstop we
  did not previously have.
- One artifact runs under Docker, Podman, Apple `container`, or a native Linux host.
  Contributors on Linux and CI runners use the same image.
- Users are not asked to run `sudo dscl` and install a root LaunchDaemon to try an
  opt-in feature. That is a meaningful adoption and trust cost avoided.
- Mac users need a container runtime installed. Acceptable — the target audience
  already has one.

### Notes on Apple `container`, for when we revisit

- Apple silicon only. Not every base image works as a machine image: `ubuntu:24.04`
  fails on `/sbin/init`; Apple ships a worked Dockerfile that adds systemd and masks
  units that make no sense in a light VM.
- A `container machine` live-mounts the macOS `$HOME` into the VM and gives the
  mirrored user passwordless sudo. Convenient for interactive dev, but it punches a
  hole in containment — jobs can write real dotfiles and SSH keys. If we adopt it
  for the execution point, disable or avoid the home mapping.
- The Virtualization framework only partially supports memory ballooning: pages
  freed inside the VM are not returned to the host. A long-running execution point
  will creep in Activity Monitor and want periodic restarts.
- Known packaging gap: the Homebrew formula has missed the `machine-apiserver`
  plugin, making `container system start` fail at machine-API verification. Prefer
  Apple's signed installer package.
