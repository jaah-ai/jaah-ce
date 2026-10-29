import { useQuery } from '@tanstack/react-query'
import { Navigate, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../auth'
import { DenyListBackground, useListBackground } from '../../components/ListDetail'
import { AUDIT_TAB_PATHS, AUDIT_WORKFLOW_TAB_PATHS, AUTOMATION_TAB_PATHS } from '../../routeGroups'
import { LoadingState } from '../../ui/LoadingState'
import { automationByLegacy } from './api'

/* THE RETIRED SCHEDULE AND TRIGGERS ADDRESSES — every one lands on the matching Automations page and never
 * 404s (bookmarks, Slack cards, old links). Each hop is a `replace`, so Back skips the retired address, and
 * keeps the rest of the query. Their routes in `App.tsx` keep the old org-admin guard. */

const AUTOMATIONS = AUTOMATION_TAB_PATHS.automations

/* `/workflows`'s legacy `?tab=` keys, which live in real bookmarks. The Audit targets are operator-only, so
 * any other reader lands on Automations. `cron` / `schedule` / `triggers` (and anything else) name what is
 * now Automations. */
const OPERATOR_TAB_REDIRECTS: Record<string, string> = {
  runs: AUDIT_TAB_PATHS.workflows,
  workflows: AUDIT_WORKFLOW_TAB_PATHS.catalogue,
  personas: AUDIT_TAB_PATHS.personas,
}

function withQuery(path: string, params: URLSearchParams): string {
  const rest = params.toString()
  return rest ? `${path}?${rest}` : path
}

/** `/workflows` (the old Schedule list): Audit for an operator's moved `?tab=`, else Automations. */
export function WorkflowsRedirect() {
  const [searchParams] = useSearchParams()
  const isOperator = useAuth().admin?.is_operator === true
  const background = useListBackground()
  const tab = searchParams.get('tab')
  const target = (tab !== null && isOperator ? OPERATOR_TAB_REDIRECTS[tab] : undefined) ?? AUTOMATIONS
  const params = new URLSearchParams(searchParams)
  params.delete('tab')
  // Behind an open editor (components/ListDetail.tsx) a redirect would replace it.
  if (background) return <DenyListBackground deny={background} />
  return <Navigate to={withQuery(target, params)} replace />
}

/** `/crons`, `/triggers` (the old lists): Automations, query kept. */
export function LegacyListRedirect() {
  const { search } = useLocation()
  const background = useListBackground()
  if (background) return <DenyListBackground deny={background} />
  return <Navigate to={`${AUTOMATIONS}${search}`} replace />
}

/** `/crons/new`, `/triggers/new`: the Automation editor's create page with When preselected. */
export function LegacyNewRedirect({ when }: { when: 'schedule' | 'webhook' }) {
  // Only `when` travels: a crafted old link must not prefill anything an admin might save unread.
  return <Navigate to={`${AUTOMATIONS}/new?when=${when}`} replace />
}

/** `/crons/:id`, `/triggers/:id`: the automation that absorbed that row; any failure (unknown id, a
 *  malformed one, the network) lands on the list, which says the automation no longer exists. */
export function LegacyEditorRedirect({ kind }: { kind: 'cron' | 'trigger' }) {
  const { id = '' } = useParams()
  const resolved = useQuery({
    queryKey: ['automations', 'by-legacy', kind, id],
    queryFn: () => automationByLegacy(kind, id),
    retry: false,
  })
  if (resolved.isPending) return <LoadingState />
  if (resolved.isError) return <Navigate to={`${AUTOMATIONS}?gone=1`} replace />
  return <Navigate to={`${AUTOMATIONS}/${encodeURIComponent(resolved.data.id)}`} replace />
}
