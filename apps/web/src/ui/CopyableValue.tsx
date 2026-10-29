import type { JSX } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { CopyButton } from './CopyButton'

/* A value the operator must copy out, offered TWO ways at once: a Copy button (`ui/CopyButton`) and
 * `user-select: all` monospace text. `navigator.clipboard` is missing in insecure contexts and can be refused
 * by the browser, so a refusal keeps the verb — the selectable text is the answer. The 2s
 * "Copied" flash is the only feedback a clipboard write gives; it reverts on a timer so it is not a toggle.
 * Validates nothing: a navigable URL must pass `lib/safeHref` at the CALLER (see `ui/AuthorizeLink`).
 */
export function CopyableValue({
  value,
  copyLabel,
  copiedLabel = 'Copied',
  sx,
  compact = false,
  testId,
  copyTestId,
}: {
  /** The exact text put on the clipboard and shown as selectable monospace. */
  value: string
  /** Name the THING ("Copy link", "Copy code") — two of these can share a dialog. */
  copyLabel: string
  /** What the verb flashes to for 2s. */
  copiedLabel?: string
  /** Merged onto the value box; the SIZE is the caller's (a long URL vs a short code). */
  sx?: SxProps<Theme>
  /** A 24px copy icon (`CopyButton`'s `icon`) instead of the text verb: a form of several values, where the
   *  verbs would outweigh them. `copyLabel` stays its accessible name and title. */
  compact?: boolean
  /** The value box's `data-testid`; `copyTestId` is the button's (a dialog e2e reads both). */
  testId?: string
  copyTestId?: string
}): JSX.Element {
  return (
    /* `flexWrap` IS LOAD-BEARING: on a phone it keeps the button from being pushed off the dialog. */
    <Box sx={{ display: 'flex', alignItems: 'center', gap: compact ? 0.5 : 1, flexWrap: compact ? 'nowrap' : 'wrap' }}>
      <Box
        component="code"
        data-testid={testId}
        sx={[
          {
            minWidth: 0,
            px: 1,
            py: 0.5,
            fontFamily: 'monospace',
            border: 1,
            borderColor: 'divider',
            borderRadius: 1,
            bgcolor: 'action.hover',
            userSelect: 'all',
          },
          ...(Array.isArray(sx) ? sx : [sx]),
        ]}
      >
        {value}
      </Box>
      <CopyButton
        variant={compact ? 'icon' : 'text'}
        text={() => value}
        label={copyLabel}
        testId={copyTestId}
        copiedLabel={copiedLabel}
        failedLabel={copyLabel}
        flashMs={2000}
      />
    </Box>
  )
}

export default CopyableValue
