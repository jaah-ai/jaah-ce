import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import FlowDiagram from './FlowDiagram'

/* Setup's Explanation step (AC-4.1): the flow diagram, what Jaah can't see, then how the operator keeps secrets
 * sealed — stated as what Jaah does and what to do (D9: breach-proof and audited; D13: every key holder is listed
 * and audited), and K1 (after a leak, re-enter rather than rotate). */

export const CANNOT_SEE = [
  'Jaah never holds your private key. It stores only the public key and copies wrapped for each node.',
  'NodeVault secrets are sealed in your browser and opened only on your nodes. A leaked Jaah database, backup, log or encryption key can’t reveal them.',
  'Every key delivery and every credential fetch is in the audit log.',
]

/** K1: a rotation chain-wraps the new key under the current one (E2), so a leaked key calls for re-entering. */
export const ROTATION_ADVICE = 'If your current key may have leaked, re-enter your secrets instead of rotating.'

export const SAFEGUARDS = [
  'A secret is opened only on your node, when a task fetches it, and goes to that task alone.',
  'Every node that holds the key is listed on this page with its fingerprint. Check each new node here and in the audit log.',
  'A secret entered with NodeVault ticked is sealed in your browser. Moving an existing one has Jaah’s server re-seal it once.',
  ROTATION_ADVICE,
]

function Points({ items, testId }: { items: string[]; testId: string }) {
  return (
    <Box component="ul" sx={{ m: 0, pl: 2.5, display: 'flex', flexDirection: 'column', gap: 0.5 }} data-testid={testId}>
      {items.map(text => (
        <Typography component="li" variant="body2" key={text}>{text}</Typography>
      ))}
    </Box>
  )
}

export default function ExplanationStep(): JSX.Element {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2.5 }} data-testid="nodevault-explanation">
      <FlowDiagram />
      <Box>
        <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>What Jaah can’t see</Typography>
        <Points items={CANNOT_SEE} testId="nodevault-cannot-see" />
      </Box>
      <Box>
        <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>Keeping secrets sealed</Typography>
        <Points items={SAFEGUARDS} testId="nodevault-safeguards" />
      </Box>
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Next you make the key and download a passphrase-protected backup. Jaah can’t recover the key without it.
      </Typography>
    </Box>
  )
}
