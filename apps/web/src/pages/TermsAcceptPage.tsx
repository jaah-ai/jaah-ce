import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { ApiError, get, post } from '../api'
import { useAuth } from '../auth'
import { useApiRead } from '../hooks/useApiRead'
import { useSubmit } from '../hooks/useSubmit'
import Banner from '../ui/Banner'
import { EntryCard, EntryFooter, EntryHeading, EntryLoading, EntrySubmit, footerActionSx } from './authEntry'
import { legalLink } from '../ui/legalLink'
import { refusalCode } from '../lib/refusalCode'

/* `/terms/accept` — the Terms gate (the API's legal-acceptance route): an owner whose org an operator
 * submitted for them accepts the Terms of Service and the Privacy Policy before any console page, as the second
 * factor's enrolment gates a first sign-in. Outside the shell. Declining is signing out: the session stays gated.
 * Accepting reloads the console, which opens where the visitor was headed (`lib/loginReturn.ts`). A member with
 * nothing pending (`terms_pending` false on `/api/auth/me`) is sent home: there is nothing here to accept. */

interface Terms {
  legal_version: string
  terms_url: string
  privacy_url: string
}

export const TERMS_COPY = 'Review and accept them to start using Jaah.'
export const TERMS_CHANGED_COPY = 'The terms just changed. Review them, then accept again.'

const isUnauthorized = (err: unknown) => err instanceof ApiError && err.status === 401

export default function TermsAcceptPage() {
  const { admin, loading, logout } = useAuth()
  const [changed, setChanged] = useState(false)
  const { busy, formError, run } = useSubmit()
  const { data: terms, error: loadError, refresh } = useApiRead(
    ['auth-terms'],
    () => get<Terms>('/api/auth/terms').catch((err: unknown) => {
      if (!isUnauthorized(err)) throw err
      window.location.replace('/login')
      // Never settles: the spinner holds until the sign-in page loads, with no error flashed first.
      return new Promise<Terms>(() => {})
    }),
    'Could not load the terms. Reload the page.',
  )

  async function accept() {
    if (!terms) return
    await run('Could not record your acceptance. Try again.', async () => {
      try {
        await post('/api/auth/terms/accept', { legal_version: terms.legal_version })
      } catch (err) {
        if (refusalCode(err) !== 'legal_version_changed') throw err
        setChanged(true)
        await refresh()
        return
      }
      // A full load: the console reads its identity afresh, now without the gate.
      window.location.replace('/')
    })
  }

  async function signOut() {
    await logout()
    window.location.replace('/login')
  }

  if (!loading && admin && !admin.terms_pending) return <Navigate to="/" replace />

  // One fixed sentence, never the server's detail: nothing a signed-in person can act on lives in it.
  if (!terms) return <EntryLoading label="Loading the terms" error={loadError ? 'Could not load the terms. Reload the page.' : undefined} />

  return (
    <EntryCard>
      <EntryHeading title="Accept the terms">{TERMS_COPY}</EntryHeading>
      {changed && <Banner severity="warning">{TERMS_CHANGED_COPY}</Banner>}
      {formError && <Banner severity="error">{formError}</Banner>}
      <Typography variant="body2" sx={{ textAlign: 'center' }} data-testid="terms-links">
        {legalLink(terms.terms_url, 'Terms of Service')} and {legalLink(terms.privacy_url, 'Privacy Policy')}
      </Typography>
      <EntrySubmit onClick={() => void accept()} disabled={busy} busy={busy} data-testid="terms-accept">
        Accept
      </EntrySubmit>
      <EntryFooter lead="Not now?">
        <Button variant="text" onClick={() => void signOut()} sx={footerActionSx} data-testid="terms-sign-out">
          Sign out
        </Button>
      </EntryFooter>
    </EntryCard>
  )
}
