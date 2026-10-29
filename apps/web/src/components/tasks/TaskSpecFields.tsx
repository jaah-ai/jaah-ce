import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { Theme } from '@mui/material/styles'
import { nanoid } from 'nanoid'
import { get } from '../../api'
import { useAuth, type Admin } from '../../auth'
import { AgentPicker, useCanPickAgent } from '../agents/AgentPicker'
import { useMaxFileBytes } from '../../lib/maxFileBytes'
import { useApiRead } from '../../hooks/useApiRead'
import { useCanCreateChats } from '../../hooks/useTaskAccess'
import { cachedRead, CATALOGUE_STALE_MS, ROSTER_STALE_MS } from '../../lib/cachedRead'
import { formatBytes } from '../../lib/formatBytes'
import { personOf } from '../../lib/authorBadge'
import { memberLabel, memberName, memberOption } from '../../lib/memberLabel'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { planSwitchOn } from '../../lib/planSwitches'
import { listAgents } from '../../pages/agents/api'
import { listAccounts } from '../../pages/credentials/api'
import { assignmentsPath, byName, type AssignmentsResponse } from '../../lib/assignments'
import type { PlaybookOut } from '../../pages/playbooks/api'
import type { OrgRef, ProjectMember, ProviderChoice, TaskPlaybookRef } from '../../types'
import { readAsOnce } from '../../lib/actAs'
import { AdvancedSection } from '../../ui/AdvancedSection'
import { useViewerMode } from '../../lib/viewerMode'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import { DetailCell, DetailField } from '../../ui/detail/DetailPage'
import { PHONE } from '../../ui/listStyles'
import { FieldRow } from '../../ui/FieldRow'
import { matchesAny } from '../../ui/FilterRow'
import { FIELD_SX } from '../../ui/formDensity'
import { FormField } from '../../ui/FormField'
import { FormTextField } from '../../ui/FormTextField'
import { SettingCheckbox } from '../../ui/SettingCheckbox'
import RichTextEditor from '../richtext/RichTextEditor'
import RichTextRenderer from '../richtext/RichTextRenderer'
import { paintableImageUrl, releasePaintedUrl, type AttachmentResolver } from '../richtext/attachmentResolver'
import { useFilingFeatures } from './featureFiling'
import { personaLabel, personaOptions } from './personaRegistry'
import TaskAttachmentsField, { ATTACHMENT_CHIP_MAX, type StagedAttachment } from './TaskAttachmentsField'
import {
  autonomousForInvolvement,
  depthForPersona,
  involvementForTask,
  personaForDepth,
  TASK_DEPTHS,
  TASK_INVOLVEMENTS,
  type TaskDepth,
  TaskDepthControl,
  type TaskInvolvement,
  TaskInvolvementControl,
} from './TaskAxisControls'
import { type TaskCreateDefaults, resolveProjectMemoryDefault, usePlaybooksEnabled } from './taskDefaults'
import { ADVISOR_PERSONA, DEFAULT_PERSONA, isEpicPersona, PROTOTYPER_PERSONA } from './taskPersona'
import { PRIORITY_LEVELS, priorityLabel } from './taskPriority'

/* ── THE TASK-CREATE FIELDS, ONE COMPONENT FOR EVERY SURFACE THAT STARTS A TASK (D7) ──────────────────────────
 * `TaskEditForm` (context `task`), an automation's Task action (context `automation`) and a task template's Task
 * tab (context `template`, `pages/templates/TemplateEditPage.tsx`) render these fields through this ONE
 * component, so a field added here reaches every surface with no extra work. It is CONTROLLED: the
 * host holds the `TaskSpec` (the wire keys) and owns every write — the create POST, the edit PUT, an
 * automation's save. What stays in the host: submit, auto-save, the breadcrumb's Title/Project/Feature,
 * Dependencies and From Jira (`task` only; O3 excludes both from an automation), and the form chrome.
 * The Depth → Involvement → Persona → Model hierarchy lives HERE, so no second copy of it can drift. */

/** What a selector targets (the C4 `TargetSelector`'s vocabulary). `nodes` adds the assignee note. */
export type TargetKind = 'projects' | 'nodes'

/** The task a surface will create, as the wire spells it. `model` and `bound_provider` use `''` for
 *  "inherit"/"Any" (`model_catalog.INHERIT`); `project_memory` null is Default; `title_template` and
 *  `feature_id` are automation-only (the task surface's Title and Feature are the breadcrumb's).
 *  `bound_provider` and `agent_id` are the one agent picker's two halves: at most one is set. `pinned_node_id` is
 *  the operator's node pin (null = Any), the task surface's alone. `account_id` is the New-task form's account
 *  pin (absent/null = Any), create only. */
export type TaskSpec = {
  persona: string
  body: string
  title_template: string | null
  model: string
  bound_provider: string
  agent_id: string | null
  pinned_node_id: string | null
  account_id?: string | null
  autonomous: boolean
  project_memory: boolean | null
  priority: number
  playbook_id: string | null
  transition_mode: string
  assignee_id: string | null
  attachment_ids: number[]
  feature_id: string | null
  hidden: boolean
}

/** The two axes and the playbook pill: UI state the spec cannot carry (`sketch` and a picked Prototyper
 *  write the same persona; `chat` and `guided` the same `autonomous` at `sketch`). */
export type TaskSpecAxes = { depth: TaskDepth; involvement: TaskInvolvement; playbook: TaskPlaybookRef | null }

/** Which seeded levels a press has taken over, so a host's late defaults seed never overwrites them. */
export type TaskSpecTouched = { persona?: true; autonomous?: true }

/** The pasted-image hooks of the task surface, which uploads (edit) or stages (create) against its row. */
export type TaskSpecEditor = {
  onUploadImage: (file: File, filename: string) => Promise<void>
  resolveAttachmentSrc: AttachmentResolver
  placeholder: string
  autoFocus: boolean
  /** A half-landed create's retry: the editor is paused, not editable. */
  paused: boolean
}

/* The model vocabulary lives ONLY in `apps/api/app/model_catalog.py`, projected as `GET /api/tasks/models`;
 * no model name is spelled here — a typo'd id is UNROUTABLE and fails hours later on an unwatched runner. */
export type TaskModelChoice = { value: string, label: string, description: string }

// The sentinel: empty means "inherit" (see `TaskUpdate.model`).
export const MODEL_INHERIT = ''

/* The one exception, spelled once so it stays greppable: the prototyper persona's SEED model tier (also resolved
 * by the `sketch` Depth). It is the charter's own default (apps/api/app/persona_registry.py), so seeding it
 * only shows the model the row would run on. A SEED, NEVER A LOCK (operator decision; the server no longer
 * refuses other pins — `test_task_model_field.py`). It may not grow a sibling; the closed set is still
 * `apps/api/app/model_catalog.py`. If it drifts, the select renders it as its own bare stored-value option,
 * visibly wrong before save. */
export const PROTOTYPER_MODEL_SEED = 'low'

/* The Advanced row's gutter, on each CELL: `ui/FieldRow` leaves `rowGap` undefined by contract, and `row-gap`
 * applies only BETWEEN wrapped lines, so it would drop the 12px under the last line. Shared by every cell. */
const ADVANCED_ROW_GUTTER = '0.75rem'
/* A read-only fact list (`dl` of `DetailField`s). One fact per line on a phone: there each field sets its label
   beside its value, and a wrapped row of pairs read as "Persona Coder Priority Normal". */
const FACTS_SX = {
  m: 0, display: 'flex', flexWrap: 'wrap', columnGap: '1.5rem', rowGap: '0.25rem', [PHONE]: { flexDirection: 'column' },
} as const

// The Model label's hover: the inherit precedence. A staged playbook's Model is a CEILING over every stage
// (the server caps each stage's own tier), relabelled Max model, with its own hover.
export const MODEL_INHERIT_HINT = 'Inherit uses stage default, then project default.'
export const MODEL_CEILING_HINT = "Caps every stage's model."
// The Persona label's hover (the served `personaPickerHint()` paragraph, cut to a tooltip).
export const PERSONA_HINT = 'Who picks it up; only some personas open pull requests.'

// The provider sentinel: empty means "Any" (see `TaskUpdate.bound_provider`), stored as NULL.
export const PROVIDER_ANY = ''

/* PROJECT MEMORY is `tasks.project_memory`, tri-state: Default stores null and is resolved at dispatch (the row's
 * own value, then its epic's, then the `project_memory_default` System Setting — `app/project_memory/switch.py`).
 * The select's values are strings, so Default is `''`. */
export const MEMORY_DEFAULT = ''
export function projectMemoryValue(stored: boolean | null | undefined): string {
  return stored == null ? MEMORY_DEFAULT : stored ? 'on' : 'off'
}
export function projectMemoryField(value: string): boolean | null {
  return value === MEMORY_DEFAULT ? null : value === 'on'
}
// The Default option names the value it stands for today, so "Default" never hides what will happen.
export function projectMemoryDefaultLabel(defaultOn: boolean): string {
  return `Default (${defaultOn ? 'On' : 'Off'})`
}
// An epic child's Default reads its epic's choice first (`switch.effective_memory`).
export const PROJECT_MEMORY_CHILD_HINT = 'Default follows the epic first.'

// DEBUG is the `transition_mode` column, inverted in presentation: ticked = 'hold' (dispatch nothing
// automatically, run each subtask by hand), clear = 'auto'.
export const DEBUG_HINT = 'On: each subtask waits for Run.'

// The automation context's own words: one line each, fewest words.
export const FEATURE_NEEDS_ONE_PROJECT = 'Feature needs exactly one target project.'
export const NODES_ASSIGNEE_NOTE = 'On nodes, a non-admin assignee falls back to unassigned.'

/** The surfaces this component serves (`TaskSpecFieldsProps.context`). */
export type TaskSpecContext = 'task' | 'automation' | 'template'

/* The width below which the one non-advanced line stops fitting its Jira lookup — MEASURED. Flex breaks on
 * each member's hypothetical size: 116 + 24 + 116 + 24 + 272 = 552px (the two axes above 600px, 1.5rem gaps,
 * the settings group's 17rem basis, measured with the since-removed help toggle). Plus the 212px sidebar and 24px column padding = 788px; 839 adds slack. Below
 * it the lookup goes to `Advanced` and the Attachments cell off the group (`stowInAdvanced`); the lookup keeps a 13rem floor
 * because narrower it stops reading as a field.
 * Its own constant, not `PHONE_QUERY` (a shared 480px token four other surfaces read). */
const AXES_ROW_TIGHT_QUERY = '(max-width:839px)'

/* `RichTextEditor`'s root (`[data-testid="rte"]`) goes FULLSCREEN (`position:fixed`) at ≤480px, right for a bare
 * editor, wrong inside a form: re-scoped to a plain flex-fill. The host's root applies it under its phone
 * breakpoint (`TaskEditForm`'s form root; this component's own wrapper in the `automation` context). */
export const PHONE_EDITOR_SX = {
  position: 'static',
  inset: 'auto',
  zIndex: 'auto',
  flex: 1,
  minHeight: 0,
  borderRadius: 2, // 8px at theme.shape.borderRadius's 4px unit.
} as const

/* Drop the markdown image referencing `token` — the ONE repair for a dangling `attachment:` ref.
 * `richtext/RichTextEditor` inserts the node and token BEFORE uploading, so a refused upload leaves a ref no
 * runner can materialize (nothing cross-checks body tokens against `task_attachments`). The body is put
 * back; `RichTextEditor`'s controlled-value effect removes the node (the cursor moves — an error path).
 * Matched by equality on the closing paren, never `includes('attachment:' + token)` — that is a prefix test
 * (`attachment:log.txt.gz` matches `log.txt`), a real shipped defect in `TaskAttachmentsField`.
 * The alt class admits an escaped bracket: prosemirror-markdown writes `[`/`]` as `\[`/`\]`, so `[^\]]*`
 * could not strip `photo[1].png`. `\\.|[^\]\\]` (an escaped pair, or neither an unescaped `]` nor a
 * backslash) anchors on `](attachment:` yet cannot run past its own node into a neighbour (the lazy
 * `[\s\S]*?` would delete both). Asserted both ways.
 * The repetition is BOUNDED (a ReDoS fix: unbounded is O(n²), ~17s on 195 KB of `![`, and reachable across
 * principals). 512 counts REPETITIONS (an escaped pair is one), so it admits 1024 chars; the worst legal
 * name is 255 escaped pairs (`app/routes/attachments.py::MAX_FILENAME_LENGTH`), so it errs permissive.
 * The token side needs no bound: it is escaped and always `img-<nanoid>.<ext>`. */
export function stripAttachmentImage(markdown: string, token: string): string {
  const quoted = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return markdown.replace(
    new RegExp(`!\\[(?:\\\\.|[^\\]\\\\]){0,512}\\]\\(attachment:${quoted}\\)`, 'g'), '',
  )
}

/* The over-cap sentence for a PASTED IMAGE, which differs from `TaskAttachmentsField`'s `overCapMessage`:
 * the node is already in the Description and is about to be removed. The cap and formatting are shared
 * (`lib/maxFileBytes`, `lib/formatBytes`). */
export function imageOverCapMessage(file: File, maxBytes: number): string {
  return `${file.name || 'image'} (${formatBytes(file.size)}) is over the `
    + `${formatBytes(maxBytes)} limit — it was removed from the description.`
}

/** The axes a spec opens on, when nothing better is known (an automation's first render). */
export function taskSpecAxesFor(spec: TaskSpec, playbook: TaskPlaybookRef | null = null): TaskSpecAxes {
  return {
    depth: depthForPersona(spec.persona),
    involvement: involvementForTask(spec.persona, spec.autonomous),
    playbook,
  }
}

/* ── WHICH CONTROLS A SPEC SHOWS, AND SO SUBMITS ──────────────────────────────────────────────────────────────
 * One home for the render gates a host's payload must agree with (shown ⇔ submitted).
 * `dispatchedRow`: is this row one a runner is handed? The per-dispatch field (the model pin) is read by the
 * runner off its OWN row; the epic container is the only row never dispatched (`persona` in `types.ts`).
 * Off the LIVE persona, so pressing `epic` withdraws the controls and their keys. FULL AUTO IS NOT
 * IN THAT SET: it follows ownership (`showsAutonomous`).
 * `showsModelPin`: `dispatchedRow` AND an org admin (`_model_pin_or_refuse`). NOT access control:
 * `update_task` refuses `model` from anyone else; it keeps the console from showing a control whose save would
 * 403. A staged playbook is not dispatched itself, but its Model is the ceiling every stage obeys, so it keeps
 * it.
 * `showsAgentPicker`: `dispatchedRow` AND a task writer (`useCanPickAgent`, DD13) — the picker's two halves are
 * written under the route's own `tasks:create`/`tasks:edit`, never an admin tier. An epic is never dispatched,
 * and the server ignores a pick on one. */
export function taskSpecGates(
  persona: string, playbook: TaskPlaybookRef | null, isOrgAdmin: boolean, childRow = false, canPickAgent = false,
): {
  playbookComplex: boolean; dispatchedRow: boolean; showsModelPin: boolean; showsAgentPicker: boolean
  showsAutonomous: boolean
} {
  const playbookComplex = playbook !== null && !playbook.simple
  const dispatchedRow = !isEpicPersona(persona) && !playbookComplex
  return {
    playbookComplex,
    dispatchedRow,
    showsModelPin: (dispatchedRow || playbookComplex) && isOrgAdmin,
    showsAgentPicker: dispatchedRow && canPickAgent,
    /* The Full Auto switch follows OWNERSHIP, not dispatch: an epic CHILD owns none (its column is inert in
     * both arms of `_task_effective_autonomous`). */
    showsAutonomous: !childRow,
  }
}

/** Is the Node select offered? The task surface only (an automation pins through a node target), to an operator
 *  (`Admin.is_operator`) or to anyone while their org's node picker is on (`Admin.task_node_pin_enabled`; the
 *  server's own gate is `_may_pin_node_or_403`), on a row that dispatches (an epic never places). */
export function taskNodePinShown(
  context: TaskSpecContext,
  admin: Pick<Admin, 'is_operator' | 'task_node_pin_enabled'> | null | undefined,
  dispatchedRow: boolean,
): boolean {
  return context === 'task' && (admin?.is_operator === true || admin?.task_node_pin_enabled === true) && dispatchedRow
}

/* A read-only picker (Assigned to; the Advanced row's pickers): no caret, so a set value reads as a value
 * rather than a closed picker. The disabled state already dims it. */
const READ_ONLY_LOOKUP_SX = {
  '& .MuiAutocomplete-endAdornment, & .MuiNativeSelect-icon, & .MuiSelect-icon': { display: 'none' },
} as const

/* `GET /api/access/members` rows (`MemberOut`): only the principal's identity is read. `principal.name` is
 * the server-derived label ("First Last", else the email — `apps/api/app/identities.py::person_label`),
 * carried whole as `first_name` so `memberName` prints it unchanged. A group row is not an assignee. */
type OrgMemberRow = { principal: { kind: string; id: string; name: string } }

/* ── THE ASSIGNEE ROSTER ──────────────────────────────────────────────────────────────────────────────────────
 * `GET /api/projects/{id}/members` for each target project, merged by id; with no project, the org's members
 * (`GET /api/access/members?scope=org:<id>`). Re-fetched when the targets move; filtered in memory (the routes
 * take no query). `lib/cachedRead` in an effect, not `useQuery`: the task form mounts without a `QueryClient` in
 * several hosts, and remounts on every own-task event — a remount answers from cache (`['project-members', id]` is
 * `ProjectEditPage`'s own key). Reset to empty when the targets move, so an old roster is never offered; "empty"
 * means not yet known.
 * The load promise is kept: `AsyncLookup` fetches once per focus, so its fetcher awaits the load rather than
 * answering from an empty list. A failed fetch is logged and reads as empty. */
function useAssigneeRoster(projectIds: number[], orgId: string | null) {
  const [members, setMembers] = useState<ProjectMember[]>([])
  const load = useRef<Promise<ProjectMember[]>>(Promise.resolve([]))
  const projectKey = projectIds.join(',')
  useEffect(() => {
    setMembers([])
    const ids = projectKey === '' ? [] : projectKey.split(',')
    if (ids.length === 0 && orgId === null) {
      load.current = Promise.resolve([])
      return
    }
    let live = true
    const reads: Promise<ProjectMember[]>[] = ids.length > 0
      ? ids.map(id => cachedRead(['project-members', Number(id)], () => get<ProjectMember[]>(`/api/projects/${id}/members`), ROSTER_STALE_MS)
        .then(rows => (Array.isArray(rows) ? rows : [])))
      : [cachedRead(['org-members', orgId], () => get<OrgMemberRow[]>(`/api/access/members?scope=${encodeURIComponent(`org:${orgId}`)}`), ROSTER_STALE_MS)
          .then(rows => (Array.isArray(rows) ? rows : [])
            .filter(r => r.principal?.kind === 'user')
            .map(r => ({ id: r.principal.id, email: '', first_name: r.principal.name, last_name: null })))]
    const merged = Promise.all(reads.map(read => read.catch((err: unknown) => {
      console.error('the project member list could not be loaded:', err)
      return [] as ProjectMember[]
    })))
      .then(lists => {
        const byId = new Map<string, ProjectMember>()
        for (const m of lists.flat()) if (!byId.has(m.id)) byId.set(m.id, m)
        return [...byId.values()]
      })
    load.current = merged
    void merged.then(rows => { if (live) setMembers(rows) })
    return () => { live = false }
  }, [projectKey, orgId])
  return { members, load }
}

/* ── FEATURE — the automation context's own field (the task surface files from its breadcrumb) ─────────────────
 * A feature belongs to ONE project, so the field exists only when the target resolves to exactly one; its own
 * component because `useFilingFeatures` needs a `QueryClient`, which the task surface's hosts may lack. */
function FeatureField({
  projectId, value, onChange, disabled, error,
}: {
  projectId: number
  value: string | null
  onChange: (next: string | null) => void
  disabled: boolean
  error?: string
}) {
  const { features } = useFilingFeatures(projectId)
  return (
    <FormTextField
      label="Feature"
      sx={FIELD_SX}
      id="task-feature"
      select
      slotProps={{ select: { inputProps: { 'aria-label': 'Feature' } }, inputLabel: { shrink: true } }}
      value={value ?? ''}
      disabled={disabled}
      error={error}
      onChange={v => onChange(v === '' ? null : v)}
      fullWidth
      options={(features ?? []).map(f => ({ value: f.id, label: f.name }))}
    >
      <option value="">No feature</option>
      {/* A stored feature the list lacks (not loaded yet, or shipped) stays as its own option. */}
      {value !== null && !(features ?? []).some(f => f.id === value) && <option value={value}>{value}</option>}
    </FormTextField>
  )
}

export type TaskSpecFieldsProps = {
  value: TaskSpec
  onChange: (next: TaskSpec) => void
  /** `task`: the console's create/edit form. `automation`: an automation's Task action (no Dependencies, no
   *  From Jira; Assignee optional; Hidden in Advanced; Feature and Title template its own). `template`: a task
   *  template's Task tab — what a template carries (`routes/task_templates.py::TemplateIn`) and nothing else: Depth,
   *  Involvement, Persona and Model; no project, feature, assignee, playbook, attachment chips or per-task settings
   *  (N-6). Pasted images stage as the automation's do; the host uploads them once the row exists. */
  context: TaskSpecContext
  /** The projects the task will land in: the Feature field needs exactly one; the assignee roster reads them. */
  projectIds: number[]
  /** The org whose members are offered when no project is targeted (a node target). */
  orgId: string | null
  targetKind?: TargetKind
  /** The host's invalid-state messages, per wire key, shown under the field that owns it. */
  errors?: Partial<Record<keyof TaskSpec, string>>
  /** An event automation's template help (`{{body.x}}` …): shows it, and the Title template field. */
  templateHint?: string
  /** Picked files not yet uploaded; the host uploads them once it has a row to hang them off. */
  stagedFiles: File[]
  /** A staging context's (`automation`, `template`) painter for a body image already STORED on the row: a token
   *  no staged file names goes here (the template editor's own assets). Absent, such a token is unpaintable. */
  resolveStoredSrc?: AttachmentResolver
  onStagedFilesChange: (files: File[]) => void
  readOnly?: boolean
  /** A write in flight: controls stay (no reflow) but refuse. */
  disabled?: boolean
  /** The operator's `task_default_*` settings: what the `task` Depth writes. */
  createDefaults?: TaskCreateDefaults
  /** The target project's `default_model`, seeded by the `task` Depth. */
  defaultModel?: string | null
  /** False words the Task depth without a PR. */
  hasRepo?: boolean
  /** An automation's stored playbook (the pill needs its name); the task context passes `axes` instead. */
  initialPlaybook?: TaskPlaybookRef | null
  // ── The `task` context's own wiring (`TaskEditForm`); an automation omits all of it. ──
  /** Controlled axes; omitted, this component holds them, seeded from `value`. */
  axes?: TaskSpecAxes
  onAxesChange?: (next: TaskSpecAxes, touched: TaskSpecTouched) => void
  /** May the playbook still be chosen (a create, or a not-yet-Saved row)? */
  playbookOpen?: boolean
  /** An epic child: no Persona, no Full Auto, no Debug (the server's own rules). */
  childRow?: boolean
  /** The task context's Hidden gate (an existing top-level row its principal may hide). */
  showsHidden?: boolean
  /** The row the attachments hang off; `null` stages. */
  attachmentsTaskId?: number | null
  /** The row's stored assignee, so the lookup never shows a bare id while the roster loads. */
  storedAssignee?: { id: string; label: string | null }
  /** Where a create's assignee falls back when the roster lacks them (the crumb moved the project). */
  assigneeFallback?: string | null
  /** The link-seeded persona, said on the main row. */
  personaChipLabel?: string
  /** About THIS press: the server's refusal of a conversion, or what it will cost. */
  personaNote?: string
  editor?: TaskSpecEditor
  /** From Jira, placed by this component: in the settings row, or `stowed` into Advanced. */
  jiraField?: (stowed: boolean) => ReactNode
  /** The prerequisite panel, at the end of Advanced. */
  dependsOn?: ReactNode
  /** May this principal write the agent pick HERE? The host that knows the project passes the project-scoped
   *  answer (`useCanOnProject`); absent, any grant counts (`useCanPickAgent`). */
  canPickAgent?: boolean
  /** Offer the Account select (`tasks.bound_account_id`): the task form's create, on a row that dispatches. */
  showsAccountPicker?: boolean
  /** The org whose accounts the Account select lists; null lists every account the roster returns. */
  accountOrgId?: string | null
  /** Another org's row (an operator's, `ui/OrgIndicator.tsx::useOtherOrg`): its agents are read as that org. */
  org?: OrgRef | null
}

// Minted once per staged File (the attachments row keys its chips by it).
const stagedKeys = new WeakMap<File, string>()
function stagedEntry(file: File): StagedAttachment {
  let key = stagedKeys.get(file)
  if (key === undefined) {
    key = nanoid()
    stagedKeys.set(file, key)
  }
  return { key, file }
}

export default function TaskSpecFields({
  value,
  onChange,
  context,
  projectIds,
  orgId,
  targetKind,
  errors = {},
  templateHint,
  stagedFiles,
  onStagedFilesChange,
  resolveStoredSrc,
  readOnly = false,
  disabled = false,
  createDefaults,
  defaultModel = null,
  hasRepo = true,
  initialPlaybook = null,
  axes: axesProp,
  onAxesChange,
  playbookOpen = true,
  childRow = false,
  showsHidden: showsHiddenProp = false,
  attachmentsTaskId = null,
  storedAssignee,
  assigneeFallback,
  personaChipLabel,
  personaNote,
  editor,
  jiraField,
  dependsOn,
  canPickAgent: canPickAgentProp,
  showsAccountPicker = false,
  accountOrgId = null,
  org = null,
}: TaskSpecFieldsProps) {
  const automation = context === 'automation'
  const template = context === 'template'
  /* No row to upload to while the form is filled: pasted images STAGE for the host (below). */
  const staging = automation || template
  const locked = disabled || readOnly
  /* The public Live Cast viewer (`lib/viewerMode.ts`) sees the task as a console reader does — every axis and
     Advanced, read-only by permission — minus the people fields: Assigned to stays hidden at every width. */
  const viewer = useViewerMode()
  const { admin } = useAuth()
  const isOrgAdmin = isOrgAdminSeat(admin)
  const canCreateChats = useCanCreateChats()
  const playbooksEnabled = usePlaybooksEnabled()

  /* The axes: the host's when it controls them (the task form seeds them from its row and its defaults),
   * else this component's own, seeded once from the spec. */
  const [ownAxes, setOwnAxes] = useState<TaskSpecAxes>(() => taskSpecAxesFor(value, initialPlaybook))
  const axes = axesProp ?? ownAxes
  const { depth, involvement, playbook } = axes
  const setAxes = (next: TaskSpecAxes, touched: TaskSpecTouched) => {
    if (axesProp === undefined) setOwnAxes(next)
    onAxesChange?.(next, touched)
  }
  const persona = value.persona
  const canPickAgentAnywhere = useCanPickAgent()
  const canPickAgent = canPickAgentProp ?? canPickAgentAnywhere
  const { playbookComplex, dispatchedRow, showsModelPin, showsAgentPicker: taskAgentPicker, showsAutonomous } =
    taskSpecGates(persona, playbook, isOrgAdmin, childRow, canPickAgent)
  /* An automation's Task action stores only `bound_provider` (`automation_actions.py`): there the picker keeps
   * the built-in half under the model pin's old gate, and offers no agent it could not save. */
  /* A reader who may not write the pick still SEES a stored one (the task view's agent label, with its warning
   * when the agent's last refresh failed), read-only; the host submits only under `taskAgentPicker`. */
  const hasAgentPick = value.agent_id !== null || value.bound_provider !== PROVIDER_ANY
  /* A template carries no agent pick (`TemplateIn`): Persona and Model are its whole "who runs it". */
  const showsAgentPicker = template ? false : automation
    ? showsModelPin && !playbookComplex
    : taskAgentPicker || (dispatchedRow && hasAgentPick)

  /* The row width in JS — a placement (which row the Jira lookup and Attachments render into;
   * `stowInAdvanced`), not a style. `AXES_ROW_TIGHT_QUERY`, not the shared `PHONE_QUERY`. `false` under jsdom's
   * `matchMedia` stub; the narrow placement is tested with `vi.stubGlobal`. Below the measured break the
   * one-line rule cannot hold, and the lookup leaves: it is the only member with a second home. */
  const stowInAdvanced = useMediaQuery(AXES_ROW_TIGHT_QUERY)

  // Who may change the persona: mirrors the server guard (`_apply_persona_conversion`) — top-level rows only.
  const showsPersonaPicker = !childRow
  const showsHidden = automation || showsHiddenProp

  /* The model catalogue, fetched once the control is on screen (the agent picker fetches its own), through
   * `lib/cachedRead` under `ProjectEditPage`'s key, so a remount answers from cache. A failed
   * fetch still renders the inherit option and a stored pin, so the pin can be read and cleared; logged, not
   * put in an error banner (nothing the operator did failed). */
  const models = useApiRead(['task-spec-models'], () => cachedRead(['task-models'], () => get<TaskModelChoice[]>('/api/tasks/models'), CATALOGUE_STALE_MS).then(
    rows => (Array.isArray(rows) ? rows : null),
    (err: unknown) => { console.error('the model catalogue could not be loaded:', err); throw err },
  ), 'the model catalogue could not be loaded', { enabled: showsModelPin })
  const modelChoices: readonly TaskModelChoice[] = models.data ?? []

  /* READ-ONLY ADVANCED is a list of facts, not dead inputs: the task context's read-only form (another
   * org's row) shows each set value as text. The stored agent's NAME comes from the same cached catalogues
   * `AgentPicker` reads, under its keys; another org's row (`org`) reads them as that org. A refused read
   * falls back to a word or the provider. */
  const factsOnly = readOnly && !staging
  // An agent the roster does not name: a word, never an id fragment.
  const agentFallback = value.agent_id !== null ? 'Custom agent' : value.bound_provider
  const rowOrgId = org?.id ?? null
  const orgKeyed = (...key: unknown[]) => (rowOrgId === null ? key : [...key, rowOrgId])
  const agentName = useApiRead(['task-spec-agent-name', value.agent_id, value.bound_provider, rowOrgId], () => (value.agent_id !== null
    ? cachedRead(orgKeyed('agents-list'), () => listAgents(org), ROSTER_STALE_MS)
      .then(rows => (Array.isArray(rows) ? rows.find(a => a.id === value.agent_id)?.display_name : undefined))
    : cachedRead(orgKeyed('agent-providers'), () => get<ProviderChoice[]>('/api/accounts/providers', ...readAsOnce(org)), CATALOGUE_STALE_MS)
      .then(rows => (Array.isArray(rows) ? rows.find(p => p.value === value.bound_provider)?.label : undefined))
  ).then(name => name ?? null, () => null), 'the agent could not be named', {
    enabled: factsOnly && showsAgentPicker && hasAgentPick,
  }).data ?? agentFallback

  /* THE NODE PIN (`tasks.pinned_node_id`): under `taskNodePinShown`, on a row that dispatches, offered over the nodes its one
   * project runs on (`GET /api/projects/{id}/nodes`). Others see a stored pin only as a read-only fact. The server
   * refuses anyone else (`_pinned_node_or_refuse`); the host submits it under `showsNodePin` alone. */
  const nodeProjectId = projectIds.length === 1 ? projectIds[0] : null
  const hasNodePin = value.pinned_node_id !== null
  const showsNodePin = taskNodePinShown(context, admin, dispatchedRow)
  const nodeChoices = useApiRead(['task-spec-node-choices', nodeProjectId], () => (nodeProjectId === null
    ? Promise.resolve([])
    : cachedRead(['task-node-choices', nodeProjectId], () => get<AssignmentsResponse>(assignmentsPath('project', nodeProjectId)), ROSTER_STALE_MS)
      .then(r => (Array.isArray(r?.assignments) ? r.assignments.map(a => ({ id: a.node_id, name: a.node_name })).sort(byName) : []))
  ), 'the project\'s nodes could not be loaded', {
    enabled: (showsNodePin || (factsOnly && dispatchedRow && hasNodePin)) && nodeProjectId !== null,
  }).data ?? []
  /* THE ACCOUNT PIN (`tasks.bound_account_id`), create only: Any, or one enabled account of the project's org. A
   * provider pin narrows the list to its vendor; the server refuses a mismatch (`_account_pick_or_422`). A roster the
   * caller cannot read lists nothing, leaving Any. */
  const accountChoices = useApiRead(['task-spec-accounts'], () => cachedRead(['accounts'], listAccounts, ROSTER_STALE_MS)
    .then(rows => (Array.isArray(rows) ? rows : [])), 'the accounts could not be loaded', {
    enabled: showsAccountPicker,
  }).data ?? []
  // The current pick always stays listed: the value must never disagree with an option.
  const accountOptions = accountChoices.filter(a => a.id === value.account_id || (a.enabled
    && (accountOrgId === null || a.org?.id === accountOrgId)
    && (value.bound_provider === '' || a.provider === value.bound_provider)))
  // A stored node the list lacks (not loaded, unassigned since, or unreadable here): a word, never an id fragment.
  const nodeName = nodeChoices.find(n => n.id === value.pinned_node_id)?.name ?? 'Pinned node'

  const projectMemoryDefault = resolveProjectMemoryDefault(admin?.task_create_defaults)
  // The org's plan switch (`lib/planSwitches.ts`): off, the field is not offered; the stored value rides untouched.
  const projectMemoryOn = planSwitchOn(admin, 'project_memory_enabled') && !template

  /* ── ASSIGNED TO ──────────────────────────────────────────────────────────────────────────────────────────── */
  const { members, load: membersLoad } = useAssigneeRoster(projectIds, orgId)
  const assigneeId = value.assignee_id ?? ''
  /* A refused clear re-asserts the value: `ui/AsyncLookup` echoes MUI's clear into its box and restores only
   * on a value IDENTITY change, so ignoring `onChange(null)` would leave a blank over a held id ("nobody").
   * Bumping this hands the lookup a fresh object with the same key and label. Never submitted or rendered. */
  const [clearRefusals, setClearRefusals] = useState(0)

  /* An assignee the (landed, non-empty) roster lacks falls back (`assigneeFallback`): the crumb may have moved a
   * create to another project. Without a fallback the value stands — the stored assignee is a fact, and a list
   * lacking them is the reader's narrowed view (see `ProjectEditPage`'s `MemberSelectField`). */
  useEffect(() => {
    if (assigneeFallback === undefined || members.length === 0) return
    if (!members.some(m => m.id === assigneeId)) {
      const fallback = assigneeFallback === '' ? null : assigneeFallback
      if (fallback !== value.assignee_id) onChange({ ...value, assignee_id: fallback })
    }
    // `value`/`onChange` are read for the write, not watched: the roster or the id moving is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assigneeFallback, members, assigneeId])

  /* The lookup's option, DERIVED from the id. Never blank over a set column (it reads as "nobody"). Fallbacks:
   * the roster's naming; the ROW's stored label while the id is the row's — the PERSON alone, without the API's
   * ", acting as {org}" (`lib/authorBadge.ts::personOf`); the PRINCIPAL's name when it is theirs; the
   * bare id. */
  const assignedToOption = useMemo<LookupOption | null>(() => {
    if (assigneeId === '') return null
    const member = members.find(m => m.id === assigneeId)
    if (member !== undefined) return memberOption(member)
    const label = storedAssignee?.id === assigneeId && storedAssignee.label
      ? personOf(storedAssignee.label)
      : admin !== null && admin.id === assigneeId ? memberName(admin) : assigneeId
    return { key: assigneeId, label }
    // `clearRefusals` is a dependency and not an input, on purpose — see its declaration.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assigneeId, members, storedAssignee?.id, storedAssignee?.label, admin, clearRefusals])

  /* A write from an EFFECT builds on the latest spec, not this render's: two effects firing in one commit (a
   * dropped feature and a cleared Title template) must not undo each other. */
  const latestValueRef = useRef(value)
  latestValueRef.current = value
  const patchValue = (patch: Partial<TaskSpec>) => {
    latestValueRef.current = { ...latestValueRef.current, ...patch }
    onChange(latestValueRef.current)
  }

  /* ── FEATURE: one project or none ───────────────────────────────────────────────────────────────────────────
   * A feature filed against several (or no) projects would name a feature of only one of them: when the target
   * stops being exactly one project, a held feature is dropped rather than submitted. */
  const featureProjectId = projectIds.length === 1 ? projectIds[0] : null
  // The project the held feature was picked under: a move to ANOTHER single project drops it too.
  const featureOwnerRef = useRef(featureProjectId)
  useEffect(() => {
    const moved = featureOwnerRef.current !== featureProjectId
    featureOwnerRef.current = featureProjectId
    if (automation && (featureProjectId === null || moved) && value.feature_id !== null) {
      patchValue({ feature_id: null })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [automation, featureProjectId, value.feature_id])

  /* The Title template exists only while the action is event-driven: one left behind by a switch to a schedule
   * would be submitted with no field to show it. */
  useEffect(() => {
    if (automation && templateHint === undefined && value.title_template !== null) {
      patchValue({ title_template: null })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [automation, templateHint, value.title_template])

  /* Does the Involvement axis offer `chat`? The grant (`useCanCreateChats`: `worktrees:edit` plus
   * `tasks:create`; the control must carry it, or an ungranted principal 403s on save) and the depth (`epic`
   * limits the axis to `unattended`/`guided` — a limit, not a disable). `|| involvement === 'chat'` so an
   * EDIT always renders its own current position. */
  const offersChatPosition =
    (canCreateChats && depth !== 'epic' && !playbookComplex) || involvement === 'chat'

  /* ── THE HIERARCHY: Depth → Involvement → Persona → Model ─────────────────────────────────────────────────
   * A press writes ONLY what is BELOW it; there is no upward edge anywhere.
   * DEPTH seeds all three: SKETCH — `unattended`, `prototyper`, Haiku (all seeds). TASK — the operator's
   * configured defaults (`task_default_autonomous`, `task_default_persona` — Coder shipped — and the
   * PROJECT's `default_model`). EPIC — persona `'epic'` and `guided`; never dispatched, so both selects are
   * WITHHELD (Model via `showsModelPin`; Persona via `!isEpicPersona(persona)` at its render site, not
   * `showsPersonaPicker`).
   * INVOLVEMENT writes `autonomous` via the axis, and below it only `chat` at `task` writes the advisor
   * persona. Leaving the chat restores nothing.
   * The model seed has two callers (Depth press, Persona picker). `isOrgAdmin` on every arm: `model` is
   * an org admin's field, and a seeded value would otherwise ride anyone else's save.
   * The pair SEEDS, never locks: a persona the depth did not resolve to simply stands; do not reintroduce a
   * lock. */
  const depthModelSeed = (next: TaskDepth): string | undefined => {
    if (!isOrgAdmin) return undefined
    if (next === 'sketch') return PROTOTYPER_MODEL_SEED
    if (next === 'task') return defaultModel ?? MODEL_INHERIT
    return undefined
  }

  /* The configured default persona, fenced, as `task` writes it. Absent (defaults not landed, or an edit) →
   * the shipped `DEFAULT_PERSONA`, so on edit a Depth press is a deliberate reset below. `personaForDepth`
   * structurally refuses `epic` and `advisor`. */
  const taskPersona = personaForDepth('task', createDefaults?.persona ?? DEFAULT_PERSONA)

  /* Every arm takes over both levels: it writes both. Also the way back from a playbook (the pill's menu), so
   * it drops one. */
  const applyDepth = (next: TaskDepth) => {
    const nextInvolvement: TaskInvolvement = next === 'epic' ? 'guided'
      : next === 'sketch' ? 'unattended'
        : (createDefaults?.autonomous ?? true) ? 'unattended' : 'guided'
    const nextPersona = next === 'task' ? taskPersona : personaForDepth(next, taskPersona)
    const model = depthModelSeed(next)
    setAxes({ depth: next, involvement: nextInvolvement, playbook: null }, { persona: true, autonomous: true })
    onChange({
      ...value,
      persona: nextPersona,
      autonomous: autonomousForInvolvement(nextInvolvement),
      playbook_id: null,
      ...(model !== undefined ? { model } : {}),
    })
  }

  /* A playbook pick, from the fourth segment or the pill. It writes the persona below it, as a Depth press does;
   * Involvement stands (only a staged one, epic-shaped, has no chat position to stand on). */
  const applyPlaybook = (next: PlaybookOut) => {
    const ref: TaskPlaybookRef = { id: next.id, name: next.name, simple: next.simple }
    if (!next.simple) {
      const leavesChat = involvement === 'chat'
      const nextInvolvement: TaskInvolvement = leavesChat ? 'guided' : involvement
      setAxes({ depth, involvement: nextInvolvement, playbook: ref },
        leavesChat ? { persona: true, autonomous: true } : { persona: true })
      onChange({
        ...value, persona: 'epic', playbook_id: ref.id, autonomous: autonomousForInvolvement(nextInvolvement),
      })
      return
    }
    /* The playbook's own persona, refused as `personaForDepth` refuses (and the advisor without the grant). */
    const seeded = next.default_persona
    setAxes({ depth, involvement, playbook: ref }, { persona: true })
    onChange({
      ...value,
      playbook_id: ref.id,
      persona: seeded && !isEpicPersona(seeded) && (seeded !== ADVISOR_PERSONA || canCreateChats)
        ? seeded
        : taskPersona,
    })
  }

  /* An Involvement press writes the axis and, at `task` + chat, the persona; never the Depth.
   * The advisor at `unattended` (`persona: 'advisor', autonomous: true`) is a LEGAL row by operator decision
   * ("Persona should not limit anything … each of the parameters just adds its own piece or prompt"), not
   * normalised on the way out (the server's `create_autonomous_for_persona` returns it unchanged). Deferred
   * follow-up: the recorded decision on Involvement pairs no longer covers this one the console can produce —
   * update that record or normalise server-side. */
  const applyInvolvement = (next: TaskInvolvement) => {
    const writesChat = next === 'chat' && depth === 'task'
    setAxes({ depth, involvement: next, playbook }, writesChat ? { persona: true, autonomous: true } : { autonomous: true })
    onChange({
      ...value,
      autonomous: autonomousForInvolvement(next),
      ...(writesChat ? { persona: ADVISOR_PERSONA } : {}),
    })
  }

  /* ── PASTED IMAGES in the staging contexts (automation, template) ─────────────────────────────────────────
   * There is no row to upload to yet, so a pasted image is STAGED as a file under its minted token name: the
   * host uploads it with the other staged files, and the body's `attachment:<token>` then names a stored
   * attachment (an automation's attachment; a template's body image asset). Painted from the staged bytes
   * meanwhile; a token no staged file names is a stored one (`resolveStoredSrc`). The task context brings its
   * own (`editor`). */
  /* The refs are the LATEST, not this render's: `RichTextEditor` uploads a multi-image paste in one synchronous
   * loop, before any re-render, so each staged write and the body it strips must build on the one before. */
  const valueRef = useRef(value)
  valueRef.current = value
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const onStagedFilesChangeRef = useRef(onStagedFilesChange)
  onStagedFilesChangeRef.current = onStagedFilesChange
  const stagedFilesRef = useRef(stagedFiles)
  const resolveStoredSrcRef = useRef(resolveStoredSrc)
  resolveStoredSrcRef.current = resolveStoredSrc
  // Read by the memoised editor, which outlives a render (as `stagedFilesRef`).
  const maxFileBytes = useMaxFileBytes()
  const maxFileBytesRef = useRef(maxFileBytes)
  maxFileBytesRef.current = maxFileBytes
  stagedFilesRef.current = stagedFiles
  // The body as last typed, set by the editor's own `onChange` ahead of the host's re-render.
  const bodyRef = useRef(value.body)
  bodyRef.current = value.body
  // The tokens this component staged for pasted images: the Description's, never a chip.
  const pastedTokensRef = useRef<Set<string>>(new Set())
  const [imageError, setImageError] = useState('')
  const paintedRef = useRef<Map<string, Promise<string>>>(new Map())
  useEffect(() => () => {
    for (const url of paintedRef.current.values()) void url.then(releasePaintedUrl, () => {})
    paintedRef.current.clear()
  }, [])
  const stageFiles = (next: File[]) => {
    stagedFilesRef.current = next
    onStagedFilesChangeRef.current(next)
  }
  const ownEditor = useMemo<TaskSpecEditor>(() => ({
    onUploadImage: async (file: File, filename: string) => {
      if (file.size > maxFileBytesRef.current) {
        setImageError(imageOverCapMessage(file, maxFileBytesRef.current))
        bodyRef.current = stripAttachmentImage(bodyRef.current, filename)
        onChangeRef.current({ ...valueRef.current, body: bodyRef.current })
        return
      }
      setImageError('')
      pastedTokensRef.current.add(filename)
      stageFiles([...stagedFilesRef.current, new File([file], filename, { type: file.type || 'application/octet-stream' })])
    },
    resolveAttachmentSrc: async (token: string) => {
      const cached = paintedRef.current.get(token)
      if (cached) return cached
      const file = stagedFilesRef.current.find(f => f.name === token)
      if (!file) {
        if (resolveStoredSrcRef.current) return resolveStoredSrcRef.current(token)
        throw new Error(`no staged image for token: ${token}`)
      }
      const url = paintableImageUrl(file, file.type)
      paintedRef.current.set(token, url)
      return url
    },
    placeholder: 'What should the task do?',
    autoFocus: false,
    paused: false,
    // The callbacks read refs, so one identity serves every render (tiptap reads the resolver once).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [])
  const descriptionEditor = editor ?? ownEditor

  /* A Description edit. In a staging context a pasted image whose node was deleted leaves the staged set too —
   * no saved row would reference it, so it must not be uploaded. */
  const changeBody = (body: string) => {
    bodyRef.current = body
    onChange({ ...value, body })
    if (!staging) return
    const orphaned = stagedFilesRef.current.filter(
      f => pastedTokensRef.current.has(f.name) && !body.includes(`attachment:${f.name}`),
    )
    if (orphaned.length > 0) {
      for (const f of orphaned) pastedTokensRef.current.delete(f.name)
      stageFiles(stagedFilesRef.current.filter(f => !orphaned.includes(f)))
    }
  }

  /* Staged files as the attachments row wants them: a pasted image is the Description's, so it has no chip, as
   * `TaskAttachmentsField` hides a stored one the body references. */
  const isPasted = (file: File) => pastedTokensRef.current.has(file.name)
  const chipFiles = staging ? stagedFiles.filter(f => !isPasted(f)) : stagedFiles
  const onStagedChange = (next: StagedAttachment[]) => {
    const kept = staging ? stagedFilesRef.current.filter(isPasted) : []
    stageFiles([...next.map(s => s.file), ...kept])
  }

  /* ── ATTACHMENTS ──────────────────────────────────────────────────────────────────────────────────────
   * A control on the task's own line (an Attach button and a chip per file), not a panel. Built ONCE into the
   * `stowInAdvanced` slot, like From Jira, since a hidden twin would hold its own list and upload with
   * invisible errors. Crossing slots remounts it: a quiescent row loses nothing (the staged files live with the
   * host; edit re-reads server truth), but mid-upload the new instance's read can beat the old POST, leaving a
   * stored file chipless until the next Attach or remove — accepted. `attachmentsTaskId` picks the mode: an id
   * uploads on pick, `null` stages for the host. */
  /* Whether the field shows any chip: below the stow width a task WITH files shows them on the main form, not
  folded into Advanced — they are what the task is about, and their Live Cast controls ride on them. */
  const [hasFiles, setHasFiles] = useState(false)
  /* A template's images are its card and its Description's pasted ones; it attaches no files (N-6). */
  const showsAttachments = !template
  /* Below the stow width with no file, Attach is the paperclip alone at the end of the axes line: stowed in
   * Advanced, the main form gave no way to add one. */
  const attachmentsOnAxes = showsAttachments && stowInAdvanced && !hasFiles
  const attachments = (
    <TaskAttachmentsField
      taskId={attachmentsTaskId}
      // Read ONLY to tell an attached FILE from a pasted description image (also stored as an attachment), which
      // gets no chip. The test lives in the component.
      body={value.body}
      staged={chipFiles.map(stagedEntry)}
      onStagedChange={onStagedChange}
      onHasFilesChange={setHasFiles}
      readOnly={readOnly}
      disabled={locked}
      compact={attachmentsOnAxes}
    />
  )

  /* ── ASSIGNED TO ──────────────────────────────────────────────────────────────────────────────────────
   * "To the right of the Attach": built beside `attachments` into the same slots (`stowInAdvanced`), ONE
   * instance; the value lives with the host, so a rotation loses at most a half-typed query.
   * A LOOKUP, not a `<select>`: `minChars={0}` + `fetchOnFocus` show the whole roster on focus and narrow as
   * you type, over `memberLabel` (both halves); the `AbortSignal` is unused.
   * The task context REFUSES a clear: a null `assigned_to_id` is a 422 (a task always has an addressee). An
   * automation's Assignee is optional (O3), so its clear is "Unassigned".
   * `disabled={locked}` only — no state or persona gate: a `done` row's assignee stays editable.
   * READ-ONLY (another org's row) drops the caret, so a set assignee reads as a value, not a picker; and
   * an empty one is omitted (`showAssignedTo`): a blank, inert field only looked live. */
  const showAssignedTo = !viewer && !template && !(readOnly && assignedToOption === null)
  /* Below the stow width an UNSET task assignee stays on the main form, like attachments with files: a
   * task always has an addressee, so "Unassigned" is a gap to see, not a setting to tuck away. An automation's
   * optional Assignee stays stowed. Decided once, at mount: picking someone must not move the field into the closed
   * disclosure under the operator's finger. */
  const [assigneeUnsetAtOpen] = useState(() => !automation && (value.assignee_id ?? '') === '')
  const assignedToInAdvanced = stowInAdvanced && !assigneeUnsetAtOpen
  const assignedToField = (
    <FormField label="Assigned to" error={errors.assignee_id}>
      <AsyncLookup
        id="task-assigned-to"
        testId="task-assigned-to"
        value={assignedToOption}
        onChange={next => {
          if (next !== null) onChange({ ...value, assignee_id: next.key })
          else if (automation) onChange({ ...value, assignee_id: null })
          else setClearRefusals(n => n + 1)
        }}
        fetchOptions={query =>
          membersLoad.current.then(rows => rows.filter(m => matchesAny(query, memberLabel(m))).map(memberOption))
        }
        fetchOnFocus
        minChars={0}
        /* An automation's optional Assignee, or a row stored with none (a pre-field row): a word, never a
           blank box. */
        placeholder="Unassigned"
        noOptionsText="No matching members"
        disabled={locked}
      />
    </FormField>
  )
  const assignedTo = readOnly ? <Box sx={READ_ONLY_LOOKUP_SX}>{assignedToField}</Box> : assignedToField
  /* THE NAME IS SHOWN WHOLE: an input cannot wrap, so a name longer than the 16rem basis asks for its
   * own width — `ch` per character (a generous measure for a proportional face) plus the lookup's padding and its
   * clear/caret adornments, capped at 28rem so one long name cannot take the settings row's budget (the cell's
   * comment below). The cell still shrinks to its floor, and wraps under the chips first. */
  const assignedToBasis = `min(max(16rem, calc(${(assignedToOption?.label ?? '').length}ch + 4.5rem)), 28rem)`
  const nodesNote = automation && targetKind === 'nodes' ? (
    <Typography
      component="p"
      variant="caption"
      data-testid="task-spec-nodes-assignee-note"
      sx={{ mt: '0.25rem', mb: 0, color: 'text.secondary' }}
    >
      {NODES_ASSIGNEE_NOTE}
    </Typography>
  ) : null

  /* From Jira, placed here and built by the host (`task` only — O3 keeps it out of an automation). */
  const fromJira = automation || jiraField === undefined ? null : jiraField(stowInAdvanced)
  /* Is the lookup in the SETTINGS ROW now: the slot, and whether the project has Jira at all. */
  const jiraInSettingsRow = !stowInAdvanced && fromJira !== null

  /* ── TEMPLATE HELP — an event automation's `{{body.x}}` …, always shown ───────────────────────────────── */
  const templateHelp = templateHint === undefined ? null : (
    <Typography
      component="p"
      variant="caption"
      data-testid="task-spec-template-hint"
      sx={{ mt: 0, mb: '0.5rem', color: 'text.secondary', wordBreak: 'break-word' }}
    >
      {templateHint}
    </Typography>
  )

  /* ── READ-ONLY ADVANCED: the Advanced row's values as `dt`/`dd` text, under the same gates (`factsOnly`) ───────
   * A value at its default is left out (Inherit model, Default project memory, an unpicked agent, Hidden and Debug
   * off); Priority always has one, so the list is never empty. */
  const advancedFacts = (
    <Box
      component="dl"
      data-testid="task-form-advanced-facts"
      sx={{ ...FACTS_SX, mb: ADVANCED_ROW_GUTTER, rowGap: ADVANCED_ROW_GUTTER }}
    >
      {showsPersonaPicker && !isEpicPersona(persona) && !playbookComplex && (
        <DetailField label="Persona" dense>{personaLabel(persona)}</DetailField>
      )}
      {showsAgentPicker && hasAgentPick && <DetailField label="Agent" dense>{agentName}</DetailField>}
      {showsModelPin && value.model !== MODEL_INHERIT && (
        <DetailField label={playbookComplex ? 'Max model' : 'Model'} dense>
          {modelChoices.find(c => c.value === value.model)?.label ?? value.model}
        </DetailField>
      )}
      {!automation && dispatchedRow && hasNodePin && <DetailField label="Node" dense>{nodeName}</DetailField>}
      <DetailField label="Priority" dense>{priorityLabel(value.priority)}</DetailField>
      {projectMemoryOn && value.project_memory != null && (
        <DetailField label="Project memory" dense>{value.project_memory ? 'On' : 'Off'}</DetailField>
      )}
      {showsHidden && value.hidden && <DetailField label="Hidden" dense>Yes</DetailField>}
      {!childRow && isEpicPersona(persona) && value.transition_mode === 'hold' && (
        <DetailField label="Debug" dense>On</DetailField>
      )}
    </Box>
  )

  const fields = (
    <>
      {/* TITLE TEMPLATE — an event automation's only: a fixed task's title comes from its Description. */}
      {automation && templateHint !== undefined && (
        <Box sx={{ mb: '0.75rem' }}>
          <FormTextField
            label="Title"
            sx={FIELD_SX}
            id="task-title-template"
            value={value.title_template ?? ''}
            disabled={locked}
            error={errors.title_template}
            placeholder="Generated from the description"
            onChange={v => onChange({ ...value, title_template: v === '' ? null : v })}
            fullWidth
          />
        </Box>
      )}

      {/* REQ-1/REQ-3: the same rich editor for create and edit.
      SANCTIONED SCROLLPORT 1 of the page's fit contract (CONTRACT-6): prose is unbounded.
      `data-testid="task-page-description"` is the PAGE's scrollport hook (`pages/taskDetail/taskDetailConstants.ts` applies the
      floor/fill-chain rule through it, and `e2e/task-detail-fit.spec.ts` selects by it). */}
      <Box
        data-testid="task-page-description"
        data-role="task-form-body-group"
        // Read-only sizes to its content: a floored, growing box around short prose read as an empty
        // editor. It never shrinks (a squeezed box clipped its one line); above the phone long prose scrolls past
        // 40vh. The page's fill rule yields to this attribute (`PAGE_SX`).
        data-readonly={readOnly ? 'true' : undefined}
        sx={theme => ({
          flex: 1,
          // Both floors live on the element that owns them (12rem, and the phone value below); the page declares only
          // the scrollport (see `PAGE_SX` for why a floor there loses the cascade).
          minHeight: '12rem',
          '&[data-readonly="true"]': {
            flex: '0 0 auto',
            minHeight: 0,
            [theme.breakpoints.up(481)]: { maxHeight: '40vh' },
          },
          display: 'flex',
          flexDirection: 'column',
          mb: '1rem',
          [theme.breakpoints.down(481)]: {
            mb: '0.5rem',
            // The floor belongs on the OUTERMOST box of the fill chain, which also contains the 44px label row and 53px
            // toolbar: 230px left ~110px of editor measured at 390x844 with Advanced open. 320 - 97 = ~223px of editor.
            // A phone may spend page height (`TaskDetailPage`'s PAGE_SX is `height: auto` here), and the action bar is
            // pinned to the viewport, so Save/Go stay reachable.
            minHeight: '320px',
            '&:has([data-testid="rte-toolbar"][data-expanded="true"])': { minHeight: '346px' },
          },
        })}
      >
        {/* No "Description" label row: the editor is the tab's only field; the accessible NAME is the editor
        wrapper's `aria-label`. */}
        {/* Risk 1 retry pass: the task exists and Save only retries pending uploads, so the editor is locked
        rather than silently editable (a keystroke could be discarded unnoticed). */}
        <Box
          data-locked={descriptionEditor.paused ? 'true' : 'false'}
          title={descriptionEditor.paused ? 'Editing is paused while retrying the image upload.' : undefined}
          sx={{
            flex: 1,
            minHeight: 0,
            display: 'flex',
            flexDirection: 'column',
            /* The editor root fills this flex-column wrapper and loses its pane (1px border, 8px radius, `background.paper`)
             * for an edge-to-edge tab — overridden HERE, not in the shared component (the gate form keeps its box). The
             * toolbar's fill goes but its bottom divider STAYS (where controls end and prose begins). The read-only arm
             * keeps a real box (see below). */
            '& > [data-testid="rte"]': {
              flex: 1,
              minHeight: 0,
              border: 'none',
              borderRadius: 0,
              backgroundColor: 'transparent',
            },
            '& [data-testid="rte-toolbar"]': {
              backgroundColor: 'transparent',
              // Toolbar: vertical padding stays; horizontal goes, so the first button aligns with the tab's left edge.
              paddingLeft: 0,
              paddingRight: 0,
            },
            /* The rest of the fill chain (`.rte-content`/`.rte-prosemirror`, ProseMirror's own classes): invisible to the
             * fit oracle, but without them the editable area collapses to ONE line at ≤480px "while every box around it
             * measured correctly". */
            /* …and the left edge in the SAME rule (a duplicate key would replace it): `.rte-content`'s 1rem horizontal
             * pad put prose 16px right of everything else (x=267 vs 251 at 1280x800); the vertical pad stays. */
            '& .rte-content': {
              display: 'flex',
              flexDirection: 'column',
              paddingLeft: 0,
              paddingRight: 0,
            },
            '& .rte-prosemirror': { flex: '1 1 auto', minHeight: 0 },
            '&[data-locked="true"]': { pointerEvents: 'none', opacity: 0.65 },
            /* Read-only needs the box back: `RichTextRenderer` has no chrome, and in a 12rem-floored scrollport short
             * prose left ~270px of unbordered space that read as loading. Same border, radius and inset as the editor. */
            ...(readOnly
              ? {
                  // Content-sized, not filling: a `flex: 1` chain in a box that no longer grows collapses to 0.
                  flex: '0 0 auto',
                  // The box's own inset is the only one: no second pad above the first line.
                  '& > .rte-content': { flex: '0 0 auto', paddingTop: 0, paddingBottom: 0 },
                  '& .ProseMirror > :first-child': { marginTop: 0 },
                  '& .ProseMirror > :last-child': { marginBottom: 0 },
                  border: '1px solid',
                  borderColor: 'divider',
                  borderRadius: 2,
                  bgcolor: 'background.paper',
                  p: '0.75rem',
                }
              : {}),
          }}
          data-role="task-form-body-wrap"
          // The name a visible label would supply, on the wrapper both arms share (the editor's root is another
          // component's).
          aria-label="Description"
        >
          {/* READ-ONLY renders the RENDERER (same schema via `buildExtensions` — `richtext/extensions.ts`), without the
          toolbar, dropzone and caret. Not `data-locked`'s dimming, which marks a TEMPORARY retry pause. */}
          {readOnly ? (
            <RichTextRenderer content={value.body} resolveAttachmentSrc={descriptionEditor.resolveAttachmentSrc} />
          ) : (
            <RichTextEditor
              value={value.body}
              onChange={changeBody}
              onUploadImage={descriptionEditor.onUploadImage}
              resolveAttachmentSrc={descriptionEditor.resolveAttachmentSrc}
              placeholder={descriptionEditor.placeholder}
              autoFocus={descriptionEditor.autoFocus}
              // An automation's body is a template rendered as the task's prompt: Enter is a line, not a paragraph.
              lineBreaks={automation}
            />
          )}
        </Box>
        {(errors.body || imageError) && (
          <Typography component="p" variant="caption" role="alert" sx={{ mt: '0.25rem', mb: 0, color: 'error.main' }}>
            {errors.body || imageError}
          </Typography>
        )}
      </Box>
      {templateHelp}

      {/* ── THE TWO AXES, heading the ONE non-Advanced line ────────────────────────────────────────────────
      DEPTH and INVOLVEMENT; what each press writes is the hierarchy (`applyDepth`/`applyInvolvement`).
      EVERYTHING outside `Advanced` shares ONE line at every width (operator instruction), hence wordless,
      content-sized axes (`TaskAxisControls`' header). The automation context is the exception: its form is filled
      rarely and read cold, so each segment shows its word too (`showLabels`), and the axis wraps rather than clip it.
      Both gates are `!childRow` but spelled apart: `showsPersonaPicker` and `showsAutonomous` answer different
      questions. */}
      {/* The rest of the line: Attachments, Assigned to and From Jira (narrow widths stow them; see
      `stowInAdvanced`). Debug is inside Advanced: a how-it-runs setting. */}
      <FieldRow
        testId="task-form-settings"
        align="center"
        columnGap="1.5rem"
        /* A row-gap where the row carries the Attachments cell (no tap padding of its own); below the tight width
         * every member has its own tap padding. `!stowInAdvanced`, since a project without Jira still has
         * attachments here. */
        rowGap={!stowInAdvanced ? '0.5rem' : 0}
        /* Tighter phone column gap, by arithmetic: at 320px the column is 296px and two 104px axes plus a 44px member
         * at 1.5rem needed 300px (measured, 4px over); 0.75rem gives 276px, bought between controls so tap heights
         * stay 44px. 481–600 is tighter still (0.5rem), matching `TaskAxisControls`' segment padding: the 212px
         * sidebar appears at 481px. `sx`, not `columnGap` (one value; `FieldRow` spreads it before `sx`). */
        sx={theme => ({
          mb: '0.5rem',
          [theme.breakpoints.down(481)]: { mb: '0.25rem', columnGap: '0.75rem' },
          [theme.breakpoints.between(481, 'sm')]: { columnGap: '0.5rem' },
        })}
      >
        {/* DEPTH (writes: `applyDepth`). Live in every state (the server archives rather than deletes an epic's
        children) and at every Involvement (a lower level cannot freeze a higher one). Only `locked` greys it. */}
        {/* PLAYBOOK: the fourth segment only while one may be picked; a row's playbook shows as the pill whatever
        the setting says now, fixed after Save. */}
        {/* READ-ONLY, the two axes are facts, not greyed segments: label + value, as `advancedFacts`. */}
        {factsOnly && (showsPersonaPicker || showsAutonomous) && (
          <Box component="dl" data-testid="task-form-axes-facts" sx={FACTS_SX}>
            {showsPersonaPicker && (
              <DetailField label="Depth" dense>
                {playbook !== null ? `Playbook: ${playbook.name}` : TASK_DEPTHS.find(d => d.value === depth)?.label ?? depth}
              </DetailField>
            )}
            {showsAutonomous && (
              <DetailField label="Involvement" dense>
                {TASK_INVOLVEMENTS.find(i => i.value === involvement)?.label ?? involvement}
              </DetailField>
            )}
          </Box>
        )}
        {showsPersonaPicker && !factsOnly && (
          <TaskDepthControl
            value={depth}
            disabled={locked}
            onChange={applyDepth}
            offersPlaybook={playbooksEnabled && playbookOpen && !template}
            playbook={playbook}
            playbookFixed={!playbookOpen}
            onPlaybook={applyPlaybook}
            hasRepo={hasRepo}
            showLabels={staging}
          />
        )}

        {/* THE LINK-SEEDED PERSONA, said on the main row so the owner never has to open Advanced to see who runs
        this. Read-only: the picker in Advanced still changes it; the host withdraws it once it no longer matches. */}
        {personaChipLabel !== undefined && (
          <Chip
            size="small"
            variant="outlined"
            label={personaChipLabel}
            aria-label={`Persona: ${personaChipLabel}`}
            data-testid="task-form-persona-chip"
            sx={{ flex: '0 0 auto', maxWidth: '100%' }}
          />
        )}

        {/* INVOLVEMENT — R4's autonomous switch as an axis, on the row that OWNS the value (`showsAutonomous`).
        `offersChatPosition` carries both reasons `chat` may be missing. */}
        {showsAutonomous && !factsOnly && (
          <TaskInvolvementControl
            value={involvement}
            disabled={locked}
            offersChat={offersChatPosition}
            onChange={applyInvolvement}
            showLabels={staging}
          />
        )}

        {/* ATTACH (≤480px, no file yet): the compact paperclip, a 44px member of this line. Read-only it would be empty
        (no Attach button), so hidden — mounted still, to report `hasFiles`. `0 1 auto`: an upload error's banner
        widens it onto its own line. Single home: `attachments`. */}
        {attachmentsOnAxes && (
          <Box sx={{ flex: '0 1 auto', minWidth: 0, ...(readOnly ? { display: 'none' } : {}) }}>{attachments}</Box>
        )}

        {/* Attachments, Assigned to and From Jira as ONE flex item: flex breaks lines on hypothetical sizes, so
        independent members orphaned one on its own line. Grouped they wrap together. It wraps internally at ~490px.
        Below the stow width its members are placed elsewhere, so the group is not rendered. */}
        {showsAttachments && !stowInAdvanced && (
          <Box
            sx={{
              display: 'flex',
              flexWrap: 'wrap',
              alignItems: 'center',
              columnGap: '1.5rem',
              rowGap: '0.5rem',
              /* A STATED basis when the lookup is here: `auto` measures content, so a confirm opening re-wrapped the row
               * under the cursor. 17rem, not the parts' 22.25rem: the basis is what the group will LIVE at (the lookup
               * shrinks to 13rem), tracking the cell's 18rem. Assigned to adds its 12rem floor + 1.5rem gap: 26.5rem with
               * Jira; without Jira (above the stow width) Assigned to's 12rem floor, since `auto` would hand the
               * line-breaker the lookup's max-content. */
              flex: jiraInSettingsRow ? '1 1 26.5rem' : '1 1 12rem',
              minWidth: 0,
            }}
          >
            {/* ATTACHMENTS (above 480px), inside this group, not a row member (as a member it pushed the group off line
            1 at 1024px). It SHRINKS TO ITS CONTENT (`0 1 auto`): a grower would carry Assigned to to
            the far edge (a 600px gap at 1440px). The orphan guard is a `maxWidth` CAP, since max-width clamps the
            hypothetical size: Assigned to's 16rem ask + 1.5 = 17.5rem; with From Jira its 13rem floor + 1.5 more
            = 32rem. Chips wrap inside the cell; `minWidth: 0` lets a chip's ellipsis give way.
            First in the group: what the task IS, what is attached, who it is for, where its text comes from. Below the
            stow width it is the axes line's paperclip, or under the line once there are files. */}
            <DetailCell
              sx={{
                flex: '0 1 auto',
                minWidth: 0,
                // Read-only with no file there is no Attach button either: an empty cell, kept mounted for `hasFiles`.
                ...(readOnly && !hasFiles ? { display: 'none' } : {}),
                /* FLOORED: a negative `calc()` max-width clamps to ZERO (481–~640px), collapsing the Attach button over its
                 * neighbour. The floor is a whole chip (`ATTACHMENT_CHIP_MAX`, or the group's width): once the group
                 * is too narrow to share a line, Assigned to wraps under the chips anyway, and a 6rem floor left
                 * every name a 96px stub (`releas…`) beside free width. The cell is content-sized, so with no file
                 * it stays the Attach button. */
                maxWidth: jiraInSettingsRow
                  ? `max(min(100%, ${ATTACHMENT_CHIP_MAX}), calc(100% - 32rem))`
                  : `max(min(100%, ${ATTACHMENT_CHIP_MAX}), calc(100% - 17.5rem))`,
              }}
            >
              {attachments}
            </DetailCell>

            {/* ASSIGNED TO (above 480px), second in the group. Its `sx` overrides `DetailCell`'s `flex` (a basis GROWS,
            `1 1 <basis>`; a grower here would take all the slack). `0 1 16rem` shows a ~20-character address whole (an
            unnamed account shows its email; 12rem clipped "admin@exa…"), shrinking to a `min()`-ed 12rem floor (a bare
            rem floor overflows the ~200px column at 481-490px). Priced into the group's `flex` above. */}
            {showAssignedTo && (
              <DetailCell sx={{ flex: `0 1 ${assignedToBasis}`, minWidth: 'min(12rem, 100%)' }}>{assignedTo}{nodesNote}</DetailCell>
            )}

            {jiraInSettingsRow && fromJira}
          </Box>
        )}

        {/* Only about THIS press: the server's refusal of a conversion, or what the selected conversion will cost. */}
        {personaNote && (
          <Typography
            component="p"
            variant="caption"
            role="status"
            data-testid="task-form-persona-reason"
            sx={{ flex: '1 1 100%', mt: 0, mb: 0, color: 'text.secondary' }}
          >
            {personaNote}
          </Typography>
        )}
      </FieldRow>

      {/* Attachments below the stow width when there are any (`hasFiles`): on the main form, under its one line. */}
      {showsAttachments && stowInAdvanced && !attachmentsOnAxes && <Box sx={{ mb: '0.75rem' }}>{attachments}</Box>}

      {/* Assigned to below the stow width while unset (`assignedToInAdvanced`): on the main form, so the gap shows. */}
      {stowInAdvanced && !assignedToInAdvanced && showAssignedTo && <Box sx={{ mb: '0.75rem' }}>{assignedTo}{nodesNote}</Box>}

      {/* ── ADVANCED: Persona, Provider, Model, Priority, Debug on one line, then the two free-text fields ─────────────
      A real disclosure at every width, collapsed by default (`ui/AdvancedSection`). ONE disclosure, always present:
      Priority is on every row. From Jira leads it at ≤480px only (`stowInAdvanced`). */}
      <AdvancedSection>

        {/* From Jira, ≤480px placement only (single home: the host's `jiraField`). */}
        {stowInAdvanced && fromJira}

        {/* Assigned to, ≤480px placement only, in the desktop row's order. Full width like `fromJira` here. Single
        home: `assignedTo`. */}
        {assignedToInAdvanced && showAssignedTo && <Box sx={{ mb: '0.75rem' }}>{assignedTo}{nodesNote}</Box>}

        {/* Persona, Provider, Model, Priority and Debug in ONE wrapping row ("ideally one line if it fits on mobile"); each
        cell's basis decides the wraps (`ui/FieldRow`). Read-only, the same fields as label + text (`advancedFacts`). */}
        {factsOnly ? advancedFacts : (
        <FieldRow
          testId="task-form-advanced-row"
          align="start"
          columnGap="1rem"
          sx={readOnly ? READ_ONLY_LOOKUP_SX : undefined}
        >
          {/* WHICH PERSONA runs this task: writes `persona` (see `taskPersona.ts`); the offered list and order are the
          server's (`operator_pickable` on `GET /api/tasks/personas`). HIDDEN at the `epic` depth (no runner persona),
          never greyed. Live at every other depth: the Depth press only seeds it, and overriding here does not move
          the axis (a level never moves for one below). */}
          {/* `maxWidth` too: with the other cells absent it grew to ~890px at 1280. */}
          {showsPersonaPicker && !isEpicPersona(persona) && !playbookComplex && (
            <DetailCell basis="13rem" max="22rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <FormTextField
                label="Persona"
                help={PERSONA_HINT}
                sx={FIELD_SX}
                id="task-persona"
                select
                slotProps={{ select: { inputProps: { 'aria-label': 'Persona' } } }}
                value={persona}
                disabled={locked}
                error={errors.persona}
                onChange={v => {
                  /* The persona is level three: this handler writes the Model below and nothing above. No Depth
                   * drag-back, no `autonomous` write — Involvement is its own state, and every combination is legal.
                   * The prototyper's seed model is the one downward write: picking a persona shows what it would run
                   * on. A seed, not a lock (`PROTOTYPER_MODEL_SEED`); leaving the persona does not clear it.
                   * `isOrgAdmin`, not `showsModelPin` (whose other half reads the persona being replaced). */
                  setAxes(axes, { persona: true })
                  onChange({
                    ...value,
                    persona: v,
                    ...(v === PROTOTYPER_PERSONA && isOrgAdmin ? { model: PROTOTYPER_MODEL_SEED } : {}),
                  })
                }}
                fullWidth
                /* The advisor carries the same grant as on the axis (`offersChatPosition`): otherwise the withheld position
                was two clicks away in Advanced, and picking it painted the segment back onto the axis.
                `|| o.value === persona`: never drop the current value. */
                options={personaOptions().filter(o => o.value !== ADVISOR_PERSONA || offersChatPosition || o.value === persona)}
              >
                {/* A stored persona this picker does not offer (an epic child's `plan`, a system persona) stays as its own
                option: the value must never disagree with what is stored. */}
                {!personaOptions().some(o => o.value === persona) && (
                  <option value={persona}>{personaLabel(persona)}</option>
                )}
              </FormTextField>
            </DetailCell>
          )}

          {/* THE AGENT — who runs this task: a built-in runner (`bound_provider`) or an SDK agent (`agent_id`),
          one select, one half written (`components/agents/AgentPicker.tsx`). Between Persona and Model (who runs it,
          on which engine, on what). `18rem`/`30rem`: a long agent name stays readable — narrower, it wraps to its own line. */}
          {showsAgentPicker && (
            <DetailCell basis="18rem" max="30rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <AgentPicker
                id="task-agent"
                label="Agent"
                value={{ agentId: value.agent_id, provider: value.bound_provider }}
                onChange={pick => onChange({
                  ...value,
                  agent_id: pick.agentId,
                  bound_provider: pick.provider,
                  // A provider pin of another vendor takes the account pick back to Any: the server would 422 the pair.
                  ...(value.account_id && pick.provider !== PROVIDER_ANY
                    && accountChoices.find(a => a.id === value.account_id)?.provider !== pick.provider
                    ? { account_id: null }
                    : {}),
                })}
                persona={persona}
                defaultLabel="Default"
                builtinsOnly={automation}
                org={org}
                disabled={locked || (!automation && !taskAgentPicker)}
                error={errors.bound_provider ?? errors.agent_id}
              />
            </DetailCell>
          )}

          {/* THE MODEL PIN — org-admin-only (`showsModelPin`; the server enforces on its own). A CLOSED SELECT over
          `modelChoices` (`apps/api/app/model_catalog.py` via `GET /api/tasks/models`), never a text box. Right after
          Persona (operator ask 5: "who runs this, and on what"), on create too (`dispatchedRow`). Sized to its
          one-word options. */}
          {showsModelPin && (
            <DetailCell basis="11rem" max="18rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <FormTextField
                label={playbookComplex ? 'Max model' : 'Model'}
                help={playbookComplex ? MODEL_CEILING_HINT : MODEL_INHERIT_HINT}
                sx={FIELD_SX}
                id="task-model"
                select
                /* `inputLabel: { shrink: true }` is REQUIRED: the unpinned value is `''`, so MUI would float "Model" over
                 * "Inherit" (as in `pages/ProjectEditPage.tsx`). */
                slotProps={{
                  select: { inputProps: { 'aria-label': playbookComplex ? 'Max model' : 'Model' } },
                  inputLabel: { shrink: true },
                }}
                value={value.model}
                // `locked` ALONE: no persona-derived lock (the server's prototyper pin rule is gone).
                disabled={locked}
                error={errors.model}
                onChange={v => onChange({ ...value, model: v })}
                fullWidth
                options={modelChoices}
              >
                {/* Not pinned, leading: nearly every task's state. `''` is the API's "inherit" (`MODEL_INHERIT`). */}
                <option value={MODEL_INHERIT}>Inherit</option>
                {/* A stored model the catalogue lacks stays as its own option (before the fetch lands, or after a
                retirement). */}
                {value.model !== MODEL_INHERIT && !modelChoices.some(c => c.value === value.model) && (
                  <option value={value.model}>{value.model}</option>
                )}
              </FormTextField>
            </DetailCell>
          )}

          {/* THE ACCOUNT — create only (`showsAccountPicker`), and not beside an SDK agent, which runs on no account. */}
          {showsAccountPicker && value.agent_id === null && (
            <DetailCell basis="14rem" max="24rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <FormTextField
                label="Account"
                sx={FIELD_SX}
                id="task-account"
                select
                // `shrink`: Any is `''`, over which MUI would otherwise float the label.
                slotProps={{ select: { inputProps: { 'aria-label': 'Account' } }, inputLabel: { shrink: true } }}
                value={value.account_id ?? ''}
                disabled={locked}
                error={errors.account_id}
                onChange={v => onChange({ ...value, account_id: v === '' ? null : v })}
                fullWidth
              >
                <option value="">Any</option>
                {accountOptions.map(a => <option key={a.id} value={a.id}>{`${a.name || a.slug} · ${a.provider}`}</option>)}
              </FormTextField>
            </DetailCell>
          )}

          {/* THE NODE — `showsNodePin` (an operator, or the org's node picker on): Any, or one node the project runs on. After Persona → Agent →
          Model (who runs it, on what), then where. */}
          {showsNodePin && (
            <DetailCell basis="12rem" max="20rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <FormTextField
                label="Node"
                sx={FIELD_SX}
                id="task-node"
                select
                // `shrink`: Any is `''`, over which MUI would otherwise float the label.
                slotProps={{ select: { inputProps: { 'aria-label': 'Node' } }, inputLabel: { shrink: true } }}
                value={value.pinned_node_id ?? ''}
                disabled={locked}
                error={errors.pinned_node_id}
                onChange={v => onChange({ ...value, pinned_node_id: v === '' ? null : v })}
                fullWidth
              >
                <option value="">Any</option>
                {hasNodePin && !nodeChoices.some(n => n.id === value.pinned_node_id) && (
                  <option value={value.pinned_node_id ?? ''}>{nodeName}</option>
                )}
                {nodeChoices.map(n => <option key={n.id} value={n.id}>{n.name}</option>)}
              </FormTextField>
            </DetailCell>
          )}

          {/* PRIORITY — named dispatch-order levels on every persona, create and edit (an epic's value is inherited at
          fan-out). An unnamed stored int stays as its own `P<n>` option. A template carries none (N-6). */}
          {!template && (
          <DetailCell basis="9rem" max="16rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
            <FormTextField
              label="Priority"
              sx={FIELD_SX}
              id="task-priority"
              select
              slotProps={{ select: { inputProps: { 'aria-label': 'Priority' } } }}
              value={value.priority}
              disabled={locked}
              error={errors.priority}
              onChange={v => onChange({ ...value, priority: Number(v) })}
              fullWidth
            >
              {!PRIORITY_LEVELS.some(l => l.value === value.priority) && (
                <option value={value.priority}>{`P${value.priority}`}</option>
              )}
              {PRIORITY_LEVELS.map(l => (
                <option key={l.value} value={l.value}>{l.label}</option>
              ))}
            </FormTextField>
          </DetailCell>
          )}

          {/* PROJECT MEMORY — `tasks.project_memory`, every persona, task and epic alike (an epic's choice is its
          children's Default at dispatch). Default stores null; its label names today's System Settings value. */}
          {projectMemoryOn && (
          <DetailCell basis="10rem" max="16rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
            <FormTextField
              label="Project memory"
              help={childRow ? PROJECT_MEMORY_CHILD_HINT : undefined}
              sx={FIELD_SX}
              id="task-project-memory"
              select
              // `shrink`: Default is `''`, over which MUI would otherwise float the label.
              slotProps={{ select: { inputProps: { 'aria-label': 'Project memory' } }, inputLabel: { shrink: true } }}
              value={projectMemoryValue(value.project_memory)}
              disabled={locked}
              error={errors.project_memory}
              onChange={v => onChange({ ...value, project_memory: projectMemoryField(v) })}
              fullWidth
            >
              <option value={MEMORY_DEFAULT}>{projectMemoryDefaultLabel(projectMemoryDefault)}</option>
              <option value="on">On</option>
              <option value="off">Off</option>
            </FormTextField>
          </DetailCell>
          )}

          {/* FEATURE — an automation's only (the task surface files from its breadcrumb): offered for exactly one
          target project, else the one-line reason in its place. */}
          {automation && (
            <DetailCell basis="12rem" max="20rem" sx={{ mb: ADVANCED_ROW_GUTTER }}>
              {featureProjectId !== null ? (
                <FeatureField
                  projectId={featureProjectId}
                  value={value.feature_id}
                  onChange={feature_id => onChange({ ...value, feature_id })}
                  disabled={locked}
                  error={errors.feature_id}
                />
              ) : (
                <Typography
                  component="p"
                  variant="caption"
                  data-testid="task-spec-feature-reason"
                  sx={{ m: 0, pt: '0.5rem', color: 'text.secondary' }}
                >
                  {FEATURE_NEEDS_ONE_PROJECT}
                </Typography>
              )}
            </DetailCell>
          )}

          {/* HIDDEN — the `hidden` column: on an existing top-level row its principal may hide (task), or on every
          task an automation starts. Applies to its whole subtree. */}
          {showsHidden && (
            <DetailCell sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <SettingCheckbox
                testId="task-form-setting"
                dataRole="task-form-checkbox"
                name="task-hidden"
                label="Hidden"
                checked={value.hidden}
                disabled={locked}
                onChange={hidden => onChange({ ...value, hidden })}
              />
            </DetailCell>
          )}

          {/* DEBUG is the epic `transition_mode` column: ticked = 'hold' (press Run on each subtask), clear = 'auto'.
          Gated on the row BEING an epic (it is read off the PARENT EPIC at dispatch), off the LIVE `persona`:
          shown ⇔ submitted ⇔ read. */}
          {!childRow && !template && isEpicPersona(persona) && (
            <DetailCell sx={{ mb: ADVANCED_ROW_GUTTER }}>
              <SettingCheckbox
                testId="task-form-setting"
                dataRole="task-form-checkbox"
                name="task-transition-mode"
                label="Debug"
                help={DEBUG_HINT}
                checked={value.transition_mode === 'hold'}
                disabled={locked}
                onChange={checked => onChange({ ...value, transition_mode: checked ? 'hold' : 'auto' })}
              />
            </DetailCell>
          )}

        </FieldRow>
        )}

        {/* Depends on — the host's (`task` only; O3 keeps it out of an automation). Not on the Live Cast viewer:
        cross-task edges are an operator exception. */}
        {!automation && !viewer && dependsOn}
      </AdvancedSection>
    </>
  )

  if (!staging) return fields

  /* A staging context has no host form root, so it brings the phone editor re-scope itself (the same rule
   * `TaskEditForm`'s root carries). */
  return (
    <Box
      data-testid="task-spec-fields"
      sx={(theme: Theme) => ({
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        [theme.breakpoints.down(481)]: { '& [data-testid="rte"]': PHONE_EDITOR_SX },
      })}
    >
      {fields}
    </Box>
  )
}
