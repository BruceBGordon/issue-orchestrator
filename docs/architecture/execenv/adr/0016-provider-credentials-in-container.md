# ADR-0016: Provider credentials reach container jobs as isolated, copy-per-job material

- **Status:** Accepted (spike executed live 2026-08-28/29; every claim below is
  evidence, not expectation)
- **Date:** 2026-08-29

## Context

Agent jobs in the execution environment (the #7112 trajectory) must
authenticate to Anthropic and OpenAI from inside the container. The host's
interactive credentials (`~/.claude` + macOS keychain, `~/.codex/auth.json`)
are the operator's live sessions: they must never be copied into a container,
and nothing a container does may disturb them. The credential flows that
create tokens are interactive (browser OAuth, device codes) and print or
handle secrets — so the orchestrator, and any AI session operating it, must
never run them; only the operator does, and secrets never transit an AI
context.

## Decision

One **credentials store** per machine, outside every repo, `0700` throughout
(spike location `~/.local/share/issue-orchestrator/credentials/`; the named-
volume packaging is equivalent — the store is bind-mounted or volume-mounted
into jobs, never baked into images).

**Anthropic (claude CLI): a long-lived setup token, read-only everywhere.**

- The operator runs `claude setup-token` in their own terminal and captures
  stdout to `claude/setup-token.txt` in the store. Packaging must extract the
  token line (`sk-ant-…`) — the command's stdout includes preamble (spike:
  27 lines, one token).
- Jobs mount `claude/` **read-only** and the job process itself exports
  `CLAUDE_CODE_OAUTH_TOKEN=$(cat …/token.clean)` — the value is read inside
  the container, never by the orchestrator or an AI session.
- The token is pure read-only material: no refresh, no write path, no
  clobber hazard. This asymmetry with codex (below) is why the two providers
  get different treatment.

**OpenAI (codex CLI): a device-auth master, copied per job.**

- The operator runs `codex login --device-auth` with `CODEX_HOME` pointed at
  `codex/` in the store. (Requires the ChatGPT account setting
  *Settings → Security → Device code authorization*; the plain browser
  `codex login` is an acceptable fallback — the isolation property comes from
  `CODEX_HOME`, not the flow.)
- **Jobs never mount the master.** Each job receives a fresh directory
  containing a *copy* of `auth.json`, mounted read-write as its `CODEX_HOME`;
  the copy is discarded with the job.
- Rationale (learned the hard way in the spike): `CODEX_HOME` is not a
  credential file, it is codex's whole working state — first touch writes
  migration markers, sqlite stores, caches — and a failed in-container use
  **deleted `auth.json` outright**. A master that jobs mount directly is a
  master that one bad job destroys. With the copy protocol the same churn
  landed harmlessly in the job dir and the master stayed byte-identical.
- The in-container codex version is **pinned** to the version that wrote the
  master (spike: `@openai/codex@0.150.1`); store migrations are one-way.
- Master refresh, when needed, is a managed operator step (re-run
  device-auth), not something jobs do.

**Never** copy the host's `~/.codex/auth.json` or `~/.claude*` anywhere: the
store's credentials are a separate token family, created by separate logins.

**Admission probe:** before agent jobs are admitted, a one-turn probe per
provider (the spike's probes, mechanized) must round-trip; a failing probe is
a typed human-fixable outage (#7096 vocabulary), not an agent failure.

## Evidence (spike, 2026-08-28/29)

| Claim | Result |
|---|---|
| claude one-turn in Linux container, auth solely via mounted token, non-root | ✅ `claude-in-container-ok` |
| root + `--dangerously-skip-permissions` refused | ✅ exact refusal message; the container's non-root user is load-bearing |
| `~/.claude.json` lives outside `CLAUDE_CONFIG_DIR` | ✅ 36 KB state file in `$HOME`; regenerable session state, not credential material |
| codex one-turn via job-local `CODEX_HOME` copy, pinned version | ✅ `codex-in-container-ok` (2,704 tokens) |
| master `auth.json` untouched by the job | ✅ byte-identical after the run |
| host sessions survive both new logins (token-family separation) | ✅ host `codex login status` logged in; host claude untouched |
| direct master mount is unsafe | ✅ demonstrated: first-touch churn + failed use deleted the master's `auth.json` |

## Consequences

- Agent jobs (#7112) unblock: both providers authenticate in-container with
  the operator's plan credentials and zero secret transit through
  orchestrator or AI contexts.
- The per-job copy step and the version pin become part of job admission;
  the probe becomes the provider-outage sentinel.
- Token lifecycle (expiry of the setup token, master re-auth cadence) is
  operator-owned; the admission probe converts silent expiry into a typed,
  visible outage instead of a mid-job stall.
