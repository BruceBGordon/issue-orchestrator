# Configuration

Launchable configuration lives in
`.issue-orchestrator/config/modes/<mode>/default.yaml` (or a named config such
as `main.yaml`). The Control Center selects a typed `(mode, config)` pair when
it starts a Repository Engine.

## Configuration modes

Each mode is a directory containing complete, coherent configuration files:

```text
.issue-orchestrator/config/
  modes/
    default/
      main.yaml
    codex/
      main.yaml
    claude/
      main.yaml
  maintenance/
    hooks-validate.yaml
```

Use modes to switch provider/model budgets or compare agent configurations.
The common case remains `default`; Control Center hides the mode selector when
no alternative exists. Mode and config controls are disabled while the
Repository Engine is Running or Paused. Stop the engine and drain any surviving
agent sessions before switching. A missing mode/config pair fails startup;
there is no cross-mode fallback.

From the CLI, global selection options precede the command:

```bash
issue-orchestrator --mode codex start
issue-orchestrator --config .issue-orchestrator/config/modes/codex/main.yaml start
```

Files under `maintenance/` are not launch modes. They support repository
maintenance such as exercising every hook adapter.

---

## TL;DR - Starter Config to Get Running

```yaml
agents:
  "agent:dev":
    prompt: ".issue-orchestrator/prompts/dev.md"
    model: "sonnet"

validation:
  quick:
    cmd: "make test"
    timeout_seconds: 300
  publish:
    cmd: "make validate-pr-raw"
    timeout_seconds: 1800
    dirty_check: tracked
```

Use the underlying raw publish command when your user-facing
`make validate-pr` target wraps the cache-aware `scripts/verify-pr.sh` path.
That keeps pre-push validation from re-entering itself.

Label an issue with `agent:dev` and start the orchestrator.

Use `validation.quick` for fast coding/review feedback and
`validation.publish` for the authoritative pre-push/pre-publish gate. The
publish dirty-tree policy lives at `validation.publish.dirty_check`.

### Control Center Repository Setup

For a discovered repository that needs configuration, select **Setup** in the
Control Center. The wizard previews the generated YAML and files before saving
them. Before preview, its GitHub stage explains the personal-identity and
GitHub App tradeoffs, waits while you complete any GitHub-side steps, and
requires **Verify**. Verification reads the selected identity and repository
without making GitHub writes; confirm the listed write permissions in GitHub.
Setup verifies again immediately before saving.

The default worktree base is the dedicated peer directory
`../worktrees/<repository-directory>`. Setup shows the resolved absolute path
and writes the selected value explicitly to YAML.

Setup defaults to a complete Claude Code pipeline:

1. The worker implements an issue in an isolated, sandboxed worktree.
2. The reviewer runs a bounded local review/rework loop after quick validation.
3. The tech lead reviews every approved PR by default and investigates
   unexplained failures.

Worker, reviewer, and tech-lead model and effort are editable independently.
The tech-lead cadence accepts `1` for every approved PR, a larger number for
batch review, or `0` for manual and failure-triggered review only. Reviewer and
tech-lead roles can be explicitly disabled.

Setup also requires quick and publish validation gates that exercise repository
behavior. It detects established Make, package-manager, and test-runner commands
when possible. If it cannot find both gates, the Configuration step waits for
you to enter the target repository's fast feedback and authoritative
pre-publish commands.

After saving, Setup names optional capabilities that were not enabled:
specialized agent routing, other AI providers, E2E, merge queue, Goal Pilot,
and tech-lead health automation. Those remain available through repository
Settings.

If the selected config already exists, the preview labels it as **Replace** and
requires an explicit acknowledgement before saving; settings not shown in the
generated preview are not preserved.

### Use Claude Opus With XHigh Effort

Set this on `default_agent` to apply it to every agent that does not override
the provider, model, or provider args:

```yaml
default_agent:
  provider: "claude-code"
  model: "opus"
  provider_args:
    effort: "xhigh"
    permission_mode: "bypassPermissions"
```

Or configure one agent directly:

```yaml
agents:
  "agent:backend":
    prompt: ".issue-orchestrator/prompts/backend.md"
    provider: "claude-code"
    model: "opus"
    provider_args:
      effort: "xhigh"
      permission_mode: "bypassPermissions"
```

---

## Environment Variable Substitution

Any string value in config can reference environment variables using `${VAR}` syntax:

```yaml
claims:
  claimant_id: "${ORCHESTRATOR_ID}"    # Expands to value of ORCHESTRATOR_ID env var

repo:
  github:
    token_env: "${GITHUB_TOKEN_VAR}"   # Works in any string field
```

If the referenced environment variable is not set, config loading fails with a clear error message showing which variable is missing and where it was referenced.

---

## Common Additions

### Limit Concurrency

```yaml
execution:
  concurrency:
    max_concurrent_sessions: 2
```

### Only Process Specific Issues

```yaml
filtering:
  label: "bot-ready"
  milestones: ["M1", "M2"]
  exclude_labels: ["test-data"]
```

### Milestone Sort Strategy

```yaml
milestones:
  sort: "milestone_number"   # default: extracts first integer from title (M1 < M2 < M10)
  # sort: "due_date"         # opt-in: sort by milestone due date; ties fall through to the remaining scheduler keys
  # sort: "pattern"          # opt-in: custom regex, requires sort_config.pattern
  # sort: "name"             # opt-in: alphabetic by milestone title
  # order: ["M0", "M1"]      # optional: explicit order for listed milestones (overrides sort)
  foundation: "M0"
```

The full sort key is `(milestone_key, priority_tier, sequence, issue.number)` — each layer only tie-breaks when the previous one ties.

`milestone_number` is the default because it works whether or not milestones have due dates. `due_date` only sorts meaningfully when every milestone has a `dueOn` set; otherwise due-less milestones tie on the milestone key and ordering falls through to priority tier (from `[Px-nnn]` in the title), then sequence, then issue number.

### Enable Code Review

```yaml
review:
  enabled: true
  default: "agent:reviewer"

agents:
  "agent:reviewer":
    prompt: ".issue-orchestrator/prompts/reviewer.md"
    model: "sonnet"
```

### Enable the Coder's Internal Review Loop

The optional internal loop asks every coder turn—including validation retries,
rework turns, and coder turns inside `via-local-loop`—to spawn one lightweight
reviewer and iterate with it before reporting success. This improves the change
presented to the independent review workflow; it does not replace that workflow
or change `review.exchange.mode`.

```yaml
review:
  internal:
    enabled: true
    max_rounds: 5
    instructions: ".io/internal-review.md"
```

`instructions` is a coder-facing Markdown file relative to the orchestrator's
configured repository root. Its trusted contents are read before launch claims
or worktree mutation and appended to each coder prompt inside a fixed contract requiring
approval from the same internally spawned reviewer. If the reviewer cannot be
spawned, approval cannot be reached, or the round limit is exhausted, the coder
must report blocked (or needs-human when a human decision is required), not
successful completion. Repository setup creates the canonical instructions
file when internal review is enabled and the configured file is missing.

### Declare Repo-Scoped GitHub Auth

```yaml
repo:
  name: "BruceBGordon/tixmeup"
  github:
    token_env: "TIXMEUP_GITHUB_TOKEN"
    keyring_service: "tixmeup-github"
    keyring_username: "${USER}"
```

Use `token_env` when the repo should read a specific environment variable.
Use `keyring_service` and `keyring_username` when the repo should read a
specific OS keyring entry. You can declare one or both.

When a repo declares `repo.github.token_env` or `repo.github.keyring_*`,
those sources become authoritative:
- `doctor` validates the configured source instead of a random global token
- Control Center start checks validate access to `repo.name`, not just `/user`
- startup fails clearly if the repo-scoped source is missing, instead of
  silently falling back to another token that may not have repo access

Control Center starts repository engines directly through the orchestrator
supervisor. It does not run target-repo wrapper scripts, so script-only token
exports are not available to Control Center-launched engines. Use `token_env`
only when the variable is already present in the Control Center process
environment; add `keyring_service` and `keyring_username` for a durable
per-repo Keychain fallback.

This PAT/keychain path makes issue-orchestrator act as the token owner. If your
repo requires PR approval and the same human needs to approve agent PRs, use
GitHub App auth instead so PRs are authored by a bot identity. See
[GitHub Auth and Permissions](github-permissions.md#protected-branch-mode-github-app).

### Ignore Repo-Local Runtime Artifacts

Use `.issue-orchestrator/runtime-ignore` when a tool writes repo-local runtime
files that should not block agent completion, pre-push dirty checks, or plain
agent `git status` output.

```text
# .issue-orchestrator/runtime-ignore
.tool/runtime.lock
cache/runtime/
*.tmp
```

Patterns are repo-relative. Blank lines and `#` comments are ignored. `!`
negations are not supported and are logged as a warning; the file is an
additive runtime-artifact list, not a full `.gitignore` replacement. Glob
patterns use lightweight matching: `*` may match path separators, so
`cache/*.json` also matches files in subdirectories of `cache/`.

The orchestrator always ignores its built-in runtime artifacts, including
`.issue-orchestrator/` session state and `.claude/scheduled_tasks.lock`. Add to
`runtime-ignore` only for additional files created by your repo's tools or agent
runtime. Do not list source files, generated artifacts that should be reviewed,
or anything the agent is expected to commit.

---

## What To Read Next

- Full reference (auto-generated): `docs/user/configuration_reference.md`
- Complete example config: `examples/config.example.yaml`

---

## Advanced Options (Teaser)

The guided Control Center baseline already includes worker, reviewer, and
tech-lead roles. Independent capabilities you may add later include:

- Specialized workers and role-specific reviewers
- Codex or custom agent providers
- E2E test runner with flake tracking
- GitHub merge queue integration
- Goal Pilot multi-issue coordination
- Tech-lead periodic health review, storm detection, and stuck-session recovery

The web Settings dialog (when `ui.mode: web`) is always available, and you can always edit the raw YAML config file directly.
Saving a field marked **Restart Required** updates the YAML immediately, but a
running Repository Engine keeps its startup-time value. Stop and start that
Repository Engine to apply the change; restarting the Control Center is not a
substitute.
If you want to revisit setup, you can rerun the setup wizard on an existing config at any time.

---

## Settings Dialog Reference

The web dashboard settings dialog is driven by `src/issue_orchestrator/infra/settings_schema.py`. The schema is the single source of truth for:
- Settings HTML form fields (rendered via Jinja2)
- GET/POST `/api/settings` serialization and validation
- Setup wizard defaults and labels
- Doctor checks (path validation, agent references)
- Documentation reference (auto-generated)

Goal Pilot uses the standard agent configuration: define its prompt under `agents` and reference the label via `goal_pilot.agent`.
