---
name: configuration
description: Add or modify YAML configuration options. Use when working on infra/config.py, infra/settings_schema.py, examples/config.example.yaml, docs/user/configuration_reference.md, docs/user/e2e.md, test_config.py, or setup_wizard.py. Ensures all config-related files stay in sync.
---

# Configuration Options

This skill provides guidance for adding or modifying configuration options.

## When to Use

- Adding a new field to a config dataclass in `config.py`
- Adding a new field to the settings schema in `settings_schema.py`
- Modifying config parsing in `_parse_*_config()` functions
- Documenting configuration options

## Files to Keep in Sync

When adding a new **settings-visible** config option (one that appears in the web dashboard settings dialog), you only need to update TWO files:

1. **`src/issue_orchestrator/infra/config.py`**
   - Add field to the appropriate dataclass (e.g., `E2EConfig`, `ReviewConfig`)
   - Update the corresponding `_parse_*_config()` function

2. **`src/issue_orchestrator/infra/settings_schema.py`**
   - Add field to the appropriate Pydantic model (e.g., `E2ESettings`, `ReviewSettings`)
   - Set `json_schema_extra` with required and optional metadata (see DSL below)

The schema drives everything else automatically:
- **settings.html** — renders from schema (no template changes needed)
- **GET/POST /api/settings** — serializes/validates via Pydantic (no web.py changes needed)
- **setup_wizard.py** — `get_setup_fields()` reads `setup` annotations from schema
- **doctor checks** — `run_schema_checks()` reads `doctor_check` annotations from schema
- **status summaries** — `format_summary()` reads `summary` annotations from schema
- **docs/user/configuration_reference.md** — regenerate via `generate_config_reference()`

### Supported form field shapes (closed set)

The settings form renders/collects by dispatching on the control kind
produced by `settings_schema_support.classify_form_control()`:
`bool`, `int`, `float`, `str`, `Literal[...]` (enum), `Optional[str]`,
and `dict[str, Literal[...]]` (key/value editor). A field whose JSON
schema falls outside this set **fails loudly** at schema build
(`UnsupportedSettingsFieldError`) and in
`tests/unit/test_settings_schema.py::TestFormControlClassification` —
there is deliberately no text-input fallback (that fallback is what
broke dict-typed fields: the form posted a Python-repr string and every
save failed validation).

To add a new field shape, extend ALL THREE together (pinned by
`tests/unit/test_dashboard_ui_guardrails.py::test_settings_form_dispatches_cover_the_classifier_kind_set`):
1. `classify_form_control()` — new kind token
2. `templates/settings.html` — render branch for the kind
3. `static/js/settings_form_controls.js` — collector (+ resetter) for the kind

`docs/user/configuration.md` is hand-written onboarding. Update it only when the user-facing setup narrative changes; schema fields drive `docs/user/configuration_reference.md`.

**Also update (when relevant):**

3. **`examples/config.example.yaml`** — Add the option with a sensible default and comment
4. **`tests/unit/test_config.py`** — Add tests for parsing the new option
5. **`docs/user/e2e.md`** — For e2e-specific options

Review nit policy options are settings-visible review fields:

```yaml
review:
  nits:
    default_policy: "surface"     # ignore | surface | address
    by_agent: {}                  # coder agent label -> policy
```

Keep `Config.review_nits_default_policy`, `Config.review_nits_by_agent`, `ReviewSettings`, `examples/config.example.yaml`, generated configuration reference docs, and config parsing tests in sync.

## Schema Field DSL Reference

Every field in `settings_schema.py` uses `json_schema_extra` as its metadata dictionary.
The schema is the **single source of truth** — doctor checks, wizard prompts, summaries,
and UI forms all derive from it.

### Required Keys (all fields)

| Key | Purpose | Example |
|-----|---------|---------|
| `config_attr` | Dotted path to config attribute | `"e2e.enabled"` |
| `yaml_path` | Dotted path in YAML config file | `"e2e.enabled"` |

### Doctor Check Annotations

Add these to make the doctor automatically validate the field:

| Key | Purpose | Values |
|-----|---------|--------|
| `doctor_check` | Check type to run | `"path_exists"`, `"path_is_file"`, `"first_arg_path_exists"`, `"references_agent"` |
| `doctor_check_condition` | Only run check when this config attr is truthy | `"e2e.enabled"`, `"review_enabled"` |
| `doctor_severity` | Severity if check fails | `"error"` (default), `"warning"` |

**Check types:**
- `path_exists` — field value is a repo-relative path that should exist
- `path_is_file` — field value is a repo-relative path that must be a regular file
- `first_arg_path_exists` — first space-separated arg in a list is a path
- `references_agent` — field value must be a key in `config.agents`

```python
quarantine_file: str = Field(
    "tests/e2e/quarantine.txt",
    json_schema_extra={
        "config_attr": "e2e.quarantine_file",
        "yaml_path": "e2e.quarantine_file",
        "doctor_check": "path_exists",
        "doctor_check_condition": "e2e.enabled",
        "doctor_severity": "warning",
    },
)
```

### Setup Wizard Annotations

Add these to make the wizard automatically prompt for the field:

```python
"setup": {
    "enabled": True,           # wizard should ask this
    "section": "concurrency",  # wizard section grouping
    "order": 10,               # sort order within section
    "prompt": "Max sessions",  # override title if needed (defaults to field title)
    "condition": {             # only ask when condition met (optional)
        "field": "ui_mode",
        "value": "web",
    },
}
```

**Current setup sections:** `concurrency`, `ui`, `worktrees`

**How the wizard consumes setup fields:**

`get_setup_fields(section)` returns a sorted list of dicts, each with:
`name`, `title`, `description`, `default`, `type`, `order`, `prompt`, `condition`, `tab_key`, `yaml_path`

The wizard iterates and prompts:
```python
for field in get_setup_fields("concurrency"):
    raw = prompter.input(field["prompt"], str(field["default"]))
    config_values[field["name"]] = int(raw)  # coerce to field type
```

**To add a field to an existing section** — just add `"setup": {"enabled": True, "section": "concurrency", "order": 20}` to the field's `json_schema_extra`. The wizard picks it up automatically.

**To add a new section** — add the `setup` annotation with a new section name, then add a `get_setup_fields("new_section")` loop in `setup_wizard.py` where the section should appear. Existing sections (`concurrency`, `ui`, `worktrees`) already have loops.

**Condition evaluation** — `condition: {"field": "ui_mode", "value": "web"}` means the wizard skips the prompt unless the local variable `ui_mode` equals `"web"`. This is evaluated in the wizard loop, not by the schema.

### Summary Annotations

Add these to include the field in doctor status summaries:

```python
"summary": {
    "section": "e2e",          # summary section
    "format": "interval",      # format type (see below)
    "label": "auto",           # display label
    "unit": "m",               # unit suffix
    "zero_label": "manual",    # label when value is 0
}
```

**Format types:**
- `enabled_flag` — indicates enabled/disabled toggle (handled by caller)
- `key_value` — displays as `label: value`
- `interval` — displays as `label=value+unit` (or `zero_label` when 0)
- `boolean_flag` — displays as `label=true_value` or `label=false_value`

### Other Optional Keys

| Key | Purpose | Example |
|-----|---------|---------|
| `section` | UI tab section grouping | `"Session Limits"` |
| `restart_required` | Needs server restart on change | `True` |
| `ui_transform` | List<->string transform | `"comma_separated_list"`, `"space_separated_list"`, `"newline_separated_list"` |
| `config_read_method` | Use method instead of attr | `"filtering.get_milestones"` |
| `doc_examples` | Generated reference examples | `["0", "30", "60"]` |
| `doc_notes` | Generated reference note | `"Set to 0 to disable automatic runs."` |

## What NOT to Edit Directly

These are schema-driven — update the schema instead of editing them manually:

- **Doctor path/agent-reference checks** — add `doctor_check` annotation to schema field
- **Wizard simple-field prompts** — add `setup` annotation to schema field
- **Doctor status summaries** — add `summary` annotation to schema field
- **`ALLOWED_TOP_LEVEL_FIELDS`** in config.py — derived from `_TOP_LEVEL_SECTION_KEYS`
- **Generated config reference body** — replace only the section between the `AUTO-GENERATED CONFIG REFERENCE` markers in `docs/user/configuration_reference.md`

CI validation (`make validate`) enforces these rules automatically.

## Example: Adding a New Doctor-Checked Path Field

```python
# settings_schema.py
custom_test_dir: str = Field(
    "tests/custom",
    title="Custom Test Directory",
    description="Path to custom test directory",
    json_schema_extra={
        "config_attr": "e2e.custom_test_dir",
        "yaml_path": "e2e.custom_test_dir",
        "doctor_check": "path_exists",
        "doctor_check_condition": "e2e.enabled",
        "doctor_severity": "warning",
        "summary": {
            "section": "e2e",
            "format": "key_value",
            "label": "custom",
        },
    },
)
```

No changes needed to doctor code, wizard code, or summary code — the schema annotation
makes them all pick it up automatically.
