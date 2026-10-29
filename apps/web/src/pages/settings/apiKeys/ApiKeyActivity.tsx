import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { get } from '../../../api'
import { useApiRead } from '../../../hooks/useApiRead'
import { absoluteTime, compactAge } from '../../../lib/relativeTime'
import { actorText, rowSentence } from '../../audit/rows'
import type { AuditPage } from '../../audit/types'
import Banner from '../../../ui/Banner'
import { AGE_SLOT } from '../../../ui/feed/geometry'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../../ui/feed/types'
import { statusGlyph, statusTone } from '../../audit/status'
import { auditEventPath } from '../../audit/AuditEventPage'

/* One key's audit rows, newest first: its lifecycle (created, edited, rotated, revoked, refused) and every request
 * made with it (`GET /api/audit?api_key=`, `app/routes/audit.py`). A row opens its event in Audit. */

const LIMIT = 50
const ACTOR_SLOT = 200

export default function ApiKeyActivity({ keyId }: { keyId: string }): JSX.Element {
  const params = new URLSearchParams({ action_type: 'all', limit: String(LIMIT), api_key: keyId })
  const { data, loading, error } = useApiRead(
    ['api-key-activity', keyId],
    () => get<AuditPage>(`/api/audit?${params}`),
    'Failed to load this key’s activity.',
  )
  const rows: FeedRowSpec[] = (data?.items ?? []).map(row => {
    const actor = actorText(row)
    return {
    key: String(row.id),
    indicator: {
      tone: row.status !== null ? statusTone(row.status) : 'neutral',
      glyph: statusGlyph(row.status),
      label: row.status !== null ? `HTTP ${row.status}` : 'no recorded status',
    },
    title: rowSentence(row),
    titleText: rowSentence(row),
    href: auditEventPath(row.id),
    slots: [
      { key: 'actor', width: ACTOR_SLOT, gate: 'id', align: 'right', dim: true, title: actor, node: actor },
      { key: 'age', width: AGE_SLOT, align: 'right', mono: true, dim: true, title: absoluteTime(row.created_at), node: compactAge(row.created_at) },
    ],
    }
  })
  return (
    <Box sx={{ pt: 2 }} data-testid="api-key-activity">
      {error && <Banner severity="error">{error}</Banner>}
      <FeedList
        ariaLabel="Key activity"
        loading={loading && data === null}
        emptyMessage="Nothing yet."
        sections={rows.length > 0 ? [{ key: 'events', rows }] : []}
      />
    </Box>
  )
}
