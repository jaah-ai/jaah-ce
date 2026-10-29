// Settings › Infrastructure › Data regions' ONE API client (`/api/operator/data-regions`): the regions an
// org may keep its data in, each with its transcript bucket. Operator-only; a refusal's `detail` is the server's own
// sentence. The control-plane region comes first; its bucket may be '' (the configured bucket).

import { get, put } from '../../../api'

export type DataRegionRow = { region: string; label: string; transcript_bucket: string }

export type OperatorDataRegions = {
  control_plane_region: string
  regions: DataRegionRow[]
  /** Every AWS region by AWS's own name, in AWS's order: the picker, and a row's label until typed. */
  aws_regions: { region: string; label: string }[]
}

export const OPERATOR_DATA_REGIONS_KEY = ['operator-data-regions'] as const

const PATH = '/api/operator/data-regions'

export const getOperatorDataRegions = () => get<OperatorDataRegions>(PATH)
/** 422 with a `detail` sentence on a bad row. */
export const putOperatorDataRegions = (regions: DataRegionRow[]) => put<OperatorDataRegions>(PATH, { regions })
