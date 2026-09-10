"""Repo-root pytest shim so per-project suites stay isolated.

The ``agents`` and ``seed`` projects each own a ``tests`` package. When a
project's suite is run from the repository root with the documented command
(``uv run --project agents pytest -q`` / ``uv run --project seed pytest -q``),
pytest's rootdir is this directory and its default recursion discovers *both*
``tests`` packages. Because both are named ``tests`` and neither is an
installed package, that collides at collection time (two different
``tests/conftest.py`` files claim the same ``tests.conftest`` module).

``uv run`` activates exactly one project's virtualenv and exports
``VIRTUAL_ENV`` pointing at it (``.../agents/.venv`` or ``.../seed/.venv``).
We use that to collect only the active project's ``tests`` directory and
ignore the sibling's. Nothing here couples to either project's source; it only
decides which ``tests`` tree belongs to the venv currently running pytest.
"""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).parent.resolve()
_PROJECTS = ("agents", "seed")


def _active_project() -> str | None:
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv:
        return None
    venv_path = Path(venv).resolve()
    for project in _PROJECTS:
        if venv_path == (_ROOT / project / ".venv").resolve():
            return project
    return None


# Ignore every sibling project's ``tests`` tree so only the active project's
# suite is collected. When no known venv is active (VIRTUAL_ENV unset or
# unrecognised) we ignore nothing and let pytest behave normally.
_active = _active_project()
collect_ignore_glob: list[str] = []
if _active is not None:
    for project in _PROJECTS:
        if project != _active:
            collect_ignore_glob.append(f"{project}/tests/*")
            collect_ignore_glob.append(f"{project}/tests")
