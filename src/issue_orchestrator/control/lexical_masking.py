"""Language-aware masking of inert source text for control-layer guards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _LiteralFrame:
    delimiter: str
    interpolated: bool
    multiline: bool
    line_start: int
    escapes: bool = True
    close_absorbs_quote: bool = False

    def closing_length(self, text: str, index: int) -> int | None:
        """Return how many characters close this literal at ``index``.

        Kotlin's ``TRIPLE_QUOTE_CLOSE`` is an optional quote followed by the
        triple quote, so a run of four quotes is one content quote plus the
        delimiter. Reading it as the delimiter followed by a new string literal
        would leave a spurious frame open across the rest of the line.
        """

        if not text.startswith(self.delimiter, index):
            return None
        if self.close_absorbs_quote and text.startswith(
            self.delimiter + self.delimiter[0], index
        ):
            return len(self.delimiter) + 1
        return len(self.delimiter)


@dataclass
class _InterpolationFrame:
    depth: int = 1
    paren_depth: int = 0
    bracket_depth: int = 0
    format_spec: bool = False


@dataclass
class _BlockCommentFrame:
    depth: int = 0


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
_JSX_CLOSING_TAG = r"</[ \t]*(?:(?:[^\W\d]|[$_])[\w$.:-]*[ \t]*)?>"
_JSX_CLOSING_TAG_AT = re.compile(_JSX_CLOSING_TAG)
_JSX_CLOSING_TAG_END = re.compile(_JSX_CLOSING_TAG + r"$")
_JAVA_UNICODE_ESCAPE = re.compile(r"\\u+([0-9a-fA-F]{4})")
# JLS 3.4 LineTerminator: LF, CR, or CRLF. Deliberately narrower than
# ``str.splitlines()``, which also breaks on form feed and other characters
# Java treats as ordinary whitespace inside a line.
_JAVA_LINE_TERMINATOR = re.compile(r"\r\n|\r|\n")


def _translate_java_unicode_escapes(text: str) -> str:
    """Apply the Unicode-escape phase that precedes Java lexing (JLS 3.2-3.3).

    Java translates eligible ``\\uXXXX`` escapes over the whole raw input
    *before* comments and literals are recognised, so ``"\\u0022"`` closes a
    string rather than sitting inertly inside one. A backslash is eligible only
    when an even number of contiguous backslashes precede it, which makes the
    last backslash of an odd-length run the only one that can open an escape:
    ``\\u0022`` is a quote, while ``\\\\u0022`` is an escaped backslash
    followed by literal ``u0022`` text.

    Escapes that cannot be modelled -- a malformed ``\\u`` with fewer than four
    hex digits -- are left as raw text. Java rejects those at compile time, so
    the surrounding source cannot execute a skip either way.
    """

    translated: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            translated.append(text[index])
            index += 1
            continue
        run_end = index
        while run_end < len(text) and text[run_end] == "\\":
            run_end += 1
        escape = (
            _JAVA_UNICODE_ESCAPE.match(text, run_end - 1)
            if (run_end - index) % 2 == 1
            else None
        )
        if escape is None:
            translated.append(text[index:run_end])
            index = run_end
            continue
        translated.append(text[index : run_end - 1])
        translated.append(chr(int(escape.group(1), 16)))
        index = escape.end()
    return "".join(translated)


def _opens_jsx_closing_tag(prefix: str, text: str, index: int) -> bool:
    """Report whether this slash starts a closing tag in an active JSX value."""

    if index == 0 or not prefix.endswith("<"):
        return False
    before_tag = prefix[:-1].rstrip()
    if not before_tag.endswith((">", "}")):
        # Keep compact relational expressions such as ``value</pattern>/``
        # eligible for regex masking instead of assuming every ``</`` is JSX.
        return False
    return _JSX_CLOSING_TAG_AT.match(text, index - 1) is not None


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


class LiteralMasker:
    """Blank inert text while retaining executable interpolation expressions."""

    def __init__(self, path: str) -> None:
        suffix = Path(path).suffix.lower()
        self._python = suffix in _PYTHON_SUFFIXES
        self._javascript = suffix in _JAVASCRIPT_SUFFIXES
        self._kotlin = suffix in _KOTLIN_SUFFIXES
        self._java = suffix in _JAVA_SUFFIXES
        self._literals_supported = (
            self._python or self._javascript or self._kotlin or self._java
        )
        self._frames: list[_LexicalFrame] = []
        self._js_code_context = ""
        self._js_line_offset = 0
        self._js_last_value_end = 0

    def mask_line(self, text: str) -> str:
        """Mask one raw branch-tip line, returning one result per raw line.

        Java's Unicode-escape phase can translate one raw line into several
        logical lines -- ``\\u000A`` ends a comment, exposing executable code
        after it. Those logical lines are lexed in order and rejoined, so the
        caller keeps its raw line numbering while seeing the code Java would
        actually compile.
        """

        if not self._literals_supported:
            return text
        if not self._java:
            return self._mask_logical_line(text)
        translated = _translate_java_unicode_escapes(text)
        return "\n".join(
            self._mask_logical_line(logical)
            for logical in _JAVA_LINE_TERMINATOR.split(translated)
        )

    def _mask_logical_line(self, text: str) -> str:
        """Mask one logical line and retain only genuinely multiline state."""

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
        if self._kotlin:
            return self._mask_kotlin_block_comment(text, masked, index)
        end = text.find("*/", index)
        if end < 0:
            self._blank(masked, index, len(text))
            return len(text)
        self._blank(masked, index, end + 2)
        self._frames.pop()
        return end + 2

    def _mask_kotlin_block_comment(
        self, text: str, masked: list[str], index: int
    ) -> int:
        """Mask Kotlin's nestable block comments without ending at an inner close."""

        frame = self._frames[-1]
        assert isinstance(frame, _BlockCommentFrame)
        cursor = index
        while cursor < len(text):
            opening = text.find("/*", cursor)
            closing = text.find("*/", cursor)
            if opening >= 0 and (closing < 0 or opening < closing):
                frame.depth += 1
                cursor = opening + 2
                continue
            if closing >= 0:
                frame.depth -= 1
                cursor = closing + 2
                if frame.depth == 0:
                    self._blank(masked, index, cursor)
                    self._frames.pop()
                    return cursor
                continue
            self._blank(masked, index, len(text))
            return len(text)
        self._blank(masked, index, len(text))
        return len(text)

    def _mask_literal(
        self, text: str, masked: list[str], index: int, frame: _LiteralFrame
    ) -> int:
        closing = frame.closing_length(text, index)
        if closing is not None:
            end = index + closing
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
        if self._kotlin:
            self._frames.append(_BlockCommentFrame())
            return self._mask_block_comment(text, masked, index)
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
        if _opens_jsx_closing_tag(prefix, text, index):
            return None
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
        # Kotlin triple-quoted strings are raw, and differ from every other
        # modeled literal in two ways: a backslash is literal text rather than
        # an escape, and the closing delimiter absorbs a preceding quote.
        # Python's ``r`` prefix and Java text blocks still escape at the token
        # level, so both traits stay off for them.
        kotlin_raw = self._kotlin and multiline
        return _LiteralFrame(
            delimiter,
            interpolated,
            multiline=multiline,
            line_start=index,
            escapes=not kotlin_raw,
            close_absorbs_quote=kotlin_raw,
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

        if prefix.endswith("/>") or _JSX_CLOSING_TAG_END.search(prefix):
            # Completed JSX elements are values, so a following slash divides.
            # Keep bare relational ``>`` in the blocking set so it can still
            # precede a genuine regex literal.
            return True
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

    def _line_continues(self, text: str) -> bool:
        """Report whether a trailing backslash continues a literal onto the next line.

        Only Python and JavaScript define a backslash-newline continuation
        inside a literal. Java and Kotlin do not, so a trailing backslash there
        must not hold a frame open and mask the executable lines that follow --
        reachable in Java through a translated ``\\u005C``.
        """

        if not (self._python or self._javascript):
            return False
        trailing = len(text) - len(text.rstrip("\\"))
        return trailing % 2 == 1

    @staticmethod
    def _blank(masked: list[str], start: int, end: int) -> None:
        masked[start:end] = " " * (end - start)
