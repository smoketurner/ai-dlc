#!/usr/bin/env bash
# Sandbox bootstrap for Rust projects.
#
# Copy this file to ``.aidlc/sandbox-bootstrap.sh`` in your repo. The Code
# Interpreter sandbox is Python/JS/TS only out of the box; this script
# installs ``mise`` and uses it to provision a Rust toolchain that
# matches your repo's ``.tool-versions`` / ``mise.toml`` (or a sensible
# default if neither is present).
#
# Runs once per sandbox session, before the Reviewer / Tester invoke
# their commands. A non-zero exit fails the extract step so the agent
# doesn't run cargo against a half-set-up workspace.

set -euo pipefail

if ! command -v mise >/dev/null 2>&1; then
    echo "installing mise..."
    curl -fsSL https://mise.run | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

eval "$(mise activate bash)"

if [ -f mise.toml ] || [ -f .tool-versions ]; then
    mise install
else
    mise use --global rust@stable
fi

echo "toolchain ready: $(rustc --version), $(cargo --version)"
