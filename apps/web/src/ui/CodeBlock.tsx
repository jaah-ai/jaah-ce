import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import type { HTMLAttributes, JSX, ReactNode } from 'react'

/* The console's one monospace block — a payload, a log tail, a command, a rendered prompt. Every `<pre>` the
 * console shows is this, so they share one font, one wrap rule and one well.
 *
 * - `tone`: `well` (shaded, the default), `shell` (dark in both themes so it reads as a terminal), `plain`
 *   (no well and no padding — the block IS the document, as in an artifact viewer).
 * - `wrap`: on by default (`pre-wrap` + `overflowWrap: anywhere`, so a uuid or URL never forces a scroller);
 *   off for content whose columns matter (box-drawing art, a pane tail).
 * - `maxHeight`: caps the block in its own scrollport. Omit it where the page should be the one scroll surface.
 * - `bordered`: a divider outline around the well. */
export function CodeBlock({
  children,
  tone = 'well',
  size = 'small',
  wrap = true,
  bordered = false,
  maxHeight,
  testId,
  sx,
  ...rest
}: {
  children: ReactNode
  tone?: 'well' | 'shell' | 'plain'
  /** `small` 0.75rem for a payload inside a detail panel; `medium` 0.8125rem for the page's main content. */
  size?: 'small' | 'medium'
  wrap?: boolean
  bordered?: boolean
  maxHeight?: number | string
  testId?: string
  sx?: SxProps<Theme>
} & Omit<HTMLAttributes<HTMLElement>, 'children' | 'color'>): JSX.Element {
  return (
    <Box
      component="pre"
      {...rest}
      data-testid={testId}
      sx={[
        (theme: Theme) => ({
          m: 0,
          fontFamily: theme.typography.mono?.fontFamily ?? 'monospace',
          fontSize: size === 'small' ? '0.75rem' : '0.8125rem',
          lineHeight: 1.55,
          minWidth: 0,
          ...(wrap
            ? { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }
            : { whiteSpace: 'pre', overflowX: 'auto' }),
          ...(maxHeight !== undefined && { maxHeight, overflow: 'auto' }),
          ...(tone === 'well' && { p: 1, bgcolor: 'action.hover', borderRadius: 1 }),
          ...(tone === 'shell' && { p: 1.5, bgcolor: 'grey.900', color: 'grey.100', borderRadius: 1.5 }),
          ...(bordered && { border: 1, borderColor: 'divider' }),
        }),
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  )
}
