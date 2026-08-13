# Stability & API Surface

Issue-Orchestrator is an early-beta `0.x` project. This page is the single
source of truth for **what you can depend on and what may change under you**:
every public-facing surface, its stability tier, and the release mechanics that
back those promises.

Per-surface *usage* documentation lives with each surface (linked below). This
page owns the **inventory and the policy**.

## The short version

While the version starts with `0.`, the public API is **not stable**. Any
surface on this page may change in a minor release, except the SSE event
envelope, whose schema version makes a breaking change detectable at runtime.
Every `0.x` release is published as a GitHub *pre-release* so that instability
is visible from the releases page, not just from prose.

## Stability tiers

<!-- inventory:tiers -->

| Tier | What it means during `0.x` |
|---|---|
| `Versioned` | The payload itself carries a schema version, so a consumer can detect a version it does not understand at runtime instead of misparsing it. A breaking change bumps that version. |
| `Contracted` | The shape is owned by a checked-in schema artifact and a drift test fails when code and artifact disagree, so a breaking change is visible in review. The payload carries **no** runtime version — you find out at review time, not at request time. |
| `Supported` | Intended for external use and documented. May change between minor versions; changes are called out in release notes. |
| `Retired` | The capability is **gone**. The name is still accepted so an old script gets a clear pointer to the replacement instead of an "invalid choice" error, but every invocation fails. Nothing to migrate to a newer flag — migrate off the command. |
| `Experimental` | Usable, but names, arguments, and return shapes may change or be removed in any release without notice. Do not build automation you cannot re-point. |
| `Internal` | Not for third-party use. No compatibility promise of any kind, including within a patch release. Reachable does not mean supported. |
| `First-party coupled` | Ships from this repo and is expected to move in lockstep with the Python package. Version skew is not supported. |

Only `Versioned` promises runtime detectability. `Contracted` is deliberately
weaker and is the honest label for everything generated from a committed schema
artifact but served without a version field.

## Surface inventory

| Surface | Where | Public? | Tier during `0.x` |
|---|---|---|---|
| Config YAML schema | [`infra/settings_schema.py`](../../src/issue_orchestrator/infra/settings_schema.py), [Configuration Reference](configuration_reference.md) | Yes | Supported |
| CLI (`issue-orchestrator …`) | [`entrypoints/cli_parser.py`](../../src/issue_orchestrator/entrypoints/cli_parser.py) | Yes | Supported (per-command tiers below) |
| Agent completion contracts (`coding-done`, `reviewer-done`) | [`entrypoints/cli_tools/`](../../src/issue_orchestrator/entrypoints/cli_tools/) | Yes (agent-facing) | Supported |
| MCP server tools (`orchestrator.*`) | [`entrypoints/mcp_server.py`](../../src/issue_orchestrator/entrypoints/mcp_server.py) | Yes | **Experimental** |
| Repository Engine dashboard event stream (`GET /api/events`) | [`entrypoints/web.py`](../../src/issue_orchestrator/entrypoints/web.py), [`events/sse_envelope.py`](../../src/issue_orchestrator/events/sse_envelope.py) | Yes | **Versioned** |
| Other SSE streams (Control API, Control Center repo status) | [`entrypoints/control_api.py`](../../src/issue_orchestrator/entrypoints/control_api.py), [`entrypoints/control_api_repo_routes.py`](../../src/issue_orchestrator/entrypoints/control_api_repo_routes.py) | No | Internal |
| Schema-backed SSE payloads and view models | [`contracts/public/`](../../contracts/public/), [`contracts/public.py`](../../src/issue_orchestrator/contracts/public.py) | Yes | Contracted (subset listed below) |
| All other SSE event payloads | [`events/catalog.py`](../../src/issue_orchestrator/events/catalog.py) | Yes | Experimental |
| Contracted HTTP routes | [`docs/api/ui-openapi.json`](../api/ui-openapi.json) | Yes | Contracted |
| All other `/api/*` and `/control/*` routes | [`entrypoints/control_api.py`](../../src/issue_orchestrator/entrypoints/control_api.py), [`entrypoints/web.py`](../../src/issue_orchestrator/entrypoints/web.py) | No | Internal |
| Python package (`import issue_orchestrator`) | [`src/issue_orchestrator/`](../../src/issue_orchestrator/) | No | Internal |
| Plugin entry points | [`infra/hooks/hookspec.py`](../../src/issue_orchestrator/infra/hooks/hookspec.py), [`infra/ai_keys.py`](../../src/issue_orchestrator/infra/ai_keys.py) | Yes | Experimental |
| VS Code extension ↔ package | [`packages/vscode`](../../packages/vscode), [VS Code Integration](vscode.md) | First-party | First-party coupled |

### Config YAML schema — supported

`.issue-orchestrator/config/modes/<mode>/<name>.yaml` is the primary way you configure the
orchestrator, and it is a supported surface. The schema is generated from
[`infra/settings_schema.py`](../../src/issue_orchestrator/infra/settings_schema.py)
into the [Configuration Reference](configuration_reference.md), and a drift test
(`tests/unit/test_settings_schema.py`) keeps the two in sync.

During `0.x`, keys may be added, renamed, or moved between minor versions.
Unknown keys are rejected rather than ignored, so a renamed key fails loudly at
startup instead of silently doing nothing. Run `issue-orchestrator doctor` after
upgrading.

### CLI — supported

`issue-orchestrator --help` is authoritative for flags. The top-level command
set is declared as data in `CLI_COMMAND_SURFACE`
([`entrypoints/cli_parser.py`](../../src/issue_orchestrator/entrypoints/cli_parser.py)),
and this table is the same set with the tier each command carries:

<!-- inventory:cli-commands -->

| Command | Group | Tier |
|---|---|---|
| `start` | Runtime | Supported |
| `status` | Runtime | Supported |
| `attach` | Runtime | Retired |
| `switch` | Runtime | Retired |
| `dashboard` | Runtime | Retired |
| `output` | Runtime | Retired |
| `pause` | Runtime | Supported |
| `resume` | Runtime | Supported |
| `tech_lead` | Runtime | Supported |
| `health-review` | Runtime | Supported |
| `refresh` | Runtime | Supported |
| `restart` | Runtime | Supported |
| `setup` | Setup | Supported |
| `init` | Setup | Supported |
| `verify` | Setup | Supported |
| `setup-hooks` | Setup | Supported |
| `setup-guardrails` | Setup | Supported |
| `auth` | Credentials | Supported |
| `keys` | Credentials | Supported |
| `doctor` | Diagnostics | Supported |
| `audit` | Diagnostics | Supported |
| `trace` | Diagnostics | Supported |
| `demo` | Diagnostics | Supported |
| `test-reset` | Development | Internal |
| `e2e-reset` | Development | Internal |

Supported command names are stable within a minor version; flags may change
between minor versions. Prefer `--config` and `--set path=value` over positional
coupling in scripts.

`attach`, `switch`, `dashboard`, and `output` are `Retired`: the parser still
accepts them, but each one prints where to go instead and **exits non-zero**.
They predate the web dashboard and the session recordings that replaced them.
Do not treat a `Retired` command as working — if a script calls one, it is
already failing.

`test-reset` and `e2e-reset` operate on test and E2E state and carry no
compatibility promise of any kind — they are reachable, not supported.

`tests/unit/test_cli.py::TestRetiredCommandStubs` pins these tiers to the
handlers: it invokes every `Retired` command and requires a non-zero exit, and
it fails if any command declared otherwise is in fact a failing stub. The tier
cannot drift from the behavior again.

The console scripts installed by the package:

<!-- inventory:console-scripts -->

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

<!-- inventory:mcp-tools -->

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

### Repository Engine dashboard event stream — versioned

**This tier applies to exactly one endpoint: `GET /api/events` as served by a
Repository Engine's dashboard app**, implemented by `entrypoints.web.events`.
That is the stream the browser dashboard consumes and the one the
[contracted payload schemas](#sse-payloads-and-dashboard-view-models--contracted)
describe. It is the only surface that promises runtime version detection, and
the model the others should grow toward.

This repository serves three SSE endpoints, and only the first is public:

<!-- inventory:sse-streams -->

| Endpoint | Route | Scope | Tier |
|---|---|---|---|
| `issue_orchestrator.entrypoints.web.events` | `/api/events` | Repository Engine dashboard | Versioned |
| `issue_orchestrator.entrypoints.control_api.events` | `/api/events` | Control API (Control Center) | Internal |
| `issue_orchestrator.entrypoints.control_api_repo_routes.control_events` | `/control/events` | Control Center repo status | Internal |

The two `/api/events` rows are different implementations at the same path on
different apps, which is exactly why the promise is bound to the handler and not
to the path. The dashboard app registers its own routes before mounting the
Control API, so on an engine dashboard `/api/events` is the versioned stream; on
the Control Center it is the internal EventHub stream, whose wire object
(`event_id`, `type`, `issue_key`, `payload`) carries no envelope version and
exists for test automation. `/control/events` streams Control Center repo-status
snapshots and is likewise internal.

`tests/unit/test_public_api_surface_docs.py` discovers every SSE endpoint in the
source, requires this table to match that set exactly, and asserts that the row
tiered `Versioned` is the handler whose module actually applies the envelope —
so the public row cannot drift onto an unversioned stream, and a new stream
cannot be added without classifying it.

**Every event on the versioned stream carries `schema`**, the
`EVENT_SCHEMA_VERSION` from
[`events/catalog.py`](../../src/issue_orchestrator/events/catalog.py). A
breaking change to the envelope bumps it, so a consumer can refuse a version it
does not understand instead of misparsing it.

That is a guarantee rather than a convention because the field is applied by a
single owner at the broadcast boundary —
[`events/sse_envelope.py`](../../src/issue_orchestrator/events/sse_envelope.py),
called from `broadcast_event()` — not by each producer. Events emitted as plain
dictionaries (the observer, direct broadcasts such as `startup_complete`) get
the same envelope as events built through `EventContext`, because nothing
reaches a subscriber without passing through that function.
`tests/unit/test_sse_envelope.py` covers both producer paths end to end and
fails if any other code starts enqueueing to subscribers directly.

**`run_id` and `tick_id` are narrower**, and deliberately so: they identify an
orchestrator run and control tick, which a direct broadcast like
`startup_complete` has no meaningful value for. They are added by
`EventContext.enrich()` in
[`events/context.py`](../../src/issue_orchestrator/events/context.py) and are
present on control-loop events only. **Do not assume every event has them** —
read them when present, and key on `schema` for version handling.

Most event names come from the `EventName` enum
([`events/catalog.py`](../../src/issue_orchestrator/events/catalog.py), guarded
by `tests/unit/test_event_catalog.py`). Two direct broadcasts are the exception
and use literal names not in the enum: `startup_complete` and
`shutdown_requested`. Both have committed payload schemas, so they are listed
below with the rest of the contracted set.

Consumers should react to events and contract fields, never to log text. Logs
are for humans and change freely.

### SSE payloads and dashboard view models — contracted

The `Contracted` tier applies to the payload *shapes* that have a committed
schema artifact — which is a **selected subset** of the event stream, not all of
it:

- Pydantic contracts in
  [`contracts/public.py`](../../src/issue_orchestrator/contracts/public.py) are
  the source of truth (`PUBLIC_CONTRACTS`).
- Generated JSON Schema artifacts are committed under
  [`contracts/public/`](../../contracts/public/) (regenerate with
  `python scripts/generate_public_contracts.py`).
- `tests/unit/test_public_contract_schemas.py` fails when the code and the
  committed schemas disagree, so a payload change cannot ship silently.

These SSE events have a committed payload schema:

<!-- inventory:sse-payloads -->

| Event | Payload schema |
|---|---|
| `session.started` | `contracts/public/sse.session.started.json` |
| `session.completed` | `contracts/public/sse.session.completed.json` |
| `orchestrator.paused` | `contracts/public/sse.orchestrator.paused.json` |
| `orchestrator.resumed` | `contracts/public/sse.orchestrator.resumed.json` |
| `queue.changed` | `contracts/public/sse.queue.changed.json` |
| `dependency.blocked` | `contracts/public/sse.dependency.blocked.json` |
| `dependency.unblocked` | `contracts/public/sse.dependency.unblocked.json` |
| `stale.in_progress_detected` | `contracts/public/sse.stale.in_progress_detected.json` |
| `stale.in_progress_cleared` | `contracts/public/sse.stale.in_progress_cleared.json` |
| `stale.persistent_detected` | `contracts/public/sse.stale.persistent_detected.json` |
| `history.reconciled` | `contracts/public/sse.history.reconciled.json` |
| `startup_complete` | `contracts/public/sse.startup_complete.json` |
| `shutdown_requested` | `contracts/public/sse.shutdown_requested.json` |

Non-SSE payloads on the same tier: `dashboard.view_model`, `timeline.issue`, and
`stack.dependency_gate_view`.

`tests/unit/test_public_api_surface_docs.py` asserts this table equals the
`sse.*` entries of `PUBLIC_CONTRACTS` exactly, in both directions.

**Every other event on the stream is `Experimental`.** The `EventName` catalog
has well over a hundred entries; the ones above are the payloads that have been
promoted to a committed contract. The rest still arrive inside the versioned
envelope — that part of the promise is unconditional — but per the
`Experimental` tier **both their payload fields and their names may change or
disappear in any release**. Only the events listed above have a name and shape
you can hold the project to. If you depend on another one, say so on an issue
and it can be promoted.

**No payload on this tier carries a version field of its own.** A breaking
change is visible in the schema artifact diff during review; it is not
detectable by a client at runtime. That is why these are `Contracted` and not
`Versioned` — pin to a release, and read the artifact diff when you upgrade.

### HTTP routes — a contracted subset, internal remainder

Two HTTP scopes exist, and they are not the same thing:

- **Control Center** — the local UI shell that manages repository engines.
  Listens on `:19080` by default and serves the `/control/*` routes.
- **Repository Engine** — one long-lived runtime per repository, serving its own
  browser dashboard and the `/api/*` routes. Its ports come from `ui.web_port`
  and `ui.control_api_port` (`0` = auto-assign a free port), so do not hardcode
  them; ask the CLI (`issue-orchestrator status`) or the
  `orchestrator.urls` MCP tool. Each engine dashboard also mounts the
  `/control/*` routes, so reaching a route says nothing about its scope.

The **contracted** HTTP surface is exactly the path set in
[`docs/api/ui-openapi.json`](../api/ui-openapi.json). That document is the
canonical schema:
[`contracts/ui_openapi_models.py`](../../src/issue_orchestrator/contracts/ui_openapi_models.py)
is generated from it, `tests/unit/test_ui_openapi_generated.py` fails when the
two disagree, and the `ui_openapi_routes` quality guardrail fails when a
contracted route goes missing or stops using its generated response model.

<!-- inventory:http-routes -->

| Path | Scope |
|---|---|
| `/api/dialog/blocked-issues` | Repository Engine |
| `/api/dialog/config` | Repository Engine |
| `/api/dialog/debug` | Repository Engine |
| `/api/dialog/doctor` | Repository Engine |
| `/api/dialog/info` | Repository Engine |
| `/api/dialog/phase/{issue_number}` | Repository Engine |
| `/api/dialog/session-diagnostics/{issue_number}` | Repository Engine |
| `/api/dialog/validation-failure/{issue_number}` | Repository Engine |
| `/api/e2e-run-detail/{run_id}` | Repository Engine |
| `/api/e2e-run/{run_id}/issue-detail/{issue_number}` | Repository Engine |
| `/api/e2e-run/{run_id}/test-output` | Repository Engine |
| `/api/e2e-runs/recent` | Repository Engine |
| `/api/issue-detail/{issue_number}` | Repository Engine |
| `/api/issue-rows` | Repository Engine |
| `/api/retrospective-review` | Repository Engine |
| `/api/retrospective-review/preflight` | Repository Engine |
| `/api/tech-lead/runs` | Repository Engine |
| `/api/view-model` | Repository Engine |
| `/api/view-model-snapshot` | Repository Engine |
| `/control/e2e/run/{run_id}/timeline` | Control Center |
| `/control/setup/detect` | Control Center |
| `/control/setup/github-auth/store-personal-token` | Control Center |
| `/control/setup/github-auth/verify` | Control Center |
| `/control/setup/preview` | Control Center |
| `/control/setup/prereqs` | Control Center |
| `/control/setup/save` | Control Center |
| `/control/tools/worktrees/cleanup` | Control Center |

`tests/unit/test_public_api_surface_docs.py` asserts this table equals the
OpenAPI path set exactly, so contracting a new route cannot leave it classified
as internal by prose.

**Every other `/api/*` and `/control/*` route is internal, with one carve-out.**
The exception is the Repository Engine dashboard's `GET /api/events`, which is
public and `Versioned` — see
[the SSE stream table](#repository-engine-dashboard-event-stream--versioned).
It is absent from the OpenAPI contract because that document describes JSON
request/response operations, not an event stream; its payload shapes are
contracted separately under [`contracts/public/`](../../contracts/public/).

The uncontracted remainder — including the *other* two SSE endpoints — is how
the Control Center, the supervisor, and orchestrator-managed agents drive a
running engine: bearer-token authenticated, and routes, payloads, and auth
semantics change whenever the internal lifecycle needs them to. Reachable is not
supported. For third-party automation, use the CLI or the MCP tools, not an
uncontracted route.

Note that `info.version` in the OpenAPI document describes the *document*, not
the responses: it is not delivered on the wire and a client cannot read it off a
response. **There is no surface-wide HTTP contract version** — no field that
every contracted response carries and that a breaking change would bump. That is
precisely what separates `Contracted` from `Versioned` here.

Individual payloads may still version themselves; `E2ETimelineEventPayload`
carries `timeline_schema_version`, for example. That is a per-payload detail, not
a surface guarantee, and it does not make the HTTP surface `Versioned`.
`tests/unit/test_public_api_surface_docs.py` checks for the surface-wide form —
a version field common to *every* contracted response — and fails if one
appears, so this section gets promoted rather than quietly under-selling itself.

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

- **Minor** (`0.10.0` → `0.11.0`) may break any surface on this page. Config
  keys may be renamed, CLI flags may change, MCP tools may disappear.
- **Patch** (`0.10.0` → `0.10.1`) is reserved for fixes that do not intend to
  break a documented surface.
- **The `Versioned` SSE envelope** is the one surface whose breakage is
  detectable at runtime: a breaking envelope change bumps `EVENT_SCHEMA_VERSION`,
  so a client can reject a version it does not understand.
- **`Contracted` surfaces** can still break in a minor. What they guarantee is
  that the break is *reviewable*: the committed schema artifacts change in the
  same diff, and the drift tests fail if they do not. Read the artifact diff
  when you upgrade — a client cannot detect the change at runtime.

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
   return payloads are contract-typed and drift-tested the way the SSE payloads
   already are, and usage documentation exists (#6463).
2. **Config schema stops renaming keys** — additive-only within a major, with a
   documented deprecation path for anything that must move.
3. **CLI flags stabilize** — command and flag names become additive-only within
   a major, and the `Retired` command stubs are deleted outright.
4. **Plugin hook signatures stabilize** — the port set settles enough that
   third-party plugins survive a minor upgrade.
5. **`Contracted` HTTP payloads become `Versioned`** — the contracted route set
   carries a version a client can read at runtime, rather than only a schema
   artifact a human can diff.

Surfaces marked Internal stay internal after `1.0`; they are not on the list
because stability there is not a goal.

## Keeping this page honest

This inventory is enforced, not aspirational. The tables marked with an
`<!-- inventory:… -->` comment are parsed by
`tests/unit/test_public_api_surface_docs.py` and compared for **exact set
equality** against the code, in both directions:

| Table | Compared against |
|---|---|
| `inventory:cli-commands` | `CLI_COMMAND_SURFACE` — name, group, and tier |
| `inventory:console-scripts` | `[project.scripts]` in `pyproject.toml` |
| `inventory:mcp-tools` | `MCP_TOOLS` in `entrypoints/mcp_server.py` |
| `inventory:http-routes` | the path set in `docs/api/ui-openapi.json` |
| `inventory:sse-payloads` | the `sse.*` entries of `PUBLIC_CONTRACTS` |
| `inventory:sse-streams` | every endpoint returning `EventSourceResponse` |
| `inventory:tiers` | every tier any inventory table uses |

So adding a surface fails the build until it is classified here, removing one
fails the build until it is deleted from here, and a tier this page invents but
never defines fails too. Mentioning a name in prose does not classify it — only
a row in the anchored table counts. Relative links on this page are checked to
resolve as well, so a moved file cannot leave a dangling promise behind.
