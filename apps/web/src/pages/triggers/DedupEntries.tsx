import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useConfirm } from '../../components/ConfirmDialog'
import { useRowActions } from '../../hooks/useBusyIds'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import RelativeTime from '../../ui/RelativeTime'
import { clearAutomationDedup, listAutomationDedup } from '../automations/api'

/* The items an event automation has filed a Task for (`GET /api/automations/{id}/dedup`), each remembered for 90 days so it
 * is not filed twice. Clearing one lets the same item file again (AC-9.2). */

export default function DedupEntries({ automationId }: { automationId: string }) {
  const query = useQuery({
    queryKey: ['automation-dedup', automationId],
    refetchOnWindowFocus: false,
    queryFn: () => listAutomationDedup(automationId),
  })
  const page = query.data ?? null
  const { busy: clearing, error: clearError, setError: setClearError, run } = useRowActions()
  const error = clearError || (query.isError && !query.isFetching ? apiDetail(query.error, 'Could not load dedup entries') : null)
  const [confirm, confirmDialog] = useConfirm()

  function reload() {
    setClearError('')
    void query.refetch()
  }

  async function clear(key: string) {
    const ok = await confirm({
      title: 'Clear entry',
      message: 'Clear this entry? The same item may file a Task again.',
      identity: key,
      confirmLabel: 'Clear',
      destructive: true,
    })
    if (!ok) return
    await run(key, 'Could not clear the entry', async () => {
      await clearAutomationDedup(automationId, key)
      reload()
    })
  }

  return (
    <Box data-testid="trigger-dedup" sx={{ display: 'flex', flexDirection: 'column', gap: 1, minWidth: 0 }}>
      {error && (
        <Banner severity="error">
          {error}{' '}
          <Button size="small" onClick={reload}>
            Retry
          </Button>
        </Banner>
      )}
      {page && page.items.length === 0 && (
        <EmptyState title="No dedup entries yet." />
      )}
      {page && page.items.length > 0 && (
        <>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            {page.total === 1 ? '1 entry' : `${page.total} entries`}
          </Typography>
          <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, minWidth: 0 }}>
            {page.items.map(entry => (
              <Box
                component="li"
                key={entry.dedup_key}
                data-testid="dedup-row"
                sx={{
                  display: 'flex',
                  flexWrap: 'wrap',
                  alignItems: 'center',
                  gap: 1,
                  py: 1,
                  borderBottom: '1px solid',
                  borderColor: 'divider',
                  minWidth: 0,
                }}
              >
                <Typography
                  variant="body2"
                  component="code"
                  sx={{
                    flex: '1 1 12rem',
                    minWidth: 0,
                    overflowWrap: 'anywhere',
                    fontFamily: theme => theme.typography.mono.fontFamily,
                  }}
                >
                  {entry.dedup_key}
                </Typography>
                <RelativeTime iso={entry.first_seen_at} variant="body2" sx={{ color: 'text.secondary' }} />
                {entry.task && (
                  <MuiLink component={Link} to={`/tasks/${entry.task.id}`} variant="body2" sx={{ overflowWrap: 'anywhere' }}>
                    {entry.task.title ? `#${entry.task.id} ${entry.task.title}` : `#${entry.task.id}`}
                  </MuiLink>
                )}
                <Button
                  size="small"
                  color="error"
                  data-testid="dedup-clear"
                  aria-label={`Clear dedup entry ${entry.dedup_key}`}
                  disabled={clearing.size > 0}
                  onClick={() => void clear(entry.dedup_key)}
                >
                  Clear
                </Button>
              </Box>
            ))}
          </Box>
        </>
      )}
      {confirmDialog}
    </Box>
  )
}
