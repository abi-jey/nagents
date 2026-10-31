# Panel divider studies

Three interactive React refinements of the selected **Soft notch** design for
resizing and collapsing ngn's Sessions and Team panels. These are isolated design
demos; they do not call the ngn backend or change the production layout.

- **B1 · Compact:** 14 × 34 px, with short rounded shoulders.
- **B2 · Balanced:** 13 × 42 px, with a tighter 5 px corner radius.
- **B3 · Slender:** 12 × 50 px, with longer curved shoulders.

All three keep the separate grip removed and use a subtle 240 ms tonal hover.
The resize target is confined to a dedicated 16 px grid gutter. Buttons and their
invisible hit areas stay inside that gutter horizontally, leaving adjacent scroll
containers and scrollbars unobstructed. The notch attaches to its own panel edge.
Use **Scroll test** in the top toolbar to fill all three panels with enough sample
content to scroll. This makes scrollbar clearance easy to compare in each version.

Each version supports dragging both sides, remembered widths, independent collapse,
keyboard resizing, double-click to reset one width, and reduced motion. On narrow
screens the panels become drawers; resizing is available in the desktop preview.
The composer and example tool disclosures are interactive, with local demo state.

From `src/nagents/web-ui`:

```sh
node node_modules/vite/bin/vite.js --config demos/panel-dividers/vite.config.ts
```

Open <http://127.0.0.1:18896/>. Choose B1, B2, or B3 at the top. Widths are stored
only under the demo's `ngn-panel-divider-studies-v1` localStorage key. Reset restores
the current version's panel widths and open states. The theme button changes only
the demo.

Check and build:

```sh
node node_modules/typescript/bin/tsc --noEmit -p demos/panel-dividers/tsconfig.json
node node_modules/vite/bin/vite.js build --config demos/panel-dividers/vite.config.ts
```
