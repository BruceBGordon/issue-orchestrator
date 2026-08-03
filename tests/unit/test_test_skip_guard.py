"""Tests for branch-diff test skip guard."""

from issue_orchestrator.control.test_skip_guard import (
    iter_added_diff_lines,
    scan_added_test_skip_guards,
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

    result = scan_added_test_skip_guards(diff)

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

    result = scan_added_test_skip_guards(diff)

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

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_ignores_documentation_mentions() -> None:
    diff = """diff --git a/docs/testing.md b/docs/testing.md
--- a/docs/testing.md
+++ b/docs/testing.md
@@ -1,0 +2,1 @@
+Document why assumeTrue is not allowed in tests.
"""

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_ignores_nested_diff_fixture_lines() -> None:
    diff = """diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -1,0 +2,1 @@
++        assumeTrue(PostgresTestSupport.isAvailable())
"""

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_ignores_quoted_nested_diff_fixture_lines() -> None:
    diff = """diff --git a/tests/unit/test_guard.py b/tests/unit/test_guard.py
--- a/tests/unit/test_guard.py
+++ b/tests/unit/test_guard.py
@@ -1,0 +2,1 @@
+                "+        assumeTrue(PostgresTestSupport.isAvailable())\\n"
"""

    assert scan_added_test_skip_guards(diff).ok


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

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_still_flags_code_after_a_literal() -> None:
    diff = """diff --git a/audit/checks.test.ts b/audit/checks.test.ts
--- a/audit/checks.test.ts
+++ b/audit/checks.test.ts
@@ -1,0 +2,1 @@
+    log("checking suite"); describe.skip('suite', () => {});
"""

    result = scan_added_test_skip_guards(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_js_template_interpolation() -> None:
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -1,0 +2,1 @@
+const message = `result: ${test.skip("real skip", () => {})}`;
"""

    result = scan_added_test_skip_guards(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_flags_python_f_string_interpolation() -> None:
    diff = """diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,1 @@
+message = f"result: {pytest.skip('real skip')}"
"""

    result = scan_added_test_skip_guards(diff)

    assert not result.ok
    assert result.violations[0].pattern == "pytest.skip"


def test_scan_added_test_skip_guards_ignores_escaped_python_f_string_braces() -> None:
    diff = """diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,1 @@
+message = f"example: {{pytest.skip('not executable')}}"
"""

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_does_not_treat_js_regex_apostrophe_as_quote() -> (
    None
):
    diff = """diff --git a/tests/guard.test.ts b/tests/guard.test.ts
--- a/tests/guard.test.ts
+++ b/tests/guard.test.ts
@@ -1,0 +2,1 @@
+const contraction = /don't/; test.skip("real skip", () => {});
"""

    result = scan_added_test_skip_guards(diff)

    assert not result.ok
    assert result.violations[0].pattern == "JS test skip"


def test_scan_added_test_skip_guards_ignores_all_added_multiline_literal() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -1,0 +2,3 @@
+fixture = """documentation
+pytest.skip("not executable")
+"""
'''

    assert scan_added_test_skip_guards(diff).ok


def test_scan_added_test_skip_guards_uses_context_opened_multiline_literal() -> None:
    diff = '''diff --git a/tests/test_guard.py b/tests/test_guard.py
--- a/tests/test_guard.py
+++ b/tests/test_guard.py
@@ -10,2 +10,3 @@
 fixture = """documentation
+pytest.skip("not executable")
 """
'''

    assert scan_added_test_skip_guards(diff).ok


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

    result = scan_added_test_skip_guards(diff)

    assert not result.ok
    assert [violation.line_number for violation in result.violations] == [5]
