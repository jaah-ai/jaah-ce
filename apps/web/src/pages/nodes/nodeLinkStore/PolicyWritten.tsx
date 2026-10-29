import type { JSX } from 'react'
import Typography from '@mui/material/Typography'
import { fmtTimestamp } from '../../../lib/relativeTime'
import { monoSx } from '../../../ui/listStyles'
import type { NodeLinkStore } from '../../../types'

/** The last policy Jaah wrote on a store: when, and the access points it admits. It stays after a Repair that
 *  failed past the write, so the policy change is never hidden behind the error. Drawn only once one is written
 *  (`policy_written_at`): a "Policy written —" fact before then read as a write that went missing. */
export default function PolicyWritten({ store, testId }: { store: NodeLinkStore; testId?: string }): JSX.Element | null {
  const ids = store.policy_access_point_ids ?? []
  if (!store.policy_written_at) return null
  return (
    <>
      <Typography variant="body2" data-testid={testId}>
        {fmtTimestamp(store.policy_written_at)} · {ids.length} access point{ids.length === 1 ? '' : 's'}
      </Typography>
      {ids.length > 0 && (
        <Typography variant="caption" component="div" sx={[monoSx, { color: 'text.secondary', overflowWrap: 'anywhere' }]}>
          {ids.join(', ')}
        </Typography>
      )}
    </>
  )
}
