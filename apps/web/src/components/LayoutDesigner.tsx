// The console layout DESIGNER popup: a scaled proxy of the console body on which the user rearranges tmux
// windows by dragging tabs, then presses Apply. It emits a LayoutModel and NOTHING ELSE (no terminal,
// socket or tmux command), so it is provable in jsdom; geometry comes from `consoleLayout.ts`.
//
// The drag is hand-rolled on window-level pointermove/pointerup, like ConsoleTerminal's panel drag: jsdom
// lacks `setPointerCapture` and cannot drive HTML5 DnD; hit-testing and refusal rules are model-level anyway.
import { useEffect, useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import { RAW } from '../theme'
import {
  TABBED_DEFAULT,
  applyDrop,
  dropPreview,
  edgeDir,
  edgeHalfRect,
  elideMiddle,
  hitTest,
  layoutRects,
  openingModel,
  setActiveTab,
  splitRefusal,
  tabLabelChars,
  tiles,
  type CellMetrics,
  type DropZone,
  type LayoutModel,
  type Rect,
  type WindowRef,
} from '../lib/consoleLayout'

type Props = {
  /** The live window list, in the order the enumeration endpoint returned it. */
  windows: WindowRef[]
  /**
   * The layout the console is ACTUALLY rendering, so the popup opens on it; absent opens the tabbed
   * default. Reconciled against `windows` on open (`openingModel`): it drifts from tmux both ways.
   */
  initialModel?: LayoutModel
  /** The REAL console body box in CSS px — what the floor arithmetic is judged against, not the proxy. */
  canvas: { w: number; h: number }
  /** Terminal cell geometry for the floor arithmetic. Supplied, never measured here. */
  cells: CellMetrics
  onApply: (model: LayoutModel) => void
  onCancel: () => void
}

/**
 * THE MEASURED CELL, and the arithmetic AC3.5 asks to be published. Measured in Chromium at 1280x800 at the
 * shipped terminal settings (`fontSize: 13`, `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`):
 *
 *     .console-panel   1232 x 752      .console-header    43px      .console-keys  38.38px
 *     .console-body    1232 x 670.63   <- THE CANVAS: the border box, which is what is measured on open
 *     .xterm-screen    1211 x 660      at the 173 x 44 the terminal fitted itself to
 *       => cellW = 1211 / 173 = 7.0     cellH = 660 / 44 = 15.0     (both exact, not rounded)
 *
 * `chromeH` = 26px tab strip + 4px padding. On the 1232 x 670.63 canvas:
 *   - undivided:        1232 / 7 = 176 cols, (670.63 - 30) / 15 = 42 rows
 *   - split left|right: width (1232 - 6) / 2 = 613px    -> 87 cols x 42 rows   ALLOWED
 *   - split top/bottom: height (670.63 - 6) / 2 = 332.3px -> 176 cols x 20 rows ALLOWED, exactly at the floor
 *   - so the full 2x2 IS available at 1280x800, each quadrant 613 x 332.3 -> 87 x 20.
 * At 1920x1080 (canvas 1872 x 950.63) a quadrant is 933 x 472.3 -> 133 cols x 29 rows.
 * Refusal: vertical split needs ≥666px of canvas height (2 x (20 x 15 + 30) + 6), horizontal ≥1126px width
 * (2 x 80 x 7 + 6), so a window shorter than ~795px refuses top/bottom only; narrower than ~1174px refuses
 * both. The 80 x 20 floor is a fixed input and was not tuned to reach these results.
 * A documented INPUT, not measured here (no terminal renders in this popup); the multi-tile runtime
 * measures live.
 */
export const SHIPPED_CELL_METRICS: CellMetrics = { cellW: 7, cellH: 15, chromeH: 30 }

// The proxy is drawn as large as fits this box, never past 1:1, so tab labels keep their real font size
// (AC2.2) while leaving room for the popup's chrome at 800px tall.
const MAX_PROXY_W = 900
const MAX_PROXY_H = 460

/** Tab strip height in DOM px — FIXED, not scaled: a strip that shrank with the proxy would be unreadable. */
const TAB_STRIP_PX = 26

/** The proxy's scale factor, exported so a test driving a drag computes the same number. */
export function designerScale(canvas: { w: number; h: number }): number {
  if (!(canvas.w > 0) || !(canvas.h > 0)) return 1
  return Math.min(MAX_PROXY_W / canvas.w, MAX_PROXY_H / canvas.h, 1)
}

function px(rect: Rect, scale: number) {
  return {
    left: `${rect.x * scale}px`,
    top: `${rect.y * scale}px`,
    width: `${rect.w * scale}px`,
    height: `${rect.h * scale}px`,
  }
}

type DragState = { windowId: string; clientX: number; clientY: number }

/**
 * The popup's backdrop and surface. `zIndex: 2`, not a theme z-index: this sits inside the overlay's stacking context, above a
 * dragged panel (`position: fixed`, `z-index: auto`).
 */
const designerBackdropSx = {
  position: 'fixed',
  inset: 0,
  zIndex: 2,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  p: 4,
  bgcolor: 'rgba(0, 0, 0, 0.6)',
  /* Explicit: this is an `aria-modal` dialog, and `pointer-events` INHERITS the `none` the floating
     console's root sets (ConsoleTerminal's `floatingRootSx`), which made every control here dead with
     clicks passing through. */
  pointerEvents: 'auto',
} as const

/** The popup card itself. Theme-following (`Paper`), unlike the terminal proxy inside it. */
const designerSurfaceSx = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
  maxWidth: '100%',
  maxHeight: '100%',
  p: 2,
  '&:focus': { outline: 'none' },
} as const

/* The proxy is a PICTURE OF THE TERMINAL, so it keeps the terminal's dark ground in both schemes (the
   `RAW.dark` map ConsolePane's XTERM_THEME uses). */
const CANVAS_BORDER = 'rgba(255, 255, 255, 0.14)'

export default function LayoutDesigner({ windows, initialModel, canvas, cells, onApply, onCancel }: Props) {
  // TWO TREES, DELIBERATELY — do not collapse them:
  //   `openSnapshot` is RESET's target only (D8: "restores original (tabbed) layout"), so Reset works for
  //   an operator who opened on a grid.
  //   `model` is the WORKING tree, starting from `initialModel` (reconciled) so the operator can ADJUST;
  //   without a usable one it starts from `openSnapshot`.
  // Both captured ONCE at mount, so the tree never moves under a drag.
  const [openSnapshot] = useState<LayoutModel>(() => TABBED_DEFAULT(windows))
  const [model, setModel] = useState<LayoutModel>(
    () => (initialModel ? openingModel(initialModel, windows) : null) ?? openSnapshot,
  )
  const [drag, setDrag] = useState<DragState | null>(null)

  const dialogRef = useRef<HTMLDivElement | null>(null)
  const canvasRef = useRef<HTMLDivElement | null>(null)
  // Stashed so an unmount mid-drag removes the window listeners (as in ConsoleTerminal).
  const dragTeardownRef = useRef<(() => void) | null>(null)
  useEffect(() => () => dragTeardownRef.current?.(), [])

  useEffect(() => {
    dialogRef.current?.focus()
  }, [])

  const scale = designerScale(canvas)
  const rects = useMemo(() => layoutRects(model, canvas), [model, canvas])
  // The strip's fixed DOM px, in canvas coordinates (where the hit-test works).
  const tabBandH = TAB_STRIP_PX / scale

  function toCanvas(ev: { clientX: number; clientY: number }): { x: number; y: number } | null {
    const el = canvasRef.current
    if (!el) return null
    const box = el.getBoundingClientRect()
    return { x: (ev.clientX - box.left) / scale, y: (ev.clientY - box.top) / scale }
  }

  function zoneAt(m: LayoutModel, ev: { clientX: number; clientY: number }): DropZone | null {
    const p = toCanvas(ev)
    if (!p) return null
    return hitTest(p, layoutRects(m, canvas), tabBandH)
  }

  function refusalFor(m: LayoutModel, zone: DropZone | null): string | null {
    if (!zone || zone.kind !== 'edge') return null
    return splitRefusal(m, zone.tileKey, edgeDir(zone.edge), canvas, cells)
  }

  function startDrag(win: WindowRef, e: React.PointerEvent) {
    if (e.button !== 0) return
    // The tab is a button; without this the browser starts a text selection over the label instead.
    e.preventDefault()
    setDrag({ windowId: win.id, clientX: e.clientX, clientY: e.clientY })

    const onMove = (ev: PointerEvent) => {
      setDrag((d) => (d ? { ...d, clientX: ev.clientX, clientY: ev.clientY } : d))
    }
    const onUp = (ev: PointerEvent) => {
      teardown()
      setDrag(null)
      // Resolved against the CURRENT model inside the updater, not the tree captured at drag start.
      setModel((cur) => {
        const zone = zoneAt(cur, ev)
        if (!zone) return cur
        if (refusalFor(cur, zone)) return cur
        return applyDrop(cur, win.id, zone)
      })
    }
    function teardown() {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      dragTeardownRef.current = null
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
    dragTeardownRef.current = teardown
  }

  const hoverZone = drag ? zoneAt(model, drag) : null
  const hoverRefusal = refusalFor(model, hoverZone)
  const previewRect = drag && hoverZone ? dropPreview(model, drag.windowId, hoverZone, canvas) : null
  // Positioned from the PRE-removal rect (`rects`), so it still shows when hovering the dragged tab's own
  // tile, where the post-removal `dropPreview` is null.
  const refusalRect =
    hoverRefusal && hoverZone && hoverZone.kind === 'edge'
      ? (() => {
          const r = rects.get(hoverZone.tileKey)
          return r ? edgeHalfRect(r, hoverZone.edge) : null
        })()
      : null
  const draggedName = drag ? windows.find((w) => w.id === drag.windowId)?.name ?? '' : ''

  return (
    <Box
      sx={designerBackdropSx}
      data-testid="layout-designer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="Console layout"
    >
      <Paper
        elevation={8}
        sx={designerSurfaceSx}
        data-testid="layout-designer"
        ref={dialogRef}
        tabIndex={-1}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            onCancel()
          }
        }}
      >
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
          <Typography variant="h6" component="h2" sx={{ fontSize: '1rem', fontWeight: 600 }}>
            Console layout
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', maxWidth: '56ch', lineHeight: 1.4 }}>
            Drag a tab onto a tile’s edge to split it, or onto a tab strip to group it there.
          </Typography>
        </Box>

        {/* The proxy canvas: inline px size (measured body × scale) keeps the real ASPECT RATIO, so
            edge-zone judgements are true. Dynamic, hence inline `style`. */}
        <Box
          ref={canvasRef}
          data-testid="layout-canvas"
          sx={{
            position: 'relative',
            overflow: 'hidden',
            borderRadius: 1,
            bgcolor: RAW.dark.terminalBg,
            touchAction: 'none',
          }}
          style={{ width: `${canvas.w * scale}px`, height: `${canvas.h * scale}px` }}
        >
          {tiles(model).map((tile) => {
            const rect = rects.get(tile.key)
            if (!rect) return null
            const chars = tabLabelChars(rect.w * scale, tile.tabs.length)
            const active = tile.tabs[tile.activeTab]
            return (
              <Box
                key={tile.key}
                data-testid="layout-tile"
                data-tile-key={tile.key}
                sx={{
                  position: 'absolute',
                  display: 'flex',
                  flexDirection: 'column',
                  overflow: 'hidden',
                  border: `1px solid ${CANVAS_BORDER}`,
                  borderRadius: 1,
                  bgcolor: 'rgba(255, 255, 255, 0.03)',
                  boxSizing: 'border-box',
                }}
                style={px(rect, scale)}
              >
                {/* AC2.4: past the label floor the strip SCROLLS horizontally (`flexShrink: 0` on the tab), so
                    the last tab is reachable and no label is shredded. */}
                <Box
                  role="tablist"
                  aria-label="Windows in this tile"
                  sx={{
                    display: 'flex',
                    alignItems: 'stretch',
                    gap: '2px',
                    flexShrink: 0,
                    p: '2px',
                    overflowX: 'auto',
                    overflowY: 'hidden',
                    scrollbarWidth: 'thin',
                    borderBottom: '1px solid rgba(255, 255, 255, 0.1)',
                    bgcolor: 'rgba(255, 255, 255, 0.06)',
                    boxSizing: 'border-box',
                  }}
                  style={{ height: `${TAB_STRIP_PX}px` }}
                >
                  {tile.tabs.map((w, i) => (
                    <Box
                      component="button"
                      key={w.id}
                      type="button"
                      role="tab"
                      aria-selected={i === tile.activeTab}
                      data-testid="layout-tab"
                      data-dragging={drag?.windowId === w.id ? 'true' : undefined}
                      // The FULL name, always; the label's elision must not be the only copy.
                      title={w.name}
                      data-window-id={w.id}
                      onPointerDown={(e: React.PointerEvent) => startDrag(w, e)}
                      onClick={() => setModel((m) => setActiveTab(m, tile.key, i))}
                      sx={(theme) => ({
                        flex: '0 0 auto',
                        maxWidth: 'none',
                        px: '0.4rem',
                        py: 0,
                        border: '1px solid transparent',
                        borderRadius: '3px',
                        bgcolor: 'transparent',
                        color: RAW.dark.textSecondary,
                        fontFamily: theme.typography.mono.fontFamily,
                        fontSize: '11px',
                        lineHeight: 1,
                        // Already elided by elideMiddle; eliding again would re-cut it at the tail.
                        whiteSpace: 'nowrap',
                        cursor: 'grab',
                        touchAction: 'none',
                        userSelect: 'none',
                        '&:hover': { bgcolor: 'rgba(255, 255, 255, 0.06)', color: RAW.dark.textPrimary },
                        '&[aria-selected="true"]': {
                          borderColor: 'rgba(255, 255, 255, 0.18)',
                          bgcolor: 'rgba(255, 255, 255, 0.08)',
                          color: RAW.dark.textPrimary,
                        },
                        '&[data-dragging="true"]': { opacity: 0.4, cursor: 'grabbing' },
                      })}
                    >
                      {elideMiddle(w.name, chars)}
                    </Box>
                  ))}
                </Box>
                <Box
                  sx={{
                    display: 'flex',
                    flex: 1,
                    minHeight: 0,
                    alignItems: 'center',
                    justifyContent: 'center',
                    p: '0.25rem',
                    overflow: 'hidden',
                  }}
                >
                  {/* The visible window's name in the tile body: the strip alone does not say which tab shows. */}
                  {active && (
                    <Box
                      component="span"
                      data-testid="layout-tile-caption"
                      sx={(theme) => ({
                        color: 'rgba(255, 255, 255, 0.45)',
                        fontFamily: theme.typography.mono.fontFamily,
                        fontSize: '11px',
                        whiteSpace: 'nowrap',
                      })}
                    >
                      {elideMiddle(active.name, tabLabelChars(rect.w * scale, 1))}
                    </Box>
                  )}
                </Box>
              </Box>
            )
          })}

          {/* The drop preview is the EXACT rectangle the release produces (asserted, not approximated). */}
          {previewRect && !hoverRefusal && (
            <Box
              data-testid="drop-preview"
              aria-hidden="true"
              sx={{
                position: 'absolute',
                pointerEvents: 'none',
                border: 2,
                borderColor: 'primary.main',
                borderRadius: 1,
                bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.22)',
                boxSizing: 'border-box',
              }}
              style={px(previewRect, scale)}
            />
          )}
          {/* A refused zone is shown DISABLED WITH ITS REASON, never silently ignored. */}
          {refusalRect && hoverRefusal && (
            <Box
              data-testid="drop-refused"
              sx={{
                position: 'absolute',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                pointerEvents: 'none',
                p: 1,
                border: '2px dashed',
                borderColor: RAW.dark.error,
                borderRadius: 1,
                bgcolor: 'rgba(var(--mui-palette-error-mainChannel) / 0.14)',
                boxSizing: 'border-box',
              }}
              style={px(refusalRect, scale)}
            >
              <Box
                component="span"
                data-testid="layout-drop-reason"
                sx={{
                  maxWidth: '32ch',
                  // On the canvas's dark ground in BOTH schemes, so the on-dark error token.
                  color: RAW.dark.error,
                  fontSize: '11px',
                  lineHeight: 1.3,
                  textAlign: 'center',
                }}
              >
                {hoverRefusal}
              </Box>
            </Box>
          )}
        </Box>

        {/* Polite, not alert: refusals appear as the pointer sweeps and must not interrupt. Present from
            mount so the first text is a CHANGE; min-height stops the actions row jumping. */}
        <Typography
          variant="body2"
          component="p"
          role="status"
          data-testid="layout-designer-status"
          sx={{ color: 'error.main', minHeight: '1.1rem', lineHeight: '1.1rem' }}
        >
          {hoverRefusal ?? ''}
        </Typography>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          {/* Reset returns to TABS even when opened on a grid, so the tooltip names the tabbed arrangement. */}
          <Button
            type="button"
            variant="outlined"
            color="inherit"
            onClick={() => setModel(openSnapshot)}
            title="Back to tabs — one tile, every window"
          >
            Reset
          </Button>
          <Box sx={{ flex: 1 }} />
          <Button type="button" variant="outlined" color="inherit" onClick={onCancel}>
            Cancel
          </Button>
          <Button type="button" variant="contained" onClick={() => onApply(model)}>
            Apply
          </Button>
        </Box>
      </Paper>

      {/* Never under the pointer: a ghost taking the pointer would hit-test as the drop target. */}
      {drag && (
        <Box
          data-testid="layout-drag-ghost"
          aria-hidden="true"
          sx={(theme) => ({
            position: 'fixed',
            zIndex: 3,
            pointerEvents: 'none',
            transform: 'translate(12px, 12px)',
            px: '0.4rem',
            py: '2px',
            border: `1px solid ${CANVAS_BORDER}`,
            borderRadius: '3px',
            bgcolor: RAW.dark.backgroundPaper,
            color: RAW.dark.textPrimary,
            fontFamily: theme.typography.mono.fontFamily,
            fontSize: '11px',
            whiteSpace: 'nowrap',
          })}
          style={{ left: `${drag.clientX}px`, top: `${drag.clientY}px` }}
        >
          {elideMiddle(draggedName, 24)}
        </Box>
      )}
    </Box>
  )
}
