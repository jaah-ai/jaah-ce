import { useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { post } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import type { ConfirmOptions } from '../ConfirmDialog'
import { KNOWN_TASK_STATES, TRANSITIONS } from '../../generated/taskTransitions'
import type { PipelineTask } from '../../types'
import Banner from '../../ui/Banner'
import { ABOVE_PHONE, PHONE } from '../../ui/listStyles'
import { allowedTransitions } from './controls'
import { orgWaitSentence, stateLabel, taskStatus } from './shared'

/* IN A FIELD CELL, THE DENSE HEIGHT: no form margin and a value-line's padding, so the State row lines up with
   the plain-text Persona and Priority beside it. Above the phone only — there the 44px tap floor wins. */
const IN_FIELD_SX = {
  [ABOVE_PHONE]: { '& .MuiSelect-select': { py: '2px', minHeight: 0 } },
} as const

/** The pause reason, offered beside the seven states (`routes/tasks/transitions.py::SET_STATE_TARGETS`). */
export const WAITING = 'waiting_on_customer'

/* What each move does, said in the confirm — `routes/tasks/transitions.py::set_task_state` is the behaviour. */
const CONSEQUENCE: Record<string, string> = {
  draft: 'Ends any runner, frees its slot and clears this attempt’s results.',
  ready: 'Queues it for a runner. A live runner is stopped first.',
  active: 'Sets the state only — no runner is started.',
  awaiting_response: 'Sets the state only — no runner is started or asked.',
  paused: 'Ends any live runner; the slot is kept.',
  system_failed: 'Ends any live runner and marks it failed.',
  done: 'Closes it. The outcome is not verified.',
}
const EPIC_CONSEQUENCE: Partial<Record<string, string>> = {
  draft: 'Stops its stage tasks and sets the epic to Draft.',
  ready: 'An epic is never queued \u2014 this starts it.',
}

/** The value the control shows: the pause reason outranks the state it rides on. */
export function currentStateValue(task: PipelineTask): string {
  return task.stop?.kind === WAITING ? WAITING : task.state
}

/* THE STATES THIS ROW MAY MOVE TO NOW: the targets of its `available_transitions` (the server's, else the
 * generated table's — `controls.ts::allowedTransitions`), never the current state. */
export function stateTargets(task: PipelineTask): ReadonlySet<string> {
  const allowed = new Set(allowedTransitions(task))
  return new Set(TRANSITIONS.filter(t => allowed.has(t.name) && t.target !== task.state).map(t => t.target))
}

/* WHETHER THE HOST DRAWS THE CONTROL AT ALL: a transition reaches another state, or the row is top-level,
 * where Waiting on customer is always a move. It gates the control only — once drawn, the control offers every state
 * (an operator override); none, the host draws the read-only badge. */
export function offersStateMove(task: PipelineTask): boolean {
  return task.parent_task_id === null || stateTargets(task).size > 0
}

/* A RUNNING ROW WHOSE STAGE ASKS: the header's pill rolls a child's gate up to "Asking" (`taskStatus`, from
 * `gated_child_id`) while the row's own state stays `active`, so Details › State said "Running" beside it. Both are
 * true; this says both — "Running · 2 stages asking" from the detail read's `children` (a stage counts when it reads
 * Asking itself), else "Running · stage #<id> asking" from the rollup's id (a payload without `children`, or one whose
 * children lag the rollup). `null` when no stage asks, or the row is not running (`GATE_LIVE_STATES`' parked
 * `awaiting_response` is the row's own question, which its state word already says). */
export function stageAskingLabel(task: PipelineTask): string | null {
  if (task.state !== 'active' || task.gated_child_id == null) return null
  // The epic's OWN open round is its own Asking, not a stage's.
  if ((task.questions ?? []).some(q => q.status === 'open')) return null
  const asking = (task.children ?? []).filter(c => taskStatus(c).key === 'asking').length
  const stages = asking > 0 ? `${asking} stage${asking === 1 ? '' : 's'}` : `stage #${task.gated_child_id}`
  return `${stateLabel(task.state)} · ${stages} asking`
}

/* THE DETAILS TAB'S STATE CONTROL: any state from any state, beyond the Start/Stop/Reset/Complete
 * table. "Waiting on customer" opens the host's note dialog (top-level tasks only — the server refuses a stage
 * task). Choosing the shown value is a no-op; every other move drops a waiting reason. */
export default function TaskStateControl({
  task,
  confirm,
  onChanged,
  onWait,
  parentGate,
  disabled = false,
  inField = false,
}: {
  task: PipelineTask
  /** A child's epic gating, when known: `taskStatus`'s parent options, as the row passes them (`toViewRow`). */
  parentGate?: Parameters<typeof taskStatus>[1]
  confirm: (options: ConfirmOptions) => Promise<boolean>
  onChanged: (updated: PipelineTask) => void
  /** Opens the Waiting-on-customer dialog. */
  onWait: () => void
  /** Held while the edit form has unsaved work: the returned row re-keys it. */
  disabled?: boolean
  /** Sits in a labelled `DetailField` cell: the cell's label names it, so the select carries none of its own. It
   *  reads as a value with a "Change…" action until pressed. */
  inField?: boolean
}) {
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  /* THE STATE SHOWS ONCE: the header's pill carries the state, so in a field cell the value reads as
     plain text with a "Change…" action; pressing it swaps in the select, already open, and closing it swaps back. */
  const [editing, setEditing] = useState(false)
  /* Closing the select hands focus back to "Change…", never to the page's body (the select unmounts). */
  const changeRef = useRef<HTMLButtonElement>(null)
  const [refocus, setRefocus] = useState(false)
  useEffect(() => {
    if (!editing && refocus) {
      changeRef.current?.focus()
      setRefocus(false)
    }
  }, [editing, refocus])
  const value = currentStateValue(task)
  const isEpic = task.persona === 'epic'
  /* A ready task's SHOWN value reads as its row does (Starting / Held / Waiting / Queued); the option list keeps
     the plain words, so "Waiting" never sits beside "Waiting on customer". The org hold's full sentence is the hover.
     A running row whose stage asks names that stage beside its word, agreeing with the header's "Asking". */
  const refined = value === 'ready' ? taskStatus(task, parentGate).label : stageAskingLabel(task)
  const title = refined && task.wait_reason ? orgWaitSentence(task.wait_reason) : undefined
  const options: { value: string; label: string }[] = [
    ...KNOWN_TASK_STATES.map(s => ({ value: s, label: stateLabel(s) })),
    ...(task.parent_task_id === null ? [{ value: WAITING, label: 'Waiting on customer' }] : []),
  ]

  async function choose(target: string) {
    if (target === value) return
    if (target === WAITING) {
      onWait()
      return
    }
    const label = stateLabel(target)
    const ok = await confirm({
      title: `Set state to ${label}?`,
      identity: `#${task.id} · ${task.title}`,
      message: (isEpic && EPIC_CONSEQUENCE[target]) || CONSEQUENCE[target] || '',
      confirmLabel: `Set ${label}`,
      destructive: target === 'draft',
    })
    if (!ok) return
    await run('Could not set the state', async () => {
      onChanged(await post<PipelineTask>(`/api/tasks/${task.id}/set-state`, { state: target }))
    })
  }

  const shown = refined ?? options.find(o => o.value === value)?.label ?? value
  return (
    <Box data-testid="task-state-control" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: inField ? 1 : 1.5, flexWrap: 'wrap' }}>
        {inField && !editing ? (
          <>
            <Typography variant="inherit" title={title} data-testid="task-state-value">{shown}</Typography>
            <Button
              size="small"
              variant="text"
              disabled={busy || disabled}
              ref={changeRef}
              onClick={() => setEditing(true)}
              aria-label="Change state"
              data-testid="task-state-change"
              // A link-weight action at the value's height; the 44px tap floor on the phone.
              sx={{ minWidth: 0, py: 0, px: 0.5, typography: 'body2', [PHONE]: { minHeight: 44 } }}
            >
              Change…
            </Button>
          </>
        ) : (
        <TextField
          select
          size="small"
          label={inField ? undefined : 'State'}
          value={value}
          disabled={busy || disabled}
          onChange={e => void choose(e.target.value)}
          margin={inField ? 'none' : undefined}
          sx={[{ width: 240, maxWidth: '100%' }, inField && IN_FIELD_SX]}
          slotProps={{
            htmlInput: { 'data-testid': 'task-state-select' },
            select: {
              renderValue: v => refined ?? options.find(o => o.value === v)?.label ?? String(v),
              SelectDisplayProps: inField ? { 'aria-label': 'State', title } : { title },
              ...(inField && {
                MenuProps: { slotProps: { list: { 'aria-label': 'State' } } },
                defaultOpen: true,
                onClose: () => {
                  setEditing(false)
                  setRefocus(true)
                },
              }),
            },
          }}
        >
          {options.map(o => (
            <MenuItem key={o.value} value={o.value} data-testid={`task-state-option-${o.value}`}>
              {o.label}
            </MenuItem>
          ))}
        </TextField>
        )}
        {busy && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Saving…</Typography>}
      </Box>
      {error && (
        <Banner severity="error" onDismiss={() => setError('')} testId="task-state-error">
          {error}
        </Banner>
      )}
    </Box>
  )
}
