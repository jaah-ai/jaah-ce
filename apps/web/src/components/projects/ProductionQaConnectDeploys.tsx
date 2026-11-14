import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post } from '../../api'
import { useSeededDraft } from '../../hooks/useServerDraft'
import { useSubmit } from '../../hooks/useSubmit'
import { PROJECTS_KEY } from '../../hooks/useRosters'
import { azureRepositoryLabel, ForgeSetup } from '../../pages/automations/EventSourceFields'
import { gitlabEventLabel } from '../../pages/triggers/vocabulary'
import { FONT_WEIGHT, grid } from '../../theme'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import ExternalLink from '../../ui/ExternalLink'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import type { Explain } from '../../ui/explain'
import FormDialog from '../../ui/FormDialog'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import LinkButton from '../../ui/LinkButton'
import type { Forge } from '../../types'

/* CONNECT DEPLOYS (`routes/production_qa.py::connect_deploys`, architecture §5.1 step 1): names the deploy workflows
 * (GitLab: the CI config; Azure DevOps: pipelines) and creates the record-only triggers deploy ingestion listens on,
 * on the forge of the project's repository (`forge`: GitHub, GitLab or Azure DevOps — one service hook target). On
 * GitHub the API installs the webhooks itself and answers no secret: `connected`, or `needs_setup` — one line with
 * Install app and Set up manually, the manual setup (`connect-deploys/manual`) the only GitHub answer that shows
 * secrets. Any other forge's answer shows each webhook's form with its secret, once. A Bitbucket project instead names
 * its production branch (and, optionally, the build statuses that deploy) and gets one record-only webhook
 * (`connect_bitbucket_deploys`, org Admins: `can_connect`); its Save changes them later. Needs edit on the project.
 * The fields sit inline under the trigger list behind their own Connect/Save (the style guide's edit in place: a
 * single setting is a field on the record's page, not a dialog), seeded from what is saved — when nothing is, GitLab's
 * CI config, or GitHub's detected Production deploy workflow (`deploy-workflows`; other candidates offered beside the
 * field, one click to add) — and dirty against what is
 * saved, so that prefill is ready to Connect. A GitHub repository whose Pages build from the branch connects with no
 * workflow named. Like the tab's own draft, no leave guard. Only the answer's secrets open a dialog: a notice shown
 * once. On every plan (the API's `deploys_router`): besides the Production QA tab, Customize › CI pipeline (a basic
 * pipeline) and Pipeline's Production stage (a custom one) draw it, since a release waits for its deploy only once
 * deploys are connected (`releases/runs.py::deploy_tracked`). */

type DeployTrigger = {
  id: string
  name: string
  source_kind: Forge
  events: string[]
  /** `owner/repo`, a GitLab project path, an Azure DevOps `organization/project/repo`; '' for any. */
  repository: string
  ingress_url: string | null
  paused: boolean
  /** A Bitbucket webhook's production branch and deploy statuses. */
  production_branch?: string | null
  statuses?: string[]
  secret?: string | null
}
/** `forge`: which forge Connect creates webhooks on; null when no supported repository is connected. */
type DeployTriggers = {
  forge?: Forge | null; deploy_workflows: string[]; triggers: DeployTrigger[]; can_connect?: boolean
  /** GitHub: a trigger no installed webhook carries yet — Connect is unfinished. */
  setup_owed?: boolean
}
/** A connect's answer. GitHub: `connected`, or `needs_setup` with the App's install page; no secret either way. */
type ConnectAnswer = DeployTriggers & {
  state?: 'connected' | 'needs_setup' | null
  reason?: 'no_reach' | 'no_permission' | null
  install_url?: string | null
  owner?: string | null
}
/** GitHub's detected deploy workflows (`github_hooks.detect_workflows`): `workflows` prefilled, `optional` offered. */
type DeployWorkflows = { workflows: string[]; optional?: string[]; pages_branch_built: boolean; readable: boolean }

/* What each forge calls the deploy config, where its webhooks are added, and the events that mark each webhook. */
const FORGE_WORDS: Record<Forge, {
  forge: string; field: string; help: Explain | ''; placeholder: string; title: string; where: string; runs: string
  jobs: string
}> = {
  github: {
    forge: 'GitHub', field: 'Deploy workflows', help: {
      tip: 'Name the workflow that deploys Production.',
      more: 'Its runs count as releases. One repo-relative .yml path per line, up to 20. Added to the project\'s '
        + 'Release workflows.',
    },
    placeholder: 'deploy.yml', title: 'Add these webhooks in GitHub',
    where: 'Secrets are shown only now. Repository › Settings › Webhooks › Add webhook, once per row.',
    runs: 'workflow_run', jobs: 'workflow_job',
  },
  gitlab: {
    forge: 'GitLab', field: 'CI config', help: {
      tip: 'Jobs in this pipeline file count as releases.',
      more: 'One repo-relative .yml path per line. Added to the project\'s Release workflows.',
    },
    placeholder: '.gitlab-ci.yml', title: 'Add these webhooks in GitLab',
    where: 'Secrets are shown only now. Project › Settings › Webhooks › Add new webhook, once per row.',
    runs: 'Pipeline Hook', jobs: 'Job Hook',
  },
  azure_devops: {
    forge: 'Azure DevOps', field: 'Pipelines to add', help: {
      tip: 'Runs of these pipelines or Azure releases count as releases.',
      more: 'One pipeline or release name per line, up to 20. Added to the project\'s Release pipelines.',
    },
    placeholder: 'Deploy', title: 'Add these service hooks in Azure DevOps',
    where: 'The password is shown only now. Project settings › Service hooks › Web Hooks, once per event.',
    runs: 'ms.vss-pipelines.run-state-changed-event', jobs: 'ms.vss-pipelines.stage-state-changed-event',
  },
  // One webhook, named by its production branch (its own form below): `runs`/`jobs` are its trigger events.
  bitbucket: {
    forge: 'Bitbucket', field: '', help: '', placeholder: '', title: 'Add this webhook in Bitbucket',
    where: 'The secret is shown only now. Repository settings › Webhooks › Add webhook.',
    runs: 'repo:push', jobs: 'repo:commit_status_updated',
  },
}

const CONNECT_HELP: Explain = {
  tip: 'Adds forge webhooks so this project\'s releases are recorded.',
  more: 'On GitHub, Jaah adds the webhooks. Elsewhere each webhook\'s secret is shown once, when it is created.',
  docs: 'using-jaah/production-qa/turn-on-production-qa-for-a-project',
}

const keyOf = (projectId: number) => ['production-qa-deploy-triggers', projectId] as const

/** The project's deploy triggers, one read and cache key for every surface that draws or asks about them. */
export function useDeployTriggers(projectId: number, enabled = true) {
  return useQuery({
    queryKey: keyOf(projectId),
    queryFn: () => get<DeployTriggers>(`/api/projects/${projectId}/production-qa/deploy-triggers`),
    retry: (count, error) => !(error instanceof ApiError && error.status === 403) && count < 2,
    enabled,
  })
}

/** Deploys are recorded: every deploy trigger of the project's forge exists (matched by the events each listens to,
 *  as the API keys them; Azure DevOps' one trigger lists both) and no GitHub setup is still owed. */
export function deploysConnected(data: DeployTriggers | undefined): boolean {
  if (!data) return false
  const words = FORGE_WORDS[data.forge ?? 'github']
  return data.triggers.some(t => t.events.includes(words.runs)) && data.triggers.some(t => t.events.includes(words.jobs))
    && !data.setup_owed
}
const lines = (text: string) => text.split('\n').map(p => p.trim()).filter(Boolean)
const fileName = (path: string) => path.split('/').pop() ?? path

type Draft = { paths: string; branch: string; statuses: string }
const sameDraft = (a: Draft, b: Draft) => a.paths === b.paths && a.branch === b.branch && a.statuses === b.statuses

/** What is saved: the deploy workflows, and the Bitbucket webhook's branch and statuses. */
function savedOf(data: DeployTriggers | undefined): Draft {
  const hook = (data?.triggers ?? []).find(t => t.source_kind === 'bitbucket')
  return {
    paths: (data?.deploy_workflows ?? []).join('\n'),
    branch: hook?.production_branch ?? '',
    statuses: (hook?.statuses ?? []).join('\n'),
  }
}

/** The draft's seed: what is saved; when no workflow is, GitLab's CI config or GitHub's detected workflows. */
function seedOf(data: DeployTriggers | undefined, detected?: DeployWorkflows): Draft {
  const saved = savedOf(data)
  if (saved.paths) return saved
  if (data?.forge === 'gitlab') return { ...saved, paths: FORGE_WORDS.gitlab.placeholder }
  if (data?.forge === 'github' && detected?.workflows.length) return { ...saved, paths: detected.workflows.join('\n') }
  return saved
}

/** The one line a GitHub connect nothing could install shows, naming the action that fixes it. */
function setupLine(answer: ConnectAnswer): string {
  const owner = answer.owner ?? 'the repository owner'
  return answer.reason === 'no_permission'
    ? `Allow the Jaah GitHub App to manage webhooks on ${owner} to connect.`
    : `Install the Jaah GitHub App on ${owner}, and connect GitHub as a repository admin, to connect.`
}

/** A deploy trigger's row: its repository (Azure DevOps), branch and statuses (Bitbucket), else its events. */
function describe(t: DeployTrigger): string {
  if (t.source_kind === 'azure_devops') return azureRepositoryLabel(t.repository)
  if (t.source_kind === 'bitbucket') {
    const statuses = t.statuses ?? []
    return `${t.production_branch ?? ''} · ${statuses.length ? statuses.join(', ') : 'every build status'}`
  }
  return t.events.map(e => (t.source_kind === 'gitlab' ? gitlabEventLabel(e) : e)).join(', ')
}

/** A deploy trigger's webhook-setup test id: one per forge, GitHub and GitLab split by their jobs hook. */
function webhookTestId(t: DeployTrigger): string {
  if (t.source_kind === 'bitbucket') return 'production-qa-webhook-bitbucket'
  if (t.source_kind === 'azure_devops') return 'production-qa-webhook-azure-devops'
  const jobs = t.source_kind === 'gitlab' ? 'Job Hook' : 'workflow_job'
  return `production-qa-webhook-${t.events.includes(jobs) ? 'jobs' : 'deploys'}`
}

const CONNECT_FORM_ID = 'production-qa-connect-form'

export default function ProductionQaConnectDeploys({ projectId, canEdit, title = 'Connect releases' }: {
  projectId: number
  canEdit: boolean
  /** The section's title: "Deploy tracking" where a pipeline's Production stage offers it. */
  title?: string
}): JSX.Element {
  const queryClient = useQueryClient()
  const query = useDeployTriggers(projectId)
  const github = query.data?.forge === 'github'
  const enabledDetection = github && canEdit
    && ((query.data?.triggers.length ?? 0) === 0 || query.data?.setup_owed === true)
  // GitHub's prefill, read once while not connected; a repository nobody can read answers empty, never an error.
  const detection = useQuery({
    queryKey: [...keyOf(projectId), 'workflows'],
    queryFn: () => get<DeployWorkflows>(`/api/projects/${projectId}/production-qa/deploy-workflows`),
    enabled: enabledDetection,
    retry: false,
    staleTime: Infinity,
  })
  const detected = detection.data
  // With no workflow saved, Connect waits for the prefill: a quick click would otherwise connect with none named.
  const prefilling = detection.isLoading && (query.data?.deploy_workflows.length ?? 0) === 0
  const { draft, setDraft, reseed } = useSeededDraft(seedOf(query.data, detected), sameDraft)
  const { paths, branch, statuses } = draft
  const pagesBuilt = github && detected?.pages_branch_built === true
  // Other workflows that may deploy Production: offered, never prefilled, while connecting and the draft names
  // none of them (by file name, so a hand-typed `deploy.yml` counts).
  const named = new Set(lines(paths).map(fileName))
  const offered = github && enabledDetection ? (detected?.optional ?? []).filter(path => !named.has(fileName(path))) : []
  // A Pages repository built from its branch, or an unfinished GitHub connect, is ready to Connect as it stands.
  const dirty = !sameDraft(draft, savedOf(query.data)) || pagesBuilt || query.data?.setup_owed === true
  const [answer, setAnswer] = useState<DeployTriggers | null>(null)
  const [setup, setSetup] = useState<ConnectAnswer | null>(null)
  const { busy, formError, run } = useSubmit()

  const forge: Forge = query.data?.forge ?? 'github'
  const words = FORGE_WORDS[forge]
  const bitbucket = forge === 'bitbucket'
  const set = (next: Partial<Draft>) => setDraft(d => ({ ...d, ...next }))
  const settle = (next: ConnectAnswer) => {
    reseed(seedOf(next, detected))
    queryClient.setQueryData(keyOf(projectId), { ...next, triggers: next.triggers.map(t => ({ ...t, secret: null })) })
    void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
    void queryClient.invalidateQueries({ queryKey: ['project'] })
    // The pipeline page's Release step reads whether deploy events are connected.
    void queryClient.invalidateQueries({ queryKey: ['release'] })
  }
  const connect = () => run('Could not connect releases', async () => {
    const next = bitbucket
      ? await post<ConnectAnswer>(`/api/projects/${projectId}/production-qa/connect-bitbucket-deploys`, {
        production_branch: branch.trim(), statuses: lines(statuses),
      })
      : await post<ConnectAnswer>(`/api/projects/${projectId}/production-qa/connect-deploys`, {
        deploy_workflows: lines(paths),
      })
    settle(next)
    setSetup(next.state === 'needs_setup' ? next : null)
    // GitHub answers no secret; an edit of a connected Bitbucket webhook neither: nothing to add on the forge.
    if (!next.state && (!bitbucket || next.triggers.some(t => t.secret))) setAnswer(next)
  })
  const connectManually = () => run('Could not connect releases', async () => {
    const next = await post<ConnectAnswer>(`/api/projects/${projectId}/production-qa/connect-deploys/manual`, {
      deploy_workflows: lines(paths),
    })
    settle(next)
    setSetup(null)
    if (!next.state) setAnswer(next)
  })

  const denied = query.error instanceof ApiError && query.error.status === 403
  const triggers = query.data?.triggers ?? []
  const connected = deploysConnected(query.data)
  const answered = answer?.forge ?? forge
  const allowed = query.data?.can_connect !== false
  // A GitHub connect that could not install stays open: Connect again (after installing the app) or set up manually.
  const editable = !denied && !query.isError && query.data !== undefined && canEdit && allowed
    && (!connected || bitbucket || setup !== null) && query.data.forge !== null

  return (
    <DetailSection title={title} help={CONNECT_HELP} testId="production-qa-connect-deploys">
      {denied ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Maintainers set this up.</Typography>
      ) : query.isError ? (
        <Banner severity="error" testId="production-qa-deploys-error">Could not load the release webhooks.</Banner>
      ) : (
        <Stack spacing={1.5}>
          {triggers.length === 0 ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>Not connected.</Typography>
          ) : (
            <Box component="ul" sx={{ m: 0, pl: 2.5 }} data-testid="production-qa-deploy-triggers">
              {triggers.map(t => (
                <Box component="li" key={t.id} sx={{ py: 0 }}>
                  <Typography variant="body2" component="span" sx={{ fontWeight: FONT_WEIGHT.medium }}>{t.name}</Typography>
                  <Typography variant="body2" component="span" sx={{ color: 'text.secondary' }}>
                    {' · '}{describe(t)}{t.paused ? ' · Paused' : ''}
                  </Typography>
                </Box>
              ))}
            </Box>
          )}
          {!allowed && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="production-qa-deploys-admins">
              Org Admins connect Bitbucket releases.
            </Typography>
          )}
          {query.data?.forge === null && triggers.length === 0 && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="production-qa-deploys-no-repo">
              Needs a GitHub, GitLab, Azure DevOps or Bitbucket repository.
            </Typography>
          )}
          {editable && (
            /* Its own form (the tab sits outside the project's), so Enter in a field connects. */
            <Stack component="form" spacing={1.5} id={CONNECT_FORM_ID} noValidate
              onSubmit={e => { e.preventDefault(); void connect() }}>
              {bitbucket ? (
                <FieldRow rowGap={2}>
                  <DetailCell basis={grid(20)}>
                    <FormTextField
                      id="production-qa-connect-branch"
                      label="Production branch"
                      help="Build statuses on this branch count as production releases."
                      placeholder="main"
                      value={branch}
                      onChange={value => set({ branch: value })}
                      disabled={busy}
                      testId="production-qa-connect-branch"
                    />
                  </DetailCell>
                  <DetailCell basis={grid(20)}>
                    <FormTextField
                      id="production-qa-connect-statuses"
                      label="Release statuses"
                      help={{
                        tip: 'Only these build statuses count as releases.',
                        more: 'One build status key or name per line. Empty: every build status on the branch counts.',
                      }}
                      multiline
                      minRows={2}
                      value={statuses}
                      onChange={value => set({ statuses: value })}
                      disabled={busy}
                      testId="production-qa-connect-statuses"
                    />
                  </DetailCell>
                </FieldRow>
              ) : (
                <Stack spacing={0.5}>
                  <FormTextField
                    id="production-qa-connect-workflows"
                    label={words.field}
                    help={words.help || undefined}
                    placeholder={prefilling ? 'Reading the repository\'s workflows…' : words.placeholder}
                    multiline
                    minRows={2}
                    value={paths}
                    onChange={value => set({ paths: value })}
                    disabled={busy}
                    testId="production-qa-connect-workflows"
                  />
                  {offered.length > 0 && (
                    <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="production-qa-connect-optional">
                      {'Also found: '}
                      {offered.map((path, i) => (
                        <span key={path}>
                          {i > 0 && ', '}
                          <LinkButton variant="body2" disabled={busy} title="Add to Deploy workflows"
                            onClick={() => set({ paths: [...lines(paths), path].join('\n') })}>
                            {fileName(path)}
                          </LinkButton>
                        </span>
                      ))}
                    </Typography>
                  )}
                </Stack>
              )}
              {formError && <Banner severity="error" testId="production-qa-connect-error">{formError}</Banner>}
              {setup && (
                <Banner severity="warning" testId="production-qa-connect-setup">
                  <Stack spacing={1}>
                    <span>{setupLine(setup)}</span>
                    <Stack direction="row" spacing={1} useFlexGap sx={{ flexWrap: 'wrap' }}>
                      {setup.install_url && (
                        <Button size="small" variant="contained" component={ExternalLink} href={setup.install_url}
                          data-testid="production-qa-connect-install">
                          Install app
                        </Button>
                      )}
                      <Button size="small" variant="outlined" onClick={() => void connectManually()} disabled={busy}
                        data-testid="production-qa-connect-manual">
                        Set up manually
                      </Button>
                    </Stack>
                  </Stack>
                </Banner>
              )}
              <CancelSaveBar plain testId="production-qa-connect-actions" saveTestId="production-qa-connect-submit"
                discardTestId="production-qa-connect-discard" saveLabel={connected && !setup ? 'Save' : 'Connect'}
                /* Back to what is SAVED, not the seed: a prefilled CI config or workflow is a change of its own. */
                onDiscard={() => setDraft(savedOf(query.data))} dirty={dirty || setup !== null} busy={busy}
                disabled={bitbucket ? !branch.trim() : prefilling || (!paths.trim() && !pagesBuilt)}
                saveReason={prefilling ? 'Reading the repository\'s workflows' : undefined}
                saveProps={{ form: CONNECT_FORM_ID }} />
            </Stack>
          )}
        </Stack>
      )}
      <FormDialog
        open={answer !== null}
        modal
        hideCancel
        maxWidth="md"
        title={FORGE_WORDS[answered].title}
        submitLabel="Done"
        onSubmit={() => setAnswer(null)}
        onClose={() => setAnswer(null)}
        testId="production-qa-webhooks-dialog"
        submitTestId="production-qa-webhooks-done"
      >
        <Banner severity="warning" testId="production-qa-webhooks-warning">
          {FORGE_WORDS[answered].where}
        </Banner>
        <Stack spacing={3} sx={{ mt: 2 }}>
          {(answer?.triggers ?? []).map(t => (
            <Box key={t.id}>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>{t.name}</Typography>
              <ForgeSetup
                forge={t.source_kind}
                events={t.events}
                repository={t.repository}
                url={t.ingress_url ?? ''}
                secret={t.secret ?? null}
                testId={webhookTestId(t)}
              />
            </Box>
          ))}
        </Stack>
      </FormDialog>
    </DetailSection>
  )
}
