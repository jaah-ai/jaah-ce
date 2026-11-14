import Box from '@mui/material/Box'
import type { JSX, ReactNode } from 'react'
import { apiDetail, isNotFound } from '../../api'
import OtherOrgGate, { type LocatableType } from '../../components/OtherOrgGate'
import { capitalize } from '../../lib/humanize'
import Banner from '../Banner'
import EmptyState from '../EmptyState'
import LoadFailedState from '../LoadFailedState'
import LoadingState from '../LoadingState'
import NotFoundState from '../NotFoundState'

/* A DETAIL PAGE BEFORE ITS RECORD: loading, not found, or a load error — in that precedence — under the
 * page's trail ALONE (`ui/PageHeader` needs a title the record has not given yet; `mb: 1.5` is its margin,
 * so content settles at the same height). No id in the not-found copy: a uuid is noise, and it is in the
 * address bar. `locate` routes not-found through `OtherOrgGate`, so a record in another org reads as
 * "switch org", not "missing". A page that can ask again passes `onRetry`: the error then reads as
 * `ui/LoadFailedState` — the detail, "<X> unavailable", Retry — never a bare banner. */
export function DetailStatus({
  trail,
  loading,
  notFound,
  error,
  failedTitle,
  onRetry,
  locate,
}: {
  trail: ReactNode
  loading?: boolean
  /** The empty-state title ("User not found"); falsy when the record exists. */
  notFound?: string | false | null
  /** The load error. Rendered when neither loading nor not-found; the server's detail, a string, with `onRetry`. */
  error?: ReactNode
  /** With `onRetry`: the heading over Retry ("Billing unavailable"). */
  failedTitle?: string
  onRetry?: () => void
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
          : onRetry
            ? <LoadFailedState title={failedTitle ?? 'Unavailable'} error={typeof error === 'string' ? error : null} onRetry={onRetry} />
            : <Banner severity="error">{error}</Banner>}
    </Box>
  )
}

/* THE EDIT PAGES' gate over `hooks/useEditRecord`'s read: the same `DetailStatus`, its three props derived the one
 * way every id-keyed edit page used to spell for itself — still loading while neither not-found nor failed (the
 * record has not landed), `notFound` as the empty-state title, else the load error (`useEditRecord` already filled
 * its `loadFallback` in). The page keeps its own condition (`loading || notFound || loadError || !row`) and returns
 * this. */
export function EditStatus({
  trail, loading, notFound, loadError, notFoundTitle, locate,
}: {
  trail: ReactNode
  loading: boolean
  notFound: boolean
  loadError: string
  /** The empty-state title ("Group not found"). */
  notFoundTitle: string
  locate?: { objectType: LocatableType, objectId: string | undefined }
}): JSX.Element {
  return (
    <DetailStatus
      trail={trail}
      loading={loading || (!notFound && !loadError)}
      notFound={notFound && notFoundTitle}
      error={loadError}
      locate={locate}
    />
  )
}

/** A detail page before its `useQuery` record (the page's own `if (!row)`): loading, "<Noun> not found" on a 404
 *  (or the page's own `notFoundTitle`), else the failure with Retry. */
export function detailStatus(
  read: { isError: boolean; error: unknown; refetch: () => unknown },
  trail: ReactNode,
  noun: string,
  notFoundTitle?: string,
): JSX.Element {
  const Noun = capitalize(noun)
  if (!read.isError) return <DetailStatus trail={trail} loading />
  return isNotFound(read.error)
    ? <DetailStatus trail={trail} notFound={notFoundTitle ?? `${Noun} not found`} />
    : <DetailStatus trail={trail} failedTitle={`${Noun} unavailable`} error={apiDetail(read.error, `Could not load the ${noun}`)} onRetry={() => void read.refetch()} />
}

export default DetailStatus
