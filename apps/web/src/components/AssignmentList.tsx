import { type JSX, useId, useLayoutEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Popover from '@mui/material/Popover'
import SvgIcon from '@mui/material/SvgIcon'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useStaticSlotsCap } from '../auth'
import type { AssignmentNote } from '../hooks/useAssignments'
import type { AssignmentDraft, AssignmentItem, AssignmentSide } from '../lib/assignments'
import { removalWarning } from '../lib/assignments'
import Banner from '../ui/Banner'
import { type ConfirmOptions, useConfirm } from './ConfirmDialog'
import { MicrovmChip } from '../ui/MarkChip'
import FeedList from '../ui/feed/FeedList'
import { matchesAny } from '../ui/FilterRow'
import type { FeedRowSpec, FeedSection } from '../ui/feed/types'
import InfoTip from '../ui/InfoTip'
import HelpLabel from '../ui/HelpLabel'
import LoadingState from '../ui/LoadingState'
import SearchBox from '../ui/SearchBox'
import { VISUALLY_HIDDEN } from '../ui/visuallyHidden'

/* The assignment pick list: which nodes run a project, or which projects a node runs — the node page's
Projects tab and the project page's Nodes. ONE LIST, the console's feed rows: the disc is the checkbox (only the disc presses it; a row click changes nothing), and a tick
APPLIES AT ONCE (`hooks/useAssignments.ts`, no Save). Assigned rows sort to the top under their own heading the
moment they are ticked; a search narrows both groups. Removing a row whose slots are in use asks first, inline,
because it deletes them; an idle one goes straight away. What blocks the node (the warning) and a failed write sit above
the rows, so a reader sees them without scrolling. Trailing, icons over words: ▶ n for slots in use, and the
static-slots override as the gear that sets it (blank = none). A group-carried row is ticked and locked (D14). */

const MAX_STATIC = 10_000

const TUNE = 'M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z'

export type AssignmentListProps = {
  /** The field's label: "Projects" on a node, "Nodes" on a project. */
  label: string
  /** A few words shown on hovering the label (`ui/HelpLabel`). */
  help?: string
  /** Which page draws it: a project lists nodes, a node lists projects. */
  side: AssignmentSide
  /** Stable id prefix for the list and test handles. */
  idPrefix: string
  rows: AssignmentDraft | null
  /** Rows with a write in flight. */
  busy: ReadonlySet<string>
  /** What the last write did, or why it failed. */
  note: AssignmentNote | null
  onAssign: (id: string) => void
  onUnassign: (id: string) => void
  onOverride: (id: string, staticSlots: number | null) => void
  readOnly?: boolean
  loading?: boolean
  /** The read failed: a banner in place of the list. */
  error?: string | null
  /** The one line naming what a removal destroys (D9). */
  destructiveHelp: string
  /** A non-blocking warning above the list (D4). */
  warning?: string | null
  /** Offer the static-slots override; false while the org's plan allows none (`auth.tsx::useStaticSlotsOffered`). */
  staticSlots?: boolean
}

export function AssignmentList({
  label,
  help,
  side,
  idPrefix,
  rows,
  busy,
  note,
  onAssign,
  onUnassign,
  onOverride,
  readOnly = false,
  loading = false,
  error,
  destructiveHelp,
  warning,
  staticSlots = true,
}: AssignmentListProps): JSX.Element {
  const labelId = useId()
  const [search, setSearch] = useState('')
  const [confirm, confirmDialog] = useConfirm()
  const [advanced, setAdvanced] = useState<{ id: string; anchor: HTMLElement } | null>(null)
  const noun = side === 'project' ? 'nodes' : 'projects'
  const rootRef = useRef<HTMLDivElement | null>(null)
  /* A ticked row jumps to the other group, a different parent, so its disc remounts and focus would fall to
     <body>: follow the row instead, so a keyboard user keeps their place. */
  const [follow, setFollow] = useState<string | null>(null)
  useLayoutEffect(() => {
    if (follow === null) return
    const disc = rootRef.current?.querySelector<HTMLElement>(`[data-testid="feed-node"][data-row="${CSS.escape(follow)}"]`)
    if (disc && document.activeElement !== disc) disc.focus()
    setFollow(null)
  }, [follow, rows])
  const assign = (id: string) => { setFollow(id); onAssign(id) }
  const unassign = (id: string) => { setFollow(id); onUnassign(id) }

  const needle = search.trim().toLowerCase()
  const match = (name: string) => matchesAny(search, name)

  async function untick(item: AssignmentItem) {
    // In use: deleting it kills work, so ask. Idle: it goes now.
    if (item.held === 0 && item.live.length === 0) { unassign(item.id); return }
    if (await confirm(removeConfirm(item))) unassign(item.id)
  }

  // The consequences as node Delete's confirm states them: what dies, listed by name.
  function removeConfirm(item: AssignmentItem): ConfirmOptions {
    const { line, names } = removalWarning(item)
    return {
      title: `Remove ${item.name}?`,
      message: (
        <>
          <span>{line}</span>
          {names.length > 0 && (
            <Box component="ul" data-testid={`${idPrefix}-confirm-names`} sx={{ m: 0, pl: 2.5, overflowWrap: 'anywhere' }}>
              {names.map(name => <li key={name}>{name}</li>)}
            </Box>
          )}
        </>
      ),
      confirmLabel: 'Remove',
      cancelLabel: 'Keep',
      destructive: true,
    }
  }

  function assignedRow(item: AssignmentItem): FeedRowSpec {
    const group = item.viaGroup?.name
    const isBusy = busy.has(item.id)
    const pressable = !readOnly && item.removable
    return {
      key: item.id,
      title: item.name,
      titleText: item.name,
      titleAside: item.microvm ? <MicrovmChip /> : undefined,
      busy: isBusy,
      indicator: {
        tone: 'success',
        glyph: isBusy ? '…' : '✓',
        label: group ? `Assigned via group ${group}` : 'Assigned',
        checked: true,
        pressLabel: item.name,
        title: group ? `Via group ${group}: remove it at the group` : `Remove ${item.name}`,
        onPress: pressable ? () => untick(item) : undefined,
        // Pointed at, the tick shows the press: ✕ removes (`ui/pressSwap.tsx`).
        pressGlyph: isBusy ? undefined : '✕',
        data: { assigned: true, row: item.id },
      },
      markers: group ? [{ key: 'group', glyph: '◆', label: `Via group ${group}`, title: `Via group ${group}: remove it at the group`, testId: `${idPrefix}-group` }] : undefined,
      slots: [
        {
          key: 'held',
          width: 36,
          align: 'right',
          node: item.held > 0 ? <span>▶ {item.held}</span> : null,
          title: item.held > 0 ? `${item.held} in use` : undefined,
          testId: `${idPrefix}-held`,
        },
        ...(staticSlots ? [{
          key: 'static',
          width: 52,
          align: 'right' as const,
          compact: true,
          title: `Static slots: ${item.staticSlots ?? 'none'}`,
          node: (
            <Box
              component="button"
              type="button"
              data-testid={`${idPrefix}-gear`}
              aria-label={`Static slots for ${item.name}: ${item.staticSlots ?? 'none'}`}
              aria-haspopup="dialog"
              disabled={readOnly}
              onClick={e => { e.stopPropagation(); setAdvanced({ id: item.id, anchor: e.currentTarget }) }}
              sx={{
                display: 'inline-flex', alignItems: 'center', gap: 0.25, px: 0.5, py: 0, height: 22,
                border: 1, borderColor: 'divider', borderRadius: 1, bgcolor: 'transparent', cursor: 'pointer',
                font: 'inherit', fontVariantNumeric: 'tabular-nums',
                color: item.staticSlots !== null ? 'primary.main' : 'text.secondary',
                '&:hover': { bgcolor: 'action.hover' },
                '&:disabled': { cursor: 'default', opacity: 0.6 },
                '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 1 },
              }}
            >
              <Box component="span" data-testid={`${idPrefix}-static`}>{item.staticSlots ?? ''}</Box>
              <SvgIcon aria-hidden sx={{ fontSize: 14 }}><path d={TUNE} /></SvgIcon>
            </Box>
          ),
        }] : []),
      ],
      data: { assigned: true },
    }
  }

  function availableRow(candidate: { id: string; name: string; microvm?: boolean }): FeedRowSpec {
    return {
      key: candidate.id,
      title: candidate.name,
      titleText: candidate.name,
      titleAside: candidate.microvm ? <MicrovmChip /> : undefined,
      busy: busy.has(candidate.id),
      indicator: {
        tone: 'neutral',
        glyph: busy.has(candidate.id) ? '…' : '',
        label: 'Not assigned',
        checked: false,
        pressLabel: candidate.name,
        title: `Add ${candidate.name}`,
        onPress: readOnly ? undefined : () => assign(candidate.id),
        pressGlyph: busy.has(candidate.id) ? undefined : '+',
        data: { assigned: false, row: candidate.id },
      },
      slots: [{ key: 'held', width: 36, node: null }, ...(staticSlots ? [{ key: 'static', width: 52, node: null }] : [])],
      data: { assigned: false },
    }
  }

  let body: JSX.Element
  if (error) {
    body = <Banner severity="error">{error}</Banner>
  } else if (loading || !rows) {
    body = <LoadingState />
  } else {
    const assigned = rows.selected.filter(item => match(item.name))
    const available = rows.available.filter(c => match(c.name))
    const sections: FeedSection[] = []
    if (assigned.length > 0 || (!needle && rows.available.length === 0)) {
      sections.push({ key: 'assigned', label: 'Assigned', count: assigned.length, rows: assigned.map(assignedRow) })
    }
    if (available.length > 0) {
      sections.push({ key: 'available', label: 'Available', count: available.length, rows: available.map(availableRow) })
    }
    const total = rows.selected.length + rows.available.length
    body = (
      <>
        {total > 0 && (
          // Enter must not submit the page's form around it (the project editor).
          <Box onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }} sx={{ mb: 0.75 }}>
            <SearchBox
              value={search}
              onChange={setSearch}
              delayMs={120}
              ariaLabel={`Search ${noun}`}
              placeholder={`Search ${noun}`}
              testId={`${idPrefix}-search`}
              sx={{ width: '100%', '& .MuiInputBase-root': { height: 34, fontSize: '0.8125rem' } }}
            />
          </Box>
        )}
        <FeedList
          ariaLabel={label}
          testId={`${idPrefix}-list`}
          sections={total === 0 || (needle && sections.length === 0) ? [] : sections}
          emptyMessage={total === 0 ? `No ${noun}.` : 'No match.'}
        />
      </>
    )
  }

  const advancedItem = advanced ? rows?.selected.find(item => item.id === advanced.id) : undefined

  return (
    <Box
      ref={rootRef}
      data-testid={idPrefix}
      role="group"
      aria-labelledby={labelId}
      aria-describedby={`${idPrefix}-help`}
      sx={{ maxWidth: '40rem' }}
    >
      {/* The tip sits outside the label so the group's name stays just the label. */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mb: 0.75 }}>
        <Typography id={labelId} variant="subtitle2" component="div">
          <HelpLabel label={label} help={help} />
        </Typography>
        <InfoTip
          title={destructiveHelp}
          label="What removing costs"
          testId={`${idPrefix}-help-tip`}
          descriptionId={`${idPrefix}-help`}
          descriptionTestId={`${idPrefix}-help`}
        />
      </Box>
      <Box aria-live="polite">
        {note?.tone === 'error' && <Box sx={{ mb: 0.75 }}><Banner severity="error" testId={`${idPrefix}-error`}>{note.text}</Banner></Box>}
      </Box>
      {warning && <Box sx={{ mb: 0.75 }}><Banner severity="warning" testId={`${idPrefix}-warning`}>{warning}</Banner></Box>}
      {body}
      {confirmDialog}
      <Box aria-live="polite">
        {note?.tone === 'success' && (
          <Typography variant="caption" component="p" sx={{ mt: 0.5, color: 'text.secondary' }} data-testid={`${idPrefix}-note`}>
            {note.text}
          </Typography>
        )}
      </Box>
      {advanced && advancedItem && (
        <StaticSlotsPopover
          anchor={advanced.anchor}
          item={advancedItem}
          readOnly={readOnly}
          onDone={slots => onOverride(advancedItem.id, slots)}
          onClose={() => {
            const anchor = advanced.anchor
            setAdvanced(null)
            // Back to the button that opened it, so keyboard users keep their place.
            requestAnimationFrame(() => anchor.focus())
          }}
        />
      )}
    </Box>
  )
}

export default AssignmentList

/* The override, written once on Done/Enter/close (each keystroke would be a save). Empty = none. */
function StaticSlotsPopover({ anchor, item, readOnly, onDone, onClose }: {
  anchor: HTMLElement
  item: AssignmentItem
  readOnly: boolean
  onDone: (staticSlots: number | null) => void
  onClose: () => void
}) {
  const fieldId = useId()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [text, setText] = useState(item.staticSlots === null ? '' : String(item.staticSlots))

  // The plan's cap, checked here so the operator reads it rather than the server's refusal.
  const cap = useStaticSlotsCap()
  const parsed = text.trim() === '' ? null : Number(text)
  const malformed = parsed !== null && !(Number.isInteger(parsed) && parsed >= 0 && parsed <= MAX_STATIC)
  const overCap = !malformed && parsed !== null && cap !== null && parsed > cap
  const invalid = malformed || overCap

  function close(value: number | null | undefined) {
    if (value !== undefined && !readOnly) onDone(value)
    onClose()
  }

  return (
    <Popover
      open
      anchorEl={anchor}
      onClose={() => close(invalid ? undefined : parsed)}
      // The field takes focus once the paper has entered (an `autoFocus` fired mid-transition is lost).
      disableAutoFocus
      anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
      transformOrigin={{ vertical: 'top', horizontal: 'right' }}
      slotProps={{
        paper: { role: 'dialog', 'aria-label': `Static slots: ${item.name}`, sx: { p: 1.5, width: 248, maxWidth: 'calc(100vw - 32px)' } } as object,
        transition: { onEntered: () => inputRef.current?.focus() },
      }}
    >
      <Typography variant="subtitle2" sx={{ mb: 1, overflowWrap: 'anywhere' }}>{item.name}</Typography>
      <TextField
        id={fieldId}
        label="Static slots"
        type="number"
        size="small"
        fullWidth
        inputRef={inputRef}
        disabled={readOnly}
        value={text}
        placeholder="None"
        error={invalid}
        helperText={malformed ? `Whole number, 0–${MAX_STATIC}` : overCap ? `Plan allows up to ${cap}` : 'Slots this node keeps for the project.'}
        onChange={e => setText(e.target.value)}
        onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); if (!invalid) close(parsed) } }}
        slotProps={{ htmlInput: { min: 0, max: cap ?? MAX_STATIC, step: 1, 'data-testid': 'assignment-static-input' }, inputLabel: { shrink: true } }}
      />
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mt: 1.25 }}>
        <Button type="button" size="small" disabled={readOnly || item.staticSlots === null} onClick={() => close(null)}>
          Clear
        </Button>
        <Button type="button" size="small" variant="contained" disabled={invalid} onClick={() => close(parsed)}>
          Done
        </Button>
      </Box>
      <Box component="span" sx={VISUALLY_HIDDEN} aria-live="polite">
        {parsed === null ? 'No static slots' : `${parsed} static`}
      </Box>
    </Popover>
  )
}
