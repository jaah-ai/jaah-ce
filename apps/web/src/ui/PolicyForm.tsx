import type { ComponentProps, JSX, ReactNode } from 'react'
import Stack from '@mui/material/Stack'
import Banner from './Banner'
import CancelSaveBar from './CancelSaveBar'

/* The ONE in-place policy form (Retention, Data residency, Data regions, SIEM export, IP allowlist): a column of
 * fields, then the save's error banner and the `CancelSaveBar`, submitted by Enter or Save only while `canSave`
 * (the draft changed and may be written). Not a dialog: it sits in the page. 560px wide unless the section says
 * otherwise (`'none'`: its container's width). `testId` is the stem of the banner's (`-error`) and the bar's
 * (`-actions`, `-save`, `-cancel`) testids, so every section's reads alike. */
export default function PolicyForm({ testId, error, bar, canSave = true, onSave, spacing = 2, maxWidth = 560, children }: {
  testId: string
  /** `usePolicyEditor`'s `formError`, under the fields. */
  error?: string
  /** The bar, `plain`, under the banner: `usePolicyEditor`'s `saveBar` plus the section's own gates and controls
   *  (`children`). Absent: no bar (a locked policy, a list not yet edited). */
  bar?: Omit<ComponentProps<typeof CancelSaveBar>, 'testId' | 'saveTestId' | 'discardTestId' | 'plain'>
  canSave?: boolean
  onSave: () => void
  spacing?: number
  maxWidth?: number | 'none'
  children: ReactNode
}): JSX.Element {
  return (
    <Stack
      component="form"
      noValidate
      spacing={spacing}
      data-testid={testId}
      onSubmit={e => {
        e.preventDefault()
        if (canSave) onSave()
      }}
      sx={{ maxWidth, minWidth: 0 }}
    >
      {children}
      {error && <Banner severity="error" testId={`${testId}-error`}>{error}</Banner>}
      {bar && <CancelSaveBar {...bar} plain testId={`${testId}-actions`} saveTestId={`${testId}-save`} discardTestId={`${testId}-cancel`} />}
    </Stack>
  )
}
