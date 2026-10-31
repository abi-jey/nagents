import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { PanelDivider } from "./PanelDivider.js";
import { fitPanelWidths, panelWidthKey, parsePanelWidths, usePanelLayout } from "./usePanelLayout.js";

test("panel preferences validate independently and responsive fitting preserves the reading width", () => {
  for (const raw of [null, "invalid", "[]", '{"version":2,"sessions":300}', '{"version":1,"sessions":"300","team":null}'])
    assert.deepEqual(parsePanelWidths(raw), { sessions: 240, team: 292 });
  assert.deepEqual(parsePanelWidths('{"version":1,"sessions":9999,"team":10}'), { sessions: 380, team: 232 });
  assert.deepEqual(parsePanelWidths('{"version":1,"sessions":281.8,"team":311}'), { sessions: 282, team: 311 });
  const preferred = { sessions: 380, team: 420 };
  for (const width of [761, 800, 1100, 1101, 1200, 1440]) {
    for (const closed of [false, true]) for (const teamOpen of [false, true]) {
      const fitted = fitPanelWidths(preferred, width, closed, teamOpen);
      const gutters = width > 1100 ? 32 : 16;
      assert.ok(width - fitted.sessions - fitted.team - gutters >= 360, `Reading width at ${width}`);
      if (closed) assert.equal(fitted.sessions, 56);
      if (width <= 1100) assert.equal(fitted.team, 0, "The tablet Team drawer does not consume a grid track");
      else if (!teamOpen) assert.equal(fitted.team, 44);
    }
  }
  assert.deepEqual(preferred, { sessions: 380, team: 420 }, "Viewport fitting never overwrites saved widths");
});

test("production dividers resize, cancel, collapse, restore and persist without replacing the reading surface", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, value });
  let viewportWidth = 1400;
  Object.defineProperty(dom.window, "innerWidth", { configurable: true, get: () => viewportWidth });
  Object.assign(dom.window.HTMLElement.prototype, {
    setPointerCapture(this: HTMLElement, id: number) { this.dataset.capture = String(id); },
    hasPointerCapture(this: HTMLElement, id: number) { return this.dataset.capture === String(id); },
    releasePointerCapture(this: HTMLElement) { delete this.dataset.capture; this.dispatchEvent(new dom.window.Event("lostpointercapture", { bubbles: true })); },
  });
  const root = createRoot(document.getElementById("root")!);
  function Probe() {
    const shell = useRef<HTMLDivElement>(null);
    const [closed, setClosed] = useState(false), [teamOpen, setTeamOpen] = useState(true);
    const layout = usePanelLayout(shell, closed, teamOpen);
    return createElement("div", { ref: shell, className: "panel-layout-shell", style: layout.style, "data-resizing": layout.dragging },
      createElement(PanelDivider, { side: "sessions", collapsed: closed, setCollapsed: setClosed, layout }),
      createElement("section", { id: "reading" }, createElement("textarea", { defaultValue: "Unsent draft" })),
      createElement(PanelDivider, { side: "team", collapsed: !teamOpen, setCollapsed: value => setTeamOpen(!value), layout }));
  }
  const separator = (side: "sessions" | "team") => document.querySelector<HTMLDivElement>(`.panel-divider-${side} [role='separator']`)!;
  const value = (side: "sessions" | "team") => Number(separator(side).getAttribute("aria-valuenow"));
  async function pointer(side: "sessions" | "team", type: string, x: number) {
    const event = new dom.window.MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX: x });
    Object.defineProperty(event, "pointerId", { value: 7 });
    await act(async () => separator(side).dispatchEvent(event));
  }
  async function key(side: "sessions" | "team", key: string, shiftKey = false) {
    await act(async () => separator(side).dispatchEvent(new dom.window.KeyboardEvent("keydown", { key, shiftKey, bubbles: true, cancelable: true })));
  }
  try {
    await act(async () => root.render(createElement(Probe)));
    const reading = document.getElementById("reading")!, draft = reading.querySelector("textarea")!;
    reading.scrollTop = 83;
    await pointer("sessions", "pointerdown", 240);
    await pointer("sessions", "pointermove", 320);
    assert.equal(value("sessions"), 320);
    assert.equal(JSON.parse(window.localStorage.getItem(panelWidthKey)!).sessions, 240, "Intermediate dragging does not write storage");
    await pointer("sessions", "pointerup", 320);
    assert.equal(JSON.parse(window.localStorage.getItem(panelWidthKey)!).sessions, 320);

    await pointer("team", "pointerdown", 1200);
    await pointer("team", "pointermove", 1136);
    await pointer("team", "pointerup", 1136);
    assert.equal(value("team"), 356, "Dragging the right divider left grows Team");
    await key("team", "ArrowLeft", true); assert.equal(value("team"), 388);
    await key("team", "Home"); assert.equal(value("team"), 232);
    await key("team", "End"); assert.equal(value("team"), 420);
    await act(async () => separator("team").dispatchEvent(new dom.window.MouseEvent("dblclick", { bubbles: true })));
    assert.equal(value("team"), 292);
    await key("team", "Enter"); assert.equal(value("team"), 44);
    await key("team", "Enter"); assert.equal(value("team"), 292);

    await pointer("sessions", "pointerdown", 320);
    await pointer("sessions", "pointermove", 360);
    await key("sessions", "Escape");
    assert.equal(value("sessions"), 320);
    assert.equal(separator("sessions").hasAttribute("data-capture"), false);
    await pointer("sessions", "pointerdown", 320);
    await pointer("sessions", "pointermove", 350);
    await pointer("sessions", "pointercancel", 350);
    assert.equal(value("sessions"), 320);
    await act(async () => document.querySelector<HTMLButtonElement>(".panel-divider-sessions .panel-notch")!.click());
    assert.equal(value("sessions"), 56);
    await act(async () => document.querySelector<HTMLButtonElement>(".panel-divider-sessions .panel-notch")!.click());
    assert.equal(value("sessions"), 320);
    assert.equal(document.getElementById("reading"), reading);
    assert.equal(reading.scrollTop, 83);
    assert.equal(reading.querySelector("textarea"), draft);
    assert.equal(draft.value, "Unsent draft");

    for (const side of ["sessions", "team"] as const) for (const cancel of ["escape", "pointer"] as const) {
      const saved = value(side);
      await act(async () => document.querySelector<HTMLButtonElement>(`.panel-divider-${side} .panel-notch`)!.click());
      const rail = value(side);
      await pointer(side, "pointerdown", 500);
      assert.equal(value(side), saved, "Dragging a collapsed panel first restores its saved width");
      await pointer(side, "pointermove", 520);
      if (cancel === "escape") await key(side, "Escape"); else await pointer(side, "pointercancel", 520);
      assert.equal(value(side), rail, `${cancel} restores the original collapsed state for ${side}`);
      await act(async () => document.querySelector<HTMLButtonElement>(`.panel-divider-${side} .panel-notch`)!.click());
      assert.equal(value(side), saved);
    }

    await act(async () => { viewportWidth = 900; dom.window.dispatchEvent(new dom.window.Event("resize")); });
    assert.equal(value("team"), 0);
    assert.equal(JSON.parse(window.localStorage.getItem(panelWidthKey)!).team, 292);
    await act(async () => root.render(createElement("div")));
    await act(async () => { viewportWidth = 1400; root.render(createElement(Probe)); });
    assert.equal(value("sessions"), 320); assert.equal(value("team"), 292);
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of Object.keys(globals)) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  }
});
