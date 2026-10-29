import { type FormEvent, type JSX, type KeyboardEvent, type ReactNode, useId, useRef } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Dialog, { type DialogProps } from '@mui/material/Dialog'
import DialogActions from '@mui/material/DialogActions'
import DialogContent from '@mui/material/DialogContent'
import DialogTitle from '@mui/material/DialogTitle'
import type { SxProps, Theme } from '@mui/material/styles'
import useMediaQuery from '@mui/material/useMediaQuery'
import Banner from './Banner'
import { DisabledReason } from './DisabledReason'
import { PHONE_QUERY, tapTargetSx } from './listStyles'

/* The shared form-dialog frame: title, form-level error, Cancel/Submit.
 *
 * A DIALOG, not a separate edit route: this is a panel inside a Settings subtab, and navigating away would
 * remount `Settings` and lose its subtab. `component="form"` so Enter submits and native required-field
 * checks work. The form-level `Banner` carries only what `api.ts::fieldForDetail` could not attribute to a
 * field; field messages go in `FormField`'s `error` slot.
 */
export function FormDialog({
  title,
  submitLabel,
  busyLabel = 'Saving…',
  busy = false,
  error = '',
  onSubmit,
  onClose,
  children,
  submitDisabledReason,
  destructive = false,
  cancelLabel = 'Cancel',
  maxWidth = 'sm',
  testId = 'form-dialog',
  submitTestId,
  cancelTestId,
  errorTestId,
  submitDisabled = false,
  hideCancel = false,
  hideSubmit = false,
  open = true,
  modal = false,
  fullScreenOnPhone = false,
  header,
  secondaryAction,
  submitAutoFocus = false,
  paperSx,
  backdropSx,
  contentSx,
  describedBy,
}: {
  title: ReactNode
  submitLabel: string
  /**
   * The in-flight word, default `Saving…`; a different act passes its own.
   */
  busyLabel?: string
  busy?: boolean
  /** The refusal no field claimed, rendered whole. */
  error?: string
  onSubmit: () => void
  onClose: () => void
  children: ReactNode
  /** When set, the submit is disabled and this is its hover reason (e.g. a pending org). */
  submitDisabledReason?: string
  /** The submit is an irreversible act: red. */
  destructive?: boolean
  cancelLabel?: string
  maxWidth?: DialogProps['maxWidth']
  /** The paper's `data-testid`. */
  testId?: string
  submitTestId?: string
  cancelTestId?: string
  errorTestId?: string
  /** Disabled with no reason to show (an incomplete form). */
  submitDisabled?: boolean
  /** An acknowledgement, not a choice: the submit (e.g. Close) is the only button. */
  hideCancel?: boolean
  /** Nothing to submit yet (loading, a choice made in the body): Cancel is the only button. */
  hideSubmit?: boolean
  open?: boolean
  /** A TRUE modal: a backdrop click or Escape does nothing, Cancel is the way out. For a form holding
   *  typed secrets (the cloud-connection dialog), where a stray click would drop them.
   *  `'backdrop'`: only the backdrop is ignored, Escape still closes (a half-written editor). */
  modal?: boolean | 'backdrop'
  /** A viewer or wide editor: the paper fills a phone screen. */
  fullScreenOnPhone?: boolean
  /** Fixed between the title and the scrolling content (a filter row, tabs). */
  header?: ReactNode
  /** A third button between Cancel and the submit. */
  secondaryAction?: ReactNode
  submitAutoFocus?: boolean
  paperSx?: SxProps<Theme>
  /** E.g. a lighter scrim, so the page behind stays readable. */
  backdropSx?: SxProps<Theme>
  /** The `id` of the body text a screen reader announces with the title (a message dialog, not a form). */
  describedBy?: string
  /** Merged over the content's column layout (e.g. `p: 0` for an edge-to-edge viewer). */
  contentSx?: Exclude<SxProps<Theme>, readonly unknown[]>
}): JSX.Element {
  // MUI's `Dialog` does not take its name from `DialogTitle`; wired here, or a
  // screen reader drops which group the tag is added to.
  const titleId = useId()
  const submitRef = useRef<HTMLButtonElement | null>(null)
  const phone = useMediaQuery(PHONE_QUERY)

  function handleSubmit(e: FormEvent) {
    e.preventDefault()
    // A portal's submit still bubbles through the REACT tree: unstopped, a dialog opened inside another
    // form (a confirm inside the project or task form) would submit that form too.
    e.stopPropagation()
    if (busy || submitDisabled || submitDisabledReason !== undefined) return
    onSubmit()
  }

  return (
    <Dialog
      open={open}
      onClose={busy ? undefined : (_e, reason) => {
        if (modal && (reason === 'backdropClick' || (modal === true && reason === 'escapeKeyDown'))) return
        onClose()
      }}
      fullScreen={fullScreenOnPhone && phone}
      fullWidth
      maxWidth={maxWidth}
      aria-labelledby={titleId}
      aria-describedby={describedBy}
      slotProps={{
        paper: { 'data-testid': testId, sx: paperSx } as object,
        backdrop: { sx: backdropSx },
        /* `autoFocus` alone can fire mid-transition and be lost, leaving focus on the paper (Enter then confirms
           nothing): once entered, the submit takes it unless the operator has already moved into the form. */
        transition: submitAutoFocus ? {
          onEntered: () => {
            const button = submitRef.current
            const active = document.activeElement
            if (button && !(active && active !== button && button.form?.contains(active))) button.focus()
          },
        } : undefined,
      }}
    >
      {/* The form carries the paper's flex column: a plain `<form>` between paper and `DialogContent` broke
          the shrink chain, so a tall form overflowed a clipped paper (measured: Save 60px below the
          viewport on the cloud-node-defaults form). `minHeight: 0` on both lets the content scroll. */}
      <Box
        component="form"
        onSubmit={handleSubmit}
        // Enter on a radio or checkbox picks, never submits: a browser's implicit submission would fire a
        // destructive submit while the operator is still choosing (`PreemptDialog`, `CampaignDialog`).
        onKeyDown={(e: KeyboardEvent<HTMLFormElement>) => {
          const t = e.target as HTMLInputElement
          if (e.key === 'Enter' && (t.type === 'radio' || t.type === 'checkbox')) e.preventDefault()
        }}
        noValidate
        sx={{ display: 'flex', flexDirection: 'column', minHeight: 0, flex: '1 1 auto', overflow: 'hidden' }}
      >
        <DialogTitle id={titleId} sx={{ px: 2, py: 1.5, fontSize: '1rem', fontWeight: 600, overflowWrap: 'anywhere' }}>{title}</DialogTitle>
        {header}
        {/* `dividers`: the hairlines signal that the content scrolls. */}
        <DialogContent dividers sx={[{ px: 2, pb: 1, display: 'flex', flexDirection: 'column', gap: 2, minHeight: 0, flex: '1 1 auto', overflowY: 'auto' }, contentSx ?? false]}>
          {error && <Banner severity="error" testId={errorTestId}>{error}</Banner>}
          {children}
        </DialogContent>
        <DialogActions sx={{ px: 2, pb: 1.5, flexWrap: 'wrap', rowGap: 0.5 }}>
          {!hideCancel && (
            <Button type="button" onClick={onClose} color="inherit" disabled={busy} sx={tapTargetSx} data-testid={cancelTestId}>
              {cancelLabel}
            </Button>
          )}
          {secondaryAction}
          {!hideSubmit && <DisabledReason reason={submitDisabledReason}>
            <Button
              type="submit"
              variant="contained"
              color={destructive ? 'error' : 'primary'}
              disabled={busy || submitDisabled || submitDisabledReason !== undefined}
              title={submitDisabledReason}
              data-testid={submitTestId}
              autoFocus={submitAutoFocus}
              ref={submitRef}
              sx={tapTargetSx}
            >
              {busy ? busyLabel : submitLabel}
            </Button>
          </DisabledReason>}
        </DialogActions>
      </Box>
    </Dialog>
  )
}

export default FormDialog
