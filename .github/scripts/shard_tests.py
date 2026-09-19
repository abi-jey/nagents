"""Split tests/ into deterministic shards so CI can run them in parallel.

Files are balanced by test count (largest-first) so each shard takes roughly the
same wall-clock. Every test module is assigned to exactly one shard, so a newly
added file can never be silently skipped.
"""

import argparse
import re
from pathlib import Path

_TEST_DEFINITION = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_", re.MULTILINE)


def discover(root: Path) -> list[tuple[str, int]]:
    """Return (module path, test count) for every test module under root."""
    found: list[tuple[str, int]] = []
    for path in sorted(root.rglob("test_*.py")):
        found.append((path.as_posix(), len(_TEST_DEFINITION.findall(path.read_text(encoding="utf-8")))))
    return found


def assign(files: list[tuple[str, int]], shards: int) -> list[list[str]]:
    """Balance modules across shards, largest first, breaking ties by path."""
    if shards < 1:
        raise ValueError("shards must be positive")
    groups: list[list[str]] = [[] for _ in range(shards)]
    weights = [0] * shards
    for name, count in sorted(files, key=lambda item: (-item[1], item[0])):
        index = min(range(shards), key=lambda position: (weights[position], position))
        groups[index].append(name)
        weights[index] += count
    return [sorted(group) for group in groups]


def select(files: list[tuple[str, int]], shards: int, group: int) -> list[str]:
    """Return the module paths of one shard."""
    if not 1 <= group <= shards:
        raise ValueError("group must be between 1 and shards")
    return assign(files, shards)[group - 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--group", type=int, default=1)
    parser.add_argument("--root", type=Path, default=Path("tests"))
    args = parser.parse_args()
    try:
        names = select(discover(args.root), args.shards, args.group)
    except ValueError as error:
        parser.error(str(error))
    print(" ".join(names))


if __name__ == "__main__":
    main()
