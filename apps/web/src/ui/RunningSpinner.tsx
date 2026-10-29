import CircularProgress from '@mui/material/CircularProgress'
import type { JSX } from 'react'

/* The console's in-progress mark: a running task's pill (`components/tasks/shared.tsx`), a deploying node's disc
 * and pills (`lib/nodeStatus.ts::nodeDeploy`), a cloud machine starting or stopping.
 * Sized to the 20px pill and in its tone; motion stops under `prefers-reduced-motion`.
 * A fixed three-quarter arc (`value={72}`) that rotates, not MUI's indeterminate variant, whose arc shrinks
 * to nothing and renders an empty box at 11px. Also screenshot-stable.
 * `role="presentation"` overrides MUI's `progressbar`: a second progressbar on the row broke
 * assistive tech and would be read as "72%". */
export function RunningSpinner(): JSX.Element {
  return (
    <CircularProgress
      variant="determinate"
      value={72}
      size={11}
      thickness={6}
      color="inherit"
      role="presentation"
      aria-hidden="true"
      data-testid="badge-spinner"
      sx={{
        display: 'block',
        '@keyframes taskStatusSpin': { to: { transform: 'rotate(360deg)' } },
        animation: 'taskStatusSpin 1.1s linear infinite',
        // The value never changes, so the transition would only delay first paint.
        '& .MuiCircularProgress-circle': { transition: 'none' },
        '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
      }}
    />
  )
}
