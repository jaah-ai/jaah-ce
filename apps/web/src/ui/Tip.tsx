import {
  cloneElement, useEffect, useRef, useState,
  type FocusEvent, type JSX, type MouseEvent, type ReactElement, type ReactNode, type SyntheticEvent,
  type TouchEvent,
} from 'react'
import Tooltip, { type TooltipProps } from '@mui/material/Tooltip'
import Box from '@mui/material/Box'

/** How long a tapped tooltip stays up (a caller's `leaveTouchDelay` overrides it). */
export const TAP_SHOW_MS = 4000
/** The one tooltip timing: a resting pointer, not one passing over, opens a tip; leaving closes it at once. Moving
 * on to the next control while one is up opens its tip at once (MUI's `enterNextDelay`, 0). */
export const TIP_TIMING = { enterDelay: 600, enterNextDelay: 0, leaveDelay: 0 } as const
/** An interactive tip's grace for the pointer to cross the gap from its control onto the tip. */
export const TIP_INTERACTIVE_LEAVE_MS = 150
/** The one tooltip measure; its ground and text colours are the theme's (`theme.ts` `MuiTooltip`). */
export const TIP_SLOT_PROPS = { tooltip: { sx: { maxWidth: '42ch' } } }
/** A touch that travels further than this is a scroll or a swipe, not a tap. */
const TAP_SLOP_PX = 10
/** A browser fires mouse events for a tap within this long after the touch; they are not a hover. */
const TAP_MOUSE_MS = 1000
/** What a tap ACTS on: tapping one of these (not disabled) does its job and never pops a tip. */
const ACTION = ['button', 'a[href]', 'input', 'select', 'textarea', 'summary', 'label', '[role="row"][tabindex="0"]',
  ...['button', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'tab', 'checkbox', 'switch', 'radio', 'option',
    'treeitem', 'combobox', 'slider', 'spinbutton', 'searchbox'].map(role => `[role="${role}"]`)].join(', ')

/** True when the touch landed on an enabled control, so the tap is that control's action. A label is as disabled as
 * the control it names (MUI marks a disabled `FormControlLabel` with `.Mui-disabled`, never `:disabled`). */
function tapActs(target: EventTarget | null): boolean {
  const hit = target instanceof Element ? target.closest(ACTION) : null
  if (hit === null || hit.matches(':disabled, [aria-disabled="true"], .Mui-disabled')) return false
  return !(hit instanceof HTMLLabelElement && hit.control instanceof HTMLInputElement && hit.control.disabled)
}

/* The last touch ANYWHERE on the page. A browser fires compatibility mouse events after a tap on whatever lies under
 * the finger then — after a tap that changes the layout (opening a section), a label that slid under it — and a
 * touch screen never sends the mouse leave that would close the tip they open. */
let touchedAnywhereAt = -Infinity
if (typeof document !== 'undefined') {
  // From the touch's start and its end: a long press fires its mouse events after the lift.
  const stamp = () => { touchedAnywhereAt = performance.now() }
  document.addEventListener('touchstart', stamp, { capture: true, passive: true })
  document.addEventListener('touchend', stamp, { capture: true, passive: true })
}
/** True when `event` is a mouse event a browser fired for a recent tap, not a hover: it must never open a tip. Ask it
 *  as the mouse event ARRIVES — a tip opens `enterDelay` later, when the window may have passed. */
export function tapMouseEcho(event: { type: string } | null | undefined): boolean {
  return Boolean(event?.type.startsWith('mouse')) && performance.now() - touchedAnywhereAt < TAP_MOUSE_MS
}

/* Every tap-held tooltip (this one's and `ui/HelpLabel`'s) registers its closer here: one tap-held tip at a time. */
export const tapClosers = new Set<() => void>()
export function closeTapTips(): void { tapClosers.forEach(close => close()) }

/* ══ THE CONSOLE'S ONE TOOLTIP ══════════════════════════════════════════════════════════════════════════
 *
 * Every hover text on every page is this: the theme's opaque tooltip, opened by hover (after `TIP_TIMING`),
 * keyboard focus, and a tap on a touch screen.
 * NEVER IN THE WAY: a tip is not interactive by default (`disableInteractive`): the pointer passes through it to
 * what lies under it (the next row), and it closes the moment the pointer leaves its control. Only a tip holding
 * something to click (`ui/HelpLabel`'s "More") passes `disableInteractive={false}` with `TIP_INTERACTIVE_LEAVE_MS`.
 * A native `title=` attribute is never a tooltip here: it ignores the theme and never opens on touch or focus.
 *
 * `describeChild` by default, so the child keeps its own accessible name and the text is its description — the
 * semantics a `title=` had; MUI also keeps the text as the child's `title` while closed, so a name that fell back
 * to the title still does. An icon-only control whose name IS this text passes `describeChild={false}` (the text
 * becomes its `aria-label`).
 * ALWAYS MOUNTED: an empty `title` renders the same tree with nothing to show (and no `title` attribute), so a
 * control whose hint comes and goes is never remounted (that would drop its focus).
 * A disabled MUI control takes no pointer events, so its tip never opens; the text stays its `title` for assistive
 * tech. A disabled control's reason a sighted reader must see rides `ui/DisabledReason` (or `pointerEvents: 'auto'`
 * on the control, as `NodeDetailPage`'s action buttons).
 *
 * A TAP ON AN ACTION — a button, link, field or other enabled control (`ACTION`) — only does that action: it never
 * pops the tip, neither by the tap nor by the mouse events a browser fires after it (`TAP_MOUSE_MS`).
 * A TAP elsewhere — a touch that lifts without travelling (`TAP_SLOP_PX`), so a scroll never pops one — opens it and holds it
 * `TAP_SHOW_MS` (MUI's own touch path waits for a HELD touch, and the mouse events a browser fires after a tap would
 * close it at once). The next touch anywhere, Escape, or focus leaving the control closes it; while held it lets
 * taps through to what is under it.
 * A CONTROLLED tip (`open` passed: `ui/HelpLabel`, `ui/DisabledReason`, a few callers that gate their own opening)
 * owns its own touch path and gets MUI's.
 *
 * NEVER AN ECHO: an uncontrolled tip whose text already reads in full on its control stays shut (`shownInFull`) —
 * a nav row's label beside its own name, a title that fits its cell. It still opens where that text is not on
 * screen (an icon-only rail or phone width hides the word with `display: none`) or is cut (an ellipsis, a line
 * clamp), so a caller passes the full text and never gates it on width or truncation itself. */
export type TipProps = Omit<TooltipProps, 'title'> & { title?: ReactNode; children: ReactElement }

/** Words only: case, spacing and the punctuation around them (`@name`, `Tab:`) never make two texts differ. */
function words(text: string): string {
  return text.replace(/\s+/g, ' ').trim().toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '')
}

/** The overflow values that hide what does not fit. */
const CLIPS = new Set(['hidden', 'clip', 'auto', 'scroll'])

/** A box at most this big is a screen-reader-only span (`visuallyHidden`), clipped by design, never a cut. */
const SR_ONLY_PX = 1

/** True when `node` clips its overflow and has some (its own content wider or taller than its box). */
function cuts(node: HTMLElement, style: CSSStyleDeclaration): boolean {
  if (node.clientWidth <= SR_ONLY_PX && node.clientHeight <= SR_ONLY_PX) return false
  return (CLIPS.has(style.overflowX || style.overflow) && node.scrollWidth > node.clientWidth)
    || (CLIPS.has(style.overflowY || style.overflow) && node.scrollHeight > node.clientHeight)
}

/** True when `text` reads in full on `el`: its rendered text (`innerText`, which skips what `display: none` hides)
 * holds it as whole words, nothing in `el` that clips its overflow is cutting content, and no clipping ancestor
 * cuts `el` itself across its width (an ellipsis cell around a plain span). */
export function shownInFull(el: Element | null, text: string): boolean {
  if (!(el instanceof HTMLElement) || typeof el.innerText !== 'string') return false
  const want = words(text)
  if (!want) return false
  const escaped = want.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  if (!new RegExp(`(^|[^\\p{L}\\p{N}])${escaped}([^\\p{L}\\p{N}]|$)`, 'u').test(words(el.innerText))) return false
  if ([el, ...el.querySelectorAll('*')].some(node => node instanceof HTMLElement && cuts(node, getComputedStyle(node)))) {
    return false
  }
  const own = el.getBoundingClientRect()
  for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
    const style = getComputedStyle(up)
    if (CLIPS.has(style.overflowX || style.overflow)) {
      const box = up.getBoundingClientRect()
      if (own.left < box.left - 0.5 || own.right > box.right + 0.5) return false
    }
  }
  return true
}

type HoverHandlers = {
  onMouseOver?: (event: MouseEvent) => void
  onFocus?: (event: FocusEvent) => void
}

type TouchHandlers = {
  onTouchStart?: (event: TouchEvent) => void
  onTouchMove?: (event: TouchEvent) => void
  onTouchEnd?: (event: TouchEvent) => void
}

export function Tip({ title, children, slotProps, ...rest }: TipProps): JSX.Element {
  const [open, setOpen] = useState(false)
  const [held, setHeld] = useState(false)
  const holdTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const touchFrom = useRef<{ x: number; y: number } | null>(null)
  const touchedAt = useRef(-Infinity)
  const anchor = useRef<Element | null>(null)
  const release = useRef(() => {})
  release.current = () => {
    clearTimeout(holdTimer.current)
    holdTimer.current = undefined
    setHeld(false)
    setOpen(false)
  }
  useEffect(() => {
    const close = () => release.current()
    tapClosers.add(close)
    return () => { tapClosers.delete(close); clearTimeout(holdTimer.current) }
  }, [])
  // While held, the next touch anywhere (a tap elsewhere, the start of a scroll) lets it go.
  useEffect(() => {
    if (!held) return undefined
    const onDown = () => release.current()
    document.addEventListener('pointerdown', onDown, true)
    return () => document.removeEventListener('pointerdown', onDown, true)
  }, [held])

  const shown = title === undefined || title === null || title === false ? '' : title
  const controlled = rest.open !== undefined
  const { leaveTouchDelay, onOpen, onClose, ...pass } = rest
  const own = children.props as TouchHandlers & HoverHandlers
  const echo = (el: Element | null) => (typeof shown === 'string' || typeof shown === 'number')
    && shownInFull(el, String(shown))
  // MUI leaves `title=""` on the child of an empty tooltip; the bare child has none.
  const child = shown === ''
    ? cloneElement(children as ReactElement<{ title?: string }>, { title: undefined })
    : controlled
      ? children
      : cloneElement(children as ReactElement<TouchHandlers & HoverHandlers>, {
        // MUI opens after `enterDelay`, when the event no longer names its control: note the control now.
        onMouseOver: (event: MouseEvent) => { own.onMouseOver?.(event); anchor.current = event.currentTarget },
        onFocus: (event: FocusEvent) => { own.onFocus?.(event); anchor.current = event.currentTarget },
        onTouchStart: (event: TouchEvent) => {
          own.onTouchStart?.(event)
          touchedAt.current = performance.now()
          const t = event.touches[0]
          touchFrom.current = t && !tapActs(event.target) ? { x: t.clientX, y: t.clientY } : null
        },
        onTouchMove: (event: TouchEvent) => {
          own.onTouchMove?.(event)
          const t = event.touches[0]
          const from = touchFrom.current
          if (from && t && Math.hypot(t.clientX - from.x, t.clientY - from.y) > TAP_SLOP_PX) touchFrom.current = null
        },
        onTouchEnd: (event: TouchEvent) => {
          own.onTouchEnd?.(event)
          if (!touchFrom.current) return
          touchFrom.current = null
          if (echo(event.currentTarget)) return
          closeTapTips()
          setOpen(true)
          setHeld(true)
          holdTimer.current = setTimeout(() => release.current(), leaveTouchDelay ?? TAP_SHOW_MS)
        },
      })
  const state = controlled
    ? { open: rest.open, onOpen, onClose, leaveTouchDelay }
    : {
      open,
      onOpen: (event: SyntheticEvent) => {
        // The mouse events after a tap are not a hover: the tap path above already chose.
        if (performance.now() - touchedAt.current < TAP_MOUSE_MS) return
        if (echo(anchor.current)) return
        setOpen(true)
        onOpen?.(event)
      },
      // A tap-held tip ignores the mouse leave a touch browser fires after the tap; Escape or a blur closes it.
      onClose: (event: Event | SyntheticEvent) => {
        if (holdTimer.current !== undefined && event.type !== 'keydown' && event.type !== 'blur') return
        release.current()
        onClose?.(event)
      },
      disableTouchListener: true,
      disableInteractive: held || (pass.disableInteractive ?? true),
    }
  return (
    <Tooltip
      title={shown}
      describeChild
      disableInteractive
      {...TIP_TIMING}
      slotProps={slotProps ?? TIP_SLOT_PROPS}
      {...pass}
      {...state}
    >
      {child}
    </Tooltip>
  )
}

/** A tip's body as short lines, one per fact (why a pipeline step waits, B4): each wraps at its words, never cut. */
export function WaitReasons({ lines, testId = 'wait-reasons' }: { lines: readonly string[]; testId?: string }): JSX.Element {
  return (
    <Box component="ul" data-testid={testId} sx={{ m: 0, p: 0, listStyle: 'none', overflowWrap: 'break-word' }}>
      {lines.map((line, i) => <Box component="li" key={i} sx={{ '& + &': { mt: 0.5 } }}>{line}</Box>)}
    </Box>
  )
}

export default Tip
