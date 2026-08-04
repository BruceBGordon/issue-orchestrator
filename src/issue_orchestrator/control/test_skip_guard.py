"""Guard against newly added test-skip constructs in agent diffs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..ports.working_copy import BranchTextFile
from .lexical_masking import LiteralMasker


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_TEST_PATH_SEGMENTS = {"test", "tests", "spec", "specs", "__tests__"}
_TEST_NAME_SEGMENTS = {"test", "tests", "spec", "specs"}


def _physical_lines(text: str) -> list[str]:
    """Split patch or source text the way Git delimits lines.

    Git delimits patch records and file lines with LF alone, and shows a CRLF
    file's carriage return as the last character of the record. Trailing
    behaviour matches ``str.splitlines()``, but the boundary set is
    deliberately narrower: ``str.splitlines()`` also breaks on form feed,
    vertical tab, NEL and the Unicode separators, none of which Git treats as a
    line boundary and all of which are ordinary in-line characters to the
    modelled languages -- form feed is plain whitespace in Java (JLS 3.6).
    Breaking there detaches source from its leading ``+`` and drops the
    addition from the scan entirely.

    The diff side and the branch-tip side must share this one rule. If they
    disagree, their line numbering diverges and additions stop lining up with
    the source they were taken from. Line terminators that a *language*
    recognises inside such a line are the masker's concern, not this function's.
    """

    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines and not lines[-1]:
        lines.pop()
    return lines


@dataclass(frozen=True)
class AddedDiffLine:
    """One added line from a unified diff."""

    path: str
    line_number: int
    text: str


@dataclass(frozen=True)
class TestSkipGuardViolation:
    """A newly added test-skip construct found in the branch diff."""

    path: str
    line_number: int
    pattern: str
    text: str

    def format(self) -> str:
        return f"{self.path}:{self.line_number}: {self.pattern}: {self.text.strip()}"


@dataclass(frozen=True)
class TestSkipGuardResult:
    """Result of scanning a diff for forbidden test-skip additions."""

    violations: tuple[TestSkipGuardViolation, ...]

    @property
    def ok(self) -> bool:
        return not self.violations

    def reason(self) -> str:
        if self.ok:
            return ""
        formatted = "; ".join(v.format() for v in self.violations[:5])
        if len(self.violations) > 5:
            formatted += f"; and {len(self.violations) - 5} more"
        return (
            "Newly added test-skip guard(s) detected. Do not skip, disable, "
            f"quarantine, or weaken failing tests: {formatted}"
        )


_BANNED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("JUnit assumeTrue", re.compile(r"\bassumeTrue\s*\(")),
    ("JUnit assumeFalse", re.compile(r"\bassumeFalse\s*\(")),
    ("JUnit @Disabled", re.compile(r"@\s*Disabled\b")),
    ("JUnit @Ignore", re.compile(r"@\s*Ignore(?:Class)?\b")),
    ("pytest skip marker", re.compile(r"\bpytest\.mark\.skip(?:if)?\b")),
    ("pytest.skip", re.compile(r"\bpytest\.skip\s*\(")),
    ("unittest skip", re.compile(r"\bunittest\.skip(?:If|Unless)?\b")),
    ("JS test skip", re.compile(r"\b(?:describe|it|test)\.skip\s*\(")),
)


def added_test_paths(diff_text: str) -> tuple[str, ...]:
    """Return test paths with added source lines that require lexical scanning."""

    return tuple(dict.fromkeys(added.path for added in _test_additions(diff_text)))


def scan_added_test_skip_guards(
    diff_text: str, branch_files: tuple[BranchTextFile, ...]
) -> TestSkipGuardResult:
    """Scan added test lines using complete branch-tip source for lexical state."""

    additions = _test_additions_by_path(diff_text)
    files_by_path = {branch_file.path: branch_file for branch_file in branch_files}
    if len(files_by_path) != len(branch_files):
        raise ValueError("Branch-tip test file content contains duplicate paths")

    missing_paths = additions.keys() - files_by_path.keys()
    if missing_paths:
        missing = ", ".join(sorted(missing_paths))
        raise ValueError(f"Missing branch-tip test file content for: {missing}")

    violations: list[TestSkipGuardViolation] = []
    for path, added_lines in additions.items():
        masker = LiteralMasker(path)
        remaining_line_numbers = set(added_lines)
        for line_number, source_text in enumerate(
            _physical_lines(files_by_path[path].content), start=1
        ):
            code_text = masker.mask_line(source_text)
            added = added_lines.get(line_number)
            if added is None:
                continue
            if source_text != added.text:
                raise ValueError(
                    f"Branch-tip content does not match diff at {path}:{line_number}"
                )
            remaining_line_numbers.remove(line_number)
            for label, pattern in _BANNED_PATTERNS:
                if pattern.search(code_text):
                    violations.append(
                        TestSkipGuardViolation(
                            path=path,
                            line_number=line_number,
                            pattern=label,
                            text=source_text,
                        )
                    )
        if remaining_line_numbers:
            missing = ", ".join(str(number) for number in sorted(remaining_line_numbers))
            raise ValueError(f"Branch-tip content is missing {path} line(s): {missing}")
    return TestSkipGuardResult(violations=tuple(violations))


def _test_additions_by_path(
    diff_text: str,
) -> dict[str, dict[int, AddedDiffLine]]:
    additions: dict[str, dict[int, AddedDiffLine]] = {}
    for added in _test_additions(diff_text):
        path_additions = additions.setdefault(added.path, {})
        if added.line_number in path_additions:
            raise ValueError(
                f"Diff contains duplicate added line {added.path}:{added.line_number}"
            )
        path_additions[added.line_number] = added
    return additions


def _test_additions(diff_text: str) -> tuple[AddedDiffLine, ...]:
    """Return every added test-path line, leaving fixture detection to lexing.

    A leading ``+``/``-`` is not proof that a line is nested-diff fixture data;
    both are valid unary operators, so ``+test.skip(...)`` executes. Only the
    branch-tip lexical scan can tell fixture text from executable source.
    """

    return tuple(
        added
        for added in iter_added_diff_lines(diff_text)
        if _is_test_path(added.path)
    )


def iter_added_diff_lines(diff_text: str) -> tuple[AddedDiffLine, ...]:
    """Return added lines from a unified diff with new-file line numbers.

    Header recognition is state-aware. ``+++``/``---`` name files only outside
    an active hunk; inside one, every leading ``+`` is an addition. Source such
    as ``++pytest.skip(...)`` — two unary-plus operators — is therefore scanned
    instead of being mistaken for the ``+++`` file header it resembles.

    Raises:
        ValueError: If a hunk header cannot be parsed. Silently dropping the
            hunk would hide its additions from the scan.
    """

    added: list[AddedDiffLine] = []
    current_path: str | None = None
    new_line: int | None = None
    for raw in _physical_lines(diff_text):
        if raw.startswith("diff --git "):
            current_path = None
            new_line = None
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.search(raw)
            if match is None:
                raise ValueError(f"Unparsable diff hunk header: {raw}")
            new_line = int(match.group(1))
            continue
        if new_line is None:
            if raw.startswith("+++ "):
                current_path = _parse_new_path(raw)
            continue
        new_line, addition = _advance_new_side_hunk_line(
            raw, current_path=current_path, new_line=new_line
        )
        if addition is not None:
            added.append(addition)
    return tuple(added)


def _advance_new_side_hunk_line(
    raw: str, *, current_path: str | None, new_line: int
) -> tuple[int, AddedDiffLine | None]:
    """Consume one hunk line and advance its branch-tip line position."""

    if raw.startswith("+"):
        addition = (
            AddedDiffLine(path=current_path, line_number=new_line, text=raw[1:])
            if current_path is not None
            else None
        )
        return new_line + 1, addition
    if raw.startswith("-") or raw.startswith("\\ No newline at end of file"):
        return new_line, None
    return new_line + 1, None


def _parse_new_path(line: str) -> str | None:
    path = line[4:].strip()
    if path == "/dev/null":
        return None
    if path.startswith("b/"):
        return path[2:]
    return path


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    file_path = Path(normalized)
    parts = {part.lower() for part in file_path.parts}
    if parts & _TEST_PATH_SEGMENTS:
        return True
    return _is_test_file_name(file_path.name)


def _is_test_file_name(name: str) -> bool:
    lower_name = name.lower()
    name_segments = lower_name.split(".")
    if any(segment in _TEST_NAME_SEGMENTS for segment in name_segments):
        return True

    stem = Path(name).stem
    lower_stem = stem.lower()
    if lower_stem in _TEST_NAME_SEGMENTS:
        return True
    if lower_stem.startswith(("test_", "test-")):
        return True
    if lower_stem.endswith(
        ("_test", "-test", "_tests", "-tests", "_spec", "-spec", "_specs", "-specs")
    ):
        return True
    return stem.endswith(("Test", "Tests", "Spec", "Specs"))
