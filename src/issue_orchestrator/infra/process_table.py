"""One owner for every read of the OS process table.

``ps`` decides how much of the COMMAND column to print from the *environment*,
and it does so even when its output is a pipe: procps truncates to ``$COLUMNS``.
A pytest-xdist worker on Linux starts with ``COLUMNS=80`` already set, which is
enough to cut every row down to the interpreter path. The table still lists
every process and every command in it is a lie -- so a matcher finds nothing,
a sweep reaps nothing, and both report success (#7142, CI failure on PR #7143).

Two guards, because either alone can be undone by a caller:

* ``-ww`` asks ``ps`` for unlimited width explicitly, and
* the width variables are dropped from the child's environment.

Both live here so a seventh caller cannot get one and miss the other, which is
what happened the first time this was fixed in five places by hand.

Selector spelling matters and is not symmetric: macOS ``ps`` rejects ``-ww``
placed before a bare BSD selector (``ps -ww ax`` is an illegal argument) while
accepting every dashed form. Callers therefore pass dashed selectors -- ``-A``
rather than ``ax`` -- and this module puts the width flag first.
"""

from __future__ import annotations

import os

# Unlimited width. Spelled the same for procps and BSD ps; twice is the
# documented "as many columns as necessary, ignore the window" form.
FULL_WIDTH_FLAG = "-ww"

# What ps reads to decide how much to print. LINES rides along because the two
# are set together and neither has any business shaping a parsed table.
_WIDTH_VARIABLES = ("COLUMNS", "LINES")


def ps_command(*arguments: str) -> list[str]:
    """A ``ps`` invocation that prints the whole COMMAND column.

    Args:
        arguments: dashed ``ps`` options, e.g. ``("-A", "-o", "pid=,command=")``.
            A bare BSD selector such as ``ax`` is rejected by macOS ``ps`` when
            the width flag precedes it; use ``-A``.
    """
    return ["ps", FULL_WIDTH_FLAG, *arguments]


def ps_env(**overrides: str) -> dict[str, str]:
    """The inherited environment with the terminal width taken out of it.

    Args:
        overrides: extra variables to pin, e.g. a locale for callers that parse
            numbers out of the table.
    """
    env = {**os.environ, **overrides}
    for variable in _WIDTH_VARIABLES:
        env.pop(variable, None)
    return env
