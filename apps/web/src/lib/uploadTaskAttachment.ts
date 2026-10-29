/* ── THE ONE UPLOAD SEQUENCE ── ticket → PUT to object storage → confirm. Every upload surface
 * (`TaskAttachmentsField`, `TaskEditForm.uploadAttachment`, the comment composer's images, the support
 * chat's files) reaches
 * `upload` below through one of the exports; do not spell it twice.
 * THROWS (always an `ApiError` with a string `detail`) so it composes with both callers' handling
 * (`Promise.allSettled` / `try/catch` + `apiDetail`).
 */

import { ApiError, post, postForm, postUploadTicket, putToStorage } from '../api'
// Both caps come from the generated projection of the Python constants — never type the numbers here.
// Only the fallback arm reads the buffered one.
import { MAX_BUFFERED_ATTACHMENT_SIZE } from '../generated/inlinePreviewTypes'
// Same formatter as the pick-time refusal: one rule, one wording.
import { formatBytes } from './formatBytes'
import type { Attachment } from '../types'
import { subjectBase, type CommentSubject, type CommentVisibility } from './commentSubject'
import { forgetCachedRead } from './cachedRead'

/* Structural, not `instanceof ApiError`, so the fallback survives `../api` being stubbed at a module
   boundary. */
function statusOf(err: unknown): number | null {
  const status = (err as { status?: unknown } | null)?.status
  return typeof status === 'number' ? status : null
}

/* `buffered`: whether `base` also has the legacy multipart door. Only a task's attachments do; a ticket's
   and a comment image's are presigned-only, so their 503 is the answer, not a detour — rethrown, or as
   `noStorage` when given. `visibility` is sent only when given (a ticket's internal toggle); a task
   upload's ticket body is exactly the three fields. */
async function upload(
  base: string,
  file: File,
  opts: { buffered: boolean; visibility?: CommentVisibility; noStorage?: string },
): Promise<Attachment> {
  let ticket
  try {
    ticket = await postUploadTicket(`${base}/ticket`, {
      filename: file.name,
      content_type: file.type || 'application/octet-stream',
      size_bytes: file.size,
      ...(opts.visibility === undefined ? {} : { visibility: opts.visibility }),
    })
  } catch (err) {
    /* 503 = this control plane has no object storage (a real deployment, not an outage): fall back to
       the legacy multipart route. A DELIBERATE DEGRADE PATH — not dead code. Any other refusal is
       rethrown untouched. */
    if (statusOf(err) !== 503) throw err
    if (!opts.buffered) throw opts.noStorage === undefined ? err : new ApiError(503, opts.noStorage)
    /* The buffered door caps at `min(MAX_ATTACHMENT_SIZE, MAX_BUFFERED_ATTACHMENT_SIZE)` (25 MB,
       `attachments.py::_read_bounded`), below the picker's cap. Refuse before any bytes move rather
       than stream until a 413. Cannot be done at pick time: storage availability is unknown until the
       ticket route answers, and with storage a larger file is fine. Same sentence as `overCapMessage`
       (only the number differs), as an `ApiError` so callers read it via `apiDetail`. */
    if (file.size > MAX_BUFFERED_ATTACHMENT_SIZE) {
      throw new ApiError(
        413,
        `${file.name} (${formatBytes(file.size)}) is over the ` +
          `${formatBytes(MAX_BUFFERED_ATTACHMENT_SIZE)} limit and was not attached — this control ` +
          `plane has no object storage, so the file has to travel through the API, which will not ` +
          `carry more than that.`,
      )
    }
    const form = new FormData()
    form.append('file', file)
    return postForm<Attachment>(base, form)
  }
  /* `new Blob([file])`, NOT `file`: a `File` has a `type`, so fetch would add a `Content-Type` header,
     and the presigned PUT signs only `content-length;host` (the API's S3 presigner,
     `SIGNABLE_CONTENT_LENGTH`; `content-type` is in `FORBIDDEN_HEADERS`). A type-less Blob sends no
     `Content-Type`, and the browser sets `Content-Length` from its byte length, which matches the
     `file.size` the ticket was minted against. It wraps by reference (no copy). Stored type becomes
     `binary/octet-stream`, which is harmless — do not send the header to "fix" it. */
  await putToStorage(ticket.upload_url, new Blob([file]))
  // Confirm flips the row out of pending and returns the same `Attachment` as the legacy route.
  return post<Attachment>(`${base}/${ticket.attachment_id}/confirm`)
}

export function uploadTaskAttachment(taskId: number, file: File): Promise<Attachment> {
  return uploadSubjectAttachment({ kind: 'task', id: taskId }, file)
}

/** The subject's cached attachment list (`TaskAttachmentsField`, `lib/cachedRead.ts`), forgotten after EVERY write to
 *  it — this module's uploads and each `DELETE …/attachments/{id}` — so the form's next mount reads the server, not a
 *  list from before the write (the Answer tab and a pasted image write outside the field). */
export function forgetSubjectAttachments(subject: CommentSubject): void {
  forgetCachedRead(['subject-attachments', subject.kind, subject.id])
}

/* A ticket has no multipart door (and it would drop `visibility`). */
export async function uploadSubjectAttachment(
  subject: CommentSubject,
  file: File,
  opts: { visibility?: CommentVisibility } = {},
): Promise<Attachment> {
  try {
    return await upload(`${subjectBase(subject)}/attachments`, file, {
      buffered: subject.kind === 'task',
      visibility: opts.visibility,
    })
  } finally {
    // A failed upload may still have left a pending row; forgetting costs one read.
    forgetSubjectAttachments(subject)
  }
}

/** A task comment's image: uploaded on paste/drop, owned by the comment its id is sent with. */
export function uploadCommentImage(taskId: number, file: File): Promise<Attachment> {
  return upload(`/api/tasks/${taskId}/comment-images`, file, {
    buffered: false,
    noStorage: 'Image upload is unavailable',
  })
}

/** A support chat's file: uploaded on paste/drop/pick, bound to the message its id is sent with. */
export function uploadChatAttachment(conversationId: number, file: File): Promise<Attachment> {
  return upload(`/api/support/conversations/${conversationId}/attachments`, file, {
    buffered: false,
    noStorage: 'File upload is unavailable',
  })
}
