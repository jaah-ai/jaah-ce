import { nanoid } from 'nanoid'
import type { NavigateFunction } from 'react-router-dom'
import { get } from '../api'
import { enterActAs } from './actAs'
import { sessionHeaders } from './activeOrg'
import { ATTACHMENT_SCHEME } from '../components/richtext/attachmentResolver'
import type { FilingTarget } from '../components/tasks/featureFiling'
import type { StagedAttachment } from '../components/tasks/TaskAttachmentsField'
import type { Attachment, OrgRef, PipelineTask } from '../types'

/* ── DUPLICATE A TASK ──
 *
 * It creates NOTHING: it opens `/tasks/new` pre-filled from the row, and nothing exists until Save or Go
 * (operator decision). No endpoint of its own.
 *
 * IT CARRIES EXACTLY WHAT THE CREATE FORM CAN EXPRESS (operator's fence):
 *   CARRIED — the description verbatim (inline `attachment:` tokens included), project, persona,
 *     involvement/autonomous, epic transition mode, model pin, priority, Jira link, every attachment.
 *   NOT CARRIED — the TITLE (`TaskCreate` has none; the server derives it, `app/task_title.py`); TAGS (no
 *     create control; `PUT /api/tasks/{id}/tags`); sweep opt-out (no control); the PROVIDER (`bound_provider` also
 *     holds the source's dispatch stamp, which no one chose); PREREQUISITES; AN EPIC'S CHILDREN — saving an
 *     epic copy auto-initializes its own children and Send to build re-fans the coders, so there is no
 *     recursive copier.
 *
 * ATTACHMENTS ARE RE-STAGED, NOT SHARED: bytes are fetched and handed over as staged `File`s
 * (`TaskAttachmentsField`'s staged mode), so the copy gets its OWN rows. Re-uploading each under its
 * stored filename is what makes a copied `![alt](attachment:<filename>)` resolve
 * (`richtext/attachmentResolver` matches by FILENAME), and paint in the editor before the row exists.
 */

/** THE ONE `location.state` SHAPE `/tasks/new` (and `/tasks/new-chat`) is handed — by the board's New task,
 *  ⤢ and New Chat (`Tasks.tsx`) and by Duplicate (below). What senders write; the reader
 *  (`TaskDetailPage`) still shape-checks every field, since a history entry can be forged. */
export type NewTaskNavState = {
  /** The inline create row's Description. */
  body?: string
  /** The board's project; `null` = All projects. */
  boardProjectId?: number | null
  /** The board's ACTIVE feature scope. */
  boardFeatureId?: string | null
  /** The composer's own "Change" pick; outranks the board's project and feature. */
  filingTarget?: FilingTarget
  /** The row being copied. */
  duplicateOf?: number
}

/** The state a Duplicate press hands `/tasks/new`: the SOURCE row's project rides under the board's
 *  `boardProjectId` name, so the create page needs no second channel. */
export function taskDuplicateNavState(task: PipelineTask): NewTaskNavState {
  return { duplicateOf: task.id, boardProjectId: task.project_id }
}

/** THE DUPLICATE PRESS, board and detail page alike: opens `/tasks/new` seeded from `task`. Another org's row
 *  (`otherOrg`, `ui/OrgIndicator.tsx::useOtherOrg`) opens the editor acting as that org, as New task
 *  does, so the copy files there; the navigation state outlives the identity re-read. */
export function duplicateTask(task: PipelineTask, otherOrg: OrgRef | null, navigate: NavigateFunction): void {
  if (otherOrg) enterActAs(otherOrg)
  navigate('/tasks/new', { state: taskDuplicateNavState(task) })
}

/** The id to copy from, or `null`. Narrowed, not trusted: `location.state` can be a forged history
 *  entry. */
export function duplicateSourceId(state: unknown): number | null {
  const raw: unknown = (state as NewTaskNavState | null)?.duplicateOf
  return typeof raw === 'number' && Number.isInteger(raw) && raw > 0 ? raw : null
}

export type TaskDuplicateSeed = {
  /** The row being copied; its columns seed the form exactly where an EDIT reads its own row. */
  source: PipelineTask
  /** The files the body references inline, keyed by their `attachment:` token (the stored filename). */
  inlineImages: ReadonlyMap<string, File>
  /** The source's remaining attachments, in the chip list's own staged shape. */
  files: readonly StagedAttachment[]
  /** Filenames whose bytes could not be read. The copy opens WITHOUT them and says so. */
  unavailable: readonly string[]
  /** The attachment LIST itself could not be read. Its own field, NOT an empty `unavailable`: "nothing
   *  is missing" and "we cannot say what is missing" are different facts (absence is not a state).
   *  The caller owns the words. */
  attachmentsUnknown: boolean
}

/** Whether the body references this attachment inline (image store, not chip list). ANCHORED ON THE
 *  TOKEN'S TERMINATOR, not a bare `includes`: `a.png` must not match `attachment:a.png.bak`, or it is
 *  filed where nothing references it and silently dropped. A scan, not a regex over an unbounded body. */
function isInlineToken(body: string, filename: string): boolean {
  const needle = `${ATTACHMENT_SCHEME}${filename}`
  for (let at = body.indexOf(needle); at !== -1; at = body.indexOf(needle, at + 1)) {
    const after = body[at + needle.length]
    if (after === undefined || after === ')' || after === ' ' || after === '\n') return true
  }
  return false
}

/** One attachment's bytes as a stageable `File`. Plain `fetch`, not `api.get`: the route answers BYTES
 *  (as `attachmentResolver` uses it). */
async function fetchAttachmentFile(taskId: number, attachment: Attachment): Promise<File> {
  const resp = await fetch(
    `/api/tasks/${taskId}/attachments/${attachment.id}/content`,
    { credentials: 'same-origin', headers: sessionHeaders(undefined) },
  )
  if (!resp.ok) throw new Error(`failed to read attachment ${attachment.filename}`)
  const blob = await resp.blob()
  // `||`, not `??`: `Blob.type` and a stored `content_type` can be `''`, never null.
  return new File([blob], attachment.filename, {
    type: attachment.content_type || blob.type || 'application/octet-stream',
  })
}

/** Split fetched attachment bytes into the create form's two stores. Exported so the sorting rule is
 *  testable without a network. */
export function splitDuplicateAttachments(
  body: string,
  fetched: readonly { attachment: Attachment; file: File }[],
): { inlineImages: Map<string, File>; files: StagedAttachment[] } {
  const inlineImages = new Map<string, File>()
  const files: StagedAttachment[] = []
  for (const { attachment, file } of fetched) {
    if (isInlineToken(body, attachment.filename)) inlineImages.set(attachment.filename, file)
    // `nanoid`, not the source id: `key` is the chip list's React key and remove handle, minted the same
    // way `TaskAttachmentsField` does; the source id would claim an identity on the wrong task.
    else files.push({ key: nanoid(), file })
  }
  return { inlineImages, files }
}

/* HOW MANY ATTACHMENT BODIES ARE IN FLIGHT AT ONCE. Nothing caps the count, so an unbounded fan-out is
 * one-click amplification. A TOTAL BYTE BUDGET is deliberately NOT here: refusing to carry a visible
 * attachment is a product decision (a named follow-up). */
const ATTACHMENT_FETCH_CONCURRENCY = 4

/* The largest file a duplicate copies: each copy is a whole `Blob` in the tab, and an org may store up to
 * `MAX_ATTACHMENT_SIZE` (5 GB) per file. A larger one is a named omission in `unavailable`. */
const DUPLICATE_COPY_MAX_BYTES = 100 * 1024 * 1024

/** The unavailable names the copied body still references inline, which would render as permanent
 *  broken images. STRIPPING them is the caller's step: `stripAttachmentImage` belongs to the editor
 *  module and importing it here would be a cycle. */
export function deadInlineTokens(seed: TaskDuplicateSeed): string[] {
  const body = seed.source.body ?? ''
  return seed.unavailable.filter(filename => isInlineToken(body, filename))
}

/** Read a row and everything a copy of it may carry. THE ROW READ IS FATAL, THE BYTES ARE NOT: a failed
 *  attachment is a named omission in `unavailable`; a failed LIST sets `attachmentsUnknown`. */
export async function loadTaskDuplicateSeed(taskId: number): Promise<TaskDuplicateSeed> {
  const source = await get<PipelineTask>(`/api/tasks/${taskId}`)
  const body = source.body ?? ''
  let listed: Attachment[] = []
  try {
    listed = await get<Attachment[]>(`/api/tasks/${taskId}/attachments`)
  } catch {
    // The copy opens with its parameters and SAYS the attachments are unknown.
    return {
      source, inlineImages: new Map(), files: [], unavailable: [], attachmentsUnknown: true,
    }
  }
  const fetched: { attachment: Attachment; file: File }[] = []
  const unavailable: string[] = []
  // A shared queue drained by fixed workers; order does not matter (entries are filed by name).
  const queue = [...listed]
  await Promise.all(Array.from({ length: ATTACHMENT_FETCH_CONCURRENCY }, async () => {
    for (let next = queue.shift(); next !== undefined; next = queue.shift()) {
      if (next.size_bytes > DUPLICATE_COPY_MAX_BYTES) {
        unavailable.push(next.filename)
        continue
      }
      try {
        fetched.push({ attachment: next, file: await fetchAttachmentFile(taskId, next) })
      } catch {
        unavailable.push(next.filename)
      }
    }
  }))
  const { inlineImages, files } = splitDuplicateAttachments(body, fetched)
  return { source, inlineImages, files, unavailable, attachmentsUnknown: false }
}
