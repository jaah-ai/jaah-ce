import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Skeleton from '@mui/material/Skeleton'
import Typography from '@mui/material/Typography'
import type { JSX } from 'react'

/* The console's ONE "still fetching" block:
 *   • `rows` given  → a SKELETON of N data-row bars, so the table does not collapse and snap back (AC5).
 *   • `rows` omitted → a small inline spinner with an optional label.
 * `data-testid="loading-state"` is UNCONDITIONAL and never caller-supplied (as `Banner`/`EmptyState`): e2e
 * specs assert its absence on a settled page, worthless if a page could omit it.
 */
export function LoadingState({
  label,
  rows,
}: {
  /** The accessible/visible description. Defaults to "Loading…". */
  label?: string
  /** How many skeleton rows to draw. Omit for the inline spinner shape. */
  rows?: number
}): JSX.Element {
  const text = label ?? 'Loading…'

  if (rows !== undefined && rows > 0) {
    return (
      <Box data-testid="loading-state" role="status" aria-label={text} sx={{ px: 1, py: 0.5 }}>
        {Array.from({ length: rows }, (_, i) => (
          // 28px = the data-row height `theme.ts`'s density buys; taller would shrink the table on settle.
          <Skeleton key={i} variant="rounded" height={20} sx={{ my: 0.5 }} />
        ))}
      </Box>
    )
  }

  return (
    <Box
      data-testid="loading-state"
      role="status"
      sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 1, py: 0.5, color: 'text.secondary' }}
    >
      <CircularProgress size={14} thickness={5} />
      <Typography variant="body2">{text}</Typography>
    </Box>
  )
}

export default LoadingState
