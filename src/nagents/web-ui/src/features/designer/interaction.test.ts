import assert from "node:assert/strict";
import test from "node:test";
import type { ComponentProps, ReactElement } from "react";
import { TestPrompt } from "./TestPrompt.js";
import { fitViewport, panViewport, resizeViewport, zoomViewport, type Viewport } from "./viewport.js";

const center = (view: Viewport) => ({ x: view.x + view.width / view.scale / 2, y: view.y + view.height / view.scale / 2 });
const close = (a: number, b: number) => assert.ok(Math.abs(a - b) < .000001, `${a} != ${b}`);

test("canvas resizing and zoom keep a uniform scale and cover wide and tall panels", () => {
  let view: Viewport = { x: 0, y: 0, width: 0, height: 0, scale: 1 };
  view = resizeViewport(view, 1400, 350);
  assert.equal(view.x, 0);
  const original = center(view);
  view = panViewport(view, 125, -40);
  close(center(view).x, original.x - 125);
  const panned = center(view);
  for (const [width, height] of [[350, 900], [1800, 240], [900, 700]]) {
    view = resizeViewport(view, width, height);
    view = zoomViewport(view, 1.25);
    close(center(view).x, panned.x);
    close(center(view).y, panned.y);
    const boxWidth = view.width / view.scale, boxHeight = view.height / view.scale;
    close(boxWidth / boxHeight, width / height);
    close(width / boxWidth, height / boxHeight);
  }
  assert.equal(resizeViewport(view, 0, 0), view);
});

test("Fit recovers far-panned graphs including negative node positions", () => {
  const nodes = [{ x: -600, y: 200 }, { x: 500, y: -200 }];
  const fit = fitViewport({ x: 10000, y: -8000, width: 1300, height: 300, scale: 4 }, nodes);
  for (const node of nodes) {
    assert.ok(node.x >= fit.x && node.x + 180 <= fit.x + fit.width / fit.scale);
    assert.ok(node.y >= fit.y && node.y + 68 <= fit.y + fit.height / fit.scale);
  }
  const moved = panViewport(fit, 60, 30);
  close((fit.x - moved.x) * fit.scale, 60);
  close((fit.y - moved.y) * fit.scale, 30);
});

test("test chat sends on Ctrl/Cmd+Enter but preserves newlines, composition, and busy drafts", () => {
  let sent = 0, prevented = 0;
  type KeyEvent = Parameters<NonNullable<ComponentProps<"textarea">["onKeyDown"]>>[0];
  const key = (changes: Partial<KeyEvent> = {}) => ({ key: "Enter", ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, repeat: false,
    nativeEvent: { isComposing: false }, preventDefault: () => { prevented++; }, ...changes } as KeyEvent);
  const input = (canSend = true, value = "Hello") => TestPrompt({ value, target: "assistant", canSend, change: () => undefined, send: () => { sent++; } }) as ReactElement<ComponentProps<"textarea">>;
  const ready = input();
  ready.props.onKeyDown?.(key({ ctrlKey: true }));
  ready.props.onKeyDown?.(key({ metaKey: true }));
  assert.equal(sent, 2); assert.equal(prevented, 2);
  ready.props.onKeyDown?.(key());
  ready.props.onKeyDown?.(key({ ctrlKey: true, shiftKey: true }));
  ready.props.onKeyDown?.(key({ ctrlKey: true, nativeEvent: { isComposing: true } as KeyboardEvent }));
  assert.equal(prevented, 2);
  ready.props.onKeyDown?.(key({ ctrlKey: true, repeat: true }));
  input(false).props.onKeyDown?.(key({ metaKey: true }));
  input(true, "  ").props.onKeyDown?.(key({ ctrlKey: true }));
  assert.equal(sent, 2);
  assert.equal(ready.props.value, "Hello");
  assert.equal(ready.props["aria-keyshortcuts"], "Control+Enter Meta+Enter");
});
