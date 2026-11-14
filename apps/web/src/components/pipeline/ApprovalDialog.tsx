import { type JSX, useState } from 'react'
import Typography from '@mui/material/Typography'
import { FormDialog } from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'

/* APPROVE OR REJECT, with the approver's instructions and an optional comment — the tasks pipeline's bulk
 * Approve/Reject (`PipelineSections.tsx`) and a release's (`ReleasePick.tsx`). The instructions show whole, never
 * clipped: they are what the approver is asked to check. The caller sends the write; its refusal shows here. */

/** The longest comment the server keeps. */
export const COMMENT_MAX = 500

export type ApprovalMode = 'approve' | 'reject'

export default function ApprovalDialog({ mode, instructions, busy = false, error = '', onConfirm, onClose }: {
  mode: ApprovalMode
  /** What an approver is asked to check; "" draws nothing. */
  instructions: string
  busy?: boolean
  error?: string
  /** The comment as typed, trimmed ("" for none). */
  onConfirm: (comment: string) => void
  onClose: () => void
}): JSX.Element {
  const [comment, setComment] = useState('')
  const label = mode === 'approve' ? 'Approve' : 'Reject'
  return (
    <FormDialog
      title={label}
      submitLabel={label}
      busyLabel={mode === 'approve' ? 'Approving…' : 'Rejecting…'}
      busy={busy}
      error={error}
      destructive={mode === 'reject'}
      onSubmit={() => onConfirm(comment.trim())}
      onClose={onClose}
      testId="approval-dialog"
      submitTestId="approval-confirm"
    >
      {instructions && (
        <Typography variant="body2" data-testid="approval-instructions"
          sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {instructions}
        </Typography>
      )}
      <FormTextField
        label="Comment (optional)"
        value={comment}
        onChange={setComment}
        multiline
        minRows={2}
        disabled={busy}
        testId="approval-comment"
        slotProps={{ htmlInput: { maxLength: COMMENT_MAX } }}
      />
    </FormDialog>
  )
}
