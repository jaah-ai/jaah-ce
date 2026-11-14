import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { FONT_SIZE, grid } from '../../theme'
import { CopyableValue } from '../../ui/CopyableValue'

/** A new webhook's URL, copyable, then the "Secret" label `SecretOnceDialog` shows its secret under. */
export function WebhookUrlField({ url, testId }: { url: string; testId: string }): JSX.Element {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Box component="span" sx={{ fontSize: FONT_SIZE.sm }}>URL</Box>
      <CopyableValue
        value={url}
        copyLabel="Copy URL"
        testId={testId}
        copyTestId={`${testId}-copy`}
        sx={{ flexGrow: 1, flexShrink: 1, flexBasis: grid(42.5), overflowWrap: 'anywhere' }}
      />
      <Box component="span" sx={{ fontSize: FONT_SIZE.sm, mt: 1 }}>Secret</Box>
    </Box>
  )
}
