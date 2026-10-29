import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import type { CloudConnectionRow } from '../../../types'

/* What a connection boots and how old its pin is, one home for the Connections table
 * (`CloudConnectionsSection`) and the Edit dialog's Image field (`ConnectionDialog`), so the two never disagree. */

const DAY_MS = 86_400_000

/** What a connection boots: `short` for a slot, `sentence` for a line, `pinAge` for prose beside the AMI field
 *  (`''` = no pin). `stale` comes from the server (`routes/cloud_nodes.py::AMI_PIN_MAX_AGE`), so the threshold
 *  has one home. */
export function connectionImage(row: CloudConnectionRow, now: number = Date.now()): {
  short: string
  sentence: string
  pinAge: string
  stale: boolean
} {
  const auto = `Newest ${row.auto_image || 'Ubuntu LTS'}`
  if (!row.ami_id) return { short: auto, sentence: `${auto} at each launch`, pinAge: '', stale: false }
  const pinned = row.ami_pinned_at ? Date.parse(row.ami_pinned_at) : NaN
  const days = Number.isNaN(pinned) ? null : Math.max(0, Math.floor((now - pinned) / DAY_MS))
  const stale = row.ami_pin_stale === true
  const age = days === null ? '?d' : `${days}d`
  const pinAge = days === null ? 'pinned at an unknown date' : `pinned ${days} day${days === 1 ? '' : 's'} ago`
  return {
    short: `${row.ami_id} · ${age}`,
    sentence: `${row.ami_id}, ${pinAge}${stale ? ' — re-pin due' : ''}`,
    pinAge,
    stale,
  }
}

/** The image text, warning-toned and marked once a pin is past its re-pin. */
export function ConnectionImageText({ stale, children, testId }: {
  stale: boolean
  children: ReactNode
  testId?: string
}): JSX.Element {
  return (
    <Box component="span" sx={stale ? { color: 'warning.main', fontWeight: 600 } : undefined} data-testid={testId}>
      {stale && <span role="img" aria-label="Re-pin due:">⚠ </span>}{children}
    </Box>
  )
}
