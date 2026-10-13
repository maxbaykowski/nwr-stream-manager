from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


PACKAGE_NAME = "nwr-stream-manager"
UNKNOWN_VERSION = "0.0.0"


def package_version() -> str:
    # A source checkout's pyproject.toml is newer than any copy installed alongside it.
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        if project.get("name") == PACKAGE_NAME:
            return str(project["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        pass
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION
