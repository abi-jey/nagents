"""Live skill sources which preserve the Harness workspace file boundary."""

import asyncio
import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from nagents._async import finish_on_cancel
from nagents.skills import BuiltinSkillDiscoverer
from nagents.skills import Skill

if TYPE_CHECKING:
    from .tools import CodingTools


class HarnessSkillDiscoverer:
    """Workspace skills override bundled defaults; every discovery replaces the catalog."""

    def __init__(self, tools: "CodingTools") -> None:
        self.tools = tools
        self.builtin = BuiltinSkillDiscoverer()
        self._bundled: dict[str, Skill] = {}

    async def discover(self) -> Sequence[Skill]:
        bundled = {skill.name: skill for skill in await self.builtin.discover()}
        while True:
            reader = self.tools.discovery_reader()
            # File traversal can take hundreds of milliseconds for installed
            # skills. Keep it off the shared media/model loop and join it before
            # releasing the Agent's discovery lock, including on cancellation.
            try:
                await finish_on_cancel(asyncio.to_thread(reader.discover_skills))
            finally:
                # The owned worker is finished even after cancellation/error.
                # Remembering protected inode identities only tightens access;
                # never publish its potentially incomplete skill catalog here.
                self.tools.retain_discovery_guards(reader)
            if self.tools.accept_discovery(reader):
                break
        workspace = {
            name: Skill(name=name, description=description, location=path)
            for name, (path, description) in self.tools.workspace_skills.items()
        }
        catalog = {**bundled, **workspace}
        self._bundled = bundled
        self.tools.skills = {name: (skill.location, skill.description) for name, skill in sorted(catalog.items())}
        return tuple(catalog[name] for name in sorted(catalog))

    async def load(self, skill: Skill) -> str:
        workspace = self.tools.workspace_skills.get(skill.name)
        if workspace is None:
            if self._bundled.get(skill.name) != skill:
                raise ValueError("Skill is no longer available; refresh discovery")
            return await self.builtin.load(skill)
        path, _ = workspace
        if path != skill.location:
            raise ValueError("Skill location changed; refresh discovery")
        relative = self.tools.relative(path)
        instructions = self.tools.instructions(relative)
        data, _ = self.tools.snapshot(path)
        self.tools.read_hashes[str(relative)] = hashlib.sha256(data).hexdigest()
        content = data.decode("utf-8")
        if instructions:
            content = f"Applicable project context:\n{instructions}\n\nSkill document:\n{content}"
        return content
