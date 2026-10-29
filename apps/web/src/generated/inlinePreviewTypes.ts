// GENERATED FILE — DO NOT EDIT BY HAND.
//
// Sources:     apps/api/app/routes/attachments.py  — the inline allowlist, the pinned and preview
//              suffix tables, the two upload caps (the presigned door and the multipart one accept
//              different sizes) and a comment image's types and cap: facts about the ROUTE.
//              apps/api/app/attachment_storage.py — the proxy ceiling: a fact about the STORE.
//              apps/api/app/comment_service.py — images per comment: a fact about the COMMENT.
//              Each constant's single home is the file it is read from, and neither file holds
//              a copy of the other's.
// Generator:   the inline-preview-types generator beside the repo's other gate tools; re-run
//              it to regenerate. Its test, a quick-tier gate stage with NO dependency probe,
//              fails on drift: a stage that can skip green never fires on the turn that drifts.
//
// Editing this file is not how you change what previews: edit the Python constants and re-run
// the generator. An edit here reds the gate on the next turn (that is the point of the gate).

/** THE STORED CONTENT TYPES THE SERVER WILL HAND OVER `inline` for a free-form `file` — the
 *  mirror of `INLINE_FILE_CONTENT_TYPES`, which argues family by family why each is safe there.
 *  A type absent here arrives as `attachment`, so previewing it in the tab would paint a viewer
 *  around a download the browser never renders. `artifactKinds.test.ts` asserts the agreement
 *  in both directions. */
export const INLINE_CONTENT_TYPES: readonly string[] = [
  'application/json',
  'application/pdf',
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/svg+xml',
  'image/webp',
  'text/csv',
  'text/html',
  'text/markdown',
  'text/plain',
]

/** THE SUFFIXES WHOSE STORED TYPE THIS REPO PINS rather than inheriting from the interpreter's
 *  `mimetypes` table — the mirror of `_EXTRA_SUFFIX_TYPES`. The built-in table moves between
 *  CPython PATCH releases, so a promised format left to it derives one type on one image and
 *  another on the next; these are the ones a preview branch may therefore rely on by name.
 *  Keys carry their leading dot, exactly as the Python table spells them. */
export const PINNED_SUFFIX_TYPES: Readonly<Record<string, string>> = {
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.markdown': 'text/markdown',
  '.md': 'text/markdown',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.webp': 'image/webp',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

/** THE NAMES A HUMAN'S ATTACHMENT PREVIEWS UNDER, AND THE TYPE EACH IS SERVED AS — the mirror of
 *  `PREVIEW_SUFFIX_TYPES` (`?preview=1` on the attachment content routes). Every other name downloads,
 *  so the viewer offers exactly these. Read by `components/tasks/attachmentPreview.ts`. */
export const PREVIEW_SUFFIX_TYPES: Readonly<Record<string, string>> = {
  '.csv': 'text/plain',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.json': 'text/plain',
  '.log': 'text/plain',
  '.markdown': 'text/plain',
  '.md': 'text/plain',
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.txt': 'text/plain',
  '.webp': 'image/webp',
  '.xml': 'text/plain',
  '.yaml': 'text/plain',
  '.yml': 'text/plain',
}

/** THE UPLOAD CEILING IN BYTES — the mirror of `MAX_ATTACHMENT_SIZE`, the bound the upload route
 *  enforces with a 413 (`_read_bounded`); each org's own limit sits under it (`lib/maxFileBytes`,
 *  which the pickers read). THE BROWSER CHECKS FIRST, and that ordering is the
 *  whole point rather than an optimisation: the create form STAGES a picked file and uploads it
 *  only after `POST /api/tasks` has already succeeded, so a file over the cap is refused at a
 *  moment when the task exists and the form has nothing left to offer but a retry that can never
 *  succeed. Refusing the PICK is what keeps that state unreachable.
 *  Advisory, never authoritative: the server enforces the same number and is the only thing that
 *  can. */
export const MAX_ATTACHMENT_SIZE = 5368709120

/** THE LEGACY MULTIPART DOOR'S CAP IN BYTES — the mirror of `MAX_BUFFERED_ATTACHMENT_SIZE`, and a
 *  fraction of the cap above rather than a second spelling of it. Two doors, two numbers: the
 *  ticket door presigns and the CLIENT carries the bytes to S3, so a large file costs the control plane
 *  nothing, while `POST …/attachments` still reads the whole body into the one asyncio process
 *  (`_read_bounded`) and so stops at 25 MB — raising it would make that hazard four times worse on
 *  the only path that still has it.
 *
 *  WHY THE BROWSER HAS TO KNOW BOTH. The multipart door is the FALLBACK for a control plane with
 *  no object storage, which is a shipped state and not an outage: the ticket route answers 503 and
 *  `lib/uploadTaskAttachment` re-sends the same file through it. Checked only against the cap
 *  above, a 40 MB pick cleared the pick-time refusal, the task was created, and the fallback
 *  then answered 413 with `attachment exceeds the 26214400 byte cap` — a number the operator had
 *  just been told was higher, on a "Retry upload" that could never succeed. The fallback arm
 *  refuses the file against THIS number instead, so what the operator is shown is the cap that
 *  actually applied.
 *
 *  Advisory in the same sense as the cap above: `_read_bounded` applies
 *  `min(MAX_ATTACHMENT_SIZE, MAX_BUFFERED_ATTACHMENT_SIZE)` and is the only thing that decides.
 *  Read by `lib/uploadTaskAttachment.ts`. */
export const MAX_BUFFERED_ATTACHMENT_SIZE = 26214400

/** THE PREVIEW CEILING IN BYTES — the mirror of `attachment_storage.MAX_PROXY_BYTES`, and NOT a
 *  second spelling of the upload cap above. The cap bounds what may be STORED (5 GB); this
 *  bounds what the control plane will BUFFER to serve a preview from its OWN origin, and the two
 *  are far apart on purpose — the control plane is one asyncio process, so a 100 MB `report`
 *  proxied through it would be a 100 MB allocation with the node bus and every agent heartbeat
 *  queued behind it.
 *
 *  WHY THE CONSOLE HAS TO KNOW, AND IT IS TWO DIFFERENT REASONS. Past this bound
 *  `artifact_content` stops proxying and answers a 307 to a presigned S3 GET signed
 *  `response-content-disposition=attachment` — correct, and a DOWNLOAD. A branch that NAVIGATES a
 *  frame to that URL (the sandboxed `report` frame, the PDF frame) therefore gets an EMPTY element
 *  while the browser tries to save a file instead — and inside the frame's `sandbox`, with no
 *  `allow-downloads`, the save is dropped too, so the operator is left with a blank panel and no
 *  error on exactly the artifacts most worth reading. The SUBRESOURCE branches are NOT that
 *  failure and must not be described as one: an `<img src>` and a `fetch` ignore
 *  `Content-Disposition` entirely, so they would render — what routes them to the card past this
 *  bound is WEIGHT, 8-100 MB pulled cross-origin into a bitmap or into a JS string held in
 *  component state. Routing both to the download card BEFORE a `src` is ever set is what closes
 *  them, and the whole 8-100 MB window they cover only became reachable when the upload cap
 *  quadrupled.
 *
 *  Advisory in the same sense the cap is: the server enforces this and is the only thing that
 *  can. What the projection buys is that the console stops PAINTING A VIEWER around a response
 *  it is about to be redirected away from. Read by `components/tasks/artifactKinds.ts`. */
export const MAX_PROXY_BYTES = 8388608

/** WHAT A COMMENT IMAGE MAY BE — the mirror of `COMMENT_IMAGE_CONTENT_TYPES`: raster only, since
 *  the Comments tab draws it in an `<img>` on the console's own origin. Advisory: the ticket route
 *  refuses anything else. Read by `components/tasks/useCommentImages.ts`. */
export const COMMENT_IMAGE_CONTENT_TYPES: readonly string[] = [
  'image/gif',
  'image/jpeg',
  'image/png',
  'image/webp',
]

/** A COMMENT IMAGE'S CAP IN BYTES — the mirror of `MAX_COMMENT_IMAGE_SIZE`, refused at the ticket
 *  and re-asked at the confirm. Advisory. Read by `components/tasks/useCommentImages.ts`. */
export const MAX_COMMENT_IMAGE_SIZE = 10485760

/** MOST IMAGES ONE COMMENT CARRIES — the mirror of `comment_service.MAX_IMAGES` (`image_ids`'
 *  `max_length`). Advisory. Read by `components/tasks/useCommentImages.ts`. */
export const MAX_COMMENT_IMAGES = 10
