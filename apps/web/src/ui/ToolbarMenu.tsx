import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import Badge from '@mui/material/Badge'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Popover from '@mui/material/Popover'
import TextField from '@mui/material/TextField'
import type { Theme } from '@mui/material/styles'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import { withUnmatched } from './FilterRow'
import { Icon } from './icons'
import { PHONE } from './listStyles'
import { NO_AUTOFILL_INPUT } from './noAutofill'

/* The console's ONE settings menu for a list page's header — a wordless sliders button opening ONE
 * DRILL-DOWN panel that holds every setting the page has (operator-chosen design).
 * Level one lists each setting's NAME and CURRENT VALUE; tapping one replaces the contents with its page
 * (back arrow, name, checked options); picking returns. The panel is a CONSTANT 268px wide and near-constant
 * tall regardless of data, and every current value is visible where it opens.
 * HELP TEXT lives in each control's `title` (and `aria-label` where the name would lose it); a page that
 * cannot say it in a tooltip prints a caption (`PAGE_CAPTION_SX`).
 * A `ui/` primitive so every list page shares the measured MECHANISM (popover, value rows, page state, back,
 * focus moves, width, and the `RadioPage`/`CheckboxPage`/`SearchPage` bodies); callers own the VOCABULARY.
 * ── THE BADGE ── counts filters on, answering "why is this list short?": only settings that REMOVE ROWS may
 * tick it (the caller decides; `TaskToolbar`'s `activeFilters` is the worked example).
 */

/* EVERY toolbar button, one declaration: on a phone it drops its WORD and keeps its 44px box (`flex: 0 0
   auto`). Here, not `listStyles.ts` (rows' vocabulary); also used by `TaskToolbar` / `BulkActionBar`. */
export const TOOLBAR_BUTTON_SX = {
  [PHONE]: { flex: '0 0 auto', minWidth: 44, minHeight: 44, px: 0 },
} as const

/* …and the word they drop; each keeps its `aria-label`, so AT names them at every width. `textTransform`: the
   word is a `variant="button"` Typography, whose `typography.button` uppercases — the theme's `MuiButton`
   override does not reach a child, so without it a bulk bar read SYNC beside the page's title-case buttons. */
export const TOOLBAR_LABEL_SX = { textTransform: 'none', [PHONE]: { display: 'none' } } as const

/* A button WORDLESS AT EVERY WIDTH (this trigger, `/tasks`'s focus toggle, `BulkActionBar`'s ✕).
   `minWidth: 40` + `px: 0` set desktop width, `GLYPH_BOX_SX` the height; the spread carries the 44×44 phone
   rule. Shared because two glyphs in different boxes is a reported defect. */
export const GLYPH_BUTTON_SX = {
  ...TOOLBAR_BUTTON_SX,
  minWidth: 40,
  px: 0,
} as const

/* A wordless TOGGLE's lit state (`/tasks`'s Focus, the Flat/Tree toggle): accent border + glyph + an ACCENT-tinted
   well, not `variant="contained"` (that is ＋'s). A neutral `action.selected` well dropped the lit glyph to
   3.0:1 in dark (WCAG 1.4.11); the accent wash measures 3.97:1 dark / 4.79:1 light. The hover arm repeats
   them so MUI's outlined hover cannot wash the accent out. `rgba(var(--mui-palette-…-mainChannel) / …)`
   repaints across a theme flip; an `alpha()`d palette value does not. */
export const GLYPH_BUTTON_ON_SX = {
  ...GLYPH_BUTTON_SX,
  color: 'primary.main',
  borderColor: 'primary.main',
  bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.16)',
  '&:hover': { borderColor: 'primary.main', bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.24)' },
} as const

/* A WORDLESS BUTTON'S LINE BOX: a labelled button's height comes from its `variant="button"` line box
   (0.875rem × 1.75 = 24.5px), so a glyph-only one renders 8.5px short. The glyph sits in a box of the same
   line height. READ FROM THE THEME (never `minHeight: 32.5`); on a WRAPPER, not the `<svg>` (which would
   scale the 16px mark). `1.75em`, NOT `calc(fontSize * lineHeight)`: `fontSize` may be a NUMBER, making
   `calc(14 * 1.75)` invalid CSS that TypeScript accepts. */
export const GLYPH_BOX_SX = (theme: Theme) => ({
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: theme.typography.button.fontSize,
  height: `${theme.typography.button.lineHeight}em`,
})

/* THE MENU'S MARK — three sliders as a 16px inline `<svg>` (`currentColor`, `aria-hidden`): no Unicode glyph
   renders consistently and there is no icon package. SLIDERS, NOT A FUNNEL: the menu also changes how the
   board is DRAWN. One mark for every consumer, not a prop. */
function SlidersIcon() {
  return (
    <Icon size={16} testId="toolbar-menu-glyph" sx={{ display: 'block' }}>
      {/* Knobs at different positions read as settings, not a list; 2px bars match the focus arcs beside it. */}
      <Box
        component="path"
        d="M2 4h12M2 8h12M2 12h12"
        sx={{ fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' }}
      />
      <Box component="circle" cx="5.5" cy="4" r="2" sx={{ fill: 'currentColor' }} />
      <Box component="circle" cx="10.5" cy="8" r="2" sx={{ fill: 'currentColor' }} />
      <Box component="circle" cx="6.5" cy="12" r="2" sx={{ fill: 'currentColor' }} />
    </Icon>
  )
}

/* ── THE PANEL ───────────────────────────────────────────────────────────────────────────────────── */

/* ONE SIZE at every level, and a constant, not a prop: 268px fits `/tasks`'s widest value rows and lands the period pills three-then-two. Per-page sizing would resize under the cursor.
   AT PHONE WIDTH A DEFINITE WIDTH (`100vw - 32px`), not a cap: an auto-width popover clamps into the viewport
   from its own measurement and ends flush against the edge. `marginThreshold` is the other half.
   `gap: 0`, no padding: rows own spacing so their rules run full width. */
const PANEL_SX = {
  display: 'flex',
  flexDirection: 'column',
  width: 268,
  maxWidth: 'calc(100vw - 32px)',
  [PHONE]: { width: 'calc(100vw - 32px)' },
} as const

/* MUI's default 16px, spelled because it pairs with the panel's phone width above. */
const PANEL_MARGIN_THRESHOLD = 16

/* The panel's title row (MUI's Popover Paper has no heading). Level one hosts Clear; sub-pages the back arrow. */
const PANEL_HEADER_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  px: 1.5,
  py: 1,
  minHeight: 44,
  borderBottom: '1px solid',
  borderColor: 'divider',
} as const

const PANEL_TITLE_SX = {
  typography: 'subtitle2',
  fontWeight: 700,
  color: 'text.primary',
  flex: 1,
  minWidth: 0,
  m: 0,
} as const

/* CLEAR IS A LINK (a rare corrective gesture, findable without competing), and RENDERS ONLY WHEN THERE IS
   SOMETHING TO CLEAR. */
const PANEL_CLEAR_SX = {
  font: 'inherit',
  typography: 'caption',
  cursor: 'pointer',
  color: 'text.secondary',
  flex: '0 0 auto',
  '&:hover': { color: 'text.primary' },
  [PHONE]: { minHeight: 44, display: 'inline-flex', alignItems: 'center' },
} as const

/* THE BACK ARROW: a real `<button>`, 44px floor, a chevron (the title beside it names the page); its
   `aria-label` says the whole thing. */
const PANEL_BACK_SX = {
  appearance: 'none',
  m: 0,
  p: 0,
  border: 0,
  background: 'none',
  cursor: 'pointer',
  color: 'text.secondary',
  font: 'inherit',
  fontSize: 18,
  lineHeight: 1,
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 28,
  height: 28,
  borderRadius: 1,
  flex: '0 0 auto',
  '&:hover': { color: 'text.primary', bgcolor: 'action.hover' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'text.primary', outlineOffset: 1 },
  [PHONE]: { width: 44, height: 44 },
} as const

/* ── LEVEL ONE: THE VALUE ROWS ───────────────────────────────────────────────────────────────────── */

/* NAME left, CURRENT VALUE right, a chevron for the page behind. A real `<button>` (one tab stop, announces
   its value, opens on Enter), so the UA button styling is reset. 40px desktop, 44px phone. */
const ROW_SX = {
  appearance: 'none',
  m: 0,
  border: 0,
  borderBottom: '1px solid',
  borderColor: 'divider',
  background: 'none',
  cursor: 'pointer',
  font: 'inherit',
  typography: 'body2',
  color: 'text.secondary',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  textAlign: 'left',
  width: '100%',
  px: 1.5,
  minHeight: 40,
  '&:last-of-type': { borderBottom: 0 },
  '&:hover': { bgcolor: 'action.hover' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'text.primary', outlineOffset: -2 },
  [PHONE]: { minHeight: 44 },
} as const

/* The loudest thing on the row: the answers are why the panel opens on a list. Truncates (a two-line row
   breaks the constant height); the row's `title` has it in full. */
const ROW_VALUE_SX = {
  ml: 'auto',
  minWidth: 0,
  fontWeight: 600,
  color: 'text.primary',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const

const ROW_CHEVRON_SX = { flex: '0 0 auto', color: 'text.disabled', fontSize: 16, lineHeight: 1 } as const

/* THE ROW THAT IS NOT A DOOR — a two-state setting's control on the row itself (`/tasks`'s Hidden switch).
   A `<label>`, so the whole 44px ROW toggles the implicitly associated input. Not a `<button>` (a nested input
   is non-conforming) and not a `<div>` (only the ~28px switch would be hittable).
   Its word in the body colour: a door's name is secondary beside its bold value, but this row has no
   value, and a dimmed word alone read as disabled. The hint is the row's `title`. */
const TOGGLE_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  typography: 'body2',
  color: 'text.primary',
  cursor: 'pointer',
  px: 1.5,
  minHeight: 40,
  borderTop: '1px solid',
  borderColor: 'divider',
  '&:hover': { bgcolor: 'action.hover' },
  /* The inner control carries the ring; the label must not add a second. */
  '&:focus-visible': { outline: 'none' },
  [PHONE]: { minHeight: 44 },
} as const

/* ── LEVEL TWO: ONE SETTING'S PAGE, AND THE KIT ITS BODIES ARE DRAWN WITH ────────────────────────── */

/* THE `OPTION_*` KIT IS PRIVATE: every option row is drawn by `RadioPage`/`CheckboxPage`, which keeps
   options identical across routes. Exported for pages that are neither: the body box, the caption, the field
   floor and the two keyboard helpers (`/tasks`'s Period pills, `/worktrees`'s Search). Ask for a private one
   with the case that needs it. */

/* `py`, not per-row margins, so the end options sit off the edges with no rule under the header. */
export const PAGE_BODY_SX = { display: 'flex', flexDirection: 'column', py: 0.5 } as const

/* A LIST-SHAPED PAGE SCROLLS rather than growing the panel (the header is not sticky, so a tall panel carries
   the back arrow off screen). UNCONDITIONAL in `RadioPage`/`CheckboxPage`: a page cannot be trusted to know
   it will grow data-driven. ONE CONSTANT for all pages. */
const PAGE_LIST_SX = {
  display: 'flex',
  flexDirection: 'column',
  maxHeight: 264,
  overflowY: 'auto',
} as const

/* The TICK column is reserved (left) so labels line up. Same 44px floor. */
const OPTION_SX = {
  appearance: 'none',
  m: 0,
  border: 0,
  background: 'none',
  cursor: 'pointer',
  font: 'inherit',
  typography: 'body2',
  color: 'text.primary',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  textAlign: 'left',
  width: '100%',
  px: 1.5,
  minHeight: 36,
  '&:hover': { bgcolor: 'action.hover' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'text.primary', outlineOffset: -2 },
  [PHONE]: { minHeight: 44 },
} as const

/* The tick. THE BOX RESERVES THE COLUMN, NOT THE MARK: `✓` or nothing is a conditional at the render sites,
   keeping the glyph out of option text that specs assert on. `aria-hidden` there: `aria-checked` is what AT
   reads. */
const OPTION_TICK_SX = {
  flex: '0 0 auto',
  width: 14,
  color: 'primary.main',
  fontSize: 13,
  lineHeight: 1,
} as const

const OPTION_LABEL_SX = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const

/* The label's own box, so a leading mark (`/tasks`'s creator disc) binds to the name with a 6px gap. ALWAYS
   RENDERED, keeping `CheckboxPage` one shape; with no mark the truncation boundary is unchanged. Not exported. */
const OPTION_LABEL_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  gap: 0.75,
  minWidth: 0,
} as const

/* Small print at the row's end (a period's day count, a creator's task count). */
const OPTION_META_SX = { ml: 'auto', flex: '0 0 auto', color: 'text.secondary', typography: 'caption' } as const

/* The one help text the panel PRINTS — only where a tooltip cannot work (`/tasks`'s period summary). */
export const PAGE_CAPTION_SX = { typography: 'caption', color: 'text.secondary', px: 1.5, pt: 0.5, pb: 1.5 } as const

/* A TEXT FIELD'S TAP FLOOR. MUI's `size="small"` input is 40px, 4px under the floor (measured on
   `/worktrees`'s Search and `/tasks`'s two date inputs).
   THE HEIGHT LIVES ON THE INPUT ROOT (the `InputBase`), not the `FormControl` wrapper, AND THE INPUT'S OWN
   PADDING IS RESTATED: a `minHeight` alone either centres a 40px input in a 44px box or, on a native date
   input, shrinks it to three dots with oracles green. 8.5 + 1.4375em (23px) + 8.5 = 40, so (44 − 23) / 2 = 10.5.
   `PHONE`, NOT `TAP_FLOOR_DOWN`: every control in this panel floors at 480px (the theme's `sm` floor is
   `MuiIconButton`'s alone), and `/tasks`'s date inputs already used this boundary.
   `theme.custom?` with the number restated (as `listStyles.ts`'s `tapTargetSx`): `custom` is absent under
   MUI's stock theme in tests. */
export const PAGE_FIELD_SX = (theme: Theme) => ({
  m: 0,
  '& .MuiInputBase-root': { [PHONE]: { minHeight: theme.custom?.tapTargetPx ?? 44 } },
  '& .MuiInputBase-input': { [PHONE]: { pt: '10.5px', pb: '10.5px' } },
})

/* ONE TAB STOP PER GROUP, entered at the checked option — the roving tabindex `radiogroup` promises. A
   helper because "checked" is spelled differently on each page. */
export function rovingTabIndex(isChecked: boolean) {
  return isChecked ? 0 : -1
}

/* ARROW KEYS MOVE BETWEEN A PAGE'S OPTIONS (what `radiogroup` promises); options are real `<button>`s.
   SELECTION DOES NOT FOLLOW FOCUS (the APG's permitted alternative): a pick navigates back, so arrows move and
   Enter/Space pick. Wraps; Home/End jump. `preventDefault` stops ArrowDown scrolling the page behind.
   READ OFF THE DOM (`[role="radio"]` children), so it cannot drift from any caller's `map`. */
export function moveWithinRadioGroup(event: React.KeyboardEvent<HTMLElement>) {
  const MOVES = ['ArrowDown', 'ArrowRight', 'ArrowUp', 'ArrowLeft', 'Home', 'End']
  if (!MOVES.includes(event.key)) return
  const options = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]'))
  if (options.length === 0) return
  /* `-1` (focus not on an option) is "before the first": ArrowDown enters at the top, ArrowUp at the bottom
     (raw modular arithmetic would pick the second-to-last). */
  const at = options.indexOf(document.activeElement as HTMLElement)
  const here = at === -1 ? -1 : at
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? options.length - 1
        : event.key === 'ArrowDown' || event.key === 'ArrowRight'
          ? (here + 1 + options.length) % options.length
          : here === -1
            ? options.length - 1
            : (here - 1 + options.length) % options.length
  event.preventDefault()
  options[next]?.focus()
}

/* ── THE CALLER'S DECLARATION ────────────────────────────────────────────────────────────────────── */

/* What a page can do to the panel, handed to `render`: WHERE THE PANEL GOES after a press is the menu's.
   ONE VERB; the other outcome is calling nothing (`/tasks`'s `Exact dates` preset holds the page that way). */
type ToolbarMenuNav = {
  /** Finished: back to the value rows, or shut the panel if the page said so. */
  pick: () => void
}

/** One setting, and the page behind its row. */
export type ToolbarMenuPage = {
  kind: 'page'
  /* Builds the row's and page's `data-testid` (`toolbar-menu-row-<id>`) and the panel's `data-page`. */
  id: string
  /** The word on its row and the page's heading. */
  label: string
  /** The CURRENT value, right of the row. */
  value: ReactNode
  /** The row's accessible value when `value` is not a plain string. */
  valueText?: string
  /** The row's `title` (help text). */
  hint?: string
  /* SHUT THE PANEL ON A PICK (for settings that redraw what is underneath — look, decide, look
     again). Off by default so two settings can change in one visit. */
  closeOnPick?: boolean
  /* The page body — `RadioPage`/`CheckboxPage`/`SearchPage`, or the caller's own markup (`/tasks`'s Period
     pills, inside `PAGE_BODY_SX` with `PAGE_FIELD_SX`'s floor).
     INITIAL FOCUS: the panel gives it to the BACK ARROW on the way in UNLESS the body already claimed it —
     `autoFocus` applies at commit, the panel's move is a later passive effect that stands down when focus is
     already inside. Claim focus and keep it; claim nothing and the back arrow takes it. */
  render: (nav: ToolbarMenuNav) => ReactNode
}

/** No page behind it: two states, a control on the row. */
type ToolbarMenuRow = {
  kind: 'row'
  /** `toolbar-menu-row-<id>` is built from it. */
  id: string
  label: string
  hint?: string
  /* Usually a `Switch`, and it OWES ITS OWN ACCESSIBLE NAME: the row's word alone rarely tells the whole story. */
  control: ReactNode
}

export type ToolbarMenuItem = ToolbarMenuPage | ToolbarMenuRow

/* ── THE TWO PAGE BODIES BOTH CALLERS TURNED OUT TO BE WRITING ───────────────────────────────────── */

/* THE PAGE BODIES every consumer builds from the `OPTION_*` kit, one home so keyboard, truncation and tap-floor
   fixes reach every route. They differ in what a press means: single-select FINISHES on a pick (returns);
   multi-select never calls `nav` (three picks must not cost two presses each). `SearchPage` is the third
   shared shape. A page that is none of them (`/tasks`'s Period, argued in `TaskToolbar.tsx`) draws itself. */

/** One option on a single-select page. */
type ToolbarMenuRadioOption<V> = {
  value: V
  /** Truncated by the shared rule. */
  label: string
  /** Its `title` (help text). */
  hint: string
  /* The option's spec handle; defaults to `<page testId>-<value>`. Stated where established names differ:
     `/tasks`'s Group-by page is `toolbar-group-by` with `toolbar-group-none` options (four e2e specs and a
     shared fixture use them). */
  testId?: string
}

/** One option on a multi-select page. */
type ToolbarMenuCheckboxOption<V> = {
  value: V
  label: string
  /* A mark before the word, inside `OPTION_LABEL_ROW_SX` (`/tasks`'s creator disc, `aria-hidden` at source). */
  icon?: ReactNode
  /** Row-end small print. See `OPTION_META_SX`. */
  meta?: string
  /* What AT hears instead of the row text, when the label is not the gesture's whole name ("Filter by Ada
     Lovelace"). */
  ariaLabel?: string
  /** Same contract as the radio option's above. */
  testId?: string
}

/* THE SINGLE-SELECT PAGE: `role="radiogroup"` + `moveWithinRadioGroup` + `rovingTabIndex`.
   GENERIC OVER THE WHOLE VALUE TYPE, `null` INCLUDED: a filter has an OFF state (offered FIRST), a grouping
   has none; widening both to `V | null` would hand groupings a null they can never receive. */
export function RadioPage<V extends string | null>({
  testId,
  label,
  options,
  value,
  onPick,
  nav,
}: {
  /** The body's handle; options build theirs from it unless stated. */
  testId: string
  /** The group's accessible name (the heading beside the back arrow shows it). */
  label: string
  options: ToolbarMenuRadioOption<V>[]
  value: V
  onPick: (next: V) => void
  nav: ToolbarMenuNav
}) {
  return (
    <Box
      data-testid={testId}
      role="radiogroup"
      onKeyDown={moveWithinRadioGroup}
      aria-label={label}
      sx={{ ...PAGE_BODY_SX, ...PAGE_LIST_SX }}
    >
      {options.map(option => {
        const checked = value === option.value
        const optionTestId = option.testId ?? `${testId}-${option.value ?? 'any'}`
        return (
          <Box
            /* The resolved testid is also the key: unique by construction and stable across re-orders. */
            key={optionTestId}
            component="button"
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={rovingTabIndex(checked)}
            onClick={() => {
              onPick(option.value)
              nav.pick()
            }}
            title={option.hint || undefined}
            data-testid={optionTestId}
            sx={OPTION_SX}
          >
            <Box component="span" aria-hidden="true" sx={OPTION_TICK_SX}>{checked ? '✓' : ''}</Box>
            <Box component="span" sx={OPTION_LABEL_SX}>{option.label}</Box>
          </Box>
        )
      })}
    </Box>
  )
}

/** A phone's filter as a menu page (the Nodes and Customers strips): its options under an "all" choice, the row
 *  showing the one picked. A URL value no option names stays visible as itself (`withUnmatched`), as on the
 *  desktop selects. */
export function filterRadioPage(
  testId: string, id: string, label: string, allLabel: string, vocabulary: { value: string; label: string }[],
  value: string, onPick: (value: string) => void,
): ToolbarMenuItem {
  const options = withUnmatched(vocabulary, value)
  return {
    kind: 'page',
    id,
    label,
    value: options.find(o => o.value === value)?.label ?? allLabel,
    render: nav => (
      <RadioPage
        testId={testId}
        label={label}
        options={[{ value: '', label: allLabel, hint: '', testId: `${testId}-any` }, ...options.map(o => ({ ...o, hint: '' }))]}
        value={value}
        onPick={onPick}
        nav={nav}
      />
    ),
  }
}

/* THE MULTI-SELECT TWIN: `role="group"` with checkbox rows, no `nav` (staying put is the difference).
   THE EMPTY PICK IS THE OFF STATE — no All option ("nothing" and "everything" are one list). */
export function CheckboxPage<V extends string>({
  testId,
  label,
  options,
  values,
  onToggle,
  emptyNote,
}: {
  testId: string
  label: string
  options: ToolbarMenuCheckboxOption<V>[]
  values: readonly V[]
  onToggle: (value: V) => void
  /* The empty page's sentence, in the CALLER'S words (a generic one would print one vocabulary everywhere).
     Omitted: renders nothing (`/tasks`'s Creator page before the roster answers). */
  emptyNote?: string
}) {
  return (
    <Box data-testid={testId} role="group" aria-label={label} sx={{ ...PAGE_BODY_SX, ...PAGE_LIST_SX }}>
      {options.length === 0 && emptyNote !== undefined && <Box sx={PAGE_CAPTION_SX}>{emptyNote}</Box>}
      {options.map(option => {
        const picked = values.includes(option.value)
        const optionTestId = option.testId ?? `${testId}-${option.value}`
        return (
          <Box
            key={optionTestId}
            component="button"
            type="button"
            role="checkbox"
            aria-checked={picked}
            aria-label={option.ariaLabel}
            onClick={() => onToggle(option.value)}
            data-testid={optionTestId}
            sx={OPTION_SX}
          >
            <Box component="span" aria-hidden="true" sx={OPTION_TICK_SX}>{picked ? '✓' : ''}</Box>
            <Box component="span" sx={OPTION_LABEL_ROW_SX}>
              {option.icon}
              <Box component="span" sx={OPTION_LABEL_SX}>{option.label}</Box>
            </Box>
            {option.meta !== undefined && <Box component="span" sx={OPTION_META_SX}>{option.meta}</Box>}
          </Box>
        )
      })}
    </Box>
  )
}

/* ── THE THIRD PAGE BODY: A SEARCH FIELD ─────────────────────────────────────────────────────────── */

/* Debounce timings for EVERY search page — one pair, not copied per page. See `SearchPage`. */
const SEARCH_DEBOUNCE_MS = 250
const SEARCH_MAX_WAIT_MS = 1000

/**
 * A search setting's level-one value: the TERM itself (the one filter value nobody recalls later; the page's
 * header shows no search box), or `Off` FOR A WHITESPACE-ONLY TERM — matching every other reader of the value.
 */
export function searchRowValue(query: string): string {
  return query.trim() === '' ? 'Off' : query
}

/**
 * A SEARCH PAGE — a single text field, debounced, that `/worktrees`' Search setting is drawn by (`/tasks` has a
 * header box instead, `components/tasks/pane/TaskSearchBox.tsx`).
 * ── THE COMMIT IS ON A DELAY; THE FIELD IS NOT ──
 * `value` lives in the query string, so committing per keystroke is a ROUTER NAVIGATION each time. The field is
 * LOCAL STATE and the caller catches up when typing pauses: 250ms (< ~300ms, still live), a 1s max-wait so a
 * held key cannot starve the commit, and the TRAILING edge guarantees the last keystroke lands.
 * IT SYNCS BACK so Clear empties the box; syncing to its own committed value is a React no-op.
 * ── THE CAPTION IS THE CALLER'S ── it states how THAT list matches (`/worktrees` AND-s terms client-side).
 */
export function SearchPage({
  testId,
  label,
  value,
  onChange,
  placeholder,
  caption,
}: {
  testId: string
  /** The setting's name — the field's label, and the word its row already carries. */
  label: string
  /** The COMMITTED term, verbatim. */
  value: string
  /** Called with the draft once the typing goes quiet. */
  onChange: (next: string) => void
  placeholder: string
  /* Stays visible (not a `title`): how typed words match must be readable while typing. */
  caption: string
}) {
  const fieldId = useId()
  const [draft, setDraft] = useState(value)
  useEffect(() => { setDraft(value) }, [value])
  const { trigger: scheduleCommit } = useDebouncedCallback(
    /* The hook reads its callback from a ref at fire time, so `draft` is the LATEST render's. */
    () => onChange(draft),
    SEARCH_DEBOUNCE_MS,
    SEARCH_MAX_WAIT_MS,
  )
  return (
    <Box sx={{ ...PAGE_BODY_SX, px: 1.5, pb: 1.5 }}>
      <TextField
        id={fieldId}
        data-testid={testId}
        size="small"
        fullWidth
        /* THE PAGE CLAIMS ITS OWN FOCUS and the back-arrow move stands down (`ToolbarMenuPage.render`); on a
           phone this is whether the keyboard appears. */
        autoFocus
        /* Clears the tap floor (`size="small"` measured 270×40); `PAGE_FIELD_SX` is the single home. */
        sx={PAGE_FIELD_SX}
        value={draft}
        onChange={event => {
          setDraft(event.target.value)
          scheduleCommit()
        }}
        label={label}
        placeholder={placeholder}
        slotProps={{ htmlInput: NO_AUTOFILL_INPUT }}
      />
      <Box sx={PAGE_CAPTION_SX}>{caption}</Box>
    </Box>
  )
}

/* A string `value` names itself; a rich one owes `valueText` (else "[object Object]"). Empty rather than a
   guess otherwise. */
function valueName(page: ToolbarMenuPage): string {
  if (page.valueText !== undefined) return page.valueText
  return typeof page.value === 'string' ? page.value : ''
}

export default function ToolbarMenu({
  name,
  heading,
  hint,
  badgeCount,
  items,
  onClear,
}: {
  /* "Filter and view" — the panel's accessible name, the trigger's (`<name> options`) and the back arrow's
     destination: ONE STRING, one fact. */
  name: string
  /** As PRINTED on the first page (where an ampersand is at home). */
  heading: string
  /** The trigger's hover hint. */
  hint: string
  /* HOW MANY FILTERS ARE ON — only settings that REMOVE ROWS (see header). */
  badgeCount: number
  /** The settings, in the order they appear on level one. */
  items: ToolbarMenuItem[]
  /* WHAT CLEAR CLEARS IS THE CALLER'S: exactly what `badgeCount` counts, nothing else (the badge is the promise).
     OMITTED, THERE IS NO CLEAR. */
  onClear?: () => void
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const [pageId, setPageId] = useState<string | null>(null)
  /* WHICH PAGE IS SHOWING: `null` = level one. State, not a route (a back-press inside a popover must not be
     browser back). RESOLVED TO THE ITEM each render, so a re-derived page redraws live and a vanished item
     falls back to the value rows. */
  const pages = items.filter((item): item is ToolbarMenuPage => item.kind === 'page')
  const page = pageId === null ? undefined : pages.find(candidate => candidate.id === pageId)
  /* …AND THE STATE FOLLOWS THE RENDER: `pageId` must be cleared when its item disappears, or the page JUMPS
     BACK open when the item returns (e.g. `/tasks`'s Creator row reappearing over SSE) with focus stranded.
     ONLY WHILE OPEN: on close `pageId` stays until the exit transition ends (`onExited`), so the fading panel
     does not swap to level one. */
  useEffect(() => {
    if (anchor !== null && pageId !== null && page === undefined) setPageId(null)
  }, [anchor, pageId, page])
  /* ── KEYBOARD FOCUS FOLLOWS THE NAVIGATION, and it has to be moved by hand ──────────────────────
     A drill-down unmounts the control just pressed, dropping focus to `document.body` (Tab restarts; MUI's
     Escape-to-close no longer fires). So: BACK arrow on the way in, the opened ROW on the way back (it now
     announces the new value). Refs, not `autoFocus` (branch-rendered elements; the way back must pick a row).
     THE WAY-IN MOVE STANDS DOWN when focus is already inside the panel (`panelRef`): this passive effect runs
     after a mounted input's `autoFocus` and would otherwise silently WIN. */
  const panelRef = useRef<HTMLDivElement | null>(null)
  const backRef = useRef<HTMLButtonElement | null>(null)
  const rowRefs = useRef<Record<string, HTMLButtonElement | null>>({})
  const previousPage = useRef<string | null>(null)
  useEffect(() => {
    /* Only while OPEN: after the exit transition MUI has already restored focus to the trigger. */
    if (anchor === null) {
      /* Clear the memory on the way out, or reopening before the exit transition finishes yanks focus onto the
         last opened row. */
      previousPage.current = null
      return
    }
    if (pageId !== null) {
      if (!panelRef.current?.contains(document.activeElement)) backRef.current?.focus()
    } else if (previousPage.current !== null) {
      /* THE REMEMBERED ROW CAN BE GONE: the press that finished a page can stop the caller offering it (e.g.
         `/tasks` with a creator picked on an emptied roster, where the row exists on the PICK alone). Then
         the FIRST ROW STILL STANDING takes focus (as `clear()` does) — somewhere INSIDE the panel, which Escape-to-close needs.
         Resolved via `pages`, not `rowRefs`' keys (which hold stale nulls in mount order). */
      const remembered = rowRefs.current[previousPage.current]
      const landing = remembered ?? pages.map(candidate => rowRefs.current[candidate.id]).find(row => row)
      landing?.focus()
    }
    previousPage.current = pageId
  }, [pageId, anchor])

  /* CLOSING PUTS FOCUS BACK ON THE BUTTON by hand: a `closeOnPick` pick unmounts the focused option, so MUI's
     restore targets nothing. `requestAnimationFrame` lands after MUI's own exit focus handling. */
  const closeMenu = () => {
    const trigger = anchor
    setAnchor(null)
    requestAnimationFrame(() => trigger?.focus())
  }

  const clear = () => {
    onClear?.()
    /* FOCUS STAYS IN THE PANEL: Clear unmounts itself, so focus moves to the first page row. */
    const first = pages[0]
    if (first) rowRefs.current[first.id]?.focus()
  }

  return (
    <>
      {/* `invisible` rather than a conditional wrapper, so the header never shifts when a filter toggles. */}
      <Badge
        badgeContent={badgeCount}
        color="primary"
        invisible={badgeCount === 0}
        data-testid="toolbar-menu-badge"
        /* STRETCHED TO ITS LINE: in a filter strip that is the field beside it (MUI small's 40px on a desktop,
           `fieldTapTargetSx`'s 44px below `sm`, the 481-599px band included), so the button can never disagree
           with the field's own height source; in a button cluster the line is the buttons', so nothing changes. */
        sx={{ alignSelf: 'stretch', [PHONE]: { flex: '0 0 auto' } }}
      >
        <Button
          variant="outlined"
          color="inherit"
          onClick={e => {
            setPageId(null)
            setAnchor(e.currentTarget)
          }}
          /* WORDLESS AT EVERY WIDTH (operator's call), so the accessible name carries what it opens and
             whether — and by how many — the list is narrowed. */
          aria-label={
            badgeCount === 0
              ? `${name} options`
              : `${name} options (${badgeCount} filter${badgeCount === 1 ? '' : 's'} on)`
          }
          aria-haspopup="dialog"
          aria-expanded={anchor !== null}
          title={hint}
          data-testid="toolbar-menu"
          sx={GLYPH_BUTTON_SX}
        >
          <Box component="span" sx={GLYPH_BOX_SX}>
            <SlidersIcon />
          </Box>
        </Button>
      </Badge>

      <Popover
        open={anchor !== null}
        anchorEl={anchor}
        onClose={closeMenu}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        marginThreshold={PANEL_MARGIN_THRESHOLD}
        /* THE PAGE RESETS after the exit transition (`onExited`), not in `onClose` (it would swap while
           fading). Reopening always lands on the value list. */
        slotProps={{ transition: { onExited: () => setPageId(null) } }}
      >
        {/* The handle is on the CONTENT: `slotProps.paper` (`PaperProps`) has no `data-*` member. */}
        {/* `role="dialog"` + a name, since the button promises `aria-haspopup="dialog"`. THE NAME IS THE PAGE,
            so drill-down navigation is heard. */}
        <Box
          data-testid="toolbar-menu-panel"
          data-page={page ? page.id : 'root'}
          role="dialog"
          aria-label={page ? page.label : name}
          ref={panelRef}
          sx={PANEL_SX}
        >
          {page ? (
            <>
              <Box sx={PANEL_HEADER_SX}>
                <Box
                  component="button"
                  type="button"
                  onClick={() => setPageId(null)}
                  /* THE PAGE'S NAME IS IN HERE: screen readers do not announce a renamed open dialog, and this
                     button is where focus lands on entry. */
                  aria-label={`${page.label} — back to ${name.toLowerCase()}`}
                  data-testid="toolbar-menu-back"
                  ref={backRef}
                  sx={PANEL_BACK_SX}
                >
                  ‹
                </Box>
                <Box component="h2" sx={PANEL_TITLE_SX}>
                  {page.label}
                </Box>
              </Box>
              {page.render({ pick: () => (page.closeOnPick ? closeMenu() : setPageId(null)) })}
            </>
          ) : (
            <>
              <Box sx={PANEL_HEADER_SX}>
                <Box component="h2" sx={PANEL_TITLE_SX}>
                  {heading}
                </Box>
                {onClear && badgeCount > 0 && (
                  <Link
                    component="button"
                    type="button"
                    underline="hover"
                    onClick={clear}
                    /* One sentence for every menu, not a prop: Clear discharges exactly what the badge counts. */
                    title="Turn every active filter off"
                    data-testid="toolbar-menu-clear"
                    sx={PANEL_CLEAR_SX}
                  >
                    Clear
                  </Link>
                )}
              </Box>

              {/* THE VALUE ROWS, IN THE CALLER'S ORDER (`/tasks`: row-removing settings first, drawing ones after,
                  its switch last). Each row's `title` is its help line. */}
              {items.map(item =>
                item.kind === 'page' ? (
                  <Box
                    key={item.id}
                    component="button"
                    type="button"
                    onClick={() => setPageId(item.id)}
                    title={item.hint}
                    /* The name carries the current value and that pressing opens a page. */
                    aria-label={`${item.label}: ${valueName(item)}. Press to change.`}
                    data-testid={`toolbar-menu-row-${item.id}`}
                    ref={(el: HTMLButtonElement | null) => { rowRefs.current[item.id] = el }}
                    sx={ROW_SX}
                  >
                    <Box component="span">{item.label}</Box>
                    <Box component="span" sx={ROW_VALUE_SX}>{item.value}</Box>
                    <Box component="span" aria-hidden="true" sx={ROW_CHEVRON_SX}>›</Box>
                  </Box>
                ) : (
                  <Box
                    key={item.id}
                    component="label"
                    sx={TOGGLE_ROW_SX}
                    title={item.hint}
                    data-testid={`toolbar-menu-row-${item.id}`}
                  >
                    {/* The word takes the spare width, ranging the control at the far end without caller `ml: 'auto'`. */}
                    <Box component="span" sx={{ flex: 1, minWidth: 0 }}>{item.label}</Box>
                    {item.control}
                  </Box>
                ),
              )}
            </>
          )}
        </Box>
      </Popover>
    </>
  )
}
