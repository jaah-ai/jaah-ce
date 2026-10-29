import { type JSX, type ReactNode, type Ref, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Divider from '@mui/material/Divider'
import ListItemIcon from '@mui/material/ListItemIcon'
import ListItemText from '@mui/material/ListItemText'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import Tooltip from '@mui/material/Tooltip'
import type { SxProps, Theme } from '@mui/material/styles'
import { listPlaybooks, PLAYBOOKS_KEY, type PlaybookOut } from '../../pages/playbooks/api'
import { AUTOMATION_TAB_PATHS } from '../../routeGroups'
import type { TaskPlaybookRef } from '../../types'
import { Icon } from '../../ui/icons'
import { AXIS_TRACK_GROUND, AXIS_TRACK_RADIUS } from './axisTrackLook'
import ChatGlyph from './ChatGlyph'
import {
  ADVISOR_PERSONA,
  DEFAULT_PERSONA,
  isEpicPersona,
  PROTOTYPER_PERSONA,
} from './taskPersona'

/* ── The task form's two axes: Depth (sketch · task · epic) and Involvement (unattended · guided · chat) ──
 * The hierarchy is Depth → Involvement → Persona → Model, and a press writes only what is below it:
 * Depth seeds all three; Involvement writes `autonomous` (and, at `task` depth, the advisor persona);
 * Persona seeds only Model. No upward edge.
 * Nothing is disabled; only `epic` hides the persona and model selects (an epic is never dispatched).
 * Wordless by operator instruction: positions are drawn glyphs, with every word in a tooltip and
 * accessible name from the same table, so the row fits one line down to 320px.
 * Glyphs are drawn `<svg>`: `@mui/icons-material` may not become a dependency (`RatingBadge`'s note),
 * and an emoji ignores `color`, losing the selected tint. Chat reuses `ChatGlyph`.
 */

// ── The glyph set ──
/* Sized in `em` and painted on `currentColor` (as `ChatGlyph`), so they follow the segment's state.
 * `aria-hidden`: the button carries the accessible name. */
function Glyph({ children, testId }: { children: ReactNode, testId: string }): JSX.Element {
  return (
    <Icon size="1.125em" testId={testId} sx={{ display: 'block', flex: '0 0 auto' }}>
      {children}
    </Icon>
  )
}

/* Depth draws what the row is: one box (same rect for all three) — dashed (provisional), solid (one
 * unit of work), divided into three (decomposed). */
const DEPTH_BOX = 'M4.5 2.6h7a1.9 1.9 0 0 1 1.9 1.9v7a1.9 1.9 0 0 1-1.9 1.9h-7a1.9 1.9 0 0 1-1.9-1.9v-7a1.9 1.9 0 0 1 1.9-1.9Z'

function SketchGlyph(): JSX.Element {
  // The box, dashed: provisional.
  return (
    <Glyph testId="depth-glyph-sketch">
      <path
        d={DEPTH_BOX}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeDasharray="2.7 2.3"
      />
    </Glyph>
  )
}

function TaskGlyph(): JSX.Element {
  // The same box, solid: one unit of work, one runner, one merged PR.
  return (
    <Glyph testId="depth-glyph-task">
      <path d={DEPTH_BOX} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
    </Glyph>
  )
}

function EpicGlyph(): JSX.Element {
  /* The same box divided by edge-to-edge rules (inset bars read as a document). Lighter than the outline. */
  return (
    <Glyph testId="depth-glyph-epic">
      <path d={DEPTH_BOX} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path d="M2.6 6.4h10.8M2.6 9.6h10.8" fill="none" stroke="currentColor" strokeWidth="1.2" />
    </Glyph>
  )
}

function PlaybookGlyph(): JSX.Element {
  /* The same box holding a small flow: a trunk with a branch off it — stages, run in order. The flow takes the
   * epic's lighter rule weight; its three nodes are filled so they survive at 1.125em. */
  return (
    <Glyph testId="depth-glyph-playbook">
      <path d={DEPTH_BOX} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <path
        d="M6 5.6v4.8M6 6.6c0 1.2.8 1.8 2 1.8h2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="round"
      />
      <circle cx="6" cy="5.5" r="1.05" fill="currentColor" />
      <circle cx="6" cy="10.5" r="1.05" fill="currentColor" />
      <circle cx="10.3" cy="8.4" r="1.05" fill="currentColor" />
    </Glyph>
  )
}

/* The pill's open-menu mark: a small chevron, the same stroke family. */
function CaretGlyph(): JSX.Element {
  return (
    <Icon size="0.75em" sx={{ display: 'block', flex: '0 0 auto', opacity: 0.7 }}>
      <path
        d="M4 6.2 8 10l4-3.8"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Icon>
  )
}

/* Involvement's first two marks: an arrow passing through, and the same arrow stopped at a wall — the
 * axis measures whether the runner stops for you. The third is the chat mark.
 */
function UnattendedGlyph(): JSX.Element {
  // The arrow, unobstructed: it never stops.
  return (
    <Glyph testId="involvement-glyph-unattended">
      <path
        d="M2.2 8h10.4M9.5 4.9 12.6 8l-3.1 3.1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </Glyph>
  )
}

function GuidedGlyph(): JSX.Element {
  /* The shortened arrow before a wall. The wall is full height with a gap; shorter or touching, it read as
  a tab stop. */
  return (
    <Glyph testId="involvement-glyph-guided">
      <path
        d="M1.4 8h5.9M5.3 4.9 8.4 8l-3.1 3.1"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M12.4 1.7v12.6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </Glyph>
  )
}

// ── DEPTH ──
/** How much machinery the ask deserves. Seeds everything below it in the hierarchy. */
export type TaskDepth = 'sketch' | 'task' | 'epic'

/* Positions as data (mark, label, hint), so the segment, tooltip and accessible name render one table.
 * Ordered by cost; `task` (the default) sits in the middle. */
export const TASK_DEPTHS: readonly {
  value: TaskDepth
  label: string
  /** What choosing it does — the tooltip's second half. */
  hint: string
  Icon: () => JSX.Element
}[] = [
  {
    value: 'sketch',
    label: 'Sketch',
    hint: 'a quick mockup to look at.',
    Icon: SketchGlyph,
  },
  {
    value: 'task',
    label: 'Task',
    hint: 'one runner ships it as a merged PR.',
    Icon: TaskGlyph,
  },
  {
    value: 'epic',
    label: 'Epic',
    hint: 'analysed and split into subtasks first.',
    Icon: EpicGlyph,
  },
]

/** `TASK_DEPTHS` on a project with no repository: nothing to merge, so its runner reports back. */
const FOLDER_TASK_DEPTHS: typeof TASK_DEPTHS = TASK_DEPTHS.map(d =>
  d.value === 'task' ? { ...d, hint: 'one runner does it and reports back.' } : d)

// ── INVOLVEMENT ──
/** How much of your attention the work gets. Owns `autonomous`, and at `task`+`chat` the persona too. */
export type TaskInvolvement = 'unattended' | 'guided' | 'chat'

/* Ordered by operator cost; `unattended` first as the default (`autonomous` defaults on). */
export const TASK_INVOLVEMENTS: readonly {
  value: TaskInvolvement
  label: string
  hint: string
  Icon: () => JSX.Element
}[] = [
  {
    value: 'unattended',
    label: 'Unattended',
    hint: 'decides everything itself.',
    Icon: UnattendedGlyph,
  },
  {
    value: 'guided',
    label: 'Guided',
    hint: 'asks you before key decisions.',
    Icon: GuidedGlyph,
  },
  {
    value: 'chat',
    label: 'Chat',
    hint: 'talk it through with an advisor.',
    // The console's chat mark, shared with New Chat and the inline create rows.
    Icon: () => <ChatGlyph size="1.125em" />,
  },
]

// ── Resolution: what a Depth press writes into the persona ──
/* The one place a depth becomes a persona. `taskPersona` is the configured default
 * (`task_default_persona`, ships as `coder`), passed in. Position-owned personas (`epic`, `advisor`)
 * are refused as defaults here, or an advisor default would trap the Involvement axis.
 */
export function personaForDepth(depth: TaskDepth, taskPersona: string): string {
  if (depth === 'epic') return 'epic'
  if (depth === 'sketch') return PROTOTYPER_PERSONA
  return isEpicPersona(taskPersona) || taskPersona === ADVISOR_PERSONA ? DEFAULT_PERSONA : taskPersona
}

/** `autonomous` off the Involvement axis alone; a chat is created off. */
export function autonomousForInvolvement(involvement: TaskInvolvement): boolean {
  return involvement === 'unattended'
}

/* ── The seeds: what a stored row implies about each axis ──
 * Run once, to open an edit or seed a create; never a live derivation, so a hand-picked pair the seed
 * would not produce stays legal.
 */
export function depthForPersona(persona: string): TaskDepth {
  if (isEpicPersona(persona)) return 'epic'
  if (persona === PROTOTYPER_PERSONA) return 'sketch'
  return 'task'
}

/* Involvement's seed. Reads both fields: `chat` only for an advisor row with `!autonomous`. Seeding from
 * the persona alone made an advisor row stored `autonomous: true` dirty at mount (chat projects false),
 * firing the unsaved-changes dialog and flipping the column on save. So that row opens on `unattended`.
 * `sketch` + chat stores (prototyper, false) and reopens as `guided`. */
export function involvementForTask(persona: string, autonomous: boolean): TaskInvolvement {
  if (persona === ADVISOR_PERSONA && !autonomous) return 'chat'
  return autonomous ? 'unattended' : 'guided'
}


/* ── The segmented control's metrics ──
 * A recessed pill track with a raised paper chip, not MUI's stock outlined group (its ~12% wash is hard
 * to see). Three selected signals: paper fill, shadow, glyph colour step.
 * Content-sized and shrinkable (`flex: 0 1 auto`, segments `minWidth: 0`), since everything outside
 * `Advanced` shares one line; at 320px the glyph cells are the only free give.
 * Tap floor on height only: 2rem on pointer devices, 44px at ≤sm.
 * Colours use the channel form (`--mui-palette-*Channel`), not `alpha(theme.palette.…)`, which freezes
 * the light literal.
 */
const GROUP_SX = (theme: Theme) => ({
  display: 'inline-flex',
  padding: '0.1875rem',
  gap: '0.125rem',
  borderRadius: AXIS_TRACK_RADIUS,
  backgroundColor: AXIS_TRACK_GROUND,
  border: `1px solid ${theme.vars.palette.divider}`,
})
/* A locked axis (a read-only task, a submit in flight). On the group by prop: MUI marks only the buttons
 * `Mui-disabled`, never the group, so a `&.Mui-disabled` rule here never matched and a locked row looked live.
 * The selected segment keeps its paper fill, so the value still reads, but drops the primary tint. */
const DISABLED_GROUP_SX = (theme: Theme) => ({
  opacity: 0.6,
  '& .MuiToggleButton-root.Mui-selected': { color: theme.vars.palette.text.secondary, boxShadow: 'none' },
})

/* The segment's flex cell is the span, not the button. `1 1 auto`: in a content-sized group a zero basis
 * collapses the positions to their padding. */
const SEGMENT_WRAP_SX = { flex: '1 1 auto', minWidth: 0, display: 'flex' } as const

const SEGMENT_SX = (theme: Theme) => ({
  width: '100%',
  minWidth: 0,
  minHeight: '2rem',
  px: '0.625rem',
  py: '0.25rem',
  border: 0,
  borderRadius: '999px !important',
  color: theme.vars.palette.text.secondary,
  transition: 'background-color 140ms, color 140ms, box-shadow 140ms',
  [theme.breakpoints.down('sm')]: { minHeight: theme.custom.tapTargetPx, px: '0.5rem' },
  /* The squeezed band: the 212px sidebar appears at 481px, collapsing the form column to ~245px (narrower
  than at 320px) until ~560px. At 0.5rem padding the row needed 276px and the ⓘ wrapped. Taken from
  padding only; the glyph and 44px height stay. */
  [theme.breakpoints.between(481, 'sm')]: { px: '0.3125rem' },
  '&:hover': { backgroundColor: 'rgba(var(--mui-palette-common-onBackgroundChannel, 0 0 0) / 0.04)' },
  '&.Mui-selected': {
    backgroundColor: theme.vars.palette.background.paper,
    color: theme.vars.palette.primary.main,
    boxShadow: '0 1px 2px rgba(0,0,0,0.16)',
    '&:hover': { backgroundColor: theme.vars.palette.background.paper },
  },
  '&.Mui-disabled': { border: 0 },
})

/* A fourth Depth segment costs ~36px at phone padding, and the row had 20px spare at 320px (two 104px axes,
 * the 44px ⓘ, 0.75rem gaps in a 296px column). Taken from padding again, never the glyph or the 44px height:
 * 0.3125rem below 481px puts the four at ~112px (row ≈ 284px); the squeezed band (~245px column) needs
 * 0.1875rem (row ≈ 242px). */
const COMPACT_SEGMENT_SX = (theme: Theme) => ({
  [theme.breakpoints.down(481)]: { px: '0.3125rem' },
  [theme.breakpoints.between(481, 'sm')]: { px: '0.1875rem' },
})

/* Labelled segments (`showLabels`): each one icon plus its word, never ellipsized, so the track wraps onto a
 * second line rather than clipping a label; a wrapped pill reads as two pills, hence the softer radius. */
const LABELLED_GROUP_SX = { flexWrap: 'wrap', borderRadius: '1rem', maxWidth: '100%' } as const
const LABELLED_WRAP_SX = { flex: '0 0 auto' } as const
const LABELLED_SEGMENT_SX = {
  gap: '0.3125rem',
  whiteSpace: 'nowrap',
  textTransform: 'none',
  fontSize: '0.8125rem',
  lineHeight: 1.2,
} as const

/** A segment's tooltip, out of the way once used: it closes on a click and on the pointer leaving (not
 *  interactive, so hovering the tip does not hold it), and opens ABOVE, clear of the row below (the form's
 *  Advanced toggle, which an open tip under Involvement covered). */
function SegmentTooltip({ title, sx, children }: { title: string, sx: SxProps<Theme>, children: ReactNode }): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <Tooltip
      title={title}
      open={open}
      onOpen={() => setOpen(true)}
      onClose={() => setOpen(false)}
      placement="top"
      disableInteractive
    >
      <Box component="span" sx={sx} onClickCapture={() => setOpen(false)} onPointerLeave={() => setOpen(false)}>
        {children}
      </Box>
    </Tooltip>
  )
}

/** One axis: its positions, drawn. */
function AxisControl<T extends string>({
  axis,
  options,
  value,
  disabled,
  onChange,
  testId,
  rootRef,
  compact = false,
  showLabels = false,
}: {
  /** The axis's name; rendered nowhere, it is the group's accessible name. */
  axis: string
  options: readonly { value: T, label: string, hint: string, Icon: () => JSX.Element }[]
  value: T
  disabled: boolean
  onChange: (next: T) => void
  testId: string
  /** The wrapper, for a menu to anchor on (Depth's Playbook segment). */
  rootRef?: Ref<HTMLDivElement>
  /** Four segments: phone padding drops to keep the row on one line (`COMPACT_SEGMENT_SX`). */
  compact?: boolean
  /** Each segment shows its label beside the icon; the tooltip keeps the hint. */
  showLabels?: boolean
}): JSX.Element {
  // Unlabelled, the exact shape it always had: the regular task form's DOM is pinned by a snapshot.
  const plainSx = compact ? [SEGMENT_SX, COMPACT_SEGMENT_SX] : SEGMENT_SX
  const segmentSx = showLabels ? [SEGMENT_SX, ...(compact ? [COMPACT_SEGMENT_SX] : []), LABELLED_SEGMENT_SX] : plainSx
  return (
    <Box ref={rootRef} data-testid={testId} data-role={testId} sx={{ minWidth: 0, flex: '0 1 auto' }}>
      <ToggleButtonGroup
        exclusive
        size="small"
        value={value}
        disabled={disabled}
        aria-label={axis}
        sx={[GROUP_SX, showLabels && LABELLED_GROUP_SX, disabled && DISABLED_GROUP_SX]}
        /* MUI passes `null` when the selected segment is clicked; swallowed, as there is no "no answer" state. */
        onChange={(_e, next: T | null) => { if (next !== null) onChange(next) }}
      >
        {/* Tooltip-wrapped buttons are safe in MUI v9: the group publishes its props through React context, not
        `cloneElement` on direct children.
        The `<span>` is load-bearing: a disabled button fires no pointer events, so the tooltip (the only
        label) listens on the wrapper and survives a locked form. */}
        {options.map(o => (
          <SegmentTooltip key={o.value} title={`${o.label} — ${o.hint}`} sx={showLabels ? [SEGMENT_WRAP_SX, LABELLED_WRAP_SX] : SEGMENT_WRAP_SX}>
            <ToggleButton
              value={o.value}
              sx={segmentSx}
              data-testid={`${testId}-${o.value}`}
              /* The whole explanation, label first. Needed because the visible content is an unnamed `<svg>`. */
              aria-label={`${o.label} — ${o.hint}`}
            >
              <o.Icon />
              {showLabels && <span data-testid={`${testId}-${o.value}-label`}>{o.label}</span>}
            </ToggleButton>
          </SegmentTooltip>
        ))}
      </ToggleButtonGroup>
    </Box>
  )
}

/* ── PLAYBOOK: Depth's fourth position, when the org runs playbooks ──
 * Not a depth of its own value: picking it opens a menu of the org's playbooks (`origin: 'tenant'` — the
 * built-ins ARE the three depths), and a pick REPLACES the whole control with a pill naming it. The pill's
 * menu offers the three depths back, then the playbooks. The list is fetched only while a menu is open, so a
 * host without the setting (or without a `QueryClient`) never asks.
 */
const PLAYBOOK_OPTION = {
  value: 'playbook',
  label: 'Playbook',
  hint: 'run one of your playbooks.',
  Icon: PlaybookGlyph,
} as const

export const PLAYBOOK_FIXED_HINT = 'Playbook is fixed after save'

/* The pill: the segment track (`GROUP_SX`) holding one raised chip (the selected segment's look), so it reads
 * as the same control in its fourth state. Content-sized up to 14rem and shrinkable (`minWidth: 0`), so the
 * name ellipsizes only when the row has run out of room — never on a fixed per-band cap, which clipped
 * "OAM Feature" to five letters at 320px beside free space. */
const PILL_SX = (theme: Theme) => ({
  ...GROUP_SX(theme),
  flex: '0 1 auto',
  minWidth: 0,
  maxWidth: '14rem',
})

const PILL_CHIP_SX = (theme: Theme) => ({
  display: 'flex',
  alignItems: 'center',
  gap: '0.3125rem',
  minWidth: 0,
  width: '100%',
  minHeight: '2rem',
  px: '0.625rem',
  py: '0.25rem',
  borderRadius: '999px',
  fontSize: '0.8125rem',
  fontWeight: 500,
  lineHeight: 1.2,
  backgroundColor: theme.vars.palette.background.paper,
  color: theme.vars.palette.primary.main,
  boxShadow: '0 1px 2px rgba(0,0,0,0.16)',
  transition: 'box-shadow 140ms',
  '&:hover': { boxShadow: '0 1px 3px rgba(0,0,0,0.24)' },
  '&.Mui-focusVisible': { outline: `2px solid ${theme.vars.palette.primary.main}`, outlineOffset: '1px' },
  '&.Mui-disabled': { color: theme.vars.palette.text.secondary, boxShadow: 'none' },
  [theme.breakpoints.down('sm')]: { minHeight: theme.custom.tapTargetPx, px: '0.5rem' },
  [theme.breakpoints.between(481, 'sm')]: { px: '0.375rem', gap: '0.25rem' },
})

/* Two lines per playbook (name, then its description or why it cannot run), each one line with ellipsis; the
 * full text rides `title`. Capped at the viewport less the 16px gutters. */
const MENU_PAPER_SX = { minWidth: '15rem', maxWidth: 'min(22rem, calc(100vw - 2rem))' } as const
const MENU_TEXT_SX = {
  my: 0,
  '& .MuiListItemText-primary, & .MuiListItemText-secondary': {
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
} as const
const MENU_ICON_SX = { minWidth: '1.75rem !important', color: 'text.secondary' } as const

/** The org's own playbooks as menu rows, in served order. */
function playbookItems(
  query: ReturnType<typeof usePlaybookList>,
  selectedId: string | null,
  onPick: (pb: PlaybookOut) => void,
  testId: string,
): JSX.Element[] {
  if (query.isPending) {
    return [<MenuItem key="loading" disabled dense>Loading…</MenuItem>]
  }
  if (query.isError) {
    return [<MenuItem key="error" disabled dense>Couldn’t load playbooks</MenuItem>]
  }
  const own = query.data.filter(pb => pb.origin === 'tenant')
  if (own.length === 0) {
    return [
      <MenuItem key="empty" disabled dense>No playbooks yet</MenuItem>,
      <MenuItem
        key="create"
        component={RouterLink}
        to={AUTOMATION_TAB_PATHS.playbooks}
        dense
        data-testid={`${testId}-playbook-create`}
      >
        Create one
      </MenuItem>,
    ]
  }
  return own.map(pb => {
    const runnable = pb.runnable !== false
    const secondary = runnable ? pb.description : (pb.not_runnable_reason ?? 'Can’t run yet')
    return (
      <MenuItem
        key={pb.id}
        selected={pb.id === selectedId}
        disabled={!runnable}
        onClick={() => onPick(pb)}
        title={secondary ? `${pb.name} — ${secondary}` : pb.name}
        data-testid={`${testId}-playbook-${pb.id}`}
      >
        <ListItemIcon sx={MENU_ICON_SX}><PlaybookGlyph /></ListItemIcon>
        <ListItemText
          primary={pb.name}
          secondary={secondary || undefined}
          sx={MENU_TEXT_SX}
        />
      </MenuItem>
    )
  })
}

function usePlaybookList() {
  return useQuery({ queryKey: PLAYBOOKS_KEY, queryFn: listPlaybooks })
}

/* The menu, MOUNTED ONLY WHILE OPEN (the caller's `open &&`), so the list is asked for only then. Its rows
 * are direct `MenuItem` children, which `MenuList`'s keyboard focus needs. The pill's menu leads with the
 * three depths, so a playbook can be put back. */
function PlaybookMenu({
  anchorEl,
  withDepths,
  selectedId,
  onDepth,
  onPick,
  onClose,
  testId,
}: {
  anchorEl: HTMLElement | null
  withDepths: boolean
  selectedId: string | null
  onDepth: (next: TaskDepth) => void
  onPick: (pb: PlaybookOut) => void
  onClose: () => void
  testId: string
}): JSX.Element {
  const query = usePlaybookList()
  return (
    <Menu
      open
      anchorEl={anchorEl}
      onClose={onClose}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
      transformOrigin={{ vertical: 'top', horizontal: 'left' }}
      slotProps={{
        paper: { sx: MENU_PAPER_SX },
        list: { dense: true, 'aria-label': withDepths ? 'Depth' : 'Playbooks' },
      }}
      data-testid={`${testId}-menu`}
    >
      {withDepths && TASK_DEPTHS.map(d => (
        <MenuItem key={d.value} onClick={() => onDepth(d.value)} data-testid={`${testId}-menu-${d.value}`}>
          <ListItemIcon sx={MENU_ICON_SX}><d.Icon /></ListItemIcon>
          <ListItemText primary={d.label} sx={MENU_TEXT_SX} />
        </MenuItem>
      ))}
      {withDepths && <Divider />}
      {playbookItems(query, selectedId, onPick, testId)}
    </Menu>
  )
}

/** Depth — sketch · task · epic, and · playbook when the org runs them. */
export function TaskDepthControl({
  value,
  disabled,
  onChange,
  testId = 'task-form-depth',
  offersPlaybook = false,
  playbook = null,
  playbookFixed = false,
  onPlaybook,
  hasRepo = true,
  showLabels = false,
}: {
  value: TaskDepth
  disabled: boolean
  onChange: (next: TaskDepth) => void
  testId?: string
  /** The task's project has a repository; false words the Task position without a PR. */
  hasRepo?: boolean
  /** Offer the fourth segment (`playbooks_enabled`, and a row whose playbook may still be chosen). */
  offersPlaybook?: boolean
  /** The picked playbook: the control renders as its pill. Shown even when not offered (a saved row's). */
  playbook?: TaskPlaybookRef | null
  /** A saved row's playbook: the pill is shown but cannot be opened (the server refuses a change). */
  playbookFixed?: boolean
  onPlaybook?: (next: PlaybookOut) => void
  /** Words beside the icons (`AxisControl`). */
  showLabels?: boolean
}): JSX.Element {
  const anchorRef = useRef<HTMLDivElement>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const close = () => setMenuOpen(false)
  const pickDepth = (next: TaskDepth) => { close(); onChange(next) }
  const pickPlaybook = (next: PlaybookOut) => { close(); onPlaybook?.(next) }
  const pillLocked = disabled || playbookFixed || onPlaybook === undefined

  /* One menu for both states; the pill's adds the three depths on top. */
  const menu = menuOpen && (
    <PlaybookMenu
      anchorEl={anchorRef.current}
      withDepths={playbook !== null}
      selectedId={playbook?.id ?? null}
      onDepth={pickDepth}
      onPick={pickPlaybook}
      onClose={close}
      testId={testId}
    />
  )

  if (playbook !== null) {
    const tip = playbookFixed ? `${playbook.name} — ${PLAYBOOK_FIXED_HINT}` : `Playbook — ${playbook.name}`
    return (
      <Box ref={anchorRef} data-testid={testId} data-role={testId} sx={PILL_SX}>
        <Tooltip title={tip}>
          {/* The `<span>` keeps the tooltip on a disabled chip (see `AxisControl`). */}
          <Box component="span" sx={{ display: 'flex', minWidth: 0, width: '100%' }}>
            <ButtonBase
              sx={PILL_CHIP_SX}
              disabled={pillLocked}
              onClick={() => setMenuOpen(true)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label={`Depth: playbook ${playbook.name}${playbookFixed ? ` — ${PLAYBOOK_FIXED_HINT}` : ''}`}
              data-testid={`${testId}-playbook-pill`}
            >
              <PlaybookGlyph />
              <Box
                component="span"
                data-testid={`${testId}-playbook-name`}
                sx={{ minWidth: 0, flex: '0 1 auto', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              >
                {playbook.name}
              </Box>
              {!pillLocked && <CaretGlyph />}
            </ButtonBase>
          </Box>
        </Tooltip>
        {!pillLocked && menu}
      </Box>
    )
  }

  if (!offersPlaybook || onPlaybook === undefined) {
    return (
      <AxisControl
        axis="Depth"
        options={hasRepo ? TASK_DEPTHS : FOLDER_TASK_DEPTHS}
        value={value}
        disabled={disabled}
        onChange={onChange}
        testId={testId}
        showLabels={showLabels}
      />
    )
  }
  return (
    <>
      <AxisControl<TaskDepth | 'playbook'>
        axis="Depth"
        options={[...(hasRepo ? TASK_DEPTHS : FOLDER_TASK_DEPTHS), PLAYBOOK_OPTION]}
        value={value}
        disabled={disabled}
        /* The fourth segment never holds the value: it opens the menu, and a pick swaps in the pill. */
        onChange={next => { if (next === 'playbook') setMenuOpen(true); else onChange(next) }}
        testId={testId}
        rootRef={anchorRef}
        compact
        showLabels={showLabels}
      />
      {menu}
    </>
  )
}

/** Involvement — unattended · guided · chat (the advisor). */
export function TaskInvolvementControl({
  value,
  disabled,
  offersChat,
  onChange,
  testId = 'task-form-involvement',
  showLabels = false,
}: {
  value: TaskInvolvement
  disabled: boolean
  /* Whether this principal may reach the chat position and this depth offers it; either no omits it.
  · Grant: a chat needs `worktrees:edit` on top of `tasks:create` (`useCanCreateChats`), or save 403s.
  · Depth: `epic` has no conversation to be, so it offers only the first two positions.
  Omitted rather than disabled, which nothing here could explain. Callers still pass `true` for a row
  that is already a chat, so the control never drops its own value. */
  offersChat: boolean
  onChange: (next: TaskInvolvement) => void
  testId?: string
  /** Words beside the icons (`AxisControl`). */
  showLabels?: boolean
}): JSX.Element {
  return (
    <AxisControl
      axis="Involvement"
      options={offersChat ? TASK_INVOLVEMENTS : TASK_INVOLVEMENTS.filter(i => i.value !== 'chat')}
      value={value}
      disabled={disabled}
      onChange={onChange}
      testId={testId}
      showLabels={showLabels}
    />
  )
}
