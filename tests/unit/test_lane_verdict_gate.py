"""The verdict layer's gate wiring: enabled where promised, nowhere else,
and the fail-one → re-run-one → all-cached lifecycle proven end to end.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _scrubbed_environment() -> dict[str, str]:
    # Hermetic against the hosting gate: when this test runs INSIDE a
    # verdict-enabled gate lane, LANE_VERDICT_* are exported into the
    # lane's environment and would leak into the sub-make under test —
    # the same hosting-gate-poisons-the-subprocess class as MAKEFLAGS
    # and the worker-width family.
    blocked = {
        "MAKEFLAGS",
        "MFLAGS",
        "MAKELEVEL",
        "LANE_EXECUTOR",
        "ISSUE_ORCHESTRATOR_LANE_EXECUTOR",
        "LANE_VERDICT_SHA",
        "LANE_VERDICT_LANES",
        "PARALLEL",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key not in blocked
        and not key.endswith("_PARALLEL")
        and not key.startswith("LANE_WORKERS_")
    }


def _make(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    make = shutil.which("gmake") or "make"
    return subprocess.run(
        [make, "-f", str(REPO_ROOT / "Makefile"), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_scrubbed_environment(),
    )


def _dry_run(target: str, *variables: str, cwd: Path) -> str:
    return _make("-n", target, *variables, cwd=cwd).stdout


def test_gate_lane_set_is_the_fan_itself(tmp_path: Path) -> None:
    """Drift guard: LANE_VERDICT_LANES is derived from the SAME
    variable the fan executes, so a lane added to the fan is cacheable
    by construction and can never be vacuously covered. Both sides come
    from dry-runs, never a hand-maintained list."""
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=condor", cwd=tmp_path)
    exported = re.search(r'LANE_VERDICT_LANES="([^"]+)"', phase)
    assert exported, "gate phase does not export the lane set"
    fan = _dry_run(
        "_validate-pr-flat-impl", "LANE_EXECUTOR=condor", cwd=tmp_path
    )
    fan_targets = set(re.findall(r'target="([^"]+)"', fan))
    assert fan_targets, "flat fan dry-run produced no targets - probe broken"
    assert set(exported.group(1).split()) == fan_targets


def test_gate_reads_the_tree_sha_exactly_once(tmp_path: Path) -> None:
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=condor", cwd=tmp_path)
    assert phase.count("git rev-parse HEAD") == 1


def test_direct_mode_gate_is_not_verdict_enabled(tmp_path: Path) -> None:
    """Deliberate exclusion (rationale in the Makefile): the direct
    phased topology has different leaves, and the re-run waste this
    layer removes lives in the condor publish-gate path."""
    phase = _dry_run("_validate-pr-impl", "LANE_EXECUTOR=direct", cwd=tmp_path)
    # The inert guard string appears in every TIMED_RUN body; what must
    # be absent is the ENABLEMENT — the export assignment.
    assert "LANE_VERDICT_SHA=$(git" not in phase
    assert 'LANE_VERDICT_LANES="' not in phase


def test_unenabled_timed_run_never_invokes_the_cli(tmp_path: Path) -> None:
    """Without the gate's exports the layer is inert: the guard is a
    plain env test, so validate-quick and friends pay nothing."""
    body = _dry_run("lint-complexity", "LANE_EXECUTOR=direct", cwd=tmp_path)
    assert 'if [ -z "$LANE_VERDICT_SHA" ]' in body


def _fake_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    environment = {
        **_scrubbed_environment(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=environment)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "x"],
        cwd=repo,
        check=True,
        env=environment,
    )
    return repo, _head(repo)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_all(repo: Path) -> str:
    """Commit everything in the fake repo: cache eligibility rightly
    disengages on untracked state, so test scaffolding must be
    committed to exercise the engaged paths."""
    environment = {
        **_scrubbed_environment(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=environment)
    subprocess.run(
        ["git", "commit", "-q", "-m", "scaffolding"],
        cwd=repo,
        check=True,
        env=environment,
    )
    return _head(repo)


def test_fail_one_rerun_one_then_all_cached(tmp_path: Path) -> None:
    """The whole prize, end to end with real make and the real CLI:
    a gate re-run after one lane's transient failure re-runs ONLY that
    lane, and a third run re-runs nothing."""
    repo, _ = _fake_repo(tmp_path)
    marker = tmp_path / "flaky-should-fail"
    marker.write_text("")
    extra = repo / "extra.mk"
    extra.write_text(
        "ok-lane:\n"
        "\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
        "flaky-lane:\n"
        "\t$(call TIMED_RUN,flaky-lane,"
        f"test ! -f {marker} && echo RAN-FLAKY)\n"
    )
    sha = _commit_all(repo)
    python = str(REPO_ROOT / ".venv" / "bin" / "python")

    def gate_run() -> subprocess.CompletedProcess[str]:
        make = shutil.which("gmake") or "make"
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={python}",
                "ok-lane",
                "flaky-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "ok-lane flaky-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    first = gate_run()
    assert first.returncode != 0
    assert "RAN-OK" in first.stdout
    assert "recorded-green" in first.stdout and "ok-lane" in first.stdout
    assert "RAN-FLAKY" not in first.stdout

    marker.unlink()
    second = gate_run()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "RAN-OK" not in second.stdout, "cached lane re-executed"
    assert "cached-green" in second.stdout
    assert "RAN-FLAKY" in second.stdout

    third = gate_run()
    assert third.returncode == 0
    assert "RAN-OK" not in third.stdout and "RAN-FLAKY" not in third.stdout
    assert third.stdout.count("cached-green") == 2


def test_cd_ing_multiline_recipe_records_and_caches(tmp_path: Path) -> None:
    """The first live gate's regression, pinned: a host-side recipe
    that ends inside a subdirectory (test-vscode ends in
    `cd packages/vscode && npm test`) left the record invocation
    running from the wrong cwd — a relative interpreter 127'd and the
    wrapper adopted that as the lane's status, failing a GREEN lane.
    The wrapper must be cwd-immune: worktree passed explicitly,
    interpreter absolutized."""
    repo, _ = _fake_repo(tmp_path)
    (repo / "subdir").mkdir()
    (repo / "subdir" / ".keep").write_text("")
    extra = repo / "extra.mk"
    extra.write_text(
        "cd-lane:\n"
        "\t$(call TIMED_RUN,cd-lane,"
        "if [ ! -d subdir ]; then \\\n"
        "\t\techo missing; exit 1; \\\n"
        "\tfi && \\\n"
        "\tcd subdir && echo RAN-CD)\n"
    )
    sha = _commit_all(repo)
    python = str(REPO_ROOT / ".venv" / "bin" / "python")
    make = shutil.which("gmake") or "make"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={python}",
                "cd-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "cd-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    first = run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "RAN-CD" in first.stdout
    assert "recorded-green" in first.stdout
    second = run()
    assert second.returncode == 0
    assert "RAN-CD" not in second.stdout
    assert "cached-green" in second.stdout


def test_record_failure_preserves_the_lane_outcome_exactly(
    tmp_path: Path,
) -> None:
    """Recording is best-effort AROUND the lane (round-1 finding 3):
    a red lane keeps its OWN exit status (record is not attempted),
    and a green lane whose record fails STAYS GREEN — a loud warning
    names the lane, NO verdict is left, and the next run re-runs
    instead of trusting anything. The wrapper never converts a lane's
    outcome into a store code."""
    repo, _ = _fake_repo(tmp_path)
    extra = repo / "extra.mk"
    extra.write_text(
        "red-lane:\n"
        "\t$(call TIMED_RUN,red-lane,sh -c 'exit 9')\n"
        "green-lane:\n"
        "\t$(call TIMED_RUN,green-lane,echo RAN-GREEN)\n"
    )
    sha = _commit_all(repo)
    make = shutil.which("gmake") or "make"

    def run(target: str, python: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={python}",
                target,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "red-lane green-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    real_python = str(REPO_ROOT / ".venv" / "bin" / "python")
    red = run("red-lane", real_python)
    assert red.returncode != 0
    assert "status=9" in red.stdout, "red lane's own exit status was altered"

    # Break record surgically while check still answers: a shim that
    # passes everything through except the record subcommand. It lives
    # OUTSIDE the repo so the worktree stays clean for eligibility.
    shim = tmp_path / "flaky-python.sh"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do [ "$a" = record ] && exit 127; done\n'
        f'exec {real_python} "$@"\n'
    )
    shim.chmod(0o755)
    green = run("green-lane", str(shim))
    assert green.returncode == 0, (
        "a green lane must stay green when recording fails:\n"
        + green.stdout
        + green.stderr
    )
    assert "RAN-GREEN" in green.stdout
    assert "status=0" in green.stdout
    assert "could not record" in green.stderr
    assert "green-lane" in green.stderr
    lanes_root = repo / ".issue-orchestrator" / "validation" / "lanes"
    assert not list(lanes_root.glob(f"{sha}/green-lane.json")), (
        "no verdict may be left behind by a failed recording"
    )
    # And the next run re-runs (nothing was cached).
    rerun = run("green-lane", real_python)
    assert rerun.returncode == 0
    assert "RAN-GREEN" in rerun.stdout


def test_unwritable_store_warns_and_lane_stays_green(tmp_path: Path) -> None:
    """Round-1 finding 3's exact repro: a 0555 store directory must
    not flip a successful lane — warn loudly, leave no verdict,
    preserve status 0."""
    repo, _ = _fake_repo(tmp_path)
    extra = repo / "extra.mk"
    extra.write_text(
        "green-lane:\n"
        "\t$(call TIMED_RUN,green-lane,echo RAN-GREEN)\n"
    )
    sha = _commit_all(repo)
    store_parent = repo / ".issue-orchestrator" / "validation" / "lanes"
    store_parent.mkdir(parents=True)
    store_parent.chmod(0o555)
    make = shutil.which("gmake") or "make"
    try:
        result = subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
                "green-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "green-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )
    finally:
        store_parent.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RAN-GREEN" in result.stdout and "status=0" in result.stdout
    assert "could not record" in result.stderr
    assert not list(store_parent.glob(f"{sha}/*.json"))


def test_nested_direct_make_never_consults_or_records(tmp_path: Path) -> None:
    """Round-1 finding 1: the scheduler lane's nested direct make
    inherits the gate environment, so its INNER TIMED_RUN recorded a
    green the OUTER lane had not earned — lane-run's own
    postconditions (journal, history) can still fail after the inner
    make succeeds, and the next gate then skipped on that phantom
    green. One execution owner: only the OUTER wrapper may consult or
    record; the wrapped command runs with the verdict environment
    scrubbed."""
    repo, _ = _fake_repo(tmp_path)
    extra = repo / "extra.mk"
    extra.write_text(
        "outer-lane:\n"
        "\t$(call TIMED_RUN,outer-lane,"
        f"$(GMAKE) -f {REPO_ROOT / 'Makefile'} -f extra.mk "
        "PYTHON=$(PYTHON) inner-step && exit 7)\n"
        "inner-step:\n"
        "\t$(call TIMED_RUN,inner-step,echo INNER-RAN)\n"
    )
    sha = _commit_all(repo)
    make = shutil.which("gmake") or "make"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
                "outer-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                # Both names are members: the inner target must be kept
                # out by the environment scrub, not by membership luck.
                "LANE_VERDICT_LANES": "outer-lane inner-step",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    first = run()
    assert first.returncode != 0
    assert "INNER-RAN" in first.stdout
    assert "recorded-green" not in first.stdout, (
        "a nested make minted a verdict the outer lane never earned:\n"
        + first.stdout
    )
    lanes_root = repo / ".issue-orchestrator" / "validation" / "lanes"
    assert not list(lanes_root.glob("*/*.json"))
    second = run()
    assert "cached-green" not in second.stdout
    assert "INNER-RAN" in second.stdout, "the failed lane must re-run fully"


def test_command_line_overrides_cannot_reach_a_nested_make(
    tmp_path: Path,
) -> None:
    """Round-2 finding B1: make forwards COMMAND-LINE variable
    assignments to sub-makes through MAKEFLAGS, bypassing the env
    unset entirely — the nested make re-instated the verdict vars as
    command-line definitions and recorded a green the failing outer
    lane never earned. The class fix is transport-based: the layer
    engages only when the variables arrive as ENVIRONMENT (the gate
    phase's sanctioned channel); override-origin delivery — argv here,
    or a hand-exported MAKEFLAGS, which a child also sees as
    command-line origin — is refused loudly at every make level, so
    the inner make is inert regardless of how the assignments travel.
    This runner deliberately does NOT scrub MAKEFLAGS."""
    repo, _ = _fake_repo(tmp_path)
    extra = repo / "extra.mk"
    extra.write_text(
        "outer-lane:\n"
        "\t$(call TIMED_RUN,outer-lane,"
        f"$(GMAKE) -f {REPO_ROOT / 'Makefile'} -f extra.mk "
        "PYTHON=$(PYTHON) inner-step && exit 7)\n"
        "inner-step:\n"
        "\t$(call TIMED_RUN,inner-step,echo INNER-RAN)\n"
    )
    sha = _commit_all(repo)
    make = shutil.which("gmake") or "make"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LANE_VERDICT_SHA", "LANE_VERDICT_LANES"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
                "outer-lane",
                f"LANE_VERDICT_SHA={sha}",
                "LANE_VERDICT_LANES=outer-lane inner-step",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    first = run()
    assert first.returncode != 0
    assert "INNER-RAN" in first.stdout
    assert "recorded-green" not in first.stdout, (
        "override-transported verdict vars reached a nested make:\n"
        + first.stdout
    )
    assert "ignoring LANE_VERDICT_" in first.stderr
    lanes_root = repo / ".issue-orchestrator" / "validation" / "lanes"
    assert not list(lanes_root.glob("*/*.json"))
    second = run()
    assert "cached-green" not in second.stdout
    assert "INNER-RAN" in second.stdout


def _mixed_origin_run(
    repo: Path,
    sha: str,
    *,
    sha_via: str,
    lanes_via: str,
) -> subprocess.CompletedProcess[str]:
    """Run ok-lane with each verdict variable delivered independently
    via 'env' or 'cli' transport (MAKEFLAGS deliberately not scrubbed)."""
    make = shutil.which("gmake") or "make"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LANE_VERDICT_SHA", "LANE_VERDICT_LANES"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    arguments = [
        make,
        "-f",
        str(REPO_ROOT / "Makefile"),
        "-f",
        "extra.mk",
        f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
        "ok-lane",
    ]
    if sha_via == "env":
        environment["LANE_VERDICT_SHA"] = sha
    else:
        arguments.append(f"LANE_VERDICT_SHA={sha}")
    if lanes_via == "env":
        environment["LANE_VERDICT_LANES"] = "ok-lane"
    else:
        arguments.append("LANE_VERDICT_LANES=ok-lane")
    return subprocess.run(
        arguments,
        cwd=repo,
        capture_output=True,
        text=True,
        env=environment,
    )


def _assert_layer_refused(
    first: "subprocess.CompletedProcess[str]",
    second: "subprocess.CompletedProcess[str]",
    repo: Path,
    *,
    naming: str,
    not_named: str = "",
) -> None:
    assert first.returncode == 0
    assert "RAN-OK" in first.stdout
    assert "recorded-green" not in first.stdout, first.stdout
    # The refusal names the EXACT offending variable(s), so an operator
    # sees which transport misfired, not a generic shrug.
    assert f"non-environment transport on: {naming} " in first.stderr, (
        first.stderr
    )
    if not_named:
        refusal = [
            line
            for line in first.stderr.splitlines()
            if "non-environment transport" in line
        ][0]
        named_section = refusal.split("transport on:")[1].split(" - ")[0]
        assert not_named not in named_section, refusal
    lanes_root = repo / ".issue-orchestrator" / "validation" / "lanes"
    assert not list(lanes_root.glob("*/*.json"))
    assert "cached-green" not in second.stdout
    assert "RAN-OK" in second.stdout


def test_mixed_origin_env_sha_cli_lanes_refuses_the_whole_layer(
    tmp_path: Path,
) -> None:
    """Round-3 finding: with environment-origin SHA but command-line
    LANES, the layer stayed fully engaged while the OVERRIDE supplied
    the gate-owned lane set (recorded, then cached-skipped, no refusal
    line). Engagement must require EVERY LANE_VERDICT_* variable to be
    environment-origin; one override-origin variable anywhere refuses
    the whole layer loudly."""
    repo, _ = _fake_repo(tmp_path)
    (repo / "extra.mk").write_text(
        "ok-lane:\n\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    first = _mixed_origin_run(repo, sha, sha_via="env", lanes_via="cli")
    second = _mixed_origin_run(repo, sha, sha_via="env", lanes_via="cli")
    _assert_layer_refused(
        first,
        second,
        repo,
        naming="LANE_VERDICT_LANES",
        not_named="LANE_VERDICT_SHA",
    )


def test_mixed_origin_cli_sha_env_lanes_refuses_the_whole_layer(
    tmp_path: Path,
) -> None:
    """The symmetric direction: command-line SHA with environment
    LANES must refuse identically."""
    repo, _ = _fake_repo(tmp_path)
    (repo / "extra.mk").write_text(
        "ok-lane:\n\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    first = _mixed_origin_run(repo, sha, sha_via="cli", lanes_via="env")
    second = _mixed_origin_run(repo, sha, sha_via="cli", lanes_via="env")
    _assert_layer_refused(
        first,
        second,
        repo,
        naming="LANE_VERDICT_SHA",
        not_named="LANE_VERDICT_LANES",
    )


def test_both_cli_refusal_names_both_variables(tmp_path: Path) -> None:
    repo, _ = _fake_repo(tmp_path)
    (repo / "extra.mk").write_text(
        "ok-lane:\n\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    first = _mixed_origin_run(repo, sha, sha_via="cli", lanes_via="cli")
    second = _mixed_origin_run(repo, sha, sha_via="cli", lanes_via="cli")
    _assert_layer_refused(
        first, second, repo, naming="LANE_VERDICT_SHA LANE_VERDICT_LANES"
    )


def _helper_override_run(
    repo: Path, sha: str, helper_assignment: str
) -> subprocess.CompletedProcess[str]:
    """Mixed-origin delivery PLUS a command-line assignment attacking
    a policy helper itself (round 4)."""
    make = shutil.which("gmake") or "make"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LANE_VERDICT_SHA", "LANE_VERDICT_LANES"}
    }
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    environment["LANE_VERDICT_SHA"] = sha
    return subprocess.run(
        [
            make,
            "-f",
            str(REPO_ROOT / "Makefile"),
            "-f",
            "extra.mk",
            f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
            "ok-lane",
            "LANE_VERDICT_LANES=ok-lane",
            helper_assignment,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_narrowing_the_declared_variable_set_cannot_bypass(
    tmp_path: Path,
) -> None:
    """Round-4 finding: the policy helpers are themselves make
    variables. LANE_VERDICT_VARIABLES=LANE_VERDICT_SHA narrowed the
    declared set so the origin check never inspected LANES, and the
    override-supplied lane set engaged silently. The `override`
    directive pins the policy definitions against command-line
    assignment at every make level."""
    repo, _ = _fake_repo(tmp_path)
    (repo / "extra.mk").write_text(
        "ok-lane:\n\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    first = _helper_override_run(
        repo, sha, "LANE_VERDICT_VARIABLES=LANE_VERDICT_SHA"
    )
    second = _helper_override_run(
        repo, sha, "LANE_VERDICT_VARIABLES=LANE_VERDICT_SHA"
    )
    _assert_layer_refused(first, second, repo, naming="LANE_VERDICT_LANES")


def test_blanking_the_override_collection_cannot_bypass(
    tmp_path: Path,
) -> None:
    """The sibling attack: LANE_VERDICT_OVERRIDDEN= blanked the
    collection wholesale, unconditionally engaging the layer."""
    repo, _ = _fake_repo(tmp_path)
    (repo / "extra.mk").write_text(
        "ok-lane:\n\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    first = _helper_override_run(repo, sha, "LANE_VERDICT_OVERRIDDEN=")
    second = _helper_override_run(repo, sha, "LANE_VERDICT_OVERRIDDEN=")
    _assert_layer_refused(first, second, repo, naming="LANE_VERDICT_LANES")


def test_untracked_state_disengages_the_cache_both_ways(
    tmp_path: Path,
) -> None:
    """Round-1 finding 2: HEAD alone is not the identity of what a
    lane consumed — the gate's tracked-mode dirty guard admits
    untracked files, so a new test file or config changes behavior
    under an unchanged key. Eligibility comes from the EXISTING
    dirty-policy owner (list_dirty_files("all") filtered of
    runtime-managed paths): any remaining path disengages BOTH
    recording and skipping, loudly. Over-inclusion is the fail-safe
    direction; no file-kind classification."""
    repo, _ = _fake_repo(tmp_path)
    extra = repo / "extra.mk"
    extra.write_text(
        "ok-lane:\n"
        "\t$(call TIMED_RUN,ok-lane,echo RAN-OK)\n"
    )
    sha = _commit_all(repo)
    make = shutil.which("gmake") or "make"

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                make,
                "-f",
                str(REPO_ROOT / "Makefile"),
                "-f",
                "extra.mk",
                f"PYTHON={REPO_ROOT / '.venv' / 'bin' / 'python'}",
                "ok-lane",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            env={
                **_scrubbed_environment(),
                "LANE_VERDICT_SHA": sha,
                "LANE_VERDICT_LANES": "ok-lane",
                "PYTHONPATH": str(REPO_ROOT / "src"),
            },
        )

    clean = run()
    assert clean.returncode == 0
    assert "recorded-green" in clean.stdout

    # An untracked consumed input appears: the cached green may no
    # longer be trusted, and nothing new may be minted.
    (repo / "novel-input.txt").write_text("changes behavior")
    dirty = run()
    assert dirty.returncode == 0
    assert "RAN-OK" in dirty.stdout, "stale green was trusted over new state"
    assert "cached-green" not in dirty.stdout
    assert "cache disengaged" in dirty.stdout + dirty.stderr

    # Runtime-managed untracked state (the store itself, diagnostics)
    # must NOT disengage: the filter is the existing owner's.
    (repo / "novel-input.txt").unlink()
    engaged_again = run()
    assert engaged_again.returncode == 0
    assert "cached-green" in engaged_again.stdout, (
        "runtime-managed paths (the verdict store) wrongly disengage:\n"
        + engaged_again.stdout
    )
