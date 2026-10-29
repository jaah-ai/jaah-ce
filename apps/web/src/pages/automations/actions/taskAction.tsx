import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { get } from '../../../api'
import RichTextRenderer from '../../../components/richtext/RichTextRenderer'
import { depthForPersona, involvementForTask, TASK_DEPTHS, TASK_INVOLVEMENTS } from '../../../components/tasks/TaskAxisControls'
import TaskSpecFields, { MODEL_INHERIT, PROVIDER_ANY, type TaskModelChoice, type TaskSpec } from '../../../components/tasks/TaskSpecFields'
import { useFilingFeatures } from '../../../components/tasks/featureFiling'
import { personaLabel } from '../../../components/tasks/personaRegistry'
import { DEFAULT_PERSONA, isEpicPersona } from '../../../components/tasks/taskPersona'
import { DEFAULT_PRIORITY, priorityLabel } from '../../../components/tasks/taskPriority'
import { useApiRead } from '../../../hooks/useApiRead'
import { ChoiceField, type Choice } from '../../../ui/ChoiceField'
import { memberName } from '../../../lib/memberLabel'
import type { ProjectMember, ProviderChoice } from '../../../types'
import Banner from '../../../ui/Banner'
import { FormTextField } from '../../../ui/FormTextField'
import LoadingState from '../../../ui/LoadingState'
import SegmentedControl, { type SegmentedOption } from '../../../ui/SegmentedControl'
import { getPlaybook } from '../../playbooks/api'
import { getTemplate, listTemplates, templateKey, templateRefProblem, templateRefs } from '../../templates/api'
import type { ActionContext, ActionEntry, ActionFormProps, ActionReviewRow } from './registry'

/* THE TASK ACTION (D7, AC4.3): the console's own task-create form, `TaskSpecFields` in its `automation` context —
 * every field and validation of regular task creation, so a field added there appears here with no work. This
 * entry only crosses between that form's `TaskSpec` and the wire's `action.params` (architecture §5.2). */

const EPIC_TRANSITION_DEFAULT = 'auto'

/** The task form's spec plus the action's own `start` (false files each task as a draft nobody dispatches) and
 *  `template_id`: null writes the task from `body`; a template's id writes it from that template's CURRENT
 *  content at each fire (the server takes title, body, persona, autonomous and model from it); `''` is Template
 *  picked as the source with no template chosen yet (sent as null). */
export type TaskActionValue = TaskSpec & { start: boolean; template_id: string | null }

type Source = 'body' | 'template'
const SOURCE_OPTIONS: readonly SegmentedOption<Source>[] = [
  { value: 'body', label: 'Body', testId: 'automation-task-source-body' },
  { value: 'template', label: 'Template', testId: 'automation-task-source-template' },
]

type StartChoice = 'now' | 'draft'
const START_CHOICES: readonly Choice<StartChoice>[] = [
  { value: 'now', label: 'Now', hint: 'Runs as soon as it is filed', testId: 'automation-task-start-now' },
  { value: 'draft', label: 'Leave as draft', hint: 'Waits for you to start it', testId: 'automation-task-start-draft' },
]

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

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
    project_memory: typeof params.project_memory === 'boolean' ? params.project_memory : null,
    priority: typeof params.priority === 'number' ? params.priority : DEFAULT_PRIORITY,
    playbook_id: typeof params.playbook_id === 'string' ? params.playbook_id : null,
    transition_mode: str(params.transition_mode, EPIC_TRANSITION_DEFAULT) || EPIC_TRANSITION_DEFAULT,
    assignee_id: typeof params.assignee_id === 'string' && params.assignee_id !== '' ? params.assignee_id : null,
    attachment_ids: Array.isArray(params.attachment_ids)
      ? params.attachment_ids.filter((id): id is number => typeof id === 'number')
      : [],
    feature_id: typeof params.feature_id === 'string' && params.feature_id !== '' ? params.feature_id : null,
    hidden: params.hidden === true,
    template_id: typeof params.template_id === 'string' && params.template_id !== '' ? params.template_id : null,
  }
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
    project_memory: spec.project_memory,
    priority: spec.priority,
    // A template's own persona decides: the Body form's playbook and epic mode do not ride along with it.
    playbook_id: fromTemplate ? null : spec.playbook_id,
    transition_mode: !fromTemplate && isEpicPersona(spec.persona) ? spec.transition_mode : null,
    assignee_id: spec.assignee_id,
    attachment_ids: fromTemplate ? [] : spec.attachment_ids,
    feature_id: oneProject ? spec.feature_id : null,
    hidden: spec.hidden,
    start: spec.start,
    template_id: fromTemplate ? spec.template_id || null : null,
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
        <FormTextField label="Title" id="task-title-template" value={value.title_template ?? ''}
          disabled={context.disabled} error={context.errors?.title_template} placeholder="From the template"
          onChange={v => onChange({ ...value, title_template: v === '' ? null : v })} fullWidth />
      )}
      <FormTextField label="Template" testId="automation-template-picker" value={id} select fullWidth
        disabled={context.disabled || cards.loading} error={context.errors?.template_id || (id === '' ? context.errors?.body : undefined) || cards.error || undefined}
        onChange={next => onChange({ ...value, template_id: next })}>
        <option value="" disabled>Pick a template</option>
        {stale && <option value={id}>{ref.data?.[0]?.name ?? problem ?? id}</option>}
        {offered.map(card => <option key={card.id} value={card.id}>{card.name}</option>)}
      </FormTextField>
      {problem && <Banner severity="warning" testId="automation-template-status">{problem}</Banner>}
      {resolves && detail.data && (
        <Box data-testid="automation-template-preview" sx={{
          border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5, minWidth: 0, overflowWrap: 'anywhere',
        }}>
          <Typography variant="body2" sx={{ fontWeight: 500 }}>{detail.data.title || detail.data.name}</Typography>
          <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', mb: 1 }}>
            {personaLabel(detail.data.persona)}
          </Typography>
          {/* The task editor's own renderer (lists, bold, code), never the body's raw markdown. */}
          <Box sx={{
            typography: 'body2', maxHeight: '16rem', overflowY: 'auto',
            '& .rte-content': { p: 0 },
            '& .rte-prosemirror > :first-child': { mt: 0 },
            '& .rte-prosemirror > :last-child': { mb: 0 },
          }}>
            {detail.data.body.trim() === ''
              ? <Typography variant="body2">Empty</Typography>
              : <RichTextRenderer content={detail.data.body} linkify={false} />}
          </Box>
        </Box>
      )}
    </Box>
  )
}

function TaskActionForm({ value, onChange, context }: ActionFormProps<TaskActionValue>) {
  /* The axes seed once from the spec at mount, and a stored playbook's pill needs its name: wait for it. The id
     is the MOUNT's (a later pick must not re-show the skeleton under the form); unread, the pill is the id. */
  const [playbookId] = useState(value.playbook_id)
  const { data: playbook, loading } = useApiRead(
    ['automation-playbook-pill', playbookId],
    () => getPlaybook(playbookId ?? '').then(row => ({ id: row.id, name: row.name, simple: row.simple })),
    'failed', { enabled: Boolean(playbookId) },
  )
  if (playbookId && loading) return <LoadingState rows={3} />
  const source: Source = value.template_id === null ? 'body' : 'template'
  return (
    <>
      <Box sx={{ mb: 2 }}>
        <SegmentedControl<Source> ariaLabel="Source" testId="automation-task-source" value={source}
          options={SOURCE_OPTIONS} disabled={context.disabled}
          onChange={next => onChange({ ...value, template_id: next === 'body' ? null : '' })} />
      </Box>
      {source === 'template' ? <TemplateSourceFields value={value} onChange={onChange} context={context} /> : <TaskSpecFields
        context="automation"
        value={value}
        // The form returns a bare spec: `start` and `template_id` are this action's own, so they ride through.
        onChange={spec => onChange({ ...spec, start: value.start, template_id: value.template_id })}
        projectIds={specificProjectIds(context)}
        orgId={context.orgId}
        targetKind={context.target?.kind}
        templateHint={context.templateHint}
        stagedFiles={context.stagedFiles}
        onStagedFilesChange={context.onStagedFilesChange}
        disabled={context.disabled}
        initialPlaybook={playbook}
        errors={context.errors as Partial<Record<keyof TaskSpec, string>> | undefined}
      />}
      <Box sx={{ mt: 2 }}>
        <ChoiceField<StartChoice> label="Start" row testId="automation-task-start" disabled={context.disabled}
          value={value.start ? 'now' : 'draft'} options={START_CHOICES}
          onChange={next => onChange({ ...value, start: next === 'now' })} />
      </Box>
    </>
  )
}

/* ── REVIEW (the New-automation wizard's last step): every chosen value, by its NAME — an id-valued field reads
 * its label from the same route the form's own picker reads. A lookup that fails shows the stored value. ── */

/** A name read once from the server; `fallback` while it loads and if it cannot be read. */
function LookedUp({ kind, read, fallback, scope = '' }: { kind: string; read: () => Promise<string | null>; fallback: string; scope?: string }) {
  // `read` is re-minted each render; `kind` (what is looked up), `fallback` (the stored value) and `scope` name it.
  const { data, loading, error } = useApiRead(['automation-review-lookup', kind, fallback, scope], read, 'failed')
  return <>{(loading || error ? null : data) ?? fallback}</>
}

function FeatureName({ projectId, id }: { projectId: number; id: string }) {
  const { features } = useFilingFeatures(projectId)
  return <>{features?.find(f => f.id === id)?.name ?? id}</>
}

async function assigneeName(id: string, context: ActionContext): Promise<string | null> {
  const projects = specificProjectIds(context)
  if (projects.length > 0) {
    const lists = await Promise.all(projects.map(p => get<ProjectMember[]>(`/api/projects/${p}/members`).catch(() => [])))
    const found = lists.flat().find(m => m.id === id)
    return found ? memberName(found) : null
  }
  if (!context.orgId) return null
  const rows = await get<{ principal: { id: string; name: string } }[]>(
    `/api/access/members?scope=${encodeURIComponent(`org:${context.orgId}`)}`,
  )
  return (Array.isArray(rows) ? rows : []).find(r => r.principal?.id === id)?.principal.name ?? null
}

/** The body as lines: a markdown line break (`\` + newline, the automation editor's Enter) reads as the line it is. */
export function reviewBodyText(body: string): string {
  return body.trim().replace(/\\\n/g, '\n')
}

function taskReviewRows(spec: TaskActionValue, context: ActionContext): ActionReviewRow[] {
  const depth = TASK_DEPTHS.find(d => d.value === depthForPersona(spec.persona))?.label ?? spec.persona
  const involvement = TASK_INVOLVEMENTS.find(i => i.value === involvementForTask(spec.persona, spec.autonomous))?.label
  const templateId = spec.template_id
  // A template supplies Type, Involvement, Body and Model: its name stands for them.
  const rows: ActionReviewRow[] = templateId !== null ? [{
    label: 'Template',
    value: templateId === '' ? 'None' : <LookedUp kind="template" fallback={templateId} read={async () => {
      const ref = (await templateRefs([templateId]))[0]
      return templateRefProblem(ref) ?? ref?.name ?? null
    }} />,
  }] : [
    { label: 'Type', value: depth === 'Task' ? `Task · ${personaLabel(spec.persona)}` : depth },
    { label: 'Involvement', value: involvement ?? (spec.autonomous ? 'Unattended' : 'Guided') },
  ]
  if (context.whenKind === 'event' && spec.title_template?.trim()) rows.push({ label: 'Title template', value: spec.title_template })
  if (templateId === null) rows.push({
    label: 'Body',
    value: spec.body.trim() === '' ? 'Empty' : (
      <Box component="span" data-testid="automation-review-body" sx={{
        display: '-webkit-box', WebkitLineClamp: 6, WebkitBoxOrient: 'vertical', overflow: 'hidden',
        whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
      }}>{reviewBodyText(spec.body)}</Box>
    ),
  })
  rows.push({ label: 'Start', value: spec.start ? 'Now' : 'Leave as draft' })
  rows.push({ label: 'Priority', value: priorityLabel(spec.priority) })
  if (templateId === null && isEpicPersona(spec.persona)) rows.push({ label: 'Debug', value: spec.transition_mode === 'hold' ? 'On' : 'Off' })
  if (templateId === null && spec.model !== MODEL_INHERIT) {
    rows.push({ label: 'Model', value: <LookedUp kind="model" fallback={spec.model} read={async () =>
      (await get<TaskModelChoice[]>('/api/tasks/models')).find(c => c.value === spec.model)?.label ?? null} /> })
  }
  if (spec.bound_provider !== PROVIDER_ANY) {
    rows.push({ label: 'Provider', value: <LookedUp kind="provider" fallback={spec.bound_provider} read={async () =>
      (await get<ProviderChoice[]>('/api/accounts/providers')).find(c => c.value === spec.bound_provider)?.label ?? null} /> })
  }
  if (spec.playbook_id) {
    const id = spec.playbook_id
    rows.push({ label: 'Playbook', value: <LookedUp kind="playbook" fallback={id} read={async () => (await getPlaybook(id)).name} /> })
  }
  if (spec.assignee_id) {
    const id = spec.assignee_id
    rows.push({ label: 'Assigned to', value: <LookedUp kind="assignee" fallback={id} scope={`${context.orgId}:${specificProjectIds(context).join(',')}`}
      read={() => assigneeName(id, context)} /> })
  } else rows.push({ label: 'Assigned to', value: 'Unassigned' })
  const projects = specificProjectIds(context)
  if (spec.feature_id && projects.length === 1) {
    rows.push({ label: 'Feature', value: <FeatureName projectId={projects[0]} id={spec.feature_id} /> })
  }
  if (spec.project_memory !== null) rows.push({ label: 'Project memory', value: spec.project_memory ? 'On' : 'Off' })
  if (spec.hidden) rows.push({ label: 'Hidden', value: 'Yes' })
  if (context.stagedFiles.length > 0) rows.push({ label: 'Attachments', value: context.stagedFiles.map(f => f.name).join(', ') })
  return rows
}

export const taskAction: ActionEntry<TaskActionValue> = {
  type: 'task',
  Form: TaskActionForm,
  initial: () => taskSpecOf({}),
  fromParams: params => taskSpecOf(params),
  toParams: taskParamsOf,
  summary: params => (str(params.template_id) ? 'Task · Template'
    : str(params.persona) ? `Task · ${personaLabel(str(params.persona))}` : 'Task'),
  brief: params => (str(params.template_id) ? 'Template' : str(params.persona) ? personaLabel(str(params.persona)) : 'Task'),
  reviewRows: taskReviewRows,
}
