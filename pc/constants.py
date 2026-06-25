# Purpose: to define constants for the project
# Code will be executed when the module is imported

import tomllib
from pathlib import Path

# get the project root path
project_root_path = Path(__file__).parents[1]

# default config path
PYPROJECT_PATH = project_root_path / "pyproject.toml"


def get_project_version() -> str:
    with open(PYPROJECT_PATH, "rb") as f:
        pyproject_data = tomllib.load(f)
    return pyproject_data["project"]["version"]
