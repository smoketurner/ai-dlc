"""Shared Jinja2 environment factory for agents rendering Markdown artifacts.

Agents call ``make_template_env(__package__)`` to get a cached environment
that loads templates from their package's ``templates/`` directory.
"""

from __future__ import annotations

from functools import cache

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape


@cache
def make_template_env(package: str) -> Environment:
    """Cached Jinja environment loading templates from ``<package>/templates/``.

    Args:
        package: Importable package name (typically ``__package__``) whose
            ``templates/`` directory contains ``.md.j2`` files.

    Returns:
        A configured ``Environment`` with autoescape disabled for Markdown
        and ``StrictUndefined`` so missing variables fail loudly.
    """
    return Environment(
        loader=PackageLoader(package, "templates"),
        autoescape=select_autoescape(disabled_extensions=("md", "j2"), default=False),
        undefined=StrictUndefined,
    )
