import type { MouseEvent } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import Banner from '../../ui/Banner'
import { DetailSection } from '../../ui/detail/DetailPage'
import { FormField } from '../../ui/FormField'
import { EntityNameFields } from '../../ui/EntityNameFields'
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
import { isKnownRetentionMode, RETENTION_MODE_OPTIONS, retentionLabel } from '../cron/vocabulary'
import type { Explain } from '../../ui/explain'
import type { ActionContext, ActionEntry } from './actions/registry'
import type { ActionType } from './api'
import EventSourceFields, { DOCS_HOW, DOCS_REFERENCE, EventExtrasFields, type EventDraft } from './EventSourceFields'
import { MANY_TARGETS, WHEN_CHOICE_HELP, WHEN_CHOICE_LABELS, type WhenChoice } from './vocabulary'
import { SIZE, grid } from '../../theme'

/* THE AUTOMATION EDITOR'S PARTS — About, When, Target, Action (the essentials) and Settings (everything else) — each
 * a section over the ONE draft model. The detail page's Setup tab stacks the essentials and its Settings tab draws
 * Settings (`AutomationEditPage.tsx`); the New-automation wizard asks only the essentials, so a new automation keeps
 * every setting's default. The draft, its validation and `save()` stay the page's: a part only renders and reports
 * edits. Every part takes `{ draft, edit, busy }` plus what it alone needs. No prose is drawn: a field's explanation
 * is its label's hover help (`ui/FormField`'s `help`). */

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
const TARGET_KIND_HELP: Record<TargetKind, string> = {
  projects: 'Files one task into each targeted project.',
  nodes: 'Files one task per targeted node, pinned to it.',
}
const ADAPTERS = { projects: projectsAdapter, nodes: nodesAdapter } as const

/* Each part's explanations (`ui/explain.ts`): the hover on its label, a pop-up where there is more to say. */
const NAME_HELP = 'Names it in the automation list, runs and alerts.'
const DESCRIPTION_HELP = 'Your own note on what this automation is for.'
const ORG_HELP: Explain = {
  tip: 'The org whose projects, mailboxes and members it uses.',
  more: 'Changing it clears the Target’s picks: projects and nodes belong to one organization.',
}
const WHEN_HELP: Explain = {
  tip: 'What makes it run: a schedule or an event.',
  more: 'Schedule runs at set times. Every other choice runs each time that source sends something: a webhook call, '
    + 'a GitHub event, an email, a Slack mention, a new user, a monitor alert or an incident. A saved event source '
    + 'is fixed; it can still switch to Schedule.',
  docs: DOCS_HOW,
}
/** The When's source field: its one-line tip; each choice's effect is a line of its pop-up. */
const SOURCE_LABEL = 'Source'
const SOURCE_HELP = 'What starts each run.'
const TARGET_HELP: Explain = {
  tip: 'Where each run files work: one task per target.',
  more: 'Projects files a task into each project; Nodes files one task per node, pinned to it. All includes ones added '
    + 'later, Specific only the ticked ones, All except all but the ticked ones. The rule is worked out again at every run.',
  docs: 'using-jaah/automations/choose-what-an-automation-runs-on',
}
const PROJECTS_FIELD_HELP = 'Each run files one task into every picked project.'
const NODES_FIELD_HELP = 'Each run files one task per picked node, pinned there.'
const RECORDS_INTO_FIELD_HELP = 'Every alert is recorded under the picked project.'
const RECORDS_INTO_HELP: Explain = {
  tip: 'The one project its incidents are recorded in.',
  more: 'A monitor or incident-shaped webhook files no task: each alert is recorded under this project’s Incidents.',
  docs: DOCS_REFERENCE,
}
const ACTION_HELP: Explain = {
  tip: 'What each run does: file tasks or send email.',
  more: 'Task files one task per target: Body writes it here; Template files it from a template’s current content. '
    + 'Send email sends one email from your organization’s Gmail per run.',
  docs: DOCS_REFERENCE,
}
const ACTION_TYPE_HELP = 'Its kind of action; fixed once the automation is saved.'
/** What picking each action type does; a type this build has no words for shows none. */
const ACTION_TYPE_CHOICE_HELP: Record<string, string> = {
  task: 'Files one task per target each run.',
  'email.send': 'Sends one email from your org’s Gmail each run.',
}
const RETENTION_HELP: Explain = {
  tip: 'Which firings are recorded under Runs.',
  more: 'Every failure and every unexpected skip is recorded whatever you pick.',
  docs: DOCS_REFERENCE,
}

export function AboutSection({ draft, edit, busy, divided, title = 'About' }: PartProps & {
  /** The section's heading; null draws none (the wizard's Name step, whose stepper already names it). */
  title?: string | null
}) {
  return (
    <DetailSection title={title ?? undefined} divided={divided} testId="automation-section-about">
      <Stack spacing={2} sx={{ maxWidth: SIZE.measure }}>
        <EntityNameFields
          disabled={busy}
          name={{ id: 'automation-name', value: draft.name, onChange: v => edit({ name: v }), testId: 'automation-name', maxLength: 200, help: NAME_HELP }}
          description={{
            value: draft.description, onChange: v => edit({ description: v }), testId: 'automation-description', maxLength: 2000,
            help: DESCRIPTION_HELP,
          }}
        />
      </Stack>
    </DetailSection>
  )
}

/** An operator's pick of the org a NEW automation files into (a saved one moves via Change org): first in the wizard,
 *  since the When's mailboxes and the Target's projects are that org's. */
export function OrgField({ draft, edit, busy, orgWarning = null, orgNote }: Pick<PartProps, 'draft' | 'edit' | 'busy'> & {
  /** Why the picked org cannot take it from this person (`orgWarningFor`), or null. */
  orgWarning?: string | null
  /** The word beside an org in the list this person cannot create in (`orgNoteFor`). */
  orgNote?: (org: Org) => string | null
}) {
  return (
    <Stack spacing={1.5} sx={{ maxWidth: SIZE.measure, mb: 2 }}>
      <FormField label={ORG_FIELD_LABEL} help={ORG_HELP}>
        <OrgSelect id="automation-org" value={draft.orgId === '' ? null : draft.orgId} disabled={busy}
          onChange={next => edit({ orgId: next, target: { kind: draft.targetKind, mode: 'specific', ids: [] } })}
          optionNote={orgNote} testId="automation-org" />
      </FormField>
      {orgWarning && <Banner severity="warning" testId="automation-org-warning">{orgWarning}</Banner>}
    </Stack>
  )
}

/** The project an event's child lives in when the Target names one: its first specific project
 *  (`automation_service._carrier_project`). */
function eventProjectId(draft: Draft): number | null {
  const first = draft.targetKind === 'projects' && draft.target.mode === 'specific' ? Number(draft.target.ids[0]) : NaN
  return Number.isInteger(first) ? first : null
}

export function WhenSection({
  draft, edit, busy, divided = true, title = 'When', choices, creatingEvent, paused, operator, operatorOrg = false,
  automationId = null,
}: PartProps & {
  /** The section's heading; null draws none (the wizard's When step). */
  title?: string | null
  choices: readonly WhenChoice[]
  /** The event fields are a new source's, not a saved one's. */
  creatingEvent: boolean
  paused: boolean
  operator: boolean
  /** The draft's org is the operator plane (an Email source may receive on the system mail domain). */
  operatorOrg?: boolean
  /** The saved automation's id; null while new. */
  automationId?: string | null
}) {
  return (
    <DetailSection title={title ?? undefined} help={WHEN_HELP} divided={divided} testId="automation-section-when">
      <Stack spacing={1.5} sx={{ maxWidth: SIZE.measure }}>
        {/* One choice (a saved event's fixed source, a schedule-only action) is a fact, not a control. */}
        {choices.length > 1 && (
          // Up to 13 sources: a native select (style guide: a segmented picker holds 2–5), each choice's effect a line
          // of the label's pop-up, so no hover ever sits over another option.
          <FormTextField
            id="automation-when"
            label={SOURCE_LABEL}
            help={SOURCE_HELP}
            value={draft.when}
            options={choices.map(choice => ({ value: choice, label: WHEN_CHOICE_LABELS[choice], help: WHEN_CHOICE_HELP[choice] }))}
            onChange={next => edit({ when: next as WhenChoice })}
            disabled={busy}
            testId="automation-when"
          />
        )}
        {draft.when === 'schedule' ? (
          <ScheduleEditor
            draft={draft.schedule}
            onChange={next => edit({ schedule: next })}
            timezone={BUILDER_TIMEZONE}
            disabled={busy}
            disabledNote={paused ? 'paused' : undefined}
          />
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
      </Stack>
    </DetailSection>
  )
}

export function TargetSection({ draft, edit, busy, divided = true, kinds, preview, orgWarning = null, onLeaveTo, recordsInto = false, required = null }: PartProps & {
  kinds: readonly TargetKind[]
  /** What the rule still needs before Next ("Pick at least one project."), said up front under the picker. */
  required?: string | null
  /** A record-only When: the Target is the one project its incidents are recorded in, not where tasks are filed. */
  recordsInto?: boolean
  /** About's org warning, said again where the project is picked. */
  orgWarning?: string | null
  /** What the rule resolves to right now (the page's debounced server preview). */
  preview: TargetPreview | undefined
  /** Leaves the page through its unsaved-changes gate (the create-project link), so a draft is never dropped. */
  onLeaveTo?: (path: string) => void
}) {
  const resolvedCount = preview?.resolvedCount ?? null
  return (
    <DetailSection title="Target" help={recordsInto ? RECORDS_INTO_HELP : TARGET_HELP} divided={divided} testId="automation-section-target">
      <Stack spacing={1.5} sx={{ maxWidth: SIZE.measure }}>
        {!recordsInto && <SegmentedControl
          value={draft.targetKind}
          options={kinds.map(kind => ({ value: kind, label: TARGET_LABELS[kind], help: TARGET_KIND_HELP[kind], testId: `automation-target-kind-${kind}` }))}
          // The selector resets a rule of the other kind and states why (AC1.2); the page's blocker holds Save.
          onChange={next => edit({ targetKind: next })}
          ariaLabel="Target type"
          color="primary"
          disabled={busy}
          testId="automation-target-kind"
        />}
        <TargetSelector
          adapter={ADAPTERS[draft.targetKind]}
          orgId={draft.orgId === '' ? null : draft.orgId}
          value={draft.target}
          onChange={next => edit({ target: next })}
          preview={preview}
          disabled={busy}
          label={recordsInto ? 'Records into' : draft.targetKind === 'nodes' ? 'Run on nodes' : 'Run in projects'}
          help={recordsInto ? RECORDS_INTO_FIELD_HELP : draft.targetKind === 'nodes' ? NODES_FIELD_HELP : PROJECTS_FIELD_HELP}
          modeHelp={!recordsInto}
          emptyAction={draft.targetKind === 'projects' && (
            <Link component={RouterLink} to="/projects/new" data-testid="automation-target-create-project"
              onClick={onLeaveTo && ((e: MouseEvent) => { e.preventDefault(); onLeaveTo('/projects/new') })}>
              Create a project
            </Link>
          )}
        />
        {required && (
          <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="automation-target-required">{required}</Typography>
        )}
        {orgWarning && <Banner severity="warning" testId="automation-target-org-warning">{orgWarning}</Banner>}
        {!recordsInto && resolvedCount !== null && resolvedCount > MANY_TARGETS && (
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
}

/** A new automation's action type, first on the wizard's What step: whether a Target follows is the type's
 *  (`takes_target`). */
export function ActionTypePicker({ draft, edit, busy, offered, loadingTypes }: ActionTypeProps) {
  return (
    <FormTextField
      label="Action"
      help={ACTION_TYPE_HELP}
      value={draft.actionType}
      disabled={busy || loadingTypes}
      onChange={next => edit({ actionType: next, actionValue: null })}
      testId="automation-action-type"
      sx={{ maxWidth: SIZE.measureNarrow }}
      options={offered.map(item => ({ value: item.type, label: item.label, help: ACTION_TYPE_CHOICE_HELP[item.type] }))}
    >
      {offered.length === 0 && <option value={draft.actionType}>{draft.actionType === 'task' ? 'Task' : draft.actionType}</option>}
    </FormTextField>
  )
}

export function ActionSection({
  draft, edit, busy, divided = true, title = 'Action', offered, typeFixed, typePicked = false, loadingTypes, typesError, entry,
  context,
}: PartProps & ActionTypeProps & {
  /** The section's heading; null draws none (the wizard's What step, under its own Action picker). */
  title?: string | null
  /** A saved automation's type is fixed: this is its read-only line, and no picker shows. */
  typeFixed: string | null
  /** The type was picked on an earlier step (the wizard's When): no picker here. */
  typePicked?: boolean
  typesError: string | null
  entry: ActionEntry<unknown> | null
  context: ActionContext
}) {
  return (
    <DetailSection title={title ?? undefined} help={ACTION_HELP} divided={divided} testId="automation-section-action">
      <Stack spacing={2} sx={{ maxWidth: grid(128) }}>
        {typeFixed !== null ? (
          <Typography variant="body2" data-testid="automation-action-readonly">{typeFixed}</Typography>
        ) : !typePicked && (
          <ActionTypePicker draft={draft} edit={edit} busy={busy} offered={offered} loadingTypes={loadingTypes} />
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

/** Everything the essentials leave out, for the edit page's Settings tab: the When's (a schedule's Keep logs, an
 *  event's Preflight, Limits and Auto-pause), then the action's own (`ActionEntry.Extras`). */
export function SettingsSection({ draft, edit, busy, entry, context }: Pick<PartProps, 'draft' | 'edit' | 'busy'> & {
  entry: ActionEntry<unknown> | null
  context: ActionContext
}) {
  const Extras = entry?.Extras
  return (
    <>
      <DetailSection title="When" testId="automation-settings-when">
        <Stack spacing={2} sx={{ maxWidth: SIZE.measure }}>
          {draft.when === 'schedule' ? (
            <RetentionField value={draft.retentionMode} disabled={busy} onChange={next => edit({ retentionMode: next })} />
          ) : (
            <EventExtrasFields draft={{ ...draft.event, sourceKind: draft.when }} onChange={next => edit({ event: next })} disabled={busy} />
          )}
        </Stack>
      </DetailSection>
      {Extras && draft.actionValue !== null && (
        <DetailSection title="Action" divided testId="automation-settings-action">
          <Box sx={{ maxWidth: grid(128) }}>
            <Extras value={draft.actionValue} onChange={next => edit({ actionValue: next })} context={context} />
          </Box>
        </DetailSection>
      )}
    </>
  )
}

/** How much of the run ledger a schedule keeps (`pages/cron/vocabulary.ts` owns the modes and words). */
function RetentionField({ value, disabled, onChange }: { value: string; disabled: boolean; onChange: (next: string) => void }) {
  return (
    <FormTextField
      label="Keep logs"
      help={RETENTION_HELP}
      value={value}
      disabled={disabled}
      onChange={onChange}
      testId="automation-retention-mode"
      options={RETENTION_MODE_OPTIONS}
    >
      {/* An unknown mode renders as itself (leading the list): a native select would show the first option, and a
          blind Save rewrite it. */}
      {!isKnownRetentionMode(value) && <option value={value}>{retentionLabel(value)}</option>}
    </FormTextField>
  )
}
