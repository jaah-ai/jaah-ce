import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import type { JSX } from 'react'
import type { GitReach } from '../pages/credentials/api'
import Banner from '../ui/Banner'

/* Which GitHub owners behind the viewer's projects block the connected token (`routes/git_connect.py::reach`).
 * GitHub Connect runs on a GitHub App (default) or an OAuth App, the operator's choice. An OAuth App token
 * reaches what its user reaches, unless an org's OAuth app access restrictions (needs an owner's approval) or
 * SAML SSO (needs the user's authorization) block it. A GitHub App token reaches only owners that installed the
 * App (`install_required` otherwise; `installed_count === 0`: none at all). Re-read every visit. Renders nothing
 * unless the server said `ok` and named a blocked owner or no install: `unknown` (no app, no connection,
 * outage…) stays silent — fail-safe, never a claim of lost access. */

/** Outbound: `_blank` keeps the unsaved form behind; `noopener` blocks `window.opener`. */
function OutLink({ href, testId, children }: { href: string; testId: string; children: string }): JSX.Element {
  return (
    <Link href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      {children}
    </Link>
  )
}

function GitReachNotice({ reach }: {
  /** `null` while in flight or on a transport failure — renders nothing, as `unknown` does. */
  reach: GitReach | null
}): JSX.Element | null {
  if (reach === null || reach.state !== 'ok') return null

  const { approve_url: approveUrl, install_url: installUrl, owners } = reach
  const approval = owners.filter(row => row.reach === 'approval_required').map(row => row.owner)
  const sso = owners.filter(row => row.reach === 'sso_required').map(row => row.owner)
  const install = owners.filter(row => row.reach === 'install_required').map(row => row.owner)
  const installedNowhere = reach.installed_count === 0 && install.length === 0
  if (approval.length === 0 && sso.length === 0 && install.length === 0 && !installedNowhere) return null
  const installLink = installUrl && <> <OutLink href={installUrl} testId="git-reach-install-link">Install or request approval</OutLink></>

  return (
    <Box data-testid="git-reach-notice" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {approval.length > 0 && (
        <Banner severity="warning" testId="git-reach-approval">
          {approval.join(', ')} {approval.length === 1 ? "hasn't approved Jaah, so its" : "haven't approved Jaah, so their"}
          {' '}private repositories are out of reach. Ask an owner to approve it, or paste a token.
          {approveUrl && <> <OutLink href={approveUrl} testId="git-reach-approval-link">Request approval</OutLink></>}
        </Banner>
      )}
      {sso.length > 0 && (
        <Banner severity="warning" testId="git-reach-sso">
          {sso.join(', ')} {sso.length === 1 ? 'uses' : 'use'} single sign-on. Authorize Jaah for {sso.length === 1 ? 'it' : 'them'}, or paste a token.
          {approveUrl && <> <OutLink href={approveUrl} testId="git-reach-sso-link">Authorize</OutLink></>}
        </Banner>
      )}
      {install.length > 0 && (
        <Banner severity="warning" testId="git-reach-install">
          {install.join(', ')} {install.length === 1 ? "hasn't installed the Jaah GitHub App, so its" : "haven't installed the Jaah GitHub App, so their"}
          {' '}repositories are out of reach.
          {installLink}
        </Banner>
      )}
      {installedNowhere && (
        <Banner severity="warning" testId="git-reach-install-none">
          The Jaah GitHub App isn't installed anywhere you can reach.
          {installLink}
        </Banner>
      )}
    </Box>
  )
}

export default GitReachNotice
