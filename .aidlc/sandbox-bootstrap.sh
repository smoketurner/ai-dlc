#!/usr/bin/env bash
# Bootstrap an AgentCore Code Interpreter sandbox session for ai-dlc.
#
# Runs from the extracted PR head (cwd = repo root) immediately after the
# tarball extract, before the agent's `commands` list. Convention is owned
# by ``packages/common/src/common/sandbox.py``; see SANDBOX_BOOTSTRAP_RELPATH.
#
# Keep it minimal: install ``uv``. ``uv run`` syncs workspace deps lazily on
# first use, so per-command cost is paid once per session and only for the
# packages the agent actually touches.
set -euo pipefail
pip install -q uv
