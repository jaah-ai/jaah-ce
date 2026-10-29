import CloudUpsell from '../../components/CloudUpsell'
import type { CapRefusal } from '../../lib/communityEdition'

/* The community edition's account cap, refused (`pages/credentials/AccountEditPage.tsx`): the API's one line and its
   link, inline, never a modal (`components/CloudUpsell.tsx`, upsell 1). */
export default function AccountCapLine({ refusal }: { refusal: CapRefusal }) {
  return (
    <CloudUpsell cta="Add more on Jaah Cloud" href={refusal.upgrade_url} testId="upsell-account-cap">
      {refusal.detail}
    </CloudUpsell>
  )
}
