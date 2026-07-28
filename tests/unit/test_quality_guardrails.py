from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_config(root: Path, *, max_lines: int = 2) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            f"""
            version: 1
            rules:
              - id: oversized
                type: file_line_budget
                max_lines: {max_lines}
                include:
                  - src/**/*.py
              - id: policy_sites
                type: control_policy_branch_sites
                new_metric_min_value: 3
                include:
                  - src/**/*.py
                  - src/**/*.js
                terms:
                  - retry
                  - status
                  - label
                  - session_state
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_ruff_config(root: Path, *, ignore_noqa: bool = True) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            f"""
            version: 1
            rules:
              - id: ruff_complexity
                type: ruff_findings
                include:
                  - src/**/*.py
                select:
                  - C901
                ignore_noqa: {str(ignore_noqa).lower()}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_noqa_config(root: Path) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            """
            version: 1
            rules:
              - id: noqa_suppressions
                type: ruff_noqa_suppressions
                include:
                  - src/**/*.py
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_semgrep_config(root: Path) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    semgrep_config = root / "tools" / "semgrep" / "rules.yml"
    semgrep_project = root / "tools" / "semgrep" / "pyproject.toml"
    semgrep_config.parent.mkdir(parents=True, exist_ok=True)
    semgrep_config.write_text("rules: []\n", encoding="utf-8")
    semgrep_project.write_text(
        textwrap.dedent(
            """
            [project]
            dependencies = [
                "semgrep==1.163.0",
            ]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            """
            version: 1
            rules:
              - id: semgrep_owner
                type: semgrep_findings
                config: tools/semgrep/rules.yml
                include:
                  - src/**/*.py
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_ui_openapi_config(root: Path) -> None:
    config = root / "tools" / "quality_guardrails.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        textwrap.dedent(
            """
            version: 1
            rules:
              - id: ui_openapi_routes
                type: ui_openapi_routes
                schema: docs/api/ui-openapi.json
                route_include:
                  - src/**/*.py
                browser_route_include:
                  - src/**/web*.py
                browser_path_prefixes:
                  - /api/
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def _write_ui_openapi_schema(root: Path, paths: dict[str, dict[str, str]]) -> None:
    schema_paths: dict[str, dict[str, object]] = {}
    components: dict[str, dict[str, object]] = {}
    for url_path, operations in paths.items():
        schema_paths[url_path] = {}
        for method, component in operations.items():
            components.setdefault(component, {"type": "object", "additionalProperties": False})
            schema_paths[url_path][method.lower()] = {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": f"#/components/schemas/{component}"}
                            }
                        }
                    }
                }
            }

    schema_path = root / "docs" / "api" / "ui-openapi.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        json.dumps(
            {
                "openapi": "3.1.0",
                "info": {"title": "UI", "version": "0"},
                "paths": schema_paths,
                "components": {"schemas": components},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_fake_semgrep(
    root: Path,
    *,
    version: str = "1.163.0",
    relative_path: str = "fake-semgrep",
) -> Path:
    fake = root / relative_path
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        textwrap.dedent(
            f"""
            #!{sys.executable}
            from pathlib import Path
            import sys

            if sys.argv[1:] == ["--version"]:
                print({version!r})
                raise SystemExit(0)
            if "scan" not in sys.argv:
                raise SystemExit("expected scan subcommand")
            sys.stdout.write(Path("semgrep-results.json").read_text(encoding="utf-8"))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _copy_runner(root: Path) -> None:
    tools_dir = root / "tools"
    tools_dir.mkdir(exist_ok=True)
    source = _repo_root() / "tools" / "quality_guardrails.py"
    (tools_dir / "quality_guardrails.py").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")


def _run(
    root: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "tools/quality_guardrails.py", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_update_baseline_records_current_metrics(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["oversized:src/pkg/control.py"]["value"] == 3
    assert baseline["metrics"]["policy_sites:src/pkg/control.py"]["value"] == 1


def test_fail_on_new_blocks_metric_increase(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "oversized:src/pkg/control.py" not in result.stderr
    assert "policy_sites" in result.stderr
    assert "1 -> 2" in result.stderr


def test_fail_on_new_blocks_new_over_budget_file(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target = tmp_path / "src" / "pkg" / "new_control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f():\n"
        "    return 1\n"
        "def g():\n"
        "    return 2\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "new file_line_budget" in result.stderr
    assert "src/pkg/new_control.py" in result.stderr


def test_new_policy_site_below_floor_does_not_fail(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    (tmp_path / "src").mkdir()
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target = tmp_path / "src" / "pkg" / "new_control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 0, result.stderr


def test_policy_terms_do_not_match_substrings(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(prestatus_enabled, retryable):\n"
        "    if prestatus_enabled:\n"
        "        return retryable\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/pkg/control.py" not in baseline["metrics"]


def test_policy_terms_match_identifier_tokens_and_plurals(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(statusCode, labels, session_state, sessionState):\n"
        "    if statusCode:\n"
        "        return True\n"
        "    if labels:\n"
        "        return True\n"
        "    if session_state:\n"
        "        return labels\n"
        "    if sessionState:\n"
        "        return labels\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/pkg/control.py"]["value"] == 4


def test_ruff_findings_are_tracked_and_ratchet_numeric_scores(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def decide(value):\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(11))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "src/pkg/control.py [ruff_complexity] ruff_finding increased" in result.stderr
    assert "11 -> 12" in result.stderr


def test_ruff_findings_can_inventory_noqa_suppressed_debt(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path, ignore_noqa=True)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):  # noqa: C901 - existing debt\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["ruff_complexity:src/pkg/control.py:C901:decide"]["value"] == 11


def test_ruff_findings_respect_noqa_by_default(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ruff_config(tmp_path, ignore_noqa=False)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def decide(value):  # noqa: C901 - existing debt\n"
        + "".join(f"    if value == {index}:\n        return {index}\n" for index in range(10))
        + "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"] == {}


def test_ruff_noqa_suppressions_are_ratchet_tracked(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_noqa_config(tmp_path)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'DOCS = "# noqa: ARG001 - docs only"\n'
        "def existing(arg):  # noqa: ARG001 - existing signature\n"
        "    return None\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert len(baseline["metrics"]) == 1
    assert "docs only" not in next(iter(baseline["metrics"].values()))["detail"]

    target.write_text(
        'DOCS = "# noqa: ARG001 - docs only"\n'
        "def existing(argument):  # noqa: ARG001 - existing signature\n"
        "    return None\n"
        "def added(arg):  # noqa: ARG001 - new bypass\n"
        "    return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "src/pkg/control.py [noqa_suppressions] new ruff_noqa_suppression" in result.stderr
    assert "new bypass" in result.stderr
    assert "existing signature" not in result.stderr


def test_semgrep_findings_are_ratchet_tracked(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_semgrep_config(tmp_path)
    fake_semgrep = _write_fake_semgrep(tmp_path)
    env = {**os.environ, "QUALITY_GUARDRAILS_SEMGREP_BIN": str(fake_semgrep)}
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f(state):\n    state.pending_reviews.append(1)\n", encoding="utf-8")
    (tmp_path / "semgrep-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "path": "src/pkg/control.py",
                        "check_id": "issue-orchestrator.direct-runtime-state-mutation",
                        "start": {"line": 2, "col": 5},
                        "extra": {
                            "message": "Route mutation through an owner.",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline", env=env).returncode == 0
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    matching_keys = [
        key
        for key in baseline["metrics"]
        if key.startswith("semgrep_owner:src/pkg/control.py:issue-orchestrator.direct-runtime-state-mutation:")
    ]
    assert len(matching_keys) == 1
    expected_key = matching_keys[0]
    assert baseline["metrics"][expected_key]["value"] == 1

    target.write_text(
        "\n"
        "def f(state):\n"
        "    state.pending_reviews.append(1)\n"
        "    state.pending_reviews.append(2)\n",
        encoding="utf-8",
    )
    (tmp_path / "semgrep-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "path": "src/pkg/control.py",
                        "check_id": "issue-orchestrator.direct-runtime-state-mutation",
                        "start": {"line": 3, "col": 5},
                        "extra": {
                            "message": "Route mutation through an owner.",
                        },
                    },
                    {
                        "path": "src/pkg/control.py",
                        "check_id": "issue-orchestrator.direct-runtime-state-mutation",
                        "start": {"line": 4, "col": 5},
                        "extra": {
                            "message": "Route mutation through an owner.",
                        },
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new", env=env)

    assert result.returncode == 2
    assert "src/pkg/control.py [semgrep_owner] new semgrep_finding" in result.stderr
    assert "issue-orchestrator.direct-runtime-state-mutation" in result.stderr
    assert "line 3" not in result.stderr


def test_semgrep_uses_locked_tool_environment_by_default(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_semgrep_config(tmp_path)
    _write_fake_semgrep(tmp_path, relative_path=".venv-semgrep/bin/semgrep")
    env = {key: value for key, value in os.environ.items() if key != "QUALITY_GUARDRAILS_SEMGREP_BIN"}
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return None\n", encoding="utf-8")
    (tmp_path / "semgrep-results.json").write_text(
        json.dumps({"results": []}),
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline", env=env)

    assert result.returncode == 0, result.stderr


def test_semgrep_missing_tool_environment_fails_fast(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_semgrep_config(tmp_path)
    env = {key: value for key, value in os.environ.items() if key != "QUALITY_GUARDRAILS_SEMGREP_BIN"}
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return None\n", encoding="utf-8")

    result = _run(tmp_path, "--update-baseline", env=env)

    assert result.returncode == 1
    assert "Semgrep tool environment not found at .venv-semgrep" in result.stderr


def test_semgrep_errors_fail_fast(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_semgrep_config(tmp_path)
    fake_semgrep = _write_fake_semgrep(tmp_path)
    env = {**os.environ, "QUALITY_GUARDRAILS_SEMGREP_BIN": str(fake_semgrep)}
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return None\n", encoding="utf-8")
    (tmp_path / "semgrep-results.json").write_text(
        json.dumps({"results": [], "errors": [{"message": "bad pattern"}]}),
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline", env=env)

    assert result.returncode == 1
    assert "Semgrep reported errors" in result.stderr


def test_semgrep_version_mismatch_fails_fast(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_semgrep_config(tmp_path)
    fake_semgrep = _write_fake_semgrep(tmp_path, version="1.162.0")
    env = {**os.environ, "QUALITY_GUARDRAILS_SEMGREP_BIN": str(fake_semgrep)}
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    return None\n", encoding="utf-8")
    (tmp_path / "semgrep-results.json").write_text(
        json.dumps({"results": []}),
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline", env=env)

    assert result.returncode == 1
    assert "Semgrep version mismatch: expected 1.163.0, got 1.162.0" in result.stderr


def test_ui_openapi_routes_ratchet_uncontracted_browser_routes(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {"/api/typed": {"get": "TypedPayload"}})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/typed', response_model=TypedPayload)\n"
        "def typed():\n"
        "    return {}\n"
        "@router.post('/api/raw')\n"
        "def raw():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "ui_openapi_routes:uncontracted:POST /api/raw" in baseline["metrics"]
    assert "ui_openapi_routes:response-model:GET /api/typed" not in baseline["metrics"]


def test_ui_openapi_routes_block_response_model_drift(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {"/api/typed": {"get": "TypedPayload"}})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/typed', response_model=TypedPayload)\n"
        "def typed():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/typed')\n"
        "def typed():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "ui_openapi_response_model_drift" in result.stderr
    assert "response_model=TypedPayload" in result.stderr


def test_ui_openapi_routes_report_schema_routes_without_fastapi_route(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {"/api/missing": {"get": "MissingPayload"}})
    (tmp_path / "src" / "app").mkdir(parents=True)

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "ui_openapi_routes:missing:GET /api/missing" in baseline["metrics"]


def test_ui_openapi_routes_track_dynamic_browser_route_paths(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    assert _run(tmp_path, "--update-baseline").returncode == 0
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "API_DYNAMIC = '/api/dynamic'\n"
        "@router.get(API_DYNAMIC)\n"
        "def dynamic():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 2
    assert "ui_openapi_dynamic_route_path" in result.stderr
    assert "dynamic path 'API_DYNAMIC'" in result.stderr


def test_ui_openapi_response_model_drift_names_an_absent_response_model(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {"/api/typed": {"get": "TypedPayload"}})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/typed')\n"
        "def typed():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    drift = [
        metric for metric in payload["metrics"] if metric["kind"] == "ui_openapi_response_model_drift"
    ]
    assert len(drift) == 1
    assert drift[0]["metric_id"] == "response-model:GET /api/typed"
    assert drift[0]["detail"] == (
        "GET /api/typed must use response_model=TypedPayload "
        "from docs/api/ui-openapi.json; found (no response_model)"
    )


def test_ui_openapi_dynamic_route_path_names_an_unknown_path_expression(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get(response_model=TypedPayload)\n"
        "def pathless():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--format", "json")

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    dynamic = [
        metric for metric in payload["metrics"] if metric["kind"] == "ui_openapi_dynamic_route_path"
    ]
    assert len(dynamic) == 1
    assert dynamic[0]["detail"] == (
        "GET route at line 3 uses dynamic path (unknown route path expression); "
        "browser-facing routes must use string literals so the UI OpenAPI guardrail "
        "can compare them"
    )
    # The id keys on an explicit token, never on the None-able path expression
    # itself and never on the diagnostic prose, so rewording the detail above
    # cannot invalidate an accepted baseline entry.
    assert dynamic[0]["metric_id"] == "dynamic-path:GET src/app/web_routes.py:<no-path-argument>"


def test_pathless_dynamic_route_metric_key_survives_line_shifts(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    pathless_route = (
        "@router.get(response_model=TypedPayload)\n"
        "def pathless():\n"
        "    return {}\n"
    )
    header = "from fastapi import APIRouter\nrouter = APIRouter()\n"
    target.write_text(header + pathless_route, encoding="utf-8")
    assert _run(tmp_path, "--update-baseline").returncode == 0
    accepted_keys = _dynamic_path_keys(tmp_path)
    assert accepted_keys == [
        "ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:<no-path-argument>"
    ]

    target.write_text(header + "\n" * 5 + pathless_route, encoding="utf-8")

    result = _run(tmp_path, "--fail-on-new", "--check-stale")

    assert result.returncode == 0, result.stderr + result.stdout
    assert _dynamic_path_keys(tmp_path) == accepted_keys


def test_ui_openapi_route_diagnostics_never_leak_placeholder_sentinels(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {"/api/typed": {"get": "TypedPayload"}})
    target = tmp_path / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get('/api/typed')\n"
        "def typed():\n"
        "    return {}\n"
        "@router.post(**route_options)\n"
        "def pathless():\n"
        "    return {}\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    details = [entry["detail"] for entry in baseline["metrics"].values()]
    assert details
    assert not any("<missing>" in detail for detail in details), details
    assert any("(no response_model)" in detail for detail in details), details
    assert any("(unknown route path expression)" in detail for detail in details), details
    # Absent metadata must not leak into persisted metric keys either — neither
    # as the old sentinel nor as a stringified None from an optional field.
    keys = list(baseline["metrics"])
    assert not any("<missing>" in key for key in keys), keys
    assert not any(key.endswith(":None") for key in keys), keys


def _write_dynamic_browser_route(
    root: Path,
    *,
    expression: str = "API_DYNAMIC",
    lines_above: int = 0,
    duplicate: bool = False,
) -> Path:
    target = root / "src" / "app" / "web_routes.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    duplicate_decorator = (
        f"@router.get({expression})\n"
        "def dynamic_twin():\n"
        "    return {}\n"
        if duplicate
        else ""
    )
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        f"{expression} = '/api/dynamic'\n"
        + "\n" * lines_above
        + f"@router.get({expression})\n"
        "def dynamic():\n"
        "    return {}\n" + duplicate_decorator,
        encoding="utf-8",
    )
    return target


def _dynamic_path_keys(root: Path) -> list[str]:
    baseline = json.loads((root / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    return sorted(key for key in baseline["metrics"] if "dynamic-path:" in key)


def _json_report(root: Path, *args: str) -> tuple[int, dict[str, list[dict[str, object]]]]:
    result = _run(root, "--format", "json", *args)
    return result.returncode, json.loads(result.stdout)


def test_dynamic_route_metric_key_ignores_the_decorator_source_line(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    _write_dynamic_browser_route(tmp_path)
    assert _run(tmp_path, "--update-baseline").returncode == 0
    accepted_keys = _dynamic_path_keys(tmp_path)
    assert accepted_keys == ["ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:API_DYNAMIC"]

    _write_dynamic_browser_route(tmp_path, lines_above=5)

    result = _run(tmp_path, "--fail-on-new", "--check-stale")

    assert result.returncode == 0, result.stderr + result.stdout
    assert _dynamic_path_keys(tmp_path) == accepted_keys


def test_dynamic_route_metric_key_changes_when_the_path_expression_changes(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    _write_dynamic_browser_route(tmp_path)
    assert _run(tmp_path, "--update-baseline").returncode == 0

    _write_dynamic_browser_route(tmp_path, expression="API_RENAMED")

    returncode, report = _json_report(tmp_path, "--fail-on-new", "--check-stale")

    assert returncode == 2
    assert [violation["key"] for violation in report["violations"]] == [
        "ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:API_RENAMED"
    ]
    assert [entry["key"] for entry in report["stale_entries"]] == [
        "ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:API_DYNAMIC"
    ]


def test_duplicate_dynamic_route_decorators_get_distinct_metric_keys(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    _write_dynamic_browser_route(tmp_path, duplicate=True)

    assert _run(tmp_path, "--update-baseline").returncode == 0

    assert _dynamic_path_keys(tmp_path) == [
        "ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:API_DYNAMIC",
        "ui_openapi_routes:dynamic-path:GET src/app/web_routes.py:API_DYNAMIC#2",
    ]


def test_duplicate_dynamic_route_metric_keys_survive_line_shifts(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    _write_dynamic_browser_route(tmp_path, duplicate=True)
    assert _run(tmp_path, "--update-baseline").returncode == 0
    accepted_keys = _dynamic_path_keys(tmp_path)

    _write_dynamic_browser_route(tmp_path, duplicate=True, lines_above=3)

    result = _run(tmp_path, "--fail-on-new", "--check-stale")

    assert result.returncode == 0, result.stderr + result.stdout
    assert _dynamic_path_keys(tmp_path) == accepted_keys


def test_ui_openapi_routes_ignore_dynamic_non_browser_route_paths(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_ui_openapi_config(tmp_path)
    _write_ui_openapi_schema(tmp_path, {})
    target = tmp_path / "src" / "app" / "control_routes.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "CONTROL_DYNAMIC = '/control/dynamic'\n"
        "@router.get(CONTROL_DYNAMIC)\n"
        "def dynamic():\n"
        "    return {}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"] == {}


def test_decrease_does_not_fail_and_stale_baseline_is_ignored(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    stale = tmp_path / "src" / "pkg" / "stale.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )
    stale.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    stale.unlink()

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 0, result.stderr


def test_check_stale_reports_removed_metric(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()

    result = _run(tmp_path, "--check-stale")

    assert result.returncode == 3
    assert "stale baseline entries" in result.stderr
    assert "src/pkg/stale.py [policy_sites]" in result.stderr


def test_prune_removes_only_named_stale_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    other_stale = tmp_path / "src" / "pkg" / "other_stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    other_stale.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()
    other_stale.unlink()

    result = _run(tmp_path, "--prune", "policy_sites:src/pkg/stale.py", "--check-stale")

    assert result.returncode == 3
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/pkg/stale.py" not in baseline["metrics"]
    assert "policy_sites:src/pkg/other_stale.py" in baseline["metrics"]
    assert "src/pkg/other_stale.py [policy_sites]" in result.stderr


def test_prune_rejects_current_metric_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    current = tmp_path / "src" / "pkg" / "current.py"
    current.parent.mkdir(parents=True)
    current.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0

    result = _run(tmp_path, "--prune", "policy_sites:src/pkg/current.py")

    assert result.returncode == 1
    assert "cannot prune current metric key" in result.stderr


def test_check_stale_fails_fast_on_malformed_baseline_entry(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    stale = tmp_path / "src" / "pkg" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    stale.unlink()
    baseline_path = tmp_path / "quality" / "guardrails-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    del baseline["metrics"]["policy_sites:src/pkg/stale.py"]["detail"]
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    result = _run(tmp_path, "--check-stale")

    assert result.returncode == 1
    assert "missing non-empty string field 'detail'" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--update-baseline", "--accept", "policy_sites:src/pkg/control.py"),
        ("--update-baseline", "--prune", "policy_sites:src/pkg/control.py"),
        (
            "--accept",
            "policy_sites:src/pkg/control.py",
            "--prune",
            "policy_sites:src/pkg/stale.py",
        ),
    ],
)
def test_update_accept_and_prune_are_mutually_exclusive(tmp_path: Path, args: tuple[str, ...]) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()

    result = _run(tmp_path, *args)

    assert result.returncode == 1
    assert "--update-baseline, --accept, and --prune cannot be combined" in result.stderr


def test_missing_baseline_returns_error(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    (tmp_path / "src").mkdir()

    result = _run(tmp_path, "--fail-on-new")

    assert result.returncode == 1
    assert "baseline not found" in result.stderr


def test_json_output_includes_metrics_and_violations(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    target = tmp_path / "src" / "pkg" / "control.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    target.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--fail-on-new", "--format", "json")

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["metrics"]
    assert payload["stale_entries"] == []
    assert payload["violations"][0]["key"] == "policy_sites:src/pkg/control.py"


def test_accept_updates_only_named_baseline_key(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=20)
    existing = tmp_path / "src" / "pkg" / "existing.py"
    accepted = tmp_path / "src" / "pkg" / "accepted.py"
    existing.parent.mkdir(parents=True)
    existing.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n",
        encoding="utf-8",
    )
    assert _run(tmp_path, "--update-baseline").returncode == 0
    existing.write_text(
        "def f(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n",
        encoding="utf-8",
    )
    accepted.write_text(
        "def g(status):\n"
        "    if status == 'retry':\n"
        "        return True\n"
        "    if status == 'done':\n"
        "        return False\n"
        "    if status == 'blocked':\n"
        "        return None\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--accept", "policy_sites:src/pkg/accepted.py")

    assert result.returncode == 2
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/pkg/accepted.py"]["value"] == 3
    assert baseline["metrics"]["policy_sites:src/pkg/existing.py"]["value"] == 1
    assert "src/pkg/existing.py [policy_sites]" in result.stderr


def test_javascript_policy_sites_are_tracked(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "function render(status) {\n"
        "  if (status === 'retry') return 'retry';\n"
        "}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/ui/view.js"]["value"] == 1


def test_javascript_policy_sites_track_multiline_conditions(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=40)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "function render(status, retryCount, sessionState) {\n"
        "  if (\n"
        "    status === 'retry'\n"
        "  ) return 'retry';\n"
        "  while (\n"
        "    retryCount > 0\n"
        "  ) retryCount -= 1;\n"
        "  switch (\n"
        "    sessionState\n"
        "  ) {\n"
        "    case 'retry':\n"
        "      return status;\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert baseline["metrics"]["policy_sites:src/ui/view.js"]["value"] == 4


def test_javascript_policy_sites_ignore_comments_and_string_keywords(tmp_path: Path) -> None:
    _copy_runner(tmp_path)
    _write_config(tmp_path, max_lines=40)
    target = tmp_path / "src" / "ui" / "view.js"
    target.parent.mkdir(parents=True)
    target.write_text(
        "// if (status === 'retry') return true;\n"
        "/*\n"
        "switch (sessionState) {\n"
        "  case 'retry':\n"
        "}\n"
        "*/\n"
        "const text = \"if (status)\";\n"
        "if (ready /* status */) return true;\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, "--update-baseline")

    assert result.returncode == 0, result.stderr
    baseline = json.loads((tmp_path / "quality" / "guardrails-baseline.json").read_text(encoding="utf-8"))
    assert "policy_sites:src/ui/view.js" not in baseline["metrics"]
