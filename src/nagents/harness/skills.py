"""Live skill sources which preserve the Harness workspace file boundary."""

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

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
        self.tools.discover_skills()
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
