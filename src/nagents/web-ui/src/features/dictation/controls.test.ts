import assert from "node:assert/strict";
import test from "node:test";
import { createElement, type ComponentProps, type ReactElement, type SubmitEvent } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Composer } from "../chat/Composer.js";
import { DictationControls, DictationReview, type DictationControlsProps } from "./DictationControls.js";
import { recordingSupport } from "./browser.js";
import { dictationConfig } from "./testFixtures.js";
import type { DictationState } from "./types.js";

function props(state: DictationState): DictationControlsProps {
  const unexpected = () => assert.fail("Rendering must not start capture, upload, insert, or submit");
  return {
    state, config: dictationConfig, unsupported: "", disabled: false,
    start: unexpected, stop: unexpected, cancel: unexpected, transcribe: unexpected,
    settings: unexpected,
  };
}

function reviewProps(state: DictationState) {
  const unexpected = () => assert.fail("Rendering must not edit, insert, or discard text");
  return { state, edit: unexpected, insert: unexpected, cancel: unexpected };
}

test("mic and review render accessible, escaped, explicitly non-submit controls without side effects", (t) => {
  const fetch = t.mock.method(globalThis, "fetch", async () => assert.fail("No fetch on mount"));
  const idle = renderToStaticMarkup(createElement(DictationControls, props({ phase: "idle", message: "" })));
  assert.match(idle, /aria-label="Record dictation"/);
  assert.match(idle, /aria-describedby="dictation-help dictation-status"/);
  assert.match(idle, /role="status" aria-live="polite" aria-atomic="true"/);
  const review = renderToStaticMarkup(createElement(DictationReview, reviewProps({ phase: "review", text: "<script> & edited", error: "Both are kept." })));
  assert.match(review, /<label for="dictation-review-text">Review transcription<\/label>/);
  assert.match(review, /aria-describedby="dictation-review-help dictation-review-error" aria-invalid="true"/);
  assert.match(review, /&lt;script&gt; &amp; edited/);
  assert.match(review, /role="alert"/);
  assert.match(review, /Insert into draft/);
  assert.match(review, /rows="2"/);
  assert.match(idle, /title="Record dictation"/);
  assert.doesNotMatch(idle, />Mic<|Microphone is off/);
  for (const html of [idle, review])
    for (const button of html.matchAll(/<button[^>]*>/g)) assert.match(button[0], /type="button"/);
  assert.equal(fetch.mock.callCount(), 0);
});

test("every unfinished phase exposes cancellation even when competing operations are blocked", () => {
  const states: DictationState[] = [
    { phase: "permission" }, { phase: "recording" }, { phase: "stopping" },
    { phase: "transcribing" }, { phase: "recorded", seconds: 1 },
    { phase: "review", text: "draft", error: "" },
  ];
  for (const state of states) {
    const html = renderToStaticMarkup(state.phase === "review"
      ? createElement(DictationReview, reviewProps(state))
      : createElement(DictationControls, { ...props(state), disabled: true }));
    assert.match(html, /<button[^>]*type="button"[^>]*aria-label="(?:Cancel|Discard) dictation"(?![^>]*disabled)[^>]*>/);
    assert.doesNotMatch(html, /Stop run/);
  }
  const capped = renderToStaticMarkup(createElement(DictationControls, props({ phase: "recorded", seconds: 1 })));
  assert.match(capped, /Nothing uploaded/);
  assert.match(capped, /Transcribe recording/);
});

test("unavailable and unsupported dictation exposes settings/help while manual input remains usable", () => {
  const controls = createElement(DictationControls, {
    ...props({ phase: "idle", message: "" }),
    config: { ...dictationConfig, available: false, status: "API key is not configured." },
  });
  const html = renderToStaticMarkup(controls);
  assert.match(html, /aria-label="Record dictation"[^>]*disabled=""/);
  assert.match(html, /API key is not configured/);
  const setup = /<button([^>]*)>Mic setup<\/button>/.exec(html);
  assert.ok(setup);
  // Visible text supplies the accessible name, including the exact words a
  // voice-control user can see. An unrelated aria-label must not override it.
  assert.doesNotMatch(setup[1], /aria-label(?:ledby)?=/);
  assert.match(setup[1], /type="button"/);
  const composer = renderToStaticMarkup(createElement(Composer, {
    inputRef: null, prompt: "Typed draft", setPrompt: () => undefined, demo: false,
    disabled: false, canSubmit: true, running: false, submit: () => undefined, cancel: () => undefined,
    dictation: controls,
  }));
  assert.doesNotMatch(composer, /<textarea[^>]*disabled/);
  assert.match(composer, /<button type="submit" class="primary">Send/);
});

test("all unfinished dictation phases preserve editable drafts and block form/Enter until normal Send is allowed", () => {
  let submitted = 0;
  const base: ComponentProps<typeof Composer> = {
    inputRef: null, prompt: "Latest unsent typing", setPrompt: () => undefined, demo: false,
    disabled: false, canSubmit: false, running: false,
    submit: () => { submitted++; }, cancel: () => assert.fail("Do not cancel chat"),
  };
  for (const state of [{ phase: "permission" }, { phase: "recording" }, { phase: "stopping" }, { phase: "transcribing" }, { phase: "recorded", seconds: 1 }, { phase: "review", text: "review", error: "" }] as DictationState[]) {
    const form = Composer({ ...base, dictation: createElement(DictationControls, props(state)), review: createElement(DictationReview, reviewProps(state)) }) as ReactElement<ComponentProps<"form">>;
    form.props.onSubmit?.({ preventDefault: () => undefined } as SubmitEvent<HTMLFormElement>);
    const children = form.props.children as ReactElement[];
    const textarea = children.find((child) => child.type === "textarea") as ReactElement<ComponentProps<"textarea">>;
    textarea.props.onKeyDown?.({ key: "Enter", shiftKey: false, nativeEvent: { isComposing: false }, preventDefault: () => undefined } as Parameters<NonNullable<ComponentProps<"textarea">["onKeyDown"]>>[0]);
    assert.equal(textarea.props.value, "Latest unsent typing");
    assert.equal(textarea.props.disabled, false);
  }
  assert.equal(submitted, 0);
  const ready = Composer({ ...base, canSubmit: true }) as ReactElement<ComponentProps<"form">>;
  ready.props.onSubmit?.({ preventDefault: () => undefined } as SubmitEvent<HTMLFormElement>);
  assert.equal(submitted, 1);
});

test("idle mic shares the Send row, optional help keeps instructions, and stale insertion feedback is screen-reader-only", () => {
  const state: DictationState = { phase: "idle", message: "Transcription inserted. Review your message, then choose Send." };
  const html = renderToStaticMarkup(createElement(Composer, {
    inputRef: null, prompt: "draft", setPrompt: () => undefined, demo: false,
    disabled: false, canSubmit: true, running: false, submit: () => undefined, cancel: () => undefined,
    dictation: createElement(DictationControls, props(state)),
  }));
  assert.match(html, /rows="1"/);
  assert.match(html, /<div class="composer-bottom">[\s\S]*aria-label="Record dictation"[\s\S]*type="submit"/);
  assert.match(html, /<details class="composer-help"><summary aria-label="Composer help"/);
  assert.match(html, /id="composer-help"/);
  assert.match(html, /id="dictation-status" class="sr-only"[^>]*>Transcription inserted/);
  assert.doesNotMatch(html, /class="bottom-note"|class="dictation-help"|Microphone is off/);
});

test("browser support detection is read-only and requires secure capture plus AudioWorklet", (t) => {
  const replace = (key: string, value: unknown) => {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { value, configurable: true });
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, key, previous);
      else Reflect.deleteProperty(globalThis, key);
    });
  };
  replace("isSecureContext", false);
  assert.match(recordingSupport(), /HTTPS/);
  replace("isSecureContext", true);
  replace("navigator", { mediaDevices: { getUserMedia: () => assert.fail("No permission prompt during detection") } });
  replace("AudioContext", class { get audioWorklet() { return {}; } });
  replace("AudioWorkletNode", undefined);
  assert.match(recordingSupport(), /does not support/);
  replace("AudioWorkletNode", class {});
  assert.equal(recordingSupport(), "");
});
