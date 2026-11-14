import type { RoutingMaxMode, RoutingMode } from '../types'

/* INTELLIGENT ROUTING'S MODES as the console names them — the twin of `app/agent_router/modes.py`'s ranking. The org's
 * maximum (`off`/`recommend`/`auto`) and a project's mode (`inherit`/`recommend`/`auto`, `inherit` = off) share one
 * ladder, lowest first. */

const RANK: Record<RoutingMaxMode | RoutingMode, number> = { off: 0, inherit: 0, recommend: 1, auto: 2 }

export const routingRank = (mode: RoutingMaxMode | RoutingMode): number => RANK[mode]

export const ROUTING_MAX_OPTIONS: readonly { value: RoutingMaxMode; label: string }[] = [
  { value: 'off', label: 'Off' },
  { value: 'recommend', label: 'Recommend' },
  { value: 'auto', label: 'Auto' },
]

export const ROUTING_MODE_OPTIONS: readonly { value: RoutingMode; label: string }[] = [
  { value: 'inherit', label: 'Off' },
  { value: 'recommend', label: 'Recommend' },
  { value: 'auto', label: 'Auto' },
]
