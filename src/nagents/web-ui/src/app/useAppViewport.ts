import { useLayoutEffect, type RefObject } from "react";

/** Follow the visible screen when a mobile keyboard resizes or pans it. */
export function trackAppViewport(element: HTMLElement, viewport: VisualViewport): () => void {
  const update = () => {
    // Pinch zoom must keep its normal browser panning and magnification.
    if (viewport.scale !== 1) return;
    element.style.setProperty("--app-viewport-height", `${viewport.height}px`);
    element.style.setProperty("--app-viewport-top", `${viewport.offsetTop}px`);
  };
  viewport.addEventListener("resize", update);
  viewport.addEventListener("scroll", update);
  update();
  return () => {
    viewport.removeEventListener("resize", update);
    viewport.removeEventListener("scroll", update);
    element.style.removeProperty("--app-viewport-height");
    element.style.removeProperty("--app-viewport-top");
  };
}

export function useAppViewport(root: RefObject<HTMLDivElement | null>) {
  useLayoutEffect(() => {
    if (root.current && window.visualViewport) return trackAppViewport(root.current, window.visualViewport);
  }, [root]);
}
