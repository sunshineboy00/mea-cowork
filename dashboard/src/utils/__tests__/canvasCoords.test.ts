import { describe, expect, it, vi } from "vitest";
import {
  createScreencastPainter,
  getCanvasCoords,
  paintBase64JpegToCanvas,
} from "../browserCanvas";

/**
 * Regression tests for the remote coordinate space.
 *
 * Background: the screencast profile can shrink frames (100% → 50%) and the
 * canvas is laid out with `object-fit: contain`. Mapping pointer positions
 * through `canvas.width` therefore sent clicks to the wrong remote element —
 * scaled toward the top-left — so inputs never took focus and hover echoed the
 * wrong cursor. These tests pin the contract: coordinates come from the remote
 * *viewport*, never from the decoded frame.
 */

function canvasIn(box: { left: number; top: number; width: number; height: number }) {
  const canvas = document.createElement("canvas");
  vi.spyOn(canvas, "getBoundingClientRect").mockReturnValue({
    ...box,
    right: box.left + box.width,
    bottom: box.top + box.height,
    x: box.left,
    y: box.top,
    toJSON: () => ({}),
  } as DOMRect);
  return canvas;
}

describe("getCanvasCoords", () => {
  it("maps through the backing store when no viewport is known (legacy)", () => {
    const canvas = canvasIn({ left: 0, top: 0, width: 400, height: 300 });
    canvas.width = 800; // e.g. a 2× screenshot
    canvas.height = 600;

    expect(getCanvasCoords(canvas, { clientX: 200, clientY: 150 })).toEqual({
      x: 400,
      y: 300,
    });
  });

  it("returns true viewport coordinates even when the frame is half size", () => {
    // Viewport 1024×768; the canvas element box happens to be smaller because
    // the pane is narrow. A decoded frame at 512×384 is irrelevant here.
    const canvas = canvasIn({ left: 10, top: 20, width: 512, height: 384 });
    canvas.width = 1024;
    canvas.height = 768;

    expect(
      getCanvasCoords(
        canvas,
        { clientX: 10 + 256, clientY: 20 + 192 },
        { width: 1024, height: 768 },
      ),
    ).toEqual({ x: 512, y: 384 });
  });

  it("compensates the object-fit letterbox offset", () => {
    // 4:3 viewport letterboxed into a 2:1 element box: 133.33px bars each side.
    const canvas = canvasIn({ left: 0, top: 0, width: 800, height: 400 });
    const viewport = { width: 1024, height: 768 };

    // Element centre → viewport centre.
    expect(
      getCanvasCoords(canvas, { clientX: 400, clientY: 200 }, viewport),
    ).toEqual({ x: 512, y: 384 });

    // Bottom-right corner of the *content* box → viewport bottom-right.
    const scale = 400 / 768;
    const contentRight = (800 - 1024 * scale) / 2 + 1024 * scale;
    expect(
      getCanvasCoords(canvas, { clientX: contentRight, clientY: 400 }, viewport),
    ).toEqual({ x: 1024, y: 768 });
  });

  it("clamps clicks inside the letterbox bars to the viewport edge", () => {
    const canvas = canvasIn({ left: 0, top: 0, width: 800, height: 400 });
    const viewport = { width: 1024, height: 768 };

    // x=10 is inside the left bar: it must not become a negative coordinate.
    expect(
      getCanvasCoords(canvas, { clientX: 10, clientY: 200 }, viewport),
    ).toEqual({ x: 0, y: 384 });
  });

  it("is unaffected by the backing store once a viewport is supplied", () => {
    const viewport = { width: 1024, height: 768 };
    const box = { left: 0, top: 0, width: 512, height: 384 };

    const halfFrame = canvasIn(box);
    halfFrame.width = 512; // degraded profile
    halfFrame.height = 384;

    const fullFrame = canvasIn(box);
    fullFrame.width = 1024;
    fullFrame.height = 768;

    const point = { clientX: 128, clientY: 96 };
    expect(getCanvasCoords(halfFrame, point, viewport)).toEqual(
      getCanvasCoords(fullFrame, point, viewport),
    );
  });

  it("degrades to the origin for a zero-sized element", () => {
    const canvas = canvasIn({ left: 0, top: 0, width: 0, height: 0 });
    expect(
      getCanvasCoords(canvas, { clientX: 5, clientY: 5 }, { width: 100, height: 100 }),
    ).toEqual({ x: 0, y: 0 });
  });
});

function stubCanvasContext(canvas: HTMLCanvasElement) {
  const ctx = {
    imageSmoothingEnabled: false,
    imageSmoothingQuality: "low",
    drawImage: vi.fn(),
  };
  vi.spyOn(canvas, "getContext").mockReturnValue(
    ctx as unknown as CanvasRenderingContext2D,
  );
  return ctx;
}

describe("backing store sizing", () => {
  it("keeps the canvas at the viewport size whatever the frame size is", async () => {
    const canvas = document.createElement("canvas");
    const ctx = stubCanvasContext(canvas);
    const painter = createScreencastPainter(canvas, { width: 1024, height: 768 });

    const realCIB = (globalThis as Record<string, unknown>).createImageBitmap;
    // A degraded frame: half the viewport in each dimension.
    (globalThis as Record<string, unknown>).createImageBitmap = async () =>
      ({ width: 512, height: 384, close: vi.fn() }) as unknown as ImageBitmap;

    try {
      painter.paint(new Blob([new Uint8Array([0xff, 0xd8])], { type: "image/jpeg" }));
      await new Promise((r) => setTimeout(r, 30));

      expect(canvas.width).toBe(1024);
      expect(canvas.height).toBe(768);
      // The bitmap is upscaled to fill the viewport-sized store.
      expect(ctx.drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 1024, 768);
    } finally {
      (globalThis as Record<string, unknown>).createImageBitmap = realCIB;
      painter.dispose();
    }
  });

  it("adopts a newly negotiated viewport without changing the coordinate space", async () => {
    const canvas = document.createElement("canvas");
    const ctx = stubCanvasContext(canvas);
    const painter = createScreencastPainter(canvas, { width: 800, height: 600 });

    const realCIB = (globalThis as Record<string, unknown>).createImageBitmap;
    (globalThis as Record<string, unknown>).createImageBitmap = async () =>
      ({ width: 400, height: 300, close: vi.fn() }) as unknown as ImageBitmap;

    try {
      painter.paint(new Blob([new Uint8Array([0xff, 0xd8])], { type: "image/jpeg" }));
      await new Promise((r) => setTimeout(r, 30));
      expect(canvas.width).toBe(800);

      painter.setViewport({ width: 1200, height: 900 });
      painter.paint(new Blob([new Uint8Array([0xff, 0xd8])], { type: "image/jpeg" }));
      await new Promise((r) => setTimeout(r, 30));

      expect(canvas.width).toBe(1200);
      expect(canvas.height).toBe(900);
      expect(ctx.drawImage).toHaveBeenLastCalledWith(
        expect.anything(),
        0,
        0,
        1200,
        900,
      );
    } finally {
      (globalThis as Record<string, unknown>).createImageBitmap = realCIB;
      painter.dispose();
    }
  });

  it("sizes the poll-mode canvas to the viewport too", () => {
    const canvas = document.createElement("canvas");
    const ctx = stubCanvasContext(canvas);

    const realImage = (globalThis as Record<string, unknown>).Image;
    class FakeImage {
      width = 640;
      height = 480;
      onload: (() => void) | null = null;
      set src(_value: string) {
        setTimeout(() => this.onload?.(), 0);
      }
    }
    (globalThis as Record<string, unknown>).Image = FakeImage;

    return new Promise<void>((resolve) => {
      paintBase64JpegToCanvas(canvas, "aGVsbG8=", { width: 1280, height: 960 });
      setTimeout(() => {
        expect(canvas.width).toBe(1280);
        expect(canvas.height).toBe(960);
        expect(ctx.drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 1280, 960);
        (globalThis as Record<string, unknown>).Image = realImage;
        resolve();
      }, 20);
    });
  });
});
