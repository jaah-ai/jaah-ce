import { useState } from 'react'
import DialogContentText from '@mui/material/DialogContentText'
import TextField from '@mui/material/TextField'
import { patch } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import type { PipelineTask } from '../../types'
import FormDialog from '../../ui/FormDialog'
import { DEFAULT_PRIORITY, PRIORITY_LEVELS, priorityLabel } from './taskPriority'

/* "PRIORITY…" — the deleted Queue style's per-row priority select, as the board row ⋯'s dialog: one
 * native select of `PRIORITY_LEVELS`, then `PATCH /api/tasks/{id}` `{priority}`. The host offers it on a queued
 * (`ready`) row it may edit (`rowActions.tsx`'s `showPriority`) and, on `onDone`, may offer a preempt when the new
 * priority sorts ahead of running work (`pages/Tasks.tsx`). */
export default function PriorityDialog({
  task,
  onClose,
  onDone,
}: {
  /** The task; `null` is closed. */
  task: PipelineTask | null
  onClose: () => void
  onDone: (updated: PipelineTask) => void
}) {
  return task === null ? null : <PriorityBody key={task.id} task={task} onClose={onClose} onDone={onDone} />
}

function PriorityBody({
  task,
  onClose,
  onDone,
}: {
  task: PipelineTask
  onClose: () => void
  onDone: (updated: PipelineTask) => void
}) {
  const current = task.priority ?? DEFAULT_PRIORITY
  const [value, setValue] = useState(current)
  const { busy, formError, run } = useSubmit()
  const named = PRIORITY_LEVELS.some(level => level.value === current)

  return (
    <FormDialog
      title="Priority"
      submitLabel="Save"
      busyLabel="Saving…"
      busy={busy}
      error={formError}
      onSubmit={() => {
        // An unchanged pick writes nothing.
        if (value === current) {
          onClose()
          return
        }
        void run('Could not set priority', async () => {
          const updated = await patch<PipelineTask>(`/api/tasks/${task.id}`, { priority: value })
          onClose()
          onDone(updated)
        })
      }}
      onClose={onClose}
      maxWidth="xs"
      testId="priority-dialog"
      submitTestId="priority-submit"
      errorTestId="priority-error"
    >
      <DialogContentText sx={{ fontSize: '0.875rem', overflowWrap: 'anywhere' }}>
        {task.id}: {task.title}
      </DialogContentText>
      <TextField
        select
        autoFocus
        fullWidth
        size="small"
        label="Priority"
        value={String(value)}
        onChange={e => setValue(Number(e.target.value))}
        disabled={busy}
        slotProps={{ select: { native: true }, htmlInput: { 'data-testid': 'priority-select' } }}
      >
        {/* A raw int set outside the UI stays shown and re-selectable; first, as in `TaskEditForm.tsx`. */}
        {!named && <option value={String(current)}>{priorityLabel(current)}</option>}
        {PRIORITY_LEVELS.map(level => (
          <option key={level.value} value={String(level.value)}>
            {level.label}
          </option>
        ))}
      </TextField>
    </FormDialog>
  )
}
