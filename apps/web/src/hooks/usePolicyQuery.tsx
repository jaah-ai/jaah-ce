import type { JSX } from 'react'
import { useQuery, type QueryKey, type UseQueryOptions } from '@tanstack/react-query'
import { apiDetail } from '../api'
import { OFF_PLAN_ONCE, offPlan } from '../lib/planSwitches'
import Banner from '../ui/Banner'
import LoadingState from '../ui/LoadingState'

/* The ONE read shell of a plan-gated org-policy section (retention, residency, SCIM, SIEM export, IP allowlist,
 * SSO): the query with `OFF_PLAN_ONCE`, and the `gate` the section returns until its data is in — a skeleton of
 * `rows` while pending, nothing for a tenant the plan refuses (the operator is never refused, so sees the
 * error), else the server's refusal (`failure` its fallback). The section's own plan check (no fetch while off)
 * stays at its call site; `data` is the query's, typed present once `gate` is null. */
export function usePolicyQuery<T>({ operator, rows, failure, ...options }: Omit<UseQueryOptions<T, Error, T, QueryKey>, 'retry' | 'retryOnMount' | 'refetchOnWindowFocus' | 'refetchOnReconnect'> & {
  operator: boolean
  rows: number
  failure: string
}): { gate: JSX.Element; data: T | undefined } | { gate: null; data: T } {
  const query = useQuery({ ...options, ...OFF_PLAN_ONCE })
  if (query.isPending) return { gate: <LoadingState rows={rows} />, data: undefined }
  if (query.isError) {
    // `data` stays the last good read through a failed refetch, so a section's draft seeding never sees it vanish.
    return { gate: !operator && offPlan(query.error) ? <></> : <Banner severity="error">{apiDetail(query.error, failure)}</Banner>, data: query.data }
  }
  return { gate: null, data: query.data }
}
