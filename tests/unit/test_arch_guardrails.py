from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_capturing(tmp_path: Path, code: str, rel_path: str):
    """Run the checker and return (exit code, combined output)."""
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(code))

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    repo_tools = _repo_root() / "tools"
    (tools_dir / "check_arch_guardrails.py").write_text(
        (repo_tools / "check_arch_guardrails.py").read_text()
    )
    (tools_dir / "ast_guardrails.yml").write_text(
        (repo_tools / "ast_guardrails.yml").read_text()
    )

    proc = subprocess.run(
        [sys.executable, "tools/check_arch_guardrails.py", "src"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _run(tmp_path: Path, code: str, rel_path: str) -> int:
    target = tmp_path / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(code))

    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    repo_tools = _repo_root() / "tools"
    (tools_dir / "check_arch_guardrails.py").write_text(
        (repo_tools / "check_arch_guardrails.py").read_text()
    )
    (tools_dir / "ast_guardrails.yml").write_text(
        (repo_tools / "ast_guardrails.yml").read_text()
    )

    proc = subprocess.run(
        [sys.executable, "tools/check_arch_guardrails.py", "src"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return proc.returncode


def test_blocks_subprocess_import_in_control(tmp_path: Path) -> None:
    assert (
        _run(tmp_path, "import subprocess\n", "src/issue_orchestrator/control/x.py")
        == 2
    )


def test_allows_subprocess_in_execution(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/execution/x.py") == 0


def test_blocks_dynamic_import(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path, "__import__('subprocess')\n", "src/issue_orchestrator/domain/x.py"
        )
        == 2
    )


def test_blocks_forbidden_call(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/ports/x.py") == 2


def test_blocks_git_subprocess_outside_allowed(tmp_path: Path) -> None:
    code = "import subprocess\nsubprocess.run(['git','status'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_allows_subprocess_in_infra_supervisor(tmp_path: Path) -> None:
    """Supervisor is allowed to use subprocess for process management."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/supervisor.py") == 0


def test_allows_subprocess_in_infra_ai_diagnose(tmp_path: Path) -> None:
    """AI diagnose is allowed to use subprocess for invoking claude."""
    code = "import subprocess\nsubprocess.run(['claude','--print'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/ai_diagnose.py") == 0


def test_blocks_subprocess_in_infra_generic(tmp_path: Path) -> None:
    """Generic infra files should NOT have subprocess access."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    # This should be blocked because it's not in the allow list
    assert _run(tmp_path, code, "src/issue_orchestrator/infra/some_other.py") == 2


def test_blocks_subprocess_in_domain(tmp_path: Path) -> None:
    """Domain layer must never use subprocess."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/domain/x.py") == 2


def test_blocks_subprocess_in_ports(tmp_path: Path) -> None:
    """Ports layer must never use subprocess."""
    code = "import subprocess\nsubprocess.run(['echo','hi'])\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/ports/x.py") == 2


def test_blocks_cached_label_reads_in_control(tmp_path: Path) -> None:
    """Control layer must use fresh label reads (no get_issue_labels)."""
    code = (
        "class X:\n"
        "    def __init__(self):\n"
        "        self.issue_tracker = None\n"
        "    def f(self):\n"
        "        self.issue_tracker.get_issue_labels(1)\n"
    )
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_blocks_github_adapter_import_in_control(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_blocks_github_adapter_import_in_entrypoint_non_composition(
    tmp_path: Path,
) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/entrypoints/cli.py") == 2


def test_allows_github_adapter_import_in_bootstrap(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert _run(tmp_path, code, "src/issue_orchestrator/entrypoints/bootstrap.py") == 0


def test_allows_github_adapter_import_in_provider_factory(tmp_path: Path) -> None:
    code = "from issue_orchestrator.adapters.github import GitHubAdapter\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/entrypoints/repository_host_factory.py",
        )
        == 0
    )


def test_blocks_github_symbol_reference_in_control(tmp_path: Path) -> None:
    code = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from issue_orchestrator.adapters.github import GitHubAdapter\n"
        "def f(x: GitHubAdapter) -> None:\n"
        "    return None\n"
    )
    assert _run(tmp_path, code, "src/issue_orchestrator/control/x.py") == 2


def test_blocks_raw_review_exchange_summary_get_in_control(tmp_path: Path) -> None:
    code = "def f(summary):\n    return summary.get('status')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/control/review_exchange_cache_resolution.py",
        )
        == 2
    )


def test_blocks_raw_review_exchange_summary_get_in_execution(tmp_path: Path) -> None:
    code = "def f(cached):\n    return cached.summary.get('reason')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/session_output_adapter.py",
        )
        == 2
    )


def test_blocks_raw_review_exchange_summary_get_in_review_artifacts(
    tmp_path: Path,
) -> None:
    code = "def f(summary):\n    return summary.get('artifacts')\n"
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/domain/review_artifacts.py",
        )
        == 2
    )


def test_blocks_review_exchange_summary_dict_parameter(tmp_path: Path) -> None:
    code = (
        "def store_review_exchange_summary(summary: dict[str, object]):\n"
        "    return summary\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/review_exchange_session_output.py",
        )
        == 2
    )


def test_blocks_review_exchange_outcome_dict_summary_constructor(
    tmp_path: Path,
) -> None:
    code = (
        "def f(ReviewExchangeOutcome, run_assets):\n"
        "    return ReviewExchangeOutcome(\n"
        "        status='ok', rounds=1, reason='reviewer_ok',\n"
        "        run_assets=run_assets,\n"
        "        summary={'status': 'ok'},\n"
        "    )\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/control/review_exchange_cache_resolution.py",
        )
        == 2
    )


def test_blocks_persistent_pair_run_rebind(tmp_path: Path) -> None:
    code = (
        "def f(pair, binding):\n"
        "    pair.run_dir = binding.run_dir\n"
        "    pair.exchange_run_id = binding.run_id\n"
    )
    assert (
        _run(
            tmp_path,
            code,
            "src/issue_orchestrator/execution/anywhere.py",
        )
        == 2
    )


# ---------------------------------------------------------------------------
# The shared needs-human block boundary (#6999 F2)
# ---------------------------------------------------------------------------
#
# A guardrail nobody has watched fail is a guardrail nobody knows works. These
# pin the two supported bypass forms - a direct write of the governed label, and
# a label action that names it without a cause - and the one audited escape.
# What the checker CANNOT see is pinned too, because that limit is the reason
# the runtime capability exists beside it.

_CONTROL = "src/issue_orchestrator/control/x.py"
# Direct label writes are separately forbidden in control by the older
# ``control_no_direct_label_mutations`` rule, so the ALLOW cases below live
# where that rule does not apply - otherwise they would prove nothing about
# this one.
_EXECUTION = "src/issue_orchestrator/execution/x.py"


def test_blocks_a_direct_write_of_the_governed_label(tmp_path: Path) -> None:
    code, out = _run_capturing(
        tmp_path,
        """
        def sneak(labels, lm, issue_number):
            labels.add_label(issue_number, lm.needs_human)
        """,
        _CONTROL,
    )
    assert code == 2
    assert "shared-needs-human-block-bypass" in out


def test_blocks_a_direct_removal_of_the_governed_label(tmp_path: Path) -> None:
    code, out = _run_capturing(
        tmp_path,
        """
        def sneak(labels, lm, issue_number):
            labels.remove_label(issue_number, lm.needs_human)
        """,
        _CONTROL,
    )
    assert code == 2
    assert "shared-needs-human-block-bypass" in out


def test_blocks_a_label_action_that_names_no_cause(tmp_path: Path) -> None:
    code, out = _run_capturing(
        tmp_path,
        """
        from .actions import AddLabelAction

        def sneak(lm, issue_number):
            return AddLabelAction(
                issue_number=issue_number, label=lm.needs_human, reason="x"
            )
        """,
        _CONTROL,
    )
    assert code == 2
    assert "shared-needs-human-block-uncaused" in out


def test_allows_a_label_action_that_names_its_cause(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            """
            from .actions import AddLabelAction
            from .needs_human_block import NeedsHumanCause

            def owned(lm, issue_number):
                return AddLabelAction(
                    issue_number=issue_number,
                    label=lm.needs_human,
                    reason="x",
                    needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                )
            """,
            _CONTROL,
        )
        == 0
    )


def test_allows_the_one_audited_ungoverned_fallback(tmp_path: Path) -> None:
    """A composition with no owner must still perform the write.

    Turning a real mutation into a silent no-op is worse than the bypass, so
    the escape exists - spelled out at the call site so it stays greppable and
    has to be justified.
    """
    assert (
        _run(
            tmp_path,
            """
            def fallback(labels, action):
                labels.add_label(  # shared-block: ungoverned fallback
                    action.pr_number, action.needs_human_label
                )
            """,
            _EXECUTION,
        )
        == 0
    )


def test_leaves_ordinary_labels_alone(tmp_path: Path) -> None:
    assert (
        _run(
            tmp_path,
            """
            def ordinary(labels, lm, issue_number):
                labels.add_label(issue_number, lm.in_progress)
                labels.remove_label(issue_number, lm.pr_pending)
            """,
            _EXECUTION,
        )
        == 0
    )


def test_a_dynamic_label_value_is_beyond_the_checker(tmp_path: Path) -> None:
    """The documented limit, pinned so nobody mistakes it for coverage.

    A value that arrives at runtime - an agent's ``pr_labels`` entry, a member
    of a planner-assembled collection - has no spelling for an AST check to
    recognise. That is exactly why ``GovernedLabelSet`` refuses the label BY
    VALUE at the capability: the two together cover what neither can alone.
    """
    assert (
        _run(
            tmp_path,
            """
            def dynamic(labels, issue_number, agent_supplied):
                for label in agent_supplied:
                    labels.add_label(issue_number, label)
            """,
            _EXECUTION,
        )
        == 0
    )


# --- one owner for provider-output classification (#6999) -------------------
#
# The startup AI gate had grown its own auth-banner table beside the provider
# classifier's, and the two had already drifted apart. These pin that the CLI
# entrypoint - the form CI runs - rejects a second classifier, and that the
# provider adapters keep the interpretation that legitimately belongs to them.

_PROVIDER_ADAPTER = "src/issue_orchestrator/execution/agent_runner_providers/newcli.py"


def test_blocks_a_second_provider_output_token_table(tmp_path: Path) -> None:
    """Caught by shape, so new wording is not an escape route."""
    code, out = _run_capturing(
        tmp_path,
        """
        _MY_OWN_MARKERS = ("please re-login now", "handshake rejected")


        def looks_dead(output):
            return any(m in output.lower() for m in _MY_OWN_MARKERS)
        """,
        "src/issue_orchestrator/infra/watcher.py",
    )
    assert code == 2
    assert "provider-output-token-table" in out


def test_allows_a_provider_adapter_to_read_its_own_cli_banners(tmp_path: Path) -> None:
    """Raw interpretation inside a provider adapter is the boundary working."""
    assert (
        _run(
            tmp_path,
            """
            _BANNERS = ("login expired", "please run /login")


            def looks_dead(output):
                return any(b in output.lower() for b in _BANNERS)
            """,
            _PROVIDER_ADAPTER,
        )
        == 0
    )
