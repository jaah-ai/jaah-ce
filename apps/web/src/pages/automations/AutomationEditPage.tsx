import { type FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { currentOrgId, useAuth, type Admin } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { usePersonaRegistry } from '../../components/tasks/usePersonaRegistry'
import { useApiRead } from '../../hooks/useApiRead'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useRemountDraft } from '../../hooks/useRemountDraft'
import { useSettled } from '../../hooks/useSettled'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useUrlTab } from '../../hooks/useUrlTab'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import FormActionBar from '../../ui/FormActionBar'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import { OPERATOR_PLANE_SLUG, useOrgsByIds } from '../../ui/OrgSelect'
import { StatusChip } from '../../ui/StatusChip'
import SubmitButton from '../../ui/SubmitButton'
import SecretOnceDialog from '../../ui/SecretOnceDialog'
import { isValidTarget, type TargetKind, type TargetPreview } from '../../ui/TargetSelector'
import UnsavedChangesDialog from '../../ui/UnsavedChangesDialog'
import { listActivityTypes, type ActivityType } from '../cron/api'
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
import AutomationCreateWizard, { type StepBlockers, type WizardPosition } from './AutomationCreateWizard'
import { automationReviewRows, type AutomationStepId } from './AutomationReview'
import AutomationRuns from './AutomationRuns'
import { isEnabled } from '../../lib/editionFeatures'
import { AboutSection, ActionSection, ActionTypePicker, TargetSection, WhenSection, type Draft } from './AutomationSections'
import {
  EVENT_TEMPLATE_HINT,
  EventSetupSection,
  EventTestPanel,
  eventDraftOf,
  eventWhenOf,
  newEventDraft,
  readableError,
} from './EventSourceFields'
import {
  STATUS_LABELS,
  STATUS_TONE,
  WHEN_CHOICES,
  whenChoiceOf,
  type WhenChoice,
} from './vocabulary'

/* Automation > one automation — `/automations/new` (`mode="create"`, optional `?when=<choice>`) and
 * `/automations/:id`, one component. ONE EDITOR FOR EVERY KIND, in the order a person thinks it:
 * WHEN (a schedule or an event source) -> TARGET (`ui/TargetSelector`, a live rule) -> ACTION (the action registry's
 * form: the Task action is the console's own task-create form, D7). The parts themselves are
 * `AutomationSections.tsx`; this page owns the draft, its validation and the one save.
 *
 * A NEW AUTOMATION IS A WIZARD (`AutomationCreateWizard.tsx`): the same four parts, one step each, then Review and
 * Create — the validation split per step, the save the same call.
 *
 * A SAVED AUTOMATION IS TABBED, first-hand work first (`hooks/useUrlTab.ts`, `?tab=`): Setup (an event's
 * setup, then the four parts), Runs, and — for an event only — Advanced (Test, Dedup entries, State document).
 * Every panel stays mounted, so an unsaved edit survives a trip to Runs; Runs and Advanced mount their content
 * the first time they are shown, so Setup paints as fast as the untabbed page did. Save is the PAGE's bar.
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

type AutomationTab = 'setup' | 'runs' | 'advanced'
const SETUP_TAB: TabDef<AutomationTab> = { id: 'setup', label: 'Setup' }
const RUNS_TAB: TabDef<AutomationTab> = { id: 'runs', label: 'Runs' }
const ADVANCED_TAB: TabDef<AutomationTab> = { id: 'advanced', label: 'Advanced' }
/** A schedule has no Test, Dedup entries or State document, so no Advanced tab; a new automation, one part. */
function tabsFor(automation: Automation | null): TabDef<AutomationTab>[] {
  if (automation === null) return [SETUP_TAB]
  return automation.when.kind === 'event' ? [SETUP_TAB, RUNS_TAB, ADVANCED_TAB] : [SETUP_TAB, RUNS_TAB]
}

export default function AutomationEditPage({ mode = 'edit' }: { mode?: 'create' | 'edit' }) {
  const { id } = useParams<{ id: string }>()
  const [params] = useSearchParams()
  const requested = params.get('when')
  const initialWhen = (WHEN_CHOICES as readonly string[]).includes(requested ?? '') ? (requested as WhenChoice) : 'schedule'
  // Keyed: a create navigates `/automations/new` -> `/automations/:id` on the same element type.
  return <AutomationEditor key={`${mode}:${id ?? ''}`} id={mode === 'create' ? null : id ?? null} initialWhen={initialWhen} />
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

/** A new automation's input and wizard step, carried across the remount a resize over the docking width causes
 *  (`hooks/useRemountDraft.ts`): `/automations/new` moves between the list's pane and the full page. */
type CreateCarry = { draft: Draft; baselineKey: string | null; stagedFiles: File[]; position: WizardPosition | null }

function AutomationEditor({ id, initialWhen }: { id: string | null; initialWhen: WhenChoice }) {
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

  const { restored, keep: keepCarry } = useRemountDraft<CreateCarry>()
  const carried = id === null ? restored : null
  const [automation, setAutomation] = useState<Automation | null>(null)
  const [draft, setDraft] = useState<Draft | null>(carried?.draft ?? null)
  const [baselineKey, setBaselineKey] = useState<string | null>(carried?.baselineKey ?? null)
  const [stagedFiles, setStagedFiles] = useState<File[]>(carried?.stagedFiles ?? [])
  /** The wizard's step, reported by it after each of its renders (a step change re-renders only the wizard). */
  const wizardPosition = useRef<WizardPosition | null>(carried?.position ?? null)
  const carry = (position: WizardPosition | null) => {
    wizardPosition.current = position
    if (id === null && draft !== null) keepCarry({ draft, baselineKey, stagedFiles, position })
  }
  useEffect(() => carry(wizardPosition.current))
  const [saving, setSaving] = useState(false)
  const savingRef = useRef(false)
  const [saveError, setSaveError] = useState('')
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
  useEffect(() => {
    if (createdId !== null && secret === null) {
      navigate(`/automations/${createdId}`, { replace: true, state: noticeState(carriedNotice) })
    }
  }, [createdId, secret, carriedNotice, navigate])
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
    const descriptor = descriptorOf(d.actionType)
    return descriptor ? descriptor.takes_target : row ? row.target !== null : d.actionType === 'task'
  }, [descriptorOf])

  /** The draft as the wire body, or the first sentence that stops it. */
  const inputOf = useCallback((d: Draft, row: Automation | null): AutomationInput | string => {
    const when = whenOf(d)
    if (typeof when === 'string') return when
    const context = contextFor(d, row)
    const entry = actionEntryFor(d.actionType, context.descriptor, row?.is_system ?? false)
    const params = entry ? entry.toParams(d.actionValue, context) : ((d.actionValue as Record<string, unknown> | null) ?? row?.action.params ?? {})
    return {
      name: d.name.trim(),
      description: d.description.trim(),
      org_id: d.orgId,
      when,
      target: takesTarget(d, row) ? d.target : null,
      action: { type: d.actionType, params },
    }
  }, [contextFor, takesTarget])

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
    setBaselineKey(null)
    setStagedFiles([])
    setFieldErrors({})
    setSaveError('')
  }, [])

  const { confirm, confirmDialog, loading, notFound, loadError, deleteError, confirmAndDelete } = useEditRecord(
    id, getAutomation, seed,
    { loadFallback: 'Couldn’t load this automation.', notFoundStatuses: [404, 422], listTo: '/automations', deleteFallback: 'Couldn’t delete this automation.' },
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
      targetKind: 'projects',
      target: { kind: 'projects', mode: 'specific', ids: [] },
      actionType: 'task',
      actionValue: null,
    })
  }, [id, draft, activityCatalogue.loading, activities, initialWhen, ownOrgId])

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

  /* The baseline is the first fully-seeded draft's wire body: dirty is "the body a save would send moved". */
  const currentKey = draft === null || draft.actionValue === null ? null : JSON.stringify(inputOf(draft, automation))
  useEffect(() => {
    if (baselineKey === null && currentKey !== null) setBaselineKey(currentKey)
  }, [baselineKey, currentKey])
  const dirty = currentKey !== null && baselineKey !== null && (currentKey !== baselineKey || stagedFiles.length > 0)

  const edit = useCallback((patch: Partial<Draft>) => {
    setNotice('')
    setDraft(prev => (prev === null ? prev : { ...prev, ...patch }))
  }, [])

  /* ── Target preview: what the rule resolves to right now (AC3.6), once the rule has settled. ── */
  const [preview, setPreview] = useState<TargetPreview | undefined>(undefined)
  const previewRule = draft && draft.target.kind === draft.targetKind && isValidTarget(draft.target) ? draft.target : null
  const previewOrg = draft?.orgId ?? ''
  const previewKey = previewRule ? JSON.stringify(previewRule) : ''
  const settledPreviewKey = useSettled(previewKey, PREVIEW_DEBOUNCE_MS)
  useEffect(() => {
    setPreview(undefined)
    if (!previewRule || previewOrg === '' || settledPreviewKey !== previewKey) return
    let live = true
    previewTarget(previewOrg, previewRule, automation?.id)
      .then(wire => {
        if (live) {
          setPreview({
            resolved: wire.resolved, resolvedCount: wire.resolved_count, universeCount: wire.universe_count,
            dangling: wire.dangling,
          })
        }
      })
      .catch(() => { if (live) setPreview(undefined) })
    return () => { live = false }
    // previewKey stands for previewRule's content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewKey, settledPreviewKey, previewOrg, automation?.id])

  /* ── Save ── */
  /* What stops a save, by the part it belongs to: the wizard blocks each step on its own, the edit page on the
     first of them (About, Target, When — its order before the split). */
  const blockers = useMemo((): StepBlockers => {
    const none: StepBlockers = { about: null, when: null, target: null, action: null }
    if (draft === null) return none
    const about = draft.name.trim() === '' ? 'Name the automation.'
      : draft.orgId === '' ? 'Pick the organization it files into.' : null
    let target: string | null = null
    if (takesTarget(draft, automation)) {
      const noun = draft.targetKind === 'nodes' ? 'node' : 'project'
      if (draft.target.kind !== draft.targetKind || !isValidTarget(draft.target)) target = `Pick at least one ${noun}.`
    }
    const when = whenOf(draft)
    // The Action step waits for its form (the value is filled once the catalogues land).
    const action = draft.actionValue === null ? 'The action is still loading.' : null
    return { about, when: typeof when === 'string' ? when : null, target, action }
  }, [draft, automation, takesTarget])
  const blocker = blockers.about ?? blockers.target ?? blockers.when

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
    if (savingRef.current || draft === null) return false
    setErrorStep(null)
    if (blocker) {
      setSaveError(blocker)
      // The wizard opens the step it belongs on (a Save from the unsaved-changes dialog may come from any step).
      setErrorStep(blockers.about ? 'about' : blockers.target ? 'target' : 'when')
      return false
    }
    const input = inputOf(draft, automation)
    if (typeof input === 'string') {
      setSaveError(input)
      return false
    }
    savingRef.current = true
    setSaving(true)
    setSaveError('')
    setFieldErrors({})
    try {
      if (automation === null) {
        const created = await createAutomation(input)
        const row = created.automation
        if (stagedFiles.length > 0 && row.action.type === 'task') {
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
        return true
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
      if (stagedFiles.length > 0 && input.action.type === 'task') {
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
      return true
    } catch (err) {
      // The server's creator-org refusal is the warning About and Target already show, in its words.
      const creatorRefused = /a trigger files as its creator/.test(apiDetailRaw(err))
      const message = (creatorRefused && orgWarning) || readableError(err, 'Couldn’t save.')
      setSaveError(message)
      const match = /^action\.params\.([a-z_]+): ([\s\S]+)$/.exec(apiDetailRaw(err))
      if (match) setFieldErrors({ [match[1]]: match[2] })
      setErrorStep(match ? 'action' : null)
      return false
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }, [draft, automation, blocker, blockers, inputOf, stagedFiles, uploadStaged, seed, orgWarning])

  const onSubmit = (e: FormEvent) => {
    e.preventDefault()
    void save()
  }

  const closeSecret = () => setSecret(null)

  /* A create leaves for the new automation itself (`createdId`), so the gate must not go on to where the dialog was
     asked for. */
  const guard = useLeaveGuard(dirty && createdId === null, async () => (await save()) && automation !== null)
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
  const showsBar = tab === SETUP_TAB.id || pending || barTab === tab

  const crumb = { label: 'Automations', to: '/automations' }

  if (loading || notFound || loadError) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'Automations', to: '/automations' }, { label: 'Automation' }]} />}
        loading={loading}
        notFound={notFound && 'Automation not found'}
        error={loadError}
      />
    )
  }
  if (draft === null) return <LoadingState />

  const busy = saving || guard.saving
  const runBlocked = automation === null ? null : runNowBlocked(automation)
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
    // No new event reaches the community edition (`routes/triggers.py::NO_EVENTS_LOCALLY`).
    return isEnabled('event_triggers')
  })
  const context: ActionContext = {
    ...contextFor(draft, automation),
    disabled: busy,
    stagedFiles,
    onStagedFilesChange: setStagedFiles,
    errors: fieldErrors,
  }
  const entry = actionEntryFor(draft.actionType, descriptor, isSystem)
  const offeredActions = catalogue.filter(item => draft.when === 'schedule' || item.when_kinds.includes('event'))
  const title = automation?.name ?? 'New automation'
  const bodyTemplate = typeof (automation?.action.params.body) === 'string' ? (automation.action.params.body as string) : ''

  /* The four parts, drawn once: the edit page's Setup tab stacks them, the New-automation wizard steps through
     them (a step's own panel draws no top rule). */
  const wizard = automation === null
  // The community edition has one org: nothing to pick.
  const showOrg = isOperator && automation === null && isEnabled('multi_org_tenancy')
  const aboutPart = (
    <AboutSection draft={draft} edit={edit} busy={busy} divided={automation?.when.kind === 'event'} showOrg={showOrg}
      orgWarning={orgWarning} orgNote={orgNote} />
  )
  const typePicker = wizard && (
    <Box sx={{ mt: 2 }}>
      <ActionTypePicker draft={draft} edit={edit} busy={busy} offered={offeredActions} loadingTypes={actionTypes.loading}
        descriptor={descriptor} />
    </Box>
  )
  const whenPart = (
    <>
      <WhenSection
        draft={draft}
        edit={edit}
        busy={busy}
        divided={!wizard}
        choices={whenChoices}
        scheduleOnlyLabel={eventOnlyRefused && !isSystem ? descriptor?.label ?? 'This action' : undefined}
        sourceLocked={automation?.when.kind === 'event'}
        creatingEvent={automation === null || automation.when.kind !== 'event'}
        paused={automation?.paused === true}
        operator={isOperator}
        operatorOrg={isOperator && isOperatorPlane(admin, draft.orgId)}
        automationId={automation?.id ?? null}
        filesTasks={showsTarget}
      />
      {/* New: the type first, so the Target step exists only for an action that takes one. */}
      {typePicker}
    </>
  )
  const targetPart = showsTarget && (
    <TargetSection draft={draft} edit={edit} busy={busy} divided={!wizard} kinds={targetKinds} preview={preview}
      orgWarning={orgWarning} onLeaveTo={guard.leaveTo} />
  )
  const actionPart = (
    <ActionSection
      draft={draft}
      edit={edit}
      busy={busy}
      divided={!wizard}
      offered={offeredActions}
      typeFixed={automation === null ? null : `${descriptor?.label ?? automation.action.type}${isSystem ? ' — read-only' : ''}`}
      typePicked={wizard}
      loadingTypes={actionTypes.loading}
      typesError={actionTypes.error || null}
      descriptor={descriptor}
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
      <UnsavedChangesDialog
        open={guard.open}
        saving={guard.saving}
        onChoose={choice => void guard.onChoose(choice)}
        message="This automation has changes that have not been saved. Save them before leaving?"
      />
    </>
  )

  if (automation === null) {
    return (
      <AutomationCreateWizard
        breadcrumb={<Breadcrumb items={[crumb, { label: title }]} />}
        blockers={blockers}
        nameMissing={draft.name.trim() === ''}
        showsTarget={showsTarget}
        about={aboutPart}
        when={whenPart}
        target={targetPart}
        action={actionPart}
        reviewRows={automationReviewRows({ draft, showOrg, showsTarget, descriptor, entry, context })}
        onCreate={() => void save()}
        onCancel={() => guard.leaveTo('/automations')}
        creating={busy}
        saveError={saveError}
        errorStep={errorStep}
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
          <StatusChip label={STATUS_LABELS[automation.status] ?? automation.status} tone={STATUS_TONE[automation.status] ?? 'neutral'} data-status={automation.status} />
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
            <Typography variant="body2" sx={{ color: 'error.main', fontWeight: 600, overflowWrap: 'anywhere', minWidth: 0 }} data-testid="automation-status-cause">
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
        <Box sx={{ pt: 2, maxWidth: '48rem' }}>
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
          {/* The reason rides a wrapper: a disabled button takes no pointer, so its own title never shows. */}
          <Box component="span" title={runBlocked ?? undefined} data-testid="automation-run-now-reason" sx={{ display: 'inline-flex' }}>
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
          </Box>
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
          {automation.stuck && (
            <Button
              variant="outlined"
              color="warning"
              disabled={busyVerb}
              title="End the run that outlived its timeout, so the next one starts. Run history stays."
              onClick={() => void confirmAndRelease()}
              sx={tapTargetSx}
              data-testid="automation-release"
            >
              Release
            </Button>
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

        <Box component="form" id={AUTOMATION_FORM_ID} onSubmit={onSubmit} noValidate aria-label="Automation">
          {aboutPart}
          {whenPart}
          {targetPart}
          {actionPart}
        </Box>
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
              <DetailSection title="Dedup entries" divided testId="automation-section-dedup">
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
