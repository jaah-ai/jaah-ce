// Readable names for AWS regions (`GET /api/data-regions`, any signed-in user): `labels` maps every region
// code to its label, the operator's own labels winning. One cached read for the whole console — the list moves only
// when the operator edits Settings › Infrastructure › Data regions.

import { useQuery } from '@tanstack/react-query'
import { get } from '../api'

export type RegionOption = { region: string; label: string }

export type DataRegions = {
  control_plane_region: string
  /** The regions an org may choose, the control plane's first. */
  regions: RegionOption[]
  /** Every AWS region code → its readable label. */
  labels: Record<string, string>
}

export const DATA_REGIONS_KEY = ['data-regions'] as const

export const getDataRegions = () => get<DataRegions>('/api/data-regions')

/** `code`'s readable label ("EU (Frankfurt)"); the code itself while the labels load, or for one they do not name.
 *  Null for no code (and then nothing is fetched). */
export function useRegionLabel(code: string | null | undefined): string | null {
  const query = useQuery({
    queryKey: DATA_REGIONS_KEY,
    queryFn: getDataRegions,
    enabled: Boolean(code),
    staleTime: 10 * 60_000,
    retry: false,
  })
  if (!code) return null
  return query.data?.labels?.[code] ?? code
}
