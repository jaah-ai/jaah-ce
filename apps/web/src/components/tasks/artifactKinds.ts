// ── The artifact-kind vocabulary the Artifacts tab reads its labels from ─────────────────────────────
//
// A LEAF (the `taskPersona.ts` rule): it rides the chunk two lazy routes share, so values only — no
// React, no api, no component. Its one import, `generated/inlinePreviewTypes.ts`, imports nothing.
// `MAX_PROXY_BYTES` is the server's decision (where it stops proxying and 307s to S3 as `attachment`),
// so it is generated, never hand-typed; see `artifactRenderMode`.
//
// HAND-MIRRORED from `apps/api/app/task_manifest.py::ARTIFACT_KIND_LABELS` (the one home); nothing
// generated crosses. Exact-set tests on both sides fail on a one-sided change (`artifactKinds.test.ts`
// and the server's manifest-shape test). ALL EIGHT KINDS are spelled though the tab lists four: half
// a map cannot disagree with the server's. `file` says nothing about format; its surface reads
// `content_type` and filename.

// Generated from the Python constant; see the header.
import { MAX_PROXY_BYTES } from '../../generated/inlinePreviewTypes'

export const ARTIFACT_KIND_LABELS: Readonly<Record<string, string>> = {
  report: 'Report',
  prd: 'Requirements (PRD)',
  architecture: 'Architecture',
  file: 'File',
  coders: 'Coders',
  merged_pr: 'Merged PR',
  no_pr: 'No-PR terminal',
  finish_report: 'Finish report',
}

/** The no-PR reason a task with no repository settles with: nothing to merge, so its deliverable is its
 *  `report`. A task surface shows that report's label, linking to the Artifacts tab, rather than a no-PR
 *  phrase — the `no_pr` label above is never the words a reader of such a task sees. */
export const NO_REPO_REASON = 'no_repo'

/** The stamp a plan settles with when there is nothing to build (`app/task_settle.py::NO_BUILD_OUTCOME_REASON`):
 *  the plan document is the epic's deliverable, so its surface reads like a no-repository task's —
 *  the report's label, never a "closed with no PR" phrase. */
export const NO_BUILD_REASON = 'no_build'

/** Whether a no-PR reason means the task's deliverable is its report: no repository, or nothing to build. */
export function isReportOutcome(reason: string | null | undefined): boolean {
  return reason === NO_REPO_REASON || reason === NO_BUILD_REASON
}

/** The four kinds that become rows — `app/task_manifest.py::DOCUMENT_KINDS`, IN THE SERVER'S ORDER (the
 *  exact-set test polices sequence too). */
export const DOCUMENT_KINDS: readonly string[] = ['report', 'prd', 'architecture', 'file']

/* ── THE TWO PREDICATES BELOW PARTITION THE KINDS, BOTH ON THE KIND ──
 *
 * Markdown kinds render as console DOM, the free-form kind has no pinned format, and the rest is
 * self-contained HTML for the sandboxed frame — the DEFAULT, the only branch safe to fall into.
 *
 * KEYED ON THE KIND, NEVER `content_type`: the server FORCES the markdown kinds' type from the kind
 * (`app/task_manifest.py::KIND_MEDIA`), and a free-form row's type is derived from the upload filename
 * (`application/octet-stream` on a miss). The kind is the only server-controlled fact. The free-form
 * renderer is picked by `artifactRenderMode`. */

/** Whether this kind's bytes are markdown a `RichTextRenderer` should format, as opposed to the
 *  self-contained HTML a `report` is and which only the sandboxed frame may render. */
export function isMarkdownArtifactKind(kind: string): boolean {
  return kind === 'prd' || kind === 'architecture'
}

/** Whether this kind has NO PINNED FORMAT — `app/task_manifest.py::FREEFORM_KINDS`.
 *
 *  NOT THE DISPOSITION AXIS: `inline` vs `attachment` is decided by
 *  `app/routes/attachments.py::artifact_content`. This is the ENTRY CONDITION to `artifactRenderMode`'s
 *  closed table; pinned kinds skip it.
 *
 *  IT ALSO DECIDES WHAT `current` MEANS: on this kind every file is its own deliverable, so `current`
 *  marks only the newest arrival. A surface rendering `current` owes this predicate a check. */
export function isDownloadArtifactKind(kind: string): boolean {
  return kind === 'file'
}

/** A kind as a human reads it — NEVER the raw slug (AC11.3). An unknown kind (older UI, newer server) is
 *  HUMANISED. A safety net only; the exact-set test keeps the map complete. */
export function artifactKindLabel(kind: string): string {
  const known = ARTIFACT_KIND_LABELS[kind]
  if (known) return known
  const words = kind.replace(/[_-]+/g, ' ').trim()
  if (!words) return 'Artifact'
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/* ── FROM A ROW TO A RENDERER ──
 *
 * The free-form kind routes by INSPECTION into a CLOSED ALLOW-LIST; THE FALLBACK IS THE DOWNLOAD CARD.
 * Never invert it: an unknown format in a renderer fails SILENTLY; in the card it
 * costs a click.
 *
 * THE EXTENSION LEADS, THE CONTENT TYPE IS A FALLBACK: the type is derived from the filename and is
 * `application/octet-stream` on a miss, so it knows strictly less — except when there is no extension.
 *
 * THE FRAME BRANCH READS BOTH: the content route sends `inline` from the stored type, and a sandboxed
 * frame pointed at an `attachment` paints nothing, so name and type must AGREE. Image and text need no
 * check — `<img src>` and `fetch` ignore `Content-Disposition`.
 */

/** Which viewer the tab hands an artifact to; `download` (the card) is what every unrecognised row gets.
 *
 *  `frame` AND `pdf` ARE BOTH IFRAMES BUT TWO MODES: `frame` carries `sandbox` and `pdf` must not.
 *  Named apart so it can never collapse to a ternary; see `TaskArtifacts.tsx`'s branches. */
export type ArtifactRenderMode = 'frame' | 'pdf' | 'image' | 'markdown' | 'text' | 'download'

/** THE CEILING ON BYTES THIS TAB WILL DECODE INTO A JS STRING, for the `markdown` and `text` inspected
 *  branches. The upload cap is 100MB; decoding a large log into component state wastes memory for an
 *  unreadable wall, so past 2MB (~30k lines) the answer is the download card.
 *
 *  THE INNER OF TWO CEILINGS: `MAX_PROXY_BYTES` (8MB) binds first and binds every branch. Do not collapse
 *  them — raise this to 8MB and the tab renders walls; lower that to 2MB and the API stops serving
 *  framable 4MB screenshots. */
export const TEXT_PREVIEW_MAX_BYTES = 2 * 1024 * 1024

/* The closed table, grouped by BRANCH so one lookup answers "what happens to a `.csv`?". Lower-case, no
   dot. */
const FRAME_EXTENSIONS: readonly string[] = ['html', 'htm']
const IMAGE_EXTENSIONS: readonly string[] = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg']
const MARKDOWN_EXTENSIONS: readonly string[] = ['md', 'markdown']
const TEXT_EXTENSIONS: readonly string[] = ['txt', 'log', 'json', 'csv']

/* PDF IS A FRAME BUT NOT IN `FRAME_EXTENSIONS`. Measured: a plain `<iframe>` renders the PDF;
   `sandbox=""` gives a grey box — even with NO response CSP, and with `allow-scripts`. The `sandbox`
   ATTRIBUTE blocks Chrome's extension-backed viewer; no header change rescues it, so weakening the
   server's CSP would buy nothing. So `.pdf` gets its own mode. What the unsandboxed frame costs and what
   contains it instead is single-homed in the `pdf` branch of `TaskArtifacts.tsx`. */

/** The lower-case extension, or `''`. Rejects a dotfile, no dot, a trailing dot and a long tail, so a
 *  version-string tail (`report.2026-08-28`) cannot pick a renderer. */
function artifactExtension(filename: string): string {
  const dot = filename.lastIndexOf('.')
  if (dot <= 0) return ''
  const ext = filename.slice(dot + 1)
  return /^[a-z0-9]{1,8}$/i.test(ext) ? ext.toLowerCase() : ''
}

/** The stored type without parameters. Absent and `application/octet-stream` (written on a lookup miss)
 *  both return `''`, a don't-know no branch may act on. */
function contentTypeEssence(contentType: string | null): string {
  const essence = (contentType ?? '').split(';')[0].trim().toLowerCase()
  return essence === 'application/octet-stream' ? '' : essence
}

/** The renderer for a free-form row, by the table above. */
function freeFormRenderMode(filename: string, contentType: string | null, sizeBytes: number): ArtifactRenderMode {
  const ext = artifactExtension(filename)
  const essence = contentTypeEssence(contentType)
  // Past the ceiling nothing is fetched. Checked before the table so a new text-ish extension inherits
  // the cap.
  const withinFetchCap = sizeBytes <= TEXT_PREVIEW_MAX_BYTES

  if (ext) {
    if (FRAME_EXTENSIONS.includes(ext)) return essence === 'text/html' ? 'frame' : 'download'
    // Same name/type agreement as the frame: a PDF is rendered by NAVIGATING a frame, and the route sends
    // `inline` only for a stored `application/pdf`.
    if (ext === 'pdf') return essence === 'application/pdf' ? 'pdf' : 'download'
    if (IMAGE_EXTENSIONS.includes(ext)) return 'image'
    if (MARKDOWN_EXTENSIONS.includes(ext)) return withinFetchCap ? 'markdown' : 'download'
    if (TEXT_EXTENSIONS.includes(ext)) return withinFetchCap ? 'text' : 'download'
    // A KNOWN-BUT-UNLISTED EXTENSION STOPS HERE, never falling to the type sniff: a `.xlsx` with a
    // `text/` type is still a spreadsheet, and must not reach `getText`.
    return 'download'
  }

  // NO USABLE EXTENSION — the one case the stored type knows more than the name.
  if (essence === 'text/html') return 'frame'
  if (essence === 'application/pdf') return 'pdf'
  if (essence.startsWith('image/')) return 'image'
  if (essence === 'text/markdown') return withinFetchCap ? 'markdown' : 'download'
  if (essence === 'text/plain' || essence === 'text/csv' || essence === 'application/json') {
    return withinFetchCap ? 'text' : 'download'
  }
  return 'download'
}

/** THE VIEWER'S WHOLE SWITCH, pure so it is testable without React and has one copy.
 *
 *  THE FORMAT-PINNED KINDS SHORT-CIRCUIT and are never inspected: a PRD named `requirements.html` is
 *  still markdown. */
export function artifactRenderMode(artifact: {
  kind: string
  filename: string
  content_type: string | null
  size_bytes: number
}): ArtifactRenderMode {
  // THE PROXY CEILING BINDS FIRST, ABOVE BOTH SHORT-CIRCUITS: past it `artifact_content` 307s to a
  // presigned S3 GET signed `response-content-disposition=attachment`.
  // The cost differs per branch: a NAVIGATED frame (`frame`, `pdf`) honours the disposition and paints an
  // EMPTY element (inside the frame's `sandbox`, with no `allow-downloads`, the save is dropped too). `<img src>` and `fetch` ignore it and
  // would render, but pull 8-100MB cross-origin — WEIGHT, not blankness. Both are closed by answering
  // `'download'` before any `src` or `fetch`. It sits above the markdown arm specifically: a pinned `prd`
  // never enters `freeFormRenderMode`, so `TEXT_PREVIEW_MAX_BYTES` cannot cover it.
  // `>` AND NOT `>=`, matching `attachments.py::artifact_content`: exactly MAX_PROXY_BYTES is proxied.
  if (artifact.size_bytes > MAX_PROXY_BYTES) return 'download'
  if (isMarkdownArtifactKind(artifact.kind)) return 'markdown'
  if (!isDownloadArtifactKind(artifact.kind)) return 'frame'
  return freeFormRenderMode(artifact.filename, artifact.content_type, artifact.size_bytes)
}
