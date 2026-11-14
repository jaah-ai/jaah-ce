import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { get, put } from '../../../api'
import { PROJECTS_KEY } from '../../../hooks/useRosters'
import { useConfirm } from '../../ConfirmDialog'
import { sameJson, useSeededDraft } from '../../../hooks/useServerDraft'
import { useSubmit } from '../../../hooks/useSubmit'
import { pipelineKey } from '../../../lib/pipeline'
import { UpgradeToActivate } from '../../../lib/planSwitches'
import { shortDate } from '../../../lib/relativeTime'
import { FONT_WEIGHT, grid } from '../../../theme'
import type { PipelineMode, Project } from '../../../types'
import Banner from '../../../ui/Banner'
import CancelSaveBar from '../../../ui/CancelSaveBar'
import FocusSafe from './FocusSafe'
import { ChoiceField, type Choice } from '../../../ui/ChoiceField'
import { DetailCell } from '../../../ui/detail/DetailPage'
import FieldRow from '../../../ui/FieldRow'
import FormTextField from '../../../ui/FormTextField'
import LoadingState from '../../../ui/LoadingState'
import SettingCheckbox from '../../../ui/SettingCheckbox'
import ProductionQaConnectDeploys from '../ProductionQaConnectDeploys'

/* CUSTOMIZE › CI PIPELINE (`GET/PUT /api/projects/{id}/pipeline/basic`, `app/releases/basics.py`). Every plan picks one
 * of two basic pipelines: its integration branch per repository (read-only, edited in the repository editor on
 * General), its release branch, and the address of each live stage. On a plan with Custom pipelines, the Custom box;
 * once saved, the Pipeline tab shows and "Customize" opens it. Off the plan the box stays off with the way to upgrade.
 * A custom pipeline whose org lost the plan keeps running, read-only, until a basic replaces it. Its own Save. A saved
 * basic pipeline also draws Deploy tracking (`ProductionQaConnectDeploys`, its own Connect): until it is connected a
 * release counts as Released at merge, not once its deploy succeeds. */

export type PipelineBasic = {
  mode: PipelineMode
  custom_offered: boolean
  read_only: boolean
  integration_branches: { repo_id: number | null; name: string; branch: string }[]
  release_branch: string | null
  addresses: { dev: string | null; production: string | null }
  reset_at: string | null
}

/** The Pipeline tab shows: a project with a repository on a custom pipeline. A basic pipeline's stages, gates and
 *  prefix are its preset's, so nothing links to the tab then. */
export function pipelineTabShown(project: Project | undefined): boolean {
  return project !== undefined && (project.has_repo ?? project.repo_url !== null) && project.pipeline_mode === 'custom'
}

/** The project is KNOWN to be on a basic pipeline (a repository, a stored mode that is not `custom`): its full gate and
 *  primary's prefix are typed on Customize. An unknown mode (a cached row from before the column) is neither this nor
 *  `pipelineTabShown`: those fields read only and go as stored. */
export function basicPipeline(project: Project | undefined): boolean {
  return project !== undefined && (project.has_repo ?? project.repo_url !== null)
    && project.pipeline_mode !== undefined && project.pipeline_mode !== 'custom'
}

export const pipelineBasicKey = (projectId: number) => ['pipeline-basic', projectId] as const

/** The project's pipeline mode and its basic's settings; `enabled` false reads nothing. */
export function usePipelineBasic(projectId: number | null, enabled = true) {
  return useQuery({
    queryKey: pipelineBasicKey(projectId ?? 0),
    queryFn: () => get<PipelineBasic>(`/api/projects/${projectId}/pipeline/basic`),
    enabled: enabled && projectId !== null,
  })
}

type Basic = 'pr_integration' | 'integration_release'
type Draft = { mode: Basic | 'custom'; release_branch: string; dev: string; production: string }

const BASICS: readonly Choice<Basic>[] = [
  { value: 'pr_integration', label: 'PR into integration branch', help: 'Merging a pull request deploys to the live stage.',
    testId: 'ci-basic-pr-integration' },
  { value: 'integration_release', label: 'Integration + Release',
    help: 'Merging deploys to Dev; a release moves it to the release branch and Production.',
    testId: 'ci-basic-integration-release' },
]

const draftOf = (b: PipelineBasic): Draft => ({
  mode: b.mode === 'custom' || b.mode === 'pr_integration' ? b.mode : 'integration_release',
  release_branch: b.release_branch ?? 'main',
  dev: b.addresses.dev ?? '',
  production: b.addresses.production ?? '',
})

function bodyOf(d: Draft): Record<string, unknown> {
  if (d.mode === 'custom') return { mode: 'custom' }
  const production = d.production.trim() || null
  return d.mode === 'pr_integration'
    ? { mode: d.mode, addresses: { production } }
    : { mode: d.mode, release_branch: d.release_branch.trim(), addresses: { dev: d.dev.trim() || null, production } }
}

export default function CiPipelineSection({ projectId, canEdit }: { projectId: number; canEdit: boolean }): JSX.Element {
  const query = usePipelineBasic(projectId)
  if (query.data === undefined) {
    return query.isError
      ? <Banner severity="error" testId="ci-pipeline-error">Couldn’t load the pipeline. Reload to try again.</Banner>
      : <LoadingState />
  }
  return <Loaded projectId={projectId} canEdit={canEdit} basic={query.data} />
}

function Loaded({ projectId, canEdit, basic }: { projectId: number; canEdit: boolean; basic: PipelineBasic }): JSX.Element {
  const queryClient = useQueryClient()
  const { draft, setDraft, seed, dirty, reseed } = useSeededDraft(draftOf(basic), sameJson)
  const { busy, formError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  const patch = (next: Partial<Draft>) => setDraft(d => ({ ...d, ...next }))
  const locked = !canEdit || busy
  const custom = draft.mode === 'custom'
  const release = draft.mode === 'integration_release'

  if (basic.mode === 'none') {
    return <Typography variant="body2" sx={{ color: 'text.secondary' }}>Add a repository first.</Typography>
  }

  const write = () => run('Couldn’t save the pipeline', async () => {
    const next = await put<PipelineBasic>(`/api/projects/${projectId}/pipeline/basic`, bodyOf(draft))
    queryClient.setQueryData(pipelineBasicKey(projectId), next)
    reseed(draftOf(next))
    void queryClient.invalidateQueries({ queryKey: pipelineKey(projectId) })
    void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
    void queryClient.invalidateQueries({ queryKey: ['project'] })
  })
  // Leaving a custom pipeline drops its stages and rules beyond the basic's: said before it happens.
  const save = async () => {
    if (basic.mode === 'custom' && !custom && !await confirm({
      title: 'Replace the custom pipeline?',
      message: 'Its stages and step rules become the basic pipeline’s. Earlier versions stay in the pipeline’s history.',
      confirmLabel: 'Replace',
    })) return
    await write()
  }

  return (
    <Box data-testid="ci-pipeline-section" sx={{ maxWidth: grid(128), minWidth: 0, display: 'grid', rowGap: 1.5 }}>
      {basic.reset_at !== null && (
        <Banner severity="info" testId="ci-pipeline-reset">
          {`Reset to a basic pipeline on ${shortDate(basic.reset_at)}: custom pipelines aren’t on your plan.`}
        </Banner>
      )}
      {basic.read_only && (
        <Banner severity="warning" testId="ci-pipeline-read-only">
          Custom pipelines are no longer on your plan. This one runs as saved, read-only; pick a basic one to change it.
        </Banner>
      )}
      {formError && <Banner severity="error" testId="ci-pipeline-save-error">{formError}</Banner>}
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 2, rowGap: 0.5 }}>
        <SettingCheckbox
          name="pipeline_custom"
          label="Custom pipeline"
          help="Your own stages, steps and rules, edited in the Pipeline tab."
          checked={custom}
          // Off the plan the box only goes off: a read-only custom pipeline is left by picking a basic.
          disabled={locked || (!basic.custom_offered && !custom)}
          onChange={on => patch({ mode: on ? 'custom' : seed.mode === 'custom' ? 'integration_release' : seed.mode })}
          testId="ci-pipeline-custom"
          dense
        />
        {!basic.custom_offered && !custom && <UpgradeToActivate testId="ci-pipeline-upgrade" />}
        {basic.mode === 'custom' && custom && (
          <Link component={RouterLink} to="?tab=pipeline" data-testid="ci-pipeline-customize">Customize</Link>
        )}
      </Box>
      {!custom && (
        <>
          <ChoiceField<Basic> label="Pipeline" value={draft.mode as Basic} options={BASICS} disabled={locked}
            onChange={mode => patch({ mode })} testId="ci-pipeline-basic" />
          <Box data-testid="ci-pipeline-integration-branches">
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium }}>Integration branch</Typography>
            {basic.integration_branches.map(repo => (
              <Typography key={repo.repo_id ?? repo.name} variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                {basic.integration_branches.length > 1 ? `${repo.name}: ` : ''}<code>{repo.branch}</code>{' '}
                <Link component={RouterLink} to="?tab=general" data-testid="ci-pipeline-edit-branch">Edit</Link>
              </Typography>
            ))}
          </Box>
          <FieldRow>
            {release && (
              <DetailCell basis={grid(26.5)} max={grid(37)}>
                <FormTextField id="ci-release-branch" label="Release branch" value={draft.release_branch}
                  help="A release moves Dev's code to this branch." disabled={locked}
                  error={draft.release_branch.trim() === '' ? 'Name the release branch.' : undefined}
                  onChange={release_branch => patch({ release_branch })} testId="ci-release-branch" />
              </DetailCell>
            )}
            {release && (
              <DetailCell basis={grid(37)} max={grid(56)}>
                <FormTextField id="ci-address-dev" label="Dev address" value={draft.dev} placeholder="https://"
                  help="Where Dev runs." disabled={locked} onChange={dev => patch({ dev })} testId="ci-address-dev" />
              </DetailCell>
            )}
            <DetailCell basis={grid(37)} max={grid(56)}>
              <FormTextField id="ci-address-production" label="Production address" value={draft.production}
                placeholder="https://" help="Where Production runs." disabled={locked}
                onChange={production => patch({ production })} testId="ci-address-production" />
            </DetailCell>
          </FieldRow>
        </>
      )}
      {!custom && basic.mode !== 'custom' && (
        <ProductionQaConnectDeploys projectId={projectId} canEdit={canEdit} title="Deploy tracking" />
      )}
      {/* Drawn once the pipeline is edited, and named: on Customize it sits among the project form's own Save, and a
          clean pair cost a phone a full-width row. */}
      {(dirty || busy) && (
        <FocusSafe>
          <CancelSaveBar plain testId="ci-pipeline-actions" saveTestId="ci-pipeline-save" discardTestId="ci-pipeline-discard"
            onDiscard={() => setDraft(seed)} dirty={dirty} busy={busy} saveLabel="Save pipeline"
            disabled={locked || (release && draft.release_branch.trim() === '')}
            saveProps={{ onClick: () => void save() }} />
        </FocusSafe>
      )}
      {confirmDialog}
    </Box>
  )
}
