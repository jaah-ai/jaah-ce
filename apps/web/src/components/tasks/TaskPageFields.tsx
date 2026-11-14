import { type ReactNode, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { post } from '../../api'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'
import { useTaskOutcomes } from '../../pages/taskDetail/TaskOutcomePanel'
import { grid } from '../../theme'
import type { PipelineTask } from '../../types'
import { DetailCell } from '../../ui/detail/DetailPage'
import type { Explain } from '../../ui/explain'
import { FieldRow } from '../../ui/FieldRow'
import { FormTextField } from '../../ui/FormTextField'

/* THE TASK PAGE'S OWN FIELDS, under `TaskSpecFields` on the edit form: settings that were one-field dialogs
 * (Caused by, the folded Rating). Caused by rides the form's Save after its PUT, like priority, and stays out of
 * auto-save (a keystroke must not file a defect link). The state lives in `useTaskPageFields`, so the form reads
 * one `dirty` and calls one `save`. */

export const ORIGIN_HELP: Explain = {
  tip: 'Charges a confirmed defect to that merged work.',
  more: 'The task number (#123) or PR URL whose merged work caused what this task fixes. The link lands on '
    + "the origin's newest merged dispatch, confirmed at once, and counts against its outcome. The origin must "
    + 'be merged work in this project.',
}

export type TaskPageFieldsState = ReturnType<typeof useTaskPageFields>

export function useTaskPageFields({ task, isEdit, readOnly }: {
  task: PipelineTask | undefined
  isEdit: boolean
  readOnly: boolean
}) {
  const client = useQueryClient()
  const { admin } = useAuth()
  const row = isEdit ? task : undefined
  const [origin, setOrigin] = useState('')
  // The page's own outcome read (same key), so the Details strip and this field share one answer.
  const outcomes = useTaskOutcomes(row ?? null, !readOnly)
  const showsCausedBy = row !== undefined && !readOnly && outcomes.data?.can_decide === true
    && planSurfaceShown(admin, 'agent_performance')
  const originValue = origin.trim().replace(/^#/, '')
  const dirty = showsCausedBy && originValue !== ''

  /* After the form's PUT: the defect link (its answer is the origin's, not this row). */
  async function save(target: PipelineTask): Promise<void> {
    if (!dirty) return
    const byTask = /^\d+$/.test(originValue)
    await post(`/api/tasks/${target.id}/outcome-links`, byTask ? { origin_task_id: Number(originValue) } : { pr_url: originValue })
    void client.invalidateQueries({ queryKey: ['task-outcomes', target.id] })
  }

  return { row, origin, setOrigin, showsCausedBy, dirty, save }
}

export default function TaskPageFields({ fields, rating, disabled }: {
  fields: TaskPageFieldsState
  /** The folded rating (`TaskRating`), drawn here when the tab strip has no room for it. */
  rating?: ReactNode
  disabled: boolean
}) {
  const { row, origin, setOrigin, showsCausedBy } = fields
  if (row === undefined || (!showsCausedBy && !rating)) return null
  return (
    <FieldRow testId="task-form-page-fields" align="start" columnGap={grid(4)} sx={{ mt: 2 }}>
      {showsCausedBy && (
        <DetailCell basis={grid(40)} sx={{ mb: 2 }}>
          <FormTextField
            label="Origin task or PR"
            value={origin}
            onChange={setOrigin}
            help={ORIGIN_HELP}
            disabled={disabled}
            testId="outcome-caused-by-origin"
          />
        </DetailCell>
      )}
      {rating && (
        // A vote applies at once (its own PUT), like an attach control: no part of Save.
        <DetailCell sx={{ mb: 2 }}>
          <Box role="group" aria-label="Rating" data-testid="task-page-rating-field">
            <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>Rating</Typography>
            {rating}
          </Box>
        </DetailCell>
      )}
    </FieldRow>
  )
}
