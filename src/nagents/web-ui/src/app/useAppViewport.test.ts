import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { trackAppViewport } from "./useAppViewport.js";

test("app viewport follows keyboard resize and pan, ignores pinch zoom, and cleans up", () => {
  const dom = new JSDOM("<main></main>");
  try {
    const element = dom.window.document.querySelector("main")!;
    const viewportState = { height: 844, offsetTop: 0, scale: 1 };
    const target = new dom.window.EventTarget();
    Object.defineProperties(target, {
      height: { get: () => viewportState.height },
      offsetTop: { get: () => viewportState.offsetTop },
      scale: { get: () => viewportState.scale },
    });
    const viewport = target as unknown as VisualViewport;
    const dispatch = (event: "resize" | "scroll") => target.dispatchEvent(new dom.window.Event(event));
    const cleanup = trackAppViewport(element, viewport);

    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "844px");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "0px");

    viewportState.height = 520;
    dispatch("resize");
    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "520px",
      "keyboard-driven visual viewport resizing updates the app height");

    viewportState.offsetTop = 136;
    dispatch("scroll");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "136px",
      "visual viewport panning updates the app top offset");

    viewportState.scale = 1.5;
    viewportState.height = 420;
    viewportState.offsetTop = 220;
    dispatch("resize");
    dispatch("scroll");
    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "520px",
      "pinch zoom does not overwrite the keyboard viewport height");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "136px",
      "pinch zoom does not overwrite the panned viewport top");

    viewportState.scale = 1;
    viewportState.height = 844;
    viewportState.offsetTop = 0;
    dispatch("resize");
    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "844px",
      "restoring the visual viewport returns the app to full height");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "0px");

    cleanup();
    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "");
    viewportState.height = 300;
    viewportState.offsetTop = 80;
    dispatch("resize");
    dispatch("scroll");
    assert.equal(element.style.getPropertyValue("--app-viewport-height"), "",
      "removed listeners do not update the element after cleanup");
    assert.equal(element.style.getPropertyValue("--app-viewport-top"), "");
  } finally {
    dom.window.close();
  }
});
