# ADR-0010: Three Python layers; jobs never touch IO's interpreter

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

IO is Python. Jobs are also Python — IO runs agents through a Python wrapper in order
to capture console output. So there are three layers, not two:

1. **IO server** — our code, pinned deps, long-lived.
2. **Job wrapper** — our code, fixed deps, named on the submit file's `executable =`
   line. Spawns the agent and captures its output.
3. **The agent and whatever it drives** — arbitrary. A Python repo wants pytest and
   that repo's requirements; a Node repo wants Node.

Layers 1 and 2 are both controlled and pinned. Layer 3 is the unpredictable one.

The failure this prevents: layer 3 running `pip install` into IO's `site-packages`
and breaking the server that launched it.

## Decision

- IO gets its own virtualenv.
- The job wrapper runs from a controlled environment with its own pinned deps.
- **Layer 3 gets a fresh environment in the job's scratch directory, per job.** That
  is the isolation boundary — not the IO/job split, since our own wrapper is not the
  threat.
- Python is present in the image for layers 1 and 2. Layer 3 may still bring its own
  toolchain.

## Consequences

- A job cannot corrupt IO's runtime by installing packages.
- Per-job environment creation costs time on every job. If it becomes a bottleneck,
  cache wheels in a volume rather than sharing an interpreter.
- Non-Python repos are unaffected — they bring their own toolchain into scratch.
