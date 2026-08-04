"""Guard against newly added test-skip constructs in agent diffs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..ports.working_copy import BranchTextFile


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
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
        masker = _LiteralMasker(path)
        remaining_line_numbers = set(added_lines)
        for line_number, source_text in enumerate(
            files_by_path[path].content.splitlines(), start=1
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
    for raw in diff_text.splitlines():
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
        if raw.startswith("+"):
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
        if raw.startswith("-"):
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


@dataclass
class _LiteralFrame:
    delimiter: str
    interpolated: bool
    multiline: bool
    line_start: int
    escapes: bool = True


@dataclass
class _InterpolationFrame:
    depth: int = 1
    paren_depth: int = 0
    bracket_depth: int = 0
    format_spec: bool = False


@dataclass
class _BlockCommentFrame:
    pass


_LexicalFrame = _LiteralFrame | _InterpolationFrame | _BlockCommentFrame
_PYTHON_SUFFIXES = {".py", ".pyi"}
_JAVASCRIPT_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
_KOTLIN_SUFFIXES = {".kt", ".kts"}
_JAVA_SUFFIXES = {".java"}
_JS_REGEX_PREFIX_KEYWORDS = {
    "await",
    "case",
    "default",
    "delete",
    "do",
    "else",
    "extends",
    "in",
    "instanceof",
    "new",
    "of",
    "return",
    "throw",
    "typeof",
    "void",
    "yield",
}
_JS_CONTROL_HEADER_KEYWORDS = {"catch", "for", "if", "switch", "while", "with"}
# Longest first: each is stripped off to expose the token it was applied to.
_JS_POSTFIX_OPERATORS = ("++", "--", "!")
# Punctuation that cannot end a value, so a following slash opens a regex.
_JS_VALUE_BLOCKING_CHARS = "([{=,:;!&|?+-*/%^~<>"


def _js_closes_control_header(prefix: str) -> bool:
    depth = 0
    for index in range(len(prefix) - 1, -1, -1):
        char = prefix[index]
        if char == ")":
            depth += 1
            continue
        if char != "(":
            continue
        depth -= 1
        if depth != 0:
            continue
        header = re.search(r"(?:[^\W\d]|[$_])[\w$]*$", prefix[:index].rstrip())
        return header is not None and header.group() in _JS_CONTROL_HEADER_KEYWORDS
    return False


def _js_closes_statement_block(prefix: str) -> bool:
    depth = 0
    opening_index: int | None = None
    for index in range(len(prefix) - 1, -1, -1):
        char = prefix[index]
        if char == "}":
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0:
                opening_index = index
                break
    if opening_index is None:
        return False

    header = prefix[:opening_index].rstrip()
    if not header:
        return True
    if header.endswith(")") and _js_closes_control_header(header):
        return True

    keyword = re.search(r"(?:[^\W\d]|[$_])[\w$]*$", header)
    if keyword is not None and keyword.group() in {"do", "else", "finally", "try"}:
        before_keyword = header[: keyword.start()].rstrip()
        if not before_keyword.endswith((".", "#")):
            return True

    statement_start = r"(?:^|[;{}])\s*"
    identifier = r"(?:[^\W\d]|[$_])[\w$]*"
    function_header = (
        rf"{statement_start}(?:(?:export\s+(?:default\s+)?)|async\s+)?"
        rf"function\s*\*?\s*(?:{identifier})?.*\)\s*$"
    )
    class_header = (
        rf"{statement_start}(?:export\s+(?:default\s+)?)?"
        rf"class(?:\s+{identifier})?(?:\s+extends\s+.+)?\s*$"
    )
    label_header = rf"{statement_start}{identifier}\s*:\s*$"
    return any(
        re.search(pattern, header, re.DOTALL)
        for pattern in (function_header, class_header, label_header)
    )


class _LiteralMasker:
    """Blank inert text while retaining executable interpolation expressions."""

    def __init__(self, path: str) -> None:
        suffix = Path(path).suffix.lower()
        self._python = suffix in _PYTHON_SUFFIXES
        self._javascript = suffix in _JAVASCRIPT_SUFFIXES
        self._kotlin = suffix in _KOTLIN_SUFFIXES
        self._literals_supported = (
            self._python
            or self._javascript
            or self._kotlin
            or suffix in _JAVA_SUFFIXES
        )
        self._frames: list[_LexicalFrame] = []
        self._js_code_context = ""
        self._js_line_offset = 0
        self._js_last_value_end = 0

    def mask_line(self, text: str) -> str:
        """Mask one line and retain only genuinely multiline lexical state."""

        if not self._literals_supported:
            return text
        self._js_line_offset = len(self._js_code_context)
        for frame in self._frames:
            if isinstance(frame, _LiteralFrame) and not frame.multiline:
                frame.line_start = 0
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

        self._discard_uncontinued_literal(text, masked)
        line = "".join(masked)
        if self._javascript:
            self._js_code_context += line + "\n"
        return line

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
            self._note_value_end(end)
            return end
        if frame.interpolated and self._python and text.startswith(("{{", "}}"), index):
            self._blank(masked, index, index + 2)
            return index + 2
        marker = self._interpolation_marker(frame, text, index)
        if marker is not None:
            self._blank(masked, index, index + len(marker))
            self._frames.append(_InterpolationFrame())
            return index + len(marker)
        if frame.escapes and text[index] == "\\":
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
        if self._python:
            return self._advance_python_interpolation(
                text, masked, index, interpolation
            )
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

    def _advance_python_interpolation(
        self,
        text: str,
        masked: list[str],
        index: int,
        interpolation: _InterpolationFrame,
    ) -> int:
        """Retain Python replacement expressions but blank inert format specs."""

        if interpolation.format_spec:
            return self._advance_python_format_spec(text, masked, index)

        char = text[index]
        if self._advance_python_expression_delimiter(
            char, masked, index, interpolation
        ):
            return index + 1
        if (
            char == ":"
            and interpolation.depth == 1
            and interpolation.paren_depth == 0
            and interpolation.bracket_depth == 0
        ):
            masked[index] = " "
            interpolation.format_spec = True
        return index + 1

    def _advance_python_format_spec(
        self, text: str, masked: list[str], index: int
    ) -> int:
        """Blank format-spec text while entering nested replacement fields."""

        char = text[index]
        masked[index] = " "
        if char == "{":
            self._frames.append(_InterpolationFrame())
        elif char == "}":
            self._frames.pop()
        return index + 1

    def _advance_python_expression_delimiter(
        self,
        char: str,
        masked: list[str],
        index: int,
        interpolation: _InterpolationFrame,
    ) -> bool:
        """Update grouping depth for one Python replacement expression."""

        if char == "(":
            interpolation.paren_depth += 1
        elif char == ")" and interpolation.paren_depth:
            interpolation.paren_depth -= 1
        elif char == "[":
            interpolation.bracket_depth += 1
        elif char == "]" and interpolation.bracket_depth:
            interpolation.bracket_depth -= 1
        elif char == "{":
            interpolation.depth += 1
        elif char == "}":
            interpolation.depth -= 1
            if interpolation.depth == 0:
                masked[index] = " "
                self._frames.pop()
        else:
            return False
        return True

    def _mask_code_token(self, text: str, masked: list[str], index: int) -> int:
        if self._python and text[index] == "#":
            self._blank(masked, index, len(text))
            return len(text)
        if not self._python and text.startswith("//", index):
            self._blank(masked, index, len(text))
            return len(text)
        if not self._python and text.startswith("/*", index):
            return self._start_block_comment(text, masked, index)
        regex_end = self._regex_end(text, masked, index)
        if regex_end is not None:
            self._blank(masked, index, regex_end)
            self._note_value_end(regex_end)
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

    def _note_value_end(self, index: int) -> None:
        """Record where a completed literal ends so a later slash reads as division."""

        if self._javascript:
            self._js_last_value_end = self._js_line_offset + index

    def _regex_end(
        self, text: str, masked: list[str], index: int
    ) -> int | None:
        if not self._javascript or text[index] != "/":
            return None
        prefix = self._js_code_context + "".join(masked[:index])
        if not self._starts_js_regex(prefix):
            return None
        return self._js_regex_end(text, index)

    def _discard_uncontinued_literal(self, text: str, masked: list[str]) -> None:
        if self._line_continues(text):
            return
        for frame_index in range(len(self._frames) - 1, -1, -1):
            frame = self._frames[frame_index]
            if isinstance(frame, _LiteralFrame) and not frame.multiline:
                if any(
                    isinstance(nested, _InterpolationFrame)
                    for nested in self._frames[frame_index + 1 :]
                ):
                    continue
                masked[frame.line_start :] = text[frame.line_start :]
                del self._frames[frame_index:]
                return

    def _literal_at(self, text: str, index: int) -> _LiteralFrame | None:
        quote = text[index]
        if quote not in {"'", '"', "`"}:
            return None
        if quote == "`":
            return _LiteralFrame(
                "`", self._javascript, multiline=True, line_start=index
            )

        delimiter = quote * 3 if text.startswith(quote * 3, index) else quote
        multiline = len(delimiter) == 3
        prefix = self._python_prefix(text, index) if self._python else ""
        interpolated = (
            self._python and any(marker in prefix.lower() for marker in ("f", "t"))
        ) or (
            self._kotlin and quote == '"'
        )
        # Kotlin triple-quoted strings are raw: a backslash before the closing
        # delimiter is literal text, so honouring it as an escape would swallow
        # the delimiter and leave the frame open over executable code. Python's
        # ``r`` prefix and Java text blocks still escape at the token level.
        escapes = not (self._kotlin and multiline)
        return _LiteralFrame(
            delimiter,
            interpolated,
            multiline=multiline,
            line_start=index,
            escapes=escapes,
        )

    @staticmethod
    def _python_prefix(text: str, quote_index: int) -> str:
        start = quote_index
        while (
            start > 0
            and quote_index - start < 3
            and text[start - 1] in "rRuUbBfFtT"
        ):
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

    def _starts_js_regex(self, prefix: str) -> bool:
        """Classify a slash using masked code from every preceding line.

        ``prefix`` spans the whole file so far, because JavaScript never inserts
        a semicolon before a slash: a line-leading ``/`` continues the previous
        expression as division whenever that expression can end a value.
        """

        return not self._ends_js_value(prefix)

    def _ends_js_value(self, prefix: str) -> bool:
        """Report whether the masked code so far ends a complete value.

        Postfix syntax is resolved against the token it follows rather than by
        its own punctuation, because the same character reads as a prefix
        operator elsewhere: ``value!`` is a TypeScript non-null assertion on a
        value, while ``= !`` is logical negation awaiting one.
        """

        prefix = prefix.rstrip()
        while True:
            if self._js_last_value_end > len(prefix):
                return True
            if not prefix:
                return False
            postfix = next(
                (
                    operator
                    for operator in _JS_POSTFIX_OPERATORS
                    if prefix.endswith(operator)
                ),
                None,
            )
            if postfix is None:
                break
            prefix = prefix[: -len(postfix)].rstrip()

        identifier = re.search(r"(?:[^\W\d]|[$_])[\w$]*$", prefix)
        if identifier is not None:
            if identifier.group() not in _JS_REGEX_PREFIX_KEYWORDS:
                return True
            before_identifier = prefix[: identifier.start()].rstrip()
            return before_identifier.endswith((".", "#"))
        if prefix.endswith(")"):
            return not _js_closes_control_header(prefix)
        if prefix.endswith("}"):
            return not _js_closes_statement_block(prefix)
        return prefix[-1] not in _JS_VALUE_BLOCKING_CHARS

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
