"""Polyglot stack discovery for ai-dlc target repos.

Given a path to a cloned repo, identify language(s), package managers,
test/build/lint/format commands, and whether it's a workspace or polyglot
monorepo. Output is a :class:`StackProfile` that:

* Drives the Architect's grounding context (rendered Markdown summary).
* Tells the Reviewer / Tester which command to run when ``MEMORY.md`` is
  thin or silent.
* Lets adopters see exactly what the platform detected on their repo.

Walk depth is bounded; manifest count is capped; per-file reads are
size-limited so a hostile or massive repo can't exhaust the agent.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

MAX_WALK_DEPTH: Final = 4
MAX_MANIFESTS: Final = 50
MAX_FILE_BYTES: Final = 64 * 1024
TOOL_VERSIONS_MIN_FIELDS: Final = 2

SKIP_DIRS: Final = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "target",
        "dist",
        "build",
        "out",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".cargo",
        "vendor",
        ".terraform",
        ".gradle",
        ".idea",
        ".vscode",
    },
)

MANIFEST_NAMES: Final = (
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "mix.exs",
)


class StackComponent(BaseModel):
    """One manifest-rooted subtree (a deployable package or workspace member)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str
    language: str
    version: str | None = None
    package_manager: str
    manifest: str
    test_command: str | None = None
    build_command: str | None = None
    lint_command: str | None = None
    format_command: str | None = None


class CIJob(BaseModel):
    """One job parsed from a GitHub Actions workflow file."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    workflow: str
    job_id: str
    name: str | None = None
    run_commands: tuple[str, ...] = ()


class StackProfile(BaseModel):
    """Repo-wide snapshot of detected languages and how to build/test them."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    components: tuple[StackComponent, ...] = ()
    primary_language: str | None = None
    workspace_kind: str | None = None
    monorepo: bool = False
    polyglot: bool = False
    containerized: bool = False
    ci_provider: str | None = None
    ci_jobs: tuple[CIJob, ...] = ()
    tool_versions: dict[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()


def discover_stack(repo_path: Path) -> StackProfile:
    """Walk ``repo_path`` and return a :class:`StackProfile`.

    Returns an empty profile (with a single ``notes`` entry) when no
    recognized manifest is found.

    Args:
        repo_path: Root of a cloned repo on the local filesystem.

    Returns:
        A frozen :class:`StackProfile` describing every detected component.
    """
    repo_path = repo_path.resolve()
    manifests = list(walk_manifests(repo_path))
    notes: list[str] = []
    if len(manifests) > MAX_MANIFESTS:
        notes.append(
            f"manifest cap reached: only the first {MAX_MANIFESTS} of "
            f"{len(manifests)} manifests were analyzed",
        )
        manifests = manifests[:MAX_MANIFESTS]

    components: list[StackComponent] = []
    workspace_kind: str | None = None
    for manifest_path, manifest_name in manifests:
        component, kind, manifest_notes = analyze_manifest(repo_path, manifest_path, manifest_name)
        if component is not None:
            components.append(component)
        if kind is not None and workspace_kind is None:
            workspace_kind = kind
        notes.extend(manifest_notes)

    apply_makefile_targets(repo_path, components, notes)
    ci_provider, ci_jobs, ci_notes = read_ci_jobs(repo_path)
    notes.extend(ci_notes)
    apply_ci_fallback_commands(components, ci_jobs)
    apply_language_conventions(components)

    if not components:
        notes.append("no recognized language manifest found within depth 4")

    languages = {c.language for c in components}
    return StackProfile(
        components=tuple(components),
        primary_language=pick_primary_language(components),
        workspace_kind=workspace_kind,
        monorepo=workspace_kind is not None or len(components) > 1,
        polyglot=len(languages) > 1,
        containerized=any_dockerfile_present(repo_path),
        ci_provider=ci_provider,
        ci_jobs=tuple(ci_jobs),
        tool_versions=read_tool_versions(repo_path),
        notes=tuple(notes),
    )


def walk_manifests(repo_path: Path) -> list[tuple[Path, str]]:
    """Walk ``repo_path`` and yield ``(manifest_path, manifest_name)`` pairs.

    Walks up to :data:`MAX_WALK_DEPTH` directories deep, skips known
    cache / build / vendored dirs, and prefers ``pyproject.toml`` when
    both ``pyproject.toml`` and ``setup.py`` exist in the same directory.

    Ordering is deterministic: directories sorted by name, manifests in
    :data:`MANIFEST_NAMES` declaration order. Caller may slice for safety.
    """
    found: list[tuple[Path, str]] = []
    queue: list[tuple[Path, int]] = [(repo_path, 0)]
    while queue:
        current, depth = queue.pop(0)
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        files = {entry.name for entry in entries if entry.is_file()}
        for name in MANIFEST_NAMES:
            if name in files:
                found.append((current / name, name))
        if depth >= MAX_WALK_DEPTH:
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink() and entry.name not in SKIP_DIRS:
                queue.append((entry, depth + 1))
    return found


def analyze_manifest(
    repo_path: Path,
    manifest_path: Path,
    manifest_name: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Dispatch one manifest to its language-specific detector."""
    rel_dir = relative_dir(repo_path, manifest_path)
    text = read_capped(manifest_path)
    if text is None:
        return None, None, [f"{rel_dir}/{manifest_name}: read failed or empty"]
    detectors = {
        "pyproject.toml": detect_python,
        "package.json": detect_node,
        "Cargo.toml": detect_rust,
        "go.mod": detect_go,
        "pom.xml": detect_java_maven,
        "build.gradle": detect_java_gradle,
        "build.gradle.kts": detect_java_gradle,
        "Gemfile": detect_ruby,
        "composer.json": detect_php,
        "mix.exs": detect_elixir,
    }
    detector = detectors[manifest_name]
    return detector(rel_dir, text)


def detect_python(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Python component from a ``pyproject.toml`` body."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, None, [f"{rel_dir}/pyproject.toml: invalid TOML ({exc.args[0]})"]
    tool = data.get("tool", {})
    workspace_kind: str | None = "uv" if "workspace" in tool.get("uv", {}) else None
    if "project" not in data and workspace_kind is None:
        return None, None, [f"{rel_dir}/pyproject.toml: no [project] table (PEP 621 missing)"]
    if "project" not in data:
        return None, workspace_kind, [f"{rel_dir}/pyproject.toml: workspace coordinator only"]
    project = data["project"]
    version = project.get("requires-python")
    package_manager = "uv" if "uv" in tool else "pip"
    cd = scoped_prefix(rel_dir)
    scripts_obj = tool.get("uv", {})
    scripts = scripts_obj.get("scripts", {}) if isinstance(scripts_obj, dict) else {}
    if not isinstance(scripts, dict):
        scripts = {}
    component = StackComponent(
        path=rel_dir,
        language="python",
        version=normalize_version(version),
        package_manager=package_manager,
        manifest="pyproject.toml",
        test_command=prefix(cd, script_str(scripts, "test")),
        build_command=prefix(cd, script_str(scripts, "build")),
        lint_command=prefix(cd, script_str(scripts, "lint")),
        format_command=prefix(cd, script_str(scripts, "fmt") or script_str(scripts, "format")),
    )
    return component, workspace_kind, []


def script_str(scripts: dict[object, object], key: str) -> str | None:
    """Return ``scripts[key]`` when it's a string, else ``None``."""
    value = scripts.get(key)
    return value if isinstance(value, str) else None


def detect_node(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Node/TypeScript component from a ``package.json`` body."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, None, [f"{rel_dir}/package.json: invalid JSON ({exc.msg})"]
    workspaces = data.get("workspaces") or data.get("pnpm", {}).get("workspaces")
    workspace_kind = node_workspace_kind(data) if workspaces else None
    scripts = data.get("scripts", {}) or {}
    if not scripts and "name" not in data and workspace_kind is None:
        return None, None, [f"{rel_dir}/package.json: no scripts or name field"]
    package_manager = parse_package_manager(data) or "npm"
    if workspace_kind is not None and not scripts:
        return None, workspace_kind, [f"{rel_dir}/package.json: workspace coordinator only"]
    cd = scoped_prefix(rel_dir)
    engines = data.get("engines", {}) or {}
    runner = package_manager
    component = StackComponent(
        path=rel_dir,
        language="node",
        version=engines.get("node"),
        package_manager=package_manager,
        manifest="package.json",
        test_command=prefix(cd, script_command(runner, scripts, "test")),
        build_command=prefix(cd, script_command(runner, scripts, "build")),
        lint_command=prefix(cd, script_command(runner, scripts, "lint")),
        format_command=prefix(cd, script_command(runner, scripts, "format")),
    )
    return component, workspace_kind, []


def node_workspace_kind(data: dict[str, object]) -> str:
    """Return ``pnpm`` if pnpm workspaces are declared, else ``node``."""
    pnpm = data.get("pnpm")
    if isinstance(pnpm, dict) and "workspaces" in pnpm:
        return "pnpm"
    return "node"


def parse_package_manager(data: dict[str, object]) -> str | None:
    """Parse ``packageManager`` (Corepack format: ``pnpm@9.0.0``)."""
    raw = data.get("packageManager")
    if not isinstance(raw, str):
        return None
    name = raw.split("@", 1)[0].strip()
    return name or None


def script_command(runner: str, scripts: dict[str, object], name: str) -> str | None:
    """Return ``<runner> run <name>`` when the named script exists."""
    if name in scripts and isinstance(scripts[name], str):
        return f"{runner} run {name}"
    return None


def detect_rust(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Rust component from a ``Cargo.toml`` body.

    Cargo.toml carries no explicit task commands — leave every slot
    empty here and let :func:`apply_language_conventions` fill them.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, None, [f"{rel_dir}/Cargo.toml: invalid TOML ({exc.args[0]})"]
    workspace_kind = "cargo" if "workspace" in data else None
    if "package" not in data:
        notes = [f"{rel_dir}/Cargo.toml: workspace coordinator only"] if workspace_kind else []
        return None, workspace_kind, notes
    pkg = data["package"]
    version = pkg.get("rust-version") or pkg.get("edition") if isinstance(pkg, dict) else None
    component = StackComponent(
        path=rel_dir,
        language="rust",
        version=normalize_version(version),
        package_manager="cargo",
        manifest="Cargo.toml",
    )
    return component, workspace_kind, []


def detect_go(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Go component from a ``go.mod`` body."""
    match = re.search(r"^go\s+(\S+)", text, re.MULTILINE)
    version = match.group(1) if match else None
    component = StackComponent(
        path=rel_dir,
        language="go",
        version=version,
        package_manager="go",
        manifest="go.mod",
    )
    return component, None, []


def detect_java_maven(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Java component from a ``pom.xml`` body (Maven)."""
    workspace_kind = "maven" if "<modules>" in text else None
    version_match = re.search(r"<maven\.compiler\.release>([^<]+)</", text)
    version = version_match.group(1).strip() if version_match else None
    component = StackComponent(
        path=rel_dir,
        language="java",
        version=version,
        package_manager="maven",
        manifest="pom.xml",
    )
    return component, workspace_kind, []


def detect_java_gradle(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Java/Kotlin component from a Gradle build file."""
    del text
    component = StackComponent(
        path=rel_dir,
        language="java",
        version=None,
        package_manager="gradle",
        manifest="build.gradle",
    )
    return component, None, []


def detect_ruby(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a Ruby component from a ``Gemfile`` body."""
    match = re.search(r"^ruby\s+['\"]([^'\"]+)['\"]", text, re.MULTILINE)
    version = match.group(1) if match else None
    lower = text.lower()
    cd = scoped_prefix(rel_dir)
    lint_command = prefix(cd, "bundle exec rubocop") if "rubocop" in lower else None
    component = StackComponent(
        path=rel_dir,
        language="ruby",
        version=version,
        package_manager="bundler",
        manifest="Gemfile",
        lint_command=lint_command,
    )
    return component, None, []


def detect_php(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect a PHP component from a ``composer.json`` body."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, None, [f"{rel_dir}/composer.json: invalid JSON ({exc.msg})"]
    require = data.get("require", {}) or {}
    version = require.get("php") if isinstance(require, dict) else None
    raw_scripts = data.get("scripts", {})
    scripts: dict[object, object] = raw_scripts if isinstance(raw_scripts, dict) else {}
    cd = scoped_prefix(rel_dir)
    component = StackComponent(
        path=rel_dir,
        language="php",
        version=version,
        package_manager="composer",
        manifest="composer.json",
        test_command=prefix(cd, "composer test") if "test" in scripts else None,
        lint_command=prefix(cd, "composer lint") if "lint" in scripts else None,
    )
    return component, None, []


def detect_elixir(
    rel_dir: str,
    text: str,
) -> tuple[StackComponent | None, str | None, list[str]]:
    """Detect an Elixir component from a ``mix.exs`` body."""
    match = re.search(r"elixir:\s*['\"]([^'\"]+)['\"]", text)
    version = match.group(1) if match else None
    workspace_kind = "mix-umbrella" if "apps_path:" in text else None
    cd = scoped_prefix(rel_dir)
    lint_command = prefix(cd, "mix credo") if "credo" in text.lower() else None
    component = StackComponent(
        path=rel_dir,
        language="elixir",
        version=version,
        package_manager="mix",
        manifest="mix.exs",
        lint_command=lint_command,
    )
    return component, workspace_kind, []


def apply_makefile_targets(
    repo_path: Path,
    components: list[StackComponent],
    notes: list[str],
) -> None:
    """Override component commands with Makefile targets when present.

    A ``Makefile`` with ``test:`` / ``build:`` / ``lint:`` / ``fmt:`` targets
    is a strong project-level signal. When a component currently has no
    command for a slot, fill it with ``make <target>`` (scoped to that
    component's dir). When it already has a command, leave it — explicit
    component manifests beat repo-wide make targets.
    """
    makefile = repo_path / "Makefile"
    if not makefile.is_file():
        return
    text = read_capped(makefile)
    if text is None:
        return
    targets = parse_makefile_targets(text)
    if not targets:
        return
    notes.append(f"Makefile targets detected: {', '.join(sorted(targets))}")
    for idx, component in enumerate(components):
        if component.path != ".":
            continue
        components[idx] = component.model_copy(
            update={
                slot: command_for_slot(component, slot, targets)
                for slot in ("test_command", "build_command", "lint_command", "format_command")
            },
        )


def command_for_slot(
    component: StackComponent,
    slot: str,
    targets: set[str],
) -> str | None:
    """Return the existing command, or ``make <target>`` when the slot is empty."""
    current = getattr(component, slot)
    if current is not None:
        return current
    target_map = {
        "test_command": "test",
        "build_command": "build",
        "lint_command": "lint",
        "format_command": "fmt",
    }
    target = target_map[slot]
    if target in targets or (target == "fmt" and "format" in targets):
        return f"make {target if target in targets else 'format'}"
    return None


def parse_makefile_targets(text: str) -> set[str]:
    """Return the set of top-level Makefile targets we care about."""
    wanted = {"test", "build", "lint", "fmt", "format"}
    found: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:", line)
        if match and match.group(1) in wanted:
            found.add(match.group(1))
    return found


def read_ci_jobs(repo_path: Path) -> tuple[str | None, list[CIJob], list[str]]:
    """Parse ``.github/workflows/*.yml`` and return ``(provider, jobs, notes)``.

    GitHub Actions only for v1 — other providers (GitLab, CircleCI) are out
    of scope. Returns ``(None, [], [])`` when no workflows exist.

    Each ``run:`` step in each job becomes one entry in ``run_commands``.
    Multi-line ``run: |`` block scalars are kept verbatim — agents can read
    them line by line. Steps without ``run:`` (``uses:`` actions, shell-less
    steps) are skipped.
    """
    workflows_dir = repo_path / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return None, [], []
    workflow_files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    if not workflow_files:
        return None, [], []
    jobs: list[CIJob] = []
    notes: list[str] = []
    for workflow_path in workflow_files:
        workflow_jobs, parse_notes = parse_workflow_file(repo_path, workflow_path)
        jobs.extend(workflow_jobs)
        notes.extend(parse_notes)
    return "github-actions", jobs, notes


def parse_workflow_file(
    repo_path: Path,
    workflow_path: Path,
) -> tuple[list[CIJob], list[str]]:
    """Parse one workflow YAML and return ``(jobs, notes)``."""
    rel = workflow_path.relative_to(repo_path).as_posix()
    text = read_capped(workflow_path)
    if text is None:
        return [], [f"{rel}: read failed"]
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [], [f"{rel}: invalid YAML ({exc!s})"]
    if not isinstance(data, dict):
        return [], [f"{rel}: top level is not a mapping"]
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, dict):
        return [], []
    jobs: list[CIJob] = []
    for job_id, raw_job in raw_jobs.items():
        if not isinstance(raw_job, dict) or not isinstance(job_id, str):
            continue
        run_commands = extract_run_commands(raw_job.get("steps"))
        if not run_commands:
            continue
        name = raw_job.get("name") if isinstance(raw_job.get("name"), str) else None
        jobs.append(
            CIJob(
                workflow=rel,
                job_id=job_id,
                name=name,
                run_commands=tuple(run_commands),
            ),
        )
    return jobs, []


def extract_run_commands(steps: object) -> list[str]:
    """Pull ``run:`` strings from a list of step mappings, in order."""
    if not isinstance(steps, list):
        return []
    commands: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        run = cast("Mapping[str, object]", step).get("run")
        if isinstance(run, str) and run.strip():
            commands.append(run.strip())
    return commands


def apply_ci_fallback_commands(
    components: list[StackComponent],
    ci_jobs: list[CIJob],
) -> None:
    """Fill empty component command slots from CI jobs with matching names.

    Match by ``job_id`` first (``test``, ``build``, ``lint``, ``fmt`` /
    ``format``), then by lower-cased ``name``. The first ``run:`` line in a
    matching job becomes the fallback command. Only applies to root-level
    components — sub-component CI mapping is unreliable without convention.
    """
    if not ci_jobs:
        return
    slot_to_match = {
        "test_command": ("test", "tests", "unit-tests", "check"),
        "build_command": ("build", "compile"),
        "lint_command": ("lint", "lints", "clippy"),
        "format_command": ("fmt", "format", "formatting"),
    }
    for idx, component in enumerate(components):
        if component.path != ".":
            continue
        updates: dict[str, str | None] = {}
        for slot, candidates in slot_to_match.items():
            if getattr(component, slot) is not None:
                continue
            fallback = first_ci_run(ci_jobs, candidates)
            if fallback is not None:
                updates[slot] = fallback
        if updates:
            components[idx] = component.model_copy(update=updates)


LANGUAGE_CONVENTIONS: Final[dict[str, dict[str, str]]] = {
    "python": {
        "test_command": "uv run pytest",
        "build_command": "uv build",
        "lint_command": "uv run ruff check",
        "format_command": "uv run ruff format",
    },
    "node": {},
    "rust": {
        "test_command": "cargo test",
        "build_command": "cargo build",
        "lint_command": "cargo clippy --all-targets --all-features -- -D warnings",
        "format_command": "cargo fmt",
    },
    "go": {
        "test_command": "go test ./...",
        "build_command": "go build ./...",
    },
    "java": {},
    "ruby": {
        "test_command": "bundle exec rspec",
        "build_command": "bundle install",
    },
    "php": {
        "build_command": "composer install",
    },
    "elixir": {
        "test_command": "mix test",
        "build_command": "mix compile",
        "format_command": "mix format",
    },
}

JAVA_MAVEN_CONVENTIONS: Final = {
    "test_command": "mvn test",
    "build_command": "mvn package",
}

JAVA_GRADLE_CONVENTIONS: Final = {
    "test_command": "./gradlew test",
    "build_command": "./gradlew build",
    "lint_command": "./gradlew check",
}


def apply_language_conventions(components: list[StackComponent]) -> None:
    """Fill remaining empty command slots with language-idiomatic defaults.

    Runs after manifest, Makefile, and CI passes. The earlier passes win;
    conventions are the final fallback so projects that wrap commands in
    Make or CI don't get overridden by generic guesses.

    Each command is scoped to the component's subtree via ``scoped_prefix``,
    so workspace members get the correct ``(cd <dir> && <cmd>)`` form.
    """
    for idx, component in enumerate(components):
        defaults = pick_convention_defaults(component)
        if not defaults:
            continue
        cd = scoped_prefix(component.path)
        updates: dict[str, str | None] = {}
        for slot, command in defaults.items():
            if getattr(component, slot) is None:
                updates[slot] = prefix(cd, command)
        if updates:
            components[idx] = component.model_copy(update=updates)


def pick_convention_defaults(component: StackComponent) -> dict[str, str]:
    """Return the convention table for the component's language + manager."""
    if component.language == "java" and component.package_manager == "maven":
        return JAVA_MAVEN_CONVENTIONS
    if component.language == "java" and component.package_manager == "gradle":
        return JAVA_GRADLE_CONVENTIONS
    return LANGUAGE_CONVENTIONS.get(component.language, {})


def first_ci_run(ci_jobs: list[CIJob], candidates: tuple[str, ...]) -> str | None:
    """Return the first single-line ``run:`` command from a matching job.

    Multi-line ``run: |`` block scalars are shell scripts (often guards,
    diagnostics, or conditional dispatch logic) — useful to record on the
    profile but not usable as a fallback command. Skip those.
    """
    candidate_set = set(candidates)
    for job in ci_jobs:
        identifiers = {job.job_id.lower()}
        if job.name:
            identifiers.add(job.name.lower())
        if not (identifiers & candidate_set):
            continue
        for command in job.run_commands:
            if "\n" not in command:
                return command
    return None


def pick_primary_language(components: list[StackComponent]) -> str | None:
    """Return the dominant language by count + root-level weighting."""
    if not components:
        return None
    scores: dict[str, float] = {}
    for component in components:
        weight = 2.0 if component.path == "." else 1.0
        scores[component.language] = scores.get(component.language, 0.0) + weight
    top = max(scores.values())
    winners = [language for language, score in scores.items() if score == top]
    if len(winners) > 1:
        return None
    return winners[0]


def read_tool_versions(repo_path: Path) -> dict[str, str]:
    """Parse ``.tool-versions`` and ``mise.toml`` at the repo root."""
    versions: dict[str, str] = {}
    versions.update(parse_tool_versions_file(repo_path / ".tool-versions"))
    versions.update(parse_mise_toml(repo_path / "mise.toml"))
    return versions


def parse_tool_versions_file(path: Path) -> dict[str, str]:
    """Parse the ``asdf``/``mise``-style ``.tool-versions`` file."""
    if not path.is_file():
        return {}
    text = read_capped(path)
    if not text:
        return {}
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= TOOL_VERSIONS_MIN_FIELDS:
            result[parts[0]] = parts[1]
    return result


def parse_mise_toml(path: Path) -> dict[str, str]:
    """Parse a ``mise.toml`` ``[tools]`` table."""
    if not path.is_file():
        return {}
    text = read_capped(path)
    if not text:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    tools = data.get("tools", {})
    if not isinstance(tools, dict):
        return {}
    return {name: value for name, value in tools.items() if isinstance(value, str)}


def any_dockerfile_present(repo_path: Path) -> bool:
    """Return True if any ``Dockerfile`` exists within the walked tree."""
    for current, depth in walk_dirs(repo_path):
        if depth > MAX_WALK_DEPTH:
            continue
        if (current / "Dockerfile").is_file():
            return True
    return False


def walk_dirs(repo_path: Path) -> list[tuple[Path, int]]:
    """Iterative dir walk that respects :data:`SKIP_DIRS` and depth limit."""
    seen: list[tuple[Path, int]] = []
    queue: list[tuple[Path, int]] = [(repo_path, 0)]
    while queue:
        current, depth = queue.pop(0)
        seen.append((current, depth))
        if depth >= MAX_WALK_DEPTH:
            continue
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink() and entry.name not in SKIP_DIRS:
                queue.append((entry, depth + 1))
    return seen


def relative_dir(repo_path: Path, manifest_path: Path) -> str:
    """Return the POSIX-style relative dir of ``manifest_path``."""
    rel = manifest_path.parent.relative_to(repo_path)
    posix = rel.as_posix()
    return "." if posix in {"", "."} else posix


def scoped_prefix(rel_dir: str) -> str:
    """Return ``""`` for the root or ``(cd <rel_dir> && )`` otherwise."""
    if rel_dir == ".":
        return ""
    return f"(cd {rel_dir} && "


def prefix(cd: str, command: str | None) -> str | None:
    """Wrap ``command`` in the ``cd`` prefix, closing the subshell as needed."""
    if command is None:
        return None
    if not cd:
        return command
    return f"{cd}{command})"


def normalize_version(value: object) -> str | None:
    """Coerce a manifest version field to a stripped string or ``None``."""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)


def read_capped(path: Path) -> str | None:
    """Read up to :data:`MAX_FILE_BYTES` from ``path``; ``None`` on error."""
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES)
    except OSError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def render_stack_profile(profile: StackProfile) -> str:
    """Render a :class:`StackProfile` as a compact Markdown block.

    Output is bounded — one section per component, two extra summary
    sections, no headers deeper than ``###``. Designed to drop into a
    system prompt under ``<stack_profile>``.
    """
    lines: list[str] = ["# Stack profile"]
    summary = render_profile_summary(profile)
    if summary:
        lines.append(summary)
    if not profile.components:
        lines.append("\nNo language manifests were detected.")
    for component in profile.components:
        lines.append("")
        lines.append(render_component(component))
    lines.extend(render_profile_extras(profile))
    return "\n".join(lines).rstrip() + "\n"


def render_profile_extras(profile: StackProfile) -> list[str]:
    """Render the tool-versions, CI, and notes blocks (empty if absent)."""
    lines: list[str] = []
    if profile.tool_versions:
        lines.append("")
        lines.append("## Pinned tool versions")
        for name, version in sorted(profile.tool_versions.items()):
            lines.append(f"- `{name}` = `{version}`")
    if profile.ci_jobs:
        lines.append("")
        lines.append(f"## CI jobs ({profile.ci_provider})")
        for job in profile.ci_jobs:
            label = job.name or job.job_id
            lines.append(f"- `{job.job_id}` ({label}) — {len(job.run_commands)} step(s)")
    if profile.notes:
        lines.append("")
        lines.append("## Notes")
        for note in profile.notes:
            lines.append(f"- {note}")
    return lines


def render_profile_summary(profile: StackProfile) -> str:
    """One-paragraph header summarizing the profile's top-level fields."""
    parts: list[str] = []
    if profile.primary_language:
        parts.append(f"primary language: **{profile.primary_language}**")
    if profile.polyglot:
        parts.append("polyglot")
    if profile.workspace_kind:
        parts.append(f"{profile.workspace_kind} workspace")
    elif profile.monorepo:
        parts.append("multi-component repo")
    if profile.containerized:
        parts.append("containerized (Dockerfile present)")
    return "\n" + ", ".join(parts) + "." if parts else ""


def render_component(component: StackComponent) -> str:
    """Render one component as a Markdown subsection."""
    path_label = "root" if component.path == "." else f"`{component.path}`"
    header = f"## {component.language} at {path_label}"
    rows: list[str] = [header]
    rows.append(f"- manifest: `{component.manifest}`")
    rows.append(f"- package manager: `{component.package_manager}`")
    if component.version:
        rows.append(f"- version: `{component.version}`")
    for label, value in (
        ("test", component.test_command),
        ("build", component.build_command),
        ("lint", component.lint_command),
        ("format", component.format_command),
    ):
        if value:
            rows.append(f"- {label}: `{value}`")
    return "\n".join(rows)
