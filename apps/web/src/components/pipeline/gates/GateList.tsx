import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import ListItemText from '@mui/material/ListItemText'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import {
  durationText, GATE_CHOICES, type GateChoice, type GateDraft, GATES_MAX, gateProblems, KIND_ICON, KIND_LABEL,
  newGate, NO_ALERT_SOURCE,
} from '../../../lib/pipelineGates'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS } from '../../../theme'
import StatusChip from '../../../ui/StatusChip'
import { PHONE, tapTargetSx } from '../../../ui/listStyles'
import GateForm from './GateForm'

/* ONE GATE LIST of Project settings › Pipeline (`PipelineSettings.tsx`): a step's gates (run before a change goes into
 * its stage) or a stage's After-arrival gates (run once a change lands there) — the "Step gates" and "After-arrival
 * gates" screens of the pipeline gates mockup. Each row reads at a glance (AC1.8): the kind's glyph, the full name, the
 * server's one-line summary, chips for Advisory / Off / time limit / retries. An admin adds one of seven kinds, edits a
 * row in place (`GateForm.tsx`), reorders by drag or by the up/down buttons, switches it off and on, and removes it;
 * the list is part of the pipeline draft, saved by the page's one Save. Read-only for a viewer (AC1.7). */

const ROW_SX = {
  display: 'flex', alignItems: 'flex-start', gap: 1.5, px: 1.5, py: 1, minWidth: 0,
  border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, bgcolor: 'background.paper',
  [PHONE]: { flexWrap: 'wrap', columnGap: 1 },
} as const
const ICON_SX = {
  width: grid(4), height: grid(4), flexShrink: 0, display: 'grid', placeItems: 'center', borderRadius: RADIUS.sm,
  bgcolor: 'action.hover', fontSize: FONT_SIZE.md, mt: 0.5,
} as const
const BODY_SX = { flex: '1 1 auto', minWidth: grid(24), display: 'flex', flexDirection: 'column', gap: 0.5 } as const
const CHIPS_SX = { display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center', mt: 0.5 } as const
const ACTIONS_SX = {
  display: 'flex', alignItems: 'center', flexShrink: 0, gap: 0.5,
  [PHONE]: { flexBasis: '100%', justifyContent: 'flex-end' },
} as const

export default function GateList({
  gates, onChange, canEdit, busy, autoStep, title, empty, projectId, errors, testId,
}: {
  gates: readonly GateDraft[]
  onChange: (next: GateDraft[]) => void
  canEdit: boolean
  busy: boolean
  /** The list runs by itself (an automatic step, or every After-arrival list): no ticket asked when pressed. */
  autoStep: boolean
  /** The list's accessible name ("Gates of Merge: Building → Dev"). */
  title: string
  /** What an empty list says. */
  empty: string
  projectId: number
  /** A refused save's message on one gate, by the gate's key. */
  errors?: ReadonlyMap<string, string>
  testId: string
}): JSX.Element {
  const off = !canEdit || busy
  const [open, setOpen] = useState<string | null>(null)
  const [menu, setMenu] = useState<HTMLElement | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const [over, setOver] = useState<number | null>(null)

  const patch = (key: string, next: Partial<GateDraft>) =>
    onChange(gates.map(g => (g.key === key ? { ...g, ...next, ...changed(g, next) } : g)))
  const move = (from: number, to: number) => {
    if (from === to || to < 0 || to >= gates.length) return
    const next = [...gates]
    const [row] = next.splice(from, 1)
    next.splice(to, 0, row)
    onChange(next)
  }
  const add = (choice: GateChoice) => {
    setMenu(null)
    const gate = newGate(choice, autoStep)
    onChange([...gates, gate])
    setOpen(gate.key)
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }} data-testid={testId}>
      {gates.length === 0 && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="gate-list-empty">{empty}</Typography>
      )}
      <Box role="list" aria-label={title} sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      {gates.map((g, index) => {
        const problems = gateProblems(g, autoStep)
        const serverError = errors?.get(g.key)
        const invalid = Object.keys(problems).length > 0 || Boolean(serverError)
        const isOpen = open === g.key || (canEdit && Boolean(serverError))
        return (
          <Box key={g.key} role="listitem" aria-label={g.name || KIND_LABEL[g.kind]} data-testid="gate-row"
            data-kind={g.kind} data-enabled={g.enabled}
            onDragOver={e => { if (dragging !== null) { e.preventDefault(); setOver(index) } }}
            onDrop={e => { e.preventDefault(); if (dragging !== null) move(dragging, index); setDragging(null); setOver(null) }}
            sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0,
              ...(over === index && dragging !== null && dragging !== index ? { outline: '2px dashed', outlineColor: 'primary.main', borderRadius: RADIUS.md } : {}) }}>
            <Box sx={[ROW_SX, !g.enabled && { bgcolor: 'action.hover' }, invalid && { borderColor: 'error.main' }]}>
              {canEdit && (
                <Tooltip title="Drag to reorder">
                  <Box component="span" draggable={!off} aria-hidden data-testid="gate-drag"
                    onDragStart={e => { setDragging(index); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', g.key) }}
                    onDragEnd={() => { setDragging(null); setOver(null) }}
                    sx={{ cursor: off ? 'default' : 'grab', color: 'text.secondary', userSelect: 'none', mt: 0.5, px: 0.5,
                      [PHONE]: { display: 'none' } }}>
                    ⋮⋮
                  </Box>
                </Tooltip>
              )}
              <Box sx={[ICON_SX, !g.enabled && { opacity: 0.5 }]} aria-hidden title={KIND_LABEL[g.kind]}>{KIND_ICON[g.kind]}</Box>
              <Box sx={[BODY_SX, !g.enabled && { color: 'text.secondary' }]}>
                <Box sx={{ fontWeight: FONT_WEIGHT.semibold, overflowWrap: 'anywhere' }} data-testid="gate-name">
                  {g.name || KIND_LABEL[g.kind]}
                </Box>
                <Box sx={{ fontSize: FONT_SIZE.sm, color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="gate-summary">
                  {g.summary ?? 'Summary shows after Save'}
                </Box>
                {g.warning === 'no_alert_source' && (
                  <Typography variant="body2" sx={{ color: 'warning.dark' }} data-testid="gate-warning">{NO_ALERT_SOURCE}</Typography>
                )}
                <Box sx={CHIPS_SX}>
                  {!g.enabled && <StatusChip label="Off" tone="neutral" title="Switched off: skipped when the list runs" testId="gate-chip-off" />}
                  {!g.required && <StatusChip label="Advisory" tone="warning" title="Advisory: a failure only warns" testId="gate-chip-advisory" />}
                  <StatusChip label={durationText(g.timeout_s)} tone="neutral" title={`Time limit ${durationText(g.timeout_s)}`} testId="gate-chip-timeout" />
                  {g.retries > 0 && <StatusChip label={`Retry ${g.retries}`} tone="neutral" title={`Retried up to ${g.retries} ${g.retries === 1 ? 'time' : 'times'} after a failure`} testId="gate-chip-retry" />}
                  {invalid && !isOpen && <StatusChip label="Needs a fix" tone="error" title={serverError ?? 'Open it to see what to fix'} testId="gate-chip-invalid" />}
                </Box>
              </Box>
              {canEdit && (
                <Box sx={ACTIONS_SX}>
                  <Tooltip title="Earlier"><span>
                    <IconButton size="small" aria-label={`Move ${g.name} earlier`} disabled={off || index === 0}
                      onClick={() => move(index, index - 1)} data-testid="gate-up" sx={tapTargetSx}>↑</IconButton>
                  </span></Tooltip>
                  <Tooltip title="Later"><span>
                    <IconButton size="small" aria-label={`Move ${g.name} later`} disabled={off || index === gates.length - 1}
                      onClick={() => move(index, index + 1)} data-testid="gate-down" sx={tapTargetSx}>↓</IconButton>
                  </span></Tooltip>
                  <Button size="small" disabled={off} onClick={() => setOpen(isOpen ? null : g.key)} aria-expanded={isOpen}
                    data-testid="gate-edit" sx={tapTargetSx}>
                    {isOpen ? 'Done' : 'Edit'}
                  </Button>
                  <Button size="small" disabled={off} onClick={() => patch(g.key, { enabled: !g.enabled })}
                    data-testid="gate-toggle" sx={[tapTargetSx, { whiteSpace: 'nowrap' }]}>
                    {g.enabled ? 'Switch off' : 'Switch on'}
                  </Button>
                  <Tooltip title="Remove"><span>
                    <IconButton size="small" aria-label={`Remove ${g.name}`} disabled={off}
                      onClick={() => { onChange(gates.filter(x => x.key !== g.key)); if (open === g.key) setOpen(null) }}
                      data-testid="gate-remove" sx={tapTargetSx}>✕</IconButton>
                  </span></Tooltip>
                </Box>
              )}
            </Box>
            {isOpen && canEdit && (
              <GateForm gate={g} problems={problems} serverError={serverError} autoStep={autoStep} busy={busy} projectId={projectId}
                onPatch={next => patch(g.key, next)} />
            )}
          </Box>
        )
      })}
      </Box>
      {canEdit && (
        <Box>
          <Button size="small" variant="outlined" disabled={off || gates.length >= GATES_MAX} onClick={e => setMenu(e.currentTarget)}
            aria-haspopup="menu" data-testid="gate-add" sx={tapTargetSx}>
            + Add gate
          </Button>
          <Menu anchorEl={menu} open={menu !== null} onClose={() => setMenu(null)} data-testid="gate-add-menu">
            {GATE_CHOICES.map(c => (
              <MenuItem key={c.value} onClick={() => add(c.value)} data-testid={`gate-add-${c.value}`}>
                <Box component="span" aria-hidden sx={{ width: grid(3.5), flexShrink: 0 }}>
                  {KIND_ICON[c.value.startsWith('ticket') ? 'ticket' : c.value as Exclude<GateChoice, 'ticket_jira' | 'ticket_servicenow'>]}
                </Box>
                <ListItemText primary={c.label} secondary={c.help} />
              </MenuItem>
            ))}
          </Menu>
        </Box>
      )}
    </Box>
  )
}

/** An edit to anything but on/off makes the read's summary and warning stale: they show again after Save. */
function changed(g: GateDraft, next: Partial<GateDraft>): Partial<GateDraft> {
  const keys = Object.keys(next).filter(k => k !== 'enabled' && k !== 'required' && k !== 'timeout_s' && k !== 'retries' && k !== 'name')
  return keys.length === 0 || g.summary === null ? {} : { summary: null, warning: null }
}
