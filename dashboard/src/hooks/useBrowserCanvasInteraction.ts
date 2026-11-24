import { type RefObject } from "react";
import { useCanvasRemotePointer } from "./useCanvasRemotePointer";
import type { ViewportSize } from "../utils/browserCanvas";

interface BrowserCanvasInteractionOptions {
  enabled: boolean;
  canvasRef: RefObject<HTMLCanvasElement | null>;
  onEvent: (event: Record<string, unknown>) => void;
  /** Remote page viewport — the coordinate space pointer events are sent in. */
  viewport?: ViewportSize | null;
}

/** Browser screencast canvas interaction (shared pointer protocol). */
export function useBrowserCanvasInteraction({
  enabled,
  canvasRef,
  onEvent,
  viewport = null,
}: BrowserCanvasInteractionOptions) {
  return useCanvasRemotePointer({ enabled, canvasRef, onEvent, viewport });
}
