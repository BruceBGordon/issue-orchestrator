#!/usr/bin/env python3
# Managed by issue-orchestrator setup-guardrails: block-no-verify helper

"""Shared policy for restricted Git/GitHub commands and hidden-dirt bypasses."""

from __future__ import annotations

import codecs
from dataclasses import dataclass
import json
import re
import shlex
import sys
from pathlib import Path


@dataclass(frozen=True)
class HookDecision:
    allowed: bool
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allowed else 2


def extract_command_from_input(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""

    cmd = ""

    tool_args = data.get("toolArgs")
    if isinstance(tool_args, str):
        try:
            args_data = json.loads(tool_args)
            if isinstance(args_data, dict):
                cmd = args_data.get("command", "")
        except (json.JSONDecodeError, TypeError):
            cmd = ""

    if not cmd:
        tool_input = data.get("tool_input")
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command", "")

    if not cmd:
        cmd = data.get("command", "")

    return cmd if isinstance(cmd, str) else ""


_SHELL_OPERATOR_CHARS = frozenset(";&|\r\n")


def _parse_shell_segments(command: str) -> list[list[str]] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\r\n")
        lexer.whitespace = " \t"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token and all(char in _SHELL_OPERATOR_CHARS for char in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def is_dry_run_no_verify_push(command: str) -> bool:
    segments = _parse_shell_segments(command)
    if not segments or len(segments) != 1 or segments[0][:2] != ["git", "push"]:
        return False
    argv = segments[0]
    return "--dry-run" in argv and "--no-verify" in argv


_NO_VERIFY_REASON = "BLOCKED: --no-verify is forbidden. Pre-push hooks must run."
_SHORT_NO_VERIFY_REASON = "BLOCKED: -n (--no-verify) is forbidden."
_GIT_CONFIG_OVERRIDE_REASON = "BLOCKED: Per-command Git config overrides (-c, --config-env, and GIT_CONFIG_*) are forbidden."
_GIT_CONFIG_MUTATION_REASON = "BLOCKED: Mutating Git config from an agent session is forbidden. Read-only `git config get`, `--get`, and `list` commands are allowed."
_GH_PR_MERGE_REASON = "BLOCKED: Agents cannot merge PRs. Only humans can merge."
_GH_API_REASON = "BLOCKED: gh api calls are forbidden. Use gh pr/issue commands instead."  # fmt: skip
_GH_ALIAS_REASON = "BLOCKED: mutating gh aliases can hide restricted GitHub actions."


def _is_git_token(token: str) -> bool:
    return Path(token.strip("`();")).name == "git"


_GH_SHORT_OPTIONS_WITH_VALUE = frozenset("-A -b -F -f -H -p -q -R -t -X".split())
_GH_LONG_OPTIONS_WITH_VALUE = frozenset("--author-email --body --body-file --cache --field --header --hostname --input --jq --match-head-commit --method --preview --raw-field --repo --subject --template".split())  # fmt: skip


def _gh_command_words(args: list[str]) -> list[str]:
    words: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in _GH_SHORT_OPTIONS_WITH_VALUE | _GH_LONG_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if any(
            token.startswith(f"{option}=") for option in _GH_LONG_OPTIONS_WITH_VALUE
        ):
            index += 1
            continue
        if any(
            token.startswith(option) and len(token) > len(option)
            for option in _GH_SHORT_OPTIONS_WITH_VALUE
        ):
            index += 1
            continue
        if token == "--":
            words.extend(value.casefold() for value in args[index + 1 :])
            break
        if token.startswith("-"):
            index += 1
            continue
        words.append(token.casefold())
        index += 1
    return words


def _evaluate_gh_args(args: list[str]) -> HookDecision | None:
    words = _gh_command_words(args)
    if words[:2] == ["pr", "merge"]:
        return HookDecision(False, _GH_PR_MERGE_REASON)
    if words[:1] == ["api"]:
        return HookDecision(False, _GH_API_REASON)
    if words[:1] == ["alias"] and words[1:2] != ["list"]:
        return HookDecision(False, _GH_ALIAS_REASON)
    return None


def _uses_git_config_override(global_args: list[str]) -> bool:
    for token in global_args:
        if token == "-c":
            return True
        if token.startswith("-c") and len(token) > 2:
            return True
        if token == "--config-env" or token.startswith("--config-env="):
            return True
    return False


_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    "-C -c --config-env --git-dir --namespace --super-prefix --work-tree".split()
)


def _split_git_args(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    global_args: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            if index + 1 >= len(args):
                return global_args, None, []
            return global_args, args[index + 1].casefold(), args[index + 2 :]
        if token in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            global_args.append(token)
            if index + 1 < len(args):
                global_args.append(args[index + 1])
            index += 2
            continue
        if (
            (token.startswith("-c") and len(token) > 2)
            or (token.startswith("-C") and len(token) > 2)
            or token.startswith("--config-env=")
            or token.startswith("--git-dir=")
            or token.startswith("--namespace=")
            or token.startswith("--super-prefix=")
            or token.startswith("--work-tree=")
        ):
            global_args.append(token)
            index += 1
            continue
        if token.startswith("-"):
            global_args.append(token)
            index += 1
            continue
        return global_args, token.casefold(), args[index + 1 :]
    return global_args, None, []


def _is_short_no_verify_option(token: str) -> bool:
    if not token.startswith("-") or token.startswith("--"):
        return False
    for option in token[1:]:
        if option == "n":
            return True
        if option in frozenset("mFcCSu"):
            return False
    return False


_CONFIG_READ_ACTIONS = frozenset({"get", "list"})
_CONFIG_MUTATION_ACTIONS = frozenset(
    {"edit", "remove-section", "rename-section", "set", "unset"}
)
_CONFIG_READ_FLAGS = frozenset(
    "-l --get --get-all --get-color --get-colorbool --get-regexp "
    "--get-urlmatch --list".split()
)
_CONFIG_MUTATION_FLAGS = frozenset(
    "--add --edit -e --remove-section --rename-section --replace-all "
    "--unset --unset-all".split()
)
_CONFIG_OPTIONS_WITH_VALUE = frozenset({"--blob", "--file", "-f"})
_CONFIG_READ_ONLY_OPTIONS = frozenset(
    "--all --bool --bool-or-int --bool-or-str --expiry-date --fixed-value "
    "--global --includes --int --local --name-only --no-includes --null "
    "--path --show-names --show-origin --system --worktree -z".split()
)


def _abbreviates_mutation_option(token: str) -> bool:
    return token.startswith("--") and any(
        candidate.startswith(token) for candidate in _CONFIG_MUTATION_FLAGS
    )


def _config_is_read_only(args: list[str]) -> bool:
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in _CONFIG_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token in _CONFIG_MUTATION_FLAGS or _abbreviates_mutation_option(token):
            return False
        if token in _CONFIG_READ_FLAGS:
            return not positional
        if token in _CONFIG_READ_ONLY_OPTIONS or token.startswith(
            ("--default=", "--type=", "--url=", "--value=")
        ):
            index += 1
            continue
        if token.startswith("-"):
            return False
        normalized = token.casefold()
        if not positional and normalized in _CONFIG_READ_ACTIONS:
            return True
        if not positional and normalized in _CONFIG_MUTATION_ACTIONS:
            return False
        positional.append(token)
        index += 1
    return len(positional) <= 1


def _evaluate_git_args(
    args: list[str], *, allow_dry_run_no_verify: bool
) -> HookDecision | None:
    args = [token.strip("`") for token in args]
    global_args, name, subcommand_args = _split_git_args(args)
    if _uses_git_config_override(global_args):
        return HookDecision(False, _GIT_CONFIG_OVERRIDE_REASON)

    if name is None:
        if any(len(token) >= 9 and "--no-verify".startswith(token) for token in args):
            return HookDecision(False, _NO_VERIFY_REASON)
        return None

    if name in {"commit", "push"} and any(
        len(token) >= 9 and "--no-verify".startswith(token) for token in subcommand_args
    ):
        if (
            name == "push"
            and "--dry-run" in subcommand_args
            and allow_dry_run_no_verify
        ):
            return None
        return HookDecision(False, _NO_VERIFY_REASON)

    if name == "commit" and any(
        _is_short_no_verify_option(token) for token in subcommand_args
    ):
        return HookDecision(False, _SHORT_NO_VERIFY_REASON)

    if name == "config":
        if not _config_is_read_only(subcommand_args):
            return HookDecision(False, _GIT_CONFIG_MUTATION_REASON)

    return None


_SHELL_COMMANDS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_SHELL_PREFIXES = frozenset({"builtin", "command", "env", "exec", "nice", "nohup", "time"})  # fmt: skip
_SHELL_STRING_EXECUTORS = frozenset({"eval"})
_SHELL_CONTROL_PREFIXES = frozenset("! ( { coproc do elif else function if then until while".split())  # fmt: skip


def _split_shell_words(value: str) -> list[str] | None:
    try:
        return shlex.split(value)
    except ValueError:
        return None


def _parsed_env_split_option(
    argv: list[str], index: int
) -> tuple[list[str], int] | None:
    option = argv[index]
    if option in {"-S", "--split-string"}:
        if index + 1 >= len(argv):
            return None
        value = argv[index + 1]
        consumed = 2
    else:
        bundled = re.fullmatch(r"-[0iv]*S(.*)", option)
        if bundled is not None:
            value = bundled.group(1)
            if not value:
                if index + 1 >= len(argv):
                    return None
                value = argv[index + 1]
                consumed = 2
            else:
                consumed = 1
        elif option.startswith("--split-string="):
            value = option.removeprefix("--split-string=")
            consumed = 1
        else:
            return None
    # BSD env(1) uses ``\_`` as an argument separator inside ``-S``.
    if "${" in value:
        raise ValueError("dynamic env -S substitution")
    split = _split_shell_words(value.replace("\\_", " "))
    if split is None:
        raise ValueError("invalid env -S payload")
    return split, consumed


_WRAPPER_VALUE_OPTIONS = {
    "env": (r"-[0iv]*[aCPSu]", {"--argv0", "--chdir", "--unset"}),
    "exec": (r"-[cl]*a", set()),
    "nice": (r"-n", {"--adjustment"}),
    "time": (r"-[ahlpqv]*[fo]", {"--format", "--output"}),
}


def _wrapper_option_consumes_next(prefix: str, token: str) -> bool:
    short_pattern, long = _WRAPPER_VALUE_OPTIONS.get(prefix, (r"(?!)", set()))
    if token in long:
        return True
    return re.fullmatch(short_pattern, token) is not None


def _env_split_replacement(expanded: list[str]) -> list[str] | None:
    for index, token in enumerate(expanded):
        if Path(token).name != "env":
            continue
        if _command_executable_index(expanded[:index] + ["__io_target__"]) != index:
            continue
        option_index = index + 1
        while option_index < len(expanded):
            option = expanded[option_index]
            parsed = _parsed_env_split_option(expanded, option_index)
            if parsed is not None:
                split, consumed = parsed
                return (
                    expanded[:option_index]
                    + split
                    + expanded[option_index + consumed :]
                )
            if option == "--":
                break
            if _wrapper_option_consumes_next("env", option):
                option_index += 2
                continue
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", option):
                break
            if option.startswith("-"):
                option_index += 1
                continue
            break
    return None


def _expand_env_split_string(segment: list[str]) -> list[str]:
    expanded = list(segment)
    for _ in range(sum(token.count("S") for token in expanded) + 1):
        replacement = _env_split_replacement(expanded)
        if replacement is None:
            return expanded
        expanded = replacement
    raise ValueError("env -S nesting exceeded input complexity")


def _shell_executable_index(segment: list[str]) -> int:
    index = 0
    while index < len(segment) and Path(segment[index]).name in _SHELL_PREFIXES:
        prefix = Path(segment[index]).name
        index += 1
        if prefix == "builtin" and index < len(segment):
            if Path(segment[index]).name not in {"command", "eval", "exec"}:
                return len(segment)
        while index < len(segment):
            token = segment[index]
            if prefix == "command" and token in {"-v", "-V"}:
                return len(segment)
            if _wrapper_option_consumes_next(prefix, token):
                index += 2
                continue
            if token == "--" or token.startswith("-"):
                index += 1
                continue
            if prefix == "env" and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token):
                index += 1
                continue
            break
    return index


def _command_executable_index(segment: list[str]) -> int:
    index = 0
    while index < len(segment):
        while index < len(segment) and re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", segment[index]
        ):
            index += 1
        if index + 1 < len(segment) and segment[index].endswith("()"):
            index += 2
            continue
        if index >= len(segment) or segment[index] not in _SHELL_CONTROL_PREFIXES:
            break
        control = segment[index]
        index += 1
        if control == "function" and index < len(segment):
            index += 1
        elif control == "coproc" and index + 1 < len(segment):
            candidate = Path(segment[index]).name
            command_starters = _SHELL_PREFIXES | _SHELL_COMMANDS | {"eval", "gh", "git"}
            if candidate not in command_starters:
                index += 1
    return index + _shell_executable_index(segment[index:])


def _resolve_literal_command_variables(segments: list[list[str]]) -> None:
    bindings: dict[str, str] = {}
    assignment = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)")
    expansion = re.compile(r"\$(?:{([A-Za-z_][A-Za-z0-9_]*)}|([A-Za-z_][A-Za-z0-9_]*))")
    for segment in segments:
        candidate = segment[0] if len(segment) == 1 else ""
        match = assignment.fullmatch(candidate)
        if match:
            name, value = match.groups()
            basename = Path(value).name
            if basename in {"git", "gh"}:
                bindings[name] = value
            continue
        executable = _command_executable_index(segment)
        if executable >= len(segment):
            continue
        match = expansion.fullmatch(segment[executable])
        if match and (value := bindings.get(match.group(1) or match.group(2))):
            segment[executable] = value


def _backtick_end(command: str, start: int) -> int | None:
    index = start
    while index < len(command):
        if command[index] == "\\":
            index += 2
        elif command[index] == "`":
            return index
        else:
            index += 1
    return None


def _after_single_quote(command: str, start: int) -> int:
    end = command.find("'", start)
    return len(command) if end < 0 else end + 1


def _comment_end(command: str, start: int) -> int:
    newline = command.find("\n", start)
    return len(command) if newline < 0 else newline


def _mask_inner_substitutions(command: str) -> str:
    while True:
        masked, count = re.subn(r"\$\([^()]*\)", "__io_substitution__", command)
        if not count:
            return command
        command = masked


def _scan_substitutions(
    command: str, start: int = 0, *, stop_at_paren: bool = False
) -> tuple[list[str], int]:
    nested: list[str] = []
    double_quoted = False
    word_start = True
    index = start
    while index < len(command):
        char = command[index]
        if char == "\\":
            index += 2
            word_start = False
            continue
        if char == "'" and not double_quoted:
            index = _after_single_quote(command, index + 1)
            word_start = False
            continue
        elif char == '"':
            double_quoted = not double_quoted
        elif char == "#" and not double_quoted and word_start:
            index = _comment_end(command, index)
            word_start = True
            continue
        elif command.startswith("$(", index):
            inner, end = _scan_substitutions(command, index + 2, stop_at_paren=True)
            nested.append(_mask_inner_substitutions(command[index + 2 : end]))
            nested.extend(inner)
            index = end + 1
            word_start = False
            continue
        elif char == "`":
            end = _backtick_end(command, index + 1)
            end = len(command) if end is None else end
            nested.append(command[index + 1 : end].replace("\\`", "`"))
            index = end + 1
            word_start = False
            continue
        elif char == ")" and not double_quoted and stop_at_paren:
            return nested, index
        if not double_quoted:
            word_start = char.isspace() or char in ";&|()"
        index += 1
    return nested, len(command)


def _nested_shell_commands(segments: list[list[str]], command: str) -> list[str]:
    nested = _scan_substitutions(command)[0]
    for original_segment in segments:
        segment = original_segment
        shell_index = _command_executable_index(segment)
        if (
            shell_index < len(segment)
            and Path(segment[shell_index]).name in _SHELL_STRING_EXECUTORS
            and shell_index + 1 < len(segment)
        ):
            payload_index = shell_index + 1
            if segment[payload_index] == "--":
                payload_index += 1
            if payload_index < len(segment):
                nested.append(" ".join(segment[payload_index:]))
            continue
        if (
            shell_index >= len(segment)
            or Path(segment[shell_index]).name not in _SHELL_COMMANDS
        ):
            continue
        command_index = next(
            (
                index + 1
                for index, token in enumerate(
                    segment[shell_index + 1 :], shell_index + 1
                )
                if token.startswith("-")
                and not token.startswith("--")
                and "c" in token[1:]
            ),
            0,
        )
        if command_index and command_index < len(segment):
            if segment[command_index] == "--":
                command_index += 1
            if command_index < len(segment):
                nested.append(segment[command_index])
    return nested


def _evaluate_gh_segment(segment: list[str]) -> HookDecision | None:
    executable_index = _command_executable_index(segment)
    if (
        executable_index >= len(segment)
        or Path(segment[executable_index].strip("`();")).name != "gh"
    ):
        return None
    return _evaluate_gh_args(segment[executable_index + 1 :])


def _git_environment_policy(
    segments: list[list[str]],
) -> tuple[bool, HookDecision | None]:
    has_git = any(_is_git_token(token) for segment in segments for token in segment)
    if has_git and any(
        token.startswith("GIT_CONFIG_") for segment in segments for token in segment
    ):
        return True, HookDecision(False, _GIT_CONFIG_OVERRIDE_REASON)
    return has_git, None


def _evaluate_nested_commands(
    segments: list[list[str]],
    command: str,
    *,
    allow_dry_run_no_verify: bool,
    depth: int,
) -> tuple[bool, HookDecision | None]:
    saw_nested_git = False
    for nested in _nested_shell_commands(segments, command):
        if depth >= 4:
            return True, HookDecision(
                False, "BLOCKED: nested shell command exceeded hook parsing depth."
            )
        _, nested_has_git, nested_decision = _evaluate_tokenized_command_policy(
            nested,
            allow_dry_run_no_verify=allow_dry_run_no_verify,
            depth=depth + 1,
        )
        saw_nested_git = saw_nested_git or nested_has_git
        if nested_decision is not None:
            return saw_nested_git, nested_decision
    return saw_nested_git, None


def _evaluate_tokenized_command_policy(
    command: str, *, allow_dry_run_no_verify: bool, depth: int = 0
) -> tuple[bool, bool, HookDecision | None]:
    segments = _parse_shell_segments(command)
    if segments is None:
        return False, False, None
    try:
        segments = [_expand_env_split_string(segment) for segment in segments]
    except ValueError:
        return (
            True,
            False,
            HookDecision(
                False, "BLOCKED: unable to safely inspect env -S command expansion."
            ),
        )
    _resolve_literal_command_variables(segments)

    saw_nested_git, nested_decision = _evaluate_nested_commands(
        segments,
        command,
        allow_dry_run_no_verify=allow_dry_run_no_verify,
        depth=depth,
    )
    if nested_decision is not None:
        return True, saw_nested_git, nested_decision

    has_git, environment_decision = _git_environment_policy(segments)
    if environment_decision is not None:
        return True, True, environment_decision

    for segment in segments:
        gh_decision = _evaluate_gh_segment(segment)
        if gh_decision is not None:
            return True, has_git or saw_nested_git, gh_decision
        for index, token in enumerate(segment):
            if not _is_git_token(token):
                continue
            decision = _evaluate_git_args(
                segment[index + 1 :],
                allow_dry_run_no_verify=allow_dry_run_no_verify,
            )
            if decision is not None:
                return True, True, decision
    return True, has_git or saw_nested_git, None


def _allow_flag_present(start_dir: Path) -> bool:
    search_dir = start_dir
    while True:
        candidate = search_dir / ".issue-orchestrator" / "allow-no-verify-dry-run"
        if candidate.exists():
            return True
        if search_dir.parent == search_dir:
            return False
        search_dir = search_dir.parent


_DIRTY_WORKAROUND_SUFFIX = " If the dirty tree is legitimately unresolvable, escalate with `coding-done needs_human --question ...`. Do NOT hide files."


def _dirty_reason(action: str) -> str:
    return f"BLOCKED: {action}." + _DIRTY_WORKAROUND_SUFFIX


# fmt: off
_DIRTY_WORKAROUND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\.git/(worktrees/[^/\s]+/)?info/exclude\b"), _dirty_reason("editing .git/info/exclude hides untracked files")),
    (re.compile(r">>?\s*(\S*/)?\.gitignore\b"), _dirty_reason("writing to .gitignore to hide files is forbidden")),
    (re.compile(r"(?:^|[;&|\s])tee\b[^\n|;&]*\s(\S*/)?\.gitignore\b"), _dirty_reason("writing to .gitignore via `tee` is forbidden")),
    (re.compile(r"sed\b[^|;&\n]*?\s-i\S*[^|;&\n]*?\s(?:\S*/)?\.gitignore(?:\s|$)"), _dirty_reason("editing .gitignore in place is forbidden")),
    (re.compile(r"git\s+update-index\b[^\n]*(?:--assume-unchanged|--skip-worktree)(?:\s|$)"), _dirty_reason("git update-index --assume-unchanged/--skip-worktree is forbidden")),
]
# fmt: on


def evaluate_command(command: str, cwd: Path | None = None) -> HookDecision:
    if not command:
        return HookDecision(True, "")

    command = re.sub(r"(?<!\\)((?:\\\\)*)\\\n", r"\1", command)
    command = re.sub(
        r"\$'((?:[^'\\]|\\.)*)'",
        lambda match: shlex.quote(
            codecs.decode(match.group(1), "unicode_escape", "backslashreplace")
        ),  # fmt: skip
        command,
    )
    cwd = cwd or Path.cwd()

    allow_dry_run_no_verify = _allow_flag_present(cwd)
    parsed, saw_git, git_decision = _evaluate_tokenized_command_policy(
        command,
        allow_dry_run_no_verify=allow_dry_run_no_verify,
    )
    if git_decision is not None:
        return git_decision

    if (
        parsed
        and saw_git
        and re.search(r"--no-veri(?:f(?:y)?)?\b", command)
        and not (allow_dry_run_no_verify and is_dry_run_no_verify_push(command))
    ):
        return HookDecision(False, _NO_VERIFY_REASON)

    for pattern, reason in _DIRTY_WORKAROUND_PATTERNS:
        if pattern.search(command):
            return HookDecision(False, reason)

    return HookDecision(True, "")


def evaluate_raw_input(raw: str, cwd: Path | None = None) -> HookDecision:
    command = extract_command_from_input(raw)
    if raw and not command:
        return HookDecision(
            False,
            "BLOCKED: unable to extract command from hook input. Input may be malformed.",
        )
    return evaluate_command(command, cwd=cwd)


def format_cursor_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permission": "allow"})
    return json.dumps({"permission": "deny", "userMessage": decision.reason})


def format_copilot_response(decision: HookDecision) -> str:
    if decision.allowed:
        return json.dumps({"permissionDecision": "allow"})
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": decision.reason}
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("claude", "codex", "cursor", "gemini", "copilot"),
        required=True,
    )
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    decision = evaluate_raw_input(raw, cwd=Path.cwd())

    if args.mode in ("claude", "codex", "gemini"):
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
        return decision.exit_code

    if args.mode == "cursor":
        print(format_cursor_response(decision))
        return 0

    if args.mode == "copilot":
        print(format_copilot_response(decision))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
