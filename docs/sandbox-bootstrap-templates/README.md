# Sandbox bootstrap templates

The Reviewer and Tester agents run a target repo's tests inside an Amazon
Bedrock AgentCore Code Interpreter sandbox. That sandbox ships with
Python, Node.js, and TypeScript out of the box. For every other
language, the sandbox needs a one-time setup step to install the
toolchain.

ai-dlc looks for that setup step at `.aidlc/sandbox-bootstrap.sh` in the
target repo's root, runs it once per session (after the PR tarball is
extracted, before any agent command), and fails closed on a non-zero
exit.

This folder holds copy-paste templates for the languages the platform
doesn't pre-install:

| File | When to copy |
|------|--------------|
| [`rust.sh`](./rust.sh) | Cargo project (single crate or workspace) |
| [`go.sh`](./go.sh) | `go.mod` / `go.work` project |
| [`java.sh`](./java.sh) | Maven (`pom.xml`) or Gradle (`build.gradle` / `build.gradle.kts`) project |
| [`ruby.sh`](./ruby.sh) | Bundler (`Gemfile`) project |

## How to use

1. Copy the template that matches your stack to `.aidlc/sandbox-bootstrap.sh` at the root of your repo.
2. Commit it.
3. If you pin tool versions in `.tool-versions` or `mise.toml`, the script will pick them up automatically. Otherwise it defaults to the latest stable release.

## How it works

Every template uses [`mise`](https://mise.jdx.dev) — a single-binary
runtime manager — to install the toolchain inside the sandbox. The
pattern is:

```bash
curl -fsSL https://mise.run | sh        # one-time bootstrap of mise itself
eval "$(mise activate bash)"            # put mise shims on PATH
mise install                            # install everything from .tool-versions / mise.toml
```

Once the toolchain is on PATH the agent runs whatever the stack profile
or the project's `MEMORY.md` says — typically `cargo test`,
`go test ./...`, `mvn test`, or `bundle exec rspec`.

## Polyglot repos

A polyglot repo can chain installs in a single script — there's nothing
special about one template per language. Concatenate the install
blocks, deduplicate the `mise install` once, and call it a day.
