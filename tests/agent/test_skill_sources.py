"""Deterministic text budgets and no-symlink, live source snapshots."""

import asyncio
import importlib
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from importlib.resources.abc import Traversable
from pathlib import Path
from types import ModuleType

import pytest

from nagents.skills import MAX_SKILL_MANIFEST_TOKENS
from nagents.skills import BuiltinSkillDiscoverer
from nagents.skills import CompositeSkillDiscoverer
from nagents.skills import DirectorySkillDiscoverer
from nagents.skills import Skill
from nagents.skills import budget_skill_content
from nagents.skills import estimate_skill_tokens
from nagents.skills import parse_skill_metadata
from nagents.skills import render_skill_manifest
from tests.agent.test_agent_skills import MemorySkills
from tests.agent.test_agent_skills import descriptor


@pytest.mark.parametrize(
    "body", ["x" * 50000, "😀漢é" * 10000, "a\n" * 20000], ids=["long-line", "unicode", "many-lines"]
)
def test_utf8_budget_long_lines_and_unlimited_line_count(body: str) -> None:
    result = budget_skill_content(descriptor("guide"), body)
    assert result["estimated_tokens"] <= 10000
    assert len(result["content"].encode("utf-8")) <= 40000
    assert body.startswith(result["content"])
    assert result["truncated"] is (len(body.encode("utf-8")) > 40000)
    assert estimate_skill_tokens(result["content"]) == result["estimated_tokens"]
    assert "�" not in result["content"]


def test_budget_exact_bytes_immutable_metadata_and_plain_markdown() -> None:
    assert estimate_skill_tokens("漢é") == 2
    assert budget_skill_content(descriptor("guide"), "😀a", 1)["content"] == "😀"
    assert budget_skill_content(descriptor("guide"), "\n" * 1500)["content"].count("\n") == 1500
    assert parse_skill_metadata("# Heading\n\nShort description.\nBody", "memory:guide", "guide") == Skill(
        "guide", "Short description.", "memory:guide"
    )
    assert parse_skill_metadata(
        "---\nname: custom\ndescription: 'Quoted'\nallowed-tools: shell\n---\n", "opaque"
    ) == Skill("custom", "Quoted", "opaque")
    with pytest.raises(FrozenInstanceError):
        descriptor("guide").name = "changed"  # type: ignore[misc]
    for budget in (-1, 0, True, 100001):
        with pytest.raises(ValueError, match="skill_token_limit"):
            budget_skill_content(descriptor("guide"), "body", budget)


@pytest.mark.parametrize(
    "header",
    [
        "metadata:\n  name: nested\n  description: Nested description\nname: guide\ndescription: Actual description",
        "name: guide\ndescription: Actual description\nmetadata:\n  name: nested\n  description: Nested description",
        "name: guide\nmetadata:\n  description: Nested description\ndescription: Actual description",
        "compatibility: |\n  name: nested\n  description: Nested description\nname: guide\ndescription: Actual description",
        "name: guide\ncompatibility: |\n  ---\n  name: nested\ndescription: Actual description",
    ],
    ids=["mapping-before", "mapping-after", "mapping-between", "unknown-block", "indented-block-delimiter"],
)
def test_frontmatter_only_interprets_top_level_name_and_description(header: str) -> None:
    content = f"---\n{header}\n---\n# Instructions\nBody text.\n"
    assert parse_skill_metadata(content, "memory:guide", "directory-default") == Skill(
        "guide", "Actual description", "memory:guide"
    )


def test_nested_metadata_does_not_replace_directory_name_or_body_description_fallback() -> None:
    content = "---\nmetadata:\n  name: nested\n  description: Nested description\n---\n# Heading\n\nBody description.\n"
    assert parse_skill_metadata(content, "memory:guide", "directory-default") == Skill(
        "directory-default", "Body description.", "memory:guide"
    )


@pytest.mark.parametrize(
    "header",
    [
        "name: guide\nname: duplicate",
        "description: First\ndescription: Duplicate",
        "name: |\n  guide",
        "description: >\n  Multiline description",
    ],
)
def test_top_level_duplicate_or_multiline_skill_fields_remain_errors(header: str) -> None:
    with pytest.raises(ValueError, match="single-line"):
        parse_skill_metadata(f"---\n{header}\n---\nBody", "memory:guide", "guide")


def write_skill(root: Path, directory: str, name: str, body: str = "Body", description: str = "Description") -> Path:
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8")
    return path


def test_directory_live_add_edit_rename_delete_and_diagnostics(tmp_path: Path) -> None:
    async def drive() -> None:
        root = tmp_path / "skills"
        root.mkdir()
        source = DirectorySkillDiscoverer([root])
        assert await source.discover() == ()
        path = write_skill(root, "first", "first")
        first = (await source.discover())[0]
        assert first.name == "first"
        path.write_text("---\nname: renamed\ndescription: Edited\n---\nNew body", encoding="utf-8")
        renamed = (await source.discover())[0]
        assert renamed.name == "renamed" and renamed.description == "Edited"
        assert "New body" in await source.load(renamed)
        with pytest.raises(ValueError, match="catalog"):
            await source.load(first)
        write_skill(root, "bad", "bad", description="|")
        assert [skill.name for skill in await source.discover()] == ["renamed"]
        assert "single-line description" in source.diagnostics[0]
        path.unlink()
        assert await source.discover() == ()
        write_skill(root, "new", "new")
        assert [skill.name for skill in await source.discover()] == ["new"]

    asyncio.run(drive())


def test_directory_rejects_symlinks_containment_and_replacement(tmp_path: Path) -> None:
    async def drive() -> None:
        root = tmp_path / "skills"
        outside = tmp_path / "outside"
        outside_file = write_skill(outside, "external", "external")
        safe_file = write_skill(root, "safe", "safe")
        (root / "linked-directory").symlink_to(outside, target_is_directory=True)
        (root / "linked-file").mkdir()
        (root / "linked-file" / "SKILL.md").symlink_to(outside_file)
        source = DirectorySkillDiscoverer([root])
        safe = (await source.discover())[0]
        assert safe.name == "safe"
        with pytest.raises(ValueError, match="catalog"):
            await source.load(Skill("external", "Description", str(outside_file)))
        safe_file.unlink()
        safe_file.symlink_to(outside_file)
        with pytest.raises(OSError):
            await source.load(safe)
        assert await source.discover() == ()
        linked_root = tmp_path / "linked-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        unsafe = DirectorySkillDiscoverer([linked_root / "external"])
        assert await unsafe.discover() == ()
        assert unsafe.diagnostics

    asyncio.run(drive())


def test_duplicate_and_bounded_directory_discovery(tmp_path: Path) -> None:
    async def drive() -> None:
        write_skill(tmp_path, "a", "duplicate")
        write_skill(tmp_path, "b", "duplicate")
        with pytest.raises(ValueError, match="Duplicate skill"):
            await DirectorySkillDiscoverer([tmp_path]).discover()
        bounded = DirectorySkillDiscoverer([tmp_path], max_entries=1)
        assert await bounded.discover() == ()
        assert "limit reached" in bounded.diagnostics[0]
        assert await bounded.discover() == ()

    asyncio.run(drive())


def test_composite_first_source_precedence_and_complete_owner_replacement() -> None:
    async def drive() -> None:
        shared = descriptor("shared")
        local = MemorySkills([(shared, "local")])
        bundled = MemorySkills([(shared, "bundled"), (descriptor("builtin"), "builtin")])
        source = CompositeSkillDiscoverer([local, bundled])
        assert [skill.name for skill in await source.discover()] == ["builtin", "shared"]
        assert await source.load(shared) == "local"
        local.entries = []
        await source.discover()
        assert await source.load(shared) == "bundled"
        bundled.entries = []
        assert await source.discover() == ()
        with pytest.raises(ValueError, match="catalog"):
            await source.load(shared)
        bundled.entries = [(shared, "one"), (shared, "two")]
        with pytest.raises(ValueError, match="Duplicate"):
            await source.discover()

    asyncio.run(drive())


def test_builtin_reads_traversable_package_resources_not_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "skills.whl"
    with zipfile.ZipFile(archive, "w") as wheel:
        for name in ("ngn-customize", "ngn-channels"):
            wheel.writestr(
                f"nagents/skills/bundled/{name}/SKILL.md", f"---\nname: {name}\ndescription: Bundled\n---\n{name} body"
            )
    with zipfile.ZipFile(archive) as wheel:
        root = zipfile.Path(wheel, "nagents/skills/")

        def files(anchor: ModuleType) -> Traversable:
            assert anchor is sys.modules["nagents.skills"]
            return root

        monkeypatch.setattr("nagents.skills.sources.resources.files", files)

        async def drive() -> None:
            source = BuiltinSkillDiscoverer()
            skills = await source.discover()
            assert [skill.name for skill in skills] == ["ngn-channels", "ngn-customize"]
            for skill in skills:
                assert skill.location.startswith("package:nagents.skills/")
                assert f"{skill.name} body" in await source.load(skill)

        asyncio.run(drive())


def test_protocol_invalid_descriptors_and_load_results_are_explicit() -> None:
    async def drive() -> None:
        class BadDiscovery(MemorySkills):
            async def discover(self) -> Sequence[Skill]:
                return ["not a descriptor"]  # type: ignore[list-item]

        class BadLoad(MemorySkills):
            async def load(self, skill: Skill) -> str:
                return 123  # type: ignore[return-value]

        with pytest.raises(TypeError, match="Skill descriptors"):
            await CompositeSkillDiscoverer([BadDiscovery()]).discover()
        source = CompositeSkillDiscoverer([BadLoad([(descriptor("bad"), "body")])])
        skill = (await source.discover())[0]
        with pytest.raises(TypeError, match="return str"):
            await source.load(skill)

    asyncio.run(drive())


def test_real_bundled_documents_discover_and_load() -> None:
    async def drive() -> None:
        source = BuiltinSkillDiscoverer()
        skills = await source.discover()
        assert [skill.name for skill in skills] == ["ngn-channels", "ngn-customize"]
        for skill in skills:
            body = await source.load(skill)
            result = budget_skill_content(skill, body)
            assert result["content"] == body
            assert not result["truncated"]

    asyncio.run(drive())


def test_builtin_resolution_uses_package_identity_despite_plugin_import_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture_module = ModuleType("plugin_fixture")
    monkeypatch.setattr(importlib, "import_module", lambda name: fixture_module)

    async def drive() -> None:
        source = BuiltinSkillDiscoverer()
        skills = await source.discover()
        assert [skill.name for skill in skills] == ["ngn-channels", "ngn-customize"]
        assert all(skill.location.startswith("package:nagents.skills/bundled/") for skill in skills)
        assert await source.load(skills[0])

    asyncio.run(drive())


def test_manifest_has_deterministic_utf8_bound_complete_markup_and_omission_notice() -> None:
    skills = [Skill(f"guide-{index:04}", '漢<&"' * 250, f"memory:{index}" + "😀" * 1000) for index in range(500)]
    manifest = render_skill_manifest(skills)
    assert estimate_skill_tokens(manifest) <= MAX_SKILL_MANIFEST_TOKENS
    assert manifest == render_skill_manifest(reversed(skills))
    assert 'name="guide-0000"' in manifest
    assert 'name="guide-0499"' not in manifest
    assert "Skill catalog truncated:" in manifest
    assert "additional descriptors omitted" in manifest
    assert manifest.count("<skill name=") == manifest.count("</skill>")
    assert "</available_skills>\n[Skill catalog truncated:" in manifest
    assert "&lt;&amp;&quot;" in manifest
    assert "�" not in manifest
