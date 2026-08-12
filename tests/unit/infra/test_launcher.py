"""Tests for the unified launcher module."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.infra.launcher import (
    LaunchResult,
    LaunchStatus,
    UnclassifiedLaunchStatusError,
    UnknownLaunchStatusError,
    launch_preflight_only,
    launch_subprocess,
    preflight,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.domain.repository_launch_selection import (
    RepositoryLaunchSelection,
)
from issue_orchestrator.infra.doctor.types import Check, DoctorResult


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.instances = 1
    config.launch_selection = RepositoryLaunchSelection.default()
    config.configuration_mode = "default"
    config.config_name = "default.yaml"
    config.config_fingerprint = "fingerprint"
    return config


def _ok_doctor(**_kw: object) -> DoctorResult:
    return DoctorResult(checks=[Check(name="Test", status="ok", detail="good")])


def _warning_doctor(**_kw: object) -> DoctorResult:
    return DoctorResult(
        checks=[Check(name="Repo", status="warning", detail="not configured")]
    )


def _error_doctor(**_kw: object) -> DoctorResult:
    return DoctorResult(
        checks=[Check(name="Hooks", status="error", detail="not installed")]
    )


def _mock_supervisor():
    """Create a mock supervisor with start/start_instances/stop."""
    sv = MagicMock()
    lock = MagicMock()
    lock.pid = 42
    lock.http_port = 8080
    lock.instance_id = None
    lock.configuration_mode = "default"
    lock.config_name = "default.yaml"
    lock.config_fingerprint = "fingerprint"
    sv.start.return_value = lock
    return sv


class TestLaunchStatus:
    """The shared vocabulary every consumer classifies outcomes with."""

    def test_every_non_start_outcome_is_a_failure(self):
        assert {status for status in LaunchStatus if status.is_failure} == {
            LaunchStatus.DOCTOR_ERROR,
            LaunchStatus.LAUNCH_ERROR,
            LaunchStatus.CONFIGURATION_CONFLICT,
        }

    def test_a_warning_or_lost_race_still_means_the_orchestrator_is_up(self):
        assert not LaunchStatus.OK.is_failure
        assert not LaunchStatus.DOCTOR_WARNING.is_failure
        assert not LaunchStatus.ALREADY_RUNNING.is_failure

    def test_every_status_declares_a_success_or_failure_disposition(self):
        """The guard: a member added to the enum cannot stay unclassified.

        Compared against ``set(LaunchStatus)`` — the enum itself — rather than
        against either disposition set, so adding ``SUPERVISOR_ERROR`` and
        forgetting to classify it fails here instead of quietly reading as a
        successful start at every consumer.
        """
        successes = LaunchStatus.success_statuses()
        failures = LaunchStatus.failure_statuses()

        assert successes | failures == set(LaunchStatus)
        assert not successes & failures

    def test_is_failure_never_defaults_to_success_for_an_unclassified_member(
        self, monkeypatch
    ):
        """Omission must raise, because "not a known failure" is not "started"."""
        monkeypatch.setattr(
            LaunchStatus, "failure_statuses", classmethod(lambda cls: frozenset())
        )
        monkeypatch.setattr(
            LaunchStatus, "success_statuses", classmethod(lambda cls: frozenset())
        )

        with pytest.raises(UnclassifiedLaunchStatusError) as excinfo:
            LaunchStatus.DOCTOR_ERROR.is_failure

        assert "doctor_error" in str(excinfo.value)

    def test_is_failure_rejects_conflicting_dispositions(self, monkeypatch):
        """Membership in both sets is as invalid as membership in neither."""
        doctor_error = frozenset({LaunchStatus.DOCTOR_ERROR})
        monkeypatch.setattr(
            LaunchStatus,
            "failure_statuses",
            classmethod(lambda cls: doctor_error),
        )
        monkeypatch.setattr(
            LaunchStatus,
            "success_statuses",
            classmethod(lambda cls: doctor_error),
        )

        with pytest.raises(UnclassifiedLaunchStatusError) as excinfo:
            LaunchStatus.DOCTOR_ERROR.is_failure

        assert "exactly one" in str(excinfo.value)

    @pytest.mark.parametrize(
        "value",
        [
            "ok",
            "doctor_warning",
            "already_running",
            "doctor_error",
            "launch_error",
            "configuration_conflict",
        ],
    )
    def test_parse_accepts_every_wire_value(self, value):
        assert LaunchStatus.parse(value).value == value

    def test_parse_is_idempotent_for_members(self):
        assert LaunchStatus.parse(LaunchStatus.OK) is LaunchStatus.OK

    @pytest.mark.parametrize("value", ["doctor_eror", "supervisor_error", "", "OK"])
    def test_parse_rejects_anything_else(self, value):
        """An unknown status must never quietly take a consumer's happy path."""
        with pytest.raises(UnknownLaunchStatusError) as excinfo:
            LaunchStatus.parse(value)

        assert repr(value) in str(excinfo.value)


class TestLaunchResult:
    def test_to_dict_minimal(self):
        doctor = DoctorResult(checks=[Check(name="Test", status="ok", detail="good")])
        result = LaunchResult(doctor=doctor, launched=False, status=LaunchStatus.OK)
        d = result.to_dict()
        assert d["launched"] is False
        assert d["status"] == "ok"
        assert d["doctor"]["overall"] == "ok"
        assert "error" not in d
        assert "supervisor" not in d

    def test_to_dict_with_error(self):
        doctor = DoctorResult(checks=[Check(name="Test", status="error", detail="bad")])
        result = LaunchResult(
            doctor=doctor,
            launched=False,
            status=LaunchStatus.DOCTOR_ERROR,
            error="check failed",
        )
        d = result.to_dict()
        assert d["error"] == "check failed"
        assert d["status"] == "doctor_error"

    def test_to_dict_with_supervisor(self):
        doctor = DoctorResult(checks=[])
        result = LaunchResult(
            doctor=doctor,
            launched=True,
            status=LaunchStatus.OK,
            supervisor={"pid": 123, "port": 8080},
        )
        d = result.to_dict()
        assert d["supervisor"]["pid"] == 123
        assert d["launched"] is True


class TestPreflight:
    def test_preflight_defaults_to_command_runner(self, mock_config):
        captured: dict[str, object] = {}

        def doctor(**kwargs: object) -> DoctorResult:
            captured.update(kwargs)
            return DoctorResult(checks=[Check(name="Test", status="ok", detail="good")])

        result = preflight(mock_config, doctor_fn=doctor)

        assert result.launched is False
        assert isinstance(captured["runner"], LocalCommandRunner)

    def test_preflight_ok(self, mock_config):
        result = preflight(mock_config, doctor_fn=_ok_doctor)
        assert result.status == "ok"
        assert result.launched is False

    def test_preflight_warning(self, mock_config):
        result = preflight(mock_config, doctor_fn=_warning_doctor)
        assert result.status == "doctor_warning"
        assert result.launched is False

    def test_preflight_error(self, mock_config):
        result = preflight(mock_config, doctor_fn=_error_doctor)
        assert result.status == "doctor_error"
        assert result.launched is False


class TestLaunchPreflightOnly:
    def test_is_alias_for_preflight(self, mock_config):
        result = launch_preflight_only(mock_config, doctor_fn=_ok_doctor)
        assert result.status == "ok"
        assert result.launched is False


class TestLaunchSubprocess:
    def test_rejects_selection_that_differs_from_preflighted_config(
        self,
        mock_config,
        tmp_path,
    ):
        with pytest.raises(ValueError, match="does not match the preflighted Config"):
            launch_subprocess(
                tmp_path,
                mock_config,
                config_name="main.yaml",
                mode="codex",
                doctor_fn=_ok_doctor,
                supervisor_ops=_mock_supervisor(),
            )

    def test_launch_subprocess_defaults_to_command_runner(self, mock_config, tmp_path):
        captured: dict[str, object] = {}

        def doctor(**kwargs: object) -> DoctorResult:
            captured.update(kwargs)
            return DoctorResult(checks=[Check(name="Test", status="ok", detail="good")])

        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=doctor, supervisor_ops=sv
        )

        assert result.launched is True
        assert isinstance(captured["runner"], LocalCommandRunner)

    def test_launch_ok(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=_ok_doctor, supervisor_ops=sv
        )
        assert result.launched is True
        assert result.status == "ok"
        assert result.supervisor["pid"] == 42
        assert result.supervisor["port"] == 8080
        assert (
            sv.start.call_args.kwargs["expected_config_fingerprint"]
            == "fingerprint"
        )

    def test_launch_blocked_by_doctor_error(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=_error_doctor, supervisor_ops=sv
        )
        assert result.launched is False
        assert result.status == "doctor_error"
        sv.start.assert_not_called()

    def test_launch_with_doctor_warning_still_launches(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=_warning_doctor, supervisor_ops=sv
        )
        assert result.launched is True
        assert result.status == "doctor_warning"

    def test_launch_start_paused_passes_supervisor_flag(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path,
            mock_config,
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
            start_paused=True,
        )
        assert result.launched is True
        sv.start.assert_called_once()
        assert sv.start.call_args.kwargs["start_paused"] is True

    def test_launch_log_level_passes_supervisor_flag(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        result = launch_subprocess(
            tmp_path,
            mock_config,
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
            log_level="DEBUG",
        )
        assert result.launched is True
        sv.start.assert_called_once()
        assert sv.start.call_args.kwargs["log_level"] == "DEBUG"

    def test_launch_supervisor_error(self, mock_config, tmp_path):
        sv = _mock_supervisor()
        sv.start.side_effect = RuntimeError("port in use")
        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=_ok_doctor, supervisor_ops=sv
        )
        assert result.launched is False
        assert result.status == "launch_error"
        assert "port in use" in result.error

    def test_launch_classifies_multi_instance_configuration_conflict(
        self, mock_config, tmp_path
    ):
        from issue_orchestrator.infra.repo_lock import ConfigurationIdentityConflict

        mock_config.instances = 2
        sv = _mock_supervisor()
        sv.start_instances.side_effect = ConfigurationIdentityConflict(
            tmp_path,
            active=("claude", "main.yaml", "claude-fingerprint"),
            requested=("codex", "main.yaml", "codex-fingerprint"),
        )

        result = launch_subprocess(
            tmp_path,
            mock_config,
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
        )

        assert result.launched is False
        assert result.status == "configuration_conflict"
        assert result.conflict == {
            "active": {
                "mode": "claude",
                "config_name": "main.yaml",
                "config_fingerprint": "claude-fingerprint",
            },
            "requested": {
                "mode": "codex",
                "config_name": "main.yaml",
                "config_fingerprint": "codex-fingerprint",
            },
        }

    def test_launch_multi_instance(self, mock_config, tmp_path):
        mock_config.instances = 3
        sv = _mock_supervisor()
        mock_info1 = MagicMock(pid=1, http_port=8081, instance_id="i1")
        mock_info2 = MagicMock(pid=2, http_port=8082, instance_id="i2")
        for info in (mock_info1, mock_info2):
            info.configuration_mode = "default"
            info.config_name = "default.yaml"
            info.config_fingerprint = "fingerprint"
        sv.start_instances.return_value = [mock_info1, mock_info2]

        result = launch_subprocess(
            tmp_path, mock_config, doctor_fn=_ok_doctor, supervisor_ops=sv
        )
        assert result.launched is True
        assert "instances" in result.supervisor
        assert len(result.supervisor["instances"]) == 2
        assert (
            sv.start_instances.call_args.kwargs["expected_config_fingerprint"]
            == "fingerprint"
        )

    def test_launch_multi_instance_start_paused_passes_supervisor_flag(
        self, mock_config, tmp_path
    ):
        mock_config.instances = 3
        sv = _mock_supervisor()
        mock_info = MagicMock(pid=1, http_port=8081, instance_id="i1")
        mock_info.configuration_mode = "default"
        mock_info.config_name = "default.yaml"
        mock_info.config_fingerprint = "fingerprint"
        sv.start_instances.return_value = [mock_info]

        result = launch_subprocess(
            tmp_path,
            mock_config,
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
            start_paused=True,
        )

        assert result.launched is True
        sv.start_instances.assert_called_once()
        assert sv.start_instances.call_args.kwargs["start_paused"] is True

    def test_launch_multi_instance_log_level_passes_supervisor_flag(
        self, mock_config, tmp_path
    ):
        mock_config.instances = 3
        sv = _mock_supervisor()
        mock_info = MagicMock(pid=1, http_port=8081, instance_id="i1")
        mock_info.configuration_mode = "default"
        mock_info.config_name = "default.yaml"
        mock_info.config_fingerprint = "fingerprint"
        sv.start_instances.return_value = [mock_info]

        result = launch_subprocess(
            tmp_path,
            mock_config,
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
            log_level="DEBUG",
        )

        assert result.launched is True
        sv.start_instances.assert_called_once()
        assert sv.start_instances.call_args.kwargs["log_level"] == "DEBUG"

    def test_launch_multi_instance_with_instance_id_starts_single(
        self, mock_config, tmp_path
    ):
        """When instance_id is provided, start only that instance even if
        config.instances > 1 (MCP auto-start targets one instance)."""
        mock_config.instances = 3
        sv = _mock_supervisor()

        result = launch_subprocess(
            tmp_path,
            mock_config,
            instance_id="i1",
            doctor_fn=_ok_doctor,
            supervisor_ops=sv,
        )
        assert result.launched is True
        sv.start.assert_called_once()
        sv.start_instances.assert_not_called()
        assert result.supervisor["pid"] == 42
