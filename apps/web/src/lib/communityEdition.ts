/* THE LOCAL EDITION'S OWN API: its first run, its caps and its one machine. The control plane serves these only in
 * the community edition (`lib/edition.ts`); the console reads them only there. Contracts (the architecture's §2.8):
 *   GET  /api/ce/setup  → `{ needs_setup }`; POST `{ email, password, timezone }` → 201 and a session cookie, 409 once a
 *                            user exists
 *   GET  /api/ce/limits → `CeLimits`
 *   a write over a cap     → 409 `CapRefusal`
 * Every cloud link these carry (`upgrade_url`) renders through `components/CloudUpsell.tsx` alone. */
import { useQuery } from '@tanstack/react-query'
import { ApiError, get, post } from '../api'
import { IS_CE } from './edition'

export type CeSetup = { needs_setup: boolean }

export type CeLimits = {
  /** `refusal`: what one more account meets, while the cap is full — the add form says it up front. */
  accounts: { used: number; cap: number; refusal: CapRefusal | null }
  agents: { running: number; cap: number; queued: number }
  node: { online: boolean; last_seen: string | null }
  upgrade_url: string
}

/** The body of a write the caps refuse (409): one line and its link, never a modal. */
export type CapRefusal = { detail: string; kind: string; limit: number; upgrade_url: string }

/** The first password's floor, the server's too. */
export const SETUP_PASSWORD_MIN = 12

export const getCeSetup = () => get<CeSetup>('/api/ce/setup')
/** Sends this browser's zone too: the console runs on the install's machine, so cron times keep the host's clock. */
export const postCeSetup = (email: string, password: string) =>
  post<unknown>('/api/ce/setup', { email, password, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone })

/** Whether the first run is still open. Never asked outside the community edition. */
export function useCeSetup() {
  return useQuery({ queryKey: ['ce', 'setup'], queryFn: getCeSetup, enabled: IS_CE, retry: false })
}

/** The caps and the machine, re-read every 30s: a queue drains and the node goes quiet without a page reload. */
export function useCeLimits() {
  return useQuery({
    queryKey: ['ce', 'limits'],
    queryFn: () => get<CeLimits>('/api/ce/limits'),
    enabled: IS_CE,
    refetchInterval: 30_000,
  })
}

/** The cap refusal a failed write carries, or null for any other failure — and always in the cloud console, which
 *  has no caps: its 409s keep their own error. */
export function capRefusal(err: unknown): CapRefusal | null {
  if (!IS_CE || !(err instanceof ApiError) || err.status !== 409) return null
  const body = err.body as Partial<CapRefusal> | null
  if (!body || typeof body.detail !== 'string' || typeof body.kind !== 'string' || typeof body.upgrade_url !== 'string') return null
  return { detail: body.detail, kind: body.kind, limit: Number(body.limit), upgrade_url: body.upgrade_url }
}
