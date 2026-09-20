"""CI sharding must cover every test module exactly once and stay deterministic."""

import runpy
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_SCRIPT = Path(__file__).parents[2] / ".github/scripts/shard_tests.py"
_NAMESPACE = runpy.run_path(str(_SCRIPT))

discover = cast("Callable[[Path], list[tuple[str, int]]]", _NAMESPACE["discover"])
select = cast("Callable[[list[tuple[str, int]], int, int], list[str]]", _NAMESPACE["select"])

_SAMPLE = [("tests/test_a.py", 30), ("tests/test_b.py", 20), ("tests/test_c.py", 10), ("tests/test_d.py", 5)]


def test_every_module_is_assigned_to_exactly_one_shard() -> None:
    assigned = [name for group in (1, 2, 3) for name in select(_SAMPLE, 3, group)]
    assert sorted(assigned) == sorted(name for name, _ in _SAMPLE)
    assert len(assigned) == len(set(assigned))


def test_assignment_is_deterministic_and_balanced() -> None:
    first = [select(_SAMPLE, 2, group) for group in (1, 2)]
    assert first == [select(_SAMPLE, 2, group) for group in (1, 2)]
    weights = [sum(dict(_SAMPLE)[name] for name in group) for group in first]
    assert max(weights) - min(weights) <= 20


def test_discovery_covers_the_real_suite() -> None:
    files = discover(Path(__file__).parents[2] / "tests")
    assert len(files) >= 70
    assert all(count > 0 for _, count in files)
    assert len({name for name, _ in files}) == len(files)


@pytest.mark.parametrize(("shards", "group"), [(1, 0), (2, 3), (0, 1)])
def test_invalid_shard_selections_are_rejected(shards: int, group: int) -> None:
    with pytest.raises(ValueError):
        select(_SAMPLE, shards, group)
