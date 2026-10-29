import { useState, type JSX } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import MenuItem from '@mui/material/MenuItem'
import MuiLink from '@mui/material/Link'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useQueryClient } from '@tanstack/react-query'
import { apiDetail, post } from '../../api'
import { useCanOnProject } from '../../auth'
import { CONTROLS, startBody } from '../../components/tasks/controls'
import { personaLabel } from '../../components/tasks/personaRegistry'
import { PANE_QUERY_KEY } from '../../components/tasks/pane/queryKeys'
import { useProjects } from '../../hooks/useRosters'
import { humanize } from '../../lib/humanize'
import { FormDialog } from '../../ui/FormDialog'
import type { PipelineTask } from '../../types'
import { TEMPLATES_KEY, createFromTemplate, type TemplateCard } from './api'

/* START — the template's one-shot action (a dialog, as the style guide allows for one-shot actions): pick a project,
 * Confirm. It files the task through the ordinary create (`POST /api/tasks {template_id, project_id}`: the server
 * copies the template's fields and images, links the row and counts the start), starts it through the ordinary
 * `POST /start`, and opens the task. A refused start leaves the task a draft: its message is shown whole, with a
 * link to that draft. The picker lists only projects where the viewer may create tasks (never a system project:
 * the create route refuses one). */
export default function StartTemplateDialog({ card, onClose }: { card: TemplateCard; onClose: () => void }): JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canOnProject = useCanOnProject()
  const projects = useProjects()
  const choices = (projects.data ?? []).filter(p => !p.system_kind && canOnProject('tasks:create', p.id))
  const [picked, setPicked] = useState<number | ''>('')
  const projectId = picked === '' && choices.length === 1 ? choices[0].id : picked
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<PipelineTask | null>(null)

  const confirm = async () => {
    if (projectId === '') return
    setBusy(true)
    setError('')
    let created = draft
    try {
      created ??= await createFromTemplate(card.id, projectId)
      setDraft(created)
      void queryClient.invalidateQueries({ queryKey: TEMPLATES_KEY })
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      await post<PipelineTask>(CONTROLS.start.path(created.id), startBody('queue'))
      navigate(`/tasks/${created.id}`)
    } catch (err) {
      setError(created
        ? apiDetail(err, 'The task was created, but starting it was refused')
        : apiDetail(err, 'Could not create the task'))
      setBusy(false)
    }
  }

  return (
    <FormDialog
      title={`Start ${card.name}`}
      submitLabel={draft ? 'Start again' : 'Start'}
      busyLabel="Starting…"
      busy={busy}
      onSubmit={() => void confirm()}
      onClose={onClose}
      submitDisabled={projectId === ''}
      testId="template-start-dialog"
      submitTestId="template-start-confirm"
      fullScreenOnPhone
    >
      <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }} data-testid="template-start-shape">
        {personaLabel(card.persona)} · {humanize(card.depth)} · {humanize(card.involvement)}
      </Typography>
      <TextField
        select
        fullWidth
        label="Project"
        value={projectId}
        onChange={e => setPicked(Number(e.target.value))}
        disabled={busy || draft !== null || projects.isLoading}
        helperText={!projects.isLoading && choices.length === 0 ? 'No project you can start tasks in' : undefined}
        slotProps={{ htmlInput: { 'data-testid': 'template-start-project' } }}
      >
        {choices.map(p => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
      </TextField>
      {error && (
        <Typography role="alert" variant="body2" sx={{ color: 'error.main', mt: 2 }} data-testid="template-start-error">
          {error}
          {draft && (
            <>
              {' '}
              <MuiLink component={RouterLink} to={`/tasks/${draft.id}`} data-testid="template-start-draft">
                Open the draft
              </MuiLink>
            </>
          )}
        </Typography>
      )}
    </FormDialog>
  )
}
