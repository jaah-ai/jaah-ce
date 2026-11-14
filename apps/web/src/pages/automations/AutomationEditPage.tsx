import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { currentOrgId, useAuth, type Admin } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { usePersonaRegistry } from '../../components/tasks/usePersonaRegistry'
import { useApiRead } from '../../hooks/useApiRead'
import { useBaselineDirty } from '../../hooks/useBaselineDirty'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useSubmit } from '../../hooks/useSubmit'
import { useNestedCreate } from '../../hooks/useNestedCreate'
import { useSettled } from '../../hooks/useSettled'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useUrlTab } from '../../hooks/useUrlTab'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { EditStatus } from '../../ui/detail/DetailStatus'
import FormActionBar from '../../ui/FormActionBar'
import FormBox from '../../ui/FormBox'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { OPERATOR_PLANE_SLUG, useOrgsByIds } from '../../ui/OrgSelect'
import { StatusChip } from '../../ui/StatusChip'
import SubmitButton from '../../ui/SubmitButton'
import SecretOnceDialog from '../../ui/SecretOnceDialog'
import { isValidTarget, type TargetKind, type TargetPreview, type TargetRule } from '../../ui/TargetSelector'
import { safeReturnPath } from '../../lib/listDetail'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { listActivityTypes, type ActivityType } from '../cron/api'
import { NEW_ADDRESS_URL, newAddressUrl, ORG_EMAIL_KEY } from '../settings/email/api'
import { scheduleFields, SCHEDULE_KIND_CRON, type ScheduleDraft } from '../cron/schedule'
import { DEFAULT_CREATE_RETENTION_MODE } from '../cron/vocabulary'
import DedupEntries from '../triggers/DedupEntries'
import StateDocumentEditor from '../triggers/StateDocumentEditor'
import './actions'
import { actionEntryFor, type ActionContext } from './actions/registry'
import {
  createAutomation,
  deleteAutomation,
  deleteAutomationAttachment,
  getAutomation,
  listActionTypes,
  pauseAutomation,
  previewTarget,
  releaseAutomation,
  takeAutomationOwnership,
  resumeAutomation,
  runAutomation,
  RUNS_REREAD_MS,
  updateAutomation,
  uploadAutomationAttachment,
  type ActionType,
  type Automation,
  type AutomationInput,
  type AutomationRun,
} from './api'
import { deleteAutomationConfirm, releaseAutomationConfirm, runNowBlocked, STUCK_PAUSED_HINT } from './AutomationList'
import AutomationCreateWizard, { type AutomationStepId, type StepBlockers, type WizardPosition } from './AutomationCreateWizard'
import AutomationRuns from './AutomationRuns'
import { isEnabled } from '../../lib/editionFeatures'
import { planSurfaceShown } from '../../lib/planSwitches'
import {
  AboutSection, ActionSection, ActionTypePicker, OrgField, SettingsSection, TargetSection, WhenSection, type Draft,
} from './AutomationSections'
import {
  ADDRESS_PICK,
  EVENT_TEMPLATE_HINT,
  EventSetupSection,
  EventTestPanel,
  eventDraftOf,
  eventWhenOf,
  newEventDraft,
  NewAddressContext,
  recordsOnly,
  readableError,
  withAddress,
} from './EventSourceFields'
import {
  PRODUCTION_QA_SOURCE_KINDS,
  STATUS,
  WHEN_CHOICE_LABELS,
  WHEN_CHOICES,
  whenChoiceOf,
  type WhenChoice,
} from './vocabulary'
import { FONT_WEIGHT, grid } from '../../theme'
import Tip from '../../ui/Tip'

/* One automation — `/automations/new` (`mode="create"`, optional `?when=<choice>`) and
 * `/automations/:id`, one component. ONE EDITOR FOR EVERY KIND, in the order a person thinks it:
 * WHEN (a schedule or an event source) -> TARGET (`ui/TargetSelector`, a live rule) -> ACTION (the action registry's
 * form: the Task action is the console's own task-create form, D7). The parts themselves are
 * `AutomationSections.tsx`; this page owns the draft, its validation and the one save.
 *
 * ESSENTIALS FIRST, EVERYTHING ELSE ON SETTINGS. A NEW AUTOMATION IS A THREE-STEP WIZARD (`AutomationCreateWizard.tsx`):
 * When, What (action, target, its essentials), Name — the name suggested from what was picked. Every other setting
 * keeps its default.
 *
 * A SAVED AUTOMATION IS TABBED, first-hand work first (`hooks/useUrlTab.ts`, `?tab=`): Setup (an event's
 * endpoint, then the essentials), Settings (everything else: `SettingsSection`), Runs, and — for an event only —
 * Advanced (Test, Dedup entries, State document). Every panel stays mounted, so an unsaved edit survives a trip to
 * Runs; Runs and Advanced mount their content the first time they are shown, so Setup paints as fast as the untabbed
 * page did. Save is the PAGE's bar, one form over Setup and Settings.
 *
 * EVERY PART'S DRAFT IS KEPT across a When change (AC4.2): the schedule draft, the event draft, the target and the
 * action value each live in their own state, and `inputOf` reads only the half the current When needs — so
 * Schedule -> Webhook -> Schedule loses nothing, and the Task body survives a schedule <-> event switch (only the
 * event-only Title template is dropped, by the Task entry's `toParams`).
 *
 * A save sends only the parts whose wire value moved (`automation_service.update` patches sent keys), so an
 * untouched schedule is not re-validated. Nothing is computed from a schedule here (`pages/cron/api.ts`). */

/** The schedule a new automation opens with when the catalogue names none: every day at 09:00. */
const FALLBACK_SCHEDULE: ScheduleDraft = { kind: SCHEDULE_KIND_CRON, expression: '0 9 * * *', intervalSeconds: null }
const PREVIEW_DEBOUNCE_MS = 300

const AUTOMATION_FORM_ID = 'automation-form'

type AutomationTab = 'setup' | 'settings' | 'runs' | 'advanced'
const SETUP_TAB: TabDef<AutomationTab> = { id: 'setup', label: 'Setup' }
const SETTINGS_TAB: TabDef<AutomationTab> = { id: 'settings', label: 'Settings' }
/** The action params the Settings tab draws (`ActionEntry.Extras`): a refusal of one opens that tab. */
const SETTINGS_PARAMS = new Set([
  'start', 'assignee_id', 'attachment_ids', 'persona', 'bound_provider', 'model', 'priority',
  'feature_id', 'hidden', 'transition_mode', 'max_running', 'cc', 'run_as_id',
])
const RUNS_TAB: TabDef<AutomationTab> = { id: 'runs', label: 'Runs' }
const ADVANCED_TAB: TabDef<AutomationTab> = { id: 'advanced', label: 'Advanced' }
/** A schedule has no Test, Dedup entries or State document, so no Advanced tab; a new automation is the wizard. */
function tabsFor(automation: Automation | null): TabDef<AutomationTab>[] {
  if (automation === null) return [SETUP_TAB]
  const base = [SETUP_TAB, SETTINGS_TAB, RUNS_TAB]
  return automation.when.kind === 'event' ? [...base, ADVANCED_TAB] : base
}

/** The Name step's suggestion: the action's first line of text (a task's body, an email's subject), else what runs
 *  when ("Task · Schedule"). */
export function suggestedName(draft: Draft, actionLabel: string): string {
  const value = (draft.actionValue ?? {}) as { body?: unknown; subject?: unknown }
  // An email's subject is its headline; its body is the letter.
  const order = typeof value.subject === 'string' ? [value.subject, value.body] : [value.body]
  const text = order.find((v): v is string => typeof v === 'string' && v.trim() !== '') ?? ''
  // A placeholder is filled per run and an image names nothing; a link keeps its words.
  const plain = (l: string) => l.replace(/\{\{[^}]*\}\}/g, '').replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/[#*_`>\\[\]]/g, '').replace(/\s+/g, ' ').trim()
  const line = text.split('\n').map(plain).find(l => l !== '') ?? ''
  if (line !== '') return line.length > 60 ? `${line.slice(0, 59).trimEnd()}…` : line
  return `${actionLabel} · ${WHEN_CHOICE_LABELS[draft.when]}`
}

/** A new automation's preset Target from `?target=projects:12` / `?target=nodes:<uuid>` (a project's or node's
 *  Automations tab): that one project or node, else none. */
export function presetTarget(param: string | null): TargetRule | null {
  const match = /^(projects|nodes):(.+)$/.exec(param ?? '')
  if (match === null) return null
  const kind = match[1] as TargetKind
  if (kind === 'nodes') return { kind, mode: 'specific', ids: [match[2]] }
  const id = Number(match[2])
  return Number.isInteger(id) && id > 0 ? { kind, mode: 'specific', ids: [id] } : null
}

export default function AutomationEditPage({ mode = 'edit' }: { mode?: 'create' | 'edit' }) {
  const { id } = useParams<{ id: string }>()
  const [params] = useSearchParams()
  const requested = params.get('when') ?? ''
  const qaShown = planSurfaceShown(useAuth().admin, 'production_qa')
  const offered = (WHEN_CHOICES as readonly string[]).includes(requested) && (qaShown || !PRODUCTION_QA_SOURCE_KINDS.has(requested))
  const initialWhen = offered ? (requested as WhenChoice) : 'schedule'
  const targetParam = mode === 'create' ? params.get('target') : null
  const initialTarget = useMemo(() => presetTarget(targetParam), [targetParam])
  // Keyed: a create navigates `/automations/new` -> `/automations/:id` on the same element type.
  return (
    <AutomationEditor
      key={`${mode}:${id ?? ''}`}
      id={mode === 'create' ? null : id ?? null}
      initialWhen={initialWhen}
      initialTarget={initialTarget}
    />
  )
}

function scheduleDraftOfWhen(automation: Automation): ScheduleDraft | null {
  if (automation.when.kind !== 'schedule') return null
  return {
    kind: automation.when.schedule_kind,
    expression: automation.when.cron_expression ?? '',
    intervalSeconds: automation.when.interval_seconds,
  }
}

/** The wire `when` for the draft's current choice, or the sentence that stops it. */
function whenOf(draft: Draft): AutomationInput['when'] | string {
  if (draft.when === 'schedule') {
    if (draft.schedule.kind === SCHEDULE_KIND_CRON && draft.schedule.expression.trim() === '') return 'Set a schedule.'
    if (draft.schedule.kind !== SCHEDULE_KIND_CRON && draft.schedule.intervalSeconds === null) return 'Set an interval.'
    return { kind: 'schedule', ...scheduleFields(draft.schedule), retention_mode: draft.retentionMode }
  }
  return eventWhenOf({ ...draft.event, sourceKind: draft.when })
}

/** Whether the draft's When only records incidents (`EventSourceFields.tsx::recordsOnly`): no Action, no Run now, and
 *  its Target is the one project the incidents are recorded in. */
export const draftRecordsOnly = (draft: Draft) =>
  draft.when !== 'schedule' && recordsOnly({ ...draft.event, sourceKind: draft.when })

/** A saved automation that only records. */
const savedRecordsOnly = (row: Automation) => row.when.kind === 'event' && recordsOnly(eventDraftOf(row.when))

/** A record-only Target: exactly one specific project. */
const isRecordsIntoTarget = (target: TargetRule) => target.kind === 'projects' && target.mode === 'specific' && target.ids.length === 1

/** Why `admin` cannot create in `orgId`, or null. An automation files as its creator, in the org they work in
 *  (`automation_fire._owner_or_reason`; an event's create is refused outright, `routes/triggers.py`), so another
 *  org needs the account menu's switch, after joining it. `orgName` is null while its name loads. */
export function orgWarningFor(admin: Admin | null, orgId: string, orgName: string | null): string | null {
  const working = admin ? currentOrgId(admin) : null
  if (!admin || orgId === '' || working === null || orgId === working) return null
  const membership = (admin.memberships ?? []).find(m => m.org_id === orgId)
  const name = membership?.org_name ?? orgName ?? 'that organization'
  return membership
    ? `You're working in another organization. Switch to ${name} from the account menu to create this.`
    : `You're not a member of ${name}. Join it, then switch to it from the account menu to create this.`
}

/** `orgId` is org zero, the operator plane (the only org whose Email source may receive on the system mail domain). */
function isOperatorPlane(admin: Admin | null, orgId: string): boolean {
  if (!admin || orgId === '') return false
  if (admin.org?.id === orgId && admin.org.slug === OPERATOR_PLANE_SLUG) return true
  return (admin.memberships ?? []).some(m => m.org_id === orgId && m.is_operator_plane)
}

/** What the Organization list says beside an org `admin` cannot create in (the same rule as `orgWarningFor`):
 *  null for the org they work in. */
export function orgNoteFor(admin: Admin | null): (org: { id: string }) => string | null {
  const working = admin ? currentOrgId(admin) : null
  const member = new Set((admin?.memberships ?? []).map(m => m.org_id))
  return org => (org.id === working ? null : member.has(org.id) ? 'not your current org' : 'not a member')
}

/** An automation's input, and a new one's wizard step, carried to the New address page and back
 *  (`hooks/useNestedCreate.ts`; no step on an edit). */
type CreateCarry = {
  draft: Draft; baselineKey: string | null; stagedFiles: File[]; position: WizardPosition | null; nameSuggested: boolean
}

function AutomationEditor({ id, initialWhen, initialTarget }: {
  id: string | null; initialWhen: WhenChoice; initialTarget: TargetRule | null
}) {
  const navigate = useNavigate()
  usePersonaRegistry()
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  const ownOrgId = admin?.org?.id ?? ''
  const orgNote = useMemo(() => orgNoteFor(admin), [admin])
  // Its schedule or trigger moves with it (`tenancy/transfer_objects.py::AUTOMATION_RULE`).
  const changeOrg = useChangeOrgAction('automation')

  const actionTypes = useApiRead(['automation-action-types'], listActionTypes, 'Couldn’t load the action types.')
  const activityCatalogue = useApiRead(['automation-activity-types'], listActivityTypes, '')
  const catalogue: ActionType[] = useMemo(() => actionTypes.data?.items ?? [], [actionTypes.data])
  const activities: ActivityType[] = useMemo(() => activityCatalogue.data?.activity_types ?? [], [activityCatalogue.data])

  /* "Receives at"'s New address… opens that page and comes back here (`hooks/useNestedCreate.ts`). The input it
     carries is `outbox`, kept current by `carry`, since the baseline it needs is computed further down; the way out
     is the leave guard's unasked one, which is made further down too (`leaveNow`). */
  const outbox = useRef<CreateCarry | null>(null)
  const leaveNowRef = useRef<(path: string) => void>(() => {})
  const leaveNow = useCallback((path: string) => leaveNowRef.current(path), [])
  const { openNew, restored: nestedBack, picked } = useNestedCreate(outbox, leaveNow)
  const queryClient = useQueryClient()
  const [back] = useState<CreateCarry | null>(() => {
    const kept = nestedBack?.current ?? null
    if (kept === null || picked?.pick !== ADDRESS_PICK) return kept
    // Re-read before "Receives at" reads it, so the new address is listed (never shown as Removed).
    void queryClient.resetQueries({ queryKey: ORG_EMAIL_KEY })
    return { ...kept, draft: { ...kept.draft, event: withAddress(kept.draft.event, picked.id) } }
  })
  const carried = id === null ? back : null
  /** A saved automation's carried input, applied over its row once it loads (`onLoad`). */
  const resume = useRef(id === null ? null : back)
  const [automation, setAutomation] = useState<Automation | null>(null)
  const [draft, setDraft] = useState<Draft | null>(carried?.draft ?? null)
  const [stagedFiles, setStagedFiles] = useState<File[]>(carried?.stagedFiles ?? [])
  /** The wizard's step, reported by it after each of its renders (a step change re-renders only the wizard). */
  const wizardPosition = useRef<WizardPosition | null>(carried?.position ?? null)
  /** The Name step is offered a name once, the first time it opens empty; a cleared name stays cleared. */
  const nameSuggested = useRef(carried?.nameSuggested ?? false)
  const carry = (position: WizardPosition | null) => {
    wizardPosition.current = position
    if (position?.step === 'name' && draft !== null && draft.name === '' && !nameSuggested.current) {
      nameSuggested.current = true
      edit({ name: suggestedName(draft, descriptorOf(draft.actionType)?.label ?? 'Task') })
    }
    if (draft !== null) outbox.current = { draft, baselineKey, stagedFiles, position, nameSuggested: nameSuggested.current }
  }
  useEffect(() => carry(wizardPosition.current))
  /** The action form's shared UI state (`ActionContext.uiState`): forgotten on a reseed and a type change. */
  const [actionUi, setActionUi] = useState<unknown>(null)
  // Save's write and its refusal; the verbs on a saved automation share the banner (`setSaveError`).
  const { busy: saving, formError: saveError, setFormError: setSaveError, run } = useSubmit()
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  /** The wizard step a refused Create belongs on. */
  const [errorStep, setErrorStep] = useState<AutomationStepId | null>(null)
  const location = useLocation()
  const [notice, setNotice] = useState(() => {
    const carried = (location.state as { notice?: unknown } | null)?.notice
    return typeof carried === 'string' ? carried : ''
  })
  const [carriedNotice, setCarriedNotice] = useState<string | undefined>(undefined)
  const [secret, setSecret] = useState<string | null>(null)
  /* The automation just created: the guard stands down, and the page moves to it once the guard's patch is lifted
     (this effect runs after the guard's cleanup in the same commit) and no secret dialog holds it, so the move is
     not taken for leaving. Replace: Back from the new automation must not reopen an emptied wizard. */
  const [createdId, setCreatedId] = useState<string | null>(null)
  // A create opened from a project's or node's Automations tab keeps its way back there (`?from=`).
  const returnTo = safeReturnPath(location.search, window.location.origin)
  const createdFrom = returnTo ? `?from=${encodeURIComponent(returnTo)}` : ''
  useEffect(() => {
    if (createdId !== null && secret === null) {
      navigate(`/automations/${createdId}${createdFrom}`, { replace: true, state: noticeState(carriedNotice) })
    }
  }, [createdId, secret, carriedNotice, navigate, createdFrom])
  const [runsKey, setRunsKey] = useState(0)
  const [busyVerb, setBusyVerb] = useState(false)

  /* ── Tabs: the URL holds the selection; a later tab's content mounts the first time it is shown. ── */
  const tabs = tabsFor(automation)
  const [tab, selectTab] = useUrlTab(tabs, SETUP_TAB.id)
  const [seen, setSeen] = useState<ReadonlySet<AutomationTab>>(() => new Set<AutomationTab>([SETUP_TAB.id]))
  useEffect(() => {
    setSeen(prev => (prev.has(tab) ? prev : new Set([...prev, tab])))
  }, [tab])
  const shown = (t: AutomationTab) => tab === t || seen.has(t)

  const descriptorOf = useCallback((type: string): ActionType | undefined => {
    const own = catalogue.find(item => item.type === type)
    if (own) return own
    // A system function no longer creatable is absent from the picker's catalogue: its schema is the cron one.
    const activity = activities.find(item => item.type === type)
    if (!activity) return undefined
    return {
      type, label: activity.label, description: activity.description, operator_only: activity.is_system,
      takes_target: false, target_kinds: [], when_kinds: ['schedule'], timeout_seconds: 0,
      params_schema: activity.params,
    }
  }, [catalogue, activities])

  /** The action context for a draft (the entry's conversions read it). */
  const contextFor = useCallback((d: Draft, row: Automation | null): ActionContext => {
    const descriptor = descriptorOf(d.actionType)
    return {
      orgId: d.orgId || null,
      target: descriptor && !descriptor.takes_target ? null : (row?.is_system ? null : d.target),
      whenKind: d.when === 'schedule' ? 'schedule' : 'event',
      eventKind: d.when === 'schedule' ? undefined : d.when,
      templateHint: d.when === 'schedule' ? undefined : EVENT_TEMPLATE_HINT[d.when],
      descriptor,
      automation: row,
      disabled: false,
      stagedFiles: [],
      onStagedFilesChange: () => {},
    }
  }, [descriptorOf])

  const takesTarget = useCallback((d: Draft, row: Automation | null): boolean => {
    if (row?.is_system) return false
    if (draftRecordsOnly(d)) return true
    const descriptor = descriptorOf(d.actionType)
    return descriptor ? descriptor.takes_target : row ? row.target !== null : d.actionType === 'task'
  }, [descriptorOf])

  /** The draft as the wire body, or the first sentence that stops it. */
  const inputOf = useCallback((d: Draft, row: Automation | null): AutomationInput | string => {
    const when = whenOf(d)
    if (typeof when === 'string') return when
    const context = contextFor(d, row)
    const entry = actionEntryFor(d.actionType, context.descriptor, row?.is_system ?? false)
    const base = {
      name: d.name.trim(),
      description: d.description.trim(),
      org_id: d.orgId,
      when,
      target: takesTarget(d, row) ? d.target : null,
    }
    // A record-only create sends no action (it files nothing); a saved one keeps its stored action untouched.
    if (row === null && draftRecordsOnly(d)) return base
    const params = entry ? entry.toParams(d.actionValue, context) : ((d.actionValue as Record<string, unknown> | null) ?? row?.action.params ?? {})
    return { ...base, action: { type: d.actionType, params } }
  }, [contextFor, takesTarget])

  /* The baseline is the first fully-seeded draft's wire body: dirty is "the body a save would send moved". */
  const currentKey = draft === null || draft.actionValue === null ? null : JSON.stringify(inputOf(draft, automation))
  const { base: baselineKey, dirty, reset: forgetBaseline } = useBaselineDirty(currentKey, {
    extra: stagedFiles.length > 0, initial: (carried ?? resume.current)?.baselineKey ?? null,
  })

  const seed = useCallback((row: Automation) => {
    const base: Draft = {
      name: row.name,
      description: row.description,
      orgId: row.org_id,
      when: whenChoiceOf(row.when),
      schedule: scheduleDraftOfWhen(row) ?? FALLBACK_SCHEDULE,
      retentionMode: row.when.kind === 'schedule' ? row.when.retention_mode : DEFAULT_CREATE_RETENTION_MODE,
      event: row.when.kind === 'event' ? eventDraftOf(row.when) : newEventDraft('webhook'),
      targetKind: row.target?.kind ?? 'projects',
      target: row.target ?? { kind: 'projects', mode: 'specific', ids: [] },
      actionType: row.action.type,
      actionValue: null,
    }
    setAutomation(row)
    setDraft(base)
    setActionUi(null)
    forgetBaseline()
    setStagedFiles([])
    setFieldErrors({})
    setSaveError('')
  }, [forgetBaseline])

  /* Back from New address on a saved automation: the input as it was left over the loaded row, against the baseline
     carried with it, so the editor stays dirty (nothing of it is saved yet). */
  const onLoad = useCallback((row: Automation) => {
    const kept = resume.current
    if (kept === null) return seed(row)
    resume.current = null
    setAutomation(row)
    setDraft(kept.draft)
    setStagedFiles(kept.stagedFiles)
  }, [seed])

  const { confirm, confirmDialog, loading, notFound, loadError, deleteError, confirmAndDelete } = useEditRecord(
    id, getAutomation, onLoad,
    { loadFallback: 'Couldn’t load this automation.', notFoundStatuses: [404, 422], listTo: returnTo ?? '/automations', deleteFallback: 'Couldn’t delete this automation.' },
  )

  /* A NEW draft waits for the catalogues (the default schedule is the Runner's declared cadence). */
  useEffect(() => {
    if (id !== null || draft !== null || activityCatalogue.loading) return
    const runner = activities.find(item => item.type === 'runner')?.default_schedule
    setDraft({
      name: '',
      description: '',
      // An operator starts in their own org and may pick another; everyone else files into their own.
      orgId: ownOrgId,
      when: initialWhen,
      schedule: runner
        ? { kind: runner.schedule_kind, expression: runner.cron_expression ?? '', intervalSeconds: runner.interval_seconds }
        : FALLBACK_SCHEDULE,
      retentionMode: DEFAULT_CREATE_RETENTION_MODE,
      event: newEventDraft(initialWhen === 'schedule' ? 'webhook' : initialWhen),
      // The default action (Task) takes either kind, so a preset from a project's or node's page applies as is.
      targetKind: initialTarget?.kind ?? 'projects',
      target: initialTarget ?? { kind: 'projects', mode: 'specific', ids: [] },
      actionType: 'task',
      actionValue: null,
    })
  }, [id, draft, activityCatalogue.loading, activities, initialWhen, initialTarget, ownOrgId])

  /* The action value is the entry's own shape: filled once the entry (and, for a system type, the catalogue) is
     known, from the stored params or the entry's initial value. */
  useEffect(() => {
    if (draft === null || draft.actionValue !== null || actionTypes.loading || activityCatalogue.loading) return
    const context = contextFor(draft, automation)
    const entry = actionEntryFor(draft.actionType, context.descriptor, automation?.is_system ?? false)
    if (!entry) {
      // Nothing can edit this type here: its stored params ride through untouched, so the rest still saves.
      setDraft(prev => (prev === null ? prev : { ...prev, actionValue: { ...(automation?.action.params ?? {}) } }))
      return
    }
    const value = automation && automation.action.type === draft.actionType
      ? entry.fromParams(automation.action.params, context)
      : entry.initial(context)
    setDraft(prev => (prev === null ? prev : { ...prev, actionValue: value }))
  }, [draft, automation, actionTypes.loading, activityCatalogue.loading, contextFor])


  const edit = useCallback((patch: Partial<Draft>) => {
    setNotice('')
    if (patch.actionType !== undefined) setActionUi(null)
    setDraft(prev => (prev === null ? prev : { ...prev, ...patch }))
  }, [])

  /* A record-only When records into a project: a Nodes target picked for another source does not carry over. */
  const recordsIntoNodes = draft !== null && draftRecordsOnly(draft) && draft.targetKind !== 'projects'
  useEffect(() => {
    if (recordsIntoNodes) edit({ targetKind: 'projects' })
  }, [recordsIntoNodes, edit])

  /* ── Target preview: what the rule resolves to right now (AC3.6), once the rule has settled. ── */
  const previewRule = draft && draft.target.kind === draft.targetKind && isValidTarget(draft.target) ? draft.target : null
  const previewOrg = draft?.orgId ?? ''
  const previewKey = previewRule ? JSON.stringify(previewRule) : ''
  const settledPreviewKey = useSettled(previewKey, PREVIEW_DEBOUNCE_MS)
  // previewKey stands for previewRule's content.
  const previewRead = useApiRead(['automation-target-preview', previewOrg, previewKey, automation?.id ?? null],
    () => previewTarget(previewOrg, previewRule!, automation?.id).then((wire): TargetPreview => ({
      resolved: wire.resolved, resolvedCount: wire.resolved_count, universeCount: wire.universe_count,
      dangling: wire.dangling,
    })), 'The preview could not be read.', { enabled: previewRule !== null && previewOrg !== '' && settledPreviewKey === previewKey })
  // No preview while the rule settles or re-reads, nor for a failed read: never the previous rule's answer.
  const preview = settledPreviewKey !== previewKey || previewRead.loading || previewRead.error ? undefined : previewRead.data ?? undefined

  /* ── Save ── */
  /* What stops a save, by the part it belongs to. The wizard blocks each step on its own (the org on When, since the
     When's mailboxes and the Target's projects are that org's); the edit page on the first of them. */
  const parts = useMemo(() => {
    const none = { name: null, org: null, when: null, target: null, action: null, content: null }
    if (draft === null) return none
    let target: string | null = null
    const recordOnly = draftRecordsOnly(draft)
    if (recordOnly) {
      if (!isRecordsIntoTarget(draft.target)) target = 'Pick the one project incidents are recorded in.'
    } else if (takesTarget(draft, automation)) {
      const noun = draft.targetKind === 'nodes' ? 'node' : 'project'
      if (draft.target.kind !== draft.targetKind || !isValidTarget(draft.target)) target = `Pick at least one ${noun}.`
    }
    const when = whenOf(draft)
    const entry = actionEntryFor(draft.actionType, descriptorOf(draft.actionType), automation?.is_system ?? false)
    return {
      name: draft.name.trim() === '' ? 'Name the automation.' : null,
      org: draft.orgId === '' ? 'Pick the organization it files into.' : null,
      when: typeof when === 'string' ? when : null,
      target,
      // The action waits for its form (the value is filled once the catalogues land).
      action: draft.actionValue === null && !recordOnly ? 'The action is still loading.' : null,
      // The action form's own refusal (an empty task description), held on What before Create.
      content: recordOnly || draft.actionValue === null ? null : entry?.blocker?.(draft.actionValue) ?? null,
    }
  }, [draft, automation, takesTarget, descriptorOf])
  const blockers: StepBlockers = {
    when: parts.org ?? parts.when,
    what: parts.target ?? parts.action ?? parts.content,
    name: parts.name,
  }
  const blocker = parts.name ?? parts.org ?? parts.target ?? parts.content ?? parts.when
  // Next refused on What for the action's own reason: said again beside the editor, not only in the footer.
  const [blockedStep, setBlockedStep] = useState<AutomationStepId | null>(null)
  const actionBlocked = blockedStep === 'what' && parts.target === null && parts.content !== null

  /* Only an operator picks the org (a new automation's About); warned there and on Target, before Create. The rule
     is the per-target filer's, so an action that takes no target is not warned. */
  const pickedOrg = isOperator && automation === null && draft !== null && takesTarget(draft, automation) ? draft.orgId : ''
  const foreign = pickedOrg !== '' && admin !== null && pickedOrg !== currentOrgId(admin)
  const pickedOrgRow = useOrgsByIds(foreign ? [pickedOrg] : [], foreign)
  const orgWarning = foreign ? orgWarningFor(admin, pickedOrg, pickedOrgRow.data?.[0]?.name ?? null) : null

  const uploadStaged = useCallback(async (automationId: string, files: File[]): Promise<number[]> => {
    const ids: number[] = []
    for (const file of files) ids.push((await uploadAutomationAttachment(automationId, file)).id)
    return ids
  }, [])

  const save = useCallback(async (): Promise<boolean> => {
    if (saving || draft === null) return false
    setErrorStep(null)
    if (blocker) {
      setSaveError(blocker)
      // The wizard opens the step it belongs on (a Save from the unsaved-changes dialog may come from any step).
      setErrorStep(parts.name ? 'name' : parts.target || parts.content ? 'what' : 'when')
      return false
    }
    const input = inputOf(draft, automation)
    if (typeof input === 'string') {
      setSaveError(input)
      return false
    }
    setFieldErrors({})
    let ok = false
    await run('Couldn’t save.', async () => {
      if (automation === null) {
        const created = await createAutomation(input)
        const row = created.automation
        // A record-only automation files nothing: files staged on an earlier action have nowhere to go.
        if (stagedFiles.length > 0 && row.action.type === 'task' && !draftRecordsOnly(draft)) {
          try {
            const ids = await uploadStaged(row.id, stagedFiles)
            await updateAutomation(row.id, { action: { type: 'task', params: { attachment_ids: ids } } })
          } catch (err) {
            // The automation exists; the files did not all land. Said on its page (the notice rides the navigation).
            setCarriedNotice(`Saved, but a file did not upload: ${readableError(err, 'upload failed')}`)
          }
        }
        setStagedFiles([])
        if (created.secret) setSecret(created.secret)
        setCreatedId(row.id)
        ok = true
        return
      }
      const saved = inputOf(seededDraftOf(automation, draft), automation)
      const body: Partial<AutomationInput> = {}
      if (typeof saved === 'string') Object.assign(body, input)
      else {
        if (input.name !== saved.name) body.name = input.name
        if (input.description !== saved.description) body.description = input.description
        if (JSON.stringify(input.when) !== JSON.stringify(saved.when)) body.when = input.when
        if (JSON.stringify(input.target) !== JSON.stringify(saved.target)) body.target = input.target
        if (JSON.stringify(input.action) !== JSON.stringify(saved.action)) body.action = input.action
      }
      let uploaded: number[] = []
      if (stagedFiles.length > 0 && input.action?.type === 'task') {
        uploaded = await uploadStaged(automation.id, stagedFiles)
        const params = { ...input.action.params }
        params.attachment_ids = [...((params.attachment_ids as number[] | undefined) ?? []), ...uploaded]
        body.action = { type: input.action.type, params }
      }
      let next
      try {
        next = await updateAutomation(automation.id, body)
      } catch (err) {
        // A refused save must not leave files nothing references (a retry would upload them again).
        await Promise.allSettled(uploaded.map(attachmentId => deleteAutomationAttachment(automation.id, attachmentId)))
        throw err
      }
      setStagedFiles([])
      seed(next.automation)
      if (next.secret) setSecret(next.secret)
      setNotice('Saved')
      ok = true
    }, { mapError: (err, fallback) => {
      const match = /^action\.params\.([a-z_]+): ([\s\S]+)$/.exec(apiDetailRaw(err))
      if (match) setFieldErrors({ [match[1]]: match[2] })
      setErrorStep(match ? 'what' : null)
      // On a saved automation, the refused field's own tab.
      if (match && automation !== null) selectTab(SETTINGS_PARAMS.has(match[1]) ? SETTINGS_TAB.id : SETUP_TAB.id)
      // The server's creator-org refusal is the warning About and Target already show, in its words.
      const creatorRefused = /a trigger files as its creator/.test(apiDetailRaw(err))
      return (creatorRefused && orgWarning) || readableError(err, fallback)
    } })
    return ok
  }, [draft, automation, blocker, parts, inputOf, stagedFiles, uploadStaged, seed, orgWarning, selectTab, saving, run, setSaveError])

  const closeSecret = () => setSecret(null)

  /* A create leaves for the new automation itself (`createdId`), so the gate must not go on to where the dialog was
     asked for. */
  const guard = useLeaveGuard(dirty && createdId === null, async () => (await save()) && automation !== null)
  leaveNowRef.current = guard.leaveNow
  /* A run opens in its own pane (`AutomationRunPage.tsx`), through the same unsaved gate; `?from=` brings its
     Close back to this tab (`components/ListDetail.tsx`), and the row rides `state` so it paints at once. */
  const openRun = (run: AutomationRun) => {
    if (automation === null) return
    const from = encodeURIComponent(`${location.pathname}${location.search}`)
    guard.leaveTo(`/automations/${encodeURIComponent(automation.id)}/runs/${encodeURIComponent(run.id)}?from=${from}`, { row: run })
  }

  /* ── Verbs on a saved automation ── */
  const verb = async (call: () => Promise<Automation | { detail: string }>, fallback: string) => {
    setBusyVerb(true)
    setSaveError('')
    try {
      const out = await call()
      if ('id' in out) seedKeepingDraft(out)
      else {
        setNotice(out.detail)
        for (const delay of RUNS_REREAD_MS) window.setTimeout(() => setRunsKey(k => k + 1), delay)
      }
    } catch (err) {
      setSaveError(readableError(err, fallback))
    } finally {
      setBusyVerb(false)
    }
  }
  const confirmAndRelease = async () => {
    if (automation === null || !(await confirm(releaseAutomationConfirm(automation)))) return
    await verb(() => releaseAutomation(automation.id), 'Couldn’t release.')
  }
  /** A pause/resume answer refreshes the row (status, paused) without throwing away an unsaved edit. */
  const seedKeepingDraft = (row: Automation) => setAutomation(row)

  /* Off Setup the Save bar shows once an edit is pending, and then STAYS for that visit, so a Save or Cancel
     made there does not pull the focused button out from under the keyboard. */
  const [barTab, setBarTab] = useState<AutomationTab | null>(null)
  const pending = dirty || saving || guard.saving
  useEffect(() => {
    setBarTab(prev => (pending ? tab : prev === tab ? prev : null))
  }, [pending, tab])
  const showsBar = tab === SETUP_TAB.id || tab === SETTINGS_TAB.id || pending || barTab === tab

  const crumb = { label: 'Automations', to: '/automations' }

  if (loading || notFound || loadError) {
    return (
      <EditStatus
        trail={<Breadcrumb items={[{ label: 'Automations', to: '/automations' }, { label: 'Automation' }]} />}
        loading={loading} notFound={notFound} loadError={loadError} notFoundTitle="Automation not found"
      />
    )
  }
  if (draft === null) return <LoadingState />

  const busy = saving || guard.saving
  const runBlocked = automation === null ? null : runNowBlocked(automation)
  const recordOnly = draftRecordsOnly(draft)
  const isSystem = automation?.is_system === true
  const descriptor = descriptorOf(draft.actionType)
  const showsTarget = takesTarget(draft, automation)
  const targetKinds: TargetKind[] = descriptor?.target_kinds?.length ? descriptor.target_kinds : ['projects', 'nodes']
  const eventOnlyRefused = descriptor ? !descriptor.when_kinds.includes('event') : isSystem
  const whenChoices = WHEN_CHOICES.filter(choice => {
    if (choice === 'schedule') return true
    if (eventOnlyRefused) return false
    // A saved event's source is fixed (the trigger's own rule); Schedule stays reachable.
    if (automation?.when.kind === 'event') return choice === automation.when.source_kind
    if (PRODUCTION_QA_SOURCE_KINDS.has(choice) && !planSurfaceShown(admin, 'production_qa')) return false
    // No new event reaches the community edition (`routes/triggers.py::NO_EVENTS_LOCALLY`).
    return isEnabled('event_triggers')
  })
  const context: ActionContext = {
    ...contextFor(draft, automation),
    disabled: busy,
    stagedFiles,
    onStagedFilesChange: setStagedFiles,
    errors: actionBlocked ? { ...fieldErrors, body: parts.content! } : fieldErrors,
    uiState: actionUi,
    onUiStateChange: setActionUi,
  }
  const entry = actionEntryFor(draft.actionType, descriptor, isSystem)
  const offeredActions = catalogue.filter(item => draft.when === 'schedule' || item.when_kinds.includes('event'))
  const title = automation?.name ?? 'New automation'
  const bodyTemplate = typeof (automation?.action.params.body) === 'string' ? (automation.action.params.body as string) : ''

  /* The essentials, drawn once: the edit page's Setup tab stacks them, the New-automation wizard steps through them
     (a step's own panel draws no top rule, and the stepper names it). */
  const wizard = automation === null
  // The community edition has one org: nothing to pick.
  const showOrg = isOperator && automation === null && isEnabled('multi_org_tenancy')
  const aboutPart = (
    <AboutSection draft={draft} edit={edit} busy={busy} divided={automation?.when.kind === 'event'} title={wizard ? null : 'About'} />
  )
  /* The New address page makes the address in the automation's org (`?org=` when it is not the viewer's own). */
  const openNewAddress = () => openNew(draft.orgId && draft.orgId !== ownOrgId ? newAddressUrl(draft.orgId) : NEW_ADDRESS_URL, ADDRESS_PICK)
  const whenPart = (
    <NewAddressContext.Provider value={openNewAddress}>
      <WhenSection
        draft={draft}
        edit={edit}
        busy={busy}
        divided={!wizard}
        title={wizard ? null : 'When'}
        choices={whenChoices}
        creatingEvent={automation === null || automation.when.kind !== 'event'}
        paused={automation?.paused === true}
        operator={isOperator}
        operatorOrg={isOperator && isOperatorPlane(admin, draft.orgId)}
        automationId={automation?.id ?? null}
      />
    </NewAddressContext.Provider>
  )
  const targetPart = showsTarget && (
    <TargetSection draft={draft} edit={edit} busy={busy} divided kinds={recordOnly ? ['projects'] : targetKinds}
      preview={preview} orgWarning={orgWarning} onLeaveTo={guard.leaveTo} recordsInto={recordOnly}
      required={wizard ? parts.target : null} />
  )
  // A record-only When files nothing: no Action to set.
  const actionPart = !recordOnly && (
    <ActionSection
      draft={draft}
      edit={edit}
      busy={busy}
      divided
      title={wizard ? null : 'Action'}
      offered={offeredActions}
      typeFixed={automation === null ? null : `${descriptor?.label ?? automation.action.type}${isSystem ? ' — read-only' : ''}`}
      typePicked={wizard}
      loadingTypes={actionTypes.loading}
      typesError={actionTypes.error || null}
      entry={entry}
      context={context}
    />
  )
  const dialogs = (
    <>
      <SecretOnceDialog
        secret={secret}
        title="Store this secret now"
        notice="This is the only time it is shown."
        submitLabel="I’ve stored it"
        testIdPrefix="trigger-secret"
        onClose={closeSecret}
      />
      <LeaveGuardDialog leave={guard} message="This automation has changes that have not been saved. Save them before leaving?" />
    </>
  )

  if (automation === null) {
    return (
      <AutomationCreateWizard
        breadcrumb={<Breadcrumb items={[crumb, { label: title }]} />}
        blockers={blockers}
        when={(
          <>
            {showOrg && <OrgField draft={draft} edit={edit} busy={busy} orgWarning={orgWarning} orgNote={orgNote} />}
            {whenPart}
          </>
        )}
        what={(
          <>
            {/* The type first: whether a Target follows is the type's. */}
            {!recordOnly && (
              <ActionTypePicker draft={draft} edit={edit} busy={busy} offered={offeredActions} loadingTypes={actionTypes.loading} />
            )}
            {targetPart}
            {actionPart}
          </>
        )}
        name={aboutPart}
        onCreate={() => void save()}
        onCancel={() => guard.leaveTo(safeReturnPath(location.search, window.location.origin) ?? '/automations')}
        creating={busy}
        saveError={saveError}
        errorStep={errorStep}
        onBlocked={setBlockedStep}
        resume={carried?.position ?? null}
        onPosition={carry}
      >
        {dialogs}
      </AutomationCreateWizard>
    )
  }

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[crumb, { label: title }]} />}
      title={<Box component="span" sx={{ overflowWrap: 'anywhere' }}>{title}</Box>}
      headerTestId="automation-header"
      subtitle={automation && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <StatusChip label={STATUS[automation.status]?.label ?? automation.status} tone={STATUS[automation.status]?.tone ?? 'neutral'} data-status={automation.status} />
          {isSystem && <StatusChip label="System" tone="info" />}
          {/* A pause hides the Stuck cause, not the held run. */}
          {automation.stuck && automation.status !== 'failing' && (
            <Box component="span" data-testid="automation-stuck" sx={{ display: 'inline-flex' }}>
              <StatusChip label="Stuck" tone="error" title={STUCK_PAUSED_HINT} />
            </Box>
          )}
          {/* The cause leads (the list's words); the raw text stays under it, whole — the one place a phone, which has
              no hover, reads it. */}
          {automation.status_cause && (
            <Typography variant="body2" sx={{ color: 'error.main', fontWeight: FONT_WEIGHT.semibold, overflowWrap: 'anywhere', minWidth: 0 }} data-testid="automation-status-cause">
              {automation.status_cause}
            </Typography>
          )}
          {automation.status_reason && automation.status_reason !== automation.status_cause && (
            <Typography
              variant="body2"
              sx={{ color: 'text.secondary', overflowWrap: 'anywhere', minWidth: 0, ...(automation.status_cause && { flexBasis: '100%' }) }}
              data-testid="automation-status-reason"
            >
              {automation.status_reason}
            </Typography>
          )}
        </Box>
      )}
      /* THE SAVE BAR IS THE PAGE'S (`DetailPage`'s `actionBar`): one form, one Save, in reach from every tab
         that has something to save — always on Setup, elsewhere once an edit is pending (a clean bar under
         Runs, or beside the State document's own Save, reads as theirs). Its notices show on every tab. The
         shared sticky bar has the floating Help launcher sit level with it rather than cover it. */
      actionBar={(
        <Box sx={{ pt: 2, maxWidth: grid(128) }}>
          {saveError && <Banner severity="error" testId="automation-save-error">{saveError}</Banner>}
          {notice && <Banner severity={notice === 'Saved' ? 'success' : 'info'} testId="automation-notice">{notice}</Banner>}
          {blocker && dirty && (
            <Typography variant="body2" sx={{ color: 'text.secondary', pt: 1 }} data-testid="automation-save-blocked">
              {blocker}
            </Typography>
          )}
          {showsBar && (
            <FormActionBar testId="automation-actions">
              <Button
                type="button"
                variant="text"
                disabled={busy || !dirty}
                onClick={() => seed(automation)}
                sx={tapTargetSx}
                data-testid="automation-cancel"
              >
                Cancel
              </Button>
              <SubmitButton
                form={AUTOMATION_FORM_ID}
                busy={saving}
                disabled={busy || blocker !== null || !dirty}
                testId="automation-save"
              >
                Save
              </SubmitButton>
            </FormActionBar>
          )}
        </Box>
      )}
      actions={automation && (
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
          {/* The reason rides a wrapper: a disabled button takes no pointer, so its own title never shows. A record-only
              automation has nothing to run, so it has no Run now. */}
          {!savedRecordsOnly(automation) && (
          <Tip title={runBlocked ?? undefined}><Box component="span" data-testid="automation-run-now-reason" sx={{ display: 'inline-flex' }}>
            <Button
              variant="outlined"
              aria-describedby={runBlocked ? 'automation-run-now-why' : undefined}
              disabled={busyVerb || runBlocked !== null}
              onClick={() => void verb(() => runAutomation(automation.id), 'Couldn’t run it.')}
              sx={tapTargetSx}
              data-testid="automation-run-now"
            >
              Run now
            </Button>
          </Box></Tip>
          )}
          {/* A phone has no hover: the reason is printed, on a line of its own under the buttons. */}
          {runBlocked && (
            <Typography
              id="automation-run-now-why"
              variant="caption"
              data-testid="automation-run-now-why"
              sx={{ order: 1, flexBasis: '100%', color: 'text.secondary', display: { xs: 'block', sm: 'none' } }}
            >
              Run now: {runBlocked}
            </Typography>
          )}
          <Button
            variant="outlined"
            disabled={busyVerb}
            onClick={() => void verb(
              () => (automation.paused ? resumeAutomation(automation.id) : pauseAutomation(automation.id)),
              automation.paused ? 'Couldn’t resume.' : 'Couldn’t pause.',
            )}
            sx={tapTargetSx}
            data-testid={automation.paused ? 'automation-resume' : 'automation-pause'}
          >
            {automation.paused ? 'Resume' : 'Pause'}
          </Button>
          {automation.foreign_owner && (
            <Tip title="Its owner is in another organization. Own it yourself so its runs file again."><Button
              variant="outlined"
              color="warning"
              disabled={busyVerb}
              onClick={() => void verb(() => takeAutomationOwnership(automation.id), 'Couldn’t take ownership.')}
              sx={tapTargetSx}
              data-testid="automation-take-ownership"
            >
              Take ownership
            </Button></Tip>
          )}
          {automation.stuck && (
            <Tip title="End the run that outlived its timeout, so the next one starts. Run history stays."><Button
              variant="outlined"
              color="warning"
              disabled={busyVerb}
              onClick={() => void confirmAndRelease()}
              sx={tapTargetSx}
              data-testid="automation-release"
            >
              Release
            </Button></Tip>
          )}
          {/* A system automation never moves (`tenancy/transfer_objects.py::_system_rows`). */}
          {!isSystem && changeOrg.button({
            id: automation.id,
            name: automation.name,
            // The dialog reads only the org's id; the row carries no more.
            org: { id: automation.org_id, name: '', slug: '' },
            href: `/automations/${encodeURIComponent(automation.id)}`,
          })}
          <Button
            variant="outlined"
            color="error"
            onClick={() => void confirmAndDelete(
              deleteAutomationConfirm(automation), () => deleteAutomation(automation.id),
            )}
            sx={tapTargetSx}
            data-testid="automation-delete"
          >
            Delete
          </Button>
        </Box>
      )}
    >
      {deleteError && <Banner severity="error">{deleteError}</Banner>}

      {tabs.length > 1 && (
        <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Automation sections" idPrefix="automation" />
      )}

      <TabPanel id="setup" current={tab} idPrefix="automation">
        {automation.when.kind === 'event' && (
          <EventSetupSection automation={automation} onAutomation={setAutomation} onSecret={setSecret} confirm={confirm} />
        )}

        <FormBox id={AUTOMATION_FORM_ID} onSubmit={save} aria-label="Automation">
          {aboutPart}
          {whenPart}
          {targetPart}
          {actionPart}
        </FormBox>
      </TabPanel>

      {/* The same draft and Save bar as Setup: the submit button names Setup's form, so Settings needs none. */}
      <TabPanel id="settings" current={tab} idPrefix="automation">
        {/* Its own form only so Enter saves here too; mounted the first time it is shown, as Runs is. */}
        {shown('settings') && (
          <FormBox onSubmit={save} aria-label="Automation settings">
            <SettingsSection draft={draft} edit={edit} busy={busy} entry={entry} context={context} />
          </FormBox>
        )}
      </TabPanel>

      {automation && (
        <TabPanel id="runs" current={tab} idPrefix="automation">
          {shown('runs') && (
            <DetailSection title="Runs" testId="automation-section-runs">
              <AutomationRuns automation={automation} reloadKey={runsKey} onOpenRun={openRun} />
            </DetailSection>
          )}
        </TabPanel>
      )}

      {automation && automation.when.kind === 'event' && (
        <TabPanel id="advanced" current={tab} idPrefix="automation">
          {shown('advanced') && (
            <>
              {/* A record-only automation renders no task and runs nothing: no Test. */}
              {!savedRecordsOnly(automation) && (
              <DetailSection title="Test" testId="automation-section-test">
                <EventTestPanel
                  automation={automation}
                  bodyTemplate={bodyTemplateOf(draft, bodyTemplate)}
                  titleTemplate={titleTemplateOf(draft)}
                  onRan={detail => {
                    setNotice(detail)
                    for (const delay of RUNS_REREAD_MS) window.setTimeout(() => setRunsKey(k => k + 1), delay)
                  }}
                />
              </DetailSection>
              )}
              <DetailSection title="Dedup entries" divided={!savedRecordsOnly(automation)} testId="automation-section-dedup">
                <DedupEntries automationId={automation.id} />
              </DetailSection>
              <DetailSection title="State document" divided testId="automation-section-state">
                <StateDocumentEditor
                  triggerId={automation.legacy.id}
                  document={automation.when.state_document}
                  onSaved={document => automation.when.kind === 'event'
                    && setAutomation({ ...automation, when: { ...automation.when, state_document: document } })}
                />
              </DetailSection>
            </>
          )}
        </TabPanel>
      )}

      {dialogs}
      {confirmDialog}
      {changeOrg.dialog}
    </DetailPage>
  )
}

/** The stored row as a draft carrying THIS draft's action value shape — the PATCH baseline. */
function seededDraftOf(row: Automation, current: Draft): Draft {
  const context: ActionContext = {
    orgId: row.org_id, target: row.target, whenKind: row.when.kind, descriptor: undefined, automation: row,
    disabled: false, stagedFiles: [], onStagedFilesChange: () => {},
    templateHint: row.when.kind === 'event' ? EVENT_TEMPLATE_HINT[row.when.source_kind] : undefined,
    eventKind: row.when.kind === 'event' ? row.when.source_kind : undefined,
  }
  const entry = actionEntryFor(row.action.type, undefined, row.is_system)
  return {
    name: row.name,
    description: row.description,
    orgId: row.org_id,
    when: whenChoiceOf(row.when),
    schedule: scheduleDraftOfWhen(row) ?? current.schedule,
    retentionMode: row.when.kind === 'schedule' ? row.when.retention_mode : current.retentionMode,
    event: row.when.kind === 'event' ? eventDraftOf(row.when) : current.event,
    targetKind: row.target?.kind ?? current.targetKind,
    target: row.target ?? current.target,
    actionType: row.action.type,
    actionValue: entry ? entry.fromParams(row.action.params, context) : null,
  }
}

/** The unsaved Task body/title the Test panel renders (a non-task action renders the stored ones). */
function bodyTemplateOf(draft: Draft, stored: string): string {
  const value = draft.actionValue as { body?: unknown } | null
  return typeof value?.body === 'string' ? value.body : stored
}
function titleTemplateOf(draft: Draft): string {
  const value = draft.actionValue as { title_template?: unknown } | null
  return typeof value?.title_template === 'string' ? value.title_template : ''
}

/** The server's raw detail string, for mapping a params refusal onto its field. */
function apiDetailRaw(err: unknown): string {
  const detail = (err as { detail?: unknown } | null)?.detail
  return typeof detail === 'string' ? detail : err instanceof Error ? err.message : ''
}

function noticeState(notice: string | undefined): { notice: string } | undefined {
  return notice ? { notice } : undefined
}
