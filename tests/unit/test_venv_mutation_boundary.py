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
)

# An executed command that installs this project or rewrites the environment.
MUTATION = re.compile(
    r"""(
        uv \s+ sync
      | \$\(UV\) \s+ sync
      | \{uv\} \s+ sync
      | uv \s+ pip \s+ install
      | pip \s+ install \s+ -e
      | -m \s+ pip \s+ install
      | "sync" \s* ,
    )""",
    re.VERBOSE,
)

# Occurrences that only *mention* a command: comments, help text, and messages
# printed for the user to copy. These cannot mutate anything.
NON_EXECUTING = re.compile(
    r"""
      (^\s*\#)          # comment
    | (^\s*[\"\'])       # a bare string literal: a message, not a command
    | (\becho\b)
    | (\bprintf\b)
    | (\bprint\()
    | (\bhelp\s*=)
    | (doc_examples)
    """,
    re.VERBOSE,
)

EXEMPTION = re.compile(r"venv-guard:\s*exempt\s*[-—:]\s*(?P<reason>.+)")

SCANNED = [
    REPO_ROOT / "Makefile",
    *sorted((REPO_ROOT / "scripts").glob("*.sh")),
    *sorted((REPO_ROOT / "scripts").glob("*.py")),
]


@dataclass(frozen=True)
class Site:
    path: Path
    line_no: int
    line: str

    def __str__(self) -> str:
        rel = self.path.relative_to(REPO_ROOT)
        return f"{rel}:{self.line_no}: {self.line.strip()[:100]}"


def _executing_mutation_sites(path: Path) -> list[Site]:
    sites: list[Site] = []
    for index, line in enumerate(path.read_text().splitlines(), start=1):
        if not MUTATION.search(line) or NON_EXECUTING.search(line):
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
            if any(ref in _enclosing_block(site) for ref in GUARD_REFERENCES):
                continue
            findings.append(str(site))
    return findings


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
