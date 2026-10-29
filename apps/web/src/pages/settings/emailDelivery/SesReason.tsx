import type { JSX } from 'react'
import Box from '@mui/material/Box'
import SesSandboxFix from './SesSandboxFix'
import { SES_SANDBOX_REASON, sesSandboxRecipients } from './vocabulary'

/** The Gmail fallback's detail on a refusal it delivered anyway (`durable/notify_outbox.py`): nothing left to fix. */
const SENT_BY_FALLBACK = 'Sent from '

/** A provider reason as an operator reads it: SES's sandbox refusal in words, its raw text kept as a details line
 *  under them and the ways out after it (`SesSandboxFix`) unless Gmail sent the mail anyway; any other reason as
 *  served. */
export default function SesReason({ detail }: { detail: string }): JSX.Element {
  const recipients = sesSandboxRecipients(detail)
  if (recipients.length === 0) return <>{detail}</>
  return (
    <>
      {SES_SANDBOX_REASON}
      <Box component="span" data-testid="ses-raw-reason" sx={{ display: 'block', color: 'text.secondary', fontSize: '0.75rem', overflowWrap: 'anywhere' }}>
        {detail}
      </Box>
      {!detail.startsWith(SENT_BY_FALLBACK) && <SesSandboxFix addresses={recipients} />}
    </>
  )
}
