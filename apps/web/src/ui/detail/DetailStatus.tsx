import Box from '@mui/material/Box'
import type { JSX, ReactNode } from 'react'
import OtherOrgGate, { type LocatableType } from '../../components/OtherOrgGate'
import Banner from '../Banner'
import EmptyState from '../EmptyState'
import LoadingState from '../LoadingState'
import NotFoundState from '../NotFoundState'

/* A DETAIL PAGE BEFORE ITS RECORD: loading, not found, or a load error — in that precedence — under the
 * page's trail ALONE (`ui/PageHeader` needs a title the record has not given yet; `mb: 1.5` is its margin,
 * so content settles at the same height). No id in the not-found copy: a uuid is noise, and it is in the
 * address bar. `locate` routes not-found through `OtherOrgGate`, so a record in another org reads as
 * "switch org", not "missing". */
export function DetailStatus({
  trail,
  loading,
  notFound,
  error,
  locate,
}: {
  trail: ReactNode
  loading?: boolean
  /** The empty-state title ("User not found"); falsy when the record exists. */
  notFound?: string | false | null
  /** The load error. Rendered when neither loading nor not-found. */
  error?: ReactNode
  locate?: { objectType: LocatableType, objectId: string | undefined }
}): JSX.Element {
  const empty = notFound ? <EmptyState title={notFound} /> : null
  // Under the operator's org filter a not-found is "hidden by the filter" first (`ui/NotFoundState.tsx`).
  const missing = empty
    ? <NotFoundState fallback={locate ? <OtherOrgGate {...locate} fallback={empty} /> : empty} />
    : null
  return (
    <Box>
      <Box sx={{ mb: 1.5 }}>{trail}</Box>
      {loading
        ? <LoadingState />
        : missing
          ? missing
          : <Banner severity="error">{error}</Banner>}
    </Box>
  )
}

export default DetailStatus
