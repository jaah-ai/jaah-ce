import { type JSX, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { useNodesRoster } from '../../../hooks/useNodesRoster'
import { countOf } from '../../../lib/humanize'
import Banner from '../../../ui/Banner'
import { SHARE_SKIPPED_STATE, type ShareSubscriptionsResult } from './api'

/* AFTER SHARE SUBSCRIPTIONS BETWEEN NODES — the nodes the share did not attach (`skipped`, each with the
 * server's reason), handed over in router state by `ShareSubscriptionsAction`. Read once at mount, so a tab
 * change keeps it; Dismiss clears the history entry's state too, so a reload does not bring it back. Names are the
 * server's, else the shared roster's; a node neither names shows its id. */

type Skipped = ShareSubscriptionsResult['skipped']

function skippedOf(state: unknown): Skipped {
  const raw = (state as Record<string, unknown> | null)?.[SHARE_SKIPPED_STATE]
  return Array.isArray(raw) ? (raw as Skipped) : []
}

export default function ShareSkippedNotice(): JSX.Element | null {
  const location = useLocation()
  const navigate = useNavigate()
  const [skipped, setSkipped] = useState<Skipped>(() => skippedOf(location.state))
  const roster = useNodesRoster(skipped.length > 0)
  if (skipped.length === 0) return null
  const nameOf = (id: string) => roster.data?.find(n => n.id === id)?.name ?? id
  const dismiss = () => {
    setSkipped([])
    navigate({ pathname: location.pathname, search: location.search, hash: location.hash }, { replace: true, state: null })
  }
  return (
    <Banner severity="info" onDismiss={dismiss} testId="nodelink-share-skipped">
      Not attached to {countOf(skipped.length, 'node')}:
      <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
        {skipped.map(s => (
          <li key={s.node_id} data-testid="nodelink-share-skipped-node">
            <Box component="span" sx={{ fontWeight: 600 }}>{s.name || nameOf(s.node_id)}</Box> — {s.reason}
          </li>
        ))}
      </Box>
    </Banner>
  )
}
