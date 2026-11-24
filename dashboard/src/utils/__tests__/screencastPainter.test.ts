import { describe, expect, it, vi } from "vitest";
import { createScreencastPainter } from "../browserCanvas";

/** Minimal Blob polyfill for Node test env (jsdom lacks createImageBitmap). */
function jpegBlob(size = 2): Blob {
  return new Blob([new Uint8Array([0xff, 0xd8, ...new Array(size).fill(0)])], {
    type: "image/jpeg",
  });
}

/** jsdom has no canvas backend: stub a 2D context so the blit is observable. */
function fakeCanvas(): { canvas: HTMLCanvasElement; drawImage: ReturnType<typeof vi.fn> } {
  const canvas = document.createElement("canvas");
  const drawImage = vi.fn();
  const ctx = {
    imageSmoothingEnabled: false,
    imageSmoothingQuality: "low",
    drawImage,
  };
  vi.spyOn(canvas, "getContext").mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  return { canvas, drawImage };
}

describe("createScreencastPainter", () => {
  it("decodes only the first and latest frame while decoding is busy", async () => {
    const { canvas, drawImage } = fakeCanvas();
    const painter = createScreencastPainter(canvas);

    const realCIB = (globalThis as Record<string, unknown>).createImageBitmap;
    let totalDecodes = 0;
    let releaseFirstDecode: (() => void) | null = null;
    const gate = new Promise<void>((resolve) => {
      releaseFirstDecode = resolve;
    });
    (globalThis as Record<string, unknown>).createImageBitmap = async () => {
      totalDecodes += 1;
      await gate; // only the first decode is gated; later ones pass through
      return { width: 2, height: 2, close: vi.fn() } as unknown as ImageBitmap;
    };

    try {
      painter.paint(jpegBlob(1)); // starts decoding, blocked on the gate
      painter.paint(jpegBlob(2)); // queued as "latest"
      painter.paint(jpegBlob(3)); // supersedes frame 2 — never decoded
      releaseFirstDecode?.();
      await new Promise((r) => setTimeout(r, 30));

      // Frame 2 must be dropped without a decode: first + latest only.
      expect(totalDecodes).toBe(2);
      // Both decoded bitmaps are blitted (the second one is the newest).
      expect(drawImage).toHaveBeenCalledTimes(2);
      expect(canvas.width).toBe(2);
      expect(canvas.height).toBe(2);
    } finally {
      (globalThis as Record<string, unknown>).createImageBitmap = realCIB;
      painter.dispose();
    }
  });

  it("ignores paint after dispose", () => {
    const { canvas, drawImage } = fakeCanvas();
    const painter = createScreencastPainter(canvas);
    painter.dispose();
    // Must not throw; nothing is drawn.
    painter.paint(jpegBlob());
    expect(drawImage).not.toHaveBeenCalled();
    expect(canvas.width).toBe(300); // jsdom default, untouched
  });
});
