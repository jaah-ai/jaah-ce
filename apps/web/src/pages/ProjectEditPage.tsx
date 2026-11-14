import { type ComponentType, useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, get, put } from '../api'
import { currentOrgId, useAuth, useCan, useCanOnProject } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import AutomationList from './automations/AutomationList'
import Breadcrumb from '../components/Breadcrumb'
import ProjectEnvironmentTab from '../components/projects/ProjectEnvironmentTab'
import ProjectProductionQaTab from '../components/projects/ProjectProductionQaTab'
import PipelineSettings from '../components/projects/PipelineSettings'
import ProjectBenchmarkTab from '../components/projects/ProjectBenchmarkTab'
import { inheritedInterval, useOutcomeScan, useOutcomeScanShown } from '../components/projects/outcomeScan'
import BudgetField from '../components/budget/BudgetField'
import { useProjectLiveCastOn, useProjectLiveCastShown } from '../lib/liveCastSurface'
import { isEnabled } from '../lib/editionFeatures'
import { editionModule } from '../lib/editionModule'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useBaselineDirty } from '../hooks/useBaselineDirty'
import { useEditRecord } from '../hooks/useEditRecord'
import { useLeaveGuard } from '../hooks/useLeaveGuard'
import { useSubmit } from '../hooks/useSubmit'
import { PROJECTS_KEY, WORKTREES_KEY } from '../hooks/useRosters'
import { AUDIT_TAB_PATHS } from '../routeGroups'
import { type TaskModelChoice, useTaskModels } from '../hooks/useTaskModels'
import { useUrlTab } from '../hooks/useUrlTab'
import { NotOnPlan, planSurfaceShown, planSwitchOn } from '../lib/planSwitches'
import ProjectNodeGroupsField, { NODE_GROUPS_ANCHOR } from '../components/projects/ProjectNodeGroupsField'
import AdvancedGroup from '../components/projects/AdvancedGroup'
import ProjectRepoLine from '../components/projects/ProjectRepoLine'
import { AdvancedSection } from '../ui/AdvancedSection'
import ExternalLink from '../ui/ExternalLink'
import { safeHref } from '../lib/safeHref'
import AccessTab from '../components/access/AccessTab'
import type { OrgRef, Project, ProjectMember, TechStackEntry } from '../types'
import {
  blankRepoFields, buildBody, FIX_MARKED_REPOS, formFromProject, formHasRepo, type GovernanceAuthority, governanceIsReadOnly, isHidden,
  NewRepoError, prepareRepos, type ProjectFormState, type ProjectMemoryChoice, refusedBodyLoc, type RepoDraft, repoRowErrors, type RepoRowErrors,
} from '../components/projects/projectForm'
import { withoutFieldErrors } from '../components/projects/repoEditing'
import { useFocusTarget } from '../components/projects/ProjectRepoFields'
import { announceMoveTask, jaahRepoReplaced, useJaahRepoQuestion } from '../components/projects/jaahRepoReplaced'
import ProjectRoutingFields from '../components/projects/ProjectRoutingFields'
import { memberLabel } from '../lib/memberLabel'
import Banner from '../ui/Banner'
import { GitReachBanner } from '../components/GitReach'
import DetailStatus from '../ui/detail/DetailStatus'
import BranchesSection, { branchesActive, POLICY_REPO_PARAM, policyRepoParam } from '../components/projects/customize/BranchesSection'
import { basicPipeline, pipelineTabShown, usePipelineBasic } from '../components/projects/customize/CiPipelineSection'
import CustomizeTab from '../components/projects/customize/CustomizeTab'
import { projectTextCell, PROJECT_FIELDS_DOCS, type StringFieldKey } from '../components/projects/customize/projectFields'
import {
  customizeSections, type CustomizeSectionKey, FIELD_SECTIONS, LEGACY_SECTIONS, SECTION_PARAM, sectionFromUrl,
} from '../components/projects/customize/sections'
import TestingSection, { testingActive } from '../components/projects/customize/TestingSection'
import { listProjectMcpLinks } from './credentials/integrations/api'
import { useApiRead } from '../hooks/useApiRead'
import { DetailCell, DetailPage, DetailSection } from '../ui/detail/DetailPage'
import InlineTitleEdit from '../ui/InlineTitleEdit'
import FieldRow from '../ui/FieldRow'
import FormBox from '../ui/FormBox'
import { BUTTON_SX, TEXTAREA_RESIZE_SX } from '../ui/formDensity'
import CancelSaveBar from '../ui/CancelSaveBar'
import { NO_CHANGES_HINT } from '../components/tasks/TaskEditForm'
import FormTextField from '../ui/FormTextField'
import type { Explain, HelpProp } from '../ui/explain'
import { personaLabel, personaOptions, projectDefaultPersona } from '../components/tasks/personaRegistry'
import { resolveProjectMemoryDefault, useTaskCreateDefaults } from '../components/tasks/taskDefaults'
import { ADVISOR_PERSONA, DEFAULT_PERSONA } from '../components/tasks/taskPersona'
import { AgentPicker, ANY_AGENT_LABEL } from '../components/agents/AgentPicker'
import { fieldTapTargetSx, tapTargetSx } from '../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, grid, LINE_HEIGHT } from '../theme'
import { ActingForNotice, OrgAside, useOtherOrg } from '../ui/OrgIndicator'
import SectionCard from '../ui/SectionCard'
import { LeaveGuardDialog } from '../ui/UnsavedChangesDialog'
import ProjectTrackersSection, { TRACKERS_HELP } from '../components/projects/ProjectTrackersSection'
import { useChangeOrgAction } from '../components/ChangeOrgDialog'
import { useActAsRows } from '../lib/actAs'
import { lockedForMember, memberBody } from '../lib/jaahManaged'

/* The project edit page, `/projects/:id` (creating one is `ProjectCreateWizard`). A routed page, so the form
 * is linkable and has room.
 * It wears the task editor's layout through shared primitives (`ui/FieldRow`, `ui/SettingCheckbox`,
 * `ui/FormActionBar`, `ui/formDensity`): tabs (see `tabsFor`); rows of rem-based cells that wrap on their
 * own; each field's explanation a hover on its label (`help`, `ui/HelpLabel`), while value notes about the
 * loaded row stay visible; no card (`DetailPage`'s `measure="read"`, reasoned at the call site).
 * Panel heights are the oracle of `e2e/project-edit-tabs.spec.ts`.
 * The form model (`ProjectFormState`, seeders, `buildBody`, the governance predicate) is
 * `components/projects/projectForm.ts`, shared with the create wizard; the governance notice is here.
 * The row loads through `useEditRecord` (`GET /api/projects/{id}`, `app/routes/portfolio.py::get_project`, a
 * `list_projects` row) under the `['project', id]` key, so a tab's own save that invalidates `['project']` re-reads
 * it behind the form; Save goes through `useSubmit`.
 */


/* The one line explaining the greyed governance controls: a page `Banner` at `severity="info"` (as
 * `GroupEditPage`'s reserved-group notice), atop the Customize tab. It is the API's own 403 said beforehand: which
 * sections, and who to ask — both doors, superadmin and `nodes:edit`
 * (`apps/api/app/routes/projects.py::_require_authority_to_author_governance`); why (shell commands the fleet runs
 * unprompted) and that the rest saves are its hover. Never says "superadmin" (`test/tenantCopy.ts::OPERATOR_SCOPE_WORDS`). */
function GovernanceReadOnlyNotice() {
  return (
    <Banner severity="info" testId="project-governance-read-only">
      <Box component="span" title="They run as a shell on every node. Your other changes still save.">
        Branch policy, gates and stack commands are read only for you. Ask an owner, or an admin with node-edit permission.
      </Box>
    </Banner>
  )
}

/* Grouping and cells are `ui/detail`'s.
 * · `DetailSection`: `titleId` names the section whose title is its field's accessible name (Agent instructions).
 *   The divider is opt-in (`divided`), a prop rather than `:first-of-type`.
 * · `DetailCell`: `basis` is the usable width and decides wrapping; `max` caps per field; omitted `basis`
 *   is a natural-width cell (a checkbox). */

/* The Run jobs as picker: a native select (as `GroupEditPage`, `CredentialEditPage`) via
 * `ui/FormTextField`, matching the text fields. `inputLabel: { shrink: true }` is required: a native select
 * always shows a value. The value is a uuid string passed through untouched — `Number(...)` made `NaN`
 * and emptied a group in `GroupEditPage`. */
function MemberSelectField({ id, label, help, value, members, listState, listNarrowed, onChange, emptyLabel = '— None —', currentName }: {
  id: string
  label: string
  help?: HelpProp
  /** A `users.id`, or `''` for nobody. */
  value: string
  members: ProjectMember[]
  /** Whether `members` has arrived; see the orphan block. */
  listState: 'loading' | 'error' | 'ready'
  /** Whether a settled `members` is still incomplete for this reader; see the orphan block. */
  listNarrowed: boolean
  onChange: (value: string) => void
  /** The empty option's words: what nobody named means for this field. */
  emptyLabel?: string
  /** The saved person's name as the record carries it: their orphan option reads as them, with no warning. */
  currentName?: string | null
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
  const named = orphaned && Boolean(currentName)
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
      hint={orphaned && !named && listState === 'ready'
        ? listNarrowed
          ? `Set to ${value} — this list does not show every account with access. Saving keeps them; pick someone else, or None, to change it.`
          : `Set to ${value}, who can no longer reach this project. Saving keeps them — pick someone else, or None, to change it.`
        : undefined}
      options={members.map(m => ({ value: m.id, label: memberLabel(m) }))}
    >
      {/* First, spelled as a choice: clearing the role is something operators come here to do. */}
      <option value="">{emptyLabel}</option>
      {orphaned && <option value={value} disabled>{named ? currentName : orphanLabel}</option>}
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
      help="Model tier when task and persona set none."
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

type ProjectTab =
  'general' | 'customize' | 'access' | 'environment'
  | 'production-qa' | 'benchmark' | 'budget' | 'pipeline' | 'automations'

/* The Jaah-repo question's Cancel: the Save ends with nothing written and nothing to report. */
const SAVE_CANCELLED = Symbol('save cancelled')

const GENERAL_TAB: TabDef<ProjectTab> = { id: 'general', label: 'General' }
/* The pipeline's stages, steps and freezes, one Save (`components/projects/PipelineSettings.tsx`): a saved row's with a
   repository, on every plan — second, so it is on-screen in the strip at any width.
   The daily Release is the pipeline page's Release step and view (`components/pipeline/ReleaseStep.tsx`). */
const PIPELINE_TAB: TabDef<ProjectTab> = { id: 'pipeline', label: 'Pipeline' }
/* The automations whose Target reaches this project (`pages/automations/AutomationList.tsx`'s `scope`): a saved row's,
   an org admin's alone, since every `/api/automations` route is `@org_admin`. */
const AUTOMATIONS_TAB: TabDef<ProjectTab> = { id: 'automations', label: 'Automations' }
/* Second, so it is on-screen in the strip at any width. */
const ACCESS_TAB: TabDef<ProjectTab> = { id: 'access', label: 'Access' }
/* Branches, Testing and the rest of a project's opt-ins, each a fold (`components/projects/customize/CustomizeTab.tsx`). */
const CUSTOMIZE_TAB: TabDef<ProjectTab> = { id: 'customize', label: 'Customize' }
/* Collections need a saved project id. */
const ENVIRONMENT_TAB: TabDef<ProjectTab> = { id: 'environment', label: 'Environment' }
/* The tabs with no Cancel/Save bar in the project form: nothing there it saves (see the bar's own comment), or — Customize
   — the bar drawn inside the tab, under the sections it saves. */
const NO_FORM_ACTIONS: ReadonlySet<ProjectTab> = new Set(['pipeline', 'automations', 'access', 'customize', 'production-qa', 'benchmark', 'budget'])
/* Retired tab ids, each to the tab that holds it now, so old links still land: Slots and Advanced (and `worktrees`, the
   Slots tab's id before it matched its label) are General's Advanced fold, opened (`ADVANCED_LEGACY_TABS`);
   `deployments` (the retired Deployments tab, whose setup is Pipeline); and the four tabs Customize replaced, each
   opening its section (`customize/sections.tsx::LEGACY_SECTIONS`). */
const ADVANCED_LEGACY_TABS: ReadonlySet<string> = new Set(['slots', 'worktrees', 'advanced'])
const LEGACY_TABS: Readonly<Record<string, ProjectTab>> = {
  ...Object.fromEntries([...ADVANCED_LEGACY_TABS].map(tab => [tab, 'general' as const])),
  deployments: 'pipeline',
  ...Object.fromEntries(Object.keys(LEGACY_SECTIONS).map(tab => [tab, 'customize' as const])),
}
/* The Customize tab's form id: its Save bar, under the form-saved sections, submits the form the form-saved sections sit in. */
const CUSTOMIZE_FORM_ID = 'project-customize-form'
/* An Easy Mode project's hosted app (`components/hostedApps/HostedAppPanel.tsx`, `easy_mode_hosting`): its hosting
   state atop General, its custom domains under General's live line, its usage inside General's Advanced. */
const HostedAppStatus = editionModule(isEnabled('easy_mode_hosting'),
  () => import.meta.glob<ComponentType<{ projectId: number; canEdit: boolean; otherOrg: OrgRef | null }>>(
    '../components/hostedApps/HostedAppPanel.tsx', { eager: true, import: 'default' }))
const HostedAppDomains = editionModule(isEnabled('easy_mode_hosting'),
  () => import.meta.glob<ComponentType<{ projectId: number; otherOrg: OrgRef | null }>>(
    '../components/hostedApps/HostedAppPanel.tsx', { eager: true, import: 'HostedAppDomains' }))
const HostedAppUsage = editionModule(isEnabled('easy_mode_hosting'),
  () => import.meta.glob<ComponentType<{ projectId: number }>>(
    '../components/hostedApps/HostedAppPanel.tsx', { eager: true, import: 'HostedAppUsage' }))
/* The retired Merge lane and Memory tabs, now Audit's, filtered to the project (`pages/audit/MergeLanesTab.tsx`,
   `MemoryTab.tsx`): an old `?tab=` link lands there. */
const AUDIT_TABS: Readonly<Record<string, string>> = {
  'merge-lane': AUDIT_TAB_PATHS.mergeLanes,
  memory: AUDIT_TAB_PATHS.memory,
}
/* Its own Save (`components/projects/ProjectProductionQaTab.tsx`): a saved row's, and only while the org's
   `production_qa` switch is on — or for the operator (`lib/planSwitches.tsx::planSurfaceShown`). */
const PRODUCTION_QA_TAB: TabDef<ProjectTab> = { id: 'production-qa', label: 'Production QA' }
/* Its own Save (`components/projects/ProjectBenchmarkTab.tsx`): only while the org's `benchmark_mode` switch is on
   (`benchmark_offered`) — an org without it gets no benchmark settings (R6.1). */
const BENCHMARK_TAB: TabDef<ProjectTab> = { id: 'benchmark', label: 'Benchmark' }
/* Its own Save and Remove (`components/budget/BudgetField.tsx`): an org admin's, as the project's org.
   Shown while the plan's switch is off too, where it says so and links the upgrade. */
const BUDGET_TAB: TabDef<ProjectTab> = { id: 'budget', label: 'Budget' }

/* A security warning on a hover, so it is also the textarea's accessible description: the text reaches every
 * runner's system prompt, outranks the charter, and is copied into session transcripts. */
const AGENT_INSTRUCTIONS_HELP: Explain = {
  tip: "Every agent's prompt; outranks charter. Viewers and transcripts see it.",
  more: "Given to every agent that works on this project. Where it disagrees with Jaah's own procedure, it wins. "
    + 'Anyone who can view the project, and every session transcript, can read it: never put a secret here.',
  docs: 'console-reference/directory/projects',
}

/* Access and Environment need a saved project. The order is S11's: General, Customize, Pipeline, Access, Environment,
 * Automations, then the plan-gated ones. */
function tabsFor(
  hasRow: boolean,
  budgetShown: boolean,
  benchmarkShown: boolean,
  productionQaShown: boolean,
  pipelineShown: boolean,
  automationsShown: boolean,
): readonly TabDef<ProjectTab>[] {
  return [
    GENERAL_TAB,
    ...(hasRow ? [CUSTOMIZE_TAB] : []),
    ...(hasRow && pipelineShown ? [PIPELINE_TAB] : []),
    ...(hasRow ? [ACCESS_TAB] : []),
    ...(hasRow ? [ENVIRONMENT_TAB] : []),
    ...(hasRow && automationsShown ? [AUTOMATIONS_TAB] : []),
    ...(hasRow && productionQaShown ? [PRODUCTION_QA_TAB] : []),
    ...(hasRow && benchmarkShown ? [BENCHMARK_TAB] : []),
    ...(hasRow && budgetShown ? [BUDGET_TAB] : []),
  ]
}

const DEFAULT_TAB: ProjectTab = 'general'

/* The General fields folded under Advanced: a refusal naming one opens the fold before focusing it. */
const ADVANCED_FIELDS: ReadonlySet<string> = new Set([
  'default_model', 'default_agent_id', 'default_provider', 'project_memory', 'routing_mode', 'routing_latency_cap_hours',
  'experiment_budget_share', 'experiment_budget_pct', 'run_jobs_as_id',
  'tech_stack', 'notes',
])
/* The number fields. `noValidate` also disables bad-input checking: typing `1e` makes Chrome hand
 * React `''` while displaying `1e`, and `buildBody` would send `merge_lane_batch_max: null` with a success message.
 * `validity.badInput` is still readable under `noValidate`; read it off the live element, since the draft
 * has already lost the value. */
const NUMBER_FIELDS = [
  { key: 'merge_lane_batch_max', tab: 'customize', inputId: 'project-merge-lane-batch-max', label: 'Batch size' },
  { key: 'outcome_scan_interval_minutes', tab: 'customize', inputId: 'project-outcome-scan-interval', label: 'Outcome scan interval' },
  { key: 'routing_latency_cap_hours', tab: 'general', inputId: 'project-routing-latency-cap', label: 'Latency cap' },
  { key: 'experiment_budget_pct', tab: 'general', inputId: 'project-experiment-budget', label: 'Experiment budget' },
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
/* Only string-valued keys may be listed (`StringFieldKey`, `customize/projectFields.tsx`). */

/* Server validation uses the same tab destinations as local validation, including optional fields. */
const FIELD_TABS: Record<string, { tab: ProjectTab; inputId: string }> = {
  ...Object.fromEntries(NUMBER_FIELDS.map(f => [f.key, f])),
  default_model: { tab: 'general', inputId: 'project-default-model' },
  default_agent_id: { tab: 'general', inputId: 'project-default-agent' },
  default_provider: { tab: 'general', inputId: 'project-default-agent' },
  default_persona: { tab: 'general', inputId: 'project-default-persona' },
  routing_mode: { tab: 'general', inputId: 'project-routing-mode' },
  experiment_budget_share: { tab: 'general', inputId: 'project-experiment-budget' },
  security_check_patterns: { tab: 'customize', inputId: 'project-security-check-patterns' },
  run_jobs_as_id: { tab: 'general', inputId: 'project-run-jobs-as' },
  tech_stack: { tab: 'general', inputId: 'project-tech-stack-0-layer' },
  notes: { tab: 'general', inputId: 'project-notes' },
  project_memory: { tab: 'general', inputId: 'project-memory' },
  agent_instructions: { tab: 'general', inputId: 'project-agent-instructions' },
  branch_prefix: { tab: 'customize', inputId: 'project-branch-prefix' },
  push_enabled: { tab: 'customize', inputId: 'project-read-only' },
  gate_quick_cmd: { tab: 'customize', inputId: 'project-gate-quick-cmd' },
  stack_up_cmd: { tab: 'customize', inputId: 'project-stack-up-cmd' },
  stack_down_cmd: { tab: 'customize', inputId: 'project-stack-down-cmd' },
}

export default function ProjectEditPage() {
  const { id } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const tab = searchParams.get('tab') ?? ''
  if (id !== undefined && /^\d+$/.test(id) && Object.hasOwn(AUDIT_TABS, tab)) {
    return <Navigate replace to={`${AUDIT_TABS[tab]}?project=${id}`} />
  }
  if (id !== undefined && !/^\d+$/.test(id)) return <ProjectNameRedirect name={id} />
  return <ProjectEditor />
}

/* `/projects/<name>`: a project's name is its human handle (unique per org), its id the address. Ask the
 * server for that exact name (`GET /api/projects?name=`, never the bounded roster) and redirect to the id
 * URL, keeping `?tab=` and the hash. Two orgs may share a name: the acting org's wins, else a lone match.
 * Nothing found is the editor's own not-found. An all-digit name is read as an id: the segment is shared. */
function ProjectNameRedirect({ name }: { name: string }) {
  const location = useLocation()
  const { admin } = useAuth()
  const query = useQuery({
    queryKey: ['project-by-name', name],
    queryFn: () => get<Project[]>(`/api/projects?${new URLSearchParams({ name })}`),
  })
  const crumbs = <Breadcrumb items={[{ label: 'Projects', to: '/projects' }, { label: name }]} />
  if (query.isPending) return <DetailStatus trail={crumbs} loading />
  const rows = query.data ?? []
  const orgId = admin ? currentOrgId(admin) : null
  const match = rows.find(p => p.org?.id === orgId) ?? (rows.length === 1 ? rows[0] : undefined)
  if (match) return <Navigate replace to={`/projects/${match.id}${location.search}${location.hash}`} />
  if (query.isError) return <DetailStatus trail={crumbs} error={apiDetail(query.error, 'Failed to load projects')} />
  return <DetailStatus trail={crumbs} notFound="Project not found" />
}

function ProjectEditor() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  /* Governance remains the server's verdict. */
  const { admin } = useAuth()
  const changeOrg = useChangeOrgAction('project')
  /* The org's "Persona" default, named on the Default persona field's empty option; an advisor default files a coder
   (`routes/tasks/create.py::_default_persona`). */
  const orgDefaultPersona = useTaskCreateDefaults().defaults.persona
  const memoryDefaultOn = resolveProjectMemoryDefault(admin?.task_create_defaults)
  const orgPersona = orgDefaultPersona === ADVISOR_PERSONA ? DEFAULT_PERSONA : orgDefaultPersona
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

  /* Only a numeric segment reaches here (a name redirects first, `ProjectNameRedirect`). */
  const numericId = id !== undefined && /^\d+$/.test(id) ? Number(id) : null

  /* The draft is seeded from each landed row, never by an effect over a query (which would paint an empty frame).
  A re-read (a tab's save invalidating `['project']`) must not discard typing. So re-seed while the draft is still
  the last seed (`hooks/useBaselineDirty`, whose `base` is that seed's JSON: one constructor, stable key order, and a
  missed field would fail toward discarding work); once the operator types, only a different `:id` re-seeds. */
  const [project, setProject] = useState<Project | undefined>(undefined)
  const [form, setForm] = useState<ProjectFormState | null>(null)
  const { base, dirty, reset } = useBaselineDirty(form && JSON.stringify(form))
  function seed(row: Project) {
    setProject(row)
    // Else their edits stand and the baseline too, so reverting by hand re-enables refresh.
    if (project?.id !== row.id || !dirty) {
      setForm(formFromProject(row))
      reset()
    }
  }
  /* A 404 or 403 falls through to the not-found state below, identical either way; any other failure is the load error. */
  const { loading, notFound, loadError } = useEditRecord(
    numericId === null ? null : String(numericId),
    key => get<Project>(`/api/projects/${key}`),
    seed,
    {
      loadFallback: 'Failed to load projects', fixedLoadError: true, notFoundStatuses: [403, 404],
      cacheKey: key => ['project', Number(key)],
      // Arriving from `/projects` paints the roster's row at once (the cache is persisted, `queryClient.ts`).
      initialData: key => queryClient.getQueryData<Project[]>(PROJECTS_KEY)?.find(p => p.id === Number(key)),
    },
  )

  /* The governance authority, asked of the server (see `governanceIsReadOnly`): `/{id}/governance-authority`,
  the PUT's door (the create wizard asks the POST's). The key carries the id so one project's verdict never
  serves another. A failure resolves to `undefined`: the seven stay locked and the rest saves. */
  const governanceQuery = useQuery({
    queryKey: ['project-governance-authority', numericId],
    queryFn: () => get<GovernanceAuthority>(`/api/projects/${numericId}/governance-authority`),
    enabled: numericId !== null,
  })
  const mayAuthorGovernance = governanceQuery.data?.may_author_governance

  /* The project's members (Run jobs as, the Environment tab's override owners), keyed by project id (the instance is
  reused across projects). Gated on a numeric id. */
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

  /* The tier catalogue (`apps/api/app/model_catalog.py`), the one list the server validates
  `default_model` against. A failed fetch still renders None and the stored value, so it round-trips. */
  const modelChoicesQuery = useTaskModels(form !== null)
  const modelChoices = modelChoicesQuery.data ?? []

  const { busy: saving, formError: saveError, setFormError: setSaveError, run } = useSubmit()
  /* The field that stopped the last submit and its message ("Required" vs "Must be a number"). Cleared on
  the next submit, not on keystrokes. */
  const [fieldError, setFieldError] = useState<{ key: StringFieldKey; message: string } | null>(null)
  // The Repository line's per-row messages (`ProjectRepoLine`), keyed by row.
  const [repoErrors, setRepoErrors] = useState<RepoRowErrors>({})
  // The tab lives in the URL (hooks/useUrlTab.ts). The row exists: the Access/Environment gate (see `tabsFor`).
  const microvmOffered = planSurfaceShown(admin, 'microvm_allowed')
  const productionQaShown = planSurfaceShown(admin, 'production_qa')
  /* Another org's project, for an operator: edited on that org's behalf. Every write below the project —
     Save and every tab's, the node ticks included — goes as its org for the one request (`lib/actAs.ts::useActAsRows`),
     the Access tab's grants too (`actAsOnce`), and `ActingForNotice` says so. Its reads that answer for the caller's
     org — the Default agent's choices, the budget, the forge lookups, Live Cast — go as its org too. Change org…
     stays the operator's own verb. The org as `headerOrg` derives it. */
  const otherOrg = useOtherOrg(project?.org ?? null)
  useActAsRows(numericId === null ? [] : [{ path: `/api/projects/${numericId}`, org: otherOrg }])
  /* Live Cast: Customize › Live Cast's body once `GET …/live-cast` answers (`lib/liveCast.ts::useProjectLiveCastShown`),
     its marker while that answer says enabled. */
  const liveCastReady = useProjectLiveCastShown(numericId, project?.live_cast_offered === true, otherOrg)
  const liveCastOn = useProjectLiveCastOn(numericId, otherOrg)
  /* Customize › MCP's marker: the same read (and key) as the section's own list, so a link or unlink moves it. */
  const mcpLinks = useApiRead(['project-mcp-links', numericId], () => listProjectMcpLinks(numericId as number),
    'Failed to load MCP links', { enabled: numericId !== null && !lockedForMember(project, admin) })
  /* The budget routes answer for the caller's org, so another org's project reads and writes its budget as that org
     (`BudgetField`'s `org`), whose plan is its own, not the viewer's: the server answers it. Off the viewer's own plan
     `BudgetField` draws nothing, so the tab goes too rather than open empty. */
  /* A custom pipeline whose org lost Custom pipelines runs as saved, read-only (`customize/CiPipelineSection.tsx`). */
  const pipelineBasic = usePipelineBasic(project?.id ?? null, project?.pipeline_mode === 'custom')
  const budgetShown = isOrgAdminSeat(admin) && (otherOrg !== null || planSwitchOn(admin, 'budgets_enabled'))
  /* The outcome scan's status (`components/projects/outcomeScan.ts`): edit mode, a repository to scan, Owner/Admin —
     the seat the budget reads, since the route is `@org_admin`. Read here too for the interval's placeholder. */
  /* Who background jobs run as: an Org Owner/Admin's or the operator's (`routes/projects.py`), edit only. */
  const runJobsAsShown = form !== null && form.mode === 'edit' && isOrgAdminSeat(admin)
  const outcomeScanShown = numericId !== null && form !== null && formHasRepo(form) && isOrgAdminSeat(admin)
  const outcomeScan = useOutcomeScan(numericId, outcomeScanShown)
  // The whole Outcome scan section is the outcome ledger's: hidden while the plan leaves Agent performance out.
  const ledgerShown = useOutcomeScanShown()
  /* A Jaah-managed project (`lib/jaahManaged.ts`): an org member's page is General, Access while the org may hold more
     than one user, and Budget while it is shown (S9: no Customize). */
  const locked = lockedForMember(project, admin)
  const [searchParams, setSearchParams] = useSearchParams()
  /* A Pipeline link (`?tab=pipeline`) followed on a plan without Custom pipelines: the tab stays for the visit and says
     why it is empty (`NotOnPlan`), never a silent General. Another org's project answers to that org's plan, not ours. */
  const [pipelineLinkedTo] = useState(() => (searchParams.get('tab') === PIPELINE_TAB.id ? numericId : null))
  const pipelineOffPlan = pipelineLinkedTo !== null && pipelineLinkedTo === numericId && project !== undefined
    && (project.has_repo ?? project.repo_url !== null) && otherOrg === null
    && !planSwitchOn(admin, 'pipeline_custom_enabled') && !pipelineTabShown(project)
  const tabs = tabsFor(
    numericId !== null,
    budgetShown,
    project?.benchmark_offered === true,
    productionQaShown,
    pipelineTabShown(project) || pipelineOffPlan,
    isOrgAdminSeat(admin),
  ).filter(tab => (locked
    ? tab === GENERAL_TAB || tab === PIPELINE_TAB || tab === BUDGET_TAB || (tab === ACCESS_TAB && admin?.users_cap !== 1)
    : true))
  /* Transfer repository: a member of a Jaah-managed project puts it on a repository of their own. Its repositories show
     emptied, the member adds theirs, and Save asks what happens to the Jaah one (`useJaahRepoQuestion`). */
  const [transferring, setTransferring] = useState(false)
  const [activeTab, selectTab] = useUrlTab(tabs, DEFAULT_TAB, 'tab', LEGACY_TABS)
  /* General's Advanced fold: open on arrival from a retired tab's link (`ADVANCED_LEGACY_TABS`) or a Node groups link
     (`#node-groups`), and held open while a refusal points into it (`showField`) until the operator closes it. */
  const [advancedLanded] = useState(() => ADVANCED_LEGACY_TABS.has(searchParams.get('tab') ?? '')
    || location.hash === `#${NODE_GROUPS_ANCHOR}`)
  const [advancedForced, setAdvancedForced] = useState(false)
  const [advancedSeen, setAdvancedSeen] = useState(advancedLanded)
  /* Branch policy's repository; with `toBranches`, Customize with Branches open — the shield's one navigation (two
     writes would race on `prev`). */
  const pickPolicyRepo = (row: RepoDraft, toBranches = false) => setSearchParams(prev => {
    const params = new URLSearchParams(prev)
    if (toBranches) {
      params.set('tab', 'customize')
      params.set(SECTION_PARAM, 'branches')
    }
    params.set(POLICY_REPO_PARAM, policyRepoParam(row))
    return params
  }, { replace: true })
  /* The Customize section holding the field the last Save refused: held open (`forceOpen`) until the next Save, so
     the focus lands on a shown field. */
  const [errorSection, setErrorSection] = useState<CustomizeSectionKey | null>(null)
  /* Bumped by General's shield: Customize remounts so the section it names opens again (`defaultOpen` is read at
     mount), even when the URL already names it. */
  const [customizeVisit, setCustomizeVisit] = useState(0)
  // Focus the input once its tab is actually shown (selecting it is a router navigation).
  const setFocusTarget = useFocusTarget(`${activeTab}:${advancedForced}`)

  /* The Repo flip's gate (`ProjectRepoLine`'s `flipLocked`): an org admin holding `projects:create` and
  `projects:delete`. */
  const can = useCan()
  const canAssign = isOrgAdminSeat(admin) && can('projects:create') && can('projects:delete')
  /* Makes or initializes a repository on Save (`POST /api/git-repos[/initialize]`, `projects:create`). Another org's
     project offers that org's owners and repositories and makes them as it (`ProjectRepoFields`'s and `prepareRepos`'s
     `org`). */
  const mayWriteRepos = can('projects:create')

  function patch(next: Partial<ProjectFormState>) {
    setForm(f => (f ? { ...f, ...next } : null))
  }

  /* One repository's Branch policy fields; its branch is the General table's too, so a fix there clears its error. */
  function patchRepo(key: string, next: Partial<RepoDraft>) {
    setForm(f => (f?.repos ? { ...f, repos: f.repos.map(r => (r.key === key ? { ...r, ...next } : r)) } : f))
    if (next.integration_branch !== undefined) setRepoErrors(e => withoutFieldErrors(e, key, 'integration_branch'))
    if (next.branch_prefix !== undefined) setRepoErrors(e => withoutFieldErrors(e, key, 'branch_prefix'))
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
    setErrorSection(FIELD_SECTIONS[field.key] ?? null)
    showField(field.key, field.tab)
    setFocusTarget(field.inputId)
  }

  /* Shows a field's tab, and opens General's Advanced when the field is folded there. */
  function showField(key: string, tab: ProjectTab) {
    selectTab(tab)
    if (ADVANCED_FIELDS.has(key)) {
      setAdvancedForced(true)
      setAdvancedSeen(true)
    }
  }

  /* Discard: the draft back to its baseline (the last seed), and with it every note the edits raised. */
  function discard() {
    if (base !== null) setForm(JSON.parse(base) as ProjectFormState)
    setTransferring(false)
    setRepoErrors({})
    setFieldError(null)
    setErrorSection(null)
    setSaveError('')
  }

  /* Asked when a Save replaces the Jaah-made repository; a `move` answer's task is announced to the shell's toast. */
  const [askJaahRepo, jaahRepoDialog] = useJaahRepoQuestion()

  /* The form's Save, and the leave dialog's: true once the write landed; an unchanged draft writes nothing. */
  async function save(): Promise<boolean> {
    if (!form) return false
    // Save is disabled while clean: an unchanged draft has nothing to write.
    if (!dirty) return false
    setErrorSection(null)

    const governanceReadOnlyNow = governanceIsReadOnly(form, mayAuthorGovernance)
    /* Its own message, since the draft is empty but the field looks filled. A hidden field is neither checked nor
    sent (`isHidden`). */
    const badNumber = NUMBER_FIELDS.find(
      f => !isHidden(form, f.key) && (document.getElementById(f.inputId) as HTMLInputElement | null)?.validity.badInput,
    )
    if (badNumber) {
      reject(badNumber, `${badNumber.label} must be a number.`, 'Must be a number')
      return false
    }
    /* Every repository needs a URL and a branch; the refusal is on the row, the banner says where to look. None is a
    project with no repository (`formHasRepo`): removing the last one sends `repos: []`. */
    const sentRepos = form.repos ?? []
    const blankRows = blankRepoFields(sentRepos)
    if (Object.keys(blankRows).length) {
      setRepoErrors(blankRows)
      setSaveError(FIX_MARKED_REPOS)
      selectTab('general')
      return false
    }
    setFieldError(null)
    setRepoErrors({})
    let rowsRefused = false
    let repoFailure = ''
    /* `repos` replaces the whole stored set, so it goes only when this draft changed it: saving another field from a
    draft older than someone else's added repository must not delete that repository. */
    const unchangedReposLeftOut = (draft: ProjectFormState, body: Record<string, unknown>) => {
      const pristine = base === null ? null : (JSON.parse(base) as ProjectFormState)
      if (pristine && JSON.stringify(draft.repos) === JSON.stringify(pristine.repos)) delete body.repos
      // Naming it at all, even unchanged, is an Org Owner/Admin's: anyone else's Save would be refused.
      if (!runJobsAsShown || (pristine && draft.run_jobs_as_id === pristine.run_jobs_as_id)) delete body.run_jobs_as_id
      return body
    }
    /* Settings › Pipeline writes the project's integration branch and prefix outside this form: a draft older than its
    save must not send them back. On a custom pipeline the prefix reads only here, so it goes as stored (a basic
    pipeline's is typed on Customize › Branches and goes as typed); the primary's branch (still editable on General)
    goes as stored unless this draft changed it. A basic pipeline's full gate goes only when this draft typed it: a
    resent stale value would overwrite another writer's (the Repo Wizard's), and a custom pipeline's never goes (it
    would collapse the Merge step's gate list) — both judged from the live project, never the draft's seed. */
    const pipelineFieldsAsStored = (draft: ProjectFormState, body: Record<string, unknown>) => {
      if (!project || !(project.has_repo ?? project.repo_url !== null)) return body
      const pristine = base === null ? null : (JSON.parse(base) as ProjectFormState)
      const primaryOf = (f: ProjectFormState | null) => f?.repos?.find(r => r.is_primary)
      const branchTouched = primaryOf(draft)?.integration_branch !== primaryOf(pristine)?.integration_branch
      const basic = basicPipeline(project)
      const prefixAsStored = !basic
      const fullGate = draft.gate_full_cmd.trim()
      // `gate_quick_cmd` rides only with the governance fragment: a viewer who may not author governance sends none.
      if (basic && 'gate_quick_cmd' in body && pristine !== null && fullGate !== pristine.gate_full_cmd.trim()) {
        body.gate_full_cmd = fullGate
      }
      if (prefixAsStored && 'branch_prefix' in body) body.branch_prefix = project.branch_prefix
      if (Array.isArray(body.repos)) {
        body.repos = (body.repos as Record<string, unknown>[]).map(row => (row.is_primary && row.id !== undefined
          ? {
            ...row,
            ...(prefixAsStored && 'branch_prefix' in row ? { branch_prefix: project.branch_prefix } : {}),
            ...(branchTouched ? {} : { integration_branch: project.integration_branch }),
          }
          : row))
      }
      return body
    }
    const landed = await run('Failed to save project', async () => {
      /* Each "Create a new repository" row is made first, and a made one left empty given main and develop
      (`projectForm.ts::prepareRepos`); a made row holds its URL from then on, so a retried Save never makes it twice. */
      let rows = form.repos
      try {
        if (rows && mayWriteRepos) rows = await prepareRepos(rows, project?.name ?? form.name, false, next => patch({ repos: next }), otherOrg)
      } catch (err) {
        if (err instanceof NewRepoError) setRepoErrors({ [err.key]: { row: err.message } })
        repoFailure = err instanceof NewRepoError ? FIX_MARKED_REPOS : apiDetail(err, 'Failed to initialize the repository')
        selectTab('general')
        throw err
      }
      const draft = { ...form, repos: rows }
      const url = `/api/projects/${form.id}`
      /* The name is the trail's, saved on its own (`rename`): a draft seeded before a rename must not send it back. */
      const { name: _renamedInTrail, ...full } = pipelineFieldsAsStored(draft, unchangedReposLeftOut(draft, buildBody(draft, !governanceReadOnlyNow)))
      const body = locked ? memberBody(full, transferring) : full
      /* A Save that replaces or removes the Jaah-made repository is refused 409 until it says what happens to it
      (`components/projects/jaahRepoReplaced.tsx`): ask, then send the same body again with the answer. Cancel writes
      nothing and says nothing. */
      let moved = false
      const sent = put<Project>(url, body).catch(async (err: unknown) => {
        const replaced = jaahRepoReplaced(err)
        if (!replaced) throw err
        const answer = await askJaahRepo(replaced)
        if (!answer) return SAVE_CANCELLED
        moved = answer.choice === 'move'
        return put<Project>(url, {
          ...body, jaah_repo: answer.choice, ...(answer.confirm === null ? {} : { jaah_repo_confirm: answer.confirm }),
        })
      })
      return sent
        .then(saved => {
          if (saved === SAVE_CANCELLED) return false
          if (moved && typeof saved?.move_task_id === 'number') announceMoveTask(saved.move_task_id)
          /* Invalidate, never await a refetch: a failed roster GET must not report a successful save as failed.
          `worktrees` too. Invalidation skips disabled queries, so no 403
          without `worktrees:view` (`components/WorktreePool.tsx` records that trap). */
          void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
          void queryClient.invalidateQueries({ queryKey: ['project'] })
          void queryClient.invalidateQueries({ queryKey: WORKTREES_KEY })
          /* A repository just added: whether each node's clone owner can read it (`components/GitReach.tsx`). */
          void queryClient.invalidateQueries({ queryKey: ['project-git-reach', form.id] })
          return true
        })
        .catch(err => {
          // The banner takes `apiDetail`'s sentence (`run`); the field it names is shown and focused here.
          // A per-row refusal lands on the rows it names (`repoRowErrors`).
          const rowErrors = repoRowErrors(err, rows ?? [])
          if (rowErrors) {
            setRepoErrors(rowErrors)
            /* A prefix refusal reads on Branch policy, so it opens there on that repository; any other, General. */
            const prefixRow = (rows ?? []).find(r => rowErrors[r.key]?.branch_prefix)
            if (prefixRow) {
              pickPolicyRepo(prefixRow, true)
              setErrorSection('branches')
              setFocusTarget('project-branch-prefix')
            } else selectTab('general')
            rowsRefused = true
          }
          const loc = refusedBodyLoc(err)
          const field = loc?.[1]
          // A hidden field cannot be shown or focused; the banner alone carries the server's words.
          const target = typeof field === 'string' && !isHidden(form, field) ? FIELD_TABS[field] : undefined
          if (loc && target) {
            setErrorSection(FIELD_SECTIONS[field as string] ?? null)
            showField(field as string, target.tab)
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
    return landed?.value ?? false
  }

  /* Leaving with unsaved edits asks first (`hooks/useLeaveGuard`); a move between tabs stays on the page. */
  const guard = useLeaveGuard(dirty, save)

  async function handleSubmit() {
    if (await save()) {
      guard.leaveNow('/projects')
    }
  }

  /* THE NAME IS RENAMED IN THE TRAIL and saved at once (`PUT {name}`, partial): Enter or blur saves, Escape reverts,
  a blank is refused. Never through Save, which no longer sends `name`. The new name shows while the write is in flight;
  a refusal puts the old one back and says why in the banner. */
  const [renaming, setRenaming] = useState<string | null>(null)
  // The banner message the last rename raised, so the next one clears only its own and never a Save's.
  const renameErrorRef = useRef<string | null>(null)
  function renameError(message: string) {
    renameErrorRef.current = message
    setSaveError(message)
  }
  async function rename(next: string) {
    const name = next.trim()
    if (numericId === null || !project || name === project.name) return
    setRenaming(name)
    if (renameErrorRef.current !== null && saveError === renameErrorRef.current) setSaveError('')
    renameErrorRef.current = null
    try {
      /* The row alone, never the draft: typing that began while the write was in flight (blur commits, so "click the
      next field and type" is the usual way out of the crumb) must survive. Save does not send `name`, and the re-read
      the invalidation starts re-seeds a still-clean draft as any re-read does. */
      setProject(await put<Project>(`/api/projects/${numericId}`, { name }))
      void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
      void queryClient.invalidateQueries({ queryKey: ['project'] })
    } catch (err) {
      renameError(apiDetail(err, 'Failed to rename project'))
    } finally {
      setRenaming(null)
    }
  }

  /* Server truth, not the draft; the seeded row outlives one that left the roster. */
  const heading = renaming ?? project?.name ?? 'Project'
  const crumbs = <Breadcrumb items={[{ label: 'Projects', to: '/projects' }, { label: heading }]} />
  /* The loaded page's trail: its last crumb is the page's `<h1>` (no title row) and the name's editor. */
  const nameEditable = numericId !== null && project !== undefined && renaming === null && !saving
    && canEditProject('projects:edit', numericId)
  const trail = (
    <Breadcrumb
      oneLine
      currentComponent="h1"
      items={[
        { label: 'Projects', to: '/projects' },
        {
          text: heading,
          label: (
            <InlineTitleEdit
              value={project?.name ?? ''}
              shown={heading}
              onCommit={next => { void rename(next) }}
              editable={nameEditable}
              blank="refuse"
              onBlankRefused={() => renameError('Name is required.')}
              tip={heading}
              inputLabel="Project name"
              editLabel={`Rename project: ${heading}`}
              testId="project-crumb-name"
            />
          ),
        },
      ]}
    />
  )
  /* The record's tenant beside its name (`titleAside`; see `ui/PageHeader`); null is a legitimate org (`types.ts::OrgRef`). */
  const headerOrg = project?.org ?? null
  /* The transient states are reachable only while there is no draft: a failed re-read must never discard a
  half-filled form. `!form` makes that structural. */
  if (!form) {
    if (loading || (loadError && !notFound)) {
      return <DetailStatus trail={crumbs} loading={loading} error={loadError} />
    }
    return <DetailStatus trail={crumbs} notFound="Project not found" locate={{ objectType: 'project', objectId: id }} />
  }

  const governanceReadOnly = governanceIsReadOnly(form, mayAuthorGovernance)
  const liveUrls = project?.live_urls ?? []
  /* The verdict arrived and was no — narrower than `governanceReadOnly` (also true while pending). Only a
  rule can be explained, so this draws the notice and the divider follows it. */
  const governanceWithheld = mayAuthorGovernance === false
  // Short message on the control, field name in the banner.
  const errorFor = (key: StringFieldKey) => (fieldError !== null && fieldError.key === key ? fieldError.message : undefined)
  const textCell = projectTextCell(form, patch, errorFor)
  /* The Customize tab's sections (`components/projects/customize/sections.tsx`): Branches and Testing are drawn here,
     bound to the draft, and their markers follow it (AC-C2). */
  const openSection = sectionFromUrl(searchParams)
  const sections = project === undefined ? [] : customizeSections({
    project,
    form,
    canEdit: canEditProject('projects:edit', project.id),
    otherOrg,
    textCell,
    patch,
    branches: {
      active: branchesActive(form),
      render: () => (
        <BranchesSection
          form={form} project={project} readOnly={governanceReadOnly} repoErrors={repoErrors} errorFor={errorFor}
          patch={patch} patchRepo={patchRepo} onPickRepo={row => pickPolicyRepo(row)}
        />
      ),
    },
    testing: {
      active: testingActive(form),
      render: () => (
        <TestingSection
          form={form} project={project} readOnly={governanceReadOnly} textCell={textCell} ledgerShown={ledgerShown}
          scanStatusShown={outcomeScanShown} scanPlaceholder={inheritedInterval(outcomeScan.data)}
        />
      ),
    },
    mcpLinked: (mcpLinks.data ?? []).length > 0,
    microvmOffered,
    liveCast: { shown: isOrgAdminSeat(admin), ready: liveCastReady, on: liveCastOn },
  })
  // The operator's "Change org…" (`components/ChangeOrgDialog.tsx`) — an existing project only.
  const changeOrgButton = numericId === null || !project
    ? null
    : changeOrg.button({ id: numericId, name: heading, org: headerOrg, href: `/projects/${numericId}` })

  return (
    <DetailPage
      breadcrumb={trail}
      /* The record's org beside the trail, the chip alone (an "Org" caption would repeat what the chip says); the
      tenant is on the chip's `title`. The create wizard labels the viewer's own org "Your org" under the same chip id. */
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

      <FormBox
        onSubmit={handleSubmit}
        /* Native validation is off (`FormBox`): it cannot see into hidden panels. */
        data-testid="project-form"
        sx={{ display: 'flex', flexDirection: 'column' }}
      >
        {/* Above the tab strip: it is about the submit and must be readable from any panel. */}
        {saveError && <Banner severity="error">{saveError}</Banner>}
        {placementWarning && <Banner severity="warning">{placementWarning}</Banner>}
        {numericId !== null && project?.has_repo && !locked && <GitReachBanner projectId={numericId} />}

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
          <Box sx={{ pt: 1.5, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            {project && HostedAppStatus && (
              <HostedAppStatus
                projectId={project.id} canEdit={canEditProject('projects:edit', project.id)} otherOrg={otherOrg}
              />
            )}
            {/* Each pipeline stage with an address, in pipeline order (`ProjectOut.live_urls`, the stored stages: never
            the pipeline route, whose read marks stages for probing). Absent with none. */}
            {liveUrls.length > 0 && (
              <Box component="p" data-testid="project-live-urls"
                sx={{ m: 0, display: 'flex', flexWrap: 'wrap', columnGap: 1, rowGap: 0.5, fontSize: FONT_SIZE.base, minWidth: 0 }}>
                {liveUrls.map(({ stage, url }, i) => (
                  <Box component="span" key={`${stage}-${i}`} data-testid="project-live-url" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                    <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold, whiteSpace: 'nowrap' }}>{stage}:</Box>{' '}
                    {safeHref(url) ? <ExternalLink href={safeHref(url)!} underline="hover">{url}</ExternalLink> : url}
                    {i < liveUrls.length - 1 && <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary', ml: 1 }}>·</Box>}
                  </Box>
                ))}
              </Box>
            )}
            {project && HostedAppDomains && <HostedAppDomains projectId={project.id} otherOrg={otherOrg} />}

            {/* Its rows decide which fields the rest of the form shows (`formHasRepo`). A Jaah-managed project's
            repository reads "at Jaah", its names closed to a member; Transfer starts today's transfer: the member's own
            repository replaces it, and Save asks what happens to the Jaah one (`useJaahRepoQuestion`). */}
            {form.repos !== null && (
              <Box>
                <ProjectRepoLine
                  repos={form.repos}
                  onChange={repos => patch({ repos })}
                  errors={repoErrors}
                  onErrorsChange={setRepoErrors}
                  primaryBranchLocked={governanceReadOnly}
                  storedBranch={project?.integration_branch ?? ''}
                  /* Both forge writes are `projects:create` (`routes/git_connections.py`): a maintainer without it
                  connects existing repositories only. */
                  initEmpty={mayWriteRepos}
                  canCreate={mayWriteRepos}
                  org={otherOrg}
                  flipLocked={!canAssign}
                  /* Transferring, the empty list is the start of a new one, never "Save leaves this project without a repository". */
                  storedHasRepo={project && !transferring ? (project.has_repo ?? project.repo_url !== null) : false}
                  onBranchPolicy={locked ? undefined : row => {
                    setCustomizeVisit(n => n + 1)
                    pickPolicyRepo(row, true)
                  }}
                  projectPolicy={{ branch_prefix: form.branch_prefix, push_enabled: form.push_enabled }}
                  readOnly={locked && !transferring}
                  atJaah={project?.jaah_managed === true && !transferring}
                  onTransfer={locked && !transferring && canEditProject('projects:edit', project?.id ?? -1)
                    ? () => setTransferring(true)
                    : undefined}
                />
                {/* Back out: the stored repositories again. */}
                {locked && transferring && (
                  <Button
                    variant="text"
                    size="small"
                    data-testid="project-transfer-cancel"
                    sx={{ ...BUTTON_SX, mt: 0.5 }}
                    onClick={() => {
                      setTransferring(false)
                      patch({ repos: base === null ? form.repos : (JSON.parse(base) as ProjectFormState).repos })
                      setRepoErrors({})
                    }}
                  >
                    Cancel transfer
                  </Button>
                )}
              </Box>
            )}

            <FieldRow>
              {/* What a new task here starts as (`withProjectPersona`); `''` = the org's Settings default. Only the
              personas the server serves as `project_default` (`PROJECT_DEFAULT_PERSONAS`). */}
              <DetailCell basis={grid(32)} max={grid(48)}>
                <FormTextField
                  id="project-default-persona"
                  label="Default persona"
                  help={{
                    tip: 'New tasks here start as this persona.',
                    more: 'Also applies to tasks agents file without naming one, and to epic subtasks the planner leaves '
                      + 'unassigned. Org default uses the Persona in Settings › Tasks › Defaults. Any task can still change it.',
                    docs: PROJECT_FIELDS_DOCS,
                  }}
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

            {/* Not tier-authored: no `readOnly`/`disabled`, since any `projects:edit` holder may write it (a Jaah-managed
            project's member included) and `buildBody` keeps it out of `governanceBody`. On General, not Governance, so
            a project with no repository, or one Jaah manages, still reaches it.
            The heading is the label: the input drops its own label, pointing at the heading via `aria-labelledby`
            (one string, no drift; `getByLabelText` still resolves). No `FormField`: no label to own, no rule to fail;
            `fullWidth`/`size="small"` spelled here.
            Three rows; `TEXTAREA_RESIZE_SX` gives the drag handle. */}
            <DetailSection title="Agent instructions" titleId="project-agent-instructions-label" help={AGENT_INSTRUCTIONS_HELP}>
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
                  slotProps={{ htmlInput: { 'aria-labelledby': 'project-agent-instructions-label', 'aria-description': AGENT_INSTRUCTIONS_HELP.tip } }}
                  sx={[fieldTapTargetSx, TEXTAREA_RESIZE_SX, { maxWidth: grid(117) }]}
                />

              </Box>
            </DetailSection>

            {/* Everything a first pass does not need, folded: collapsed on every visit (a retired tab's link opens it,
            `LEGACY_TABS`), compact one-line controls whose explanations are hovers. A refusal inside opens it
            (`showField`; an invalid field holds it open, `ui/AdvancedSection`). Node groups are read once it opens; a `#node-groups` link opens it. */}
            <AdvancedSection
              testId="project-advanced"
              defaultOpen={advancedLanded}
              forceOpen={advancedForced}
              onOpenChange={open => {
                if (open) setAdvancedSeen(true)
                else setAdvancedForced(false)
              }}
            >
              <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
                {/* The project's default agent: what an unpicked task runs. The task form's own picker
                (`components/agents/AgentPicker.tsx`), with no persona: a project spans them. */}
                <FieldRow rowGap={2}>
                  <DetailCell basis={grid(48)} max={grid(80)}>
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
                  {!locked && <DetailCell basis={grid(24)} max={grid(42.5)}>
                    <ModelTierField
                      value={form.default_model}
                      choices={modelChoices}
                      onChange={v => patch({ default_model: v })}
                    />
                  </DetailCell>}
                  {/* Project memory (`projects.project_memory`): the only layer above the org's default; tasks carry no
                  override (`app/project_memory/switch.py`). Shown while the org's plan carries memory. */}
                  {numericId !== null && !locked && planSwitchOn(admin, 'project_memory_enabled') && (
                    <DetailCell basis={grid(28)} max={grid(40)}>
                      <FormTextField
                        id="project-memory"
                        label="Project memory"
                        help={{
                          tip: "Lets this project's runners draw on what earlier tasks recorded.",
                          more: 'Default follows System Settings, and its label shows which.',
                          docs: PROJECT_FIELDS_DOCS,
                        }}
                        value={form.project_memory}
                        onChange={v => patch({ project_memory: v as ProjectMemoryChoice })}
                        options={[{ value: 'on', label: 'On' }, { value: 'off', label: 'Off' }]}
                      >
                        <option value="">{`Default (${memoryDefaultOn ? 'On' : 'Off'})`}</option>
                      </FormTextField>
                    </DetailCell>
                  )}
                </FieldRow>

                {/* Intelligent routing (`components/projects/ProjectRoutingFields.tsx`): Jaah's to set on a project it manages. */}
                {!locked && (
                  <ProjectRoutingFields bare form={form} project={project} patch={patch} errorFor={k => errorFor(k as StringFieldKey)} />
                )}

                {runJobsAsShown && !locked && (
                  <Box>
                    <FieldRow rowGap={2}>
                      <DetailCell basis={grid(42.5)} max={grid(64)}>
                        <MemberSelectField
                          id="project-run-jobs-as"
                          label="Run jobs as"
                          help={{
                            tip: "Whose Git access the project's background jobs use.",
                            more: "The outcome scan, the merge lane's own tasks and benchmark reviews. Org default uses the "
                              + "organization's Background jobs choice, else its first owner or admin. An automation's own Run as comes first.",
                            docs: PROJECT_FIELDS_DOCS,
                          }}
                          value={form.run_jobs_as_id}
                          members={members}
                          listState={memberListState}
                          listNarrowed={memberListNarrowed}
                          onChange={v => patch({ run_jobs_as_id: v })}
                          emptyLabel="Org default"
                          currentName={form.run_jobs_as_id === project?.run_jobs_as_id ? project?.run_jobs_as_name : null}
                        />
                        {/* The saved choice's outcome: who the jobs run as (Org default names nobody) and why not the pick. */}
                        {form.run_jobs_as_id === (project?.run_jobs_as_id ?? '') && project?.jobs_run_as_name && (
                          <Typography variant="caption" component="p" sx={{ mt: 0.5, color: 'text.secondary' }}>
                            Runs as {project.jobs_run_as_name}
                          </Typography>
                        )}
                      </DetailCell>
                    </FieldRow>
                    {form.run_jobs_as_id === (project?.run_jobs_as_id ?? '') && project?.jobs_run_as_warning && (
                      <Banner severity="warning">{project.jobs_run_as_warning}</Banner>
                    )}
                  </Box>
                )}

                {/* Which node groups run this project, each change applied at once (no Save). Mounted with its read, once
                    Advanced first opens: before that it would be a spinner nothing ever answers. */}
                {numericId !== null && !locked && advancedSeen && <ProjectNodeGroupsField projectId={numericId} projectName={project?.name} />}

                {numericId !== null && (
                  <AdvancedGroup label="Issue trackers" help={TRACKERS_HELP} testId="project-trackers-group">
                    <ProjectTrackersSection projectId={numericId} bare />
                  </AdvancedGroup>
                )}

                {!locked && (
                  <>
                    <AdvancedGroup label="Tech stack" help={{ tip: "Runners read it as the project's stack.", docs: PROJECT_FIELDS_DOCS }} testId="project-tech-stack">
                      {form.tech_stack.map((row, idx) => (
                        <Stack
                          key={idx}
                          direction="row"
                          spacing={0.5}
                          useFlexGap
                          sx={{ alignItems: 'center', flexWrap: 'wrap', width: '100%', maxWidth: grid(90.5), mb: 0.5 }}
                        >
                          <TextField
                            id={`project-tech-stack-${idx}-layer`}
                            placeholder="layer (e.g. frontend)"
                            value={row.layer}
                            onChange={e => updateTechStackRow(idx, 'layer', e.target.value)}
                            slotProps={{ htmlInput: { 'aria-label': `Tech stack layer ${idx + 1}` } }}
                            size="small"
                            sx={[fieldTapTargetSx, { flex: `1 1 ${grid(21)}` }]}
                          />
                          <TextField
                            id={`project-tech-stack-${idx}-technology`}
                            placeholder="technology (e.g. React)"
                            value={row.technology}
                            onChange={e => updateTechStackRow(idx, 'technology', e.target.value)}
                            slotProps={{ htmlInput: { 'aria-label': `Tech stack technology ${idx + 1}` } }}
                            size="small"
                            sx={[fieldTapTargetSx, { flex: `1 1 ${grid(21)}` }]}
                          />
                          <IconButton
                            aria-label={`Remove tech stack row ${idx + 1}`}
                            onClick={() => setForm(f => f ? { ...f, tech_stack: f.tech_stack.filter((_, i) => i !== idx) } : null)}
                          >
                            <Box component="span" aria-hidden="true" sx={{ fontSize: FONT_SIZE.lg, lineHeight: LINE_HEIGHT.tight }}>×</Box>
                          </IconButton>
                        </Stack>
                      ))}
                      <Button
                        type="button"
                        variant="text"
                        size="small"
                        onClick={() => setForm(f => f ? { ...f, tech_stack: [...f.tech_stack, { layer: '', technology: '' }] } : null)}
                        sx={[tapTargetSx, { ml: -1 }]}
                      >
                        Add
                      </Button>
                    </AdvancedGroup>

                    <FormTextField
                      id="project-notes"
                      label="Notes"
                      help="Free text runners can read."
                      multiline
                      rows={2}
                      value={form.notes}
                      onChange={v => patch({ notes: v })}
                      sx={[TEXTAREA_RESIZE_SX, { maxWidth: grid(117) }]}
                    />
                  </>
                )}

                {/* An Easy Mode project's hosted app: this month's usage against its terms (nothing without an app). */}
                {project && HostedAppUsage && <HostedAppUsage projectId={project.id} />}
              </Box>
            </AdvancedSection>
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

        {/* The page's action bar: one form, one submit, reachable from every panel but Access (outside the form, below;
        it saves on its own), Customize (its own copy of this bar, under the sections it saves, drawn once something changed) and Budget (its own Save, outside the form). The style guide's edit-in-place bar: Save and
        Discard both stand down until the draft moves off its baseline, and a hand-reverted edit stands them down again. */}
        {!NO_FORM_ACTIONS.has(activeTab) && (
          <CancelSaveBar
            testId="project-form-actions"
            saveTestId="project-form-submit"
            discardTestId="project-form-discard"
            onDiscard={discard}
            dirty={dirty}
            /* A rename in flight holds Save: Save's answer, seeded after the rename's, would show the old name. */
            busy={saving || renaming !== null}
            // '' not undefined: the reason's wrapper stays mounted, so Save is one element (focus kept) as it enables.
            saveReason={dirty ? '' : NO_CHANGES_HINT}
            // `BUTTON_SX` keeps a disabled Save hit-testable for the reachability probes.
            saveProps={{ sx: BUTTON_SX }}
          />
        )}
      </FormBox>

      {/* Outside the project `<form>`: its self-saving sections hold forms of their own (MicroVM's editors), and the
      form-saved ones sit in a form of the tab's own that runs the same Save (`CustomizeTab`). A section a
      link names opens on mount; `customizeVisit` remounts it for the General shield's jump. */}
      {project && (
        <TabPanel id="customize" current={activeTab} idPrefix="project">
          <CustomizeTab
            key={customizeVisit}
            sections={sections}
            open={activeTab === 'customize'}
            openKey={openSection}
            errorKey={errorSection}
            formId={CUSTOMIZE_FORM_ID}
            onSubmit={handleSubmit}
            notice={governanceWithheld && formHasRepo(form) ? <GovernanceReadOnlyNotice /> : undefined}
            /* Only once the draft moved: the tab's self-saving sections draw their own Saves, and a clean disabled
               pair under the form cost a phone a full-width row beside theirs. */
            actions={(dirty || saving) && (
              <CancelSaveBar
                testId="project-form-actions"
                saveTestId="project-form-submit"
                discardTestId="project-form-discard"
                onDiscard={discard}
                dirty={dirty}
                busy={saving}
                saveReason={dirty ? '' : NO_CHANGES_HINT}
                saveProps={{ sx: BUTTON_SX, form: CUSTOMIZE_FORM_ID }}
              />
            )}
          />
        </TabPanel>
      )}
      {pipelineOffPlan && (
        <TabPanel id="pipeline" current={activeTab} idPrefix="project">
          {activeTab === 'pipeline' && <NotOnPlan name="pipeline_custom_enabled" />}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own Save, to the pipeline route. */}
      {!pipelineOffPlan && project && (project.has_repo ?? project.repo_url !== null) && (
        <TabPanel id="pipeline" current={activeTab} idPrefix="project">
          {activeTab === 'pipeline' && (
            <Box sx={{ pt: 1.5 }}>
              <PipelineSettings projectId={project.id}
                canEdit={canEditProject('projects:edit', project.id) && pipelineBasic.data?.read_only === false}
                mergeLane={isEnabled('merge_lane') && project.merge_lane === true}
                project={project} />
            </Box>
          )}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its rows open in the automation editor, which Closes back here. */}
      {numericId !== null && isOrgAdminSeat(admin) && (
        <TabPanel id="automations" current={activeTab} idPrefix="project">
          {activeTab === 'automations' && (
            <Box sx={{ pt: 1.5 }}>
              <AutomationList scope={{ kind: 'projects', id: numericId, orgId: project?.org?.id }} />
            </Box>
          )}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own draft and Save, a partial PUT of its own fields. */}
      {project && productionQaShown && (
        <TabPanel id="production-qa" current={activeTab} idPrefix="project">
          {activeTab === 'production-qa' && (
            <Box sx={{ pt: 1.5 }}>
              <ProjectProductionQaTab project={project} canEdit={canEditProject('projects:edit', project.id)} />
            </Box>
          )}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own draft and Save, a partial PUT of its two fields. */}
      {project?.benchmark_offered === true && (
        <TabPanel id="benchmark" current={activeTab} idPrefix="project">
          {activeTab === 'benchmark' && (
            <Box sx={{ pt: 1.5 }}>
              <ProjectBenchmarkTab project={project} canEdit={canEditProject('projects:edit', project.id)} />
            </Box>
          )}
        </TabPanel>
      )}
      {/* Outside the `<form>`: its own Save, to its own route — never the project's PUT. */}
      {numericId !== null && budgetShown && (
        <TabPanel id="budget" current={activeTab} idPrefix="project">
          {activeTab === 'budget' && (
            <Box sx={{ pt: 1.5, maxWidth: grid(80) }}>
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
            {activeTab === 'access' && <AccessTab scope={{ type: 'project', id: String(numericId) }} otherOrg={otherOrg} />}
          </Box>
        </TabPanel>
      )}
      {changeOrg.dialog}
      {jaahRepoDialog}
      <LeaveGuardDialog leave={guard} message="This project has changes that have not been saved. Save them before leaving?" />
    </DetailPage>
  )
}
