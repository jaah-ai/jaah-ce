/* THE CONSOLE'S ONE LIST — every route's rows, drawn once. A route supplies facts (`./types`) and owns no
 * pixels; every number is `./geometry`, every band `ui/listStyles.ts`.
 * A FLEX ROW, NOT A TABLE: one greedy title and fixed slots that drop as width shrinks — one row model from
 * 320px to 1440px, no card transform.
 * IT CARRIES TABLE SEMANTICS ANYWAY, and the WHOLE OWNERSHIP CHAIN: `table` owns `rowgroup`s, a `rowgroup`
 * owns `row`s, a `row` owns CELLS AND NOTHING ELSE, or browse-mode readers skip it. So everything a row
 * draws sits in a cell (disc, disclosure, ⋯), every detail region is its own one-cell ROW, and
 * the heading+rowgroup bucket is `role="presentation"`.
 */
import { Fragment, memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { DragEvent, JSX, KeyboardEvent, MouseEvent, PointerEvent as ReactPointerEvent, ReactNode, SyntheticEvent } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Link from '@mui/material/Link'
import Skeleton from '@mui/material/Skeleton'
import Tooltip from '@mui/material/Tooltip'
import { Link as RouterLink } from 'react-router-dom'
import type { Theme } from '@mui/material/styles'
import { PHONE, ROW_MARKED_SX, SR_ONLY_SX, TAP_FLOOR_DOWN } from '../listStyles'
import { PRESS_SWAP_SX, PressSwap } from '../pressSwap'
import { ProgressRing } from '../ProgressRing'
import type { Tone } from '../StatusChip'
import { TONE_COLOR } from '../statusTone'
import { disclosureGlyph } from '../../components/ExpandToggleGlyph'
import { RowActionsOverflow } from '../../components/tasks/rowActions'
import { useOpenDetailPath } from '../../components/ListDetail'
import { isOpenPath } from '../../lib/listDetail'
import {
  ACTIONS_SLOT,
  CHILD_INDENT,
  CHILD_INDENT_PHONE,
  DISCLOSURE_SLOT,
  DISCLOSURE_TOUCH_MIN,
  GATE_SX,
  NODE,
  NODE_PHONE,
  RAIL_X,
  RAIL_X_PHONE,
  ROW_H,
  ROW_H_PHONE,
  ROW_PAD_L,
  ROW_PAD_L_PHONE,
  monoFamily,
  nodeLeft,
  rowPadLeft,
  type SlotGate,
} from './geometry'
import { NO_DROPS, useFittedDrops } from './fit'
import type { FeedColumn, FeedIndicator, FeedMarker, FeedNote, FeedRowSpec, FeedSection, FeedSlot, SortDir } from './types'

/* `rgba(var(--mui-palette-*-mainChannel) / a)`: `alpha()` cannot parse `var(...)`. `neutral` uses the
   theme's translucent grey. */
const TONE_TINT: Record<Tone, string> = {
  neutral: 'action.selected',
  info: 'rgba(var(--mui-palette-info-mainChannel) / 0.18)',
  success: 'rgba(var(--mui-palette-success-mainChannel) / 0.18)',
  warning: 'rgba(var(--mui-palette-warning-mainChannel) / 0.18)',
  error: 'rgba(var(--mui-palette-error-mainChannel) / 0.18)',
}

const CONTAINER_SX = { pb: 2, [PHONE]: { pb: 1 } } as const

/* Sticky, backed by the page ground so rows slide UNDER it. 22px: a divider with a word on it. */
const HEAD_SX = {
  position: 'sticky',
  top: 0,
  zIndex: 1,
  display: 'flex',
  alignItems: 'center',
  gap: 0.75,
  height: 22,
  bgcolor: 'background.default',
  fontSize: '0.6875rem',
  fontWeight: 700,
  letterSpacing: '0.08em',
  textTransform: 'uppercase',
  color: 'text.disabled',
  [PHONE]: { height: 18, fontSize: '0.625rem' },
} as const

/* `fontFamily`, NOT the `font` shorthand: spread after `HEAD_SX`, `font: inherit` reset the heading's
   treatment. NO 44px FLOOR: a full-width heading far exceeds the 44x44 area even at 18px. */
const HEAD_BUTTON_SX = {
  width: '100%',
  border: 0,
  m: 0,
  p: 0,
  background: 'none',
  cursor: 'pointer',
  textAlign: 'left',
  fontFamily: 'inherit',
  color: 'inherit',
  '&:hover': { color: 'text.secondary' },
  [PHONE]: { minHeight: 18 },
} as const

const COUNT_SX = { fontWeight: 400, letterSpacing: '0.04em', opacity: 0.7, flex: '0 0 auto' } as const
/* The count's small print, in sentence case beside the capitals: it truncates first, before the label does. The
   dot keeps "4" and "2 failed" from reading as "42 failed". */
const COUNT_NOTE_SX = {
  ...COUNT_SX,
  '&::before': { content: '"· "' },
  textTransform: 'none',
  letterSpacing: 0,
  flex: '0 1 auto',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const

/** Two pixels under its parent; both stay under the 24px an e2e case caps headings at. */
const HEAD_H_NESTED = 20
const HEAD_H_NESTED_PHONE = 16

/* Truncates like a row title; otherwise a long name widens the page. */
const HEAD_LABEL_SX = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  minWidth: 0,
} as const

/* ── WHAT MAKES A NESTED HEADING READ AS NESTED ────────────────────────────────────────────────────
   Indent alone reads FLAT (measured on `/worktrees`), so the ladder spends INDENT, CASE and STRENGTH — none
   costing height. ONLY AT `depth > 0`: a depth-0 heading renders exactly as before.
   HEIGHT IS A BUDGET (`HEAD_SX`: 22px, 18px on phone; e2e caps 24px), and type may not go below 10px at
   320px. */
const headLevel = (depth: number, nested: boolean) =>
  depth === 0
    /* Air above a top-level heading, only in a NESTED outline, so groups do not run together. */
    ? (nested ? { mt: 1 } : null)
    : {
        /* INDENT DERIVED FROM THE RAIL, NOT `CHILD_INDENT`: the 10px caret box put the label RIGHT of the row
           titles it heads. At `RAIL_X - 5` the caret's centre lands on the rail (14.5 → 23.5 → 30). Deeper
           levels step by `CHILD_INDENT`. */
        pl: `${RAIL_X - 5 + (depth - 1) * CHILD_INDENT}px`,
        /* Lower-case against the parent's caps: the strongest level signal without height, greyscale-safe;
           `textContent` unchanged for oracles. Hostnames also fit better (uppercase is ~12% wider). */
        textTransform: 'none' as const,
        letterSpacing: '0.01em',
        fontWeight: 600,
        /* A step DOWN in strength; placed after `HEAD_BUTTON_SX` (whose `color: 'inherit'` would win). NOT
           `text.disabled` (2.7:1, below AA). Quiet means SMALLER, never FAINTER. */
        color: 'text.secondary',
        '&:hover': { color: 'text.primary' },
        height: HEAD_H_NESTED,
        [PHONE]: {
          pl: `${RAIL_X_PHONE - 5 + (depth - 1) * CHILD_INDENT_PHONE}px`,
          height: HEAD_H_NESTED_PHONE,
          // Overrides `HEAD_BUTTON_SX`'s phone `minHeight` of 18.
          minHeight: HEAD_H_NESTED_PHONE,
        },
      }
const BODY_SX = { position: 'relative' } as const

/* The thread, inset half a row at each end so it stops at the first and last node. */
const RAIL_SX = {
  position: 'absolute',
  left: `${RAIL_X}px`,
  top: `${ROW_H / 2}px`,
  bottom: `${ROW_H / 2}px`,
  width: '1px',
  bgcolor: 'divider',
  pointerEvents: 'none',
  [PHONE]: { left: `${RAIL_X_PHONE}px`, top: `${ROW_H_PHONE / 2}px`, bottom: `${ROW_H_PHONE / 2}px` },
} as const

const ROW_SX = {
  position: 'relative',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  minHeight: ROW_H,
  pr: 0.5,
  borderRadius: 1,
  /* Gap halves to 3px: with five slots, 6px gaps cost the title too much. */
  [PHONE]: { minHeight: ROW_H_PHONE, gap: 0.5 },
} as const

const ROW_HOVER_SX = { '&:hover': { bgcolor: 'action.hover' } } as const

/* `FeedRowSpec.muted` — controls are pulled back OUT of the dimming. */
const ROW_MUTED_SX = {
  opacity: 0.62,
  '& button, & a, & [role="button"]': { opacity: 1 },
} as const
const ROW_PRESS_SX = { cursor: 'pointer' } as const

/* ── THE DRAG'S FOUR STATES, all four a WASH (`FeedDrag`) — a slot mark would drop at narrow widths.
   `cursor: grab` IS THE WHOLE DISCOVERY AFFORDANCE, so it shows before any drag. */
const ROW_GRAB_SX = { cursor: 'grab' } as const
/* The row in flight: pulled back only. NOT `pointerEvents: none` — the gesture resolves from the element
   under the pointer, and dragging back onto the source must read as "put it back". No `dropKey`, so not a
   target. */
const ROW_LIFTED_SX = { opacity: 0.45, cursor: 'grabbing' } as const
/* WILL take the drag, shown during any drag. Faint: on a 200-row tree every accepting folder lights. */
const ROW_DROP_ACCEPT_SX = {
  bgcolor: 'rgba(var(--mui-palette-success-mainChannel) / 0.10)',
} as const
/* Under the pointer: an outline, reading as a destination rather than a selection. */
const ROW_DROP_OVER_SX = {
  bgcolor: 'rgba(var(--mui-palette-success-mainChannel) / 0.22)',
  outline: '2px solid',
  outlineColor: 'success.main',
  outlineOffset: '-2px',
} as const
/* REFUSES this drop: cursor, faint error wash, and `dropReason` on `title` — never silence. A row that is
   not a target at all passes no `drop` (tinting every non-folder red is noise). */
const ROW_DROP_REJECT_SX = {
  cursor: 'not-allowed',
  bgcolor: 'rgba(var(--mui-palette-error-mainChannel) / 0.10)',
  '&:hover': { bgcolor: 'rgba(var(--mui-palette-error-mainChannel) / 0.14)' },
} as const

/* NO TEXT SELECTION WHILE A DRAG IS UP, on every row: press-and-move is also text selection, which would
   outlive the drop and disable `rowPress`. For the drag's DURATION only (names/ids are copyable at rest).
   `hooks/usePointerDrag.ts` clears the selection the press already made. */
const ROW_NO_SELECT_SX = { userSelect: 'none' } as const

/* ONE WASH (`ROW_MARKED_SX`, ui/listStyles.ts) SERVES `deepLinked` AND `current`; only `current` spells
   `aria-current`. */
const ROW_LIVE_SX = {
  bgcolor: 'rgba(var(--mui-palette-info-mainChannel) / 0.07)',
  '&:hover': { bgcolor: 'rgba(var(--mui-palette-info-mainChannel) / 0.13)' },
} as const
/* Weight, because colour alone is not a signal everyone receives. */
const LIVE_TITLE_SX = { fontWeight: 600 } as const

/* `color` is the tone (`currentColor` in the pulse). The disc MASKS THE RAIL where it crosses. */
const NODE_SX = {
  position: 'absolute',
  left: `${RAIL_X - NODE / 2}px`,
  width: NODE,
  height: NODE,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  borderRadius: '50%',
  fontSize: '0.75rem',
  fontWeight: 700,
  lineHeight: 1,
  pointerEvents: 'none',
  /* The disc shrinks, the glyph does not: at 10px » and ▸ were indistinguishable. `left` comes from
     `nodeLeft`. */
  [PHONE]: { width: NODE_PHONE, height: NODE_PHONE },
} as const

/* A ring, not a solid pulse (it would swallow the glyph). A SECOND KEYFRAME NAME for the phone — one name
   cannot hold two definitions. A 2.4x halo would spill over neighbouring 24px rows. */
const LIVE_NODE_SX = {
  '@keyframes feedPulse': {
    '0%': { transform: 'scale(1)', opacity: 0.55 },
    '70%': { transform: 'scale(2.4)', opacity: 0 },
    '100%': { transform: 'scale(2.4)', opacity: 0 },
  },
  '@keyframes feedPulsePhone': {
    '0%': { transform: 'scale(1)', opacity: 0.55 },
    '70%': { transform: 'scale(1.8)', opacity: 0 },
    '100%': { transform: 'scale(1.8)', opacity: 0 },
  },
  '&::after': {
    content: '""',
    position: 'absolute',
    inset: 0,
    borderRadius: '50%',
    border: '1px solid currentColor',
    opacity: 0,
    animation: 'feedPulse 1.8s ease-out infinite',
    [PHONE]: { animation: 'feedPulsePhone 1.8s ease-out infinite' },
  },
  '@media (prefers-reduced-motion: reduce)': { '&::after': { animation: 'none' } },
} as const

/* A `<button>` reset to the disc. `pointerEvents: auto` reverses `NODE_SX`. Focus uses `outline`, not the
   deep-link `boxShadow`, so both can show at once. */
const NODE_BUTTON_SX = {
  pointerEvents: 'auto',
  p: 0,
  border: 0,
  cursor: 'pointer',
  fontFamily: 'inherit',
  '&:hover': { filter: 'brightness(1.25)' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: '1px' },
} as const

const UNCHECKED_NODE_SX = { boxShadow: 'inset 0 0 0 1.5px var(--mui-palette-text-disabled)' } as const

const MARKED_NODE_SX = {
  boxShadow:
    '0 0 0 1px var(--mui-palette-background-default), 0 0 0 3px var(--mui-palette-primary-main)',
} as const

/* The rollup arc (`FeedIndicator.ring`). An `<svg>`, not a pseudo-element: `::after` is the pulse and
   Playwright cannot select pseudo-elements. The track always draws (a 5% arc alone is a speck). */
const NODE_RING_SX = {
  position: 'absolute',
  inset: 0,
  width: '100%',
  height: '100%',
  pointerEvents: 'none',
  overflow: 'visible',
} as const

/* The link covers only the naming text: a `<button>` inside an `<a>` is invalid HTML. */
const LINK_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  display: 'flex',
  alignItems: 'baseline',
  gap: 0.75,
  textDecoration: 'none',
  color: 'inherit',
  '&:hover .feed-title': { textDecoration: 'underline' },
} as const

/* THE TITLE TAKES EVERY PIXEL THE CLUSTER LEAVES AND ELLIPSISES — it never shrinks to fit. */
const TITLE_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  fontSize: '0.8125rem',
  lineHeight: 1.3,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  [PHONE]: { fontSize: '0.75rem' },
} as const

/* `FeedRowSpec.titleLines: 2` — wraps at word boundaries (a word wider than the box still breaks), then clamps. */
const TITLE_TWO_LINES_SX = {
  whiteSpace: 'normal',
  overflowWrap: 'break-word',
  display: '-webkit-box',
  WebkitLineClamp: 2,
  WebkitBoxOrient: 'vertical',
} as const

/* `FeedRowSpec.titleLines: 'all'` — wraps and never clamps, for a title that IS the record (a history entry).
   4px above and below: a wrapped title otherwise touches the next row. One line stays inside the row's
   own floor (`ROW_H`/`ROW_H_PHONE`), so only a wrapped row grows. */
const TITLE_ALL_LINES_SX = { whiteSpace: 'normal', overflowWrap: 'anywhere', py: '4px' } as const

const leadSx = (width: number, gate: SlotGate = 'id', align: 'left' | 'right' = 'right', drops: ReadonlySet<SlotGate> = NO_DROPS) => (theme: Theme) =>
  ({
    ...GATE_SX[gate],
    ...(drops.has(gate) ? DROPPED_SX : null),
    flex: '0 0 auto',
    width,
    fontFamily: monoFamily(theme),
    fontSize: '0.6875rem',
    color: 'text.disabled',
    textAlign: align,
    /* Truncates, or a long lead overspills and the title's ellipsis never engages. */
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  }) as const

/* A fixed cell, centred: glyphs differ in advance (`▶` vs `?`), and a glyph-wide marker shifted every slot after it
   off its header. `minWidth`, so a wider glyph still shows whole. */
const MARKER_SX = { flex: '0 0 auto', minWidth: '1.25em', textAlign: 'center', fontSize: '0.625rem', color: 'text.disabled' } as const
/* `FeedMarker.hug`: the name's own gap, not a column's centring cell. */
const MARKER_HUG_SX = { ml: 0.5 } as const
/* `FeedMarker.word`: a hair after the glyph, in the marker's own size and tone. */
const MARKER_WORD_SX = { ml: '2px', fontWeight: 600 } as const

/* `FeedRowSpec.titleWrapAt`: `TITLE_TWO_LINES_SX` inside the band(s), nested so several must all match. */
function titleWrapSx(bands: string | readonly string[]): object {
  return (typeof bands === 'string' ? [bands] : bands).reduceRight<object>(
    (inner, band) => ({ [band]: inner }),
    TITLE_TWO_LINES_SX,
  )
}

/* ── THE TOUCH LONG-PRESS (`FeedRowSpec.onLongPress`) ──────────────────────────────────────
   A hover-less device has no hover-revealed tick, so holding a row picks it. 500ms is the platform's own
   long-press beat; 10px of travel is a scroll or a drag beginning, never a press. A browser that takes the
   gesture for a pan fires `pointercancel`, which cancels it too, so scrolling a list never picks. The click the
   lift then delivers is swallowed (capture phase), or the title link would navigate away from the pick. The
   system's own long-press sheet (a link's preview, text selection) is withheld on such a row: it would open over
   the pick. */
const LONG_PRESS_MS = 500
const LONG_PRESS_SLOP_PX = 10
/* How long after the lift a pick still swallows a click: a lift's click lands within one gesture, so a later
   real click is never eaten. */
const LONG_PRESS_CLICK_WINDOW_MS = 400
const ROW_LONG_PRESS_SX = {
  '@media (hover: none)': { WebkitTouchCallout: 'none', userSelect: 'none' },
} as const

/* NO WIDTH GATE: phone users must see and open parents directly. The tap-floor pullback pair
   (`TAP_FLOOR_DOWN` / `[PHONE]` last) is `./geometry`'s `DISCLOSURE_TOUCH_MIN` block; `minHeight: 0` is its
   height axis. */
const DISCLOSURE_SX = {
  flex: '0 0 auto',
  width: DISCLOSURE_SLOT,
  minWidth: DISCLOSURE_SLOT,
  height: 22,
  p: 0,
  borderRadius: 1,
  fontSize: '0.875rem',
  lineHeight: 1,
  color: 'text.secondary',
  [TAP_FLOOR_DOWN]: { minWidth: DISCLOSURE_TOUCH_MIN },
  [PHONE]: { width: 18, minWidth: 18, height: 22, minHeight: 0, fontSize: '0.8125rem' },
} as const

/* Reserves the disclosure's WIDTH and MUST NEVER set the row's height (it once made rows 44px). */
const DISCLOSURE_SPACER_SX = {
  flex: '0 0 auto',
  width: DISCLOSURE_SLOT,
  minWidth: DISCLOSURE_SLOT,
  pointerEvents: 'none',
  height: 'auto',
  minHeight: 0,
  [TAP_FLOOR_DOWN]: { minWidth: DISCLOSURE_TOUCH_MIN },
  [PHONE]: { width: 18, minWidth: 18, height: 'auto', minHeight: 0 },
} as const

/* The ⋯: narrower than 44px (a 30px row's scarce axis is width); a full 24x24 on phones (WCAG 2.5.8)
   because its verbs have no second home. */
const ACTIONS_SX = {
  flex: '0 0 auto',
  display: 'inline-flex',
  width: ACTIONS_SLOT,
  minWidth: ACTIONS_SLOT,
  height: 24,
  justifyContent: 'center',
  fontSize: '1rem',
  lineHeight: 1,
  borderRadius: 1,
  [PHONE]: { width: 24, minWidth: 24, height: 24, minHeight: 0, fontSize: '0.875rem' },
} as const

/* Reserved, so a verbless row cannot shift every column above it. */
const ACTIONS_SPACER_SX = {
  flex: '0 0 auto',
  width: ACTIONS_SLOT,
  [PHONE]: { width: 24 },
} as const

const EMPTY_SX = { py: 6, textAlign: 'center', fontSize: '0.8125rem', color: 'text.disabled' } as const

const SKELETON_ROW_SX = {
  ml: `${RAIL_X + ROW_PAD_L}px`,
  my: '9px',
  [PHONE]: { ml: `${RAIL_X_PHONE + ROW_PAD_L_PHONE}px`, my: '6px' },
} as const

/* `FeedRowSpec.notes` on the disc's tap tooltip: one line per note, as on the native hover; an unbroken token (an
   address, a provider's ARN) wraps rather than overflow a phone. */
const NOTE_TIP_SLOTS = { tooltip: { sx: { whiteSpace: 'pre-line', overflowWrap: 'anywhere' } } } as const

/* A row's hover sentence and its notes (`FeedNote`), one per line, each said once: a note that opens one of the
   sentence's lines (a task's wait reason is its state sentence too) is not repeated. Whole lines, so a short note
   ("Default") is never swallowed by a name that merely contains it. */
function withNotes(sentence: string, notes: readonly FeedNote[]): string {
  const lines = sentence.split('\n')
  const said = (text: string) => lines.some((line) => line === text || line.startsWith(`${text}. `))
  return [sentence, ...notes.map((note) => note.text).filter((text) => !said(text))].join('\n')
}

/* The tone a row's notes give its disc: an error beats a warning beats the indicator's own. */
function notedTone(tone: Tone, notes: readonly FeedNote[]): Tone {
  if (notes.some((note) => note.tone === 'error')) return 'error'
  return notes.some((note) => note.tone === 'warning') ? 'warning' : tone
}

/* The one flexible column; `minWidth: 0` lets the title's ellipsis engage. */
const CELL_GROW_SX = { flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'center' } as const

/* `FeedRowSpec.titleAction`. The link stops growing so the control HUGS the text rather than sitting at the
   far edge of the cell; the title still ellipsises first because the action never shrinks. Hidden (not
   removed) at rest so hover never reflows the row; kept visible while its own state is non-idle (a copy
   flash must not vanish when the pointer leaves). A hover-less device never draws it. */
const LINK_HUG_SX = { flex: '0 1 auto' } as const
const TITLE_ACTION_SX = {
  flex: '0 0 auto',
  display: 'inline-flex',
  alignItems: 'center',
  ml: 0.5,
  opacity: 0,
  transition: 'opacity 120ms ease-out',
  '&:has(:focus-visible), &:has([data-copy-state="copied"]), &:has([data-copy-state="failed"])': { opacity: 1 },
  '@media (hover: none)': { display: 'none' },
} as const
/* `FeedRowSpec.titleAside`: hugs the name like `titleAction`, but always drawn. */
const TITLE_ASIDE_SX = { flex: '0 0 auto', display: 'inline-flex', alignItems: 'center', ml: 1, fontSize: '0.6875rem' } as const
const ROW_TITLE_ACTION_REVEAL_SX = { '&:hover .feed-title-action': { opacity: 1 } } as const

/* THE DISC'S CELL DRAWS NO BOX: the disc is absolutely positioned (no flex `gap`), and a boxed wrapper would
   push every title 8px right. `display: contents` gives the cell to the a11y tree only. */
const DISC_CELL_SX = { display: 'contents' } as const

/* These cells replace their control as the flex item, so cluster arithmetic is unchanged; the control keeps
   its own `sx` (the tap-floor pullback must land on its class). */
const CONTROL_CELL_SX = { flex: '0 0 auto', display: 'inline-flex' } as const
/* `FeedRowSpec.titleMaxWidth`'s spare width. */
const PUSH_END_SX = { ml: 'auto' } as const

const ROW_CONTROL_SELECTOR = 'a, button, input, textarea, select, label, [role="button"]'

/* WHAT A DRAG MAY NOT BEGIN ON — `ROW_CONTROL_SELECTOR` MINUS THE LINK: the title `<a>` is the natural grab
   point; a sub-threshold press is still a click, and a drag's click is swallowed by `hooks/usePointerDrag.ts`. */
const DRAG_BLOCKING_SELECTOR = 'button, input, textarea, select, label, [role="button"]'

/* The drop-target attribute's single home: stamped from `FeedDrag.dropKey`, read by `feedDropKeyAt`. */
export const FEED_DROP_TARGET_ATTR = 'data-drop-target'

/**
 * THE DESTINATION UNDER THE POINTER, or `null` — what a route hands `usePointerDrag`'s `resolve`. `closest`,
 * since the hit element is usually a descendant; anything unmarked (margin, detail region, heading, a refusing
 * row) yields `null`, which is the abort gesture: nothing is written.
 */
export function feedDropKeyAt(element: Element | null): string | null {
  if (!(element instanceof Element)) return null
  return element.closest(`[${FEED_DROP_TARGET_ATTR}]`)?.getAttribute(FEED_DROP_TARGET_ATTR) ?? null
}

const alignOf = (align: FeedSlot['align']) =>
  align === 'left' ? 'left' : align === 'center' ? 'center' : 'right'

/* A gate the list's own width dropped (`./fit`): on top of its window rule, never instead of it. */
const DROPPED_SX = { display: 'none' } as const

function slotSx(slot: FeedSlot, drops: ReadonlySet<SlotGate>) {
  const gate: Record<string, object> = slot.gate ? GATE_SX[slot.gate] : {}
  const dropped = slot.gate !== undefined && drops.has(slot.gate)
  return (theme: Theme) => withNarrowWidth(slot.narrowWidth, withPhoneWidth(slot.phoneWidth, {
      ...gate,
      flex: '0 0 auto',
      width: slot.width,
      fontSize: slot.fontSize ?? '0.6875rem',
      ...(slot.mono ? { fontFamily: monoFamily(theme) } : null),
      color: slot.dim ? 'text.disabled' : 'text.secondary',
      textAlign: alignOf(slot.align),
      overflow: 'hidden',
      textOverflow: 'ellipsis',
      whiteSpace: 'nowrap',
      ...(slot.lines === 2 ? { ...TITLE_TWO_LINES_SX, lineHeight: 1.3 } : null),
      /* After `lines`, whose clamp sets its own `display`. */
      ...(dropped ? DROPPED_SX : null),
      /* The tap-floor pullback per `./geometry`'s `DISCLOSURE_TOUCH_MIN` block; `FeedSlot.compact` says when. MERGED
         into the gate's own rule for the same query (a phone gate, `peopleSelectIdle`): a second key would replace it. */
      ...(slot.compact
        ? {
            [TAP_FLOOR_DOWN]: {
              ...gate[TAP_FLOOR_DOWN],
              '& .MuiIconButton-root, & .MuiButtonBase-root': {
                minHeight: 0,
                minWidth: 0,
                width: 24,
                height: 24,
              },
            },
            [PHONE]: {
              ...gate[PHONE],
              '& .MuiIconButton-root, & .MuiButtonBase-root': {
                minHeight: 0,
                minWidth: 0,
                width: 24,
                height: 24,
              },
            },
          }
        : null),
    }))
}

/* `FeedSlot.phoneWidth`, MERGED into whatever phone rule the gate or `compact` already wrote: a second `[PHONE]`
 * key in the literal would replace theirs. */
function withPhoneWidth<T extends Record<string, unknown>>(phoneWidth: number | undefined, sx: T) {
  if (phoneWidth === undefined) return sx
  return { ...sx, [PHONE]: { ...(sx[PHONE] as object | undefined), width: phoneWidth } }
}

/* `FeedSlot.narrowWidth`, merged the same way (a gate may already write that query). */
function withNarrowWidth<T extends Record<string, unknown>>(narrow: FeedSlot['narrowWidth'], sx: T) {
  if (narrow === undefined) return sx
  return { ...sx, [narrow.query]: { ...(sx[narrow.query] as object | undefined), width: narrow.width } }
}

/* KEEPING `detail` IN THE DOM WITHOUT KEEPING IT ON SCREEN (`FeedRowSpec.detailKeepMounted`).
 * `hidden="until-found"`: find-in-page can reach it and the browser reveals it itself (`beforematch`); plain
 * `hidden` is `display: none`, unfindable. SET ON THE NODE: React serialises the JSX prop as `hidden=""`.
 * Chromium 102+ / Safari 18+; elsewhere it degrades to plain `hidden`, never a visible leak.
 * `useLayoutEffect` so a shut region never flashes. */
function KeepMountedDetail({ shut, children }: { shut: boolean; children: ReactNode }): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    if (shut) el.setAttribute('hidden', 'until-found')
    else el.removeAttribute('hidden')
  }, [shut])
  return (
    <Box ref={ref} role="row" data-testid="feed-detail">
      <Box role="cell">{children}</Box>
    </Box>
  )
}

function FeedDisc({
  row,
  indicator,
  depth,
  named,
}: {
  row: FeedRowSpec
  indicator: FeedIndicator
  depth: number
  named: boolean
}): JSX.Element {
  /* `row.busy` DOES NOT REACH THE DISC: a busy row's ⋯ is disabled (no pointer events, so `rowPress` checks
     `!row.busy`), and the disc stays the only live target. */
  const pressable = Boolean(indicator.onPress)
  // Pointed at or focused, a pressable disc shows what it does (`../pressSwap.tsx`).
  const swap = pressable && indicator.pressGlyph !== undefined
  const ring = indicator.ring
  /* THE ROW'S NOTES RIDE THE DISC (`FeedNote`): its hover, and its tap on a touch device. An error note turns the
     disc red, the cue the words no longer give at a glance. */
  const notes = row.notes ?? []
  const tone = notedTone(indicator.tone, notes)
  const pressTitle = withNotes(indicator.title ?? indicator.pressLabel ?? indicator.label, notes)
  const stateTitle = withNotes(indicator.title ?? indicator.label, notes)
  /* A tap opens the hover sentence too (`tapTitle`, or any note): the tooltip then owns `title` (`describeChild`
     keeps it on the disc while closed), so hover and tap show one sentence. The disc takes the pointer for it
     (`NODE_SX` gives an inert disc none, so its `title` never showed).
     CONTROLLED, OPENED BY THE CLICK A TAP DELIVERS: MUI's own touch path opens only on a disabled child, so
     `enterTouchDelay` never fired on a phone. Its `leaveTouchDelay` still closes it. */
  const tapTip = !pressable && ((indicator.tapTitle && indicator.title) || notes.length > 0) ? stateTitle : undefined
  const [tipOpen, setTipOpen] = useState(false)
  const tappedRef = useRef(false)
  // The tap is the disc's alone: on a row with a body press or a long-press it would also open the record.
  const toggleTip = (event: MouseEvent<HTMLElement>) => {
    event.stopPropagation()
    tappedRef.current = !tipOpen
    setTipOpen(!tipOpen)
  }
  /* A tapped tip ignores the `mouseleave` a touch browser fires as the popper lands over the disc: only the touch
     leave timer or a second tap closes it. */
  const closeTip = (event: Event | SyntheticEvent) => {
    if (tappedRef.current && event.type !== 'touchend') return
    tappedRef.current = false
    setTipOpen(false)
  }
  const discSx = [
    NODE_SX,
    nodeLeft(depth),
    { color: TONE_COLOR[tone], bgcolor: TONE_TINT[tone] },
    indicator.live ? LIVE_NODE_SX : null,
    row.deepLinked || row.current ? MARKED_NODE_SX : null,
    pressable ? NODE_BUTTON_SX : null,
    swap ? PRESS_SWAP_SX : null,
    // An unticked checkbox disc draws its edge, so it reads as a box to tick rather than a blank state.
    indicator.checked === false ? UNCHECKED_NODE_SX : null,
    tapTip ? { pointerEvents: 'auto' as const } : null,
  ]

  const discData: Record<string, string> = {}
  for (const [key, value] of Object.entries(indicator.data ?? {})) {
    if (value !== undefined) discData[`data-${key}`] = String(value)
  }

  const discBody = (
    <>
      <Box component="span" aria-hidden="true">
        {swap ? <PressSwap rest={indicator.glyph} press={{ glyph: indicator.pressGlyph }} /> : indicator.glyph}
      </Box>
      {ring && <ProgressRing pct={ring.pct} variant="feed" testId="feed-node-ring" sx={NODE_RING_SX} />}
    </>
  )

  /* A PRESSABLE disc is announced by its VERB (`pressLabel`), not its state — a checkbox disc by its item. */
  const discBox = pressable ? (
    <Box
      component="button"
      type="button"
      data-testid="feed-node"
      data-tone={tone}
      {...(indicator.checked === undefined ? {} : { role: 'checkbox', 'aria-checked': indicator.checked })}
      aria-label={indicator.pressLabel ?? indicator.label}
      title={pressTitle}
      onClick={indicator.onPress}
      sx={discSx}
      {...discData}
    >
      {discBody}
    </Box>
  ) : (
    <Box
      component="span"
      data-testid="feed-node"
      data-tone={tone}
      {...(named
        ? { 'aria-hidden': true as const }
        : { role: 'img' as const, 'aria-label': indicator.label })}
      {...(tapTip ? { onClick: toggleTip } : { title: stateTitle })}
      sx={discSx}
      {...discData}
    >
      {discBody}
    </Box>
  )

  /* Puts the disc on the ownership chain (see `DISC_CELL_SX`); a pressable disc keeps its `button` role. */
  return (
    <Box role="cell" sx={DISC_CELL_SX}>
      {tapTip ? (
        <Tooltip
          title={tapTip}
          open={tipOpen}
          onOpen={() => setTipOpen(true)}
          onClose={closeTip}
          slotProps={NOTE_TIP_SLOTS}
          describeChild
          leaveTouchDelay={6000}
        >
          {discBox}
        </Tooltip>
      ) : discBox}
    </Box>
  )
}

/** A trailing cell: a `FeedRowSpec.slots` entry, or the row's `control`. */
function slotCell(slot: FeedSlot, drops: ReadonlySet<SlotGate>): JSX.Element {
  const slotData: Record<string, string> = {}
  for (const [key, value] of Object.entries(slot.data ?? {})) {
    if (value !== undefined) slotData[`data-${key}`] = String(value)
  }
  return (
    <Box
      component="span"
      key={slot.key}
      role="cell"
      data-testid={slot.testId}
      title={slot.title}
      sx={slotSx(slot, drops)}
      {...slotData}
    >
      {slot.node}
    </Box>
  )
}

/** One row and everything it owns (detail, children).
 *  MEMOIZED ON THE SPEC'S IDENTITY: a poll or an SSE push re-renders the list, and redrawing 75 rows
 *  each time stalled typing in the create row. A route that hands back the SAME spec object for an unchanged row
 *  (`components/tasks/listViews/ActivityFeedView.tsx` caches them) skips that row's render entirely; a route
 *  that rebuilds every spec loses nothing but the saving. */
const FeedRow = memo(function FeedRow({ row: spec, depth, actionsColumn, drops }: { row: FeedRowSpec; depth: number; actionsColumn: boolean; drops: ReadonlySet<SlotGate> }): JSX.Element {
  /* THE EDITOR OPEN BESIDE THIS LIST (components/ListDetail.tsx) is what the surface shows now: its row is
     `current`, so every list marks the open record without passing anything. */
  const openInPane = isOpenPath(useOpenDetailPath(), spec.href)
  const row = openInPane && !spec.current ? { ...spec, current: true } : spec
  const { indicator } = row
  /* No disclosure = nothing to fold, so anything nested is shown. `'spacer'` is the same case. */
  const expanded = !row.disclosure || row.disclosure === 'spacer' || row.disclosure.expanded
  /* Announcing the disc and markers derives from `row.ariaLabel` (see that field). */
  const named = Boolean(row.ariaLabel)

  /* No indicator: nothing on the rail, and nothing in the a11y tree (a container is named by its title). */
  const disc = indicator ? <FeedDisc row={row} indicator={indicator} depth={depth} named={named} /> : null

  const disclosure =
    row.disclosure && row.disclosure !== 'spacer' ? (
      <Box role="cell" sx={CONTROL_CELL_SX}>
      <IconButton
        data-testid="feed-disclosure"
        aria-label={`${row.disclosure.expanded ? 'Collapse' : 'Expand'} ${row.disclosure.label ?? 'children'} of ${row.titleText}`}
        aria-expanded={row.disclosure.expanded}
        /* The glyph alone does not say what it folds: the tooltip names it (the label carries the row too). */
        title={`${row.disclosure.expanded ? 'Collapse' : 'Expand'} ${row.disclosure.label ?? 'children'}`}
        /* BUSY SHOWS, IT DOES NOT DISABLE: leaving the tab order mid-press strands a changed mind. */
        onClick={row.disclosure.onToggle}
        sx={DISCLOSURE_SX}
      >
        <Box component="span" aria-hidden="true">
          {row.disclosure.busy ? '…' : disclosureGlyph(row.disclosure.expanded)}
        </Box>
      </IconButton>
      </Box>
    ) : row.disclosure === 'spacer' ? (
      <Box aria-hidden="true" sx={DISCLOSURE_SPACER_SX} />
    ) : null

  /* THE LEAD SITS RIGHT AFTER THE DISC, before the disclosure — its own cell, outside the title link, so the id
     reads as the row's label beside its status rather than floating past the caret. */
  const lead = row.lead ? (
    <Box
      component="span"
      data-testid="feed-lead"
      title={row.lead.title ?? row.lead.text}
      /* A named row already says its id (`ariaLabel`), so the lead is hidden there like the disc and markers. */
      {...(named ? { 'aria-hidden': true as const } : { role: 'cell' as const })}
      sx={leadSx(row.lead.width ?? 40, row.lead.gate, row.lead.align, drops)}
    >
      {row.lead.text}
    </Box>
  ) : null

  const titleNode = (
    <Box
      component="span"
      className="feed-title"
      data-testid="feed-title"
      sx={[
        TITLE_SX,
        row.titleLines === 2 ? TITLE_TWO_LINES_SX : row.titleLines === 'all' ? TITLE_ALL_LINES_SX : null,
        row.titleWrapAt && row.titleLines === undefined ? titleWrapSx(row.titleWrapAt) : null,
        indicator?.live ? LIVE_TITLE_SX : null,
      ]}
      /* During a refused drag the row's `dropReason` wins over the truncation `title` (the inner title would
         otherwise win over the name). Restored when the drag ends. */
      title={row.drag?.drop === 'reject' ? undefined : withNotes(row.titleText, row.notes ?? [])}
    >
      {row.title}
    </Box>
  )

  /* The `<a>` keeps its `link` role, so the cell WRAPS it. The name goes on the `<a>` where there is one,
     else on the CELL (an `aria-label` on a role-less `<div>` is ignored). */
  const titleAction = row.titleAction ? (
    <Box component="span" className="feed-title-action" sx={TITLE_ACTION_SX}>
      {row.titleAction}
    </Box>
  ) : null
  const titleAside = row.titleAside ? (
    <Box component="span" sx={TITLE_ASIDE_SX}>
      {row.titleAside}
    </Box>
  ) : null
  /* A marker's own markup, column or hugged. Hugged it sits INSIDE the naming cell, so it cannot be a `cell` too:
     unnamed, it is an `img` carrying its label. */
  const markerNode = (marker: FeedMarker) => {
    const markerData: Record<string, string> = {}
    for (const [key, value] of Object.entries(marker.data ?? {})) {
      if (value !== undefined) markerData[`data-${key}`] = String(value)
    }
    return (
      <Box
        component="span"
        key={marker.key}
        data-testid={marker.testId}
        title={marker.title ?? marker.label}
        {...(named
          ? { 'aria-hidden': true as const }
          : { role: marker.hug ? ('img' as const) : ('cell' as const), 'aria-label': marker.label })}
        sx={[MARKER_SX, marker.hug ? MARKER_HUG_SX : null, marker.tone ? { color: TONE_COLOR[marker.tone] } : null]}
        {...markerData}
      >
        {marker.glyph}
        {marker.word && (
          <Box
            component="span"
            data-testid={marker.testId ? `${marker.testId}-word` : undefined}
            sx={[MARKER_WORD_SX, marker.word.gate ? GATE_SX[marker.word.gate] : null]}
          >
            {marker.word.text}
          </Box>
        )}
      </Box>
    )
  }
  /* `FeedMarker.hug`: a column marker lands at the GROWING title cell's far end, which on a phone row
     (no slots beside the name) is the row's far edge. A hugged one follows the name, which stops growing as it
     does for `titleAction`. */
  const huggedMarkers = row.markers?.filter((marker) => marker.hug) ?? []
  const columnMarkers = row.markers?.filter((marker) => !marker.hug) ?? []
  const hugged = huggedMarkers.length > 0 ? huggedMarkers.map(markerNode) : null
  const linkSx = titleAction || titleAside || hugged ? [LINK_SX, LINK_HUG_SX] : LINK_SX
  const growSx = row.titleMaxWidth ? [CELL_GROW_SX, { maxWidth: row.titleMaxWidth }] : CELL_GROW_SX
  /* THE NOTES, READ ALOUD: a named row's link label replaces its contents and the disc is `aria-hidden`, so the
     hover alone would leave a screen reader without them. A visually hidden span describes the name; it draws
     nothing, so the row stays one line. */
  const notesId = useId()
  const notesText = row.notes?.map((note) => note.text).join('. ')
  const notesNode = notesText ? <Box component="span" id={notesId} data-feed-notes="" sx={SR_ONLY_SX}>{notesText}</Box> : null
  const describedBy = notesText ? notesId : undefined
  const naming = row.href ? (
    <Box role="cell" data-feed-name="" sx={growSx}>
      <Link
        component={RouterLink}
        to={row.href}
        aria-label={row.ariaLabel}
        aria-describedby={describedBy}
        draggable={row.drag ? false : undefined}
        sx={linkSx}
      >
        {titleNode}
      </Link>
      {hugged}
      {titleAside}
      {titleAction}
      {notesNode}
    </Box>
  ) : (
    <Box role="cell" data-feed-name="" aria-label={row.ariaLabel} aria-describedby={describedBy} sx={growSx}>
      <Box sx={linkSx}>
        {titleNode}
      </Box>
      {hugged}
      {titleAside}
      {titleAction}
      {notesNode}
    </Box>
  )

  const onRowClick = row.onRowClick
  /* THREE GUARDS, EACH A REGRESSION THAT HAPPENED.
     · A control inside the row owns its press — matched by SELECTOR, so future controls are covered.
     · The event must come from INSIDE this row's DOM (portalled popups like the ⋯ sheet re-fired the row).
     · A live text selection is not a click. */
  const rowPress =
    onRowClick && !row.busy
      ? (event: MouseEvent<HTMLElement>) => {
          const target = event.target as HTMLElement
          if (!event.currentTarget.contains(target)) return
          if (target.closest(ROW_CONTROL_SELECTOR)) return
          if (!window.getSelection()?.isCollapsed) return
          onRowClick()
        }
      : undefined

  /* A pressable row is a real control (keyboard-reachable). Space is preventDefault-ed (it would scroll). */
  const rowKeyDown = rowPress
    ? (event: KeyboardEvent<HTMLElement>) => {
        if (event.key !== 'Enter' && event.key !== ' ') return
        if (event.target !== event.currentTarget) return
        event.preventDefault()
        onRowClick?.()
      }
    : undefined

  /* ── THE DRAG (`FeedDrag`) ───────────────────────────────────────────────────────────────────────
     Attachments, no state. `onPointerDown` is the ONLY event reported; the gesture resolves the pointer's
     position from `data-drop-target` (`FEED_DROP_TARGET_ATTR`) — see `hooks/usePointerDrag.ts`'s header.
     `data-drop` is what oracles read (jsdom computes no colours).
     `draggable={false}` IS LOAD-BEARING: the title link is a native drag source whose drag would cancel the
     pointer stream. */
  const drag = row.drag
  const dragUp = Boolean(drag?.dragging)
  const onLift = drag?.onLift
  const liftPress = onLift
    ? (event: ReactPointerEvent<HTMLElement>) => {
        const target = event.target as HTMLElement
        if (target.closest(DRAG_BLOCKING_SELECTOR)) return
        onLift(event)
      }
    : undefined

  /* ── THE TOUCH LONG-PRESS (`LONG_PRESS_MS`'s block). Refs, not state: arming a press must not re-render. ── */
  const pressRef = useRef<{ timer: number; x: number; y: number } | null>(null)
  const swallowClickRef = useRef(false)
  const cancelPress = () => {
    if (pressRef.current) window.clearTimeout(pressRef.current.timer)
    pressRef.current = null
  }
  useEffect(() => () => {
    if (pressRef.current) window.clearTimeout(pressRef.current.timer)
  }, [])
  const onLongPress = row.onLongPress
  const pressDown = onLongPress
    ? (event: ReactPointerEvent<HTMLElement>) => {
        swallowClickRef.current = false
        cancelPress()
        if (event.pointerType !== 'touch' || event.isPrimary === false) return
        // A control in the row owns its press (the ⋯, the tick, the disc); the title link is row body.
        if ((event.target as HTMLElement).closest(DRAG_BLOCKING_SELECTOR)) return
        const { clientX: x, clientY: y } = event
        const timer = window.setTimeout(() => {
          pressRef.current = null
          swallowClickRef.current = true
          onLongPress()
        }, LONG_PRESS_MS)
        pressRef.current = { timer, x, y }
      }
    : undefined
  const pressMove = onLongPress
    ? (event: ReactPointerEvent<HTMLElement>) => {
        const press = pressRef.current
        if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) > LONG_PRESS_SLOP_PX) cancelPress()
      }
    : undefined
  const pressUp = onLongPress
    ? () => {
        cancelPress()
        if (swallowClickRef.current) {
          window.setTimeout(() => { swallowClickRef.current = false }, LONG_PRESS_CLICK_WINDOW_MS)
        }
      }
    : undefined
  const pointerDown =
    liftPress && pressDown
      ? (event: ReactPointerEvent<HTMLElement>) => { liftPress(event); pressDown(event) }
      : liftPress ?? pressDown

  /* `testid` and `drop-target` ARE REFUSED: the spread lands after `data-testid="feed-row"` and the
     gesture's `data-drop-target`, and would silently replace them. */
  const data: Record<string, string> = {}
  // `drop` too: `data-drop` is what every drag oracle reads.
  const refused = new Set(['testid', 'drop', FEED_DROP_TARGET_ATTR.slice('data-'.length)])
  for (const [key, value] of Object.entries(row.data ?? {})) {
    if (refused.has(key.toLowerCase())) continue
    if (value !== undefined) data[`data-${key}`] = String(value)
  }

  return (
    <>
      <Box
        role="row"
        id={row.id}
        data-testid="feed-row"
        data-depth={depth}
        data-drop={drag?.drop}
        {...(drag?.dropKey === undefined ? {} : { [FEED_DROP_TARGET_ATTR]: drag.dropKey })}
        draggable={drag ? false : row.dragOut ? true : undefined}
        onDragStart={
          !drag && row.dragOut
            ? (event: DragEvent<HTMLElement>) => {
                event.dataTransfer.setData(row.dragOut!.type, row.dragOut!.data)
                event.dataTransfer.effectAllowed = 'move'
              }
            : undefined
        }
        title={drag?.drop === 'reject' ? drag.dropReason : undefined}
        onPointerDown={pointerDown}
        onPointerMove={pressMove}
        onPointerUp={pressUp}
        /* A pan after the press fired ends here, not in `pressUp`: the swallow still expires, so a later keyboard
           Enter on the row's link is never eaten. */
        onPointerCancel={pressUp}
        /* The pick's lift delivers a click: swallowed before the title link or `rowPress` hear it. */
        onClickCapture={
          onLongPress
            ? (event: MouseEvent<HTMLElement>) => {
                if (!swallowClickRef.current) return
                swallowClickRef.current = false
                event.preventDefault()
                event.stopPropagation()
              }
            : undefined
        }
        /* The system's long-press sheet would open over the pick (Android fires it at the same beat). */
        onContextMenu={
          onLongPress
            ? (event: MouseEvent<HTMLElement>) => {
                if (pressRef.current || swallowClickRef.current) event.preventDefault()
              }
            : undefined
        }
  /* A focusable control owes a name: `rowClickLabel` (what Enter does); `ariaLabel` names the RECORD. */
        aria-label={rowPress ? row.rowClickLabel : undefined}
        aria-current={row.current ? 'true' : undefined}
        tabIndex={rowPress ? 0 : undefined}
        onClick={rowPress}
        onKeyDown={rowKeyDown}
        sx={[
          ROW_SX,
          rowPadLeft(depth),
          ROW_HOVER_SX,
          row.titleAction ? ROW_TITLE_ACTION_REVEAL_SX : null,
          row.muted ? ROW_MUTED_SX : null,
          indicator?.live ? ROW_LIVE_SX : null,
          /* LAST, so a live-and-marked row reads as marked. */
          row.deepLinked || row.current ? ROW_MARKED_SX : null,
          rowPress ? ROW_PRESS_SX : null,
          /* After every other wash: mid-drag, the drag state is what the row is read for. */
          drag?.onLift && !drag.lifted ? ROW_GRAB_SX : null,
          drag?.drop === 'accept' ? ROW_DROP_ACCEPT_SX : null,
          drag?.drop === 'over' ? ROW_DROP_OVER_SX : null,
          drag?.drop === 'reject' ? ROW_DROP_REJECT_SX : null,
          drag?.lifted ? ROW_LIFTED_SX : null,
          dragUp ? ROW_NO_SELECT_SX : null,
          onLongPress ? ROW_LONG_PRESS_SX : null,
        ]}
        {...data}
      >
        {disc}
        {lead}
        {disclosure}
        {naming}
        {columnMarkers.map(markerNode)}
        {row.slots?.map((slot) => slotCell(slot, drops))}
        {/* A capped title leaves spare width; it goes before the ⋯, which keeps the row's right edge. */}
        {row.actions && row.actions.length > 0 ? (
          <Box role="cell" sx={[CONTROL_CELL_SX, row.titleMaxWidth ? PUSH_END_SX : null]}>
            <RowActionsOverflow name={row.titleText} busy={Boolean(row.busy)} items={row.actions} sx={ACTIONS_SX} />
          </Box>
        ) : actionsColumn && !row.control ? (
          <Box aria-hidden="true" sx={[ACTIONS_SPACER_SX, row.titleMaxWidth ? PUSH_END_SX : null]} />
        ) : null}
        {row.control && (row.titleMaxWidth && !(row.actions && row.actions.length > 0)
          ? <Box sx={[CONTROL_CELL_SX, PUSH_END_SX]}>{slotCell(row.control, drops)}</Box>
          : slotCell(row.control, drops))}
      </Box>
      {/* Children and detail follow the disclosure; with NO disclosure children still render. The region is a
          one-cell ROW; `detailKeepMounted` keeps it findable (`KeepMountedDetail`). */}
      {row.detailKeepMounted ? (
        <KeepMountedDetail shut={!expanded}>{row.detail}</KeepMountedDetail>
      ) : (
        expanded && row.detail && (
          <Box role="row" data-testid="feed-detail">
            <Box role="cell">{row.detail}</Box>
          </Box>
        )
      )}
      {expanded &&
        row.children?.map((child) => <FeedRow key={child.key} row={child} depth={depth + 1} actionsColumn={actionsColumn} drops={drops} />)}
    </>
  )
})

/* ── THE COLUMN HEADER ROW (`columns`) ──────────────────────────────────────────────────────────────
   THE ROW'S OWN GEOMETRY, NOT A TABLE HEAD: the same rail inset, gap and right pad as `ROW_SX`, a blank
   reserving the lead and the disclosure, the title cell growing, each slot header at its slot's width and gate,
   and the ⋯ spacer — so every label sits over its values and drops off at the same width they do.
   THE SECTION HEADING'S VOICE, A STEP STRONGER: small caps-tracked type, but `text.secondary` (`text.disabled`
   is 2.7:1, too faint for a control) and a hairline under it, so it reads as a key to the rows rather than one
   more bucket. 24px tall at every width: the floor WCAG 2.5.8 sets a target (a header press is a full-height
   button), and still 6px under a row. */
const COLUMNS_ROW_SX = {
  display: 'flex',
  alignItems: 'stretch',
  gap: 1,
  minHeight: 24,
  pr: 0.5,
  borderBottom: '1px solid',
  borderColor: 'divider',
  [PHONE]: { gap: 0.5 },
} as const

const COLUMN_CELL_SX = {
  display: 'flex',
  alignItems: 'center',
  minWidth: 0,
  fontSize: '0.6875rem',
  fontWeight: 600,
  letterSpacing: '0.06em',
  textTransform: 'uppercase',
  lineHeight: 1,
  color: 'text.secondary',
  whiteSpace: 'nowrap',
  /* 10px is the floor at 320px (`headLevel`'s budget note). */
  [PHONE]: { fontSize: '0.625rem', letterSpacing: '0.04em' },
} as const

const COLUMN_TITLE_SX = { flex: '1 1 auto' } as const

/* Fills its cell, so the whole header box is the target; a reset `<button>` keeping the cell's type. The arrow
   hangs after the label, OUTSIDE its box, in the gap toward the next column: the label's own edge stays on the
   column's edge whether or not it is the sorted one, and a slot priced for its values never ellipsises its word
   ("GRO…") because it is the sorted one. Always after, never before a right-aligned label: there it would sit
   between two headers and read as the left neighbour's. After a right-aligned label the arrow (~10px with its
   margin) would fill the whole gap and touch the next header ("MERGE▼AGE"), so the sorted label steps in by half
   of it (`COLUMN_ARROW_ROOM_SX`): the overhang splits across both gaps and neither neighbour is touched. It never
   shrinks for it, spilling into its left gap instead. */
const COLUMN_BUTTON_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: '3px',
  width: '100%',
  height: '100%',
  minWidth: 0,
  m: 0,
  p: 0,
  border: 0,
  borderRadius: 0.5,
  background: 'none',
  cursor: 'pointer',
  font: 'inherit',
  letterSpacing: 'inherit',
  textTransform: 'inherit',
  color: 'inherit',
  transition: 'color 120ms ease-out',
  '&:hover': { color: 'text.primary' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: '-2px', color: 'text.primary' },
} as const

const COLUMN_ACTIVE_SX = { color: 'text.primary' } as const

const COLUMN_LABEL_SX = { overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 } as const

/* The label's box, unclipped, so the arrow can hang outside it. */
const COLUMN_LABEL_BOX_SX = { position: 'relative', display: 'flex', minWidth: 0, maxWidth: '100%' } as const

/* Half the arrow's room (glyph plus margin), reserved after a sorted right-aligned label. */
const COLUMN_ARROW_ROOM_SX = { mr: '5px', flexShrink: 0, maxWidth: 'none' } as const

/* Smaller than the label: a mark beside a word, not a second word. */
const COLUMN_ARROW_SX = {
  position: 'absolute',
  top: '50%',
  transform: 'translateY(-50%)',
  fontSize: '0.5625rem',
  lineHeight: 1,
  pointerEvents: 'none',
  left: '100%',
  ml: '2px',
} as const

const justifyOf = (align: FeedSlot['align']) =>
  align === 'left' ? 'flex-start' : align === 'center' ? 'center' : 'flex-end'

/** One header cell: a `columnheader`, carrying `aria-sort` when it is the sorted column; a sort button when the
 *  column names a `sortKey` and the list takes `onSort`. */
function FeedColumnCell({
  column,
  align,
  sx,
  sort,
  onSort,
}: {
  column: FeedColumn
  align: FeedSlot['align']
  sx: object | ((theme: Theme) => object)
  sort?: { key: string | null; dir: SortDir }
  onSort?: (sortKey: string, firstDir?: SortDir) => void
}): JSX.Element {
  const sortKey = column.sortKey
  const active = sortKey !== undefined && sort?.key === sortKey
  const dir = active ? sort!.dir : null
  const hover = column.ariaLabel ?? (typeof column.label === 'string' ? column.label : undefined)
  const body = (
    <Box component="span" sx={[COLUMN_LABEL_BOX_SX, dir && align !== 'left' ? COLUMN_ARROW_ROOM_SX : null]}>
      <Box component="span" sx={COLUMN_LABEL_SX}>{column.label}</Box>
      {dir && (
        <Box
          component="span"
          aria-hidden="true"
          data-testid="feed-column-arrow"
          sx={COLUMN_ARROW_SX}
        >
          {dir === 'asc' ? '▲' : '▼'}
        </Box>
      )}
    </Box>
  )
  return (
    <Box
      role="columnheader"
      data-testid="feed-column"
      data-column={column.key}
      aria-sort={dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : undefined}
      aria-label={sortKey && onSort ? undefined : column.ariaLabel}
      title={hover}
      sx={[COLUMN_CELL_SX, { justifyContent: justifyOf(align) }, active ? COLUMN_ACTIVE_SX : null, sx]}
    >
      {sortKey && onSort ? (
        <Box
          component="button"
          type="button"
          aria-label={column.ariaLabel}
          onClick={() => onSort(sortKey, column.firstDir)}
          sx={[COLUMN_BUTTON_SX, { justifyContent: justifyOf(align) }]}
        >
          {body}
        </Box>
      ) : body}
    </Box>
  )
}

/** The column header row, laid out off `template` (the list's first row) so it mirrors the rows' geometry. */
function FeedColumnsRow({
  template,
  columns,
  sort,
  onSort,
  nested = false,
  actionsColumn,
  drops,
}: {
  template: FeedRowSpec
  columns: { title: FeedColumn; slots?: FeedColumn[] }
  sort?: { key: string | null; dir: SortDir }
  onSort?: (sortKey: string, firstDir?: SortDir) => void
  nested?: boolean
  actionsColumn: boolean
  drops: ReadonlySet<SlotGate>
}): JSX.Element {
  const byKey = new Map((columns.slots ?? []).map((column) => [column.key, column]))
  /* IN A NESTED OUTLINE the title header starts flush left, on the top-level headings' caret: inset to the rows'
     titles it sat right of the outline it heads. The title cell grows, so the slot headers stay over
     their slots; a capped title (`titleMaxWidth`) does not, so it keeps the rows' inset. */
  const pad = nested && !template.titleMaxWidth ? null : rowPadLeft(template.depth ?? 0)
  return (
    <Box role="rowgroup">
      <Box role="row" data-testid="feed-columns" sx={[COLUMNS_ROW_SX, pad]}>
        {/* The disc is absolutely placed in the rail inset, so it needs no blank; the lead and the disclosure
            are flex items and do. */}
        {template.lead && <Box aria-hidden="true" sx={leadSx(template.lead.width ?? 40, template.lead.gate, template.lead.align, drops)} />}
        {template.disclosure && <Box aria-hidden="true" sx={DISCLOSURE_SPACER_SX} />}
        <FeedColumnCell column={columns.title} align="left" sx={template.titleMaxWidth ? { ...COLUMN_TITLE_SX, maxWidth: template.titleMaxWidth } : COLUMN_TITLE_SX} sort={sort} onSort={onSort} />
        {/* Capped, the title no longer absorbs the markers' width: an invisible copy of each keeps the headers over
            their slots. */}
        {template.titleMaxWidth && template.markers?.filter((marker) => !marker.hug).map((marker) => (
          <Box key={marker.key} component="span" aria-hidden="true" sx={[MARKER_SX, { visibility: 'hidden' }]}>{marker.glyph}</Box>
        ))}
        {template.slots?.map((slot) => {
          const column = byKey.get(slot.key)
          /* A slot no column names keeps its width (so later headers stay over their values) and says nothing. */
          return column ? (
            <FeedColumnCell
              key={slot.key}
              column={column}
              align={slot.align}
              sx={columnSlotSx(slot, drops)}
              sort={sort}
              onSort={onSort}
            />
          ) : (
            <Box key={slot.key} aria-hidden="true" sx={columnSlotSx(slot, drops)} />
          )
        })}
        {actionsColumn && <Box aria-hidden="true" sx={[ACTIONS_SPACER_SX, template.titleMaxWidth ? PUSH_END_SX : null]} />}
      </Box>
    </Box>
  )
}

/* A slot's footprint only — width and gate — so its header drops at the same width; the type is the header's. */
const columnSlotSx = (slot: FeedSlot, drops: ReadonlySet<SlotGate>) => ({
  ...(slot.gate ? GATE_SX[slot.gate] : null),
  ...(slot.gate !== undefined && drops.has(slot.gate) ? DROPPED_SX : null),
  flex: '0 0 auto',
  width: slot.width,
})

/** A section's heading row — a folding button when `onToggle` is given. */
function FeedSectionHeading({
  label,
  labelAside,
  count,
  countNote,
  collapsed,
  onToggle,
  depth = 0,
  nested = false,
  underColumns = false,
}: {
  label: string
  labelAside?: ReactNode
  count?: number | null
  countNote?: string
  collapsed?: boolean
  onToggle?: () => void
  depth?: number
  nested?: boolean
  underColumns?: boolean
}): JSX.Element {
  /* UNDER A COLUMN ROW a depth-0 heading starts where the Name header and the row titles do, past the disc;
     flush left it read as a stray label beside the table. NOT IN A NESTED OUTLINE: there the ladder rules
     (`headLevel`), and the inset would put a parent heading right of the headings under it. */
  const inset = underColumns && depth === 0 && !nested ? rowPadLeft(0) : null
  /* THE HEADING IS A ROW TOO: inside a `presentation` bucket it would otherwise be a stray direct child of
     `role="table"` (the toggle's `button` announced adrift). The inner box keeps the layout; the row/cell pair
     carries the semantics. */
  return (
    <Box role="row">
      <Box role="cell">
        {onToggle ? (
          <Box
            component="button"
            type="button"
            onClick={onToggle}
            aria-expanded={!collapsed}
            data-testid="feed-section-toggle"
            sx={[HEAD_SX, HEAD_BUTTON_SX, headLevel(depth, nested), inset]}
          >
            {/* Pinned width: the carets differ in width, and a moving label reads as broken. */}
            <Box component="span" aria-hidden="true" sx={{ width: 10 }}>{disclosureGlyph(!collapsed)}</Box>
            <Box component="span" title={label} sx={HEAD_LABEL_SX}>{label}</Box>
            {labelAside}
            {count != null && <Box component="span" sx={COUNT_SX}>{count}</Box>}
            {countNote && <Box component="span" data-testid="feed-section-note" sx={COUNT_NOTE_SX}>{countNote}</Box>}
          </Box>
        ) : (
          <Box data-testid="feed-bucket-head" sx={[HEAD_SX, headLevel(depth, nested), inset]}>
            <Box component="span" title={label} sx={HEAD_LABEL_SX}>{label}</Box>
            {labelAside}
            {count != null && <Box component="span" sx={COUNT_SX}>{count}</Box>}
            {countNote && <Box component="span" data-testid="feed-section-note" sx={COUNT_NOTE_SX}>{countNote}</Box>}
          </Box>
        )}
      </Box>
    </Box>
  )
}

/** Does this row, or any row nested under it, carry a ⋯? */
const hasActions = (row: FeedRowSpec): boolean => (row.actions?.length ?? 0) > 0 || (row.children?.some(hasActions) ?? false)

export function FeedList({
  sections,
  ariaLabel,
  loading,
  emptyMessage = 'Nothing to show.',
  leading,
  testId,
  skeletonRows = 6,
  columns,
  sort,
  onSort,
}: {
  sections: FeedSection[]
  ariaLabel: string
  loading?: boolean
  emptyMessage?: string
  /** A row rendered above the list — an inline create, most often. */
  leading?: ReactNode
  testId?: string
  skeletonRows?: number
  /** A column header row over the rows: `slots[].key` names the `FeedSlot.key` each heads. Drawn only while
   *  there are rows to head. */
  columns?: { title: FeedColumn; slots?: FeedColumn[] }
  /** The sorted column (`hooks/useListPage`'s `sort`/`dir`); its header shows ▲/▼ and carries `aria-sort`. */
  sort?: { key: string | null; dir: SortDir }
  /** A sortable header's press: its `sortKey` and `firstDir` (`useListPage`'s `toggleSort`). */
  onSort?: (sortKey: string, firstDir?: SortDir) => void
}): JSX.Element {
  /* EMPTINESS IS `sections.length`, not the row total: a zero-row section is a statement. */
  const empty = sections.length === 0
  /* Read off the sections, not a prop, so a caller cannot claim nesting it does not pass. */
  const nested = sections.some((section) => (section.depth ?? 0) > 0)
  /* The header's geometry is the first row's: every row of a list shares one slot set. */
  const template = columns ? sections.find((section) => section.rows.length > 0)?.rows[0] : undefined
  /* THE ⋯ COLUMN EXISTS ONLY WHILE SOME ROW HAS A ⋯: its spacer keeps a verbless row's columns under a
     verbed one's, and with no ⋯ anywhere it was an empty 30px after the last slot (an inline Remove sat short of
     the right edge). */
  const actionsColumn = sections.some((section) => section.rows.some(hasActions))
  /* The slots this list's OWN width cannot afford (`./fit`), priced off the first row: every row shares one slot set. */
  const first = useMemo(() => sections.find((section) => section.rows.length > 0)?.rows[0], [sections])
  const rootRef = useRef<HTMLDivElement>(null)
  const drops = useFittedDrops(rootRef, first)

  return (
    <Box ref={rootRef} role="table" aria-label={ariaLabel} data-testid={testId} sx={CONTAINER_SX}>
      {/* The ownership rule applied to `leading`: wrapped as a row centrally, since its caller
          (`ActivityFeedView`) puts the primary inline task-create FORM there. */}
      {leading && (
        <Box role="row">
          <Box role="cell">{leading}</Box>
        </Box>
      )}
      {loading && empty ? (
        <Box role="row" data-testid="feed-loading">
          {/* Tapering: a uniform block reads as empty rows, a ragged one as content arriving. */}
          <Box role="cell">
            {Array.from({ length: skeletonRows }, (_, index) => (
              <Skeleton key={index} variant="text" height={12} width={`${70 - index * 6}%`} sx={SKELETON_ROW_SX} />
            ))}
          </Box>
        </Box>
      ) : empty ? (
        <Box role="row" data-testid="feed-empty" sx={EMPTY_SX}>
          <Box role="cell">{emptyMessage}</Box>
        </Box>
      ) : (
        <>
          {columns && template && <FeedColumnsRow template={template} columns={columns} sort={sort} onSort={onSort} nested={nested} actionsColumn={actionsColumn} drops={drops} />}
          {sections.map((section) => (
            /* `presentation`, so the table OWNS the rowgroup below (a generic box breaks the chain). */
            <Box key={section.key} role="presentation" data-testid="feed-bucket" data-section={section.key}>
              {/* THE HEADING IS A ROW TOO: inside a `presentation` bucket it would otherwise be a stray direct child
                  of `role="table"` (the toggle's `button` announced adrift). The inner `presentation` keeps the
                  layout; the row/cell pair carries the semantics. */}
              {section.label != null && (
                <FeedSectionHeading
                  label={section.label}
                  labelAside={section.labelAside}
                  count={section.count}
                  countNote={section.countNote}
                  collapsed={section.collapsed}
                  onToggle={section.onToggle}
                  depth={section.depth ?? 0}
                  nested={nested}
                  underColumns={template != null}
                />
              )}
              {!section.collapsed && (
                <Box role="rowgroup" sx={BODY_SX}>
                  {section.rail !== false && <Box aria-hidden="true" sx={RAIL_SX} />}
                  {section.rows.map((row) => (
                    <Fragment key={row.key}>
                      <FeedRow row={row} depth={row.depth ?? 0} actionsColumn={actionsColumn} drops={drops} />
                    </Fragment>
                  ))}
                </Box>
              )}
            </Box>
          ))}
        </>
      )}
    </Box>
  )
}

export default FeedList
