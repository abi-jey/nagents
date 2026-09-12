import { useEffect, useState, useSyncExternalStore } from "react";
import { browserRecording, recordingSupport } from "./browser.js";
import { DictationController } from "./controller.js";
import { transcribe } from "./transport.js";

export function useDictation(token: string, sessionId: string, interrupted: boolean) {
  const [controller] = useState(() => new DictationController({ record: browserRecording, transcribe }));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);
  const [unsupported, setUnsupported] = useState("");
  useEffect(() => { setUnsupported(recordingSupport()); }, []);
  useEffect(() => {
    if (interrupted && controller.active) controller.cancel("Dictation interrupted by active work. Your draft is kept.");
  }, [controller, interrupted]);
  useEffect(() => () => {
    if (controller.active) controller.cancel("Dictation cancelled after changing connections or sessions. Your draft is kept.");
  }, [controller, token, sessionId]);
  useEffect(() => {
    const hide = () => controller.visibilityChanged(true);
    const visibility = () => controller.visibilityChanged(document.hidden);
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", hide);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", hide);
      controller.cancel("");
    };
  }, [controller]);
  return { controller, state, unfinished: controller.active, unsupported };
}
