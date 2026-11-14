import { Fragment, type JSX, type KeyboardEvent, type ReactNode, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import InputBase from '@mui/material/InputBase'
import Stack from '@mui/material/Stack'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  ACTION_LABEL, actionAt, APPROVALS_MAX, type Level, levelFor, rulesOf, stageProblems, type StepAction, type StepRules,
  type Who,
} from '../../lib/pipeline'
import type { ProjectMember } from '../../types'
import { FONT_SIZE, FONT_WEIGHT, grid, LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import type { LookupOption } from '../../ui/AsyncLookup'
import CellLabel from '../../ui/CellLabel'
import DataTable from '../../ui/DataTable'
import { DisabledReason } from '../../ui/DisabledReason'
import { DetailSection } from '../../ui/detail/DetailPage'
import FormTextField from '../../ui/FormTextField'
import { LockFilledIcon, LockOpenIcon } from '../../ui/icons'
import SegmentedControl from '../../ui/SegmentedControl'
import SelectField from '../../ui/SelectField'
import { StatusChip } from '../../ui/StatusChip'
import {
  HOVERLESS_OR_NARROW, PHONE, phoneCardSx, phoneColumnOrder, PIPELINE_STEPS_CONTAINER, PIPELINE_STEPS_NARROW, PIPELINE_STEPS_WIDE,
  tapTargetSx,
} from '../../ui/listStyles'
import WhoPicker from './PipelineWho'
import type { GateDraft, Target } from '../../lib/pipelineGates'

/* THE PIPELINE EDITOR — the stages as a row of identical cards (name, a badge for what its branch means, branch,
 * address) with one + on every arrow and Add stage at the end, and one table with every step (who may do it, approvals
 * needed, by hand or by itself) — or, given `stepPanel`, one panel per step that the host draws. Data-agnostic: it edits `rows` and knows nothing of where they are saved. Two hosts:
 * Project settings › Pipeline (`PipelineSettings.tsx`, which adds the freezes and Save) and an
 * org pipeline template (`pages/pipelineTemplates/`, `template`): there each overridable field carries a lock mark
 * (locked: a linked project can't change it), a stage's branch is a token (each project's work branches, each
 * project's integration branch, a named branch or none), and a step names roles only. A project linked to a template (`linkedField`) keeps the template's stages (no add, move,
 * remove or rename); a field the template locks reads only with a lock mark and "Set by template", an open one that
 * differs from the template says "Changed here" with "Use template value". What the API would refuse in the stages
 * (`lib/pipeline.ts::stageProblems`) shows on the card as you type. */

/** Which fields a linked project may not change (true = locked): a template's lock marks. */
export type Locks = { branch: boolean; url: boolean; who: boolean; approvals: boolean; auto: boolean }
export type LockField = keyof Locks
/** A linked project's field: locked by its template, or open and `changed` from the template's value (`reset` puts the
 *  template's value back in the draft). */
export type LinkedField = { locked: boolean; changed: boolean; reset: () => void }
/** Why a linked project's stages can't be added, moved, removed or renamed. */
export const SET_BY_TEMPLATE = 'Set by template'

/** A new template stage's and step's marks: the project keeps its own branch and address, the template sets the step. */
export const DEFAULT_LOCKS: Locks = { branch: false, url: false, who: true, approvals: true, auto: true }

export type EditorRow = {
  key: string; id?: string; name: string; branch: string; url: string; who: Who | null; approvals: number; auto: boolean
  /** A project's step gates, After-arrival gates and deploy target (`components/pipeline/gates/`): edited by the
   *  project's host through `stageTarget`/`stageFooter`/`stepPanel`, absent on a template. */
  gates?: GateDraft[]
  after?: GateDraft[]
  target?: Target
  /** The step's rules: edited in the project's step panel (`stepPanel`), else sent back unchanged. */
  rules: StepRules
  /** A template's lock marks; absent on a project. */
  locks?: Locks
}

/** A template stage's branch tokens (`apps/api/app/releases/pipeline_doc.py`). */
export const WORK_TOKEN = '*'
export const INTEGRATION_TOKEN = '$integration'

let nextKey = 0
export const newKey = (): string => `new-${++nextKey}`

/** A name not yet taken: "Stage", "Stage 2", … */
export function freshName(rows: readonly { name: string }[]): string {
  const taken = new Set(rows.map(r => r.name.trim().toLowerCase()))
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? 'Stage' : `Stage ${n}`
    if (!taken.has(name.toLowerCase())) return name
  }
}

const CARDS_SX = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'stretch', rowGap: 1.5, minWidth: 0,
  [PHONE]: { flexDirection: 'column', alignItems: 'stretch' },
} as const
const CARD_SX = {
  flex: `1 1 ${grid(30)}`, maxWidth: grid(42), minWidth: 0, p: 1.5, border: '1px solid', borderColor: 'divider',
  borderRadius: RADIUS.md, bgcolor: 'background.paper', display: 'flex', flexDirection: 'column', gap: 1.5,
  '& [data-stage-actions]': { opacity: 0, transition: 'opacity 120ms' },
  '&:hover [data-stage-actions], &:focus-within [data-stage-actions]': { opacity: 1 },
  [HOVERLESS_OR_NARROW]: { '& [data-stage-actions]': { opacity: 1 } },
  [PHONE]: { maxWidth: 'none', flexBasis: 'auto' },
} as const
const ARROW_SX = {
  display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', px: 0.5,
  color: 'text.secondary', flexShrink: 0,
  [PHONE]: { flexDirection: 'row', gap: 1, py: 0.5 },
} as const
const GLYPH_SX = { fontSize: FONT_SIZE.lg, lineHeight: LINE_HEIGHT.tight } as const
/* The table's own width decides its shape (`PIPELINE_STEPS_CONTAINER`), not the viewport's: a 600px window left the
   settings column too narrow for five columns and Checks was cut off at its edge.
   Wide: every column fits (no sideways scroll) — the step's from → to wraps, the check names break anywhere, each cell
   keeps 8px a side instead of MUI's 16px, and How it runs stacks when tight. */
const STEPS_CONTAINER_SX = { containerType: 'inline-size', containerName: PIPELINE_STEPS_CONTAINER, minWidth: 0 } as const
const STEPS_FIT_SX = {
  [PIPELINE_STEPS_WIDE]: {
    '& th, & td': { px: 1 },
    /* Room for two chips a line in Who. */
    '& td[data-col="who"]': { minWidth: grid(22) },
  },
} as const
/* A muted empty field: the placeholder reads as a hint, never as a typed value — a read-only field (MUI `disabled`)
   would otherwise draw it at the disabled text's strength, the same as a value. */
const MUTED_PLACEHOLDER_SX = {
  '& input::placeholder, & textarea::placeholder': { color: 'text.secondary', opacity: 0.8, fontStyle: 'italic' },
  '& input.Mui-disabled::placeholder, & textarea.Mui-disabled::placeholder': { WebkitTextFillColor: 'currentColor', opacity: 0.6 },
} as const
/* Narrow (a phone, or a window that leaves the column under 560px) the steps table restacks as one card per step
   (`ui/listStyles.ts` phone card): the step, who may do it, approvals and how it runs, each value beside its
   heading — no column hangs past the edge. */
const STEPS_TABLE_SX = {
  [PIPELINE_STEPS_NARROW]: {
    ...phoneCardSx(phoneColumnOrder([
      ['step', { flex: '1 1 100%', display: 'block', pt: 1 }],
      ['who', { flex: '1 1 100%' }],
      'approvals',
      'auto',
    ]), { rowGap: 1 })[PHONE],
    '& [data-cell-label]': { display: 'inline' },
  },
} as const
/* The tap floor on a phone for the step's By hand / By itself (the rest is `SegmentedControl`'s default face). */
export const AUTO_BUTTON_SX = { [PHONE]: { minHeight: SIZE.tapTarget } } as const
/* Each option keeps its words on one line; where the column is tight the second stacks under the first (softer radius,
   as `TaskAxisControls`' labelled track) rather than push Checks past the edge. */
const AUTO_TRACK_SX = {
  whiteSpace: 'nowrap',
  [PIPELINE_STEPS_WIDE]: { flexWrap: 'wrap', borderRadius: RADIUS.md, '& > span': { flex: '1 0 auto' } },
} as const
/* A control and its lock mark on one line: the control takes the room, the mark keeps its tap target. */
const WITH_LOCK_SX = { display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0 } as const
/* A stage card's field takes the card's width, its lock mark beside it. */
const FILL_SX = { flex: 1, minWidth: 0, '& .MuiFormControl-root': { width: '100%' } } as const
/* A template's cards are wider, one a row in a narrow pane: "Each project's integration branch" reads whole. */
const TEMPLATE_CARD_SX = { flex: `1 1 ${grid(48)}`, maxWidth: grid(64) } as const
/* An inline control (approvals, how it runs) keeps its own width beside its lock mark. */
const KEEP_SX = { flexShrink: 0 } as const
/* A linked field's note under its control. */
const NOTE_SX = { color: 'text.secondary', fontSize: FONT_SIZE.sm } as const
const CHANGED_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1 } as const
/* A stage's address wraps onto more lines rather than be cut at the card's edge: a narrow card (or a phone, with no
   hover) still reads it whole. It stays one value: Enter adds no line, and a pasted line break is dropped. */
const ADDRESS_SX = { ...MUTED_PLACEHOLDER_SX, '& textarea': { overflowWrap: 'anywhere' } } as const
const ADDRESS_INPUT = {
  inputMode: 'url',
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => { if (e.key === 'Enter') e.preventDefault() },
} as const
const oneLine = (value: string): string => value.replace(/[\r\n]+/g, '')

/* What a stage's branch means, as the badge on its card (`lib/pipeline.ts::levelFor`). */
const LEVEL_BADGE: Record<Level, { label: string; title: string }> = {
  none: { label: 'Queue', title: 'No branch: work waits here until a step moves it on.' },
  work: { label: 'Work branches', title: 'Each task is built on its own branch matching this pattern.' },
  branch: { label: 'Branch', title: 'Work lands on this branch.' },
}

const FIELD_NAME: Record<LockField, string> = { branch: 'Branch', url: 'Address', who: 'Who', approvals: 'Approvals', auto: 'How it runs' }

/** A template field's lock mark: locked, a linked project can't change it; open, it may. */
function LockToggle({ field, where, locked, disabled, onChange }: {
  field: LockField
  /** The stage the field belongs to, for its name. */
  where: string
  locked: boolean
  disabled: boolean
  onChange: (locked: boolean) => void
}): JSX.Element {
  const title = locked ? 'Locked: projects can’t change it' : 'Open: projects can change it'
  return (
    <Tooltip title={title}>
      <span>
        <IconButton size="small" disabled={disabled} aria-pressed={locked} onClick={() => onChange(!locked)}
          aria-label={`Lock ${FIELD_NAME[field]} for ${where}`} data-testid={`template-lock-${field}`} data-locked={locked}
          sx={[tapTargetSx, { color: locked ? 'text.primary' : 'text.secondary' }]}>
          {locked ? <LockFilledIcon /> : <LockOpenIcon />}
        </IconButton>
      </span>
    </Tooltip>
  )
}

/** A linked project's field: the control with a lock mark and "Set by template" when locked; "Changed here" and
 *  "Use template value" when open and different from the template. */
export function LinkedControl({ link, field, where, fill, canReset, children }: {
  link: LinkedField
  field: LockField
  where: string
  fill: boolean
  canReset: boolean
  children: ReactNode
}): JSX.Element {
  return (
    <Box sx={{ minWidth: 0 }} data-testid={`pipeline-linked-${field}`}>
      <Box sx={WITH_LOCK_SX}>
        <Box sx={fill ? FILL_SX : KEEP_SX}>{children}</Box>
        {link.locked && <LockFilledIcon size={SIZE.iconSm} sx={{ color: 'text.secondary' }} />}
      </Box>
      {link.locked ? (
        <Typography variant="body2" sx={NOTE_SX} data-testid="pipeline-field-set-by-template">{SET_BY_TEMPLATE}</Typography>
      ) : link.changed && (
        <Box sx={CHANGED_SX}>
          <Typography variant="body2" sx={NOTE_SX} data-testid="pipeline-field-changed">Changed here</Typography>
          {canReset && (
            <Button size="small" onClick={link.reset} sx={tapTargetSx} data-testid="pipeline-field-use-template"
              aria-label={`Use template value for ${FIELD_NAME[field]} of ${where}`}>
              Use template value
            </Button>
          )}
        </Box>
      )}
    </Box>
  )
}

/** A project stage's work-branch pattern in words, as the template editor names it: "Work branches (fleet/*)", or
 *  "Work branches (any name)" for a bare `*` (a project with no branch prefix). */
export function workBranchText(branch: string): string {
  const text = branch.trim()
  return `Work branches (${text === WORK_TOKEN ? 'any name' : text})`
}

type BranchKind = 'none' | 'work' | 'integration' | 'named'
const BRANCH_KINDS: readonly { value: BranchKind; label: string }[] = [
  { value: 'none', label: 'No branch' },
  { value: 'work', label: 'Each project’s work branches' },
  { value: 'integration', label: 'Each project’s integration branch' },
  { value: 'named', label: 'A named branch' },
]
const kindOf = (branch: string): BranchKind => {
  const text = branch.trim()
  if (!text) return 'none'
  if (text === WORK_TOKEN) return 'work'
  if (text === INTEGRATION_TOKEN) return 'integration'
  return 'named'
}
/** A branch name for a stage switched to "A named branch": its own name, lowercased, not already used. */
function namedBranch(rows: readonly EditorRow[], row: EditorRow): string {
  const base = row.name.trim().toLowerCase().replace(/[^a-z0-9._/-]+/g, '-').replace(/^-+|-+$/g, '') || 'release'
  const taken = new Set(rows.map(r => r.branch.trim()))
  for (let n = 1; ; n += 1) {
    const name = n === 1 ? base : `${base}-${n}`
    if (!taken.has(name)) return name
  }
}

/** A template stage's branch: what kind (each project's own, a named one, none) and, when named, the name. */
function TemplateBranch({ rows, row, disabled, readOnly, onChange }: {
  rows: readonly EditorRow[]
  row: EditorRow
  disabled: boolean
  readOnly: boolean
  onChange: (branch: string) => void
}): JSX.Element {
  /* "A named branch" stays picked while its name is cleared or retyped: deriving it from the text alone would drop the
     field the moment it is empty. */
  const [named, setNamed] = useState(() => kindOf(row.branch) === 'named')
  const kind: BranchKind = named ? 'named' : kindOf(row.branch)
  return (
    <Stack spacing={1} sx={{ minWidth: 0 }}>
      <SelectField id={`template-${row.key}-branch-kind`} label="Branch" value={kind} disabled={disabled || readOnly}
        testId="template-stage-branch-kind" options={BRANCH_KINDS.map(k => ({ value: k.value, label: k.label }))}
        onChange={v => {
          setNamed(v === 'named')
          onChange(v === 'none' ? '' : v === 'work' ? WORK_TOKEN : v === 'integration' ? INTEGRATION_TOKEN : namedBranch(rows, row))
        }} />
      {kind === 'named' && (
        <FormTextField id={`template-${row.key}-branch`} label="Branch name" value={row.branch} onChange={onChange}
          readOnly={readOnly} disabled={disabled} testId="pipeline-stage-branch" />
      )}
    </Stack>
  )
}

export default function PipelineEditor({
  rows, setRows, canEdit, busy, defaults, names = {}, onName, members, errors, warnings, stagesActions, branchLocked,
  stepPanel, template = false, linkedField, stageMark, stageTarget, addressOf, stageFooter,
}: {
  rows: readonly EditorRow[]
  /** Every edit, as an update of the rows. */
  setRows: (next: (rows: EditorRow[]) => EditorRow[]) => void
  canEdit: boolean
  busy: boolean
  /** Each action's default roles: what `who: null` resolves to. */
  defaults: Partial<Record<StepAction, string[]>>
  /** The named users' names, by id. */
  names?: Record<string, string>
  onName?: (option: LookupOption) => void
  /** Whom a step may name; absent, roles only. */
  members?: () => Promise<ProjectMember[]>
  /** A message on a stage's card, by row key (a refused save's). What `stageProblems` finds as you type wins. */
  errors?: ReadonlyMap<string, string>
  /** Org rule warnings on a stage's card, by row key. */
  warnings?: ReadonlyMap<string, readonly string[]>
  /** The Stages section's header actions. */
  stagesActions?: ReactNode
  /** A stage whose branch the viewer may not change (`PipelineSettings.tsx`: the project's governance fields). */
  branchLocked?: (index: number) => boolean
  /** Each step as its own panel in place of the steps table (`PipelineStepPanel.tsx`, the project's every step rule);
   *  absent (a template), the table. `patch` edits the step's row. */
  stepPanel?: (row: EditorRow, index: number, action: StepAction, patch: (patch: Partial<EditorRow>) => void) => ReactNode
  /** A template: lock marks, branch tokens, roles only. */
  template?: boolean
  /** A project linked to an org template: its stages are the template's, and each field is locked or open. */
  linkedField?: (index: number, field: LockField) => LinkedField
  /** A mark before a stage's name on its card (`PipelineSettings.tsx`: the stage's health dot). */
  stageMark?: (row: EditorRow) => ReactNode
  /** A stage card's deploy target, drawn above its Address (the project's host). */
  stageTarget?: (row: EditorRow, index: number) => ReactNode
  /** Whether a stage's Address reads only (a target reports it) and the line naming where it came from. */
  addressOf?: (row: EditorRow, index: number) => { readOnly: boolean; source: string } | undefined
  /** A line at the foot of a stage card (its After-arrival gates). */
  stageFooter?: (row: EditorRow, index: number) => ReactNode
}): JSX.Element {
  const levels = rows.map(r => levelFor(r.branch))
  const off = !canEdit || busy
  const problems = stageProblems(rows)
  /** Why stages can't be added, moved, removed or renamed: linked to a template. */
  const structure = linkedField ? SET_BY_TEMPLATE : undefined
  const lockedHere = (index: number, field: LockField) => linkedField?.(index, field).locked === true
  const patchRow = (key: string, patch: Partial<EditorRow>) => setRows(rs => rs.map(r => (r.key === key ? { ...r, ...patch } : r)))
  const lockRow = (row: EditorRow, field: LockField, locked: boolean) =>
    patchRow(row.key, { locks: { ...(row.locks ?? DEFAULT_LOCKS), [field]: locked } })
  const insertAt = (index: number) => setRows(rs => [
    ...rs.slice(0, index),
    { key: newKey(), name: freshName(rs), branch: '', url: '', who: null, approvals: 0, auto: false, rules: rulesOf(undefined),
      ...(template ? { locks: { ...DEFAULT_LOCKS } } : {}) },
    ...rs.slice(index),
  ])
  const move = (index: number, by: -1 | 1) => setRows(rs => {
    const next = [...rs]
    const [row] = next.splice(index, 1)
    next.splice(index + by, 0, row)
    return next
  })
  /** `control` with the row's lock mark for `field` beside it, on a template (on a linked project, the template's lock
   *  or "Changed here"); `fill`: the control takes the width. */
  const withLock = (row: EditorRow, index: number, field: LockField, control: ReactNode, { fill = false, show = true } = {}) => {
    const link = linkedField?.(index, field)
    if (link) {
      return (
        <LinkedControl link={link} field={field} where={row.name || 'this stage'} fill={fill} canReset={canEdit && !busy}>
          {control}
        </LinkedControl>
      )
    }
    if (!template) return control
    return (
      <Box sx={WITH_LOCK_SX}>
        <Box sx={fill ? FILL_SX : KEEP_SX}>{control}</Box>
        {show && (
          <LockToggle field={field} where={row.name || 'this stage'} locked={(row.locks ?? DEFAULT_LOCKS)[field]}
            disabled={off} onChange={on => lockRow(row, field, on)} />
        )}
      </Box>
    )
  }

  /* Linked to a template, the stages are the template's: no add, move or remove (the linked line above says why). */
  const reshape = canEdit && structure === undefined
  const plus = (index: number) => reshape && (
    <Tooltip title="Add a stage here">
      <span>
        <IconButton size="small" aria-label="Add stage here" disabled={off || rows.length >= 50} onClick={() => insertAt(index)}
          data-testid="pipeline-stage-add" sx={tapTargetSx}>
          +
        </IconButton>
      </span>
    </Tooltip>
  )

  return (
    <>
      <DetailSection title="Stages" testId="pipeline-stages" actions={stagesActions}
        help={template ? 'Locked fields are set by the template; projects can change open ones.' : undefined}>
        <Box sx={CARDS_SX}>
          {rows.map((r, index) => {
            const error = problems.get(index) ?? errors?.get(r.key)
            const badge = LEVEL_BADGE[levels[index]]
            const stageWarnings = !error ? warnings?.get(r.key) ?? [] : []
            const ownBranch = kindOf(r.branch) === 'work' || kindOf(r.branch) === 'integration'
            const locked = canEdit && !template && branchLocked?.(index) === true
            /* A work-branch pattern reads in words where it can't be typed into; a bare `*` typed into gets them under it. */
            const branchReadOnly = !canEdit || locked || lockedHere(index, 'branch')
            const workWords = levels[index] === 'work' ? workBranchText(r.branch) : undefined
            const address = addressOf?.(r, index)
            return (
              <Fragment key={r.key}>
                {index > 0 && (
                  <Box sx={ARROW_SX} aria-hidden={!reshape}>
                    <Box component="span" sx={[GLYPH_SX, { [PHONE]: { transform: 'rotate(90deg)' } }]} aria-hidden>→</Box>
                    {plus(index)}
                  </Box>
                )}
                <Box sx={[CARD_SX, template && TEMPLATE_CARD_SX, stageWarnings.length > 0 && { borderColor: 'warning.main' }, Boolean(error) && { borderColor: 'error.main' }]}
                  data-testid="pipeline-stage" role="group"
                  aria-label={r.name || 'Stage'}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0 }}>
                    {stageMark?.(r)}
                    <InputBase value={r.name} onChange={e => patchRow(r.key, { name: e.target.value })} readOnly={!canEdit || structure !== undefined}
                      disabled={busy} inputProps={{ 'aria-label': 'Stage name', 'data-testid': 'pipeline-stage-name', maxLength: 120 }}
                      sx={{ flex: 1, minWidth: 0, fontWeight: FONT_WEIGHT.semibold, '& input': { py: 0, textOverflow: 'ellipsis' },
                        [PHONE]: { '& input': { minHeight: SIZE.tapTarget, boxSizing: 'border-box' } } }} />
                    {reshape && (
                      <Box data-stage-actions sx={{ display: 'flex', flexShrink: 0, [PHONE]: { '& [data-move]': { transform: 'rotate(90deg)' } } }}>
                        <Tooltip title="Earlier"><span>
                          <IconButton size="small" aria-label={`Move ${r.name} earlier`} disabled={off || index === 0}
                            onClick={() => move(index, -1)} data-move data-testid="pipeline-stage-left">‹</IconButton>
                        </span></Tooltip>
                        <Tooltip title="Later"><span>
                          <IconButton size="small" aria-label={`Move ${r.name} later`} disabled={off || index === rows.length - 1}
                            onClick={() => move(index, 1)} data-move data-testid="pipeline-stage-right">›</IconButton>
                        </span></Tooltip>
                        <Tooltip title="Remove"><span>
                          <IconButton size="small" aria-label={`Remove ${r.name}`} disabled={off || rows.length <= 1}
                            onClick={() => setRows(rs => rs.filter(x => x.key !== r.key))} data-testid="pipeline-stage-remove">✕</IconButton>
                        </span></Tooltip>
                      </Box>
                    )}
                  </Box>
                  {/* Its own line under the name: beside it, a long name would be cut to make room. */}
                  <Box sx={{ display: 'flex', mt: -1 }} data-testid="pipeline-stage-kind" data-level={levels[index]}>
                    <StatusChip label={badge.label} title={badge.title} ariaLabel={`${badge.label}: ${badge.title}`}
                      tone={levels[index] === 'none' ? 'neutral' : 'info'} />
                  </Box>
                  {template ? withLock(r, index, 'branch', (
                    <TemplateBranch rows={rows} row={r} disabled={busy} readOnly={!canEdit}
                      onChange={branch => patchRow(r.key, { branch })} />
                  ), { fill: true, show: !ownBranch }) : withLock(r, index, 'branch', (
                    <DisabledReason reason={locked ? 'Only an owner, or an admin with node-edit permission, can change it.' : undefined}>
                      <FormTextField id={`pipeline-${r.key}-branch`} label="Branch"
                        value={branchReadOnly && workWords ? workWords : r.branch}
                        placeholder="No branch" sx={MUTED_PLACEHOLDER_SX} hint={!branchReadOnly && r.branch.trim() === WORK_TOKEN ? workWords : undefined}
                        onChange={branch => patchRow(r.key, { branch })} readOnly={branchReadOnly}
                        disabled={busy}
                        help="Empty: work moves here. A pattern like fleet/*: work is built here. A name: work merges into it."
                        testId="pipeline-stage-branch" />
                    </DisabledReason>
                  ), { fill: true })}
                  {stageTarget?.(r, index)}
                  {withLock(r, index, 'url', (
                    <FormTextField id={`pipeline-${r.key}-url`} label="Address" multiline value={r.url}
                      placeholder={canEdit && !lockedHere(index, 'url') && !address?.readOnly ? 'Add address' : 'No address'} sx={ADDRESS_SX}
                      slotProps={{ htmlInput: ADDRESS_INPUT }} onChange={url => patchRow(r.key, { url: oneLine(url) })} readOnly={!canEdit || lockedHere(index, 'url') || address?.readOnly === true} disabled={busy}
                      hint={address?.source} help={template ? 'Empty: each project sets its own.' : 'Where this stage is running.'} testId="pipeline-stage-url" />
                  ), { fill: true })}
                  {stageFooter?.(r, index)}
                  {error && (
                    <Typography variant="body2" color="error" aria-live="polite" data-testid="pipeline-stage-error">{error}</Typography>
                  )}
                  {stageWarnings.map(w => (
                    <Typography key={w} variant="body2" sx={{ color: 'warning.dark' }} data-testid="pipeline-stage-warning">{w}</Typography>
                  ))}
                </Box>
              </Fragment>
            )
          })}
          {reshape && (
            <Box sx={[ARROW_SX, { [PHONE]: { justifyContent: 'flex-start' } }]}>
              <Button size="small" variant="outlined" disabled={off || rows.length >= 50}
                onClick={() => insertAt(rows.length)} data-testid="pipeline-stage-add-end" sx={tapTargetSx}>
                Add stage
              </Button>
            </Box>
          )}
        </Box>
      </DetailSection>

      <DetailSection title="Steps" divided testId="pipeline-steps">
        {stepPanel ? (
          <Stack spacing={1} data-testid="pipeline-steps-list">
            {rows.slice(1).map((r, i) => (
              <Fragment key={r.key}>{stepPanel(r, i + 1, actionAt(levels, i + 1), patch => patchRow(r.key, patch))}</Fragment>
            ))}
          </Stack>
        ) : (
        <Box sx={STEPS_CONTAINER_SX}>
        <Box sx={[STEPS_FIT_SX, STEPS_TABLE_SX]}>
        <DataTable ariaLabel="Steps" testId="pipeline-steps-table">
          <TableHead>
            <TableRow>
              <TableCell>Step</TableCell>
              <TableCell>Who</TableCell>
              <TableCell>Approvals</TableCell>
              <TableCell>How it runs</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.slice(1).map((r, i) => {
              const index = i + 1
              const action: StepAction = actionAt(levels, index)
              const who = r.who ?? { roles: defaults[action] ?? [], users: [] }
              const label = ACTION_LABEL[action]
              return (
                <TableRow key={r.key} data-testid="pipeline-step">
                  <TableCell data-col="step">
                    <Box sx={{ fontWeight: FONT_WEIGHT.semibold }} data-testid="pipeline-step-name">{label}</Box>
                    <Box sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>
                      {rows[index - 1].name} → {r.name}
                    </Box>
                  </TableCell>
                  <TableCell data-col="who">
                    <CellLabel>Who</CellLabel>
                    {withLock(r, index, 'who', (
                      <WhoPicker who={who} names={names} title={`Who can ${label.toLowerCase()}`} disabled={off || lockedHere(index, 'who')} members={members}
                        onName={o => onName?.(o)} onChange={next => patchRow(r.key, { who: next })} />
                    ), { fill: true })}
                  </TableCell>
                  <TableCell data-col="approvals">
                    <CellLabel>Approvals</CellLabel>
                    {withLock(r, index, 'approvals', (
                      <SelectField id={`pipeline-${r.key}-approvals`} ariaLabel={`Approvals needed to ${label.toLowerCase()}`}
                        value={String(r.approvals)} onChange={v => patchRow(r.key, { approvals: Number(v) })}
                        disabled={off || lockedHere(index, 'approvals')} dense
                        testId="pipeline-step-approvals"
                        options={Array.from({ length: APPROVALS_MAX + 1 }, (_, n) => ({ value: String(n), label: String(n) }))} />
                    ))}
                  </TableCell>
                  <TableCell data-col="auto">
                    {withLock(r, index, 'auto', (
                      <SegmentedControl ariaLabel={`How ${label.toLowerCase()} runs`} value={r.auto ? 'auto' : 'hand'}
                        disabled={off || lockedHere(index, 'auto')}
                        testId="pipeline-step-auto" sx={AUTO_TRACK_SX} buttonSx={AUTO_BUTTON_SX}
                        onChange={v => patchRow(r.key, { auto: v === 'auto' })}
                        options={[
                          { value: 'hand', label: 'By hand', help: 'Someone presses it.', testId: 'pipeline-step-by-hand' },
                          { value: 'auto', label: 'By itself', help: 'Runs as soon as work arrives.', testId: 'pipeline-step-by-itself' },
                        ]} />
                    ), { fill: true })}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </DataTable>
        </Box>
        </Box>
        )}
      </DetailSection>
    </>
  )
}
