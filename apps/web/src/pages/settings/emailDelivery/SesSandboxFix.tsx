import { type JSX, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { useSubmit } from '../../../hooks/useSubmit'
import { ROW_ACTION_SX } from '../../../ui/listStyles'
import { EMAIL_SETTINGS_URL } from '../email/api'
import { getSesAccount, SES_ACCOUNT_QUERY, SES_ACCOUNT_URL, verifySesRecipient, type SesRecipientStatus } from './api'
import { sesReviewLabel } from './vocabulary'

/* THE WAYS OUT OF SES'S SANDBOX, under its refusal (`SesReason`): verify the recipient in SES (AWS mails it a link),
 * connect Gmail (`system_mail.fallback_send` carries what SES refuses), or ask AWS for production access (Settings ›
 * Integrations › SES account). Spans only: it renders inside a caption `<p>` and a Banner. Every route is the
 * operator's (`routes/email_delivery_log.py`). */

const ROW_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, mt: 0.5, minWidth: 0 } as const

function Verify({ address, named }: { address: string; named: boolean }): JSX.Element {
  const [status, setStatus] = useState<SesRecipientStatus | null>(null)
  const { busy, formError: error, run } = useSubmit()

  const verify = () => void run(`Could not verify ${address}.`, async () => setStatus((await verifySesRecipient(address)).status))

  if (status === 'verified') {
    return <Box component="span" data-testid="ses-verify-state" data-state="verified">Verified in SES — send again.</Box>
  }
  return (
    <>
      <Button
        type="button"
        size="small"
        variant="outlined"
        onClick={verify}
        disabled={busy}
        data-testid="ses-verify"
        aria-label={`Verify ${address} in SES`}
        sx={[...ROW_ACTION_SX, { textTransform: 'none', overflowWrap: 'anywhere', textAlign: 'left' }]}
      >
        {busy ? 'Verifying…' : named ? `Verify ${address}` : 'Verify in SES'}
      </Button>
      {status === 'pending' && (
        <Box component="span" role="status" data-testid="ses-verify-state" data-state="pending" sx={{ overflowWrap: 'anywhere' }}>
          AWS emailed {address} a verification link. Sends work once it’s opened.
        </Box>
      )}
      {(status === 'failed' || status === 'none') && (
        <Box component="span" role="status" data-testid="ses-verify-state" data-state={status} sx={{ color: 'error.main' }}>
          Verification failed. Verify again.
        </Box>
      )}
      {error && (
        <Box component="span" role="alert" data-testid="ses-verify-error" sx={{ color: 'error.main', overflowWrap: 'anywhere' }}>
          {error}
        </Box>
      )}
    </>
  )
}

/** The fix row for a sandbox refusal naming `addresses` (`vocabulary.sesSandboxRecipients`). */
export default function SesSandboxFix({ addresses }: { addresses: string[] }): JSX.Element {
  const account = useQuery({ queryKey: SES_ACCOUNT_QUERY, queryFn: getSesAccount, staleTime: 60_000 })
  const review = account.data?.review_status
  const reviewWords = review ? sesReviewLabel(review).toLowerCase() : null
  return (
    <Box component="span" data-testid="ses-sandbox-fix" sx={ROW_SX}>
      {addresses.map(address => <Verify key={address} address={address} named={addresses.length > 1} />)}
      {account.data?.gmail_fallback ? (
        <Box component="span" data-testid="ses-gmail-fallback">Next send goes out via Gmail.</Box>
      ) : (
        <Link component={RouterLink} to={EMAIL_SETTINGS_URL} data-testid="ses-connect-gmail">Connect Gmail</Link>
      )}
      <Link component={RouterLink} to={SES_ACCOUNT_URL} data-testid="ses-production-access">
        {reviewWords ? `Production access: ${reviewWords}` : 'Production access'}
      </Link>
    </Box>
  )
}
