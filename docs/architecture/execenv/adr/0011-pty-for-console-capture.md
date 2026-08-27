# ADR-0011: Capture agent console through a pty, not a pipe

- **Status:** Accepted
- **Date:** 2026-08-27

## Context

The job wrapper (ADR-0010) exists to capture agent console output for display in IO's
web UI.

HTCondor jobs have no controlling terminal — stdout is redirected to a file. Most
agent CLIs detect this and change behaviour: progress rendering is disabled, output
formatting differs, interactive features may be switched off entirely. Capturing with
`subprocess.PIPE` inherits that problem, because a pipe is not a tty either.

## Decision

The wrapper allocates a pty (`pty.openpty()`) and runs the agent against it.

## Consequences

- The UI shows the output the agent actually intends to produce, including progress
  rendering.
- ANSI escape sequences arrive in the stream and must be handled — either rendered in
  the UI or stripped. This is a feature, not a defect, but it is work.
- Recorded here because six months from now the alternative looks like an inexplicable
  formatting bug rather than a decision.
