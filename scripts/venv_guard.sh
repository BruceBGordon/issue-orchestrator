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
#   0  OWNED     .venv belongs to this checkout (or is absent). Mutate freely.
#   1  SHARED    .venv is another checkout's. Never install THIS project into
#                it. Dependency-only syncs are still permitted and are how
#                callers keep their postcondition -- see --explain sync.
#   2  BROKEN    .venv is a dangling symlink. The environment cannot be used or
#                safely created over. Callers must FAIL rather than continue.
#
# Callers must distinguish 1 from 2. Treating "not zero" as "skip" is what let
# a dangling venv report success.

set -uo pipefail

quiet=0
explain=""
while [ $# -gt 0 ]; do
  case "$1" in
    --quiet) quiet=1 ;;
    --explain) explain="${2:-}"; shift ;;
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

# Absent or a real directory => private to this checkout => fully owned.
if [ ! -L .venv ]; then
  exit "$OWNED"
fi

here="$(pwd -P)"
link_target="$(readlink .venv)"

if ! target="$(cd .venv 2>/dev/null && pwd -P)"; then
  if [ "$quiet" -eq 0 ]; then
    cat >&2 <<MSG
venv-guard: .venv is a DANGLING symlink.
  this checkout : $here
  .venv points  : $link_target  (does not exist)

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

if [ "$quiet" -eq 0 ]; then
  owner="${target%/.venv}"
  cat >&2 <<MSG
venv-guard: .venv here is SHARED from another checkout.
  this checkout : $here
  .venv resolves: $target
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
