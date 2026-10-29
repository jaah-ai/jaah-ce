import { type JSX, useState } from 'react'
import TextField from '@mui/material/TextField'
import { useQueryClient } from '@tanstack/react-query'
import { useSubmit } from '../../../hooks/useSubmit'
import type { VirtualNode } from '../../../types'
import FormDialog from '../../../ui/FormDialog'
import { updateVirtualNodeCaps, virtualNodesKey } from './api'

/* A co-located node's cap OVERRIDES, operator only (the PATCH 403s a tenant). Empty = the node's Shared tier
 * disk, else the system setting (`vnode_disk_cap_gb` / `vnode_inode_cap`), sent as null; both fields are always sent, so what the
 * operator sees is what lands. The placeholder shows what applies now when no override is set. */

// The server's floors (`VirtualNodeCapsIn`): 1 GB, 1000 inodes.
const DISK_MIN = 1
const INODE_MIN = 1000

const parseCap = (raw: string, min: number): number | null | 'invalid' => {
  const t = raw.trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isInteger(n) && n >= min ? n : 'invalid'
}

export function VnodeCapsDialog({
  hostNodeId,
  vnode,
  onClose,
}: {
  hostNodeId: string
  vnode: VirtualNode
  onClose: () => void
}): JSX.Element {
  const queryClient = useQueryClient()
  const [disk, setDisk] = useState(vnode.disk_cap_gb == null ? '' : String(vnode.disk_cap_gb))
  const [inodes, setInodes] = useState(vnode.inode_cap == null ? '' : String(vnode.inode_cap))
  const { busy, formError, run } = useSubmit()
  const diskCap = parseCap(disk, DISK_MIN)
  const inodeCap = parseCap(inodes, INODE_MIN)
  const name = vnode.node_name ?? `slot ${vnode.slot}`
  // With an override set, the effective value IS the override, so it says nothing about the default.
  const placeholder = (override: number | null | undefined, effective: number | undefined) =>
    override == null && effective != null ? String(effective) : 'Default'

  return (
    <FormDialog
      title={`Caps · ${name}`}
      submitLabel="Save"
      busy={busy}
      error={formError}
      onSubmit={() => {
        if (diskCap === 'invalid' || inodeCap === 'invalid') return
        void run('Could not save caps.', async () => {
          await updateVirtualNodeCaps(hostNodeId, vnode.id, { disk_cap_gb: diskCap, inode_cap: inodeCap })
          await queryClient.invalidateQueries({ queryKey: virtualNodesKey(hostNodeId) })
          onClose()
        })
      }}
      onClose={onClose}
      submitDisabled={diskCap === 'invalid' || inodeCap === 'invalid'}
      maxWidth="xs"
      testId="vnode-caps-dialog"
      submitTestId="vnode-caps-save"
    >
      <TextField
        label="Disk (GB)"
        type="number"
        value={disk}
        onChange={e => setDisk(e.target.value)}
        placeholder={placeholder(vnode.disk_cap_gb, vnode.effective_disk_cap_gb)}
        error={diskCap === 'invalid'}
        helperText={diskCap === 'invalid' ? `Whole number, ${DISK_MIN} or more` : undefined}
        autoFocus
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: DISK_MIN, step: 1, 'data-testid': 'vnode-caps-disk' } }}
      />
      <TextField
        label="Inodes"
        type="number"
        value={inodes}
        onChange={e => setInodes(e.target.value)}
        placeholder={placeholder(vnode.inode_cap, vnode.effective_inode_cap)}
        error={inodeCap === 'invalid'}
        helperText={inodeCap === 'invalid' ? `Whole number, ${INODE_MIN} or more` : undefined}
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: INODE_MIN, step: 1, 'data-testid': 'vnode-caps-inodes' } }}
      />
    </FormDialog>
  )
}

export default VnodeCapsDialog
