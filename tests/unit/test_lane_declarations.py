"""The lane-declarations file is the single, schema-enforced home for
lane scheduling facts — and it cannot drift from the Makefile's lanes.

Drift is enforced bidirectionally the way the settings-schema tests
enforce theirs: every work key the gate submits must be declared, and
every declared lane must still exist in the gate. A row nobody runs is
dead configuration; a lane nobody declared cannot run at all.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.infra.lane_declarations import (
    LANES_FILE_RELATIVE,
    LaneDeclarationError,
    load_lane_declaration,
    load_lane_declarations,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_lanes(tmp_path: Path, body: str) -> Path:
    path = tmp_path / LANES_FILE_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(body, encoding="utf-8")
    return path


_VALID = """
lanes:
  test-unit:
    request_cpus: 8
    memory_mb: 6144
    suspendable: true
"""


def test_valid_declaration_loads(tmp_path: Path) -> None:
    _write_lanes(tmp_path, _VALID)
    declaration = load_lane_declaration(tmp_path, "test-unit")
    assert declaration.request_cpus == 8
    assert declaration.memory_mb == 6144
    assert declaration.suspendable is True
    assert declaration.exclusive == ()


def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(LaneDeclarationError, match="not found"):
        load_lane_declaration(tmp_path, "test-unit")


def test_malformed_yaml_fails_loudly(tmp_path: Path) -> None:
    _write_lanes(tmp_path, "lanes: [unclosed")
    with pytest.raises(LaneDeclarationError, match="not valid YAML"):
        load_lane_declaration(tmp_path, "test-unit")


def test_undeclared_lane_fails_loudly(tmp_path: Path) -> None:
    """No policy-by-absence: an undeclared lane names itself and the
    file it belongs in."""
    _write_lanes(tmp_path, _VALID)
    with pytest.raises(LaneDeclarationError, match="'test-web' is not declared"):
        load_lane_declaration(tmp_path, "test-web")


def test_suspendability_must_be_declared_explicitly(tmp_path: Path) -> None:
    """The explicit-classification guard, now schema-enforced: a lane
    nobody classified fails validation, it does not default."""
    _write_lanes(
        tmp_path,
        "lanes:\n  test-unit:\n    request_cpus: 8\n    memory_mb: 6144\n",
    )
    with pytest.raises(LaneDeclarationError, match="suspendable"):
        load_lane_declaration(tmp_path, "test-unit")


def test_field_types_are_strict_never_coerced(tmp_path: Path) -> None:
    """C1 (#7122 review): lax pydantic coerces `true` to 1, "8" to 8,
    and 1 to True — a YAML typo could silently under-request one CPU
    or flip suspension policy. Every coercion is a loud error."""
    coercion_typos = (
        "request_cpus: true\n    memory_mb: 6144\n    suspendable: true",
        'request_cpus: "8"\n    memory_mb: 6144\n    suspendable: true',
        'request_cpus: 8\n    memory_mb: "1024"\n    suspendable: true',
        "request_cpus: 8\n    memory_mb: 6144\n    suspendable: 1",
    )
    for body in coercion_typos:
        _write_lanes(tmp_path, f"lanes:\n  test-unit:\n    {body}\n")
        with pytest.raises(LaneDeclarationError, match="schema validation"):
            load_lane_declaration(tmp_path, "test-unit")
        (tmp_path / LANES_FILE_RELATIVE).unlink()
        (tmp_path / LANES_FILE_RELATIVE).parent.rmdir()


def test_memory_budget_must_be_declared(tmp_path: Path) -> None:
    """No silent slot sizing: without a declared budget the scheduler
    derives the slot from the tiny exec wrapper and the workload is
    OOM-killed — so the field is required, never defaulted."""
    _write_lanes(
        tmp_path,
        "lanes:\n  test-unit:\n    request_cpus: 8\n    suspendable: true\n",
    )
    with pytest.raises(LaneDeclarationError, match="memory_mb"):
        load_lane_declaration(tmp_path, "test-unit")


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    """An unread field sitting in the file is configuration theater."""
    _write_lanes(
        tmp_path,
        (
            "lanes:\n  test-unit:\n    request_cpus: 8\n    memory_mb: 6144\n"
            "    suspendable: true\n    priority: 50\n"
        ),
    )
    with pytest.raises(LaneDeclarationError, match="schema validation"):
        load_lane_declaration(tmp_path, "test-unit")


def test_nonsense_values_are_rejected(tmp_path: Path) -> None:
    _write_lanes(
        tmp_path,
        "lanes:\n  test-unit:\n    request_cpus: 0\n    memory_mb: 6144\n"
        "    suspendable: true\n",
    )
    with pytest.raises(LaneDeclarationError, match="schema validation"):
        load_lane_declaration(tmp_path, "test-unit")


def test_repository_declarations_file_is_valid() -> None:
    declarations = load_lane_declarations(REPO_ROOT)
    assert declarations.lanes, "the repository must declare its lanes"


# --- Drift enforcement against the Makefile's actual lanes ----------------

_CONDOR_LANE_TARGETS = (
    "_validate-pr-flat-impl",
    # Condor-capable targets outside the flat fan.
    "test-integration-core-local",
    "test-integration-agent",
)


def _scrubbed_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "MAKEFLAGS",
            "MFLAGS",
            "MAKELEVEL",
            "LANE_EXECUTOR",
            "ISSUE_ORCHESTRATOR_LANE_EXECUTOR",
        }
    }


def _submitted_work_keys(tmp_path: Path) -> set[str]:
    make = shutil.which("gmake") or "make"
    keys: set[str] = set()
    for target in _CONDOR_LANE_TARGETS:
        completed = subprocess.run(
            [
                make,
                "-C",
                str(tmp_path),
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-n",
                target,
                "LANE_EXECUTOR=condor",
            ],
            capture_output=True,
            text=True,
            env=_scrubbed_environment(),
        )
        keys.update(re.findall(r"--work-key (\S+)", completed.stdout))
    return keys


def test_every_submitted_lane_is_declared_and_vice_versa(
    tmp_path: Path,
) -> None:
    """Bidirectional drift enforcement, from the Makefile's own
    dry-run expansion — never from a hand-maintained list."""
    submitted = _submitted_work_keys(tmp_path)
    assert submitted, "dry-run produced no lane submissions - probe broken"
    declared = set(load_lane_declarations(REPO_ROOT).lanes)
    undeclared = submitted - declared
    assert not undeclared, (
        f"lanes submitted by the Makefile but not declared: {undeclared}"
    )
    dead = declared - submitted
    assert not dead, (
        f"declared lanes no Makefile target submits (dead rows): {dead}"
    )
