import { useEffect, useRef, useState, type JSX } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import Chip from '@mui/material/Chip'
import Link from '@mui/material/Link'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, get, post } from '../api'
import { useAuth, useCan } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { isEnabled } from '../lib/editionFeatures'
import { ACCOUNTS_KEY, listAccounts } from './credentials/api'
import { taskDeepLinkPath } from '../lib/taskDeepLink'
import { scopeHref } from '../lib/taskScope'
import Breadcrumb from '../components/Breadcrumb'
import AgentPicker, { ANY_AGENT_LABEL, useCanPickAgent } from '../components/agents/AgentPicker'
import {
  blankRepoFields, buildBody, FIX_MARKED_REPOS, NEW_REPO_BRANCH, createCommands, emptyForm, filledRepos, forgePickerShown, formHasRepo,
  type GovernanceAuthority, governanceIsReadOnly, isEmptyRepo, isHidden, NewRepoError, prepareRepos, type ProjectFormState, refusedBodyLoc,
  repoName, repoRowErrors, type RepoRowErrors, withPrimary,
} from '../components/projects/projectForm'
import ProjectRepoFields, { useFocusTarget } from '../components/projects/ProjectRepoFields'
import { useNoOwnerToPick } from '../components/projects/NewRepoFields'
import { useWizard, type WizardStep } from '../components/wizard/useWizard'
import WizardFooter from '../components/wizard/WizardFooter'
import WizardReview, { type WizardReviewRow } from '../components/wizard/WizardReview'
import WizardStepPanel from '../components/wizard/WizardStepPanel'
import WizardSteps from '../components/wizard/WizardSteps'
import { useNodesByActivity, workNodes } from '../hooks/useNodesRoster'
import { PROJECTS_KEY, WORKTREES_KEY } from '../hooks/useRosters'
import { NODE_STATUS_LABEL, NODE_UNKNOWN_TITLE } from '../lib/nodeStatus'
import type { NodeRecord } from '../types'
import { topPicks } from '../lib/topPicks'
import { useRemountDraft } from '../hooks/useRemountDraft'
import { useLeaveGuard } from '../hooks/useLeaveGuard'
import Banner from '../ui/Banner'
import { NewRepoChip, PrimaryChip } from '../ui/MarkChip'
import { DetailCell, DetailPage } from '../ui/detail/DetailPage'
import FieldRow from '../ui/FieldRow'
import StatusDot from '../ui/StatusDot'
import FormField from '../ui/FormField'
import FormTextField from '../ui/FormTextField'
import { NO_AUTOFILL_INPUT } from '../ui/noAutofill'
import { OrgAside } from '../ui/OrgIndicator'
import OrgSelect, { ORG_FIELD_LABEL } from '../ui/OrgSelect'
import UnsavedChangesDialog from '../ui/UnsavedChangesDialog'

/* `/projects/new` — the minimal create wizard (the shared stepper: `components/wizard/useWizard.ts`). It asks
 * only what a project cannot be born without: Repository (left blank: no repo; a picked repo names the project) → Name → Nodes (only where the viewer
 * may place and a node exists, or the roster is still answering; the five busiest online nodes come pre-picked) → Review. Everything else goes out at its `emptyForm` seed — the same body the
 * old create form sent untouched — and is changed afterwards on the edit page (`ProjectEditPage`), where
 * Create lands. Nothing is posted before Create. Create first makes each repository the Repository step asked for
 * ("Create a new repository") and gives each empty one main and develop (`projectForm.ts::prepareRepos`), then
 * posts the project. */

type StepId = 'repo' | 'name' | 'nodes' | 'review'

/** The fields a server refusal can name, by the step that shows them. */
const FIELD_STEPS: Record<string, { step: StepId; inputId: string }> = {
  name: { step: 'name', inputId: 'project-name' },
  repos: { step: 'repo', inputId: 'project-repo-add' },
  placement: { step: 'nodes', inputId: 'project-node' },
}

/** The wizard's input and position, carried across a layout switch (`hooks/useRemountDraft.ts`): crossing the
 *  docking width moves `/projects/new` between the list's pane and the full page, which remounts it. */
type WizardDraft = {
  orgPick: { id: string; name: string } | null
  chosenNodes: string[] | null
  form: ProjectFormState
  step: StepId
  index: number
  reached: StepId[]
}

export default function ProjectCreateWizard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  const { restored: draft, keep: keepDraft } = useRemountDraft<WizardDraft>()

  /* The operator may file the project under another org (`routes/projects.py::create_project`); everyone else
  creates in their own, so the field is the operator's alone, and only where there is more than one org to pick
  (never on the single-user community edition). `null` = the viewer's own org. */
  const isOperator = admin?.is_operator === true
  const choosesOrg = isOperator && isEnabled('multi_org_tenancy')
  const [orgPick, setOrgPick] = useState<{ id: string; name: string } | null>(draft?.orgPick ?? null)
  const foreignOrg = choosesOrg && orgPick !== null && orgPick.id !== admin?.org?.id ? orgPick : null

  /* Placement mirrors `routes/pool_slots.py::require_slot_writer` (an org admin). It declares no static slot:
  each task makes its own worktree on a placed node (`routes/projects.py::ProjectPlacement`). */
  const canPlace = isOrgAdminSeat(admin)
  const nodesQuery = useNodesByActivity(canPlace)
  /* Registered rows only: a presence-only host has no `id` and placement 404s it. A foreign pick is refused
  whole by the server (`routes/projects.py::_placement_nodes`), which writes nothing. None when seeding another
  org: its placement is that org's own to declare (`create_project` refuses it). The operator's roster holds every
  org's nodes, so only the own org's are offered: another org's would 404 the whole create. A Multi-Node Host runs
  no work, so it is never offered (`vnodes.refuse_work_on_host` 422s the whole create); its nodes are. Busiest first. */
  const placementNodes = foreignOrg !== null ? [] : workNodes(nodesQuery.data ?? []).filter(n =>
    n.enabled && n.id != null && (!isOperator || n.org?.id === admin?.org?.id))
  // null = untouched: the busiest online nodes (`lib/topPicks.ts`); the operator removes any.
  const [chosenNodes, setChosenNodes] = useState<string[] | null>(draft?.chosenNodes ?? null)
  /* A saved draft's pick is kept only while it is still offered: a host (or a gone node) it named would 422 the
  whole create, and the picker would not show it to remove. */
  const placedNodes = chosenNodes?.filter(id => placementNodes.some(n => n.id === id))
    ?? topPicks(placementNodes, n => n.status === 'online').map(n => n.id ?? '')

  /* The POST's governance door (`_require_authority_to_author_governance`): a viewer it refuses sends none of
  the seven, and the server seeds them exactly as `emptyForm` would. */
  const governanceQuery = useQuery({
    queryKey: ['project-governance-authority', 'create'],
    queryFn: () => get<GovernanceAuthority>('/api/projects/governance-authority'),
  })

  // The draft keeps the full model; only name, repo, forge and default agent are ever edited here.
  const [form, setForm] = useState<ProjectFormState>(() => draft?.form ?? emptyForm('create', createCommands(admin?.project_create_defaults)))
  const patch = (next: Partial<ProjectFormState>) => setForm(f => ({ ...f, ...next }))
  const repos = form.repos ?? []
  /* The rows sent, as `buildBody` sends them: a blank URL is no repository (`filledRepos`), a new one is; none is a
  project without one. */
  const sentRepos = withPrimary(filledRepos(repos))
  const hasRepo = formHasRepo(form)
  const primaryBranchLocked = governanceIsReadOnly(form, governanceQuery.data?.may_author_governance)
  const placing = canPlace && placedNodes.length > 0
  /* "Create a new repository" with no owner to pick (all blocked, none listed, or the read failed): Next has nowhere
  to go, so it is off; the step's own words say why and offer "Use an existing repository". */
  const noOwner = useNoOwnerToPick(repos.some(r => r.create !== undefined))

  /* The default agent the onboarding task runs on (`project_onboarding.py`). Not while seeding another org: the
  picker would offer the viewer's own org's agents, which the server refuses. */
  const agentPickShown = useCanPickAgent() && foreignOrg === null
  // Null while a picked agent's name loads: the note keeps its unpicked wording until the name is known.
  const [agentLabel, setAgentLabel] = useState<string | null>(ANY_AGENT_LABEL)
  const agentPicked = form.default_agent_id !== null || form.default_provider !== ''
  /* The create files AND starts the onboarding task only for a viewer holding both (`project_onboarding.py`). */
  const can = useCan()
  /* No account in the org: no agent can run anything yet, so say so and where to add one. Only on a definite
  empty list (`accounts:view`, the `/accounts` route's key); a failed read says nothing. */
  const accountsQuery = useQuery({
    queryKey: ACCOUNTS_KEY, queryFn: listAccounts, enabled: can('accounts:view') && foreignOrg === null,
  })
  const noAccount = Array.isArray(accountsQuery.data) && accountsQuery.data.length === 0
  const onboards = can('tasks:create') && can('tasks:edit') && foreignOrg === null

  const [creating, setCreating] = useState(false)
  /* Read at call time, as `hooks/useSubmit.ts`'s guard: a second Create in the same tick still sees the first's
  flag, where `creating` would be the stale closure's `false` — and post twice. */
  const creatingRef = useRef(false)
  const [saveError, setSaveError] = useState('')
  const [fieldError, setFieldError] = useState<{ key: string; message: string } | null>(null)
  // The Repositories table's per-row messages (`ProjectReposTable`), keyed by row.
  const [repoErrors, setRepoErrors] = useState<RepoRowErrors>({})

  const rosterPending = canPlace && nodesQuery.isPending
  /* Offered while the roster is still answering too: a Next pressed before it lands would otherwise step straight
  past a Nodes step that appears a moment later, onto a Review that places nothing. */
  const nodesShown = canPlace && (placementNodes.length > 0 || (rosterPending && foreignOrg === null))
  const steps: WizardStep<StepId>[] = [
    repoStep(),
    { id: 'name', label: 'Name', valid: form.name.trim() !== '', invalidField: { inputId: 'project-name', label: 'Name' } },
    ...(nodesShown
      ? [{
        id: 'nodes' as const,
        label: 'Nodes',
        valid: !rosterPending,
        invalidMessage: 'Nodes are still loading.',
      }]
      : []),
    { id: 'review', label: 'Review', valid: true },
  ]
  const wizard = useWizard(steps, draft)
  useEffect(() => {
    keepDraft({ orgPick, chosenNodes, form, step: wizard.step, index: wizard.index, reached: wizard.reached })
  })

  /* Each row with a URL needs a branch, and each new one an owner and a valid name; a blank row is no repository,
  and none is valid. The first offender is the one named. */
  function repoStep(): WizardStep<StepId> {
    const base = { id: 'repo' as const, label: 'Repository' }
    const blank = blankRepoFields(sentRepos)
    const index = repos.findIndex(r => blank[r.key])
    if (index === -1) return { ...base, valid: true }
    const e = blank[repos[index].key]
    const [field, label, invalidMessage] = e.owner ? ['owner', 'Owner', 'Pick an owner']
      : e.name ? ['name', 'Repository name', e.name === 'Required' ? 'Name the repository' : e.name]
      : ['branch', 'Integration branch', "Fill in each repository's branch"]
    return { ...base, valid: false, invalidField: { inputId: `project-repo-${index}-${field}`, label }, invalidMessage }
  }

  // A server refusal's field, focused once its step is showing.
  const setFocusTarget = useFocusTarget(wizard.step)

  function showField(key: string) {
    const target = FIELD_STEPS[key]
    // A hidden field cannot be shown or focused; the banner alone carries the server's words.
    if (!target || isHidden(form, key)) return
    wizard.goTo(target.step)
    // One repository stands as bare fields (`ProjectRepoFields` `single`): no Add button to land on.
    setFocusTarget(key === 'repos' && repos.length <= 1 ? 'project-repo-0-url' : target.inputId)
  }

  /* The created project's page, once the POST landed: the guard stands down, and the page moves there once the
     guard's patch is lifted (this effect runs after the guard's cleanup in the same commit), so the move is not
     taken for leaving. Replace: Back from the new project must not reopen an emptied wizard. */
  const [createdTo, setCreatedTo] = useState<{ path: string; state?: unknown } | null>(null)
  useEffect(() => {
    if (createdTo) navigate(createdTo.path, { replace: true, state: createdTo.state })
  }, [createdTo, navigate])

  /** The one POST. Resolves true only when the project now exists (and the page is leaving for it). */
  async function create(): Promise<boolean> {
    if (!wizard.validateAll() || creatingRef.current) return false
    creatingRef.current = true
    setCreating(true)
    setSaveError('')
    setFieldError(null)
    setRepoErrors({})
    let preparedRepos: ProjectFormState['repos']
    try {
      preparedRepos = form.repos && await prepareRepos(form.repos, form.name, true, rows => patch({ repos: rows }))
    } catch (err) {
      if (err instanceof NewRepoError) setRepoErrors({ [err.key]: { row: err.message } })
      // A repository that could not be made says why on its row; the banner only says where to look.
      setSaveError(err instanceof NewRepoError ? FIX_MARKED_REPOS : apiDetail(err, 'Failed to initialize the repository'))
      wizard.goTo('repo')
      creatingRef.current = false
      setCreating(false)
      return false
    }
    try {
      // The commands are the org's defaults as `me` says them NOW: `me` may land after mount.
      const draft = { ...form, repos: preparedRepos, ...createCommands(admin?.project_create_defaults) }
      const body = buildBody(draft, !governanceIsReadOnly(draft, governanceQuery.data?.may_author_governance))
      if (placing) body.placement = { node_ids: placedNodes }
      if (foreignOrg) {
        body.org_id = foreignOrg.id
        // The viewer's org defaults; the server seeds the target org's own.
        delete body.gate_quick_cmd
        delete body.gate_full_cmd
        delete body.stack_up_cmd
        delete body.stack_down_cmd
      }
      // One half or neither (`ProjectCreate`, as the PUT): unpicked leaves the server's default.
      if (agentPickShown && form.default_agent_id) body.default_agent_id = form.default_agent_id
      else if (agentPickShown && form.default_provider) body.default_provider = form.default_provider
      const created = await post<{
        id: number; placement_warning?: string | null; onboarding_task_id?: number | null
      }>('/api/projects', body)
      void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
      void queryClient.invalidateQueries({ queryKey: ['project'] })
      void queryClient.invalidateQueries({ queryKey: WORKTREES_KEY })
      // A placement warning is the project page's to show (no `?tab=`: it opens on General); otherwise the
      // project's task list, on the onboarding task its create started (`apps/api/app/project_onboarding.py`)
      // when there is one.
      if (created.placement_warning) {
        setCreatedTo({ path: `/projects/${created.id}`, state: { placementWarning: created.placement_warning } })
      } else {
        setCreatedTo({
          path: created.onboarding_task_id
            ? taskDeepLinkPath(created.onboarding_task_id, created.id)
            : scopeHref({ kind: 'project', projectId: created.id }),
        })
      }
      return true
    } catch (err) {
      // A per-row refusal lands on the rows it names (`repoRowErrors`); the banner only says where to look.
      const rowErrors = repoRowErrors(err, withPrimary(filledRepos(preparedRepos ?? [])))
      setSaveError(rowErrors ? FIX_MARKED_REPOS : apiDetail(err, 'Failed to create project'))
      if (rowErrors) {
        setRepoErrors(rowErrors)
        wizard.goTo('repo')
      }
      const field = refusedBodyLoc(err)?.[1]
      if (typeof field === 'string') showField(field)
      creatingRef.current = false
      setCreating(false)
      return false
    }
  }

  const dirty = form.name !== '' || repos.length !== 1
    || repos.some(r => r.repo_url !== '' || r.integration_branch !== NEW_REPO_BRANCH || r.forge !== '' || r.forge_base_url !== '')
    || repos.some(r => r.create !== undefined)
    || chosenNodes !== null || foreignOrg !== null || agentPicked
  /* The dialog's Save is Create, but only where the footer's Create is: on Review, with the node roster in —
  earlier, it would create past values (the pre-picked nodes) the operator has not seen, so Save keeps them in the
  wizard instead. On success the page leaves for the new project itself (`createdTo`), so the gate must not go on
  to where the dialog was asked for. */
  const guard = useLeaveGuard(
    dirty && createdTo === null, async () => { if (wizard.isLast && !rosterPending) await create(); return false },
  )

  /* Claims the pre-pick only when it added something; an untouched empty field says why it is empty. */
  const nodesHelp = chosenNodes === null
    ? (placing ? 'Its tasks run here. The busiest online nodes are added.'
      : rosterPending ? 'Loading nodes…' : 'No node is online, so none is added. Add one, or add nodes later.')
    : placing ? 'Its tasks run here.' : 'None — add nodes later.'
  const errorFor = (key: string) => (fieldError?.key === key ? fieldError.message : undefined)
  const nodeNames = placementNodes.filter(n => placedNodes.includes(n.id ?? '')).map(n => n.name)
  const reviewRows: WizardReviewRow[] = [
    ...(hasRepo
      ? sentRepos.map(r => ({
        stepId: 'repo',
        label: 'Repository',
        value: (
          <Box component="span" data-testid="project-review-repo" sx={{ overflowWrap: 'anywhere' }}>
            {repoName(r)}{r.is_primary && <PrimaryChip />}{r.create && <NewRepoChip />}
            <Box component="span" sx={{ color: 'text.secondary' }}>
              {' · '}{r.create ? `${r.create.create_url ? '' : 'private, '}main and develop` : r.integration_branch.trim()}
              {isEmptyRepo(r) ? ' · empty: main and develop added' : ''}
              {forgePickerShown(r.repo_url) && r.forge === 'gitlab' ? ` · GitLab (self-managed)${r.forge_base_url.trim() ? ` at ${r.forge_base_url.trim()}` : ''}` : ''}
            </Box>
          </Box>
        ),
      }))
      : [{ stepId: 'repo', label: 'Repository', value: 'No repo' }]),
    { stepId: 'name', label: 'Name', value: form.name.trim() },
    ...(choosesOrg ? [{ stepId: 'name', label: ORG_FIELD_LABEL, value: (orgPick ?? admin?.org)?.name ?? '' }] : []),
    ...(agentPickShown ? [{ stepId: 'name', label: 'Default agent', value: agentLabel ?? '…' }] : []),
    ...(nodesShown
      ? [{ stepId: 'nodes', label: 'Nodes', value: nodeNames.length > 0 ? nodeNames.join(', ') : 'None' }]
      : []),
  ]

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'Projects', to: '/projects' }, { label: 'New project' }]} />}
      title="New project"
      headerTestId="project-page-header"
      measure="read"
    >
      <Box data-testid="project-wizard" sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {/* `admin.org` is the viewer's org, not where the create lands (`write_seam.stamp_org`), hence "Your org"
        (operator only; `ui/OrgIndicator.tsx`). Its own line, not the title's aside: at 390px an aside centres
        between the breadcrumb and the title and crowds both. */}
        <OrgAside
          label="Your org"
          labelTestId="project-org-viewer-label"
          org={admin?.org ?? null}
          chipTestId="project-org"
          wrap
        />
        {saveError && <Banner severity="error">{saveError}</Banner>}

        <WizardSteps steps={steps} wizard={wizard} label="New project steps" testId="project-wizard-steps" />

        <WizardStepPanel id="repo" wizard={wizard} label="Repository">
          <ProjectRepoFields
            form={form}
            patch={patch}
            repoErrors={repoErrors}
            onRepoErrorsChange={setRepoErrors}
            primaryBranchLocked={primaryBranchLocked}
            storedBranch={NEW_REPO_BRANCH}
            nameFromRepo
            single
            initEmpty
            canCreate={foreignOrg === null}
          />
        </WizardStepPanel>

        {/* Unnamed: its lead field is "Name" (the operator also gets Organization), and a group of the same name would shadow it. */}
        <WizardStepPanel id="name" wizard={wizard}>
          <FieldRow rowGap="0.75rem">
            <DetailCell basis="14rem" max="22rem">
              <FormTextField
                id="project-name"
                label="Name"
                help="Shown on tasks, boards and the project list."
                required
                requiredMark={false}
                value={form.name}
                onChange={v => patch({ name: v })}
                error={errorFor('name')}
              />
            </DetailCell>
            {choosesOrg && (
              <DetailCell basis="14rem" max="22rem">
                <FormField label={ORG_FIELD_LABEL} help="Owns the project; its defaults and access apply.">
                  <OrgSelect
                    id="project-org-select"
                    value={(orgPick ?? admin?.org)?.id ?? null}
                    valueLabel={(orgPick ?? admin?.org)?.name}
                    onChange={(id, org) => {
                      setOrgPick({ id, name: org.name })
                      setChosenNodes(null)
                      // Another org's project makes no repository here: a new row goes back to a blank one.
                      if (id !== admin?.org?.id) patch({ repos: repos.map(({ create: _create, ...r }) => r) })
                    }}
                    testId="project-org-select"
                  />
                </FormField>
              </DetailCell>
            )}
            {agentPickShown && (
              <DetailCell basis="14rem" max="22rem">
                <AgentPicker
                  id="project-default-agent"
                  label="Default agent"
                  help="Runs tasks that pick no agent."
                  value={{ agentId: form.default_agent_id, provider: form.default_provider }}
                  onChange={pick => patch({ default_agent_id: pick.agentId, default_provider: pick.provider })}
                  defaultLabel={ANY_AGENT_LABEL}
                  onPickLabel={setAgentLabel}
                />
              </DetailCell>
            )}
          </FieldRow>
        </WizardStepPanel>

        {nodesShown && (
          <WizardStepPanel id="nodes" wizard={wizard} label="Nodes">
            <Autocomplete
              multiple
              options={placementNodes}
              value={placementNodes.filter(n => placedNodes.includes(n.id ?? ''))}
              onChange={(_, picked) => setChosenNodes(picked.map(n => n.id ?? ''))}
              getOptionLabel={n => n.name}
              isOptionEqualToValue={(a, b) => a.id === b.id}
              /* A dot and the status word, as the Nodes roster shows: why an offline node was not pre-picked. A picked
                 chip keeps both, so the choice stays visible once made. */
              renderOption={({ key, ...props }, n) => (
                <Box component="li" key={key} {...props} sx={{ gap: 1 }}>
                  <NodeStatusDot status={n.status} />
                  <Typography variant="body2" noWrap sx={{ minWidth: 0, flex: 1 }}>{n.name}</Typography>
                  {n.status !== 'online' && <NodeStatusWord status={n.status} />}
                </Box>
              )}
              renderValue={(picked, getItemProps) => picked.map((n, index) => {
                const { key, ...itemProps } = getItemProps({ index })
                return (
                  <Chip
                    key={key}
                    {...itemProps}
                    size="small"
                    icon={<NodeStatusDot status={n.status} sx={{ ml: 1 }} />}
                    label={n.status === 'online' ? n.name : <>{n.name} <NodeStatusWord status={n.status} /></>}
                    data-status={n.status}
                  />
                )
              })}
              noOptionsText="No nodes"
              renderInput={params => (
                <TextField
                  {...params} id="project-node" label="Nodes" size="small"
                  helperText={nodesHelp}
                  slotProps={{ ...params.slotProps, htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT } }}
                />
              )}
              data-testid="project-nodes"
              sx={{ maxWidth: '40rem' }}
            />
          </WizardStepPanel>
        )}

        <WizardStepPanel id="review" wizard={wizard} label="Review">
          <WizardReview rows={reviewRows} onEdit={id => wizard.goTo(id as StepId)} testId="project-wizard-review" />
          {canPlace && nodesQuery.isError && <Banner severity="error">Failed to load nodes</Banner>}
          {(!canPlace || (nodesQuery.isSuccess && placementNodes.length === 0)) && (
            <Typography variant="body2" data-testid="project-placement-hint" sx={{ color: 'text.secondary', mt: 1.5 }}>
              An admin adds nodes on the{' '}
              <Link component={RouterLink} to="/nodes" data-testid="project-placement-link">Nodes</Link> page.
            </Typography>
          )}
          {/* On Review, beside the Create it describes (above the Repository step's fields it read as
          part of them): the create files and starts one
          (`apps/api/app/project_onboarding.py`) for a viewer holding `tasks:create` and `tasks:edit`, except when
          seeding another org. It pins no engine, so it runs on the project's default agent, the Name step's pick;
          unpicked, "Any" (`agent_resolution.resolve_agent` step 5): its first dispatch's account decides. With no
          account it waits for one: naming an engine beside "No agent is set up yet" read as a contradiction. */}
          {/* Once, on Review beside Create; above every step it repeated as each one was walked.
          With onboarding too it is one line: two lines both naming accounts read as two problems. */}
          {noAccount && (
            <Typography variant="body2" data-testid="project-no-agent-note" sx={{ color: 'warning.main', mt: 1.5 }}>
              {onboards
                ? 'No agent is set up yet: the Guided onboarding task waits for an account. Add one in '
                : 'No agent is set up yet, so no task can run. Add an account in '}
              <Link component={RouterLink} to="/accounts" data-testid="project-no-agent-link">Accounts</Link>.
            </Typography>
          )}
          {onboards && !noAccount && (
            <Typography variant="body2" data-testid="project-onboarding-note" sx={{ color: 'text.secondary', mt: 1.5 }}>
              {agentPicked && agentLabel !== null
                ? `Starts a Guided onboarding task on ${agentLabel}.`
                : `Starts a Guided onboarding task on the first free engine (${ANY_AGENT_LABEL}).`}
            </Typography>
          )}
        </WizardStepPanel>

        <WizardFooter
          wizard={wizard}
          onCreate={() => void create()}
          /* Until the roster answers, Create would skip the Nodes step the wizard is about to offer. */
          creating={creating || guard.saving || rosterPending}
          nextDisabled={wizard.step === 'repo' && noOwner}
          testId="project-wizard-actions"
        />
      </Box>

      <UnsavedChangesDialog
        open={guard.open}
        saving={guard.saving}
        onChoose={choice => void guard.onChoose(choice)}
        message="This project is not created yet. Create it before leaving?"
      />
    </DetailPage>
  )
}

type NodeStatus = NodeRecord['status']

function NodeStatusDot({ status, sx }: { status: NodeStatus, sx?: SxProps<Theme> }): JSX.Element {
  return <StatusDot tone={status === 'online' ? 'success' : 'neutral'} hollow={status !== 'online'} sx={sx} />
}

/** The roster's word for a node that is not online (`lib/nodeStatus.ts`), with its hover words for `unknown`. */
function NodeStatusWord({ status }: { status: NodeStatus }): JSX.Element {
  return (
    <Typography
      component="span" variant="caption" sx={{ color: 'text.secondary' }} data-testid="project-node-status"
      title={status === 'unknown' ? NODE_UNKNOWN_TITLE : undefined}
    >
      {NODE_STATUS_LABEL[status]}
    </Typography>
  )
}
