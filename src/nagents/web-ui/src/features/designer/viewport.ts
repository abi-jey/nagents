export interface Viewport { x: number; y: number; width: number; height: number; scale: number }
export interface Point { x: number; y: number }

export function resizeViewport(view: Viewport, width: number, height: number): Viewport {
  if (width <= 0 || height <= 0) return view;
  return {
    ...view, width, height,
    x: view.width > 0 ? view.x + (view.width - width) / (2 * view.scale) : view.x,
    y: view.height > 0 ? view.y + (view.height - height) / (2 * view.scale) : view.y,
  };
}

export function zoomViewport(view: Viewport, factor: number): Viewport {
  const scale = Math.max(.05, Math.min(4, view.scale * factor));
  return { ...view, scale,
    x: view.x + view.width / (2 * view.scale) - view.width / (2 * scale),
    y: view.y + view.height / (2 * view.scale) - view.height / (2 * scale),
  };
}

export function panViewport(view: Viewport, dx: number, dy: number): Viewport {
  return { ...view, x: view.x - dx / view.scale, y: view.y - dy / view.scale };
}

export function fitViewport(view: Viewport, nodes: Point[]): Viewport {
  if (!nodes.length || !view.width || !view.height) return view;
  const left = Math.min(...nodes.map((node) => node.x)), top = Math.min(...nodes.map((node) => node.y));
  const right = Math.max(...nodes.map((node) => node.x + 180)), bottom = Math.max(...nodes.map((node) => node.y + 68));
  const scale = Math.min(1, view.width / (right - left + 80), view.height / (bottom - top + 80));
  return { ...view, scale, x: (left + right - view.width / scale) / 2, y: (top + bottom - view.height / scale) / 2 };
}
