"""Tests for branch-diff test skip guard."""

import pytest

from issue_orchestrator.control.test_skip_guard import (
    TestSkipGuardResult as SkipGuardResult,
    added_test_paths,
    iter_added_diff_lines,
    scan_added_test_skip_guards,
)
from issue_orchestrator.ports.working_copy import BranchTextFile


def _branch_files_from_added_lines(diff: str) -> tuple[BranchTextFile, ...]:
    lines_by_path: dict[str, list[str]] = {}
    for added in iter_added_diff_lines(diff):
        lines = lines_by_path.setdefault(added.path, [])
        lines.extend("" for _ in range(added.line_number - len(lines)))
        lines[added.line_number - 1] = added.text
    return tuple(
        BranchTextFile(path=path, content="\n".join(lines))
        for path, lines in lines_by_path.items()
    )


def _scan(
    diff: str, branch_files: tuple[BranchTextFile, ...] | None = None
) -> SkipGuardResult:
    return scan_added_test_skip_guards(
        diff,
        branch_files
        if branch_files is not None
        else _branch_files_from_added_lines(diff),
    )


def test_iter_added_diff_lines_tracks_new_file_line_numbers() -> None:
    diff = """diff --git a/src/test/FooTest.kt b/src/test/FooTest.kt
--- a/src/test/FooTest.kt
+++ b/src/test/FooTest.kt
@@ -10,0 +11,2 @@
+import org.junit.jupiter.api.Assumptions.assumeTrue
+class FooTest
"""

    lines = iter_added_diff_lines(diff)

    assert [(line.path, line.line_number, line.text) for line in lines] == [
        (
            "src/test/FooTest.kt",
            11,
            "import org.junit.jupiter.api.Assumptions.assumeTrue",
        ),
        ("src/test/FooTest.kt", 12, "class FooTest"),
    ]


def test_scan_added_test_skip_guards_flags_junit_assumption_in_test_path() -> None:
    diff = """diff --git a/inventory-impl/src/test/kotlin/RepoTest.kt b/inventory-impl/src/test/kotlin/RepoTest.kt
--- a/inventory-impl/src/test/kotlin/RepoTest.kt
+++ b/inventory-impl/src/test/kotlin/RepoTest.kt
@@ -25,0 +26,1 @@
+        assumeTrue(PostgresTestSupport.isAvailable(), PostgresTestSupport.skipReason())
"""

    result = _scan(diff)

    assert not result.ok
    assert len(result.violations) == 1
    assert result.violations[0].path == "inventory-impl/src/test/kotlin/RepoTest.kt"
    assert result.violations[0].line_number == 26
    assert result.violations[0].pattern == "JUnit assumeTrue"
    assert "Newly added test-skip guard" in result.reason()


def test_scan_added_test_skip_guards_flags_test_file_name_without_test_directory() -> (
    None
):
    diff = """diff --git a/inventory-impl/src/main/kotlin/RepoTest.kt b/inventory-impl/src/main/kotlin/RepoTest.kt
--- a/inventory-impl/src/main/kotlin/RepoTest.kt
+++ b/inventory-impl/src/main/kotlin/RepoTest.kt
@@ -25,0 +26,1 @@
+        assumeTrue(PostgresTestSupport.isAvailable())
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].path == "inventory-impl/src/main/kotlin/RepoTest.kt"


def test_scan_added_test_skip_guards_does_not_match_test_substrings_in_regular_files() -> (
    None
):
    diff = """diff --git a/src/latest.py b/src/latest.py
--- a/src/latest.py
+++ b/src/latest.py
@@ -1,0 +2,1 @@
+pytest.skip("not in a test file")
diff --git a/src/protest.kt b/src/protest.kt
--- a/src/protest.kt
+++ b/src/protest.kt
@@ -1,0 +2,1 @@
+        assumeTrue(PostgresTestSupport.isAvailable())
diff --git a/pytest.ini b/pytest.ini
--- a/pytest.ini
+++ b/pytest.ini
@@ -1,0 +2,1 @@
+note = "pytest.skip appears in docs"
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_ignores_documentation_mentions() -> None:
    diff = """diff --git a/docs/testing.md b/docs/testing.md
--- a/docs/testing.md
+++ b/docs/testing.md
@@ -1,0 +2,1 @@
+Document why assumeTrue is not allowed in tests.
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_ignores_nested_diff_fixture_lines() -> None:
    diff = '''diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -0,0 +1,4 @@
+fixture_diff = """@@ -25,0 +26,1 @@
++        assumeTrue(PostgresTestSupport.isAvailable())
+"""
+run_guard(fixture_diff)
'''

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_flags_unary_prefixed_skip_calls() -> None:
    diff = """diff --git a/tests/unit/guard.test.ts b/tests/unit/guard.test.ts
--- a/tests/unit/guard.test.ts
+++ b/tests/unit/guard.test.ts
@@ -0,0 +1,1 @@
++test.skip("real skip", () => {});
diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -0,0 +1,1 @@
+-pytest.skip("real skip")
"""

    result = _scan(diff)

    assert added_test_paths(diff) == (
        "tests/unit/guard.test.ts",
        "tests/unit/test_guard.py",
    )
    assert [
        (violation.path, violation.line_number, violation.pattern)
        for violation in result.violations
    ] == [
        ("tests/unit/guard.test.ts", 1, "JS test skip"),
        ("tests/unit/test_guard.py", 1, "pytest.skip"),
    ]


def test_scan_added_test_skip_guards_flags_source_resembling_diff_headers() -> None:
    diff = """diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -0,0 +1,2 @@
+++pytest.skip("real skip")
+--pytest.mark.skip
"""

    result = _scan(diff)

    assert added_test_paths(diff) == ("tests/unit/test_guard.py",)
    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(1, "pytest.skip"), (2, "pytest skip marker")]


def test_iter_added_diff_lines_still_reads_file_headers_between_hunks() -> None:
    diff = """diff --git a/tests/first.test.ts b/tests/first.test.ts
--- a/tests/first.test.ts
+++ b/tests/first.test.ts
@@ -0,0 +1,1 @@
+++first;
diff --git a/tests/second.test.ts b/tests/second.test.ts
--- a/tests/second.test.ts
+++ b/tests/second.test.ts
@@ -0,0 +1,1 @@
+++second;
"""

    lines = iter_added_diff_lines(diff)

    assert [(line.path, line.line_number, line.text) for line in lines] == [
        ("tests/first.test.ts", 1, "++first;"),
        ("tests/second.test.ts", 1, "++second;"),
    ]


def test_iter_added_diff_lines_rejects_unparsable_hunk_header() -> None:
    diff = """diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@@ -1,1 -1,1 +1,1 @@@
++pytest.skip("real skip")
"""

    with pytest.raises(ValueError, match="Unparsable diff hunk header"):
        iter_added_diff_lines(diff)


def test_scan_added_test_skip_guards_ignores_quoted_nested_diff_fixture_lines() -> None:
    diff = """diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -1,0 +2,1 @@
+                "+        assumeTrue(PostgresTestSupport.isAvailable())\\n"
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_ignores_skip_constructs_in_literals() -> None:
    diff = """diff --git a/audit/checks.test.ts b/audit/checks.test.ts
--- a/audit/checks.test.ts
+++ b/audit/checks.test.ts
@@ -1409,0 +1410,4 @@
+    const fixture = "describe.skip('forbidden', () => {})";
+    expect(report).toContain('describe.skip()');
+    const junitExample = "@Disabled";
+    const pythonExample = 'pytest.skip("not executable")';
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_still_flags_code_after_a_literal() -> None:
    diff = """diff --git a/audit/checks.test.ts b/audit/checks.test.ts
--- a/audit/checks.test.ts
+++ b/audit/checks.test.ts
@@ -1,0 +2,1 @@
+    log("checking suite"); describe.skip('suite', () => {});
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_js_template_interpolation() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -1,0 +2,1 @@
+const message = `result: ${test.skip("real skip", () => {})}`;
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_python_f_string_interpolation() -> None:
    diff = """diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,1 @@
+message = f"result: {pytest.skip('real skip')}"
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "pytest.skip"


def test_scan_added_test_skip_guards_ignores_escaped_python_f_string_braces() -> None:
    diff = """diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,1 @@
+message = f"example: {{pytest.skip('not executable')}}"
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_does_not_treat_js_regex_apostrophe_as_quote() -> (
    None
):
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -1,0 +2,1 @@
+const contraction = /don't/; test.skip("real skip", () => {});
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_does_not_treat_division_as_js_regex() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -1,0 +2,1 @@
+const ratio = obj.return / test.skip("real skip", () => {}) / 2;
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_handles_regex_after_js_statement_blocks() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,4 @@
+{} /don't/.test(value); test.skip("plain block", () => {});
+if (ready) {} /don't/.test(value); test.skip("control block", () => {});
+function helper() {} /don't/.test(value); test.skip("function", () => {});
+class Helper {} /don't/.test(value); test.skip("class", () => {});
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [1, 2, 3, 4]


def test_scan_added_test_skip_guards_tracks_js_statement_blocks_across_lines() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,2 @@
+function helper() {
+} /don't/.test(value); test.skip("after multiline block", () => {});
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2]


def test_scan_added_test_skip_guards_flags_division_continued_onto_next_line() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,2 @@
+const ratio = value
+/ test.skip("real skip", () => {}) / 2;
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2]
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_division_after_string_and_template() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,3 @@
+const first = "seed" / test.skip("string divisor", () => {}) / 2;
+const second = `seed`
+/ test.skip("template divisor", () => {}) / 2;
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [1, 3]


def test_scan_added_test_skip_guards_flags_division_after_postfix_operators() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,4 @@
+let count = 0;
+count++ / test.skip("real skip", () => {}) / 2;
+let total = 10;
+total-- / test.skip("decrement skip", () => {}) / 2;
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2, 4]
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_ignores_regex_after_prefix_operators() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,3 @@
+const flagged = +/test.skip("fixture", () => {})/.test(source);
+const negated = !/describe.skip("fixture")/.test(source);
+const doubled = - -/it.skip("fixture")/.test(source);
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_flags_division_after_non_null_assertion() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,3 @@
+const value: number | undefined = 2;
+value! / test.skip("real skip", () => {}) / 2;
+const negated = !/test.skip("fixture", () => {})/.test(source);
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2]
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_division_after_self_closing_jsx() -> None:
    diff = """diff --git a/tests/guard.test.tsx b/tests/guard.test.tsx
--- a/tests/guard.test.tsx
+++ b/tests/guard.test.tsx
@@ -0,0 +1,3 @@
+const ratio = <Widget /> / (test.skip("real skip", () => {}), 1) / 2;
+const attrs = <Widget a={x} b="y" {...rest} /> / (it.skip("attrs"), 1) / 2;
+const inert = left > /test.skip("fixture", () => {})/.test(source);
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [1, 2]
    assert {violation.pattern for violation in result.violations} == {"JS test skip"}


def test_scan_added_test_skip_guards_ignores_fixtures_after_paired_jsx() -> None:
    diff = """diff --git a/tests/guard.test.tsx b/tests/guard.test.tsx
--- a/tests/guard.test.tsx
+++ b/tests/guard.test.tsx
@@ -0,0 +1,5 @@
+const view = <Widget></Widget>; expect(/it.skip("fixture")/.test(source));
+const named = <A.B></A.B>; expect(/describe.skip("fixture")/.test(source));
+const dashed = <my-el></my-el>; expect(/test.skip("fixture")/.test(source));
+const frag = <></>; expect(/test.skip("fixture")/.test(source));
+const quoted = <Widget></Widget>; const fixture = "a/b test.skip(inert)";
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_flags_executable_skips_around_paired_jsx() -> None:
    diff = """diff --git a/tests/guard.test.tsx b/tests/guard.test.tsx
--- a/tests/guard.test.tsx
+++ b/tests/guard.test.tsx
@@ -0,0 +1,3 @@
+const view = <Widget></Widget>; test.skip("real skip", () => {});
+const ratio = <Widget></Widget> / (it.skip("real skip"), 1) / 2;
+const inert = left > /test.skip("fixture", () => {})/.test(source);
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [1, 2]
    assert {violation.pattern for violation in result.violations} == {"JS test skip"}


def test_scan_added_test_skip_guards_keeps_compact_relational_regex() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,2 @@
+const compact = value</pytest.mark.skip>/.test(source);
+const escaped = value</\\d+ test.skip("fixture")>/.test(source);
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_closes_kotlin_four_quote_raw_string() -> None:
    diff = '''diff --git a/src/test/kotlin/GuardTest.kt b/src/test/kotlin/GuardTest.kt
--- a/src/test/kotlin/GuardTest.kt
+++ b/src/test/kotlin/GuardTest.kt
@@ -0,0 +1,1 @@
+val fixture = """assumeTrue(false) stays inert""""; assumeFalse(true, "reason")
'''

    result = _scan(diff)

    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(1, "JUnit assumeFalse")]


def test_scan_added_test_skip_guards_closes_kotlin_raw_string_ending_in_backslash() -> (
    None
):
    diff = '''diff --git a/src/test/kotlin/GuardTest.kt b/src/test/kotlin/GuardTest.kt
--- a/src/test/kotlin/GuardTest.kt
+++ b/src/test/kotlin/GuardTest.kt
@@ -0,0 +1,3 @@
+val fixture = """assumeTrue(false) at C:\\fixtures\\"""
+val inert = fixture
+assumeTrue(false)
'''

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [3]
    assert result.violations[0].pattern == "JUnit assumeTrue"


def test_scan_added_test_skip_guards_flags_java_unicode_escaped_quote() -> None:
    diff = '''diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,3 @@
+        String fixture = "\\u0022; assumeTrue(false); //";
+        assume\\u0054rue(false);
+        String escaped = "\\u005c\\\\u0022; assumeFalse(true); //";
'''

    result = _scan(diff)

    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [
        (1, "JUnit assumeTrue"),
        (2, "JUnit assumeTrue"),
        (3, "JUnit assumeFalse"),
    ]


def test_scan_added_test_skip_guards_ignores_inert_java_fixture_strings() -> None:
    diff = '''diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,4 @@
+        String plain = "assumeTrue(false) stays inert";
+        String escaped = "\\\\u0022 assumeTrue(false) stays inert";
+        String unicode = "\\u0061ssumeTrue(false) stays inert";
+        \\u002f\\u002f assumeFalse(true);
'''

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_flags_java_unicode_escaped_line_terminator() -> (
    None
):
    diff = '''diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,2 @@
+        // fixture \\u000A assumeTrue(false);
+        int after = 1;
'''

    result = _scan(diff)

    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(1, "JUnit assumeTrue")]


def test_scan_added_test_skip_guards_preserves_form_feed_in_java_added_line() -> None:
    source_line = "\fassumeTrue(false);"
    diff = f"""diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,1 @@
+{source_line}
"""
    branch_files = (
        BranchTextFile(path="src/test/java/GuardTest.java", content=source_line),
    )

    result = _scan(diff, branch_files)

    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(1, "JUnit assumeTrue")]


def test_scan_added_test_skip_guards_ignores_form_feed_before_java_fixture() -> None:
    source_line = '\fString fixture = "assumeTrue(false)";'
    diff = f"""diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,1 @@
+{source_line}
"""
    branch_files = (
        BranchTextFile(path="src/test/java/GuardTest.java", content=source_line),
    )

    assert _scan(diff, branch_files).ok


def test_scan_added_test_skip_guards_ends_java_literal_at_trailing_backslash() -> None:
    diff = '''diff --git a/src/test/java/GuardTest.java b/src/test/java/GuardTest.java
--- a/src/test/java/GuardTest.java
+++ b/src/test/java/GuardTest.java
@@ -0,0 +1,2 @@
+        String fixture = "path\\u005C
+        assumeTrue(false);
'''

    result = _scan(diff)

    assert [
        (violation.line_number, violation.pattern) for violation in result.violations
    ] == [(2, "JUnit assumeTrue")]


def test_scan_added_test_skip_guards_keeps_escapes_in_kotlin_quoted_strings() -> None:
    diff = '''diff --git a/src/test/kotlin/GuardTest.kt b/src/test/kotlin/GuardTest.kt
--- a/src/test/kotlin/GuardTest.kt
+++ b/src/test/kotlin/GuardTest.kt
@@ -0,0 +1,2 @@
+val quoted = "escaped \\" assumeTrue(false) still inert"
+val plain = 1
'''

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_ignores_regex_literal_starting_a_line() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,3 @@
+const pattern =
+  /test.skip("fixture", () => {})/;
+expect(source).toMatch(pattern);
"""

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_preserves_division_after_js_expressions() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -0,0 +1,2 @@
+const functionRatio = function () {} / test.skip("function", () => {}) / 2;
+const classRatio = class {} / test.skip("class", () => {}) / 2;
"""

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [1, 2]


def test_scan_added_test_skip_guards_flags_python_template_interpolation() -> None:
    diff = """diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,1 @@
+message = t"result: {pytest.skip('real skip')}"
"""

    result = _scan(diff)

    assert not result.ok
    assert result.violations[0].pattern == "pytest.skip"


def test_scan_added_test_skip_guards_tracks_multiline_python_interpolation() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -0,0 +1,6 @@
+executable = f"{(
+    pytest.skip('real skip')
+)}"
+inert = f"{(
+    value
+)} pytest.skip('fixture text')"
'''

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2]


def test_scan_added_test_skip_guards_masks_inert_python_format_spec_text() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -0,0 +1,2 @@
+inert = f"{value:pytest.skip('format text')}"
+executable = f"{value:{pytest.skip('nested field')}}"
'''

    result = _scan(diff)

    assert [violation.line_number for violation in result.violations] == [2]


def test_scan_added_test_skip_guards_fails_closed_for_unknown_literal_syntax() -> None:
    diff = '''diff --git a/tests/GuardSpec.scala b/tests/GuardSpec.scala
--- a/tests/GuardSpec.scala
+++ b/tests/GuardSpec.scala
@@ -0,0 +1,1 @@
+val message = s"${assumeTrue(false)}"
diff --git a/tests/GuardSpec.groovy b/tests/GuardSpec.groovy
--- a/tests/GuardSpec.groovy
+++ b/tests/GuardSpec.groovy
@@ -0,0 +1,1 @@
+def message = "${assumeFalse(false)}"
'''

    result = _scan(diff)

    assert [(violation.path, violation.pattern) for violation in result.violations] == [
        ("tests/GuardSpec.scala", "JUnit assumeTrue"),
        ("tests/GuardSpec.groovy", "JUnit assumeFalse"),
    ]


def test_scan_added_test_skip_guards_ignores_all_added_multiline_literal() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,3 @@
+fixture = """documentation
+pytest.skip("not executable")
+"""
'''

    assert _scan(diff).ok


def test_scan_added_test_skip_guards_uses_post_image_for_existing_literal() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -10,0 +11,1 @@
+pytest.skip("not executable")
'''
    post_image = "\n".join(
        [
            *([""] * 9),
            'fixture = """documentation',
            'pytest.skip("not executable")',
            '"""',
        ]
    )

    assert added_test_paths(diff) == ("tests/test_guard.py",)
    assert _scan(
        diff,
        (BranchTextFile(path="tests/test_guard.py", content=post_image),),
    ).ok


def test_scan_added_test_skip_guards_flags_code_after_multiline_literal() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,4 @@
+fixture = """documentation
+pytest.skip("not executable")
+"""
+pytest.skip("executable")
'''

    result = _scan(diff)

    assert not result.ok
    assert [violation.line_number for violation in result.violations] == [5]


def test_scan_added_test_skip_guards_ignores_nested_kotlin_block_comment() -> None:
    diff = '''diff --git a/tests/GuardTest.kt b/tests/GuardTest.kt
--- a/tests/GuardTest.kt
+++ b/tests/GuardTest.kt
@@ -0,0 +1,3 @@
+/* outer
+   /* inner */ assumeTrue(false)
+*/
'''

    assert _scan(diff).ok
