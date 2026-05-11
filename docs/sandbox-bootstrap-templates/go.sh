#!/usr/bin/env bash
# Sandbox bootstrap for Go projects.
#
# Copy to ``.aidlc/sandbox-bootstrap.sh`` in your repo. Installs ``mise``
# and provisions a Go toolchain matching your ``.tool-versions`` /
# ``mise.toml`` (or the version declared in ``go.mod`` as a fallback).

set -euo pipefail

if ! command -v mise >/dev/null 2>&1; then
    echo "installing mise..."
    curl -fsSL https://mise.run | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

eval "$(mise activate bash)"

if [ -f mise.toml ] || [ -f .tool-versions ]; then
    mise install
elif [ -f go.mod ]; then
    version="$(awk '/^go /{print $2; exit}' go.mod)"
    mise use --global "go@${version:-latest}"
else
    mise use --global go@latest
fi

if [ -f go.sum ]; then
    go mod download
fi

echo "toolchain ready: $(go version)"
