"""The per-run adapter is retired; ngn runs the `nagents engine` server instead."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "ngn-nagents no longer runs agents. ngn now starts the Python engine "
        "(`nagents engine`); update ngn with `ngn update`.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
