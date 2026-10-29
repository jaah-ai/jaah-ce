import CloudUpsell from '../../components/CloudUpsell'
import { useCeLimits } from '../../lib/communityEdition'

/* The Tasks board's line in the community edition while work waits on its agent cap: how many wait, and where more run
   at once (`components/CloudUpsell.tsx`, upsell 2). Nothing otherwise, and never in the cloud console (no limits
   read). */
export default function QueuedOverCap() {
  const { data } = useCeLimits()
  if (!data || data.agents.queued <= 0) return null
  const { queued, cap } = data.agents
  return (
    <CloudUpsell cta="Run more at once on Jaah Cloud" href={data.upgrade_url} testId="upsell-queued">
      {`${queued} queued: this machine runs ${cap} agents at once.`}
    </CloudUpsell>
  )
}
