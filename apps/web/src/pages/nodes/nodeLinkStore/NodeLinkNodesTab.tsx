import { type JSX, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import { useAuth, useCan } from '../../../auth'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useNodesRoster, workNodes } from '../../../hooks/useNodesRoster'
import { useSubmit } from '../../../hooks/useSubmit'
import { isOrgAdminSeat } from '../../../lib/orgAdmin'
import { toggled } from '../../../lib/sets'
import Banner from '../../../ui/Banner'
import EmptyState from '../../../ui/EmptyState'
import FormDialog from '../../../ui/FormDialog'
import { tapTargetSx } from '../../../ui/listStyles'
import StatusChip, { type Tone } from '../../../ui/StatusChip'
import type { NodeRecord } from '../../../types'
import { listNodeLinkMounts, listStores, type NodeLinkDetail, type NodeMount, setNodeLinkNodes } from './api'
import MountReason from './MountReason'
import { storesKey } from './NodeLinkStorageTab'

/* A NodeLink's Nodes tab: the nodes it is attached to, each with whether that node reports it mounted — from
 * ONE org-wide read (`GET …/nodelinks/mounts`), never a read per node. Attaching is the node-switch tier (an
 * org admin, or the operator) plus the plan term; detaching needs no term — the server re-decides all of it.
 * Names: the roster, else the mounts read's, else "Deleted node". `orgId`: the operator's read-only view of another
 * org's NodeLink — that org's mounts and nodes, nothing to attach or detach. */

const MOUNT: Record<NodeMount, { label: string; tone: Tone }> = {
  mounted: { label: 'Mounted', tone: 'success' },
  not_mounted: { label: 'Not mounted', tone: 'warning' },
  needs_permission: { label: 'Needs permission', tone: 'warning' },
  unknown: { label: 'Unknown', tone: 'neutral' },
  not_set_up: { label: 'Not set up yet', tone: 'neutral' },
}

const PLAN_OFF = 'NodeLink isn’t on your plan.'

export const MOUNTS_KEY = ['nodelink-mounts']

export default function NodeLinkNodesTab({
  detail,
  orgId: viewedOrgId,
  onChange,
}: {
  detail: NodeLinkDetail
  /** Another org's NodeLink, viewed read-only by the operator. */
  orgId?: string
  onChange: (row: NodeLinkDetail) => void
}): JSX.Element {
  const { admin } = useAuth()
  const can = useCan()
  const planOn = admin?.nodelink_enabled === true
  const canAttach = isOrgAdminSeat(admin) && !viewedOrgId
  const queryClient = useQueryClient()
  const [attaching, setAttaching] = useState(false)
  const [confirm, confirmDialog] = useConfirm()
  const { busy, error: writeError, run } = useRowActions<string>()

  const nodesQuery = useNodesRoster()
  const orgId = viewedOrgId ?? admin?.org?.id
  // A presence-only host has no uuid and cannot be attached; on the operator's roster, the NodeLink's org only.
  const orgNodes = useMemo(
    () => (nodesQuery.data ?? []).filter(n => n.id !== null && (!orgId || !n.org || n.org.id === orgId)),
    [nodesQuery.data, orgId],
  )
  const nodesById = useMemo(() => new Map(orgNodes.map(n => [n.id as string, n])), [orgNodes])
  const nodesError = nodesQuery.isError ? apiDetail(nodesQuery.error, 'Could not load nodes.') : null

  const mountsQuery = useQuery({
    queryKey: viewedOrgId ? [...MOUNTS_KEY, viewedOrgId] : MOUNTS_KEY,
    queryFn: () => listNodeLinkMounts(viewedOrgId),
  })
  const mountsError = mountsQuery.isError ? apiDetail(mountsQuery.error, 'Could not read mounts.') : null
  const mounts = mountsQuery.data
  // The storage's mount target IP, for a DNS failure's hint (the Storage tab's read, shared; `credentials:view`,
  // so a keyless reader never asks). Managed storage is the operator's to fix; a failed or skipped read leaves only
  // the server's own out-of-VPC verdict.
  const storesQuery = useQuery({
    queryKey: storesKey(viewedOrgId),
    queryFn: () => listStores(viewedOrgId),
    enabled: can('credentials:view') && detail.store_id !== null && !detail.store_managed,
  })
  const store = storesQuery.data?.find(s => s.id === detail.store_id)
  const mountTargetIp = store ? store.mount_target_ip : undefined
  // The Storage tab's Change: its `canEdit` (`NodeLinkEditPage.tsx`) on adopted storage, where the read tells.
  const canChangeMountTarget = can('credentials:edit') && planOn && !viewedOrgId && (store?.adopted ?? true)
  // A failed read settles each node too (as unknown), never "Reading…" forever.
  const settled = mounts !== undefined || mountsError !== null
  const rosterSettled = nodesQuery.data !== undefined || nodesQuery.isError
  const mine = new Map((mounts ?? []).filter(m => m.nodelink_id === detail.id).map(m => [m.node_id, m]))
  const nameOf = (uuid: string, gone: string) =>
    nodesById.get(uuid)?.name ?? mine.get(uuid)?.node_name ?? (rosterSettled && settled ? gone : '…')

  const saved = (row: NodeLinkDetail) => {
    onChange(row)
    void queryClient.invalidateQueries({ queryKey: MOUNTS_KEY })
  }

  async function detach(uuid: string) {
    const name = nameOf(uuid, 'Deleted node')
    const ok = await confirm({
      title: 'Detach node?',
      identity: name,
      message: `${detail.name} unmounts from this node.`,
      confirmLabel: 'Detach',
      destructive: true,
    })
    if (!ok) return
    await run(uuid, 'Could not detach. Retry.', async () => {
      saved(await setNodeLinkNodes(detail.id, detail.node_ids.filter(id => id !== uuid)))
    })
  }

  const isDeleting = detail.status === 'deleting'
  const attachButton = canAttach && !isDeleting && planOn && (
    <Button variant="outlined" onClick={() => setAttaching(true)} sx={tapTargetSx} data-testid="nodelink-attach">
      Attach
    </Button>
  )
  const waiting = detail.pending_unmount_node_ids

  return (
    <Box data-testid="nodelink-nodes" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 2, maxWidth: '72rem' }}>
      {writeError && <Banner severity="error" testId="nodelink-nodes-error">{writeError}</Banner>}
      {mountsError && <Banner severity="error">{mountsError}</Banner>}
      {waiting.length > 0 && (
        <Typography variant="body2" data-testid="nodelink-waiting">
          Waiting to unmount: {waiting.map(uuid => nameOf(uuid, 'deleted node')).join(', ')}
        </Typography>
      )}

      {detail.node_ids.length === 0 ? (
        <EmptyState title="No nodes attached." action={attachButton || undefined} />
      ) : (
        <>
          <Box component="ul" aria-label="Attached nodes" sx={{ m: 0, p: 0, listStyle: 'none', display: 'flex', flexDirection: 'column' }}>
            {detail.node_ids.map(uuid => {
              const m = mine.get(uuid)
              const state = m ? { mount: m.mount, reason: m.mount_reason } : settled ? { mount: 'unknown' as const, reason: null } : undefined
              const chip = state && (MOUNT[state.mount] ?? MOUNT.unknown)
              // An attached node the mounts read leaves out is one the caller may not read: hidden, not gone.
              const name = mounts !== undefined && rosterSettled && !nodesById.has(uuid) && !m ? 'Hidden node' : nameOf(uuid, 'Deleted node')
              return (
                <Box
                  component="li"
                  key={uuid}
                  data-testid={`nodelink-node-${uuid}`}
                  sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, py: 1, borderBottom: 1, borderColor: 'divider' }}
                >
                  <Typography sx={{ fontWeight: 500, overflowWrap: 'anywhere' }}>{name}</Typography>
                  {state && chip
                    ? <StatusChip label={chip.label} tone={chip.tone} data-status={state.mount} />
                    : <Typography variant="caption" sx={{ color: 'text.secondary' }}>Reading…</Typography>}
                  {canAttach && !isDeleting && (
                    <Button size="small" color="error" disabled={busy.has(uuid)} onClick={() => void detach(uuid)}
                      aria-label={`Detach ${name}`} sx={{ ml: 'auto', ...tapTargetSx }}>
                      Detach
                    </Button>
                  )}
                  {state?.reason && (
                    <MountReason reason={state.reason} nodelinkId={detail.id} mountTargetIp={mountTargetIp}
                      efs={(detail.store_provider ?? 'aws') === 'aws'}
                      canChange={canChangeMountTarget} testId={`nodelink-node-reason-${uuid}`} />
                  )}
                </Box>
              )
            })}
          </Box>
          {attachButton && <Box>{attachButton}</Box>}
        </>
      )}

      {attaching && (
        <AttachNodesDialog
          detail={detail}
          // A Multi-Node Host is not attached itself: it mounts each of its nodes' NodeLinks for them.
          nodes={workNodes(orgNodes)}
          loading={nodesQuery.isPending}
          loadError={nodesError}
          onClose={() => setAttaching(false)}
          onSaved={row => { setAttaching(false); saved(row) }}
        />
      )}
      {confirmDialog}
    </Box>
  )
}

/** Exactly the checked nodes mount it afterwards (`PUT …/{id}/nodes`). `nodes`: the org's, uuid present. Only
 *  nodes that can mount are offered: a Multi-Node Host runs no work (never listed), and a node whose OS cannot
 *  (`unsupported.shared_accounts`) is shown disabled with that reason — unless attached, so it can be detached.
 *  Without the plan term, attached nodes may only be unchecked. */
function AttachNodesDialog({
  detail,
  nodes,
  loading,
  loadError,
  onClose,
  onSaved,
}: {
  detail: NodeLinkDetail
  nodes: NodeRecord[]
  loading: boolean
  loadError: string | null
  onClose: () => void
  onSaved: (row: NodeLinkDetail) => void
}): JSX.Element {
  const { admin } = useAuth()
  const detachOnly = admin?.nodelink_enabled !== true
  const offered = nodes.filter(n => n.hosting !== 'multi_node_host')
  const [picked, setPicked] = useState<Set<string>>(() => new Set(detail.node_ids))
  const { busy, formError: error, setFormError: setError, run } = useSubmit()

  const original = new Set(detail.node_ids)
  const unchanged = picked.size === original.size && [...picked].every(id => original.has(id))

  const toggle = (id: string) => {
    setPicked(prev => toggled(prev, id))
    setError('')
  }

  const submit = () => run('Could not save the nodes. Retry.', async () => onSaved(await setNodeLinkNodes(detail.id, [...picked])))

  return (
    <FormDialog
      title={`Nodes · ${detail.name}`}
      submitLabel="Save"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
      onClose={onClose}
      submitDisabled={unchanged || loading || loadError !== null}
      testId="nodelink-attach-dialog"
    >
      {loading && <Typography variant="body2" sx={{ color: 'text.secondary' }}>Loading nodes…</Typography>}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {!loading && !loadError && offered.length === 0 && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>No nodes yet.</Typography>
      )}
      {!loading && offered.length > 0 && (
        <Box role="group" aria-label="Nodes" sx={{ display: 'flex', flexDirection: 'column' }}>
          {offered.map(node => {
            const id = node.id as string
            // An attached node always stays checkable (to detach it); any other one needs the plan and a capable OS.
            const blocked = original.has(id) ? undefined : detachOnly ? PLAN_OFF : node.unsupported?.shared_accounts || undefined
            return (
              <FormControlLabel
                key={id}
                disabled={blocked !== undefined}
                control={<Checkbox checked={picked.has(id)} onChange={() => toggle(id)} />}
                label={
                  <Box component="span" sx={{ display: 'block', overflowWrap: 'anywhere' }}>
                    {node.name}
                    {blocked && (
                      <Typography component="span" variant="caption" data-testid={`nodelink-attach-blocked-${id}`}
                        sx={{ display: 'block', color: 'text.secondary' }}>
                        {blocked}
                      </Typography>
                    )}
                  </Box>
                }
                sx={{ alignItems: 'center', mr: 0 }}
              />
            )
          })}
        </Box>
      )}
    </FormDialog>
  )
}
