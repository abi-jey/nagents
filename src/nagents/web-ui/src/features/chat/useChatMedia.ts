import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { loadMedia, mediaBudget } from "./deliveries.js";

type MediaOptions = {
  token: string;
  path: string;
  asset: { media_type: string; byte_length: number };
  enabled?: boolean;
  hold?: boolean;
};
type MediaState = { url: string; status: "waiting" | "loading" | "ready" | "error"; error: string };
type MediaController = { setHold: (hold: boolean) => void; retry: () => void };
type LoadedMedia = Awaited<ReturnType<typeof loadMedia>>;
const waiting: MediaState = { url: "", status: "waiting", error: "" };

/** Own a single authenticated media URL while it is near the viewport or held. */
export function useChatMedia({ token, path, asset, enabled = true, hold = false }: MediaOptions): MediaState & {
  container: RefObject<HTMLElement | null>; retry: () => void;
} {
  const container = useRef<HTMLElement | null>(null);
  const current = useRef<MediaController | null>(null);
  const { media_type, byte_length } = asset;
  const identity = JSON.stringify([token, path, media_type, byte_length]);
  const [state, setState] = useState<MediaState & { identity: string }>({ ...waiting, identity });

  useLayoutEffect(() => {
    let active = true, near = false, held = hold, attempted = false;
    let request: AbortController | null = null;
    let retained: LoadedMedia | null = null;
    const publish = (next: MediaState) => { if (active) setState({ ...next, identity }); };
    const release = () => {
      const previous = request;
      request = null;
      previous?.abort();
      retained?.release();
      retained = null;
    };
    const reconcile = () => {
      if (!active) return;
      if (!near && !held) {
        attempted = false;
        release();
        publish(waiting);
        return;
      }
      if (attempted) return;
      attempted = true;
      const controller = new AbortController();
      request = controller;
      publish(waiting);
      const isCurrent = () => active && request === controller && !controller.signal.aborted;
      void loadMedia(token, path, { media_type, byte_length }, controller.signal, mediaBudget, true, () => {
        if (isCurrent()) publish({ url: "", status: "loading", error: "" });
      }).then(result => {
        if (!isCurrent()) { result.release(); return; }
        request = null;
        retained = result;
        publish({ url: result.url, status: "ready", error: "" });
      }).catch(error => {
        if (!isCurrent()) return;
        request = null;
        publish({ url: "", status: "error", error: error instanceof Error ? error.message : "Media is unavailable." });
      });
    };
    const controller: MediaController = {
      setHold: value => { if (held !== value) { held = value; reconcile(); } },
      retry: () => {
        if (!active || request || retained) return;
        attempted = false;
        reconcile();
      },
    };
    publish(waiting);
    let observer: IntersectionObserver | undefined;
    const node = container.current;
    if (enabled && token && path && node) {
      current.current = controller;
      if (typeof IntersectionObserver === "undefined") {
        near = true;
      } else {
        observer = new IntersectionObserver(entries => {
          if (!active) return;
          const entry = entries.filter(value => value.target === node).at(-1);
          if (!entry || near === entry.isIntersecting) return;
          near = entry.isIntersecting;
          reconcile();
        }, { rootMargin: "200px" });
        observer.observe(node);
      }
      reconcile();
    }
    return () => {
      active = false;
      observer?.disconnect();
      if (current.current === controller) current.current = null;
      release();
    };
  }, [identity, enabled, token, path, media_type, byte_length]);

  // Changing a playing/lightbox hold updates ownership without replacing the
  // observer, aborting a fetch, or refetching the current identity.
  useLayoutEffect(() => { current.current?.setHold(hold); }, [hold]);
  const retry = useCallback(() => { current.current?.retry(); }, []);
  const visibleState = enabled && token && path && state.identity === identity ? state : waiting;
  return { container, url: visibleState.url, status: visibleState.status, error: visibleState.error, retry };
}
