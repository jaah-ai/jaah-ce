import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { post, put } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { actAsOnce } from '../../lib/actAs'
import type { FeatureSummary, PipelineTask, TaskTree } from '../../types'
import Banner from '../../ui/Banner'
import FormDialog from '../../ui/FormDialog'
import { useOtherOrg } from '../../ui/OrgIndicator'
import { featureName, invalidateFilingQueries, useFilingFeatures } from './featureFiling'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'

/* "MOVE TO FEATURE…" — the row menu's and the task page's filing action (AC-4.3). Type-ahead over the task's
 * OWN project's active features the caller may file into (`useFilingFeatures`: never a shipped one, never
 * another project's — AC-7.2, AC-1.3), plus "New feature…" (create, then file, one flow) and "Remove from
 * feature". It files with `PUT /api/tasks/{id}` `{feature_id}`, which carries the whole subtree (an epic
 * files its stage tasks, D7). A refusal shows the API's own sentence. Never offered on a stage task (the
 * host's gate). */
export default function MoveToFeatureDialog({
  task,
  tree,
  onClose,
  onMoved,
}: {
  /** The task being filed; `null` is closed. */
  task: PipelineTask | null
  tree: TaskTree | undefined
  onClose: () => void
  /** The filed row, as the PUT returned it; the host refreshes its list. */
  onMoved: (updated: PipelineTask) => void
}) {
  // Keyed on the task, so a second opening never inherits the first's pick or error.
  return task === null ? null : (
    <MoveToFeatureBody key={task.id} task={task} tree={tree} onClose={onClose} onMoved={onMoved} />
  )
}

function MoveToFeatureBody({
  task,
  tree,
  onClose,
  onMoved,
}: {
  task: PipelineTask
  tree: TaskTree | undefined
  onClose: () => void
  onMoved: (updated: PipelineTask) => void
}) {
  const client = useQueryClient()
  const { features, failed } = useFilingFeatures(task.project_id)
  // Another org's task (an operator's): its new feature is that org's, made as it for the one request.
  const asOrg = actAsOnce(useOtherOrg(task.org))
  const [picked, setPicked] = useState<FeatureSummary | null>(null)
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  // A new feature goes in the task's own project, when the caller may create there.
  const canCreateFeature = tree?.projects.find(p => p.id === task.project_id)?.can_create_tasks === true
  const { busy, formError, setFormError, run } = useSubmit()
  const current = task.feature_id ?? null
  const currentName = featureName(current, tree, features)
  // A task with no feature is its project's own.
  const projectName = tree?.projects.find(p => p.id === task.project_id)?.name ?? 'its project'

  async function file(featureId: string | null) {
    const updated = await put<PipelineTask>(`/api/tasks/${task.id}`, { feature_id: featureId })
    invalidateFilingQueries(client)
    onMoved(updated)
    onClose()
  }

  const submit = () => {
    if (creating) {
      const name = newName.trim()
      if (name === '' || !canCreateFeature) return
      void run('Could not create the feature', async () => {
        const body = { project_id: task.project_id, name }
        const created = await (asOrg ? post<FeatureSummary>('/api/features', body, asOrg) : post<FeatureSummary>('/api/features', body))
        // The feature exists now even if the filing is refused; the pickers must see it either way.
        invalidateFilingQueries(client)
        await file(created.id)
      })
    } else if (picked) {
      void run('Could not move the task', () => file(picked.id))
    }
  }

  const canSubmit = creating ? newName.trim() !== '' && canCreateFeature : picked !== null

  return (
    <FormDialog
      title="Move to feature"
      submitLabel={creating ? 'Create and move' : 'Move'}
      busyLabel="Moving…"
      busy={busy}
      error={formError}
      onSubmit={submit}
      onClose={onClose}
      submitDisabled={!canSubmit}
      maxWidth="xs"
      testId="move-to-feature-dialog"
      submitTestId="move-to-feature-submit"
      errorTestId="move-to-feature-error"
    >
      <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
        {task.id}: {task.title} — now in {currentName ?? projectName}
      </Typography>
      {creating ? (
        <TextField
          autoFocus
          size="small"
          label="New feature name"
          value={newName}
          onChange={e => setNewName(e.target.value)}
          disabled={busy}
          slotProps={{ htmlInput: { 'data-testid': 'move-to-feature-new-name', maxLength: 200 } }}
        />
      ) : (
        <Autocomplete
          openOnFocus
          autoHighlight
          options={features ?? []}
          loading={features === undefined && !failed}
          value={picked}
          onChange={(_e, value) => setPicked(value)}
          disabled={busy}
          getOptionLabel={f => f.name}
          getOptionDisabled={f => f.id === current}
          isOptionEqualToValue={(a, b) => a.id === b.id}
          noOptionsText="No active features in this project"
          renderOption={(props, f) => {
            const { key, ...rest } = props as typeof props & { key: string }
            return (
              <Box component="li" key={key} {...rest}>
                {f.name}{f.id === current ? ' (current)' : ''}
              </Box>
            )
          }}
          renderInput={params => (
            <TextField
              {...params}
              autoFocus
              size="small"
              label="Feature"
              slotProps={{ ...params.slotProps, htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT, 'data-testid': 'move-to-feature-input' } }}
            />
          )}
        />
      )}
      {failed && <Banner severity="error">Could not load this project’s features.</Banner>}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        {canCreateFeature && (
          <Button
            size="small"
            disabled={busy}
            onClick={() => { setCreating(c => !c); setFormError('') }}
            data-testid="move-to-feature-new"
          >
            {creating ? 'Pick existing' : 'New feature…'}
          </Button>
        )}
        {current !== null && !creating && (
          <Button
            size="small"
            color="warning"
            disabled={busy}
            onClick={() => void run('Could not remove the task from its feature', () => file(null))}
            data-testid="move-to-feature-remove"
          >
            Remove from feature
          </Button>
        )}
      </Box>
    </FormDialog>
  )
}
