"""Workflow guardrails: action pinning and the execenv trigger contract.

The remote policy rejects unpinned actions at job SETUP — after a push,
after the gate, with zero local signal (B1, #7119 review: both execenv
runs died there). This makes the same rule fail in validate-quick.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

_PINNED_USES = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def test_workflows_exist() -> None:
    assert WORKFLOWS, "no workflows found - the glob is broken, not the repo"


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    offenders: list[str] = []
    for workflow in WORKFLOWS:
        for line_number, line in enumerate(
            workflow.read_text().splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped.startswith(("uses:", "- uses:")):
                continue
            reference = stripped.split("uses:", 1)[1].split("#", 1)[0].strip()
            if not _PINNED_USES.match(reference):
                offenders.append(f"{workflow.name}:{line_number}: {reference}")
    assert not offenders, (
        "actions not pinned to a full-length commit SHA (remote policy "
        "rejects these at job setup):\n" + "\n".join(offenders)
    )


def _execenv_triggers() -> dict:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "execenv.yml").read_text()
    )
    # YAML parses the bare `on:` key as boolean True.
    triggers = workflow.get("on") or workflow.get(True)
    assert isinstance(triggers, dict), "execenv.yml has no trigger block"
    return triggers


def _execenv_top_level() -> dict:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "execenv.yml").read_text()
    )
    assert isinstance(workflow, dict)
    return workflow


# Everything the in-container selftest executes: the image sources, the
# driver, the workflow itself, the lane execution surface the suite
# imports, and the Python runtime/lock inputs of `uv sync`. A fork PR
# touching any of these must trigger the proof (fork pushes never run
# in this repository).
_REQUIRED_PR_PATHS = {
    "docker/execenv/**",
    "scripts/condor-execenv.sh",
    ".github/workflows/execenv.yml",
    "src/issue_orchestrator/adapters/condor/**",
    "src/issue_orchestrator/adapters/direct_lane_executor.py",
    "src/issue_orchestrator/domain/lane_execution.py",
    "src/issue_orchestrator/ports/lane_executor.py",
    "src/issue_orchestrator/ports/lane_runtime_history.py",
    "src/issue_orchestrator/adapters/json_lane_runtime_history.py",
    "src/issue_orchestrator/entrypoints/cli_tools/lane_run.py",
    "tests/unit/lane_executor_contract.py",
    "tests/integration/test_condor_lane_executor.py",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
}


def test_execenv_push_leg_is_main_only() -> None:
    """Running push on every branch plus pull_request doubled every
    internal job (B2, #7119 review)."""
    triggers = _execenv_triggers()
    assert triggers["push"]["branches"] == ["main"], triggers["push"]


def test_execenv_pr_paths_cover_the_selftest_dependency_surface() -> None:
    triggers = _execenv_triggers()
    pr_paths = set(triggers["pull_request"]["paths"])
    missing = _REQUIRED_PR_PATHS - pr_paths
    assert not missing, (
        "fork PRs touching these would merge without the execenv proof: "
        f"{sorted(missing)}"
    )


def test_execenv_declares_concurrency_and_least_privilege() -> None:
    workflow = _execenv_top_level()
    assert workflow.get("concurrency", {}).get("cancel-in-progress") is True
    assert workflow.get("permissions") == {"contents": "read"}


def test_execenv_diagnostics_go_through_the_lifecycle_owner() -> None:
    """The workflow must not reach into docker or the pool's log layout
    itself; the driver owns the container lifecycle end to end (A1,
    #7119 review)."""
    text = (REPO_ROOT / ".github" / "workflows" / "execenv.yml").read_text()
    run_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("run:")
    ]
    offenders = [line for line in run_lines if "docker" in line]
    assert not offenders, (
        f"workflow bypasses the driver with direct docker calls: {offenders}"
    )
