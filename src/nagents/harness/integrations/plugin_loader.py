"""Fresh private Python namespaces for generation-owned extension packages."""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import CodeType
    from types import ModuleType


class _SourceLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, name: str, path: str, digests: dict[str, str]) -> None:
        super().__init__(name, path)
        self.digests = digests

    def get_code(self, fullname: str) -> CodeType:
        # Never consult or write .pyc: two same-size edits inside one timestamp
        # tick must not resurrect an earlier generation's helper implementation.
        source = self.get_data(self.path)
        self.digests[str(Path(self.path).resolve())] = hashlib.sha256(source).hexdigest()
        return self.source_to_code(source, self.path)


class _Namespace(importlib.abc.MetaPathFinder):
    def __init__(self, name: str, root: importlib.machinery.ModuleSpec) -> None:
        self.name = name
        self.root = root
        self.digests: dict[str, str] = {}

    def find_spec(
        self, fullname: str, path: Sequence[str] | None = None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        spec: importlib.machinery.ModuleSpec | None
        if fullname == self.name:
            root = self.root
            if root.origin is None:
                spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
                spec.submodule_search_locations = root.submodule_search_locations
                return spec
            spec = importlib.util.spec_from_file_location(
                fullname,
                root.origin,
                submodule_search_locations=list(root.submodule_search_locations)
                if root.submodule_search_locations is not None
                else None,
            )
        elif fullname.startswith(self.name + "."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        else:
            return None
        if spec is not None and spec.origin and spec.origin.endswith(".py"):
            spec.loader = _SourceLoader(fullname, spec.origin, self.digests)
        return spec

    def close(self) -> None:
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        for name in tuple(sys.modules):
            if name == self.name or name.startswith(self.name + "."):
                sys.modules.pop(name, None)


@dataclass
class PluginModule:
    namespace: _Namespace
    module: ModuleType
    entry: str

    @property
    def name(self) -> str:
        return self.namespace.name

    @property
    def sources(self) -> dict[str, str]:
        return self.namespace.digests

    @property
    def source_digest(self) -> str:
        assert self.module.__file__ is not None
        return self.sources[str(Path(self.module.__file__).resolve())]

    def close(self) -> None:
        self.namespace.close()


def load_plugin(reference: str, workspace: Path) -> PluginModule:
    """Load one entry point, isolating its entire package-relative import tree.

    Shared absolute imports outside this private namespace retain normal Python
    ownership; no host or framework module is removed or globally reloaded.
    """
    module_name, separator, entry = reference.rpartition(":")
    if not separator or not module_name or not entry.isidentifier():
        raise ValueError("Invalid plugin reference; expected path.py:setup or installed.module:setup")
    suffix = ""
    if module_name.endswith(".py"):
        path = (workspace / Path(module_name).expanduser()).resolve()
        root = importlib.util.spec_from_file_location("ngn_source", path)
        selected = root
    else:
        root_name, _, suffix = module_name.partition(".")
        root = importlib.util.find_spec(root_name)
        selected = root
        # Locate children without importing the original host package. Every
        # package __init__ below is executed only inside the private namespace.
        for part in suffix.split(".") if suffix else ():
            if selected is None or selected.submodule_search_locations is None:
                raise ValueError("Reloadable plugin package does not exist")
            selected = importlib.machinery.PathFinder.find_spec(part, selected.submodule_search_locations)
    if root is None or selected is None or not selected.origin or not selected.origin.endswith(".py"):
        raise ValueError("Reloadable plugins require a Python source module")
    namespace = _Namespace(f"_ngn_plugin_{uuid.uuid4().hex}", root)
    sys.meta_path.insert(0, namespace)
    try:
        module = importlib.import_module(namespace.name + ("." + suffix if suffix else ""))
        return PluginModule(namespace, module, entry)
    except BaseException:
        namespace.close()
        raise
