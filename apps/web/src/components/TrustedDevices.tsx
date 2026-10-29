import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Divider from '@mui/material/Divider'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { del, get } from '../api'
import { useConfirm } from './ConfirmDialog'
import { shortDate } from '../lib/billing'
import { absoluteTime } from '../lib/relativeTime'
import type { TrustedDevice } from '../types'
import { useApiRead } from '../hooks/useApiRead'
import { useRowActions } from '../hooks/useBusyIds'
import Banner from '../ui/Banner'
import EmptyState from '../ui/EmptyState'
import LoadingState from '../ui/LoadingState'
import { ROW_ACTION_SX } from '../ui/listStyles'
import { StatusChip } from '../ui/StatusChip'
import RelativeTime from '../ui/RelativeTime'

/* The signed-in user's trusted browsers (`/profile` → Security): each skips the second factor until it
 * expires. A browser is trusted only from the MFA pages' "Trust this device" box; here it can only be
 * revoked — one row, or all. Self-scoped server-side (`/api/auth/me/...`), so nothing is permission-gated.
 * Owns its own fetch: `ProfilePage` mounts it only while its tab is open. Every button is `type="button"`
 * because the page wraps every panel in its one Save form. */

const PATH = '/api/auth/me/trusted-devices'

export default function TrustedDevices() {
  const { data, loading, error: loadError, reload } = useApiRead(
    ['trusted-devices'],
    async () => {
      const rows = await get<TrustedDevice[]>(PATH)
      return Array.isArray(rows) ? rows : []
    },
    'Failed to load trusted devices. Reload to retry.',
  )
  const devices = data ?? []
  /* Two errors, as `ProfilePage`'s credentials: a failed revoke must not call a working list unavailable.
   * `busy` holds the row in flight, or 'all'. */
  const { busy, error: actionError, run } = useRowActions<number | 'all'>()
  const [confirm, confirmDialog] = useConfirm()

  /* Re-read, not spliced, after every write: the server is the record. */
  const revoke = (target: number | 'all', failure: string) =>
    run(target, failure, async () => {
      await del(target === 'all' ? PATH : `${PATH}/${target}`)
      await reload()
    })

  async function handleRevokeAll() {
    const ok = await confirm({
      title: 'Revoke all devices',
      message: 'Every browser asks for a code again. Other sessions sign out.',
      confirmLabel: 'Revoke all',
      destructive: true,
    })
    if (ok) await revoke('all', 'Failed to revoke devices')
  }

  return (
    <>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {actionError && <Banner severity="error">{actionError}</Banner>}

      {loading && data === null ? (
        <LoadingState rows={2} />
      ) : loadError ? null : devices.length === 0 ? (
        <EmptyState title="No trusted devices" />
      ) : (
        /* A divided list: a table starves the label in a 480px column. */
        <Stack divider={<Divider />} data-testid="trusted-devices">
          {devices.map(d => (
            <Box
              key={d.id}
              data-testid="trusted-device"
              sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 1 }}
            >
              <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', columnGap: 1, rowGap: 0.5 }}>
                  <Typography variant="body2" sx={{ fontWeight: 500, overflowWrap: 'break-word' }}>
                    {d.label}
                  </Typography>
                  {d.current && <StatusChip label="This device" tone="info" />}
                </Box>
                {/* One caption; its `·`-joined parts wrap whole on a phone rather than mid-date. */}
                <Typography
                  variant="caption"
                  component="p"
                  sx={{ color: 'text.secondary', display: 'flex', flexWrap: 'wrap', columnGap: 0.75 }}
                >
                  <span title={absoluteTime(d.created_at)}>Trusted {shortDate(d.created_at)}</span>
                  <span aria-hidden>·</span>
                  {d.last_used_at ? <RelativeTime iso={d.last_used_at} prefix="Last used" /> : <span>Not used yet</span>}
                  <span aria-hidden>·</span>
                  <span title={absoluteTime(d.expires_at)}>Expires {shortDate(d.expires_at)}</span>
                </Typography>
              </Box>
              <Button
                type="button"
                onClick={() => void revoke(d.id, 'Failed to revoke device')}
                disabled={busy.size > 0}
                aria-label={`Revoke ${d.label}${d.current ? ' (this device)' : ''}`}
                variant="outlined"
                color="error"
                sx={ROW_ACTION_SX}
              >
                Revoke
              </Button>
            </Box>
          ))}
        </Stack>
      )}

      {!loadError && devices.length > 0 && (
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          <Button
            type="button"
            onClick={() => void handleRevokeAll()}
            disabled={busy.size > 0}
            variant="outlined"
            color="error"
            sx={ROW_ACTION_SX}
          >
            Revoke all
          </Button>
        </Box>
      )}

      {confirmDialog}
    </>
  )
}
