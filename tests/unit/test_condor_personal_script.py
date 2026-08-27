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
