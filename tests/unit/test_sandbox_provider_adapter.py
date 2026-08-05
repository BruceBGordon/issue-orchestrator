"""Unit tests for the provider sandbox adapters (ADR-0034).

Covers both provider translations and their ``build_command`` integration,
including the byte-for-byte regression guard when no scope is supplied.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

from issue_orchestrator.domain.sandbox_scope import (
    SandboxScope,
    SandboxUnsupportedError,
)
from issue_orchestrator.execution.agent_runner_providers.claude import (
    ClaudeCodeProvider,
)
from issue_orchestrator.execution.agent_runner_providers.codex import CodexProvider
from issue_orchestrator.execution.agent_runner_providers.sandbox import (
    MODEL_API_DOMAINS,
    CODEX_PERMISSION_PROFILE,
    GitWorktreeAccess,
    ClaudeSandboxAdapter,
    CodexSandboxAdapter,
    ProviderSandboxAdapter,
    build_claude_sandbox_argv,
    build_claude_sandbox_settings,
    build_codex_sandbox_argv,
    resolve_git_worktree_access,
    validate_codex_permission_profile_compatibility,
)
from issue_orchestrator.execution.agent_runner_providers import (
    sandbox as sandbox_module,
)


def _scope(egress: str = "model-only") -> SandboxScope:
    return SandboxScope(
        working_directory=Path("/wt/issue-42"),
        read_roots=(Path("/wt/issue-42"),),
        write_roots=(Path("/wt/issue-42"),),
        egress=egress,  # type: ignore[arg-type]
        deny_env=("GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY"),
        deny_read_files=("~/.ssh", "~/.issue-orchestrator"),
    )


# ---------------------------------------------------------------------------
# Claude settings translation
# ---------------------------------------------------------------------------


def test_claude_settings_enable_real_sandbox() -> None:
    settings = build_claude_sandbox_settings(_scope())
    sandbox = settings["sandbox"]
    assert sandbox["enabled"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["allowUnsandboxedCommands"] is False


def test_claude_settings_pin_sandbox_weakener_booleans_false() -> None:
    # Pinned false so the session cannot be softened out from under the boundary.
    sandbox = build_claude_sandbox_settings(_scope())["sandbox"]
    assert sandbox["allowAppleEvents"] is False
    assert sandbox["enableWeakerNetworkIsolation"] is False
    assert sandbox["enableWeakerNestedSandbox"] is False


def test_claude_settings_bound_read_and_write_roots() -> None:
    settings = build_claude_sandbox_settings(_scope())
    fs = settings["sandbox"]["filesystem"]
    assert fs["allowRead"] == ["/wt/issue-42"]
    assert fs["allowWrite"] == ["/wt/issue-42"]


def test_claude_tech_lead_reads_god_view_but_writes_only_worktree() -> None:
    # R5 (#6824): a tech-lead read-broad/write-narrow scope maps directly to
    # Claude allowRead (worktree + god-view) vs allowWrite (worktree only).
    scope = SandboxScope(
        working_directory=Path("/wt/repo-tech-lead-42-abc"),
        read_roots=(
            Path("/wt/repo-tech-lead-42-abc"),
            Path("/repo"),
            Path("/repo/.issue-orchestrator/state"),
        ),
        write_roots=(Path("/wt/repo-tech-lead-42-abc"),),
        egress="model-only",
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )
    fs = build_claude_sandbox_settings(scope)["sandbox"]["filesystem"]
    assert fs["allowRead"] == [
        "/wt/repo-tech-lead-42-abc",
        "/repo",
        "/repo/.issue-orchestrator/state",
    ]
    assert fs["allowWrite"] == ["/wt/repo-tech-lead-42-abc"]  # writes never widen


def test_claude_settings_deny_read_home_and_reallow_roots() -> None:
    # Reads are OPEN by default; the boundary is denyRead ~/ + re-allow roots.
    fs = build_claude_sandbox_settings(_scope())["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["~/"]
    assert fs["allowRead"] == ["/wt/issue-42"]


def test_claude_settings_fail_closed_deny_secret_files() -> None:
    # Secret FILES are fail-closed via ONE mechanism: a ``permissions.deny``
    # ``Read(path)`` rule, which blocks the native Read tool AND (because Claude
    # merges permission rules into the OS sandbox profile) Bash. A separate
    # ``credentials.files`` deny would apply the same restriction and double-count
    # the path in the Seatbelt profile (E2BIG risk), so it is NOT emitted.
    settings = build_claude_sandbox_settings(_scope())
    deny = settings["permissions"]["deny"]
    assert "Read(~/.ssh)" in deny
    assert "Read(~/.issue-orchestrator)" in deny
    assert "files" not in settings["sandbox"]["credentials"], (
        "credential FILE reads are covered by permissions.deny Read; a "
        "credentials.files entry would duplicate the path in the sandbox profile"
    )


def test_claude_settings_deny_credentials_as_objects() -> None:
    settings = build_claude_sandbox_settings(_scope())
    env_vars = settings["sandbox"]["credentials"]["envVars"]
    assert {"name": "GITHUB_TOKEN", "mode": "deny"} in env_vars
    assert {"name": "AWS_SECRET_ACCESS_KEY", "mode": "deny"} in env_vars


def test_model_only_egress_allows_only_model_api_not_github() -> None:
    # A broad github wildcard is an exfil risk; model-only pre-allows only the
    # model API host for Bash. (Bash reaching it is moot; it documents the floor.)
    domains = build_claude_sandbox_settings(_scope("model-only"))["sandbox"]["network"][
        "allowedDomains"
    ]
    assert domains == list(MODEL_API_DOMAINS)
    assert not any("github" in d for d in domains)


def test_model_only_egress_denies_web_search_and_curl() -> None:
    settings = build_claude_sandbox_settings(_scope("model-only"))
    deny = settings["permissions"]["deny"]
    assert "WebSearch" in deny
    assert "WebFetch" in deny
    assert "Bash(curl *)" in deny


def test_model_web_egress_has_no_network_restriction_but_keeps_secret_denies() -> None:
    # model+web drops the OS network allowlist and the WEB tool denies, but the
    # permission block still carries the read/edit allow-list and the native secret
    # denies (secrets are fail-closed regardless of egress posture).
    settings = build_claude_sandbox_settings(_scope("model+web"))
    assert "network" not in settings["sandbox"]
    permissions = settings["permissions"]
    assert permissions["allow"] == ["Read", "Grep", "Glob", "Edit(//wt/issue-42/**)"]
    assert "Read(~/.ssh)" in permissions["deny"]
    assert "WebSearch" not in permissions["deny"]
    assert "WebFetch" not in permissions["deny"]


def test_none_egress_emits_explicit_empty_allowlist() -> None:
    # "none" blocks Bash network entirely: the key is PRESENT with an empty
    # list (an omitted key would instead add no restriction).
    settings = build_claude_sandbox_settings(_scope("none"))
    assert settings["sandbox"]["network"]["allowedDomains"] == []
    assert "WebSearch" in settings["permissions"]["deny"]


# ---------------------------------------------------------------------------
# Native file-tool permission layer (the OS sandbox binds Bash only)
# ---------------------------------------------------------------------------


def test_native_read_and_worktree_edit_allowed() -> None:
    # dontAsk runs only allow-listed tools: native reads + a worktree-scoped Edit
    # (which governs the file-editing tools). No broad native edit allow.
    allow = build_claude_sandbox_settings(_scope())["permissions"]["allow"]
    assert allow == ["Read", "Grep", "Glob", "Edit(//wt/issue-42/**)"]


def test_native_file_tools_deny_each_secret_path() -> None:
    # Exactly ONE rule per secret path: ``Read(path)``. It covers all reading tools
    # (Read/Grep/Glob) AND — via the permission->sandbox merge — Bash; per-tool
    # Grep/Glob/Write rules are ineffective per the live CLI. NO ``Edit(secret)``
    # rule: writes are already worktree-confined, so a secret outside the worktree
    # is unwritable regardless. NO ``/**`` variant: a bare Read(<dir>) already
    # denies everything beneath it. Each redundant rule enlarges the single
    # ``sandbox-exec -p`` argument and risks E2BIG, which breaks all sandboxed Bash.
    deny = build_claude_sandbox_settings(_scope())["permissions"]["deny"]
    assert not any(e.startswith(("Write(", "Grep(", "Glob(", "Edit(~")) for e in deny)
    assert not any(
        e.endswith("/**)") and not e.startswith("Edit(//wt/") for e in deny
    ), f"no secret deny should carry a /** glob variant: {deny}"
    for path in ("~/.ssh", "~/.issue-orchestrator"):
        assert f"Read({path})" in deny, f"missing Read({path})"
        assert f"Edit({path})" not in deny, (
            f"redundant Edit({path}) (writes are worktree-confined)"
        )


def test_anti_self_modification_denies_policy_files() -> None:
    # The agent may write its worktree but not its own policy: denyWrite (Bash) +
    # Edit deny (native) on the two settings files; deny wins over the worktree
    # allow, so a session cannot hot-reload a wider policy after launch.
    settings = build_claude_sandbox_settings(_scope())
    deny_write = settings["sandbox"]["filesystem"]["denyWrite"]
    deny = settings["permissions"]["deny"]
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        assert f"/wt/issue-42/{rel}" in deny_write
        assert f"Edit(//wt/issue-42/{rel})" in deny


def test_native_deny_precedes_egress_deny() -> None:
    # Deterministic ordering: native secret denies first, egress tool denies last.
    deny = build_claude_sandbox_settings(_scope("model-only"))["permissions"]["deny"]
    assert deny.index("Read(~/.ssh)") < deny.index("WebSearch")


def test_absolute_secret_path_uses_double_slash_permission_spec() -> None:
    # Read/Edit permission specifiers use // for absolute paths, which differs
    # from the single-slash sandbox.filesystem convention. An operator/test path
    # outside home must be doubled or it would read as project-relative.
    scope = SandboxScope(
        working_directory=Path("/wt/x"),
        read_roots=(Path("/wt/x"),),
        write_roots=(Path("/wt/x"),),
        egress="model-only",
        deny_env=(),
        deny_read_files=("/var/folders/abc/planted-secret.txt",),
    )
    deny = build_claude_sandbox_settings(scope)["permissions"]["deny"]
    assert "Read(//var/folders/abc/planted-secret.txt)" in deny


def test_generated_deny_rule_count_is_bounded() -> None:
    # E2BIG guard: Claude passes the whole sandbox profile as a single
    # ``sandbox-exec -p`` argument, and its OWN built-in default deny list already
    # sits near the OS arg limit. OUR contribution must stay tiny and, crucially,
    # BOUNDED — O(number of secret roots), never O(files) — so a large scope (e.g.
    # the tech-lead's evidence map) can't balloon the profile. Assert the total
    # emitted deny/allow rule count is small and does not scale with glob expansion.
    #
    # A big deny_read_files list of DIRECTORIES must still yield one rule each (no
    # per-file, no ``/**`` fan-out).
    big = SandboxScope(
        working_directory=Path("/wt/x"),
        read_roots=(Path("/wt/x"),),
        write_roots=(Path("/wt/x"),),
        egress="model-only",
        deny_env=(),
        deny_read_files=tuple(f"~/.secret{i}" for i in range(50)),
    )
    settings = build_claude_sandbox_settings(big)
    deny = settings["permissions"]["deny"]
    # One Read() per secret + a couple of self-config Edit denies + egress denies.
    assert len(deny) <= len(big.deny_read_files) + 8, (
        f"deny rule count unbounded: {len(deny)}"
    )
    # No rule may fan out into globbed subpaths (what triggers Claude's filesystem
    # expansion and the E2BIG blowup).
    assert not any(r.endswith("/**)") and not r.startswith("Edit(//wt/") for r in deny)
    # No duplicate secret coverage in the OS sandbox: credentials.files is absent.
    assert "files" not in settings["sandbox"]["credentials"]


# ---------------------------------------------------------------------------
# Claude argv translation
# ---------------------------------------------------------------------------


def test_claude_argv_uses_dontask_not_bypass() -> None:
    argv = build_claude_sandbox_argv(_scope())
    assert argv[:2] == ["--permission-mode", "dontAsk"]
    assert "bypassPermissions" not in argv


def test_claude_argv_carries_inline_settings_json() -> None:
    argv = build_claude_sandbox_argv(_scope())
    settings_idx = argv.index("--settings")
    payload = json.loads(argv[settings_idx + 1])
    assert payload["sandbox"]["enabled"] is True
    assert payload["sandbox"]["filesystem"]["allowWrite"] == ["/wt/issue-42"]


def test_claude_adapter_implements_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "resolve_git_worktree_access",
        lambda _worktree: _git_access(),
    )
    adapter = ClaudeSandboxAdapter()
    assert isinstance(adapter, ProviderSandboxAdapter)
    assert adapter.apply_scope(_scope()) == build_claude_sandbox_argv(
        _scope(),
        git_access=_git_access(),
    )


# ---------------------------------------------------------------------------
# Codex permission-profile translation
# ---------------------------------------------------------------------------


def _git_access() -> GitWorktreeAccess:
    common_dir = Path("/repo/.git")
    return GitWorktreeAccess(
        git_dir=common_dir / "worktrees" / "issue-42",
        common_dir=common_dir,
        head_ref=common_dir / "refs" / "heads" / "42-fix",
    )


def test_claude_settings_grant_only_current_linked_worktree_git_writes() -> None:
    settings = build_claude_sandbox_settings(
        _scope(),
        git_access=_git_access(),
    )
    filesystem = settings["sandbox"]["filesystem"]

    assert "/repo/.git" in filesystem["allowRead"]
    assert "/repo/.git" not in filesystem["allowWrite"]
    for path in (
        "/repo/.git/worktrees/issue-42",
        "/repo/.git/objects",
        "/repo/.git/refs/heads/42-fix",
        "/repo/.git/refs/heads/42-fix.lock",
        "/repo/.git/logs/refs/heads/42-fix",
        "/repo/.git/logs/refs/heads/42-fix.lock",
    ):
        assert path in filesystem["allowWrite"]

    for path in (
        "/repo/.git/worktrees/issue-42/HEAD",
        "/repo/.git/worktrees/issue-42/commondir",
        "/repo/.git/worktrees/issue-42/gitdir",
        "/repo/.git/worktrees/issue-42/config",
        "/repo/.git/worktrees/issue-42/config.worktree",
        "/repo/.git/objects/info",
        "/repo/.git/objects/pack",
    ):
        assert path in filesystem["denyWrite"]

    assert "/repo/.git/config" not in filesystem["allowWrite"]
    assert "/repo/.git/refs/heads/main" not in filesystem["allowWrite"]


def _codex_argv(scope: SandboxScope | None = None) -> list[str]:
    return build_codex_sandbox_argv(scope or _scope(), git_access=_git_access())


def _codex_config_overrides(argv: list[str]) -> dict[str, object]:
    overrides: dict[str, object] = {}
    for idx, token in enumerate(argv):
        if token != "-c":
            continue
        key, raw = argv[idx + 1].split("=", 1)
        overrides[key] = tomllib.loads(f"value = {raw}")["value"]
    return overrides


def test_codex_adapter_implements_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "resolve_git_worktree_access",
        lambda _worktree: _git_access(),
    )
    adapter = CodexSandboxAdapter()
    assert isinstance(adapter, ProviderSandboxAdapter)
    assert adapter.apply_scope(_scope()) == _codex_argv()


def test_codex_argv_pins_cwd_approval_and_bounded_roots() -> None:
    scope = SandboxScope(
        working_directory=Path("/wt/issue-42"),
        read_roots=(Path("/wt/issue-42"), Path("/evidence/run-7")),
        write_roots=(Path("/wt/issue-42"), Path("/scratch/issue-42")),
        egress="model-only",
        deny_env=("GITHUB_TOKEN",),
        deny_read_files=("~/.ssh",),
    )
    argv = build_codex_sandbox_argv(scope, git_access=_git_access())

    assert argv[argv.index("-a") + 1] == "never"
    assert argv[argv.index("-C") + 1] == "/wt/issue-42"
    assert "--strict-config" in argv
    # R5 (#6824): --add-dir grants WRITE, so only write roots appear here. The
    # read-only god-view root (/evidence/run-7) is NOT writable...
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--add-dir"] == [
        "/scratch/issue-42",
    ]
    # ...it is emitted as an explicit read-only filesystem entry in the profile.
    profile = argv[argv.index("-c") + 1 :]
    profile_text = " ".join(profile)
    assert '"/evidence/run-7" = "read"' in profile_text
    assert "-s" not in argv
    assert "--sandbox" not in argv
    assert "--profile" not in argv


def test_codex_profile_denies_secrets_temp_writes_and_self_modification() -> None:
    overrides = _codex_config_overrides(_codex_argv())
    assert overrides["default_permissions"] == CODEX_PERMISSION_PROFILE
    profile = overrides[f"permissions.{CODEX_PERMISSION_PROFILE}"]
    assert isinstance(profile, dict)
    assert profile["extends"] == ":workspace"
    filesystem = profile["filesystem"]
    assert filesystem[":tmpdir"] == "read"
    assert filesystem[":slash_tmp"] == "read"
    assert filesystem[":workspace_roots"] == {".": "write", ".codex": "read"}
    assert filesystem["~/.ssh"] == "deny"
    assert filesystem["~/.issue-orchestrator"] == "deny"
    assert filesystem["~/.codex"] == "deny"


def test_codex_profile_grants_only_current_linked_worktree_git_writes() -> None:
    overrides = _codex_config_overrides(_codex_argv())
    profile = overrides[f"permissions.{CODEX_PERMISSION_PROFILE}"]
    filesystem = profile["filesystem"]

    assert filesystem["/repo/.git"] == "read"
    assert filesystem["/repo/.git/worktrees/issue-42"] == "write"
    assert filesystem["/repo/.git/worktrees/issue-42/HEAD"] == "read"
    assert filesystem["/repo/.git/worktrees/issue-42/commondir"] == "read"
    assert filesystem["/repo/.git/worktrees/issue-42/gitdir"] == "read"
    assert filesystem["/repo/.git/worktrees/issue-42/config.worktree"] == "read"
    assert filesystem["/repo/.git/objects"] == "write"
    assert filesystem["/repo/.git/objects/info"] == "read"
    assert filesystem["/repo/.git/objects/pack"] == "read"
    assert filesystem["/repo/.git/refs/heads/42-fix"] == "write"
    assert filesystem["/repo/.git/refs/heads/42-fix.lock"] == "write"
    assert filesystem["/repo/.git/logs/refs/heads/42-fix"] == "write"
    assert "/repo/.git/refs/heads/main" not in filesystem
    assert filesystem.get("/repo/.git/config") != "write"


def test_codex_profile_supports_detached_reviewer_worktree() -> None:
    linked = _git_access()
    detached = GitWorktreeAccess(
        git_dir=linked.git_dir,
        common_dir=linked.common_dir,
        head_ref=None,
    )

    argv = build_codex_sandbox_argv(_scope(), git_access=detached)
    profile = _codex_config_overrides(argv)[f"permissions.{CODEX_PERMISSION_PROFILE}"]
    filesystem = profile["filesystem"]

    assert filesystem["/repo/.git/worktrees/issue-42/HEAD"] == "write"
    assert filesystem["/repo/.git/worktrees/issue-42/HEAD.lock"] == "write"
    assert not any(path.startswith("/repo/.git/refs/heads/") for path in filesystem)


def test_codex_profile_grants_only_specific_non_linked_git_writes() -> None:
    common_dir = Path("/repo/.git")
    access = GitWorktreeAccess(
        git_dir=common_dir,
        common_dir=common_dir,
        head_ref=common_dir / "refs" / "heads" / "main",
    )

    argv = build_codex_sandbox_argv(_scope(), git_access=access)
    profile = _codex_config_overrides(argv)[f"permissions.{CODEX_PERMISSION_PROFILE}"]
    filesystem = profile["filesystem"]

    assert filesystem["/repo/.git"] == "read"
    for path in (
        "/repo/.git/index",
        "/repo/.git/index.lock",
        "/repo/.git/COMMIT_EDITMSG",
        "/repo/.git/COMMIT_EDITMSG.lock",
        "/repo/.git/logs/HEAD",
        "/repo/.git/logs/HEAD.lock",
        "/repo/.git/refs/heads/main",
        "/repo/.git/refs/heads/main.lock",
        "/repo/.git/logs/refs/heads/main",
        "/repo/.git/logs/refs/heads/main.lock",
    ):
        assert filesystem[path] == "write"
    assert filesystem.get("/repo/.git/config") != "write"
    assert filesystem.get("/repo/.git/HEAD") != "write"


def test_codex_resolves_linked_worktree_git_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Sandbox Test",
            "-c",
            "user.email=sandbox@example.invalid",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            "base",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "issue-42", str(worktree)],
        cwd=repo,
        check=True,
    )

    access = resolve_git_worktree_access(worktree)

    assert access.common_dir == (repo / ".git").resolve()
    assert access.git_dir.parent == (repo / ".git" / "worktrees").resolve()
    assert access.head_ref == (repo / ".git" / "refs" / "heads" / "issue-42").resolve()


def test_codex_fails_loud_when_working_directory_is_not_git() -> None:
    with pytest.raises(SandboxUnsupportedError, match="not a Git worktree"):
        build_codex_sandbox_argv(_scope())


def test_codex_fails_loud_when_legacy_project_sandbox_disables_profile(
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    config = tmp_path / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('sandbox_mode = "workspace-write"\n', encoding="utf-8")

    with pytest.raises(
        SandboxUnsupportedError,
        match=r"sandbox_mode.*remove that key",
    ):
        validate_codex_permission_profile_compatibility(tmp_path)


def test_codex_fails_loud_for_legacy_sandbox_in_ancestor_project_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    repo = tmp_path / "repo"
    nested = repo / "packages" / "worker"
    (repo / ".git").mkdir(parents=True)
    nested.mkdir(parents=True)
    config = repo / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")

    with pytest.raises(
        SandboxUnsupportedError,
        match=rf"sandbox_mode.*{re.escape(str(config))}",
    ):
        validate_codex_permission_profile_compatibility(nested)


def test_codex_ignores_config_above_documented_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    outside_config = tmp_path / ".codex" / "config.toml"
    outside_config.parent.mkdir()
    outside_config.write_text('sandbox_mode = "danger-full-access"\n', encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    validate_codex_permission_profile_compatibility(repo)


def test_codex_rejects_project_root_markers_that_can_escape_git_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    (codex_home / "config.toml").write_text(
        'project_root_markers = [".hg"]\n', encoding="utf-8"
    )
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    with pytest.raises(
        SandboxUnsupportedError,
        match=r"project_root_markers.*omits '\.git'",
    ):
        validate_codex_permission_profile_compatibility(repo)


@pytest.mark.parametrize(
    ("egress", "network_enabled", "web_search"),
    [
        ("none", False, "disabled"),
        ("model-only", False, "disabled"),
        ("model+web", True, "live"),
    ],
)
def test_codex_profile_maps_egress(
    egress: str, network_enabled: bool, web_search: str
) -> None:
    overrides = _codex_config_overrides(_codex_argv(_scope(egress)))
    profile = overrides[f"permissions.{CODEX_PERMISSION_PROFILE}"]
    assert profile["network"]["enabled"] is network_enabled
    assert overrides["web_search"] == web_search


def test_codex_profile_excludes_denied_environment_variables() -> None:
    overrides = _codex_config_overrides(_codex_argv())
    assert overrides["shell_environment_policy.exclude"] == [
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CODEX_HOME",
    ]
    assert overrides["shell_environment_policy.ignore_default_excludes"] is False


def test_codex_profile_denies_custom_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "codex-home"
    custom_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom_home))

    overrides = _codex_config_overrides(_codex_argv())
    profile = overrides[f"permissions.{CODEX_PERMISSION_PROFILE}"]

    assert profile["filesystem"][str(custom_home.resolve())] == "deny"


def test_codex_rejects_relative_secret_deny_path() -> None:
    scope = SandboxScope(
        working_directory=Path("/wt/issue-42"),
        read_roots=(Path("/wt/issue-42"),),
        write_roots=(Path("/wt/issue-42"),),
        egress="model-only",
        deny_env=(),
        deny_read_files=("relative-secret.txt",),
    )
    with pytest.raises(SandboxUnsupportedError, match="absolute or home-relative"):
        build_codex_sandbox_argv(scope, git_access=_git_access())


def test_codex_build_command_places_scope_before_exec_and_ignores_yolo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox_module,
        "resolve_git_worktree_access",
        lambda _worktree: _git_access(),
    )
    cmd = CodexProvider().build_command(
        prompt="task",
        model=None,
        execution_mode="exec",
        approval_mode="yolo",
        sandbox="danger-full-access",
        sandbox_scope=_scope(),
    )
    exec_index = cmd.index("exec")
    assert cmd.index("-a") < exec_index
    assert cmd[cmd.index("-a") + 1] == "never"
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    assert "danger-full-access" not in cmd
    assert cmd[-1] == "task"


# ---------------------------------------------------------------------------
# Claude build_command integration + byte-for-byte OFF regression guard
# ---------------------------------------------------------------------------


def test_build_command_none_scope_is_byte_for_byte_unchanged() -> None:
    provider = ClaudeCodeProvider()
    without_param = provider.build_command(
        prompt="task",
        model="sonnet",
        permission_mode="bypassPermissions",
        system_prompt="SP",
    )
    with_none = provider.build_command(
        prompt="task",
        model="sonnet",
        sandbox_scope=None,
        permission_mode="bypassPermissions",
        system_prompt="SP",
    )
    assert without_param == with_none
    assert "--permission-mode" in with_none
    assert with_none[with_none.index("--permission-mode") + 1] == "bypassPermissions"


def test_scope_argv_replaces_bypass_with_dontask() -> None:
    # The pure translation (the provider launch path is covered in the
    # launch-wiring suite).
    argv = build_claude_sandbox_argv(_scope())
    assert "bypassPermissions" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--settings" in argv
