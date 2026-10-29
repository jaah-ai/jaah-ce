import type { MouseEvent, ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import Banner from '../../ui/Banner'
import { DetailSection } from '../../ui/detail/DetailPage'
import { FormField } from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import LoadingState from '../../ui/LoadingState'
import { OrgSelect, ORG_FIELD_LABEL } from '../../ui/OrgSelect'
import SegmentedControl from '../../ui/SegmentedControl'
import TargetSelector, {
  nodesAdapter,
  projectsAdapter,
  type TargetKind,
  type TargetPreview,
  type TargetRule,
} from '../../ui/TargetSelector'
import type { Org } from '../../types'
import type { ScheduleDraft } from '../cron/schedule'
import ScheduleEditor from '../cron/ScheduleEditor'
import { isKnownRetentionMode, RETENTION_MODE_OPTIONS, retentionHelp, retentionLabel } from '../cron/vocabulary'
import type { ActionContext, ActionEntry } from './actions/registry'
import type { ActionType } from './api'
import EventSourceFields, { type EventDraft } from './EventSourceFields'
import {
  EVENT_OVERLAP_POLICY,
  MANY_TARGETS,
  MISSED_RUNS_POLICY,
  OVERLAP_POLICY,
  WHEN_CHOICE_LABELS,
  type WhenChoice,
} from './vocabulary'

/* THE AUTOMATION EDITOR'S FOUR PARTS — About, When, Target, Action — each a section over the ONE draft model.
 * The detail page's Setup tab composes them (`AutomationEditPage.tsx`), and so does the New-automation flow:
 * the draft, its validation (`inputOf`/`blocker`) and `save()` stay the page's, so a part only renders and
 * reports edits. Every part takes `{ draft, edit, busy }` plus what it alone needs. */

export type Draft = {
  name: string
  description: string
  orgId: string
  when: WhenChoice
  schedule: ScheduleDraft
  retentionMode: string
  event: EventDraft
  targetKind: TargetKind
  target: TargetRule
  actionType: string
  /** The action entry's own value (its `fromParams` shape). */
  actionValue: unknown
}

type PartProps = {
  draft: Draft
  edit: (patch: Partial<Draft>) => void
  busy: boolean
  /** Draws the section's top rule (`DetailSection`'s `divided`). */
  divided?: boolean
}

/** The card's zone: a new row has none yet, and the server preview captions its own (as the cron editor did). */
const BUILDER_TIMEZONE = 'UTC'
const TARGET_LABELS: Record<TargetKind, string> = { projects: 'Projects', nodes: 'Nodes' }
const ADAPTERS = { projects: projectsAdapter, nodes: nodesAdapter } as const

export function AboutSection({ draft, edit, busy, divided, showOrg, orgWarning = null, orgNote }: PartProps & {
  /** An operator picks the org a NEW automation files into; a saved one moves via Change org. */
  showOrg: boolean
  /** Why the picked org cannot take it from this person (`orgWarningFor`), or null. */
  orgWarning?: string | null
  /** The word beside an org in the list this person cannot create in (`orgNoteFor`). */
  orgNote?: (org: Org) => string | null
}) {
  return (
    <DetailSection title="About" divided={divided} testId="automation-section-about">
      <Stack spacing={2.25} sx={{ maxWidth: '40rem' }}>
        <FormTextField id="automation-name" label="Name" required value={draft.name} disabled={busy} onChange={v => edit({ name: v })}
          testId="automation-name" slotProps={{ htmlInput: { maxLength: 200 } }} />
        <FormTextField label="Description" value={draft.description} disabled={busy} multiline minRows={2}
          onChange={v => edit({ description: v })} testId="automation-description" slotProps={{ htmlInput: { maxLength: 2000 } }} />
        {showOrg && (
          <FormField label={ORG_FIELD_LABEL}>
            <OrgSelect id="automation-org" value={draft.orgId === '' ? null : draft.orgId} disabled={busy}
              onChange={next => edit({ orgId: next, target: { kind: draft.targetKind, mode: 'specific', ids: [] } })}
              optionNote={orgNote} testId="automation-org" />
          </FormField>
        )}
        {showOrg && orgWarning && <Banner severity="warning" testId="automation-org-warning">{orgWarning}</Banner>}
      </Stack>
    </DetailSection>
  )
}

/** The project an event's child lives in when the Target names one: its first specific project
 *  (`automation_service._carrier_project`). */
function eventProjectId(draft: Draft): number | null {
  const first = draft.targetKind === 'projects' && draft.target.mode === 'specific' ? Number(draft.target.ids[0]) : NaN
  return Number.isInteger(first) ? first : null
}

export function WhenSection({
  draft, edit, busy, divided = true, choices, scheduleOnlyLabel, sourceLocked, creatingEvent, paused, operator,
  filesTasks = true, operatorOrg = false, automationId = null,
}: PartProps & {
  choices: readonly WhenChoice[]
  /** Set when the action runs only on a schedule: the hint names it. */
  scheduleOnlyLabel?: string
  /** A saved event's source is fixed (the trigger's own rule). */
  sourceLocked: boolean
  /** The event fields are a new source's, not a saved one's. */
  creatingEvent: boolean
  paused: boolean
  operator: boolean
  /** False for an action that files no Task (Send email): the task overlap and queueing lines do not apply. */
  filesTasks?: boolean
  /** The draft's org is the operator plane (an Email source may receive on the system mail domain). */
  operatorOrg?: boolean
  /** The saved automation's id; null while new. */
  automationId?: string | null
}) {
  return (
    <DetailSection title="When" divided={divided} testId="automation-section-when">
      <Stack spacing={1.5} sx={{ maxWidth: '40rem' }}>
        <SegmentedControl
          value={draft.when}
          options={choices.map(choice => ({ value: choice, label: WHEN_CHOICE_LABELS[choice], testId: `automation-when-${choice}` }))}
          onChange={next => edit({ when: next })}
          ariaLabel="When"
          color="primary"
          disabled={busy}
          testId="automation-when"
        />
        {scheduleOnlyLabel !== undefined && <Hint>{scheduleOnlyLabel} runs on a schedule.</Hint>}
        {sourceLocked && <Hint>An event’s source can’t change after create.</Hint>}
        {draft.when === 'schedule' ? (
          /* Its own Stack: Keep logs' raised outlined label overhangs the field's top, so at the section's 1.5 it sat
             flush on the summary's "Next …" line on a phone. */
          <Stack spacing={3}>
            <ScheduleEditor
              draft={draft.schedule}
              onChange={next => edit({ schedule: next })}
              timezone={BUILDER_TIMEZONE}
              disabled={busy}
              disabledNote={paused ? 'paused' : undefined}
            />
            <RetentionField value={draft.retentionMode} disabled={busy} onChange={next => edit({ retentionMode: next })} />
          </Stack>
        ) : (
          <EventSourceFields
            draft={{ ...draft.event, sourceKind: draft.when }}
            onChange={next => edit({ event: next })}
            disabled={busy}
            creating={creatingEvent}
            operator={operator}
            projectId={eventProjectId(draft)}
            orgId={draft.orgId}
            operatorOrg={operatorOrg}
            automationId={automationId}
          />
        )}
        <Box component="ul" sx={{ m: 0, pl: 2.5, color: 'text.secondary' }} data-testid="automation-policies">
          {draft.when === 'schedule' && <Typography component="li" variant="body2">{MISSED_RUNS_POLICY}</Typography>}
          {filesTasks && (
            <Typography component="li" variant="body2">
              {draft.when === 'schedule' ? OVERLAP_POLICY : EVENT_OVERLAP_POLICY}
            </Typography>
          )}
        </Box>
      </Stack>
    </DetailSection>
  )
}

export function TargetSection({ draft, edit, busy, divided = true, kinds, preview, orgWarning = null, onLeaveTo }: PartProps & {
  kinds: readonly TargetKind[]
  /** About's org warning, said again where the project is picked. */
  orgWarning?: string | null
  /** What the rule resolves to right now (the page's debounced server preview). */
  preview: TargetPreview | undefined
  /** Leaves the page through its unsaved-changes gate (the create-project link), so a draft is never dropped. */
  onLeaveTo?: (path: string) => void
}) {
  const resolvedCount = preview?.resolvedCount ?? null
  return (
    <DetailSection title="Target" divided={divided} testId="automation-section-target">
      <Stack spacing={1.5} sx={{ maxWidth: '40rem' }}>
        <SegmentedControl
          value={draft.targetKind}
          options={kinds.map(kind => ({ value: kind, label: TARGET_LABELS[kind], testId: `automation-target-kind-${kind}` }))}
          // The selector resets a rule of the other kind and states why (AC1.2); the page's blocker holds Save.
          onChange={next => edit({ targetKind: next })}
          ariaLabel="Target type"
          color="primary"
          disabled={busy}
          testId="automation-target-kind"
        />
        <TargetSelector
          adapter={ADAPTERS[draft.targetKind]}
          orgId={draft.orgId === '' ? null : draft.orgId}
          value={draft.target}
          onChange={next => edit({ target: next })}
          preview={preview}
          disabled={busy}
          label={draft.targetKind === 'nodes' ? 'Run on nodes' : 'Run in projects'}
          emptyAction={draft.targetKind === 'projects' && (
            <Link component={RouterLink} to="/projects/new" data-testid="automation-target-create-project"
              onClick={onLeaveTo && ((e: MouseEvent) => { e.preventDefault(); onLeaveTo('/projects/new') })}>
              Create a project
            </Link>
          )}
        />
        {draft.targetKind === 'nodes' && (
          <Hint>An offline node’s task waits for it; a disabled node is skipped.</Hint>
        )}
        {orgWarning && <Banner severity="warning" testId="automation-target-org-warning">{orgWarning}</Banner>}
        {resolvedCount !== null && resolvedCount > MANY_TARGETS && (
          <Banner severity="info" testId="automation-many-targets">{resolvedCount} tasks per run.</Banner>
        )}
      </Stack>
    </DetailSection>
  )
}

type ActionTypeProps = Pick<PartProps, 'draft' | 'edit' | 'busy'> & {
  /** The types a new automation may pick (empty: the draft's own type is the only option). */
  offered: readonly ActionType[]
  loadingTypes: boolean
  descriptor: ActionType | undefined
}

/** A new automation's action type. The wizard draws it on the When step, before Target: whether there is a Target
 *  step at all is the type's (`takes_target`), and which Whens it allows. */
export function ActionTypePicker({ draft, edit, busy, offered, loadingTypes, descriptor }: ActionTypeProps) {
  return (
    <TextField
      select
      size="small"
      label="Action"
      value={draft.actionType}
      disabled={busy || loadingTypes}
      onChange={e => edit({ actionType: e.target.value, actionValue: null })}
      slotProps={{ select: { native: true }, htmlInput: { 'data-testid': 'automation-action-type' } }}
      sx={{ maxWidth: '24rem' }}
      helperText={descriptor?.description}
    >
      {offered.length === 0 && <option value={draft.actionType}>{draft.actionType === 'task' ? 'Task' : draft.actionType}</option>}
      {offered.map(item => <option key={item.type} value={item.type}>{item.label}</option>)}
    </TextField>
  )
}

export function ActionSection({
  draft, edit, busy, divided = true, offered, typeFixed, typePicked = false, loadingTypes, typesError, descriptor, entry,
  context,
}: PartProps & ActionTypeProps & {
  /** A saved automation's type is fixed: this is its read-only line, and no picker shows. */
  typeFixed: string | null
  /** The type was picked on an earlier step (the wizard's When): no picker here. */
  typePicked?: boolean
  typesError: string | null
  entry: ActionEntry<unknown> | null
  context: ActionContext
}) {
  return (
    <DetailSection title="Action" divided={divided} testId="automation-section-action">
      <Stack spacing={2} sx={{ maxWidth: '48rem' }}>
        {typeFixed !== null ? (
          <Typography variant="body2" data-testid="automation-action-readonly">{typeFixed}</Typography>
        ) : !typePicked && (
          <ActionTypePicker draft={draft} edit={edit} busy={busy} offered={offered} loadingTypes={loadingTypes}
            descriptor={descriptor} />
        )}
        {typesError && <Banner severity="error">{typesError}</Banner>}
        {entry === null ? (
          <Typography variant="body2" data-testid="automation-no-editor">No editor for {draft.actionType}</Typography>
        ) : draft.actionValue === null ? (
          <LoadingState rows={3} />
        ) : (
          <entry.Form value={draft.actionValue} onChange={next => edit({ actionValue: next })} context={context} />
        )}
      </Stack>
    </DetailSection>
  )
}

function Hint({ children }: { children: ReactNode }) {
  return <Typography variant="body2" sx={{ color: 'text.secondary' }}>{children}</Typography>
}

/** How much of the run ledger a schedule keeps (`pages/cron/vocabulary.ts` owns the modes and words). */
function RetentionField({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (next: string) => void }) {
  return (
    <FormTextField
      label="Keep logs"
      help={retentionHelp(value)}
      select
      slotProps={{ inputLabel: { shrink: true } }}
      value={value}
      disabled={disabled}
      onChange={onChange}
      testId="automation-retention-mode"
    >
      {RETENTION_MODE_OPTIONS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
      {/* An unknown mode renders as itself: a native select would show the first option, and a blind Save rewrite it. */}
      {!isKnownRetentionMode(value) && <option value={value}>{retentionLabel(value)}</option>}
    </FormTextField>
  )
}
