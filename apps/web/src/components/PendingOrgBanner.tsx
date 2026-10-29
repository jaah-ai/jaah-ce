import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { Banner } from '../ui/Banner'
import { PENDING_ORG_BANNER, useOrgPending } from '../lib/orgLifecycle'

/* The shell-wide notice while the viewer's org awaits approval: persistent (no dismiss — it lasts exactly as
   long as the state), mounted once in `App.tsx::AuthLayout` above every page. Absent otherwise. */
export function PendingOrgBanner(): JSX.Element | null {
  if (!useOrgPending()) return null
  return (
    <Box data-testid="pending-org-banner" sx={{ mb: 1.5, '& .MuiAlert-message': { whiteSpace: 'normal', overflowWrap: 'anywhere' } }}>
      <Banner severity="info">{PENDING_ORG_BANNER}</Banner>
    </Box>
  )
}

export default PendingOrgBanner
