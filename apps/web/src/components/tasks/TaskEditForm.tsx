import {
  type FormEvent, type MutableRefObject, useCallback, useEffect, useMemo, useRef, useState,
} from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import MuiLink from '@mui/material/Link'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { apiDetail, patch, post, put } from '../../api'
import { useMaxFileBytes } from '../../lib/maxFileBytes'
import { useAuth } from '../../auth'
import { useOtherOrg } from '../../ui/OrgIndicator'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { useActingAs } from '../../lib/actAs'
import { useCanPickAgent } from '../agents/AgentPicker'
import { useCanOnProject } from '../../auth'
import { useCanCreateChats, useCanRunTasksOnProject } from '../../hooks/useTaskAccess'
import { useAwaitingApprovalReason } from '../../lib/orgLifecycle'
import { safeHref } from '../../lib/safeHref'
import { useFocusedProjectId } from '../../hooks/useFocusedProjectId'
import { useTaskAutosaveSeconds } from '../../lib/taskAutosaveInterval'
// The create-time seed a row's Duplicate hands over (see the `duplicateSeed` prop).
import type { TaskDuplicateSeed } from '../../lib/taskDuplicate'
import type { TaskPrefill } from './taskPrefill'
// The ticket → PUT → complete sequence from its ONE home, shared with `TaskAttachmentsField`.
import { uploadTaskAttachment } from '../../lib/uploadTaskAttachment'
import ActAsCoderOrg from './ActAsCoderOrg'
import ActAsToCreate from './ActAsToCreate'
import type { OrgRef, PipelineTask, Project, TaskPlaybookRef } from '../../types'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import { Banner } from '../../ui/Banner'
import { useConfirm } from '../ConfirmDialog'
import { BUTTON_SX } from '../../ui/formDensity'
import { FormActionBar } from '../../ui/FormActionBar'
import { FormField } from '../../ui/FormField'
import { SubmitButton } from '../../ui/SubmitButton'
import {
  createAttachmentResolver,
  paintableImageUrl,
  releasePaintedUrl,
  type AttachmentResolver,
} from '../richtext/attachmentResolver'
import { can, CONTROLS, startBody, startIntentFor } from './controls'
import { type PushIdentity, readStarters, usePushIdentity } from './PushIdentityDialog'
import { NEW_TASK_CRUMB } from './TaskBreadcrumb'
import { getJiraIssue, type JiraIssueRef, type JiraIssueText, searchJiraIssues } from './jiraApi'
import {
  createRefusal, refusedInOrg, type DiscardSubject, discardConfirmOptions, orgWaitSentence, resetConfirmOptions,
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

/* The field vocabulary — model/provider sentinels and hints, Hidden, Project memory, Debug — is single-homed in
 * `TaskSpecFields`, which renders every field of this form. Re-exported where this module's callers read it. */
export {
  DEBUG_HINT, stripAttachmentImage, type TaskModelChoice,
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
export const RUNNING_GO_HINT = 'Running. Save, then restart to apply.'
// A row waiting on a question (`awaiting_response`): its runner is live, so Go would reset it.
// Built from parts: "Answer it" links to the Answer tab when the host offers one (`onOpenAnswer`).
const AWAITING_BEFORE_LINK = 'Waiting on a question. '
export const AWAITING_ANSWER_LINK = 'Answer it'
const AWAITING_AFTER_LINK = ', or save and restart.'
export const AWAITING_GO_HINT = AWAITING_BEFORE_LINK + AWAITING_ANSWER_LINK + AWAITING_AFTER_LINK
/* Go is gated on "may this row be STARTED" (the transition table), which is also false for a state outside
 * the vocabulary (legacy `cancelled`) — where RUNNING_GO_HINT would be a lie. So this covers the rest. */
export const NOT_STARTABLE_GO_HINT = 'Can’t start from this state.'
// A `ready` row is already started: Go would only re-save it. One the org holds (`wait_reason`) says the
// hold's sentence instead (`orgWaitSentence`): it does not start until the hold clears.
export const QUEUED_GO_HINT = 'Queued. Starts on its own.'
// A `done` row's Go is withheld until something is edited, exactly as Save is.
export const DONE_GO_HINT = 'Done. Edit to run again.'

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

/* The act-as push caption's container: below ~460px its sentence wrapped to two lines, so the short one shows. */
const PUSH_CAPTION_CONTAINER = 'push-caption'
const PUSH_CAPTION_NARROW = `@container ${PUSH_CAPTION_CONTAINER} (max-width: 459px)`

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
  const plural = failures.length > 1 ? 's' : ''
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
  return `${lead} ${failures.length} attachment${plural} failed to upload (${named}). `
    + `Press “${retryLabel}” to try again${escape}.`
}

// What an Epic → Coder conversion COSTS, said before Save: the server STOPS and ARCHIVES the epic's children
// (`_apply_persona_conversion`, tasks.py), so it is safe at any time but archived subtasks leave the board.
// Shown only when an epic with subtasks is moved off the `epic` depth.
export const PERSONA_TO_CODER_WARNING =
  'Saving stops this task’s subtasks and archives them. They leave the board but are not deleted.'

// The axes are wordless by operator instruction; each position's tooltip and `aria-label` is its label from
// the position table in `TaskAxisControls`.

// ── FROM JIRA: fill the Description from a Jira issue ─────────────────────────────────────────────────
// On Save the task records its source (`external_task_system: 'jira'` + `external_task_id`, a pair the
// server validates together). It reads through the project's `jira_credential_id`, so it is absent on a
// project without Jira rather than 409ing per keystroke.

/* What the Jira pick is doing, as one discriminated union (the states are mutually exclusive): the wait,
 * the confirm it may ask first, the outcome, the failure. The pick REWRITES the Description, so every stage
 * is visible. */
type JiraPick =
  | { kind: 'idle' }
  | { kind: 'loading'; key: string }
  /* The issue is in hand and the Description is non-empty, so the replace waits for a confirm. `previous`
   * restores the lookup on decline, making it a true cancel. */
  | { kind: 'confirm'; issue: JiraIssueText; previous: LookupOption | null }
  /* Settled. `replaced` is false for an empty Jira description (`''`): the link is recorded, but blanking the
   * operator's prose would import nothing and also block Save (a non-empty body is the gate). */
  | { kind: 'done'; key: string; url: string; summary: string; replaced: boolean }
  | { kind: 'error'; message: string }

/** Typeahead row → lookup option. The KEY leads the label so identity survives truncation; `AsyncLookup`
 *  highlights the matched run. Status is the dim `hint` column; null when the server did not decorate. */
function jiraOption(ref: JiraIssueRef): LookupOption {
  return { key: ref.key, label: `${ref.key} · ${ref.summary}`, hint: ref.status ?? undefined }
}

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
//     children are dispatched with (`showsAutonomous`), and Debug.
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
  onOpenAnswer,
  readOnly = false,
  canStart = true,
  titleDraft,
  projectIdDraft,
  featureIdDraft,
  onDirtyChange,
  submitRef,
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
   * project is its scope, and `Project.jira_credential_id` is the From Jira control's config (`jiraOwner`). */
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
  /* Selects the host's Answer tab: "Answer it" in the bar's waiting reason links to it. Absent, plain text. */
  onOpenAnswer?: () => void
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
  /* The breadcrumb owns the Title and the PROJECT: the page holds their drafts and hands
   * them down to be SUBMITTED. Optional with a fallback to internal state, so a host without a breadcrumb (unit
   * tests) keeps working. `undefined` = not lifted, distinct from `''`: a blanked title reaches `submit()` as
   * `''`, which the server's `exclude_unset` handling leaves alone. */
  titleDraft?: string
  projectIdDraft?: number
  /* The breadcrumb's staged FEATURE: `null` is "No feature", `undefined` not lifted (the stored filing
   * stands). The page keeps it and `projectIdDraft` in agreement — a feature is always of the staged
   * project — so the PUT never earns the API's mismatch 422. */
  featureIdDraft?: string | null
  /* Reported up so the page can hold its live refetch while the editor has unsaved work. */
  onDirtyChange?: (dirty: boolean) => void
  /* Lets a host run THIS form's save (same path as Save). Resolves to whether it landed. A ref because it
   * closes over render-fresh state; a prop would re-render the host per keystroke. */
  submitRef?: MutableRefObject<(() => Promise<boolean>) | null>
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
  const focusedProjectId = useFocusedProjectId()
  /* An unchosen default skips a project whose tasks could not be placed (`Project.dispatch_block`): the first
   * creatable unblocked one wins, else the original pick. A board or focused project that resolved is kept. */
  const [ownProjectId] = useState<number | null>(() => {
    if (isEdit) return task?.project_id ?? null
    // A template seed (Customize) takes only the scope's project — the board's, else the focused one — and
    // otherwise leaves it for the author to pick (`taskPrefill.ts`; the bar says "Pick a project").
    if (prefillSeed?.template !== undefined) {
      const scope = filterProjectId ?? focusedProjectId
      return projects.some(p => p.id === scope && canCreateInProject(p.id)) ? scope : null
    }
    const pick = resolveCreateProject(projects, filterProjectId, canCreateInProject, focusedProjectId).id
    const chosen = pick !== null && (pick === filterProjectId || pick === focusedProjectId)
    if (chosen || !projects.find(p => p.id === pick)?.dispatch_block) return pick
    return resolveCreateProject(projects.filter(p => !p.dispatch_block), null, canCreateInProject).id ?? pick
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
  // Epic transition mode: create + epic editor only (read off the PARENT epic at dispatch, meaningless on a
  // child; `tasks.py`). 'auto' dispatches a dependency-satisfied child; 'hold' keeps it `ready` for a manual
  // Run.
  const [transitionMode, setTransitionMode] = useState(seedRow?.transition_mode ?? 'auto')
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
    return involvementForTask(persona, seedRow?.autonomous ?? prefillSeed?.template?.autonomous ?? true)
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
  /* A link-seeded persona counts as touched too: the late defaults seed must not replace what the link asked for. */
  const untouchedRef = useRef({
    autonomous: !duplicating && prefillSeed?.template === undefined,
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
  /* The node pin (`tasks.pinned_node_id`), null = Any. From `task`, never a duplicate's source (a copy
   * starts on Any). Under `taskNodePinShown`, enforced server-side (`_pinned_node_or_refuse`). */
  const [pinnedNodeId, setPinnedNodeId] = useState<string | null>(task?.pinned_node_id ?? null)
  /* The account pin (`tasks.bound_account_id`), null = Any. Create only (`showsAccountPicker`), on the POST and the
   * create flow's auto-save PUTs alike (`_account_pick_or_422`, which also stamps the account's provider). Once one
   * was sent, Any is sent too, so a later PUT can take it back. */
  const [accountId, setAccountId] = useState<string | null>(null)
  const accountSentRef = useRef(false)
  /* Project memory On/Off, null = Default. From `task`, never a duplicate's source (a copy starts on Default,
   * `lib/taskDuplicate.ts`). The Default option's "(On)/(Off)" is the operator's knob off the identity payload. */
  const [projectMemory, setProjectMemory] = useState<boolean | null>(task?.project_memory ?? null)
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
  /* ── FROM JIRA ──────────────────────────────────────────────────────────────────────────────────────
   * Pre-filled from the STORED link (only the key is stored, so that is the label). Other trackers open as
   * `null`; submit writes the pair only when the operator moved it. */
  const [jiraIssue, setJiraIssue] = useState<LookupOption | null>(() => (
    seedRow?.external_task_system === 'jira' && seedRow.external_task_id
      ? { key: seedRow.external_task_id, label: seedRow.external_task_id }
      : null
  ))
  const [jiraPick, setJiraPick] = useState<JiraPick>({ kind: 'idle' })
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
  stagedImagesRef.current ??= new Map(duplicateSeed?.inlineImages)
  const stagedImages = stagedImagesRef.current
  /* Staging for deliberately ATTACHED files, deliberately not the map above: inline images are keyed by the
   * body's `attachment:<token>` (so a pasted-then-deleted image is skipped), while attached files have no body
   * reference and may share a filename. Hence a LIST of `{ key, file }` with minted keys, in STATE (chips
   * render from it). Create only; edit uploads on pick. */
  // A duplicate opens with the source's unreferenced attachments already staged, uploaded on Save.
  const [stagedAttachments, setStagedAttachments] = useState<File[]>(
    () => (duplicateSeed?.files ?? []).map(entry => entry.file),
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
  const sourceResolver = useMemo<AttachmentResolver | undefined>(
    () => editResolver ?? templateSeed?.resolve,
    [editResolver, templateSeed],
  )
  // Painted URLs for images staged in THIS form, by token. A ref: minting must not render. The PROMISE is
  // cached, set before it settles, so two resolves of one token mint one URL and a release finds it pending.
  const stagedBlobsRef = useRef<Map<string, Promise<string>>>(new Map())
  // Revoked on unmount, or each pins its File for the tab's lifetime — and `TaskDetailPage` re-keys this form
  // on `${task.id}:${task.updated_at}`. Safe: the upload has landed, and the fresh mount uses `editResolver`.
  useEffect(() => () => {
    for (const url of stagedBlobsRef.current.values()) void url.then(releasePaintedUrl, () => {})
    stagedBlobsRef.current.clear()
  }, [])
  // ONE resolver for both modes, staged-first: `RichTextEditor`'s `insertImageFiles` inserts the node
  // optimistically, so in edit mode resolving from the server raced the upload and latched "⚠ image
  // unavailable" (permanently, via `listPromise`'s cache). Only tokens this form did NOT stage go to the server.
  // tiptap reads the resolver ONCE (`attachmentImage`'s `addNodeView()`), so a later identity never reaches
  // the node view.
  const resolveAttachmentSrc = useMemo<AttachmentResolver>(() => {
    const blobs = stagedBlobsRef.current
    return async (token: string) => {
      const cached = blobs.get(token)
      if (cached) return cached
      const file = stagedImages.get(token)
      if (file) {
        // Inert even for the uploader's own file (M9): `attachmentResolver.ts::paintableImageUrl`.
        const url = paintableImageUrl(file, file.type)
        blobs.set(token, url)
        return url
      }
      if (!sourceResolver) throw new Error(`no staged image for token: ${token}`)
      return sourceResolver(token)
    }
  }, [sourceResolver])

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
  const showsAccountPicker = !isEdit && dispatchedRow

  /* ── WHICH PROJECT THIS FORM IS IN, AND WHETHER IT HAS A JIRA ─────────────────────────────────────────────
   * Also the `task` Depth's model default (`TaskSpecFields`' `defaultModel`). `projectId` (the task's
   * own, or the crumb's staged move; on create the resolved target) in `projects` reaches
   * `Project.jira_credential_id` (the whole Jira config). A null credential renders NOTHING (a field that 409s
   * per keystroke is worse than none). `readOnly` folds into `jiraProjectId` so submit cannot write the pair
   * without a visible control. */
  const jiraOwner = projectId === null ? undefined : projects.find(p => p.id === projectId)
  const jiraProjectId = !readOnly && jiraOwner?.jira_credential_id != null ? jiraOwner.id : null

  /* Is the submitted row a chat? Off the PERSONA (what is submitted), not the Involvement position (`chat`
   * writes the advisor only at `task`). The Description placeholder and Depends-on panel all
   * read this ONE const; keying some on the position once promised a conversation over a prototyper run. */
  const submitsChatRow = persona === ADVISOR_PERSONA

  /* The Jira pair is submitted only when the operator MOVED it, so unrelated PUTs are unchanged and a link
   * written by another tracker is never wiped. Cleared → both nulls: `update_task` merges a missing half from
   * the stored row, and a lone null is a 422 (the pair rule). */
  const jiraKey = jiraIssue?.key ?? null
  const storedJiraKey = task?.external_task_system === 'jira' ? task.external_task_id : null
  /* ── A COPIED JIRA KEY IS THIS PROJECT'S OR NOBODY'S ─────────────────────────────────────────────────────
   * Searched keys come from the target project's own site; a DUPLICATE's key is copied, and the copy may land
   * in another project (`resolveCreateProject` falls through silently). Submitted there, it would be
   * stamped on a row with a DIFFERENT Jira credential, and `comment_on_done_task` would post onto some other
   * site's issue. The server checks only the pair's shape (`_external_issue_or_422`), so this is the fence
   * (a server-side check is a follow-up). Re-derived every render: the crumb can move the project. */
  const duplicateJiraOwnerId = duplicateSeed === undefined ? null
    : projects.find(project => project.id === duplicateSeed.source.project_id)?.id ?? null
  const jiraKeyIsForeign =
    duplicateSeed !== undefined
    && jiraKey !== null
    && jiraKey === duplicateSeed.source.external_task_id
    && jiraProjectId !== duplicateJiraOwnerId
  const externalIssueFields = !jiraKeyIsForeign && jiraProjectId !== null && jiraKey !== storedJiraKey
    ? { external_task_system: jiraKey === null ? null : 'jira', external_task_id: jiraKey }
    : {}
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
  const jiraTitleFill = isEdit && title.trim() === '' && jiraPick.kind === 'done' ? jiraPick.summary : ''
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

  /* ── UNSAVED WORK, reported UP ─────────────────────────────────────────────────────────────────────────
   * The page gates the "Tasks" crumb on this (there is no router blocker in the app). Field-by-field against
   * the STORED row, so typing and deleting leaves it clean. `title` and `projectId` are included: the
   * breadcrumb edits them and they ride this submit. CREATE: dirty = something to lose. */
  const dirty = isEdit && task !== undefined
    ? title !== (task.title ?? '')
      || (projectId !== null && projectId !== task.project_id)
      || (featureIdDraft !== undefined && featureIdDraft !== (task.feature_id ?? null))
      || body !== (task.body ?? '')
      || autonomous !== (task.autonomous ?? true)
      || personaChanged
      || (playbook?.id ?? null) !== (task.playbook?.id ?? null)
      || transitionMode !== (task.transition_mode ?? 'auto')
      || hidden !== (task.hidden ?? false)
      // The model pin alone is a real edit (`?? MODEL_INHERIT` mirrors the seed).
      || model !== (task.model ?? MODEL_INHERIT)
      // The provider pin alone is a real edit (`?? PROVIDER_ANY` mirrors the seed).
      || provider !== (task.bound_provider ?? PROVIDER_ANY)
      // The agent pick alone is a real edit.
      || agentId !== (task.agent_id ?? null)
      // The node pin alone is a real edit.
      || pinnedNodeId !== (task.pinned_node_id ?? null)
      // The project-memory choice alone is a real edit (`?? null` mirrors the seed).
      || projectMemory !== (task.project_memory ?? null)
      // The Jira link alone is a real edit.
      || jiraKey !== storedJiraKey
      // The assignee alone is a real edit (`?? ''` mirrors the seed).
      || assignedToId !== (task.assigned_to_id ?? '')
      // The priority alone is a real edit (it rides its own PATCH).
      || priorityChangedFrom(task)
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
   * disabled with the server's sentence beside it; Save stays (a draft is fine). Edit is not gated here. A missing
   * coding-agent account (`no_account`) does not block a picked SDK agent, which needs none. */
  const createProject = isEdit ? undefined : projects.find(p => p.id === projectId)
  const dispatchBlock = createProject?.dispatch_block_kind === 'no_account' && agentId !== null
    ? null
    : (createProject?.dispatch_block ?? null)
  /* Go held off by the row or project, not by an empty or in-flight form: the push caption goes with it,
   * while an untouched create keeps it so the bar does not jump at the first keystroke. */
  const goBlocked = goNotStartable || goDoneUnchanged || awaitingApproval !== undefined || dispatchBlock !== null
  const goDisabled = !canSubmit || goBlocked
  // Said on the action bar’s note line; not while the retry button stands in for Go.
  const barDispatchBlock = !inRetry && dispatchBlock !== null

  /* Hover text per button: a withheld action explains itself. `''` renders no tooltip (an empty create
   * description, where the form's hint already speaks). One reason per button, so no precedence: Go's
   * dead-state is "cannot be started" (running vs not), `NO_CHANGES_HINT` is Save's alone. */
  /* Why the ROW withholds Go, off the live row (`goRow`). Also said on the bar's note line: a hover alone
   * hid it from a phone and from anyone not pointing at the button. */
  const rowGoHint = !goNotStartable ? null
    : goRow?.state === 'active' ? RUNNING_GO_HINT
    : goRow?.state === 'awaiting_response' ? AWAITING_GO_HINT
    // The org's hold, in the header pill's and banner's words: it starts when the hold clears, not "on its own".
    : goRow?.state === 'ready' ? (goRow.wait_reason ? orgWaitSentence(goRow.wait_reason) : QUEUED_GO_HINT)
    : NOT_STARTABLE_GO_HINT
  const barRowGoHint = !inRetry && rowGoHint !== null && awaitingApproval === undefined && dispatchBlock === null
  /* While the bar says why the row cannot start, Go is withdrawn rather than greyed: beside a greyed Save
   * it was a second dead button, and on a phone the pair took the whole bar. The reason line is the explanation. */
  const goHidden = barRowGoHint
  const goTooltip = awaitingApproval ? awaitingApproval
    : dispatchBlock !== null ? dispatchBlock
    : rowGoHint !== null ? rowGoHint
    : goDoneUnchanged ? DONE_GO_HINT
    : canSubmit ? 'Save and start this task' : ''
  /* A create with no project yet (Customize outside a project scope): Save is dead for that reason alone, so the
   * bar says it — a disabled Save with nothing beside it read as broken. */
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
        const staleBlob = stagedBlobsRef.current.get(filename)
        if (staleBlob) {
          void staleBlob.then(releasePaintedUrl, () => {})
          stagedBlobsRef.current.delete(filename)
        }
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
              startBody(startIntentFor(current, current.parent_task_id == null ? 'root' : 'leaf'), pushAsRef.current),
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

  /* ── THE PICK: one round-trip, then the Description ─────────────────────────────────────────────────────
   * The value is set FIRST so the field shows the choice during the read; a failed read puts it back (a link
   * whose text never arrived would be false). Nothing is saved here — Save stores both. */
  async function pickJiraIssue(next: LookupOption | null) {
    if (jiraProjectId === null) return
    const previous = jiraIssue
    setJiraIssue(next)
    if (next === null) {
      setJiraPick({ kind: 'idle' })
      return
    }
    setJiraPick({ kind: 'loading', key: next.key })
    let issue: JiraIssueText
    try {
      issue = await getJiraIssue(jiraProjectId, next.key)
    } catch (err) {
      setJiraIssue(previous)
      // The server's sentence (a 404 means "does not exist OR the credential cannot see it"), plus which issue
      // and that the Description was left alone.
      setJiraPick({
        kind: 'error',
        message: `${next.key} was not loaded: ${apiDetail(err, 'Jira could not be reached.')}`
          + ' The Description is unchanged.',
      })
      return
    }
    /* The one question: replacing typed prose is unrecoverable (no undo across `setContent`), so the replace
     * waits for a press only when both texts are non-empty; the common create case (empty box) goes straight. */
    if (issue.description.trim() !== '' && bodyRef.current.trim() !== '') {
      setJiraPick({ kind: 'confirm', issue, previous })
      return
    }
    applyJiraIssue(issue)
  }

  /* An issue with no Jira description is a LINK, not a replace: blanking the body would import nothing and
   * make Save go dead (`canSubmit` needs a non-empty body). */
  function applyJiraIssue(issue: JiraIssueText) {
    const replaced = issue.description.trim() !== ''
    if (replaced) setBody(issue.description)
    setJiraPick({ kind: 'done', key: issue.key, url: issue.url, summary: issue.summary, replaced })
  }

  /** Declining is a true CANCEL: the lookup returns to its previous value. */
  function declineJiraIssue(previous: LookupOption | null) {
    setJiraIssue(previous)
    setJiraPick({ kind: 'idle' })
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

  function formFields() {
    return {
      body: body.trim(),
      // On the same gate as the Debug control: the column is read only off a PARENT EPIC.
      ...(isEpicPersona(persona) ? { transition_mode: transitionMode } : {}),
      // `showsAutonomous`: false only on an epic child, whose column is inert.
      ...(showsAutonomous ? { autonomous } : {}),
      /* Absent for a non-superadmin and an epic (absent ≠ empty); a chat may pin (no persona rule server-side).
       * `MODEL_INHERIT` is a real value: the API reads `''` as `model_catalog.INHERIT`. */
      ...(showsModelPin ? { model } : {}),
      /* The agent picker (`showsAgentPicker`): ONE half per write, never both (`_AGENT_OR_PROVIDER`). */
      ...(showsAgentPicker ? agentPickFields() : {}),
      /* The node pin (`showsNodePin`): only a real choice — Any on create is the absent field, on edit only a change.
       * `null` takes the row back to Any. */
      ...(showsNodePin && pinnedNodeId !== (isEdit ? (task?.pinned_node_id ?? null) : null)
        ? { pinned_node_id: pinnedNodeId }
        : {}),
      /* The account pin (`showsAccountPicker`): only a real choice, never beside an agent pick (which runs on none). */
      ...(showsAccountPicker && agentId === null && (accountId !== null || accountSentRef.current)
        ? { account_id: accountId }
        : {}),
      /* Only a real choice: on create Default is the absent field, on edit only a change (a form opened earlier
       * must not revert another writer's choice). `null` takes the row back to Default. */
      ...(projectMemory !== (isEdit ? (task?.project_memory ?? null) : null) ? { project_memory: projectMemory } : {}),
      // The Jira pair when the lookup moved, both-or-neither (`TaskCreate` validates it like the PUT).
      ...externalIssueFields,
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
    // `update_task`, apps/api/app/routes/tasks/update.py — rather than 422ing on ''). Exception: a Jira import into an
    // EMPTY Title fills it (`jiraTitleFill`); a typed title always wins.
    const titleField = title.trim() !== ''
      ? { title: title.trim() }
      : jiraTitleFill !== '' ? { title: jiraTitleFill } : {}
    /* The move: `project_id` rides the PUT only when the crumb's picker moved it, so unrelated PUTs are
     * unchanged and a save is not refused for a move nobody asked for. The server authorizes the destination
     * and refuses a live subtree; its 400 surfaces through `error`. */
    const projectField = projectId !== null && projectId !== target.project_id
      ? { project_id: projectId }
      : {}
    /* The filing, the same way: only when the crumb's feature picker moved it. With a moved project both ride
     * one PUT and agree (the page stages them together). */
    const featureField = featureIdDraft !== undefined && featureIdDraft !== (target.feature_id ?? null)
      ? { feature_id: featureIdDraft }
      : {}
    return {
      ...titleField,
      ...projectField,
      ...featureField,
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
      } else if (filedRow !== null) {
        // Remembered like the POST arm's, so a retry after a failed prerequisite or upload finishes a Go as a Go.
        setCreateIntent(intent)
        // The row already EXISTS — never a second POST: this is the retry pass (Risk 1) or the ordinary Save after
        // an auto-save tick filed the row. It writes the fields first (the operator kept typing). The PUT carries NO
        // `autosave` flag, which is the whole finalize: it clears `unsaved`, derives the title and refinement, builds
        // an epic's children and publishes the row (`_finalize_unsaved_draft`, apps/api/app/routes/tasks/dispatch.py).
        // `persona` rides unconditionally (see `createSurfaceFields`).
        let savedCreate = await put<PipelineTask>(`/api/tasks/${filedRow.id}`, {
          ...createSurfaceFields(),
          persona,
        })
        // The first tick filed the row with the priority of THEN; a later level takes the action route, only if it
        // moved.
        if (priorityChangedFrom(filedRow)) {
          savedCreate = await patch<PipelineTask>(`/api/tasks/${filedRow.id}`, { priority })
        }
        setCreatedTask(savedCreate)
        if (!(await flushFiled(savedCreate))) return false
        // A previously refused prerequisite is retried here (staged, not lost).
        if (!(await flushPrereqs(filedRow.id))) return false
        // Both staged sets, in sequence (each is internally `Promise.all`; the second's state writes must not
        // interleave). `failed` is their union.
        const failed = [
          ...await uploadPendingStagedImages(filedRow.id),
          ...await uploadPendingStagedFiles(filedRow.id),
        ]
        if (failed.length > 0) {
          setRetryStage('upload')
          setError(uploadFailureBanner(failed, 'Still', retryActionLabel(intent)))
          setSubmittingIntent(null)
          return false
        }
        if (intent === 'go') {
          const started = await startCreatedTask(savedCreate)
          setSubmittingIntent(null)
          if (started !== null) settled = started
        } else {
          setRetryStage(null)
          setSubmittingIntent(null)
          // The row the PUT returned (finalized), not the POST's pre-finalize "New task" row.
          settled = savedCreate
        }
      } else {
        const created = await post<PipelineTask>('/api/tasks', {
          ...createSurfaceFields(),
          persona,
          // `TaskCreate` takes priority directly; only a non-default level rides.
          ...priorityCreateField,
          ...templateCreateField,
        })
        setCreatedTask(created)
        // Remembered BEFORE anything can fail, so a retry finishes the intent pressed (Go).
        setCreateIntent(intent)
        if (!(await flushFiled(created))) return false
        // Now there is an id for edges: before uploads and start, so a Go dispatches with its gate declared.
        if (!(await flushPrereqs(created.id))) return false
        const failed = [
          ...await uploadPendingStagedImages(created.id),
          ...await uploadPendingStagedFiles(created.id),
        ]
        if (failed.length > 0) {
          setRetryStage('upload')
          setError(uploadFailureBanner(failed, 'Task created, but', retryActionLabel(intent)))
          setSubmittingIntent(null)
          return false
        }
        if (intent === 'go') {
          // `startCreatedTask` owns the failure; `createdTask` is set, so a re-submit can never POST again.
          const started = await startCreatedTask(created)
          setSubmittingIntent(null)
          if (started !== null) settled = started
        } else {
          setRetryStage(null)
          setSubmittingIntent(null)
          settled = created
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

  /* ── FROM JIRA ───────────────────────────────────────────────────────────────────────────────────────
   * Where this task's TEXT comes from, so it sits with the what-this-task-is controls, not behind Advanced.
   * ABSENT when the project has no Jira credential (`jiraProjectId`). `ui/AsyncLookup` owns debounce,
   * superseded-response discard, loading/error lines and a non-submitting Enter; this block adds only the
   * pick's consequence.
   * Two placements, ONE instance (`stowed`, placed by `TaskSpecFields`), never a `display: none` pair: a hidden twin could hold
   * an armed, unresolvable replace (`pages/credentials/Accounts.tsx`'s `RowActions` records that trap). Cost:
   * crossing slots remounts `AsyncLookup` and resets its input state (e.g. on rotation); `jiraPick` and
   * `jiraIssue` live on the form, so a pick and an armed confirm survive. Accepted. */
  const fromJira = (stowed: boolean) => jiraProjectId === null ? null : (
    <Box
      data-testid="task-form-jira"
      /* The wrapper is what the placement changes. In the settings ROW it is a flex CELL with an 18rem basis (a
       * key plus the front of its summary; the popup is where summaries are read), a floor and two caps, each
       * `min()`-ed against the real space:
       * · floor `min(13rem, 100%)` — a bare min-width beats max-width and overflowed the `<form>` at 481-490px,
       *   scrolling `.app-main` sideways.
       * · 26rem — wider shows nothing more (at 34rem it grew to ~544px around a ten-character key).
       * Inside ADVANCED it is a full-width strip set off by a RULE (the tab's "different kind of thing" idiom). */
      sx={stowed
        ? { mb: '0.5rem', pb: '0.5rem', borderBottom: '1px solid', borderColor: 'divider' }
        : { flex: '1 1 18rem', minWidth: 'min(13rem, 100%)', maxWidth: 'min(26rem, 100%)' }}
    >
      <FormField
        label="From Jira"
        // The invalid state through `ui/FormField` (helper slot, error colour, `aria-describedby`).
        error={jiraPick.kind === 'error' ? jiraPick.message : undefined}
      >
        <AsyncLookup
          id="task-from-jira"
          testId="task-form-jira-lookup"
          /* Shown ⇔ submitted: a copied key that will NOT ride the POST (`jiraKeyIsForeign`) shows blank. */
          value={jiraKeyIsForeign ? null : jiraIssue}
          onChange={next => void pickJiraIssue(next)}
          /* The server decides what matches (key or summary words). Not memoised: `AsyncLookup` reads its fetcher
           * from a ref. */
          fetchOptions={(query, signal) =>
            searchJiraIssues(jiraProjectId, query, signal).then(rows => rows.map(jiraOption))}
          placeholder="Issue key, or words from the summary"
          // WCAG 2.5.3: the accessible name CONTAINS the visible label.
          ariaLabel="From Jira — issue key or summary"
          /* Two characters: one would spend a Jira round-trip on an unusable list. */
          minChars={2}
          noOptionsText="No matching Jira issues"
          disabled={locked}
        />
      </FormField>

      {/* The round-trip, said out loud: the pick rewrites the Description, so the wait cannot be silent. */}
      {jiraPick.kind === 'loading' && (
        <Box
          role="status"
          data-testid="task-form-jira-loading"
          sx={{ mt: '0.35rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}
        >
          <CircularProgress size={12} thickness={5} />
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {`Loading ${jiraPick.key} from Jira…`}
          </Typography>
        </Box>
      )}

      {/* ── THE ONE CONFIRM ──────────────────────────────────────────────────────────────────────────────
      Shown only when the replace would destroy existing prose (`pickJiraIssue`). Names the issue and summary. A
      plain bordered block, not `ui/Banner` (page-level, with an unsuppressable testid). Both buttons are
      `type="button"` (inside the `<form>`). `autoFocus` on Replace: the pick was an Enter. */}
      {jiraPick.kind === 'confirm' && (
        <Box
          role="group"
          aria-label="Replace the Description from Jira"
          data-testid="task-form-jira-confirm"
          sx={{
            mt: '0.5rem',
            p: '0.5rem 0.75rem',
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            gap: '0.5rem',
            border: '1px solid',
            borderColor: 'warning.main',
            borderRadius: 2,
            /* The two answers stay together, ranged right. Structural, not a viewport rule: the block's width comes from
             * its CELL (a 481px rule split the answers at 700-1000px viewports; see `ui/FieldRow`'s header). */
            justifyContent: 'flex-end',
          }}
        >
          <Typography
            variant="caption"
            sx={{
              flex: '1 1 15rem',
              minWidth: 0,
              color: 'text.primary',
              wordBreak: 'break-word',
            }}
          >
            {`Replace the Description with ${jiraPick.issue.key} — “${jiraPick.issue.summary}”? `
              + 'What is written there now is lost.'}
          </Typography>
          {/* ONE flex item, so the pair wraps as a unit or not at all. */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: '0 0 auto' }}>
            <Button
              sx={BUTTON_SX}
              type="button"
              size="small"
              variant="contained"
              color="primary"
              autoFocus
              data-testid="task-form-jira-replace"
              onClick={() => applyJiraIssue(jiraPick.issue)}
            >
              Replace
            </Button>
            <Button
              sx={BUTTON_SX}
              type="button"
              size="small"
              variant="text"
              color="inherit"
              data-testid="task-form-jira-keep"
              onClick={() => declineJiraIssue(jiraPick.previous)}
            >
              Keep mine
            </Button>
          </Box>
        </Box>
      )}

      {/* What happened (the write is to this form only). The key links out through `lib/safeHref` (the URL is
      built from an operator-typed base), degrading to plain text. */}
      {jiraPick.kind === 'done' && (
        <Typography
          component="p"
          variant="caption"
          role="status"
          data-testid="task-form-jira-status"
          sx={{
            mt: '0.35rem',
            mb: 0,
            wordBreak: 'break-word',
            color: jiraPick.replaced ? 'text.secondary' : 'warning.main',
          }}
        >
          {jiraPick.replaced ? 'Description replaced from ' : 'No description in Jira on '}
          {safeHref(jiraPick.url)
            ? (
              <MuiLink href={safeHref(jiraPick.url)} target="_blank" rel="noreferrer">
                {jiraPick.key}
              </MuiLink>
            )
            : jiraPick.key}
          {jiraPick.replaced
            ? '. Nothing is saved yet — press Save.'
            : ', so the Description is unchanged. The issue key is still saved.'}
          {jiraTitleFill !== '' && ` The Title will be set to “${jiraTitleFill}”.`}
        </Typography>
      )}
    </Box>
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
    project_memory: projectMemory,
    priority,
    playbook_id: playbook?.id ?? null,
    transition_mode: transitionMode,
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
    if (next.pinned_node_id !== spec.pinned_node_id) setPinnedNodeId(next.pinned_node_id)
    if (next.account_id !== spec.account_id) {
      if (next.account_id) accountSentRef.current = true
      setAccountId(next.account_id ?? null)
    }
    if (next.project_memory !== spec.project_memory) setProjectMemory(next.project_memory)
    if (next.priority !== spec.priority) setPriority(next.priority)
    if (next.transition_mode !== spec.transition_mode) setTransitionMode(next.transition_mode)
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
    <Box sx={{ mt: '1rem' }}>
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
      // Implicit submit stays on `'save'`; Go is click-only. The project select and the Debug checkbox fire
      // implicit submission here, so Enter-as-Go would dispatch a runner off a stray keystroke. A pending retry
      // resumes its original intent, Go included.
      onSubmit={e => void submit(e, createdTask !== null ? createIntent : 'save')}
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
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          No project yet — add one first.
        </Typography>
      ) : (
        <>
          {/* The Project and the Title are the breadcrumb's (`{Project} › Tasks › {id}: {title}`); their values
          still ride this submit (`titleDraft`/`projectIdDraft`). An emptied title is still OMITTED from the PUT;
          `project_id` still rides a create. */}

          {/* EVERY FIELD — the Description, the axes row, Attachments, Assigned to and Advanced — is `TaskSpecFields`'
          (the one task-create form, D7); this form owns the writes and hands in From Jira and Depends on. */}
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
            defaultModel={jiraOwner?.default_model ?? null}
            hasRepo={jiraOwner?.has_repo !== false}
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
            jiraField={fromJira}
            dependsOn={dependsOn}
          />

          {/* Tags are edited on the DETAILS tab (`pages/taskDetail/TaskDetailsPanel.tsx`), the one task property this form does not
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
        /* The bar's own line above the actions, read from the left: right-aligned beside them it crowded Close/Save/Go,
           and outside the bar a phone's stuck bar covered it. */
        note={(needsProject || (goPermitted && (barDispatchBlock || barRowGoHint || (acting && !goBlocked)))) && (
          <>
            {needsProject && (
              <Typography variant="caption" data-testid="task-form-pick-project" sx={{ color: 'text.secondary', display: 'block' }}>
                {PICK_PROJECT_HINT}
              </Typography>
            )}
            {/* Why Go is disabled on this project, said where it is read (the tooltip alone needs a hover). */}
            {barDispatchBlock && (
              <Typography variant="caption" data-testid="task-form-dispatch-block" sx={{ color: 'text.secondary', display: 'block' }}>
                {dispatchBlock}
                {/* An operator on their own plane has no account, but an org they act as may. */}
                {createProject?.dispatch_block_kind === 'no_account' && <ActAsCoderOrg projectOrg={createProject.org} />}
              </Typography>
            )}
            {/* …and why this ROW withholds it (running, waiting on a question), the same way. */}
            {barRowGoHint && (
              <Typography variant="caption" data-testid="task-form-go-blocked" sx={{ color: 'text.secondary', display: 'block' }}>
                {rowGoHint === AWAITING_GO_HINT && onOpenAnswer ? (
                  <>
                    {AWAITING_BEFORE_LINK}
                    <MuiLink
                      component="button"
                      type="button"
                      variant="caption"
                      data-testid="task-form-open-answer"
                      onClick={onOpenAnswer}
                      sx={{ verticalAlign: 'baseline' }}
                    >
                      {AWAITING_ANSWER_LINK}
                    </MuiLink>
                    {AWAITING_AFTER_LINK}
                  </>
                ) : rowGoHint}
              </Typography>
            )}
            {/* Not beside a blocked Go: under a dispatch block it was a second note about a dead button. */}
            {acting && !goBlocked && (
              /* One short line when the form is narrow — a phone, or the editor beside the list — keyed on
                 the caption's own width: the act-as bar above already names the org. */
              <Typography
                variant="caption"
                data-testid="task-form-push-caption"
                sx={{ color: 'text.secondary', display: 'block', containerType: 'inline-size', containerName: PUSH_CAPTION_CONTAINER }}
              >
                <Box component="span" sx={{ [PUSH_CAPTION_NARROW]: { display: 'none' } }}>
                  You aren’t a member of {acting.name}: Go asks whose git account to push with.
                </Box>
                <Box component="span" data-testid="task-form-push-caption-short" sx={{ display: 'none', [PUSH_CAPTION_NARROW]: { display: 'inline' } }}>
                  Go asks whom to push as.
                </Box>
              </Typography>
            )}
          </>
        )}
        /* Save and Go sit inside the `<span>` their tooltips need; the span is the flex item, so both are named to
         * take the spare width. A lone Save (Go withdrawn or not permitted) keeps its own width: stretched, a
         * disabled Save took the whole bar under the reason line. */
        stretchTestIds={!goPermitted || goHidden ? ['task-form-retry'] : [
          'task-form-save-wrap', 'task-form-go-wrap',
          'task-form-save', 'task-form-go', 'task-form-retry',
        ]}
      >
        {/* DISCARD (ex-Cancel): it abandons typed work and navigates to the list (`onCancel` →
        `navigate('/tasks')`); on a dirty form it asks first (`discardAndCancel`). The prop and `task-form-cancel`
        testid keep their names (a stable seam for two call sites and ~40 test renders). */}
        <Button
            sx={BUTTON_SX}
            /* `() => void …`: the handler is async (it awaits the confirm), as elsewhere in this repo. */
            type="button" onClick={() => void discardAndCancel()} variant="text" color="inherit" data-testid="task-form-cancel">
          {offersDiscard ? 'Discard' : 'Close'}
        </Button>
        {!inRetry ? (
          <>
            {/* Save is `type="submit"`, so Enter writes without running; Go is click-only. */}
            {/* Wrapped in a span: MUI gives a disabled button `pointer-events: none`, so only the wrapper hears the hover
            that shows why. Present in both states so the row's geometry does not change. */}
            <Tooltip title={saveTooltip}>
              <Box component="span" data-testid="task-form-save-wrap" sx={{ display: 'flex' }}>
                <SubmitButton sx={BUTTON_SX} busy={submittingIntent === 'save'} disabled={saveDisabled} variant="outlined" testId="task-form-save">
                  Save
                </SubmitButton>
              </Box>
            </Tooltip>
            {/* Go is the one action the permission answer takes (`goPermitted`: `canStart` for a held row, the resolved
            project for a create) — offered, it would land the save and 403 on the transition. ABSENT, not disabled;
            Discard and Save untouched (unlike `readOnly`). Also absent while the bar's reason line says why the row
            cannot start (`goHidden`). */}
            {goPermitted && !goHidden && (
            // Same span, same reason as Save's.
            <Tooltip title={goTooltip}>
              <Box component="span" data-testid="task-form-go-wrap" sx={{ display: 'flex' }}>
                <Button
                sx={BUTTON_SX}
                  type="button"
                  disabled={goDisabled}
                  variant="contained"
                  color="primary"
                  data-testid="task-form-go"
                  onClick={e => void submit(e, 'go')}
                >
                  {submittingIntent === 'go' ? CONTROLS.start.busy : 'Go'}
                </Button>
              </Box>
            </Tooltip>
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
