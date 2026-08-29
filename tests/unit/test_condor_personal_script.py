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


def test_lane_config_never_states_per_job_accounting_unconditionally(
    tmp_path: Path,
) -> None:
    """The knob must NOT ride the always-applied lane config (PR #7135).

    An unwritable PER_JOB_HISTORY_DIR does not disable the feature, it
    EXCEPTs the schedd into a restart loop, so the knob may only ever be
    stated for a directory this script proved writable first. Anything
    that puts it back in the unconditional config re-arms that gun."""
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
    assert "PER_JOB_HISTORY_DIR" not in generated


def _per_job_history_config(
    tmp_path: Path,
    spool_value: str,
    config_dir: Path,
    extra_stubs: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run write_per_job_history_config against stubbed tools.

    ``sudo`` is stubbed as a plain passthrough so the privilege
    fallbacks are exercised deterministically: with a real sudo these
    assertions would depend on whether the host grants passwordless
    root (it does on CI runners and does not on a developer laptop),
    which is exactly the kind of environment-dependent test that hides
    the behaviour it claims to pin.
    """
    import os

    stubs = tmp_path / "stub-bin"
    stubs.mkdir(exist_ok=True)
    reader = stubs / "condor_config_val"
    reader.write_text(f"#!/bin/bash\nprintf '%s\\n' '{spool_value}'\n")
    reader.chmod(0o755)
    unprivileged_sudo = stubs / "sudo"
    unprivileged_sudo.write_text('#!/bin/bash\nexec "$@"\n')
    unprivileged_sudo.chmod(0o755)
    for name, body in (extra_stubs or {}).items():
        stub = stubs / name
        stub.write_text(body)
        stub.chmod(0o755)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'export PATH="{stubs}:$PATH"; '
            f'source "{SCRIPT}" && write_per_job_history_config "$1"',
            "_",
            str(config_dir),
        ],
        capture_output=True,
        text=True,
        env={**os.environ},
    )


_CONFIG_NAME = "93-io-per-job-history.conf"


def test_per_job_history_dir_is_created_world_writable(tmp_path: Path) -> None:
    """Condor never creates the directory, and the daemons may run as
    any uid (the submitting user on a tarball pool, `condor` on a system
    install). Mode 1777 makes writability true without guessing which -
    guessing is what crashed all three Linux pools in PR #7135."""
    spool = tmp_path / "spool"
    spool.mkdir()
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(tmp_path, str(spool), config_dir)
    assert result.returncode == 0, result.stderr

    history = spool / "per-job-history"
    assert history.is_dir()
    mode = history.stat().st_mode & 0o7777
    assert mode == 0o1777, oct(mode)
    assert (config_dir / _CONFIG_NAME).read_text().rstrip().endswith(
        f"PER_JOB_HISTORY_DIR = {history}"
    )


def test_an_existing_wrong_moded_directory_is_repaired(tmp_path: Path) -> None:
    """A directory left behind by the broken first attempt (root-owned,
    0755) must be REPAIRED, not inherited: skipping when it already
    exists is what would keep a poisoned pool poisoned."""
    spool = tmp_path / "spool"
    history = spool / "per-job-history"
    history.mkdir(parents=True, mode=0o755)
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(tmp_path, str(spool), config_dir)
    assert result.returncode == 0, result.stderr
    assert history.stat().st_mode & 0o7777 == 0o1777
    assert (config_dir / _CONFIG_NAME).exists()


def test_an_unpreparable_directory_writes_no_knob_at_all(tmp_path: Path) -> None:
    """The fail-safe: if the directory cannot be made writable, the pool
    must come up with per-job accounting simply OFF. Stating the knob
    anyway is the difference between losing the ClassAds and losing the
    schedd.

    SPOOL is a regular file here, so creating a directory beneath it is
    ENOTDIR for root as well - the impossibility does not depend on who
    runs the suite."""
    spool = tmp_path / "spool-is-a-file"
    spool.write_text("not a directory\n")
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(tmp_path, str(spool), config_dir)
    assert result.returncode == 0, result.stderr
    assert not (config_dir / _CONFIG_NAME).exists()
    assert "per-job accounting is off" in result.stderr


def test_a_regular_file_at_the_history_path_is_refused(tmp_path: Path) -> None:
    """Round 1 finding B, escape 1: `find -perm -0002` matches a
    world-writable FILE just as happily as a directory, so the knob was
    emitted for something the schedd cannot open as one."""
    spool = tmp_path / "spool"
    spool.mkdir()
    impostor = spool / "per-job-history"
    impostor.write_text("not a directory\n")
    impostor.chmod(0o777)
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(tmp_path, str(spool), config_dir)
    assert result.returncode == 0, result.stderr
    assert not (config_dir / _CONFIG_NAME).exists()
    assert "not a plain directory" in result.stderr
    assert impostor.is_file(), "the impostor must be left alone, not replaced"


def test_a_symlink_at_the_history_path_is_refused_before_any_chmod(
    tmp_path: Path,
) -> None:
    """Round 1 finding B, escape 2: `[ -d ]` follows symlinks, so a
    symlink here passed the existence check and the SUDO chmod below
    then re-moded whatever it pointed at. The refusal must happen before
    anything is chmodded, so the target's mode is the assertion."""
    spool = tmp_path / "spool"
    spool.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir(mode=0o700)
    (spool / "per-job-history").symlink_to(unrelated)
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(tmp_path, str(spool), config_dir)
    assert result.returncode == 0, result.stderr
    assert not (config_dir / _CONFIG_NAME).exists()
    assert "not a plain directory" in result.stderr
    assert unrelated.stat().st_mode & 0o7777 == 0o700, (
        "the symlink's target was re-moded by a chmod that should never "
        "have run"
    )


def test_a_failed_chmod_leaves_no_knob_even_though_it_is_world_writable(
    tmp_path: Path,
) -> None:
    """Round 1 finding B, escape 3: verifying only the other-write bit
    accepted a pre-existing 0777 directory whose chmod silently failed -
    world-writable but NOT sticky, so any uid could delete another's
    ClassAd. The check must assert the mode OUTCOME."""
    spool = tmp_path / "spool"
    history = spool / "per-job-history"
    history.mkdir(parents=True, mode=0o777)
    history.chmod(0o777)
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    result = _per_job_history_config(
        tmp_path,
        str(spool),
        config_dir,
        extra_stubs={"chmod": "#!/bin/bash\nexit 1\n"},
    )
    assert result.returncode == 0, result.stderr
    assert history.stat().st_mode & 0o7777 == 0o777, "stub chmod must be inert"
    assert not (config_dir / _CONFIG_NAME).exists()
    assert "sticky, world-writable" in result.stderr


def test_verification_escalates_like_the_creation_did(tmp_path: Path) -> None:
    """A system install's spool may be traversable only by condor and
    root. The writability check must escalate the same way the creation
    did, or a perfectly good directory reads as unusable and accounting
    is switched off on exactly the pools that need it.

    The sudo stub stands in for real root: it can see what the caller
    cannot, so this exercises the escalation branch end to end without
    the suite needing privilege."""
    import os

    spool = tmp_path / "spool"
    history = spool / "per-job-history"
    history.mkdir(parents=True)
    history.chmod(0o1777)
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()

    stubs = tmp_path / "stub-bin"
    stubs.mkdir(exist_ok=True)
    reader = stubs / "condor_config_val"
    reader.write_text(f"#!/bin/bash\nprintf '%s\\n' '{spool}'\n")
    reader.chmod(0o755)
    privileged_sudo = stubs / "sudo"
    privileged_sudo.write_text(
        "#!/bin/bash\n"
        f"chmod u+x '{spool}'\n"
        'test -n "$*" && "$@"\n'
        "status=$?\n"
        f"chmod u-x '{spool}'\n"
        "exit $status\n"
    )
    privileged_sudo.chmod(0o755)

    # Owner loses traverse: the direct check cannot reach the directory.
    spool.chmod(0o600)
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                f'export PATH="{stubs}:$PATH"; '
                f'source "{SCRIPT}" && write_per_job_history_config "$1"',
                "_",
                str(config_dir),
            ],
            capture_output=True,
            text=True,
            env={**os.environ},
        )
    finally:
        spool.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert (config_dir / _CONFIG_NAME).read_text().rstrip().endswith(
        f"PER_JOB_HISTORY_DIR = {history}"
    ), result.stderr


def test_unset_spool_writes_no_knob_and_says_so(tmp_path: Path) -> None:
    """Accounting is a diagnostic aid, not a precondition for running
    lanes: an exotic pool with no SPOOL loses the ClassAds, not `up`."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    result = _per_job_history_config(tmp_path, "", config_dir)
    assert result.returncode == 0, result.stderr
    assert not (config_dir / _CONFIG_NAME).exists()
    assert "per-job accounting is off" in result.stderr


def test_a_previously_written_knob_is_removed_when_preparation_fails(
    tmp_path: Path,
) -> None:
    """Symmetric lifecycle, same invariant as the backoff and capacity
    policies: a stale knob pointing at a directory nobody verified this
    run is exactly the configuration that crashes the schedd."""
    config_dir = tmp_path / "config.d"
    config_dir.mkdir()
    (config_dir / _CONFIG_NAME).write_text("PER_JOB_HISTORY_DIR = /gone\n")
    result = _per_job_history_config(tmp_path, "", config_dir)
    assert result.returncode == 0, result.stderr
    assert not (config_dir / _CONFIG_NAME).exists()


def test_the_knob_file_is_reconciled_by_the_install_boundary() -> None:
    """The Linux path COPIES staged files, which cannot delete, so the
    knob must be in the managed-optional set or a stale one survives
    every later install."""
    result = subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}" && echo "$MANAGED_OPTIONAL_CONFIGS"'],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert _CONFIG_NAME in result.stdout


def _write_lane_config(tmp_path: Path, **env: str) -> None:
    import os

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
        env={**os.environ, **env},
    )
    assert result.returncode == 0, result.stderr


def test_load_backoff_is_off_by_default(tmp_path: Path) -> None:
    """Freezing running work is opt-in: without the switch, no
    suspension policy is written at all."""
    _write_lane_config(tmp_path)
    assert not (tmp_path / "91-io-load-backoff.conf").exists()
    assert "SUSPEND" not in (
        tmp_path / "90-issue-orchestrator-lanes.conf"
    ).read_text()


def test_load_backoff_keys_on_machine_wide_owner_load(tmp_path: Path) -> None:
    """Two invariants: subtract condor's own load (or the policy trips
    on the gate's own fan), and use the MACHINE-wide Total* attributes
    (the unprefixed pair is per-slot on multi-core machines and gives
    different answers on different slots of the same host)."""
    _write_lane_config(tmp_path, IO_CONDOR_LOAD_BACKOFF="1")
    generated = (tmp_path / "91-io-load-backoff.conf").read_text()
    assert "OwnerLoadAvg = (TotalLoadAvg - TotalCondorLoadAvg)" in generated
    assert "$(OwnerLoadAvg) > 5.0" in generated
    assert "$(OwnerLoadAvg) < 2.0" in generated
    import re as _re

    assert not _re.search(r"[^l]\bLoadAvg\b", generated.replace("TotalLoadAvg", "").replace("TotalCondorLoadAvg", "").replace("CondorLoadAvg", "")), generated


def _intent(tmp_path: Path) -> dict[str, str]:
    written: dict[str, str] = {}
    for line in (tmp_path / "90-io-policy-intent.conf").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            written[key.strip()] = value.strip()
    return written


def test_intent_record_is_written_on_every_run(tmp_path: Path) -> None:
    """The opt-ins are environment variables read once, here. Without a
    persisted record no later reader can tell "opted out" from
    "removed by hand" — which is exactly how a hand-deleted backoff
    policy preflighted clean (C1, #7132 review). The sentinel is
    written in BOTH states so a pool that predates intent records is
    distinguishable from one that opted out."""
    _write_lane_config(tmp_path)
    assert _intent(tmp_path)["IO_INTENT_LOAD_BACKOFF"] == "False"

    _write_lane_config(tmp_path, IO_CONDOR_LOAD_BACKOFF="1")
    assert _intent(tmp_path)["IO_INTENT_LOAD_BACKOFF"] == "True"


def test_intent_record_tracks_the_policy_files_symmetrically(
    tmp_path: Path,
) -> None:
    """Intent and installed policy move together, in both directions:
    every state the installer can produce must be self-consistent, or
    the check it feeds would report drift on a pool the installer
    itself just built."""
    _write_lane_config(
        tmp_path, IO_CONDOR_LOAD_BACKOFF="1", IO_POOL_CAPACITY_PERCENT="150"
    )
    declared = _intent(tmp_path)
    assert declared["IO_INTENT_LOAD_BACKOFF"] == "True"
    assert declared["IO_INTENT_CAPACITY_PERCENT"] == "150"
    assert (tmp_path / "91-io-load-backoff.conf").exists()
    assert (tmp_path / "92-io-pool-capacity.conf").exists()

    # Opting back out must retract BOTH the policy files and the intent
    # that claimed them; a stale claim would fail the next preflight.
    _write_lane_config(tmp_path)
    declared = _intent(tmp_path)
    assert declared["IO_INTENT_LOAD_BACKOFF"] == "False"
    assert "IO_INTENT_CAPACITY_PERCENT" not in declared
    assert not (tmp_path / "91-io-load-backoff.conf").exists()
    assert not (tmp_path / "92-io-pool-capacity.conf").exists()


def test_capacity_intent_is_undefined_rather_than_empty(tmp_path: Path) -> None:
    """An empty config assignment reads back as "Not defined" from the
    config tool, so absence is the only encoding that means the same
    thing on both sides of the channel."""
    _write_lane_config(tmp_path)
    assert "IO_INTENT_CAPACITY_PERCENT" not in (
        tmp_path / "90-io-policy-intent.conf"
    ).read_text()


def test_capacity_intent_records_the_normalized_dial(tmp_path: Path) -> None:
    """Base-10 normalization, matching what write_capacity_config
    actually used: a leading-zero value must not record a different
    number than the one the pool was sized with."""
    _write_lane_config(tmp_path, IO_POOL_CAPACITY_PERCENT="080")
    assert _intent(tmp_path)["IO_INTENT_CAPACITY_PERCENT"] == "80"


def test_a_rejected_capacity_dial_records_no_intent_at_all(
    tmp_path: Path,
) -> None:
    """The capacity writer validates and rejects a malformed dial, and
    the intent record is written after it — so a run that installs no
    policy never leaves a record claiming it did."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'export IO_POOL_CAPACITY_PERCENT=abc; source "{SCRIPT}" '
            '&& write_lane_config "$1"',
            "_",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout
    assert not (tmp_path / "90-io-policy-intent.conf").exists()


def test_load_backoff_disable_removes_the_previous_policy(tmp_path: Path) -> None:
    """Enable then plain re-run must leave NO suspension policy behind:
    darwin writes into the persistent directory and linux copies only
    staged files, so a stale 91 file keeps freezing lanes after the
    operator opted out (B2, #7118 review)."""
    _write_lane_config(tmp_path, IO_CONDOR_LOAD_BACKOFF="1")
    assert (tmp_path / "91-io-load-backoff.conf").exists()
    _write_lane_config(tmp_path)
    assert not (tmp_path / "91-io-load-backoff.conf").exists()


def test_load_backoff_freezes_only_lanes_that_declared_it_safe(
    tmp_path: Path,
) -> None:
    """A live provider exchange frozen mid-turn thaws into a
    manufactured provider-outage failure; only lanes carrying
    SuspendableLane = True may freeze."""
    _write_lane_config(tmp_path, IO_CONDOR_LOAD_BACKOFF="1")
    generated = (tmp_path / "91-io-load-backoff.conf").read_text()
    assert generated.count("TARGET.SuspendableLane =?= True") == 2
    assert "WANT_SUSPEND = (TARGET.SuspendableLane =?= True)" in generated


def test_load_backoff_thresholds_are_overridable(tmp_path: Path) -> None:
    _write_lane_config(
        tmp_path,
        IO_CONDOR_LOAD_BACKOFF="1",
        IO_CONDOR_SUSPEND_LOAD="8.5",
        IO_CONDOR_CONTINUE_LOAD="3.0",
    )
    generated = (tmp_path / "91-io-load-backoff.conf").read_text()
    assert "> 8.5" in generated
    assert "< 3.0" in generated


def _physical_cores() -> int:
    """The script's own oracle, verbatim (sysctl, nproc fallback).

    NOT os.cpu_count(): pytest-xdist sets PYTHON_CPU_COUNT to the
    worker count in worker processes, so os.cpu_count() reports the
    xdist -n value, not the hardware — this test failed in the condor
    lane (12 workers) while passing on the host (18 workers = 18
    cores, a coincidence). Two different core detectors will always
    drift; the test must ask the one the script asks."""
    result = subprocess.run(
        ["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True
    )
    if result.returncode != 0:
        result = subprocess.run(["nproc"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_capacity_dial_is_unset_by_default(tmp_path: Path) -> None:
    """Without the dial, condor's own physical detection rules — no
    capacity file is written at all."""
    _write_lane_config(tmp_path)
    assert not (tmp_path / "92-io-pool-capacity.conf").exists()


def test_capacity_dial_scales_physical_cores(tmp_path: Path) -> None:
    """One throughput dial: NUM_CPUS = percent x physical cores. 150%
    is deliberate oversubscription for I/O-bound lane mixes; 50%
    throttles the whole pool uniformly."""
    _write_lane_config(tmp_path, IO_POOL_CAPACITY_PERCENT="150")
    generated = (tmp_path / "92-io-pool-capacity.conf").read_text()
    assert f"NUM_CPUS = {_physical_cores() * 150 // 100}" in generated

    _write_lane_config(tmp_path, IO_POOL_CAPACITY_PERCENT="50")
    generated = (tmp_path / "92-io-pool-capacity.conf").read_text()
    assert f"NUM_CPUS = {_physical_cores() * 50 // 100}" in generated


def test_capacity_dial_unset_removes_the_previous_setting(
    tmp_path: Path,
) -> None:
    """Symmetric lifecycle, same invariant as the backoff policy: a
    plain re-run must not leave a stale capacity override behind."""
    _write_lane_config(tmp_path, IO_POOL_CAPACITY_PERCENT="150")
    assert (tmp_path / "92-io-pool-capacity.conf").exists()
    _write_lane_config(tmp_path)
    assert not (tmp_path / "92-io-pool-capacity.conf").exists()


def test_capacity_dial_normalizes_leading_zeros_as_base_ten(
    tmp_path: Path,
) -> None:
    """B2 (#7122 review): bash arithmetic reads a leading zero as
    octal — an unnormalized '08' passed the digits-only check and then
    died with 'value too great for base', writing no config."""
    _write_lane_config(tmp_path, IO_POOL_CAPACITY_PERCENT="08")
    generated = (tmp_path / "92-io-pool-capacity.conf").read_text()
    assert f"NUM_CPUS = {max(1, _physical_cores() * 8 // 100)}" in generated
    assert "8% of" in generated


def test_capacity_dial_rejects_nonsense_loudly(tmp_path: Path) -> None:
    import os

    for bad in ("abc", "0", "-20", "1.5"):
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
            env={**os.environ, "IO_POOL_CAPACITY_PERCENT": bad},
        )
        assert result.returncode != 0, bad
        assert "IO_POOL_CAPACITY_PERCENT" in result.stderr


def test_install_boundary_reconciles_managed_files(tmp_path: Path) -> None:
    """B2 round two (#7118 review): the Linux path stages fresh and
    COPIES staged .conf files over the persistent destination — copying
    cannot delete, so a stale opt-in policy in the destination survived
    a disabled re-run. The install boundary must reconcile: a managed
    file absent from staging is removed from the destination."""
    staging = tmp_path / "staging"
    destination = tmp_path / "config.d"
    staging.mkdir()
    destination.mkdir()
    (destination / "91-io-load-backoff.conf").write_text("SUSPEND = stale\n")
    (staging / "90-issue-orchestrator-lanes.conf").write_text("x = 1\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && install_staged_configs "$1" "$2"',
            "_",
            str(staging),
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert not (destination / "91-io-load-backoff.conf").exists(), (
        "a disabled install left the stale opt-in policy in the destination"
    )
    assert (destination / "90-issue-orchestrator-lanes.conf").exists()


def test_install_boundary_installs_staged_managed_files(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    destination = tmp_path / "config.d"
    staging.mkdir()
    destination.mkdir()
    (staging / "91-io-load-backoff.conf").write_text("SUSPEND = fresh\n")
    (staging / "90-issue-orchestrator-lanes.conf").write_text("x = 1\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{SCRIPT}" && install_staged_configs "$1" "$2"',
            "_",
            str(staging),
            str(destination),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (destination / "91-io-load-backoff.conf").read_text() == "SUSPEND = fresh\n"
