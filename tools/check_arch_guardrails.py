#!/usr/bin/env python3
# AST guardrails checker (agent-hostile backstop).
#
# Reads tools/ast_guardrails.yml for rules.
# Detects:
# - forbidden imports
# - forbidden dynamic imports (__import__, importlib.import_module)
# - forbidden calls (subprocess.run, os.system, etc.)
#
# Exit codes: 0 OK, 2 violations, 1 unexpected error

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

import yaml


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    col: int
    kind: str
    detail: str

    def fmt(self) -> str:
        return f"{self.path}:{self.lineno}:{self.col} [{self.kind}] {self.detail}"


def iter_py_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        parts = set(p.parts)
        if (
            "__pycache__" in parts
            or ".venv" in parts
            or "venv" in parts
            or ".tox" in parts
        ):
            continue
        yield p


def base_mod(name: str) -> str:
    return name.split(".", 1)[0]


def is_allowed(path: Path, allow_prefixes: Sequence[str]) -> bool:
    p = path.as_posix()
    return any(p.startswith(prefix.rstrip("/")) for prefix in allow_prefixes)


def get_attr_call(node: ast.AST) -> Tuple[Optional[str], Optional[str]]:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None, None


def load_rules(rules_path: Path) -> dict:
    with rules_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("ast_guardrails.yml must contain a mapping at top level")
    return data


def _is_git_subprocess_call(node: ast.Call) -> bool:
    m, a = get_attr_call(node.func)
    if not (m and a) or m != "subprocess":
        return False
    if a not in {"run", "Popen", "call", "check_call", "check_output"}:
        return False
    if not node.args:
        return False
    arg0 = node.args[0]
    if not isinstance(arg0, (ast.List, ast.Tuple)) or not arg0.elts:
        return False
    first = arg0.elts[0]
    return isinstance(first, ast.Constant) and first.value == "git"


def matches_module(import_name: str, deny_patterns: Sequence[str]) -> Optional[str]:
    """Check if an import matches any of the deny patterns.

    Returns the matching pattern if found, None otherwise.
    """
    for pattern in deny_patterns:
        # Match if import starts with pattern (e.g., 'issue_orchestrator.adapters' matches 'issue_orchestrator.adapters.github')
        if import_name == pattern or import_name.startswith(pattern + "."):
            return pattern
    return None


def resolve_relative_import(
    path: Path, module: Optional[str], level: int
) -> Optional[str]:
    """Resolve a relative import to an absolute module name.

    Args:
        path: Path to the file containing the import
        module: The module name from ast.ImportFrom (e.g., 'adapters.github')
        level: Number of dots (e.g., 2 for 'from ..adapters')

    Returns:
        Absolute module name (e.g., 'issue_orchestrator.adapters.github')
    """
    if level == 0:
        return module

    # Convert path to module parts
    # e.g., 'src/issue_orchestrator/entrypoints/web.py' -> ['issue_orchestrator', 'entrypoints', 'web']
    parts = list(path.with_suffix("").parts)

    # Find 'issue_orchestrator' in path and use that as the root
    try:
        root_idx = parts.index("issue_orchestrator")
        parts = parts[root_idx:]  # Start from issue_orchestrator
    except ValueError:
        return module  # Can't resolve, return as-is

    # Remove 'level' number of parts from the end (for the dots)
    # level=1 means current package, level=2 means parent, etc.
    if level > len(parts):
        return module
    parts = parts[:-level] if level > 0 else parts

    # Append the imported module
    if module:
        parts.extend(module.split("."))

    return ".".join(parts)


def check_layer_boundaries(path: Path, tree: ast.AST, rules: dict) -> list[Violation]:
    """Check layer boundary rules (e.g., entrypoints cannot import adapters)."""
    violations: list[Violation] = []
    layer_rules = rules.get("layer_boundaries", []) or []

    for rule in layer_rules:
        deny_in = rule.get("deny_in", []) or []
        deny_imports = rule.get("deny_imports", []) or []
        allow = rule.get("allow", []) or []
        rule_name = rule.get("name", "layer-boundary")

        # Check if this file is in a denied path
        p = path.as_posix()
        in_denied_path = any(p.startswith(prefix.rstrip("/")) for prefix in deny_in)

        # Check if this file is explicitly allowed
        is_allowed_file = any(
            p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in allow
        )

        if not in_denied_path or is_allowed_file:
            continue

        # Check imports
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    match = matches_module(alias.name, deny_imports)
                    if match:
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                rule_name,
                                f"import {alias.name} (forbidden: {match})",
                            )
                        )

            if isinstance(node, ast.ImportFrom):
                # Resolve relative imports to absolute module names
                resolved = resolve_relative_import(path, node.module, node.level)
                if resolved:
                    match = matches_module(resolved, deny_imports)
                    if match:
                        # Format the import for display
                        dots = "." * node.level
                        display_module = f"{dots}{node.module}" if node.module else dots
                        violations.append(
                            Violation(
                                path.as_posix(),
                                node.lineno,
                                node.col_offset,
                                rule_name,
                                f"from {display_module} import ... (forbidden: {match})",
                            )
                        )

    return violations


def check_attr_call_rules(path: Path, tree: ast.AST, rules: dict) -> list[Violation]:
    """Check attribute call rules (e.g., disallow get_issue_labels in control)."""
    violations: list[Violation] = []
    attr_rules = rules.get("deny_attr_calls", []) or []

    for rule in attr_rules:
        deny_in = rule.get("deny_in", []) or []
        deny_attr_names = set(rule.get("deny_attr_names", []) or [])
        allow = rule.get("allow", []) or []
        rule_name = rule.get("name", "deny-attr-call")

        p = path.as_posix()
        in_denied_path = any(p.startswith(prefix.rstrip("/")) for prefix in deny_in)
        is_allowed_file = any(
            p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in allow
        )

        if not in_denied_path or is_allowed_file:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in deny_attr_names:
                    violations.append(
                        Violation(
                            path.as_posix(),
                            node.lineno,
                            node.col_offset,
                            rule_name,
                            f"{node.func.attr}(...)",
                        )
                    )

    return violations


def check_symbol_ref_rules(path: Path, tree: ast.AST, rules: dict) -> list[Violation]:
    """Check symbol reference rules (e.g., no GitHub symbols in core layers)."""
    violations: list[Violation] = []
    symbol_rules = rules.get("deny_symbol_refs", []) or []

    for rule in symbol_rules:
        deny_in = rule.get("deny_in", []) or []
        deny_symbols = set(rule.get("deny_symbols", []) or [])
        allow = rule.get("allow", []) or []
        rule_name = rule.get("name", "deny-symbol-ref")

        p = path.as_posix()
        in_denied_path = any(p.startswith(prefix.rstrip("/")) for prefix in deny_in)
        is_allowed_file = any(
            p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in allow
        )

        if not in_denied_path or is_allowed_file:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in deny_symbols:
                violations.append(
                    Violation(
                        path.as_posix(),
                        node.lineno,
                        node.col_offset,
                        rule_name,
                        node.id,
                    )
                )
            if isinstance(node, ast.Attribute) and node.attr in deny_symbols:
                violations.append(
                    Violation(
                        path.as_posix(),
                        node.lineno,
                        node.col_offset,
                        rule_name,
                        node.attr,
                    )
                )

    return violations


_REVIEW_EXCHANGE_TYPED_SUMMARY_PATHS = frozenset(
    {
        "src/issue_orchestrator/domain/review_artifacts.py",
        "src/issue_orchestrator/domain/review_exchange.py",
        "src/issue_orchestrator/ports/session_output.py",
        "src/issue_orchestrator/control/completion_review_exchange.py",
        "src/issue_orchestrator/control/review_exchange_cache_resolution.py",
        "src/issue_orchestrator/execution/persistent_session_exchange.py",
        "src/issue_orchestrator/execution/review_exchange_session_output.py",
        "src/issue_orchestrator/execution/session_output_adapter.py",
        "src/issue_orchestrator/execution/persistent_review_exchange_runner.py",
    }
)


def _is_review_exchange_typed_summary_path(path: Path) -> bool:
    rel = path.as_posix()
    return rel in _REVIEW_EXCHANGE_TYPED_SUMMARY_PATHS


def _is_review_exchange_control_or_execution_path(path: Path) -> bool:
    rel = path.as_posix()
    if _is_review_exchange_typed_summary_path(path):
        return True
    if not (
        rel.startswith("src/issue_orchestrator/control/")
        or rel.startswith("src/issue_orchestrator/execution/")
    ):
        return False
    name = path.name
    return "review_exchange" in name or name in {
        "persistent_session_exchange.py",
        "persistent_pair_contract.py",
    }


def _is_summary_expr(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "summary"
    if isinstance(node, ast.Attribute):
        return node.attr == "summary" or _is_summary_expr(node.value)
    return False


def _annotation_mentions_dict(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == "dict" or node.id == "Dict"
    if isinstance(node, ast.Subscript):
        return _annotation_mentions_dict(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr in {"dict", "Dict"}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_mentions_dict(node.left) or _annotation_mentions_dict(
            node.right
        )
    return False


def _arg_is_summary_dict(arg: ast.arg) -> bool:
    return arg.arg == "summary" and _annotation_mentions_dict(arg.annotation)


def _constructor_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _keyword_value_for(node: ast.Call, key: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == key:
            return keyword.value
    return None


def _base_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _base_name(node.value)
    return None


def _is_pair_run_binding_mutation(target: ast.Attribute) -> bool:
    if target.attr == "exchange_run_id":
        return True
    if target.attr != "run_dir":
        return False
    base = _base_name(target.value)
    return base is not None and "pair" in base


def _violation(
    path: Path,
    node: ast.AST,
    kind: str,
    detail: str,
) -> Violation:
    return Violation(
        path.as_posix(),
        getattr(node, "lineno", 1),
        getattr(node, "col_offset", 0),
        kind,
        detail,
    )


def _summary_get_violation(path: Path, node: ast.AST) -> Violation | None:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and _is_summary_expr(node.func.value)
    ):
        return None
    return _violation(
        path,
        node,
        "review-exchange-summary-typed-contract",
        "use ReviewExchangeSummaryV1 typed fields instead of summary.get(...)",
    )


def _summary_constructor_violation(path: Path, node: ast.Call) -> Violation | None:
    constructor_name = _constructor_name(node.func)
    if constructor_name not in {"ReviewExchangeOutcome", "ReviewExchangeSummary"}:
        return None
    summary_value = _keyword_value_for(node, "summary")
    if not isinstance(summary_value, ast.Dict):
        return None
    return _violation(
        path,
        node,
        "review-exchange-summary-typed-contract",
        f"{constructor_name}(summary={{...}}) must use ReviewExchangeSummaryV1",
    )


def _summary_dict_coercion_violation(path: Path, node: ast.Call) -> Violation | None:
    if not (
        node.args
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and _is_summary_expr(node.args[0])
    ):
        return None
    return _violation(
        path,
        node,
        "review-exchange-summary-typed-contract",
        "do not coerce review-exchange summary to dict; use typed fields/to_payload()",
    )


def _summary_function_arg_violations(
    path: Path,
    node: ast.FunctionDef,
) -> list[Violation]:
    violations: list[Violation] = []
    for arg in [*node.args.args, *node.args.kwonlyargs]:
        if _arg_is_summary_dict(arg):
            violations.append(
                _violation(
                    path,
                    arg,
                    "review-exchange-summary-typed-contract",
                    "review-exchange summary parameters must use "
                    "ReviewExchangeSummaryV1, not dict",
                )
            )
    return violations


def _summary_annassign_violation(path: Path, node: ast.AST) -> Violation | None:
    if not (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "summary"
        and _annotation_mentions_dict(node.annotation)
    ):
        return None
    return _violation(
        path,
        node,
        "review-exchange-summary-typed-contract",
        "review-exchange summary variables must use ReviewExchangeSummaryV1, not dict",
    )


def _review_exchange_summary_contract_violations(
    path: Path,
    tree: ast.AST,
) -> list[Violation]:
    violations: list[Violation] = []
    if not _is_review_exchange_typed_summary_path(path):
        return violations
    for node in ast.walk(tree):
        if violation := _summary_get_violation(path, node):
            violations.append(violation)
        if isinstance(node, ast.Call):
            if violation := _summary_constructor_violation(path, node):
                violations.append(violation)
            if violation := _summary_dict_coercion_violation(path, node):
                violations.append(violation)
        if isinstance(node, ast.FunctionDef):
            violations.extend(_summary_function_arg_violations(path, node))
        if violation := _summary_annassign_violation(path, node):
            violations.append(violation)
    return violations


def _assignment_targets(node: ast.AST) -> list[ast.AST]:
    if isinstance(node, ast.Assign):
        return list(node.targets)
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    if isinstance(node, ast.AugAssign):
        return [node.target]
    return []


def _pair_run_rebind_violations(path: Path, tree: ast.AST) -> list[Violation]:
    violations: list[Violation] = []
    if not path.as_posix().startswith("src/issue_orchestrator/"):
        return violations
    for node in ast.walk(tree):
        for target in _assignment_targets(node):
            if isinstance(target, ast.Attribute) and _is_pair_run_binding_mutation(
                target
            ):
                violations.append(
                    _violation(
                        path,
                        target,
                        "review-exchange-pair-run-rebind",
                        "release/respawn persistent pairs instead of rebinding "
                        "run_dir/exchange_run_id",
                    )
                )
    return violations


def check_review_exchange_typed_flow_rules(
    path: Path,
    tree: ast.AST,
) -> list[Violation]:
    """Check review-exchange typed-dataflow ownership guardrails."""
    violations: list[Violation] = []
    violations.extend(_review_exchange_summary_contract_violations(path, tree))
    violations.extend(_pair_run_rebind_violations(path, tree))
    return violations


def _check_import_denies(
    path: Path,
    tree: ast.AST,
    *,
    allow_general: bool,
    deny_imports: set[str],
) -> list[Violation]:
    violations: list[Violation] = []
    if allow_general:
        return violations
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if base_mod(alias.name) in deny_imports:
                    violations.append(
                        _violation(path, node, "import", f"import {alias.name}")
                    )
        if (
            isinstance(node, ast.ImportFrom)
            and node.module
            and base_mod(node.module) in deny_imports
        ):
            violations.append(
                _violation(path, node, "import", f"from {node.module} import ...")
            )
    return violations


def _constant_string_arg(node: ast.Call) -> str | None:
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _dynamic_import_name_call_violation(
    path: Path,
    node: ast.Call,
    *,
    allow_general: bool,
    deny_dynamic_imports: set[str],
    deny_imports: set[str],
    deny_dynamic_any: set[str],
) -> Violation | None:
    if not (
        isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
        and not allow_general
    ):
        return None
    import_name = _constant_string_arg(node)
    if import_name is not None:
        mod = base_mod(import_name)
        if mod in deny_dynamic_imports or mod in deny_imports:
            return _violation(
                path,
                node,
                "dynamic-import",
                f'__import__("{import_name}")',
            )
    elif "__import__" in deny_dynamic_any:
        return _violation(path, node, "dynamic-import", "__import__(...)")
    return None


def _importlib_dynamic_import_violation(
    path: Path,
    node: ast.Call,
    *,
    allow_general: bool,
    deny_dynamic_imports: set[str],
    deny_imports: set[str],
    deny_dynamic_any: set[str],
) -> Violation | None:
    if get_attr_call(node.func) != ("importlib", "import_module") or allow_general:
        return None
    import_name = _constant_string_arg(node)
    if import_name is not None:
        mod = base_mod(import_name)
        if mod in deny_dynamic_imports or mod in deny_imports:
            return _violation(
                path,
                node,
                "dynamic-import",
                f'importlib.import_module("{import_name}")',
            )
    elif "importlib.import_module" in deny_dynamic_any:
        return _violation(
            path,
            node,
            "dynamic-import",
            "importlib.import_module(...)",
        )
    return None


def _check_dynamic_import_denies(
    path: Path,
    tree: ast.AST,
    *,
    allow_general: bool,
    deny_dynamic_imports: set[str],
    deny_imports: set[str],
    deny_dynamic_any: set[str],
) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for check in (
            _dynamic_import_name_call_violation,
            _importlib_dynamic_import_violation,
        ):
            violation = check(
                path,
                node,
                allow_general=allow_general,
                deny_dynamic_imports=deny_dynamic_imports,
                deny_imports=deny_imports,
                deny_dynamic_any=deny_dynamic_any,
            )
            if violation is not None:
                violations.append(violation)
    return violations


def _check_denied_call_rules(
    path: Path,
    tree: ast.AST,
    *,
    allow_general: bool,
    deny_calls: set[tuple[str, str]],
    deny_os_calls: bool,
    deny_git_subprocess: bool,
    allow_git_subprocess: bool,
) -> list[Violation]:
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        module, attr = get_attr_call(node.func)
        if not allow_general and module and attr and (module, attr) in deny_calls:
            violations.append(_violation(path, node, "call", f"{module}.{attr}(...)"))
        if (
            not allow_general
            and deny_os_calls
            and module == "os"
            and attr in {"system", "popen"}
        ):
            violations.append(_violation(path, node, "call", f"os.{attr}(...)"))
        if (
            deny_git_subprocess
            and not allow_git_subprocess
            and _is_git_subprocess_call(node)
        ):
            violations.append(
                _violation(
                    path,
                    node,
                    "git-subprocess",
                    "subprocess.*(['git', ...])",
                )
            )
    return violations



# --- shared needs-human block boundary (#6999 F2 round 3) -------------------
#
# The shared ``needs-human`` label has several independent causes, and a
# remover that cannot see all of them takes a block away from a lifecycle that
# still needs it. ``control/needs_human_block.py`` is the one owner that
# applies, releases and force-clears that label; everything else consumes its
# typed commands. These checks stop a future call site quietly reaching around
# it, which is exactly how the four bypasses this rule was written for got in.

_BLOCK_OWNER = "src/issue_orchestrator/control/needs_human_block.py"
_BLOCK_LABEL_WRITES = {"add_label", "remove_label"}
_BLOCK_LABEL_ACTIONS = {"AddLabelAction", "RemoveLabelAction"}
_BLOCK_FALLBACK_MARKER = "# shared-block: ungoverned fallback"


def _mentions_shared_block_label(node: ast.AST | None) -> bool:
    """Whether an expression names the governed label (not the tech-lead marker)."""
    if node is None:
        return False
    for sub in ast.walk(node):
        name = None
        if isinstance(sub, ast.Attribute):
            name = sub.attr
        elif isinstance(sub, ast.Name):
            name = sub.id
        if name is None:
            continue
        if "tech_lead_needs_human" in name:
            continue
        if name in {"needs_human", "needs_human_label"}:
            return True
    return False


def check_shared_needs_human_block(
    path: Path, tree: ast.AST, source: str
) -> list[Violation]:
    if path.as_posix().endswith(_BLOCK_OWNER):
        return []

    # One audited escape: a composition with no owner wired must still perform
    # the write, or the boundary turns a real mutation into a silent no-op.
    # Spelled out at the call site so it is greppable and has to be justified.
    lines = source.splitlines()
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        if isinstance(node.func, ast.Attribute) and node.func.attr in _BLOCK_LABEL_WRITES:
            label_arg = node.args[1] if len(node.args) > 1 else None
            marked = _BLOCK_FALLBACK_MARKER in lines[node.lineno - 1]
            if _mentions_shared_block_label(label_arg) and not marked:
                violations.append(
                    Violation(
                        path.as_posix(),
                        node.lineno,
                        node.col_offset,
                        "shared-needs-human-block-bypass",
                        f"{node.func.attr}(...) writes the shared needs-human "
                        "label directly; route it through NeedsHumanBlock "
                        "acquire/release/force_clear",
                    )
                )
            continue

        constructor = _constructor_name(node.func)
        if constructor not in _BLOCK_LABEL_ACTIONS:
            continue
        label = _keyword_value_for(node, "label")
        if not _mentions_shared_block_label(label):
            continue
        if _keyword_value_for(node, "needs_human_cause") is None:
            violations.append(
                Violation(
                    path.as_posix(),
                    node.lineno,
                    node.col_offset,
                    "shared-needs-human-block-uncaused",
                    f"{constructor} targets the shared needs-human label with "
                    "no needs_human_cause; name the lifecycle that requires it",
                )
            )
    return violations


# --- one owner for provider-output classification (#6999 F2 round 6) --------
#
# "Is this provider output an auth failure" must have exactly one answer. The
# startup AI gate had grown a second marker table, and the two had already
# drifted: each recognised banners the other missed, so the same expired login
# was a startup failure on one path and a 90-minute timeout on the other.
#
# The first check pins the vocabulary (a copy of the owner's banner phrases
# somewhere else, read out of the owner's own table so the rule can never be a
# stale duplicate of it) and the second pins the shape, so a second classifier
# cannot evade the guardrail merely by choosing different words.
#
# The shape rule keys on *normalization*: a membership test against text that
# has been lowered or casefolded. That is what reading a banner looks like, and
# it is the one structural signal available to an AST check - a matcher that
# skips normalization is matching case-sensitively, which no banner reader
# survives. Left operand is deliberately unconstrained: a bare literal, a token
# table, an attribute constant and a parameter all read the same way here.
#
# The exemption below is keyed by FUNCTION, not by module. A module-level
# exemption for infra/hooks/_ai_gate.py would let its deleted auth table return
# beside its legitimate hook-block classifier - the precise regression this
# guardrail exists to prevent. The vocabulary rule is not exemptible at all, so
# an exempted function still cannot host the owner's banner phrases.

_CLASSIFICATION_OWNER = "execution/agent_runner_errors.py"
_PROVIDER_ADAPTER_SURFACE = "execution/agent_runner_providers/"
_AUTH_TABLE_NAME = "_AUTH_TOKENS"
_TEXT_NORMALIZERS = {"lower", "casefold"}
_MODULE_SCOPE = "<module>"

# Functions that classify text which is NOT provider CLI output. Every entry is
# a claim, made in review, about where the text comes from - that is the point
# of naming them one function at a time.
_NON_PROVIDER_TEXT_CLASSIFIERS = frozenset(
    {
        # --- git and GitHub responses, not a provider CLI ---
        "adapters/github/github_adapter.py::GitHubAdapter.create_issue._check",
        "adapters/github/http_client.py::classify_github_http_failure",
        "adapters/worktree/_worktree.py::_delete_remote_branch",
        "adapters/worktree/worktree_policy.py::ValidateOrDeletePolicy._check_broken_git_state",
        "control/completion_pr_collision.py::is_pr_collision_error",
        "control/completion_pr_collision.py::_is_raw_no_commits_error",
        "control/completion_processor.py::CompletionProcessor._is_non_fast_forward",
        "control/issue_fetch_resilience.py::_looks_like_rate_limit",
        "execution/git_push_operations.py::determine_retryable",
        "execution/git_push_operations.py::get_preflight_fix_hint",
        "execution/git_working_copy.py::GitWorkingCopy.push_preflight",
        "execution/verification_service.py::DefaultVerificationService.classify_error",
        # --- local operating-system command output, not a provider CLI ---
        "adapters/ps_process_group_observer.py::PsProcessGroupObserver._snapshot",
        # --- this orchestrator's own artifacts: labels, reasons, summaries ---
        "control/lexical_masking.py::LiteralMasker._literal_at",
        "control/planner.py::Planner._plan_issues",
        "control/publish_retry_finalize.py::_is_publish_failure_history",
        "control/session_completion.py::_is_session_already_gone_error",
        "control/stuck_sweep.py::_reconciler_owns",
        "control/stuck_sweep.py::_scan_stuck_issues",
        "execution/repository_setup_artifacts.py::plan_missing_setup_prompts",
        "view_models/issue_detail.py::_project_review_terminal_story_event",
        "view_models/journey_projection.py::_coerce_non_review_latest_outcome",
        "view_models/journey_projection.py::_round_completed_outcome_label",
        # --- local tooling, configuration and filenames ---
        "adapters/hooks/codex.py::CodexAdapter._execpolicy_allows",
        "entrypoints/cli.py::cmd_default",
        "entrypoints/cli_tools/setup_wizard.py::_prompt_manual_existing_agent",
        "entrypoints/cli_tools/setup_wizard.py::wizard_existing_project",
        "infra/ai_systems_config.py::AISystemsConfig.detect_from_tags",
        "infra/doctor/checks/guardrails.py::_check_completion_commands_available",
        "infra/doctor/checks/guardrails.py::_check_git_push_bypass",
        "infra/e2e_reports.py::_artifact_record_for_path",
        # The AI gate's hook-block classifier answers "did the hook stop the
        # command", never "are these credentials dead". Named ALONE, so the auth
        # table this guardrail removed from the same module cannot come back
        # beside it.
        "infra/hooks/_ai_gate.py::_detect_blocked_from_output",
        # --- session transcripts, read after the fact ---
        #
        # These four do read agent output, so the exemption is narrower than it
        # looks: they answer "did this session finish", "which AI wrote this
        # log", "what should a human read first" - never a credential question,
        # and never on the launch or live-session path. The vocabulary rule is
        # not exemptible, so none of them can start listing banner phrases.
        "adapters/session_log/registry.py::DataDrivenLogProvider._parse_markdown_log",
        "adapters/session_log/registry.py::DataDrivenLogProvider._extract_errors_from_entries",
        "adapters/session_log/registry.py::DataDrivenLogProvider._extract_permission_issues",
        "adapters/session_log/registry.py::DataDrivenLogProvider._check_completion_marker",
        "infra/session_failure_diagnosis.py::_build_warnings_and_suggestions",
        "ports/session_log.py::detect_ai_system_from_output",
    }
)


def _provider_surface_relpath(path: Path) -> str | None:
    """Path relative to the package root, or ``None`` if outside the package."""
    parts = path.as_posix().split("/")
    if "issue_orchestrator" not in parts:
        return None
    return "/".join(parts[parts.index("issue_orchestrator") + 1 :])


def _string_elements(node: ast.AST | None) -> list[tuple[ast.Constant, str]]:
    """The string-literal members of a collection literal, with their text."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return []
    return [
        (e, e.value)
        for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]


def _auth_banner_phrases(package_root: Path) -> tuple[str, ...]:
    """Read the owner's distinctive auth banner phrases out of its own table.

    Only multi-word phrases count. Single tokens like ``forbidden`` or ``401``
    are ordinary English and HTTP vocabulary that other modules use for their
    own unrelated reasons; a banner phrase is what a copy looks like.
    """
    owner = package_root / _CLASSIFICATION_OWNER
    if not owner.exists():
        return ()
    try:
        tree = ast.parse(owner.read_text(encoding="utf-8"), filename=owner.as_posix())
    except (OSError, SyntaxError):
        return ()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if _AUTH_TABLE_NAME not in names:
            continue
        return tuple(
            sorted(
                {
                    text.lower()
                    for _, text in _string_elements(node.value)
                    if " " in text or "/" in text
                }
            )
        )
    return ()


def _matching_string_literals(tree: ast.AST) -> list[tuple[ast.Constant, str]]:
    """String literals used *as matchers*: ``in`` operands and token tables."""
    literals: list[tuple[ast.Constant, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                if any(isinstance(op, ast.In) for op in node.ops):
                    literals.append((left, left.value))
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            elements = _string_elements(node)
            if len(elements) >= 2:
                literals.extend(elements)
    return literals


def _is_normalizing_call(node: ast.AST) -> bool:
    """Whether an expression is ``<text>.lower()`` / ``<text>.casefold()``."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _TEXT_NORMALIZERS
    )


def _normalized_aliases(body: Iterable[ast.stmt]) -> set[str]:
    """Local names bound to normalized text inside one scope.

    ``lowered = output.lower()`` then ``token in lowered`` reads exactly like
    the direct form, and the AI gate's own hook-block classifier is written
    that way, so an alias must not be an escape hatch.
    """
    aliases: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if isinstance(node, ast.Assign) and _is_normalizing_call(node.value):
                aliases.update(
                    t.id for t in node.targets if isinstance(t, ast.Name)
                )
            elif (
                isinstance(node, ast.AnnAssign)
                and node.value is not None
                and _is_normalizing_call(node.value)
                and isinstance(node.target, ast.Name)
            ):
                aliases.add(node.target.id)
            elif (
                isinstance(node, ast.NamedExpr)
                and _is_normalizing_call(node.value)
                and isinstance(node.target, ast.Name)
            ):
                aliases.add(node.target.id)
    return aliases


def _normalized_membership_tests(
    tree: ast.Module,
) -> list[tuple[ast.Compare, str]]:
    """Every ``x in <normalized text>`` in the module, with its owning scope.

    The scope is the dotted path of enclosing classes and functions, so the
    exemption list can name one function rather than a whole module.
    """
    found: list[tuple[ast.Compare, str]] = []

    def visit(body: list[ast.stmt], scope: str, inherited: set[str]) -> None:
        aliases = inherited | _normalized_aliases(body)
        nested: list[tuple[list[ast.stmt], str]] = []
        for statement in body:
            for node in ast.walk(statement):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    child = node.name if scope == _MODULE_SCOPE else f"{scope}.{node.name}"
                    nested.append((node.body, child))
                    continue
                if not isinstance(node, ast.Compare):
                    continue
                if not any(isinstance(op, ast.In) for op in node.ops):
                    continue
                normalized = any(
                    _is_normalizing_call(c)
                    or (isinstance(c, ast.Name) and c.id in aliases)
                    for c in node.comparators
                )
                if normalized:
                    found.append((node, scope))
        for child_body, child_scope in nested:
            visit(child_body, child_scope, aliases)

    visit(tree.body, _MODULE_SCOPE, set())
    # A nested scope's statements are also reachable from ast.walk above, so
    # the same comparison can be recorded twice; the innermost scope wins.
    innermost: dict[tuple[int, int], tuple[ast.Compare, str]] = {}
    for node, scope in found:
        key = (node.lineno, node.col_offset)
        previous = innermost.get(key)
        if previous is None or len(scope) > len(previous[1]):
            innermost[key] = (node, scope)
    return [innermost[key] for key in sorted(innermost)]


def check_provider_output_classification(path: Path, tree: ast.AST) -> list[Violation]:
    relpath = _provider_surface_relpath(path)
    if relpath is None or not isinstance(tree, ast.Module):
        return []
    if relpath == _CLASSIFICATION_OWNER or relpath.startswith(
        _PROVIDER_ADAPTER_SURFACE
    ):
        return []

    package_root = Path(path.as_posix()[: -len(relpath) - 1])
    violations: list[Violation] = []

    for phrase in _auth_banner_phrases(package_root):
        for literal, text in _matching_string_literals(tree):
            if phrase not in text.lower():
                continue
            violations.append(
                Violation(
                    path.as_posix(),
                    literal.lineno,
                    literal.col_offset,
                    "provider-banner-vocabulary",
                    f"{text!r} matches provider banner text owned by "
                    f"{_CLASSIFICATION_OWNER}; ask the provider adapter for a "
                    "typed ProviderErrorType instead of re-listing its banners",
                )
            )

    for node, scope in _normalized_membership_tests(tree):
        if f"{relpath}::{scope}" in _NON_PROVIDER_TEXT_CLASSIFIERS:
            continue
        violations.append(
            Violation(
                path.as_posix(),
                node.lineno,
                node.col_offset,
                "provider-output-classifier",
                f"{scope} matches text against normalized output; provider "
                f"output has one classifier ({_CLASSIFICATION_OWNER}) and "
                "consumers ask a provider adapter for a typed "
                "ProviderErrorType. If this text is not provider output, name "
                "this function in _NON_PROVIDER_TEXT_CLASSIFIERS",
            )
        )
    return violations


def check_file(
    path: Path, rules: dict, allow_prefixes: Sequence[str]
) -> list[Violation]:
    allow_general = is_allowed(path, allow_prefixes)

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
    except SyntaxError as e:
        return [
            Violation(path.as_posix(), e.lineno or 1, e.offset or 0, "syntax", e.msg)
        ]

    violations: list[Violation] = []

    # Check layer boundary rules first
    violations.extend(check_layer_boundaries(path, tree, rules))
    violations.extend(check_attr_call_rules(path, tree, rules))
    violations.extend(check_symbol_ref_rules(path, tree, rules))
    violations.extend(check_review_exchange_typed_flow_rules(path, tree))
    violations.extend(
        check_shared_needs_human_block(path, tree, path.read_text(encoding="utf-8"))
    )
    violations.extend(check_provider_output_classification(path, tree))

    deny_imports = set(rules.get("deny_imports", []) or [])
    deny_dynamic_imports = set(rules.get("deny_dynamic_imports", []) or [])
    deny_calls = set(
        tuple(x.split(".", 1)) for x in (rules.get("deny_calls", []) or [])
    )
    deny_os_calls = bool(rules.get("deny_os_system_like", True))
    deny_dynamic_any = set(rules.get("deny_dynamic_any", []) or [])
    deny_git_subprocess = bool(rules.get("deny_git_subprocess", False))
    allow_git_prefixes = rules.get("allow_git_subprocess_prefixes", []) or []
    allow_git_subprocess = is_allowed(path, allow_git_prefixes)

    violations.extend(
        _check_import_denies(
            path,
            tree,
            allow_general=allow_general,
            deny_imports=deny_imports,
        )
    )
    violations.extend(
        _check_dynamic_import_denies(
            path,
            tree,
            allow_general=allow_general,
            deny_dynamic_imports=deny_dynamic_imports,
            deny_imports=deny_imports,
            deny_dynamic_any=deny_dynamic_any,
        )
    )
    violations.extend(
        _check_denied_call_rules(
            path,
            tree,
            allow_general=allow_general,
            deny_calls=deny_calls,
            deny_os_calls=deny_os_calls,
            deny_git_subprocess=deny_git_subprocess,
            allow_git_subprocess=allow_git_subprocess,
        )
    )

    return violations


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "root", nargs="?", default="src", help="Root to scan (default: src)"
    )
    ap.add_argument(
        "--rules", default="tools/ast_guardrails.yml", help="Rules file path"
    )
    ap.add_argument(
        "--allow-prefix",
        action="append",
        default=[],
        help="Allowed path prefix (repeatable)",
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"Root not found: {root}", file=sys.stderr)
        return 1

    rules = load_rules(Path(args.rules))
    allow_prefixes = args.allow_prefix or (rules.get("allow_prefixes", []) or [])
    if not allow_prefixes:
        allow_prefixes = [
            "src/issue_orchestrator/execution",
            "src/issue_orchestrator/adapters",
            "src/issue_orchestrator/entrypoints/e2e_worker.py",  # E2E worker subprocess
            "src/issue_orchestrator/infra/e2e_runner.py",  # E2E runner spawns workers
        ]

    all_v: list[Violation] = []
    for p in iter_py_files(root):
        all_v.extend(check_file(p, rules, allow_prefixes))

    if all_v:
        print("Architecture guardrails violations:\n", file=sys.stderr)
        for v in sorted(all_v, key=lambda x: (x.path, x.lineno, x.col, x.kind)):
            print(v.fmt(), file=sys.stderr)
        print(
            "\nFix: move side effects into allowed adapters/runners, or explicitly allow a folder via allow_prefixes.",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
