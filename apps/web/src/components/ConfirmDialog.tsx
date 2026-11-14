import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import DialogContentText from '@mui/material/DialogContentText'
import FormControlLabel from '@mui/material/FormControlLabel'
import { useRequestId } from '../hooks/useRequestId'
import { type RunOptions, useSubmit } from '../hooks/useSubmit'
import { NO_REQUEST_ID } from '../lib/mintUuid'
import { FONT_SIZE, FONT_WEIGHT, RADIUS } from '../theme'
import { FormDialog } from '../ui/FormDialog'
import TypedNameConfirm, { typedNameMatches } from '../ui/TypedNameConfirm'

// Spec cockpit-tasks-robustness-hardening R3: a keyboard-accessible in-app confirm replacing
// `window.confirm` (unstyled, untestable, and suppressible by the browser, after which the guarded action
// fires or blocks without intent). Drawn on `ui/FormDialog.tsx`, the app's ONE modal frame.

export interface ConfirmOptions {
  /** Dialog heading (aria-labelledby target). */
  title: string
  /** Body copy — a string, or rich nodes (e.g. a repo/title callout for R2). */
  message: ReactNode
  /**
   * Optional identity banner above `message`, naming exactly what the confirm targets (e.g. "task #6 ·
   * issue #40 · Epic — “ti6”"). An unlabeled modal once launched the wrong draft epic;
   * callers acting on one row should always pass it. Caller-formatted (vocabulary in
   * `components/tasks/shared.tsx`).
   */
  identity?: ReactNode
  /** Confirm button label. Default 'Confirm'. */
  confirmLabel?: string
  /** Cancel button label. Default 'Cancel'. */
  cancelLabel?: string
  /** Destructive (delete) styling — red confirm button, visually distinct from a neutral confirm. */
  destructive?: boolean
  /** One opt-in/out below `message`, read by the caller through `onChange` (the confirm still resolves a boolean).
   *  The label carries the choice's consequence. Unchecked unless `defaultChecked`. */
  checkbox?: { label: ReactNode; defaultChecked?: boolean; onChange: (checked: boolean) => void }
  /** A typed gate below `message` (`ui/TypedNameConfirm`): the confirm stays disabled until `name` is typed —
   *  for an act a mis-aimed click must not satisfy over many rows at once (a bulk Delete types the count). */
  typed?: { name: string; action: string }
  /** The write the confirm stands for, run WHILE the dialog is open: a refusal keeps it open with the
   *  server's reason (`apiDetail`, else `failed`), so the reader sees why beside what they tried; the confirm resolves
   *  `true` only once `action` succeeds. Without it the dialog closes on press and the caller writes after.
   *  `mapError` words the refusal in place of `apiDetail` (`hooks/useSubmit`'s `RunOptions`).
   *  `action` gets ONE request id per open (`hooks/useRequestId`): a retry after a refusal replays the same write, so a
   *  money write moves money once; a browser that cannot mint one is refused (`NO_REQUEST_ID`) whatever the
   *  action, so every `run` confirm shares one rule. Actions that need no key ignore the argument. */
  run?: { action: (requestId: string) => Promise<unknown>; failed: string; busyLabel?: string; mapError?: RunOptions['mapError'] }
}

/** A destructive confirm's options: `confirmLabel` names the red button (`confirm(danger('Delete', { title, message }))`). */
export const danger = (confirmLabel: string, options: Omit<ConfirmOptions, 'confirmLabel' | 'destructive'>): ConfirmOptions =>
  ({ ...options, confirmLabel, destructive: true })

// The modal itself, on the shared `FormDialog` frame (focus trap, Escape/backdrop dismissal, focus
// restoration). Rendered only while a confirm is pending (see `useConfirm`). Keyboard contract:
//   • Enter  → confirm (the confirm button is auto-focused; the frame is a form, so Enter submits).
//   • Escape → cancel.
//   • Space on the focused checkbox → toggles it (Tab reaches it).
export function ConfirmDialog({
  options,
  onConfirm,
  onCancel,
}: {
  options: ConfirmOptions
  onConfirm: () => void
  onCancel: () => void
}) {
  const {
    title,
    message,
    identity,
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    destructive = false,
    checkbox,
    typed,
    run,
  } = options
  const messageId = useId()
  const [requestId] = useRequestId()
  const { busy, formError: error, run: runAction } = useSubmit()
  const [checked, setChecked] = useState(checkbox?.defaultChecked ?? false)
  const [typedValue, setTypedValue] = useState('')
  const typedOk = !typed || typedNameMatches(typedValue, typed.name)
  const noRequestId = !!run && requestId === undefined

  function submit() {
    if (!run) return onConfirm()
    if (!requestId) return
    // Success unmounts the dialog (`onConfirm`); the hook's own busy reset lands in that same commit.
    return runAction(run.failed, async () => { await run.action(requestId); onConfirm() }, { mapError: run.mapError })
  }

  return (
    <FormDialog
      title={title}
      submitLabel={confirmLabel}
      cancelLabel={cancelLabel}
      destructive={destructive}
      busy={busy}
      busyLabel={run?.busyLabel}
      error={error || (noRequestId ? NO_REQUEST_ID : '')}
      errorTestId="confirm-dialog-error"
      onSubmit={() => void submit()}
      onClose={onCancel}
      // With a typed gate the field takes focus instead: Enter must not confirm before the name is typed.
      submitAutoFocus={!typed}
      submitDisabledReason={typedOk ? (noRequestId ? NO_REQUEST_ID : undefined) : `Type ${typed.name} to confirm`}
      maxWidth="xs"
      testId="confirm-dialog"
      describedBy={messageId}
    >
      <Box id={messageId}>
        {identity && (
          <Box
            data-testid="confirm-dialog-identity"
            sx={{
              mb: 1,
              px: 1,
              py: 0.5,
              borderRadius: RADIUS.sm,
              bgcolor: 'action.hover',
              fontWeight: FONT_WEIGHT.semibold,
              fontSize: FONT_SIZE.md,
            }}
          >
            {identity}
          </Box>
        )}
        <DialogContentText component="div" sx={{ fontSize: FONT_SIZE.base }}>
          {message}
        </DialogContentText>
      </Box>
      {checkbox && (
        <FormControlLabel
          sx={{ mt: 1, alignItems: 'flex-start', '& .MuiFormControlLabel-label': { fontSize: FONT_SIZE.base, pt: 1 } }}
          control={
            <Checkbox
              checked={checked}
              onChange={e => { setChecked(e.target.checked); checkbox.onChange(e.target.checked) }}
              data-testid="confirm-dialog-checkbox"
            />
          }
          label={checkbox.label}
        />
      )}
      {typed && (
        <Box sx={{ mt: 1.5 }}>
          <TypedNameConfirm name={typed.name} value={typedValue} onChange={setTypedValue} action={typed.action} testId="confirm-dialog-typed" />
        </Box>
      )}
    </FormDialog>
  )
}

/** ONE PENDING ASK AS A PROMISE, for a dialog awaited where it is opened (`useConfirm`, `usePushIdentity`):
 *  `ask(input)` opens it and resolves with `settle`'s result — or `cancelled` when a second ask supersedes it or the
 *  host unmounts, so an awaiter never hangs. `pending.id` keys the dialog so a superseding ask remounts. The resolver
 *  lives in a ref, not state: resolving is a side effect and `setState` updaters must be pure (StrictMode
 *  double-invokes). One slot suffices (an asking action disables its trigger while in flight). */
export function usePromptSlot<T, R>(cancelled: R): {
  pending: { id: number; input: T } | null
  ask: (input: T) => Promise<R>
  settle: (result: R) => void
} {
  const [pending, setPending] = useState<{ id: number; input: T } | null>(null)
  const resolveRef = useRef<((result: R) => void) | null>(null)
  const idRef = useRef(0)

  const settle = useCallback((result: R) => {
    resolveRef.current?.(result)
    resolveRef.current = null
    setPending(null)
  }, [])

  const ask = useCallback(
    (input: T) =>
      new Promise<R>(resolve => {
        resolveRef.current?.(cancelled)
        resolveRef.current = resolve
        idRef.current += 1
        setPending({ id: idRef.current, input })
      }),
    [cancelled],
  )

  useEffect(
    () => () => {
      resolveRef.current?.(cancelled)
      resolveRef.current = null
    },
    [cancelled],
  )

  return { pending, ask, settle }
}

// Promise-based confirm without a provider: `const [confirm, confirmDialog] = useConfirm()`, render
// `{confirmDialog}` once, await `confirm({...})` — `true` on confirm, `false` on cancel/Escape/backdrop.
export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const { pending, ask, settle } = usePromptSlot<ConfirmOptions, boolean>(false)
  const dialog = pending ? (
    <ConfirmDialog
      key={pending.id}
      options={pending.input}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  ) : null
  return [ask, dialog]
}
