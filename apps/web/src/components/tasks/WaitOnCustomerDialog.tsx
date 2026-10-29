import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import DialogContentText from '@mui/material/DialogContentText'
import TextField from '@mui/material/TextField'
import { post } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { TASK_TREE_QUERY_KEY } from '../../hooks/useTaskTree'
import type { PipelineTask } from '../../types'
import FormDialog from '../../ui/FormDialog'

/** The note's cap — `task_sections.STOP_NOTE_MAX` on the API. */
const NOTE_MAX = 2000

/* "WAITING ON CUSTOMER…" (D-waiting, AC-6.3) — a PAUSE REASON, not a state. One action from the row menu and
 * the task page's ⋯ and State control: an optional note, then `POST /api/tasks/{id}/set-state` with
 * `waiting_on_customer`, which stops a running or queued task and records the reason (a draft, paused or failed
 * task keeps its state and gains the reason; a done one is reopened as paused). Any state change clears it. The
 * host offers it only for a top-level task the caller may run; a stage task's row points at its epic instead
 * (`rowActions.tsx`). */
export default function WaitOnCustomerDialog({
  task,
  onClose,
  onDone,
}: {
  /** The task; `null` is closed. */
  task: PipelineTask | null
  onClose: () => void
  onDone: (updated: PipelineTask) => void
}) {
  return task === null ? null : <WaitBody key={task.id} task={task} onClose={onClose} onDone={onDone} />
}

function WaitBody({
  task,
  onClose,
  onDone,
}: {
  task: PipelineTask
  onClose: () => void
  onDone: (updated: PipelineTask) => void
}) {
  const client = useQueryClient()
  const [note, setNote] = useState('')
  const { busy, formError, run } = useSubmit()
  const running = task.state === 'ready' || task.state === 'active' || task.state === 'awaiting_response'

  return (
    <FormDialog
      title="Waiting on customer"
      submitLabel="Stop and wait"
      busyLabel="Stopping…"
      busy={busy}
      error={formError}
      onSubmit={() =>
        void run('Could not set Waiting on customer', async () => {
          const trimmed = note.trim()
          const updated = await post<PipelineTask>(
            `/api/tasks/${task.id}/set-state`,
            trimmed === '' ? { state: 'waiting_on_customer' } : { state: 'waiting_on_customer', note: trimmed },
          )
          void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
          onDone(updated)
          onClose()
        })
      }
      onClose={onClose}
      maxWidth="xs"
      testId="wait-on-customer-dialog"
      submitTestId="wait-on-customer-submit"
      errorTestId="wait-on-customer-error"
    >
      <DialogContentText sx={{ fontSize: '0.875rem', overflowWrap: 'anywhere' }}>
        {task.id}: {task.title} moves to Stopped{running ? ' — a running task is stopped' : ''}. Any state change clears it.
      </DialogContentText>
      <TextField
        autoFocus
        fullWidth
        multiline
        minRows={2}
        size="small"
        label="Note (optional)"
        value={note}
        onChange={e => setNote(e.target.value)}
        disabled={busy}
        slotProps={{ htmlInput: { 'data-testid': 'wait-on-customer-note', maxLength: NOTE_MAX } }}
      />
    </FormDialog>
  )
}
