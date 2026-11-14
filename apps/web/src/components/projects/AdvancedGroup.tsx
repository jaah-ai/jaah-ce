import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { FONT_WEIGHT, grid } from '../../theme'
import { ExplainMoreButton, HelpLabel } from '../../ui/HelpLabel'
import type { HelpProp } from '../../ui/explain'

/* ONE COMPACT GROUP of the project page's General › Advanced: a label on the left (above it on a phone) and its
 * controls on the right, with no section heading, rule or paragraph (the style guide's prose-on-hover: the label's
 * explanation is its hover and "?"). For a group whose controls do not name it themselves (Routing, Issue trackers,
 * Tech stack); a single labelled control stands on its own. */
export default function AdvancedGroup({ label, help, testId, children }: {
  label: string
  help?: HelpProp
  testId?: string
  children: ReactNode
}): JSX.Element {
  return (
    <Box
      role="group"
      aria-label={label}
      data-testid={testId}
      sx={{
        display: 'grid', gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: `${grid(21)} minmax(0, 1fr)` },
        columnGap: 2, rowGap: 0.5, alignItems: 'start', minWidth: 0,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, pt: { sm: 1 }, minWidth: 0 }}>
        <Typography variant="body2" component="div" sx={{ fontWeight: FONT_WEIGHT.semibold, whiteSpace: 'nowrap' }}>
          <HelpLabel label={label} help={help} />
        </Typography>
        <ExplainMoreButton label={label} help={help} />
      </Box>
      <Box sx={{ minWidth: 0 }}>{children}</Box>
    </Box>
  )
}
