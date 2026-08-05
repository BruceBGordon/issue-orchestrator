#!/usr/bin/env python3
"""Ratcheted quality guardrails for architecture-control drift.

Existing debt is recorded once; future PRs fail when they add new debt or make a
tracked metric worse.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tomllib
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


BASELINE_VERSION = 1
EXIT_RATCHET_VIOLATION = 2
EXIT_STALE_BASELINE = 3
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_RUN = re.compile(r"[A-Za-z0-9]+")
_JS_POLICY_KEYWORD = re.compile(r"\b(if|while|switch|case)\b")
_JS_PAREN_KEYWORDS = {"if", "switch", "while"}
_RUFF_NUMERIC_THRESHOLD = re.compile(r"\((\d+)\s*>\s*\d+\)")
_RUFF_SYMBOL = re.compile(r"`([^`]+)`")
_RUFF_NOQA = re.compile(r"#\s*noqa(?::|\b)")
_SEMGREP_BIN_ENV = "QUALITY_GUARDRAILS_SEMGREP_BIN"
_SEMGREP_VERSION = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_SEMGREP_PIN = re.compile(r"^semgrep==(?P<version>\d+\.\d+\.\d+)$")
_SEMGREP_PROJECT = Path("tools/semgrep/pyproject.toml")
_HTTP_ROUTE_METHODS = {"delete", "get", "patch", "post", "put"}
_UI_OPENAPI_COMPONENT_PREFIX = "#/components/schemas/"
# Diagnostic wording for route metadata a decorator simply does not carry. These
# are prose for humans reading a guardrail failure, never metric identifiers.
_NO_RESPONSE_MODEL_DETAIL = "(no response_model)"
_UNKNOWN_ROUTE_PATH_DETAIL = "(unknown route path expression)"
# Machine-readable stand-in used only inside a dynamic-route metric id when the
# decorator carries no path argument to key on. Deliberately distinct from the
# prose above: metric ids are baseline keys, so rewording a diagnostic must never
# invalidate an accepted entry.
_ABSENT_ROUTE_PATH_IDENTITY = "<no-path-argument>"


@dataclass(frozen=True)
class Metric:
    rule_id: str
    kind: str
    metric_id: str
    value: int
    path: str
    detail: str
    new_metric_min_value: int = 1

    @property
    def key(self) -> str:
        return f"{self.rule_id}:{self.metric_id}"

    def to_baseline_entry(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "metric_id": self.metric_id,
            "value": self.value,
            "path": self.path,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RatchetViolation:
    key: str
    rule_id: str
    kind: str
    path: str
    previous: int | None
    current: int
    detail: str

    def fmt(self) -> str:
        if self.previous is None:
            return (
                f"{self.path} [{self.rule_id}] new {self.kind}: "
                f"{self.current} ({self.detail})"
            )
        return (
            f"{self.path} [{self.rule_id}] {self.kind} increased: "
            f"{self.previous} -> {self.current} ({self.detail})"
        )


@dataclass(frozen=True)
class StaleBaselineEntry:
    key: str
    rule_id: str
    kind: str
    path: str
    value: int
    detail: str

    def fmt(self) -> str:
        return f"{self.path} [{self.rule_id}] stale {self.kind}: {self.value} ({self.detail})"


@dataclass(frozen=True)
class GuardrailResult:
    violations: list[RatchetViolation]
    stale_entries: list[StaleBaselineEntry]


@dataclass(frozen=True)
class RouteOperation:
    method: str
    url_path: str | None
    file_path: str
    line: int
    response_model: str | None
    path_expression: str | None

    @property
    def key(self) -> tuple[str, str]:
        if self.url_path is None:
            raise ValueError("dynamic route path has no stable OpenAPI key")
        return self.method, self.url_path

    @property
    def metric_id(self) -> str:
        if self.url_path is None:
            raise ValueError("dynamic route path has no literal metric id; use dynamic_identity")
        return _route_metric_id(self.method, self.url_path)

    @property
    def dynamic_identity(self) -> str:
        """Line-independent identity of a route whose path is not a string literal.

        Keys on method, file, and path expression so an accepted baseline entry
        survives unrelated line shifts above the decorator. A decorator carrying
        no path argument has no expression to key on, so it keys on an explicit
        token instead — never on the diagnostic prose, which is free to change.
        """
        if self.url_path is not None:
            raise ValueError("literal route paths are identified by method and URL path")
        expression = (
            _ABSENT_ROUTE_PATH_IDENTITY if self.path_expression is None else self.path_expression
        )
        return f"{self.method.upper()} {self.file_path}:{expression}"

    @property
    def response_model_detail(self) -> str:
        """Render this route's response model for a guardrail failure message."""
        if self.response_model is None:
            return _NO_RESPONSE_MODEL_DETAIL
        return self.response_model

    @property
    def path_expression_detail(self) -> str:
        """Render this route's path expression for a guardrail failure message.

        Concrete expressions are quoted so they read as source; a decorator that
        carries no path argument at all renders as explicit prose instead.
        """
        if self.path_expression is None:
            return _UNKNOWN_ROUTE_PATH_DETAIL
        return repr(self.path_expression)


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping at top level")
    return data


def _load_baseline(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _write_baseline_data(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_baseline(path: Path, metrics: Sequence[Metric]) -> None:
    payload = {
        "version": BASELINE_VERSION,
        "metrics": {
            metric.key: metric.to_baseline_entry()
            for metric in sorted(metrics, key=lambda m: (m.rule_id, m.path, m.metric_id))
        },
    }
    _write_baseline_data(path, payload)


def _patterns(rule: Mapping[str, Any], key: str) -> list[str]:
    value = rule.get(key, []) or []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field {key!r} must be a list of strings")
    return value


def _new_metric_min_value(rule: Mapping[str, Any]) -> int:
    value = int(rule.get("new_metric_min_value", 1))
    if value < 1:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} new_metric_min_value must be >= 1")
    return value


def _is_excluded(rel_path: str, excludes: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in excludes)


def _iter_included_files(root: Path, rule: Mapping[str, Any]) -> Iterable[Path]:
    includes = _patterns(rule, "include")
    excludes = _patterns(rule, "exclude")
    seen: set[Path] = set()

    for pattern in includes:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            rel_path = path.relative_to(root).as_posix()
            if _is_excluded(rel_path, excludes):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _collect_file_line_budget(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    max_lines = int(rule["max_lines"])
    new_metric_min_value = _new_metric_min_value(rule)
    metrics: list[Metric] = []

    for path in _iter_included_files(root, rule):
        count = _line_count(path)
        if count <= max_lines:
            continue
        rel_path = path.relative_to(root).as_posix()
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="file_line_budget",
                metric_id=rel_path,
                value=count,
                path=rel_path,
                detail=f"{count} lines exceeds budget {max_lines}",
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def _bool_field(rule: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    value = rule.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field {key!r} must be a boolean")
    return value


def _ruff_select(rule: Mapping[str, Any]) -> list[str]:
    select = _patterns(rule, "select")
    if not select:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field 'select' must not be empty")
    return select


def _ruff_diagnostic_value(message: str) -> int:
    match = _RUFF_NUMERIC_THRESHOLD.search(message)
    if match:
        return int(match.group(1))
    return 1


def _ruff_diagnostic_identifier(diagnostic: Mapping[str, Any]) -> str:
    code = _required_string(diagnostic, "code")
    message = _required_string(diagnostic, "message")
    location = _required_mapping(diagnostic, "location")
    row = _required_int(location, "row")
    column = _required_int(location, "column")

    symbol_match = _RUFF_SYMBOL.search(message)
    if symbol_match:
        symbol = re.sub(r"[^A-Za-z0-9_.-]+", "-", symbol_match.group(1)).strip("-")
        if symbol:
            return f"{code}:{symbol}"
    return f"{code}:line-{row}:col-{column}"


def _required_mapping(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"diagnostic missing mapping field {key!r}")
    return value


def _required_string(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"diagnostic missing non-empty string field {key!r}")
    return value


def _required_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int):
        raise ValueError(f"diagnostic missing integer field {key!r}")
    return value


def _diagnostic_rel_path(root: Path, diagnostic: Mapping[str, Any]) -> str:
    raw_filename = _required_string(diagnostic, "filename")
    path = Path(raw_filename)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _run_ruff_json(
    root: Path,
    rel_paths: Sequence[str],
    *,
    select: Sequence[str],
    ignore_noqa: bool,
) -> list[Mapping[str, Any]]:
    if not rel_paths:
        return []

    cmd = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--output-format=json",
        "--select",
        ",".join(select),
    ]
    if ignore_noqa:
        cmd.append("--ignore-noqa")
    cmd.extend(rel_paths)

    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode not in {0, 1}:
        raise ValueError(f"Ruff failed with exit code {result.returncode}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Ruff emitted invalid JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError("Ruff JSON output must be a list of diagnostic objects")
    return data


def _collect_ruff_findings(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    select = _ruff_select(rule)
    ignore_noqa = _bool_field(rule, "ignore_noqa")
    new_metric_min_value = _new_metric_min_value(rule)
    rel_paths = sorted(path.relative_to(root).as_posix() for path in _iter_included_files(root, rule))
    diagnostics = _run_ruff_json(root, rel_paths, select=select, ignore_noqa=ignore_noqa)

    seen: set[str] = set()
    metrics: list[Metric] = []
    for diagnostic in diagnostics:
        rel_path = _diagnostic_rel_path(root, diagnostic)
        location = _required_mapping(diagnostic, "location")
        row = _required_int(location, "row")
        code = _required_string(diagnostic, "code")
        message = _required_string(diagnostic, "message")
        identifier = _ruff_diagnostic_identifier(diagnostic)
        metric_id = f"{rel_path}:{identifier}"
        if metric_id in seen:
            metric_id = f"{metric_id}:line-{row}"
        seen.add(metric_id)
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="ruff_finding",
                metric_id=metric_id,
                value=_ruff_diagnostic_value(message),
                path=rel_path,
                detail=f"{code} line {row}: {message}",
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def _line_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _iter_noqa_comments(path: Path) -> Iterable[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type != tokenize.COMMENT:
                continue
            if _RUFF_NOQA.search(token.string):
                yield token.start[0], " ".join(token.string.strip().split())
    except tokenize.TokenError as exc:
        raise ValueError(f"Could not tokenize {path}: {exc}") from exc


def _collect_ruff_noqa_suppressions(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    new_metric_min_value = _new_metric_min_value(rule)
    metrics: list[Metric] = []
    seen: set[str] = set()

    for path in _iter_included_files(root, rule):
        if path.suffix != ".py":
            continue
        rel_path = path.relative_to(root).as_posix()
        for line_number, normalized in _iter_noqa_comments(path):
            metric_id = f"{rel_path}:{_line_digest(normalized)}"
            if metric_id in seen:
                metric_id = f"{metric_id}:line-{line_number}"
            seen.add(metric_id)
            metrics.append(
                Metric(
                    rule_id=rule_id,
                    kind="ruff_noqa_suppression",
                    metric_id=metric_id,
                    value=1,
                    path=rel_path,
                    detail=f"line {line_number}: {normalized}",
                    new_metric_min_value=new_metric_min_value,
                )
            )

    return metrics


def _semgrep_config(root: Path, rule: Mapping[str, Any]) -> str:
    raw_config = rule.get("config")
    if not isinstance(raw_config, str) or not raw_config:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field 'config' must be a non-empty string")
    config_path = root / raw_config
    if not config_path.is_file():
        raise ValueError(f"Semgrep config not found: {raw_config}")
    return raw_config


def _semgrep_required_version(root: Path) -> str:
    project_path = root / _SEMGREP_PROJECT
    try:
        data = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read Semgrep tool project at {_SEMGREP_PROJECT}: {exc}") from exc
    dependencies = data.get("project", {}).get("dependencies", [])
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        match = _SEMGREP_PIN.match(dependency)
        if match:
            return match.group("version")
    raise ValueError(f"Semgrep tool project must pin semgrep with `semgrep==X.Y.Z`: {_SEMGREP_PROJECT}")


def _semgrep_default_binary(root: Path) -> Path | None:
    candidates = [
        root / ".venv-semgrep" / "bin" / "semgrep",
        root / ".venv-semgrep" / "Scripts" / "semgrep.exe",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _semgrep_command(root: Path) -> list[str]:
    configured = os.environ.get(_SEMGREP_BIN_ENV)
    if configured:
        cmd = [configured]
    else:
        default_binary = _semgrep_default_binary(root)
        if default_binary is None:
            raise ValueError(
                "Semgrep tool environment not found at .venv-semgrep. "
                "Run `make semgrep-venv` or `make worktree-setup`."
            )
        cmd = [str(default_binary)]

    _verify_semgrep_version(root, cmd, required_version=_semgrep_required_version(root))
    return cmd


def _verify_semgrep_version(root: Path, cmd: Sequence[str], *, required_version: str) -> None:
    result = subprocess.run(
        [*cmd, "--version"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(
            f"Semgrep version check failed with exit code {result.returncode}: {result.stderr.strip()}"
        )
    version_text = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    match = _SEMGREP_VERSION.search(version_text)
    if match is None:
        raise ValueError(f"Could not determine Semgrep version from: {version_text!r}")
    version = match.group(1)
    if version != required_version:
        raise ValueError(
            f"Semgrep version mismatch: expected {required_version}, got {version}. "
            "Run `make semgrep-venv` to install the locked tool version."
        )


def _run_semgrep_json(
    root: Path,
    rel_paths: Sequence[str],
    *,
    config: str,
) -> list[Mapping[str, Any]]:
    if not rel_paths:
        return []

    cmd = [
        *_semgrep_command(root),
        "scan",
        "--config",
        config,
        "--json",
        "--metrics=off",
        "--disable-version-check",
        "--no-rewrite-rule-ids",
        "--quiet",
        *rel_paths,
    ]
    result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(f"Semgrep failed with exit code {result.returncode}: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Semgrep emitted invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Semgrep JSON output must be an object")
    errors = data.get("errors", []) or []
    if errors:
        raise ValueError(f"Semgrep reported errors: {errors!r}")
    findings = data.get("results", []) or []
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise ValueError("Semgrep JSON field 'results' must be a list of finding objects")
    return findings


def _semgrep_source_digest(root: Path, rel_path: str, line: int) -> str:
    path = root / rel_path
    lines = path.read_text(encoding="utf-8").splitlines()
    if line < 1 or line > len(lines):
        raise ValueError(f"{rel_path}: Semgrep finding line {line} is outside file bounds")
    normalized = " ".join(lines[line - 1].strip().split())
    return _line_digest(normalized)


def _semgrep_finding_identifier(root: Path, rel_path: str, finding: Mapping[str, Any]) -> str:
    check_id = _required_string(finding, "check_id")
    start = _required_mapping(finding, "start")
    line = _required_int(start, "line")
    return f"{check_id}:{_semgrep_source_digest(root, rel_path, line)}"


def _semgrep_rel_path(root: Path, finding: Mapping[str, Any]) -> str:
    raw_path = _required_string(finding, "path")
    path = Path(raw_path)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _collect_semgrep_findings(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    config = _semgrep_config(root, rule)
    new_metric_min_value = _new_metric_min_value(rule)
    rel_paths = sorted(path.relative_to(root).as_posix() for path in _iter_included_files(root, rule))
    findings = _run_semgrep_json(root, rel_paths, config=config)

    seen: set[str] = set()
    metrics: list[Metric] = []
    for finding in findings:
        rel_path = _semgrep_rel_path(root, finding)
        start = _required_mapping(finding, "start")
        line = _required_int(start, "line")
        check_id = _required_string(finding, "check_id")
        extra = finding.get("extra", {}) or {}
        if not isinstance(extra, dict):
            raise ValueError("Semgrep finding field 'extra' must be a mapping when present")
        message = extra.get("message", "")
        if not isinstance(message, str):
            raise ValueError("Semgrep finding extra.message must be a string when present")
        metric_id = f"{rel_path}:{_semgrep_finding_identifier(root, rel_path, finding)}"
        if metric_id in seen:
            metric_id = f"{metric_id}:line-{line}"
        seen.add(metric_id)
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="semgrep_finding",
                metric_id=metric_id,
                value=1,
                path=rel_path,
                detail=f"{check_id} line {line}: {message}".rstrip(),
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def _control_term_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RUN.finditer(text):
        for part in _CAMEL_BOUNDARY.sub(" ", match.group(0)).split():
            tokens.append(part.lower())
    return tokens


def _control_phrase_tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RUN.finditer(text)]


def _token_matches_term(token: str, term: str) -> bool:
    return token in {term, f"{term}s", f"{term}es"} or (term.endswith("y") and token == f"{term[:-1]}ies")


def _contains_term_tokens(text_tokens: Sequence[str], term_tokens: Sequence[str]) -> bool:
    if not text_tokens or not term_tokens:
        return False
    if len(term_tokens) == 1:
        return any(_token_matches_term(token, term_tokens[0]) for token in text_tokens)
    width = len(term_tokens)
    return any(list(text_tokens[index : index + width]) == list(term_tokens) for index in range(len(text_tokens) - width + 1))


def _contains_any_term(text: str, terms: Sequence[str]) -> bool:
    text_tokens = _control_term_tokens(text)
    phrase_tokens = _control_phrase_tokens(text)
    for term in terms:
        term_tokens = _control_term_tokens(term)
        if len(term_tokens) == 1 and _contains_term_tokens(text_tokens, term_tokens):
            return True
        if len(term_tokens) > 1 and (
            _contains_term_tokens(phrase_tokens, term_tokens) or _contains_term_tokens(text_tokens, term_tokens)
        ):
            return True
    return False


def _python_policy_site_count(path: Path, terms: Sequence[str]) -> int:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path.as_posix())
    count = 0

    for node in ast.walk(tree):
        candidate: ast.AST | None = None
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            candidate = node.test
        elif isinstance(node, ast.Match):
            candidate = node.subject

        if candidate is None:
            continue

        segment = ast.get_source_segment(source, candidate)
        if segment and _contains_any_term(segment, terms):
            count += 1

    return count


def _js_policy_scan_sources(source: str) -> tuple[str, str]:
    term_chars = list(source)
    structure_chars = list(source)
    index = 0

    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""

        if char == "/" and next_char == "/":
            index = _blank_js_comment(term_chars, structure_chars, source, index, line_comment=True)
            continue

        if char == "/" and next_char == "*":
            index = _blank_js_comment(term_chars, structure_chars, source, index, line_comment=False)
            continue

        if char in {"'", '"', "`"}:
            index = _blank_js_string_structure(structure_chars, source, index, char)
            continue

        index += 1

    return "".join(term_chars), "".join(structure_chars)


def _blank_js_char(chars: list[str], index: int, source: str) -> None:
    chars[index] = "\n" if source[index] == "\n" else " "


def _blank_js_comment(
    term_chars: list[str],
    structure_chars: list[str],
    source: str,
    index: int,
    *,
    line_comment: bool,
) -> int:
    end = index + 2
    while end < len(source):
        if line_comment and source[end] == "\n":
            break
        if not line_comment and source[end : end + 2] == "*/":
            end += 2
            break
        end += 1

    for blank_index in range(index, end):
        _blank_js_char(term_chars, blank_index, source)
        _blank_js_char(structure_chars, blank_index, source)
    return end


def _blank_js_string_structure(
    structure_chars: list[str],
    source: str,
    start_index: int,
    quote: str,
) -> int:
    _blank_js_char(structure_chars, start_index, source)
    index = start_index + 1
    escaped = False

    while index < len(source):
        char = source[index]
        _blank_js_char(structure_chars, index, source)
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == quote:
            return index + 1
        index += 1

    return index


def _skip_js_whitespace(source: str, index: int) -> int:
    while index < len(source) and source[index].isspace():
        index += 1
    return index


def _find_matching_js_paren(structure_source: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(structure_source)):
        char = structure_source[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_js_case_colon(structure_source: str, start_index: int) -> int | None:
    depth = 0
    for index in range(start_index, len(structure_source)):
        char = structure_source[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(depth - 1, 0)
        elif char == ":" and depth == 0:
            return index
    return None


def _iter_js_policy_segments(source: str) -> Iterable[str]:
    term_source, structure_source = _js_policy_scan_sources(source)

    for match in _JS_POLICY_KEYWORD.finditer(structure_source):
        keyword = match.group(1)
        segment_start = _skip_js_whitespace(term_source, match.end())

        if keyword in _JS_PAREN_KEYWORDS:
            if segment_start >= len(structure_source) or structure_source[segment_start] != "(":
                continue
            segment_end = _find_matching_js_paren(structure_source, segment_start)
            if segment_end is not None:
                yield term_source[segment_start + 1 : segment_end]
            continue

        segment_end = _find_js_case_colon(structure_source, segment_start)
        if segment_end is not None:
            yield term_source[segment_start:segment_end]


def _js_policy_site_count(path: Path, terms: Sequence[str]) -> int:
    source = path.read_text(encoding="utf-8")
    return sum(1 for segment in _iter_js_policy_segments(source) if _contains_any_term(segment, terms))


def _collect_control_policy_branch_sites(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    terms = _patterns(rule, "terms")
    new_metric_min_value = _new_metric_min_value(rule)
    metrics: list[Metric] = []

    for path in _iter_included_files(root, rule):
        rel_path = path.relative_to(root).as_posix()
        try:
            if path.suffix == ".py":
                count = _python_policy_site_count(path, terms)
            elif path.suffix == ".js":
                count = _js_policy_site_count(path, terms)
            else:
                continue
        except SyntaxError as exc:
            raise ValueError(f"{rel_path}: syntax error while collecting policy sites: {exc}") from exc

        if count == 0:
            continue
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="control_policy_branch_sites",
                metric_id=rel_path,
                value=count,
                path=rel_path,
                detail=f"{count} branch site(s) mention lifecycle/control terms",
                new_metric_min_value=new_metric_min_value,
            )
        )

    return metrics


def _route_metric_id(method: str, url_path: str) -> str:
    return f"{method.upper()} {url_path}"


def _matches_patterns(rel_path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def _route_literal_path(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    for keyword in call.keywords:
        if keyword.arg == "path" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _route_path_expression(call: ast.Call) -> str | None:
    """Return the route's path source expression, or None when it has no path argument."""
    if call.args:
        return ast.unparse(call.args[0])
    for keyword in call.keywords:
        if keyword.arg == "path":
            return ast.unparse(keyword.value)
    return None


def _expr_model_name(expr: ast.AST) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ast.unparse(expr)


def _response_model_name(call: ast.Call) -> str | None:
    for keyword in call.keywords:
        if keyword.arg == "response_model":
            return _expr_model_name(keyword.value)
    return None


def _iter_route_operations(root: Path, rule: Mapping[str, Any]) -> Iterable[RouteOperation]:
    include = _patterns(rule, "route_include")
    exclude = _patterns(rule, "route_exclude")
    route_rule = {"id": rule["id"], "include": include, "exclude": exclude}

    for path in _iter_included_files(root, route_rule):
        if path.suffix != ".py":
            continue
        rel_path = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
        except SyntaxError as exc:
            raise ValueError(f"{rel_path}: syntax error while collecting UI OpenAPI routes: {exc}") from exc

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr.lower()
                if method not in _HTTP_ROUTE_METHODS:
                    continue
                url_path = _route_literal_path(decorator)
                yield RouteOperation(
                    method=method,
                    url_path=url_path,
                    file_path=rel_path,
                    line=decorator.lineno,
                    response_model=_response_model_name(decorator),
                    path_expression=_route_path_expression(decorator),
                )


def _schema_component_name(schema: Mapping[str, Any], *, schema_path: str, route_key: str) -> str:
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith(_UI_OPENAPI_COMPONENT_PREFIX):
        raise ValueError(f"{schema_path}: {route_key} 200 application/json schema must be a component $ref")
    component = ref.removeprefix(_UI_OPENAPI_COMPONENT_PREFIX)
    if not component:
        raise ValueError(f"{schema_path}: {route_key} component $ref is empty")
    return component


def _ui_openapi_response_schema(operation: Mapping[str, Any], *, schema_path: str, route_key: str) -> Mapping[str, Any]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise ValueError(f"{schema_path}: {route_key} operation missing responses mapping")
    response = responses.get("200")
    if not isinstance(response, dict):
        raise ValueError(f"{schema_path}: {route_key} operation missing 200 response")
    content = response.get("content")
    if not isinstance(content, dict):
        raise ValueError(f"{schema_path}: {route_key} 200 response missing content mapping")
    json_content = content.get("application/json")
    if not isinstance(json_content, dict):
        raise ValueError(f"{schema_path}: {route_key} 200 response missing application/json content")
    schema = json_content.get("schema")
    if not isinstance(schema, dict):
        raise ValueError(f"{schema_path}: {route_key} application/json content missing schema")
    return schema


def _load_ui_openapi_operations(root: Path, rule: Mapping[str, Any]) -> dict[tuple[str, str], str]:
    schema_rel = rule.get("schema")
    if not isinstance(schema_rel, str) or not schema_rel:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field 'schema' must be a non-empty string")
    schema_path = root / schema_rel
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{schema_rel}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{schema_rel} must contain a JSON object")
    paths = data.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{schema_rel} missing paths mapping")

    operations: dict[tuple[str, str], str] = {}
    for url_path, raw_methods in paths.items():
        if not isinstance(url_path, str) or not isinstance(raw_methods, dict):
            raise ValueError(f"{schema_rel}: paths entries must map string paths to operation mappings")
        for method, operation in raw_methods.items():
            method_name = str(method).lower()
            if method_name not in _HTTP_ROUTE_METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError(f"{schema_rel}: {_route_metric_id(method_name, url_path)} operation must be a mapping")
            route_key = _route_metric_id(method_name, url_path)
            schema = _ui_openapi_response_schema(operation, schema_path=schema_rel, route_key=route_key)
            operations[(method_name, url_path)] = _schema_component_name(
                schema,
                schema_path=schema_rel,
                route_key=route_key,
            )
    return operations


def _path_prefixes(rule: Mapping[str, Any], key: str) -> list[str]:
    prefixes = _patterns(rule, key)
    if not prefixes:
        raise ValueError(f"rule {rule.get('id', '<unknown>')} field {key!r} must not be empty")
    return prefixes


def _is_browser_route(route: RouteOperation, rule: Mapping[str, Any]) -> bool:
    browser_route_include = _patterns(rule, "browser_route_include")
    prefixes = _path_prefixes(rule, "browser_path_prefixes")
    if not _matches_patterns(route.file_path, browser_route_include):
        return False
    if route.url_path is None:
        return True
    return any(route.url_path.startswith(prefix) for prefix in prefixes)


def _collect_ui_openapi_routes(root: Path, rule: Mapping[str, Any]) -> list[Metric]:
    rule_id = str(rule["id"])
    new_metric_min_value = _new_metric_min_value(rule)
    schema_rel = str(rule["schema"])
    schema_operations = _load_ui_openapi_operations(root, rule)
    routes = list(_iter_route_operations(root, rule))
    routes_by_key: dict[tuple[str, str], list[RouteOperation]] = {}
    for route in routes:
        if route.url_path is None:
            continue
        routes_by_key.setdefault(route.key, []).append(route)

    metrics: list[Metric] = []

    for (method, url_path), expected_model in sorted(schema_operations.items(), key=lambda item: item[0]):
        route_key = _route_metric_id(method, url_path)
        matching_routes = routes_by_key.get((method, url_path), [])
        if not matching_routes:
            metrics.append(
                Metric(
                    rule_id=rule_id,
                    kind="ui_openapi_missing_route",
                    metric_id=f"missing:{route_key}",
                    value=1,
                    path=schema_rel,
                    detail=f"{route_key} exists in {schema_rel} but no FastAPI route was found",
                    new_metric_min_value=new_metric_min_value,
                )
            )
            continue

        if any(route.response_model == expected_model for route in matching_routes):
            continue
        actual_models = ", ".join(
            sorted({route.response_model_detail for route in matching_routes})
        )
        primary_route = matching_routes[0]
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="ui_openapi_response_model_drift",
                metric_id=f"response-model:{route_key}",
                value=1,
                path=primary_route.file_path,
                detail=(
                    f"{route_key} must use response_model={expected_model} "
                    f"from {schema_rel}; found {actual_models}"
                ),
                new_metric_min_value=new_metric_min_value,
            )
        )

    dynamic_routes: list[RouteOperation] = []

    for route in sorted(routes, key=lambda item: (item.file_path, item.line, item.method, item.url_path or "")):
        if not _is_browser_route(route, rule):
            continue
        if route.url_path is None:
            dynamic_routes.append(route)
            continue
        if route.key in schema_operations:
            continue
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="ui_openapi_uncontracted_route",
                metric_id=f"uncontracted:{route.metric_id}",
                value=1,
                path=route.file_path,
                detail=(
                    f"{route.metric_id} is a browser-facing JSON route absent from {schema_rel}; "
                    "add it to the UI OpenAPI contract or explicitly accept the ratchet"
                ),
                new_metric_min_value=new_metric_min_value,
            )
        )

    metrics.extend(
        _dynamic_route_metrics(
            dynamic_routes,
            rule_id=rule_id,
            new_metric_min_value=new_metric_min_value,
        )
    )

    return metrics


def _dynamic_route_metrics(
    routes: Sequence[RouteOperation],
    *,
    rule_id: str,
    new_metric_min_value: int,
) -> list[Metric]:
    """Metrics for browser-facing routes whose path is not a string literal.

    Owns dynamic-route metric identity: ids key on method, file, and path
    expression rather than the source line, so an intentionally accepted baseline
    entry is not invalidated by unrelated edits above the decorator. Repeated
    identical decorators in one file are disambiguated by occurrence order (the
    source line stays in the human-readable detail).
    """
    occurrences: Counter[str] = Counter()
    metrics: list[Metric] = []
    for route in routes:
        identity = route.dynamic_identity
        occurrences[identity] += 1
        ordinal = occurrences[identity]
        metric_id = identity if ordinal == 1 else f"{identity}#{ordinal}"
        metrics.append(
            Metric(
                rule_id=rule_id,
                kind="ui_openapi_dynamic_route_path",
                metric_id=f"dynamic-path:{metric_id}",
                value=1,
                path=route.file_path,
                detail=(
                    f"{route.method.upper()} route at line {route.line} uses dynamic path "
                    f"{route.path_expression_detail}; browser-facing routes must use string "
                    "literals so the UI OpenAPI guardrail can compare them"
                ),
                new_metric_min_value=new_metric_min_value,
            )
        )
    return metrics


def collect_metrics(root: Path, config: Mapping[str, Any]) -> list[Metric]:
    rules = config.get("rules", []) or []
    if not isinstance(rules, list):
        raise ValueError("quality guardrail config field 'rules' must be a list")

    collectors = {
        "file_line_budget": _collect_file_line_budget,
        "control_policy_branch_sites": _collect_control_policy_branch_sites,
        "ruff_findings": _collect_ruff_findings,
        "ruff_noqa_suppressions": _collect_ruff_noqa_suppressions,
        "semgrep_findings": _collect_semgrep_findings,
        "ui_openapi_routes": _collect_ui_openapi_routes,
    }
    metrics: list[Metric] = []

    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("quality guardrail rules must be mappings")
        rule_id = rule.get("id")
        rule_type = rule.get("type")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("quality guardrail rule missing non-empty 'id'")
        if rule_type not in collectors:
            raise ValueError(f"quality guardrail rule {rule_id!r} has unsupported type {rule_type!r}")
        metrics.extend(collectors[rule_type](root, rule))

    return sorted(metrics, key=lambda m: (m.rule_id, m.path, m.metric_id))


def compare_to_baseline(metrics: Sequence[Metric], baseline: Mapping[str, Any]) -> list[RatchetViolation]:
    raw_entries = _baseline_entries(baseline)

    current_by_key = {metric.key: metric for metric in metrics}
    violations: list[RatchetViolation] = []

    for key, metric in sorted(current_by_key.items()):
        raw_previous = raw_entries.get(key)
        if raw_previous is None:
            if metric.value < metric.new_metric_min_value:
                continue
            violations.append(
                RatchetViolation(
                    key=key,
                    rule_id=metric.rule_id,
                    kind=metric.kind,
                    path=metric.path,
                    previous=None,
                    current=metric.value,
                    detail=metric.detail,
                )
            )
            continue
        previous = _baseline_entry_mapping(key, raw_previous)
        previous_value = _baseline_entry_int(key, previous, "value")
        if metric.value > previous_value:
            violations.append(
                RatchetViolation(
                    key=key,
                    rule_id=metric.rule_id,
                    kind=metric.kind,
                    path=metric.path,
                    previous=previous_value,
                    current=metric.value,
                    detail=metric.detail,
                )
            )

    return violations


def _baseline_entries(baseline: Mapping[str, Any]) -> dict[str, Any]:
    raw_entries = baseline.get("metrics", {}) or {}
    if not isinstance(raw_entries, dict):
        raise ValueError("baseline field 'metrics' must be a mapping")
    return raw_entries


def _baseline_entry_mapping(key: str, raw_entry: Any) -> Mapping[str, Any]:
    if not isinstance(raw_entry, dict):
        raise ValueError(f"baseline entry {key!r} must be a mapping")
    return raw_entry


def _baseline_entry_text(key: str, raw_entry: Mapping[str, Any], field: str) -> str:
    value = raw_entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"baseline entry {key!r} missing non-empty string field {field!r}")
    return value


def _baseline_entry_int(key: str, raw_entry: Mapping[str, Any], field: str) -> int:
    value = raw_entry.get(field)
    if not isinstance(value, int):
        raise ValueError(f"baseline entry {key!r} missing integer field {field!r}")
    return value


def find_stale_baseline_entries(
    metrics: Sequence[Metric],
    baseline: Mapping[str, Any],
) -> list[StaleBaselineEntry]:
    current_keys = {metric.key for metric in metrics}
    stale: list[StaleBaselineEntry] = []

    for key, raw_entry in sorted(_baseline_entries(baseline).items()):
        if key in current_keys:
            continue
        entry = _baseline_entry_mapping(key, raw_entry)
        stale.append(
            StaleBaselineEntry(
                key=key,
                rule_id=_baseline_entry_text(key, entry, "rule_id"),
                kind=_baseline_entry_text(key, entry, "kind"),
                path=_baseline_entry_text(key, entry, "path"),
                value=_baseline_entry_int(key, entry, "value"),
                detail=_baseline_entry_text(key, entry, "detail"),
            )
        )

    return stale


def accept_baseline_keys(
    baseline_path: Path,
    metrics: Sequence[Metric],
    keys: Sequence[str],
) -> dict[str, Any]:
    if not baseline_path.exists():
        raise ValueError(f"baseline not found: {baseline_path}")

    baseline = _load_baseline(baseline_path)
    raw_entries = baseline.setdefault("metrics", {})
    if not isinstance(raw_entries, dict):
        raise ValueError("baseline field 'metrics' must be a mapping")

    current_by_key = {metric.key: metric for metric in metrics}
    missing = [key for key in keys if key not in current_by_key]
    if missing:
        raise ValueError(f"cannot accept missing current metric(s): {', '.join(missing)}")

    baseline["version"] = BASELINE_VERSION
    for key in keys:
        raw_entries[key] = current_by_key[key].to_baseline_entry()

    _write_baseline_data(baseline_path, baseline)
    return baseline


def prune_baseline_keys(
    baseline_path: Path,
    metrics: Sequence[Metric],
    keys: Sequence[str],
) -> dict[str, Any]:
    if not baseline_path.exists():
        raise ValueError(f"baseline not found: {baseline_path}")

    baseline = _load_baseline(baseline_path)
    raw_entries = _baseline_entries(baseline)
    current_keys = {metric.key for metric in metrics}

    missing = [key for key in keys if key not in raw_entries]
    if missing:
        raise ValueError(f"cannot prune key(s) not present in baseline: {', '.join(missing)}")

    current = [key for key in keys if key in current_keys]
    if current:
        raise ValueError(f"cannot prune current metric key(s): {', '.join(current)}")

    baseline["version"] = BASELINE_VERSION
    for key in keys:
        del raw_entries[key]

    _write_baseline_data(baseline_path, baseline)
    return baseline


def _print_text_report(
    metrics: Sequence[Metric],
    violations: Sequence[RatchetViolation],
    stale_entries: Sequence[StaleBaselineEntry],
) -> None:
    print(f"Quality guardrails tracked metrics: {len(metrics)}")
    by_rule: dict[str, int] = {}
    for metric in metrics:
        by_rule[metric.rule_id] = by_rule.get(metric.rule_id, 0) + 1
    for rule_id, count in sorted(by_rule.items()):
        print(f"  {rule_id}: {count}")

    if stale_entries:
        print("\nQuality guardrails stale baseline entries:", file=sys.stderr)
        for entry in stale_entries:
            print(f"  {entry.fmt()}", file=sys.stderr)

    if violations:
        print("\nQuality guardrails ratchet violations:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.fmt()}", file=sys.stderr)


def _print_json_report(
    metrics: Sequence[Metric],
    violations: Sequence[RatchetViolation],
    stale_entries: Sequence[StaleBaselineEntry],
) -> None:
    payload = {
        "metrics": [metric.to_baseline_entry() for metric in metrics],
        "stale_entries": [
            {
                "key": entry.key,
                "rule_id": entry.rule_id,
                "kind": entry.kind,
                "path": entry.path,
                "value": entry.value,
                "detail": entry.detail,
            }
            for entry in stale_entries
        ],
        "violations": [
            {
                "key": violation.key,
                "rule_id": violation.rule_id,
                "kind": violation.kind,
                "path": violation.path,
                "previous": violation.previous,
                "current": violation.current,
                "detail": violation.detail,
            }
            for violation in violations
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def _operation_count(args: argparse.Namespace) -> int:
    return int(args.update_baseline) + int(bool(args.accept)) + int(bool(args.prune))


def _load_required_baseline(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValueError(f"baseline not found: {path}")
    return _load_baseline(path)


def _compare_and_check_stale(
    metrics: Sequence[Metric],
    baseline: Mapping[str, Any],
    *,
    check_stale: bool,
) -> GuardrailResult:
    stale_entries = find_stale_baseline_entries(metrics, baseline) if check_stale else []
    return GuardrailResult(
        violations=compare_to_baseline(metrics, baseline),
        stale_entries=stale_entries,
    )


def _run_guardrail_operation(
    args: argparse.Namespace,
    baseline_path: Path,
    metrics: Sequence[Metric],
) -> GuardrailResult:
    if _operation_count(args) > 1:
        raise ValueError("--update-baseline, --accept, and --prune cannot be combined")

    if args.update_baseline:
        _write_baseline(baseline_path, metrics)
        return GuardrailResult(violations=[], stale_entries=[])

    if args.accept:
        baseline = accept_baseline_keys(baseline_path, metrics, args.accept)
        return _compare_and_check_stale(metrics, baseline, check_stale=args.check_stale)

    if args.prune:
        baseline = prune_baseline_keys(baseline_path, metrics, args.prune)
        return _compare_and_check_stale(metrics, baseline, check_stale=args.check_stale)

    if args.fail_on_new or args.check_stale:
        baseline = _load_required_baseline(baseline_path)
        return GuardrailResult(
            violations=compare_to_baseline(metrics, baseline) if args.fail_on_new else [],
            stale_entries=find_stale_baseline_entries(metrics, baseline) if args.check_stale else [],
        )

    return GuardrailResult(violations=[], stale_entries=[])


def _print_operation_messages(args: argparse.Namespace, root: Path, baseline_path: Path) -> None:
    if args.update_baseline:
        print(f"Quality guardrails baseline updated: {baseline_path.relative_to(root)}")
    if args.accept:
        accepted = ", ".join(args.accept)
        print(f"Quality guardrails accepted baseline key(s): {accepted}")
    if args.prune:
        pruned = ", ".join(args.prune)
        print(f"Quality guardrails pruned baseline key(s): {pruned}")


def _result_exit_code(result: GuardrailResult) -> int:
    if result.violations:
        return EXIT_RATCHET_VIOLATION
    if result.stale_entries:
        return EXIT_STALE_BASELINE
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Repository root to scan")
    parser.add_argument("--config", default="tools/quality_guardrails.yml")
    parser.add_argument("--baseline", default="quality/guardrails-baseline.json")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument(
        "--accept",
        action="append",
        default=[],
        metavar="KEY",
        help="Update one baseline metric key from current results without rewriting all entries",
    )
    parser.add_argument(
        "--prune",
        action="append",
        default=[],
        metavar="KEY",
        help="Remove one stale baseline metric key without rewriting all entries",
    )
    parser.add_argument("--check-stale", action="store_true", help="Fail when the baseline contains stale entries")
    parser.add_argument("--fail-on-new", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve()
    baseline_path = (root / args.baseline).resolve()

    try:
        config = _load_yaml(config_path)
        metrics = collect_metrics(root, config)
        result = _run_guardrail_operation(args, baseline_path, metrics)

        if args.format == "json":
            _print_json_report(metrics, result.violations, result.stale_entries)
        else:
            _print_operation_messages(args, root, baseline_path)
            _print_text_report(metrics, result.violations, result.stale_entries)

        return _result_exit_code(result)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        print(f"Quality guardrails error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
