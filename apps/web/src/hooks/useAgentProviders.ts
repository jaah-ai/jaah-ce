import { useMemo } from 'react'
import { get } from '../api'
import type { ProviderChoice } from '../types'
import { useApiRead } from './useApiRead'

/* The provider catalogue, fetched once per mount from `GET /api/accounts/providers` (`accounts:view`);
 * `apps/jaahd/agent_providers.py` is the single home of which providers exist. Shared by the Accounts
 * page (`pages/credentials/Accounts.tsx`) and the console's provider choice (`components/WorktreePool.tsx`).
 * It swallows errors: with no catalogue a new add is the Claude-only flow, and a console open offers only
 * "Any". It does not decide an existing account's provider: re-login and enrolment key behaviour on the
 * value; only the display keys on a resolved row.
 * `[]` suffices for consumers that add something (options, losses, `has_quota_model` → `false`).
 * `settled` exists for the one that states something, `providerCell`: "not back yet" and "back with
 * nothing" are the same `[]`, and without it the edit dialog painted the raw enum under a false "failed"
 * hover (R1.2). Failure settles too, so `providerCell` can fall back to the stored value.
 */
export type ProviderCatalogue = {
  rows: ProviderChoice[]
  /** False until the GET has come back either way; not "succeeded". */
  settled: boolean
}

const NO_ROWS: ProviderChoice[] = []

export function useAgentProviders(): ProviderCatalogue {
  // Best-effort: no catalogue is the Claude-only flow, so the error is dropped. It still settles.
  const { data, loading } = useApiRead(['accounts-providers'], () => get<ProviderChoice[]>('/api/accounts/providers'), '')
  // Not an array (a stub, a proxy's page) reads as no catalogue, never a crash in a consumer.
  return useMemo(() => ({ rows: Array.isArray(data) ? data : NO_ROWS, settled: !loading }), [data, loading])
}
