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
import type { Theme } from '@mui/material/styles'
import { listPlaybooks, PLAYBOOKS_KEY, type PlaybookOut } from '../../pages/playbooks/api'
import { CUSTOMIZE_TAB_PATHS } from '../../routeGroups'
import type { TaskPlaybookRef } from '../../types'
import { Icon } from '../../ui/icons'
import SegmentedControl, { SEGMENT_TRACK_GROUND } from '../../ui/SegmentedControl'
import ChatGlyph from './ChatGlyph'
import {
  ADVISOR_PERSONA,
  DEFAULT_PERSONA,
  isEpicPersona,
  PROTOTYPER_PERSONA,
} from './taskPersona'
import { ELLIPSIS_SX } from '../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import Tip from '../../ui/Tip'

/* ── The task form's two axes: Depth (sketch · task · epic) and Involvement (unattended · guided · chat) ──
 * The hierarchy is Depth → Involvement → Persona → Model, and a press writes only what is below it:
 * Depth seeds all three; Involvement writes `autonomous` (and, at `task` depth, the advisor persona);
 * Persona seeds only Model. No upward edge.
 * Nothing is disabled; only `epic` hides the persona and model selects (an epic is never dispatched).
 * Wordless below `md` by operator instruction: positions are drawn glyphs, with every word in a tooltip and
 * accessible name from the same table, so the row fits one line down to 320px; from `md` each word shows beside
 * its glyph (`DESKTOP_LABEL_SX`).
 * Glyphs are drawn `<svg>`: `@mui/icons-material` may not become a dependency (`RatingBadge`'s note),
 * and an emoji ignores `color`, losing the selected tint. Chat reuses `ChatGlyph`.
 */

// ── The glyph set ──
/* Sized in `em` and painted on `currentColor` (as `ChatGlyph`), so they follow the segment's state.
 * `aria-hidden`: the button carries the accessible name. */
function Glyph({ children, testId }: { children: ReactNode, testId: string }): JSX.Element {
  return (
    <Icon size={SIZE.iconXs} testId={testId} sx={{ display: 'block', flex: '0 0 auto' }}>
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
    <Icon size={grid(1.5)} sx={{ display: 'block', flex: '0 0 auto', opacity: 0.7 }}>
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
    Icon: () => <ChatGlyph size={SIZE.iconXs} />,
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


/* ── The axes' metrics on the shared track (`ui/SegmentedControl`) ──
 * Content-sized and shrinkable (`flex: 0 1 auto`, segments `minWidth: 0`), since everything outside
 * `Advanced` shares one line; at 320px the glyph cells are the only free give.
 * Tap floor on height only: `SIZE.thumb` on pointer devices, 44px at ≤sm. The chip sits inset in the track.
 */
const AXIS_TRACK_SX = { p: 0.5, gap: 0 } as const

const SEGMENT_SX = (theme: Theme) => ({
  border: 0, // every pixel of the one-line row is spoken for
  minHeight: SIZE.thumb,
  px: 1.5,
  py: 0.5,
  fontSize: FONT_SIZE.md,
  [theme.breakpoints.down('sm')]: { minHeight: theme.custom.tapTargetPx, px: 1 },
  /* The squeezed band: the 212px sidebar appears at 481px, collapsing the form column to ~245px (narrower
  than at 320px) until ~560px, where the row's last member (the 44px Attach paperclip) wrapped. Taken from
  padding only; the glyph and 44px height stay. */
  [theme.breakpoints.between(481, 'sm')]: { px: 0.5 },
})

/* A fourth Depth segment costs a cell; in the squeezed band it is taken from padding again, never the glyph or the
 * 44px height. */
const COMPACT_SEGMENT_SX = (theme: Theme) => ({
  [theme.breakpoints.between(481, 'sm')]: { px: 0.5 },
})

/* Labelled segments (`showLabels`): each one icon plus its word, never ellipsized, so the track wraps onto a
 * second line rather than clipping a label; a wrapped pill reads as two pills, hence the softer radius. */
const LABELLED_TRACK_SX = { flexWrap: 'wrap', borderRadius: RADIUS.md, '& > span': { flex: '0 0 auto' } } as const
const LABELLED_SEGMENT_SX = {
  gap: 1,
  whiteSpace: 'nowrap',
  fontSize: FONT_SIZE.md,
  lineHeight: LINE_HEIGHT.snug,
} as const

/* Wordless segments (`!showLabels`) show their word too from `md` up, where the row has the room: "Epic" is found
 * without hovering. Below `md` the word is `display: none`, so the 320px row keeps its one line. From `md` the axis
 * keeps its width (`0 0 auto`): the row wraps a member rather than clip a word. */
const DESKTOP_LABEL_SX = { display: { xs: 'none', md: 'inline' } } as const
const DESKTOP_LABEL_SEGMENT_SX = (theme: Theme) => ({ [theme.breakpoints.up('md')]: { gap: 1, whiteSpace: 'nowrap' } })
const DESKTOP_LABEL_AXIS_SX = (theme: Theme) => ({ [theme.breakpoints.up('md')]: { flex: '0 0 auto' } })

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
  return (
    <Box ref={rootRef} data-testid={testId} data-role={testId} sx={[{ minWidth: 0, flex: '0 1 auto' }, !showLabels && DESKTOP_LABEL_AXIS_SX]}>
      <SegmentedControl<T>
        value={value}
        disabled={disabled}
        ariaLabel={axis}
        onChange={onChange}
        sx={[AXIS_TRACK_SX, showLabels && LABELLED_TRACK_SX]}
        buttonSx={[SEGMENT_SX, compact && COMPACT_SEGMENT_SX, showLabels ? LABELLED_SEGMENT_SX : DESKTOP_LABEL_SEGMENT_SX]}
        /* The whole explanation, label first, is the name and the tooltip: the visible content is an unnamed `<svg>`. */
        options={options.map(o => ({
          value: o.value,
          testId: `${testId}-${o.value}`,
          ariaLabel: `${o.label} — ${o.hint}`,
          help: `${o.label} — ${o.hint}`,
          label: (
            <>
              <o.Icon />
              {showLabels
                ? <span data-testid={`${testId}-${o.value}-label`}>{o.label}</span>
                : <Box component="span" data-testid={`${testId}-${o.value}-label`} sx={DESKTOP_LABEL_SX}>{o.label}</Box>}
            </>
          ),
        }))}
      />
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

/* The pill: the segment track (`ui/SegmentedControl`'s look) holding one raised chip (the selected segment's look), so it reads
 * as the same control in its fourth state. Content-sized up to 14rem and shrinkable (`minWidth: 0`), so the
 * name ellipsizes only when the row has run out of room — never on a fixed per-band cap, which clipped
 * "OAM Feature" to five letters at 320px beside free space. */
const PILL_SX = (theme: Theme) => ({
  display: 'inline-flex',
  p: 0.5,
  borderRadius: RADIUS.round,
  backgroundColor: SEGMENT_TRACK_GROUND,
  outline: `1px solid ${theme.vars.palette.divider}`,
  outlineOffset: -1,
  flex: '0 1 auto',
  minWidth: 0,
  maxWidth: grid(37),
})

const PILL_CHIP_SX = (theme: Theme) => ({
  display: 'flex',
  alignItems: 'center',
  gap: 0.5,
  minWidth: 0,
  width: '100%',
  minHeight: SIZE.thumb,
  px: 1.5,
  py: 0.5,
  borderRadius: RADIUS.round,
  fontSize: FONT_SIZE.md,
  fontWeight: FONT_WEIGHT.medium,
  lineHeight: LINE_HEIGHT.snug,
  backgroundColor: theme.vars.palette.background.paper,
  color: theme.vars.palette.primary.main,
  boxShadow: 1,
  transition: 'box-shadow 140ms',
  '&:hover': { boxShadow: 2 },
  '&.Mui-focusVisible': { outline: `2px solid ${theme.vars.palette.primary.main}`, outlineOffset: '1px' },
  '&.Mui-disabled': { color: theme.vars.palette.text.secondary, boxShadow: 'none' },
  [theme.breakpoints.down('sm')]: { minHeight: theme.custom.tapTargetPx, px: 1 },
  [theme.breakpoints.between(481, 'sm')]: { px: 1, gap: 0.5 },
})

/* Two lines per playbook (name, then its description or why it cannot run), each one line with ellipsis; the
 * full text rides `title`. Capped at the viewport less the 16px gutters. */
const MENU_PAPER_SX = { minWidth: grid(40), maxWidth: `min(${grid(58.5)}, calc(100vw - ${grid(5)}))` } as const
const MENU_TEXT_SX = {
  my: 0,
  '& .MuiListItemText-primary, & .MuiListItemText-secondary': ELLIPSIS_SX,
} as const
const MENU_ICON_SX = { minWidth: `${grid(4.5)} !important`, color: 'text.secondary' } as const

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
        to={CUSTOMIZE_TAB_PATHS.playbooks}
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
      <Tip key={pb.id} title={secondary ? `${pb.name} — ${secondary}` : pb.name}><MenuItem
        selected={pb.id === selectedId}
        disabled={!runnable}
        onClick={() => onPick(pb)}
        data-testid={`${testId}-playbook-${pb.id}`}
      >
        <ListItemIcon sx={MENU_ICON_SX}><PlaybookGlyph /></ListItemIcon>
        <ListItemText
          primary={pb.name}
          secondary={secondary || undefined}
          sx={MENU_TEXT_SX}
        />
      </MenuItem></Tip>
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
        <Tip describeChild={false} title={tip}>
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
                sx={{ ...ELLIPSIS_SX, flex: '0 1 auto' }}
              >
                {playbook.name}
              </Box>
              {!pillLocked && <CaretGlyph />}
            </ButtonBase>
          </Box>
        </Tip>
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
