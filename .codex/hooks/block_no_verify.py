"""Shared hook policy for blocking no-verify and restricted commands.

Also blocks the specific workarounds agents reach for when the
``coding-done`` dirty-tree guard rejects them: editing
``.git/info/exclude``, appending to ``.gitignore``, and marking tracked
files ``--assume-unchanged`` / ``--skip-worktree``. Each of these
*hides* dirtiness from the guard rather than resolving it; all four
were observed on live sessions (see #5949). Claude Code's native
sensitive-file gate blocks the ``.git/info/exclude`` edit interactively
and hangs the session for the full 90-minute timeout; this hook fires
before that gate so the agent gets a fail-fast rejection with a clear
next step instead.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shlex
import sys
from pathlib import Path


@dataclass(frozen=True)
class HookDecision:
    """Decision for a hook evaluation."""

    allowed: bool
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return 0 if self.allowed else 2


def extract_command_from_input(raw: str) -> str:
    """Extract the shell command from hook JSON input."""
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


_SHELL_OPERATOR_CHARS = frozenset(";&|()")


def _parse_shell_segments(command: str) -> list[list[str]] | None:
    """Tokenize shell commands while preserving command boundaries.

    ``shlex.split`` removes quoting, which is essential for recognizing quoted
    Git options. Enabling punctuation also prevents a later command in a shell
    chain from being mistaken for an argument to the first one.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
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


def _parse_argv(command: str) -> list[str]:
    segments = _parse_shell_segments(command)
    if not segments or len(segments) != 1:
        return []
    return segments[0]


def _is_git_push(argv: list[str]) -> bool:
    return len(argv) >= 2 and argv[0] == "git" and argv[1] == "push"


def is_dry_run_no_verify_push(command: str) -> bool:
    argv = _parse_argv(command)
    if not argv or not _is_git_push(argv):
        return False
    return "--dry-run" in argv and "--no-verify" in argv


_NO_VERIFY_REASON = "BLOCKED: --no-verify is forbidden. Pre-push hooks must run."
_SHORT_NO_VERIFY_REASON = "BLOCKED: -n (--no-verify) is forbidden."
_GIT_CONFIG_OVERRIDE_REASON = (
    "BLOCKED: Per-command Git config overrides "
    "(-c, --config-env, and GIT_CONFIG_*) are forbidden."
)
_GIT_CONFIG_MUTATION_REASON = (
    "BLOCKED: Mutating Git config from an agent session is forbidden. "
    "Read-only `git config get`, `--get`, and `list` commands are allowed."
)


def _is_git_token(token: str) -> bool:
    """Recognize Git invoked by name or an explicit executable path."""
    return Path(token.strip("`")).name == "git"


def _uses_git_config_override(global_args: list[str]) -> bool:
    """Detect Git's per-command config override forms."""
    for token in global_args:
        if token == "-c":
            return True
        if token.startswith("-c") and len(token) > 2:
            return True
        if token == "--config-env" or token.startswith("--config-env="):
            return True
    return False


def _uses_git_config_environment(segment: list[str]) -> bool:
    return any(token.startswith("GIT_CONFIG_") for token in segment)


_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
)


def _split_git_args(args: list[str]) -> tuple[list[str], str | None, list[str]]:
    """Separate Git global options from its subcommand and subcommand argv."""
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
    """Return whether a short Git option bundle contains commit's ``-n``."""
    if not token.startswith("-") or token.startswith("--"):
        return False
    for option in token[1:]:
        if option == "n":
            return True
        if option in frozenset("mFcCSu"):
            return False
    return False


def _is_no_verify_option(token: str) -> bool:
    """Recognize Git's canonical flag and accepted unique abbreviations."""
    return len(token) >= len("--no-veri") and "--no-verify".startswith(token)


_CONFIG_READ_ACTIONS = frozenset({"get", "list"})
_CONFIG_MUTATION_ACTIONS = frozenset(
    {"edit", "remove-section", "rename-section", "set", "unset"}
)
_CONFIG_READ_FLAGS = frozenset(
    {
        "-l",
        "--get",
        "--get-all",
        "--get-color",
        "--get-colorbool",
        "--get-regexp",
        "--get-urlmatch",
        "--list",
    }
)
_CONFIG_MUTATION_FLAGS = frozenset(
    {
        "--add",
        "--edit",
        "-e",
        "--remove-section",
        "--rename-section",
        "--replace-all",
        "--unset",
        "--unset-all",
    }
)
_CONFIG_OPTIONS_WITH_VALUE = frozenset({"--blob", "--file", "-f"})
_CONFIG_READ_ONLY_OPTIONS = frozenset(
    {
        "--all",
        "--bool",
        "--bool-or-int",
        "--bool-or-str",
        "--expiry-date",
        "--fixed-value",
        "--global",
        "--includes",
        "--int",
        "--local",
        "--name-only",
        "--no-includes",
        "--null",
        "--path",
        "--show-names",
        "--show-origin",
        "--system",
        "--worktree",
        "-z",
    }
)


def _abbreviates_mutation_option(token: str) -> bool:
    return token.startswith("--") and any(
        candidate.startswith(token) for candidate in _CONFIG_MUTATION_FLAGS
    )


def _config_is_read_only(args: list[str]) -> bool:
    """Allow-list Git config observation while rejecting every mutation form."""
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
    # The legacy `git config <name>` form is a read. Zero positional
    # arguments only prints usage and cannot mutate configuration.
    return len(positional) <= 1


def _evaluate_git_args(
    args: list[str], *, allow_dry_run_no_verify: bool
) -> HookDecision | None:
    args = [token.strip("`") for token in args]
    global_args, name, subcommand_args = _split_git_args(args)
    if _uses_git_config_override(global_args):
        return HookDecision(False, _GIT_CONFIG_OVERRIDE_REASON)

    if name is None:
        if any(_is_no_verify_option(token) for token in args):
            return HookDecision(False, _NO_VERIFY_REASON)
        return None

    if name in {"commit", "push"} and any(
        _is_no_verify_option(token) for token in subcommand_args
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
_SHELL_PREFIXES = frozenset({"command", "env"})


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
        bundled = re.fullmatch(r"-[^-]*S(.*)", option)
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
    split = _split_shell_words(value.replace("\\_", " "))
    return (split, consumed) if split is not None else None


def _expand_env_split_string(segment: list[str]) -> list[str]:
    """Expand the command string accepted by ``env -S`` for inspection."""
    expanded = list(segment)
    for index, token in enumerate(expanded):
        if Path(token).name != "env":
            continue
        for option_index in range(index + 1, len(expanded)):
            option = expanded[option_index]
            parsed = _parsed_env_split_option(expanded, option_index)
            if parsed is not None:
                split, consumed = parsed
                return (
                    expanded[:option_index]
                    + split
                    + expanded[option_index + consumed :]
                )
            if not option.startswith("-") and not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*=.*", option
            ):
                break
    return expanded


def _shell_executable_index(segment: list[str]) -> int:
    index = 0
    while index < len(segment) and Path(segment[index]).name in _SHELL_PREFIXES:
        prefix = Path(segment[index]).name
        index += 1
        while index < len(segment):
            token = segment[index]
            if prefix == "env" and token in {
                "-C",
                "-P",
                "-u",
                "--chdir",
                "--unset",
            }:
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


def _nested_shell_commands(segments: list[list[str]]) -> list[str]:
    nested: list[str] = []
    for original_segment in segments:
        segment = _expand_env_split_string(original_segment)
        shell_index = _shell_executable_index(segment)
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


def _evaluate_tokenized_git_policy(
    command: str, *, allow_dry_run_no_verify: bool, depth: int = 0
) -> tuple[bool, bool, HookDecision | None]:
    """Return tokenization, actual-Git detection, and the policy decision."""
    segments = _parse_shell_segments(command)
    if segments is None:
        return False, False, None
    segments = [_expand_env_split_string(segment) for segment in segments]

    saw_nested_git = False
    for nested in _nested_shell_commands(segments):
        if depth >= 4:
            return (
                True,
                True,
                HookDecision(
                    False, "BLOCKED: nested shell command exceeded hook parsing depth."
                ),
            )
        _, nested_has_git, nested_decision = _evaluate_tokenized_git_policy(
            nested,
            allow_dry_run_no_verify=allow_dry_run_no_verify,
            depth=depth + 1,
        )
        saw_nested_git = saw_nested_git or nested_has_git
        if nested_decision is not None:
            return True, saw_nested_git, nested_decision

    has_git = any(_is_git_token(token) for segment in segments for token in segment)
    if has_git and any(_uses_git_config_environment(segment) for segment in segments):
        return True, True, HookDecision(False, _GIT_CONFIG_OVERRIDE_REASON)

    for segment in segments:
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


# Shared suffix appended to every dirty-tree-workaround rejection
# reason. Must tell the agent exactly what to do next — without this,
# a blocked agent just cycles through novel workarounds we haven't
# banned yet, which is the whole failure mode this hook exists to
# prevent. Treat as a load-bearing invariant; every new dirty-tree
# workaround pattern MUST use this suffix.
_DIRTY_WORKAROUND_SUFFIX = (
    " If the dirty tree is legitimately unresolvable, escalate with "
    "`coding-done needs_human --question ...`. Do NOT hide files."
)

# Static policy: dirty-tree-workaround patterns. Module-level so the
# regex compilation is amortised across calls and so the policy is
# visibly a single-source-of-truth list, not a per-call decision.
_DIRTY_WORKAROUND_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # ``.git/info/exclude`` and its linked-worktree form. Any mention
    # of this path in a bash command is an attempt to hide untracked
    # files from the dirty-tree guard; agents have no legitimate
    # reason to read or write it.
    (
        re.compile(r"\.git/(worktrees/[^/\s]+/)?info/exclude\b"),
        "BLOCKED: editing .git/info/exclude hides untracked files "
        "from the dirty-tree guard." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # Shell redirection (``>`` / ``>>``) to *any* ``.gitignore`` —
    # subdirectory forms and absolute paths included. Reading
    # (``cat``/``grep``/``less``/``head``) and observation commands
    # (``git check-ignore``/``ls-files``/``status``) remain allowed
    # because they don't match this pattern.
    (
        re.compile(r">>?\s*(\S*/)?\.gitignore\b"),
        "BLOCKED: writing to .gitignore to hide files is not "
        "allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``tee`` is the other common "append to file" shell idiom. Any
    # ``tee`` where ``.gitignore`` appears as an argument is a write
    # attempt regardless of flags (``-a``, ``--append``, or plain
    # overwrite).
    (
        re.compile(r"(?:^|[;&|\s])tee\b[^\n|;&]*\s(\S*/)?\.gitignore\b"),
        "BLOCKED: writing to .gitignore via `tee` to hide files is not "
        "allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``sed -i`` on ``.gitignore``, flag- and token-order tolerant.
    # GNU's bare ``-i``, BSD/macOS's ``-i ''``, and ``-i.bak`` backup-
    # suffix form all begin with the literal ``-i`` token. Agents also
    # commonly pass ``-e '<expr>'`` before ``-i``; the bounded lazy
    # match ``[^|;&\n]*?`` lets arbitrary intervening tokens appear
    # while staying inside a single shell command (not crossing pipes,
    # command separators, or newlines into unrelated commands).
    (
        re.compile(r"sed\b[^|;&\n]*?\s-i\S*[^|;&\n]*?\s(?:\S*/)?\.gitignore(?:\s|$)"),
        "BLOCKED: editing .gitignore in place to hide files is "
        "not allowed from an agent session." + _DIRTY_WORKAROUND_SUFFIX,
    ),
    # ``git update-index --assume-unchanged`` / ``--skip-worktree``
    # mark tracked files invisible to ``git status`` without a commit.
    # Both are guard-hiding; ``git ls-files -v`` (observation) is
    # unaffected because it doesn't mutate the index.
    (
        re.compile(
            r"git\s+update-index\b[^\n]*(?:--assume-unchanged|--skip-worktree)(?:\s|$)"
        ),
        "BLOCKED: `git update-index --assume-unchanged` and "
        "`--skip-worktree` hide tracked files from the dirty-tree "
        "guard." + _DIRTY_WORKAROUND_SUFFIX,
    ),
]

_MALFORMED_GIT_FALLBACK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\bgit\b[^\n;&|]*\b(?:commit|push)\b[^\n;&|]*"
            r"--no-veri(?:f(?:y)?)?(?:\s|$)"
        ),
        _NO_VERIFY_REASON,
    ),
    (re.compile(r"git\s+--no-veri(?:f(?:y)?)?"), _NO_VERIFY_REASON),
    (
        re.compile(r"\bgit\b[^\n;&|]*\bcommit\b[^\n;&|]*\s-n(?:\s|$)"),
        _SHORT_NO_VERIFY_REASON,
    ),
    (
        re.compile(r"\bgit\b[^\n;&|]*\s-c\s+core\.hooksPath(?:=|\s+)\S+"),
        _GIT_CONFIG_OVERRIDE_REASON,
    ),
    (
        re.compile(
            r"\bgit\s+config\b"
            r"(?=[^\n;&|]*\bcore\.hooksPath\b)"
            r"(?![^\n;&|]*\s--get(?:-all)?\s+core\.hooksPath(?:\s|$))"
            r"[^\n;&|]*"
        ),
        _GIT_CONFIG_MUTATION_REASON,
    ),
]

_STATIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"gh\s+pr\s+merge"),
        "BLOCKED: Agents cannot merge PRs. Only humans can merge.",
    ),
    (
        re.compile(r"gh\s+api\s+.*pulls/[0-9]+/merge"),
        "BLOCKED: Agents cannot merge PRs via API. Only humans can merge.",
    ),
    *_DIRTY_WORKAROUND_PATTERNS,
]


def evaluate_command(command: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate a command string and return allow/deny decision.

    Blocks three categories of forbidden bash commands:

    1. Git-guardrail bypass: ``--no-verify`` and any mutation or per-command
       override of ``core.hooksPath``. Explicit ``git config --get`` reads
       remain allowed.
    2. GitHub action boundary: ``gh pr merge``, merge via ``gh api``.
    3. ``coding-done`` dirty-tree-guard workarounds: edits to
       ``.git/info/exclude``, writes to ``.gitignore`` (shell
       redirection, ``tee``, or ``sed -i``), and
       ``git update-index --assume-unchanged`` / ``--skip-worktree``.

    Category 3 exists because Claude Code's native sensitive-file gate
    blocks the underlying edits *interactively* and silently hangs the
    session for 90 minutes; firing this hook first converts the hang
    into a fail-fast rejection with a clear next step (the shared
    escalation suffix — see ``_DIRTY_WORKAROUND_SUFFIX``).
    """
    if not command:
        return HookDecision(True, "")

    cwd = cwd or Path.cwd()

    allow_dry_run_no_verify = _allow_flag_present(cwd)
    parsed, saw_git, git_decision = _evaluate_tokenized_git_policy(
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

    if not parsed:
        for pattern, reason in _MALFORMED_GIT_FALLBACK_PATTERNS:
            if pattern.search(command):
                return HookDecision(False, reason)

    for pattern, reason in _STATIC_PATTERNS:
        if pattern.search(command):
            return HookDecision(False, reason)

    return HookDecision(True, "")


def evaluate_raw_input(raw: str, cwd: Path | None = None) -> HookDecision:
    """Evaluate raw hook JSON input and return allow/deny decision."""
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
