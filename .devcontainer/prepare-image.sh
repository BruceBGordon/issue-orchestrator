#!/usr/bin/env bash
# Prepare image-level prerequisites that a Codespaces prebuild should bake.
set -euo pipefail

# The Python devcontainer image already has Yarn installed, but its Yarn apt
# source is signed by a key that the image no longer carries. Any later apt
# update then fails, which prevents Playwright from installing Chromium's host
# libraries during `make worktree-setup`. This repository uses npm, not the
# Yarn apt repository, so remove the unusable source before dependency setup.
sudo rm -f -- /etc/apt/sources.list.d/yarn.list

npm install -g @openai/codex@0.149.0
