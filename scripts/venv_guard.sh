#!/usr/bin/env bash
#
# THE mutation-authorization owner for this repo's Python environment.
#
# Every path that installs this project or syncs its dependencies asks this one
# question first: what may this checkout's tooling do to the .venv it can see?
# Shell callers exec it; Python callers shell out to it (it deliberately has no
# Python dependency, because Control Center startup consults it *before* the
# package is importable).
#
# WHY THIS EXISTS
# The orchestrator links the base repo's venv into every worktree it creates
# (adapters/worktree/_worktree_runtime.py::_link_repo_venv_into_worktree) so
# validation works there without building a venv per worktree. That sharing is
# intentional. What is not is a worktree then running `uv sync` or
# `pip install -e .` through the link: uv resolves `.venv` to the BASE venv and
# reinstalls THIS checkout's project as editable into it, rewriting
# `_editable_impl_issue_orchestrator.pth`. Imports then resolve to whichever
# checkout last ran setup, and dangle entirely once it is deleted.
#
# OUTCOMES (exit codes)
#   0  OWNED     the venv belongs to this checkout, is absent, or is a
#                standalone environment no checkout owns. Mutate freely.
#   1  SHARED    the venv belongs to ANOTHER checkout. Never install THIS
#                project into it. Dependency-only syncs are still permitted and
#                are how callers keep their postcondition -- see --explain sync.
#   2  BROKEN    the venv is a dangling symlink. It cannot be used, and creating
#                over it writes into a dead path. Callers must FAIL.
#  64  USAGE     bad arguments.
#
# Callers must classify ALL outcomes exhaustively and FAIL CLOSED on anything
# they do not recognise -- including this script being missing or
# non-executable. Two failure shapes have already been caught here:
#   - treating "not zero" as "skip" let a dangling venv report success;
#   - routing "any other code" to the else branch turned a missing guard into a
#     full project sync, which is the exact mutation the guard exists to stop.
#
# --venv PATH targets an environment other than ./.venv (Control Center honours
# CC_VENV_PATH, and guarding ./.venv while mutating a different path guards
# nothing).

set -uo pipefail

quiet=0
explain=""
venv_path=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) quiet=1 ;;
    --explain) explain="${2:-}"; shift ;;
    --venv) venv_path="${2:-}"; shift ;;
    *) echo "venv-guard: unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

OWNED=0
SHARED=1
BROKEN=2

if [ "$explain" = "sync" ]; then
  # The dependency-only sync a SHARED caller may run. --no-install-project is
  # the load-bearing flag: it updates dependencies without reinstalling this
  # project, so the editable pointer is never rewritten. --inexact stops the
  # sync removing packages other users of the shared venv still need.
  echo "--frozen --all-extras --no-install-project --inexact"
  exit 0
fi

here="$(pwd -P)"
[ -n "$venv_path" ] || venv_path=".venv"

# Absent => nothing to protect.
if [ ! -e "$venv_path" ] && [ ! -L "$venv_path" ]; then
  exit "$OWNED"
fi

link_target="$(readlink "$venv_path" 2>/dev/null || printf '%s' "$venv_path")"

if ! target="$(cd "$venv_path" 2>/dev/null && pwd -P)"; then
  if [ "$quiet" -eq 0 ]; then
    cat >&2 <<MSG
venv-guard: the target venv is a DANGLING symlink.
  this checkout : $here
  venv          : $venv_path
  points at     : $link_target  (does not exist)

The checkout that owned this venv was deleted. Nothing here can use it, and
creating a venv over the link would silently write into the dead path.

Remove the link and build a private environment:
  rm .venv && make venv-fast
MSG
  fi
  exit "$BROKEN"
fi

case "$target" in
  "$here"/*) exit "$OWNED" ;;
esac

# The venv resolves outside this checkout. That is only a hazard when ANOTHER
# checkout owns it -- installing this project would rewrite that checkout's
# editable pointer. A standalone environment (an operator-chosen CC_VENV_PATH,
# a system env) is owned by nobody, so mutating it harms no other checkout.
owner="${target%/*}"
if [ ! -e "$owner/pyproject.toml" ] && [ ! -e "$owner/.git" ]; then
  exit "$OWNED"
fi

if [ "$quiet" -eq 0 ]; then
  cat >&2 <<MSG
venv-guard: the target venv is SHARED from another checkout.
  this checkout : $here
  venv resolves : $target
  owned by      : $owner

This checkout's project will NOT be installed into it: doing so rewrites that
venv's editable install to point at THIS checkout's src, which silently breaks
every other user of it and dangles once this checkout is removed.

Dependencies are still synced, without touching the project install.

To install the project itself, do it where the venv lives:
  make -C "$owner" install
To give this checkout its own environment instead:
  rm .venv && make venv-fast
MSG
fi
exit "$SHARED"
