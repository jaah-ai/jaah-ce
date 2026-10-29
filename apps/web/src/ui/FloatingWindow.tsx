/* A NON-MODAL floating card (the Help panel, `SupportChat.tsx`; no backdrop, the page behind stays usable)
 * that the user drags by its header and resizes from any edge or corner. It opens bottom-right at a default
 * size; once moved or resized, its geometry is remembered per browser and reused, clamped back inside the
 * viewport on open and on every window resize (a smaller screen never strands it off-screen). Minimized, it
 * collapses to its header, anchored where its bottom edge was, so a bottom-right window folds down into the
 * corner rather than floating mid-screen. Phone width never renders this: the Help panel keeps a full-screen
 * sheet. */
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import Box from '@mui/material/Box'
import Paper from '@mui/material/Paper'
import Portal from '@mui/material/Portal'
import { readJsonObject, writeJson } from '../lib/safeStorage'

export type Geometry = { left: number; top: number; width: number; height: number }

const GEOMETRY_KEY = 'support-chat:geometry'
export const MIN_WIDTH = 320
export const MIN_HEIGHT = 360
/** Gap kept between the window and the viewport's edges. */
export const MARGIN = 16
const DEFAULT_WIDTH = 400
const DEFAULT_HEIGHT = 620

type Edge = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
const EDGES: readonly Edge[] = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']
const CURSOR: Record<Edge, string> = {
  n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize',
  ne: 'nesw-resize', sw: 'nesw-resize', nw: 'nwse-resize', se: 'nwse-resize',
}

/** Bottom-right, `MARGIN` from the edges, as large as the default allows. */
export function defaultGeometry(vw: number, vh: number): Geometry {
  const width = Math.min(DEFAULT_WIDTH, vw - 2 * MARGIN)
  const height = Math.min(DEFAULT_HEIGHT, vh - 2 * MARGIN)
  return { left: vw - MARGIN - width, top: vh - MARGIN - height, width, height }
}

/** `g` shrunk to fit the viewport and moved fully inside it — never below the minimum size while the
 *  viewport has room for it. */
export function clampGeometry(g: Geometry, vw: number, vh: number): Geometry {
  const width = Math.max(Math.min(MIN_WIDTH, vw - 2 * MARGIN), Math.min(g.width, vw - 2 * MARGIN))
  const height = Math.max(Math.min(MIN_HEIGHT, vh - 2 * MARGIN), Math.min(g.height, vh - 2 * MARGIN))
  const left = Math.min(Math.max(g.left, MARGIN), Math.max(MARGIN, vw - MARGIN - width))
  const top = Math.min(Math.max(g.top, MARGIN), Math.max(MARGIN, vh - MARGIN - height))
  return { left, top, width, height }
}

/** The geometry `edge` produces when dragged by (dx, dy) from `start`: between the minimum size and the
 *  viewport's margin, and the opposite edge stays put. */
export function resizeGeometry(start: Geometry, edge: Edge, dx: number, dy: number, vw: number, vh: number): Geometry {
  const fit = (size: number, room: number, min: number) => Math.max(Math.min(min, room), Math.min(size, room))
  let { left, top, width, height } = start
  if (edge.includes('e')) width = fit(start.width + dx, vw - MARGIN - start.left, MIN_WIDTH)
  if (edge.includes('s')) height = fit(start.height + dy, vh - MARGIN - start.top, MIN_HEIGHT)
  if (edge.includes('w')) {
    width = fit(start.width - dx, start.left + start.width - MARGIN, MIN_WIDTH)
    left = start.left + start.width - width
  }
  if (edge.includes('n')) {
    height = fit(start.height - dy, start.top + start.height - MARGIN, MIN_HEIGHT)
    top = start.top + start.height - height
  }
  return { left, top, width, height }
}

function readStored(): Geometry | null {
  const o = readJsonObject('local', GEOMETRY_KEY)
  if (!o) return null
  const { left, top, width, height } = o
  const nums = [left, top, width, height]
  return nums.every(n => typeof n === 'number' && Number.isFinite(n))
    ? { left: left as number, top: top as number, width: width as number, height: height as number }
    : null
}

function initial(): Geometry {
  const vw = window.innerWidth
  const vh = window.innerHeight
  return clampGeometry(readStored() ?? defaultGeometry(vw, vh), vw, vh)
}

/**
 * ONE POINTER GESTURE AT A TIME, shared by the move and every resize edge. `track` follows one
 * pointer on `e`'s target, applying `next(dx, dy)` on each move, and hands the last value to `commit` at the
 * end when it moved anything (a plain click must not pin the default placement). A second finger is ignored
 * rather than fighting the first. `active` is true while a gesture runs (transitions off, text unselectable).
 */
function useGesture() {
  const [active, setActive] = useState(false)
  const gesture = useRef(false)
  function track<T>(
    e: ReactPointerEvent<HTMLElement>,
    start: T,
    next: (start: T, dx: number, dy: number) => T,
    apply: (value: T) => void,
    commit: (value: T) => void,
  ): void {
    if (e.button !== 0 || gesture.current) return
    e.preventDefault()
    const x0 = e.clientX
    const y0 = e.clientY
    const id = e.pointerId
    const target = e.currentTarget
    target.setPointerCapture?.(id)
    gesture.current = true
    setActive(true)
    let last = start
    const move = (ev: PointerEvent) => {
      if (ev.pointerId !== id) return
      last = next(start, ev.clientX - x0, ev.clientY - y0)
      apply(last)
    }
    const end = (ev: PointerEvent) => {
      if (ev.pointerId !== id) return
      for (const type of ['pointerup', 'pointercancel', 'lostpointercapture'] as const) target.removeEventListener(type, end)
      target.removeEventListener('pointermove', move)
      gesture.current = false
      setActive(false)
      if (last !== start) commit(last)
    }
    target.addEventListener('pointermove', move)
    for (const type of ['pointerup', 'pointercancel', 'lostpointercapture'] as const) target.addEventListener(type, end)
  }
  return { active, track }
}

/** Presses that start on a control are left to that control: only the header's bare surface drags. */
const startsOnControl = (e: ReactPointerEvent<HTMLElement>): boolean =>
  Boolean((e.target as HTMLElement).closest('button, a, input, textarea, [role="button"]'))

const DRAG_SX = { cursor: 'move', userSelect: 'none', touchAction: 'none' } as const

/** What a header needs to become the drag handle: spread onto it. Presses that start on a button (or any
 *  other control) are left to that control. */
export type DragHandleProps = { onPointerDown: (e: ReactPointerEvent<HTMLElement>) => void; sx: object }

type Props = {
  open: boolean
  minimized: boolean
  /** The height of the header shown while minimized. */
  collapsedHeight?: number
  onEscape: () => void
  testId?: string
  ariaLabel: string
  children: (drag: DragHandleProps) => ReactNode
}

/** The Paper's own props: non-modal, Escape handed to the owner. */
function paperProps({ ariaLabel, testId, minimized, onEscape }: Props) {
  return {
    role: 'dialog',
    'aria-label': ariaLabel,
    'aria-modal': false,
    'data-testid': testId,
    'data-minimized': minimized || undefined,
    elevation: 12,
    onKeyDown: (e: ReactKeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onEscape()
      }
    },
  }
}

const PAPER_SX = {
  position: 'fixed',
  /* Below every modal (a page's temporary Drawers are 1200, Dialogs 1300) and above the app's own
     chrome (≤ 60): a modal opened over the window covers it cleanly, never the reverse — above one, the
     modal's focus trap would steal every click in the window. */
  zIndex: (theme: { zIndex: { drawer: number } }) => theme.zIndex.drawer - 1,
  display: 'flex',
  flexDirection: 'column',
  overflow: 'hidden',
  borderRadius: 3,
  border: 1,
  borderColor: 'divider',
} as const

export default function FloatingWindow(props: Props) {
  const { open, minimized, collapsedHeight = 0, children } = props
  const [geometry, setGeometry] = useState<Geometry>(() => initial())
  const { active, track } = useGesture()
  const latest = useRef(geometry)
  latest.current = geometry

  // Back inside the viewport whenever it opens or the window shrinks; on close, focus returns to whatever
  // opened it (the Help button), as a modal's would.
  useEffect(() => {
    if (!open) return
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const fit = () => setGeometry(g => clampGeometry(g, window.innerWidth, window.innerHeight))
    fit()
    window.addEventListener('resize', fit)
    return () => {
      window.removeEventListener('resize', fit)
      if (opener?.isConnected) opener.focus()
    }
  }, [open])

  function trackGeometry(e: ReactPointerEvent<HTMLElement>, next: (start: Geometry, dx: number, dy: number) => Geometry): void {
    track(
      e,
      latest.current,
      (start, dx, dy) => clampGeometry(next(start, dx, dy), window.innerWidth, window.innerHeight),
      setGeometry,
      last => writeJson('local', GEOMETRY_KEY, last),
    )
  }

  const drag: DragHandleProps = {
    onPointerDown: e => {
      if (startsOnControl(e)) return
      trackGeometry(e, (start, dx, dy) => ({ ...start, left: start.left + dx, top: start.top + dy }))
    },
    sx: DRAG_SX,
  }

  if (!open) return null
  const shown = minimized
    ? { ...geometry, top: geometry.top + geometry.height - collapsedHeight, height: collapsedHeight }
    : geometry

  return (
    <Portal>
      <Paper
        {...paperProps(props)}
        sx={{
          ...PAPER_SX,
          left: shown.left,
          top: shown.top,
          width: shown.width,
          height: shown.height,
          // No transition while the pointer drives it: the window must track the cursor exactly.
          transition: active ? 'none' : 'height 160ms ease, top 160ms ease',
          '@media (prefers-reduced-motion: reduce)': { transition: 'none' },
          ...(active && { userSelect: 'none' }),
        }}
      >
        {children(drag)}
        {!minimized && EDGES.map(edge => (
          <Box
            key={edge}
            aria-hidden="true"
            data-testid={`support-resize-${edge}`}
            onPointerDown={e => trackGeometry(e, (start, dx, dy) => resizeGeometry(start, edge, dx, dy, window.innerWidth, window.innerHeight))}
            sx={{
              position: 'absolute',
              touchAction: 'none',
              cursor: CURSOR[edge],
              zIndex: 1,
              ...(edge.length === 2 ? { width: 14, height: 14 } : {}),
              ...(edge.includes('n') && { top: 0 }),
              ...(edge.includes('s') && { bottom: 0 }),
              ...(edge.includes('w') && { left: 0 }),
              ...(edge.includes('e') && { right: 0 }),
              ...(edge === 'n' || edge === 's' ? { left: 14, right: 14, height: 6 } : {}),
              ...(edge === 'e' || edge === 'w' ? { top: 14, bottom: 14, width: 6 } : {}),
            }}
          />
        ))}
      </Paper>
    </Portal>
  )
}
