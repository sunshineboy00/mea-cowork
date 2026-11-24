export interface CanvasPoint {
  x: number;
  y: number;
}

/** Map a screen pointer position to canvas pixel coordinates. */
export function getCanvasCoords(
  canvas: HTMLCanvasElement | null,
  e: { clientX: number; clientY: number },
): CanvasPoint {
  if (!canvas) return { x: 0, y: 0 };
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: Math.round((e.clientX - rect.left) * scaleX),
    y: Math.round((e.clientY - rect.top) * scaleY),
  };
}

/** Paint a JPEG base64 frame onto a canvas (WebSocket stream). */
export function paintBase64JpegToCanvas(
  canvas: HTMLCanvasElement | null,
  base64Data: string,
): void {
  if (!canvas) return;
  const img = new Image();
  img.onload = () => {
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    if (canvas.width !== img.width || canvas.height !== img.height) {
      canvas.width = img.width;
      canvas.height = img.height;
    }
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) {
      ctx.imageSmoothingQuality = "high";
    }
    ctx.drawImage(img, 0, 0);
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
): { paint: (blob: Blob) => void; dispose: () => void } {
  let latest: Blob | null = null;
  let decoding = false;
  let disposed = false;
  let lastDrawnSeq = 0;

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
    if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
    }
    ctx.imageSmoothingEnabled = true;
    if ("imageSmoothingQuality" in ctx) {
      ctx.imageSmoothingQuality = "high";
    }
    ctx.drawImage(bitmap, 0, 0);
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
