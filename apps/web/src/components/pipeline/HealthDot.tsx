/* A stage's health dot (PRD D10): its address's last check — Healthy (green), Down (red), Not checked yet (a grey
 * ring) — on the Tasks board's stage block and the stage card in Project settings › Pipeline. The word, where the
 * address comes from and how long ago it was checked are on hover and in the dot's accessible name, so colour is never
 * the only signal. A stage with no address draws no dot (`NO_ADDRESS` is its hover on the block). The check itself is
 * the control plane's (`apps/api/app/releases/health.py`). */
import type { JSX } from 'react'
import Box from '@mui/material/Box'
import type { StageHealth, TargetKind } from '../../lib/pipeline'
import { compactSpan } from '../../lib/relativeTime'
import StatusDot from '../../ui/StatusDot'
import Tip from '../../ui/Tip'

export const NO_ADDRESS = 'No address'
/** A page showing a dot re-reads this often: the probe's own cadence while someone looks. */
export const HEALTH_POLL_MS = 60_000

const WORD: Record<StageHealth['state'], string> = { healthy: 'Healthy', down: 'Down', unchecked: 'Not checked yet' }
const SOURCE: Partial<Record<TargetKind, string>> = {
  forge_env: 'from deploy environment', argocd: 'from Argo CD', webhook: 'from webhook', script: 'from script',
}
const DOT_PX = 10

/** The hover: "Healthy · from Argo CD · checked 40s ago"; Down and Not checked yet add their detail. */
export function healthWords(health: StageHealth, target?: TargetKind | null, now: number = Date.now()): string {
  const parts = [WORD[health.state]]
  // What a Down check saw ("HTTP 503"), or why an address is not checked (a private network).
  if (health.state !== 'healthy' && health.detail) parts.push(health.detail)
  const source = target ? SOURCE[target] : undefined
  if (source) parts.push(source)
  const checked = health.checked_at ? Date.parse(health.checked_at) : NaN
  if (!Number.isNaN(checked)) parts.push(`checked ${compactSpan(now - checked, { seconds: true })} ago`)
  return parts.join(' · ')
}

export default function HealthDot({ health, target }: { health: StageHealth; target?: TargetKind | null }): JSX.Element {
  const words = healthWords(health, target)
  return (
    <Tip title={words} describeChild={false}>
      <Box component="span" role="img" aria-label={words} data-testid="stage-health" data-state={health.state}
        sx={{ display: 'inline-flex', alignItems: 'center', flex: 'none' }}>
        <StatusDot size={DOT_PX} tone={health.state === 'healthy' ? 'success' : health.state === 'down' ? 'error' : 'neutral'}
          hollow={health.state === 'unchecked'} />
      </Box>
    </Tip>
  )
}
