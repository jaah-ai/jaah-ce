import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import CloudUpsell from '../../components/CloudUpsell'
import { useCeLimits } from '../../lib/communityEdition'
import { absoluteTime, relativeAge } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import LoadingState from '../../ui/LoadingState'
import StatusDot from '../../ui/StatusDot'

/* SETTINGS › THIS MACHINE — the community edition's only node UI: is the node on this machine up, when it last reported,
 * and how many agents run against the cap (`GET /api/ce/limits`). Then the one upsell line of this page
 * (`components/CloudUpsell.tsx`, upsell 3). */
export default function ThisMachineSection() {
  const { data, error, isPending } = useCeLimits()
  if (isPending) return <LoadingState rows={2} />
  if (error || !data) return <Banner severity="error">{apiDetail(error, 'Could not read this machine')}</Banner>
  const { node, agents } = data
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }} data-testid="this-machine">
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <StatusDot tone={node.online ? 'success' : 'error'} />
        <Typography variant="body2" data-testid="this-machine-status">
          {node.online ? 'Online' : 'Offline'}
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }} title={absoluteTime(node.last_seen)} data-testid="this-machine-last-seen">
          · last seen {relativeAge(node.last_seen)}
        </Typography>
      </Box>
      <Typography variant="body2" data-testid="this-machine-agents">
        {agents.running} of {agents.cap} agents running{agents.queued > 0 && `, ${agents.queued} queued`}
      </Typography>
      <CloudUpsell cta="Run agents on more machines with Jaah Cloud" href={data.upgrade_url} testId="upsell-machines" />
    </Box>
  )
}
