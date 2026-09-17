# Skills

Skills provide specialized task instructions **on demand**. Nagents advertises a
small catalog of names, descriptions, and locations, then loads a selected skill's
text when the model or user asks for it. The catalog is dynamic: adding, editing,
renaming, or removing a skill is reflected at the next Agent execution boundary.

The catalog is a separate, request-local **system-level message** containing
escaped, descriptive tool-selection metadata. It does not create a user turn or
reorder existing user/tool messages. Loaded skill bodies remain user-level task
context for explicit activation or normal tool results for model-selected loading;
they are never promoted into the system-level catalog.

This page describes the current checkout. See the [API reference](../api/skills.md)
for the Python interfaces and [release availability](ngn-installation.md#release-availability)
when using an older published package.

## Use skills in ngn

The Harness includes two bundled skills without workspace setup:

| Skill | Use it for |
| --- | --- |
| `ngn-customize` | Agent/providers/sessions, tools/executors, plugin hooks, compaction, Harness config/profiles/instructions/commands, skills, children, wakeups, channels, and web settings. |
| `ngn-channels` | Connector contracts and installation, Telegram setup, standalone shared identity versus web chat routing, explicit sends, activity, and durable admission/recovery. |

Mention a discovered skill explicitly in incoming user text:

```text
$ngn-customize Add a read-only review profile and explain how its tools are restricted.
```

```text
$ngn-channels Explain how to connect Telegram and keep chats in separate sessions.
```

In a shell, use single quotes to preserve the literal dollar sign:

```bash
ngn run '$ngn-customize Explain the AgentPlugin hooks.'
```

`$skill-name` is **ngn's explicit activation syntax**. The Agent resolves matching
names and adds bounded skill text as per-turn user-level task context before the
model request. It does not grant developer/system authority. Tool-result text
containing a dollar mention does not trigger explicit activation.

Without a mention, the ordinary model reads the catalog, decides whether a skill
matches the task, and calls the registered `skill(name)` tool. There is no hidden
selection model or deterministic relevance classifier. A conceptual tool call is:

```python
skill(name="ngn-customize")
```

This is a model tool, not a Python function to import. Python callers use
`await agent.load_skill("ngn-customize")`.

The terminal client also offers `/skill:<name> [task]`, for example:

```text
/skill:ngn-customize Explain custom compaction strategies
```

An owned TUI worker refreshes command suggestions, and command execution refreshes
discovery again. A newly added `/skill:<name>` can therefore execute even before
its name appears in the cached menu. Slash syntax is a
client command convention, not a universal skill format or a promised web/Telegram
slash-command interface. Ordinary web/Telegram text reaches the Agent's automatic
refresh and dollar-mention boundaries; it needs no skill-specific Refresh button.
The web Channels panel's **Refresh** discovers installed connector packages,
which is a separate operation.

## Create a workspace skill

The Harness discovers guarded workspace skills under `.ngn/skills/` and
`.agents/skills/`. For example, create
`.agents/skills/review-checks/SKILL.md`:

```markdown
---
name: review-checks
description: Review code changes for regressions and missing checks. Use when reviewing a patch or preparing a verification summary.
---

# Review checks

1. Read the change and its surrounding contracts.
2. Identify concrete regression paths and missing validation.
3. Run relevant available checks and report their actual outcomes.
4. Cite affected files and distinguish findings from questions.
```

Use top-level, single-line `name` and `description` frontmatter for ngn's lightweight parser.
Keep the name identical to the containing directory. For portable skills, follow
the standard: 1–64 lowercase letters/numbers/hyphens, no leading/trailing or
consecutive hyphens; a nonempty description of at most 1,024 characters. A useful
description explains both the operation and when to select it.

The current `Skill` descriptor accepts at most 1,000 description characters.
Directory, builtin, and Harness workspace sources share `parse_skill_metadata`,
which keeps up to 1,000 description characters and recognizes only top-level
`name`/`description` fields. Other frontmatter, including nested metadata, is
ignored. Keep descriptions within that limit to preserve their complete wording.
Extra metadata does not execute code or implement the experimental `allowed-tools`
policy. This is a lightweight single-line parser, not a full YAML implementation.

The body contains steps, examples, and relevant edge cases. Metadata alone should
be sufficient for selection; detailed instructions belong in the body. Loading
the file runs no scripts and installs no Python packages.

Workspace access retains CodingTools' containment, ignore, symlink, sensitive-path,
and file-size rules. Choose an ordinary contained file; a skill directory does not
open general filesystem access. Workspace names override bundled names. Conflicting
names within one discovery source remain errors rather than silently replacing
one another; use a unique name or an intentional composite-source precedence rule.

### References, scripts, and assets

The Agent Skills format allows optional `references/`, `scripts/`, and `assets/`
directories. Reference specific files using paths relative to the skill root and
say **when** each should be read. These files are not automatically loaded when
`SKILL.md` is activated; scripts are not automatically executed.

For a workspace skill, a permitted file tool can read a referenced file beneath
that skill's directory. A library discoverer may instead return an opaque location
such as `memory:review-checks` or a package-resource identifier. That identifier
is not necessarily a filesystem path accessible to the model. Supply an explicit
resource tool or include essential reference content in `load()` if your
application needs such resources.

Both ngn builtins are self-contained `SKILL.md` bodies. `BuiltinSkillDiscoverer`
loads them through `importlib.resources`; it does not depend on Harness
`read_file` being able to enter the installed package directory. The current
discoverer protocol loads skill text, not arbitrary supporting files. Links and
source pointers in a body are verification references, not automatic reads.
Directory and builtin loaders return the complete `SKILL.md`, including its
frontmatter; only the catalog metadata is disclosed to the model before activation.

## Dynamic lifecycle and context budget

With a `skill_discoverer`, the text Agent refreshes discovery:

1. At an incoming text-message boundary, before resolving explicit mentions.
2. Before every model request.
3. Before and after every tool invocation.

An after-tool refresh makes a newly created or edited skill available during the
same turn, including before the next sequential call in a model-generated tool
batch. There are no polling threads and no requirement to restart the Agent.
Discovery and loading are awaited and cancellable. Custom sources should keep
discovery bounded and responsive because it runs frequently.

`await agent.refresh_skills()` atomically replaces the full name-to-descriptor
catalog. It does not append another manifest to persisted history. Removed or
renamed descriptors disappear on a successful refresh. Filesystem sources skip
malformed, unreadable, or disappearing entries with bounded diagnostics where
possible; protocol violations such as duplicate descriptors remain explicit.
Check current discovery diagnostics when a skill is absent.

The model's catalog preview has a separate fixed cap:
`MAX_SKILL_MANIFEST_TOKENS = 10_000`. It contains complete escaped descriptors
in deterministic name order, followed by an omission notice when entries do not
fit. This cap limits only the preview sent to the model: `Agent.skills` retains
the complete discovered catalog, and omitted names remain available through
`load_skill(name)`, `skill(name)`, and explicit `$skill-name` activation. Their
ordinary loading/activation budgets still apply. Changing `skill_token_limit`
does not change this manifest cap; neither limit bounds the whole model request.

Each load defaults to **10,000 estimated tokens**, calculated deterministically as:

```python
estimated_tokens = (len(text.encode("utf-8")) + 3) // 4
```

This is `ceil(UTF-8 bytes / 4)`, not a provider tokenizer. Content is truncated at a
valid UTF-8 boundary to fit `skill_token_limit`; it has no fixed 1,000-line cap.
The constructor accepts integer limits from 1 through 100,000 (booleans are
rejected); the budget applies to content, excluding result metadata.
Other source/file bounds still apply. Multiple explicit activations share a bounded
per-turn aggregate budget, so mentions cannot multiply the context allowance
without limit: the current implementation considers at most 32 distinct mentions
in text order and shares `skill_token_limit` across their returned content.
Inspect `truncated`, `estimated_tokens`, and `token_limit` in the
load result. Prefer concise instructions over increasing the budget blindly.

The Harness forwards its trusted `skill_token_limit` configuration to this same
Agent budget. Configure it in flat YAML:

```yaml
skill_token_limit: 10000
```

Or supply `NGN_SKILL_TOKEN_LIMIT=10000` as an environment default. Both accept
integers from 1 through 100,000; YAML takes precedence over the environment
default under the normal configuration rules. This one allowance controls each
load and the aggregate content of explicit activations, across CLI, TUI, and web
Harness execution. It is distinct from `max_output` and `max_file_bytes`.

Explicit context is per-turn, not a permanent policy change. A model's skill tool
result follows ordinary tool history and compaction behavior; it is not pinned
forever or exempt from a custom compactor. Load relevant guidance again when
needed. Catalog metadata is escaped when rendered and is never interpreted as
an instruction to change host policy.

## Library: an in-memory discoverer

`SkillDiscoverer` is a structural protocol. Implement two async methods; no base
class or filesystem is required. This example replaces its records atomically
when the application updates the catalog:

```python
from collections.abc import Sequence
from dataclasses import dataclass

from nagents.skills import Skill


@dataclass
class MemorySkills:
    records: dict[str, tuple[Skill, str]]

    async def discover(self) -> Sequence[Skill]:
        return tuple(skill for skill, _ in self.records.values())

    async def load(self, skill: Skill) -> str:
        current, content = self.records[skill.name]
        if current != skill:
            raise ValueError("Skill changed; refresh discovery before loading")
        return content


review = Skill(
    name="review-checks",
    description="Review changes for concrete regressions and missing validation.",
    location="memory:review-checks",
)
source = MemorySkills({review.name: (review, "Read the patch, check callers, and cite concrete findings.")})
```

Attach that source to the normal text Agent. Assuming the definitions above:

```python
import asyncio
import os
from pathlib import Path

from nagents import Agent, Provider, ProviderType, SessionManager


async def main() -> None:
    agent = Agent(
        provider=Provider(
            ProviderType.OPENAI_COMPATIBLE,
            api_key=os.environ["OPENAI_API_KEY"],
            model=os.environ["OPENAI_MODEL"],
        ),
        session_manager=SessionManager(Path("agent.db")),
        skill_discoverer=source,
        skill_token_limit=10_000,
    )
    try:
        await agent.refresh_skills()
        print(tuple(agent.skills))
        loaded = await agent.load_skill("review-checks")
        print(loaded["content"])
        async for event in agent.run(
            "$review-checks Explain how you will review this project.",
            session_id="review-session",
        ):
            print(event)
        source.records = {}  # The next refresh removes the descriptor.
        await agent.refresh_skills()
    finally:
        await agent.close()


asyncio.run(main())
```

`load_skill()` returns data; calling it directly does not itself run a model or
append a user request. The incoming mention or model tool call is what integrates
loaded content with execution. A custom source must enforce its own access rules,
validate current descriptors in `load`, return strings, and propagate cancellation.
Avoid network connections or heavyweight full-body reads during `discover()`.

## Library: directory, bundled, and composite sources

Choose roots explicitly in library code:

```python
from pathlib import Path

from nagents.skills import BuiltinSkillDiscoverer
from nagents.skills import CompositeSkillDiscoverer
from nagents.skills import DirectorySkillDiscoverer

workspace_skills = DirectorySkillDiscoverer((Path("/state/workspace/.agents/skills"),))
source = CompositeSkillDiscoverer((workspace_skills, BuiltinSkillDiscoverer()))

# Pass source as Agent(..., skill_discoverer=source).
```

`DirectorySkillDiscoverer` performs bounded discovery of contained plain
`SKILL.md` files under developer-selected roots without following symlinks.
`BuiltinSkillDiscoverer` reads packaged resources. A composite applies
**first-source precedence** across sources and remembers which source owns each
selected descriptor for loading. Duplicates within the same source are errors.
Reversing the sources reverses which copy wins when names collide.

This generic directory source is for applications deliberately choosing their
own filesystem boundary. The Harness uses its guarded CodingTools source for
workspace skills and composes it ahead of the builtin resource source, preserving
workspace and sensitive-path policy. Plain library Agents do not discover roots
or enable builtins unless a discoverer is supplied.

## Execution modes

Dynamic skills use the **text `Agent.run()` lifecycle**, including that lifecycle
when called by a channel/web host. Supplying discovery is an extension-enabled
configuration: batch execution, realtime voice execution, and `run_simple()`
reject it rather than silently ignoring refresh/loading hooks. Use a separate
Agent without skill discovery for those modes. Dictation that produces text for
a subsequent normal run is distinct from realtime execution.

The registered skill tool retains the normal tool executor and plugin hooks.
Harness skill loading is a read-only builtin; it does not authorize file writes,
shell commands, custom Python, or connector activation.

## Agent Skills standard and host choices

The authoritative [Agent Skills specification](https://agentskills.io/specification)
defines YAML frontmatter plus a Markdown body in `SKILL.md`, and optional resources.
Its [client integration guide](https://agentskills.io/client-implementation/adding-skills-support)
describes catalog disclosure followed by model-driven or explicit activation.

| Standard concept | ngn integration |
| --- | --- |
| Required `name` and `description` metadata | Lightweight single-line frontmatter; immutable `Skill` descriptors. |
| Optional `license`, `compatibility`, string-map `metadata`, and experimental `allowed-tools` | Extra keys are ignored consistently by the shared metadata parser. They do not grant permissions or imply full YAML support. |
| Catalog first, selected instructions later | Dynamic manifest and `skill(name)` tool. |
| Optional supporting resources loaded as needed | Separate permitted resource access; the two builtin bodies are self-contained. |
| Client-defined discovery scopes and activation UI | Guarded workspace roots plus packaged builtins in Harness; `$skill-name` in text and `/skill:<name>` in the TUI. |
| Recommended concise bodies (under 5,000 tokens and 500 lines) | A recommendation distinct from ngn's 10,000-token approximate load budget. |

Neither `$skill-name`, `/skill-name`, `/skill:<name>`, a particular prompt role,
nor a particular web endpoint is mandated by the file-format specification.
Keep portable task instructions separate from host-specific tool names and
permissions. A skill is guidance and resources, not a Python plugin or an
authorization mechanism.
