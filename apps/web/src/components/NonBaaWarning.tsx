import type { JSX } from 'react'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { post } from '../api'
import { currentOrgId, useAuth } from '../auth'
import { useSubmit } from '../hooks/useSubmit'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import Banner from '../ui/Banner'

/* THE NON-BAA WARNING: in a HIPAA org, every surface whose vendor has no BAA on file says so at
 * the point of use. The warned set is the server's (`Admin.hipaa_warnings`, from `app/hipaa.py::SURFACES` minus
 * the operator's `hipaa_vendor_baas`), so recording a BAA removes a warning with no code change. Off, or a
 * surface not warned: nothing. Never disables the feature it sits on (N2).
 *
 * Unacknowledged: a warning banner, with Acknowledge for an org admin or the operator. Acknowledged: the same
 * sentence as a one-line caption, kept for good — a security warning is never removed (D-A6). */

/** A `hipaa.SURFACES` key: `slack`, `telegram`, `claude_subscription`, `help_chat`, `jira`, or an agent CLI's provider value
 *  (`pages/credentials/accountModel::nonBaaSurfaceFor`). A key the server does not warn about renders nothing. */
export type NonBaaSurface = string

export const NON_BAA_WARNING = 'Not covered by a BAA — don’t send PHI here.'

/** Acknowledge one org's warning on one surface (`routes/orgs.py::acknowledge_hipaa_warning`), then re-read the
 *  session and, given, the caller's own copy (`onSaved`). One home for the POST: the banner here and the
 *  operator's per-surface list (`pages/orgs/SsoTab`), which names any org. */
export function useAcknowledgeNonBaa(onSaved?: () => Promise<unknown>) {
  const { refresh } = useAuth()
  const { busy, formError, run } = useSubmit()
  async function acknowledge(orgId: string, surface: NonBaaSurface) {
    let saved = false
    await run('Could not acknowledge.', async () => {
      await post(`/api/orgs/${encodeURIComponent(orgId)}/hipaa/acknowledgements`, { surface })
      saved = true
    })
    // Outside `run`: a failed re-read must not report a saved acknowledgement as refused.
    if (saved) {
      await refresh().catch(() => {})
      await onSaved?.().catch(() => {})
    }
  }
  return { busy, formError, acknowledge }
}

export function NonBaaWarning({ surface }: { surface: NonBaaSurface | null }): JSX.Element | null {
  const { admin } = useAuth()
  const { busy, formError, acknowledge } = useAcknowledgeNonBaa()
  const warning = surface ? admin?.hipaa_warnings?.find(w => w.surface === surface) : undefined
  if (!admin || !surface || !warning) return null

  if (warning.acknowledged) {
    return (
      <Typography variant="caption" component="div" data-testid={`non-baa-caption-${surface}`}
        /* The darker amber on light paper: `warning.main` is under 4.5:1 there, and this line stays for good. */
        sx={theme => ({ color: 'warning.dark', ...theme.applyStyles('dark', { color: 'warning.main' }) })}>
        {NON_BAA_WARNING}
      </Typography>
    )
  }

  const orgId = currentOrgId(admin)
  const canAcknowledge = isOrgAdminSeat(admin) && orgId !== null

  return (
    <div data-testid={`non-baa-warning-${surface}`}>
      <Banner severity="warning">
        {NON_BAA_WARNING}
        {canAcknowledge && (
          <Button
            size="small"
            color="warning"
            disabled={busy}
            onClick={() => void acknowledge(orgId ?? '', surface)}
            data-testid="non-baa-acknowledge"
            sx={{ ml: 1, minHeight: 32, verticalAlign: 'baseline' }}
          >
            Acknowledge
          </Button>
        )}
        {formError && (
          <Typography variant="caption" component="div" sx={{ color: 'error.main' }}>
            {formError}
          </Typography>
        )}
      </Banner>
    </div>
  )
}

export default NonBaaWarning
