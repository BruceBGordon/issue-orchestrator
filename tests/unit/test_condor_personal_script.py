"""Shell-level contracts of the pool helper's config-dir selection."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "condor-personal.sh"


def _select(value: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && select_config_dir "$1"',
            "_",
            value,
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_single_directory_value_passes_through(tmp_path: Path) -> None:
    writable = tmp_path / "config.d"
    writable.mkdir()
    result = _select(str(writable), tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == str(writable)


def test_comma_list_prefers_existing_writable_entry(tmp_path: Path) -> None:
    missing = tmp_path / "share" / "config.d"
    writable = tmp_path / "etc-like" / "config.d"
    writable.mkdir(parents=True)
    result = _select(f"{missing},{writable}/", tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == str(writable)


def test_ubuntu_shaped_list_selects_the_etc_entry_when_neither_writable(
    tmp_path: Path,
) -> None:
    # Neither exists (as root-owned dirs would not be writable to a test):
    # the /etc/* entry is the local-admin location and must win.
    result = _select(
        "/usr/share/condor/config.d,/etc/condor/config.d/", tmp_path
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "/etc/condor/config.d"


def test_empty_value_fails(tmp_path: Path) -> None:
    result = _select("", tmp_path)
    assert result.returncode != 0


def test_personal_role_overlay_defines_a_complete_loopback_pool(
    tmp_path: Path,
) -> None:
    """The Linux role overlay must name every pool daemon and pair the
    loopback interface with a loopback CONDOR_HOST - one without the
    other strands discovery (proven in both directions on this PR)."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && write_personal_role_config "$1"',
            "_",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    generated = (tmp_path / "85-io-personal-role.conf").read_text()
    for daemon in ("MASTER", "COLLECTOR", "NEGOTIATOR", "SCHEDD", "STARTD"):
        assert daemon in generated
    assert "CONDOR_HOST = 127.0.0.1" in generated
    assert "NETWORK_INTERFACE = 127.0.0.1" in generated
    # Run-as-owner triplet: without it, system installs run jobs as the
    # slot user and every lane holds on the submitter's 0700 cwd.
    assert "UID_DOMAIN = $(FULL_HOSTNAME)" in generated
    assert "TRUST_UID_DOMAIN = TRUE" in generated
    assert "STARTER_ALLOW_RUNAS_OWNER = TRUE" in generated
    # Lane compatibility lives in the always-applied lane config, not here.
    assert "MOUNT_UNDER_SCRATCH" not in generated


def _stub_bin(tmp_path: Path, submit_body: str) -> Path:
    """Command stubs for the asynchronous probe, no scheduler required."""
    stubs = tmp_path / "stub-bin"
    stubs.mkdir()
    (stubs / "condor_submit").write_text(submit_body)
    (stubs / "condor_q").write_text(
        "#!/bin/bash\necho '1 5 held-for-testing runner'\n"
    )
    (stubs / "condor_rm").write_text(
        f"#!/bin/bash\ntouch '{tmp_path}/rm-witness'\nexit 0\n"
    )
    (stubs / "condor_config_val").write_text("#!/bin/bash\necho stub-config\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    return stubs


def _run_probe(tmp_path: Path, stubs: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'export PATH="{stubs}:$PATH" IO_CONDOR_PROBE_TIMEOUT=1 '
            f'TMPDIR="{tmp_path}"; source "{SCRIPT}" && assert_execution_invariant',
        ],
        capture_output=True,
        text=True,
    )


def _probe_dirs(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("io-condor-probe-*"))


def test_probe_submit_failure_reports_and_cleans_up(tmp_path: Path) -> None:
    """A nonzero condor_submit must reach the diagnostic and cleanup
    lines despite set -euo pipefail - the original assignment form
    exited the script before either."""
    stubs = _stub_bin(
        tmp_path, "#!/bin/bash\necho 'submit refused (stub)' >&2\nexit 1\n"
    )
    result = _run_probe(tmp_path, stubs)
    assert result.returncode == 70
    assert "could not submit" in result.stderr
    assert not _probe_dirs(tmp_path), "failed submit leaked the probe directory"


def test_probe_timeout_diagnoses_and_removes_the_job(tmp_path: Path) -> None:
    """A job that never writes its marker must produce the identity
    diagnostics, condor_rm the probe, and clean the directory."""
    stubs = _stub_bin(tmp_path, "#!/bin/bash\necho '7.0'\n")
    result = _run_probe(tmp_path, stubs)
    assert result.returncode == 70
    assert "execution probe FAILED" in result.stderr
    assert "config:" in result.stderr
    assert "probe:" in result.stderr
    assert (tmp_path / "rm-witness").exists(), "timed-out probe job was not removed"
    assert not _probe_dirs(tmp_path), "timed-out probe leaked its directory"


def test_probe_success_cleans_up_silently(tmp_path: Path) -> None:
    """A working pool: the stub 'runs' the job by writing the marker in
    the submit file's initialdir; the probe must succeed and remove its
    directory."""
    submit_body = (
        "#!/bin/bash\n"
        "dir=$(awk -F' = ' '/^initialdir/{print $2}' \"$2\")\n"
        "echo alive > \"$dir/proof\"\n"
        "echo '9.0'\n"
    )
    stubs = _stub_bin(tmp_path, submit_body)
    result = _run_probe(tmp_path, stubs)
    assert result.returncode == 0, result.stderr
    assert "execution probe ok" in result.stdout
    assert not _probe_dirs(tmp_path), "successful probe leaked its directory"


def test_ambient_role_decision_covers_both_branches(tmp_path: Path) -> None:
    for value, expected in (
        ("MASTER", 0),
        ("", 0),
        ("MASTER COLLECTOR NEGOTIATOR SCHEDD STARTD", 1),
    ):
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{SCRIPT}" && ambient_needs_personal_role "$1"',
                "_",
                value,
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected, (value, result.returncode)


def test_lane_config_always_disables_scratch_over_tmp(tmp_path: Path) -> None:
    """MOUNT_UNDER_SCRATCH is lane compatibility, not role topology:
    it must ride the ALWAYS-applied lane config so a complete ambient
    pool gets it too."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && write_lane_config "$1"',
            "_",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    generated = (tmp_path / "90-issue-orchestrator-lanes.conf").read_text()
    assert "MOUNT_UNDER_SCRATCH =" in generated
    assert "CONCURRENCY_LIMIT_DEFAULT = 1" in generated
