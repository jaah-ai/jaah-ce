import Button from '@mui/material/Button'
import type { JSX, ReactNode } from 'react'
import { useApplyOrgScope } from '../hooks/useOrgScopeUrl'
import { useOrgScope } from '../lib/orgScope'
import InterstitialPanel from './InterstitialPanel'
import { tapTargetSx } from './listStyles'

/* A RECORD THE API DID NOT FIND, while the operator's global org filter is on (`lib/orgScope.ts`): the filter
 * hides other orgs' rows from by-id reads too, so a deep link into an unselected org is "hidden", never a bare
 * "not found" — and one click shows every org again (if the record is still missing then, the page says so).
 * The title names the filter's orgs, so the pane says why without the picker's chip beside it. The same card as
 * `components/OtherOrgGate.tsx`'s "switch org": a heading, the one sentence of why, the one way out. "May": a
 * filtered by-id read cannot tell a hidden record from a deleted one. With no filter on it renders `fallback`,
 * the page's own not-found. */
export function NotFoundState({ fallback }: { fallback: ReactNode }): JSX.Element {
  const scope = useOrgScope()
  const apply = useApplyOrgScope()
  if (!scope) return <>{fallback}</>
  return (
    <InterstitialPanel
      testId="org-scope-hidden"
      title={`Hidden by the org filter (${scope.map(org => org.name).join(', ')})`}
      body="It may belong to an org outside the filter. Show all orgs to open it."
    >
      <Button
        type="button"
        variant="contained"
        onClick={() => apply(null)}
        sx={[tapTargetSx, { alignSelf: 'flex-start', textTransform: 'none' }]}
        data-testid="org-scope-show-all"
      >
        Show all orgs
      </Button>
    </InterstitialPanel>
  )
}

export default NotFoundState
