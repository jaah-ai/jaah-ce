import { type ComponentType, type FormEvent, useEffect, useMemo, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, get, put } from '../api'
import { useAuth, useCan, useCanOnProject, useStaticSlotsOffered } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import Breadcrumb from '../components/Breadcrumb'
import ProjectEnvironmentTab from '../components/projects/ProjectEnvironmentTab'
import ProjectMemoryTab from '../components/projects/ProjectMemoryTab'
import BudgetField from '../components/budget/BudgetField'
import { ProjectLiveCastTab, useProjectLiveCastShown } from '../lib/liveCastSurface'
import { ProjectMergeLaneTab, promoteLink, useMergeLaneTab } from '../lib/mergeLaneSurface'
import { isEnabled } from '../lib/editionFeatures'
import { memoryFeedsKey } from '../components/projects/projectMemory'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useSubmit } from '../hooks/useSubmit'
import { PROJECTS_KEY, WORKTREES_KEY, useProjects } from '../hooks/useRosters'
import { useUrlTab } from '../hooks/useUrlTab'
import { planSurfaceShown, planSwitchOn } from '../lib/planSwitches'
import { useAssignments } from '../hooks/useAssignments'
import AssignmentList from '../components/AssignmentList'
import { destructiveHelp, emptyAssignmentWarning } from '../lib/assignments'
import AccessTab from '../components/access/AccessTab'
import type { OrgRef, Project, ProjectMember, TechStackEntry } from '../types'
import {
  blankRepoFields, buildBody, FIX_MARKED_REPOS, formFromProject, formHasRepo, type GovernanceAuthority, governanceIsReadOnly, isHidden, namesHumans,
  NewRepoError, prepareRepos, type ProjectFormState, refusedBodyLoc, repoRowErrors, type RepoRowErrors,
} from '../components/projects/projectForm'
import ProjectRepoFields, { useFocusTarget } from '../components/projects/ProjectRepoFields'
import type { TaskModelChoice } from '../components/tasks/TaskEditForm'
import { memberLabel } from '../lib/memberLabel'
import Banner from '../ui/Banner'
import { GitReachBanner } from '../components/GitReach'
import DetailStatus from '../ui/detail/DetailStatus'
import CredentialSelect, { type CredentialOption } from '../ui/CredentialSelect'
import { credentialMark } from '../ui/credentialMarks'
import { DetailCell, DetailPage, DetailSection } from '../ui/detail/DetailPage'
import EmptyState from '../ui/EmptyState'
import NotFoundState from '../ui/NotFoundState'
import { ResourceRefusal } from './NoAccess'
import FieldRow from '../ui/FieldRow'
import { BUTTON_SX, TEXTAREA_RESIZE_SX } from '../ui/formDensity'
import FormField from '../ui/FormField'
import MuteSwitch from '../components/tasks/MuteSwitch'
import FormActionBar from '../ui/FormActionBar'
import FormTextField from '../ui/FormTextField'
import { personaLabel, personaOptions, projectDefaultPersona } from '../components/tasks/personaRegistry'
import { useTaskCreateDefaults } from '../components/tasks/taskDefaults'
import { ADVISOR_PERSONA, DEFAULT_PERSONA } from '../components/tasks/taskPersona'
import { AgentPicker, ANY_AGENT_LABEL } from '../components/agents/AgentPicker'
import { fieldTapTargetSx, tapTargetSx } from '../ui/listStyles'
import { ActingForNotice, OrgAside, useOtherOrg, useOwnOrgRows } from '../ui/OrgIndicator'
import SectionCard from '../ui/SectionCard'
import SettingCheckbox from '../ui/SettingCheckbox'
import SubmitButton from '../ui/SubmitButton'
import { getCredential, loadCredentialOptions, type Credential } from './credentials/api'
import OtherOrgGate from '../components/OtherOrgGate'
import NonBaaWarning from '../components/NonBaaWarning'
import { useChangeOrgAction } from '../components/ChangeOrgDialog'
import { useActAsRows } from '../lib/actAs'

/* The project edit page, `/projects/:id` (creating one is `ProjectCreateWizard`). A routed page, so the form
 * is linkable and has room.
 * It wears the task editor's layout through shared primitives (`ui/FieldRow`, `ui/SettingCheckbox`,
 * `ui/FormActionBar`, `ui/formDensity`): tabs (see `tabsFor`); rows of rem-based cells that wrap on their
 * own; each field's explanation a hover on its label (`help`, `ui/HelpLabel`), while value notes about the
 * loaded row stay visible; no card (`DetailPage`'s `measure="read"`, reasoned at the call site).
 * Panel heights are the oracle of `e2e/project-edit-tabs.spec.ts`.
 * The form model (`ProjectFormState`, seeders, `buildBody`, the governance predicate) is
 * `components/projects/projectForm.ts`, shared with the create wizard; the governance notice is here.
 * There is no `GET /api/projects/{id}`: this page reads the list's `['projects']` react-query key
 * (`app/routes/portfolio.py::list_projects`) and picks its row, so arriving from `/projects` paints with
 * no fetch (the cache is persisted, `queryClient.ts`).
 */


/* The one sentence explaining seven greyed controls: a page `Banner` at `severity="info"` (as
 * `GroupEditPage`'s reserved-group notice). One notice at the top of the Governance tab naming all three
 * groups, and saying Agent instructions below them still saves.
 * It is the API's own 403 said beforehand: which fields, why (four are shell commands the fleet runs
 * unprompted), that the rest saves, and who to ask — both doors, superadmin and `nodes:edit`
 * (`apps/api/app/routes/projects.py::_require_authority_to_author_governance`).
 * The closing sentence never says "superadmin" (`test/tenantCopy.ts::OPERATOR_SCOPE_WORDS`). */
function GovernanceReadOnlyNotice() {
  return (
    <Banner severity="info">
      Branch policy, test gates and stack commands are not yours to change — they run as a shell on
      every node. Other fields, Agent instructions included, still save. Ask an owner, or an admin with
      node-edit permission.
    </Banner>
  )
}

/* Grouping and cells are `ui/detail`'s.
 * · `DetailSection`: Governance holds three decisions (branch, gate, stack), so headings survive the tabs.
 *   `titleId` names the section whose title is its field's accessible name (Agent instructions). The
 *   divider is opt-in (`divided`), a prop rather than `:first-of-type`, which also matched the read-only
 *   notice and made the divider depend on permission — hence Branch policy's `divided={governanceReadOnly}`.
 * · `DetailCell`: `basis` is the usable width and decides wrapping; `max` caps per field; omitted `basis`
 *   is a natural-width cell (a checkbox). */

/* One of the two role pickers: a native select (as `GroupEditPage`, `CredentialEditPage`) via
 * `ui/FormTextField`, matching the text fields. `inputLabel: { shrink: true }` is required: a native select
 * always shows a value. The value is a uuid string passed through untouched — `Number(...)` made `NaN`
 * and emptied a group in `GroupEditPage`. */
function MemberSelectField({ id, label, help, value, members, listState, listNarrowed, onChange }: {
  id: string
  label: string
  help: string
  /** A `users.id`, or `''` for nobody. */
  value: string
  members: ProjectMember[]
  /** Whether `members` has arrived; see the orphan block. */
  listState: 'loading' | 'error' | 'ready'
  /** Whether a settled `members` is still incomplete for this reader; see the orphan block. */
  listNarrowed: boolean
  onChange: (value: string) => void
}) {
  /* A set value not in the list. Without its own option the select shows "— None —" and the next Save
  clears the role, so the id gets a disabled but selected option that round-trips unchanged.
  What it may say depends on why it is missing: loading or failed → "cannot name yet"; settled →
  "lost access" — unless `listNarrowed`: a tenant-org viewer's list withholds superadmins by design
  (`routes/projects.py::list_project_members`), so they get the uncertain wording. */
  const orphaned = value !== '' && !members.some(m => m.id === value)
  const orphanLabel = {
    loading: 'Currently set — loading…',
    error: 'Currently set — member list unavailable',
    ready: listNarrowed ? 'Currently set — not in this list' : 'Unknown user — no longer has access',
  }[listState]
  return (
    <FormTextField
      id={id}
      label={label}
      value={value}
      onChange={onChange}
      slotProps={{ inputLabel: { shrink: true } }}
      /* Ellipsis on the closed control only: "First Last — email" can exceed the field. The open list is the
      platform popup, which page CSS cannot reach — so `memberLabel` must not truncate. */
      sx={{ '& .MuiNativeSelect-select': { textOverflow: 'ellipsis' } }}
      /* The uuid on the helper line, settled case only, so an operator can search `/users` for it: it is
      about the loaded value, a warning. */
      help={help}
      hint={orphaned && listState === 'ready'
        ? listNarrowed
          ? `Set to ${value} — this list does not show every account with access. Saving keeps them; pick someone else, or None, to change it.`
          : `Set to ${value}, who can no longer reach this project. Saving keeps them — pick someone else, or None, to change it.`
        : undefined}
      options={members.map(m => ({ value: m.id, label: memberLabel(m) }))}
    >
      {/* First, spelled as a choice: clearing the role is something operators come here to do. */}
      <option value="">— None —</option>
      {orphaned && <option value={value} disabled>{orphanLabel}</option>}
    </FormTextField>
  )
}

/* The project's default model TIER as a native select over `GET /api/tasks/models`, so a typo'd name can no
 * longer be saved. `''` is None (no project default). A stored value missing from the catalogue (loading, or
 * the fetch failed) keeps its own option so Save does not silently clear it. */
function ModelTierField({ value, choices, onChange }: {
  value: string
  choices: readonly TaskModelChoice[]
  onChange: (value: string) => void
}) {
  const orphaned = value !== '' && !choices.some(c => c.value === value)
  return (
    <FormTextField
      id="project-default-model"
      label="Default model"
      help="Tier for tasks that pin no model."
      value={value}
      onChange={onChange}
      slotProps={{ inputLabel: { shrink: true } }}
      options={choices}
    >
      <option value="">None</option>
      {orphaned && <option value={value}>{value}</option>}
    </FormTextField>
  )
}

/* A Jira credential in the picker: the hint is the site and optional project key, fields on the credential
 * row (`credentials/registry.py`'s `jira` spec, non-secret) — the only way to tell two "Jira" rows apart.
 * The label stays the name alone: `ui/AsyncLookup` ellipsises the hint, and a clipped name is a defect. */
function jiraOption(c: Credential): CredentialOption {
  const site = (c.config?.base_url ?? '').replace(/^https?:\/\//, '').replace(/\/+$/, '')
  const scope = [site, c.config?.project_key ?? ''].filter(Boolean).join(' · ')
  return {
    id: String(c.id),
    name: c.name,
    hint: scope || undefined,
    icon: credentialMark(c.type, c.type_label),
  }
}

type ProjectTab =
  'general' | 'access' | 'governance' | 'slots' | 'environment' | 'memory' | 'microvm' | 'merge-lane' | 'live-cast'
  | 'budget' | 'advanced'

const GENERAL_TAB: TabDef<ProjectTab> = { id: 'general', label: 'General' }
/* Second, so it is on-screen in the strip at any width. */
const ACCESS_TAB: TabDef<ProjectTab> = { id: 'access', label: 'Access' }
const GOVERNANCE_TAB: TabDef<ProjectTab> = { id: 'governance', label: 'Governance' }
const SLOTS_TAB: TabDef<ProjectTab> = { id: 'slots', label: 'Slots' }
/* Collections need a saved project id. */
const ENVIRONMENT_TAB: TabDef<ProjectTab> = { id: 'environment', label: 'Environment' }
/* What runners learned (project memory, C8): a saved row's, like Environment. */
const MEMORY_TAB: TabDef<ProjectTab> = { id: 'memory', label: 'Memory' }
/* The tabs with nothing the project form saves: no Cancel/Save bar (see the bar's own comment). */
const NO_FORM_ACTIONS: ReadonlySet<ProjectTab> = new Set(['access', 'microvm', 'merge-lane', 'live-cast', 'memory', 'budget'])
/* Its VM size and egress edits (`components/microvm/ProjectMicrovmTab.tsx`): a saved row's, saved at once. */
const MICROVM_TAB: TabDef<ProjectTab> = { id: 'microvm', label: 'MicroVM' }
/* Loaded where present: MicroVM's (`microvm`), which the community edition's tree leaves out (`import.meta.glob`
   yields `{}` for an absent file). */
const ProjectMicrovmTab = isEnabled('microvm') ? Object.values(
  import.meta.glob<ComponentType<{ project: Project; canEdit: boolean }>>(
    '../components/microvm/ProjectMicrovmTab.tsx', { eager: true, import: 'default' }))[0] : undefined
/* The lane's queue, read-only (D11): only while the lane is active or has rows (`laneTabShown`); second, ahead
   of Access, while the lane is on or draining (`laneLive`), so the operator watching it never opens "More ▾". */
const MERGE_LANE_TAB: TabDef<ProjectTab> = { id: 'merge-lane', label: 'Merge lane' }
/* Its own Save and Publish (`components/projects/ProjectLiveCastTab.tsx`): an org admin's, and only while
   `GET …/live-cast` answers — an org without the product gets 403, so the tab and the API agree (AC1.6). */
const LIVE_CAST_TAB: TabDef<ProjectTab> = { id: 'live-cast', label: 'Live Cast' }
/* Its own Save and Remove (`components/budget/BudgetField.tsx`): an org admin's, as the project's org.
   Shown while the plan's switch is off too, where it says so and links the upgrade. */
const BUDGET_TAB: TabDef<ProjectTab> = { id: 'budget', label: 'Budget' }

/* A security warning on a hover, so it is also the textarea's accessible description: the text reaches every
 * runner's system prompt, outranks the charter, and is copied into session transcripts. */
const AGENT_INSTRUCTIONS_HELP = "Every agent's prompt; outranks charter. Viewers and transcripts see it."

const ADVANCED_TAB: TabDef<ProjectTab> = { id: 'advanced', label: 'Advanced' }

/* Access and Environment need a saved project; MicroVM and Memory also the plan's switch (`lib/planSwitches.ts`),
 * which Memory waives for an editor (its off-plan Purge). */
function tabsFor(
  hasRow: boolean,
  laneShown: boolean,
  laneUpFront: boolean,
  microvmShown: boolean,
  liveCastShown: boolean,
  memoryShown: boolean,
  budgetShown: boolean,
): readonly TabDef<ProjectTab>[] {
  const lane = hasRow && laneShown
  return [
    GENERAL_TAB,
    ...(lane && laneUpFront ? [MERGE_LANE_TAB] : []),
    ...(hasRow ? [ACCESS_TAB] : []),
    GOVERNANCE_TAB,
    SLOTS_TAB,
    ...(hasRow ? [ENVIRONMENT_TAB] : []),
    ...(hasRow && memoryShown ? [MEMORY_TAB] : []),
    ...(hasRow && microvmShown ? [MICROVM_TAB] : []),
    ...(lane && !laneUpFront ? [MERGE_LANE_TAB] : []),
    ...(hasRow && liveCastShown ? [LIVE_CAST_TAB] : []),
    ...(hasRow && budgetShown ? [BUDGET_TAB] : []),
    ADVANCED_TAB,
  ]
}

const DEFAULT_TAB: ProjectTab = 'general'

/* The number fields. `noValidate` also disables bad-input checking: typing `1e` makes Chrome hand
 * React `''` while displaying `1e`, and `buildBody` would send `port_block: null` with a success message.
 * `validity.badInput` is still readable under `noValidate`; read it off the live element, since the draft
 * has already lost the value. */
const NUMBER_FIELDS = [
  { key: 'port_block', tab: 'slots', inputId: 'project-port-block', label: 'Port block' },
  { key: 'merge_lane_batch_max', tab: 'governance', inputId: 'project-merge-lane-batch-max', label: 'Batch size' },
] as const satisfies readonly {
  key: StringFieldKey
  tab: ProjectTab
  inputId: string
  label: string
}[]

/* The fields that may never be submitted empty (the gate commands may: `''` is "no gate"), each tagged with its tab. `TabPanel` hides inactive
 * panels with `hidden`, and a native `required` input in a `display:none` subtree makes Chrome refuse
 * the submit silently ("An invalid form control … is not focusable"). So the form is `noValidate` and this
 * table validates: first offender wins, its tab is shown, it is focused, and the banner names it.
 * `required` attributes stay for `aria-required`.
 * Role pickers are absent: `''` is their meaningful "— None —".
 * `governance: true` fields are skipped when `governanceIsReadOnly` (they render without `required` and
 * are not sent), so what is shown, checked and sent agree. */
/* Only string-valued keys may be listed, type-enforced: `.trim()` on `String(null)`/`String(false)` is
 * truthy, so a non-string entry would never fire. */
type StringFieldKey = {
  [K in keyof ProjectFormState]: ProjectFormState[K] extends string ? K : never
}[keyof ProjectFormState]

const REQUIRED_FIELDS = [
  { key: 'name', tab: 'general', inputId: 'project-name', label: 'Name', governance: false },
] as const satisfies readonly {
  key: StringFieldKey
  tab: ProjectTab
  inputId: string
  label: string
  governance: boolean
}[]

/* Server validation uses the same tab destinations as local validation, including optional fields. */
const FIELD_TABS: Record<string, { tab: ProjectTab; inputId: string }> = {
  ...Object.fromEntries([...REQUIRED_FIELDS, ...NUMBER_FIELDS].map(f => [f.key, f])),
  default_model: { tab: 'advanced', inputId: 'project-default-model' },
  default_agent_id: { tab: 'advanced', inputId: 'project-default-agent' },
  default_provider: { tab: 'advanced', inputId: 'project-default-agent' },
  default_persona: { tab: 'general', inputId: 'project-default-persona' },
  business_analyst_id: { tab: 'advanced', inputId: 'project-business-analyst' },
  architect_id: { tab: 'advanced', inputId: 'project-architect' },
  jira_credential_id: { tab: 'advanced', inputId: 'project-jira-credential' },
  tech_stack: { tab: 'advanced', inputId: 'project-tech-stack-0-layer' },
  notes: { tab: 'advanced', inputId: 'project-notes' },
  agent_instructions: { tab: 'governance', inputId: 'project-agent-instructions' },
  branch_prefix: { tab: 'governance', inputId: 'project-branch-prefix' },
  gate_quick_cmd: { tab: 'governance', inputId: 'project-gate-quick-cmd' },
  gate_full_cmd: { tab: 'governance', inputId: 'project-gate-full-cmd' },
  port_vars: { tab: 'slots', inputId: 'project-port-vars' },
}

export default function ProjectEditPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  /* Governance remains the server's verdict. A static-slot control is not offered while the plan allows none. */
  const { admin } = useAuth()
  const changeOrg = useChangeOrgAction('project')
  /* The org's "Persona" default, named on the Default persona field's empty option; an advisor default files a coder
   (`routes/tasks/create.py::_default_persona`). */
  const orgDefaultPersona = useTaskCreateDefaults().defaults.persona
  const orgPersona = orgDefaultPersona === ADVISOR_PERSONA ? DEFAULT_PERSONA : orgDefaultPersona
  const staticOffered = useStaticSlotsOffered()
  /* The create wizard's one-time git-access advisory, moved off history state so a reload does not repeat it. */
  const [placementWarning, setPlacementWarning] = useState<string | null>(null)
  useEffect(() => {
    const handed = (location.state as { placementWarning?: string } | null)?.placementWarning
    if (!handed) return
    setPlacementWarning(handed)
    navigate(location.pathname + location.search, { replace: true, state: null })
  }, [location, navigate])

  /* The Environment tab's project tier is read-only for a `projects:view` holder (R10). Project-scoped,
  not `useCan`; the routes re-derive it, and this only decides whether a 403-bound control is offered. */
  const canEditProject = useCanOnProject()

  /* A non-numeric segment can never name a row, so it is not-found without a request (as
  `CredentialEditPage`, `UserEditPage`). */
  const numericId = id !== undefined && /^\d+$/.test(id) ? Number(id) : null
  /* Memory's off-plan refusal is the answer for this page open only: closing the page drops it, so a plan turned on
     meanwhile shows on the next open. */
  useEffect(() => () => {
    if (numericId !== null) queryClient.removeQueries({ queryKey: memoryFeedsKey(numericId) })
  }, [numericId, queryClient])

  /* The list page's key, so arriving from `/projects` needs no fetch; revalidates behind the form. Disabled
  for an unmatchable segment. */
  const projectsQuery = useProjects({ enabled: numericId !== null })
  const listed = useMemo(
    () => (numericId === null ? undefined : projectsQuery.data?.find(p => p.id === numericId)),
    [projectsQuery.data, numericId],
  )
  /* Fetched on this mount: a roster restored from localStorage (`queryClient.ts`) is not an answer yet. */
  const listFresh = projectsQuery.isFetchedAfterMount
  /* The list is bounded (`routes/portfolio.py::list_projects`), so an operator's All-orgs view can miss the
  row: once the list settles without it, read it by id. A 404 falls through to the refusal chain below. */
  const byIdQuery = useQuery({
    queryKey: ['project', numericId],
    queryFn: () => get<Project>(`/api/projects/${numericId}`),
    enabled: numericId !== null && projectsQuery.isSuccess && (listFresh || !projectsQuery.isFetching) && listed === undefined,
  })
  const project = listed ?? byIdQuery.data

  /* The governance authority, asked of the server (see `governanceIsReadOnly`): `/{id}/governance-authority`,
  the PUT's door (the create wizard asks the POST's). The key carries the id so one project's verdict never
  serves another. A failure resolves to `undefined`: the seven stay locked and the rest saves. */
  const governanceQuery = useQuery({
    queryKey: ['project-governance-authority', numericId],
    queryFn: () => get<GovernanceAuthority>(`/api/projects/${numericId}/governance-authority`),
    enabled: numericId !== null,
  })
  const mayAuthorGovernance = governanceQuery.data?.may_author_governance

  /* The role pickers' candidates, keyed by project id (the instance is reused across projects). Gated like
  `projectsQuery`. A failure disables only these two controls, stated
  beside them. */
  const membersQuery = useQuery({
    queryKey: ['project-members', numericId],
    queryFn: () => get<ProjectMember[]>(`/api/projects/${numericId}/members`),
    enabled: numericId !== null,
  })
  /* Data wins over `isError`: the persisted cache can rehydrate a list while its refetch fails, and that
  list is still the best answer; the caption reports the failed refresh. */
  const members = membersQuery.data ?? []
  const memberListState = membersQuery.data ? 'ready' : membersQuery.isError ? 'error' : 'loading'
  /* A settled list is incomplete for a non-operator: the endpoint drops superadmins for them.
  `is_operator` is the signal; `MemberSelectField`'s orphan block owns what it changes. */
  // The server narrows on the viewer's org (`routes/projects.py::list_project_members`).
  const memberListNarrowed = admin?.tenant !== false
  /* `user_id` → person for the Environment tab's foreign-override rows, via `memberLabel`. Unknown ids are
  absent; that tab shows the tier without a name. */
  const overrideOwners = useMemo(
    () => Object.fromEntries(members.map(m => [m.id, memberLabel(m)])),
    [members],
  )

  /* The draft is seeded during render, never by an effect (which would paint an empty frame).
  A revalidation must not discard typing, nor leave a stale draft seeded from a days-old persisted roster
  (Save would write old values back). So re-seed while the draft still equals `pristine` (the last seed);
  once the operator types, only a different `:id` re-seeds. `JSON.stringify` compare: one constructor,
  stable key order, and a missed field would fail toward discarding work. */
  const [form, setForm] = useState<ProjectFormState | null>(null)
  const [seeded, setSeeded] = useState<{ row: Project; pristine: ProjectFormState } | null>(null)
  if (project !== undefined && seeded?.row !== project) {
    const untouched =
      seeded === null ||
      seeded.row.id !== project.id ||
      JSON.stringify(form) === JSON.stringify(seeded.pristine)
    if (untouched) {
      const next = formFromProject(project)
      setSeeded({ row: project, pristine: next })
      setForm(next)
    } else {
      // Their edits stand. `pristine` stays the original baseline, so reverting by hand re-enables refresh.
      setSeeded({ row: project, pristine: seeded.pristine })
    }
  }

  /* The tier catalogue (`apps/api/app/model_catalog.py`), the one list the server validates
  `default_model` against. A failed fetch still renders None and the stored value, so it round-trips. */
  const modelChoicesQuery = useQuery({
    queryKey: ['task-models'],
    queryFn: () => get<TaskModelChoice[]>('/api/tasks/models'),
    enabled: form !== null,
    staleTime: Infinity,
  })
  const modelChoices = modelChoicesQuery.data ?? []

  /* The name behind the saved Jira id, fetched by id (a first page of `type=jira` may not contain it).
  A pick made this session is not re-fetched (`ui/CredentialSelect` remembers it); `pickedJiraId` holds
  the id, since a re-seed can replace the value under a stale pick.
  The failure arm is reachable: `GET /api/credentials/{id}` is grant-scoped, so `CredentialSelect`
  renders the id itself and the hint says why. */
  const [pickedJiraId, setPickedJiraId] = useState<string | null>(null)
  const jiraId = form?.jira_credential_id ?? null
  const jiraPicked = pickedJiraId !== null && pickedJiraId === jiraId
  const jiraQuery = useQuery({
    queryKey: ['credential', jiraId],
    queryFn: () => getCredential(jiraId as string),
    enabled: jiraId !== null && !jiraPicked,
  })
  // Memoised for `CredentialSelect`'s identity contract; this form re-renders on every keystroke.
  const jiraSelected = useMemo(
    () => (jiraQuery.data !== undefined ? jiraOption(jiraQuery.data) : null),
    [jiraQuery.data],
  )

  const { busy: saving, formError: saveError, setFormError: setSaveError, run } = useSubmit()
  /* The field that stopped the last submit and its message ("Required" vs "Must be a number"). Cleared on
  the next submit, not on keystrokes. */
  const [fieldError, setFieldError] = useState<{ key: StringFieldKey; message: string } | null>(null)
  // The Repositories table's per-row messages (`ProjectReposTable`), keyed by row.
  const [repoErrors, setRepoErrors] = useState<RepoRowErrors>({})
  // The tab lives in the URL (hooks/useUrlTab.ts). The row exists: the Access/Environment gate (see `tabsFor`).
  /* The Merge lane tab shows while the lane is active or has rows (`components/projects/mergeLane.ts`). */
  const lane = useMergeLaneTab(numericId)
  const microvmShown = ProjectMicrovmTab !== undefined && planSurfaceShown(admin, 'microvm_allowed')
  /* Another org's project, for an operator: edited on that org's behalf. Every write below the project —
     Save and every tab's, the node ticks included — goes as its org for the one request (`lib/actAs.ts::useActAsRows`),
     the Access tab's grants too (`actAsOnce`), and `ActingForNotice` says so. Its reads that answer for the caller's
     org — the Default agent's choices, the budget, the forge lookups, Live Cast — go as its org too. Change org…
     stays the operator's own verb. The org as `headerOrg` derives it. */
  const otherOrg = useOtherOrg(project ? project.org : seeded?.row.org ?? null)
  useActAsRows(numericId === null ? [] : [{ path: `/api/projects/${numericId}`, org: otherOrg }])
  /* Live Cast: its tab only while `GET …/live-cast` answers (`lib/liveCast.ts::useProjectLiveCastShown`). */
  const liveCastShown = useProjectLiveCastShown(numericId, project?.live_cast_offered === true, otherOrg)
  /* The budget routes answer for the caller's org, so another org's project reads and writes its budget as that org
     (`BudgetField`'s `org`), whose plan is its own, not the viewer's: the server answers it. Off the viewer's own plan
     `BudgetField` draws nothing, so the tab goes too rather than open empty. */
  const budgetShown = isOrgAdminSeat(admin) && (otherOrg !== null || planSwitchOn(admin, 'budgets_enabled'))
  const tabs = tabsFor(
    numericId !== null,
    isEnabled('merge_lane') && (project?.merge_lane === true || lane.hasRows),
    lane.live,
    microvmShown,
    liveCastShown,
    /* Off the plan an editor still gets the tab: Purge alone stays open there, so memory can be erased. */
    planSurfaceShown(admin, 'project_memory_enabled')
      || (numericId !== null && canEditProject('projects:edit', numericId)),
    budgetShown,
  )
  const [urlTab, selectTab] = useUrlTab(tabs, DEFAULT_TAB)
  // `?tab=worktrees`: the Slots tab's id before it matched its label; old links still land there.
  const activeTab = new URLSearchParams(location.search).get('tab') === 'worktrees' ? 'slots' : urlTab

  // Focus the input once its tab is actually shown (selecting it is a router navigation).
  const setFocusTarget = useFocusTarget(activeTab)

  /* THE NODES CONTROL, edit only: a create has no id to assign. Its write and the default's are
  `routes/projects.py::require_assignment_writer` (D13): an org admin holding `projects:create` and
  `projects:delete`; anyone else sees the lists read-only. Read on the first visit to Slots. */
  const can = useCan()
  const ownOrgRows = useOwnOrgRows()
  // Another org's project attaches that org's rows: its Save goes as that org.
  const projectOrgRows = <T extends { org?: OrgRef | null }>(rows: T[]): T[] =>
    rows.filter(row => row.org === undefined || row.org?.id === otherOrg?.id)
  const canAssign = isOrgAdminSeat(admin) && can('projects:create') && can('projects:delete')
  /* Makes or initializes a repository on Save (`POST /api/git-repos[/initialize]`, `projects:create`). Another org's
     project offers that org's owners and repositories and makes them as it (`ProjectRepoFields`'s and `prepareRepos`'s
     `org`). */
  const mayWriteRepos = can('projects:create')
  const [worktreesVisited, setWorktreesVisited] = useState(false)
  if (activeTab === 'slots' && !worktreesVisited) setWorktreesVisited(true)
  const assignments = useAssignments('project', worktreesVisited ? numericId : null)

  function patch(next: Partial<ProjectFormState>) {
    setForm(f => (f ? { ...f, ...next } : null))
  }

  function updateTechStackRow(idx: number, field: keyof TechStackEntry, value: string) {
    setForm(f => f ? {
      ...f,
      tech_stack: f.tech_stack.map((row, i) => i === idx ? { ...row, [field]: value } : row),
    } : null)
  }

  /* Refusing a submit: show the offending tab, state the problem readably from any tab, focus the control. */
  function reject(
    field: { key: StringFieldKey; tab: ProjectTab; inputId: string },
    bannerMessage: string,
    fieldMessage: string,
  ) {
    setFieldError({ key: field.key, message: fieldMessage })
    setSaveError(bannerMessage)
    selectTab(field.tab)
    setFocusTarget(field.inputId)
  }

  /* Save with nothing changed says so instead of writing the same row back; any edit makes a new draft, which
  clears the note. */
  const [unchangedDraft, setUnchangedDraft] = useState<ProjectFormState | null>(null)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!form) return
    if (seeded && JSON.stringify(form) === JSON.stringify(seeded.pristine)) {
      setSaveError('')
      setUnchangedDraft(form)
      return
    }

    /* The form's own required check (see `REQUIRED_FIELDS`). First offender only. */
    const governanceReadOnlyNow = governanceIsReadOnly(form, mayAuthorGovernance)
    // A hidden field is neither checked nor sent (`isHidden`).
    const missing = REQUIRED_FIELDS.find(
      f => !(f.governance && governanceReadOnlyNow) && !isHidden(form, f.key) && !form[f.key].trim(),
    )
    if (missing) {
      reject(missing, `${missing.label} is required.`, 'Required')
      return
    }

    /* After the required pass. Its own message, since the draft is empty but the field looks filled. */
    const badNumber = NUMBER_FIELDS.find(
      f => !isHidden(form, f.key) && (document.getElementById(f.inputId) as HTMLInputElement | null)?.validity.badInput,
    )
    if (badNumber) {
      reject(badNumber, `${badNumber.label} must be a number.`, 'Must be a number')
      return
    }
    /* Every repository needs a URL and a branch; the refusal is on the row, the banner says where to look. None is a
    project with no repository (`formHasRepo`): removing the last one sends `repos: []`. */
    const sentRepos = form.repos ?? []
    const blankRows = blankRepoFields(sentRepos)
    if (Object.keys(blankRows).length) {
      setRepoErrors(blankRows)
      setSaveError(FIX_MARKED_REPOS)
      selectTab('general')
      return
    }
    setFieldError(null)
    setRepoErrors({})
    let rowsRefused = false
    let repoFailure = ''
    /* `repos` replaces the whole stored set, so it goes only when this draft changed it: saving another field from a
    draft older than someone else's added repository must not delete that repository. */
    const unchangedReposLeftOut = (draft: ProjectFormState, body: Record<string, unknown>) => {
      if (seeded && JSON.stringify(draft.repos) === JSON.stringify(seeded.pristine.repos)) delete body.repos
      return body
    }
    await run('Failed to save project', async () => {
      /* Each "Create a new repository" row is made first, and a made one left empty given main and develop
      (`projectForm.ts::prepareRepos`); a made row holds its URL from then on, so a retried Save never makes it twice. */
      let rows = form.repos
      try {
        if (rows && mayWriteRepos) rows = await prepareRepos(rows, form.name, false, next => patch({ repos: next }), otherOrg)
      } catch (err) {
        if (err instanceof NewRepoError) setRepoErrors({ [err.key]: { row: err.message } })
        repoFailure = err instanceof NewRepoError ? FIX_MARKED_REPOS : apiDetail(err, 'Failed to initialize the repository')
        selectTab('general')
        throw err
      }
      const draft = { ...form, repos: rows }
      return put(`/api/projects/${form.id}`, unchangedReposLeftOut(draft, buildBody(draft, !governanceReadOnlyNow)))
        .then(() => {
          /* Invalidate, never await a refetch: a failed roster GET must not report a successful save as failed.
          `worktrees` too (`port_block`/`port_vars` reach the pool). Invalidation skips disabled queries, so no 403
          without `worktrees:view` (`components/WorktreePool.tsx` records that trap). */
          void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
          void queryClient.invalidateQueries({ queryKey: ['project'] })
          void queryClient.invalidateQueries({ queryKey: WORKTREES_KEY })
          navigate('/projects')
        })
        .catch(err => {
          // The banner takes `apiDetail`'s sentence (`run`); the field it names is shown and focused here.
          // A per-row refusal lands on the rows it names (`repoRowErrors`).
          const rowErrors = repoRowErrors(err, rows ?? [])
          if (rowErrors) {
            setRepoErrors(rowErrors)
            selectTab('general')
            rowsRefused = true
          }
          const loc = refusedBodyLoc(err)
          const field = loc?.[1]
          // A hidden field cannot be shown or focused; the banner alone carries the server's words.
          const target = typeof field === 'string' && !isHidden(form, field) ? FIELD_TABS[field] : undefined
          if (loc && target) {
            selectTab(target.tab)
            setFocusTarget(field === 'tech_stack' && typeof loc[2] === 'number'
              ? `project-tech-stack-${loc[2]}-${loc[3] === 'technology' ? 'technology' : 'layer'}`
              : target.inputId)
          }
          throw err
        })
    })
    // The rows carry the server's words; the banner only says where to look.
    if (rowsRefused) setSaveError(FIX_MARKED_REPOS)
    if (repoFailure) setSaveError(repoFailure)
  }

  /* Server truth, not the draft; the seeded name outlives a row that left the roster. */
  const heading = project?.name ?? seeded?.row.name ?? 'Project'
  const crumbs = <Breadcrumb items={[{ label: 'Projects', to: '/projects' }, { label: heading }]} />
  /* The record's tenant beside its name (`titleAside`; see `ui/PageHeader`). `project ? project.org : …`,
  not `??`: an org can legitimately be null (`types.ts::OrgRef`), and `??` would show the seed's stale
  tenant. */
  const headerOrg = project ? project.org : seeded?.row.org ?? null
  /* `ui/PageHeader`'s own bottom margin, so transient states align with the settled page. */
  const trailOnly = <Box sx={{ mb: 1.5 }}>{crumbs}</Box>

  /* The transient states are reachable only while there is no draft. Gating on the query was wrong:
  `isLoading` is false with cached data (showing "not found" before the form), and a page-level `isError`
  branch must never discard a half-filled form. `!form` makes that structural. */
  if (!form) {
    /* Only until a first answer, never on a background refetch: that would unmount the refusal chain below,
    whose queries restart on remount, in a loop. */
    const loading = (projectsQuery.isFetching && !listFresh) || byIdQuery.isLoading
    if (loading || projectsQuery.isError) {
      return <DetailStatus trail={crumbs} loading={loading} error="Failed to load projects" />
    }
    return (
      <Box>
        {trailOnly}
        {/* A same-org project the caller cannot open is a refusal with a request offer, not "not found". */}
        <NotFoundState
          fallback={
            <ResourceRefusal
              fallback={
                <OtherOrgGate
                  objectType="project"
                  objectId={id}
                  fallback={<EmptyState title="Project not found" />}
                />
              }
            />
          }
        />
      </Box>
    )
  }

  const governanceReadOnly = governanceIsReadOnly(form, mayAuthorGovernance)
  /* The verdict arrived and was no — narrower than `governanceReadOnly` (also true while pending). Only a
  rule can be explained, so this draws the notice and the divider follows it. */
  const governanceWithheld = mayAuthorGovernance === false
  // Short message on the control, field name in the banner.
  const errorFor = (key: StringFieldKey) => (fieldError !== null && fieldError.key === key ? fieldError.message : undefined)
  // The operator's "Change org…" (`components/ChangeOrgDialog.tsx`) — an existing project only.
  const changeOrgButton = numericId === null || !project
    ? null
    : changeOrg.button({ id: numericId, name: heading, org: headerOrg, href: `/projects/${numericId}` })

  return (
    <DetailPage
      breadcrumb={crumbs}
      title={heading}
      /* The record's org, the chip alone (an "Org" caption would repeat what the chip says); the tenant is
      on the chip's `title`. The create wizard labels the viewer's own org "Your org" under the same chip id. */
      titleAside={
        /* Composed from `ui/OrgIndicator.tsx`'s `OrgAside` (box, token; operator only). */
        <OrgAside
          org={headerOrg}
          chipTestId="project-org"
        />
      }
      headerTestId="project-page-header"
      /* `read` (64rem) so the full-bleed rules (tab hairline, section dividers, action bar border) overhang the
      widest row (47rem: an open repository's Repo URL 30 + Integration branch 16 + 1 gap) by ~176px rather than ~400px. No effect below 1024px. */
      measure="read"
      actions={changeOrgButton ?? undefined}
    >
      <ActingForNotice org={otherOrg} testId="project-acting-for" />

      <Box
        component="form"
        onSubmit={e => void handleSubmit(e)}
        /* Native validation is off: it cannot see into hidden panels (`REQUIRED_FIELDS`). */
        noValidate
        data-testid="project-form"
        sx={{ display: 'flex', flexDirection: 'column' }}
      >
        {/* Above the tab strip: it is about the submit and must be readable from any panel. */}
        {saveError && <Banner severity="error">{saveError}</Banner>}
        {placementWarning && <Banner severity="warning">{placementWarning}</Banner>}
        {numericId !== null && project?.has_repo && <GitReachBanner projectId={numericId} />}

        {/* `overflowMenu`: up to ten tabs. In the side pane a scrolling strip hid most of them behind its arrows,
        and wrapping cost a second row, so the ones that do not fit go behind More — on a phone too, as on the node
        and user pages: a strip wrapped onto two rows there read as two strips. */}
        <Tabs
          tabs={tabs}
          tab={activeTab}
          onSelect={selectTab}
          className="project-tabs"
          label="Project sections"
          idPrefix="project"
          overflowMenu
          sticky="default"
        />

        <TabPanel id="general" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            {/* First: its rows decide which fields the rest of the form shows (`formHasRepo`). */}
            <ProjectRepoFields
              form={form}
              patch={patch}
              repoErrors={repoErrors}
              onRepoErrorsChange={setRepoErrors}
              primaryBranchLocked={governanceReadOnly}
              storedBranch={project?.integration_branch ?? ''}
              /* Both forge writes are `projects:create` (`routes/git_connections.py`): a maintainer without it
              connects existing repositories only, as before. */
              initEmpty={mayWriteRepos}
              canCreate={mayWriteRepos}
              org={otherOrg}
              flipLocked={!canAssign}
              nameCell={
                /* Default agent's width, so the two fields' right edges line up. */
                <DetailCell basis="18rem" max="30rem">
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
              }
            />

            {/* The project's default agent: what an unpicked task runs (else today's behaviour). The task form's
            own picker (`components/agents/AgentPicker.tsx`), with no persona: a project spans them. Edit only. */}
            {form.mode === 'edit' && (
              <FieldRow>
                <DetailCell basis="18rem" max="30rem">
                  <AgentPicker
                    id="project-default-agent"
                    label="Default agent"
                    help="Runs tasks that pick no agent."
                    value={{ agentId: form.default_agent_id, provider: form.default_provider }}
                    onChange={pick => patch({ default_agent_id: pick.agentId, default_provider: pick.provider })}
                    defaultLabel={ANY_AGENT_LABEL}
                    // Another org's project offers that org's agents, read as it.
                    org={otherOrg}
                  />
                </DetailCell>
                {/* What a new task here starts as (`withProjectPersona`); `''` = the org's Settings default. Only the
                personas the server serves as `project_default` (`PROJECT_DEFAULT_PERSONAS`). */}
                <DetailCell basis="12rem" max="18rem">
                  <FormTextField
                    id="project-default-persona"
                    label="Default persona"
                    help="New tasks here start as this persona."
                    value={form.default_persona}
                    onChange={v => patch({ default_persona: v })}
                    error={errorFor('default_persona')}
                    /* The picker's labels, but the shipped default's plain one: "Coder (default)" here would contradict
                    the Org default option. */
                    options={personaOptions()
                      .filter(o => projectDefaultPersona(o.value))
                      .map(o => (o.value === DEFAULT_PERSONA ? { ...o, label: personaLabel(o.value) } : o))}
                  >
                    <option value="">{`Org default (${personaLabel(orgPersona)})`}</option>
                    {form.default_persona && !personaOptions().some(o => o.value === form.default_persona) && (
                      <option value={form.default_persona}>{personaLabel(form.default_persona)}</option>
                    )}
                  </FormTextField>
                </DetailCell>
              </FieldRow>
            )}

            {/* The user's own view of the board (`lib/taskMute.ts`), saved on change apart from this form. */}
            {numericId !== null && (
              <Box sx={{ pt: 1.5 }}>
                <MuteSwitch scope={{ kind: 'project', projectId: numericId }} noun="project" />
              </Box>
            )}
          </Box>
        </TabPanel>

        <TabPanel id="governance" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            {/* Read-only for any viewer the verdict does not admit (`governanceIsReadOnly`, also read by
            `handleSubmit`). The notice is drawn only on a real `false`, not while locked pending. `divided` follows
            the notice, since it tracks whether something is drawn above this section. */}
            {/* The notice and the three git groups go whole on a project with no repository, so no orphan header or
            divider is left above Agent instructions. */}
            {formHasRepo(form) && (
            <>
            {governanceWithheld && <GovernanceReadOnlyNotice />}

            <DetailSection title="Branch policy" divided={governanceWithheld}>
              {/* One decision on one line: prefix, whether we may write. The integration branch is each
              repository's, on General (the primary's is the project's). `align="start"`: an error line under one
              input must not shift the others. */}
              <Box sx={{ minWidth: 0 }}>
                <FieldRow align="start" rowGap="0.75rem">
                  <DetailCell basis="9rem" max="14rem">
                    <FormTextField
                      id="project-branch-prefix"
                      label="Branch prefix"
                      placeholder="e.g. sn/"
                      help="Pushes and PRs only from branches under it. Empty: unrestricted."
                      value={form.branch_prefix}
                      onChange={v => patch({ branch_prefix: v })}
                      readOnly={governanceReadOnly}
                    />
                  </DetailCell>

                  <DetailCell>
                    <SettingCheckbox
                      name="project-push-enabled"
                      label="Push enabled"
                      help="Off: no pushes or PRs; the clone's push URL is removed."
                      /* `checked` + `indeterminate` for three states; an empty box would claim `false`. */
                      checked={form.push_enabled === true}
                      indeterminate={form.push_enabled === null}
                      disabled={governanceReadOnly}
                      onChange={next => patch({ push_enabled: next })}
                    />
                  </DetailCell>
                </FieldRow>

              </Box>
            </DetailSection>

            {/* Gate and stack pairs: same shape, different questions, so two sections. */}
            <DetailSection title="Test gates" divided>
              <FieldRow rowGap="0.75rem">
                <DetailCell basis="16rem" max="26rem">
                  <FormTextField
                    id="project-gate-quick-cmd"
                    label="Quick gate command"
                    placeholder="None"
                    help="Runs on every commit and Stop hook; keep it fast."
                    value={form.gate_quick_cmd}
                    onChange={v => patch({ gate_quick_cmd: v })}
                    readOnly={governanceReadOnly}
                    error={errorFor('gate_quick_cmd')}
                  />
                </DetailCell>

                <DetailCell basis="16rem" max="26rem">
                  <FormTextField
                    id="project-gate-full-cmd"
                    label="Full gate command"
                    placeholder="None"
                    help={
                      isEnabled('merge_lane') && project?.merge_lane
                        ? "The merge lane runs it on each batch's merged tree."
                        : 'Runs once before a pull request is opened.'
                    }
                    value={form.gate_full_cmd}
                    onChange={v => patch({ gate_full_cmd: v })}
                    readOnly={governanceReadOnly}
                    error={errorFor('gate_full_cmd')}
                  />
                </DetailCell>

                {isEnabled('merge_lane') && formHasRepo(form) && form.mode === 'edit' && (
                  <DetailCell basis="7rem" max="10rem">
                    <FormTextField
                      id="project-merge-lane-batch-max"
                      label="Batch size"
                      placeholder="6"
                      help="PRs per merge-lane batch."
                      type="number"
                      value={form.merge_lane_batch_max}
                      onChange={v => patch({ merge_lane_batch_max: v })}
                      error={errorFor('merge_lane_batch_max')}
                    />
                  </DetailCell>
                )}
              </FieldRow>
            </DetailSection>

            <DetailSection title="Stack commands" divided>
              <FieldRow rowGap="0.75rem">
                <DetailCell basis="16rem" max="26rem">
                  <FormTextField
                    id="project-stack-up-cmd"
                    label="Stack up command"
                    placeholder="None"
                    help="Starts the slot's stack. No secrets: viewers see it."
                    value={form.stack_up_cmd}
                    onChange={v => patch({ stack_up_cmd: v })}
                    readOnly={governanceReadOnly}
                    error={errorFor('stack_up_cmd')}
                  />
                </DetailCell>

                <DetailCell basis="16rem" max="26rem">
                  <FormTextField
                    id="project-stack-down-cmd"
                    label="Stack down command"
                    placeholder="None"
                    help="Stop containers only, keep volumes. No secrets: viewers see it."
                    value={form.stack_down_cmd}
                    onChange={v => patch({ stack_down_cmd: v })}
                    readOnly={governanceReadOnly}
                    error={errorFor('stack_down_cmd')}
                  />
                </DetailCell>
              </FieldRow>
            </DetailSection>
            </>
            )}

            {/* The one field here that is not tier-authored: no `readOnly`/`disabled`, since any `projects:edit`
            holder may write it and `buildBody` keeps it out of `governanceBody`.
            The heading is the label: the notice names this section ("Agent instructions at the foot of this tab
            included"), so the heading stays and the input drops its own label, pointing at the heading via
            `aria-labelledby` (one string, no drift; `getByLabelText` still resolves). No `FormField`: no label to
            own, no rule to fail; `fullWidth`/`size="small"` spelled here.
            Last in the panel. Three rows, matching Notes, since the panel's phone height is an e2e oracle;
            `TEXTAREA_RESIZE_SX` gives the drag handle. */}
            <DetailSection title="Agent instructions" titleId="project-agent-instructions-label" help={AGENT_INSTRUCTIONS_HELP} divided={formHasRepo(form)}>
              {/* One block, as Branch policy, for the same measured gap reason. */}
              <Box sx={{ minWidth: 0 }}>
                <TextField
                  id="project-agent-instructions"
                  multiline
                  rows={3}
                  fullWidth
                  size="small"
                  value={form.agent_instructions}
                  onChange={e => patch({ agent_instructions: e.target.value })}
                  slotProps={{ htmlInput: { 'aria-labelledby': 'project-agent-instructions-label', 'aria-description': AGENT_INSTRUCTIONS_HELP } }}
                  sx={[fieldTapTargetSx, TEXTAREA_RESIZE_SX, { maxWidth: '44rem' }]}
                />

              </Box>
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="slots" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            {/* Ports size a git worktree's stack, which a folder slot does not run (`REPO_ONLY_KEYS`). */}
            {formHasRepo(form) && (
            <FieldRow align="center" rowGap="0.75rem">
              <DetailCell basis="7rem" max="10rem">
                <FormTextField
                  id="project-port-block"
                  label="Port block"
                  help="This project's unique index into the port grid."
                  /* Empty means no ports (`lease_drain.py`'s band): said in the box, which also shrinks the label —
                  unshrunk, the dotted label filled the box and it read as a button. */
                  placeholder="None"
                  type="number"
                  value={form.port_block}
                  onChange={v => patch({ port_block: v })}
                  error={errorFor('port_block')}
                />
              </DetailCell>
            </FieldRow>
            )}

            {/* Its own row: a comma-separated list earns a wide box. */}
            {formHasRepo(form) && (
            <FieldRow rowGap="0.75rem">
              <DetailCell basis="20rem" max="34rem">
                <FormTextField
                  id="project-port-vars"
                  label="Port vars"
                  placeholder="e.g. FRONTEND_PORT, API_PORT"
                  help="Comma-separated env vars that receive each slot's host ports."
                  value={form.port_vars}
                  onChange={v => patch({ port_vars: v })}
                />
              </DetailCell>
            </FieldRow>
            )}

            {/* Which nodes run this project: the shared pick list, each tick applied at once (no Save). The gear on
            an assigned row is the one static-slots control, offered while the plan allows any. */}
            {worktreesVisited && (
              <Box sx={{ mt: 1.5 }}>
                <AssignmentList
                  label="Nodes"
                  help="Nodes that run this project; ticks apply at once."
                  side="project"
                  idPrefix="project-nodes"
                  rows={assignments.current}
                  busy={assignments.busy}
                  note={assignments.note}
                  onAssign={assignments.assign}
                  onUnassign={assignments.unassign}
                  onOverride={assignments.setOverride}
                  readOnly={!canAssign}
                  loading={assignments.loading}
                  error={assignments.error ? 'Failed to load nodes' : null}
                  destructiveHelp={destructiveHelp('project')}
                  warning={emptyAssignmentWarning('project', assignments.current, assignments.onboardingActive)}
                  staticSlots={staticOffered && formHasRepo(form)}
                />
              </Box>
            )}
          </Box>
        </TabPanel>

        {/* Outside the form's body; gated on existence (`tabsFor`). Rendered only with its tab, or `TabPanel`
        would fetch on create. Inside the `<form>`, so no nested `<form>` and `type="button"` on every control.
        `canEdit` gates one tier, not the tab: a `projects:view` holder may still write their own override
        (R3/D10). `admin?.id` is the only `user_id` their write may name. Owners are named via `memberLabel`. */}
        {numericId !== null && (
          <TabPanel id="environment" current={activeTab} idPrefix="project">
            <Box sx={{ pt: 1.5 }}>
              <ProjectEnvironmentTab
                projectId={numericId}
                canEdit={canEditProject('projects:edit', numericId)}
                viewerId={admin?.id ?? null}
                overrideOwners={overrideOwners}
              />
            </Box>
          </TabPanel>
        )}

        {/* A saved row's, like Environment; its body mounts only while its tab is open, so the page's other tabs
        never fetch it. Retire and Purge need `projects:edit`; reading needs only the page's own view. */}
        {numericId !== null && (
          <TabPanel id="memory" current={activeTab} idPrefix="project">
            {activeTab === 'memory' && (
              <Box sx={{ pt: 1.5 }}>
                <ProjectMemoryTab
                  projectId={numericId}
                  canEdit={canEditProject('projects:edit', numericId)}
                  orgId={otherOrg?.id ?? (admin?.is_operator === true ? admin.org?.id : null) ?? null}
                  repos={[...(project?.repos ?? [])]
                    .sort((a, b) => Number(b.is_primary) - Number(a.is_primary))
                    .map(r => r.name)}
                />
              </Box>
            )}
          </TabPanel>
        )}

        {/* Read-only but Stop lane (D11); mounted only while open, like Memory. */}
        {numericId !== null && ProjectMergeLaneTab && (
          <TabPanel id="merge-lane" current={activeTab} idPrefix="project">
            {activeTab === 'merge-lane' && (
              <Box sx={{ pt: 1.5 }}>
                <ProjectMergeLaneTab
                  projectId={numericId}
                  projectName={project?.name}
                  canStop={canEditProject('projects:edit', numericId)}
                  promoteUrl={canEditProject('projects:edit', numericId) && project
                    ? promoteLink(project) ?? undefined : undefined}
                />
              </Box>
            )}
          </TabPanel>
        )}

        <TabPanel id="advanced" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            <FieldRow align="center" rowGap="0.75rem">
              <DetailCell basis="9rem" max="16rem">
                <ModelTierField
                  value={form.default_model}
                  choices={modelChoices}
                  onChange={v => patch({ default_model: v })}
                />
              </DetailCell>
            </FieldRow>

            {/* The two named people, opening the panel's titled run (what the project is → who answers for it →
            what it is built with → notes). Edit only (`namesHumans`). */}
            {namesHumans(form) && (
              <DetailSection title="Humans" divided>
                {/* A failed fetch costs these two controls only, stated (as `GroupEditPage`'s picker); both stay
                mounted with their values. Two sentences: with a rehydrated list the names may be stale; with none,
                the picker is down to "— None —". Visible: it reports this page load. */}
                {membersQuery.isError && (
                  <Typography variant="caption" component="p" sx={{ mb: 0.5, color: 'error.main' }}>
                    {membersQuery.data
                      ? 'Could not refresh this project’s people — the lists below are the last we loaded.'
                      : 'Failed to load this project’s people — nobody new can be named until it loads.'}
                  </Typography>
                )}

                {/* 16rem reads common "First Last — email" options whole; the pair stacks below ~33rem. */}
                <FieldRow rowGap="0.75rem">
                  <DetailCell basis="16rem" max="24rem">
                    <MemberSelectField
                      id="project-business-analyst"
                      label="Business analyst"
                      help="Epic questions in business analysis; None sends them to creator."
                      value={form.business_analyst_id}
                      members={members}
                      listState={memberListState}
                      listNarrowed={memberListNarrowed}
                      onChange={v => patch({ business_analyst_id: v })}
                    />
                  </DetailCell>

                  <DetailCell basis="16rem" max="24rem">
                    <MemberSelectField
                      id="project-architect"
                      label="Architect"
                      help="Other epic questions; None sends them to the creator."
                      value={form.architect_id}
                      members={members}
                      listState={memberListState}
                      listNarrowed={memberListNarrowed}
                      onChange={v => patch({ architect_id: v })}
                    />
                  </DetailCell>
                </FieldRow>

              </DetailSection>
            )}

            {/* Any project editor may select Jira. */}
            <DetailSection title="Jira" divided>
              <FieldRow rowGap="0.75rem">
                <DetailCell basis="18rem" max="26rem">
                  <FormField label="Jira credential" help="Which Jira token this project's tasks use.">
                    <CredentialSelect
                      id="project-jira-credential"
                      testId="project-jira-credential"
                      value={jiraId}
                      /* The saved row from the GET by id; wins over the roster so an older credential reads as a name. */
                      selected={jiraSelected}
                      /* Verbatim uuid; never `Number()` — `NaN` serialises to `null` and clears the config. */
                      onChange={next => {
                        setPickedJiraId(next)
                        patch({ jira_credential_id: next })
                      }}
                      /* Only a credential of the project's own org may be attached. */
                      fetchOptions={(query, signal) =>
                        loadCredentialOptions(query, signal, jiraOption, { types: ['jira'], keep: otherOrg ? projectOrgRows : ownOrgRows })}
                      placeholder="Search by name"
                      noOptionsText="No match — add one on the Credentials page."
                    />
                  </FormField>
                </DetailCell>
              </FieldRow>
              <NonBaaWarning surface="jira" />
            </DetailSection>

            <DetailSection title="Tech stack" help="Runners read it as the project's stack." divided>
              {form.tech_stack.map((row, idx) => (
                <Stack
                  key={idx}
                  direction="row"
                  spacing={0.5}
                  useFlexGap
                  sx={{ alignItems: 'center', flexWrap: 'wrap', width: '100%', maxWidth: '34rem', mb: 0.5 }}
                >
                  <TextField
                    id={`project-tech-stack-${idx}-layer`}
                    placeholder="layer (e.g. frontend)"
                    value={row.layer}
                    onChange={e => updateTechStackRow(idx, 'layer', e.target.value)}
                    slotProps={{ htmlInput: { 'aria-label': `Tech stack layer ${idx + 1}` } }}
                    size="small"
                    sx={[fieldTapTargetSx, { flex: '1 1 8rem' }]}
                  />
                  <TextField
                    id={`project-tech-stack-${idx}-technology`}
                    placeholder="technology (e.g. React)"
                    value={row.technology}
                    onChange={e => updateTechStackRow(idx, 'technology', e.target.value)}
                    slotProps={{ htmlInput: { 'aria-label': `Tech stack technology ${idx + 1}` } }}
                    size="small"
                    sx={[fieldTapTargetSx, { flex: '1 1 8rem' }]}
                  />
                  <IconButton
                    aria-label={`Remove tech stack row ${idx + 1}`}
                    title="Delete"
                    onClick={() => setForm(f => f ? { ...f, tech_stack: f.tech_stack.filter((_, i) => i !== idx) } : null)}
                  >
                    <Box component="span" aria-hidden="true" sx={{ fontSize: '1rem', lineHeight: 1 }}>×</Box>
                  </IconButton>
                </Stack>
              ))}
              <Button
                type="button"
                variant="text"
                onClick={() => setForm(f => f ? { ...f, tech_stack: [...f.tech_stack, { layer: '', technology: '' }] } : null)}
                sx={tapTargetSx}
              >
                Add
              </Button>
            </DetailSection>

            {/* Untitled: the field's label says "Notes". The divider comes from the shared control. */}
            <DetailSection divided>
              <FormTextField
                id="project-notes"
                label="Notes"
                help="Free text runners can read."
                multiline
                rows={3}
                value={form.notes}
                onChange={v => patch({ notes: v })}
                sx={[TEXTAREA_RESIZE_SX, { maxWidth: '44rem' }]}
              />
            </DetailSection>
          </Box>
        </TabPanel>

        {/* The page's action bar: one form, one submit, reachable from every panel but Access, MicroVM and Live Cast (outside the
        form, below; each saves on its own) and Merge lane and Memory (nothing there to save; at 360px the bar would
        only cover the list) and Budget (its own Save, outside the form). Cancel is a real `RouterLink`. */}
        {!NO_FORM_ACTIONS.has(activeTab) && (
          <FormActionBar testId="project-form-actions" stretchTestIds={['project-form-submit']}>
            {/* Both: `tapTargetSx` keeps the 44px floor up to 599px (`sm`); `BUTTON_SX` keeps a disabled button
            hit-testable for the reachability probes. */}
            <Typography
              role="status"
              variant="body2"
              data-testid="project-form-unchanged"
              sx={{ color: 'text.secondary', alignSelf: 'center', mr: 'auto' }}
            >
              {unchangedDraft !== null && unchangedDraft === form ? 'No changes to save.' : null}
            </Typography>
            <Button
              component={RouterLink}
              to="/projects"
              variant="text"
              color="inherit"
              sx={[tapTargetSx, BUTTON_SX]}
            >
              Cancel
            </Button>
            <SubmitButton
              busy={saving}
              testId="project-form-submit"
              sx={BUTTON_SX}
            >
              Save
            </SubmitButton>
          </FormActionBar>
        )}
      </Box>

      {/* Outside the `<form>`: its controls and dialogs are not the project form's (a portal's submit still
      bubbles through the React tree). Mounted only with its tab, since it fetches. */}
      {/* Outside the `<form>` too: its editors are forms of their own, each saving at once. */}
      {project && microvmShown && ProjectMicrovmTab && (
        <TabPanel id="microvm" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            <ProjectMicrovmTab project={project} canEdit={canEditProject('projects:edit', project.id)} />
          </Box>
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own Save and Publish. */}
      {numericId !== null && liveCastShown && ProjectLiveCastTab && (
        <TabPanel id="live-cast" current={activeTab} idPrefix="project">
          {activeTab === 'live-cast' && (
            <Box sx={{ pt: 1.5 }}>
              <ProjectLiveCastTab projectId={numericId} canEdit org={otherOrg} />
            </Box>
          )}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own Save, to its own route — never the project's PUT. */}
      {numericId !== null && budgetShown && (
        <TabPanel id="budget" current={activeTab} idPrefix="project">
          {activeTab === 'budget' && (
            <Box sx={{ pt: 1.5, maxWidth: 480 }}>
              <SectionCard>
                <BudgetField scope="project" scopeId={numericId} org={otherOrg} />
              </SectionCard>
            </Box>
          )}
        </TabPanel>
      )}
      {numericId !== null && (
        <TabPanel id="access" current={activeTab} idPrefix="project">
          <Box sx={{ pt: 1.5 }}>
            {/* Push is an access decision too, but a governance field saved with the form: named here, set there. */}
            <Typography variant="body2" sx={{ color: 'text.secondary', mb: 1.5 }} data-testid="project-access-push">
              Push{form.push_enabled === null ? '' : form.push_enabled ? ': on' : ': off'} ·{' '}
              <RouterLink to={{ search: '?tab=governance' }}>Governance</RouterLink>
            </Typography>
            {activeTab === 'access' && <AccessTab scope={{ type: 'project', id: String(numericId) }} otherOrg={otherOrg} />}
          </Box>
        </TabPanel>
      )}
      {changeOrg.dialog}
    </DetailPage>
  )
}
