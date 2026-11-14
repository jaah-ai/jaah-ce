import { useRef, useState, type JSX } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useQueryClient } from '@tanstack/react-query'
import { apiDetail, post } from '../../api'
import { useCanOnProject } from '../../auth'
import { CONTROLS, startBody } from '../../components/tasks/controls'
import { usePushIdentity } from '../../components/tasks/PushIdentityDialog'
import { personaLabel } from '../../components/tasks/personaRegistry'
import { PANE_QUERY_KEY } from '../../components/tasks/pane/queryKeys'
import { useProjects } from '../../hooks/useRosters'
import { useSubmit } from '../../hooks/useSubmit'
import { humanize } from '../../lib/humanize'
import { FormDialog } from '../../ui/FormDialog'
import { FormField } from '../../ui/FormField'
import SelectField from '../../ui/SelectField'
import { START_REFUSED_STATE } from '../taskDetail/startRefusedState'
import type { PipelineTask } from '../../types'
import { TEMPLATES_KEY, createFromTemplate, type TemplateCard } from './api'
import TemplateInputFields, { missingInputs } from './TemplateInputFields'

/* START — the template's one-shot action (a dialog, as the style guide allows for one-shot actions): pick a project,
 * Confirm. It files the task through the ordinary create (`POST /api/tasks {template_id, project_id}`: the server
 * copies the template's fields and images, links the row and counts the start), starts it through the ordinary
 * `POST /start`, closes, and opens the task — the dialog closes itself, since a host that stays mounted across the
 * navigation (the task list's template starters) would otherwise leave it open. A refused start still closes and
 * opens the task, now a draft, handing the refusal to the task page (`START_REFUSED_STATE`); only a refused create,
 * which leaves nothing to open, keeps the dialog open on its message. The Start ask (`usePushIdentity`: whom an
 * acting operator pushes as, whether a starter who can't see the repository runs without it) comes before the
 * create, so Cancel files nothing. The picker lists only projects where the
 * viewer may create tasks (never a system project: the create route refuses one). */
/** The shape line: persona, depth, involvement — a word the previous one already said is dropped (an Epic
 *  persona at epic depth reads "Epic · Guided", not "Epic · Epic · Guided"). */
export function shapeLine(card: Pick<TemplateCard, 'persona' | 'depth' | 'involvement'>): string {
  const words = [personaLabel(card.persona), humanize(card.depth), humanize(card.involvement)]
  return words.filter((w, i) => w !== words[i - 1]).join(' · ')
}

export default function StartTemplateDialog({ card, onClose }: { card: TemplateCard; onClose: () => void }): JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canOnProject = useCanOnProject()
  const projects = useProjects()
  const choices = (projects.data ?? []).filter(p => !p.system_kind && canOnProject('tasks:create', p.id))
  const [picked, setPicked] = useState<number | ''>('')
  const projectId = picked === '' && choices.length === 1 ? choices[0].id : picked
  const { busy, formError: error, run } = useSubmit()
  const [inputs, setInputs] = useState<Record<string, string>>({})
  const pushIdentity = usePushIdentity()

  // One ask at a time: a second Start press during the repository read must not file the template twice.
  const asking = useRef(false)
  const confirm = async () => {
    if (projectId === '' || asking.current) return
    asking.current = true
    const pushAs = await pushIdentity.ask({ projectId, title: card.name, epic: card.depth === 'epic' })
      .finally(() => { asking.current = false })
    if (!pushAs) return
    await run('Could not create the task', async () => {
      const created = await createFromTemplate(card.id, projectId, inputs)
      void queryClient.invalidateQueries({ queryKey: TEMPLATES_KEY })
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      let refused: string | null = null
      try {
        await post<PipelineTask>(CONTROLS.start.path(created.id), startBody('queue', pushAs))
      } catch (err) {
        refused = apiDetail(err, 'The task was created, but starting it was refused')
      }
      onClose()
      navigate(`/tasks/${created.id}`, refused === null ? undefined : { state: { [START_REFUSED_STATE]: refused } })
    })
  }

  return (
    <>
      <FormDialog
        title={`Start ${card.name}`}
        submitLabel="Start"
        busyLabel="Starting…"
        busy={busy}
        onSubmit={() => void confirm()}
        onClose={onClose}
        submitDisabled={projectId === '' || missingInputs(card.inputs, inputs)}
        testId="template-start-dialog"
        submitTestId="template-start-confirm"
        fullScreenOnPhone
      >
        <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }} data-testid="template-start-shape">
          {shapeLine(card)}
        </Typography>
        <FormField
          label="Project"
          help="Files the task here; it runs on this repo."
          hint={!projects.isLoading && choices.length === 0 ? 'No project you can start tasks in' : undefined}
        >
          <SelectField
            value={String(projectId)}
            onChange={v => setPicked(Number(v))}
            disabled={busy || projects.isLoading}
            testId="template-start-project"
            options={choices.map(p => ({ value: String(p.id), label: p.name }))}
          >
            {/* Blank until picked, as before: a native select would otherwise show a project it has not chosen. */}
            {projectId === '' && <option value="" disabled />}
          </SelectField>
        </FormField>
        {card.inputs.length > 0 && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, mt: 2 }} data-testid="template-start-inputs">
            <TemplateInputFields inputs={card.inputs} values={inputs} onChange={setInputs} disabled={busy} />
          </Box>
        )}
        {error && (
          <Typography role="alert" variant="body2" sx={{ color: 'error.main', mt: 2 }} data-testid="template-start-error">
            {error}
          </Typography>
        )}
      </FormDialog>
      {pushIdentity.dialog}
    </>
  )
}
