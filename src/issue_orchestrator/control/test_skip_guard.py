"""Guard against newly added test-skip constructs in agent diffs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_QUOTED_NESTED_DIFF_RE = re.compile(r"(?i)^(?:[rubf]{0,3})?[\"']{1,3}[+-]")
_TEST_PATH_SEGMENTS = {"test", "tests", "spec", "specs", "__tests__"}
_TEST_NAME_SEGMENTS = {"test", "tests", "spec", "specs"}


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


def scan_added_test_skip_guards(diff_text: str) -> TestSkipGuardResult:
    """Scan branch diff text for newly added test-skip constructs."""

    violations: list[TestSkipGuardViolation] = []
    for added in iter_added_diff_lines(diff_text):
        if not _is_test_path(added.path):
            continue
        if _looks_like_nested_diff_fixture(added.text):
            continue
        code_text = _mask_literals(added.text)
        for label, pattern in _BANNED_PATTERNS:
            if pattern.search(code_text):
                violations.append(
                    TestSkipGuardViolation(
                        path=added.path,
                        line_number=added.line_number,
                        pattern=label,
                        text=added.text,
                    )
                )
    return TestSkipGuardResult(violations=tuple(violations))


def iter_added_diff_lines(diff_text: str) -> tuple[AddedDiffLine, ...]:
    """Return added lines from a unified diff with new-file line numbers."""

    added: list[AddedDiffLine] = []
    current_path: str | None = None
    new_line: int | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            current_path = None
            new_line = None
            continue
        if raw.startswith("+++ "):
            current_path = _parse_new_path(raw)
            new_line = None
            continue
        if raw.startswith("@@"):
            match = _HUNK_RE.search(raw)
            new_line = int(match.group(1)) if match else None
            continue
        if new_line is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            if current_path is not None:
                added.append(
                    AddedDiffLine(
                        path=current_path,
                        line_number=new_line,
                        text=raw[1:],
                    )
                )
            new_line += 1
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue
        new_line += 1
    return tuple(added)


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


def _looks_like_nested_diff_fixture(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("+", "-")) or bool(_QUOTED_NESTED_DIFF_RE.search(stripped))


def _mask_literals(text: str) -> str:
    """Preserve executable text while blanking quoted string literals."""

    masked = list(text)
    index = 0
    while index < len(text):
        char = text[index]
        if char in {"'", '"', "`"}:
            end = _literal_end(text, index, char)
            masked[index:end] = " " * (end - index)
            index = end
            continue
        index += 1
    return "".join(masked)


def _literal_end(text: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)
