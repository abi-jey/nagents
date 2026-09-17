# Skills API

Skills are available from `nagents.skills`. Attach a discoverer with
`Agent(..., skill_discoverer=source, skill_token_limit=10_000)` to enable the
dynamic catalog and `skill(name)` tool in the text loop. Without a discoverer,
plain library Agents retain their existing behavior.

See the [Skills guide](../guide/skills.md) for in-memory and directory/composite
examples, builtin content, authoring, activation syntax, and mode limitations.

## Descriptor and protocol

`Skill(name, description, location)` is immutable. `location` identifies content
in its owning source; it need not be a path accessible through a file tool.
Descriptors contain metadata, not eager instruction text or executable policy.
The current descriptor validates a 1–64 character ASCII name (letters, digits,
underscores, or hyphens), a nonblank description of at most 1,000 characters,
and a nonblank location of at most 4,096 characters. For portable file names,
follow the stricter lowercase/hyphen conventions in the guide.

The structural `SkillDiscoverer` protocol is:

```python
from collections.abc import Sequence
from typing import Protocol

from nagents.skills import Skill


class SkillDiscoverer(Protocol):
    async def discover(self) -> Sequence[Skill]: ...

    async def load(self, skill: Skill) -> str: ...
```

`discover()` returns a complete current snapshot with unique names; `load()`
returns instruction text for a current descriptor. Sources own containment,
resource resolution, and stale-descriptor checks. Both methods are awaited and
must allow cancellation. Protocol errors remain explicit rather than being
converted into an apparently successful empty catalog.

::: nagents.skills.Skill

::: nagents.skills.SkillDiscoverer

## Discovery implementations

- **Directory:** bounded plain-file discovery under explicitly selected roots,
  without symlink following. Malformed/unreadable/disappearing entries are
  excluded with bounded diagnostics where possible.
- **Builtin:** packaged `ngn-customize` and `ngn-channels` resources loaded with
  `importlib.resources`, independent of workspace file tools.
- **Composite:** first source wins across sources; duplicates within a source
  remain errors. Loading delegates to the source owning the selected descriptor.

The Harness supplies its own guarded workspace source ahead of the builtin
source; generic directory discovery does not replace Harness filesystem rules.

::: nagents.skills.DirectorySkillDiscoverer

::: nagents.skills.BuiltinSkillDiscoverer

::: nagents.skills.CompositeSkillDiscoverer

Directory sources accept an iterable of `Path` or string roots, with
`max_entries=10_000` and `max_depth=16` by default. Entries include directories;
files over 4,000,000 bytes are skipped. `diagnostics` is a bounded tuple of strings.
Composite sources accept an iterable of discoverers in precedence order.

Directory and builtin `load()` return the complete `SKILL.md` text, including
frontmatter. Custom sources can return body-only instructions. The Agent budget
applies to whichever string the source returns.

## Agent integration

| Member | Contract |
| --- | --- |
| Constructor `skill_discoverer` | Optional discovery source; enables metadata disclosure and model tool loading. |
| Constructor `skill_token_limit` | Integer 1–100,000, default 10,000 estimated tokens; booleans rejected. |
| `skill_discoverer` property | Replace a source at runtime; immediately invalidates the old catalog. |
| `skills` | Complete read-only snapshot mapping from name to immutable `Skill` descriptor, including names omitted from the model preview. |
| `await refresh_skills()` | Atomically replace and return the catalog from a fresh discovery result. |
| `await load_skill(name)` | Resolve and load a discovered name, returning the JSON-compatible result below. Does not run a model. |

`load_skill()` refreshes before resolving the name and returns a
`SkillLoadResult` with these fields. An unknown name raises `ValueError`.

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | `str` | Selected skill name. |
| `location` | `str` | Source location identifier. |
| `content` | `str` | Loaded text within the budget. |
| `truncated` | `bool` | Whether content was shortened. |
| `estimated_tokens` | `int` | Deterministic estimate for returned content. |
| `token_limit` | `int` | Applied estimated-token budget. |

The estimate is `ceil(len(content.encode("utf-8")) / 4)`, not a model tokenizer.
Truncation preserves valid UTF-8 and imposes no fixed 1,000-line cap. Explicit
`$skill-name` activations additionally share a bounded per-turn aggregate budget.
File/discovery bounds remain separate from the instruction token budget.

`HarnessConfig.skill_token_limit` forwards the same allowance to the Agent,
defaulting to 10,000 with the same integer range. Trusted flat YAML
`skill_token_limit` and the `NGN_SKILL_TOKEN_LIMIT` environment default configure
it for Harness clients. It covers both individual loads and aggregate explicit
activation content, independently of ordinary tool-output and file-size bounds.

::: nagents.Agent.refresh_skills

::: nagents.Agent.load_skill

## Budget and metadata helpers

`DEFAULT_SKILL_TOKEN_LIMIT` is 10,000 and `MAX_SKILL_TOKEN_LIMIT` is 100,000.
`budget_skill_content(skill, content, token_limit=...)` is the shared helper for
host wrappers and custom integrations. It returns the same `SkillLoadResult`
shape, enforcing the budget without executing or resolving a source. The estimate
and limit cover only `content`, excluding the result's metadata fields.

::: nagents.skills.SkillLoadResult

::: nagents.skills.estimate_skill_tokens

::: nagents.skills.budget_skill_content

`parse_skill_metadata` is a lightweight parser for optional top-level, single-line
`name`/`description` frontmatter; unknown keys are ignored. It also supports
plain Markdown using a supplied directory name and a description fallback.
Directory, builtin, and Harness workspace sources use this same helper. Parsed
descriptions retain up to 1,000 characters, and extra metadata is ignored rather
than used as executable policy. Nested fields are ignored, including nested
`name`/`description` keys; this is not a full YAML parser.

::: nagents.skills.parse_skill_metadata

### Catalog preview limit

The exported `MAX_SKILL_MANIFEST_TOKENS` is 10,000 estimated UTF-8 tokens.
`render_skill_manifest` escapes descriptor metadata and emits complete entries
in deterministic name order, with an explicit omission notice when not all fit.
The bound includes the rendered wrapper and notice. It reads no skill bodies.

This limit applies only to the model's catalog preview. `Agent.skills`, direct
named loads, and explicit references still resolve against the full catalog;
regular load and activation budgets apply independently. `skill_token_limit`
does not configure the manifest cap or the complete model-request size.

::: nagents.skills.render_skill_manifest

## Lifecycle and authority

Refresh occurs at incoming text-message boundaries, before model requests, and
before/after each tool invocation. Sequential calls in a model-generated tool
batch each get those boundaries. Catalogs are replaced, not accumulated as
transcript messages; removed or renamed descriptors do not survive a successful
refresh. Refresh/loading are cancellable and do not start background polling.
If discovery raises a protocol error, the last successful snapshot remains
available for inspection, but the current run aborts rather than submitting
that stale catalog to the model.

The Agent inserts the preview as a separate request-local **system-level
message**, explicitly framed as escaped descriptive tool-selection data. Catalog
refresh does not fabricate a user turn or change the existing user/tool message
order. Loaded bodies remain user-level activation context or ordinary tool
results, never system/developer instructions in the catalog.

Ordinary relevance selection is the model's `skill(name)` call through the
normal executor and hooks. `$skill-name` in incoming user text explicitly adds
bounded per-turn user-level context; arbitrary tool-result mentions do not
activate it. Descriptions and loaded content cannot elevate authority, allow
tools, or execute scripts. Supporting resource files require a separate access
mechanism. Loaded tool results use ordinary history/compaction behavior.

These contracts apply to text `run()`. Skill-enabled batch, realtime voice, and
`run_simple()` reject unsupported extension execution. The TUI's
`/skill:<name> [task]` is a client convenience, not a protocol method or universal
slash syntax.
