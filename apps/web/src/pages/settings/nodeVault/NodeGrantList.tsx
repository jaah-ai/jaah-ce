import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useApiRead } from '../../../hooks/useApiRead'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useConfirm } from '../../../components/ConfirmDialog'
import Banner from '../../../ui/Banner'
import StatusChip from '../../../ui/StatusChip'
import { keyHashOf, shortHash } from '../../../lib/nodeVaultKeys'
import { grantFor, grantView, identityChanged, keyRecipients, resetIdentity, retryGrant, type GrantView, type NodeVaultNode } from './api'
import { monoSx } from '../../../ui/listStyles'

/** Distribute before sending: who gets a wrapped copy now, and who gets the key later from a peer. */
function previewView(node: NodeVaultNode): GrantView {
  if (keyRecipients([node]).length === 0) return grantView(node, undefined)
  return node.online
    ? { label: 'Ready', tone: 'info', retry: false }
    : { label: 'Gets it when online', tone: 'neutral', retry: false }
}

/* Every node of the org and where one key stands on it (AC-4.5): Delivered / Waiting (offline) / Failed —
 * retrying / Waiting for a node that holds the key, a Retry on a failed delivery, and Reset identity for a
 * node whose identity key changed (§3.5). The fingerprint is RECOMPUTED here from the identity key, never
 * read off the server's field, so an admin comparing it with `jaah nodevault fingerprint` on the machine
 * compares against the key the browser actually wraps to (D13). */

function useFingerprints(nodes: NodeVaultNode[]): Record<string, string> {
  const keys = nodes.map(n => `${n.id}:${n.identity_key ?? ''}`).join('|')
  // `keys` is the identity of `nodes` that matters.
  const { data } = useApiRead(['nodevault-fingerprints', keys], () => Promise.all(nodes.map(async n => [n.id, n.identity_key ? await keyHashOf(n.identity_key).catch(() => '') : ''] as const))
    .then(rows => Object.fromEntries(rows)), '')
  return data ?? {}
}

export default function NodeGrantList({
  nodes,
  keyHash,
  onChanged,
  preview = false,
}: {
  nodes: NodeVaultNode[]
  /** The key whose placement is shown. */
  keyHash: string | null
  onChanged: () => void
  /** Before the key is sent: which nodes will receive it, not where it stands. */
  preview?: boolean
}): JSX.Element {
  const fps = useFingerprints(nodes)
  const [confirm, confirmDialog] = useConfirm()
  const { busy, error, run } = useRowActions()

  const act = (nodeId: string, write: () => Promise<unknown>, fallback: string) =>
    run(nodeId, fallback, async () => { await write(); onChanged() })

  async function reset(node: NodeVaultNode) {
    const ok = await confirm({
      title: 'Reset identity?',
      identity: node.name,
      message: 'Jaah forgets this node’s identity key and removes its key deliveries. When the node registers its new identity, a node that holds the key sends it again. Do this only if you reinstalled or wiped the node.',
      confirmLabel: 'Reset',
      destructive: true,
    })
    if (ok) await act(node.id, () => resetIdentity(node.id), 'Couldn’t reset the identity. Try again.')
  }

  if (nodes.length === 0) {
    return <Typography variant="body2" sx={{ color: 'text.secondary' }}>No nodes yet.</Typography>
  }

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {error && <Banner severity="error">{error}</Banner>}
      <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, border: 1, borderColor: 'divider', borderRadius: 1 }} data-testid="nodevault-node-list">
        {nodes.map((node, i) => {
          const grant = grantFor(node, keyHash)
          const view = preview ? previewView(node) : grantView(node, grant)
          const changed = !preview && identityChanged(node)
          const fp = fps[node.id]
          return (
            <Box
              component="li"
              key={node.id}
              data-testid="nodevault-node"
              data-node={node.name}
              sx={{
                display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5,
                px: 1.5, py: 1, borderTop: i === 0 ? 0 : 1, borderColor: 'divider',
              }}
            >
              <Box sx={{ minWidth: 0, flex: '1 1 180px' }}>
                <Typography variant="body2" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>
                  {node.name}
                  <Typography component="span" variant="caption" sx={{ color: node.online ? 'success.main' : 'text.secondary', ml: 1, fontWeight: 400 }}>
                    {node.online ? 'Online' : 'Offline'}
                  </Typography>
                </Typography>
                <Typography variant="caption" sx={{ color: 'text.secondary' }} component="div">
                  {node.identity_key ? (
                    <span title={fp ? `Identity fingerprint ${fp}` : undefined}>
                      Fingerprint <Box component="span" sx={monoSx} data-testid="nodevault-node-fp">{fp ? shortHash(fp) : '…'}</Box>
                    </span>
                  ) : 'No identity key yet'}
                </Typography>
              </Box>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                <StatusChip label={changed ? 'Identity changed' : view.label} tone={changed ? 'error' : view.tone} data-status={grant?.state ?? 'none'} />
                {view.retry && grant && keyHash && (
                  <Button size="small" disabled={busy.has(node.id)} onClick={() => void act(node.id, () => retryGrant(node.id, keyHash), 'Couldn’t retry. Try again.')}>
                    Retry
                  </Button>
                )}
                {changed && (
                  <Button size="small" color="error" disabled={busy.has(node.id)} onClick={() => void reset(node)}>
                    Reset identity
                  </Button>
                )}
              </Box>
              {(changed || view.detail) && (
                <Typography variant="caption" sx={{ color: changed || grant?.state === 'failed' ? 'error.main' : 'text.secondary', flexBasis: '100%' }}>
                  {changed ? 'Identity key changed — reset to re-enroll' : view.detail}
                </Typography>
              )}
            </Box>
          )
        })}
      </Box>
      {confirmDialog}
    </Box>
  )
}
