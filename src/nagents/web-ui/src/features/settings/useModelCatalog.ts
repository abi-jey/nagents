import { useEffect, useRef, useState } from "react";
import {
  modelCatalogFailure,
  readModelCatalog,
  type ModelCatalogState,
} from "./modelCatalog.js";

export function useModelCatalog(token: string) {
  const [state, setState] = useState<ModelCatalogState>({
    loading: false,
    attempted: false,
    error: "",
  });
  const reading = useRef<AbortController | null>(null);

  useEffect(() => () => reading.current?.abort(), []);

  async function fetchModels() {
    if (reading.current) return;
    const controller = new AbortController();
    reading.current = controller;
    setState((current) => ({ ...current, loading: true, attempted: true, error: "" }));
    try {
      const catalog = await readModelCatalog(token, controller.signal);
      if (!controller.signal.aborted && reading.current === controller) {
        setState({ catalog, loading: false, attempted: true, error: "" });
      }
    } catch (cause) {
      if (!controller.signal.aborted && reading.current === controller) {
        setState((current) => ({
          ...current,
          loading: false,
          error: modelCatalogFailure(cause),
        }));
      }
    } finally {
      if (reading.current === controller) reading.current = null;
    }
  }

  return { ...state, fetchModels };
}
