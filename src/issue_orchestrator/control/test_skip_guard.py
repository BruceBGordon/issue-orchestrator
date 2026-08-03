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
class _NewDiffLine:
    """One line from the new side of a unified-diff hunk."""

    path: str
    hunk: int
    line_number: int
    text: str
    added: bool


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
    current_hunk: tuple[str, int] | None = None
    masker: _LiteralMasker | None = None
    for line in _iter_new_diff_lines(diff_text):
        if not _is_test_path(line.path):
            continue
        hunk = (line.path, line.hunk)
        if hunk != current_hunk:
            current_hunk = hunk
            masker = _LiteralMasker(line.path)
        assert masker is not None
        code_text = masker.mask_line(line.text)
        if not line.added or _looks_like_nested_diff_fixture(line.text):
            continue
        for label, pattern in _BANNED_PATTERNS:
            if pattern.search(code_text):
                violations.append(
                    TestSkipGuardViolation(
                        path=line.path,
                        line_number=line.line_number,
                        pattern=label,
                        text=line.text,
                    )
                )
    return TestSkipGuardResult(violations=tuple(violations))


def iter_added_diff_lines(diff_text: str) -> tuple[AddedDiffLine, ...]:
    """Return added lines from a unified diff with new-file line numbers."""

    return tuple(
        AddedDiffLine(path=line.path, line_number=line.line_number, text=line.text)
        for line in _iter_new_diff_lines(diff_text)
        if line.added
    )


def _iter_new_diff_lines(diff_text: str) -> tuple[_NewDiffLine, ...]:
    """Return context and additions, preserving new-side lexical order."""

    lines: list[_NewDiffLine] = []
    current_path: str | None = None
    new_line: int | None = None
    hunk = 0
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
            new_line = _parse_hunk_start(raw)
            hunk += 1
            continue
        if new_line is None:
            continue
        if raw.startswith("+"):
            _append_new_diff_line(
                lines, current_path, hunk, new_line, raw[1:], added=True
            )
            new_line += 1
            continue
        if raw.startswith("-"):
            continue
        if raw.startswith("\\ No newline at end of file"):
            continue
        _append_new_diff_line(
            lines, current_path, hunk, new_line, raw.removeprefix(" "), added=False
        )
        new_line += 1
    return tuple(lines)


def _append_new_diff_line(
    lines: list[_NewDiffLine],
    path: str | None,
    hunk: int,
    line_number: int,
    text: str,
    *,
    added: bool,
) -> None:
    if path is None:
        return
    lines.append(
        _NewDiffLine(
            path=path,
            hunk=hunk,
            line_number=line_number,
            text=text,
            added=added,
        )
    )


def _parse_hunk_start(line: str) -> int | None:
    match = _HUNK_RE.search(line)
    return int(match.group(1)) if match else None


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
    return stripped.startswith(("+", "-")) or bool(
        _QUOTED_NESTED_DIFF_RE.search(stripped)
    )


@dataclass
class _LiteralFrame:
    delimiter: str
    interpolated: bool
    multiline: bool


@dataclass
class _InterpolationFrame:
    depth: int = 1


@dataclass
class _BlockCommentFrame:
    pass


_LexicalFrame = _LiteralFrame | _InterpolationFrame | _BlockCommentFrame
_PYTHON_SUFFIXES = {".py", ".pyi"}
_JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
_KOTLIN_SUFFIXES = {".kt", ".kts"}


class _LiteralMasker:
    """Blank inert text while retaining executable interpolation expressions."""

    def __init__(self, path: str) -> None:
        suffix = Path(path).suffix.lower()
        self._python = suffix in _PYTHON_SUFFIXES
        self._javascript = suffix in _JAVASCRIPT_SUFFIXES
        self._kotlin = suffix in _KOTLIN_SUFFIXES
        self._frames: list[_LexicalFrame] = []

    def mask_line(self, text: str) -> str:
        """Mask one line and retain only genuinely multiline lexical state."""

        masked = list(text)
        index = 0
        while index < len(text):
            frame = self._frames[-1] if self._frames else None
            if isinstance(frame, _BlockCommentFrame):
                index = self._mask_block_comment(text, masked, index)
                continue
            if isinstance(frame, _LiteralFrame):
                index = self._mask_literal(text, masked, index, frame)
                continue

            interpolation = frame if isinstance(frame, _InterpolationFrame) else None
            brace_end = self._advance_interpolation(text, masked, index, interpolation)
            if brace_end is not None:
                index = brace_end
                continue
            index = self._mask_code_token(text, masked, index)

        self._discard_uncontinued_literal(text)
        return "".join(masked)

    def _mask_block_comment(self, text: str, masked: list[str], index: int) -> int:
        end = text.find("*/", index)
        if end < 0:
            self._blank(masked, index, len(text))
            return len(text)
        self._blank(masked, index, end + 2)
        self._frames.pop()
        return end + 2

    def _mask_literal(
        self, text: str, masked: list[str], index: int, frame: _LiteralFrame
    ) -> int:
        if text.startswith(frame.delimiter, index):
            end = index + len(frame.delimiter)
            self._blank(masked, index, end)
            self._frames.pop()
            return end
        if frame.interpolated and self._python and text.startswith(("{{", "}}"), index):
            self._blank(masked, index, index + 2)
            return index + 2
        marker = self._interpolation_marker(frame, text, index)
        if marker is not None:
            self._blank(masked, index, index + len(marker))
            self._frames.append(_InterpolationFrame())
            return index + len(marker)
        if text[index] == "\\":
            end = min(index + 2, len(text))
            self._blank(masked, index, end)
            return end
        masked[index] = " "
        return index + 1

    def _advance_interpolation(
        self,
        text: str,
        masked: list[str],
        index: int,
        interpolation: _InterpolationFrame | None,
    ) -> int | None:
        if interpolation is None:
            return None
        if text[index] == "{":
            interpolation.depth += 1
            return index + 1
        if text[index] != "}":
            return None
        interpolation.depth -= 1
        if interpolation.depth == 0:
            masked[index] = " "
            self._frames.pop()
        return index + 1

    def _mask_code_token(self, text: str, masked: list[str], index: int) -> int:
        if self._python and text[index] == "#":
            self._blank(masked, index, len(text))
            return len(text)
        if not self._python and text.startswith("//", index):
            self._blank(masked, index, len(text))
            return len(text)
        if not self._python and text.startswith("/*", index):
            return self._start_block_comment(text, masked, index)
        regex_end = self._regex_end(text, index)
        if regex_end is not None:
            self._blank(masked, index, regex_end)
            return regex_end
        literal = self._literal_at(text, index)
        if literal is None:
            return index + 1
        self._blank(masked, index, index + len(literal.delimiter))
        self._frames.append(literal)
        return index + len(literal.delimiter)

    def _start_block_comment(self, text: str, masked: list[str], index: int) -> int:
        end = text.find("*/", index + 2)
        if end < 0:
            self._blank(masked, index, len(text))
            self._frames.append(_BlockCommentFrame())
            return len(text)
        self._blank(masked, index, end + 2)
        return end + 2

    def _regex_end(self, text: str, index: int) -> int | None:
        if not self._javascript or text[index] != "/":
            return None
        if not self._starts_js_regex(text, index):
            return None
        return self._js_regex_end(text, index)

    def _discard_uncontinued_literal(self, text: str) -> None:
        if self._line_continues(text):
            return
        for frame_index, frame in enumerate(self._frames):
            if isinstance(frame, _LiteralFrame) and not frame.multiline:
                del self._frames[frame_index:]
                return

    def _literal_at(self, text: str, index: int) -> _LiteralFrame | None:
        quote = text[index]
        if quote not in {"'", '"', "`"}:
            return None
        if quote == "`":
            return _LiteralFrame("`", self._javascript, multiline=True)

        delimiter = quote * 3 if text.startswith(quote * 3, index) else quote
        multiline = len(delimiter) == 3
        prefix = self._python_prefix(text, index) if self._python else ""
        interpolated = (self._python and "f" in prefix.lower()) or (
            self._kotlin and quote == '"'
        )
        return _LiteralFrame(
            delimiter,
            interpolated,
            multiline=multiline,
        )

    @staticmethod
    def _python_prefix(text: str, quote_index: int) -> str:
        start = quote_index
        while start > 0 and quote_index - start < 3 and text[start - 1] in "rRuUbBfF":
            start -= 1
        if start > 0 and (text[start - 1].isalnum() or text[start - 1] == "_"):
            return ""
        return text[start:quote_index]

    def _interpolation_marker(
        self, frame: _LiteralFrame, text: str, index: int
    ) -> str | None:
        if not frame.interpolated:
            return None
        if self._python:
            return "{" if text[index] == "{" else None
        return "${" if text.startswith("${", index) else None

    @staticmethod
    def _starts_js_regex(text: str, index: int) -> bool:
        prefix = text[:index].rstrip()
        if not prefix:
            return True
        if prefix.endswith(("return", "case", "throw", "yield", "=>")):
            return True
        return prefix[-1] in "([{=,:;!&|?+-*%^~<>"

    @staticmethod
    def _js_regex_end(text: str, start: int) -> int | None:
        index = start + 1
        in_class = False
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            elif char == "/" and not in_class:
                index += 1
                while index < len(text) and text[index].isalpha():
                    index += 1
                return index
            index += 1
        return None

    @staticmethod
    def _line_continues(text: str) -> bool:
        trailing = len(text) - len(text.rstrip("\\"))
        return trailing % 2 == 1

    @staticmethod
    def _blank(masked: list[str], start: int, end: int) -> None:
        masked[start:end] = " " * (end - start)
