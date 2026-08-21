"""Every environment-mutating call site must be authorized by the guard.

The first version of this boundary was a hand-written list of make targets. A
list is only as good as the next author's memory, and it already missed
``deps-batch``, ``scripts/prepare_release.py``, and ``start_control_center.sh``.
This module *discovers* mutation sites instead, so a new one fails the build
until it either routes through ``scripts/venv_guard.sh`` or carries an explicit,
justified exemption.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# A site is authorized if its enclosing block reaches the guard, directly or
# through one of the named wrappers that expand to it.
GUARD_REFERENCES = (
    "venv_guard.sh",       # direct invocation
    "VENV_GUARD",          # Makefile variable
    "venv_sync",           # Makefile macro -> guard
    "venv_require_owned",  # Makefile macro -> guard
    "require_owned_venv",  # prepare_release.py helper -> guard
    "venv_mutation_outcome",  # start_control_center.sh helper -> guard
    "venv_decide",         # Makefile macro -> guard
    "authority.authorize", # VenvMutationAuthority -> guard
    "venv_guard_check",    # test double for the scanner's own tests
    ".authorize(",         # any call into VenvMutationAuthority
)

# An executed command that creates or rewrites a Python environment.
#
# Matched per language, because the two express commands differently and the
# difference is what separates a call from prose. Python builds argv as
# separate string elements (`["uv", "sync", ...]`), so requiring that form
# never matches a sentence like f"uv sync failed for {venv}", while still
# catching the variable form `[uv, "sync", ...]`.
SHELL_MUTATION = re.compile(
    r"""(
        uv \s+ sync
      | \$\(UV\) \s+ sync
      | uv \s+ venv
      | \$\(UV\) \s+ venv
      | uv \s+ pip \s+ install
      | pip \s+ install \s+ -e
      | -m \s+ pip \s+ install
    )""",
    re.VERBOSE,
)

PY_MUTATION = re.compile(
    r"""(
        ["']?\buv\b["']? \s* , \s* ["'](sync|venv)["']
      | ["']?\buv\b["']? \s* , \s* ["']pip["'] \s* , \s* ["']install["']
      | ["']-m["'] \s* , \s* ["']pip["'] \s* , \s* ["']install["']
      | ["']pip["'] \s* , \s* ["']install["'] \s* , \s* ["']-e["']
    )""",
    re.VERBOSE,
)

# Occurrences that only *mention* a command: comments and help text.
NON_EXECUTING = re.compile(
    r"""(^\s*\#) | (\becho\b) | (\bprintf\b) | (\bhelp\s*=) | (doc_examples)""",
    re.VERBOSE,
)

EXEMPTION = re.compile(r"venv-guard:\s*exempt\s*[-—:]\s*(?P<reason>.+)")

# Discovery must cover every place a mutation can be *executed*, not just the
# build and script layers. Source code runs these commands too: the E2E worktree
# manager syncs a worktree venv, and orchestrator worktrees have .venv symlinked
# at the base venv, so that path is a first-class contamination vector.
SCANNED = [
    REPO_ROOT / "Makefile",
    *sorted((REPO_ROOT / "scripts").rglob("*.sh")),
    *sorted((REPO_ROOT / "scripts").rglob("*.py")),
    *sorted((REPO_ROOT / "src").rglob("*.py")),
]


@dataclass(frozen=True)
class Site:
    path: Path
    line_no: int
    line: str

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.line_no}: {self.line.strip()[:100]}"


def _string_literal_lines(path: Path) -> frozenset[int]:
    """Line numbers inside Python string literals (docstrings included).

    Prose that merely *names* a command cannot mutate anything. Deciding that
    from the text of a single line fails on continuation lines of a docstring,
    so the literals are located structurally instead.
    """
    if path.suffix != ".py":
        return frozenset()
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return frozenset()
    covered: set[int] = set()
    for node in ast.walk(tree):
        # Only PROSE: a docstring or a bare string expression statement. Not
        # every string constant -- `subprocess.run(["uv", "sync"])` is a real
        # call whose command lives in string literals, and skipping those lines
        # would hide exactly what this scanner exists to find.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                covered.update(
                    range(node.lineno, (node.end_lineno or node.lineno) + 1)
                )
    return frozenset(covered)


def _executing_mutation_sites(path: Path) -> list[Site]:
    pattern = PY_MUTATION if path.suffix == ".py" else SHELL_MUTATION
    literals = _string_literal_lines(path)
    sites: list[Site] = []
    for index, line in enumerate(path.read_text().splitlines(), start=1):
        if not pattern.search(line) or NON_EXECUTING.search(line):
            continue
        if index in literals:
            continue
        sites.append(Site(path, index, line))
    return sites


def _makefile_block(lines: list[str], line_no: int) -> str:
    """The recipe containing this line: back up to its target, forward to a gap."""
    start = line_no - 1
    while start > 0 and not re.match(r"^[A-Za-z0-9_.-]+:", lines[start]):
        start -= 1
    end = line_no
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[start:end])


def _shell_block(lines: list[str], line_no: int) -> str:
    start = line_no - 1
    while start > 0 and not re.match(r"^\s*[A-Za-z0-9_]+\s*\(\)\s*\{", lines[start]):
        start -= 1
    end = line_no
    while end < len(lines) and not re.match(r"^\}", lines[end]):
        end += 1
    return "\n".join(lines[start : end + 1])


def _authorization_dominates(path: Path, line_no: int) -> bool:
    """Does authorization run before this mutation on EVERY path to it?

    A guard reference anywhere in the function is not proof for every branch:
    the E2E sync authorized inside ``if pyproject.exists()`` while the
    no-pyproject fallback below it mutated the same environment unguarded, and
    the scanner still reported the function authorized.

    The check is deliberately strict: authorization must appear at the
    function's top level (so it cannot be skipped by a branch) and before the
    mutation line.
    """
    if path.suffix != ".py":
        return False
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (node.lineno <= line_no <= (node.end_lineno or node.lineno)):
            continue
        for statement in node.body:
            if (statement.end_lineno or statement.lineno) >= line_no:
                break
            # Only statements that execute unconditionally can dominate.
            # Unparsing an `if` would include its branches, so a guard call
            # inside ONE branch would count for a sibling branch too -- exactly
            # the hole this check exists to close.
            if not isinstance(
                statement, (ast.Expr, ast.Assign, ast.AnnAssign, ast.AugAssign)
            ):
                continue
            text = ast.unparse(statement)
            if any(reference in text for reference in GUARD_REFERENCES):
                return True
    return False


def _python_block(path: Path, line_no: int) -> str:
    source = path.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if node.lineno <= line_no <= end:
            if best is None or node.lineno > best.lineno:
                best = node
    if best is None:
        return source
    return "\n".join(lines[best.lineno - 1 : (best.end_lineno or best.lineno)])


def _enclosing_block(site: Site) -> str:
    lines = site.path.read_text().splitlines()
    if site.path.suffix == ".py":
        return _python_block(site.path, site.line_no)
    if site.path.suffix == ".sh":
        return _shell_block(lines, site.line_no)
    return _makefile_block(lines, site.line_no)


def _is_exempt(site: Site) -> str | None:
    """An exemption applies to the enclosing block, as authorization does.

    A fixed line window cannot express "this recipe is exempt" when the recipe
    is a multi-line continuation, and putting the marker inside such a block is
    fragile: a shell comment would swallow the rest of a continued command.
    """
    match = EXEMPTION.search(_enclosing_block(site))
    return match.group("reason").strip() if match else None


def _unauthorized(paths: list[Path]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if path.name == "venv_guard.sh":
            continue
        for site in _executing_mutation_sites(path):
            if _is_exempt(site):
                continue
            if path.suffix == ".py":
                # Python is checked structurally: authorization must dominate
                # the mutation, not merely appear somewhere in the function.
                if _authorization_dominates(path, site.line_no):
                    continue
            elif any(ref in _enclosing_block(site) for ref in GUARD_REFERENCES):
                continue
            findings.append(str(site))
    return findings


def test_scanner_rejects_authorization_that_only_covers_one_branch(tmp_path: Path) -> None:
    """One guarded branch is not proof for a sibling branch that also mutates."""
    module = tmp_path / "branchy.py"
    module.write_text(
        "import subprocess\n\n\n"
        "def sync(path):\n"
        "    if path.exists():\n"
        "        venv_guard_check()\n"
        '        subprocess.run(["uv", "sync", "--frozen"])\n'
        "        return\n"
        '    subprocess.run(["uv", "venv", ".venv"])\n'
        '    subprocess.run(["uv", "pip", "install", "pytest"])\n'
    )

    sites = _executing_mutation_sites(module)
    unguarded = [s for s in sites if not _authorization_dominates(module, s.line_no)]

    assert len(unguarded) >= 2, "the fallback branch mutates unauthorized"


def test_scanner_accepts_authorization_above_the_branches(tmp_path: Path) -> None:
    module = tmp_path / "hoisted.py"
    module.write_text(
        "import subprocess\n\n\n"
        "def sync(path):\n"
        "    decision = venv_guard_check()\n"
        "    if path.exists():\n"
        '        subprocess.run(["uv", "sync", "--frozen"])\n'
        "        return\n"
        '    subprocess.run(["uv", "venv", ".venv"])\n'
    )

    for site in _executing_mutation_sites(module):
        assert _authorization_dominates(module, site.line_no), site


def test_scanner_flags_an_unguarded_mutation(tmp_path: Path) -> None:
    """Negative control: a scanner that never fires would pass vacuously."""
    rogue = tmp_path / "rogue.sh"
    rogue.write_text("setup() {\n  uv sync --frozen --all-extras\n}\n")

    sites = _executing_mutation_sites(rogue)

    assert len(sites) == 1
    assert not _is_exempt(sites[0])
    assert not any(ref in _enclosing_block(sites[0]) for ref in GUARD_REFERENCES)


def test_scanner_accepts_a_guarded_mutation(tmp_path: Path) -> None:
    ok = tmp_path / "ok.sh"
    ok.write_text(
        "setup() {\n  scripts/venv_guard.sh || return 1\n"
        "  uv sync --frozen --all-extras\n}\n"
    )

    site = _executing_mutation_sites(ok)[0]

    assert any(ref in _enclosing_block(site) for ref in GUARD_REFERENCES)


def test_scanner_honours_a_justified_exemption(tmp_path: Path) -> None:
    exempt = tmp_path / "exempt.sh"
    exempt.write_text(
        "setup() {\n  # venv-guard: exempt - pinned to an isolated tool env\n"
        "  uv sync --frozen\n}\n"
    )

    site = _executing_mutation_sites(exempt)[0]

    assert _is_exempt(site) == "pinned to an isolated tool env"


def test_scanner_ignores_commands_named_inside_docstrings(tmp_path: Path) -> None:
    """Prose that names a command cannot run it, even across continuation lines."""
    doc = tmp_path / "doc.py"
    doc.write_text(
        '"""Module.\n\nThe orchestrator is installed editable\n'
        '(``pip install -e .``) so imports resolve.\n"""\n'
    )

    assert _executing_mutation_sites(doc) == []


def test_scanner_still_sees_a_real_call_in_a_documented_module(tmp_path: Path) -> None:
    real = tmp_path / "real.py"
    real.write_text(
        '"""Docs mentioning uv sync."""\n'
        "import subprocess\n\n\n"
        "def go():\n"
        '    subprocess.run(["uv", "sync", "--frozen"])\n'
    )

    assert len(_executing_mutation_sites(real)) == 1


def test_the_scanner_finds_the_known_mutation_sites() -> None:
    """Guard the guard: a scanner that silently matches nothing proves nothing."""
    found = {s.path.name for path in SCANNED for s in _executing_mutation_sites(path)}

    assert "Makefile" in found
    assert len(found) >= 2, f"scanner is too narrow, only saw {found}"


def test_every_environment_mutation_is_authorized() -> None:
    unauthorized = _unauthorized(SCANNED)

    assert not unauthorized, (
        "These commands mutate the Python environment without consulting "
        "scripts/venv_guard.sh. Route them through the guard, or annotate the "
        "line with `venv-guard: exempt - <reason>` if it cannot mutate:\n  "
        + "\n  ".join(unauthorized)
    )


def test_exemptions_must_carry_a_reason() -> None:
    """An exemption without a justification is just a silent bypass."""
    bare = re.compile(r"venv-guard:\s*exempt\s*$")
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{index}"
        for path in SCANNED
        for index, line in enumerate(path.read_text().splitlines(), start=1)
        if bare.search(line)
    ]

    assert not offenders, f"exemptions need a reason: {offenders}"
