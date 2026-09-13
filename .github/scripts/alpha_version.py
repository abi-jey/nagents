"""Stamp an alpha in the build checkout, without changing the feature branch."""

import os
import re
import tomllib
from pathlib import Path


def alpha_version(version: str, run_number: int, attempt: int) -> str:
    """Unique, monotonically ordered alpha IDs across this workflow's branches."""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:a\d+)?", version)
    if match is None or type(run_number) is not int or run_number < 1:
        raise ValueError("Expected a release/alpha base and positive workflow run number")
    if type(attempt) is not int or not 1 <= attempt <= 99:
        raise ValueError("Workflow attempt must be between 1 and 99")
    return f"{match[1]}a{run_number * 100 + attempt}"


def main() -> None:
    path = Path("pyproject.toml")
    source = path.read_text()
    version = str(tomllib.loads(source)["project"]["version"])
    stamped = alpha_version(version, int(os.environ["GITHUB_RUN_NUMBER"]), int(os.environ["GITHUB_RUN_ATTEMPT"]))
    source, replacements = re.subn(r'^version = "[^"]+"$', f'version = "{stamped}"', source, count=1, flags=re.M)
    if replacements != 1:
        raise ValueError("Expected one project version declaration")
    path.write_text(source)
    print(f"Building feature alpha {stamped} from {os.environ['GITHUB_SHA']}")


if __name__ == "__main__":
    main()
