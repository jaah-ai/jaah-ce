import Box from '@mui/material/Box'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import type { JSX, ReactNode } from 'react'

/* The console's ONE interstitial: a page that cannot show what was asked for and says why instead — no
 * access (`pages/NoAccess.tsx`), not on the plan (`lib/planSwitches.tsx`), held by another org
 * (`components/OtherOrgGate.tsx`). A heading, one sentence, then whatever the reader can do next.
 *
 * Centred horizontally only: `.app-main` is the scrollport, and a vertically centred block could sit below
 * the fold. A readable measure at 1440px, full width (minus `.app-main`'s own inset) at 375px. Long words
 * (an org name, an email) break rather than overflow. */
export function InterstitialPanel({
  title,
  body,
  testId,
  titleTestId,
  children,
}: {
  title: ReactNode
  /** The one sentence under the heading. */
  body?: ReactNode
  /** On the outer block: what the page's tests and e2e wait for. */
  testId: string
  titleTestId?: string
  children?: ReactNode
}): JSX.Element {
  return (
    <Box data-testid={testId} sx={{ display: 'flex', justifyContent: 'center', paddingTop: { xs: '1rem', sm: '4rem' } }}>
      <Paper
        variant="outlined"
        sx={{
          maxWidth: '32rem',
          width: '100%',
          padding: { xs: '1.25rem', sm: '2rem' },
          display: 'flex',
          flexDirection: 'column',
          gap: 1.5,
        }}
      >
        <Typography
          variant="h6"
          component="h1"
          data-testid={titleTestId}
          sx={{ fontWeight: 600, lineHeight: 1.25, overflowWrap: 'anywhere' }}
        >
          {title}
        </Typography>
        {body != null && (
          <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
            {body}
          </Typography>
        )}
        {children}
      </Paper>
    </Box>
  )
}

export default InterstitialPanel
