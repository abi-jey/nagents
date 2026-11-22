"""Paths and deterministic writes shared by the v1 generation scripts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/engine/v1"


def write_or_check(path: Path, contents: str, *, check: bool) -> bool:
    if check:
        if not path.is_file() or path.read_text(encoding="utf-8") != contents:
            print(f"Stale generated file: {path.relative_to(ROOT)}")
            return False
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8", newline="\n")
    return True
