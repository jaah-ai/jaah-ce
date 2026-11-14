import type { JSX, MouseEvent, ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Button, { type ButtonProps } from '@mui/material/Button'
import { DisabledReason } from './DisabledReason'
import FormActionBar from './FormActionBar'
import SavedNote from './SavedNote'
import SubmitButton from './SubmitButton'
import { tapTargetSx } from './listStyles'

/* A record form's Cancel + Save bar in `FormActionBar`: the optional "Saved." note, Cancel (Back when read-only,
 * which also drops Save) linking to `cancelTo`, and Save — Create on a new record — reading Saving…/Creating…
 * while `busy`. `saveProps` pass through to the `SubmitButton` (a click handler, a `data-*` mark).
 *
 * THE DISCARD MODE, for a form that edits in place (the style guide's one Save bar, disabled until something
 * changed): `onDiscard` draws Discard instead of Cancel, and `dirty` gates both buttons — Discard stands down
 * while clean or `busy`, Save while clean, `busy` or `disabled`. `plain` is the same pair off the sticky bar: a
 * row under a tab's fields, or a cluster a host places itself — the in-place policy sections (`ui/PolicyForm`,
 * `hooks/usePolicyEditor`'s `saveBar`). `children` are a section's own controls beside the pair (a test send, the
 * operator's Clear list); `showButtons={false}` keeps only the note and them: a form that draws the pair once
 * edited, so a disabled full-width pair does not cost a phone two rows. */
export default function CancelSaveBar({
  testId,
  saveTestId,
  discardTestId,
  cancelTo,
  onCancel,
  onDiscard,
  dirty,
  creating = false,
  busy,
  disabled,
  saved,
  savedDetail,
  saveLabel,
  busyLabel,
  saveReason,
  readOnly = false,
  plain,
  note,
  size,
  saveProps,
  showButtons = true,
  children,
}: {
  testId: string
  saveTestId: string
  discardTestId?: string
  /** Where Cancel links; unused once `onDiscard` is set. */
  cancelTo?: string
  /** Runs on Cancel in place of following `cancelTo` (still its href): a way back the page drives itself. */
  onCancel?: () => void
  /** Resets the draft in place: Discard replaces the Cancel link. */
  onDiscard?: () => void
  /** Something changed; omit on a form that is always saveable. */
  dirty?: boolean
  creating?: boolean
  busy: boolean
  disabled?: boolean
  /** Show "Saved." — omit on a page that does not use `useSubmit`'s `saved`. */
  saved?: boolean
  /** `SavedNote`'s detail: what the save changed that the user did not type. */
  savedDetail?: string
  /** Save's label when it is not the one word (a count: `Save (2)`). */
  saveLabel?: ReactNode
  /** Save's label while `busy`, when it is not Saving…/Creating… (`Changing…`). */
  busyLabel?: ReactNode
  /** Why Save is disabled, on hover and tap (`DisabledReason`); the wrapper mounts once the prop is given. */
  saveReason?: string
  readOnly?: boolean
  /** `FormActionBar`'s `plain` and `note`. */
  plain?: boolean
  note?: ReactNode
  /** Both buttons' size: `small` on a toolbar row. */
  size?: ButtonProps['size']
  saveProps?: Omit<ButtonProps, 'type'> & { [data: `data-${string}`]: string }
  showButtons?: boolean
  children?: ReactNode
}): JSX.Element {
  const save = (
    <SubmitButton {...saveProps} size={size} testId={saveTestId} busy={busy} busyLabel={busyLabel ?? (creating ? 'Creating…' : 'Saving…')}
      disabled={disabled || dirty === false}>
      {saveLabel ?? (creating ? 'Create' : 'Save')}
    </SubmitButton>
  )
  return (
    <FormActionBar testId={testId} stretchTestIds={[saveTestId]} plain={plain} note={note}>
      {saved !== undefined && <SavedNote show={saved} detail={savedDetail} sx={{ alignSelf: 'center', mr: 'auto' }} />}
      {children}
      {!showButtons ? null : onDiscard ? (
        <Button type="button" variant="text" size={size} onClick={onDiscard} disabled={!dirty || busy} sx={tapTargetSx}
          data-testid={discardTestId}>
          Discard
        </Button>
      ) : cancelTo && (
        <Button component={RouterLink} to={cancelTo} variant="text" size={size} sx={tapTargetSx}
          onClick={onCancel && ((event: MouseEvent) => { event.preventDefault(); onCancel() })}>
          {readOnly ? 'Back' : 'Cancel'}
        </Button>
      )}
      {showButtons && !readOnly && (saveReason === undefined ? save : <DisabledReason reason={saveReason}>{save}</DisabledReason>)}
    </FormActionBar>
  )
}
