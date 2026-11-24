import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Button, Input, Tooltip } from "antd";
import {
  ArrowLeft,
  ArrowRight,
  Globe,
  RotateCcw,
  Star,
  AlertTriangle,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import cx from "classnames";

import type {
  BrowserStreamState,
  BrowserTab as StreamTab,
} from "../../hooks/useBrowserStream";
import { useBrowserCanvasInteraction } from "../../hooks/useBrowserCanvasInteraction";
import { useBrowserKeyboard } from "../../hooks/useBrowserKeyboard";
import {
  paintBase64JpegToCanvas,
  createScreencastPainter,
  type ViewportSize,
} from "../../utils/browserCanvas";
import { normalizeUrl } from "../../utils/normalizeUrl";
import { ChromeTabBar } from "../ChromeTabBar";
import StreamConnectingIndicator from "../StreamConnectingIndicator";
import styles from "./index.module.less";

/** Imperative handle so the parent can paint incoming frames onto the canvas. */
export interface BrowserViewerHandle {
  /** Legacy base64 JPEG frame (poll mode). */
  paintFrame: (base64Data: string) => void;
  /** Screencast binary frame (Blob). Uses the latest-frame-wins painter. */
  paintBlobFrame?: (blob: Blob) => void;
}

interface BrowserViewerProps {
  // --- stream state & actions (from useBrowserStream) ---
  status: BrowserStreamState;
  tabs: StreamTab[];
  switchTab: (tabId: number | string) => void;
  closeTab: (tabId: number | string) => void;
  newTab: () => void;
  sendEvent: (event: Record<string, unknown>) => boolean;

  // --- address bar (controlled by parent) ---
  navUrl: string;
  onNavUrlChange: (value: string) => void;
  onNavigate: (url: string) => void;

  /** Whether the canvas forwards pointer and keyboard input. */
  interactive: boolean;

  /**
   * Remote cursor style pushed by the backend (``{"type":"cursor"}``):
   * "pointer" over links, "text" over inputs, … Applied to the canvas so
   * hover feedback is local — screencast frames carry no cursor.
   */
  remoteCursor?: string;

  /**
   * Remote page viewport in CSS pixels (the size requested when connecting).
   *
   * Frames may be smaller than this — the adaptive profile trades resolution
   * for bandwidth — so pointer coordinates must be derived from the viewport,
   * never from the decoded frame size. Without it, clicks and hover land at a
   * scaled position and inputs never take focus.
   */
  remoteViewport?: ViewportSize | null;

  /** Bookmark toggle for the current URL (address-bar star button). */
  bookmarked?: boolean;
  onToggleBookmark?: (url: string, title: string) => void;

  /** Extra actions rendered after the Go button (e.g. AI / skill buttons). */
  addressBarExtra?: ReactNode;

  /** Optional overlay rendered on top of the canvas (e.g. edge controls). */
  overlay?: ReactNode;

  /** Called whenever a frame is painted (parent tracks readiness). */
  onFrameReadyChange?: (ready: boolean) => void;

  /** Reconnect handler shown in the error / connecting placeholder. */
  onReconnect?: () => void;

  /** Page-specific hint shown under the connecting placeholder. */
  connectingHint?: ReactNode;
}

function activeTabTitle(tabs: StreamTab[]): string {
  const active = tabs.find((t) => t.active);
  return active?.title ?? "";
}

/**
 * Shared browser view: canvas screencast preview, address bar (with bookmark
 * button) and tab management. Used by both the standalone /remote-browser page
 * and the chat BrowserWorkspace popup so their previews stay identical.
 *
 * Personalized features (install flow, AI panel, handoff, panel mode) live in
 * the callers.
 */
export const BrowserViewer = forwardRef<
  BrowserViewerHandle,
  BrowserViewerProps
>(function BrowserViewer(
  {
    status,
    tabs,
    switchTab,
    closeTab,
    newTab,
    sendEvent,
    navUrl,
    onNavUrlChange,
    onNavigate,
    interactive,
    remoteCursor = "default",
    remoteViewport = null,
    bookmarked = false,
    onToggleBookmark,
    addressBarExtra,
    overlay,
    onFrameReadyChange,
    onReconnect,
    connectingHint,
  },
  ref,
) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [frameReady, setFrameReady] = useState(false);
  const { focusInput, ...keyboard } = useBrowserKeyboard(
    interactive,
    sendEvent,
  );

  // Read through a ref so the stable paint callbacks always see the current
  // viewport without being re-created (which would drop queued frames).
  const viewportRef = useRef<ViewportSize | null>(remoteViewport);
  viewportRef.current = remoteViewport;

  // Fit the canvas element box to the viewport aspect ratio.
  //
  // `object-fit: contain` *does* letterbox a canvas, and `getCanvasCoords`
  // assumes the bitmap fills the element — so any aspect mismatch would offset
  // every pointer event by the bar size. Sizing the element to the exact
  // aspect-fit box makes the fit an identity transform.
  const viewportContainerRef = useRef<HTMLDivElement | null>(null);
  const [displaySize, setDisplaySize] = useState<{ w: number; h: number } | null>(
    null,
  );
  const viewportW = remoteViewport?.width ?? 0;
  const viewportH = remoteViewport?.height ?? 0;
  useEffect(() => {
    const box = viewportContainerRef.current;
    if (!box || viewportW <= 0 || viewportH <= 0) {
      setDisplaySize(null);
      return undefined;
    }
    const measure = () => {
      const bw = box.clientWidth;
      const bh = box.clientHeight;
      if (bw <= 0 || bh <= 0) return;
      const scale = Math.min(bw / viewportW, bh / viewportH);
      const w = Math.max(1, Math.floor(viewportW * scale));
      const h = Math.max(1, Math.floor(viewportH * scale));
      // Keep the previous object when nothing changed: resizing emits many
      // observations and each new object would re-render the whole viewer.
      setDisplaySize((prev) => (prev && prev.w === w && prev.h === h ? prev : { w, h }));
    };
    measure();
    if (typeof ResizeObserver !== "function") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [viewportW, viewportH]);

  const paintFrame = useCallback((base64Data: string) => {
    paintBase64JpegToCanvas(canvasRef.current, base64Data, viewportRef.current);
    setFrameReady(true);
  }, []);

  // Latest-frame-wins painter for binary screencast frames. The canvas is
  // conditionally rendered (placeholder while connecting / on error), so it is
  // created lazily on the first frame rather than in a mount-only effect —
  // a mount-only effect would run while canvasRef is still null and leave the
  // painter permanently missing, silently dropping every binary frame.
  const painterRef = useRef<{
    painter: ReturnType<typeof createScreencastPainter>;
    canvas: HTMLCanvasElement;
  } | null>(null);
  useEffect(
    () => () => {
      painterRef.current?.painter.dispose();
      painterRef.current = null;
    },
    [],
  );

  const paintBlobFrame = useCallback((blob: Blob) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    if (!painterRef.current || painterRef.current.canvas !== canvas) {
      painterRef.current?.painter.dispose();
      painterRef.current = {
        painter: createScreencastPainter(canvas, viewportRef.current),
        canvas,
      };
    }
    // The profile can change the frame size mid-session; the coordinate space
    // (and therefore the backing store) must not follow it.
    painterRef.current.painter.setViewport(viewportRef.current);
    painterRef.current.painter.paint(blob);
    setFrameReady(true);
  }, []);

  useImperativeHandle(
    ref,
    () => ({ paintFrame, paintBlobFrame }),
    [paintFrame, paintBlobFrame],
  );

  // Notify the parent whenever a frame is painted (parent tracks readiness).
  useEffect(() => {
    onFrameReadyChange?.(frameReady);
  }, [frameReady, onFrameReadyChange]);

  const forwardCanvasEvent = useCallback(
    (event: Record<string, unknown>) => {
      if (
        event.type === "click" ||
        event.type === "dblclick" ||
        event.type === "mousedown"
      ) {
        focusInput();
      }
      sendEvent(event);
    },
    [sendEvent, focusInput],
  );

  const {
    handleWheel,
    onPointerDown: handlePanPointerDown,
    onPointerMove: handlePointerMove,
    onPointerLeave: handlePointerLeave,
    onDoubleClick: handlePanDoubleClick,
    onContextMenu: handleContextMenu,
    isDragging,
    pointerStyle,
  } = useBrowserCanvasInteraction({
    enabled: interactive,
    canvasRef,
    onEvent: forwardCanvasEvent,
    viewport: remoteViewport,
  });

  const isStreaming = status === "streaming" || status === "browser_started";
  const isConnecting = status === "connecting" || status === "browser_started";
  const isError = status === "error";

  const navUrlNormalized = normalizeUrl(navUrl);

  const handleGo = useCallback(() => {
    const target = normalizeUrl(navUrl);
    if (!target) return;
    onNavigate(target);
  }, [navUrl, onNavigate]);

  const handleNavInputKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        handleGo();
      }
    },
    [handleGo],
  );

  const activeKey =
    tabs.find((tab) => tab.active)?.id != null
      ? String(tabs.find((tab) => tab.active)!.id)
      : tabs[0]
      ? String(tabs[0].id)
      : undefined;

  return (
    <div className={styles.browserViewer}>
      <ChromeTabBar
        tabs={tabs.map((tab) => ({
          key: String(tab.id),
          label: tab.title || tab.url || tab.id,
          leading: <Globe size={11} style={{ flexShrink: 0 }} />,
          tooltip: tab.url,
          closable: tabs.length > 1,
        }))}
        activeKey={activeKey}
        onChange={(key) => {
          const match = tabs.find((tab) => String(tab.id) === key);
          if (match) switchTab(match.id);
        }}
        onClose={(key) => {
          const match = tabs.find((tab) => String(tab.id) === key);
          if (match) closeTab(match.id);
        }}
        onNewTab={() => newTab()}
        newTabTitle={t("browserViewer.newTab")}
      />

      {/* Address bar */}
      <div className={styles.addressBar}>
        <Tooltip title={t("browserViewer.goBack")}>
          <Button
            size="small"
            icon={<ArrowLeft size={14} />}
            onClick={() => sendEvent({ type: "goback" })}
          />
        </Tooltip>
        <Tooltip title={t("browserViewer.goForward")}>
          <Button
            size="small"
            icon={<ArrowRight size={14} />}
            onClick={() => sendEvent({ type: "goforward" })}
          />
        </Tooltip>
        <Tooltip title={t("browserViewer.reload")}>
          <Button
            size="small"
            icon={<RotateCcw size={14} />}
            onClick={() => sendEvent({ type: "reload" })}
          />
        </Tooltip>
        <Input
          size="small"
          className={styles.urlInput}
          value={navUrl}
          placeholder={t("browserViewer.urlPlaceholder")}
          onChange={(e) => onNavUrlChange(e.target.value)}
          onKeyDown={handleNavInputKeyDown}
          prefix={<Globe size={13} />}
          suffix={
            onToggleBookmark && (
              <Tooltip
                title={
                  bookmarked
                    ? t("browserViewer.bookmarkRemove")
                    : t("browserViewer.bookmarkAdd")
                }
              >
                <button
                  type="button"
                  className={cx(
                    styles.bookmarkBtn,
                    bookmarked && styles.bookmarkBtnActive,
                  )}
                  disabled={!navUrlNormalized}
                  aria-label={
                    bookmarked
                      ? t("browserViewer.bookmarkRemove")
                      : t("browserViewer.bookmarkAdd")
                  }
                  onClick={() => onToggleBookmark(navUrl, activeTabTitle(tabs))}
                >
                  <Star size={14} fill={bookmarked ? "currentColor" : "none"} />
                </button>
              </Tooltip>
            )
          }
        />
        <Button
          size="small"
          type="primary"
          onClick={handleGo}
          disabled={!navUrlNormalized}
        >
          {t("browserViewer.go")}
        </Button>
        {addressBarExtra}
      </div>

      {/* Viewport (canvas screencast) */}
      <div
        ref={viewportContainerRef}
        className={cx(
          styles.viewportContainer,
          interactive && styles.viewportInteractive,
        )}
      >
        {isError ? (
          <div className={styles.placeholder}>
            <AlertTriangle
              size={24}
              style={{ marginBottom: 8, color: "#faad14" }}
            />
            <div style={{ marginBottom: 12 }}>
              {t("browserViewer.connectFailed")}
            </div>
            {onReconnect && (
              <Button type="primary" size="small" onClick={onReconnect}>
                {t("browserViewer.reconnect")}
              </Button>
            )}
          </div>
        ) : isConnecting && !frameReady ? (
          <div className={styles.placeholder}>
            <StreamConnectingIndicator
              label={
                isStreaming
                  ? t("browserViewer.streaming")
                  : t("browserViewer.connecting")
              }
              hint={connectingHint}
            />
          </div>
        ) : (
          <canvas
            ref={canvasRef}
            tabIndex={0}
            className={cx(
              styles.canvas,
              interactive && styles.canvasInteractive,
              !frameReady && styles.canvasHidden,
            )}
            style={{
              cursor: isDragging
                ? "grabbing"
                : remoteCursor || "default",
              ...pointerStyle,
              ...(displaySize
                ? {
                    width: displaySize.w,
                    height: displaySize.h,
                    // The box is already the exact aspect-fit size — never let
                    // flexbox shrink it (a shrunken box would reintroduce the
                    // letterbox the coordinate mapping has to undo).
                    flex: "0 0 auto",
                    margin: "auto",
                  }
                : {}),
            }}
            onPointerDown={handlePanPointerDown}
            onPointerMove={handlePointerMove}
            onPointerLeave={handlePointerLeave}
            onDoubleClick={handlePanDoubleClick}
            onContextMenu={handleContextMenu}
            onWheel={handleWheel}
            onFocus={focusInput}
          />
        )}
        <textarea
          ref={keyboard.inputRef}
          className={styles.keyboardInput}
          aria-label={t("browserViewer.keyboardInput")}
          tabIndex={-1}
          readOnly={!interactive}
          autoCapitalize="off"
          autoComplete="off"
          spellCheck={false}
          onInput={keyboard.onInput}
          onKeyDown={keyboard.onKeyDown}
          onCompositionStart={keyboard.onCompositionStart}
          onCompositionEnd={keyboard.onCompositionEnd}
        />
        {overlay}
        {interactive && isStreaming && (
          <div className={styles.interactiveHint}>
            {t("browserViewer.interactiveHint")}
          </div>
        )}
      </div>
    </div>
  );
});

export default BrowserViewer;
