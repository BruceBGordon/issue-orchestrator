"""Regression tests for issue #6686.

Saving one settings tab must not rewrite the whole ``main.yaml`` through the
lossy full-config serializer (``Config.to_dict``). The old path dropped every
operational section that ``to_dict`` does not represent -- most notably the
``repo.github`` auth subsection -- and reordered/pruned the rest.

The fix builds a field-granular patch plan and writes only the settings-owned
``yaml_path`` keys whose value actually changed. These tests cover both sides of
that boundary:

* the persistence-policy owner (``build_save_plan`` -> ``SettingsSavePlan``,
  composed with ``save_config_document_patch`` via ``plan.entries``), and
* the settings HTTP handler (``update_settings``) that wires them together.

They pin the two invariants the field-granular plan protects: an unedited
``${VAR}`` sibling in a changed tab keeps its literal form (never its expanded
value), and a no-op save leaves the file byte-for-byte untouched.
"""

from __future__ import annotations

import types

import pytest
import yaml

from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_document_patch import save_config_document_patch
from issue_orchestrator.infra.doctor import DoctorResult
from issue_orchestrator.infra.settings_schema import (
    apply_to,
    build_save_plan,
    from_config,
)


# A config that mixes settings-visible fields (concurrency, review, merge queue,
# hooks, validation) with operational fields the settings form does NOT own
# (repo.github auth, write_verify, rate_limit, scopes). All of it must survive a
# targeted settings save.
_OPERATIONAL_CONFIG = """# Issue Orchestrator Configuration
# Hand-authored operational config -- must survive settings saves.

repo:
  name: owner/repo  # inline comments are part of the operator-owned document
  github:
    # Keep the repo-scoped credential locator documented here.
    token_env: MY_GH_TOKEN
    keyring_service: io-gh
    app:
      client_id: Iv23example
      app_id: "4250697"
      installation_id: "145305179"
      private_key_path: ~/.config/issue-orchestrator/github-apps/bot.pem
    api_url: https://ghe.example.com/api/v3
    write_verify:
      enabled: true
    rate_limit:
      min_remaining: 100
    audit:
      enabled: true
    required_scopes:
      - repo
      - read:org

merge_queue:
  enabled: true
  provider: github
  enqueue_after: code-reviewed

hooks:
  ai_gate:
    interval_days: 14

validation:
  publish:
    cmd: ./scripts/validate-pr.sh
    dirty_check: unstaged
  quick:
    cmd: ./scripts/validate-fast.sh

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-local-loop

execution:
  concurrency:
    max_concurrent_sessions: 2
    session_timeout_minutes: 45

agents:
  agent:test:
    prompt: prompt.txt
    provider: "codex"
    provider_args:
      reasoning_effort: 'xhigh'
    initial_prompt: >-
      Keep this hand-wrapped scalar exactly as the operator wrote it when an
      unrelated setting changes.
...
"""


@pytest.fixture
def loaded_config(tmp_path):
    """Write the operational config to disk and return a loaded Config."""
    (tmp_path / "prompt.txt").write_text("test prompt")
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(_OPERATIONAL_CONFIG)
    return Config.load(cfg_path), cfg_path


def _save_one_tab_change(config: Config, cfg_path, mutate) -> dict:
    """Simulate a settings save: apply a mutation, patch the YAML doc, reload.

    ``mutate`` receives the ``from_config`` tab dict and edits one field, the
    same way the settings POST handler does before persisting. Persistence goes
    through the real field-granular owner (``build_save_plan`` -> ``plan.entries``)
    so these tests exercise the same seam the route does, and the write is
    skipped for an empty (no-op) plan.
    """
    snapshot = from_config(config)
    tabs = from_config(config)
    mutate(tabs)
    apply_to(tabs, config)
    plan = build_save_plan(snapshot, tabs)
    if not plan.is_empty:
        save_config_document_patch(config, plan.entries)
    return yaml.safe_load(cfg_path.read_text())


def test_partial_save_preserves_repo_github_and_operational_sections(loaded_config):
    """The core acceptance scenario: a one-tab edit keeps unrelated config."""
    config, cfg_path = loaded_config

    saved = _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["concurrency"], "max_concurrent_sessions", 7),
    )

    # The edited field is persisted.
    assert saved["execution"]["concurrency"]["max_concurrent_sessions"] == 7

    # repo.github auth survives in full -- this is what to_dict() dropped.
    github = saved["repo"]["github"]
    assert github["token_env"] == "MY_GH_TOKEN"
    assert github["keyring_service"] == "io-gh"
    assert github["app"]["client_id"] == "Iv23example"
    assert github["app"]["app_id"] == "4250697"
    assert github["app"]["installation_id"] == "145305179"
    assert (
        github["app"]["private_key_path"]
        == "~/.config/issue-orchestrator/github-apps/bot.pem"
    )
    assert github["api_url"] == "https://ghe.example.com/api/v3"
    assert github["write_verify"]["enabled"] is True
    assert github["rate_limit"]["min_remaining"] == 100
    assert github["audit"]["enabled"] is True
    assert github["required_scopes"] == ["repo", "read:org"]

    # Other operational sections survive untouched.
    assert saved["merge_queue"]["enabled"] is True
    assert saved["merge_queue"]["enqueue_after"] == "code-reviewed"
    assert saved["hooks"]["ai_gate"]["interval_days"] == 14
    assert saved["validation"]["publish"]["cmd"] == "./scripts/validate-pr.sh"
    assert saved["validation"]["publish"]["dirty_check"] == "unstaged"
    assert saved["review"]["exchange"]["mode"] == "via-local-loop"


def test_saved_config_reloads_with_github_auth_intact(loaded_config):
    """A saved settings edit round-trips: repo.github still loads afterwards."""
    config, cfg_path = loaded_config

    _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["concurrency"], "max_concurrent_sessions", 9),
    )

    reloaded = Config.load(cfg_path)
    assert reloaded.github_token_env == "MY_GH_TOKEN"
    assert reloaded.github_keyring_service == "io-gh"
    assert reloaded.github_app_client_id == "Iv23example"
    assert reloaded.github_app_installation_id == "145305179"
    assert reloaded.github_required_scopes == ["repo", "read:org"]
    assert reloaded.max_concurrent_sessions == 9


def test_partial_save_preserves_leading_comment_header(loaded_config):
    """The file's top comment block is re-emitted, not stripped."""
    config, cfg_path = loaded_config

    _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["concurrency"], "max_concurrent_sessions", 4),
    )

    text = cfg_path.read_text()
    assert text.startswith("# Issue Orchestrator Configuration")
    assert "# Hand-authored operational config" in text


def test_single_field_save_preserves_entire_yaml_presentation(tmp_path):
    """A one-field edit produces a one-line diff in a hand-authored config."""
    (tmp_path / "prompt.txt").write_text("p")
    cfg_path = tmp_path / "main.yaml"
    original = """# Porchpin configuration
repo:
  name: porchpin/porchpin
  github:
    # Keep the repo-scoped credential locator documented here.
    keyring_username: "${USER}"

agents:
  agent:backend:
    prompt: prompt.txt
    provider: "claude-code"
    initial_prompt: "Keep this deliberately long quoted string on one line when an unrelated setting changes."
    provider_args: &shared_args
      effort: "xhigh"
  agent:reviewer:
    prompt: prompt.txt
    provider_args: *shared_args

execution:
  concurrency:
    # Tune the worker cap without rewriting this hand-authored document.
    max_concurrent_sessions: 2
    session_timeout_minutes: 120

filtering:
  exclude_labels: [ "proposed-tech-lead",  "deferred" ] # keep custom flow spacing
""".rstrip("\n")
    cfg_path.write_text(original)
    config = Config.load(cfg_path)

    _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["concurrency"], "max_concurrent_sessions", 3),
    )

    assert cfg_path.read_text() == original.replace(
        "    max_concurrent_sessions: 2\n",
        "    max_concurrent_sessions: 3\n",
    )
    assert not cfg_path.read_bytes().endswith(b"\n")


def test_partial_save_does_not_expand_env_var_references(tmp_path, monkeypatch):
    """``${VAR}`` references are preserved verbatim; no secret is leaked.

    ``Config.load`` expands ``${VAR}`` in memory, but the save re-reads the raw
    on-disk file, so the literal reference -- not the resolved secret -- must
    remain in the saved YAML.
    """
    monkeypatch.setenv("SECRET_GH_TOKEN", "super-secret-value")
    (tmp_path / "prompt.txt").write_text("p")
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(
        "repo:\n"
        "  name: owner/repo\n"
        "  github:\n"
        "    token: ${SECRET_GH_TOKEN}\n"
        "execution:\n"
        "  concurrency:\n"
        "    max_concurrent_sessions: 2\n"
        "agents:\n"
        "  agent:test:\n"
        "    prompt: prompt.txt\n"
    )
    config = Config.load(cfg_path)

    _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["concurrency"], "max_concurrent_sessions", 6),
    )

    text = cfg_path.read_text()
    # The literal reference is preserved verbatim; no expansion leaks a secret.
    assert "${SECRET_GH_TOKEN}" in text
    assert "super-secret-value" not in text
    assert yaml.safe_load(text)["repo"]["github"]["token"] == "${SECRET_GH_TOKEN}"


def test_save_plan_emits_only_owned_changed_paths():
    """The save plan contains only changed settings-owned paths."""
    # Build tabs from a minimal config, then flip one owned field.
    config = Config()
    snapshot = from_config(config)
    submitted = from_config(config)
    submitted["concurrency"].max_concurrent_sessions = 5

    plan = build_save_plan(snapshot, submitted)

    assert [(entry.yaml_path, entry.value) for entry in plan.entries] == [
        ("execution.concurrency.max_concurrent_sessions", 5)
    ]


def test_save_plan_entry_reverses_list_ui_transform():
    """Comma-separated display values become list-valued persistence entries."""
    config = Config()
    snapshot = from_config(config)
    submitted = from_config(config)
    submitted["filtering"].exclude_labels = "test-data, skip"

    plan = build_save_plan(snapshot, submitted)

    assert [(entry.yaml_path, entry.value) for entry in plan.entries] == [
        ("filtering.exclude_labels", ["test-data", "skip"])
    ]


def test_save_config_document_patch_starts_from_empty_when_file_missing(tmp_path):
    """A missing file is treated as an empty document, not an error."""
    config = Config()
    target = tmp_path / "new.yaml"

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.max_concurrent_sessions",
                value=3,
            ),
        ),
        path=target,
    )

    assert (
        yaml.safe_load(target.read_text())["execution"]["concurrency"][
            "max_concurrent_sessions"
        ]
        == 3
    )


def test_save_config_document_patch_rejects_non_mapping_document(tmp_path):
    """A YAML file whose root is not a mapping fails fast."""
    config = Config()
    target = tmp_path / "list.yaml"
    target.write_text("- a\n- b\n")

    with pytest.raises(ValueError, match="not a mapping"):
        save_config_document_patch(
            config,
            (types.SimpleNamespace(yaml_path="execution.concurrency.max", value=3),),
            path=target,
        )


def test_save_config_document_patch_requires_a_path():
    """No path and no config_path is a hard error."""
    config = Config()
    with pytest.raises(ValueError, match="No path specified"):
        save_config_document_patch(
            config,
            (types.SimpleNamespace(yaml_path="execution.concurrency.max", value=3),),
        )


def test_save_config_document_patch_inserts_missing_block_path_locally(tmp_path):
    """Adding a field retains unrelated block indentation and document markers."""
    config = Config()
    target = tmp_path / "main.yaml"
    original = """# operator header
repo:
  scopes:
      - repo
      - read:org
execution:
  concurrency:
    max_concurrent_sessions: 2
review: { enabled: true, default: "agent:reviewer" }
...
"""
    target.write_text(original)

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.session_timeout_minutes",
                value=45,
            ),
        ),
        path=target,
    )

    assert target.read_text() == original.replace(
        "    max_concurrent_sessions: 2\n",
        "    max_concurrent_sessions: 2\n    session_timeout_minutes: 45\n",
    )


def test_save_config_document_patch_inserts_missing_flow_path_locally(tmp_path):
    """A missing field can be added without normalizing custom flow spacing."""
    config = Config()
    target = tmp_path / "main.yaml"
    original = "execution: { concurrency: { max_concurrent_sessions: 2 } }"
    target.write_text(original)

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.session_timeout_minutes",
                value=45,
            ),
        ),
        path=target,
    )

    assert target.read_text() == (
        "execution: { concurrency: { max_concurrent_sessions: 2 , "
        '"session_timeout_minutes": 45} }'
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "execution: { concurrency: { max_concurrent_sessions: 2 "
            "# operator note\n  } }\n",
            "execution: { concurrency: { max_concurrent_sessions: 2 "
            '# operator note\n  , "session_timeout_minutes": 45} }\n',
        ),
        (
            "execution: { concurrency: { max_concurrent_sessions: 2, } }\n",
            "execution: { concurrency: { max_concurrent_sessions: 2, "
            '"session_timeout_minutes": 45} }\n',
        ),
    ],
    ids=("trailing-comment", "trailing-comma"),
)
def test_save_config_document_patch_inserts_after_flow_trivia(
    tmp_path, original, expected
):
    """Flow insertion respects comments/commas and persists the requested value."""
    config = Config()
    target = tmp_path / "main.yaml"
    target.write_text(original)

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.session_timeout_minutes",
                value=45,
            ),
        ),
        path=target,
    )

    assert target.read_text() == expected
    assert (
        yaml.safe_load(target.read_text())["execution"]["concurrency"][
            "session_timeout_minutes"
        ]
        == 45
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        (
            "---\n...\n",
            "---\nexecution:\n  concurrency:\n    max_concurrent_sessions: 3\n...\n",
        ),
        (
            "%YAML 1.2\n# operator header\n---\n...\n",
            "%YAML 1.2\n# operator header\n---\nexecution:\n  concurrency:\n"
            "    max_concurrent_sessions: 3\n...\n",
        ),
    ],
    ids=("document-markers", "directive-comment-preamble"),
)
def test_save_config_document_patch_populates_explicit_empty_document(
    tmp_path, original, expected
):
    """A marker-framed null document gains its first path inside the markers."""
    config = Config()
    target = tmp_path / "main.yaml"
    target.write_text(original)

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.max_concurrent_sessions",
                value=3,
            ),
        ),
        path=target,
    )

    assert target.read_text() == expected
    assert yaml.safe_load(target.read_text()) == {
        "execution": {"concurrency": {"max_concurrent_sessions": 3}}
    }


def test_save_config_document_patch_preserves_changed_scalar_anchor(tmp_path):
    """Changing an anchored scalar keeps aliases elsewhere in the document valid."""
    config = Config()
    target = tmp_path / "main.yaml"
    original = """execution:
  concurrency:
    max_concurrent_sessions: &worker_limit 2
custom:
  mirrored_limit: *worker_limit
"""
    target.write_text(original)

    save_config_document_patch(
        config,
        (
            types.SimpleNamespace(
                yaml_path="execution.concurrency.max_concurrent_sessions",
                value=3,
            ),
        ),
        path=target,
    )

    assert target.read_text() == original.replace(
        "max_concurrent_sessions: &worker_limit 2",
        "max_concurrent_sessions: &worker_limit 3",
    )
    assert yaml.safe_load(target.read_text())["custom"]["mirrored_limit"] == 3


def test_save_config_document_patch_rejects_changed_alias_without_writing(tmp_path):
    """An alias-owned field fails closed instead of mutating its remote anchor."""
    config = Config()
    target = tmp_path / "main.yaml"
    original = """defaults:
  worker_limit: &worker_limit 2
execution:
  concurrency:
    max_concurrent_sessions: *worker_limit
"""
    target.write_text(original)

    with pytest.raises(ValueError, match="Cannot patch YAML alias"):
        save_config_document_patch(
            config,
            (
                types.SimpleNamespace(
                    yaml_path="execution.concurrency.max_concurrent_sessions",
                    value=3,
                ),
            ),
            path=target,
        )

    assert target.read_text() == original


def test_save_plan_selects_only_changed_fields(loaded_config):
    """The persistence-policy owner emits only the changed field yaml_paths.

    This is the decision F1/A1/F2 pin down: which YAML paths a save should
    persist. It must be a field-granular value comparison against the
    current-config snapshot -- never whole tabs, and never which tabs a request
    happens to carry.
    """
    config, _ = loaded_config

    snapshot = from_config(config)
    submitted = from_config(config)  # A whole-form post: every tab present.

    # No edits -> empty plan (a no-op save), even though every tab is submitted.
    assert build_save_plan(snapshot, submitted).is_empty

    # Edit one field in one tab -> plan carries only that field's yaml_path,
    # not the tab's other (unchanged) fields.
    submitted["concurrency"].max_concurrent_sessions = 11
    plan = build_save_plan(snapshot, submitted)
    assert plan.changed_yaml_paths == ("execution.concurrency.max_concurrent_sessions",)


def test_save_plan_preserves_unedited_env_var_field_in_changed_tab(
    tmp_path, monkeypatch
):
    """A ``${VAR}`` sibling of an edit, in the SAME tab, keeps its literal form.

    This is F1: ``Config.load`` expands ``${VAR}``, so ``from_config`` holds the
    resolved secret. A field-granular plan must not rewrite the unedited sibling
    with that resolved value merely because another field in the tab changed.
    """
    monkeypatch.setenv("SECRET_VALIDATE_CMD", "resolved-secret-cmd")
    (tmp_path / "prompt.txt").write_text("p")
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(
        "repo:\n"
        "  name: owner/repo\n"
        "validation:\n"
        "  quick:\n"
        "    cmd: ${SECRET_VALIDATE_CMD}\n"
        "  publish:\n"
        "    cmd: ./scripts/validate-pr.sh\n"
        "    timeout_seconds: 1800\n"
        "execution:\n"
        "  concurrency:\n"
        "    max_concurrent_sessions: 2\n"
        "agents:\n"
        "  agent:test:\n"
        "    prompt: prompt.txt\n"
    )
    config = Config.load(cfg_path)

    # Edit a sibling field in the SAME (Validation) tab as the ${VAR} field.
    saved = _save_one_tab_change(
        config,
        cfg_path,
        lambda tabs: setattr(tabs["validation"], "publish_timeout_seconds", 900),
    )

    text = cfg_path.read_text()
    # The literal reference in the unedited sibling survives; no secret leaks.
    assert "${SECRET_VALIDATE_CMD}" in text
    assert "resolved-secret-cmd" not in text
    assert saved["validation"]["quick"]["cmd"] == "${SECRET_VALIDATE_CMD}"
    # The edited sibling field is persisted; the other unedited sibling is kept.
    assert saved["validation"]["publish"]["timeout_seconds"] == 900
    assert saved["validation"]["publish"]["cmd"] == "./scripts/validate-pr.sh"


async def test_update_settings_route_preserves_operational_config(
    loaded_config, monkeypatch
):
    """End-to-end: POST /api/settings keeps repo.github + operational sections.

    Exercises the real handler wiring (validate -> apply -> save) with doctor
    stubbed to a clean result so persistence runs. This is the acceptance
    scenario at the HTTP boundary the settings ``Save`` button hits.

    Critically, the posted body mirrors the *real* browser producer: the
    settings form's ``collectForm`` gathers every ``[data-tab][data-field]``
    control, so a save carries ALL tabs, not just the edited one. The handler
    must still persist only the changed tab, or every untouched tab's defaults
    would be materialized into ``main.yaml``.
    """
    from issue_orchestrator.entrypoints import web_settings_routes

    config, cfg_path = loaded_config

    # Doctor must not error, or the handler rolls back and skips the save.
    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.run_doctor",
        lambda **kwargs: DoctorResult(),
    )

    orchestrator = types.SimpleNamespace(config=config)

    # Build the full whole-form payload the browser posts (every tab), then
    # change exactly one Concurrency field -- exactly like the settings UI save.
    full_payload = {
        key: model.model_dump() for key, model in from_config(config).items()
    }
    full_payload["concurrency"]["max_concurrent_sessions"] = 8

    class _FakeRequest:
        async def json(self):
            return full_payload

    response = await web_settings_routes.update_settings(_FakeRequest(), orchestrator)

    assert response.status_code == 200

    # This is the regression boundary the original #6686 tests missed: a real
    # changed save must preserve the whole hand-authored YAML presentation, not
    # merely parse back to the same data. Only the requested scalar may differ.
    assert cfg_path.read_text() == _OPERATIONAL_CONFIG.replace(
        "    max_concurrent_sessions: 2\n",
        "    max_concurrent_sessions: 8\n",
    )

    saved = yaml.safe_load(cfg_path.read_text())
    assert saved["execution"]["concurrency"]["max_concurrent_sessions"] == 8
    # The operational sections the old save() would have dropped survive.
    assert saved["repo"]["github"]["token_env"] == "MY_GH_TOKEN"
    assert saved["repo"]["github"]["write_verify"]["enabled"] is True
    assert saved["merge_queue"]["enabled"] is True
    assert saved["hooks"]["ai_gate"]["interval_days"] == 14
    assert saved["validation"]["publish"]["cmd"] == "./scripts/validate-pr.sh"
    assert saved["review"]["exchange"]["mode"] == "via-local-loop"

    # Even though the request carried every tab, only the CHANGED (Concurrency)
    # tab is persisted: other settings tabs' sections are neither rewritten nor
    # materialized from defaults.
    assert "provider_resilience" not in saved  # Advanced tab, untouched
    assert "sqlite_backup" not in saved  # Advanced tab, untouched
    assert "goal_pilot" not in saved  # Goal Pilot tab, untouched
    # An unedited field in an existing section is not added from its default.
    assert "dangerous_allow_failure" not in saved["hooks"]["ai_gate"]


async def test_noop_settings_save_leaves_file_bytes_untouched(tmp_path, monkeypatch):
    """A Save with no changed fields must not rewrite ``main.yaml`` (F2).

    Even though changed saves now use a presentation-preserving codec, a no-op
    should not touch the file at all. The empty-plan path therefore keeps the
    bytes -- including a non-leading inline comment -- exactly identical.
    """
    from issue_orchestrator.entrypoints import web_settings_routes

    (tmp_path / "prompt.txt").write_text("p")
    cfg_path = tmp_path / "main.yaml"
    cfg_path.write_text(
        "# Leading header\n"
        "repo:\n"
        "  name: owner/repo  # inline comment on a non-leading line\n"
        "execution:\n"
        "  concurrency:\n"
        "    max_concurrent_sessions: 2\n"
        "agents:\n"
        "  agent:test:\n"
        "    prompt: prompt.txt\n"
    )
    config = Config.load(cfg_path)

    monkeypatch.setattr(
        "issue_orchestrator.infra.doctor.run_doctor",
        lambda **kwargs: DoctorResult(),
    )
    orchestrator = types.SimpleNamespace(config=config)

    original_bytes = cfg_path.read_bytes()
    # The real whole-form payload the browser posts, with NO edits.
    full_payload = {
        key: model.model_dump() for key, model in from_config(config).items()
    }

    class _FakeRequest:
        async def json(self):
            return full_payload

    response = await web_settings_routes.update_settings(_FakeRequest(), orchestrator)

    assert response.status_code == 200
    # No field changed -> empty plan -> file not rewritten, byte-for-byte.
    assert cfg_path.read_bytes() == original_bytes
    assert b"# inline comment on a non-leading line" in cfg_path.read_bytes()
