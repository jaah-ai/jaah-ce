import type { JSX } from 'react'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { type NodeOs, nodeOsGuide } from '../lib/nodeOs'
import { usePublicSettings } from '../pages/authEntry'
import { HelpLabel } from '../ui/HelpLabel'

/* The one line an operator reads before installing a node of `os`: minimum version, prerequisites, where
 * to run it (`lib/nodeOs.ts` holds the words). Rendered in the create dialog before Register and in the
 * onboarding dialog beside the command. Nothing for Ubuntu, whose flow is unchanged. A requirement the machine must
 * meet (`guide.warning`) follows as its own warning line: a hover says why, and the steps link to the reader docs on
 * the operator's docs base (no base, no link). */
export function NodeOsRequirements({ os, testId }: { os: NodeOs; testId: string }): JSX.Element | null {
  const guide = nodeOsGuide(os)
  const { docsUrl } = usePublicSettings().signupLinks
  if (!guide) return null
  const warning = guide.warning
  return (
    <>
    <Typography variant="body2" data-testid={testId} data-os={os}>
      {guide.before}
      {guide.tailscaleLink ? (
        <Link
          href={guide.tailscaleLink.href}
          target="_blank"
          rel="noopener noreferrer"
          title={guide.tailscaleLink.title}
        >
          {guide.tailscale}
        </Link>
      ) : guide.tailscale}
      {guide.after}
    </Typography>
    {warning && (
      <Typography variant="body2" sx={{ color: 'warning.main' }} data-testid={`${testId}-warning`}>
        <HelpLabel label={warning.text} help={warning.help} />
        {docsUrl && (
          <>
            {'. '}
            <Link href={`${docsUrl.replace(/\/+$/, '')}${warning.docsPath}`} target="_blank" rel="noopener noreferrer">
              How to turn it on
            </Link>
          </>
        )}
      </Typography>
    )}
    </>
  )
}

export default NodeOsRequirements
