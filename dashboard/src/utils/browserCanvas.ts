export interface CanvasPoint {
  x: number;
  y: number;
}

/**
 * The remote page viewport in CSS pixels — the coordinate space pointer events
 * must be expressed in. It is *not* the size of a decoded frame: screencast
 * frames can be scaled down by the adaptive profile, while `canvas.width` only
 * ever holds the viewport.
 */
export interface ViewportSize {
  width: number;
  height: number;
}

function clampCoord(value: number, max: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value < 0) return 0;
  if (value > max) return max;
  return value;
}

/**
 * Map a screen pointer position to remote viewport coordinates — the space
 * `Input.dispatchMouseEvent` expects.
 *
 * `getBoundingClientRect` is the *element* box, and the canvas is laid out with
 * `object-fit: contain`, so the remote viewport is letterboxed inside it.
 * Passing `viewport` undoes that fit (scale + centring offset) and yields true
 * page coordinates regardless of how small the decoded frame is — the adaptive
 * profile can shrink frames to 50% without moving a single click.
 *
 * Without `viewport` (unknown session) we fall back to the legacy assumption
 * that the backing store already is the coordinate space.
 */
export function getCanvasCoords(
  canvas: HTMLCanvasElement | null,
  e: { clientX: number; clientY: number },
  viewport: ViewportSize | null = null,
): CanvasPoint {
  if (!canvas) return { x: 0, y: 0 };
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };

  if (viewport && viewport.width > 0 && viewport.height > 0) {
    const scale = Math.min(
      rect.width / viewport.width,
      rect.height / viewport.height,
    );
    if (scale > 0) {
      const offsetX = (rect.width - viewport.width * scale) / 2;
      const offsetY = (rect.height - viewport.height * scale) / 2;
      return {
        x: clampCoord(
          Math.round((e.clientX - rect.left - offsetX) / scale),
          viewport.width,
        ),
        y: clampCoord(
          Math.round((e.clientY - rect.top - offsetY) / scale),
          viewport.height,
        ),
      };
    }
  }

  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: Math.round((e.clientX - rect.left) * scaleX),
    y: Math.round((e.clientY - rect.top) * scaleY),
  };
}

/**
 * Size the backing store for one decoded frame.
 *
 * With a known viewport the canvas stays at the viewport size and the frame is
 * scaled to fill it, so `getCanvasCoords` keeps returning true remote
 * coordinates no matter what the adaptive profile did to the frame. Without
 * one (unknown session) we fall back to the frame's own size.
 */
function sizeBackingStore(
  canvas: HTMLCanvasElement,
  frameWidth: number,
  frameHeight: number,
  viewport: ViewportSize | null,
): { width: number; height: number } {
  const width = viewport?.width || frameWidth;
  const height = viewport?.height || frameHeight;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return { width, height };
}

/** Paint a JPEG base64 frame onto a canvas (poll mode / bridge relay). */
export function paintBase64JpegToCanvas(
  canvas: HTMLCanvasElement | null,
  base64Data: string,
  viewport: ViewportSize | null = null,
): void {
  if (!canvas) return;
  const img = new Image();
  img.onload = () => {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const target = sizeBackingStore(canvas, img.width, img.height, viewport);
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) {
      ctx.imageSmoothingQuality = "high";
    }
    ctx.drawImage(img, 0, 0, target.width, target.height);
  };
  img.src = `data:image/jpeg;base64,${base64Data}`;
}

/**
 * Decoder worker source: receives a JPEG Blob, decodes it via
 * `createImageBitmap`, and posts the bitmap back as a transferable (zero
 * copy). Off-main-thread decode removes the last main-thread cost during
 * bursts of large frames.
 */
const SCREENCAST_WORKER_SRC = `self.onmessage = async (ev) => {
  const { seq, blob } = ev.data;
  try {
    const bitmap = await createImageBitmap(blob, { imageOrientation: "none" });
    self.postMessage({ seq, bitmap }, [bitmap]);
  } catch {
    self.postMessage({ seq, error: true });
  }
};`;

function createDecodeWorker(): { worker: Worker; url: string } | null {
  try {
    if (
      typeof Worker === "undefined" ||
      typeof Blob === "undefined" ||
      typeof URL === "undefined" ||
      typeof URL.createObjectURL !== "function"
    ) {
      return null;
    }
    const blob = new Blob([SCREENCAST_WORKER_SRC], { type: "text/javascript" });
    const url = URL.createObjectURL(blob);
    return { worker: new Worker(url), url };
  } catch {
    return null;
  }
}

/**
 * Latest-frame-wins paint scheduler for screencast streams.
 *
 * Decoding is async, so frames can finish out of order and pile up during
 * bursts. Keeping only the most recent pending frame and drawing under a
 * `requestAnimationFrame` gate drops stale frames instead of queuing them —
 * the perceived latency stays bounded at one decode.
 *
 * When `Worker` is available, JPEG decode happens off the main thread and
 * the decoded `ImageBitmap` returns as a transferable; the main thread only
 * blits. Otherwise it falls back to main-thread `createImageBitmap`.
 */
export function createScreencastPainter(
  canvas: HTMLCanvasElement | null,
  initialViewport: ViewportSize | null = null,
): {
  paint: (blob: Blob) => void;
  setViewport: (viewport: ViewportSize | null) => void;
  dispose: () => void;
} {
  let latest: Blob | null = null;
  let decoding = false;
  let disposed = false;
  let lastDrawnSeq = 0;
  let viewport = initialViewport;

  const setViewport = (next: ViewportSize | null) => {
    viewport = next;
  };

  const workerHandle = createDecodeWorker();

  const blit = (bitmap: ImageBitmap) => {
    if (!canvas) {
      bitmap.close();
      return;
    }
    const ctx = canvas.getContext("2d");
    if (!ctx) {
      bitmap.close();
      return;
    }
    const target = sizeBackingStore(canvas, bitmap.width, bitmap.height, viewport);
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) {
      ctx.imageSmoothingQuality = "high";
    }
    ctx.drawImage(bitmap, 0, 0, target.width, target.height);
    bitmap.close();
  };

  const scheduleDraw = (bitmap: ImageBitmap, seq: number) => {
    const render = () => {
      if (disposed || seq < lastDrawnSeq) {
        bitmap.close();
        return;
      }
      lastDrawnSeq = seq;
      blit(bitmap);
    };
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(render);
    } else {
      render();
    }
  };

  const draw = async (blob: Blob) => {
    decoding = true;
    try {
      const bitmap = await createImageBitmap(blob, { imageOrientation: "none" });
      if (disposed || !canvas) {
        bitmap.close();
        return;
      }
      scheduleDraw(bitmap, Number.MAX_SAFE_INTEGER);
    } catch {
      // Undecodable frame — drop it; the next screencast frame supersedes.
    } finally {
      decoding = false;
      if (latest) {
        const next = latest;
        latest = null;
        void draw(next);
      }
    }
  };

  const paintOnMainThread = (blob: Blob) => {
    void draw(blob);
  };

  if (workerHandle) {
    const { worker, url } = workerHandle;
    let inFlight = false;
    let pending: { seq: number; blob: Blob } | null = null;
    let seqCounter = 0;

    worker.onmessage = (ev: MessageEvent) => {
      inFlight = false;
      const { seq, bitmap, error } = (ev.data ?? {}) as {
        seq: number;
        bitmap?: ImageBitmap;
        error?: boolean;
      };
      if (disposed) {
        bitmap?.close();
      } else if (!error && bitmap) {
        scheduleDraw(bitmap, seq);
      }
      if (pending && !disposed) {
        const next = pending;
        pending = null;
        inFlight = true;
        worker.postMessage({ seq: next.seq, blob: next.blob });
      }
    };

    return {
      paint(blob: Blob) {
        if (disposed || !canvas) return;
        seqCounter += 1;
        if (inFlight) {
          // Replace any queued frame: only the newest one matters.
          pending = { seq: seqCounter, blob };
          return;
        }
        inFlight = true;
        try {
          worker.postMessage({ seq: seqCounter, blob });
        } catch {
          inFlight = false;
          paintOnMainThread(blob);
        }
      },
      setViewport,
      dispose() {
        disposed = true;
        pending = null;
        worker.terminate();
        URL.revokeObjectURL(url);
      },
    };
  }

  return {
    paint(blob: Blob) {
      if (disposed || !canvas) return;
      if (decoding) {
        // Replace any queued frame: only the newest one matters.
        latest = blob;
        return;
      }
      void draw(blob);
    },
    setViewport,
    dispose() {
      disposed = true;
      latest = null;
    },
  };
}

/** Paint a screenshot blob onto a canvas (HTTP polling). */
export async function paintBlobToCanvas(
  canvas: HTMLCanvasElement | null,
  blob: Blob,
): Promise<boolean> {
  if (!canvas) return false;
  const bitmap = await createImageBitmap(blob);
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0);
  bitmap.close();
  return true;
}

export function clearCanvas(canvas: HTMLCanvasElement | null): void {
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
}
