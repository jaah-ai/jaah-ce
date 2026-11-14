import { useRef, type JSX } from 'react'
import Box from '@mui/material/Box'
import ExternalLink from './ExternalLink'
import Popover from '@mui/material/Popover'
import Typography from '@mui/material/Typography'
import { usePublicSettings } from '../pages/authEntry'
import type { ChoiceHelp, Explain } from './explain'
import { FONT_WEIGHT, grid } from '../theme'

/* The pop-up behind an explanation's "More" (`ui/HelpLabel`): the setting's name, its short paragraph, one line
 * per choice and a link to its reader-docs page. MUI's Popover keeps its focus trap and focus return; focus lands
 * on the heading so a screen reader starts at the name. The docs link uses the deployment's docs base
 * (`usePublicSettings`, as `components/NodeOsRequirements.tsx`), and is absent when that base is unset. Mounted
 * only once opened, so a hover never reads the public settings. */
export function ExplainPopover({
  open,
  anchorEl,
  onClose,
  label,
  explain,
  choices,
}: {
  open: boolean
  anchorEl: HTMLElement | null
  onClose: () => void
  label: string
  explain: Explain
  choices?: readonly ChoiceHelp[]
}): JSX.Element {
  const heading = useRef<HTMLHeadingElement>(null)
  const { docsUrl } = usePublicSettings().signupLinks
  const docsHref = docsUrl && explain.docs ? `${docsUrl.replace(/\/+$/, '')}/${explain.docs}` : null
  return (
    <Popover
      open={open && Boolean(anchorEl)}
      anchorEl={anchorEl}
      onClose={onClose}
      anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
      transformOrigin={{ vertical: 'top', horizontal: 'left' }}
      slotProps={{
        transition: { onEntered: () => heading.current?.focus() },
        paper: {
          'data-testid': 'explain-popover',
          sx: { maxWidth: `min(${grid(74.5)}, calc(100vw - ${grid(5.5)}))`, p: 2, overflowWrap: 'anywhere' },
        } as object,
      }}
    >
      <Typography ref={heading} component="h2" variant="subtitle2" tabIndex={-1} sx={{ fontWeight: FONT_WEIGHT.semibold, outline: 'none' }}>
        {label}
      </Typography>
      {explain.more && <Typography variant="body2" sx={{ mt: 0.5 }}>{explain.more}</Typography>}
      {choices && choices.length > 0 && (
        <Box
          component="dl"
          data-role="explain-choices"
          sx={{ display: 'grid', gridTemplateColumns: 'minmax(0, max-content) minmax(0, 1fr)', columnGap: 1.5, rowGap: 0.5, m: 0, mt: 1 }}
        >
          {choices.map(c => (
            <Box key={c.label} sx={{ display: 'contents' }}>
              <Typography component="dt" variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium }}>{c.label}</Typography>
              <Typography component="dd" variant="body2" sx={{ m: 0, color: 'text.secondary' }}>{c.help}</Typography>
            </Box>
          ))}
        </Box>
      )}
      {docsHref && (
        <ExternalLink
          href={docsHref}
          data-role="explain-docs"
          variant="body2"
          sx={{ display: 'inline-block', mt: 1 }}
        >
          Docs
        </ExternalLink>
      )}
    </Popover>
  )
}

export default ExplainPopover
