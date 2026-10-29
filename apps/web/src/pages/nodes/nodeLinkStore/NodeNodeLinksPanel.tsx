// The node detail's NodeLinks tab: which of its org's NodeLinks this node mounts, and how each mount is doing.
// Reads and writes `/api/nodes/{node}/nodelinks` (`routes/nodelinks.py`, "One node's NodeLinks"); the PUT is the
// whole attached set and answers with the new list, so the panel refreshes from it.

import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useApiRead } from '../../../hooks/useApiRead'
import { useSubmit } from '../../../hooks/useSubmit'
import Banner from '../../../ui/Banner'
import { DisabledReason } from '../../../ui/DisabledReason'
import EmptyState from '../../../ui/EmptyState'
import { fieldTapTargetSx, monoSx, tapTargetSx } from '../../../ui/listStyles'
import StatusChip, { type Tone } from '../../../ui/StatusChip'
import { getNodeNodeLinks, type NodeMount, type NodeNodeLink, setNodeNodeLinks } from './api'
import MountReason from './MountReason'

export const PLAN_REASON = "NodeLink isn't on your plan."

const MOUNT_LABEL: Record<NodeMount, string> = {
  mounted: 'Mounted', not_mounted: 'Not mounted', needs_permission: 'Needs permission', unknown: 'Unknown',
  not_set_up: 'Not set up yet',
}
const MOUNT_TONE: Record<NodeMount, Tone> = {
  mounted: 'success', not_mounted: 'warning', needs_permission: 'warning', unknown: 'neutral', not_set_up: 'neutral',
}

export default function NodeNodeLinksPanel({
  nodeId,
  canAttach,
  planAllowsAttach,
  unsupportedReason,
  canChangeMountTarget,
}: {
  nodeId: string
  /** The PUT's tier (`_require_attach_tier`): the operator or an admin of the node's org. ABSENT, NOT DISABLED. */
  canAttach: boolean
  /** The org's NodeLink plan term; gates attaching only — detaching always works. */
  planAllowsAttach: boolean
  /** The server's sentence for an OS that cannot mount (`NodeRecord.unsupported.shared_accounts`). */
  unsupportedReason?: string
  /** Whether this reader may change a NodeLink's mount target IP (`MountReason`'s `canChange`). */
  canChangeMountTarget: boolean
}): JSX.Element {
  // One mount per node (`NodeDetailPage` keys this panel by node id), so neither read nor answer needs a node guard.
  const { data: read, error: loadError } = useApiRead(['node-nodelink-panel', nodeId], () => getNodeNodeLinks(nodeId), 'Failed to load NodeLinks')
  // The PUT's answer stands in for the read: no second GET for a list the server just sent.
  const [written, setWritten] = useState<NodeNodeLink[] | null>(null)
  const rows = written ?? read
  const { busy, formError: writeError, run } = useSubmit()
  const [pick, setPick] = useState('')
  const [confirm, confirmDialog] = useConfirm()

  const write = (ids: string[]) => run('Failed to update NodeLinks', async () => setWritten(await setNodeNodeLinks(nodeId, ids)))

  if (loadError) return <Banner severity="error" testId="node-nodelinks-error">{loadError}</Banner>
  if (!rows) return <Typography variant="body2" sx={{ color: 'text.secondary' }}>Loading…</Typography>

  const attached = rows.filter(r => r.attached)
  const attachedIds = attached.map(r => r.id)
  // Every row is the node's own org's (`_node_view`); a deleting one cannot be attached.
  const available = rows.filter(r => !r.attached && r.status !== 'deleting')
  const blocked = unsupportedReason ?? (planAllowsAttach ? undefined : PLAN_REASON)
  const chosen = available.some(r => r.id === pick) ? pick : (available[0]?.id ?? '')

  async function detach(row: NodeNodeLink) {
    const ok = await confirm({
      title: 'Detach NodeLink?',
      identity: row.name,
      message: 'It unmounts from this node.',
      confirmLabel: 'Detach',
      destructive: true,
    })
    if (ok) await write(attachedIds.filter(id => id !== row.id))
  }

  const attachControl = canAttach && (available.length ? (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
      <TextField
        id="node-nodelinks-pick"
        select
        size="small"
        label="NodeLink"
        value={chosen}
        onChange={e => setPick(e.target.value)}
        disabled={busy || Boolean(blocked)}
        slotProps={{ select: { native: true } }}
        sx={theme => ({ minWidth: '12rem', maxWidth: '100%', ...fieldTapTargetSx(theme) })}
      >
        {available.map(r => <option key={r.id} value={r.id}>{r.name}</option>)}
      </TextField>
      <DisabledReason reason={blocked}>
        <Button
          variant="outlined"
          disabled={busy || Boolean(blocked) || !chosen}
          title={blocked}
          onClick={() => void write([...attachedIds, chosen])}
          sx={tapTargetSx}
        >
          Attach
        </Button>
      </DisabledReason>
      {blocked && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="node-nodelinks-attach-reason">{blocked}</Typography>
      )}
    </Box>
  ) : (
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>No other NodeLinks to attach.</Typography>
  ))

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {/* For every reader: it explains why nothing here mounts. */}
      {unsupportedReason && <Banner severity="info" testId="node-nodelinks-unsupported">{unsupportedReason}</Banner>}
      {writeError && <Banner severity="error" testId="node-nodelinks-write-error">{writeError}</Banner>}

      {attached.length === 0 ? (
        <EmptyState title="No NodeLinks attached." action={attachControl || undefined} />
      ) : (<>
        <Box component="ul" aria-label="Attached NodeLinks" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: 1 }}>
          {attached.map(r => (
            <Box
              component="li"
              key={r.id}
              data-testid={`node-nodelink-${r.id}`}
              sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, py: 1, borderBottom: 1, borderColor: 'divider' }}
            >
              <Typography sx={{ fontWeight: 500, overflowWrap: 'anywhere' }} title={r.name}>{r.name}</Typography>
              <Box component="span" sx={[monoSx, { fontSize: '0.8125rem', overflowWrap: 'anywhere' }]}>
                {r.node_mount_path ?? r.mount_path}
              </Box>
              <StatusChip label={MOUNT_LABEL[r.mount]} tone={MOUNT_TONE[r.mount]} title={r.mount_reason ?? undefined} data-status={r.mount} />
              {canAttach && (
                <Button
                  size="small"
                  color="error"
                  disabled={busy}
                  onClick={() => void detach(r)}
                  aria-label={`Detach ${r.name}`}
                  sx={{ ml: 'auto', ...tapTargetSx }}
                >
                  Detach
                </Button>
              )}
              {r.mount_reason && (
                // Its storage is not this reader's to read: only the server's out-of-VPC verdict gets the hint.
                <MountReason reason={r.mount_reason} nodelinkId={r.id} mountTargetIp={undefined}
                  canChange={canChangeMountTarget} testId={`node-nodelink-reason-${r.id}`} />
              )}
            </Box>
          ))}
        </Box>
        {attachControl}
      </>)}
      {confirmDialog}
    </Box>
  )
}
