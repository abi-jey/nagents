"""Feature release numbering must stay prerelease-only and unique on reruns."""

import runpy
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

alpha_version = cast(
    "Callable[[str, int, int], str]",
    runpy.run_path(str(Path(__file__).parents[1] / ".github/scripts/alpha_version.py"))["alpha_version"],
)


def test_alpha_numbering_across_branches_and_reruns() -> None:
    assert alpha_version("0.6.0a0", 10, 1) == "0.6.0a1001"
    assert alpha_version("0.6.0a1001", 10, 2) == "0.6.0a1002"
    assert alpha_version("0.6.0", 11, 1) == "0.6.0a1101"


@pytest.mark.parametrize("version", ["", "0.6", "0.6.0.dev1", "0.6.0rc1", "0.6.0+local"])
def test_unsupported_release_bases_rejected(version: str) -> None:
    with pytest.raises(ValueError):
        alpha_version(version, 10, 1)


@pytest.mark.parametrize(("run", "attempt"), [(0, 1), (-1, 1), (1, 0), (1, 100), (True, 1), (1, True)])
def test_invalid_numbering_cannot_collide(run: int, attempt: int) -> None:
    with pytest.raises(ValueError):
        alpha_version("0.6.0", run, attempt)
