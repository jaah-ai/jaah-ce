import { type KeyboardEvent, type MouseEvent, type ReactNode, useEffect, useLayoutEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import MuiTab from '@mui/material/Tab'
import MuiTabs, { tabsClasses } from '@mui/material/Tabs'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { Theme } from '@mui/material/styles'
import { FORM_BAR_STUCK_ATTR, scrollportOf, useStuck } from '../ui/FormActionBar'
import { ACT_AS_OVERLAP_VAR } from './ActAsBar'
import { PHONE, PHONE_QUERY, SR_ONLY_SX, TAP_FLOOR_DOWN } from '../ui/listStyles'
import { paperGround } from '../theme'

/** A sticky strip's fill: the page's ground, or its paper's (`theme.ts`'s `paperGround`, lighter on elevated dark paper). */
const groundOfSticky = (theme: Theme, sticky: 'default' | 'paper'): string =>
  sticky === 'paper' ? paperGround(theme) : (theme.vars ?? theme).palette.background.default

// The app's tab shell over `@mui/material/Tabs` (ARIA roles, roving tabIndex, automatic activation).
// Arrow keys stay hand-rolled: MUI's handler does not wrap, and this shell's contract (asserted in
// `NodeDetailPage.test.tsx`) is that they wrap round. `aria-controls` and element ids stay explicit so
// `TabPanel` agrees with them and `idPrefix` can namespace two shells on one page. The selected signal
// is `aria-selected`, which consuming suites read.

/* `short`: the tab's name below the strip's `shortBelow` width, where the full names and the verb outrun one row
   (Billing's Payments is "Bills" at 390px). Its accessible name follows what is drawn. */
/* `cue`: something on this tab wants the reader without taking them there (the task page's question that arrived
   mid-typing) — a dot after the name, and these words for a screen reader. */
export type TabDef<Id extends string> = { id: Id; label: string; short?: string; cue?: string }

const CUE_SX = {
  display: 'inline-block', width: '0.5rem', height: '0.5rem', borderRadius: '50%', bgcolor: 'primary.main', ml: '0.375rem',
  verticalAlign: 'middle',
} as const
const CUED_NAME_SX = { display: 'inline-flex', alignItems: 'center', position: 'relative' } as const

/* A tab's drawn name: `label`, or `short` while the viewport is narrower than `shortBelow`. Both spans are in the
   DOM, CSS shows one, so the strip never re-renders on a resize. */
function tabName(t: { label: string; short?: string; cue?: string }, shortBelow: number | undefined): ReactNode {
  const cue = t.cue === undefined ? null : (
    <>
      <Box component="span" aria-hidden sx={CUE_SX} data-testid="tab-cue" />
      <Box component="span" sx={SR_ONLY_SX}>{`, ${t.cue}`}</Box>
    </>
  )
  if (t.short === undefined || shortBelow === undefined) {
    return cue === null ? t.label : <Box component="span" sx={CUED_NAME_SX}>{t.label}{cue}</Box>
  }
  const narrow = `@media (max-width:${shortBelow - 1}px)`
  const names = (
    <>
      <Box component="span" sx={{ [narrow]: { display: 'none' } }}>{t.label}</Box>
      <Box component="span" sx={{ display: 'none', [narrow]: { display: 'inline' } }}>{t.short}</Box>
    </>
  )
  // One row with the cue: MUI's Tab stacks its label's children in a column.
  return cue === null ? names : <Box component="span" sx={CUED_NAME_SX}>{names}{cue}</Box>
}

/* The wrapped strip. The sliding indicator cannot follow a wrapped tab, so the selected tab draws its own underline. */
/* The More button, in a tab's own type and colour (MUI `Tab`, `textColor="primary"`). */
const MORE_SX = {
  typography: 'button',
  textTransform: 'none',
  fontWeight: 500,
  lineHeight: 1.25,
  whiteSpace: 'nowrap',
  color: 'text.secondary',
  alignSelf: 'stretch',
} as const

/* The More button standing in for the selected tab it hides: the same underline the wrapped strip draws. */
const SELECTED_MORE_SX = {
  color: 'primary.main',
  boxShadow: (theme: Theme) => `inset 0 -2px 0 ${theme.palette.primary.main}`,
} as const

const WRAP_SX = {
  '& .MuiTabs-list': { flexWrap: 'wrap' },
  '& .MuiTabs-scrollButtons, & .MuiTabs-indicator': { display: 'none' },
  '& .MuiTab-root.Mui-selected': { boxShadow: (theme: Theme) => `inset 0 -2px 0 ${theme.palette.primary.main}` },
} as const

/* Wrapped at phone width, rows are what a strip costs (44px each): 6px sides fit nine Project tabs in two rows at
   390px where 14px took three. Wrapped tabs keep a 12px gap and the selected tab's underline. */
const PHONE_TAB_PX = '6px'
const PHONE_WRAP_SX = { ...WRAP_SX, '& .MuiTab-root': { px: PHONE_TAB_PX } } as const

/* Wrapped on a phone, More sits at the end of the last row, which the fit left room for: in the flow it would
   take a row of its own under the wrapped strip. */
const PHONE_MORE_SX = { position: 'absolute', right: 0, bottom: 0, height: 44, px: PHONE_TAB_PX } as const

/* Shared by the strip's tabs, the More button and their hidden width probes, so a probe measures what renders.
`minWidth: 0` drops MUI's 90px per-tab floor, which five tabs exceed at 390px. `padding` is load-bearing:
`minHeight: 36` derives from it (8 + 8 + 20px line box); MUI's `12px 16px` makes 42px content in a 36px strip,
a rogue inner scrollport (`e2e/task-detail-fit.spec.ts`). */
const TAB_SX = { minWidth: 0, minHeight: { xs: 44, sm: 36 }, padding: '8px 14px', flexShrink: 0 } as const
const MORE_GLYPH = ' ▾'
/* A fitted strip in a narrow room takes the phone's sides: the project page beside the Directory list at a 1024px
   window (~320px) fitted only General and Access before More at 14px, and Governance and Slots at 6px.
   A container query on the fit box, so the probes inside it measure what renders. */
const NARROW_FIT_PX = 480
const NARROW_FIT_SX = {
  containerType: 'inline-size',
  [`@container (max-width: ${NARROW_FIT_PX}px)`]: { '& .MuiTab-root, & [data-testid="tabs-more"]': { px: PHONE_TAB_PX } },
} as const
/* Sub-pixel widths round up in the scroller: a strip filled to the pixel would overflow by one and grow chevrons. */
const FIT_SLACK = 2

/* How many leading tabs fit `rows` rows of `width` (each tab's measured width in order, wrapping as a flex row
   does), leaving room for the More button on the last row when any must overflow: `moreWidth` is its width, or
   its width for a given count shown — More takes the name of the selected tab it hides, so reserving the widest
   name it could ever take sent a tab that fits behind it. All when they all fit; never fewer than one. */
export function fittingCount(
  widths: readonly number[], width: number, moreWidth: number | ((shown: number) => number), rows = 1,
): number {
  // Whether the first `count` tabs, then `more` px on the same row as the last of them, fit in `rows` rows.
  const fits = (count: number, more: number) => {
    let row = 1
    let used = 0
    for (const w of widths.slice(0, count)) {
      if (used > 0 && used + w > width) {
        row += 1
        used = 0
      }
      used += w
    }
    return row <= rows && used + more <= width
  }
  if (fits(widths.length, 0)) return widths.length
  const moreFor = typeof moreWidth === 'number' ? () => moreWidth : moreWidth
  let best = 1
  for (let count = 1; count < widths.length; count++) {
    if (fits(count, moreFor(count))) best = count
  }
  return best
}

/* A phone's wrapped strip with `overflowMenu` stops at two rows (88px): nine Project tabs took three at 360px even
   with 6px sides, so what a third row would hold goes behind More, drawn at the second row's end. */
const PHONE_ROWS = 2

// Past MUI's 300ms tab-scroll animation (`theme.transitions.duration.standard`).
const REVEAL_SETTLE_MS = 400

/* WHOLE TABS AT EACH EDGE, at every width: a tab the scrolled edge cuts is masked out whole (the CUT), so the strip
   never opens or ends mid-word ("hines" for Machines, "kflows" beside a chevron). The cue that more lies that way
   is MUI's chevron from `sm` up; below it, where MUI hides its scroll buttons (no `allowScrollButtonsMobile`), a
   small drawn chevron (the CUE) at that edge, over the cut's gap and the next tab's own padding — never over a
   letter: a fade there dimmed a whole tab's last letters and read as a clipped word ("NodeLin"). Each side's cue
   shows only while the strip can scroll that way. The widths and cues are CSS variables the scroll handler
   writes, so a scroll costs no render. */
const CUE_PX = 10
// The cue's own media query: the reveal reads it in JS.
const CUE_QUERY = TAP_FLOOR_DOWN.replace(/^@media\s*/, '')
const CUE_MARK_SX = {
  content: '""',
  position: 'absolute',
  top: '50%',
  zIndex: 1,
  width: 6,
  height: 6,
  borderTop: '2px solid',
  borderRight: '2px solid',
  borderColor: 'text.secondary',
  pointerEvents: 'none',
} as const
/** The CUT and the CUE for any sideways-scrolling row (`scroller`: the selector of its scrolling element, under the
 *  host the sx lands on). The cues sit on the host, positioned for them (a sticky host keeps sticky by listing its own
 *  position after this); `markEdgeCuts` writes the variables. */
export function edgeMaskSx(scroller: string) {
  return {
    [scroller]: {
      maskImage: 'linear-gradient(to right, transparent var(--tabs-cut-start, 0px), '
        + '#000 var(--tabs-cut-start, 0px), #000 calc(100% - var(--tabs-cut-end, 0px)), '
        + 'transparent calc(100% - var(--tabs-cut-end, 0px)))',
    },
    [TAP_FLOOR_DOWN]: {
      position: 'relative',
      '&::before': { ...CUE_MARK_SX, left: 3, transform: 'translateY(-50%) rotate(-135deg)', opacity: 'var(--tabs-more-start, 0)' },
      '&::after': { ...CUE_MARK_SX, right: 3, transform: 'translateY(-50%) rotate(45deg)', opacity: 'var(--tabs-more-end, 0)' },
    },
  } as const
}
const EDGE_MASK_SX = edgeMaskSx(`& .${tabsClasses.scroller}`)

/** `edgeMaskSx`'s variables on `host`: the item each scrolled edge of `scroller` cuts, and the cue on each side it
 *  can scroll toward. */
export function markEdgeCuts(host: HTMLElement, scroller: HTMLElement, items: readonly (Element | null)[]): void {
  const max = scroller.scrollWidth - scroller.clientWidth
  const start = scroller.scrollLeft > 1
  const end = scroller.scrollLeft < max - 1
  const view = scroller.getBoundingClientRect()
  const boxes = items.flatMap(b => (b ? [b.getBoundingClientRect()] : []))
  // How much of the item straddling each edge shows: that fragment is masked out.
  const cutStart = boxes.find(b => b.left < view.left - 1 && b.right > view.left + 1)
  const cutEnd = boxes.find(b => b.left < view.right - 1 && b.right > view.right + 1)
  host.style.setProperty('--tabs-cut-start', start && cutStart ? `${cutStart.right - view.left}px` : '0px')
  host.style.setProperty('--tabs-cut-end', end && cutEnd ? `${view.right - cutEnd.left}px` : '0px')
  host.style.setProperty('--tabs-more-start', start ? '1' : '0')
  host.style.setProperty('--tabs-more-end', end ? '1' : '0')
}

/** On a sticky strip's parent, so the panels beside it inherit it: how far down the scrollport the stuck strip ends
 *  (the scrollport's top padding, the strip's own `top`, its height). A panel's in-page scroll target sets its
 *  `scroll-margin-top` from it, so a jump never lands under the strip (`components/NotificationsPanel.tsx`). */
export const STICKY_TABS_HEIGHT_VAR = '--sticky-tabs-height'

/** Keeps `STICKY_TABS_HEIGHT_VAR` current on `row`'s parent while `active`: re-measured when the row resizes, the window
 *  resizes (the scrollport's padding is per breakpoint) and the root's style changes (`ACT_AS_OVERLAP_VAR` moves `top`). */
function usePublishStickyHeight(row: { current: HTMLElement | null }, active: boolean): void {
  useEffect(() => {
    const el = row.current
    const host = el?.parentElement
    if (!el || !host || !active) return undefined
    const measure = () => {
      const port = scrollportOf(el)
      const pad = port ? parseFloat(getComputedStyle(port).paddingTop || '0') : 0
      const top = parseFloat(getComputedStyle(el).top) || 0
      host.style.setProperty(STICKY_TABS_HEIGHT_VAR, `${Math.ceil(pad + top + el.getBoundingClientRect().height)}px`)
    }
    measure()
    const resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    resizes?.observe(el)
    const rootStyle = typeof MutationObserver === 'undefined' ? null : new MutationObserver(measure)
    rootStyle?.observe(document.documentElement, { attributes: true, attributeFilter: ['style'] })
    window.addEventListener('resize', measure)
    return () => {
      resizes?.disconnect()
      rootStyle?.disconnect()
      window.removeEventListener('resize', measure)
      host.style.removeProperty(STICKY_TABS_HEIGHT_VAR)
    }
  }, [row, active])
}

export function Tabs<Id extends string>({
  tabs,
  tab,
  onSelect,
  label,
  idPrefix,
  className,
  actions,
  compactActions = false,
  wrapOnPhone = false,
  wrap = false,
  overflowMenu = false,
  shortBelow,
  sticky,
}: {
  tabs: readonly TabDef<Id>[]
  tab: Id
  onSelect: (tab: Id) => void
  // The tablist's accessible name ("Node sections", "Task sections").
  label: string
  // Namespaces `${idPrefix}-tab-<id>` / `${idPrefix}-panel-<id>` so two shells never collide.
  idPrefix: string
  // The strip's page-scoped class, for per-page touch metrics.
  className?: string
  /* The page's own controls (Add, New…), right-aligned on the strip's row: the selected tab already names
     the page, so no title row sits above it to carry them. */
  actions?: ReactNode
  /* The actions never take a line of their own: they stay at the end of the strip's row at every width, on a phone
     in a one-tap-target slot. The caller passes verbs that drop their word there (`ui/CompactVerb`). A tab with no
     verb has no slot: kept empty on every tab so wrapped tabs never reflowed (Billing, a verb on three of five
     tabs), it cost each verbless tab 44px of its row for a blank — a tab more behind More — which
     outweighs panels starting at one y across tabs. */
  compactActions?: boolean
  /* At ≤480px the tabs WRAP onto a second line instead of scrolling, so every tab is visibly present. For a
     strip whose labels overflow a phone and whose tabs are all equally likely picks (Dashboard, Agents). With
     `overflowMenu` it wraps to at most two rows and More takes the rest (`PHONE_ROWS`). */
  wrapOnPhone?: boolean
  /* `wrapOnPhone` at EVERY width: for a strip too long for a desktop too, or for a side pane. */
  wrap?: boolean
  /* The tabs that fit the strip's width stay on one row and the rest go behind a "More ▾" menu, which takes
     the selected tab's name while that tab is one of them. For a long strip in a pane too narrow to show it
     (the project page in the Directory side pane), where wrapping costs a row and scrolling hides tabs. */
  overflowMenu?: boolean
  /* The viewport width (px) below which each tab with a `short` name shows it instead (`TabDef`). */
  shortBelow?: number
  /* A record page's strip: held at the top of its scrollport, so a short window (a side pane at 420px tall) keeps
     every section one click away. The value is the ground it sits on, which it paints to cover what scrolls under. */
  sticky?: 'default' | 'paper'
}) {
  // `noSsr`: read on the first render, so a phone never draws More for a frame first.
  const phoneWrap = useMediaQuery(PHONE_QUERY, { noSsr: true }) && wrapOnPhone
  // `overflowMenu`: one row, or `PHONE_ROWS` wrapped on a phone (`wrapOnPhone` above).
  const fitted = overflowMenu
  const rows = phoneWrap ? PHONE_ROWS : 1
  const [shownCount, setShownCount] = useState(tabs.length)
  // The fewest pixels the strip ever needs (`overflowMenu`): the first tab and the widest More; 0 until measured.
  const [floor, setFloor] = useState(0)
  const [moreAnchor, setMoreAnchor] = useState<HTMLElement | null>(null)
  const fitRef = useRef<HTMLDivElement | null>(null)
  const probesRef = useRef<HTMLDivElement | null>(null)
  const tabsKey = tabs.map(t => t.label).join('\u0000')
  const buttonsRef = useRef<(HTMLButtonElement | null)[]>([])
  const stripRef = useRef<HTMLDivElement | null>(null)
  // The whole row, for `sticky`: whether it is stuck at its scrollport's head (`ui/FormActionBar`'s measure).
  const rowRef = useRef<HTMLDivElement | null>(null)
  useStuck(rowRef, sticky !== undefined, 'top')
  usePublishStickyHeight(rowRef, sticky !== undefined)
  // Set by the first touch of the strip and never cleared: from then on its scroll position is the person's.
  const touchedRef = useRef(false)
  const selectedIndex = tabs.findIndex(t => t.id === tab)
  const shown = fitted ? tabs.slice(0, shownCount) : tabs
  const hidden = fitted ? tabs.slice(shownCount) : []
  // A widened strip unmounts the menu: drop its anchor, or the menu would reopen on a removed button later.
  useEffect(() => { if (hidden.length < 2) setMoreAnchor(null) }, [hidden.length])
  const hiddenSelected = hidden.find(t => t.id === tab)
  const lone = hidden.length === 1 ? hidden[0] : undefined

  /* FIT THE STRIP (`overflowMenu`): the probes are every tab plus a More button labelled with the longest name
     it can take, laid out off-screen at their natural widths; re-fit whenever the room or the labels change. */
  useLayoutEffect(() => {
    const room = fitRef.current
    const probes = probesRef.current
    if (!fitted || !room || !probes) return undefined
    const fit = () => {
      const width = (el: Element) => el.getBoundingClientRect().width
      const widths = Array.from(probes.querySelectorAll('.MuiTab-root:not([data-more-probe])'), width)
      // [More, More as each tab's name] — the order the probes render in.
      const moreWidths = Array.from(probes.querySelectorAll('[data-more-probe]'), width)
      const moreWidth = Math.max(0, ...moreWidths)
      const total = widths.reduce((sum, w) => sum + w, 0)
      setFloor(widths.length ? Math.ceil(Math.min(total, widths[0] + moreWidth)) + FIT_SLACK : 0)
      /* The More this count would draw: the one hidden tab's own name when only the last is hidden (its "<name> ▾"
         probe bounds it), the selected tab's name while it is hidden, else "More". */
      const moreFor = (shown: number) =>
        moreWidths[shown === widths.length - 1 ? shown + 1 : selectedIndex >= shown ? selectedIndex + 1 : 0] ?? moreWidth
      // Not laid out (a hidden ancestor, jsdom): nothing to fit, so every tab stays.
      setShownCount(room.clientWidth === 0 ? widths.length : fittingCount(widths, room.clientWidth - FIT_SLACK, moreFor, rows))
    }
    fit()
    // A late web font widens every label: re-fit once it lands (the probe list's resize says so too).
    let live = true
    void document.fonts?.ready.then(() => { if (live) fit() })
    if (typeof ResizeObserver === 'undefined') return () => { live = false }
    const observer = new ResizeObserver(fit)
    observer.observe(room)
    // The list, not the probe strip: that is pinned at 0×0, so only its content reports a label's growth.
    observer.observe(probes.querySelector(`.${tabsClasses.list}`) ?? probes)
    return () => {
      live = false
      observer.disconnect()
    }
  }, [fitted, rows, tabsKey, selectedIndex])

  /* CHEVRONS ONLY WHILE THE TABS OVERFLOW THE WHOLE STRIP. MUI decides from whether the last tab is in view, so once
     its end chevron has taken 40px a label that shrinks back into the full width (a count landing, a label
     shortened) still sits under it, and the chevron stays for good. Measured here against the strip's own width,
     the chevrons' room included, on every label or room change: callers need not key-remount the strip. Turned
     back on, MUI may draw its last chevron state for a frame until its observer reports (accepted). */
  const [overflows, setOverflows] = useState(false)
  useLayoutEffect(() => {
    const strip = stripRef.current
    if (fitted || !strip) return undefined
    const measure = () => {
      const buttons = buttonsRef.current.slice(0, tabs.length)
      const tabsWidth = buttons.reduce((sum, b) => sum + (b?.getBoundingClientRect().width ?? 0), 0)
      // A pixel of slack: sub-pixel widths that fit would otherwise read as overflow and grow chevrons.
      setOverflows(tabsWidth > strip.getBoundingClientRect().width + 1)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(strip)
    buttonsRef.current.slice(0, tabs.length).forEach(b => { if (b) observer.observe(b) })
    return () => observer.disconnect()
  }, [fitted, tabsKey, tabs.length])

  /* KEEP THE SELECTED TAB IN VIEW while the strip settles. MUI scrolls it in once, measured BEFORE its scroll
     chevrons appear (they narrow the scroller by ~80px) and before late fonts widen the labels, so a later tab
     opened by `?tab=` at 390px ended up behind the end chevron. Re-reveal on every scroller or tab-list resize
     until the person touches the strip (a chevron press shows the start chevron, a resize that must not snap
     back). A selection they make MUI scrolls in itself, and it still gets the one settle pass below. */
  useEffect(() => {
    const strip = stripRef.current
    const scroller = strip?.querySelector<HTMLElement>(`.${tabsClasses.scroller}`)
    const list = strip?.querySelector<HTMLElement>(`.${tabsClasses.list}`)
    if (!strip || !scroller || selectedIndex < 0) return undefined
    const reveal = () => {
      const button = buttonsRef.current[selectedIndex]
      if (!button) return
      /* Clear of the edge cue too, but only below `sm`, where the cue exists: MUI's own reveal stops at the
         scroller's edge, under the cue. A first or last tab clamps at the end, where that side's cue is off.
         From `sm` up the chevrons mark the edge and the margin would only over-scroll. Read per pass: the
         viewport may cross `sm` between them. */
      const margin = window.matchMedia(CUE_QUERY).matches ? CUE_PX : 0
      const view = scroller.getBoundingClientRect()
      const box = button.getBoundingClientRect()
      if (box.left < view.left + margin) scroller.scrollLeft -= view.left + margin - box.left
      else if (box.right > view.right - margin) scroller.scrollLeft += box.right - (view.right - margin)
    }
    /* Again once MUI's own animated scroll (`transitions.duration.standard`, 300ms) has run: it targets the
       pre-chevron width and would overwrite an instant reveal — a data-gated last tab (Diagnostics) arrives
       after first paint, so its scroll is the animated kind. A tab the person picked half under the cue
       gets this one settle pass too, so it ends fully visible. */
    let settle: ReturnType<typeof setTimeout> | undefined = setTimeout(reveal, REVEAL_SETTLE_MS)
    if (touchedRef.current || typeof ResizeObserver === 'undefined') return () => clearTimeout(settle)
    const observer = new ResizeObserver(() => {
      reveal()
      clearTimeout(settle)
      settle = setTimeout(reveal, REVEAL_SETTLE_MS)
    })
    observer.observe(scroller)
    if (list) observer.observe(list)
    const events = ['pointerdown', 'wheel', 'touchstart', 'keydown'] as const
    const stop = () => {
      clearTimeout(settle)
      observer.disconnect()
      events.forEach(name => strip.removeEventListener(name, release))
    }
    function release() {
      touchedRef.current = true
      stop()
    }
    events.forEach(name => strip.addEventListener(name, release, { passive: true }))
    return stop
  }, [selectedIndex])

  // The edge mask's variables (`markEdgeCuts`).
  useEffect(() => {
    const strip = stripRef.current
    const scroller = strip?.querySelector<HTMLElement>(`.${tabsClasses.scroller}`)
    if (!strip || !scroller) return undefined
    const mask = () => markEdgeCuts(strip, scroller, buttonsRef.current)
    mask()
    scroller.addEventListener('scroll', mask, { passive: true })
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(mask)
    observer?.observe(scroller)
    const list = strip.querySelector<HTMLElement>(`.${tabsClasses.list}`)
    if (list) observer?.observe(list)
    return () => {
      scroller.removeEventListener('scroll', mask)
      observer?.disconnect()
    }
  }, [])

  // Selection and focus move together (APG automatic activation).
  function moveTo(index: number) {
    // Over the tabs on the strip only: those behind More are the menu's.
    const wrapped = (index + shown.length) % shown.length
    /* Focus moves unconditionally; `onSelect` fires only on a change. Some shells navigate on it and drop
    the query string (`components/RouteTabLayout.tsx`, `pages/audit/AuditPage.tsx`), so a no-op select would
    strip Sessions' `?task=`. */
    if (shown[wrapped].id !== tab) onSelect(shown[wrapped].id)
    buttonsRef.current[wrapped]?.focus()
  }

  // A `switch`, not a lookup object: `moves[e.key]` would resolve inherited keys ("constructor").
  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>, index: number) {
    let next: number
    switch (e.key) {
      case 'ArrowRight': next = index + 1; break
      case 'ArrowLeft': next = index - 1; break
      case 'Home': next = 0; break
      case 'End': next = shown.length - 1; break
      default: return
    }
    e.preventDefault()
    // Keep it from MUI's non-wrapping handler, which would move focus a second time.
    e.stopPropagation()
    moveTo(next)
  }

  const strip = (
    <MuiTabs
      ref={stripRef}
      className={className}
      // `false` while the selected tab is behind More: MUI then marks none, and the More button carries it.
      value={hiddenSelected ? false : tab}
      onChange={(_e, next: Id) => onSelect(next)}
      aria-label={label}
      // `standard` pins the scroller to `overflow-x: hidden`, making tabs past a phone's width unreachable by
      // touch. `scrollable` makes it swipeable; `scrollButtons="auto"` shows chevrons only on overflow, and
      // only from `sm` up — below it MUI hides them (`display: none`) and the edge cue (`EDGE_MASK_SX`), a smaller
      // chevron drawn over the cut tab's gap, marks the overflow instead: a chevron at 375px is that cue, not MUI's.
      variant="scrollable"
      /* None beside More: the fit keeps the strip from overflowing, and MUI keeps chevrons it drew before the
         first fit (80px gone) since dropping tabs does not re-measure it. None while every tab fits (`overflows`). */
      scrollButtons={fitted || !overflows ? false : 'auto'}
      selectionFollowsFocus
      /* 36px, not MUI's 48px: chrome height above the first row is a measured criterion (AC5). Concrete, not
      `0`: the scroller is a stretch flex item, and a zero floor collapses the strip to 0px (caught by
      `task-detail-fit.spec.ts`). 44px below `sm` is the tap floor
      (AC11/AC4(f)). */
      sx={[
        /* Beside More, only its tabs' width, so the button follows the last of them; wrapped, the row's width. Never
           grown into the room after More: the fit leaves it only when the next tab is wider (Environment needs 96px of
           the project pane's 65px at 1024px), and tabs grown to the pixel overflowed the scroller by a sub-pixel,
           which the edge mask cut a whole tab for. */
        {
          minHeight: { xs: 44, sm: 36 },
          flex: fitted && !phoneWrap ? '0 1 auto' : '1 1 auto',
          minWidth: 0,
          /* A disabled chevron is only transparent in MUI (40px kept), so a strip scrolled to its start opened 40px
             in from the page content under it. Gone instead; it takes its width back once there is somewhere to go, eased
             so the strip slides rather than jumps on the first scroll (the 40px move itself is the accepted cost). */
          [`& .${tabsClasses.scrollButtons}`]: { transition: 'width 150ms ease' },
          [`& .${tabsClasses.scrollButtons}.Mui-disabled`]: { width: 0 },
        },
        EDGE_MASK_SX,
        wrap ? WRAP_SX : wrapOnPhone && { [PHONE]: PHONE_WRAP_SX },
      ]}
    >
      {shown.map((t, i) => (
        <MuiTab
          key={t.id}
          value={t.id}
          label={tabName(t, shortBelow)}
          id={`${idPrefix}-tab-${t.id}`}
          aria-controls={`${idPrefix}-panel-${t.id}`}
          ref={(el: HTMLElement | null) => { buttonsRef.current[i] = el as HTMLButtonElement | null }}
          onKeyDown={(e: KeyboardEvent<HTMLDivElement>) => handleKeyDown(e, i)}
          sx={TAB_SX}
        />
      ))}
    </MuiTabs>
  )

  return (
    /* A wrapped strip wraps this row too: beside a detail pane its actions drop under the tabs rather than squeeze
       them one per line. So does one with More, at its `floor`: a pane too narrow for one tab, More and the actions
       (Directory's list beside an editor) puts the actions under the tabs instead of squashing the tab. */
    <Box
      ref={rowRef}
      data-sticky={sticky ? '' : undefined}
      sx={{
        ...(sticky && {
          // Under the act-as bar (`components/ActAsBar.tsx`, 3), which also sticks at the head and must stay readable.
          position: 'sticky', zIndex: 2, bgcolor: (theme: Theme) => groundOfSticky(theme, sticky),
          // Below the act-as bar's reach, while it is drawn: it would cover the strip's top ~10px.
          top: `var(${ACT_AS_OVERLAP_VAR}, 0px)`,
          /* Sticky offsets count from the scrollport's padding edge (`.app-main`'s 1.75rem, a phone's 0.75rem, the side
             pane's 4px), so content scrolled through that band showed above the stuck strip. Its ground reaches up
             over the band, clipped by the scrollport, only while stuck: in the flow it would cover the page title
             above it. */
          [`&[${FORM_BAR_STUCK_ATTR}]`]: {
            boxShadow: (theme: Theme) => `0 -1.75rem 0 0 ${groundOfSticky(theme, sticky)}`,
          },
        }),
        borderBottom: 1,
        borderColor: 'divider',
        display: 'flex',
        flexWrap: wrap || fitted ? 'wrap' : compactActions ? 'nowrap' : { xs: 'wrap', sm: 'nowrap' },
        alignItems: 'center',
        columnGap: 1,
      }}
    >
      {fitted ? (
        // Never past the row itself: a pane narrower than the floor clips inside the strip rather than overflow.
        <Box ref={fitRef} sx={{ flex: `1 1 ${floor}px`, minWidth: `min(${floor}px, 100%)`, display: 'flex', alignItems: 'center', position: 'relative', ...NARROW_FIT_SX }}>
          {strip}
          {/* A button beside the tablist, never inside it: a tablist owns only tabs, and a tab-like item in it
              joins MUI's roving tab stop, so Shift+Tab from it skipped the whole strip. */}
          {/* ONE hidden tab is no menu: its own name, no caret, one click to it. It carries the tab's id,
              so its panel stays labelled; never wider than the "<name> ▾" probe the fit reserved. */}
          {lone && (
            <ButtonBase
              id={`${idPrefix}-tab-${lone.id}`}
              aria-current={hiddenSelected ? 'true' : undefined}
              data-testid="tabs-more"
              className="tabs-more"
              onClick={() => { if (lone.id !== tab) onSelect(lone.id) }}
              sx={[TAB_SX, MORE_SX, hiddenSelected ? SELECTED_MORE_SX : {}, phoneWrap && PHONE_MORE_SX]}
            >
              {lone.label}
            </ButtonBase>
          )}
          {hidden.length > 1 && (
            <ButtonBase
              aria-haspopup="menu"
              aria-expanded={moreAnchor !== null}
              aria-label={hiddenSelected ? `More sections (${hiddenSelected.label} selected)` : 'More sections'}
              data-testid="tabs-more"
              // The phone density's hook (`ui/listStyles` `PHONE_TAB_STRIP_SX`).
              className="tabs-more"
              onClick={(e: MouseEvent<HTMLElement>) => setMoreAnchor(e.currentTarget)}
              sx={[TAB_SX, MORE_SX, hiddenSelected ? SELECTED_MORE_SX : {}, phoneWrap && PHONE_MORE_SX]}
            >
              {`${hiddenSelected?.label ?? 'More'}${MORE_GLYPH}`}
            </ButtonBase>
          )}
          {/* Only while two or more tabs are hidden: an empty `role="menu"` would sit in the DOM. `keepMounted`: each
              item carries its tab's id, so a hidden tab's panel stays labelled. */}
          {hidden.length > 1 && (
          <Menu anchorEl={moreAnchor} open={moreAnchor !== null} onClose={() => setMoreAnchor(null)} keepMounted>
            {hidden.map(t => (
              <MenuItem
                key={t.id}
                id={`${idPrefix}-tab-${t.id}`}
                selected={t.id === tab}
                onClick={() => {
                  setMoreAnchor(null)
                  if (t.id !== tab) onSelect(t.id)
                }}
              >
                {tabName(t, undefined)}
              </MenuItem>
            ))}
          </Menu>
          )}
          {/* Off-screen, at natural widths: a tab needs a strip around it to render. */}
          <MuiTabs ref={probesRef} value={false} aria-hidden="true" tabIndex={-1}
            sx={{
              position: 'absolute', top: 0, left: 0, width: 0, height: 0, minHeight: 0, overflow: 'hidden', visibility: 'hidden',
              ...(phoneWrap && { '& .MuiTab-root': { px: PHONE_TAB_PX } }),
            }}>
            {tabs.map(t => <MuiTab key={t.id} value={t.id} tabIndex={-1} label={tabName(t, shortBelow)} sx={TAB_SX} />)}
            {/* More as each name it can take; the widest is the room it needs. */}
            {['More', ...tabs.map(t => t.label)].map((name, i) => (
              <MuiTab key={`more-${i}`} value={`\u0000more-${i}`} tabIndex={-1} label={`${name}${MORE_GLYPH}`} sx={TAB_SX}
                data-more-probe="" />
            ))}
          </MuiTabs>
        </Box>
      ) : strip}
      {(actions || compactActions) && (
        /* Below `sm` the controls take their own line under the tabs, so a phone keeps every tab label —
           unless they are compact (`compactActions`), which stay on the strip's row. */
        <Box
          data-testid="tab-strip-actions"
          sx={[{
            flex: { xs: '1 0 100%', sm: '0 0 auto' },
            /* Never wider than the row: on a line of its own in a narrow pane (Directory's list beside an editor)
               the actions wrap one per line rather than run past the pane and clip a label. */
            maxWidth: '100%',
            // Right-aligned on a line of its own too (a wrapped strip's narrow pane).
            ml: 'auto',
            display: 'flex',
            flexWrap: 'wrap',
            justifyContent: 'flex-end',
            alignItems: 'center',
            gap: 1,
            pb: 0.5,
            /* An empty slot takes no room at any width: nothing passed, or a layout's slot (`RouteTabLayout`) with
               nothing portalled into it. Compact too (`compactActions`). */
            '&:empty, &:has(> span:empty)': { display: 'none' },
          }, compactActions && {
            // Responsive like the rule it overrides: a plain value loses to its `xs` media query.
            flex: { xs: '0 0 auto' },
            /* Level with the first row of a wrapped strip, a verb's slot one tap target wide. */
            [PHONE]: { alignSelf: 'flex-start', minWidth: 44, minHeight: 44, pb: 0 },
          }]}
        >
          {actions}
        </Box>
      )}
    </Box>
  )
}

// Every panel stays mounted; inactive ones carry `hidden`. Switching tabs then costs zero refetches,
// and in-progress typing (a gate answer, an unsaved edit) survives a trip to another tab.
export function TabPanel<Id extends string>({
  id,
  current,
  idPrefix,
  label,
  className,
  children,
}: {
  id: Id
  current: Id
  idPrefix: string
  /** Names the panel directly, for a host that draws no strip — there is no tab to be labelled by. */
  label?: string
  className?: string
  children: ReactNode
}) {
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={label === undefined ? `${idPrefix}-tab-${id}` : undefined}
      aria-label={label}
      hidden={current !== id}
      className={className}
    >
      {children}
    </div>
  )
}
