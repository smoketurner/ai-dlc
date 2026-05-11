"""Tests for :mod:`common.stack_discovery`.

Each test builds a synthetic repo under a ``tmp_path`` fixture and calls
:func:`common.stack_discovery.discover_stack` against it. Fixtures cover:

* single-language projects (python, node, rust, go, java, ruby, php, elixir)
* native workspaces (uv, pnpm, cargo, go.work)
* polyglot repos without a workspace declaration
* edge cases (manifest-less, bad TOML, capped monorepo)
* CI workflow fallback for components that don't surface commands
* Makefile precedence + tool-version pinning
"""

from __future__ import annotations

from pathlib import Path

from common.stack_discovery import (
    MAX_MANIFESTS,
    StackComponent,
    StackProfile,
    discover_stack,
    parse_makefile_targets,
    render_stack_profile,
)


def write(path: Path, body: str) -> None:
    """Create ``path`` (and parents) and write ``body`` UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def component_at(profile: StackProfile, rel_dir: str) -> StackComponent:
    """Return the single component whose path equals ``rel_dir``."""
    matches = [c for c in profile.components if c.path == rel_dir]
    assert len(matches) == 1, f"expected one component at {rel_dir}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Single-language detection
# ---------------------------------------------------------------------------


def test_python_pyproject_uv(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        """
[project]
name = "demo"
requires-python = ">=3.14,<3.15"

[tool.uv]

[tool.pytest.ini_options]
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "python"
    assert component.package_manager == "uv"
    assert component.version == ">=3.14,<3.15"
    assert component.test_command == "uv run pytest"
    assert profile.primary_language == "python"
    assert profile.polyglot is False
    assert profile.monorepo is False


def test_node_package_json_with_pnpm(tmp_path: Path) -> None:
    write(
        tmp_path / "package.json",
        """{
            "name": "demo",
            "engines": {"node": ">=22"},
            "packageManager": "pnpm@9.0.0",
            "scripts": {"test": "vitest", "build": "tsc", "lint": "oxlint"}
        }""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "node"
    assert component.package_manager == "pnpm"
    assert component.version == ">=22"
    assert component.test_command == "pnpm run test"
    assert component.lint_command == "pnpm run lint"


def test_rust_cargo_package(tmp_path: Path) -> None:
    write(
        tmp_path / "Cargo.toml",
        """
[package]
name = "demo"
version = "0.1.0"
edition = "2024"
rust-version = "1.83"
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "rust"
    assert component.version == "1.83"
    assert component.test_command == "cargo test"
    assert "clippy" in (component.lint_command or "")


def test_go_module(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "go"
    assert component.version == "1.23"
    assert component.test_command == "go test ./..."


def test_java_maven(tmp_path: Path) -> None:
    write(
        tmp_path / "pom.xml",
        """<?xml version="1.0"?>
<project>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
  </properties>
</project>
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "java"
    assert component.version == "21"
    assert component.package_manager == "maven"


def test_ruby_gemfile(tmp_path: Path) -> None:
    write(
        tmp_path / "Gemfile",
        """source "https://rubygems.org"
ruby "3.3.0"
gem "rspec"
gem "rubocop"
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "ruby"
    assert component.version == "3.3.0"
    assert component.lint_command == "bundle exec rubocop"


def test_php_composer(tmp_path: Path) -> None:
    write(
        tmp_path / "composer.json",
        """{"require": {"php": "^8.3"}, "scripts": {"test": "phpunit"}}""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "php"
    assert component.version == "^8.3"
    assert component.test_command == "composer test"


def test_elixir_mix(tmp_path: Path) -> None:
    write(
        tmp_path / "mix.exs",
        """defmodule Demo.MixProject do
  use Mix.Project
  def project, do: [app: :demo, elixir: "~> 1.16"]
end
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.language == "elixir"
    assert component.version == "~> 1.16"


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


def test_cargo_workspace_with_members(tmp_path: Path) -> None:
    write(tmp_path / "Cargo.toml", '[workspace]\nmembers = ["core", "cli"]\n')
    write(tmp_path / "core" / "Cargo.toml", '[package]\nname = "core"\nedition = "2024"\n')
    write(tmp_path / "cli" / "Cargo.toml", '[package]\nname = "cli"\nedition = "2024"\n')
    profile = discover_stack(tmp_path)
    paths = {c.path for c in profile.components}
    assert paths == {"core", "cli"}
    assert profile.workspace_kind == "cargo"
    assert profile.monorepo is True
    core = component_at(profile, "core")
    assert core.test_command == "(cd core && cargo test)"


def test_pnpm_workspace(tmp_path: Path) -> None:
    write(
        tmp_path / "package.json",
        '{"name": "root", "packageManager": "pnpm@9", "pnpm": {"workspaces": ["packages/*"]}}',
    )
    write(
        tmp_path / "packages" / "app" / "package.json",
        '{"name": "app", "scripts": {"test": "vitest"}}',
    )
    write(
        tmp_path / "packages" / "lib" / "package.json",
        '{"name": "lib", "scripts": {"test": "vitest"}}',
    )
    profile = discover_stack(tmp_path)
    paths = {c.path for c in profile.components if c.path != "."}
    assert paths == {"packages/app", "packages/lib"}
    assert profile.workspace_kind == "pnpm"
    app = component_at(profile, "packages/app")
    assert app.test_command == "(cd packages/app && npm run test)"


def test_uv_workspace(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        """[tool.uv.workspace]
members = ["services/api", "packages/lib"]
""",
    )
    write(
        tmp_path / "services" / "api" / "pyproject.toml",
        """[project]
name = "api"
requires-python = ">=3.14"

[tool.uv]
""",
    )
    write(
        tmp_path / "packages" / "lib" / "pyproject.toml",
        """[project]
name = "lib"
requires-python = ">=3.14"

[tool.uv]
""",
    )
    profile = discover_stack(tmp_path)
    paths = {c.path for c in profile.components}
    assert paths == {"services/api", "packages/lib"}
    assert profile.workspace_kind == "uv"


def test_go_work_workspace(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/root\n\ngo 1.23\n")
    write(tmp_path / "services" / "api" / "go.mod", "module example.com/api\n\ngo 1.23\n")
    profile = discover_stack(tmp_path)
    paths = {c.path for c in profile.components}
    assert paths == {".", "services/api"}
    assert profile.monorepo is True


# ---------------------------------------------------------------------------
# Polyglot without a workspace declaration
# ---------------------------------------------------------------------------


def test_polyglot_python_backend_typescript_frontend(tmp_path: Path) -> None:
    write(
        tmp_path / "services" / "api" / "pyproject.toml",
        '[project]\nname = "api"\nrequires-python = ">=3.14"\n[tool.uv]\n',
    )
    write(
        tmp_path / "services" / "web" / "package.json",
        '{"name": "web", "scripts": {"test": "vitest"}}',
    )
    profile = discover_stack(tmp_path)
    languages = {c.language for c in profile.components}
    assert languages == {"python", "node"}
    assert profile.polyglot is True
    assert profile.monorepo is True
    assert profile.workspace_kind is None
    assert profile.primary_language is None


def test_polyglot_rust_core_python_bindings(tmp_path: Path) -> None:
    write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "core"\nedition = "2024"\nrust-version = "1.83"\n',
    )
    write(
        tmp_path / "bindings" / "pyproject.toml",
        '[project]\nname = "core-py"\nrequires-python = ">=3.12"\n',
    )
    profile = discover_stack(tmp_path)
    languages = {c.language for c in profile.components}
    assert languages == {"rust", "python"}
    assert profile.primary_language == "rust"


# ---------------------------------------------------------------------------
# Manifest-less repo + corruption
# ---------------------------------------------------------------------------


def test_no_manifest_returns_empty_with_note(tmp_path: Path) -> None:
    write(tmp_path / "README.md", "# Just docs\n")
    profile = discover_stack(tmp_path)
    assert profile.components == ()
    assert profile.primary_language is None
    assert any("no recognized language manifest" in note for note in profile.notes)


def test_invalid_pyproject_toml_emits_note(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", "[project\nbroken")
    profile = discover_stack(tmp_path)
    assert profile.components == ()
    assert any("invalid TOML" in note for note in profile.notes)


def test_manifest_cap_truncates(tmp_path: Path) -> None:
    for i in range(MAX_MANIFESTS + 5):
        write(
            tmp_path / f"pkg{i:03d}" / "pyproject.toml",
            f'[project]\nname = "p{i}"\nrequires-python = ">=3.14"\n',
        )
    profile = discover_stack(tmp_path)
    assert len(profile.components) == MAX_MANIFESTS
    assert any("manifest cap reached" in note for note in profile.notes)


def test_skip_dirs_are_excluded(tmp_path: Path) -> None:
    write(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nrequires-python = ">=3.14"\n')
    write(tmp_path / "node_modules" / "pkg" / "package.json", '{"name": "should-skip"}')
    write(tmp_path / ".venv" / "lib" / "pyproject.toml", '[project]\nname = "skip"\n')
    profile = discover_stack(tmp_path)
    assert {c.path for c in profile.components} == {"."}


# ---------------------------------------------------------------------------
# Makefile fallback + tool-version pinning
# ---------------------------------------------------------------------------


def test_makefile_fills_missing_root_commands(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(tmp_path / "Makefile", "test:\n\tgo test ./...\n\nlint:\n\tgolangci-lint run\n")
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.lint_command == "make lint"


def test_makefile_does_not_override_uv_scripts(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        """[project]
name = "demo"
requires-python = ">=3.14"

[tool.uv.scripts]
test = "pytest -x"
""",
    )
    write(tmp_path / "Makefile", "test:\n\tpython -m unittest\n")
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.test_command == "pytest -x"


def test_parse_makefile_targets_filters_to_known_slots() -> None:
    text = "test:\n\techo t\nbuild:\n\techo b\nrelease:\n\techo r\nlint:\n\techo l\n"
    targets = parse_makefile_targets(text)
    assert targets == {"test", "build", "lint"}


def test_tool_versions_file_is_read(tmp_path: Path) -> None:
    write(tmp_path / ".tool-versions", "python 3.14.0\nrust 1.83.0  # pinned\n")
    write(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nrequires-python = ">=3.14"\n')
    profile = discover_stack(tmp_path)
    assert profile.tool_versions == {"python": "3.14.0", "rust": "1.83.0"}


def test_mise_toml_is_read(tmp_path: Path) -> None:
    write(tmp_path / "mise.toml", '[tools]\nnode = "22"\ngo = "1.23"\n')
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    profile = discover_stack(tmp_path)
    assert profile.tool_versions == {"node": "22", "go": "1.23"}


# ---------------------------------------------------------------------------
# CI workflow fallback
# ---------------------------------------------------------------------------


def test_ci_workflow_fills_missing_test_command(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """name: ci
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: golangci-lint run ./...
  build:
    runs-on: ubuntu-latest
    steps:
      - run: go build ./...
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.lint_command == "golangci-lint run ./..."
    assert profile.ci_provider == "github-actions"
    assert len(profile.ci_jobs) == 2


def test_ci_workflow_does_not_override_uv_scripts(tmp_path: Path) -> None:
    write(
        tmp_path / "pyproject.toml",
        """[project]
name = "demo"
requires-python = ">=3.14"

[tool.uv.scripts]
test = "pytest -x"
""",
    )
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: python -m unittest
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.test_command == "pytest -x"


def test_layering_makefile_beats_ci_beats_conventions(tmp_path: Path) -> None:
    """Makefile takes precedence over CI; CI takes precedence over conventions."""
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(tmp_path / "Makefile", "lint:\n\tgolangci-lint run\n")
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: gotestsum --format testname
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.lint_command == "make lint"
    assert component.test_command == "gotestsum --format testname"
    assert component.build_command == "go build ./..."


def test_ci_workflow_skips_multiline_run_blocks(tmp_path: Path) -> None:
    """Multi-line ``run: |`` scripts are recorded but not used as fallbacks."""
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """name: ci
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: |
          if [ -d agents ]; then
            echo "build=true" >> "$GITHUB_OUTPUT"
          fi
      - run: go build ./...
""",
    )
    profile = discover_stack(tmp_path)
    component = component_at(profile, ".")
    assert component.build_command == "go build ./..."


def test_ci_workflow_with_invalid_yaml_emits_note(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\njobs: [broken")
    profile = discover_stack(tmp_path)
    assert any("invalid YAML" in note for note in profile.notes)


# ---------------------------------------------------------------------------
# Containerized detection + rendering
# ---------------------------------------------------------------------------


def test_dockerfile_present_sets_containerized(tmp_path: Path) -> None:
    write(tmp_path / "Dockerfile", "FROM scratch\n")
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    profile = discover_stack(tmp_path)
    assert profile.containerized is True


def test_render_stack_profile_summarises_polyglot(tmp_path: Path) -> None:
    write(
        tmp_path / "Cargo.toml",
        '[package]\nname = "core"\nedition = "2024"\nrust-version = "1.83"\n',
    )
    write(
        tmp_path / "bindings" / "pyproject.toml",
        '[project]\nname = "py"\nrequires-python = ">=3.12"\n',
    )
    rendered = render_stack_profile(discover_stack(tmp_path))
    assert "# Stack profile" in rendered
    assert "polyglot" in rendered
    assert "## rust at root" in rendered
    assert "## python at `bindings`" in rendered


def test_render_stack_profile_handles_empty_profile() -> None:
    rendered = render_stack_profile(StackProfile())
    assert "No language manifests were detected." in rendered


def test_render_stack_profile_lists_ci_jobs(tmp_path: Path) -> None:
    write(tmp_path / "go.mod", "module example.com/demo\n\ngo 1.23\n")
    write(
        tmp_path / ".github" / "workflows" / "ci.yml",
        """name: ci
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: go test ./...
""",
    )
    rendered = render_stack_profile(discover_stack(tmp_path))
    assert "## CI jobs (github-actions)" in rendered
    assert "- `test`" in rendered
