# CodeBlock

`CodeBlock` is an independent React code viewer, used by chat Markdown and other
literal-source views. It does not depend on chat state, a provider, or a browser
API. Import the component and load its stylesheet once in the host entry point:

```tsx
import { CodeBlock, type CodeBlockProps } from "./components/CodeBlock.js";
import "./components/codeBlock.css";

const example: CodeBlockProps = {
  label: "Configuration",
  language: "yaml",
  text: "enabled: true\n",
};

<CodeBlock {...example} />;
```

`label` supplies the visible heading and accessible name. `text` is the exact
source: no trimming, newline insertion, entity decoding, formatting, or execution
takes place. Optional `language` accepts one language name or alias, ignoring case
and surrounding whitespace. Markdown extracts the first word of fence metadata
before passing it here; inline code stays literal.

| Grammar | Accepted names |
| --- | --- |
| YAML | `yaml`, `yml` |
| Python | `python`, `py`, `python3` |
| JavaScript | `javascript`, `js`, `jsx`, `mjs`, `cjs` |
| TypeScript | `typescript`, `ts`, `tsx`, `mts`, `cts` |
| JSON | `json`, `jsonc` |
| Bash | `bash`, `sh`, `zsh`, `shell` |
| Shell session | `console`, `shellsession` |
| HTML / XML | `html`, `xml`, `xhtml`, `svg` |
| CSS | `css` |
| SQL | `sql` |
| Diff | `diff`, `patch` |

The selected grammars are bundled locally using
[lowlight](https://github.com/wooorm/lowlight) and
[highlight.js](https://github.com/highlightjs/highlight.js). There is no automatic
fence-language detection, remote stylesheet, grammar download, or resource request.
Unknown or omitted languages, including `text` and `plaintext`, remain plain text.
Lowlight's syntax tree is rendered with React text nodes and `span` elements with
token classes; source never becomes HTML or element attributes.

Highlighting is skipped above 32,000 UTF-16 code units or 1,000 lines. Rendering
also has a 5,000-node and 64-level nesting budget. Exceeding a limit or encountering
a highlighter error shows the entire original source as plaintext. These bounds
keep the amount of parsing and rendered token work modest during streaming; they
do not truncate the source. Text/language pairs are memoized per component, and
updates always retain the same `section`, keyboard-focusable `pre`, and `code`
elements, including transitions to/from plaintext.

The stylesheet contains layout, keyboard focus, and the muted token palette.
It uses ngn's `--text-code`, `--text-secondary`, `--surface-output`,
`--border-group`, `--focus`, and `--accent-*` variables with standalone dark
fallbacks. The region is at most 400px tall and scrolls within its available width.
Hosts can override its CSS for a particular context. Import CSS in the entry
point, not the component, so the module also works with server rendering and the
Node test runner.
