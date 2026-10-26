"""Keep the core independent and make private cross-package couplings explicit."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from tests.support.hang_guard import HANG_GUARD

SOURCE = Path(__file__).resolve().parents[1] / "src" / "nagents"
PACKAGES = {path.name for path in SOURCE.iterdir() if (path / "__init__.py").is_file()}
HOSTS = ("nagents.harness", "nagents.web", "nagents.tui")


def module_name(path: Path) -> str:
    suffix = path.relative_to(SOURCE).with_suffix("").as_posix().replace("/", ".")
    return "nagents" if suffix == "__init__" else "nagents." + suffix.removesuffix(".__init__")


def owner(module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 and parts[1] in PACKAGES | {"cli"} else "nagents"


def imports(path: Path, *, annotations: bool) -> list[tuple[str, str]]:
    name = module_name(path)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: list[tuple[str, str]] = []

    class Visitor(ast.NodeVisitor):
        def visit_If(self, node: ast.If) -> None:
            check = node.test
            type_only = (isinstance(check, ast.Name) and check.id == "TYPE_CHECKING") or (
                isinstance(check, ast.Attribute) and check.attr == "TYPE_CHECKING"
            )
            if type_only and not annotations:
                for statement in node.orelse:
                    self.visit(statement)
            else:
                self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            found.extend((alias.name, "") for alias in node.names)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            target = (
                importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                if node.level
                else node.module or ""
            )
            found.extend((target, alias.name) for alias in node.names)

    Visitor().visit(ast.parse(path.read_text(encoding="utf-8")))
    return found


def private_edges() -> set[tuple[str, str, str]]:
    edges: set[tuple[str, str, str]] = set()
    for path in SOURCE.rglob("*.py"):
        source = module_name(path)
        for target, name in imports(path, annotations=True):
            private = name.startswith("_") or any(part.startswith("_") for part in target.split("."))
            if (target == "nagents" or target.startswith("nagents.")) and owner(source) != owner(target) and private:
                edges.add((source, target, name))
    return edges


def test_private_cross_package_imports_require_an_explicit_boundary_review() -> None:
    allowed = {
        tuple(edge)
        for edge in json.loads((Path(__file__).parent / "support/import_boundaries.json").read_text(encoding="utf-8"))
    }
    unexpected = private_edges() - allowed
    assert not unexpected, f"New private cross-package imports: {sorted(unexpected)}"


def test_core_has_no_runtime_imports_of_the_application_layers() -> None:
    unexpected: list[tuple[str, str]] = []
    for path in SOURCE.rglob("*.py"):
        source = module_name(path)
        if owner(source) != "nagents" or path.name in {"cli.py", "__main__.py"}:
            continue
        for target, name in imports(path, annotations=False):
            candidate = target + "." + name if target == "nagents" else target
            if any(candidate == host or candidate.startswith(host + ".") for host in HOSTS):
                unexpected.append((source, candidate))
    assert not unexpected, f"Application-layer imports in core: {unexpected}"


def test_live_package_and_low_level_helpers_do_not_load_routes_or_harness() -> None:
    script = """
import importlib
import sys
for name in ('nagents.web.live', 'nagents.web.live.output', 'nagents.web.live.loop_diagnostics'):
    importlib.import_module(name)
blocked = ('nagents.harness', 'nagents.tui', 'nagents.web.app', 'nagents.web.live.routes', 'fastapi')
assert not any(name == item or name.startswith(item + '.') for name in sys.modules for item in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=HANG_GUARD, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_route_public_imports_refer_to_the_same_definitions() -> None:
    from nagents.web import live
    from nagents.web.live import routes

    for name in live.__all__:
        assert getattr(live, name) is getattr(routes, name)


def test_settings_public_imports_refer_to_the_same_definitions() -> None:
    from nagents.web import settings
    from nagents.web.settings import preferences

    for name in settings.__all__:
        assert getattr(settings, name) is getattr(preferences, name)


def test_task_public_imports_refer_to_the_same_definitions() -> None:
    from nagents.web import tasks
    from nagents.web.tasks import routes

    for name in tasks.__all__:
        assert getattr(tasks, name) is getattr(routes, name)


def test_a2a_public_exports_still_resolve_after_server_moves() -> None:
    from importlib import import_module

    from nagents import a2a

    for name in a2a.__all__:
        implementation = import_module(f"nagents.a2a.{a2a._MODULES[name]}")
        assert getattr(a2a, name) is getattr(implementation, name)


def test_a2a_client_public_imports_refer_to_the_same_definitions() -> None:
    from nagents.a2a import client
    from nagents.a2a.client import transport

    for name in client.__all__:
        assert getattr(client, name) is getattr(transport, name)
