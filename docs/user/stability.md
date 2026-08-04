# Stability & API Surface

Issue-Orchestrator is an early-beta `0.x` project. This page is the single
source of truth for **what you can depend on and what may change under you**:
every public-facing surface, its stability tier, and the release mechanics that
back those promises.

Per-surface *usage* documentation lives with each surface (linked below). This
page owns the **inventory and the policy**.

## The short version

While the version starts with `0.`, the public API is **not stable**. Any
surface on this page may change in a minor release, except the versioned
Web/SSE contracts. Every `0.x` release is published as a GitHub *pre-release*
so that instability is visible from the releases page, not just from prose.

## Stability tiers

| Tier | What it means during `0.x` |
|---|---|
| **Versioned** | Changes are explicit and detectable: the payload carries a schema version, generated schema artifacts are checked in, and a drift test fails when the shape changes without regeneration. Breaking changes bump the version. |
| **Supported** | Intended for external use and documented. May change between minor versions; changes are called out in release notes. |
| **Experimental** | Usable, but names, arguments, and return shapes may change or be removed in any release without notice. Do not build automation you cannot re-point. |
| **Internal** | Not for third-party use. No compatibility promise of any kind, including within a patch release. Reachable does not mean supported. |
| **First-party coupled** | Ships from this repo and is expected to move in lockstep with the Python package. Version skew is not supported. |

## Surface inventory

| Surface | Where | Public? | Tier during `0.x` |
|---|---|---|---|
| Config YAML schema | [`infra/settings_schema.py`](../../src/issue_orchestrator/infra/settings_schema.py), [Configuration Reference](configuration_reference.md) | Yes | Supported |
| CLI (`issue-orchestrator …`) | [`entrypoints/cli_parser.py`](../../src/issue_orchestrator/entrypoints/cli_parser.py) | Yes | Supported |
| Agent completion contracts (`coding-done`, `reviewer-done`) | [`entrypoints/cli_tools/`](../../src/issue_orchestrator/entrypoints/cli_tools/) | Yes (agent-facing) | Supported |
| MCP server tools (`orchestrator.*`) | [`entrypoints/mcp_server.py`](../../src/issue_orchestrator/entrypoints/mcp_server.py) | Yes | **Experimental** |
| Web / SSE public contracts | [`contracts/public/`](../../contracts/public/), [`contracts/public.py`](../../src/issue_orchestrator/contracts/public.py) | Yes | **Versioned** |
| UI OpenAPI contract | [`docs/api/ui-openapi.json`](../api/ui-openapi.json) | Yes | Versioned |
| Control API (HTTP, default `:19080`) | [`entrypoints/control_api.py`](../../src/issue_orchestrator/entrypoints/control_api.py) | No | Internal |
| Python package (`import issue_orchestrator`) | [`src/issue_orchestrator/`](../../src/issue_orchestrator/) | No | Internal |
| Plugin entry points | [`infra/hooks/hookspec.py`](../../src/issue_orchestrator/infra/hooks/hookspec.py), [`infra/ai_keys.py`](../../src/issue_orchestrator/infra/ai_keys.py) | Yes | Experimental |
| VS Code extension ↔ package | [`packages/vscode`](../../packages/vscode), [VS Code Integration](vscode.md) | First-party | First-party coupled |

### Config YAML schema — supported

`.issue-orchestrator/config/<name>.yaml` is the primary way you configure the
orchestrator, and it is a supported surface. The schema is generated from
[`infra/settings_schema.py`](../../src/issue_orchestrator/infra/settings_schema.py)
into the [Configuration Reference](configuration_reference.md), and a drift test
(`tests/unit/test_settings_schema.py`) keeps the two in sync.

During `0.x`, keys may be added, renamed, or moved between minor versions.
Unknown keys are rejected rather than ignored, so a renamed key fails loudly at
startup instead of silently doing nothing. Run `issue-orchestrator doctor` after
upgrading.

### CLI — supported

`issue-orchestrator --help` is authoritative for flags; the top-level command
set is:

**Runtime:** `start` · `status` · `pause` · `resume` · `refresh` · `restart` ·
`attach` · `switch` · `dashboard` · `output` · `tech_lead` · `health-review`

**Setup:** `setup` · `init` · `setup-hooks` · `setup-guardrails` · `verify`

**Credentials:** `auth` · `keys`

**Diagnostics:** `doctor` · `audit` · `trace` · `demo`

**Internal / development only:** `test-reset` · `e2e-reset` — these operate on
test and E2E state and carry no compatibility promise.

Command names are supported; flags may change between minor versions. Prefer
`--config` and `--set path=value` over positional coupling in scripts.

The additional console scripts installed by the package:

| Script | Audience | Tier |
|---|---|---|
| `issue-orchestrator` | Operators | Supported |
| `issue-orchestrator-mcp` | MCP clients (VS Code) | Experimental |
| `coding-done` | Coding/rework agents | Supported |
| `reviewer-done` | Review agents | Supported |
| `exchange-respond` | Review-exchange agents | Experimental |
| `prepush-check` | Agents and git hooks | Supported |
| `verify-agent-sandbox` | Guardrail verification | Internal |

### Agent completion contracts — supported

`coding-done` and `reviewer-done` are how agents report intent; the orchestrator
validates that intent as untrusted input and decides what happens next. They are
a supported, agent-facing surface: prompts and target-repo guardrails depend on
them, so the subcommand names (`completed`, `blocked`, `needs_human`,
`approved`, `changes_requested`) are stable within a minor version, while flags
may gain options between minors. See
[`AGENT_PROTOCOL.md`](../../AGENT_PROTOCOL.md).

### MCP server tools — experimental

The MCP server (`issue-orchestrator-mcp`, stdio transport only) exposes these
tools. **Names, arguments, and return shapes may change in any `0.x` release.**

| Tool | Purpose |
|---|---|
| `orchestrator.status` | Current orchestrator status |
| `orchestrator.start` | Start the orchestrator for the configured repo |
| `orchestrator.stop` | Stop the orchestrator |
| `orchestrator.pause` | Pause issue claiming |
| `orchestrator.resume` | Resume issue claiming |
| `orchestrator.refresh` | Force an immediate issue refresh |
| `orchestrator.shutdown` | Shut down; `force=True` also requires `confirm=True` |
| `orchestrator.snapshot` | Full state snapshot |
| `orchestrator.state` | Unified dashboard state |
| `orchestrator.urls` | Dashboard and API URLs |
| `orchestrator.doctor` | Run diagnostics |
| `orchestrator.session.worktree` | Worktree path for an issue's session |
| `orchestrator.session.manifest` | Run manifest for an issue's session |
| `orchestrator.session.phases` | Phase history for an issue's session |
| `orchestrator.session.claude_log` | Agent log tail |
| `orchestrator.session.orchestrator_log` | Orchestrator log for the session |
| `orchestrator.session.kill` | Kill an issue's session |
| `orchestrator.session.focus` | Focus the session terminal |
| `orchestrator.repos` | List registered repos |
| `orchestrator.repos.start` | Start the orchestrator for a repo path |
| `orchestrator.repos.stop` | Stop the orchestrator for a repo path |

The registered set is declared as data in `MCP_TOOLS`
([`entrypoints/mcp_server.py`](../../src/issue_orchestrator/entrypoints/mcp_server.py))
and drift-tested against this table by
`tests/unit/test_public_api_surface_docs.py`.

Two deliberate omissions, not oversights: there is no tool that types free-form
text into a running agent session (a prompt-injection primitive), and the
transport is stdio only. Detailed usage documentation is tracked separately in
issue #6463; see [VS Code Integration](vscode.md) for client setup today.

### Web / SSE public contracts — versioned (the model to follow)

This is the surface other surfaces should grow toward:

- Pydantic contracts in
  [`contracts/public.py`](../../src/issue_orchestrator/contracts/public.py) are
  the source of truth.
- Generated JSON Schema artifacts are committed under
  [`contracts/public/`](../../contracts/public/) (regenerate with
  `python scripts/generate_public_contracts.py`).
- `tests/unit/test_public_contract_schemas.py` fails when the code and the
  committed schemas disagree, so a payload change cannot ship silently.
- Every structured event payload carries a `schema` field
  (`EVENT_SCHEMA_VERSION` in
  [`events/catalog.py`](../../src/issue_orchestrator/events/catalog.py)) plus
  `run_id` and `tick_id`, so consumers can detect a version they do not
  understand instead of misparsing it.
- Event names come from the `EventName` enum, not ad-hoc strings.

Consumers should react to events and contract fields, never to log text. Logs
are for humans and change freely.

The UI HTTP surface is described by [`docs/api/ui-openapi.json`](../api/ui-openapi.json),
generated from the canonical schema and drift-tested the same way.

### Control API — internal

The Control API on port `19080` (`/api/*`, `/control/*`) is how the Control
Center, the supervisor, and orchestrator-managed agents talk to a running
engine. It is bearer-token authenticated and **not a third-party integration
point**: routes, payloads, and auth semantics change whenever the internal
lifecycle needs them to. Use the CLI or the MCP tools instead.

### Python package — internal

`import issue_orchestrator` is not a supported API. Module layout follows the
hexagonal boundaries described in
[Internal Architecture](../architecture/internal-architecture.md) and is
refactored freely. The supported programmatic entry points are the CLI, the
completion tools, and (experimentally) the MCP server.

### Plugin entry points — experimental

Two entry point groups let external packages extend the orchestrator:

- `issue_orchestrator.plugins` — pluggy plugins implementing the hook spec in
  [`infra/hooks/hookspec.py`](../../src/issue_orchestrator/infra/hooks/hookspec.py).
- `issue_orchestrator.ai_provider_keys` — provider API-key metadata, so optional
  packages can contribute key names without hardcoding them in core.

Both are real extension points and both are experimental: hook signatures may
change while the port set is still settling.

### VS Code extension — first-party coupled

The extension in [`packages/vscode`](../../packages/vscode) drives the Python
package through `issue-orchestrator-mcp`. Because it depends on the
experimental MCP surface, **run the extension built from the same commit as the
installed Python package**. Version skew between an older extension and a newer
package (or the reverse) is not supported and is the first thing to rule out
when extension commands fail. See [VS Code Integration](vscode.md).

## Release mechanics

**SemVer, and `0.x` means what SemVer says it means.** Per
[semver.org clause 4](https://semver.org/#spec-item-4), a `0.y.z` version exists
for initial development and the public API should not be considered stable.
Concretely, during `0.x`:

- **Minor** (`0.10.0` → `0.11.0`) may break any surface on this page except the
  versioned contracts. Config keys may be renamed, CLI flags may change, MCP
  tools may disappear.
- **Patch** (`0.10.0` → `0.10.1`) is reserved for fixes that do not intend to
  break a documented surface.
- **Versioned contracts** are the exception: a breaking change to a public
  contract payload bumps its schema version, and the committed schema artifacts
  make the change reviewable in the diff.

**Every `0.x` release is a GitHub pre-release.** `make release VERSION=v0.11.0`
publishes with `gh release create … --prerelease`, so `0.x` tags carry the
pre-release badge and do not claim the "Latest" pointer. This is derived from
the version itself (major `0`), not from an operator remembering a flag. The
first `1.0.0` release publishes as a normal release.

SemVer pre-release identifiers (`v0.11.0-beta.1`) are not supported by the
release tooling today; it requires a stable `X.Y.Z` version so that package
metadata, the lockfile, and the tag cannot drift apart. The `0.` prefix plus the
GitHub pre-release marking is how instability is signalled during `0.x`.

The two-step operator flow (`make release-pr`, then `make release`) is in
[Release Process](../development/RELEASE.md).

## Path to 1.0

Dropping the leading `0` is a promise, so it waits on the experimental surfaces
graduating:

1. **MCP tools become supported** — the tool set stops moving, arguments and
   return payloads are contract-typed and drift-tested the way the Web/SSE
   payloads already are, and usage documentation exists (#6463).
2. **Config schema stops renaming keys** — additive-only within a major, with a
   documented deprecation path for anything that must move.
3. **CLI flags stabilize** — command and flag names become additive-only within
   a major.
4. **Plugin hook signatures stabilize** — the port set settles enough that
   third-party plugins survive a minor upgrade.

Surfaces marked Internal stay internal after `1.0`; they are not on the list
because stability there is not a goal.

## Keeping this page honest

This inventory is enforced, not aspirational.
`tests/unit/test_public_api_surface_docs.py` fails when a console script, an
MCP tool, or a CLI command exists in the code but is not classified here (and
when this page names one that no longer exists). Adding a surface means
declaring its tier in the same change.
