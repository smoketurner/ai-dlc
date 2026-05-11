#!/usr/bin/env bash
# Sandbox bootstrap for Java projects (Maven or Gradle).
#
# Copy to ``.aidlc/sandbox-bootstrap.sh`` in your repo. Installs ``mise``
# and provisions a Temurin JDK plus the matching build tool from your
# ``.tool-versions`` / ``mise.toml`` (or LTS defaults).

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
    mise use --global java@temurin-21
    if [ -f pom.xml ]; then
        mise use --global maven@latest
    elif [ -f build.gradle ] || [ -f build.gradle.kts ]; then
        mise use --global gradle@latest
    fi
fi

echo "toolchain ready: $(java -version 2>&1 | head -n1)"
if command -v mvn >/dev/null 2>&1; then
    echo "build tool: $(mvn --version | head -n1)"
fi
if command -v gradle >/dev/null 2>&1; then
    echo "build tool: $(gradle --version | grep Gradle | head -n1)"
fi
