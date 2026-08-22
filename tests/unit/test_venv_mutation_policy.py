"""Behavioral coverage for the venv mutation policy, driven through real make.

These tests run the repository's actual Makefile against a fake ``uv`` that
records its argv. Source-text assertions cannot show what a recipe *did*, and
two of the defects these cover (a knowingly-stale environment reported as
success, and a dangling venv reported as success) were invisible to text
matching.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKE = shutil.which("gmake") or shutil.which("make")

pytestmark = pytest.mark.skipif(MAKE is None, reason="make is unavailable")


@dataclass(frozen=True)
class MakeRun:
    returncode: int
    stdout: str
    uv_calls: tuple[str, ...]

    @property
    def output(self) -> str:
        return self.stdout

    @property
    def _args(self) -> tuple[str, ...]:
        return tuple(call.split("|", 1)[1] for call in self.uv_calls if "|" in call)

    @property
    def targets(self) -> tuple[str, ...]:
        return tuple(call.split("|", 1)[0] for call in self.uv_calls if "|" in call)

    @property
    def project_targets(self) -> tuple[str, ...]:
        """Environments used for THIS project's operations.

        `--project` invocations steer an isolated tool environment (the pinned
        semgrep venv) and legitimately target a different path.
        """
        return tuple(
            call.split("|", 1)[0]
            for call in self.uv_calls
            if "|" in call and "--project" not in call.split("|", 1)[1]
        )

    def synced_project(self) -> bool:
        """Did any uv sync install this checkout's project into the venv?"""
        return any(
            a.startswith("sync") and "--no-install-project" not in a for a in self._args
        )

    def synced_dependencies_only(self) -> bool:
        return any(
            a.startswith("sync") and "--no-install-project" in a for a in self._args
        )

    def ran_uv(self) -> bool:
        return bool(self._args)


def _install_guard(checkout: Path) -> None:
    """Install the wrapper and the package resource it execs."""
    (checkout / "scripts").mkdir(parents=True, exist_ok=True)
    resource_dir = checkout / "src" / "issue_orchestrator" / "resources"
    resource_dir.mkdir(parents=True, exist_ok=True)
    for source, target in (
        (REPO_ROOT / "scripts" / "venv_guard.sh", checkout / "scripts" / "venv_guard.sh"),
        (
            REPO_ROOT / "src" / "issue_orchestrator" / "resources" / "venv_guard.sh",
            resource_dir / "venv_guard.sh",
        ),
    ):
        shutil.copy2(source, target)
        target.chmod(0o755)


def _make_checkout(tmp_path: Path, name: str = "checkout") -> Path:
    checkout = tmp_path / name
    (checkout / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "Makefile", checkout / "Makefile")
    _install_guard(checkout)
    (checkout / "pyproject.toml").write_text("[project]\nname='x'\n")
    (checkout / "uv.lock").write_text("# lock\n")
    return checkout


def _fake_uv(tmp_path: Path) -> tuple[Path, Path]:
    """A uv that records the environment it was pointed at, then its arguments.

    The target matters as much as the arguments: uv honours
    UV_PROJECT_ENVIRONMENT, so a mutation can be authorized for one environment
    and executed against another.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "uv-calls.log"
    fake = tmp_path / "fake-uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s|%s\\n" "${{UV_PROJECT_ENVIRONMENT:-none}}" "$*" >> {log}\n'
        # `uv venv .venv` fails when .venv already exists as a symlink, which is
        # the state a dangling link presents.
        'if [ "${1:-}" = "venv" ] && [ -L .venv ]; then exit 1; fi\n'
        'if [ "${1:-}" = "venv" ]; then mkdir -p .venv; fi\n'
        # The isolated semgrep tool env is a separate project; satisfy it so
        # unrelated recipe steps do not fail the run.
        'if [ "${1:-}" = "sync" ] && [ "${2:-}" = "--project" ] \\\n'
        '   && [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then\n'
        '  mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"\n'
        '  : > "$UV_PROJECT_ENVIRONMENT/bin/semgrep"\n'
        '  chmod +x "$UV_PROJECT_ENVIRONMENT/bin/semgrep"\n'
        "fi\n"
        "exit 0\n"
    )
    fake.chmod(0o755)
    return fake, log


def _run_make(
    checkout: Path,
    target: str,
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
    uv: Path | None = None,
    make_args: list[str] | None = None,
) -> MakeRun:
    fake_uv, log = _fake_uv(tmp_path)
    assert MAKE is not None
    proc = subprocess.run(
        [
            MAKE,
            target,
            f"UV={uv or fake_uv}",
            f"SETUP_LOG={tmp_path / 'setup.log'}",
            *(make_args or []),
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path), **(extra_env or {})},
    )
    calls = tuple(log.read_text().splitlines()) if log.exists() else ()
    return MakeRun(proc.returncode, proc.stdout + proc.stderr, calls)


def _share_venv_from(owner_root: Path, checkout: Path) -> None:
    """Link this checkout's .venv at another *checkout's* venv.

    The owner is given a pyproject.toml because that is what makes it a
    checkout. A venv under a directory that owns no project is a standalone
    environment nobody can be contaminated through.
    """
    owner_venv = owner_root / ".venv"
    owner_venv.mkdir(parents=True, exist_ok=True)
    (owner_root / "pyproject.toml").write_text("[project]\nname='owner'\n")
    (checkout / ".venv").symlink_to(owner_venv, target_is_directory=True)


# --------------------------------------------------------------- B1: staleness


def test_sync_deps_on_shared_venv_syncs_dependencies_rather_than_skipping(
    tmp_path: Path,
) -> None:
    """sync-deps runs *because* deps are stale; it must not report success idle.

    Skipping left every downstream test target running against an environment
    already known to be out of date, which can produce a false green.
    """
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode == 0, run.output
    assert run.synced_dependencies_only(), run.uv_calls
    assert not run.synced_project(), (
        "a shared venv must never receive this checkout's project install"
    )


def test_sync_deps_on_owned_venv_still_installs_the_project(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").mkdir()

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode == 0, run.output
    assert run.synced_project(), run.uv_calls


def test_sync_deps_fails_on_a_dangling_venv(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").symlink_to(tmp_path / "gone", target_is_directory=True)

    run = _run_make(checkout, "sync-deps", tmp_path)

    assert run.returncode != 0, run.output
    assert not run.uv_calls, "nothing may be synced into a broken environment"


# ------------------------------------------------------------- B2: venv-fast


def test_venv_fast_fails_on_a_dangling_venv(tmp_path: Path) -> None:
    """A dangling symlink is not an absent venv.

    ``[ ! -d .venv ]`` treated it as absent, ran ``uv venv`` (which fails), had
    that status overwritten by the next assignment, and still printed Done and
    exited 0.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").symlink_to(tmp_path / "gone", target_is_directory=True)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode != 0, f"venv-fast reported success on a dangling venv:\n{run.output}"
    assert "Done!" not in run.output


def test_venv_fast_on_shared_venv_succeeds_without_installing_the_project(
    tmp_path: Path,
) -> None:
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode == 0, run.output
    assert not run.synced_project(), run.uv_calls
    assert run.synced_dependencies_only(), run.uv_calls


def test_venv_fast_on_a_fresh_checkout_creates_and_fully_syncs(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)

    run = _run_make(checkout, "venv-fast", tmp_path)

    assert run.returncode == 0, run.output
    assert any(a.startswith("venv") for a in run._args), run.uv_calls
    assert run.synced_project(), run.uv_calls


# ------------------------------------------------- destructive targets refuse


@pytest.mark.parametrize("target", ["venv", "venv-pip"])
def test_destructive_targets_refuse_a_shared_venv(target: str, tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _share_venv_from(tmp_path / "owner", checkout)

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert (tmp_path / "owner" / ".venv").exists(), "the shared venv was destroyed"


# ------------------------------------------------- R1: shared freshness state


def test_one_checkout_sync_does_not_make_another_look_fresh(tmp_path: Path) -> None:
    """DEPS_MARKER lives in the venv, so on a shared venv it is another
    checkout's state. Reading it let B skip its sync because A had just
    stamped it -- with a different lock and freshly changed versions.
    """
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    a = _make_checkout(tmp_path, "a")
    b = _make_checkout(tmp_path, "b")
    _share_venv_from(owner, a)
    _share_venv_from(owner, b)

    first = _run_make(a, "sync-deps", tmp_path / "a-run")
    assert first.returncode == 0, first.output
    assert first.synced_dependencies_only(), first.uv_calls

    # B's lock predates the marker A just touched. It must still sync.
    second = _run_make(b, "sync-deps", tmp_path / "b-run")

    assert second.returncode == 0, second.output
    assert second.uv_calls, "B skipped its sync because A stamped the shared marker"
    assert second.synced_dependencies_only(), second.uv_calls


def test_shared_sync_does_not_stamp_the_owners_marker(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    checkout = _make_checkout(tmp_path, "a")
    _share_venv_from(owner, checkout)

    _run_make(checkout, "sync-deps", tmp_path / "run")

    assert not (owner / ".venv" / ".deps-synced").exists(), (
        "a non-owning checkout stamped the shared freshness marker"
    )


# --------------------------------------------------------- R2: fail closed


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_targets_fail_closed_when_the_guard_is_missing(target: str, tmp_path: Path) -> None:
    """A guard that cannot run is not evidence of ownership.

    Routing "any other exit code" to the else branch turned a missing guard
    into a full `uv sync --frozen --all-extras`.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / "scripts" / "venv_guard.sh").unlink()

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.synced_project(), run.uv_calls


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_targets_fail_closed_on_an_unexpected_guard_outcome(
    target: str, tmp_path: Path
) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / "scripts" / "venv_guard.sh").write_text("#!/usr/bin/env bash\nexit 7\n")
    (checkout / "scripts" / "venv_guard.sh").chmod(0o755)

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.synced_project(), run.uv_calls


# ================= the follow-up review's adversarial reproductions =========


def _guard_stub(checkout: Path, body: str) -> None:
    """Replace the wrapper with a stub, keeping the path callers use."""
    guard = checkout / "scripts" / "venv_guard.sh"
    guard.write_text("#!/usr/bin/env bash\n" + body)
    guard.chmod(0o755)


# ---- A1: decision and permitted arguments must arrive together -------------


@pytest.mark.parametrize(
    "body",
    [
        # classification succeeds, but the record carries no arguments
        'echo "outcome=shared"; exit 1\n',
        # empty output entirely
        "exit 1\n",
        # malformed record
        'echo "garbage"; exit 1\n',
        # arguments line present but empty
        'echo "outcome=shared"; echo "sync_args="; exit 1\n',
    ],
)
@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_no_uv_runs_when_the_decision_lacks_arguments(
    body: str, target: str, tmp_path: Path
) -> None:
    """A decision without its arguments must never degrade to a plain sync.

    Fetching the outcome and the argument set in separate executions let a
    failed second call expand to nothing, turning a restricted dependency sync
    into an unrestricted project install.
    """
    checkout = _make_checkout(tmp_path)
    _guard_stub(checkout, body)

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.ran_uv(), run.uv_calls


# ---- A2: the authorized target must bind the mutation ----------------------


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install", "venv"])
def test_ambient_project_environment_cannot_redirect_the_mutation(
    target: str, tmp_path: Path
) -> None:
    """uv honours UV_PROJECT_ENVIRONMENT; inheriting it moves the mutation.

    The local venv is authorized, then the install lands somewhere nobody
    authorized.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").mkdir()
    foreign = tmp_path / "foreign-owner" / ".venv"
    foreign.mkdir(parents=True)

    run = _run_make(
        checkout,
        target,
        tmp_path,
        extra_env={"UV_PROJECT_ENVIRONMENT": str(foreign)},
    )

    assert run.returncode == 0, run.output
    assert run.project_targets, run.uv_calls
    for used in run.project_targets:
        assert used == str(checkout / ".venv"), (
            f"uv was pointed at {used}, not the authorized {checkout / '.venv'}"
        )


# ---- A6: uv is required only where it actually syncs -----------------------


def test_sync_deps_succeeds_without_uv_when_owned_and_fresh(tmp_path: Path) -> None:
    """venv-pip supports systems with no uv and stamps the marker itself.

    Requiring uv before the freshness test broke `make test-unit` on exactly
    the fallback the Makefile advertises.
    """
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").mkdir()
    marker = checkout / ".venv" / ".deps-synced"
    marker.write_text("")
    import os as _os
    future = marker.stat().st_mtime + 1000
    _os.utime(marker, (future, future))

    run = _run_make(
        checkout, "sync-deps", tmp_path, uv=Path("/definitely/missing/uv")
    )

    assert run.returncode == 0, run.output


def test_sync_deps_still_requires_uv_when_it_must_sync(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").mkdir()

    run = _run_make(
        checkout, "sync-deps", tmp_path, uv=Path("/definitely/missing/uv")
    )

    assert run.returncode != 0, run.output
    assert "uv not found" in run.output


# ---- the refusal a caller prints must be actionable ------------------------


def test_make_refusal_tells_the_operator_how_to_fix_it(tmp_path: Path) -> None:
    """Every caller passes --quiet, so the guard's stderr guidance is unreachable.

    The remedy must arrive through the decision and be printed by the caller,
    or a refusal reads as a dead end.
    """
    checkout = _make_checkout(tmp_path)
    external = tmp_path / "envs" / "cc"
    external.mkdir(parents=True)

    run = _run_make(
        checkout, "sync-deps", tmp_path, extra_env={"MAKEFLAGS": ""},
        make_args=[f"VENV_TARGET={external}"],
    )

    assert run.returncode != 0, run.output
    assert "to fix:" in run.output.lower(), run.output
    assert "claim --venv" in run.output, run.output
    assert str(external) in run.output


def test_venv_target_checks_that_creation_succeeded(tmp_path: Path) -> None:
    """Dropping `set -e` left a failed `uv venv` followed by a sync anyway."""
    checkout = _make_checkout(tmp_path)
    failing = tmp_path / "failing-uv"
    failing.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "venv" ]; then exit 3; fi\n'
        f'printf "%s|%s\\n" "${{UV_PROJECT_ENVIRONMENT:-none}}" "$*" >> {tmp_path / "uv-calls.log"}\n'
        "exit 0\n"
    )
    failing.chmod(0o755)

    run = _run_make(checkout, "venv", tmp_path, uv=failing)

    assert run.returncode != 0, run.output
    assert not run.synced_project(), "synced into a venv that was never created"


# ---- the contract must be identical on every path -------------------------


GUARD_RESOURCE = REPO_ROOT / "src" / "issue_orchestrator" / "resources" / "venv_guard.sh"


def _answering_guard(checkout: Path, body: str) -> None:
    """A stub that lies in `decide` but delegates `check` to the real validator.

    `check` is the shared contract validator; stubbing it out too would test a
    caller against a validator that does not exist, rather than against the one
    that actually ships.
    """
    guard = checkout / "scripts" / "venv_guard.sh"
    guard.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "${{1:-}}" = "check" ]; then exec {GUARD_RESOURCE} "$@"; fi\n'
        + body
    )
    guard.chmod(0o755)


@pytest.mark.parametrize(
    "target,operation",
    [("venv", "recreate"), ("venv-pip", "recreate"),
     ("sync-deps", "sync-dependencies"), ("install", "sync-dependencies")],
)
def test_each_target_declares_the_operation_it_performs(
    target: str, operation: str, tmp_path: Path
) -> None:
    """`allowed` is meaningless if every caller asks the same generic question.

    Every Make decision defaulted to sync-dependencies, including targets that
    destroy and rebuild the environment, leaving safety to rest on a separate
    outcome check instead.
    """
    checkout = _make_checkout(tmp_path)
    log = tmp_path / "asked.log"
    _answering_guard(
        checkout,
        f'printf "%s\\n" "$*" >> {log}\n'
        'venv=""; operation=""\n'
        'while [ $# -gt 0 ]; do case "$1" in --venv) venv="$2"; shift ;; '
        '--operation) operation="$2"; shift ;; esac; shift; done\n'
        'echo "outcome=owned"; echo "sync_args=--frozen --all-extras"\n'
        'echo "venv=$venv"; echo "operation=$operation"; echo "allowed=yes"\n'
        "exit 0\n",
    )

    _run_make(checkout, target, tmp_path)

    asked = log.read_text() if log.exists() else ""
    assert f"--operation {operation}" in asked, asked


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_make_refuses_an_answer_about_a_different_environment(
    target: str, tmp_path: Path
) -> None:
    """The returned target must equal the requested one, on every path."""
    checkout = _make_checkout(tmp_path)
    _answering_guard(
        checkout,
        'echo "outcome=owned"; echo "sync_args=--frozen --all-extras"\n'
        'echo "venv=/somewhere/else"; echo "operation=sync-dependencies"\n'
        'echo "allowed=yes"\nexit 0\n',
    )

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.ran_uv(), run.uv_calls


@pytest.mark.parametrize("target", ["sync-deps", "venv-fast", "install"])
def test_make_refuses_an_answer_about_a_different_operation(
    target: str, tmp_path: Path
) -> None:
    checkout = _make_checkout(tmp_path)
    _answering_guard(
        checkout,
        'venv=""\nwhile [ $# -gt 0 ]; do case "$1" in --venv) venv="$2"; shift ;; esac; shift; done\n'
        'echo "outcome=owned"; echo "sync_args=--frozen --all-extras"\n'
        'echo "venv=$venv"; echo "operation=recreate"; echo "allowed=yes"\nexit 0\n',
    )

    run = _run_make(checkout, target, tmp_path)

    assert run.returncode != 0, run.output
    assert not run.ran_uv(), run.uv_calls


def test_make_refuses_when_the_operation_is_not_permitted(tmp_path: Path) -> None:
    """A shared venv may not be recreated, and the refusal must be actionable."""
    owner = tmp_path / "owner"
    (owner / ".venv").mkdir(parents=True)
    (owner / "pyproject.toml").write_text("[project]\nname='o'\n")
    checkout = _make_checkout(tmp_path)
    (checkout / ".venv").symlink_to(owner / ".venv", target_is_directory=True)

    run = _run_make(checkout, "venv", tmp_path)

    assert run.returncode != 0, run.output
    assert not run.ran_uv(), run.uv_calls
    assert (owner / ".venv").exists(), "the owner's environment was destroyed"
