import { type JSX, useState } from 'react'
import Typography from '@mui/material/Typography'
import { useSubmit } from '../../hooks/useSubmit'
import { OVERRIDE_REASON_MIN } from '../../lib/releases'
import { FormDialog } from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import { refusal } from './DeployDialog'

/* THE FREEZE OVERRIDE (R7.5, mockup `pipeline-rules-mockup.html`): a person who may press the step and edit the
 * pipeline runs one request through the freeze holding it by typing a reason (at least `OVERRIDE_REASON_MIN`
 * characters). Opened from the release list (`ReleasePick.tsx`) and the Merge pick (`PipelineSections.tsx`); the
 * caller's `onOverride` sends the request with the reason (`override_reason`). The server records it on the Audit
 * page and refuses a short reason (422 `reason_short`) or a caller without the right (403 `override_forbidden`).
 * The same right and reason release untested (`untested`: `ReleasePick.tsx`, `untested_reason`): its own title and
 * words, the same dialog. */

/** What a refused override says: the field's own words, else the release refusal's. */
function overrideRefusal(err: unknown, fallback: string): string {
  const detail = (err as { detail?: { code?: string } } | null)?.detail
  if (detail?.code === 'reason_short') return `Reason needs at least ${OVERRIDE_REASON_MIN} characters.`
  if (detail?.code === 'override_forbidden') return 'Only someone who can edit the pipeline can override a freeze.'
  return refusal(err, fallback)
}

/** What a refused untested release says: the same rules, its own words. */
function untestedRefusal(err: unknown, fallback: string): string {
  const detail = (err as { detail?: { code?: string } } | null)?.detail
  if (detail?.code === 'override_forbidden') return 'Only someone who can edit the pipeline can release untested.'
  return overrideRefusal(err, fallback)
}

export default function OverrideFreezeDialog({ freezeName, untested = false, subject, submitLabel, onOverride, onClose, onDone }: {
  /** The freeze holding the step: "Weekend", or "Freeze now". Ignored with `untested`. */
  freezeName: string
  /** Release untested: ship the source's head without the full tests. */
  untested?: boolean
  /** What runs now: "Release to production", "Merge 2 tasks". */
  subject: string
  /** "Override and release", "Override and merge". */
  submitLabel: string
  /** Send the request with the reason. */
  onOverride: (reason: string) => Promise<unknown>
  onClose: () => void
  onDone: () => void
}): JSX.Element {
  const [reason, setReason] = useState('')
  const [tooShort, setTooShort] = useState(false)
  const { busy, formError, run } = useSubmit()
  const submit = () => {
    const text = reason.trim()
    if (text.length < OVERRIDE_REASON_MIN) {
      setTooShort(true)
      return
    }
    void run(untested ? 'Could not release' : 'Could not override', async () => {
      await onOverride(text)
      onDone()
    }, { mapError: untested ? untestedRefusal : overrideRefusal })
  }
  const title = untested ? 'Release untested'
    : freezeName === 'Freeze now' ? 'Override Freeze now' : `Override freeze “${freezeName}”`
  return (
    <FormDialog
      title={title}
      submitLabel={submitLabel}
      busyLabel={untested ? 'Releasing…' : 'Overriding…'}
      busy={busy}
      error={formError}
      onSubmit={submit}
      onClose={onClose}
      destructive
      testId="override-freeze-dialog"
      submitTestId="override-freeze-confirm"
    >
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        {untested ? `${subject} ships without the full tests. Recorded on the Audit page.` : `${subject} runs now. Recorded on the Audit page.`}
      </Typography>
      <FormTextField
        label="Reason"
        value={reason}
        onChange={value => {
          setReason(value)
          if (tooShort && value.trim().length >= OVERRIDE_REASON_MIN) setTooShort(false)
        }}
        multiline
        minRows={2}
        autoFocus
        required
        disabled={busy}
        error={tooShort ? `Reason needs at least ${OVERRIDE_REASON_MIN} characters.` : undefined}
        testId="override-freeze-reason"
      />
    </FormDialog>
  )
}
