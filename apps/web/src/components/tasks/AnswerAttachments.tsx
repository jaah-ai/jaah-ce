import { useRef, useState } from 'react'
import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import { apiDetail, del } from '../../api'
import { forgetSubjectAttachments, uploadTaskAttachment } from '../../lib/uploadTaskAttachment'
import { useMaxFileBytes } from '../../lib/maxFileBytes'
import type { Attachment } from '../../types'
import { Banner } from '../../ui/Banner'
import { PaperclipIcon } from '../../ui/icons'
import { PHONE } from '../../ui/listStyles'
import { AXIS_TRACK_GROUND } from './axisTrackLook'
import { attachmentContentUrl, FileChip, overCapMessage, QUIET_ATTACH_SX, useAttachmentViewer, viewerItem } from './TaskAttachmentsField'

/* Max files one answer may carry — a MIRROR of `_MAX_ANSWER_ATTACHMENTS` in `apps/api/app/routes/tasks/answers.py`
   (Pydantic `max_length`, whose list `detail` `apiDetail` cannot render). */
export const MAX_ANSWER_ATTACHMENTS = 20

/* ── FILES THAT RIDE WITH ONE ANSWER ──
 * An Attach button and one chip per file, for ONE open round. Rendered `display: contents`, so the button,
 * spinner and chips join the host's action row instead of spending a row of their own; an error wraps onto
 * its own line there (`flexBasis: 100%`). Each pick uploads at once as an ordinary task
 * attachment (so it also joins the task's Attachments list and a later dispatch's inputs); its id is sent
 * with the answer (`attachment_ids`), which names the file's path to the runner and stages it on the
 * runner's node. The × removes the file from the answer AND deletes the upload — a file the operator took
 * back must not linger as a task input.
 * Controlled: the host (`GateForm`'s `AnswerControl`) owns the list, so it is posted with whichever control
 * sends the answer. `onBusyChange` lets the host hold its send controls while an upload is in flight.
 */
export default function AnswerAttachments({
  taskId,
  files,
  onChange,
  onBusyChange,
  disabled,
  roundLabel,
}: {
  taskId: number
  files: Attachment[]
  onChange: (next: Attachment[]) => void
  onBusyChange: (busy: boolean) => void
  disabled: boolean
  /** E.g. `round 3`, for the button's accessible name (see `AnswerControl`'s `roundLabel`). */
  roundLabel?: string
}): JSX.Element {
  const maxFileBytes = useMaxFileBytes()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement | null>(null)
  // The latest list, for the async upload tail: a removal during an upload must not be undone by it.
  const filesRef = useRef(files)
  filesRef.current = files
  const { previewFor, viewer } = useAttachmentViewer(files.flatMap(a => viewerItem({ kind: 'task', id: taskId }, a) ?? []))

  function setWorking(next: boolean) {
    setBusy(next)
    onBusyChange(next)
  }

  async function addFiles(picked: File[]) {
    if (picked.length === 0 || busy) return
    const room = MAX_ANSWER_ATTACHMENTS - files.length
    const overCap = picked.filter(f => f.size > maxFileBytes)
    const fits = picked.filter(f => f.size <= maxFileBytes)
    const accepted = fits.slice(0, Math.max(room, 0))
    const problems = overCap.length > 0 ? [overCapMessage(overCap, maxFileBytes)] : []
    if (fits.length > accepted.length) {
      problems.push(`One answer can carry ${MAX_ANSWER_ATTACHMENTS} files at most — ${fits.length - accepted.length} not attached.`)
    }
    if (accepted.length === 0) {
      setError(problems.join(' '))
      return
    }
    setWorking(true)
    setError('')
    const results = await Promise.allSettled(accepted.map(file => uploadTaskAttachment(taskId, file)))
    const uploaded = results.flatMap(r => (r.status === 'fulfilled' ? [r.value] : []))
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected')
    if (failures.length > 0) {
      problems.push(apiDetail(failures[0].reason, `Failed to upload ${failures.length} of ${accepted.length} file(s)`))
    }
    onChange([...filesRef.current, ...uploaded])
    setError(problems.join(' '))
    setWorking(false)
  }

  async function remove(a: Attachment) {
    if (busy || disabled) return
    setWorking(true)
    setError('')
    try {
      await del(`/api/tasks/${taskId}/attachments/${a.id}`)
      forgetSubjectAttachments({ kind: 'task', id: taskId })
      onChange(filesRef.current.filter(f => f.id !== a.id))
    } catch (err) {
      // Already gone (deleted from the task's Attachments list): drop the chip, or the answer stays unsendable.
      if ((err as { status?: unknown } | null)?.status === 404) {
        onChange(filesRef.current.filter(f => f.id !== a.id))
        return
      }
      setError(apiDetail(err, `Failed to remove ${a.filename}`))
    } finally {
      setWorking(false)
    }
  }

  return (
    <Box data-testid="gate-attachments" sx={{ display: 'contents' }}>
      <Button
        type="button"
        variant="outlined"
        color="inherit"
        data-testid="gate-attach"
        aria-label={roundLabel ? `Attach files to the answer for ${roundLabel}` : 'Attach files to the answer'}
        disabled={disabled || busy || files.length >= MAX_ANSWER_ATTACHMENTS}
        onClick={() => inputRef.current?.click()}
        title="Attach files"
        /* On a phone the paperclip alone: the word pushed Attach onto a line of its own under the
           answer's buttons. The accessible name is `aria-label` either way; 44px is the touch floor.
           QUIET: beside a disabled Submit, `inherit`'s outline made Attach the loudest control there.
           GROUNDED while live: the quiet outline alone matched a disabled Comment only's, so a live Attach read
           as disabled; the axis pills' recessed ground tells it apart and stays quiet. Through MUI's variable, off hover,
           so the hover still shows (see `COMPACT_ATTACH_SX`). */
        sx={{
          ...QUIET_ATTACH_SX,
          '&:not(.Mui-disabled):not(:hover)': { '--variant-outlinedBg': AXIS_TRACK_GROUND },
          whiteSpace: 'nowrap',
          flexShrink: 0,
          [PHONE]: { minHeight: 44, minWidth: 44, px: 1 },
        }}
      >
        {/* A PAPERCLIP, NOT AN ARROW: `⬆` read as "upload/send", and alone on the phone it named nothing.
            The mark is `ui/icons.tsx`'s, shared with the task form's Attach. */}
        <PaperclipIcon size="1.1em" testId="gate-attach-icon" sx={{ mr: 0.5, [PHONE]: { mr: 0 } }} />
        <Box component="span" data-testid="gate-attach-word" sx={{ [PHONE]: { display: 'none' } }}>Attach</Box>
      </Button>
      {busy && (
        <Box role="status" aria-label="Uploading files" sx={{ display: 'inline-flex', alignItems: 'center', alignSelf: 'center', flexShrink: 0 }}>
          <CircularProgress size={14} thickness={5} />
        </Box>
      )}
      {files.map(a => (
        // A flex item of the host's action row: centred against the taller buttons there.
        <Box key={a.id} sx={{ alignSelf: 'center', minWidth: 0, maxWidth: '100%' }}>
          <FileChip
            name={a.filename}
            size={a.size_bytes}
            href={attachmentContentUrl({ kind: 'task', id: taskId }, a.id)}
            removable
            disabled={disabled || busy}
            onRemove={() => void remove(a)}
            testId="gate-attachment-chip"
            preview={previewFor(a.id)}
          />
        </Box>
      ))}
      <Box
        component="input"
        type="file"
        multiple
        ref={inputRef}
        aria-label={roundLabel ? `Files for ${roundLabel}` : 'Files for the answer'}
        disabled={disabled || busy}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
          const picked = Array.from(e.target.files ?? [])
          e.target.value = '' // allow re-picking the same file
          void addFiles(picked)
        }}
        sx={{ display: 'none' }}
      />
      {error && <Box sx={{ flexBasis: '100%', minWidth: 0 }}><Banner severity="error">{error}</Banner></Box>}
      {viewer}
    </Box>
  )
}
