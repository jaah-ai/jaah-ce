import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Button from '@mui/material/Button'
import { EmptyState } from '../ui/EmptyState'
import { DEPLOYED_VERSION_KEY, newerVersionDeployed, VERSION_CHECKED } from '../hooks/useVersionCheck'
import { useMarkNotFound } from '../lib/notFound'

/* The catch-all route. A path this bundle does not know may be one a newer deploy added (a tab open from
 * before `/automations` existed), so it asks for the deployed version first: newer offers a reload; same,
 * unreadable or a dev build says "Page not found" with a way home — never a silent redirect, which left a
 * mistyped `/settings/billing` on an unrelated page with no hint why. The answer lands in the Sidebar banner's query, so the
 * two agree. A sticky `true` already cached is not re-asked (`useVersionCheck`). While shown, the nav lights nothing
 * (`lib/notFound`). */
export default function UnknownRoute(): JSX.Element | null {
  useMarkNotFound()
  const { data, isFetchedAfterMount, isError } = useQuery({
    queryKey: DEPLOYED_VERSION_KEY,
    queryFn: newerVersionDeployed,
    // Sticky as `useVersionCheck`: a cached `true` is not re-asked; a dev build asks nothing.
    enabled: query => query.state.data !== true && VERSION_CHECKED,
    staleTime: 0,
  })
  // Nothing until THIS visit's answer: a `false` the Sidebar cached may predate the deploy that added the path.
  const newer = !VERSION_CHECKED || isError ? false : data === true || isFetchedAfterMount ? data : undefined

  if (newer === undefined) return null
  if (!newer) {
    return (
      <EmptyState
        title="Page not found"
        action={(
          <Button component={RouterLink} to="/" variant="contained" size="small" data-testid="unknown-route-home">
            Go home
          </Button>
        )}
      />
    )
  }
  return (
    <EmptyState
      title="This page needs the latest version"
      action={(
        <Button variant="contained" size="small" onClick={() => window.location.reload()} data-testid="unknown-route-reload">
          Reload
        </Button>
      )}
    />
  )
}
