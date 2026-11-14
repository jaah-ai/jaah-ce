import { useEffect, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Step from '@mui/material/Step'
import StepLabel from '@mui/material/StepLabel'
import Stepper from '@mui/material/Stepper'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { Theme } from '@mui/material/styles'
import { apiDetail, apiFixUrl, get, post } from '../api'
import { useAuth, useCan } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { isEnabled } from '../lib/editionFeatures'
import { editionModule } from '../lib/editionModule'
import { ACCOUNTS_KEY, listAccounts } from './credentials/api'
import type { Account, NodeRecord, Org, OrgRef } from '../types'
import { customerUrl } from '../lib/customers'
import { enterActAs, readAsOnce } from '../lib/actAs'
import { taskDeepLinkPath } from '../lib/taskDeepLink'
import { scopeHref } from '../lib/taskScope'
import Breadcrumb from '../components/Breadcrumb'
import {
  blankRepoFields, buildBody, FIX_MARKED_REPOS, NEW_REPO_BRANCH, createCommands, emptyForm, filledRepos, formHasRepo,
  type GovernanceAuthority, governanceIsReadOnly, isHidden, NewRepoError, prepareRepos, type ProjectFormState, refusedBodyLoc,
  type RepoDraft, repoName, repoRowErrors, type RepoRowErrors, withPrimary,
} from '../components/projects/projectForm'
import { ProjectNameField } from '../components/projects/ProjectNameField'
import { IDEAS_ONLY_PRESET } from '../lib/pipeline'
import {
  AGE_QUESTION, type AppAge, type AppKind, DESCRIBE_YOUR_APP, DESCRIBE_YOUR_INITIATIVE,
  DescriptionField, EasyModeFields, intakeKind, KIND_QUESTION, type ProjectWhat, QuestionCards, WHAT_QUESTION,
} from '../components/projects/EasyModeFields'
import ProjectRepoFields, { useFocusTarget } from '../components/projects/ProjectRepoFields'
import { useNoOwnerToPick } from '../components/projects/NewRepoFields'
import RepoGitConnect, { GIT_ACCOUNT_NEW_PATH } from '../components/projects/RepoGitConnect'
import { REPO_OWNERS_KEY } from '../components/projects/githubOAuthPopup'
import { resetRepoLookupCache } from '../ui/RepoUrlLookup'
import { useNodesByActivity, workNodes } from '../hooks/useNodesRoster'
import { PROJECTS_KEY, WORKTREES_KEY } from '../hooks/useRosters'
import { usePoll } from '../hooks/usePoll'
import { usePickAdvance } from '../hooks/usePickAdvance'
import { useLeaveGuard } from '../hooks/useLeaveGuard'
import { useNestedCreate } from '../hooks/useNestedCreate'
import { returnUrl } from '../lib/nestedCreate'
import { ADD_NODE_PATH } from '../components/addNode/deepLink'
import Banner from '../ui/Banner'
import { DisabledReason } from '../ui/DisabledReason'
import { DetailPage } from '../ui/detail/DetailPage'
import FormActionBar from '../ui/FormActionBar'
import FormField from '../ui/FormField'
import { tapTargetSx } from '../ui/listStyles'
import { OrgAside } from '../ui/OrgIndicator'
import OrgSelect, { ORG_FIELD_LABEL } from '../ui/OrgSelect'
import { LeaveGuardDialog } from '../ui/UnsavedChangesDialog'
import { FONT_WEIGHT, SIZE } from '../theme'

/* `/projects/new` — one page, for every org, in steps under a step bar (the Add node page's shape,
 * `components/addNode/AddNodePage.tsx`): What are you making? → Is it new or existing? (an app only: a business
 * initiative is always new) → Details. The first two are large cards (`EasyModeFields.tsx::QuestionCards`); a pointer
 * pick moves on (`hooks/usePickAdvance.ts`), a keyboard pick only selects and Next moves on. Back keeps every answer.
 * The Details step shows what the answer needs, then a "What happens next" note, Cancel, Back and Create project:
 *   - a new app: Name, (a web app) the Address it gets, What kind of app? (Web app, picked to start, or Something
 *     else) and (a web app) Describe your app. Jaah makes the repository; a web app is hosted and lands on its App
 *     Wizard epic;
 *   - a business initiative: Name and Describe the initiative. Jaah makes a private repository and starts its Business
 *     Wizard task, where the page lands;
 *   - an existing app: Repository (required; a picked repo names the project) and Name.
 * No agent is picked here: the project runs on the org's default until its edit page (`ProjectEditPage`) sets one.
 * It is placed on every enabled node the viewer may place on (an org admin); a web app in an org with a usable
 * shared node runs on that node instead (the server drops the placement); which nodes run it is its Node groups (the edit
 * page's Advanced fold). Everything else goes
 * out at its `emptyForm` seed. Create posts ONE `POST /api/projects` carrying the answer as `intake`
 * (`apps/api/app/project_intake.py`), after doing the forge's work for an existing app's rows: it makes each
 * repository asked for ("Create a new repository") and gives each empty one main and develop
 * (`projectForm.ts::prepareRepos`). */

type StepId = 'what' | 'age' | 'details'

/** Each step's name on the step bar. */
const STEP_LABELS: Record<StepId, string> = { what: 'What you’re making', age: 'New or existing', details: 'Details' }

/** What Create starts, said on the Details step (only where it does start the onboarding task). */
const NEXT_FOR_APP = 'Click Create project and Jaah will automatically execute the initial setup and deployment of the app. The task may ask you questions and, after it’s done, you may proceed to implement new features.'
const NEXT_FOR_BUSINESS = 'Click Create project and Jaah will automatically set up your initiative. The task may ask you questions and, after it’s done, you can start adding ideas and tasks.'

/** The Repository field's words while an existing app names no repository (`project_intake.PICK_YOUR_REPO`). */
const PICK_YOUR_REPO = 'Pick your repository.'

/** How often the roster is read again while every placed node is offline. */
const OFFLINE_POLL_MS = 15_000

/** "a", "a and b", "a, b and c". */
const listed = (items: string[]): string =>
  items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`

/** The org picked under Organization; `slug` is what acting as it needs (`lib/actAs.ts`). */
type OrgPick = { id: string; name: string; slug?: string }

/** The page's input, carried to a nested create page and back (`hooks/useNestedCreate.ts`). */
type WizardDraft = {
  orgPick: OrgPick | null
  form: ProjectFormState
  what?: ProjectWhat | null
  age?: AppAge | null
  appKind?: AppKind | null
  step?: number
  description?: string
  madeRepos?: Record<string, { url: string; note: string }>
}

/* The Add node page (`cloud_nodes`, `/nodes/new`), offered where present (`lib/editionModule.ts`; a lazy glob, so
   nothing of it is bundled here): an edition without it offers no node to add here. */
const ADDS_NODES = editionModule(isEnabled('cloud_nodes'),
  () => import.meta.glob('../components/addNode/AddNodePage.tsx')) !== undefined

export default function ProjectCreateWizard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  /* New node and Connect open the Add node and Git account pages beside the list and come back here with this input
     restored (`hooks/useNestedCreate.ts`). The draft is a snapshot taken at the click; the leave guard's `leaveNow` goes,
     so the dirty form is not asked about. */
  const nested = useNestedCreate<() => WizardDraft>(
    (): WizardDraft => ({ orgPick, form, what, age, appKind, step: stepIndex, description, madeRepos }),
    (path: string): void => guard.leaveNow(path),
  )
  const draft: WizardDraft | null = nested.restored?.() ?? null

  /* The operator may file the project under another org (`routes/projects.py::create_project`); everyone else
  creates in their own, so the field is the operator's alone, and only where there is more than one org to pick
  (never on the single-user community edition). `null` = the viewer's own org. */
  const isOperator = admin?.is_operator === true
  const choosesOrg = isOperator && isEnabled('multi_org_tenancy')
  const [orgPick, setOrgPick] = useState<OrgPick | null>(draft?.orgPick ?? null)

  /* The opening questions' answers; `intake` is what they add up to (null while one the path needs is open). */
  const [what, setWhat] = useState<ProjectWhat | null>(draft?.what ?? null)
  const [age, setAge] = useState<AppAge | null>(draft?.age ?? null)
  // Asked on the Details step with Web app picked to start: most new apps are one.
  const [appKind, setAppKind] = useState<AppKind | null>(draft?.appKind ?? 'web')
  const intake = intakeKind(what, age, appKind)
  const existing = intake === 'existing_app'
  // A new web app, a new app of another kind, or a new business initiative: Jaah makes the repository.
  const jaahMade = intake !== null && !existing
  const webApp = intake === 'web_app'
  const newApp = intake === 'web_app' || intake === 'other_app'
  const business = intake === 'business'
  const [description, setDescription] = useState(draft?.description ?? '')
  /* Only an existing app may be filed under another org: a Jaah-made repository is made in the viewer's own
  (`project_intake.NOT_OWN_ORG`). */
  const choosesOrgHere = choosesOrg && !jaahMade
  const foreignOrg = choosesOrgHere && orgPick !== null && orgPick.id !== admin?.org?.id ? orgPick : null

  /* Placement mirrors `routes/pool_slots.py::require_slot_writer` (an org admin). Each task
  makes its own worktree on a placed node (`routes/projects.py::ProjectPlacement`). A web app on the org's shared node drops it on the server (`create_project`). */
  const placesNodes = isOrgAdminSeat(admin)
  const nodesQuery = useNodesByActivity(placesNodes)
  /* Every enabled node, online or not. Registered rows only: a presence-only host has no `id` and placement 404s
  it. None when seeding another org: its placement is that org's own to declare (`create_project` refuses it). The
  operator's roster holds every org's nodes, so only the own org's are placed: another org's would 404 the whole
  create. A Multi-Node Host runs no work, so it is never placed (`vnodes.refuse_work_on_host` 422s the whole
  create); its nodes are. None where nothing is placed: a disabled query keeps its last data. */
  const placementNodes = !placesNodes || foreignOrg !== null ? [] : workNodes(nodesQuery.data ?? []).filter(n =>
    n.enabled && n.id != null && (!isOperator || n.org?.id === admin?.org?.id))
  const placedNodes = placementNodes.map(n => n.id ?? '')
  const placing = placesNodes && placedNodes.length > 0
  const rosterPending = placesNodes && nodesQuery.isPending
  // Every placed node offline: its tasks wait, so the page says so and adds a node in place.
  const allOffline = placementNodes.length > 0 && placementNodes.every(n => n.status !== 'online')
  /* A node added here takes minutes to come online, after its page closed: re-read the roster while none is, so the
  warning goes and Create places on it (`usePoll`: nothing while the tab is hidden). Back from New node with one
  ordered, read it at once: it is placed on as soon as it is listed. */
  usePoll(() => nodesQuery.refetch(), allOffline ? OFFLINE_POLL_MS : null)
  const nodeOrdered = nested.picked?.pick === 'node'
  useEffect(() => { if (nodeOrdered) void nodesQuery.refetch() }, [nodeOrdered])

  /* The POST's governance door (`_require_authority_to_author_governance`): a viewer it refuses sends none of
  the seven, and the server seeds them exactly as `emptyForm` would. */
  const governanceQuery = useQuery({
    queryKey: ['project-governance-authority', 'create'],
    queryFn: () => get<GovernanceAuthority>('/api/projects/governance-authority'),
  })

  // The draft keeps the full model; only name, repo and forge are ever edited here.
  const [form, setForm] = useState<ProjectFormState>(() => draft?.form ?? emptyForm('create', createCommands(admin?.project_create_defaults)))
  const patch = (next: Partial<ProjectFormState>) => setForm(f => ({ ...f, ...next }))
  const repos = form.repos ?? []
  const hasRepo = formHasRepo(form)
  const primaryBranchLocked = governanceIsReadOnly(form, governanceQuery.data?.may_author_governance)
  /* "Create a new repository" with no owner to pick (all blocked, none listed, or the read failed): Create has
  nowhere to go, so it is off; the field's own words say why and offer "Use an existing repository". */
  const noOwner = useNoOwnerToPick(repos.some(r => r.create !== undefined))
  /* Bumped when a Git connection lands above the Repository field (`RepoGitConnect`): the repository fields remount on
  a fresh lookup, so the account just connected lists its repositories (and owners). */
  const [gitConnects, setGitConnects] = useState(0)
  const afterGitConnect = () => {
    resetRepoLookupCache()
    // "Create a new repository" offers the owners the connections list: one just made adds its own.
    void queryClient.invalidateQueries({ queryKey: [REPO_OWNERS_KEY] })
    setGitConnects(n => n + 1)
  }
  // Back from the Git account page with an account connected: list its repositories.
  const gitConnected = nested.picked?.pick === 'git'
  useEffect(() => { if (gitConnected) afterGitConnect() }, [gitConnected])
  /* Cancel goes back where the page was opened from (`?return=`: the Command Center, the task tree), else Projects. */
  const { search } = useLocation()
  const cancelTo = returnUrl(search, window.location.origin, null) ?? '/projects'

  /* The create files AND starts the onboarding task only for a viewer holding both (`project_onboarding.py`); in
  another org, as its Owner seat. */
  const can = useCan()
  /* No account in the org the project lands in: no agent can run anything yet, so say so and where to add one. Only
  on a definite empty list (`accounts:view`, the `/accounts` route's key); a failed read says nothing. Another org's
  is read as that org (`readAsOnce`), so its own roster answers. */
  const foreignRef: OrgRef | null = foreignOrg && { id: foreignOrg.id, name: foreignOrg.name, slug: foreignOrg.slug ?? '' }
  const accountsQuery = useQuery({
    queryKey: foreignOrg ? [...ACCOUNTS_KEY, 'org', foreignOrg.id] : ACCOUNTS_KEY,
    queryFn: () => (foreignOrg ? get<Account[]>('/api/accounts', ...readAsOnce(foreignRef)) : listAccounts()),
    enabled: can('accounts:view'),
  })
  const noAccount = Array.isArray(accountsQuery.data) && accountsQuery.data.length === 0
  /* Another org with no node online runs nothing: its onboarding task waits, so the page says so. Its own roster,
  read as it; the viewer's own org places on the nodes above (`allOffline`). */
  const foreignNodesQuery = useQuery({
    queryKey: ['project-wizard-org-nodes', foreignOrg?.id],
    queryFn: () => get<NodeRecord[]>('/api/nodes', ...readAsOnce(foreignRef)),
    enabled: foreignOrg !== null,
  })
  const foreignNoNode = foreignOrg !== null && Array.isArray(foreignNodesQuery.data)
    && !workNodes(foreignNodesQuery.data).some(n => n.enabled && n.id != null && n.status === 'online')
  /* Nobody to push as there (no enabled Owner): its onboarding task is filed but cannot start
  (`project_onboarding.py`). The operator's read of that org (`has_owner`). */
  const foreignOrgQuery = useQuery({
    queryKey: ['project-wizard-org', foreignOrg?.id],
    queryFn: () => get<Org>(`/api/orgs/${encodeURIComponent(foreignOrg?.id ?? '')}`),
    enabled: foreignOrg !== null,
  })
  const foreignNoOwner = foreignOrg !== null && foreignOrgQuery.data?.has_owner === false
  /* What another org lacks to run its onboarding task, worst first: each one's fix is that org's to make. */
  const foreignLacks = foreignOrg === null ? [] : [
    ...(foreignNoOwner ? ['an Owner'] : []),
    ...(noAccount ? ['an agent'] : []),
    ...(foreignNoNode ? ['a node online'] : []),
  ]
  const onboards = can('tasks:create') && can('tasks:edit')
  /* Fixing another org's setup is done as that org: this tab acts as it (`lib/actAs.ts`), then opens the page. */
  const actAsForeign = () => { if (foreignRef) enterActAs(foreignRef) }

  const [creating, setCreating] = useState(false)
  /* Read at call time, as `hooks/useSubmit.ts`'s guard: a second Create in the same tick still sees the first's
  flag, where `creating` would be the stale closure's `false` — and post twice. */
  const creatingRef = useRef(false)
  const [saveError, setSaveError] = useState('')
  // Where the banner's refusal is fixed (`apiFixUrl`): the operator's Settings section for a setup not finished.
  const [saveFix, setSaveFix] = useState<string | null>(null)
  // A refusal of one field, under it; `repo` is the Repository field's, read beside Create.
  const [fieldError, setFieldError] = useState<{ key: 'name' | 'repo' | 'description'; message: string } | null>(null)
  // The Repository line's per-row messages (`ProjectRepoLine`), keyed by row.
  const [repoErrors, setRepoErrors] = useState<RepoRowErrors>({})
  /* The rows the forge's work last finished for (`prepare`): while they are still this very list, a second Create
  does not repeat it. */
  const preparedRef = useRef<RepoDraft[] | null>(null)
  /* The rows "Create a new repository" made, by key: their URL then. */
  const [madeRepos, setMadeRepos] = useState<Record<string, { url: string; note: string }>>(draft?.madeRepos ?? {})

  /* A repository already made belongs to an existing project: a Jaah-made answer would leave it to no project, so
  New is off once one is made. */
  const madeOne = Object.keys(madeRepos).length > 0
  const MADE = 'Off: your repository is already made.'

  /* The steps this path walks: a business initiative is always new, so it skips New or existing. */
  const steps: StepId[] = what === 'business' ? ['what', 'details'] : ['what', 'age', 'details']
  const [stepIndex, setStepIndex] = useState(draft?.step ?? 0)
  const stepId = steps[Math.min(stepIndex, steps.length - 1)]
  const onLast = stepId === 'details'
  const phone = useMediaQuery((t: Theme) => t.breakpoints.down('sm'))
  // A new step moves focus to its heading, so a keyboard or screen-reader user starts at the top of it.
  const headingRef = useRef<HTMLHeadingElement | null>(null)
  const moved = useRef(false)
  useEffect(() => {
    if (moved.current) headingRef.current?.focus()
    moved.current = true
  }, [stepId])

  /* An answer that differs from the one held drops what belonged to the old path, and the old path's refusals. */
  const changed = () => {
    setSaveError('')
    setFieldError(null)
    setRepoErrors({})
  }
  const pickWhat = (v: ProjectWhat) => {
    if (v !== what) { setAge(null); setAppKind('web'); changed() }
    setWhat(v)
    advance.picked()
  }
  const pickAge = (v: AppAge) => {
    if (v !== age) changed()
    setAge(v)
    advance.picked()
  }
  const answered = stepId === 'what' ? what !== null : stepId === 'age' ? age !== null : true
  // Every answer the path needs is in (Back keeps them); `ready` = also on the Details step, where its fields show.
  const answeredAll = intake !== null
  const ready = onLast && answeredAll

  /** Next: the following step once this one is answered. */
  function next() {
    if (!answered || onLast) return
    setStepIndex(Math.min(steps.indexOf(stepId) + 1, steps.length - 1))
  }
  // A pointer pick on a card moves on as Next would.
  const advance = usePickAdvance(() => next())
  function back() {
    advance.cancel()
    setFieldError(null)
    setStepIndex(Math.max(steps.indexOf(stepId) - 1, 0))
  }

  // A server refusal's field, focused once it is showing.
  const setFocusTarget = useFocusTarget(ready)
  const nameInputId = newApp ? 'easy-app-name' : 'project-name'
  const descriptionId = webApp ? 'easy-app-description' : 'project-description'

  /* The first thing Create cannot go past, said where it is and focused: a blank name, or an existing app's
  repository row missing a field. */
  function validate(): boolean {
    if (!answeredAll) return false
    /* The leave dialog's Create, pressed after Back: the fields are on Details, so a refusal there goes back to it
    (its focus target lands once the step shows, `useFocusTarget`). */
    if (!onLast) setStepIndex(steps.length - 1)
    if (existing && (!hasRepo || noOwner)) {
      setFieldError({ key: 'repo', message: PICK_YOUR_REPO })
      setFocusTarget(repos.length <= 1 ? 'project-repo-0-url' : 'project-repo-add')
      return false
    }
    if (existing) {
      const sentRepos = withPrimary(filledRepos(repos))
      const blank = blankRepoFields(sentRepos)
      const index = repos.findIndex(r => blank[r.key])
      if (index !== -1) {
        const e = blank[repos[index].key]
        const [field, message] = e.owner ? ['owner', 'Pick an owner']
          : e.name ? ['name', e.name === 'Required' ? 'Name the repository' : e.name]
          : ['branch', "Fill in each repository's branch"]
        setFieldError({ key: 'repo', message })
        setFocusTarget(`project-repo-${index}-${field}`)
        return false
      }
    }
    if (form.name.trim() === '') {
      setFieldError({ key: 'name', message: 'Required' })
      setFocusTarget(nameInputId)
      return false
    }
    if ((webApp || business) && description.trim() === '') {
      setFieldError({ key: 'description', message: webApp ? DESCRIBE_YOUR_APP : DESCRIBE_YOUR_INITIATIVE })
      setFocusTarget(descriptionId)
      return false
    }
    return true
  }

  function showField(key: string) {
    // A hidden field cannot be focused; the banner alone carries the server's words.
    if (isHidden(form, key)) return
    if (key === 'name') setFocusTarget(nameInputId)
    // One repository stands as bare fields (`ProjectRepoFields` `single`): no Add button to land on.
    else if (key === 'repos') setFocusTarget(repos.length <= 1 ? 'project-repo-0-url' : 'project-repo-add')
  }

  /* The forge's work for `rows` (`prepareRepos`): the rows to send, or null once a refusal is on its row (or, not
  the rows', in the banner). Called straight from a click: a hand-off owner's GitHub popup must open before any
  await. */
  async function prepare(rows: RepoDraft[]): Promise<RepoDraft[] | null> {
    setSaveError('')
    setRepoErrors({})
    if (rows === preparedRef.current) return rows
    const notes = Object.fromEntries(rows.flatMap(r => (r.create
      ? [[r.key, `${r.create.create_url ? '' : 'private, '}main and develop`]] : [])))
    // The README's title: the project's name, or the primary's own until Name is typed.
    const primary = withPrimary(filledRepos(rows)).find(r => r.is_primary)
    const title = form.name.trim() || (primary ? repoName(primary).split('/').pop() ?? '' : '')
    try {
      const out = await prepareRepos(rows, title, true, next => {
        patch({ repos: next })
        const made = next.filter(r => !r.create && notes[r.key] !== undefined)
        if (made.length) setMadeRepos(m => ({ ...m, ...Object.fromEntries(made.map(r => [r.key, { url: r.repo_url, note: notes[r.key] }])) }))
      })
      preparedRef.current = out
      return out
    } catch (err) {
      if (err instanceof NewRepoError) setRepoErrors({ [err.key]: { row: err.message } })
      // A repository that could not be made says why on its row; the banner only says where to look.
      setSaveError(err instanceof NewRepoError ? FIX_MARKED_REPOS : apiDetail(err, 'Failed to initialize the repository'))
      return null
    }
  }

  /* The created project's page, once the POST landed: the guard stands down, and the page moves there once the
     guard's patch is lifted (this effect runs after the guard's cleanup in the same commit), so the move is not
     taken for leaving. Replace: Back from the new project must not reopen an emptied page. */
  const [createdTo, setCreatedTo] = useState<{ path: string; state?: unknown } | null>(null)
  useEffect(() => {
    if (createdTo) navigate(createdTo.path, { replace: true, state: createdTo.state })
  }, [createdTo, navigate])

  function begin(): boolean {
    setFieldError(null)
    if (!validate() || creatingRef.current) return false
    creatingRef.current = true
    setCreating(true)
    setSaveError('')
    setSaveFix(null)
    return true
  }
  function fail(err: unknown) {
    setSaveFix(apiFixUrl(err))
    const field = refusedBodyLoc(err)?.[1]
    if (field === 'name') setFieldError({ key: 'name', message: apiDetail(err, 'Check the name.') })
    if (typeof field === 'string') showField(field)
    creatingRef.current = false
    setCreating(false)
  }

  /** The POST itself, and the lists a new project stales. */
  async function postProject(body: ReturnType<typeof buildBody>) {
    const created = await post<{
      id: number; placement_warning?: string | null; onboarding_task_id?: number | null
    }>('/api/projects', body)
    for (const queryKey of [PROJECTS_KEY, ['project'], WORKTREES_KEY]) void queryClient.invalidateQueries({ queryKey })
    return created
  }

  /** A Jaah-made project's one POST: the server makes its repository (and, for a web app, the hosted app) and
   *  starts its onboarding task (`routes/projects.py::_create_project_in`). A web app or business initiative lands
   *  on that task; otherwise the new project, which shows its repository and, for a web app, the app reading
   *  "Setting up" until it is live. A refusal (the plan's app cap, hosting not set up, a taken name) is the
   *  server's own words in the banner. */
  async function createJaahMade(kind: 'web_app' | 'other_app' | 'business'): Promise<boolean> {
    if (!begin()) return false
    try {
      const draft = { ...form, repos: [], ...createCommands(admin?.project_create_defaults) }
      const body = buildBody(draft, !governanceIsReadOnly(draft, governanceQuery.data?.may_author_governance))
      body.intake = kind === 'other_app' ? { kind } : { kind, description: description.trim() }
      /* No pipeline is picked here: a new business initiative deploys nothing, so it starts on Ideas only; a new app
      takes the server's default, Standard. An existing repository takes Standard too, and its onboarding task sets
      the stages the repo really deploys (`dispatch_compose/roles_build.py` ONBOARD). */
      if (kind === 'business') body.pipeline_preset = IDEAS_ONLY_PRESET
      if (placing) body.placement = { node_ids: placedNodes }
      const created = await postProject(body)
      /* A new web app lands on its App Wizard epic, a business initiative on its Business Wizard task, each already
      asking (`project_onboarding.py`). */
      if (kind !== 'other_app' && created.onboarding_task_id && !created.placement_warning) {
        setCreatedTo({ path: taskDeepLinkPath(created.onboarding_task_id, created.id) })
        return true
      }
      setCreatedTo({
        path: `/projects/${created.id}`,
        state: created.placement_warning ? { placementWarning: created.placement_warning } : undefined,
      })
      return true
    } catch (err) {
      setSaveError(apiDetail(err, 'Failed to create project'))
      fail(err)
      return false
    }
  }

  /** The one POST. Resolves true only when the project now exists (and the page is leaving for it). */
  async function create(): Promise<boolean> {
    if (intake === 'web_app' || intake === 'other_app' || intake === 'business') return createJaahMade(intake)
    if (!begin()) return false
    setRepoErrors({})
    const preparedRepos = form.repos && await prepare(form.repos)
    if (form.repos && !preparedRepos) {
      creatingRef.current = false
      setCreating(false)
      return false
    }
    try {
      // The commands are the org's defaults as `me` says them NOW: `me` may land after mount.
      const draft = { ...form, repos: preparedRepos, ...createCommands(admin?.project_create_defaults) }
      const body = buildBody(draft, !governanceIsReadOnly(draft, governanceQuery.data?.may_author_governance))
      /* Not when seeding another org: the server refuses any answer there (`project_intake.NOT_OWN_ORG`), and an
      existing app's answer only words its Agent instructions. */
      if (!foreignOrg) {
        body.intake = { kind: 'existing_app' }
      }
      if (placing) body.placement = { node_ids: placedNodes }
      if (foreignOrg) {
        body.org_id = foreignOrg.id
        // The viewer's org defaults; the server seeds the target org's own.
        delete body.gate_quick_cmd
        delete body.gate_full_cmd
        delete body.stack_up_cmd
        delete body.stack_down_cmd
      }
      const created = await postProject(body)
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
        // No Branch prefix field here: its refusal reads on the row.
        setRepoErrors(Object.fromEntries(Object.entries(rowErrors).map(([key, { branch_prefix, ...e }]) =>
          [key, branch_prefix && !e.row ? { ...e, row: branch_prefix } : e])))
      }
      fail(err)
      return false
    }
  }

  const dirty = form.name !== '' || repos.length !== 1
    || repos.some(r => r.repo_url !== '' || r.integration_branch !== NEW_REPO_BRANCH || r.forge !== '' || r.forge_base_url !== '')
    || repos.some(r => r.create !== undefined)
    || foreignOrg !== null
    || what !== null
    || description !== ''
  /* The dialog's Save is Create, but only once Create is offered: every question answered and the node roster in
  (earlier, it would place on nodes the page has not named yet). On success the page leaves for the new project
  itself (`createdTo`), so the gate must not go on to where the dialog was asked for. */
  const guard = useLeaveGuard(
    dirty && createdTo === null, async () => { if (answeredAll && !rosterPending) await create(); return false },
  )

  const createOff = existing && (noOwner || !hasRepo)

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'Projects', to: '/projects' }, { label: 'New project' }]} />}
      title="New project"
      headerTestId="project-page-header"
      measure="read"
    >
      <Box data-testid="project-wizard" sx={{ display: 'flex', flexDirection: 'column', gap: 3, maxWidth: SIZE.measure }}>
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
        {saveError && (
          <Banner
            severity="error"
            action={saveFix ? (
              <Button component={RouterLink} to={saveFix} color="inherit" size="small">Open settings</Button>
            ) : undefined}
          >
            {saveError}
          </Banner>
        )}

        <Box data-testid="project-steps">
          {phone ? (
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="project-step-count">
              Step {steps.indexOf(stepId) + 1} of {steps.length}: {STEP_LABELS[stepId]}
            </Typography>
          ) : (
            <Stepper activeStep={steps.indexOf(stepId)} aria-label="Steps">
              {steps.map(id => <Step key={id}><StepLabel>{STEP_LABELS[id]}</StepLabel></Step>)}
            </Stepper>
          )}
        </Box>

        <Typography
          ref={headingRef} id="project-step-heading" tabIndex={-1} component="h3" variant="subtitle1"
          data-testid="project-step-heading" sx={{ fontWeight: FONT_WEIGHT.semibold, outline: 'none', mb: -1 }}
        >
          {stepId === 'what' ? WHAT_QUESTION.title : stepId === 'age' ? AGE_QUESTION.title
            : existing ? 'Connect your repository' : business ? 'Tell us about your initiative' : 'Tell us about your app'}
        </Typography>

        {stepId === 'what' && (
          <QuestionCards
            question={WHAT_QUESTION} name="project-what" value={what} onPick={pickWhat} labelledBy="project-step-heading"
            off={madeOne ? { business: MADE } : undefined} groupProps={advance.groupProps}
          />
        )}
        {stepId === 'age' && (
          <QuestionCards
            question={AGE_QUESTION} name="project-age" value={age} onPick={pickAge} labelledBy="project-step-heading"
            off={madeOne ? { new: MADE } : undefined} groupProps={advance.groupProps}
          />
        )}

        {ready && (
          <Box data-testid="project-details" sx={{ display: 'flex', flexDirection: 'column', gap: 3 }}>
            {existing && (
              <Box>
                {/* Another org's project lists that org's connections, not the viewer's: nothing to connect here. */}
                {foreignOrg === null && <RepoGitConnect onConnect={() => nested.openNew(GIT_ACCOUNT_NEW_PATH, 'git')} />}
                {/* Held while Create's work runs: its rows replace the list, so an edit typed meanwhile would be lost. */}
                <Box component="fieldset" disabled={creating} sx={{ border: 0, p: 0, m: 0, minWidth: 0 }}>
                  <ProjectRepoFields
                    key={gitConnects}
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
                </Box>
              </Box>
            )}

            {newApp ? (
              <EasyModeFields
                name={form.name} onName={v => patch({ name: v })} nameError={fieldError?.key === 'name' ? fieldError.message : undefined}
                web={webApp}
                kind={(
                  <Box>
                    <Typography id="project-kind-label" variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium, mb: 1 }}>
                      {KIND_QUESTION.title}
                    </Typography>
                    <QuestionCards
                      question={KIND_QUESTION} name="project-kind" value={appKind} labelledBy="project-kind-label" compact
                      onPick={v => { if (v !== appKind) changed(); setAppKind(v) }}
                    />
                  </Box>
                )}
                description={description} onDescription={setDescription}
                descriptionError={fieldError?.key === 'description' ? fieldError.message : undefined}
              />
            ) : (
              <>
                <ProjectNameField
                  value={form.name} onChange={v => patch({ name: v })} error={fieldError?.key === 'name' ? fieldError.message : undefined}
                  help={jaahMade ? 'Names the project and its private repository.' : undefined}
                />
                {choosesOrgHere && (
                  <FormField label={ORG_FIELD_LABEL} help="Owns the project; its defaults and access apply.">
                    <OrgSelect
                      id="project-org-select"
                      value={(orgPick ?? admin?.org)?.id ?? null}
                      valueLabel={(orgPick ?? admin?.org)?.name}
                      onChange={(id, org) => {
                        setOrgPick({ id, name: org.name, slug: org.slug })
                        // Another org's project makes no repository here: a new row goes back to a blank one.
                        if (id !== admin?.org?.id) patch({ repos: repos.map(({ create: _create, ...r }) => r) })
                      }}
                      testId="project-org-select"
                    />
                  </FormField>
                )}
                {business && (
                  <DescriptionField
                    id="project-description" label="Describe the initiative"
                    help="What it is, who it's for, and what you want done. Jaah asks about anything missing."
                    placeholder="Find new wholesale buyers for our coffee roastery each week, email them, and track who replies."
                    value={description} onChange={setDescription}
                    error={fieldError?.key === 'description' ? fieldError.message : undefined}
                  />
                )}
              </>
            )}

            {placesNodes && nodesQuery.isError && <Banner severity="error">Failed to load nodes</Banner>}
            {allOffline && (
              <Banner
                severity="warning"
                role="status"
                testId="project-nodes-offline"
                action={ADDS_NODES && <Button size="small" color="inherit" onClick={() => nested.openNew(ADD_NODE_PATH, 'node')} data-testid="project-add-node">New node</Button>}
              >
                No node is online. Tasks wait until one is.
              </Banner>
            )}
            {/* The create files and starts one (`apps/api/app/project_onboarding.py`) for a viewer holding
            `tasks:create` and `tasks:edit`; in another org, as its Owner seat. It pins no engine and the project
            picks no agent here, so it runs on "Any" (`agent_resolution.resolve_agent` step 5): its first
            dispatch's account decides. With no account it waits for one, so the warning says that instead. */}
            {noAccount && foreignOrg === null && (
              <Banner
                severity="warning"
                testId="project-no-agent-note"
                action={<Button component={RouterLink} to="/accounts" size="small" color="inherit" data-testid="project-no-agent-link">Add account</Button>}
              >
                {!onboards ? 'No agent is set up, so no task can run.'
                  : webApp ? 'No agent is set up. Building your app waits for one.'
                    : 'No agent is set up. The first task waits for one.'}
              </Banner>
            )}
            {/* Another org that cannot run its onboarding task yet: what it lacks, and the way to add it as that org. */}
            {foreignOrg !== null && foreignLacks.length > 0 && (
              <Banner
                severity="warning"
                testId="project-org-not-ready"
                action={foreignNoOwner ? (
                  <Button component={RouterLink} to={customerUrl(foreignOrg.id, { tab: 'access' })} size="small" color="inherit" data-testid="project-org-open">
                    Open org
                  </Button>
                ) : noAccount ? (
                  <Button size="small" color="inherit" data-testid="project-org-add-account" onClick={() => { actAsForeign(); guard.leaveNow('/accounts') }}>
                    Add account
                  </Button>
                ) : ADDS_NODES ? (
                  <Button size="small" color="inherit" data-testid="project-org-add-node" onClick={() => { actAsForeign(); nested.openNew(ADD_NODE_PATH, 'node') }}>
                    New node
                  </Button>
                ) : undefined}
              >
                {`${foreignOrg.name} needs ${listed(foreignLacks)} before its first task can run.`}
              </Banner>
            )}

            {onboards && !(noAccount && foreignOrg === null) && foreignLacks.length === 0 && (
              <Banner severity="info" role="status" testId="project-onboarding-note">
                <Box component="span" sx={{ display: 'block', fontWeight: FONT_WEIGHT.semibold }}>What happens next</Box>
                {business ? NEXT_FOR_BUSINESS : NEXT_FOR_APP}
              </Banner>
            )}
          </Box>
        )}

        <FormActionBar testId="project-wizard-actions" stretchTestIds={['project-wizard-actions-next', 'project-wizard-actions-create']} plain>
          {fieldError?.key === 'repo' && (
            <Typography role="alert" variant="body2" data-testid="project-wizard-actions-blocked" sx={{ color: 'error.main', alignSelf: 'center', mr: 'auto' }}>
              {fieldError.message}
            </Typography>
          )}
          {/* Through the leave guard: a typed draft asks before it goes. */}
          <Button component={RouterLink} to={cancelTo} variant="text" disabled={creating} sx={tapTargetSx} data-testid="project-wizard-actions-cancel">
            Cancel
          </Button>
          {stepIndex > 0 && (
            <Button onClick={back} color="inherit" disabled={creating} sx={tapTargetSx} data-testid="project-wizard-actions-back">Back</Button>
          )}
          {onLast ? (
            /* Off with no repository picked: the reason is Create's hover, not a line under the field. With no
            owner to pick, the row's own words say why, so the hover stays quiet. */
            <DisabledReason reason={existing && !hasRepo && !noOwner && !creating ? PICK_YOUR_REPO : undefined}>
              <Button
                type="button"
                variant="contained"
                /* Until the roster answers, Create would place on none of the nodes the page is about to name. */
                disabled={!ready || creating || guard.saving || rosterPending || createOff}
                onClick={() => void create()}
                data-testid="project-wizard-actions-create"
                sx={tapTargetSx}
              >
                {creating ? 'Creating…' : 'Create project'}
              </Button>
            </DisabledReason>
          ) : (
            <DisabledReason reason={answered ? undefined : 'Pick one.'}>
              <Button
                type="button" variant="contained" disabled={!answered}
                /* Next pressed just after a click-pick would skip the step that pick is moving to. */
                onClick={() => { if (!advance.guarding()) next() }}
                data-testid="project-wizard-actions-next" sx={tapTargetSx}
              >
                Next
              </Button>
            </DisabledReason>
          )}
        </FormActionBar>
      </Box>

      <LeaveGuardDialog leave={guard} message="This project is not created yet. Create it before leaving?" />
    </DetailPage>
  )
}
