#!/usr/bin/env bash
#
# Is this checkout's .venv safe for THIS checkout's tooling to mutate?
#
# The orchestrator links the base repo's venv into every worktree it creates
# (adapters/worktree/_worktree_runtime.py::_link_repo_venv_into_worktree) so
# validation commands work there without building a venv per worktree. That
# sharing is intentional. What is not intentional is a worktree then running
# `uv sync` / `pip install -e .` against that link: uv resolves `.venv` through
# the symlink to the BASE venv and reinstalls THIS worktree's project as
# editable into it, rewriting `_editable_impl_issue_orchestrator.pth` to point
# at this worktree's `src`.
#
# The base venv's `issue_orchestrator` then resolves to whichever worktree most
# recently ran setup. While that worktree lives, imports silently pick up
# another checkout's half-written source; once the orchestrator removes it,
# every import dangles with a bare ModuleNotFoundError that names neither the
# .pth nor the deleted directory.
#
# Exit 0  -> .venv belongs to this checkout; mutate freely.
# Exit 1  -> .venv is shared from another checkout; do not mutate.
#
# Callers decide the consequence. Targets the orchestrator invokes on every
# worktree setup (venv-fast, install, sync-deps) SKIP the sync and carry on —
# the shared venv is already synced by whoever owns it, and failing there would
# break agent session launches. Explicitly destructive, human-typed targets
# (venv, venv-pip, which `rm -rf .venv`) FAIL instead of silently converting a
# shared venv into a private one.

set -uo pipefail

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1

# Not a symlink (or absent) => private to this checkout => safe.
[ -L .venv ] || exit 0

here="$(pwd -P)"
target="$(cd .venv 2>/dev/null && pwd -P)" || {
  # Dangling symlink: the venv it pointed at is gone. Not shared-and-live, but
  # not usable either. Report it as shared so callers do not sync into a void.
  if [ "$quiet" -eq 0 ]; then
    echo "venv-guard: .venv is a DANGLING symlink -> $(readlink .venv)" >&2
    echo "venv-guard: remove it and re-run, or repair the checkout it pointed at." >&2
  fi
  exit 1
}

case "$target" in
  "$here"/*) exit 0 ;;
esac

if [ "$quiet" -eq 0 ]; then
  owner="${target%/.venv}"
  cat >&2 <<MSG
venv-guard: .venv here is SHARED from another checkout.
  this checkout : $here
  .venv resolves: $target
  owned by      : $owner

Skipping the dependency sync. Syncing would rewrite that shared venv's editable
install to point at THIS checkout's src, which silently breaks every other user
of it (and dangles entirely once this checkout is removed).

To sync dependencies, do it where the venv lives:
  make -C "$owner" install

To give this checkout its own venv instead:
  rm .venv && make venv-fast
MSG
fi
exit 1
