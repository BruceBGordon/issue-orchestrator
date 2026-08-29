"""The per-lane verdict store: green-only, SHA-keyed, loud on corruption."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.lane_verdicts import (
    LANE_VERDICTS_RELATIVE,
    LaneVerdictError,
    read_green,
    record_green,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def test_record_then_read_round_trips(tmp_path: Path) -> None:
    recorded = record_green(tmp_path, SHA_A, "test-unit")
    verdict = read_green(tmp_path, SHA_A, "test-unit")
    assert verdict == recorded
    assert verdict is not None and verdict.tree_sha == SHA_A


def test_absent_verdict_is_a_miss_not_an_error(tmp_path: Path) -> None:
    assert read_green(tmp_path, SHA_A, "test-unit") is None


def test_other_sha_is_a_miss_and_pruned_on_record(tmp_path: Path) -> None:
    record_green(tmp_path, SHA_A, "test-unit")
    assert read_green(tmp_path, SHA_B, "test-unit") is None
    record_green(tmp_path, SHA_B, "typecheck")
    # A worktree has exactly one HEAD: recording at the new SHA prunes
    # the old SHA's verdicts wholesale.
    assert not (tmp_path / LANE_VERDICTS_RELATIVE / SHA_A).exists()
    assert read_green(tmp_path, SHA_B, "typecheck") is not None


def test_corrupt_json_is_loud_never_green(tmp_path: Path) -> None:
    record_green(tmp_path, SHA_A, "test-unit")
    path = tmp_path / LANE_VERDICTS_RELATIVE / SHA_A / "test-unit.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(LaneVerdictError, match="corrupt"):
        read_green(tmp_path, SHA_A, "test-unit")


def test_key_disagreement_is_corruption_not_a_hit(tmp_path: Path) -> None:
    """A record whose own body names a different target or SHA was
    written by something else — trusting any field of it would be a
    fabricated verdict."""
    record_green(tmp_path, SHA_A, "test-unit")
    path = tmp_path / LANE_VERDICTS_RELATIVE / SHA_A / "test-unit.json"
    body = json.loads(path.read_text())
    body["target"] = "test-web"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(LaneVerdictError, match="disagrees with its key"):
        read_green(tmp_path, SHA_A, "test-unit")


def test_stored_non_green_exit_is_corruption(tmp_path: Path) -> None:
    """Only green is ever recorded; a red verdict on disk means the
    store was hand-edited or written by foreign code."""
    record_green(tmp_path, SHA_A, "test-unit")
    path = tmp_path / LANE_VERDICTS_RELATIVE / SHA_A / "test-unit.json"
    body = json.loads(path.read_text())
    body["exit_code"] = 1
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(LaneVerdictError, match="non-green"):
        read_green(tmp_path, SHA_A, "test-unit")


def test_short_or_unhex_sha_is_rejected(tmp_path: Path) -> None:
    for bad in ("abc123", "A" * 40, "z" * 40, ""):
        with pytest.raises(LaneVerdictError, match="40-hex"):
            record_green(tmp_path, bad, "test-unit")
        with pytest.raises(LaneVerdictError, match="40-hex"):
            read_green(tmp_path, bad, "test-unit")


def test_unsafe_target_names_are_rejected(tmp_path: Path) -> None:
    for bad in ("../escape", "a/b", "", ".hidden", "a" * 200):
        with pytest.raises(LaneVerdictError, match="safe make target"):
            record_green(tmp_path, SHA_A, bad)


def test_record_is_idempotent_and_leaves_no_debris(tmp_path: Path) -> None:
    """Two gates racing in one worktree at the same SHA both land the
    same verdict via atomic replace — and no .part temp survives."""
    record_green(tmp_path, SHA_A, "test-unit")
    record_green(tmp_path, SHA_A, "test-unit")
    sha_dir = tmp_path / LANE_VERDICTS_RELATIVE / SHA_A
    assert [p.name for p in sha_dir.iterdir()] == ["test-unit.json"]
    assert read_green(tmp_path, SHA_A, "test-unit") is not None
