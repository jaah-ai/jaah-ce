import { useState } from 'react'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import { useQueryClient } from '@tanstack/react-query'
import { post } from '../../../api'
import { useSubmit } from '../../../hooks/useSubmit'
import { TASK_TREE_QUERY_KEY } from '../../../hooks/useTaskTree'
import { locateScope, type TaskScope } from '../../../lib/taskScope'
import { OrgQualifier, orgQualifierFor } from '../../../lib/projectLabel'
import type { TaskTree, TaskTreeProject } from '../../../types'
import FormDialog from '../../../ui/FormDialog'

/* NEW FEATURE from the pane header's New ▾, PREFILLED FROM THE SELECTION: a project or narrower selection
 * supplies the project; under All projects the dialog asks for it first. Only projects the user may create
 * tasks in are offered (the tree's `can_create_tasks`); the API refuses the rest anyway. On success the
 * selection moves to the new feature, as the rail's own "+" does. */

/** The projects a selection lets the user create a feature in — every creatable project under All projects,
 *  else the selection's own project. */
export function creatableFor(scope: TaskScope, tree: TaskTree | undefined): TaskTreeProject[] {
  const all = (tree?.projects ?? []).filter(p => p.can_create_tasks)
  if (scope.kind === 'all') return all
  const { project } = locateScope(scope, tree)
  return project ? all.filter(p => p.id === project.id) : []
}

export default function NewNodeDialog({
  scope,
  tree,
  onClose,
  onCreated,
}: {
  scope: TaskScope
  tree: TaskTree | undefined
  onClose: () => void
  onCreated: (next: TaskScope) => void
}) {
  const client = useQueryClient()
  const projects = creatableFor(scope, tree)
  const askProject = scope.kind === 'all'
  const [projectId, setProjectId] = useState<number | ''>(askProject ? '' : (projects[0]?.id ?? ''))
  const project = projects.find(p => p.id === projectId)
  const qualifierOf = orgQualifierFor(projects)
  const [name, setName] = useState('')
  const { busy, formError, run } = useSubmit()

  return (
    <FormDialog
      title="New feature"
      submitLabel="Create"
      busyLabel="Creating…"
      busy={busy}
      error={formError}
      onSubmit={() =>
        void run('Could not create the feature', async () => {
          const created = await post<{ id: string }>('/api/features', { project_id: project!.id, name: name.trim() })
          await client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
          onCreated({ kind: 'feature', featureId: created.id })
        })
      }
      onClose={onClose}
      submitDisabled={project === undefined || name.trim() === ''}
      maxWidth="xs"
      testId="new-feature-dialog"
      submitTestId="new-feature-submit"
    >
      {askProject && (
        <TextField
          select
          label="Project"
          value={projectId}
          onChange={e => setProjectId(Number(e.target.value))}
          autoFocus
          data-testid="new-feature-project"
        >
          {projects.map(p => (
            <MenuItem key={p.id} value={p.id}>{p.name}<OrgQualifier org={qualifierOf(p)} /></MenuItem>
          ))}
        </TextField>
      )}
      <TextField
        label="Name"
        value={name}
        onChange={e => setName(e.target.value)}
        autoFocus={!askProject}
        disabled={askProject && project === undefined}
        slotProps={{ htmlInput: { maxLength: 200, 'data-testid': 'new-feature-name' } }}
      />
    </FormDialog>
  )
}
