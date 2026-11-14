import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../auth'
import { gitConnectionsQuery } from '../pages/credentials/api'

/* THE PROJECT CREATE BUTTON'S WORD: "Add project" when the viewer has a repository to bring in, else "New project"
 * (Jaah starts it). The signal is the viewer's OWN Git connections (`GET /api/users/{id}/git-connections`), any
 * row `connected`: the create wizard's repository lookup and its forge work run on exactly those
 * (`routes/git_connections.py`), so an org forge credential the viewer cannot pick there is no repo to bring in.
 * A `needs_reconnect` account is no repo to bring in either: the wizard cannot use it until it is reconnected.
 * Shares `components/GitConnectionsCard.tsx`'s query (`gitConnectionsQuery`), so a connect on /profile re-words it. Unknown (loading,
 * failed, signed out) reads as no connection. `shown` false (no button to word) skips the read. */

const ADD_PROJECT = 'Add project'
const NEW_PROJECT = 'New project'

export function useProjectCreateLabel(shown = true): string {
  const userId = useAuth().admin?.id ?? null
  const query = useQuery({
    ...gitConnectionsQuery(userId ?? ''),
    enabled: shown && userId !== null,
  })
  return query.data?.some(row => row.state === 'connected') ? ADD_PROJECT : NEW_PROJECT
}
