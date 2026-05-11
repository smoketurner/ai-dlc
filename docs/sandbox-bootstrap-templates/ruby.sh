#!/usr/bin/env bash
# Sandbox bootstrap for Ruby projects.
#
# Copy to ``.aidlc/sandbox-bootstrap.sh`` in your repo. Installs ``mise``
# and provisions a Ruby toolchain matching your ``.tool-versions`` /
# ``mise.toml`` (or the version declared in your ``Gemfile`` as a
# fallback). Then runs ``bundle install`` so specs can run.
#
# Note: ``mise`` builds Ruby from source, which needs ~3 minutes on
# first install. The Code Interpreter session timeout is ~10 minutes;
# if your suite is slow, bump it from the agent side (see
# ``common.agentcore_code_interpreter.session_timeout_seconds``).

set -euo pipefail

if ! command -v mise >/dev/null 2>&1; then
    echo "installing mise..."
    curl -fsSL https://mise.run | sh
    export PATH="${HOME}/.local/bin:${PATH}"
fi

eval "$(mise activate bash)"

if [ -f mise.toml ] || [ -f .tool-versions ]; then
    mise install
elif [ -f Gemfile ]; then
    version="$(grep -E '^ruby ' Gemfile | head -n1 | sed -E "s/^ruby [\"']([^\"']+)[\"'].*/\\1/")"
    mise use --global "ruby@${version:-3.3}"
else
    mise use --global ruby@3.3
fi

if [ -f Gemfile ]; then
    gem install bundler --no-document
    bundle install
fi

echo "toolchain ready: $(ruby --version)"
