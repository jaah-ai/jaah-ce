// The interactive Console OVERLAY: the floating panel, header, on-screen key
// bar, and the drag/resize/soft-keyboard chrome around a terminal.
//
// The terminal (xterm.js, the PTY WebSocket, reconnects) lives in <ConsolePane>; this mounts exactly ONE,
// and reaches it only through its imperative handle so a reconnect never strands the key bar or paste.
//
// A designed layout is ONE pane: the tile tree is encoded (`encodeLayoutSpec`) and sent to the node,
// where tmux draws the splits and returns a single PTY. Pure model: lib/consoleLayout.ts.
import { useCallback, useEffect, useRef, useState } from 'react'
import Alert from '@mui/material/Alert'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Paper from '@mui/material/Paper'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
/* The window buttons and their style are shared with the list/detail pane. */
import { CloseButton, MaximizeButton, paneHeaderButtonSx as headerButtonSx } from './PaneWindowButtons'
import ConsolePane, { DEFAULT_FONT_SIZE, type ConsolePaneHandle, type ConsoleStatus } from './ConsolePane'
import { clipboardFiles } from '../lib/clipboardFiles'
import type { ConsolePresentation, ConsoleView } from '../lib/consoleDock'
import LayoutDesigner, { SHIPPED_CELL_METRICS } from './LayoutDesigner'
import FormDialog from '../ui/FormDialog'
import HelpLabel from '../ui/HelpLabel'
import { useFleetWindows } from '../hooks/useFleetWindows'
import { useViewportIs } from '../hooks/useViewportWidth'
import { layoutStorageKey, loadLayout, saveLayout } from '../lib/consoleLayoutStorage'
import {
  MULTI_TILE_FONT_SIZE,
  TILE_STATUS_ROWS,
  countTiles,
  encodeLayoutSpec,
  hasSlotWindow,
  restoreLayout,
  tileFontSize,
  type CellMetrics,
  type LayoutModel,
} from '../lib/consoleLayout'
/* `SR_ONLY_SX` (ui/listStyles) is the single home for the sr-only shape; read its note before moving the
   announcer out of the (positioned) dock header. */
import { PHONE, SR_ONLY_SX, ELLIPSIS_SX } from '../ui/listStyles'
import StatusDot from '../ui/StatusDot'
import type { Tone } from '../ui/StatusChip'
import { FONT_SIZE, FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE, grid } from '../theme'
import { directUrlOverrideKey, loadDirectUrlOverride, saveDirectUrlOverride } from '../lib/directConsole'
import { countOf } from '../lib/humanize'
import { useCanDriveWorktreeSession } from '../hooks/useWorktreeAccess'
import { useWindowDrag } from '../hooks/useWindowDrag'
import {
  DIRECT_MODE_EXPLAINER,
  DIRECT_TOGGLE_VIEW,
  directToggleState,
  loadDirectEnabled,
  saveDirectEnabled,
} from '../lib/directToggle'
import { CopyButton } from '../ui/CopyButton'
import ChatView from './console/ChatView'
import type { ConsoleRuntime } from './console/consoleRuntime'
import Tip from '../ui/Tip'

type Props = {
  // The display name; `projectId` addresses every console/worktree call.
  project: string
  projectId: number
  number: number
  // Which node's slot (its uuid): (project, number) is unique only PER NODE.
  nodeId?: string
  kind?: 'clauded' | 'term'
  // The slot's Title (lib/worktree.ts's resolveTitle) — the same string as the Worktrees Title column
  // (AC8.3); absent falls back to `project-number`. Read-only here.
  title?: string
  // May this principal drive THIS slot — the row's server-computed `may_edit` (`types.ts::Worktree.may_edit`,
  // single home `apps/api/app/authz/`). Needed besides the key predicate: pool admins SEE every slot but
  // may drive only their own. REQUIRED so a forgetful caller cannot get a writable terminal; with no row,
  // pass `false` (fail closed).
  mayEdit: boolean
  // Whether the opener's row showed a live session at open time (`lib/worktree.ts::hasLiveSession`),
  // passed to `<ConsolePane hadLiveSession>` (the single home for its use). Optional is fail-safe here.
  // A SNAPSHOT of what was clicked, deliberately not refreshed from the cache.
  hadLiveSession?: boolean
  // A MicroVM task's `vm:<vm_id>` (`lib/consoleDock.ts::ConsoleTarget`), passed to `<ConsolePane view>`. No slot
  // behind it, so no saved layouts, designer or direct transport.
  view?: string
  onClose: () => void
  // Prev/next session chevrons. The caller owns neighbor order; an absent handler disables that edge.
  onPrev?: () => void
  onNext?: () => void
  hasPrev?: boolean
  hasNext?: boolean
  // `docked` / `floating` / `maximized` / `modal` (default); `components/ConsoleDock.tsx` owns what they
  // mean. Defaults to `modal` so direct renders (the existing spec suite) keep pre-dock behaviour.
  presentation?: ConsolePresentation
  // Change the stored view. Absent when nobody owns the view, which withholds those controls.
  onSetView?: (view: ConsoleView) => void
  // The slot's live agent session (`console/consoleRuntime.ts`), read by the dock. A task whose session can chat
  // shows the Chat view; absent (a VM view, a direct render, an older CP) the console is the terminal alone.
  runtime?: ConsoleRuntime
  // Reports the task whose Chat view is on screen (null on the terminal and on unmount) up to the dock.
  onChatTask?: (taskId: number | null) => void
}

// The panel starts CSS-centered and switches to this explicit px box on first drag/resize.
// `null` means still on the CSS default.
type ManualBox = { top: number; left: number; width: number; height: number }

/**
 * Quiet period before re-fitting, in ms: one divider drag must produce ONE resize, since `fit()` reflows a
 * real remote PTY and repaints the TUI.
 */
const PANEL_REFIT_DEBOUNCE_MS = 120
/** The direct-address field's explanation: blank dials the node's own address; the value stays in this browser. */
const DIRECT_OVERRIDE_TIP = 'Dials this address instead of the node’s; this browser only.'

const MIN_WIDTH = 320
const MIN_HEIGHT = 200
// Below this the panel is pinned full-screen via `!important`, so drag/resize no-op.
const MOBILE_BREAKPOINT = 480

// Min gap between live-region announcements; a burst (tiles dropping together) would be dropped by the
// screen reader anyway.
const ANNOUNCE_MIN_INTERVAL_MS = 1500

// Initial state and the fallback for a tile not yet reporting: a mounting pane IS connecting.
const CONNECTING: ConsoleStatus = { state: 'connecting', label: 'connecting…' }

const isDesktopWidth = (width: number): boolean => width > MOBILE_BREAKPOINT

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max))
}

// Keys a phone soft keyboard cannot produce but TUIs need, rendered as a tap bar.
// `arrow` entries carry the FINAL char only: DECCKM mode needs ESC O A, otherwise ESC [ A; xterm.js knows
// the live mode, so the sequence is resolved per press.
type ConsoleKey = { label: string; ariaLabel: string; send: string } | { label: string; ariaLabel: string; arrow: 'A' | 'B' | 'C' | 'D' }

const CONSOLE_KEYS: ConsoleKey[] = [
  { label: 'Esc', ariaLabel: 'Escape', send: '\x1b' },
  { label: 'Tab', ariaLabel: 'Tab', send: '\t' },
  { label: '←', ariaLabel: 'Left arrow', arrow: 'D' },
  { label: '↑', ariaLabel: 'Up arrow', arrow: 'A' },
  { label: '↓', ariaLabel: 'Down arrow', arrow: 'B' },
  { label: '→', ariaLabel: 'Right arrow', arrow: 'C' },
  { label: '^C', ariaLabel: 'Control C', send: '\x03' },
]

/* ── Overlay styling ─────────────────────────────
   Chrome follows the theme (`background.*`, `divider`, `text.*`; AC8). The terminal canvas stays dark in
   both schemes — see ConsolePane's XTERM_THEME. */

/** The modal scrim. `components/ConsoleDock.tsx`'s Suspense fallback reproduces the dim so it is dark
 *  before this chunk loads. */
const overlaySx = {
  position: 'fixed',
  inset: 0,
  bgcolor: 'rgba(0, 0, 0, 0.55)',
  backdropFilter: `blur(${grid(0.5)})`,
  zIndex: (t: Theme) => t.zIndex.mobileStepper,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  p: 4,
  '@keyframes consoleOverlayIn': { from: { opacity: 0 }, to: { opacity: 1 } },
  animation: 'consoleOverlayIn 160ms ease',
  [PHONE]: { p: 0 },
} as const

const panelSx = {
  position: 'relative',
  borderRadius: RADIUS.md,
  // Viewport minus the scrim padding; the window-resize handler re-fits.
  width: `calc(100vw - ${grid(8)})`,
  height: `calc(100vh - ${grid(8)})`,
  /* Never taller than the VISUAL viewport, so the soft keyboard cannot cover the cursor rows. On the base
     rule because the phone query keys on width and landscape phones never match it. Non-binding with no
     keyboard. The setter is the effect below and must move with this: a `var()` fallback fails silently. */
  maxHeight: `var(--console-vv-height, calc(100vh - ${grid(8)}))`,
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
  '@keyframes consolePanelIn': {
    from: { opacity: 0, transform: `translateY(${grid(1)}) scale(0.98)` },
    to: { opacity: 1, transform: 'none' },
  },
  animation: 'consolePanelIn 180ms ease',
  // After the first drag/resize: a free fixed window out of the scrim's flex-centering.
  '&[data-manual="true"]': { position: 'fixed', margin: 0 },
  [PHONE]: {
    position: 'fixed',
    inset: 0,
    // `!important` because the manual box is an INLINE style.
    top: '0 !important',
    left: '0 !important',
    width: '100vw !important',
    height: '100vh !important',
    margin: 0,
    borderRadius: RADIUS.none,
    /* `100vh` above for engines without `dvh`/`visualViewport` (else an invalid `var()` collapses the
       panel to `height: auto`), then `dvh`, then the var. `dvh` ignores the soft keyboard, hence the var. */
    '@supports (height: 100dvh)': { height: 'var(--console-vv-height, 100dvh) !important' },
  },
} as const

/* ── The dock's three non-modal presentations (the `modal` one is `overlaySx`/`panelSx` above) ─────────
   None paints a dim or captures clicks it does not own: `floating` spans the viewport only for
   positioning and passes misses through (`pointerEvents`), so an undocked console is modeless. */
const dockedRootSx = { width: '100%', height: '100%', minWidth: 0, display: 'flex' } as const

const floatingRootSx = {
  position: 'fixed',
  inset: 0,
  zIndex: (t: Theme) => t.zIndex.mobileStepper,
  pointerEvents: 'none',
} as const

const maximizedRootSx = { position: 'fixed', inset: 0, zIndex: (t: Theme) => t.zIndex.mobileStepper, display: 'flex' } as const

const PANEL_BODY_SX = {
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
  position: 'relative',
} as const

/* Square-edged with NO border: the divider's rule is the one seam (a second border drew a double line). */
const dockedPanelSx = {
  ...PANEL_BODY_SX,
  width: '100%',
  height: '100%',
  borderRadius: RADIUS.none,
} as const

/* Default box for a just-undocked, never-dragged console: `right`-anchored so it opens where the dock was.
   The first drag replaces it with an explicit px box (`panelStyle`), clearing `right`. */
const floatingPanelSx = {
  ...PANEL_BODY_SX,
  position: 'fixed',
  pointerEvents: 'auto',
  top: grid(4),
  right: grid(4),
  width: `min(${grid(126.5)}, 46vw)`,
  height: `calc(100dvh - ${grid(8)})`,
  borderRadius: RADIUS.md,
} as const

const maximizedPanelSx = {
  ...PANEL_BODY_SX,
  position: 'fixed',
  inset: 0,
  width: '100%',
  height: '100%',
  borderRadius: RADIUS.none,
  margin: 0,
} as const

const headerSx = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  px: 1,
  py: 0.5,
  bgcolor: 'background.default',
  borderBottom: 1,
  borderColor: 'divider',
  cursor: 'move',
  touchAction: 'none',
  userSelect: 'none',
  /* Both load-bearing: a docked panel can be 320px (`CONSOLE_MIN_PX`) wide. `minWidth: 0` lets the title
     ellipsise; `overflow: hidden` stops the row pushing the last control (✕ Close) off the viewport. */
  minWidth: 0,
  overflow: 'hidden',
  [PHONE]: { px: 2, minHeight: SIZE.tapTarget, cursor: 'default' },
} as const

/* Connection state as a DOT (the close reason is written into the terminal body). States differ by
   SILHOUETTE (WCAG 1.4.1): hollow ring / filled circle / filled square; colour is reinforcement only.
   A fixed size so the header never reflows. State rides `data-state` for specs. */
const STATUS_DOT: Record<ConsoleStatus['state'], { tone: Tone; hollow?: boolean }> = {
  connecting: { tone: 'warning', hollow: true },
  connected: { tone: 'success' },
  closed: { tone: 'error' },
}
const statusDotSx = {
  ml: 1,
  // Keeps the dot beside the title in a space-between header; on a phone a gap from the nav chevron instead.
  mr: 'auto',
  '&[data-state="closed"]': { borderRadius: RADIUS.none },
  // Larger at arm's length.
  [PHONE]: { mr: 1, width: SIZE.iconXs, height: SIZE.iconXs },
} as const

/* The one-way-console badge: a muted header LABEL, not a dismissible banner or a warning colour. */
const readOnlyBadgeSx = {
  flexShrink: 0,
  mr: 1,
  px: 1,
  py: 0,
  border: 1,
  borderColor: 'divider',
  borderRadius: RADIUS.sm,
  fontSize: FONT_SIZE.xs,
  fontWeight: FONT_WEIGHT.semibold,
  letterSpacing: LETTER_SPACING.none,
  textTransform: 'uppercase',
  color: 'text.disabled',
  // Kept on phones too: it is the only explanation for a keyboard that does nothing.
  whiteSpace: 'nowrap',
} as const

export default function ConsoleTerminal({ project, projectId, number, nodeId, kind = 'clauded', title, mayEdit, hadLiveSession, view, onClose, onPrev, onNext, hasPrev = false, hasNext = false, presentation = 'modal', onSetView, runtime, onChatTask }: Props) {
  /* The presentation, unpacked once so rules do not drift. `isModal` is the full pre-dock behaviour
     (scrim, scroll lock, free window once dragged). */
  const isModal = presentation === 'modal'
  const isDocked = presentation === 'docked'
  const isFloating = presentation === 'floating'
  const isMaximized = presentation === 'maximized'
  // Drag/resize only for a free window; docked and maximized sizes are owned elsewhere.
  const isMovable = isFloating || isModal
  // Always the browser overlay view (spec R1).
  const kindLabel = kind === 'term' ? 'Web Term' : 'Web Console'
  /* Whether anything may go upstream (stdin, key bar). Both halves of the server's rule:
       · the key: `useCanDriveWorktreeSession`, NOT `useCanEditWorktrees` — the relay needs
         `worktrees:edit` alone (see `hooks/useWorktreeAccess.ts`);
       · the slot: `mayEdit` — pool admins may watch any slot read-only (`components/WorktreePool.tsx`
         keeps the button enabled) but not drive it.
     `mayEdit !== true` fails closed on an `undefined` from outside the type system. */
  const readOnly = !useCanDriveWorktreeSession() || mayEdit !== true
  // The slot slug is the stable identity for aria-label and tooltip, independent of the title.
  const slotLabel = `${project}-${number}`
  // `''` when unresolved, which falls back to the slug.
  const resolvedTitle = title ?? ''
  const headerTitle = resolvedTitle || slotLabel
  /* Where Restore returns to: per maximize gesture, kept in a ref (the panel never unmounts across the
     trip — components/ConsoleDock.tsx). A reload into `maximized` restores to `docked`, the default. */
  const restoreViewRef = useRef<ConsoleView>('docked')
  const panelRef = useRef<HTMLDivElement | null>(null)
  // The live terminal/socket, reached only via the pane's handle.
  const paneRef = useRef<ConsolePaneHandle | null>(null)
  // Written by the pane (it latches `setStatus` into a ref, so no stability contract).
  const [status, setStatus] = useState<ConsoleStatus>(CONNECTING)
  // The dot is not a live region (WCAG 4.1.3), so a separate polite region announces state. Its node must
  // exist BEFORE its text (this overlay is lazy-mounted; a region inserted with its text is not reliably
  // announced): commit 1 inserts it empty, the effect populates it. Polite so keystrokes are never
  // interrupted. Known, accepted: the first connected announcement races `term.focus()`.
  // One region even with multiple tiles (see the announcement section below).
  // null = still on the CSS default (viewport-filling, centered); set on the first drag or resize.
  const [manual, setManual] = useState<ManualBox | null>(null)
  // Bumping this re-runs the pane's connect effect from scratch ("try now"; both paths retry on their own
  // cadence — ConsolePane's RECONNECT_IDLE_DELAY_MS / DIRECT_RETRY_DELAY_MS). It re-runs a first attach,
  // still attach-only: a click never creates a session.
  const [reconnectNonce, setReconnectNonce] = useState(0)

  // ── Chat or Terminal ──
  // No chooser: a session that can chat (an ACP run) shows its Chat view, every other one the terminal.
  // Chat UNMOUNTS the pane (its socket close detaches tmux, never kills).
  const hasTask = !view && runtime !== undefined && runtime.task_id !== null
  const chatTaskId = hasTask ? runtime.task_id : null
  const chatSessionId = hasTask ? runtime.session_id : null
  const showChat = hasTask && runtime.chat.available && chatSessionId !== null
  const shownChatTask = showChat ? chatTaskId : null
  useEffect(() => {
    onChatTask?.(shownChatTask)
  }, [onChatTask, shownChatTask])
  useEffect(() => () => onChatTask?.(null), [onChatTask])

  // ── The layout designer ───────────────────────────────────────
  // Desktop only, hidden (not disabled) below MOBILE_BREAKPOINT (settled D10): it is a pointer-drag
  // surface. Live, not a render-time read, because the window can be resized with the console open.
  const isDesktopViewport = useViewportIs(isDesktopWidth)

  // ── The applied layout ────────────────────────────────────────────────
  // `layout === null` is the plain single-pane console; everything below is additive.
  const storageKey = layoutStorageKey(project, number, kind)
  const [layout, setLayout] = useState<LayoutModel | null>(null)
  // A stored model read but not yet validated against the live window list (a stored `@id` may be gone).
  // Read during the FIRST render: no pane mounts while a restore is pending, and reading an effect later
  // would dial and drop a throwaway PTY on every open.
  const [pendingRestore, setPendingRestore] = useState<LayoutModel | null>(() => (view ? null : loadLayout(storageKey)))
  // The layout as the node will build it, derived during render (not a second state copy). `null` also
  // for an inexpressible model, falling back to the plain single pane.
  const layoutSpec = layout ? encodeLayoutSpec(layout) : null
  // The measured cell box once a terminal exists; the estimate stands in until then.
  const [measuredCell, setMeasuredCell] = useState<{ cellW: number; cellH: number } | null>(null)

  // ── Direct connection ───────────────────────────────────────────────────────────────────────────
  // The header toggle owns ON/OFF; the ⚙ popup owns the `direct_url` diagnostic override.
  // Persisted per user (settled D11, lib/directToggle.ts). Read during the FIRST render: the pane's
  // connect effect runs before the parent's, so a later read would dial the relay and re-dial direct.
  const [directEnabled, setDirectEnabled] = useState(loadDirectEnabled)

  // The one writer, persisting on the user's ACT (an effect would also fire on load). The write sits
  // outside the updater, which must stay pure (StrictMode double-invokes).
  // Statuses are reset in the same act: they describe the outgoing sockets, and keeping them would
  // announce an unreachable node on a successful enable.
  const toggleDirect = useCallback(() => {
    setDirectEnabled((on) => !on)
    saveDirectEnabled(!directEnabled)
    setStatus(CONNECTING)
  }, [directEnabled])
  const overrideKey = directUrlOverrideKey(project, number, kind, nodeId)
  // Re-read whenever the KEY moves: chevrons switch slots without a remount, and a stale override would be
  // written back under the new slot's key. Derived during render (not an effect) because the pane's connect
  // effect runs first and would dial with the previous slot's value.
  const [override, setOverride] = useState(() => ({ key: overrideKey, value: loadDirectUrlOverride(overrideKey) }))
  if (override.key !== overrideKey) {
    setOverride({ key: overrideKey, value: loadDirectUrlOverride(overrideKey) })
  }
  const directUrlOverride = override.value
  const setDirectUrlOverride = (value: string) => setOverride({ key: overrideKey, value })
  const [directSettingsOpen, setDirectSettingsOpen] = useState(false)
  // The popup edits a draft and commits on Apply; the live prop would re-dial on every keystroke.
  const [draftOverride, setDraftOverride] = useState('')

  function openDirectSettings() {
    setDraftOverride(directUrlOverride)
    setDirectSettingsOpen(true)
  }

  function applyDirectSettings() {
    const next = draftOverride.trim()
    // A failed write never blocks applying the value for this session.
    saveDirectUrlOverride(overrideKey, next)
    setDirectUrlOverride(next)
    setDirectSettingsOpen(false)
  }

  const [designing, setDesigning] = useState(false)
  // The real console body box in CSS px, sampled on popup open; the min-tile floor is judged against
  // this, never against the designer's scaled proxy canvas.
  const [designCanvas, setDesignCanvas] = useState<{ w: number; h: number } | null>(null)
  // Enumerated only while the popup is open or a stored layout awaits validation (desktop only); `null`
  // otherwise so mounting a console costs no request. Re-fetched per open, so `index` is a display hint
  // and `id` (tmux `@N`) the identity.
  const fleet = useFleetWindows(designing || (pendingRestore && isDesktopViewport) ? nodeId : null)

  // ── Reaching the pane ────────────────────────────────────────────────────────────────────────────
  // One target always: key bar, paste, dot and re-fits go to the single <ConsolePane>. Routing within a
  // grid is tmux's (`mouse on`).
  const activePane = useCallback((): ConsolePaneHandle | null => paneRef.current, [])

  // ── The single live region ───────────────────────────────────────────
  // One polite region (WCAG 4.1.3). Multi-tile announcements are tile-labelled and rate-limited
  // (leading-edge: first change at once, last change when the window closes). The single-pane path
  // bypasses the limiter.
  const [announcement, setAnnouncement] = useState('')
  const announceTimerRef = useRef<number | null>(null)
  const announcePendingRef = useRef<string | null>(null)
  useEffect(() => () => {
    if (announceTimerRef.current !== null) window.clearTimeout(announceTimerRef.current)
  }, [])

  const announceThrottled = useCallback((text: string) => {
    if (announceTimerRef.current !== null) {
      announcePendingRef.current = text
      return
    }
    setAnnouncement(text)
    const openWindow = () => {
      announceTimerRef.current = window.setTimeout(() => {
        announceTimerRef.current = null
        const pending = announcePendingRef.current
        if (pending === null) return
        announcePendingRef.current = null
        setAnnouncement(pending)
        openWindow()
      }, ANNOUNCE_MIN_INTERVAL_MS)
    }
    openWindow()
  }, [])

  // The single-pane announcement: immediate, one line per state change.
  useEffect(() => {
    if (layout) return
    setAnnouncement(`Connection: ${status.label}`)
  }, [status.label, layout])

  // ── Restore, validated against the LIVE enumeration ──────────────────────────────────────────────
  // Re-read on every slot change; also the first-mount path.
  const restoredKeyRef = useRef(storageKey)
  useEffect(() => {
    // Skipped on the first run: the initial state already read this key.
    if (restoredKeyRef.current === storageKey) return
    restoredKeyRef.current = storageKey
    setLayout(null)
    setPendingRestore(view ? null : loadLayout(storageKey))
  }, [storageKey, view])

  useEffect(() => {
    if (!pendingRestore) return
    // Desktop only (settled D10). The stored layout is left untouched for the next desktop open.
    if (!isDesktopViewport) {
      setPendingRestore(null)
      return
    }
    // `settled`, not `!loading`: before the first fetch `loading` is false too, and an empty list would
    // drop the whole layout.
    if (!fleet.settled || fleet.loading) return
    setPendingRestore(null)
    if (fleet.error || fleet.windows.length === 0) return

    // No window of its own ⇒ no layout: tiles dial the view path, which never creates a window
    // (`apps/jaahd/console.py::_open_view_pty`), so the console would be stranded on neighbours' windows
    // with no way to start this worktree's agent. Checked against the LIVE list so deliberate cross-slot
    // layouts survive; the stored layout stays on disk. The single pane below resolves-or-CREATES.
    // `clauded` only: a Web Term window is labelled `{worktree basename}|term` by `apps/jaahd/console.py`,
    // not `fleet_launch.slot_window_name`, and this client cannot spell it. The fix is node-side.
    if (kind === 'clauded' && !hasSlotWindow(fleet.windows, project, number)) {
      announceThrottled(
        'Saved layout skipped — this slot has no window open. Showing its own console.',
      )
      return
    }

    const { model, dropped } = restoreLayout(pendingRestore, fleet.windows)
    if (!model) {
      announceThrottled('Saved layout dropped — none of its windows are open.')
      return
    }
    setLayout(model)
    if (dropped.length > 0) {
      announceThrottled(
        `Saved layout restored without ${countOf(dropped.length, 'closed window')}.`,
      )
    }
  }, [pendingRestore, fleet.settled, fleet.loading, fleet.error, fleet.windows, isDesktopViewport,
      announceThrottled, project, number, kind])

  // A desk-authored layout must not survive narrowing to phone width (same rule as the restore gate).
  useEffect(() => {
    if (!isDesktopViewport) setLayout(null)
  }, [isDesktopViewport])

  function openDesigner() {
    // The real box the grid gets: the terminal body, never the designer's proxy canvas.
    const stage = panelRef.current?.querySelector('[data-testid="console-body"]')
    const box = stage?.getBoundingClientRect()
    const measured = box && box.width > 0 && box.height > 0
    setDesignCanvas(measured ? { w: box.width, h: box.height } : null)
    measureCells()
    setDesigning(true)
  }

  // Only a live xterm can measure cells, which is why `splitRefusal` takes CellMetrics as a parameter.
  function measureCells() {
    const cell = activePane()?.cellSize() ?? null
    if (cell) setMeasuredCell(cell)
  }

  // What the designer judges the minimum-tile floor against, with two corrections:
  //   - the cell scaled to MULTI_TILE_FONT_SIZE (single pane measures at 13px);
  //   - `chromeH`: tmux's per-tile status line, `TILE_STATUS_ROWS` text rows, scaling with the cell.
  const cells: CellMetrics = measuredCell
    ? {
        cellW: measuredCell.cellW * (MULTI_TILE_FONT_SIZE / (layout ? tileFontSize(countTiles(layout)) : DEFAULT_FONT_SIZE)),
        cellH: measuredCell.cellH * (MULTI_TILE_FONT_SIZE / (layout ? tileFontSize(countTiles(layout)) : DEFAULT_FONT_SIZE)),
        chromeH: measuredCell.cellH * TILE_STATUS_ROWS,
      }
    : SHIPPED_CELL_METRICS

  // Apply persists the design, closes the popup and applies it. A failed write is not surfaced.
  function applyDesign(model: LayoutModel) {
    saveLayout(storageKey, model)
    setLayout(model)
    setStatus(CONNECTING)
    setDesigning(false)
  }

  // Why the popup cannot show the designer. An in-flight fetch is reported before "no windows".
  const designerNotice = fleet.loading
    ? 'Reading the node’s windows…'
    : fleet.error
      ? fleet.error
      : !designCanvas
        ? 'Console still starting — reopen the designer once the terminal is visible.'
        : 'No console windows on this node.'

  // Key bar → PTY: resolves an arrow's introducer against the LIVE cursor-keys mode (CONSOLE_KEYS), then
  // returns focus to the terminal so the soft keyboard is not stranded.
  function pressKey(key: ConsoleKey) {
    const pane = activePane()
    if (!pane) return
    const data =
      'arrow' in key ? `\x1b${pane.isApplicationCursorKeys() ? 'O' : '['}${key.arrow}` : key.send
    pane.sendKeys(data)
  }

  // ── Paste that landed on CHROME ────────────────────────────────────────────
  // Panes claim image pastes inside their own terminal (capture phase, stopPropagation); this catches
  // pastes with focus on chrome that would otherwise go nowhere. BUBBLE phase, so both can never fire
  // (no double send). Text pastes are left alone.
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    const onPaste = (e: ClipboardEvent) => {
      const [file] = clipboardFiles(e.clipboardData)
      if (!file) return
      e.preventDefault()
      // Bytes first, then resolve the pane: a reconnect may replace the socket during the async gap.
      void file
        .arrayBuffer()
        .then((buffer) => activePane()?.pasteImage(new Uint8Array(buffer), file.type))
        .catch(() => {
          /* the clipboard's bytes were unreadable */
        })
    }
    panel.addEventListener('paste', onPaste)
    return () => panel.removeEventListener('paste', onPaste)
  }, [activePane])

  // Lock body scroll for the overlay's lifetime, so drags do not scroll the page behind it.
  // MODAL ONLY: docked and floating are modeless by design; maximized has nothing behind it.
  useEffect(() => {
    if (!isModal) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [isModal])

  // Drag-to-move (header) and drag-to-resize (corner).
  const attachDrag = useWindowDrag()
  // setManual waits for the first pointermove so a plain header click keeps the CSS-default sizing.
  const startDrag = (e: React.PointerEvent) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest('button, input') || !isMovable || !isDesktopViewport) return
    const rect = panelRef.current!.getBoundingClientRect()
    const base = manual ?? { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
    const startX = e.clientX
    const startY = e.clientY

    const onMove = (ev: PointerEvent) => {
      const maxTop = window.innerHeight - 40
      const maxLeft = window.innerWidth - 40
      setManual((prev) => {
        const box = prev ?? base
        return {
          ...box,
          top: clamp(base.top + (ev.clientY - startY), 0, maxTop),
          left: clamp(base.left + (ev.clientX - startX), 0, maxLeft),
        }
      })
    }
    attachDrag(onMove)
  }

  const startResize = (e: React.PointerEvent) => {
    if (e.button !== 0 || !isMovable || !isDesktopViewport) return
    const rect = panelRef.current!.getBoundingClientRect()
    const base = manual ?? { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
    const startX = e.clientX
    const startY = e.clientY

    const onMove = (ev: PointerEvent) => {
      const maxWidth = window.innerWidth - base.left
      const maxHeight = window.innerHeight - base.top
      setManual((prev) => {
        const box = prev ?? base
        return {
          ...box,
          width: clamp(base.width + (ev.clientX - startX), MIN_WIDTH, maxWidth),
          height: clamp(base.height + (ev.clientY - startY), MIN_HEIGHT, maxHeight),
        }
      })
    }
    attachDrag(onMove)
  }

  // Soft keyboard: bound the panel by the VISUAL viewport. `dvh`/`vh` ignore the keyboard, and iOS Safari
  // does not change `innerHeight` for it, so the typed line ended up behind the keyboard.
  // `visualViewport.height` drives `--console-vv-height` (consumed by `panelSx`; the two must move
  // together) and a re-`fit()` reveals the line. A CSS var keeps this off the render path.
  // Deliberately NOT compensating `offsetTop`: body scroll is locked, and on iOS builds that re-anchor
  // fixed boxes a `top` offset would push the input back under the keyboard.
  useEffect(() => {
    const vv = window.visualViewport
    const panel = panelRef.current
    if (!vv || !panel) return

    let lastHeight = vv.height
    let frame: number | null = null
    // OR-ed across a frame's coalesced events: the first event wins the frame, so per-event capture would
    // lose a later shrink.
    let pendingShrank = false

    const apply = () => {
      // Ignore a transient 0: FitAddon floors at one row, and tmux reflows the pane destructively.
      if (!(vv.height > 0)) return

      // Geometry synchronously; the expensive fit is coalesced below.
      panel.style.setProperty('--console-vv-height', `${vv.height}px`)
      if (vv.height < lastHeight) pendingShrank = true
      lastHeight = vv.height

      // One fit per frame: each row-count change sends a PTY resize (a SIGWINCH storm during the keyboard
      // animation otherwise).
      if (frame !== null) return
      frame = requestAnimationFrame(() => {
        frame = null
        const shrank = pendingShrank
        pendingShrank = false
        // After the setProperty: FitAddon measures the COMPUTED height. An unlaid container retries next event.
        activePane()?.fit()
        // Re-anchor on the cursor only on shrink, so it cannot fight a deliberate scroll-back. (Under
        // `tmux attach`, the alternate screen, this is a no-op; clamp + fit do the work.)
        if (shrank) activePane()?.scrollToBottom()
      })
    }

    apply()
    vv.addEventListener('resize', apply)
    vv.addEventListener('scroll', apply)
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
      vv.removeEventListener('resize', apply)
      vv.removeEventListener('scroll', apply)
      panel.style.removeProperty('--console-vv-height')
    }
  }, [activePane])

  /* Re-fit whenever the panel's BOX changes, whatever moved it (divider drag, dock/undock/maximize, the
   * sidebar collapsing) — observing the element covers causes no cause-specific hook sees.
   * TRAILING DEBOUNCE, not rAF: each `fit()` that changes cols/rows resizes the remote PTY and repaints the
   * TUI, and a divider drag crosses a column every ~9px. */
  useEffect(() => {
    const panel = panelRef.current
    if (!panel || typeof ResizeObserver === 'undefined') return
    let timer: number | null = null
    const observer = new ResizeObserver(() => {
      if (timer !== null) window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        timer = null
        activePane()?.fit()
      }, PANEL_REFIT_DEBOUNCE_MS)
    })
    observer.observe(panel)
    return () => {
      if (timer !== null) window.clearTimeout(timer)
      observer.disconnect()
    }
  }, [activePane])

  // Re-fit after React commits a manual size change (keyed on the committed box, so the final size after
  // pointerup is always fit).
  useEffect(() => {
    if (!manual) return
    activePane()?.fit()
  }, [manual?.width, manual?.height, activePane])

  // On viewport shrink, pull a manual panel back fully into view so it stays grabbable.
  const isManual = manual !== null
  useEffect(() => {
    if (!isManual) return
    const onWindowResize = () => {
      setManual((prev) => {
        if (!prev) return prev
        const maxTop = window.innerHeight - 40
        const maxLeft = window.innerWidth - 40
        return {
          top: clamp(prev.top, 0, maxTop),
          left: clamp(prev.left, 0, maxLeft),
          width: Math.min(prev.width, window.innerWidth),
          height: Math.min(prev.height, window.innerHeight),
        }
      })
    }
    window.addEventListener('resize', onWindowResize)
    return () => window.removeEventListener('resize', onWindowResize)
  }, [isManual])

  /* The drag/resize px box. `right: 'auto'` cancels `floatingPanelSx`'s anchor (with left+width+right
     all set the browser drops one). Withheld unless movable, so a stale floating box never overrides
     docked/maximized sizing. */
  const panelStyle = manual && isMovable
    ? { top: manual.top, left: manual.left, right: 'auto', width: manual.width, height: manual.height, margin: 0 }
    : undefined

  // One socket, one status: a designed grid is a single PTY, so there is nothing to aggregate (AC7.2).
  const headerStatus = status
  const transportStatuses = [status]
  const directState = directToggleState(directEnabled, transportStatuses)
  const directView = DIRECT_TOGGLE_VIEW[directState]

  // The direct transport's visible reason, read from the same panes the toggle describes (not from the
  // single pane's `status`, which a layout unmounts). First non-empty wins; stacked banners would push
  // the terminal off screen.
  const noticeOwner = transportStatuses.find((s) => s.notice)
  const directNotice = noticeOwner?.notice ?? ''

  // One notice bar. `kind: 'error'`: the console is NOT connected and retrying; nothing carries the bytes meanwhile —
  // except `relayed`, where the relay carries them and the notice only explains why (`info`).
  const notice: { text: string; kind: 'error' | 'info' } | null =
    directNotice ? { text: directNotice, kind: noticeOwner?.transport === 'cp' ? 'info' : 'error' } : null

  return (
    /* `data-testid="console-overlay"` is the specs' hook. `components/ConsoleDock.tsx` renders its own
       scrim as the Suspense fallback so the dim persists across the lazy-load swap. */
    <Box
      data-testid="console-overlay"
      data-presentation={presentation}
      sx={isModal ? overlaySx : isDocked ? dockedRootSx : isMaximized ? maximizedRootSx : floatingRootSx}
      /* `dialog` only for the modal presentation (dims, locks scroll, must be dismissed). Docked, floating
         and maximized are modeless: `region` + label. */
      role={isModal ? 'dialog' : 'region'}
      aria-label={`${kindLabel} ${slotLabel}`}
    >
      <Paper
        /* Docked is flush furniture: no shadow bleeding onto the page beside it. */
        elevation={isDocked ? 0 : 24}
        ref={panelRef}
        data-testid="console-panel"
        data-manual={panelStyle ? 'true' : undefined}
        sx={isModal ? panelSx : isDocked ? dockedPanelSx : isMaximized ? maximizedPanelSx : floatingPanelSx}
        style={panelStyle}
      >
        <Box
          data-testid="console-header"
          /* The move cursor is withheld wherever `startDrag` refuses. */
          sx={isMovable ? headerSx : { ...headerSx, cursor: 'default' }}
          onPointerDown={startDrag}
        >
          {/* Header shows the slot TITLE (the identity that distinguishes consoles); kind stays in the
              aria-label, and the tooltip carries title + slug since the title ellipsises. */}
          <Tip title={resolvedTitle ? `${headerTitle} (${slotLabel})` : slotLabel}><Box
            component="span"
            data-testid="console-title"
            sx={(theme) => ({
              fontSize: FONT_SIZE.md,
              fontWeight: FONT_WEIGHT.semibold,
              fontFamily: theme.typography.mono.fontFamily,
              color: 'text.primary',
              /* The title is the element that gives, at EVERY width: a docked panel can be 320px on a
                 desktop, where no `[PHONE]` rule applies, and every control beside it is `flexShrink: 0`
                 (else ✕ Close is pushed off). One line, ellipsised, full string on the tooltip. */
              /* `block`, not `flex`: `text-overflow` does not apply to a flex container, whose text
                 would be clipped with no ellipsis. */
              display: 'block',
              ...ELLIPSIS_SX,
              flex: '1 1 auto',
            })}
            // Tooltip keeps Title + slug.
          >
            {headerTitle}
          </Box></Tip>
          <StatusDot
            aria-hidden={false}
            data-testid="console-status-dot"
            data-state={headerStatus.state}
            {...STATUS_DOT[headerStatus.state]}
            size={grid(2)}
            sx={statusDotSx}
            role="img"
            aria-label={`Connection: ${headerStatus.label}`}
            title={headerStatus.label}
          />
          {readOnly && (
            <Box
              component="span"
              data-testid="console-readonly-badge"
              sx={readOnlyBadgeSx}
            >
              read-only
            </Box>
          )}
          <Box component="span" data-testid="console-announcer" role="status" sx={SR_ONLY_SX}>
            {announcement}
          </Box>
          {/* TRANSPORT controls, a group separate from the session/navigation actions: they are about
              HOW bytes travel. Desktop only (diagnostic). */}
          {/* The ⚡ toggle lives here rather than in `.console-header-actions` (AC7.1's words) so it sits
              beside the ⚙ that configures the same transport; the two groups read as one strip. */}
          {!view && (
          <Box
            data-testid="console-header-transport"
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.5,
              flexShrink: 0,
              mr: 0.5,
            }}
          >
            {/* The ⚡ toggle (AC7.1), shown at every width. role="switch": immediate on/off. `aria-checked`
                follows what the USER ASKED FOR, never what the transport achieved — a failure changes what it
                reports, not its state. */}
            {/* Four states (AC7.2), none by colour alone: name, tooltip (lib/directToggle.ts) and badge
                glyph differ per state; `sx` colour is reinforcement. State rides `data-direct-state`. */}
            <Tip title={directView.title}><IconButton
              type="button"
              role="switch"
              aria-checked={directEnabled}
              data-testid="console-direct-toggle"
              data-direct-state={directState}
              onClick={toggleDirect}
              aria-label={directView.label}
              sx={{
                ...headerButtonSx,
                position: 'relative',
                // Baseline alignment keeps the badge from pulling the bolt off centre.
                display: 'inline-flex',
                alignItems: 'center',
                justifyContent: 'center',
                gap: 0,
                // OFF: dimmer than neighbours — discoverable without demanding attention.
                '&[data-direct-state="off"]': { color: 'text.disabled' },
                // CONNECTING: the pulse is decoration only.
                '@keyframes consoleDirectPulse': { '0%, 100%': { opacity: 1 }, '50%': { opacity: 0.55 } },
                '&[data-direct-state="connecting"]': {
                  color: 'warning.main',
                  animation: 'consoleDirectPulse 1.2s ease-in-out infinite',
                },
                '&[data-direct-state="direct"]': { color: 'success.main' },
                // CLOSED: not connected (node unreachable and retrying, or session ended). `error.main`
                // because there is no relay to land on; the notice bar says why.
                '&[data-direct-state="closed"]': { color: 'error.main' },
                // RELAYED: direct console is not configured for this node — as dim as OFF, badge says why.
                '&[data-direct-state="relayed"]': { color: 'text.disabled' },
                // Hover must not re-colour: the colour IS the state.
                '&:hover:not(:disabled)': { color: 'inherit' },
                // Reduced motion: drop the pulse; the state remains readable without it.
                '@media (prefers-reduced-motion: reduce)': {
                  '&[data-direct-state="connecting"]': { animation: 'none' },
                },
              }}
            >
              <Box component="span" aria-hidden="true">⚡</Box>
              {/* aria-hidden: the accessible name already carries the state. */}
              {directView.badge && (
                <Box
                  component="span"
                  data-testid="console-direct-badge"
                  aria-hidden="true"
                  sx={{ fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.tight, fontWeight: FONT_WEIGHT.bold }}
                >
                  {directView.badge}
                </Box>
              )}
            </IconButton></Tip>
            {isDesktopViewport && (
              <Tip title="Direct connection settings"><IconButton
                sx={headerButtonSx}
                onClick={openDirectSettings}
                aria-label="Direct connection settings"
              >
                ⚙
              </IconButton></Tip>
            )}
          </Box>
          )}
          <Box
            data-testid="console-header-actions"
            sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}
          >
            {/* Only while closed: the automatic reconnect covers recoverable drops. */}
            {headerStatus.state === 'closed' && (
              <IconButton
                sx={headerButtonSx}
                // Only bumps the nonce; the pane's connect effect owns the status reset.
                onClick={() => setReconnectNonce((n) => n + 1)}
                aria-label="Reconnect"
              >
                ↻
              </IconButton>
            )}
            {/* The pane's whole history as plain text — the readable way back past an inline-redrawing CLI. The write
                lands when the node's OSC 52 does, so the button flashes what it heard back. */}
            {headerStatus.state === 'connected' && !readOnly && !showChat && (
              <CopyButton
                variant="icon"
                testId="console-copy-pane"
                label="Copy pane text"
                copiedLabel="Copied"
                failedLabel="Copy failed"
                flashMs={2000}
                announce={false}
                /* The header's rhythm and, as its neighbours, the theme's phone tap floor (the icon face drops it). */
                sx={(theme) => ({
                  minWidth: headerButtonSx.minWidth,
                  [theme.breakpoints.down('sm')]: { minWidth: SIZE.tapTarget, minHeight: SIZE.tapTarget },
                })}
                text={{ run: (settle) => { activePane()?.copyPaneText(settle) } }}
              />
            )}
            {(onPrev || onNext) && (
              <>
                <IconButton
                  sx={headerButtonSx}
                  onClick={onPrev}
                  disabled={!onPrev || !hasPrev}
                  aria-label="Previous session"
                >
                  ‹
                </IconButton>
                <IconButton
                  sx={headerButtonSx}
                  onClick={onNext}
                  disabled={!onNext || !hasNext}
                  aria-label="Next session"
                >
                  ›
                </IconButton>
              </>
            )}
            {isDesktopViewport && !view && (
              <Tip title="Console layout"><IconButton
                sx={headerButtonSx}
                onClick={openDesigner}
                aria-label="Console layout"
              >
                ▦
              </IconButton></Tip>
            )}
            {/* DOCK/UNDOCK and MAXIMIZE/RESTORE: only when something owns the view and above
                `DOCK_MIN_VIEWPORT`. Each has `title` + `aria-label`, since a glyph alone cannot say what it does. */}
            {onSetView && !isModal && (
              <>
                {/* Hidden while maximized: Restore already returns to the dock. */}
                {!isMaximized && (
                  <Tip title={isDocked ? 'Undock console' : 'Dock console to the right'}><IconButton
                    sx={headerButtonSx}
                    onClick={() => onSetView(isDocked ? 'floating' : 'docked')}
                    aria-label={isDocked ? 'Undock console' : 'Dock console to the right'}
                  >
                    {isDocked ? '⧉' : '⇥'}
                  </IconButton></Tip>
                )}
                <MaximizeButton
                  noun="console"
                  maximized={isMaximized}
                  onToggle={() => {
                    if (isMaximized) {
                      onSetView(restoreViewRef.current)
                      return
                    }
                    restoreViewRef.current = isDocked ? 'docked' : 'floating'
                    onSetView('maximized')
                  }}
                />
              </>
            )}
            <CloseButton noun="console" onClose={onClose} />
          </Box>
        </Box>
        {/* The transport's visible reason, OUTSIDE the terminal: tmux repaints the body on re-attach and
            would scroll the in-terminal copy away. Non-dismissible: acknowledging does not clear a
            standing failure. `role="status"`; the reason is a plain string, never markup. */}
        {notice && (
          /* A bar, not a toast (a timeout would take the explanation with it). An `info` notice maps to
             `warning`. `role="status"` overrides Alert's assertive `role="alert"`. */
          <Alert
            data-testid="console-notice"
            data-kind={notice.kind}
            severity={notice.kind === 'error' ? 'error' : 'warning'}
            role="status"
            icon={false}
            sx={{
              flexShrink: 0,
              borderRadius: RADIUS.none,
              fontSize: FONT_SIZE.sm,
              lineHeight: LINE_HEIGHT.snug,
              py: 0,
              '& .MuiAlert-message': { py: 0.5 },
            }}
          >
            {notice.text}
          </Alert>
        )}
        {showChat && runtime && chatTaskId !== null && chatSessionId !== null ? (
          <ChatView
            projectId={projectId}
            number={number}
            nodeId={nodeId}
            runtime={{ ...runtime, task_id: chatTaskId, session_id: chatSessionId }}
          />
        ) : pendingRestore && isDesktopViewport ? (
          // A saved layout is being validated; mounting the plain pane would attach a PTY about to be
          // replaced. Holds the body's box so the panel does not reflow.
          <Box data-testid="console-body" aria-busy="true" sx={{ flex: 1, minHeight: 0 }} />
        ) : (
          <ConsolePane
            // Keyed on the spec: a re-design is a clean detach/re-attach (the node takes the old grid down),
            // not a live pane re-dialled onto another window with stale scrollback.
            key={layoutSpec ?? 'single'}
            ref={paneRef}
            projectId={projectId}
            number={number}
            nodeId={nodeId}
            kind={kind}
            // Absent for a plain console; present, the node builds the tmux window this PTY shows.
            layout={layoutSpec ?? undefined}
            fontSize={layout ? tileFontSize(countTiles(layout)) : undefined}
            onStatusChange={setStatus}
            reconnectNonce={reconnectNonce}
            // One-way for a view principal (`disableStdin` + the pane's write gate). The CP relay and the
            // daemon's `tmux -r` enforce it independently; this half keeps the UI honest.
            readOnly={readOnly}
            // Passed through unexamined; absent means "the caller did not say".
            hadLiveSession={hadLiveSession}
            view={view}
            // The direct route cannot open a VM view; relay only.
            direct={directEnabled && !view}
            // Only while direct mode is on: the relay path ignores it, and changing it re-runs the pane's
            // connect effect.
            directUrlOverride={directEnabled && !view ? directUrlOverride : undefined}
          />
        )}
        {/* onPointerDown preventDefault: moving focus to the button would dismiss the phone keyboard. */}
        {/* Keys a soft keyboard cannot produce (Esc, arrows, Tab, ^C). Narrow viewports SCROLL the bar
            (`pan-x`) rather than wrap. Gone entirely for a one-way console, and while Chat replaces the terminal. */}
        {!readOnly && !showChat && (
        <Box
          data-testid="console-keys"
          role="group"
          aria-label="Terminal keys"
          sx={{
            display: 'flex',
            flexShrink: 0,
            gap: 0.5,
            px: 1,
            py: 0.5,
            bgcolor: 'background.default',
            borderTop: 1,
            borderColor: 'divider',
            overflowX: 'auto',
            touchAction: 'pan-x',
          }}
        >
          {CONSOLE_KEYS.map((key) => (
            <Button
              key={key.label}
              type="button"
              variant="outlined"
              color="inherit"
              data-testid="console-key"
              onPointerDown={(e) => e.preventDefault()}
              onClick={() => pressKey(key)}
              aria-label={key.ariaLabel}
              sx={(theme) => ({
                flex: '0 0 auto',
                minWidth: grid(6.5),
                px: 1,
                py: 0.5,
                fontFamily: theme.typography.mono.fontFamily,
                fontSize: FONT_SIZE.md,
                lineHeight: LINE_HEIGHT.tight,
                borderColor: 'divider',
                color: 'text.primary',
                // Thumb-sized keys on a phone, where this bar is the only way to send Esc or arrows.
                [PHONE]: { minWidth: SIZE.tapTarget, minHeight: SIZE.tapTarget, fontSize: FONT_SIZE.base },
              })}
            >
              {key.label}
            </Button>
          ))}
        </Box>
        )}
        {/* Bottom-right resize grip; hidden on phones and for docked/maximized panels. */}
        <Box
          data-testid="console-resize-handle"
          onPointerDown={startResize}
          aria-hidden="true"
          sx={{
            position: 'absolute',
            right: 0,
            bottom: 0,
            width: SIZE.iconSm,
            height: SIZE.iconSm,
            cursor: 'nwse-resize',
            touchAction: 'none',
            '&::after': {
              content: '""',
              position: 'absolute',
              right: grid(0.5),
              bottom: grid(0.5),
              width: grid(1),
              height: grid(1),
              borderRight: '2px solid',
              borderBottom: '2px solid',
              borderColor: 'text.disabled',
            },
            [PHONE]: { display: 'none' },
            ...(isMovable ? null : { display: 'none' }),
          }}
        />
      </Paper>
      {directSettingsOpen && (
        <FormDialog
          title="Direct connection"
          submitLabel="Apply"
          onSubmit={applyDirectSettings}
          onClose={() => setDirectSettingsOpen(false)}
          testId="console-direct-settings"
        >
          {/* The switch is the header ⚡; this popup explains what it DOES. */}
          <Box data-testid="console-direct-note">
            <Typography variant="body2">Connect straight to the node</Typography>
            {/* The pre-toggle explainer (`DIRECT_MODE_EXPLAINER`); must describe the trade honestly. */}
            <Typography variant="caption" component="p" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.snug }}>
              {DIRECT_MODE_EXPLAINER}
            </Typography>
          </Box>
          {/* Monospace, full width: a URL wrapped mid-host cannot be checked. */}
          <TextField
            data-testid="console-direct-url"
            label={<HelpLabel label="Direct address override" help={DIRECT_OVERRIDE_TIP} />}
            type="text"
            fullWidth
            slotProps={{
              htmlInput: { inputMode: 'url', spellCheck: false, 'aria-description': DIRECT_OVERRIDE_TIP },
            }}
            placeholder="wss://node.tailnet.example:7787/console/project/1/clauded"
            value={draftOverride}
            onChange={(e) => setDraftOverride(e.target.value)}
            sx={(theme) => ({
              '& .MuiInputBase-input': {
                fontFamily: theme.typography.mono.fontFamily,
                fontSize: FONT_SIZE.sm,
              },
            })}
          />
        </FormDialog>
      )}
      {designing &&
        (designCanvas && fleet.windows.length > 0 ? (
          <LayoutDesigner
            windows={fleet.windows}
            // What the console is ACTUALLY rendering, so the designer can adjust rather than rebuild.
            // `layout`, not the stored model (already validated by `restoreLayout`); the designer
            // re-reconciles against `fleet.windows` on open for windows killed or opened since.
            initialModel={layout ?? undefined}
            canvas={designCanvas}
            cells={cells}
            onApply={applyDesign}
            onCancel={() => setDesigning(false)}
          />
        ) : (
          // Same shell carrying the reason, not an empty designer the user must interpret. Paper testid
          // `layout-designer-backdrop` + inner `layout-designer-notice`: e2e `openDesigner` tells the two apart.
          <FormDialog
            title="Console layout"
            submitLabel="Close"
            hideCancel
            maxWidth="xs"
            onSubmit={() => setDesigning(false)}
            onClose={() => setDesigning(false)}
            testId="layout-designer-backdrop"
          >
            <Typography variant="body2" component="p" role="status" data-testid="layout-designer-notice">
              <span data-testid="layout-designer-status">{designerNotice}</span>
            </Typography>
          </FormDialog>
        ))}
    </Box>
  )
}
