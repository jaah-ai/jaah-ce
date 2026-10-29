import { useEffect, useRef, useState } from 'react'
import type { JSX, KeyboardEvent, MouseEvent } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Chip from '@mui/material/Chip'
import CircularProgress from '@mui/material/CircularProgress'
import Tooltip from '@mui/material/Tooltip'
import { nanoid } from 'nanoid'
import { apiDetail, apiHref, del, get, useApiRewriterVersion } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { cachedRead, CATALOGUE_STALE_MS, forgetCachedRead } from '../../lib/cachedRead'
import { useSubmit } from '../../hooks/useSubmit'
// The ticket → PUT → complete sequence from its one home (`TaskEditForm` runs the same one).
import { uploadSubjectAttachment, uploadTaskAttachment } from '../../lib/uploadTaskAttachment'
import { subjectBase, type CommentSubject, type CommentVisibility } from '../../lib/commentSubject'
import { useMaxFileBytes } from '../../lib/maxFileBytes'
import { formatBytes } from '../../lib/formatBytes'
// The shared download arrow (`ui/DownloadGlyph`). 13px, not 16: it sits inside a 24px chip.
import { DownloadGlyph } from '../../ui/DownloadGlyph'
import type { Attachment } from '../../types'
import { Banner } from '../../ui/Banner'
import { PaperclipIcon } from '../../ui/icons'
import { PHONE } from '../../ui/listStyles'
import { AXIS_TRACK_GROUND, AXIS_TRACK_RADIUS } from './axisTrackLook'
import { attachmentPreviewMode } from './attachmentPreview'
import AttachmentViewer, { type ViewerItem } from './AttachmentViewer'

/* ── Attachments as one wrapping form row: an Attach button and one chip per file ──
 * Sits in `TaskEditForm`'s settings row; no heading, helper paragraph, empty-state sentence or card.
 * No drag/drop or paste: a ~7rem button is too narrow a drop target and misses land on neighbouring
 * checkboxes; the Description editor (`richtext/RichTextEditor`) keeps the image paste path.
 * A failed list read shows a banner under a row that still works, so Attach stays available (an upload
 * then gets no confirming chip, since the refetch fails the same way).
 */

export type StagedAttachment = { key: string; file: File }

// The default for a host that never stages (a ticket thread); module-level so it is one stable array.
const NO_STAGED: StagedAttachment[] = []

// Checks the three fields this row consumes: `a.id` goes into the DELETE path, the others are rendered.
function isAttachmentList(data: unknown): data is Attachment[] {
  return Array.isArray(data) && data.every(a =>
    typeof a === 'object' && a !== null
    && typeof (a as Attachment).id === 'number'
    && typeof (a as Attachment).filename === 'string'
    && typeof (a as Attachment).size_bytes === 'number')
}

/* The bytes of an uploaded attachment, for a plain anchor. No `?download=1`: unlike
`artifact_content`, `user_task_attachment_content` (`app/routes/attachments.py`) always answers
`Content-Disposition: attachment`. A same-origin `<a href download>` (as in `TaskArtifacts.tsx`): the
cookie rides the navigation and the browser handles transfer and Save. The route is `tasks:view`, so
the download is not gated on `!readOnly`; every write affordance stays gated. */
export function attachmentContentUrl(subject: CommentSubject, attachmentId: number): string {
  return apiHref(`${subjectBase(subject)}/attachments/${attachmentId}/content`)
}

/** An uploaded file as `AttachmentViewer` shows it, or `null` when it only downloads (`attachmentPreviewMode`). */
export function viewerItem(subject: CommentSubject, a: Attachment): ViewerItem | null {
  const mode = attachmentPreviewMode(a.filename, a.size_bytes)
  if (mode === null) return null
  return {
    id: a.id,
    name: a.filename,
    size: a.size_bytes,
    mode,
    previewPath: `${subjectBase(subject)}/attachments/${a.id}/content?preview=1`,
    downloadHref: attachmentContentUrl(subject, a.id),
  }
}

/** The viewer's open file plus its `FileChip` hook-up, for a host that lists uploaded files. */
export function useAttachmentViewer(items: ViewerItem[]) {
  const [openId, setOpenId] = useState<number | null>(null)
  const previewFor = (id: number): ChipPreview | undefined => {
    const item = items.find(i => i.id === id)
    return item && { mode: item.mode, src: apiHref(item.previewPath), open: () => setOpenId(id) }
  }
  const viewer = <AttachmentViewer items={items} openId={openId} onOpenIdChange={setOpenId} />
  return { previewFor, viewer }
}

/* Why a client-side cap check exists although the server enforces it: a create-form pick is staged and
uploaded only after `POST /api/tasks` succeeds, so a 413 would arrive with the task already created and
the editor locked, leaving an unrecoverable "Retry upload". Refusing the pick keeps that state
unreachable.
Advisory: `cap` is the org's per-file limit (`lib/maxFileBytes::useMaxFileBytes`); the server still decides.
It is the higher of two server caps: the legacy multipart door (object-storage-less control planes only)
applies `min(cap, MAX_BUFFERED_ATTACHMENT_SIZE)`, known only after the ticket route 503s, so that refusal
lives in `lib/uploadTaskAttachment`'s fallback arm. */
export function overCapMessage(files: File[], maxBytes: number): string {
  const cap = formatBytes(maxBytes)
  const named = files.map(f => `${f.name} (${formatBytes(f.size)})`).join(', ')
  return files.length === 1
    ? `${named} is over the ${cap} limit and was not attached.`
    : `${files.length} files are over the ${cap} limit and were not attached: ${named}.`
}

/* A tighter `columnGap` than the settings row so the members read as a group. `flexWrap` and no
`overflow`: the row must never raise a horizontal scrollbar in `.app-main` (seen at 481-490px).
`minWidth: 0` lets the chip cap below bite. */
const ROW_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: '0.5rem',
  rowGap: '0.375rem',
  minWidth: 0,
} as const

/* The full-size Attach: `color="inherit"`, the form's own text colour (an accessory beside other controls wears
`QUIET_ATTACH_SX` instead). `minHeight: 44` at ≤480px is the touch floor; `nowrap` keeps the label on one line. */
const ATTACH_SX = {
  whiteSpace: 'nowrap',
  flexShrink: 0,
  [PHONE]: { minHeight: 44 },
} as const

/** An accessory Attach's quiet outline (MUI `ToggleButton`'s divider border, `action.active` glyph): `inherit`'s
 *  currentColor outline made it the loudest control on its line. The answer form's Attach wears it too
 *  (`AnswerAttachments`). */
export const QUIET_ATTACH_SX = { color: 'action.active', borderColor: 'divider' } as const

/* Compact, the paperclip sits among the axes' pills, so it takes their whole look: the quiet outline plus the
track's round shape and recessed ground, not a rounded square beside fully round groups. The ground goes
through MUI's `--variant-outlinedBg`, never `backgroundColor`: the outlined Button paints that variable and its hover
only changes it, so a plain background would freeze the hover. */
const COMPACT_ATTACH_SX = {
  ...ATTACH_SX,
  minWidth: 44,
  ...QUIET_ATTACH_SX,
  borderRadius: AXIS_TRACK_RADIUS,
  '--variant-outlinedBg': AXIS_TRACK_GROUND,
} as const

/** The widest a chip grows to fit its name (~50 characters); its cell's floor reads it too (`TaskSpecFields`). */
export const ATTACHMENT_CHIP_MAX = '20rem'

/* The chip's width contract, two rules read together:
· `maxWidth: min(ATTACHMENT_CHIP_MAX, 100%)` — a name shows whole up to ~50 characters, and `min(…, 100%)` for the
~200px form column at 481-490px.
· `flexShrink: 0` — wrap but never shrink, or MUI's ellipsizing label turns every chip into a stub.
Height goes to 30px at ≤480px (an increase): the × is the only control, and a 24px chip gives it an
~11px hit box. Not 44px, which would be taller than the chip. */
const CHIP_SX = {
  maxWidth: `min(${ATTACHMENT_CHIP_MAX}, 100%)`,
  minWidth: 0,
  flexShrink: 0,
  [PHONE]: { height: 30 },
} as const

/* The origin of a group of chips ("from task N"), and the files' region, each a row member of its own rather than
part of a chip: the settings row gives this field as little as 96px, where a chip has no room for a filename AND its
origin. As a flex item it sits inline when the row is wide and on its own line under its chips when it is narrow. */
const ORIGIN_SX = { fontSize: '0.75rem', color: 'text.secondary', whiteSpace: 'nowrap', flexShrink: 0 } as const

const REMOVE_SX = {
  px: 0.5,
  lineHeight: 1,
  fontSize: '1rem',
  cursor: 'pointer',
  color: 'text.secondary',
  '&:hover': { color: 'error.main' },
  '&[aria-disabled="true"]': { cursor: 'default', opacity: 0.5, '&:hover': { color: 'text.secondary' } },
  [PHONE]: { display: 'inline-flex', alignItems: 'center', justifyContent: 'center', minWidth: 30, minHeight: 30 },
} as const

/* The download anchor's box, not a copy of `REMOVE_SX`: hover goes to `primary.main` (the × uses
`error.main`), and no disabled branch — a read is untouched by `locked` and `readOnly`. The ≤480px box
matches the ×'s 30px. `textDecoration: 'none'` because it is an `<a>`. */
const DOWNLOAD_SX = {
  px: 0.5,
  cursor: 'pointer',
  color: 'text.secondary',
  textDecoration: 'none',
  display: 'inline-flex',
  alignItems: 'center',
  '&:hover': { color: 'primary.main' },
  [PHONE]: { justifyContent: 'center', minWidth: 30, minHeight: 30 },
} as const

/** What a chip needs to open its file in the viewer; an image also draws a thumbnail from `src`. */
export type ChipPreview = { mode: ViewerItem['mode']; src: string; open: () => void }

/* An image's chip is a THUMBNAIL: a small rectangle of the picture itself (Slack's look), 40px tall so a row of
them stays one compact line beside the 24px chips. The × sits on its corner, a badge, so the thumbnail keeps its
whole face; 24px at ≤480px, where it is the only touch target for removing. Download lives in the viewer. */
const THUMB_SX = {
  position: 'relative',
  flexShrink: 0,
  width: 56,
  height: 40,
} as const

const THUMB_FACE_SX = {
  width: '100%',
  height: '100%',
  borderRadius: 1,
  overflow: 'hidden',
  border: '1px solid',
  borderColor: 'divider',
  backgroundColor: 'action.hover',
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 1 },
  '& img': { width: '100%', height: '100%', objectFit: 'cover', display: 'block' },
} as const

const THUMB_REMOVE_SX = {
  position: 'absolute',
  top: -6,
  right: -6,
  width: 18,
  height: 18,
  borderRadius: '50%',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  fontSize: '0.875rem',
  lineHeight: 1,
  cursor: 'pointer',
  color: 'text.secondary',
  backgroundColor: 'background.paper',
  border: '1px solid',
  borderColor: 'divider',
  '&:hover': { color: 'error.main' },
  '&[aria-disabled="true"]': { cursor: 'default', opacity: 0.5, '&:hover': { color: 'text.secondary' } },
  [PHONE]: { width: 24, height: 24 },
} as const

/* A previewable chip's name opens the viewer: a real button inside the label, so the chip root stays the
non-interactive group described below. Reads as the name it is, until hovered. */
const NAME_BUTTON_SX = {
  all: 'unset',
  cursor: 'pointer',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  '&:hover': { textDecoration: 'underline' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 1, borderRadius: 0.5 },
} as const

/* One file, staged or uploaded — they look identical, except only an uploaded chip has bytes to
download (`href`).
The chip root is not a button: with `onDelete` MUI makes the root a `ButtonBase`, nesting the named ×
inside an unnamed button. `role="group"` + `tabIndex={-1}` leaves one named control per chip. The × is
a `role="button"` span with its own `aria-label` and Enter/Space handling. */
export function FileChip({
  name,
  size,
  href,
  internal = false,
  sourceTaskId = null,
  regionLabel = null,
  removable,
  disabled,
  onRemove,
  testId,
  preview,
}: {
  name: string
  size: number
  /** The content URL for an uploaded file; `undefined` for a staged pick. */
  href?: string
  /** A ticket's staff-only file: marked in the label, so it is never mistaken for a shared one. */
  internal?: boolean
  /** The task this file was attached FROM (a filer's own artifact); named in the tooltip. */
  sourceTaskId?: number | null
  /** Where the bytes are stored; named in the tooltip. */
  regionLabel?: string | null
  removable: boolean
  disabled: boolean
  onRemove: () => void
  testId: string
  /** An uploaded file the viewer can show (`useAttachmentViewer`); absent, the chip only downloads. */
  preview?: ChipPreview
}): JSX.Element {
  // A picture that will not decode falls back to the plain chip, so the file stays reachable.
  const [thumbFailed, setThumbFailed] = useState(false)
  const remove = () => { if (!disabled) onRemove() }
  const origin = sourceTaskId == null ? null : `from task ${sourceTaskId}`
  // Size in the tooltip, not the label; the full name rides along because the label ellipsizes.
  const tooltip = [name, origin, formatBytes(size), regionLabel ? `Region: ${regionLabel}` : null].filter(Boolean).join(' · ')
  const removeControl = (sx: object) => (
    <Box
      component="span"
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled ? 'true' : undefined}
      // Named by file, so four chips are not four identical buttons.
      aria-label={`Remove ${name}`}
      onClick={(e: MouseEvent) => { e.stopPropagation(); remove() }}
      onKeyDown={(e: KeyboardEvent) => {
        if (e.key !== 'Enter' && e.key !== ' ') return
        e.preventDefault()
        e.stopPropagation() // the chip root carries MUI's own key handling; this press is the ×'s
        remove()
      }}
      sx={sx}
    >
      ×
    </Box>
  )
  if (preview?.mode === 'image' && !thumbFailed) {
    return (
      <Box sx={THUMB_SX} role="group" data-testid={testId} data-internal={internal ? 'true' : undefined}>
        <Tooltip title={internal ? `Internal · ${tooltip}` : tooltip}>
          <ButtonBase
            aria-label={`Preview ${name}`}
            onClick={preview.open}
            data-testid={`${testId}-thumb`}
            sx={{ ...THUMB_FACE_SX, ...(internal ? { borderColor: 'warning.main' } : {}) }}
          >
            <img src={preview.src} alt="" loading="lazy" onError={() => setThumbFailed(true)} />
          </ButtonBase>
        </Tooltip>
        {removable && removeControl(THUMB_REMOVE_SX)}
      </Box>
    )
  }
  const shownName = preview ? (
    <Box component="button" type="button" aria-label={`Preview ${name}`} onClick={preview.open} data-testid={`${testId}-open`} sx={NAME_BUTTON_SX}>
      {name}
    </Box>
  ) : name
  return (
    // The chip's ONE tooltip: its download and × controls are named by `aria-label` only — a native `title`
    // on either would pop the browser's tooltip on top of this one.
    <Tooltip title={tooltip}>
      <Chip
        size="small"
        variant="outlined"
        role="group"
        tabIndex={-1}
        data-testid={testId}
        data-internal={internal ? 'true' : undefined}
        label={internal ? (
          <>
            <Box component="span" sx={{ color: 'warning.dark', fontWeight: 600, mr: 0.5 }}>Internal</Box>
            {shownName}
          </>
        ) : shownName}
        sx={CHIP_SX}
        /* The leading slot: MUI's single trailing slot (`deleteIcon`) is cloned with its own `onClick`, so a
        download placed there would fire the delete. Leading also puts the two controls at opposite ends. */
        icon={
          href === undefined ? undefined : (
            <Box
              component="a"
              href={href}
              // The Save dialog's name; the route's `Content-Disposition` reads the same stored `filename`.
              download={name}
              data-testid={`${testId}-download`}
              // Named by file, so four chips are not four identical buttons.
              aria-label={`Download ${name}`}
              // `stopPropagation`, not `preventDefault`: the navigation must happen.
              onClick={e => e.stopPropagation()}
              sx={DOWNLOAD_SX}
            >
              <DownloadGlyph size={13} />
            </Box>
          )
        }
        onDelete={removable ? remove : undefined}
        deleteIcon={removeControl(REMOVE_SX)}
      />
    </Tooltip>
  )
}

export default function TaskAttachmentsField({
  taskId = null,
  subject: subjectProp,
  visibility,
  body = '',
  staged = NO_STAGED,
  onStagedChange = () => undefined,
  onHasFilesChange,
  readOnly = false,
  disabled = false,
  compact = false,
  testId = 'task-form-attachments',
}: {
  /** `null` on the create form: no task yet, so picks are staged. Shorthand for a task `subject`. */
  taskId?: number | null
  /** What the files hang off (a support ticket); wins over `taskId`. */
  subject?: CommentSubject
  /** Minted onto each upload (a ticket's internal toggle); omitted, the ticket body carries none. */
  visibility?: CommentVisibility
  /** The markdown Description, read only for `hidden` below. */
  body?: string
  /** Create-mode picks, owned by the host form, which POSTs them once the create returns an id. */
  staged?: StagedAttachment[]
  onStagedChange?: (next: StagedAttachment[]) => void
  /** Told whether the field shows any chip, once it knows (never mid-read), so a host can place it by that. */
  onHasFilesChange?: (hasFiles: boolean) => void
  /* `GET …/attachments` is `tasks:view`, `POST`/`DELETE` are `tasks:edit`: a read-only principal keeps
  chips readable and loses Attach and every ×. A prop, not `useCan()`: the host knows the principal. */
  readOnly?: boolean
  /** The form's `locked`. Controls stay (no reflow during a save) but refuse. */
  disabled?: boolean
  /** Attach as the paperclip alone, named by `aria-label`: a phone's axes line has room for an icon, not a word. */
  compact?: boolean
  testId?: string
}): JSX.Element {
  const maxFileBytes = useMaxFileBytes()
  // Its hrefs are rewritten on the live host (`apiHref`): re-render when the rewriter is installed.
  useApiRewriterVersion()
  // `null` = create mode (no id yet). Primitive deps below, so a fresh `subject` literal per render is fine.
  const subject: CommentSubject | null = subjectProp ?? (taskId === null ? null : { kind: 'task', id: taskId })
  const subjectKind = subject?.kind
  const subjectId = subject?.id
  const { busy, formError: actionError, setFormError: setActionError, run } = useSubmit()
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  /* Create mode never reads: there is no id (`/api/tasks/null/attachments`). The fence keeps a slower mount
  read from clobbering a post-upload refetch; the error is this field's own words, never the server's.
  Read through `lib/cachedRead`: the task detail page remounts its form on every own-task event, and a remount
  answers from cache. This field's own upload/delete FORGETS the entry first, so its re-read always
  reaches the server and the operator sees their own change. */
  const cacheKey = ['subject-attachments', subjectKind ?? null, subjectId ?? null] as const
  const list = useApiRead(
    // Its own name, never `cacheKey`: the fetcher reads `cacheKey` through the app client, and one key for both
    // would have the query wait on itself.
    ['task-attachments-field', subjectKind ?? null, subjectId ?? null],
    // `subject!`: `enabled` holds the read while it is null.
    () => cachedRead(cacheKey, () => get<Attachment[]>(`${subjectBase(subject!)}/attachments`), CATALOGUE_STALE_MS)
      .then(data => (isAttachmentList(data) ? data : [])),
    'Failed to load attachments',
    { enabled: subject !== null },
  )
  const attachments = list.data ?? []
  const loadError = list.error ? 'Failed to load attachments' : ''
  const loadAttachments = () => {
    forgetCachedRead(cacheKey)
    return list.refresh()
  }

  /* One `uploadTaskAttachment` sequence per file under `Promise.allSettled` (no batch endpoint). In create
  mode picks are handed to the host instead. A task goes through `uploadTaskAttachment` itself, the seam
  `TaskEditForm`'s tests mock. */
  async function addFiles(files: File[]) {
    if (files.length === 0 || busy) return
    /* The cap is checked here, once, for both modes (see `overCapMessage`). A partition, not an abort: good
    files attach and the over-cap one is named. */
    const overCap = files.filter(f => f.size > maxFileBytes)
    const accepted = files.filter(f => f.size <= maxFileBytes)
    // Collected, so an upload failure does not replace the over-cap notice (React keeps the last set).
    const problems = overCap.length > 0 ? [overCapMessage(overCap, maxFileBytes)] : []
    if (accepted.length === 0) {
      setActionError(problems.join(' '))
      return
    }
    if (subject === null) {
      // `nanoid`, not the filename: two `screenshot.png` picks are two chips.
      onStagedChange([...staged, ...accepted.map(file => ({ key: nanoid(10), file }))])
      setActionError(problems.join(' '))
      return
    }
    // Each file's failure is collected into `problems`; the fallback covers only an unexpected throw.
    await run('Upload failed', async () => {
      const results = await Promise.allSettled(
        accepted.map(file => (subject.kind === 'task'
          ? uploadTaskAttachment(subject.id, file)
          : uploadSubjectAttachment(subject, file, { visibility }))),
      )
      const failures = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected')
      if (failures.length > 0) {
        problems.push(
          apiDetail(
            failures[0].reason,
            `Failed to upload ${failures.length} of ${accepted.length} attachment(s)`,
          ),
        )
      }
      setActionError(problems.join(' '))
      // Refetch before clearing `busy`, so the spinner never gives way to a stale row.
      await loadAttachments()
    })
  }

  // Synchronous: the host owns the array and nothing was sent.
  function removeStaged(key: string) {
    // `readOnly` too, as defence in depth should a chip ever render its × unconditionally.
    if (readOnly || disabled) return
    onStagedChange(staged.filter(s => s.key !== key))
  }

  async function removeUploaded(a: Attachment) {
    // `readOnly` as above; the server refuses a reader's DELETE regardless (`tasks:edit`).
    if (subject === null || readOnly || disabled || busy) return
    // A refused DELETE is never silent (the chip is still on screen) and skips the refetch.
    await run(`Failed to remove ${a.filename}`, async () => {
      await del(`${subjectBase(subject)}/attachments/${a.id}`)
      await loadAttachments()
    })
  }

  function handleFilesSelected(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? [])
    e.target.value = '' // allow re-selecting the same file(s) next time
    void addFiles(files)
  }

  /* Inline Description images get no chip: a pasted image's filename is the `attachment:<filename>`
  token in the markdown (`mintImageFilename`), and it is already visible in the editor.
  The test is the body, not the `img-` prefix, so a user file named `img-2.png` still gets a chip.
  Tokens are parsed and compared by equality, never `body.includes(...)`: a substring test hides `log.txt`
  behind `log.txt.gz`, stranding a file the × is the only way to remove. Equality matches
  `richtext/attachmentResolver` (`a.filename === token`). The token class stops at whitespace and the
  delimiters a markdown link or HTML attribute closes with. */
  const referenced = new Set(
    Array.from(body.matchAll(/attachment:([^\s)\]"'>]+)/g), m => m[1]),
  )
  const visible = attachments.filter(a => !referenced.has(a.filename))
  /* Not while a read is in flight: a host that moves the field on this answer remounts it, and a fresh mount's
  empty list must not move it straight back. */
  const known = subject === null || list.data !== null
  const hasFiles = visible.length + staged.length > 0
  useEffect(() => {
    if (known) onHasFilesChange?.(hasFiles)
  }, [known, hasFiles, onHasFilesChange])
  /* Grouped by origin, first appearance first, so each "from task N" caption follows exactly its own chips;
  files with no origin form the leading group with no caption. */
  const groups: { source: number | null; rows: Attachment[] }[] = []
  for (const a of visible) {
    const source = a.source_task_id ?? null
    const group = groups.find(g => g.source === source)
    if (group) group.rows.push(a)
    else groups.push({ source, rows: [a] })
  }
  groups.sort((x, y) => Number(x.source !== null) - Number(y.source !== null))
  // The viewer steps through the previewable files in the order their chips show.
  const { previewFor, viewer } = useAttachmentViewer(
    subject === null ? [] : groups.flatMap(g => g.rows).flatMap(a => viewerItem(subject, a) ?? []),
  )
  // Where the files are stored, as the Artifacts tab says it: visible text, not only each chip's tooltip.
  const regions = [...new Set(visible.map(a => a.region_label).filter((r): r is string => Boolean(r)))]

  const message = actionError || loadError

  return (
    <Box data-testid={testId} sx={{ display: 'flex', flexDirection: 'column', gap: '0.375rem', minWidth: 0 }}>
      <Box sx={ROW_SX}>
        {/* Not mounted read-only (the API refuses the upload); `disabled` for the temporary `locked`. */}
        {!readOnly && (
          <Button
            // Inside `TaskEditForm`'s `<form>`, a button with no `type` would submit it.
            type="button"
            size="small"
            variant="outlined"
            color="inherit"
            data-testid={`${testId}-attach`}
            disabled={disabled || busy}
            onClick={() => fileInputRef.current?.click()}
            aria-label={compact ? 'Attach' : undefined}
            sx={compact ? COMPACT_ATTACH_SX : ATTACH_SX}
          >
            {/* The answer form's paperclip, not `⬆`: one mark for Attach wherever it appears. */}
            <PaperclipIcon size="1.1em" testId={`${testId}-attach-icon`} sx={compact ? undefined : { mr: 0.5 }} />
            {!compact && 'Attach'}
          </Button>
        )}

        {/* A spinner beside the chips, never instead of them. */}
        {busy && (
          <Box
            role="status"
            aria-label="Working on attachments"
            data-testid={`${testId}-busy`}
            sx={{ display: 'inline-flex', alignItems: 'center', flexShrink: 0 }}
          >
            <CircularProgress size={14} thickness={5} />
          </Box>
        )}

        {/* Staged first (most recent). Empty in edit mode, but rendered so a host that stages anyway shows it. */}
        {staged.map(s => (
          <FileChip
            key={s.key}
            name={s.file.name}
            size={s.file.size}
            removable={!readOnly}
            disabled={disabled}
            onRemove={() => removeStaged(s.key)}
            testId={`${testId}-chip`}
          />
        ))}

        {groups.map(g => [
          ...g.rows.map(a => (
          <FileChip
            key={a.id}
            name={a.filename}
            size={a.size_bytes}
            // `subject` is non-null on every path that produced this list.
            href={subject === null ? undefined : attachmentContentUrl(subject, a.id)}
            internal={a.visibility === 'internal'}
            sourceTaskId={a.source_task_id}
            regionLabel={a.region_label}
            // A ticket's file says whether this reader may remove it; a task's never carries the flag.
            removable={!readOnly && a.may_delete !== false}
            disabled={disabled || busy}
            onRemove={() => void removeUploaded(a)}
            testId={`${testId}-chip`}
            preview={previewFor(a.id)}
          />
          )),
          g.source === null ? null : (
            <Box component="span" key={`origin-${g.source}`} data-testid={`${testId}-origin`} sx={ORIGIN_SX}>
              from task {g.source}
            </Box>
          ),
        ])}

        {regions.length > 0 && (
          <Box component="span" data-testid={`${testId}-region`} sx={ORIGIN_SX}>
            Region: {regions.join(', ')}
          </Box>
        )}

        {/* The hidden picker, driven by the ref. Inside `!readOnly` too: `display: none` hides it from a mouse
        but not from `getByLabelText`, a screen reader's forms list, or `.click()`. */}
        {!readOnly && (
          <Box
            component="input"
            type="file"
            multiple
            ref={fileInputRef}
            aria-label="Upload attachment"
            disabled={disabled || busy}
            onChange={handleFilesSelected}
            sx={{ display: 'none' }}
          />
        )}
      </Box>

      {/* Below the row. `ui/Banner` so the `error-banner` token e2e reads is emitted. */}
      {message && <Banner severity="error">{message}</Banner>}
      {viewer}
    </Box>
  )
}
