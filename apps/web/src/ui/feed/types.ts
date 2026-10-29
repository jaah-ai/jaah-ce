/* What a list hands the feed: the console's one row contract. A route supplies facts; `FeedList` owns
 * every pixel and `./geometry` every number. Nothing here is task-shaped
 * (`components/tasks/listViews/ActivityFeedView.tsx` is the first consumer). */
import type { PointerEvent, ReactNode } from 'react'
import type { SortDir } from '../../hooks/useListPage'
import type { OverflowItem } from '../../components/tasks/rowActions'
import type { Tone } from '../StatusChip'
import type { SlotGate } from './geometry'

export type { OverflowItem, SortDir }

/* The state mark: a glyph on a tone-tinted disc, not a coloured dot (colour alone), legible at 18px. */
export type FeedIndicator = {
  tone: Tone
  /** One character. `○ » ● ? ✕ ■ ✓` is the task vocabulary; a route picks its own, or `ui/RunningSpinner` for
   *  work in progress. */
  glyph: ReactNode
  /** What the mark means in words — the row's only state signal. See `FeedRowSpec.ariaLabel`. */
  label: string
  /** The disc's hover sentence. */
  title?: string
  /** Also shows `title` on a TAP (an inert disc only): hover is unreachable by touch, so a disc whose sentence is
   *  the row's state reason opts in. */
  tapTitle?: boolean
  /** Makes the disc a button. Absent = an inert mark that never eats the row's press. */
  onPress?: () => void
  /** What the press does ("Open web console"). Required with `onPress`. */
  pressLabel?: string
  /** The action's mark, shown in place of `glyph` while the pressable disc is hovered or focused
   *  (`../pressSwap.tsx`): the console icon on a disc that opens it, ▶ on one that starts. */
  pressGlyph?: ReactNode
  /** Makes a pressable disc a CHECKBOX (`role="checkbox"`, `aria-checked`): a pick list's tick, whose
   *  `pressLabel` then names the item rather than a verb. */
  checked?: boolean
  /** `data-*` on the disc, for state oracles. */
  data?: Record<string, string | number | boolean | undefined>
  /** Draws the slow halo that marks a row as live right now. */
  live?: boolean
  /** An arc on the disc's perimeter, a rollup costing no column. `pct` (0-100) is the route's rounding,
   *  never re-derived, so the arc and `data-pct` are one number. */
  ring?: { pct: number } | null
}

/* A fixed-width trailing column, reserved on every row (see `./geometry`). */
export type FeedSlot = {
  key: string
  /** Pixels. A slot cannot yield; price a new one against `ui/listStyles.ts`'s two stated sums. A CSS length
   *  (`./geometry::contentSlotWidth`) sizes a column to its widest value, the same on every row. */
  width: number | string
  /** Pixels on the phone band, for a right-most, right-aligned slot whose value on THIS row is shorter (the
   *  column's right edge holds). Not applied to the column header. */
  phoneWidth?: number
  /** `phoneWidth`'s rule under a list's own narrow band (a container query from `ui/listStyles.ts`), for a
   *  list whose pane narrows on a wide window. Same terms: right-most, right-aligned, header untouched. */
  narrowWidth?: { query: string; width: number }
  /** Absent renders the reserved blank; pass `null` for "no value on this row". */
  node?: ReactNode
  /** Which band can afford it. Order slots meta → id → ungated; see `SlotGate`. */
  gate?: SlotGate
  align?: 'left' | 'center' | 'right'
  mono?: boolean
  /** `text.disabled`, for a slot that dates or numbers the row. */
  dim?: boolean
  /* This slot's control gives up the phone tap floor (`./geometry`'s `DISCLOSURE_TOUCH_MIN`), opt-in:
   * otherwise an icon button makes the row 44px against the band's 24px. Only when the verb has a second
   * home in the row's ⋯ menu; otherwise gate the slot off the phone band. */
  compact?: boolean
  /** 2: the value wraps at word boundaries, clamped to two lines (`FeedRowSpec.titleLines`' rule), for prose a
   *  column cannot hold on one. Default: one line, ellipsised. */
  lines?: 1 | 2
  /** Defaults to the cluster's 11px; raise it and re-price the slot. */
  fontSize?: string
  /** The value in words, when the rendered node is a glyph or an abbreviation. */
  title?: string
  /** `data-*` on the slot, for a state an oracle should read as a state. */
  data?: Record<string, string | number | boolean | undefined>
  testId?: string
}

/** A one-character mark beside the title; reserves no width on rows without it. Announced per
 *  `FeedRowSpec.ariaLabel`. */
export type FeedMarker = {
  key: string
  glyph: string
  label: string
  /** The hover sentence. */
  title?: string
  data?: Record<string, string | number | boolean | undefined>
  testId?: string
  /** Drawn right after the name, inside the title cell, rather than as a column at that cell's far end: a mark ON
   *  the name (a built-in's 🔒), not a fact read down a column. Absent = the column. */
  hug?: boolean
  /** Colours the glyph (and `word`) in this tone, for a mark that must be noticed (a bumped priority).
   *  Absent = `text.disabled`, a quiet mark. Colour is never the only signal: the glyph and `label` still say it. */
  tone?: Tone
  /** A short word after the glyph ("High"), dropped at `gate` so it costs the title nothing where width is
   *  scarce. Silenced with the marker on a named row (`FeedRowSpec.ariaLabel`), like the glyph. */
  word?: { text: string; gate?: SlotGate }
}

export type FeedDisclosure = {
  expanded: boolean
  /** In flight: the control shows `…` and refuses a second press. */
  busy?: boolean
  onToggle: () => void
  /** This list's word for its children ("Expand subtasks"). */
  label?: string
}

/** A fact about a row in words: a reason, a note, an error. NEVER A SECOND LINE: a one-row item stays one
 *  row, so the words ride the row's hover — the name's and the disc's — and a tap on an inert disc on a touch
 *  device (prose on hover, never a wrapped second line). A list, because a row can owe two at once. */
export type FeedNote = {
  key: string
  text: string
  /** `'error'` / `'warning'` also turn the row's disc to that tone (error wins): the at-a-glance signal the hidden
   *  words no longer give. */
  tone: 'error' | 'warning' | 'muted'
}

/* The feed's half of a drag. A pointer drag, not HTML5 DnD (`components/LayoutDesigner.tsx`'s header:
 * jsdom cannot drive DnD). The feed owns no drag state: it reports `onLift` and draws the route's
 * verdict. `hooks/usePointerDrag.ts` resolves the destination from the element under the pointer via
 * `dropKey`. List-specific rules live with the route (e.g. `lib/nameTreeMove.ts`). */
export type FeedDrag = {
  /** The row is a drag source: pointer-down on its body reports here. The route owns threshold,
   *  destination and release. Absent = cannot be picked up. */
  onLift?: (event: PointerEvent<HTMLElement>) => void
  /** A drag is up somewhere in this list (set on every row); the list stops selecting text meanwhile. */
  dragging?: boolean
  /** This is the row in flight. */
  lifted?: boolean
  /** `'accept'` will take it, `'over'` will take it and the pointer is on it, `'reject'` refuses.
   *  Absent = not a target. A row that would take drops but refuses this one passes `'reject'` with a
   *  reason: a silent target is what a drag must never have. */
  drop?: 'accept' | 'over' | 'reject'
  /** Why the row refuses; its hover sentence while `'reject'`. */
  dropReason?: string
  /** The route's name for this destination, stamped as `data-drop-target` and only on a row that will
   *  take the drop (`feedDropKeyAt`), so a `'reject'` row carries none. A string, compared by identity;
   *  the route decodes it at the drop. */
  dropKey?: string
}

export type FeedRowSpec = {
  key: string
  /** The row's DOM `id`, for deep-link `getElementById`. */
  id?: string
  /** 0 = a root row on the rail, 1 = a child. A route's own list is flat; a tree nests deeper. */
  depth?: number
  /** Absent = an empty disc slot: a row that is a place, not a record with a state (a tree container). */
  indicator?: FeedIndicator
  /** Absent on a row that can have no children; a childless row that could have them passes `'spacer'`
   *  so titles align. */
  disclosure?: FeedDisclosure | 'spacer'
  title: ReactNode
  /** The name as plain text, for `title` and the ⋯ menu's label. */
  titleText: string
  /** 2: the title wraps at word boundaries, clamped to two lines. `'all'`: wraps, never clamped. Default: one
   *  line, ellipsised. */
  titleLines?: 1 | 2 | 'all'
  /** A band (`@media`/`@container`, `ui/listStyles.ts`) inside which a one-line title wraps to two (`titleLines: 2`'s
   *  rule), or several bands NESTED (all must match): a list whose pane narrows keeps a readable name rather than a
   *  few characters and an ellipsis. */
  titleWrapAt?: string | readonly string[]
  /** Caps the title cell's growth (px): the slots follow the name rather than sit across a dead band from a
   *  short one, and the spare width goes before the ⋯ instead. Default: the title takes all spare width. */
  titleMaxWidth?: number
  /** The row's whole accessible name when the title alone does not identify it ("Task 41: Ship the widget
   *  — Running, created by ada"). Supplying it silences the state disc and markers (`aria-hidden`) so the
   *  state is not heard twice; slots are always announced. */
  ariaLabel?: string
  /** Router path. Absent = unnavigable (title renders as text). */
  href?: string
  /** A small control hugging the title's text, OUTSIDE the link (a button inside an `<a>` is invalid).
   *  Revealed on row hover and keyboard focus (`:focus-visible`, so a mouse click does not pin it); never drawn on a hover-less (touch) device, so it must have
   *  a second home (e.g. the ⋯ menu). The title yields width to it, never the reverse. */
  titleAction?: ReactNode
  /** A fact hugging the title's text (an org chip), drawn at every width and on touch, unlike `titleAction`.
   *  Never shrinks: the title yields width to it. A caller that moves it elsewhere on a phone hides it itself. */
  titleAside?: ReactNode
  /** The mono identifier left of the title, inside the link. Gated at `FEED_NO_ID`, or at `gate` (which should
   *  include that band) where the list's own width drops it too. `align` defaults to `right` (numeric ids line up
   *  by their last digit); a free-text lead takes `left`, so short and truncated values start at one edge. */
  lead?: { text: string; width?: number; title?: string; gate?: SlotGate; align?: 'left' | 'right' }
  markers?: FeedMarker[]
  /** The trailing cluster, ordered by gate strength. */
  slots?: FeedSlot[]
  actions?: OverflowItem[]
  /** The row's own control (a switch) at its right edge: in place of the reserved ⋯ column, after the ⋯ when the
   *  row has verbs, so it ends where every other row's ⋯ ends. No column header heads it, and the header row reserves
   *  only the ⋯'s width: a list with `columns` keeps its controls in `slots`. */
  control?: FeedSlot
  /** A write is in flight; the row's controls go busy. */
  busy?: boolean
  /** Pressing the row body. Only for a destination the row already advertises another way, or the very toggle
   *  its checkbox disc already offers (a pick list, `components/AssignmentList.tsx`); never any other verb that
   *  changes anything. Keyboard-reachable (tab stop, Enter, Space). */
  onRowClick?: () => void
  /** What that press does. Required with `onRowClick`; not `ariaLabel`, which names the record. */
  rowClickLabel?: string
  /** A TOUCH long-press (~500ms held, under 10px of travel) on the row body: the hover-less device's second home
   *  for a verb the row already offers elsewhere (a pick list's tick). The click that follows the
   *  press is swallowed, so the title link does not also navigate; a scroll or a move cancels it. */
  onLongPress?: () => void
  /** Marks the row as the one the operator navigated to. */
  deepLinked?: boolean
  /** The row's words on hover and disc tap (`FeedNote`). */
  notes?: FeedNote[]
  /** A region under the row while the disclosure is open (payload, diff, stack trace), the only thing a row draws
   *  beneath itself: the reader opened it. Short and frequent only; a record with more to say has a detail page. */
  detail?: ReactNode
  /** Keep `detail` mounted but hidden while shut, for browser find. Default unmounts. */
  detailKeepMounted?: boolean
  /** This row is what the surface is showing now (e.g. the viewer's artifact); renders `aria-current`.
   *  Distinct from `deepLinked`, which is about arrival. */
  current?: boolean
  /** Present but not currently meaningful (offline node, superseded intent): reduced strength; controls
   *  keep full strength. */
  muted?: boolean
  /** `data-*` for the route's tests and oracles. `testid` is ignored: the handle is always `feed-row`. */
  data?: Record<string, string | number | boolean | undefined>
  /** Children, already built; rendered at `depth + 1` when open. */
  children?: FeedRowSpec[]
  /** Pick-up and drop, when this list has a drag. */
  drag?: FeedDrag
  /** A NATIVE HTML5 drag source carrying `{ type: data }` out of the list (a task row onto the task rail).
   *  Ignored when `drag` is set: the list's own pointer drag owns the row then. */
  dragOut?: { type: string; data: string }
}

/** A run of rows under one heading. A list with no sectioning passes one, unlabelled. */
export type FeedSection = {
  key: string
  /** Absent = no heading. Long labels truncate with the whole string on `title`. */
  label?: string
  /** Drawn after the label and yielding width before it (a project bar's org: `lib/projectLabel.tsx`). */
  labelAside?: ReactNode
  /** A heading nested one level (project → node), one `CHILD_INDENT` step. Sections stay flat. */
  depth?: number
  /** Shown beside the heading. `null` while a server total is in flight, so no window count poses as it. */
  count?: number | null
  /** Small print after the count: what it is made of ("2 failed, 1 paused"). */
  countNote?: string
  collapsed?: boolean
  /** Absent = the section does not fold. */
  onToggle?: () => void
  /** `false` = no thread down the indicator column: the rows are flat, so a line joining their discs would
   *  claim a tree. Absent = drawn. */
  rail?: boolean
  rows: FeedRowSpec[]
}

/** A header cell of `FeedList`'s optional column row. The title column's header is `columns.title`; a slot's is
 *  matched by `key` to the `FeedSlot.key` it heads and borrows that slot's width, gate and alignment, so it lines
 *  up and drops at the same widths as the values under it. */
export type FeedColumn = {
  key: string
  label: ReactNode
  /** The header's name when `label` is a glyph or an abbreviation; also its hover sentence. */
  ariaLabel?: string
  /** The server's sort key (`hooks/useListPage`'s vocabulary). Absent = not sortable. */
  sortKey?: string
  /** The direction a first press sorts in; a date or a count reads best `'desc'`. Default `'asc'`. */
  firstDir?: SortDir
}
