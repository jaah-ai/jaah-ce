import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import LinearProgress from '@mui/material/LinearProgress'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { useApiRead } from '../../../hooks/useApiRead'
import { useRowActions } from '../../../hooks/useBusyIds'
import Banner from '../../../ui/Banner'
import LoadingState from '../../../ui/LoadingState'
import { KeyValue, KeyValueList } from '../../../ui/KeyValueList'
import { getOperation, NODEVAULT_AUDIT_URL, retryItem, type OperationDetail, type OperationItem } from './api'

/* The Migrate progress and the Result step (AC-4.8), both read off `GET /api/nodevault/operations/{id}` —
 * the server's own rows, so a reload lands on the same numbers. Result: nodes holding the key (n/total),
 * credentials moved, skipped (with the reason) and failed (with Retry), and the audit link. A rotation's
 * Result adds its rekey phase: rekeyed, and the ones no node could move with what to do (a rekey has no
 * Retry — re-entering the secret seals it under the current key). Polls while anything is still moving. */

const POLL_MS = 3000

const unsettled = (i: OperationItem) => i.state === 'pending' || i.state === 'sealed'

/** A titled list of credentials, each with its " — note" (a reason, a message) when it has one; nothing when
 *  the list is empty. Also the Migrate step's "Can’t move" list. */
export function NoteList({ title, testId, items }: {
  title: string
  testId: string
  items: { id: string; name: string; note: string | null }[]
}): JSX.Element | null {
  if (items.length === 0) return null
  return (
    <Box>
      <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>{title}</Typography>
      <Box component="ul" sx={{ m: 0, pl: 2.5 }} data-testid={testId}>
        {items.map(item => (
          <li key={item.id}>
            <Typography variant="body2" component="span" sx={{ overflowWrap: 'anywhere' }}>{item.name}</Typography>
            {item.note !== null && <Typography variant="body2" component="span" sx={{ color: 'text.secondary' }}> — {item.note}</Typography>}
          </li>
        ))}
      </Box>
    </Box>
  )
}

function isMoving(op: OperationDetail | null): boolean {
  return op === null || op.state === 'running' || op.items.some(unsettled)
}

export default function ResultStep({
  operationId,
  onDone,
}: {
  operationId: string
  /** Leave the result; absent while the operation is still migrating. */
  onDone?: () => void
}): JSX.Element {
  const { data: op, error: loadError, refresh: load } = useApiRead(
    ['nodevault-operation', operationId],
    () => getOperation(operationId),
    'Couldn’t load the result. Reload the page.',
    // Polled while the operation moves; stops once every item settles.
    { refetchInterval: op => (isMoving(op) ? POLL_MS : false) },
  )
  const { busy, error, run } = useRowActions()

  async function retry(item: OperationItem) {
    await run(item.credential_id, 'Couldn’t retry. Try again.', async () => {
      await retryItem(operationId, item.credential_id)
      await load()
    })
  }

  if (!op) {
    if (!loadError) return <LoadingState />
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, alignItems: 'flex-start' }}>
        <Banner severity="error">{loadError}</Banner>
        {onDone && <Button onClick={onDone}>Close</Button>}
      </Box>
    )
  }

  const migrate = op.items.filter(i => i.phase === 'migrate')
  const done = migrate.filter(i => i.state === 'done')
  const skipped = migrate.filter(i => i.state === 'skipped')
  const failed = migrate.filter(i => i.state === 'failed')
  const pending = migrate.filter(unsettled)
  const running = op.state === 'running'
  const rotation = op.kind === 'rotation'
  const rekeyed = op.items.filter(i => i.phase === 'rekey' && i.state === 'done')
  const notRekeyed = op.items.filter(i => i.phase === 'rekey' && i.state === 'failed')

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }} data-testid="nodevault-result">
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {op.state === 'failed' && (
        <Banner severity="info">
          {rotation
            ? 'Abandoned. The new key was removed from your nodes; your current key stays in use.'
            : done.length ? 'Abandoned. Credentials already moved stay in NodeVault.' : 'Abandoned.'}
        </Banner>
      )}
      {running && (
        <Box>
          <Typography variant="body2" sx={{ mb: 1 }}>
            Moving credentials: {done.length + failed.length} of {done.length + failed.length + pending.length}
          </Typography>
          <LinearProgress
            variant={pending.length + done.length + failed.length === 0 ? 'indeterminate' : 'determinate'}
            value={pending.length + done.length + failed.length === 0 ? undefined : (100 * (done.length + failed.length)) / (done.length + failed.length + pending.length)}
          />
          {op.waiting_for_holder && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>Waiting for a node that holds the key to come online.</Typography>
          )}
        </Box>
      )}

      <KeyValueList>
        <KeyValue label={rotation ? 'Nodes with the new key' : 'Nodes with the key'} strong valueTestId="nodevault-result-nodes">
          {op.counts.nodes_holding ?? 0} of {op.counts.nodes_total ?? 0}
        </KeyValue>
        {rotation && (
          <>
            <KeyValue label="Rekeyed" strong valueTestId="nodevault-result-rekeyed">{rekeyed.length}</KeyValue>
            <KeyValue label="Not rekeyed" strong color={notRekeyed.length ? 'error.main' : undefined} valueTestId="nodevault-result-not-rekeyed">{notRekeyed.length}</KeyValue>
          </>
        )}
        <KeyValue label={rotation ? 'Moved to NodeVault' : 'Moved'} strong valueTestId="nodevault-result-migrated">{done.length}</KeyValue>
        <KeyValue label="Skipped" strong valueTestId="nodevault-result-skipped">{skipped.length}</KeyValue>
        <KeyValue label={rotation ? 'Failed to move' : 'Failed'} strong color={failed.length ? 'error.main' : undefined} valueTestId="nodevault-result-failed">{failed.length}</KeyValue>
      </KeyValueList>

      {failed.length > 0 && (
        <Box>
          <Typography variant="body2" sx={{ fontWeight: 600, mb: 0.5 }}>Failed</Typography>
          <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none' }} data-testid="nodevault-result-failed-list">
            {failed.map(item => (
              <Box component="li" key={item.credential_id} sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', py: 0.25 }}>
                <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{item.name}</Typography>
                <Typography variant="body2" sx={{ color: 'text.secondary', flex: '1 1 200px' }}>{item.message ?? 'The move failed. Retry.'}</Typography>
                {op.state !== 'failed' && <Button size="small" disabled={busy.has(item.credential_id)} onClick={() => void retry(item)}>Retry</Button>}
              </Box>
            ))}
          </Box>
        </Box>
      )}

      <NoteList title="Not rekeyed" testId="nodevault-result-not-rekeyed-list"
        items={notRekeyed.map(i => ({ id: i.credential_id, name: i.name, note: i.message ?? 'Re-enter the secret to move it to the current key.' }))} />

      <NoteList title="Skipped" testId="nodevault-result-skipped-list"
        items={skipped.map(i => ({ id: i.credential_id, name: i.name, note: i.message || null }))} />

      {error && <Banner severity="error">{error}</Banner>}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, flexWrap: 'wrap' }}>
        {onDone && !running && <Button variant="contained" onClick={onDone}>Done</Button>}
        <Link component={RouterLink} to={NODEVAULT_AUDIT_URL} variant="body2">Audit</Link>
      </Box>
    </Box>
  )
}
