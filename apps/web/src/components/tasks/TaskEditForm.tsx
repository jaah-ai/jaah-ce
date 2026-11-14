import {
  type FormEvent, type MutableRefObject, type ReactNode, useCallback, useEffect, useMemo, useRef, useState,
} from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { Link as RouterLink } from 'react-router-dom'
import Tip from '../../ui/Tip'
import { apiDetail, patch, post, put } from '../../api'
import { countOf } from '../../lib/humanize'
import { useMaxFileBytes } from '../../lib/maxFileBytes'
import { useAuth } from '../../auth'
import { saveLastCreateProject } from '../../lib/lastCreateProject'
import { readMuted, withoutHiddenProjects } from '../../lib/taskMute'
import { useOtherOrg } from '../../ui/OrgIndicator'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { planSurfaceShown } from '../../lib/planSwitches'
import { useActingAs } from '../../lib/actAs'
import { useCanPickAgent } from '../agents/AgentPicker'
import { useCanOnProject } from '../../auth'
import { useCanCreateChats, useCanRunTasksOnProject } from '../../hooks/useTaskAccess'
import { useAwaitingApprovalReason } from '../../lib/orgLifecycle'
import { useTaskAutosaveSeconds } from '../../lib/taskAutosaveInterval'
// The create-time seed a row's Duplicate hands over (see the `duplicateSeed` prop).
import type { TaskDuplicateSeed } from '../../lib/taskDuplicate'
import type { TaskPrefill } from './taskPrefill'
// The ticket → PUT → complete sequence from its ONE home, shared with `TaskAttachmentsField`.
import { uploadTaskAttachment } from '../../lib/uploadTaskAttachment'
import { RecommendationBlock } from '../routing/RecommendationBlock'
import ActAsToCreate from './ActAsToCreate'
import type { OrgRef, PipelineTask, Project, TaskPlaybookRef } from '../../types'
import { Banner } from '../../ui/Banner'
import { modEnterCapture } from '../../ui/modEnter'
import { MOD_ENTER_LABEL } from '../../ui/modifierKey'
import { useConfirm } from '../ConfirmDialog'
import { BUTTON_SX } from '../../ui/formDensity'
import { FormActionBar } from '../../ui/FormActionBar'
import { SubmitButton } from '../../ui/SubmitButton'
import EmptyState from '../../ui/EmptyState'
import { createAttachmentResolver, useStagedImagePaint, type AttachmentResolver } from '../richtext/attachmentResolver'
import { can, CONTROLS, startBody, startIntentFor } from './controls'
import { type PushIdentity, readStarters, usePushIdentity } from './PushIdentityDialog'
import { NEW_TASK_CRUMB } from './TaskBreadcrumb'
import TaskPageFields, { useTaskPageFields } from './TaskPageFields'
import { isSuggestion } from './listViews/boardSections'
import IssuePicker, { heldElsewhere, issueField, useIssuePick, useProjectHasTrackers } from '../trackers/IssuePicker'
import {
  createRefusal, refusedInOrg, type DiscardSubject, discardConfirmOptions, resetConfirmOptions,
  resolveCreateProject,
} from './shared'
import { type PrereqCommitHandle, TaskDependsOn } from './TaskDependencies'
/* The two axes — Depth (sketch · task · epic) and Involvement (unattended · guided · chat) — with their
 * vocabulary, axes→persona resolution, seeds and read-out (see that module's header). */
import {
  autonomousForInvolvement,
  depthForPersona,
  involvementForTask,
  type TaskDepth,
  type TaskInvolvement,
} from './TaskAxisControls'
import { type TaskCreateDefaults, withProjectPersona } from './taskDefaults'
// Persona VALUES written by name, in their own LEAF module: `taskDefaults` needs `DEFAULT_PERSONA` at
// runtime, and importing it from here dragged the rich-text tree into the `/tasks` list chunk.
import { ADVISOR_PERSONA, DEFAULT_PERSONA, isEpicPersona } from './taskPersona'
// The named priority levels' default, one table for the select, detail page and inline pick.
import { DEFAULT_PRIORITY } from './taskPriority'
// EVERY field of this form: the one task-create component (D7), shared with an automation's Task action.
import TaskSpecFields, {
  imageOverCapMessage,
  MODEL_INHERIT,
  PHONE_EDITOR_SX,
  PROVIDER_ANY,
  stripAttachmentImage,
  type TaskSpec,
  type TaskSpecAxes,
  type TaskSpecTouched,
  taskNodePinShown,
  taskSpecGates,
} from './TaskSpecFields'

// What the main button was pressed for: 'go' saves AND starts (the same `task.start` the row's Start
// posts); 'save' writes and stops. Both modes offer both; `goTransitions` is what an edit-mode Go posts.
export type SubmitIntent = 'save' | 'go'

/* The field vocabulary — model/provider sentinels and hints, Hidden, Throttle — is single-homed in
 * `TaskSpecFields`, which renders every field of this form. Re-exported where this module's callers read it. */
export {
  THROTTLE_HINT, stripAttachmentImage,
} from './TaskSpecFields'

/* The persona an edit SHOWS for a row: a one-stage playbook row shows the expert it runs as, not its settle persona
 * (an off-native stage settles on its deliverable's persona; `playbook_engine.stage_out`). The seed, the dirty gate
 * and the conversion all read it, so an untouched picker is never an edit (the seed alone must not read dirty). */
function shownPersona(row: PipelineTask): string {
  return (row.playbook?.simple ? row.playbook_stage?.expert_persona : undefined) ?? row.persona
}

// The transitions an edit-mode **Go** posts after the save, for a row in `state` ("Go is save and start;
// if running, save, RESET and start" — the runner dispatched against the OLD body):
//   • not running (neither `active` nor `awaiting_response`) → `['start']` (`task_lifecycle.py`; start
//     reset-freshes a `done` row).
//   • running (`active`/`awaiting_response`) → `['reset', 'start']`: `reset` kills the runner and releases
//     the slot in the same transaction as the state write, so no client-side `stop` is needed.
//   • a running EPIC → `['start']` only. `POST /{id}/reset` on an epic resets its WHOLE TREE (far more
//     than a body edit means), and stop-then-start WEDGES it: Stop cascades `paused` onto live children, and re-activating never un-parks them
//     (`start_task`'s epic-reactivate branch, `routes/tasks/dispatch.py`). `start` on `active` is an idempotent
//     no-op; a mid-run edit applies to the NEXT dispatch (`update_task`).
// Go is withheld on running rows (`goNotStartable`, off the LIVE row), so this arm is a BACKSTOP: the sequence is read
// off the row the PUT RETURNED, which may have gone running after render. Removing it would post a 409 `start`.
export function goTransitions(row: PipelineTask): string[] {
  // Branched on the STATE, never `!can(row,'start')`: a legacy `cancelled` row has empty
  // `available_transitions`, and treating it as running would post a `reset` (which kills a runner and
  // releases a slot) at a row with neither. `awaiting_response` joins `active`: a live runner is frozen in
  // AskUserQuestion and `start` is not among that state's sources.
  if (row.state !== 'active' && row.state !== 'awaiting_response') return ['start']
  return row.persona === 'epic' ? ['start'] : ['reset', 'start']
}

/* The hover sentences for a withheld action, exported so the suite asserts the rendered string. Where each
 * applies: `goTooltip`/`saveTooltip`. */
export const NO_CHANGES_HINT = 'No changes to save'
export const PICK_PROJECT_HINT = 'Pick a project'
// A `done` row's Go is withheld until something is edited, exactly as Save is.
export const DONE_GO_HINT = 'Done. Edit to run again.'
// Go's hover while acting as an org you aren't a member of: Go asks whose git account to push with.
export const PUSH_AS_HINT = 'Save and start. Asks whom to push as.'

// What a partially-completed create still owes, i.e. what the retry press will DO. `prereq`: the task
// landed but its prerequisite edges were refused (neither uploads nor start reached).
export type RetryStage = 'upload' | 'start' | 'prereq'

// What the retry button DOES, in full: after a Go it retries the uploads AND starts the task, so a bare
// "Retry upload" would dispatch a runner without saying so. Banner and button share this string. `stage`
// defaults to 'upload'; a prerequisite refusal must not be labelled as an upload one.
export function retryActionLabel(intent: SubmitIntent, stage: RetryStage = 'upload'): string {
  const what = stage === 'prereq' ? 'Retry prerequisites' : 'Retry upload'
  return intent === 'go' ? `${what} and ${CONTROLS.start.label.toLowerCase()}` : what
}

const START_TITLE_MAX = 60


/** One description line as plain words: markdown's images dropped, links to their text, markers, emphasis and
 *  escapes removed (`\\[ops\\]` is how the editor writes `[ops]`). */
function plainLine(line: string): string {
  return line
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)*/, '')
    .replace(/(?<!\\)(?:[*`~]+|(?<![\w\\])_+|_+(?!\w))/g, '')
    .replace(/\\([\\`*_{}[\]()#+\-.!>~|])/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
}

/* What the Start ask calls the row: its title, else the description's first worded line — close to how the board
 * names it once Go's save derives the title from the body (`app/task_title.py::placeholder_title`, which also
 * shortens it). The draft's "New task" placeholder is no title: the body already says what the task is. */
export function startTitle(title: string, body: string): string {
  const authored = title.trim()
  if (authored !== '' && authored !== NEW_TASK_CRUMB) return authored
  const line = body.split('\n').map(plainLine).find(l => l !== '')
  if (line === undefined) return authored || NEW_TASK_CRUMB
  return line.length > START_TITLE_MAX ? `${line.slice(0, START_TITLE_MAX - 1).trimEnd()}…` : line
}

/* One failed upload with its reason.
 * `detail` is the server's own sentence (`apiDetail`), e.g. `413 attachment exceeds the 26214400 byte cap` —
 * never a guessed cause; without it the operator retried an upload that could never succeed.
 * `name` is the name the operator can SEE (`file.name`), not the minted `img-<nanoid>.<ext>` storage token
 * of a pasted image.
 * `inline` says which surface the file is on, because the escape differs: an attachment chip has an ×; a
 * pasted description image has no chip (`TaskAttachmentsField`'s `referenced` filter) and the editor is
 * paused during a pending retry. `uploadFailureBanner` branches on it. */
type UploadFailure = { name: string; detail: string; inline: boolean }

/* The partially-completed-create banner: quote the server's reason (a cap cannot be retried away), name the
 * escape (the chip's × is live in this state, and removing the file then pressing the same button
 * finishes the create), and name the file. One builder for the first pass and the retry; only `lead`
 * differs. */
function uploadFailureBanner(
  failures: UploadFailure[], lead: string, retryLabel: string,
): string {
  /* Every failure carries its OWN reason, paired per file: one shared `detail` would pin a 413 on a PDF that
   * was refused for permission. */
  const named = failures.map(f => `${f.name}: ${f.detail}`).join('; ')
  /* The escape is offered only where it exists, in three arms: a chip can be ×'d now; an inline image cannot
   * (no chip, editor paused), so a MIXED batch may not promise "remove the file with its ×". */
  const hasChip = failures.some(f => !f.inline)
  const hasInline = failures.some(f => f.inline)
  const escape = hasChip && hasInline
    ? ', or remove the attached file with its × — the pasted image cannot be dropped that way, so '
      + 'press Discard if it keeps failing'
    : hasChip
      ? `, or remove the file with its × and press it again to continue without `
        + `${failures.length > 1 ? 'them' : 'it'}`
      : ' — the description stays paused until it lands, so press Discard if it keeps failing'
  return `${lead} ${countOf(failures.length, 'attachment')} failed to upload (${named}). `
    + `Press “${retryLabel}” to try again${escape}.`
}

// What an Epic → Coder conversion COSTS, said before Save: the server STOPS and ARCHIVES the epic's children
// (`_apply_persona_conversion`, tasks.py), so it is safe at any time but archived subtasks leave the board.
// Shown only when an epic with subtasks is moved off the `epic` depth.
export const PERSONA_TO_CODER_WARNING =
  'Saving stops this task’s subtasks and archives them. They leave the board but are not deleted.'

// The axes are wordless by operator instruction; each position's tooltip and `aria-label` is its label from
// the position table in `TaskAxisControls`.

// The task create/edit form (spec cockpit-task-fleet-dispatch-workflow R1 + the draft-edit route). One form
// for BOTH modes with ONE field set: a field is gated by the ROW it writes (`dispatchedRow`,
// `isChildEdit`), never by the mode. What differs is only what a row that does not exist yet needs:
//   • create — POST /api/tasks. No Title (the server auto-titles from the `body`); images and attachments
//     are staged until the POST gives an id; a half-landed create retries. The PROJECT resolves silently, never a
//     blocking picker, via `shared.tsx`'s `resolveCreateProject` (the board's
//     project, else the first creatable one). In practice `TaskDetailPage` resolves it and lifts it in as
//     `projectIdDraft`; `ownProjectId` is the fallback.
//   • edit — PUT /api/tasks/{id}, allowed in EVERY state, `active` included (an active epic's
//     transition_mode can change mid-run). Every field pre-fills from the task.
// The field set splits by whether the row is DISPATCHED (`dispatchedRow`), not by parentage:
//   • epic (a top-level container, never dispatched): Body plus Full Auto, which its two interviewing
//     children are dispatched with (`showsAutonomous`), and Throttle.
//   • every other row (incl. a top-level `coder` task, which is dispatched): Body plus the per-dispatch model pin
//     (`showsModelPin`). An epic CHILD renders no Full Auto (its column is inert).
// A PAGE form, not a modal: the page shell (`TaskDetailPage`) owns scroll locking and viewport handling.
// `is_plannable` has no control (AC7.6); its column is still read by `doneWarnReason` for legacy rows.
// A setting's explanation is its label's hover (`ui/HelpLabel`).
// On success the host navigates/refreshes; on error the message shows and inputs are kept.
export default function TaskEditForm({
  mode,
  chat = false,
  task,
  liveTask,
  projects,
  filterProjectId = null,
  initialBody = '',
  createDefaults: orgCreateDefaults,
  duplicateSeed,
  prefill,
  onSaved,
  onCancel,
  onActionError,
  onSavedNotRestarted,
  onFiled,
  readOnly = false,
  canStart = true,
  titleDraft,
  projectIdDraft,
  featureIdDraft,
  onDirtyChange,
  submitRef,
  rating,
}: {
  mode: 'create' | 'edit'
  /* Did this create open at the CHAT position? A chat is an ordinary task with the `advisor` persona, created
   * `autonomous=false` and talked to in the row's Web Console — Involvement's third position. This is only a
   * PRESET (New Chat and the list's 💬 are shortcuts to a position); everything on the ordinary create form
   * stays reachable, and moving off `chat` makes it an ordinary task.
   * CREATE ONLY (operator decision D3): an edit must seed both axes from the ROW, or it would rewrite that
   * row's persona. Reduced against `mode` once, on `chatPreset`. */
  chat?: boolean
  task?: PipelineTask
  /* The row as the host holds it NOW, when that is newer than `task`: the page keeps `task` at the row the form
   * opened on while it is dirty (`useTaskDetailLoad`'s `formTask`). Go, its reset confirm and the done rule read
   * this one, so a held `ready` row that has since gone `awaiting_response` never offers a Go that
   * would reset its waiting runner unasked. EDIT ONLY; defaults to `task`. */
  liveTask?: PipelineTask
  /* Every project the operator may see (`GET /api/projects`). The task's
   * project is its scope (`formProject`): its model default, its org, its repo. */
  projects: Project[]
  // The `/tasks` board's project, the first rung of `resolveCreateProject` (`shared.tsx`). Silent.
  // Irrelevant in edit mode.
  filterProjectId?: number | null
  // The Description a CREATE opens with (the inline row's Expand carries what was typed). Create-only and
  // NON-REACTIVE: it seeds initial state and must never overwrite later typing.
  initialBody?: string
  /* What a NEW task's parameters start at: the operator's `task_default_*` settings, reduced by
   * `useTaskCreateDefaults`. Its fences (a prefill only, operator-created tasks only, never in edit mode) are
   * single-homed in `components/tasks/taskDefaults`. Seeds `autonomous` and the mode/persona state only.
   * Applied by an EFFECT (it resolves after mount); `undefined` = not resolved, so nothing seeds early. */
  createDefaults?: TaskCreateDefaults
  /* The row this create copies (a row's Duplicate). `lib/taskDuplicate.ts` single-homes WHAT a copy carries;
   * this form places the values via the create-mode initialisers. CREATE ONLY and NON-REACTIVE, like
   * `initialBody`; the host (`pages/taskDetail/TaskDetailPage.tsx`) mounts only once the seed has landed, so initialisers
   * suffice. */
  duplicateSeed?: TaskDuplicateSeed
  /* The `?persona=&project_id=` link seed, already validated by the host (`taskPrefill.ts`). CREATE ONLY and
   * NON-REACTIVE, fenced like `chat` (see `prefillSeed`): it seeds the persona and the Description. Its project
   * is the host's to place (the breadcrumb owns the project). */
  prefill?: TaskPrefill
  // Carries the saved task and the button pressed: after a CREATE the host redirects (a Go to the new task).
  onSaved: (saved: PipelineTask, intent: SubmitIntent) => void
  onCancel: () => void
  // A Go whose SAVE landed but whose transition failed: reported UP, because the host re-keys this form on
  // `updated_at` and the SSE refetch remounts it, losing an in-form banner (see `pages/taskDetail/TaskDetailBanners.tsx`).
  // Optional; the in-form banner fires either way.
  onActionError?: (message: string) => void
  /* A Go whose save landed on a row that went running meanwhile, its late reset confirm CANCELLED: the
   * press ended as a save, not the start it was. A host that offers this stays put and says so instead of `onSaved`
   * (whose host may leave as for a Save); without it, `onSaved(saved, 'save')`. EDIT ONLY. */
  onSavedNotRestarted?: (saved: PipelineTask) => void
  /* CREATE ONLY: the row exists (an auto-save tick filed it, or a Save/Go pass wrote it), before its uploads and
   * start, so a host's pick for the row lands before it can run (the header's Live Cast). Answers ''
   * or the refusal, which stops the pass like a refused prerequisite. */
  onFiled?: (row: PipelineTask) => Promise<string>
  /* Read-only for a `tasks:view` principal without `tasks:edit`: every field non-editable, the Description
   * rendered, and the Save/Go/Retry block ABSENT (a greyed Save advertises a capability they lack). A PROP,
   * not `useCan()` here (tests need no permission mock). EDIT-ONLY: create's route is gated on `tasks:create`
   * (`App.tsx`). */
  readOnly?: boolean
  /* May this principal START this task, i.e. may **Go** exist? Narrower than `readOnly`: a `tasks:create`
   * holder may edit and delete their own draft (`PipelineTask.authored_draft`), but `/start`, `/force-start`
   * and `/reset` declare `tasks:edit` (`hooks/useTaskAccess.ts` single-homes the split). So Save survives and
   * Go does not — Go is the PUT then `goTransitions`, and would 403 half-executed.
   * ABSENT, never disabled. It also removes the partial-create retry whose remaining step is `'start'`; a
   * retry still owing images stays. Defaults TRUE. The create surface does not use it: it asks about the
   * PROJECT it will create in (`goPermitted`), resolved in here, so it cannot come down as a prop. */
  canStart?: boolean
  /* The breadcrumb owns the Title and the PROJECT: the page holds their drafts and hands them down (a create
   * submits the project and feature; an edit's crumbs move the task themselves). Optional with a fallback to internal state, so a host without a breadcrumb (unit
   * tests) keeps working. `undefined` = not lifted, distinct from `''`: a blanked title reaches `submit()` as
   * `''`, which the server's `exclude_unset` handling leaves alone. */
  titleDraft?: string
  projectIdDraft?: number
  /* The breadcrumb's staged FEATURE: `null` is the project alone, `undefined` not lifted (the stored filing
   * stands). The page keeps it and `projectIdDraft` in agreement — a feature is always of the staged
   * project — so the PUT never earns the API's mismatch 422. */
  featureIdDraft?: string | null
  /* Reported up so the page can hold its live refetch while the editor has unsaved work. */
  onDirtyChange?: (dirty: boolean) => void
  /* Lets a host run THIS form's save (same path as Save). Resolves to whether it landed. A ref because it
   * closes over render-fresh state; a prop would re-render the host per keystroke. */
  submitRef?: MutableRefObject<(() => Promise<boolean>) | null>
  /* EDIT ONLY: the folded rating (`TaskRating`), drawn among the page's fields (`TaskPageFields`). */
  rating?: ReactNode
}) {
  const maxFileBytes = useMaxFileBytes()
  const isEdit = mode === 'edit'
  /* The D3 fence in one expression: every reader uses `chatPreset`, never the prop — including the two
   * `useState` initialisers, since `task === undefined` is not "is a create" (an edit render without a row
   * is a real call shape). Every reader is a SEED; nothing gates on it. */
  const chatPreset = !isEdit && chat
  /* The link seed under the same fence: an edit never reads it, so it can never rewrite a stored row. */
  const prefillSeed = isEdit ? undefined : prefill
  // A child row gets the action editor; a top-level `epic` row keeps the epic editor (see header).
  const isChildEdit = isEdit && task?.parent_task_id != null
  const { admin } = useAuth()
  // Another org's row, for an operator: the spec's reads that answer for the caller's org go as its org.
  const otherOrg = useOtherOrg(task?.org)
  /* May this principal reach the chat position — `useCanCreateChats` (single home). Read here because both
   * the `createDefaults` seed and the duplicate seed's persona rung need it. */
  const canCreateChats = useCanCreateChats()
  /* `seedRow`: the row the initialisers read — the task being EDITED or COPIED — resolved once so no
   * initialiser disagrees (`lib/taskDuplicate.ts` says what a copy carries). `duplicating` is read by
   * `untouchedRef`: a copy has seeded both axes, so the late `createDefaults` seed must not overwrite them. */
  const duplicating = duplicateSeed !== undefined
  const seedRow: PipelineTask | undefined = duplicateSeed?.source ?? task

  // AC2.3 "no gates": create resolves a default project SILENTLY. The override is the breadcrumb's
  // project menu (`projectIdDraft`); a host without a breadcrumb gets the silent default only. The ladder
  // is `resolveCreateProject`'s (`shared.tsx`), shared with the inline create row. Edit stays in the task's
  // own project: only the crumb's pick may repoint a row, and the PUT carries `project_id` only when it moved.
  /* Fallback values for a host that does not lift them; otherwise never read. */
  /* The seed also asks the grant map: `GET /api/projects` is wider than what the create route accepts (a
   * project the caller may view but not create in), and an untouched preselection is what Save posts. See
   * `resolveCreateProject`. */
  const canOnProject = useCanOnProject()
  const canCreateInProject = useCallback(
    (id: number) => canOnProject('tasks:create', id),
    [canOnProject],
  )
  /* An unchosen default skips a project whose tasks could not be placed (`Project.dispatch_block`): the first
   * creatable unblocked one wins, else the original pick. A board project that resolved is kept. */
  const [ownProjectId] = useState<number | null>(() => {
    if (isEdit) return task?.project_id ?? null
    // A template seed (Customize) takes only the board's project and otherwise leaves it for the author to
    // pick (`taskPrefill.ts`; the bar says "Pick a project").
    if (prefillSeed?.template !== undefined) {
      return projects.some(p => p.id === filterProjectId && canCreateInProject(p.id)) ? filterProjectId : null
    }
    // Never a hidden project, unless it is the board's (`lib/taskMute.ts::withoutHiddenProjects`).
    const visible = withoutHiddenProjects(projects, readMuted(admin?.preferences), [filterProjectId])
    const pick = resolveCreateProject(visible, filterProjectId, canCreateInProject).id
    const chosen = pick !== null && pick === filterProjectId
    if (chosen || !visible.find(p => p.id === pick)?.dispatch_block) return pick
    return resolveCreateProject(visible.filter(p => !p.dispatch_block), null, canCreateInProject).id ?? pick
  })
  const [ownTitle] = useState(task?.title ?? '')
  const projectId: number | null = projectIdDraft ?? ownProjectId
  /* The create seed for THIS project: its own `default_persona` over the org's (`withProjectPersona`). A new value
   * on a project move, so an untouched persona follows the breadcrumb's pick. */
  const projectPersona = projects.find(p => p.id === projectId)?.default_persona
  const createDefaults = useMemo(
    () => orgCreateDefaults && withProjectPersona(orgCreateDefaults, projectPersona),
    [orgCreateDefaults, projectPersona],
  )
  const title = titleDraft ?? ownTitle
  // Create seeds from `initialBody`, edit from the task (`task?.body` wins where both apply).
  const [body, setBody] = useState(() => {
    if (!isEdit && initialBody !== '') return initialBody
    if (prefillSeed?.body !== undefined) return prefillSeed.body
    return task?.body ?? initialBody
  })
  // The persona. CREATE defaults to `coder` (most new tasks are one work item); edit pre-fills from the row
  // (the column is NOT NULL). The two are spelled apart: `task === undefined` IS create.
  /* The chat shortcut presets `advisor` here, so the ONE `persona` state stays the single truth for the
   * payload, the Advanced picker, both axes and the read-out. */
  /* A duplicate opens on the copied row's persona — except an advisor this principal may not create
   * (`canCreateChats`): there is no submit-time guard, so a copied chat would 403 on Save. It degrades to the
   * default persona, the same refusal the `createDefaults` seed makes. */
  const duplicatePersona =
    duplicateSeed === undefined ? undefined
      : duplicateSeed.source.persona === ADVISOR_PERSONA && !canCreateChats ? DEFAULT_PERSONA
      : duplicateSeed.source.persona
  const [persona, setPersona] = useState(
    duplicatePersona
      ?? (task === undefined
        ? (chatPreset ? ADVISOR_PERSONA : (prefillSeed?.persona?.value ?? DEFAULT_PERSONA))
        : shownPersona(task)),
  )
  /* ── DEPTH, as its own state ─────────────────────────────────────────────────────────────────────────
   * Sketch / task / epic. STATE, not derived from the persona (`depthForPersona`'s header): `sketch` and a
   * hand-picked Prototyper `task` write the same persona, so re-deriving would silently promote one to
   * `sketch`. Seeded from the persona: `epic` → epic, prototyper → sketch, else task.
   * Involvement is state too (see `involvement`). Nothing restores anything: a press writes only what is
   * BELOW it, `task` writes the configured default, and leaving the chat leaves the persona as it is. */
  const [depth, setDepth] = useState<TaskDepth>(() => depthForPersona(persona))
  /* ── PLAYBOOK: Depth's fourth position (`playbooks_enabled`) ───────────────────────────────────────────
   * The org playbook this task runs, or null. Seeded from the edited row only (a duplicate does not copy it).
   * May be picked or changed only on a create or a not-yet-Saved row (`playbookOpen`); after Save the server
   * refuses a change, so the pill stays but is fixed. SIMPLE (one stage) keeps the Persona select, seeded from the
   * playbook's `default_persona`; COMPLEX (staged) is epic-shaped: persona `'epic'`, Persona hidden, Model kept
   * as the stages' ceiling. */
  const [playbook, setPlaybook] = useState<TaskPlaybookRef | null>(() => task?.playbook ?? null)
  const playbookOpen = !isEdit || task?.unsaved === true
  /* Has this row ever carried a playbook here? Then a return to a plain depth must SEND `playbook_id: null`
   * (absent means "leave it"); otherwise the key never rides, so an ordinary create body is unchanged. */
  const playbookSentRef = useRef(task?.playbook != null)
  // The server's own reason for refusing a conversion (CONTRACT-4's 400 body), shown beside the control (AC7.5).
  const [personaError, setPersonaError] = useState('')
  // Epic Throttle: create + epic editor only (read off the PARENT epic at dispatch, meaningless on a child;
  // `tasks.py`). 'auto' dispatches a dependency-satisfied child, at most `max_running` at once (null = no cap);
  // 'hold' keeps it `ready` for a manual Run.
  const [transitionMode, setTransitionMode] = useState(seedRow?.transition_mode ?? 'auto')
  const [maxRunning, setMaxRunning] = useState<number | null>(seedRow?.max_running ?? null)
  /* `hidden` — EDIT ONLY, top-level rows only (`_apply_hidden`, apps/api routes/tasks/update.py: a child is hidden
   * with its epic). Independent of the persona: converting a system row never un-hides it. */
  const [hidden, setHidden] = useState(task?.hidden ?? false)
  /* Who may flip it: the server's `may_set_hidden` (creator or superadmin — a hidden row is theirs alone, so
   * anyone else hiding it would lose it). Shown ⇔ submitted. */
  const showsHidden = isEdit && !isChildEdit && task?.may_set_hidden === true
  // R4: Full Auto — ON for create, the row's own value in the action editor; the epic editor never submits it.
  /* ── INVOLVEMENT, as its own state; `autonomous` is DERIVED from it ──────────────────────────────────
   * The axis is the state and the column its projection: `chat` writes the advisor persona only at the `task`
   * depth, so at `sketch` chat and guided store the same pair and no derivation can tell them apart. ONE
   * state: a second `autonomous` kept in step by hand is shown-≠-submitted drift (it once shipped an
   * advisor lit as `chat` over a POSTed `autonomous: true`). `autonomousForInvolvement` projects on render.
   * A chat is the one create that starts OFF, so the shortcut presets the position. Seeded from
   * `involvementForTask` (the row's pair) once at mount. */
  const [involvement, setInvolvement] = useState<TaskInvolvement>(() => {
    if (chatPreset && task === undefined) return 'chat'
    /* Off the RESOLVED `persona`, so a chat copied by a principal who may not create one lands on `guided`. */
    return involvementForTask(
      persona, seedRow?.autonomous ?? prefillSeed?.template?.autonomous ?? prefillSeed?.autonomous ?? true,
    )
  })
  const autonomous = autonomousForInvolvement(involvement)
  /* ── The create-time seed, applied when `createDefaults` ARRIVES ──────────────────────────────────────
   * The form mounts before the identity payload, so `useState` cannot see the settings; like
   * `TaskDetailPage`'s `draftProjectId` effect, seed on arrival, create mode only, never over a moved value.
   * "Untouched" is TRACKED, not inferred from equality with the default: moving a control and back would
   * otherwise be re-seeded silently.
   * Two flags: `persona` (the Depth axis or the picker being used ends it) and `autonomous` (any press on the
   * Involvement axis ends it; named for `task_default_autonomous`). A ref: nothing renders off them. */
  /* A duplicate counts as touched: both axes were seeded from the copied row, and the late seed would
   * half-undo the copy. Two fields, not one: each is retired by its own control. */
  /* A link-seeded persona or involvement counts as touched too: the late defaults seed must not replace what the
   * link asked for. */
  const untouchedRef = useRef({
    autonomous: !duplicating && prefillSeed?.template === undefined && prefillSeed?.autonomous === undefined,
    persona: !duplicating && prefillSeed?.persona === undefined,
  })
  /* May this principal reach the chat position (`useCanCreateChats`: `worktrees:edit` on top of
   * `tasks:create`)? The position lives on a control every create renders, so the control must carry the
   * gate or an ungranted principal picks `chat` and 403s. Read here: the defaults seed needs it too. */
  useEffect(() => {
    /* `chatPreset` joins the early return: the create defaults answer "what should a new TASK start at", and
     * New Chat must land on the position it names (not an architect, not a Full Auto chat). */
    if (isEdit || chatPreset || createDefaults === undefined) return
    /* The configured default persona may itself be the advisor, making this seed a CHAT. Granted: seed the
     * PAIR (`seedsChat`), or an advisor would sit on `unattended`. Ungranted: the plain default (a chat would
     * paint the withheld position back and 403 on save). An epic default (`complex`) outranks both. */
    const seedsChat = !createDefaults.complex
      && createDefaults.persona === ADVISOR_PERSONA
      && canCreateChats
    const seedPersona = createDefaults.persona === ADVISOR_PERSONA && !seedsChat
      ? DEFAULT_PERSONA
      : createDefaults.persona
    /* The POSITION, not the boolean: `autonomousForInvolvement('chat')` and `guided` both say false. */
    if (untouchedRef.current.autonomous) {
      setInvolvement(seedsChat ? 'chat' : (createDefaults.autonomous ? 'unattended' : 'guided'))
    }
    // `'epic'` is what the `epic` depth means (read back through `isEpicPersona`).
    if (untouchedRef.current.persona) {
      setPersona(createDefaults.complex ? 'epic' : seedPersona)
      /* The Depth axis moves with the seeded persona under the same flag, or a `complex` default would seed an
       * EPIC with the axis reading `task`. `complex ? 'epic' : 'task'`, not `depthForPersona`: `sketch` also seeds
       * a model, which is a press, not a preference. */
      setDepth(createDefaults.complex ? 'epic' : 'task')
    }
  }, [isEdit, chatPreset, createDefaults, canCreateChats])
  /* The model pin (`tasks.model`, the top rung of dispatch model precedence), seeded from the STORED value;
   * `MODEL_INHERIT` = not pinned / null. Superadmin-only (`showsModelPin`), but enforcement is server-side
   * (`update_task` refuses others) — not drawing a field is UX, not access control. */
  const [model, setModel] = useState(seedRow?.model ?? prefillSeed?.template?.model ?? MODEL_INHERIT)
  /* The provider pin (`tasks.bound_provider`: dispatch offers only that vendor's accounts), seeded from the STORED
   * value — the operator's pin or the vendor its first dispatch stamped. `PROVIDER_ANY` = unbound / null. Same
   * gate as the model pin (`showsModelPin`), enforced server-side (`_provider_pin_or_refuse`). From `task`, never
   * a duplicate's source: pin and stamp are one column, so copying it would lock the copy to whoever ran the
   * original (`lib/taskDuplicate.ts`). */
  const [provider, setProvider] = useState(task?.bound_provider ?? PROVIDER_ANY)
  /* The SDK half of the one agent picker (`tasks.agent_id`), from `task` like the provider (a duplicate starts
   * on Default). At most one of `provider`/`agentId` is set: the picker writes both on every pick. */
  const [agentId, setAgentId] = useState<string | null>(task?.agent_id ?? null)
  /* The router's recommendation this person took with "Use this" (`RecommendationBlock`): the Config itself, sent
   * back as `routing_accepted_config` while the picker still holds it. A later picker move drops it (`applySpec`). */
  const [acceptedPick, setAcceptedPick] = useState<Record<string, unknown> | null>(null)
  /* Whether the recommendation block shows a pick now (`RecommendationBlock.onShown`). */
  const [recommendationShown, setRecommendationShown] = useState(false)
  /* The node pin (`tasks.pinned_node_id`), null = Any. From `task`, never a duplicate's source (a copy
   * starts on Any). Under `taskNodePinShown`, enforced server-side (`_pinned_node_or_refuse`). */
  const [pinnedNodeId, setPinnedNodeId] = useState<string | null>(task?.pinned_node_id ?? null)
  /* The account pin (`tasks.bound_account_id`), null = Any. From `task`, never a duplicate's source (a copy starts on
   * Any). Create and edit alike (`showsAccountPicker`), so a row stuck on a bad account can be re-pinned in place
   * (`_account_pick_or_422`, which also stamps the account's provider). On create, once one was sent, Any is sent
   * too, so a later auto-save PUT can take it back; on edit only a change rides. */
  const [accountId, setAccountId] = useState<string | null>(task?.bound_account_id ?? null)
  const accountSentRef = useRef(false)
  /* ── ASSIGNED TO — whose inbox this task's notifications land in (`PipelineTask.assigned_to_id`) ─────────
   * Edit seeds from the row; create from the signed-in principal (`admin.id`): the API refuses an empty value
   * (`PUT` with null is a 422), so `''` means only "no principal yet", and `assigneeField` then omits the key.
   * A STRING ID, never the `LookupOption` (derived by `assignedToOption`), so a late roster re-labels without
   * moving the value. */
  const [assignedToId, setAssignedToId] = useState<string>(() =>
    isEdit ? (task?.assigned_to_id ?? '') : (admin?.id ?? ''))
  /* ── PRIORITY — the dispatch order, a named level off `PRIORITY_LEVELS` stored as `Task.priority` ─────────
   * On every row, an epic included (the server copies it to children at fan-out); seeded off `seedRow`.
   * CREATE rides `TaskCreate` (`priorityCreateField`); EDIT goes through the set-priority route
   * (`PATCH /api/tasks/{id}`), only when it moved — see `submit()`. */
  const [priority, setPriority] = useState<number>(seedRow?.priority ?? DEFAULT_PRIORITY)
  /* ── FROM ISSUE (`components/trackers/IssuePicker.tsx`) ───────────────────────────────────────────────
   * Seeded from the STORED link (a duplicate's from its source); submit sends it only when the operator moved it.
   * A duplicate's link is a hand link on the copy, whatever started the source. */
  const issuePick = useIssuePick({
    stored: duplicateSeed !== undefined && seedRow?.tracker_issue ? { ...seedRow.tracker_issue, origin: 'manual' } : seedRow?.tracker_issue,
    storedProjectId: seedRow?.project_id ?? null,
    body: () => bodyRef.current,
    setBody: text => setBody(text),
  })
  // The in-app confirm before Go's destructive `reset` step (see `submit`), rendered once at the end.
  const [confirm, confirmDialog] = useConfirm()
  /* An acting operator's Go asks whom to push as (`PushIdentityDialog.tsx`, the row Start's same ask) before any
   * write; the answer rides this pass's `start` POST. `{}` when not acting. */
  const acting = useActingAs()
  const pushIdentity = usePushIdentity()
  const pushAsRef = useRef<PushIdentity>({})
  /* Acting, a create's default assignee is a member who may start here (the ask's own list): the operator is no
   * member, so their own default opened every Start with "The assignee can't start tasks". `null`
   * when not acting, not yet read, or the operator may start here themselves: the author stays the default. */
  const [actingAssignee, setActingAssignee] = useState<string | null>(null)
  // The member this pick last set, so a project move or leaving act-as re-picks over it as over the author.
  const autoAssigneeRef = useRef<string | null>(null)
  const actingOrgId = acting?.id ?? null
  useEffect(() => {
    setActingAssignee(null)
    if (isEdit) return undefined
    const author = admin?.id ?? ''
    // Only over an untouched default (the author, or the last pick): an assignee the operator picked stays.
    const settleTo = (next: string | null) => {
      const prior = autoAssigneeRef.current
      autoAssigneeRef.current = next
      setAssignedToId(current => (current === '' || current === author || current === prior ? next ?? author : current))
    }
    if (actingOrgId === null || projectId === null) {
      if (autoAssigneeRef.current !== null) settleTo(null)
      return undefined
    }
    let live = true
    readStarters(projectId)
      .then(rows => {
        if (!live) return
        // The server's order (`?may_start=true`); the operator, when they may start, keeps their own default.
        const pick = rows.some(m => m.id === author) ? null : rows[0]?.id ?? null
        setActingAssignee(pick)
        settleTo(pick)
      })
      // Unread, the author is the default and Start's ask says why.
      .catch(() => { if (live) settleTo(null) })
    return () => { live = false }
  }, [isEdit, actingOrgId, projectId, admin?.id])
  // WHICH button is in flight, so only the pressed one reads "Saving…"; `submitting` keeps its boolean
  // meaning for `disabled=`.
  const [submittingIntent, setSubmittingIntent] = useState<SubmitIntent | null>(null)
  const submitting = submittingIntent !== null
  // Every field's `disabled=` reads this: a write in flight and a principal who may never write answer "may I
  // type here" alike.
  const locked = submitting || readOnly
  // The intent of a create that got part way (uploads or start failed), so the retry finishes the SAME
  // intent — a retried Go must not degrade into a save.
  const [createIntent, setCreateIntent] = useState<SubmitIntent>('save')
  // What a partial create still owes (images, start, prereq); names the retry button's step.
  const [retryStage, setRetryStage] = useState<RetryStage | null>(null)
  const [error, setError] = useState('')
  /** The org a create was refused in for acting as another org (`ActAsToCreate`); cleared by the next submit. */
  const [refusedOrg, setRefusedOrg] = useState<OrgRef | null>(null)

  /* ── THE PREREQUISITE PANEL'S STAGED EDITS ───────────────────────────────────────────────────────────
   * Why the panel stages: `TaskDependsOn` (`./TaskDependencies`). This form owns the FLUSH (`flushPrereqs`,
   * three call sites in `submit()`). A ref to trigger it (re-registered per render, like `submitRef`) and a
   * boolean the dirty gate reads (state, since it must re-render). */
  const prereqRef = useRef<PrereqCommitHandle | null>(null)
  const [prereqStaged, setPrereqStaged] = useState(false)

  // No `visualViewport` publisher and no `body.overflow` lock here: `TaskDetailPage` owns both for the page
  // (AC1c.4 + AD-10); a lock here would leave `body` locked after visiting `/tasks/new`.

  // Create has no task id, so a pasted/dropped/picked image's bytes are staged
  // here (token filename → File) until Save; edit uploads immediately (`handleUploadImage`). A ref: staging
  // needs no render.
  /* Seeded from a duplicate: the copied body references the source's `attachment:<token>` verbatim and the
   * token IS the stored filename (`lib/taskDuplicate.ts`). A COPY of the seed's map: the paste path mutates
   * this ref in place. */
  const stagedImagesRef = useRef<Map<string, File> | null>(null)
  // Lazily (`??=`), so a Map is not built and discarded every render.
  stagedImagesRef.current ??= new Map(duplicateSeed?.inlineImages ?? prefillSeed?.staged?.inlineImages)
  const stagedImages = stagedImagesRef.current
  /* Staging for deliberately ATTACHED files, deliberately not the map above: inline images are keyed by the
   * body's `attachment:<token>` (so a pasted-then-deleted image is skipped), while attached files have no body
   * reference and may share a filename. Hence a LIST of `{ key, file }` with minted keys, in STATE (chips
   * render from it). Create only; edit uploads on pick. */
  // A duplicate or ticket seed opens with the source's files the body does not show inline already staged, uploaded on Save.
  const [stagedAttachments, setStagedAttachments] = useState<File[]>(
    () => duplicateSeed ? duplicateSeed.files.map(entry => entry.file) : [...(prefillSeed?.staged?.files ?? [])],
  )
  // Tokens already uploaded; a retry pass re-attempts only the rest.
  const [uploadedTokens, setUploadedTokens] = useState<Set<string>>(new Set())
  // The created Task once a create-mode Save has POSTed: a later Save only retries what failed, never
  // re-creates, and hands `onSaved` the SAME task.
  const [createdTask, setCreatedTask] = useState<PipelineTask | null>(null)

  // The retry button, focused when it appears: it REPLACES the Save/Go pair (React unmounts that subtree),
  // which would leave focus on `<body>`. `submitting` is in the deps because entering the retry state takes
  // two renders; focusing earlier hits a still-disabled button.
  const retryButtonRef = useRef<HTMLButtonElement>(null)
  /* A half-landed create is `retryStage !== null`, not `createdTask !== null`: auto-save sets `createdTask` on
   * every create. `submit` sets `retryStage` when an upload/start fails and clears it on success. */
  const inRetry = !isEdit && retryStage !== null
  useEffect(() => {
    if (inRetry && !submitting) retryButtonRef.current?.focus()
  }, [inRetry, submitting])

  // Edit mode resolves `attachment:<token>` against the task's uploaded attachments (C1's content route).
  const editResolver = useMemo<AttachmentResolver | undefined>(
    () => (isEdit && task ? createAttachmentResolver(task.id) : undefined),
    [isEdit, task],
  )
  /* A create seeded from a template (Customize) paints the template's own images from the template's content route
     (`taskPrefill.ts::TemplateSeed.resolve`); nothing is re-uploaded — the create copies each one the body still names
     (`routes/tasks/create.py`). */
  const templateSeed = prefillSeed?.template
  // ONE resolver for both modes, staged-first (`useStagedImagePaint`); only a token this form did not stage goes
  // to the server. Unmount releases every paint: the upload has landed, and the fresh mount uses `editResolver`.
  const { resolve: resolveAttachmentSrc, release: releaseStagedPaint } = useStagedImagePaint(
    token => stagedImages.get(token),
    editResolver ?? templateSeed?.resolve,
  )

  // No project at all: nothing to create in.
  const noProjects = !isEdit && projects.length === 0
  // The client gate mirrors the server's 422 (non-empty body), trimmed, and a create needs a project from the
  // roster. Edit needs a non-empty body only — Title is optional (AC2.2/AC2.3; a cleared Title keeps the
  // stored one).
  const selectedProjectKnown = projectId !== null && projects.some(p => p.id === projectId)
  // `!readOnly` on the GATE: `submit()` early-returns on `!canSubmit`, so a read-only form cannot write
  // however its submit is reached.
  const canSubmit = !readOnly && (isEdit
    ? body.trim() !== '' && !submitting
    : inRetry
      ? !submitting // AC-7/Risk 1 retry pass: the task is already created, only images are left to retry
      // `inRetry`, not `createdTask !== null` (auto-save): an emptied Description must still disable Save.
      : selectedProjectKnown && body.trim() !== '' && !submitting)

  /* The render gates the payload must agree with (shown ⇔ submitted), from the ONE home `TaskSpecFields` renders
   * by (`taskSpecGates`). Full Auto follows OWNERSHIP, not dispatch: create, an EPIC (its switch governs its two
   * interviewing children) and a top-level non-epic row each own a value; an epic CHILD owns none (its column
   * is inert in both arms of `_task_effective_autonomous`: BA/architecture read the epic's, plan/coder/finish
   * are always Full Auto). The model pin is an org admin's, on a dispatched row or a staged playbook. */
  const isOrgAdmin = isOrgAdminSeat(admin)
  /* The pick rides this form's own write (`tasks:create` on create, `tasks:edit` on edit), so it is offered
   * only where that write is held ON THIS PROJECT; before a project resolves, any grant counts. */
  const canPickAgentAnywhere = useCanPickAgent()
  const canPickAgent = projectId === null
    ? canPickAgentAnywhere
    : canOnProject(isEdit ? 'tasks:edit' : 'tasks:create', projectId)
  const { dispatchedRow, showsModelPin, showsAgentPicker, showsAutonomous } =
    taskSpecGates(persona, playbook, isOrgAdmin, isChildEdit, canPickAgent)
  const showsNodePin = taskNodePinShown('task', admin, dispatchedRow)
  const showsAccountPicker = dispatchedRow

  /* ── WHICH PROJECT THIS FORM IS IN, AND WHETHER IT LINKS A TRACKER ─────────────────────────────────────────
   * Also the `task` Depth's model default (`TaskSpecFields`' `defaultModel`). `projectId` (the task's own, or the
   * crumb's staged move; on create the resolved target) in `projects`. The From issue field shows when the project
   * links a tracker project, or the form already holds a link (so it can be cleared); with no tracker linked it shows
   * disabled, pointing at the project's Issue trackers; never read-only, so submit cannot write a link without a
   * visible control. */
  const formProject = projectId === null ? undefined : projects.find(p => p.id === projectId)
  const projectHasTrackers = useProjectHasTrackers(readOnly ? null : projectId)
  const previewOrg = useOtherOrg(formProject?.org ?? task?.org)

  /* Is the submitted row a chat? Off the PERSONA (what is submitted), not the Involvement position (`chat`
   * writes the advisor only at `task`). The Description placeholder and Depends-on panel all
   * read this ONE const; keying some on the position once promised a conversation over a prototyper run. */
  const submitsChatRow = persona === ADVISOR_PERSONA

  /* ── A LINK IS ITS PROJECT'S OR NOBODY'S ───────────────────────────────────────────────────────────────────
   * A pick, a stored link and a duplicate's copy each belong to the project they came from (`heldElsewhere`). Once
   * the form's project moves (the crumb; a duplicate landing elsewhere, `resolveCreateProject` falls through
   * silently), the link is neither shown nor sent: the server would refuse the whole save. A copy also needs its
   * project to still link a tracker. Re-derived every render. */
  const heldIssue = issuePick.held
  const copiedIssue = duplicateSeed !== undefined && heldIssue !== null
    && heldIssue.connection_id === duplicateSeed.source.tracker_issue?.connection_id
    && heldIssue.key === duplicateSeed.source.tracker_issue?.key
  const issueIsForeign = heldElsewhere(heldIssue, projectId) || (copiedIssue && projectHasTrackers !== true)
  const issueLinked = projectHasTrackers === true || (heldIssue !== null && !issueIsForeign)
  // Known to link none (not while the read is in flight): the field shows disabled with the way to link one.
  const issueUnlinked = !issueLinked && projectHasTrackers === false
  const issueShown = !readOnly && projectId !== null && (issueLinked || issueUnlinked)
  /* `tracker_issue` only when the operator MOVED it (`issueField`), so unrelated writes leave the link alone. */
  const issueFields = readOnly || issueIsForeign ? {} : issueField(issuePick, isEdit ? task?.tracker_issue : null)
  /* The assignee on every write (POST, both PUT arms, each auto-save tick; the server suppresses the notice on
   * `autosave`). Sent whenever held (a resend is a no-op); omitted when empty (would 422). One spread. */
  const assigneeField = assignedToId !== '' ? { assigned_to_id: assignedToId } : {}
  /* Priority on a CREATE POST only (`TaskUpdate` lacks it; edit PATCHes the action route). Omitted at the
   * default, so an ordinary create body is unchanged. */
  const priorityCreateField = priority !== DEFAULT_PRIORITY ? { priority } : {}
  /* The template a Customize create came from, on the create POST only: the server links the row, copies the images
   * the body names and counts the start once (`routes/tasks/create.py::_link_template`). */
  const templateCreateField = templateSeed ? { template_id: templateSeed.id } : {}
  /* Has the select moved off what the row STORES — shared by the dirty gate and both PATCH sites. `??
   * DEFAULT_PRIORITY` mirrors the seed. */
  const priorityChangedFrom = (row: PipelineTask) => priority !== (row.priority ?? DEFAULT_PRIORITY)
  /* The Title the picked issue's SUMMARY fills: only an empty Title on EDIT (`TaskCreate` has no `title`;
   * `create_task` derives it from the body). Evaluated at SUBMIT, so a typed title is never overwritten. */
  const issueTitleFill = isEdit && title.trim() === '' && issuePick.state.kind === 'done' ? issuePick.state.issue.title : ''
  /* The Description NOW, for a question asked across an await. */
  const bodyRef = useRef(body)
  bodyRef.current = body

  // Against the persona the picker OPENED on (`shownPersona`): only moving it posts a conversion.
  const personaChanged = isEdit && task !== undefined && persona !== shownPersona(task)
  // The conversion warning only when true: moved off Epic, and the epic has live subtasks (`child_count`,
  // from `list_tasks`) to stop and archive.
  /* `!isEpicPersona(persona)`, not `persona === 'coder'`: picking Architect after leaving `epic` hid the
   * warning while the server still archived the children. */
  const warnsPersonaTeardown = personaChanged && !isEpicPersona(persona) && (task?.child_count ?? 0) > 0
  // Caused by: it rides Save after the PUT, like priority (`TaskPageFields`).
  const pageFields = useTaskPageFields({ task, isEdit, readOnly })

  /* ── UNSAVED WORK, reported UP ─────────────────────────────────────────────────────────────────────────
   * The page gates the "Tasks" crumb on this (there is no router blocker in the app). Field-by-field against
   * the STORED row, so typing and deleting leaves it clean. `title` is included: the breadcrumb edits it and
   * it rides this submit. The project and feature crumbs are not: a pick moves the task at once
   * (`useTaskDetailFiling`'s `moveNow`), so it is never unsaved work. CREATE: dirty = something to lose. */
  const dirty = isEdit && task !== undefined
    ? title !== (task.title ?? '')
      || body !== (task.body ?? '')
      || autonomous !== (task.autonomous ?? true)
      || personaChanged
      || (playbook?.id ?? null) !== (task.playbook?.id ?? null)
      || transitionMode !== (task.transition_mode ?? 'auto')
      || maxRunning !== (task.max_running ?? null)
      || hidden !== (task.hidden ?? false)
      // The model pin alone is a real edit (`?? MODEL_INHERIT` mirrors the seed).
      || model !== (task.model ?? MODEL_INHERIT)
      // The provider pin alone is a real edit (`?? PROVIDER_ANY` mirrors the seed).
      || provider !== (task.bound_provider ?? PROVIDER_ANY)
      // The agent pick alone is a real edit.
      || agentId !== (task.agent_id ?? null)
      // The node pin alone is a real edit.
      || pinnedNodeId !== (task.pinned_node_id ?? null)
      // The account pin alone is a real edit.
      || accountId !== (task.bound_account_id ?? null)
      // The issue link alone is a real edit.
      || 'tracker_issue' in issueFields
      // The assignee alone is a real edit (`?? ''` mirrors the seed).
      || assignedToId !== (task.assigned_to_id ?? '')
      // The priority alone is a real edit (it rides its own PATCH).
      || priorityChangedFrom(task)
      // Caused by, its own write after the PUT.
      || pageFields.dirty
      // The prerequisite panel diffs against its fetched graph and reports only the answer.
      || prereqStaged
    // CREATE only: staged attachments are held in the browser until Save. Edit uploads on pick.
    : body.trim() !== '' || prereqStaged || stagedAttachments.length > 0
  useEffect(() => { onDirtyChange?.(dirty) }, [dirty, onDirtyChange])

  /* ── DISCARD IS A CREATE-SURFACE WORD ──────────────────────────────────────────────────────────────────
   * On an existing task auto-save has already written the edits, so "Discard" would promise a loss that does
   * not happen; the edit surface offers CLOSE (no confirm, nothing to lose). On CREATE Discard is exact (the
   * text or an auto-filed "New task" row, which stays — nothing is auto-deleted). It confirms on a dirty
   * create form (it sits beside Save with the opposite meaning); a clean form never asks. */
  const offersDiscard = !isEdit
  async function discardAndCancel() {
    if (offersDiscard && dirty) {
      /* Two subjects, both create-surface (see `DiscardSubject`): an auto-saved or half-landed create EXISTS on
       * the server, so "nothing is created" would be false. */
      const subject: DiscardSubject = createdTask !== null
        ? { kind: 'created', title: createdTask.title ?? '' }
        : { kind: 'create' }
      const ok = await confirm(discardConfirmOptions(subject))
      if (!ok) return
    }
    onCancel()
  }

  /* A host's save runs THIS form's `submit` (one write path). Re-registered every render (it closes over
   * fresh state); cleared on unmount. */
  useEffect(() => {
    if (!submitRef) return
    submitRef.current = () => submit(undefined, 'save')
    return () => { submitRef.current = null }
  })

  /* ── WHEN SAVE AND GO GO DEAD ─────────────────────────────────────────────────────────────────────────
   * Two independent reasons, one button each.
   * (1) NOTHING HAS CHANGED → withholds SAVE (EDIT only: a clean re-PUT just moves `updated_at`; on create
   *     `dirty` is already required by `canSubmit`). Go is NOT in this term (operator decision): an unchanged
   *     runnable task's Go means START THIS, and it still saves first on the one code path — EXCEPT on a
   *     `done` row, where it withholds Go too (operator decision): re-running finished work
   *     unchanged is a misclick, so Go follows Save there. The row's own Start/Re-run is untouched.
   * (2) THIS ROW CANNOT BE STARTED → withholds GO alone (Save stays: a running task is saved, then
   *     stopped/started from its row). Read off the transition table, `can(row, 'start')` (`controls.ts`:
   *     the row's `available_transitions` from `task_lifecycle.py`, else the generated snapshot). `active` is
   *     `start`'s idempotent no-op, so every "Running" row — an epic included, by operator decision — is
   *     excluded; an out-of-vocabulary state is excluded too. So is a `ready` row the org holds (`wait_reason`):
   *     already started, it starts on its own once the hold clears.
   * Permission is separate: `goPermitted` withholds Go ABSENTLY. */
  const goRow = isEdit ? (liveTask ?? task) : undefined
  const goNotStartable = goRow !== undefined && (!can(goRow, 'start') || (goRow.state === 'ready' && !!goRow.wait_reason))

  /* ── MAY THIS PRINCIPAL PRESS GO AT ALL ────────────────────────────────────────────────────────────────
   * EDIT: `canStart` from the host. CREATE: asked here about the project this form will create in (the host
   * cannot know it: `projectIdDraft` is `undefined` until the crumb moves, and the default resolves in here), so
   * a `tasks:create`-without-`tasks:edit` principal is not offered a Go whose `start` would 403 after the POST.
   * Keys and routes: `hooks/useTaskAccess.ts`. ABSENT, never disabled; Save untouched. An unresolved scope
   * answers no (fail-closed; `canSubmit` is false then anyway). */
  const canRunOnProject = useCanRunTasksOnProject()
  const goPermitted = canStart && (isEdit || canRunOnProject(projectId))
  const noChanges = isEdit && !dirty
  const saveDisabled = !canSubmit || noChanges
  const goDoneUnchanged = noChanges && goRow?.state === 'done'
  /* A pending org starts nothing (the API 403s it): Go is disabled with the reason, Save stays (a draft is fine). */
  const awaitingApproval = useAwaitingApprovalReason()
  /* A create into a project nothing can run (`Project.dispatch_block`) would start a task that sits unplaced: Go is
   * disabled with the server's sentence on its hover; Save stays (a draft is fine). Edit is not gated here. A missing
   * coding-agent account (`no_account`) does not block a picked SDK agent, which needs none. */
  const createProject = isEdit ? undefined : projects.find(p => p.id === projectId)
  const dispatchBlock = createProject?.dispatch_block_kind === 'no_account' && agentId !== null
    ? null
    : (createProject?.dispatch_block ?? null)
  /* Go held off by the row or project, not by an empty or in-flight form. */
  const goBlocked = goNotStartable || goDoneUnchanged || awaitingApproval !== undefined || dispatchBlock !== null
  const goDisabled = !canSubmit || goBlocked
  /* Hover text per button: a withheld action explains itself. `''` renders no tooltip (an empty create
   * description). One reason per button, so no precedence: `NO_CHANGES_HINT` is Save's alone.
   * While the ROW cannot start (running, queued, waiting on a question), Go is withdrawn rather than greyed and
   * nothing on the bar says why: the header's status chip already does. */
  /* A Jaah suggestion (`isSuggestion`): the header's Accept/Discard (`SuggestionActions`) are its one set, so the bar
   * keeps only Save — Go would be a second Accept, Close a second way out. */
  const suggestionDraft = isEdit && task !== undefined && isSuggestion(task)
  const goHidden = suggestionDraft || (!inRetry && goNotStartable && awaitingApproval === undefined && dispatchBlock === null)
  const goTooltip = awaitingApproval ? awaitingApproval
    : dispatchBlock !== null ? dispatchBlock
    : goDoneUnchanged ? DONE_GO_HINT
    : canSubmit ? (acting ? PUSH_AS_HINT : 'Save and start this task') : ''
  /* A create with no project yet (Customize outside a project scope): Save is dead for that reason alone, so the
   * Save's hover says it. */
  const needsProject = !isEdit && !inRetry && !readOnly && !selectedProjectKnown
  const saveTooltip = noChanges ? NO_CHANGES_HINT : needsProject ? PICK_PROJECT_HINT : ''

  // RichTextEditor's upload contract: once per inserted image with the ORIGINAL file and the MINTED token
  // filename in the body. Edit uploads now; create only stages (see submit()).
  async function handleUploadImage(file: File, filename: string) {
    /* The cap is checked before staging, in both modes: `TaskAttachmentsField`'s pick-time check cannot see a
     * pasted image, and an over-cap stage would wedge the create at submit. The node is stripped
     * (`stripAttachmentImage`). */
    if (file.size > maxFileBytes) {
      setBody(prev => stripAttachmentImage(prev, filename))
      setError(imageOverCapMessage(file, maxFileBytes))
      return
    }
    // Staged in BOTH modes and SYNCHRONOUSLY: the node view resolves on the next commit (`resolveAttachmentSrc`
    // reads this). Edit mode keeps the entry after upload to hold the paint; `uploadPendingStagedImages` is
    // only reached from submit()'s create branches.
    stagedImages.set(filename, file)
    if (isEdit && task) {
      const failure = await uploadAttachment(task.id, filename, file)
      if (failure !== null) {
        // Un-stage a failed upload (file and object URL): nothing is stored under this token. Create mode never
        // lands here (it has a retry pass from this map).
        stagedImages.delete(filename)
        releaseStagedPaint(filename)
        /* And strip the node: edit mode has no retry channel, so a dangling ref would stay as a permanent
         * `⚠ image unavailable` dispatched to a runner. The banner carries the server's reason. */
        setBody(prev => stripAttachmentImage(prev, filename))
        setError(
          `Failed to upload ${file.name || 'image'}: ${failure}. It was removed from the description `
          + `— try pasting or dropping it again.`,
        )
      }
    }
  }

  /* Uploads one file under its minted token filename (a fresh File, so the stored name matches the
   * `attachment:<token>` ref). Resolves rather than throws, so a batch continues past a failure (Risk 1).
   * The form's SINGLE upload seam (paste path and both create flush passes); the ticket → PUT → complete
   * sequence lives in `lib/uploadTaskAttachment`, shared with `TaskAttachmentsField`.
   * `null` = landed; a string is the server's refusal via `apiDetail` (e.g. the 413 naming the cap). */
  async function uploadAttachment(
    taskId: number, filename: string, file: File,
  ): Promise<string | null> {
    const tokenFile = new File([file], filename, { type: file.type || 'application/octet-stream' })
    try {
      await uploadTaskAttachment(taskId, tokenFile)
      return null
    } catch (err) {
      return apiDetail(err, 'the upload was refused')
    }
  }

  /* Uploads in flight or landed, by row and token: the auto-save tick and Save both run the pass below, and a Save
   * pressed mid-tick (its render predating the tick's `uploadedTokens` write) must await the tick's upload
   * rather than store the image twice. A failure leaves the map, so the next pass retries. A ref: never renders. */
  const imageUploadsRef = useRef<Map<string, Promise<string | null>>>(new Map())

  // Uploads every staged image not yet uploaded; returns what failed. Skips an image whose token is no longer
  // in `body` (pasted then deleted), which no saved task would reference.
  async function uploadPendingStagedImages(taskId: number): Promise<UploadFailure[]> {
    const pending = Array.from(stagedImages.keys())
      .filter(fn => !uploadedTokens.has(fn))
      .filter(fn => body.includes(`attachment:${fn}`))
    if (pending.length === 0) return []
    const inFlight = imageUploadsRef.current
    const results = await Promise.all(
      pending.map(async filename => {
        const file = stagedImages.get(filename)
        // Keyed by row too: a Save racing the first tick's POST can write a second row, whose image must land there.
        const key = `${taskId}:${filename}`
        let upload = inFlight.get(key)
        if (upload === undefined) {
          // A missing staged file (unreachable today) says so, rather than implying the server refused it.
          upload = file
            ? uploadAttachment(taskId, filename, file)
            : Promise.resolve('the picked file is no longer available in this tab — paste or drop it again')
          inFlight.set(key, upload)
          void upload.then(failure => { if (failure !== null) inFlight.delete(key) })
        }
        const detail = await upload
        // `shown` is the operator's name; `filename` is the minted token, only the ledger key (`UploadFailure.name`).
        return { filename, shown: file?.name || 'image', detail }
      }),
    )
    const succeeded = results.filter(r => r.detail === null).map(r => r.filename)
    if (succeeded.length > 0) setUploadedTokens(prev => new Set([...prev, ...succeeded]))
    return results
      .filter((r): r is { filename: string; shown: string; detail: string } => r.detail !== null)
      .map(r => ({ name: r.shown, detail: r.detail, inline: true }))
  }

  /* The staged-FILE twin of `uploadPendingStagedImages`: every entry goes (no body ref to test), uploaded
   * under its own filesystem name. The ledger is the list itself: a success leaves it, so what remains is what
   * a retry has left (the retry is a later click, so the list is trimmed by then). Returns what still failed. */
  async function uploadPendingStagedFiles(taskId: number): Promise<UploadFailure[]> {
    if (stagedAttachments.length === 0) return []
    const results = await Promise.all(
      stagedAttachments.map(async file => ({
        file,
        detail: await uploadAttachment(taskId, file.name || 'attachment', file),
      })),
    )
    const failed = results.filter(r => r.detail !== null)
    setStagedAttachments(failed.map(r => r.file))
    return failed.map(r => ({
      name: r.file.name || 'attachment', detail: r.detail as string, inline: false,
    }))
  }

  // The `task.start` transition for a task just created — the same registry entry, `'queue'` for the same
  // reason as the inline create (no prior attempt; an EXISTING row reads `startIntentFor`, see
  // `runGoTransitions`). No `can()` gate: the row is `draft` by construction, a `start` source for every
  // persona (`task_lifecycle.py`). Owns its failure (the task EXISTS, so "Failed to create task" would lie):
  // returns the started task, or null after setting the banner and the retry state.
  async function startCreatedTask(created: PipelineTask): Promise<PipelineTask | null> {
    setRetryStage('start')
    try {
      const started = await post<PipelineTask>(CONTROLS.start.path(created.id), startBody('queue', pushAsRef.current))
      setRetryStage(null)
      return started
    } catch (err) {
      setError(
        `Task created, but starting it failed: ${apiDetail(err, 'the start request was refused')} — `
        + `press ${CONTROLS.start.label} to try again.`,
      )
      return null
    }
  }

  // The edit-mode **Go**, against the row the PUT returned: `goTransitions` picks them; this runs them via the
  // `CONTROLS` registry (`controls.ts`). SEQUENTIAL and FAIL-FAST, never `Promise.all` (each step's legality
  // depends on the previous state), reporting WHICH step failed — a landed `reset` killed the runner and
  // released the slot. Owns its failure (the SAVE landed): returns the latest row, or null after the banner.
  async function runGoTransitions(saved: PipelineTask): Promise<PipelineTask | null> {
    let current = saved
    for (const name of goTransitions(saved)) {
      const control = CONTROLS[name]
      try {
        // Only `start` carries a body, its intent from `startIntentFor` (the row buttons' single home) — a
        // hardcoded `'queue'` once made a merged branch undispatchable. `variant` from the row's depth (a child =
        // per-subtask Run, top-level = reconciling re-entry). `reset` takes none.
        current = name === 'start'
          ? await post<PipelineTask>(
              control.path(current.id),
              startBody(startIntentFor(current), pushAsRef.current),
            )
          : await post<PipelineTask>(control.path(current.id))
      } catch (err) {
        const message =
          `Task saved, but ${control.label.toLowerCase()} failed: `
          + `${apiDetail(err, `the ${name} request was refused`)} — the change is stored; use the row's `
          + `own controls to run it.`
        // Both: the in-form banner for a host that does not remount, `onActionError` for one that does.
        setError(message)
        onActionError?.(message)
        return null
      }
    }
    return current
  }

  // The host's own write for a filed create (`onFiled`); a refusal stops the pass before uploads and start.
  async function flushFiled(row: PipelineTask): Promise<boolean> {
    const detail = onFiled ? await onFiled(row) : ''
    if (detail === '') return true
    setError(detail)
    setSubmittingIntent(null)
    return false
  }

  /* Write the prerequisite panel's staged adds/removes against a task that now has an id; answer whether this
   * submit may go on. A refused edge stops here (the save is never rolled back). An empty draft is a no-op.
   * NO HANDLE + A STAGED DRAFT FAILS CLOSED: success would let the host navigate away having dropped edges the
   * operator watched staged. `prereqStaged` is the independent witness. No handle and nothing staged passes. */
  async function flushPrereqs(id: number): Promise<boolean> {
    if (prereqRef.current === null) {
      if (!prereqStaged) return true
      setError('The prerequisite editor is not available, so its unsaved changes could not be written. '
        + 'Reload the page and set them again.')
      setSubmittingIntent(null)
      return false
    }
    const detail = await prereqRef.current.commit(id)
    if (detail === '') return true
    /* CREATE only: the retry control's word comes from `retryStage`, and `null` would read "Retry upload" over
     * a pass that never reached an upload. */
    if (!isEdit) setRetryStage('prereq')
    setError(detail)
    setSubmittingIntent(null)
    return false
  }

  /* ── WHAT EVERY WRITE SUBMITS ───────────────────────────────────────────────────────────────────────────
   * ONE field set for create and edit, every auto-save tick and the final Save (a narrower timer payload
   * would silently drop work). Each key rides on its control's own render gate, so shown ⇔ submitted.
   * `createSurfaceFields`/`editFields` add only what their route needs on top. */
  /* The picker's write: ONE half. An agent pick sends `agent_id` (the server clears the provider); a built-in
   * sends `bound_provider` (which clears the agent). Default on create is the absent field. On edit only what
   * CHANGED: the first claim stamps `bound_provider` server-side, so resending a form opened before it would
   * silently unbind; Default after an agent clears it with `agent_id: null` alone, after a built-in with
   * `bound_provider: ''` (`PROVIDER_ANY`, stored NULL). */
  function agentPickFields(): Record<string, unknown> {
    if (!isEdit) return agentId !== null ? { agent_id: agentId } : provider !== PROVIDER_ANY ? { bound_provider: provider } : {}
    if (agentId !== null) return agentId !== (task?.agent_id ?? null) ? { agent_id: agentId } : {}
    if ((task?.agent_id ?? null) !== null && provider === PROVIDER_ANY) return { agent_id: null }
    return provider !== (task?.bound_provider ?? PROVIDER_ANY) ? { bound_provider: provider } : {}
  }

  /** Does the agent picker still hold this router Config? An agent by id; a built-in by its provider (Default = none). */
  function pickHeld(pick: Record<string, unknown>): boolean {
    if (pick.kind === 'agent') return agentId === pick.agent_id && provider === PROVIDER_ANY
    // Where the form draws the model pin, a model moved off the pick's tier is the person's own choice.
    const tierHeld = !showsModelPin || model === ((pick.model as string | null) ?? (pick.tier as string | null) ?? MODEL_INHERIT)
    return agentId === null && provider === ((pick.provider as string | null) ?? PROVIDER_ANY) && tierHeld
  }

  /* "Use this": the recommendation becomes the picker's pick (and the model pin's, where the form draws one), as a
   * person's own pick would; the next write carries it with `routing_accepted_config`. */
  function takeRecommendation(pick: Record<string, unknown>) {
    if (pick.kind === 'agent') {
      setAgentId(pick.agent_id as string)
      setProvider(PROVIDER_ANY)
    } else {
      setAgentId(null)
      setProvider((pick.provider as string | null) ?? PROVIDER_ANY)
      if (showsModelPin) setModel((pick.model as string | null) ?? (pick.tier as string | null) ?? MODEL_INHERIT)
    }
    setAcceptedPick(pick)
  }

  function formFields() {
    return {
      body: body.trim(),
      // On the same gate as the Throttle control: the columns are read only off a PARENT EPIC. The cap rides only as
      // a real choice (create: a number; edit: a change), so a form opened earlier never reverts another writer's.
      ...(isEpicPersona(persona) ? { transition_mode: transitionMode } : {}),
      ...(isEpicPersona(persona) && maxRunning !== (isEdit ? (task?.max_running ?? null) : null)
        ? { max_running: maxRunning }
        : {}),
      // `showsAutonomous`: false only on an epic child, whose column is inert.
      ...(showsAutonomous ? { autonomous } : {}),
      /* Absent for a non-superadmin and an epic (absent ≠ empty); a chat may pin (no persona rule server-side).
       * `MODEL_INHERIT` is a real value: the API reads `''` as `model_catalog.INHERIT`. */
      ...(showsModelPin ? { model } : {}),
      /* The agent picker (`showsAgentPicker`): ONE half per write, never both (`_AGENT_OR_PROVIDER`). */
      ...(showsAgentPicker ? agentPickFields() : {}),
      /* While a recommendation is on screen, the answer to it: the pick taken with "Use this" while the picker still
       * holds it (`acceptedPick`), else null — a runner change is then the person's own choice. Absent when none was
       * shown, so nothing is recorded for a recommendation nobody saw. */
      ...(showsAgentPicker && recommendationShown
        ? { routing_accepted_config: acceptedPick !== null && pickHeld(acceptedPick) ? acceptedPick : null }
        : {}),
      /* The node pin (`showsNodePin`): only a real choice — Any on create is the absent field, on edit only a change.
       * `null` takes the row back to Any. */
      ...(showsNodePin && pinnedNodeId !== (isEdit ? (task?.pinned_node_id ?? null) : null)
        ? { pinned_node_id: pinnedNodeId }
        : {}),
      /* The account pin (`showsAccountPicker`): only a real choice — on create a pick (or Any after one was sent), on
       * edit only a change — and never beside an agent pick (which runs on none; the PUT 422s the pair). */
      ...(showsAccountPicker && agentId === null
        && (isEdit ? accountId !== (task?.bound_account_id ?? null) : accountId !== null || accountSentRef.current)
        ? { account_id: accountId }
        : {}),
      // The issue link when the picker moved it (`TaskCreate` and the PUT take the same shape).
      ...issueFields,
      ...assigneeField,
      /* On every write while it may still change (create, auto-save, an unsaved row); `null` takes one back. */
      ...(playbookOpen && (playbook !== null || playbookSentRef.current)
        ? { playbook_id: playbook?.id ?? null }
        : {}),
    }
  }

  /* What a CREATE submits (the first auto-save POST, later auto-save PUTs, the final Save). No `title`:
   * `TaskCreate` has none. `persona` is the caller's, always sent: absent, the POST takes the org's default and
   * a PUT leaves it. */
  function createSurfaceFields() {
    // The staged feature rides every create write, `null` included, so a later auto-save PUT can un-file it.
    return {
      project_id: projectId,
      ...(featureIdDraft !== undefined ? { feature_id: featureIdDraft } : {}),
      ...formFields(),
    }
  }

  /* What an EDIT submits. `target` is the row written. */
  function editFields(target: PipelineTask) {
    // AC2.2/AC2.3: an emptied Title OMITS `title` (the server's `exclude_unset` keeps the stored one —
    // `update_task`, apps/api/app/routes/tasks/update.py — rather than 422ing on ''). Exception: an issue pick into an
    // EMPTY Title fills it (`issueTitleFill`); a typed title always wins.
    const titleField = title.trim() !== ''
      ? { title: title.trim() }
      : issueTitleFill !== '' ? { title: issueTitleFill } : {}
    /* No `project_id`/`feature_id`: the crumbs move the task themselves (`useTaskDetailFiling`'s `moveNow`), so a
     * Save never re-sends a filing from a row it may have opened on before the move. */
    return {
      ...titleField,
      ...formFields(),
      // AC7.5/CONTRACT-4: sent ONLY when changed, so no unrequested conversion is posted. `is_plannable` is
      // deliberately ABSENT (AC7.6: no control).
      ...(personaChanged ? { persona } : {}),
      // Sent ONLY when changed, like `persona`. The server ignores it on an auto-save (it lands on Save only).
      ...(showsHidden && hidden !== (target.hidden ?? false) ? { hidden } : {}),
    }
  }

  /* ── AUTO-SAVE: the ticket is on the SERVER before it is finished ──────────────────────────────────────
   * A timer writes what the form holds back to the server, so a closed tab, reload, Back or SSE remount loses
   * nothing. `lib/taskAutosaveInterval.ts` owns the cadence (one org knob, 15s default).
   * It writes the LIVE row (operator ruling, no draft buffer): on edit the task; on create the first tick
   * FILES it `unsaved`, off everyone else's board until Save. The same two builders as the button; the
   * `autosave` flag buys only SILENCE (no activity row, title refinement, snapshot or console event — an event
   * would remount this editor). An INTERVAL, not a debounce ("every 15 seconds" must hold for someone who
   * never pauses); `autosavedRef` keeps an idle editor silent. */
  const autosaveSeconds = useTaskAutosaveSeconds()
  /* The FIELDS as last written (serialized), or null: an idle editor makes no request. The fields, not the
   * payload (see where it is set). A ref: read in the tick, never renders. */
  const autosavedRef = useRef<string | null>(null)
  /* One write in flight at a time: two overlapping first ticks would file TWO rows for one ticket. */
  const autosaveInFlightRef = useRef(false)
  /* The tick in flight, awaited by `submit`, and the row a tick filed, read before React re-renders with it: a Save
   * or Go pressed mid-tick would otherwise see `createdTask === null` and POST a SECOND row, leaving the tick's
   * `unsaved` one behind. `submitPassRef` stops new ticks from the press on (`submitting` lags a render). */
  const autosavePromiseRef = useRef<Promise<void> | null>(null)
  const autosaveFiledRef = useRef<PipelineTask | null>(null)
  const submitPassRef = useRef(false)
  /* Re-registered after every commit from an effect with no deps (like `submitRef`), so the tick sees fresh
   * values. An effect, not a render-phase assignment, which StrictMode or a concurrent re-render could
   * discard or leave stale. */
  const autosaveRef = useRef<() => Promise<void>>(async () => {})
  const runAutosave = async () => {
    if (readOnly || submitting || submitPassRef.current || autosaveInFlightRef.current) return
    /* Nothing to save, nothing saved — load-bearing: on a fresh mount `autosavedRef` is null, and a write of the
     * seeded row would still move the actor stamp and timestamp every 15s forever. `dirty` is the navigation
     * guard's term. NOT `canSubmit`, whose create arm goes permissive once a row exists (it is the retry gate).
     * An empty body 422s on both routes anyway. */
    if (!dirty || body.trim() === '') return
    if (!isEdit && createdTask === null && !selectedProjectKnown) return
    /* A template-seeded create files nothing while its Description is still the template's: the first create POST is
     * what counts the template's start, so an opened-and-abandoned Customize must not. Save files it as ever. */
    if (!isEdit && createdTask === null && templateSeed !== undefined && body === prefillSeed?.body) return
    const target = isEdit && task !== undefined ? task : createdTask
    /* The content WITHOUT the wire flag: the first tick POSTs and later ones PUT (different flags; `persona` may
     * be omitted on the POST), so keyed on the payload the tick after a create could never match. */
    const content = JSON.stringify(
      isEdit && target !== null && target !== undefined
        ? editFields(target)
        : { ...createSurfaceFields(), persona },
    )
    /* CREATE holds pasted images in the tab until Save (edit uploads on paste). The filed row's body already
     * references them, so a draft reopened from the board would show `⚠ image unavailable` for bytes that
     * never left the tab: each create tick also uploads what is staged, against the row it wrote.
     * A failure stays silent and staged — the next tick, or Save's own pass, retries it. */
    const uploadsPending = !isEdit && Array.from(stagedImages.keys())
      .some(fn => !uploadedTokens.has(fn) && body.includes(`attachment:${fn}`))
    if (content === autosavedRef.current && !(uploadsPending && target !== null && target !== undefined)) return
    autosaveInFlightRef.current = true
    let settle = () => {}
    autosavePromiseRef.current = new Promise<void>(resolve => { settle = resolve })
    try {
      let filedId = target?.id
      if (content !== autosavedRef.current) {
        if (target !== null && target !== undefined) {
          await put<PipelineTask>(`/api/tasks/${target.id}`, {
            ...(isEdit ? editFields(target) : { ...createSurfaceFields(), persona }),
            autosave: true,
          })
        } else {
          /* The first create tick FILES the row; `createdTask` is what every later tick and Save write against
           * (the same state the retry pass uses). */
          const filed = await post<PipelineTask>('/api/tasks', {
            ...createSurfaceFields(),
            persona,
            ...priorityCreateField,
            ...templateCreateField,
            unsaved: true,
          })
          autosaveFiledRef.current = filed
          setCreatedTask(filed)
          filedId = filed.id
          // Silent like the tick: a refusal is retried by Save's pass.
          if (onFiled) await onFiled(filed)
        }
        autosavedRef.current = content
      }
      if (uploadsPending && filedId !== undefined) await uploadPendingStagedImages(filedId)
    } catch {
      /* Silent: a tick must neither paint nor clear a banner (`error` belongs to the operator's press).
       * `autosavedRef` is untouched, so the next tick retries; a still-failing Save shows its own error. */
    } finally {
      autosaveInFlightRef.current = false
      autosavePromiseRef.current = null
      settle()
    }
  }
  useEffect(() => { autosaveRef.current = runAutosave })
  /* The timer, re-armed only when the CADENCE changes; it calls through the ref so the interval survives
   * renders while the work is always current. */
  useEffect(() => {
    const id = setInterval(() => { void autosaveRef.current() }, autosaveSeconds * 1000)
    return () => clearInterval(id)
  }, [autosaveSeconds])

  /* Returns whether a write LANDED, read by the page's unsaved-changes dialog; `e` is optional (the dialog's
   * Save is not a form submission). */
  async function submit(e?: FormEvent, intent: SubmitIntent = 'save'): Promise<boolean> {
    e?.preventDefault()
    // A second press before `submitting` renders is the same press: one pass at a time.
    if (!canSubmit || submitPassRef.current) return false
    // A Go on a running row posts `reset` before `start` (`goTransitions`), which discards the attempt (Q&A,
    // report, stage output, outcome), so it takes the board's Reset confirm in the SAME words
    // (`resetConfirmOptions`), BEFORE the save, so cancelling leaves the row untouched. Read off the LIVE row
    // (`goRow`). Go is withheld on running rows (`goNotStartable`), so this asks only if Go is ever offered on one;
    // a row that went running after render is asked after the save (below).
    const resetConfirmed = intent === 'go' && goRow !== undefined && goTransitions(goRow).includes('reset')
    if (resetConfirmed) {
      const ok = await confirm(resetConfirmOptions(goRow.title, { thenRestart: true }))
      if (!ok) return false
    }
    // Asked before the save too: Cancel leaves the form as it was. A retry that owes the start asks again.
    if (intent === 'go' && projectId !== null) {
      const pushAs = await pushIdentity.ask({
        projectId, title: startTitle(title, body), assigneeId: assignedToId === '' ? null : assignedToId,
        epic: isEpicPersona(persona),
      })
      if (!pushAs) return false
      pushAsRef.current = pushAs
    }
    setSubmittingIntent(intent)
    setError('')
    setRefusedOrg(null)
    setPersonaError('')
    submitPassRef.current = true
    try {
      return await submitPass(intent, resetConfirmed)
    } finally {
      submitPassRef.current = false
    }
  }

  async function submitPass(intent: SubmitIntent, resetConfirmed: boolean): Promise<boolean> {
    // A tick mid-write finishes first; the row it filed is the one this pass writes (see `autosavePromiseRef`).
    if (autosavePromiseRef.current) await autosavePromiseRef.current
    const filedRow = createdTask ?? autosaveFiledRef.current
    // The task this pass saved, or null. `onSaved` runs ONCE after the try/catch settles, so a throwing host
    // callback (e.g. `navigate`) is never reported as a failed write.
    let settled: PipelineTask | null = null
    let notRestarted = false
    try {
      if (isEdit && task) {
        let saved = await put<PipelineTask>(`/api/tasks/${task.id}`, editFields(task))
        // Priority rides the set-priority route (`PATCH /api/tasks/{id}`, not a `TaskUpdate` field), only when it
        // moved. Not persona-gated: an epic's priority is inherited at fan-out.
        if (priorityChangedFrom(task)) {
          saved = await patch<PipelineTask>(`/api/tasks/${task.id}`, { priority })
        }
        // Not in `editFields`, so never auto-saved: a keystroke must not file a defect link.
        await pageFields.save(task)
        // The prerequisite draft before Go, so an edge declared now gates this dispatch.
        if (!(await flushPrereqs(task.id))) return false
        // Go: save, then the row's transition sequence (`runGoTransitions`, which owns its failure). A failed
        // sequence leaves the save landed and does not navigate, so the banner is read here. `submitting` is
        // cleared explicitly on both arms (a no-op edit would not remount the form).
        /* A row that went running since render (the PUT's answer) asks the reset confirm HERE, the save landed:
         * cancelled, the press ends as a save — the row stays saved, its runner untouched (`onSavedNotRestarted`). */
        if (intent === 'go' && !resetConfirmed && goTransitions(saved).includes('reset')
          && !(await confirm(resetConfirmOptions(saved.title, { thenRestart: true })))) {
          setSubmittingIntent(null)
          settled = saved
          notRestarted = true
        } else if (intent === 'go') {
          const ran = await runGoTransitions(saved)
          setSubmittingIntent(null)
          if (ran !== null) settled = ran
        } else {
          setSubmittingIntent(null)
          settled = saved
        }
      } else {
        // Remembered BEFORE anything can fail, so a retry after a failed prerequisite, upload or start finishes the
        // intent pressed (Go). Safe this early: its readers wait on `createdTask` (the submit handler) or `retryStage`.
        setCreateIntent(intent)
        /* `filedRow`: the row already EXISTS — never a second POST: this is the retry pass (Risk 1) or the ordinary
         * Save after an auto-save tick filed the row. It writes the fields first (the operator kept typing). The PUT
         * carries NO `autosave` flag, which is the whole finalize: it clears `unsaved`, derives the title and
         * refinement, builds an epic's children and publishes the row (`_finalize_unsaved_draft`,
         * apps/api/app/routes/tasks/dispatch.py). `persona` rides unconditionally (see `createSurfaceFields`). */
        let row = filedRow !== null
          ? await put<PipelineTask>(`/api/tasks/${filedRow.id}`, { ...createSurfaceFields(), persona })
          : await post<PipelineTask>('/api/tasks', {
            ...createSurfaceFields(),
            persona,
            // `TaskCreate` takes priority directly; only a non-default level rides.
            ...priorityCreateField,
            ...templateCreateField,
          })
        // The project the next create under All projects defaults to (`lib/lastCreateProject.ts`), recorded once the
        // row is filed, before anything else can fail.
        saveLastCreateProject(admin?.id ?? null, row.project_id)
        // The first tick filed the row with the priority of THEN; a later level takes the action route, only if it
        // moved.
        if (filedRow !== null && priorityChangedFrom(filedRow)) {
          row = await patch<PipelineTask>(`/api/tasks/${row.id}`, { priority })
        }
        setCreatedTask(row)
        if (!(await flushFiled(row))) return false
        // Now there is an id for edges (a previously refused prerequisite is retried here, staged, not lost): before
        // uploads and start, so a Go dispatches with its gate declared.
        if (!(await flushPrereqs(row.id))) return false
        // Both staged sets, in sequence (each is internally `Promise.all`; the second's state writes must not
        // interleave). `failed` is their union.
        const failed = [
          ...await uploadPendingStagedImages(row.id),
          ...await uploadPendingStagedFiles(row.id),
        ]
        if (failed.length > 0) {
          setRetryStage('upload')
          setError(uploadFailureBanner(failed, filedRow !== null ? 'Still' : 'Task created, but', retryActionLabel(intent)))
          setSubmittingIntent(null)
          return false
        }
        if (intent === 'go') {
          // `startCreatedTask` owns the failure; `createdTask` is set, so a re-submit can never POST again.
          const started = await startCreatedTask(row)
          setSubmittingIntent(null)
          if (started !== null) settled = started
        } else {
          setRetryStage(null)
          setSubmittingIntent(null)
          // The row the write returned (a PUT's is finalized, not the POST's pre-finalize "New task" row).
          settled = row
        }
      }
    } catch (err) {
      // Keep inputs for retry.
      const project = projects.find(p => p.id === projectId)
      const detail = isEdit ? apiDetail(err, 'Failed to update task') : createRefusal(err, 'Failed to create task', project, admin?.is_operator === true)
      setError(detail)
      if (!isEdit) setRefusedOrg(refusedInOrg(err, project))
      // AC7.5: CONTRACT-4's conversion guard 400s WITH the reason; echoed beside the selector too.
      if (personaChanged) setPersonaError(detail)
      setSubmittingIntent(null)
      return false
    }
    // Outside the write's try/catch: a throwing host callback must not be blamed on the write. Its own
    // try/catch stops a synchronous throw becoming an unhandled rejection (`submit()` is called as `void`).
    if (settled !== null) {
      try {
        if (notRestarted && onSavedNotRestarted) onSavedNotRestarted(settled)
        else onSaved(settled, notRestarted ? 'save' : intent)
      } catch (err) {
        console.error('onSaved threw after a successful save:', err)
      }
    }
    return settled !== null
  }

  /* ── FROM ISSUE ──────────────────────────────────────────────────────────────────────────────────────
   * Where this task's TEXT comes from, so it sits with the what-this-task-is controls. Two placements, ONE instance
   * (`stowed`, placed by `TaskSpecFields`), never a `display: none` pair: a hidden twin could hold an armed,
   * unresolvable replace. The pick's state lives on this form (`issuePick`), so it survives the slot change. */
  const fromIssue = (stowed: boolean) => !issueShown || projectId === null ? null : (
    <IssuePicker projectId={projectId} pick={issuePick} disabled={locked || issueUnlinked} stowed={stowed}
      titleFill={issueTitleFill} heldHidden={issueIsForeign}
      reason={issueUnlinked
        // `?tab=advanced`: General with its Advanced fold open, where Issue trackers sits (`ProjectEditPage`).
        ? <>Link an issue tracker in <Link component={RouterLink} to={`/projects/${projectId}?tab=advanced`} color="inherit">project settings</Link>.</>
        : undefined} />
  )

  /* ── THE SPEC `TaskSpecFields` RENDERS, from this form's own state ─────────────────────────────────────────
   * The state stays here (the writes, the dirty gate and auto-save read it); the fields write back through
   * `applySpec`, which takes only the keys that MOVED against this render's spec — a handler's spread of an
   * older render can never undo a write that landed since (the late `createDefaults` seed). `title_template`,
   * `attachment_ids` and `feature_id` are an automation's (the breadcrumb owns this form's Title and Feature). */
  const spec: TaskSpec = {
    persona,
    body,
    title_template: null,
    model,
    bound_provider: provider,
    agent_id: agentId,
    pinned_node_id: pinnedNodeId,
    account_id: accountId,
    autonomous,
    priority,
    playbook_id: playbook?.id ?? null,
    transition_mode: transitionMode,
    max_running: maxRunning,
    assignee_id: assignedToId === '' ? null : assignedToId,
    attachment_ids: [],
    feature_id: null,
    hidden,
  }
  const applySpec = (next: TaskSpec) => {
    if (next.persona !== spec.persona) setPersona(next.persona)
    if (next.body !== spec.body) setBody(next.body)
    if (next.model !== spec.model) setModel(next.model)
    if (next.bound_provider !== spec.bound_provider) setProvider(next.bound_provider)
    if (next.agent_id !== spec.agent_id) setAgentId(next.agent_id)
    // The person's own pick replaces the recommendation they took.
    if (next.bound_provider !== spec.bound_provider || next.agent_id !== spec.agent_id) setAcceptedPick(null)
    if (next.pinned_node_id !== spec.pinned_node_id) setPinnedNodeId(next.pinned_node_id)
    if (next.account_id !== spec.account_id) {
      if (next.account_id) accountSentRef.current = true
      setAccountId(next.account_id ?? null)
    }
    if (next.priority !== spec.priority) setPriority(next.priority)
    if (next.transition_mode !== spec.transition_mode) setTransitionMode(next.transition_mode)
    if (next.max_running !== spec.max_running) setMaxRunning(next.max_running)
    if (next.assignee_id !== spec.assignee_id) setAssignedToId(next.assignee_id ?? '')
    if (next.hidden !== spec.hidden) setHidden(next.hidden)
    // `autonomous` and `playbook_id` are projections of the axes, written by `applyAxes`.
  }
  /* An axis or playbook press: the axes, and the create-time seed's touch flags (a press ends the late
   * `createDefaults` seed for the level it wrote). A picked playbook marks the row as having carried one. */
  const applyAxes = (next: TaskSpecAxes, touched: TaskSpecTouched) => {
    if (touched.persona) untouchedRef.current.persona = false
    if (touched.autonomous) untouchedRef.current.autonomous = false
    if (next.playbook !== null && next.playbook.id !== playbook?.id) playbookSentRef.current = true
    setDepth(next.depth)
    setInvolvement(next.involvement)
    setPlaybook(next.playbook)
  }

  /* ── Depends on ──────────────────────────────────────────────────────────────────────────────────────
   * Which tasks must be `done` before this dispatches — a SETTING changed with the runner and priority (the
   * reverse `Blocks` list stays read-only on Details), placed at the end of Advanced. Both modes: it writes
   * through its own routes (`POST`/`DELETE …/dependencies`) on THIS form's Save (`flushPrereqs`), so a staged
   * pick is `dirty`. Every control inside is `type="button"`. `createdTask?.id`: once the POST landed, the panel
   * uses the real graph. Not for a chat (off the live persona, `submitsChatRow`): a chat is not placed in a
   * pipeline. */
  const dependsOn = submitsChatRow ? null : (
    <Box sx={{ mt: 2.5 }}>
      <TaskDependsOn
        taskId={task?.id ?? createdTask?.id ?? null}
        parentTaskId={task?.parent_task_id ?? null}
        readOnly={readOnly}
        /* The same refusal Go takes: the per-row Re-run POSTs `start`, so it follows `canStart`. */
        canRerun={canStart}
        onStagedChange={setPrereqStaged}
        handleRef={prereqRef}
      />
    </Box>
  )

  return (
    <Box
      component="form"
      // Implicit submit stays on `'save'`; Go is a click or Cmd/Ctrl+Enter. The project select and the Hidden checkbox fire
      // implicit submission here, so Enter-as-Go would dispatch a runner off a stray keystroke. A pending retry
      // resumes its original intent, Go included.
      onSubmit={e => void submit(e, createdTask !== null ? createIntent : 'save')}
      // Cmd/Ctrl+Enter anywhere in the form is Go (operator decision: create and edit alike), under exactly the gates
      // the button has.
      onKeyDownCapture={modEnterCapture(
        () => void submit(undefined, 'go'),
        !(readOnly || inRetry || !goPermitted || goHidden || goDisabled),
      )}
      // No `<h3>`: the page heading already names the task; `aria-label` keeps the accessible name.
      aria-label={isEdit ? 'Edit task' : 'New task'}
      data-testid="task-form-card"
      sx={theme => ({
        display: 'flex',
        flexDirection: 'column',
        flex: '1 1 auto',
        minHeight: 0,
        // The form is its own scrollport, so a tall variant scrolls inside the panel, not past `.app-main`
        // (CONTRACT-6).
        overflowY: 'auto',
        [theme.breakpoints.down(481)]: {
          // At this width the form is NOT a scrollport: it grows and the SHELL scrolls (a scroll container in between
          // swallowed the gesture). It becomes one only while the page is CEILINGED (keyboard up), where
          // `TaskDetailPage`'s PAGE_SX sets `auto` in its `[data-keyboard="up"]` block so the sticky action row rides
          // the visible edge. Single home for both rules: PAGE_SX, "WHY NO SCROLLPORT ON THIS PAGE CONTAINS ITS
          // OVERSCROLL" + "ONE SCROLLPORT PER PHONE".
          overflowY: 'visible',
          // The editor's phone fullscreen, re-scoped to a flex-fill (`PHONE_EDITOR_SX`).
          '& [data-testid="rte"]': PHONE_EDITOR_SX,
        },
      })}
    >
      {/* `role="alert"`: here the banner can be the only signal that a write half landed (created, not started). */}
      {error && <Banner severity="error" action={refusedOrg ? <ActAsToCreate org={refusedOrg} /> : undefined}>{error}</Banner>}
      {noProjects ? (
        <EmptyState inline title="No project yet — add one first." />
      ) : (
        <>
          {/* The Project and the Title are the breadcrumb's (`{Project} › Tasks › {id}: {title}`); their values
          title still rides this submit (`titleDraft`). An emptied title is still OMITTED from the PUT;
          `project_id` rides a create only (an edit's crumb moves the task at once). */}

          {/* EVERY FIELD — the Description, the axes row, Attachments, Assigned to and Advanced — is `TaskSpecFields`'
          (the one task-create form, D7); this form owns the writes and hands in From issue and Depends on. */}
          <TaskSpecFields
            context="task"
            value={spec}
            onChange={applySpec}
            projectIds={projectId === null ? [] : [projectId]}
            canPickAgent={canPickAgent}
            showsAccountPicker={showsAccountPicker}
            accountOrgId={createProject?.org?.id ?? null}
            orgId={null}
            org={otherOrg}
            stagedFiles={stagedAttachments}
            onStagedFilesChange={setStagedAttachments}
            readOnly={readOnly}
            disabled={submitting}
            createDefaults={createDefaults}
            defaultModel={formProject?.default_model ?? null}
            hasRepo={formProject?.has_repo !== false}
            axes={{ depth, involvement, playbook }}
            onAxesChange={applyAxes}
            playbookOpen={playbookOpen}
            childRow={isChildEdit}
            showsHidden={showsHidden}
            attachmentsTaskId={isEdit && task ? task.id : null}
            storedAssignee={task?.assigned_to_id ? { id: task.assigned_to_id, label: task.assigned_to ?? null } : undefined}
            /* On CREATE, an assignee the landed roster lacks falls back to the author: the crumb may have moved the
             * create to another project. EDIT is exempt: the stored assignee is a fact. */
            assigneeFallback={isEdit ? undefined : (actingAssignee ?? admin?.id ?? '')}
            personaChipLabel={
              prefillSeed?.persona !== undefined && persona === prefillSeed.persona.value
                ? prefillSeed.persona.label
                : undefined
            }
            personaNote={personaError || (warnsPersonaTeardown ? PERSONA_TO_CODER_WARNING : undefined)}
            editor={{
              onUploadImage: handleUploadImage,
              resolveAttachmentSrc,
              /* Three prompts, off the PERSONA (`submitsChatRow`): the placeholder is the create surface's only
               * instruction. */
              placeholder: submitsChatRow
                ? 'What do you want to ask?'
                : isEdit
                  ? 'Goal, acceptance criteria, and out-of-scope lists.'
                  : 'What needs to happen?',
              autoFocus: !isEdit,
              /* `inRetry`, not `createdTask !== null`: auto-save sets the latter on every create, which would lock the
               * editor 15 seconds into typing. */
              paused: inRetry,
            }}
            issuePicker={fromIssue}
            dependsOn={dependsOn}
            /* Only where "Use this" has a picker to set (`showsAgentPicker`) and a write to ride, and while the org's
               plan has `intelligent_routing` on (the operator always; without it the preview never answers a pick). */
            recommendation={showsAgentPicker && !readOnly && planSurfaceShown(admin, 'intelligent_routing') ? (
              <RecommendationBlock
                projectId={projectId}
                persona={persona}
                title={title}
                body={body}
                taskId={task?.id ?? null}
                org={previewOrg}
                accepted={acceptedPick !== null && pickHeld(acceptedPick)}
                onUse={takeRecommendation}
                onShown={setRecommendationShown}
              />
            ) : undefined}
          />

          {isEdit && <TaskPageFields fields={pageFields} rating={rating} disabled={submitting} />}

          {/* Tags are edited on the DETAILS tab (`pages/taskDetail/TaskSummaryPanel.tsx`), the one task property this form does not
          own. */}
        </>
      )}
      {/* Read-only: the whole row goes (a disabled Save names a missing capability; Discard has nothing to
      abandon; an empty sticky bar is an orphaned toolbar). */}
      {/* The action row, sticky to the bottom of the form's scrollport (A1b). Three actions in ascending
      commitment: Discard → Save → Go (the main button, the only filled one; Save is outlined). Edit renders the
      same three; what Go does differs by row (`goTransitions`). */}
      {/* Each action carries a `task-form-<role>` class: with three actions `button[type="submit"]` no longer
      identifies "save" (on create the submit is Go), so specs address them by role. */}
      {!readOnly && (
      <FormActionBar
        testId="task-form-actions"
        /* Save and Go sit inside the `<span>` their tooltips need; the span is the flex item, so both are named to
         * take the spare width. A lone Save (Go withdrawn or not permitted) keeps its own width: stretched, a
         * disabled Save took the whole bar. */
        stretchTestIds={!goPermitted || goHidden ? ['task-form-retry'] : [
          'task-form-save-wrap', 'task-form-go-wrap',
          'task-form-save', 'task-form-go', 'task-form-retry',
        ]}
      >
        {/* DISCARD (ex-Cancel): it abandons typed work and navigates to the list (`onCancel` →
        `navigate('/tasks')`); on a dirty form it asks first (`discardAndCancel`). The prop and `task-form-cancel`
        testid keep their names (a stable seam for two call sites and ~40 test renders). */}
        {!suggestionDraft && (
        <Button
            sx={BUTTON_SX}
            /* `() => void …`: the handler is async (it awaits the confirm), as elsewhere in this repo. */
            type="button" onClick={() => void discardAndCancel()} variant="text" color="inherit" data-testid="task-form-cancel">
          {offersDiscard ? 'Discard' : 'Close'}
        </Button>
        )}
        {!inRetry ? (
          <>
            {/* Save is `type="submit"`, so Enter writes without running; Go is a click or Cmd/Ctrl+Enter (below). */}
            {/* Wrapped in a span: MUI gives a disabled button `pointer-events: none`, so only the wrapper hears the hover
            that shows why. Present in both states so the row's geometry does not change. */}
            <Tip describeChild={false} title={saveTooltip}>
              <Box component="span" data-testid="task-form-save-wrap" sx={{ display: 'flex' }}>
                <SubmitButton sx={BUTTON_SX} busy={submittingIntent === 'save'} disabled={saveDisabled} variant="outlined" testId="task-form-save">
                  Save
                </SubmitButton>
              </Box>
            </Tip>
            {/* Go is the one action the permission answer takes (`goPermitted`: `canStart` for a held row, the resolved
            project for a create) — offered, it would land the save and 403 on the transition. ABSENT, not disabled;
            Discard and Save untouched (unlike `readOnly`). Also absent while the bar's reason line says why the row
            cannot start (`goHidden`). */}
            {goPermitted && !goHidden && (
            // Same span, same reason as Save's.
            <Tip describeChild={false} title={goTooltip}>
              <Box component="span" data-testid="task-form-go-wrap" sx={{ display: 'flex' }}>
                <Button
                sx={BUTTON_SX}
                  type="button"
                  disabled={goDisabled}
                  variant="contained"
                  color="primary"
                  data-testid="task-form-go"
                  aria-keyshortcuts="Control+Enter Meta+Enter"
                  onClick={e => void submit(e, 'go')}
                >
                  {submittingIntent === 'go' ? CONTROLS.start.busy : (
                    <>
                      Go
                      {/* Out of the accessible name (that stays "Go"; `aria-keyshortcuts` carries the key). */}
                      <Box
                        component="span"
                        aria-hidden
                        data-testid="task-form-go-shortcut"
                        sx={theme => ({ ml: 0.5, [theme.breakpoints.down(481)]: { display: 'none' } })}
                      >
                        ({MOD_ENTER_LABEL})
                      </Box>
                    </>
                  )}
                </Button>
              </Box>
            </Tip>
            )}
          </>
        ) : (
          // Partially-completed create: ONE button naming the step still owed (images or start), its start words from
          // `CONTROLS.start`; splitting into Save/Go would re-decide a half-executed intent. Focused on appearance
          // (it replaces the pair, unmounting whichever had focus).
          // `canStart` deliberately does NOT reach this button: it is the bar's only control on a half-landed create, so
          // withholding it leaves no way to finish or dismiss and focuses nothing. An honest 403 beats a dead end.
          // Unreachable from edit (`createdTask` is set only by a CREATE).
          <Button
            sx={BUTTON_SX}
            ref={retryButtonRef}
            type="submit"
            disabled={!canSubmit}
            variant="contained"
            color="primary"
            data-testid="task-form-retry"
          >
            {submitting
              ? retryStage === 'start' ? CONTROLS.start.busy : 'Retrying…'
              : retryStage === 'start' ? CONTROLS.start.label : retryActionLabel(createIntent, retryStage ?? 'upload')}
          </Button>
        )}
      </FormActionBar>
      )}
      {confirmDialog}
      {pushIdentity.dialog}
    </Box>
  )
}
