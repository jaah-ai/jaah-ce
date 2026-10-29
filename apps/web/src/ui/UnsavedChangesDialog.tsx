import type { ReactNode } from 'react'
import Button from '@mui/material/Button'
import DialogContentText from '@mui/material/DialogContentText'
import { FormDialog } from './FormDialog'
import { tapTargetSx } from './listStyles'

/** `cancel` (stay in the editor) is also what Escape/backdrop resolve to — the only safe accidental answer. */
export type UnsavedChoice = 'save' | 'discard' | 'cancel'

/* THREE-way gate, so not `useConfirm` (`ui/ConfirmDialog.tsx`), whose contract is `Promise<boolean>` with
 * two buttons; `FormDialog`'s frame with Discard as its third button. Save writes then leaves; Discard
 * leaves and loses edits (styled destructive); Cancel stays. While saving, `busy` seals every exit:
 * `onUnsavedChoice` navigates on the save's success, so a second choice mid-flight would PUT twice.
 */
export default function UnsavedChangesDialog({
  open,
  saving,
  onChoose,
  message,
}: {
  open: boolean
  /** A save from this dialog is in flight — the row stays up, disabled, until it lands. */
  saving: boolean
  onChoose: (choice: UnsavedChoice) => void
  /** Overrides the body text; a caller whose subject is not a task cannot say "this task". */
  message?: ReactNode
}) {
  return (
    /* Ascending commitment left→right: stay → discard → save (filled: it keeps the work). */
    <FormDialog
      open={open}
      title="Unsaved changes"
      submitLabel="Save"
      busy={saving}
      onSubmit={() => onChoose('save')}
      onClose={() => onChoose('cancel')}
      maxWidth="xs"
      testId="unsaved-changes-dialog"
      submitTestId="unsaved-save"
      cancelTestId="unsaved-cancel"
      secondaryAction={(
        <Button
          type="button"
          onClick={() => onChoose('discard')}
          disabled={saving}
          color="error"
          sx={tapTargetSx}
          data-testid="unsaved-discard"
        >
          Discard
        </Button>
      )}
    >
      <DialogContentText>
        {message ?? 'This task has changes that have not been saved. Save them before going back to the list?'}
      </DialogContentText>
    </FormDialog>
  )
}
