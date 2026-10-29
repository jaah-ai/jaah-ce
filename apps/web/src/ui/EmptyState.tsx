import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { JSX, ReactNode } from 'react'
import { scopedTitle, useOrgScope } from '../lib/orgScope'

/* The console's ONE "nothing here" block. `data-testid="empty-state"` is unconditional and never
 * caller-supplied: e2e asserts a populated page shows NO empty state, and that negative assertion
 * passes forever if a page forgets the attribute. A title and at most one action — no explanatory
 * paragraph: the console is not a marketing site. */
export function EmptyState({
  title,
  action,
  scoped = false,
}: {
  title: string
  /** A single call-to-action (usually a Button) under the copy. */
  action?: ReactNode
  /** The list is one the operator's global org filter narrows (`lib/orgScope.ts`): while it is on, the title
   *  names the orgs — "No nodes in Acme" — so an empty page is never mistaken for an empty fleet. */
  scoped?: boolean
}): JSX.Element {
  const scope = useOrgScope()
  const shown = scoped ? scopedTitle(title, scope) : title
  return (
    <Box
      data-testid="empty-state"
      sx={{
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        gap: 1,
        textAlign: 'center',
        px: 2,
        py: 4,
        color: 'text.secondary',
      }}
    >
      <Typography variant="subtitle2" component="p" sx={{ color: 'text.primary' }}>
        {shown}
      </Typography>
      {action}
    </Box>
  )
}

export default EmptyState
