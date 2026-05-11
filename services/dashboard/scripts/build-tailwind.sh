#!/usr/bin/env bash
# Build services/dashboard/src/dashboard/static/tailwind.css from
# tailwind.input.css using the standalone tailwindcss CLI. Run from
# services/dashboard/src/dashboard/ (the terraform-aws-modules/lambda
# `commands` step CDs there before invoking us).
set -euo pipefail

TAILWIND_VERSION="v4.3.0"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
service_dir="$(cd "${script_dir}/.." && pwd)"
bin_dir="${service_dir}/.bin"

uname_s="$(uname -s)"
uname_m="$(uname -m)"
case "${uname_s}-${uname_m}" in
    Darwin-arm64)  asset="tailwindcss-macos-arm64" ;;
    Darwin-x86_64) asset="tailwindcss-macos-x64" ;;
    Linux-aarch64) asset="tailwindcss-linux-arm64" ;;
    Linux-arm64)   asset="tailwindcss-linux-arm64" ;;
    Linux-x86_64)  asset="tailwindcss-linux-x64" ;;
    *) echo "Unsupported platform: ${uname_s}-${uname_m}" >&2; exit 1 ;;
esac

binary="${bin_dir}/${asset}-${TAILWIND_VERSION}"

if [[ ! -x "${binary}" ]]; then
    mkdir -p "${bin_dir}"
    url="https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/${asset}"
    echo "Downloading ${url}" >&2
    curl -fsSL -o "${binary}.tmp" "${url}"
    chmod +x "${binary}.tmp"
    mv "${binary}.tmp" "${binary}"
fi

cd "${service_dir}/src/dashboard"
"${binary}" -i static/tailwind.input.css -o static/tailwind.css --minify
