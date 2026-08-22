# Codespaces

This repo can run in GitHub Codespaces without changing the normal local
`main.yaml` workflow.

## Included setup

The repo includes:

- `.devcontainer/devcontainer.json`
- `.devcontainer/seed-agent-onboarding.sh`
- `.issue-orchestrator/config/modes/default/z-codespaces.yaml`

The `z-` prefix is intentional so the Codespaces config sorts after the normal
local configs in Control Center lists.

The Codespaces config pins stable repo-engine ports:

- dashboard: `8080`
- control API: `19081`

The Control Center still runs on `19080`.

## Container lifecycle

Setup is split across dev container lifecycle hooks on purpose, because a
prebuild bakes `onCreateCommand` and `updateContentCommand` but **not**
`postCreateCommand`:

| Hook | What runs | Baked by a prebuild? |
|------|-----------|----------------------|
| `onCreateCommand` | Codex CLI install | Yes |
| `updateContentCommand` | `make worktree-setup` (uv sync, npm ci, Playwright) | Yes, and re-run on create |
| `postCreateCommand` | `seed-agent-onboarding.sh` | No — runs per codespace |
| `postStartCommand` | nothing | No — would run on every resume |

Leaving dependency installation in `postCreateCommand` is the most common
reason enabling prebuilds appears to change nothing.

`updateContentCommand` is re-run when a codespace is created from a prebuild,
which is what closes the drift between a stale prebuild and current `HEAD`.
Both `uv sync --frozen` and `npm ci` converge from any starting state, so a
prebuild that is weeks old still produces a correct tree — it just does a
little more work. Staleness is bounded by lockfile churn, not code churn.

## Create the codespace

1. Open the repository on GitHub.
2. Use **Code** -> **Codespaces** -> **Create codespace on main**.
3. Wait for the lifecycle commands to finish.

## Set required credentials

Codespaces secrets are the supported path — a secret is available to every new
codespace automatically, and survives rebuilds. Set them at
**Settings -> Codespaces -> Secrets** for your account, or with
`gh secret set --user <NAME> --repos issue-orchestrator/issue-orchestrator`.

Your local desktop login state does **not** carry over. Provider CLIs
authenticate inside the Codespace.

### GitHub

- `ISSUE_ORCH_GITHUB_TOKEN` — required.

This short-circuits the whole token chain before any keyring is touched, which
is what makes the orchestrator work on a headless Linux box (ADR-0008).

### Claude

Set **one** of:

- `ANTHROPIC_API_KEY` — a Console key. Metered billing, but rotatable and
  revocable on demand, and unambiguously permitted for automation.
- `CLAUDE_CODE_OAUTH_TOKEN` — produced by running `claude setup-token` **on a
  machine with a browser** (once). It is a one-year token and is explicitly
  portable. This rides your subscription.

Anthropic documents this exact pattern for Codespaces, and notes that
`~/.claude` persists across stop/start but is cleared on rebuild — which is why
the credential belongs in a secret rather than in the container.

**Do not copy `~/.claude/.credentials.json` from your Mac.** Headless OAuth
refresh has been unreliable for a long time, Cloudflare blocks token refresh
from datacenter IPs, and macOS deletes that file after migrating credentials to
Keychain, so the copy is often empty anyway. A static token in a secret avoids
the refresh path entirely.

Two behaviours matter because the orchestrator drives `claude` as an
**interactive PTY session**, not `claude -p`:

- With `ANTHROPIC_API_KEY` set, interactive mode shows a one-time key-approval
  prompt. Non-interactive mode does not.
- The onboarding wizard can still block even with a valid token.
  `seed-agent-onboarding.sh` pre-seeds `hasCompletedOnboarding` in
  `~/.claude.json` to prevent that. Note that this file lives *outside*
  `~/.claude`, which is the most common container auth failure.

### Codex

> **`OPENAI_API_KEY` does not authenticate the Codex CLI.** The credential
> chain is `CODEX_API_KEY` (for `codex exec` only), then an ephemeral store,
> then `CODEX_ACCESS_TOKEN`, then a persisted `auth.json`. Setting
> `OPENAI_API_KEY` alone leaves you logged out.

Pick one:

- `CODEX_API_KEY`, referenced per invocation — the recommended path for
  automation. Prefer injecting it per command rather than as a job-wide
  environment variable.
- `printenv OPENAI_API_KEY | codex login --with-api-key` — reads your shell
  variable as a *source*; Codex is not consuming the variable itself.
- `codex login` — interactive; needs a browser.

`codex login --api-key <key>` is no longer supported and exits 1.

Verify with `codex login status`, which prints `Logged in using ChatGPT` and
exits 0, or `Not logged in` and exits 1.

Two cautions for unattended use. Codex refresh tokens are **single-use and
rotate**, and the CLI takes no cross-process lock, so a shared `auth.json`
across concurrent jobs or machines produces `your refresh token was already
used`. And ChatGPT-account auth is not appropriate for public repositories.

### Provider rate limits are per account

Claude and Codex limits are per seat, shared across every machine and session —
Claude's are shared with Claude chat as well. Running more codespaces does not
buy more agent throughput.

## Start the Control Center

From the integrated terminal:

```bash
source .venv/bin/activate
python -m issue_orchestrator.entrypoints.control_center --port 19080 --no-browser
```

Codespaces auto-forwards `19080` and opens the Control Center URL.

## Start the repo engine with the Codespaces config

In Control Center:

1. Select this repository.
2. Choose `z-codespaces.yaml`.
3. Click `Start engine`.

## Direct start without Control Center

```bash
source .venv/bin/activate
issue-orchestrator --config .issue-orchestrator/config/modes/default/z-codespaces.yaml start --port 8080
```

To scope a run to a single issue:

```bash
issue-orchestrator --config .issue-orchestrator/config/modes/default/z-codespaces.yaml \
  start --port 8080 --issue <NUMBER>
```

## Idle timeout and unattended runs

A codespace stops after an idle period — default 30 minutes, maximum 240. The
documentation says terminal output resets that timer; GitHub staff have said
only client interaction does, and the question has been open since 2022. Treat
a long unattended run as at risk, and set the timeout to the maximum when
starting one.

Stopping preserves the disk, so a stopped codespace can be restarted and the
run resumed. Because the orchestrator recovers state from GitHub labels, an
interrupted run is recoverable rather than lost — but work that was never
pushed lives only on that codespace's disk, so prefer stopping over deleting.

## Local development

Nothing changes for local Mac development. Keep using
`.issue-orchestrator/config/modes/default/main.yaml` and your existing local
startup flow.
