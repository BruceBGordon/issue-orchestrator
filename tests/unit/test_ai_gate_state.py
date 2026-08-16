"""Unit tests for AI gate state persistence."""

from datetime import datetime, timedelta, timezone

from issue_orchestrator.infra.ai_gate_state import (
    AiGateResult,
    AiGateState,
    load_ai_gate_state,
    save_ai_gate_state,
    get_ai_gate_state_path,
)


class TestAiGateResult:
    """Tests for AiGateResult dataclass."""

    def test_creation(self):
        """Test basic creation."""
        result = AiGateResult(success=True, message="Test passed")
        assert result.success is True
        assert result.message == "Test passed"
        assert result.timestamp is not None

    def test_creation_with_timestamp(self):
        """Test creation with explicit timestamp."""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = AiGateResult(success=False, message="Failed", timestamp=ts)
        assert result.success is False
        assert result.timestamp == ts


class TestAiGateState:
    """Tests for AiGateState dataclass."""

    def test_empty_state(self):
        """Test empty state defaults."""
        state = AiGateState()
        assert state.last_check is None
        assert state.last_results == {}

    def test_is_stale_disabled(self):
        """Test is_stale returns False when disabled (interval_days=0)."""
        state = AiGateState()
        assert state.is_stale(0) is False

        # Even with old last_check, 0 means disabled
        state.last_check = datetime.now(timezone.utc) - timedelta(days=100)
        assert state.is_stale(0) is False

    def test_is_stale_first_run(self):
        """Test is_stale returns True on first run."""
        state = AiGateState()
        assert state.is_stale(7) is True

    def test_is_stale_fresh(self):
        """Test is_stale returns False when recently checked."""
        state = AiGateState()
        state.last_check = datetime.now(timezone.utc) - timedelta(days=1)
        assert state.is_stale(7) is False

    def test_is_stale_expired(self):
        """Test is_stale returns True when interval exceeded."""
        state = AiGateState()
        state.last_check = datetime.now(timezone.utc) - timedelta(days=8)
        assert state.is_stale(7) is True

    def test_is_stale_boundary(self):
        """Test is_stale at exactly the boundary."""
        state = AiGateState()
        # Exactly 7 days ago should trigger
        state.last_check = datetime.now(timezone.utc) - timedelta(days=7)
        assert state.is_stale(7) is True

        # 6 days 23 hours should not trigger
        state.last_check = datetime.now(timezone.utc) - timedelta(days=6, hours=23)
        assert state.is_stale(7) is False

    def test_mark_checked(self):
        """Test mark_checked updates state."""
        state = AiGateState()
        results = {
            "claude-code": (True, "Blocked git push --no-verify"),
            "gemini": (False, "Did not block"),
        }

        state.mark_checked(results)

        assert state.last_check is not None
        assert len(state.last_results) == 2
        assert state.last_results["claude-code"].success is True
        assert state.last_results["gemini"].success is False
        assert state.required_agent_types == ("claude-code", "gemini")

    def test_is_stale_when_required_agent_set_changes(self):
        """A fresh cache cannot stand in for hooks required by another mode."""
        state = AiGateState(
            last_check=datetime.now(timezone.utc),
            required_agent_types=("claude-code",),
        )

        assert state.is_stale(7, {"claude-code"}) is False
        assert state.is_stale(7, {"codex"}) is True

    def test_is_stale_when_required_agent_set_changes_from_empty(self):
        """A fresh no-provider cache cannot authorize a provider-backed mode."""
        state = AiGateState(
            last_check=datetime.now(timezone.utc),
            required_agent_types=(),
        )

        assert state.is_stale(7, set()) is False
        assert state.is_stale(7, {"codex"}) is True

    def test_to_dict(self):
        """Test serialization to dict."""
        ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        state = AiGateState(
            last_check=ts,
            last_results={
                "claude-code": AiGateResult(
                    success=True, message="Passed", timestamp=ts
                ),
            },
            required_agent_types=("claude-code",),
        )

        result = state.to_dict()

        assert result["last_check"] == "2024-01-15T12:00:00+00:00"
        assert result["last_results"]["claude-code"]["success"] is True
        assert result["required_agent_types"] == ["claude-code"]

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            "last_check": "2024-01-15T12:00:00+00:00",
            "last_results": {
                "claude-code": {
                    "success": True,
                    "message": "Passed",
                    "timestamp": "2024-01-15T12:00:00+00:00",
                },
            },
            "required_agent_types": ["claude-code"],
        }

        state = AiGateState.from_dict(data)

        assert state.last_check.year == 2024
        assert state.last_results["claude-code"].success is True
        assert state.required_agent_types == ("claude-code",)

    def test_from_dict_empty(self):
        """Test from_dict with minimal data."""
        state = AiGateState.from_dict({})
        assert state.last_check is None
        assert state.last_results == {}

    def test_roundtrip(self):
        """Test to_dict -> from_dict roundtrip."""
        state = AiGateState()
        state.mark_checked({
            "claude-code": (True, "Blocked push"),
            "gemini": (False, "Failed to block"),
        })

        restored = AiGateState.from_dict(state.to_dict())

        assert restored.last_check is not None
        assert len(restored.last_results) == 2
        assert restored.last_results["claude-code"].success is True
        assert restored.last_results["gemini"].success is False


class TestAiGateStatePersistence:
    """Tests for load/save functions."""

    def test_get_ai_gate_state_path(self, tmp_path):
        """Test path construction."""
        path = get_ai_gate_state_path(tmp_path)
        assert path == tmp_path / ".issue-orchestrator/ai-gate-state.json"

    def test_load_missing_file(self, tmp_path):
        """Test loading when file doesn't exist."""
        state = load_ai_gate_state(tmp_path)
        assert state.last_check is None
        assert state.last_results == {}

    def test_save_and_load(self, tmp_path):
        """Test save and load roundtrip."""
        state = AiGateState()
        state.mark_checked({
            "claude-code": (True, "Blocked"),
        })

        save_ai_gate_state(tmp_path, state)

        # Verify file exists
        state_path = get_ai_gate_state_path(tmp_path)
        assert state_path.exists()

        # Load and verify
        loaded = load_ai_gate_state(tmp_path)
        assert loaded.last_check is not None
        assert loaded.last_results["claude-code"].success is True

    def test_save_creates_parent_dir(self, tmp_path):
        """Test save creates parent directory if needed."""
        # Use a fresh path that definitely doesn't exist
        repo_root = tmp_path / "new-repo"
        repo_root.mkdir()

        state = AiGateState()
        state.mark_checked({"agent": (True, "OK")})

        save_ai_gate_state(repo_root, state)

        state_path = get_ai_gate_state_path(repo_root)
        assert state_path.exists()

    def test_load_corrupted_json(self, tmp_path):
        """Test loading handles corrupted JSON gracefully."""
        state_path = get_ai_gate_state_path(tmp_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text("not valid json{{{")

        state = load_ai_gate_state(tmp_path)

        # Should return empty state on error
        assert state.last_check is None
        assert state.last_results == {}

    def test_load_invalid_data(self, tmp_path):
        """Test loading handles invalid data structure."""
        state_path = get_ai_gate_state_path(tmp_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text('{"last_results": {"agent": "not a dict"}}')

        state = load_ai_gate_state(tmp_path)

        # Should return empty state on error
        assert state.last_check is None
        assert state.last_results == {}
