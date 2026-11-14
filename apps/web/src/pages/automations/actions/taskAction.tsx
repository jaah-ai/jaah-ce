import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import RichTextRenderer from '../../../components/richtext/RichTextRenderer'
import TaskSpecFields, {
  MODEL_INHERIT, PROVIDER_ANY, taskSpecAxesFor, type TaskSpec, type TaskSpecAxes,
} from '../../../components/tasks/TaskSpecFields'
import type { TaskPlaybookRef } from '../../../types'
import { personaLabel } from '../../../components/tasks/personaRegistry'
import { DEFAULT_PERSONA, isEpicPersona } from '../../../components/tasks/taskPersona'
import { DEFAULT_PRIORITY } from '../../../components/tasks/taskPriority'
import { useApiRead } from '../../../hooks/useApiRead'
import { ChoiceField, type Choice } from '../../../ui/ChoiceField'
import Banner from '../../../ui/Banner'
import { FormTextField } from '../../../ui/FormTextField'
import LoadingState from '../../../ui/LoadingState'
import SegmentedControl, { type SegmentedOption } from '../../../ui/SegmentedControl'
import { getPlaybook } from '../../playbooks/api'
import { getTemplate, listTemplates, templateKey, templateRefProblem, templateRefs } from '../../templates/api'
import TemplateInputFields, { previewBody } from '../../templates/TemplateInputFields'
import { str } from '../vocabulary'
import type { ActionContext, ActionEntry, ActionFormProps } from './registry'
import { searchUserOptions } from '../../../lib/users'
import AsyncLookup, { type LookupOption } from '../../../ui/AsyncLookup'
import FormField from '../../../ui/FormField'
import type { Explain } from '../../../ui/explain'
import { FONT_WEIGHT, RADIUS, grid } from '../../../theme'

/* THE TASK ACTION (D7, AC4.3): the console's own task-create form, `TaskSpecFields` in its `automation` context —
 * every field and validation of regular task creation, so a field added there appears here with no work. This
 * entry only crosses between that form's `TaskSpec` and the wire's `action.params` (architecture §5.2). */

const EPIC_TRANSITION_DEFAULT = 'auto'

/** The task form's spec plus the action's own `start` (false files each task as a draft nobody dispatches) and
 *  `template_id`: null writes the task from `body`; a template's id writes it from that template's CURRENT
 *  content at each fire (the server takes title, body, persona, autonomous and model from it); `''` is Template
 *  picked as the source with no template chosen yet (sent as null). */
export type TaskActionValue = TaskSpec & {
  start: boolean
  template_id: string | null
  /** The template's input placeholders' values, by label (`TemplateInputFields`); filled at each fire. */
  template_inputs: Record<string, string>
  /** "Run as": whose git login the filed tasks use; null is the project's "Run jobs as", else the org's
   *  (`git_access.project_forge_person`). */
  run_as_id: string | null
}

type Source = 'body' | 'template'
const SOURCE_OPTIONS: readonly SegmentedOption<Source>[] = [
  { value: 'body', label: 'Body', testId: 'automation-task-source-body' },
  { value: 'template', label: 'Template', testId: 'automation-task-source-template' },
]

type StartChoice = 'now' | 'draft'
const START_CHOICES: readonly Choice<StartChoice>[] = [
  { value: 'now', label: 'Now', help: 'Each task starts as soon as it is filed.', testId: 'automation-task-start-now' },
  { value: 'draft', label: 'Leave as draft', help: 'Files each task as a draft nobody starts.', testId: 'automation-task-start-draft' },
]

/* The action's own fields' explanations (`ui/explain.ts`); the task form's fields carry their own. */
const START_HELP = 'Whether each filed task starts or waits as a draft.'
const RUN_AS_HELP: Explain = {
  tip: 'Whose git login and agent account the tasks use.',
  more: 'Blank (Project default) uses the project’s Run jobs as, else the organization’s, else its first Owner or Admin.',
  docs: 'console-reference/automation/automations',
}
const TEMPLATE_HELP = 'Each run files from this template’s current content.'
/** An event's Title: the placeholders it may quote are the pop-up. */
const titleHelp = (placeholders: string): Explain => ({ tip: 'The task title; placeholders fill in from the event.', more: placeholders })

export function taskSpecOf(params: Record<string, unknown>): TaskActionValue {
  return {
    start: params.start !== false,
    persona: str(params.persona, DEFAULT_PERSONA) || DEFAULT_PERSONA,
    body: str(params.body),
    title_template: typeof params.title_template === 'string' ? params.title_template : null,
    model: str(params.model, MODEL_INHERIT),
    bound_provider: str(params.bound_provider, PROVIDER_ANY),
    // The automation action stores the built-in half only (`automation_actions.py`); its picker offers no agent.
    agent_id: null,
    // A task surface's alone: an automation pins only through a node target.
    pinned_node_id: null,
    autonomous: params.autonomous !== false,
    priority: typeof params.priority === 'number' ? params.priority : DEFAULT_PRIORITY,
    playbook_id: typeof params.playbook_id === 'string' ? params.playbook_id : null,
    transition_mode: str(params.transition_mode, EPIC_TRANSITION_DEFAULT) || EPIC_TRANSITION_DEFAULT,
    max_running: typeof params.max_running === 'number' ? params.max_running : null,
    assignee_id: typeof params.assignee_id === 'string' && params.assignee_id !== '' ? params.assignee_id : null,
    attachment_ids: Array.isArray(params.attachment_ids)
      ? params.attachment_ids.filter((id): id is number => typeof id === 'number')
      : [],
    feature_id: typeof params.feature_id === 'string' && params.feature_id !== '' ? params.feature_id : null,
    hidden: params.hidden === true,
    template_id: typeof params.template_id === 'string' && params.template_id !== '' ? params.template_id : null,
    template_inputs: inputsOf(params.template_inputs),
    run_as_id: typeof params.run_as_id === 'string' && params.run_as_id !== '' ? params.run_as_id : null,
  }
}

function inputsOf(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object') return {}
  return Object.fromEntries(Object.entries(value).filter((e): e is [string, string] => typeof e[1] === 'string'))
}

/** The projects a fired task lands in when the target names them outright (the Feature and assignee read them). */
export function specificProjectIds(context: ActionContext): number[] {
  const target = context.target
  if (!target || target.kind !== 'projects' || target.mode !== 'specific') return []
  return target.ids.filter((id): id is number => typeof id === 'number')
}

export function taskParamsOf(spec: TaskActionValue, context: ActionContext): Record<string, unknown> {
  const oneProject = specificProjectIds(context).length === 1
  // A template supplies the body and the model: neither is sent beside it.
  const fromTemplate = spec.template_id !== null
  return {
    persona: spec.persona,
    body: fromTemplate ? '' : spec.body,
    // Only an event renders a title (the server refuses one on a schedule); Body is kept either way.
    title_template: context.whenKind === 'event' && spec.title_template?.trim() ? spec.title_template : null,
    model: fromTemplate || spec.model === MODEL_INHERIT ? null : spec.model,
    bound_provider: spec.bound_provider === PROVIDER_ANY ? null : spec.bound_provider,
    autonomous: spec.autonomous,
    priority: spec.priority,
    // A template's own persona decides: the Body form's playbook and epic mode do not ride along with it.
    playbook_id: fromTemplate ? null : spec.playbook_id,
    transition_mode: !fromTemplate && isEpicPersona(spec.persona) ? spec.transition_mode : null,
    // The Throttle cap rides only when set (an epic's, under 'auto'): absent is no limit.
    ...(!fromTemplate && isEpicPersona(spec.persona) && spec.max_running !== null ? { max_running: spec.max_running } : {}),
    assignee_id: spec.assignee_id,
    attachment_ids: fromTemplate ? [] : spec.attachment_ids,
    feature_id: oneProject ? spec.feature_id : null,
    hidden: spec.hidden,
    start: spec.start,
    template_id: fromTemplate ? spec.template_id || null : null,
    template_inputs: fromTemplate ? spec.template_inputs : {},
    run_as_id: spec.run_as_id,
  }
}

/** Template as the source: the picker (published templates the caller may start), the chosen one read-only, and
 *  the event's Title. A stored id that no longer resolves says so from `refs`, never from a failed detail read. */
function TemplateSourceFields({ value, onChange, context }: ActionFormProps<TaskActionValue>) {
  const id = value.template_id ?? ''
  const cards = useApiRead(['automation-template-cards'], () => listTemplates(), 'Couldn’t load templates.')
  const ref = useApiRead(['automation-template-ref', id], () => templateRefs([id]), 'failed', { enabled: id !== '' })
  const problem = id === '' ? null : templateRefProblem(ref.data?.[0])
  const resolves = ref.data?.[0]?.status === 'ok'
  const detail = useApiRead(templateKey(id), () => getTemplate(id), 'Couldn’t load the template.', { enabled: resolves })
  const offered = (cards.data ?? []).filter(card => card.published && card.may_start)
  const stale = id !== '' && !offered.some(card => card.id === id)
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
      {context.templateHint !== undefined && (
        <FormTextField label="Title" id="task-title-template" help={titleHelp(context.templateHint)} value={value.title_template ?? ''}
          disabled={context.disabled} error={context.errors?.title_template} placeholder="From the template"
          onChange={v => onChange({ ...value, title_template: v === '' ? null : v })} fullWidth />
      )}
      <FormTextField label="Template" help={TEMPLATE_HELP} testId="automation-template-picker" value={id} select fullWidth
        disabled={context.disabled || cards.loading} error={context.errors?.template_id || (id === '' ? context.errors?.body : undefined) || cards.error || undefined}
        onChange={next => onChange({ ...value, template_id: next })}>
        <option value="" disabled>Pick a template</option>
        {stale && <option value={id}>{ref.data?.[0]?.name ?? problem ?? id}</option>}
        {offered.map(card => <option key={card.id} value={card.id}>{card.name}</option>)}
      </FormTextField>
      {problem && <Banner severity="warning" testId="automation-template-status">{problem}</Banner>}
      {resolves && detail.data && (
        <TemplateInputFields inputs={detail.data.inputs} values={value.template_inputs}
          onChange={next => onChange({ ...value, template_inputs: next })} disabled={context.disabled}
          error={context.errors?.template_inputs} />
      )}
      {resolves && detail.data && (
        <Box data-testid="automation-template-preview" sx={{
          border: 1, borderColor: 'divider', borderRadius: RADIUS.sm, p: 1.5, minWidth: 0, overflowWrap: 'anywhere',
        }}>
          <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium }}>{detail.data.title || detail.data.name}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 1 }}>
            {personaLabel(detail.data.persona)}
          </Typography>
          {/* The task editor's own renderer (lists, bold, code), never the body's raw markdown. */}
          <Box sx={{
            typography: 'body2', maxHeight: grid(42.5), overflowY: 'auto',
            '& .rte-content': { p: 0 },
            '& .rte-prosemirror > :first-child': { mt: 0 },
            '& .rte-prosemirror > :last-child': { mb: 0 },
          }}>
            {detail.data.body.trim() === ''
              ? <Typography variant="body2">Empty</Typography>
              : <RichTextRenderer content={previewBody(detail.data.body, value.template_inputs)} linkify={false} />}
          </Box>
        </Box>
      )}
    </Box>
  )
}

/** The stored playbook the task form seeds from, read once. The axes seed once from the spec at mount, and a stored
 *  playbook's pill needs its name: wait for it. The id is the MOUNT's (a later pick must not re-show the skeleton
 *  under the form); unread, the pill is the id. */
function useMountPlaybook(value: TaskActionValue) {
  const [playbookId] = useState(value.playbook_id)
  const { data: playbook, loading } = useApiRead(
    ['automation-playbook-pill', playbookId],
    () => getPlaybook(playbookId ?? '').then(row => ({ id: row.id, name: row.name, simple: row.simple })),
    'failed', { enabled: Boolean(playbookId) },
  )
  return { playbook, waiting: Boolean(playbookId) && loading }
}

/** The axes both halves draw from (Depth on Setup gates Persona and Model on Settings): the editor's shared form
 *  state, seeded from the value until a press sets it. */
function sharedAxes(value: TaskActionValue, playbook: TaskPlaybookRef | null | undefined, context: ActionContext) {
  return {
    axes: (context.uiState as TaskSpecAxes | null | undefined) ?? taskSpecAxesFor(value, playbook ?? null),
    onAxesChange: (next: TaskSpecAxes) => context.onUiStateChange?.(next),
  }
}

function TaskActionForm({ value, onChange, context }: ActionFormProps<TaskActionValue>) {
  const { playbook, waiting } = useMountPlaybook(value)
  if (waiting) return <LoadingState rows={3} />
  const source: Source = value.template_id === null ? 'body' : 'template'
  return (
    <>
      <Box sx={{ mb: 2 }}>
        <SegmentedControl<Source> ariaLabel="Source" testId="automation-task-source" value={source}
          options={SOURCE_OPTIONS} disabled={context.disabled}
          onChange={next => onChange({ ...value, template_id: next === 'body' ? null : '' })} />
      </Box>
      {source === 'template'
        ? <TemplateSourceFields value={value} onChange={onChange} context={context} />
        : <TaskSpecFields part="main" {...specFieldProps(value, onChange, context, playbook)} />}
    </>
  )
}

/** The task form's props, the same for its `main` and `extras` halves. The form returns a bare spec: `start` and
 *  the template fields are this action's own, so they ride through. */
function specFieldProps(
  value: TaskActionValue, onChange: (next: TaskActionValue) => void, context: ActionContext, playbook: TaskPlaybookRef | null | undefined,
) {
  return {
    context: 'automation' as const,
    value,
    onChange: (spec: TaskSpec) => onChange({
      ...spec, start: value.start, template_id: value.template_id, template_inputs: value.template_inputs,
      run_as_id: value.run_as_id,
    }),
    projectIds: specificProjectIds(context),
    orgId: context.orgId,
    targetKind: context.target?.kind,
    templateHint: context.templateHint,
    stagedFiles: context.stagedFiles,
    onStagedFilesChange: context.onStagedFilesChange,
    disabled: context.disabled,
    initialPlaybook: playbook,
    ...sharedAxes(value, playbook, context),
    errors: context.errors as Partial<Record<keyof TaskSpec, string>> | undefined,
  }
}

/** "Run as": any enabled member of the automation's org; blank is the project's "Run jobs as", else the org's. The
 *  stored person's name comes from the automation (`automation_service.out` → `run_as`), so no bare id shows. */
function RunAsField({ value, onChange, context }: ActionFormProps<TaskActionValue>) {
  const stored = context.automation?.run_as
  // The row picked in this session keeps its label until a save names it.
  const [lastPick, setLastPick] = useState<LookupOption | null>(null)
  const label = lastPick?.key === value.run_as_id ? lastPick.label
    : stored?.id === value.run_as_id && stored.name ? stored.name : value.run_as_id
  const picked: LookupOption | null = value.run_as_id === null ? null : { key: value.run_as_id, label: label ?? '' }
  const orgId = context.orgId ?? undefined
  return (
    <FormField label="Run as" help={RUN_AS_HELP} error={context.errors?.run_as_id}>
      <AsyncLookup
        id="automation-task-run-as"
        value={picked}
        onChange={next => { setLastPick(next); onChange({ ...value, run_as_id: next?.key ?? null }) }}
        fetchOptions={(query, signal) => searchUserOptions(query, signal, { enabled: true, org: orgId })}
        fetchOnFocus
        stacked
        placeholder="Project default"
        ariaLabel="Run as"
        noOptionsText="No matching member"
        disabled={context.disabled}
      />
    </FormField>
  )
}

/** The Settings tab's half: Start, Run as, then (a Body source) the task form's extras — a template decides the rest. */
function TaskActionExtras({ value, onChange, context }: ActionFormProps<TaskActionValue>) {
  const { playbook, waiting } = useMountPlaybook(value)
  if (waiting) return <LoadingState rows={3} />
  return (
    <>
      <Box sx={{ mb: 2 }}>
        <ChoiceField<StartChoice> label="Start" help={START_HELP} row testId="automation-task-start" disabled={context.disabled}
          value={value.start ? 'now' : 'draft'} options={START_CHOICES}
          onChange={next => onChange({ ...value, start: next === 'now' })} />
      </Box>
      <Box sx={{ mb: 2 }}>
        <RunAsField value={value} onChange={onChange} context={context} />
      </Box>
      {value.template_id === null && (
        <TaskSpecFields part="extras" {...specFieldProps(value, onChange, context, playbook)} />
      )}
    </>
  )
}

/** The server's own refusal (`automation_actions.TASK_SOURCE_MISSING`), said before Next instead of after Create. */
export const TASK_SOURCE_MISSING = 'Add a description or pick a template.'

/** A task needs something to say: a Body with words, or a picked template. */
export function taskActionBlocker(value: TaskActionValue): string | null {
  if (value.template_id === '') return 'Pick a template.'
  if (value.template_id === null && value.body.trim() === '') return TASK_SOURCE_MISSING
  return null
}

export const taskAction: ActionEntry<TaskActionValue> = {
  type: 'task',
  Form: TaskActionForm,
  Extras: TaskActionExtras,
  blocker: taskActionBlocker,
  initial: () => taskSpecOf({}),
  fromParams: params => taskSpecOf(params),
  toParams: taskParamsOf,
  summary: params => (str(params.template_id) ? 'Task · Template'
    : str(params.persona) ? `Task · ${personaLabel(str(params.persona))}` : 'Task'),
  brief: params => (str(params.template_id) ? 'Template' : str(params.persona) ? personaLabel(str(params.persona)) : 'Task'),
}
