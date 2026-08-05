"""Provider adapters that translate a :class:`SandboxScope` into CLI flags.

A :class:`~issue_orchestrator.domain.sandbox_scope.SandboxScope` is
provider-agnostic. Each AI-agent CLI enforces a sandbox differently, so the
translation lives here, next to the providers, behind the
:class:`ProviderSandboxAdapter` port.

The **claude-code** translation emits a Claude Code
settings object (passed inline via ``--settings '<json>'``, matching the
existing ``--mcp-config '<json>'`` pattern the provider already uses) plus
``--permission-mode dontAsk`` — a non-yolo, still-unattended mode that runs only
allow-listed tools and auto-denies the rest, instead of ``bypassPermissions``.

ENFORCEMENT LAYERS (verified against docs.claude.com — Sandbox / Permission
modes, and confirmed against the live CLI). Two mechanisms exist, but for
secrets they OVERLAP, so we deliberately use only ONE:

1. **OS sandbox — Bash and its child processes only.** ``sandbox.filesystem`` /
   ``credentials`` / ``network`` are enforced by the operating system (Seatbelt
   / bubblewrap) on sandboxed Bash subprocesses. They do NOT constrain Claude's
   built-in Read/Edit/Grep/Glob tools ("Built-in file tools ... use the
   permission system directly rather than running through the sandbox").
2. **Permission rules — every tool, incl. the native file tools.**
   ``permissions.allow`` / ``permissions.deny`` are evaluated before any tool
   runs. Deny rules apply in every permission mode (even ``bypassPermissions``),
   so a ``permissions.deny`` entry is un-bypassable. Crucially, Claude ALSO
   **merges permission rules into the OS sandbox profile** (docs: "Paths from
   both sandbox.filesystem settings and permission rules are merged"), so a
   ``permissions.deny Read(path)`` denies the path to the native Read tool AND to
   a sandboxed ``cat``.

FAIL-CLOSED secret model — exactly ONE rule per secret, ``permissions.deny
Read(path)`` (single-mechanism by design; see #6867 for why enumerating more
rules is harmful):
- ``Read(path)`` covers ALL of Claude's file-READING tools (Read/Grep/Glob); the
  live CLI warns per-tool ``Grep``/``Glob``/``Write`` rules are no-ops ("not
  matched by file permission checks — only Read(path) rules are"). Because
  permission denies merge into the OS profile (layer 2 above), this ONE rule
  blocks the secret for the native Read tool AND for Bash. We therefore do NOT
  emit a ``sandbox.credentials.files`` deny for secrets — it applies the SAME
  restriction and would DUPLICATE every secret path in the profile.
- No ``Edit(secret)`` rule: writes are already confined to the worktree by
  ``allowWrite`` + the worktree-scoped ``Edit`` allow, so a secret outside the
  worktree is unwritable regardless.
- No ``path/**`` glob: a bare ``Read(<dir>)`` already denies the whole subtree,
  whereas a ``/**`` deny makes Claude glob-expand it against the real filesystem
  into one rule per file. Claude bakes the whole profile into a single
  ``sandbox-exec -p`` argument, and its own default deny list is already large,
  so extra/expanded rules can overflow the OS arg limit (``E2BIG``) and break ALL
  sandboxed Bash. ``sandbox.credentials.envVars`` (secret env-var unset) IS still
  emitted — that has no filesystem-path equivalent to duplicate.

TRUST BOUNDARY (ADR-0034 trusted-repository contract). The orchestrator's
operator selects and onboards the target repository, so its checked-in Claude
configuration (project/local ``.claude/settings.json``) plus the operator's own
user/managed settings are TRUSTED inputs — accepting workspace trust is
authorization to load them, exactly as Claude Code normally treats a trusted
workspace. ``sandbox: true`` therefore provides a provider-native, per-session
boundary *under that trusted configuration*; it is NOT an "open an arbitrary
hostile repository safely" mode (that is the separate untrusted-repository track,
optional hardening in #6861 via an external isolation substrate). This adapter
does not try to out-parse Claude's settings model; it translates the scope into
Claude's native sandbox and constrains the AGENT.

WHAT THE ADAPTER CONSTRAINS (against the agent, not the trusted repo):
- **Writes** — Bash and native ``Edit`` (which governs Edit/Write/MultiEdit) are
  allowed only within the worktree write roots; outside is denied.
- **Secrets** — one ``permissions.deny Read(path)`` per secret, which (because
  permission rules merge into the OS sandbox profile) covers BOTH the native Read
  tool and Bash; plus ``credentials.envVars`` for secret env vars.
- **Egress** — restricted per :data:`SandboxEgress` (Bash ``allowedDomains`` +
  ``WebSearch``/``WebFetch``/``curl``/``wget`` denies).
- **Self-modification** — the agent may not rewrite its own policy: the worktree
  ``.claude/settings.json`` / ``settings.local.json`` are ``denyWrite`` (Bash) and
  ``Edit``-denied (native). Deny beats the worktree allow, so a session cannot
  hot-reload a wider policy after launch. This defends against the agent, which is
  distinct from distrusting the repository's *initial* contents.

READ POSTURE (deliberate): non-secret reads OUTSIDE the worktree remain possible
(``Read``/``Grep``/``Glob`` allowed; ``denyRead: ["~/"]`` bounds *Bash* reads as
defense-in-depth). This is not a read jail; a hard read boundary is the
whole-process track (#6861).

KNOWN LIMITATION: a GitHub *App* private key at an operator-configured absolute
path *outside* home is covered only if it is listed in ``deny_read_files``; the
static secret list lives in the domain (``DEFAULT_SANDBOX_DENY_READ_FILES``) and
a home-based key is additionally covered by the Bash-layer ``denyRead: ["~/"]``.

Settings schema (docs.claude.com — Sandbox settings / Permissions):
- ``sandbox.enabled`` / ``failIfUnavailable`` / ``allowUnsandboxedCommands``
- ``sandbox.filesystem.denyRead[]`` / ``allowRead[]`` / ``allowWrite[]`` /
  ``denyWrite[]`` (the policy files; ``denyWrite`` wins over ``allowWrite``)
- ``sandbox.network.allowedDomains[]`` (omitted for ``model+web``; explicit
  empty list for ``none`` to block Bash network entirely)
- ``sandbox.credentials.envVars[]`` — objects ``{"name": ..., "mode": "deny"}``
  (secret env vars only; ``credentials.files`` is intentionally NOT emitted —
  secret file reads are covered by ``permissions.deny Read(...)`` above)
- ``permissions.allow[]`` — ``Read``/``Grep``/``Glob`` + worktree-scoped
  ``Edit(//worktree/**)`` (Edit governs the file-editing tools; no ``Write`` rule)
- ``permissions.deny[]`` — ``ToolName(pattern)``: one ``Read(secret)`` per secret
  (covers native + Bash via the permission→sandbox merge) + the policy-file
  ``Edit`` denies + egress. NOTE: Read/Edit permission specifiers use ``//abs`` /
  ``/rel`` / ``~/`` prefixes, which DIFFER from ``sandbox.filesystem`` paths
  (``/abs`` absolute).

CODEX TRANSLATION (Codex 0.138+ permission profiles): ``--sandbox
workspace-write`` cannot represent :attr:`SandboxScope.deny_read_files` and,
empirically, permits reads outside the workspace. Current Codex also documents
that ``--sandbox`` selects the legacy policy and disables permission profiles.
The adapter therefore uses an invocation-scoped profile extending Codex's native
``:workspace`` profile instead of emitting the superficially similar but weaker
``-s workspace-write`` flag. Because a legacy ``sandbox_mode`` in any loaded
Codex config layer disables profiles, the adapter checks the documented system,
user, and complete project-root-to-working-directory config chain and fails loud
with migration guidance when that incompatible key is present. The provider
emits neither a named ``--profile`` layer nor arbitrary CLI config overrides;
``CODEX_HOME`` is the only documented environment variable that relocates a
config layer. A custom ``project_root_markers`` value that omits ``.git`` fails
loud because it could extend project config discovery outside the orchestrator
worktree. The profile:

- pins the worktree with ``-C`` and grants only explicitly scoped extra roots
  with repeated ``--add-dir``;
- writes workspace roots, carves every ``deny_read_files`` path to ``deny``, and
  keeps ``.codex`` read-only so the agent cannot rewrite its own project policy;
- resolves linked-worktree Git metadata before launch so staging and commits can
  update only that worktree's admin state and current branch; shared config,
  hooks, packs, other refs, and other worktrees stay read-only;
- denies temporary-directory writes outside the explicit roots;
- disables command network plus native web search for ``none`` / ``model-only``;
- excludes ``deny_env`` names from spawned-command environments; and
- uses ``-a never`` so a denied operation fails instead of prompting to escape.

Codex ``--add-dir`` grants WRITE, so it carries only ``write_roots`` — a
read-only ``read_root`` (the tech-lead evidence god-view, #6824 R5) is NOT
added there. Those read-only roots are emitted as explicit ``read`` entries in
the permission profile's ``filesystem`` table (see ``_codex_read_only_roots`` /
``_codex_permission_profile``), so a tech lead READS them while WRITES stay
confined to the scratch worktree. The adapter widens only the exact
orchestrator-computed roots, never ``$HOME`` or an unrelated repository.

Git commits have one further linked-worktree residual: they must create loose
objects in the base repository's shared object store. That object subtree is
writable, while ``objects/info`` (including alternates) and ``objects/pack`` are
carved back to read-only. The current branch ref/reflog is writable by exact
path; all other shared Git metadata remains read-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol, runtime_checkable

from issue_orchestrator.domain.sandbox_scope import (
    SandboxEgress,
    SandboxScope,
    SandboxUnsupportedError,
)

from .codex_config import (
    resolve_codex_home,
    validate_codex_permission_profile_compatibility,
)
from .git_worktree_access import (
    GitWorktreeAccess,
    git_worktree_filesystem_rules as _git_worktree_filesystem_rules,
    resolve_git_worktree_access,
)

CodexGitWorktreeAccess = GitWorktreeAccess
resolve_codex_git_worktree_access = resolve_git_worktree_access

__all__ = [
    "MODEL_API_DOMAINS",
    "MODEL_ONLY_DENY_TOOLS",
    "CODEX_PERMISSION_PROFILE",
    "CodexGitWorktreeAccess",
    "GitWorktreeAccess",
    "ClaudeSandboxAdapter",
    "CodexSandboxAdapter",
    "ProviderSandboxAdapter",
    "build_claude_sandbox_argv",
    "build_claude_sandbox_settings",
    "build_codex_sandbox_argv",
    "resolve_codex_git_worktree_access",
    "resolve_git_worktree_access",
    "validate_codex_permission_profile_compatibility",
]

CODEX_PERMISSION_PROFILE = "issue_orchestrator_scope"
_CODEX_CREDENTIAL_FILES: tuple[str, ...] = ("~/.codex",)
_CODEX_CREDENTIAL_ENV: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CODEX_HOME",
)

# The Anthropic model API host. This is the ONLY domain a restricted-egress
# ("model-only") sandbox pre-allows for Bash subprocesses. A broad ``github.com``
# entry is deliberately NOT allowed: the docs warn it is a data-exfiltration /
# domain-fronting path, and in this architecture the orchestrator (not the
# sandboxed agent) performs git pushes and PR creation, so the agent's Bash does
# not need source-host egress. NOTE: the model API is reached by the *agent
# process*, which is unsandboxed; listing it here only affects Bash subprocess
# egress (harmless, and documents the "model" floor explicitly).
MODEL_API_DOMAINS: tuple[str, ...] = ("api.anthropic.com",)

# Tools/commands denied for restricted egress. Belt-and-suspenders alongside the
# OS-level network allowlist: no web search, no ad-hoc HTTP fetchers. Entries
# use Claude Code's ``ToolName(pattern)`` permission syntax.
MODEL_ONLY_DENY_TOOLS: tuple[str, ...] = (
    "WebSearch",
    "WebFetch",
    "Bash(curl *)",
    "Bash(wget *)",
)

# A single ``Read(path)`` permission-deny fully protects a secret path:
#   - It governs ALL of Claude's file-READING tools (Read/Grep/Glob). The live CLI
#     warns that per-tool ``Grep``/``Glob``/``Write`` rules are "not matched by file
#     permission checks — only Read(path) rules are", so those are no-ops.
#   - Claude merges permission rules into the OS Seatbelt profile (verified live),
#     so ``Read(secret)`` also denies the secret to Bash — no separate
#     ``credentials.files`` / ``denyRead`` entry is needed (that would double-count).
#   - No ``Edit(secret)`` rule is needed: writes are already confined to the worktree
#     by ``allowWrite`` + the worktree-scoped ``Edit`` allow, so a secret OUTSIDE the
#     worktree is unwritable regardless.
# Emitting exactly one rule per secret keeps the ``sandbox-exec -p`` profile small;
# every redundant rule enlarges the single argument Claude passes and risks
# overflowing the OS arg limit (``E2BIG``), which breaks ALL sandboxed Bash.
NATIVE_FILE_TOOLS: tuple[str, ...] = ("Read",)

# Native read/enumeration capability allowed explicitly (``dontAsk`` runs only
# allow-listed tools). A worktree-scoped ``Edit(...)`` allow is added per session
# in :func:`build_claude_sandbox_settings` so the agent can edit its worktree;
# secret and settings-file ``Edit`` denies still win (deny beats allow).
NATIVE_READ_ALLOW_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

# Config files an agent must not modify at session time — writing a wider policy
# here would hot-reload and escalate. Relative to each write root.
_SELF_CONFIG_FILES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
)


def _self_config_paths(write_roots: tuple[Path, ...]) -> list[str]:
    """Absolute paths of the policy files the agent may not modify."""
    return [str(root / rel) for root in write_roots for rel in _SELF_CONFIG_FILES]


def _permission_rule_path(path: str) -> str:
    """Render a sandbox-style secret path as a Read/Edit permission specifier.

    Read/Edit permission rules use a DIFFERENT prefix convention from
    ``sandbox.filesystem.*`` paths: ``//abs`` for an absolute path, ``~/`` for
    home-relative, ``/rel`` for project-relative (docs.claude.com — Sandbox,
    "This syntax differs from Read and Edit permission rules"). ``deny_read_files``
    entries are home-relative (``~/.ssh``) or absolute (an operator/test path):
    a tilde path passes through, and an absolute path gets its leading slash
    doubled so it is read as absolute rather than project-relative.
    """
    if path.startswith("~"):
        return path
    if path.startswith("/"):
        return "/" + path
    return path


def _native_secret_deny_rules(deny_read_files: tuple[str, ...]) -> list[str]:
    """The single fail-closed secret-deny layer: one ``Read(path)`` per secret.

    Permission deny rules hold in every permission mode (un-bypassable) AND Claude
    merges them into the OS sandbox profile, so ``Read(secret)`` blocks the secret
    for both the native Read tool and a sandboxed ``cat`` — there is no separate
    ``credentials.files`` layer to keep in sync. One rule per (tool, path) suffices:

    - ``Read(path)`` covers every reading tool (Read/Grep/Glob) and ``Edit(path)``
      every editing tool (Edit/Write/MultiEdit) — see :data:`NATIVE_FILE_TOOLS`.
    - A single ``Read(<dir>)`` already denies everything beneath the directory, so
      NO ``/**`` variant is emitted. This is not just economy: Claude expands a
      ``path/**`` deny by globbing it against the *real* filesystem into one
      concrete rule per file, so a handful of populated secret dirs balloon into
      hundreds of paths and overflow the sandboxed-Bash seatbelt argument limit
      (``E2BIG``), which breaks ALL sandboxed Bash. A bare directory rule is a
      single subtree match with no enumeration. (Both facts verified against the
      live CLI.)
    """
    rules: list[str] = []
    for path in deny_read_files:
        spec = _permission_rule_path(path)
        for tool in NATIVE_FILE_TOOLS:
            rules.append(f"{tool}({spec})")
    return rules


@runtime_checkable
class ProviderSandboxAdapter(Protocol):
    """Port: translate a provider-agnostic scope into that provider's CLI args.

    Implementations return the argv fragment to splice into the provider's
    launch command (before the positional prompt). A provider that cannot yet
    enforce a sandbox raises :class:`NotImplementedError`.
    """

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        """Return the CLI argv fragment enforcing *scope* for this provider."""
        ...


def _allowed_domains_for_egress(egress: SandboxEgress) -> tuple[str, ...] | None:
    """Bash-subprocess network allowlist for an egress posture.

    Returns ``None`` for ``model+web`` — the adapter OMITS the network key so the
    sandbox adds no OS-level domain restriction. Otherwise returns an explicit
    (possibly empty) tuple: ``model-only`` pre-allows just the model API host,
    and ``none`` returns ``()`` so the adapter emits an EXPLICIT empty allowlist
    (Bash reaches no network) rather than omitting the key.
    """
    if egress == "model+web":
        return None
    if egress == "model-only":
        return MODEL_API_DOMAINS
    return ()  # "none": explicit empty allowlist — no Bash network at all


def _deny_tools_for_egress(egress: SandboxEgress) -> tuple[str, ...]:
    if egress == "model+web":
        return ()
    return MODEL_ONLY_DENY_TOOLS  # "model-only" and "none"


def build_claude_sandbox_settings(
    scope: SandboxScope,
    *,
    git_access: "GitWorktreeAccess | None" = None,
) -> dict[str, Any]:
    """Pure translation of a :class:`SandboxScope` into a Claude Code settings dict.

    Extracted as a pure function so the mapping is unit-testable without
    building a full command.
    """
    self_config_paths = _self_config_paths(scope.write_roots)
    git_rules = (
        _git_worktree_filesystem_rules(git_access) if git_access is not None else []
    )
    git_read_paths = [path for path, permission in git_rules if permission == "read"]
    git_write_paths = [path for path, permission in git_rules if permission == "write"]
    git_write_roots = tuple(Path(path) for path in git_write_paths)
    git_write_carveouts = [
        path
        for path in git_read_paths
        if any(Path(path).is_relative_to(root) for root in git_write_roots)
    ]

    allow_read = list(
        dict.fromkeys(
            [
                *(str(path) for path in scope.read_roots),
                *git_read_paths,
                *git_write_paths,
            ]
        )
    )
    allow_write = list(
        dict.fromkeys([*(str(path) for path in scope.write_roots), *git_write_paths])
    )
    deny_write = list(dict.fromkeys([*self_config_paths, *git_write_carveouts]))
    sandbox: dict[str, Any] = {
        "enabled": True,
        # Hard-fail rather than silently run unsandboxed if the sandbox can't
        # start, and refuse the ``dangerouslyDisableSandbox`` escape hatch.
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        # Sandbox-weakening booleans, pinned false so the session cannot be
        # softened out from under the boundary.
        "allowAppleEvents": False,
        "enableWeakerNetworkIsolation": False,
        "enableWeakerNestedSandbox": False,
        "filesystem": {
            # Reads are OPEN by default: deny the home dir (Bash defense-in-depth)
            # and re-allow only the read roots within it. The fail-closed secret
            # layer is the ``permissions.deny Read(...)`` rules below, which merge
            # into this OS profile and also cover Bash.
            "denyRead": ["~/"],
            "allowRead": allow_read,
            "allowWrite": allow_write,
            # Anti-self-modification (Bash layer): the agent may write its worktree
            # but NOT its own policy files. ``denyWrite`` wins over ``allowWrite``,
            # so a sandboxed command cannot rewrite settings to hot-reload a wider
            # policy after launch.
            "denyWrite": deny_write,
        },
        "credentials": {
            # Unset credential env vars for sandboxed commands. Secret FILE reads
            # are NOT listed here: a ``credentials.files`` deny applies the same OS
            # restriction as ``filesystem.denyRead`` (per the Claude Code docs), so
            # it would DUPLICATE the ``permissions.deny`` ``Read(...)`` rules — which
            # already block the secret for BOTH the native Read tool AND Bash, since
            # Claude merges permission rules into the Seatbelt profile (verified
            # live). Every duplicate path enlarges the profile Claude passes to
            # ``sandbox-exec -p`` as a single argument; a long-enough profile
            # overflows the OS arg limit (E2BIG) and breaks ALL sandboxed Bash. One
            # mechanism (``permissions.deny Read``), not two, keeps the profile small.
            "envVars": [{"name": name, "mode": "deny"} for name in scope.deny_env],
        },
    }
    allowed_domains = _allowed_domains_for_egress(scope.egress)
    if allowed_domains is not None:
        sandbox["network"] = {"allowedDomains": list(allowed_domains)}

    # Permission layer — governs EVERY tool, incl. the native file tools the OS
    # sandbox does not touch.
    #  - allow: native reads + a worktree-scoped Edit (governs Edit/Write/
    #    MultiEdit) so the agent can edit its worktree, nothing broader.
    #  - deny: one Read(secret) per secret (the single secret-deny layer; merges
    #    into the OS profile so it also covers Bash), the agent's own policy files
    #    (anti-self-modification), then egress tools. Deny beats allow, so the
    #    worktree Edit allow cannot reach a secret or a settings file.
    worktree_edit_allows = [
        f"Edit({_permission_rule_path(str(root))}/**)" for root in scope.write_roots
    ]
    self_config_denies = [
        f"Edit({_permission_rule_path(path)})" for path in self_config_paths
    ]
    settings: dict[str, Any] = {
        "sandbox": sandbox,
        "permissions": {
            "allow": list(NATIVE_READ_ALLOW_TOOLS) + worktree_edit_allows,
            "deny": _native_secret_deny_rules(scope.deny_read_files)
            + self_config_denies
            + list(_deny_tools_for_egress(scope.egress)),
        },
    }
    return settings


def build_claude_sandbox_argv(
    scope: SandboxScope,
    *,
    git_access: "GitWorktreeAccess | None" = None,
) -> list[str]:
    """Return the claude-code argv fragment that enforces *scope*.

    Emits ``--permission-mode dontAsk`` (non-yolo, unattended, deny-by-default)
    and the sandbox settings as an inline ``--settings`` JSON string, serialized
    with sorted keys and compact separators for deterministic, testable output.
    The trusted target-repo configuration is loaded normally (ADR-0034 trusted-
    repository contract); the policy this adds constrains the agent, not the repo.
    """
    settings_json = json.dumps(
        build_claude_sandbox_settings(scope, git_access=git_access),
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        "--permission-mode",
        "dontAsk",
        "--settings",
        settings_json,
    ]


class ClaudeSandboxAdapter:
    """Claude Code implementation of :class:`ProviderSandboxAdapter`."""

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        git_access = resolve_git_worktree_access(scope.working_directory)
        return build_claude_sandbox_argv(scope, git_access=git_access)


def _toml_string(value: str) -> str:
    """Encode a string accepted by Codex's TOML ``-c key=value`` parser."""
    return json.dumps(value, ensure_ascii=False)


def _toml_inline_table(entries: list[tuple[str, str]]) -> str:
    return "{ " + ", ".join(f"{_toml_string(k)} = {v}" for k, v in entries) + " }"


def _is_absolute_or_home_path(raw: str) -> bool:
    return (
        raw == "~"
        or raw.startswith("~/")
        or Path(raw).is_absolute()
        or PureWindowsPath(raw).is_absolute()
    )


def _codex_credential_files() -> tuple[str, ...]:
    """Credential stores to deny, including an operator-set Codex home."""
    paths = list(_CODEX_CREDENTIAL_FILES)
    if os.environ.get("CODEX_HOME"):
        paths.append(str(resolve_codex_home()))
    return tuple(dict.fromkeys(paths))


def _codex_permission_profile(
    scope: SandboxScope,
    git_access: GitWorktreeAccess,
) -> str:
    """Serialize the invocation-scoped Codex permission profile for *scope*."""
    workspace_rules = _toml_inline_table(
        [
            (".", _toml_string("write")),
            # Codex project policy/config is trusted at launch but immutable to
            # the running agent, preventing a session-time self-widen.
            (".codex", _toml_string("read")),
        ]
    )
    filesystem_entries = [
        # Built-in :workspace historically grants temp writes. Reduce both
        # temp aliases to read-only, then reopen only explicit workspace/Git
        # roots below. Read (rather than deny) is necessary because Git
        # canonicalizes linked-worktree paths through their temp ancestors on
        # macOS; exact secret-path denies still override this broad read.
        (":tmpdir", _toml_string("read")),
        (":slash_tmp", _toml_string("read")),
        (":workspace_roots", workspace_rules),
    ]
    filesystem_entries.extend(
        (path, _toml_string(permission))
        for path, permission in _git_worktree_filesystem_rules(git_access)
    )
    # Read-only god-view roots (read_roots that are NOT write roots): a tech-lead
    # failure investigation READS these but must not WRITE them. Codex's
    # --add-dir workspace roots are writable, so these are emitted as explicit
    # read-only filesystem entries instead, keeping writes confined to the
    # scratch worktree (#6824 R5). Denies below still override any read.
    for root in _codex_read_only_roots(scope):
        filesystem_entries.append((str(root), _toml_string("read")))
    seen_denies: set[str] = set()
    for raw in (*scope.deny_read_files, *_codex_credential_files()):
        if not _is_absolute_or_home_path(raw):
            raise SandboxUnsupportedError(
                "Codex sandbox deny_read_files entries must be absolute or "
                f"home-relative (got {raw!r})"
            )
        if raw not in seen_denies:
            filesystem_entries.append((raw, _toml_string("deny")))
            seen_denies.add(raw)

    filesystem = _toml_inline_table(filesystem_entries)
    network_enabled = "true" if scope.egress == "model+web" else "false"
    return (
        "{ "
        f"extends = {_toml_string(':workspace')}, "
        f"filesystem = {filesystem}, "
        f"network = {{ enabled = {network_enabled} }} "
        "}"
    )


def _codex_workspace_roots(scope: SandboxScope) -> list[Path]:
    """Additional WRITABLE roots (Codex ``--add-dir`` grants write).

    Only ``write_roots`` — a read-only god-view root must NOT become writable
    here; those are emitted as read-only filesystem entries instead (#6824 R5).
    """
    additional: list[Path] = []
    for root in scope.write_roots:
        if root != scope.working_directory and root not in additional:
            additional.append(root)
    return additional


def _codex_read_only_roots(scope: SandboxScope) -> list[Path]:
    """Read roots that are NOT writable (the god-view grant), emitted read-only."""
    writable = set(scope.write_roots) | {scope.working_directory}
    read_only: list[Path] = []
    for root in scope.read_roots:
        if root not in writable and root not in read_only:
            read_only.append(root)
    return read_only


def build_codex_sandbox_argv(
    scope: SandboxScope,
    *,
    git_access: GitWorktreeAccess | None = None,
) -> list[str]:
    """Return Codex global argv enforcing *scope* with a native profile.

    These arguments must appear before the ``exec`` subcommand because ``-a``
    is a root-command option in current Codex releases. Unknown permission-
    profile keys fail under ``--strict-config``; this makes pre-0.138 Codex
    versions fail loud instead of silently dropping the boundary.
    """
    if git_access is None:
        validate_codex_permission_profile_compatibility(scope.working_directory)
        resolved_git_access = resolve_git_worktree_access(scope.working_directory)
    else:
        resolved_git_access = git_access
    argv = [
        "--strict-config",
        "-a",
        "never",
        "-C",
        str(scope.working_directory),
    ]
    for root in _codex_workspace_roots(scope):
        argv.extend(["--add-dir", str(root)])

    profile = _codex_permission_profile(scope, resolved_git_access)
    deny_env_names = list(dict.fromkeys((*scope.deny_env, *_CODEX_CREDENTIAL_ENV)))
    deny_env = json.dumps(deny_env_names, ensure_ascii=False, separators=(",", ":"))
    web_search = "live" if scope.egress == "model+web" else "disabled"
    argv.extend(
        [
            "-c",
            f"default_permissions={_toml_string(CODEX_PERMISSION_PROFILE)}",
            "-c",
            f"permissions.{CODEX_PERMISSION_PROFILE}={profile}",
            "-c",
            f"shell_environment_policy.exclude={deny_env}",
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
            "-c",
            f"web_search={_toml_string(web_search)}",
        ]
    )
    return argv


class CodexSandboxAdapter:
    """Codex permission-profile implementation of :class:`ProviderSandboxAdapter`."""

    def apply_scope(self, scope: SandboxScope) -> list[str]:
        return build_codex_sandbox_argv(scope)
