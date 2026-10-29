import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import { CLOUD_SIGNUP_URL } from '../lib/edition'
import { safeHref } from '../lib/safeHref'

/* THE COMMUNITY EDITION'S ONE UPSELL LINE: a short sentence, its link to Jaah Cloud, never a modal (PRD R20). The single
 * place a cloud link renders, so the complete list of where one shows is this component's callers:
 *   1. the account cap's refusal (`pages/ce/AccountCapLine.tsx`)
 *   2. the Tasks board while work queues over the agent cap (`pages/ce/QueuedOverCap.tsx`)
 *   3. Settings › This machine (`pages/ce/ThisMachineSection.tsx`)
 *   4. the user menu, where Users and invites were (`components/Sidebar.tsx`)
 *   5. a console path of a feature the edition leaves out (`pages/ce/NotInEdition.tsx`)
 * `children` is the sentence; `cta` the link's words, ending it. `href` comes from the API (`upgrade_url`): anything but
 * an http(s) URL (`lib/safeHref.ts`'s verdict) falls back to the static sign-up link, so a payload can never plant a
 * `javascript:` link. */

export default function CloudUpsell({ children, cta, href, testId, sx }: {
  children?: string
  cta: string
  href: string
  testId: string
  sx?: SxProps<Theme>
}) {
  return (
    <Typography variant="body2" data-testid={testId} sx={[{ color: 'text.secondary' }, ...(Array.isArray(sx) ? sx : [sx])]}>
      {children && `${children} `}
      <Link href={safeHref(href) ?? CLOUD_SIGNUP_URL} target="_blank" rel="noopener noreferrer" underline="hover" data-testid={`${testId}-link`}>
        {cta}
      </Link>
    </Typography>
  )
}
