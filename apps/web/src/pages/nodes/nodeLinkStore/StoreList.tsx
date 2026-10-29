import { type JSX, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import { useCan } from '../../../auth'
import { STATUS_TONE } from '../../../components/NodeLinkPicker'
import { useRowActions } from '../../../hooks/useBusyIds'
import type { CloudProvider, NodeLinkStore } from '../../../types'
import Banner from '../../../ui/Banner'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedIndicator, FeedRowSpec } from '../../../ui/feed/types'
import FormDialog from '../../../ui/FormDialog'
import SectionHeader from '../../../ui/SectionHeader'
import TypedNameConfirm, { typedNameMatches } from '../../../ui/TypedNameConfirm'
import { PROVIDER_LABELS } from '../../settings/cloudConnections/api'
import { countOf } from '../../../lib/humanize'
import { deleteStore, listStores, type NodeLinkStatus, sizeText } from './api'
import { storesKey } from './NodeLinkStorageTab'

/* Networks › NodeLink's Storage section, under the NodeLinks list: the caller's org's stores
 * (`GET /api/org/nodelink-stores`), one `ui/feed/FeedList` row each — the status disc, the name, the file system
 * id, cloud, tier and size, and how many NodeLinks use it. A store Jaah made and the cloud bills
 * (`deletable_in_cloud`) outlives its last NodeLink; the row's ⋯ › Delete is how it goes — only once no NodeLink
 * uses it (a provisioning one too: left by a NodeLink deleted mid-provision). A refusal (a Jaah-managed store's,
 * say) is the server's sentence, shown as is. Delete forgets the row, and with the box ticked deletes the cloud
 * object too (`DELETE …?delete_in_cloud=true`): a 202 leaves the row `deleting`, re-read until gone, and a refused
 * pass (its `last_error` on the disc's hover) offers ⋯ › Retry. Nothing shows while the org has none. */

/* A cloud delete and a provision are the server's passes (`nodelink-provision` cron): re-read while either is out. */
const POLL_MS = 15000

/** What the cloud calls the object a delete in the cloud removes (Azure has none per store). */
const CLOUD_OBJECT: Partial<Record<CloudProvider, string>> = { aws: 'EFS file system', gcp: 'Filestore instance' }

/* A provisioning store no NodeLink uses (one deleted mid-provision) is no pass's: only Delete moves it. */
const busy = (rows: NodeLinkStore[] | undefined) =>
  (rows ?? []).some(r => r.status === 'deleting' || (r.status === 'provisioning' && r.nodelink_count > 0))

/** "Zonal · 1 TiB": what the store is, as far as the server knows. */
const tierText = (s: NodeLinkStore) =>
  [s.tier, s.capacity_gib != null ? sizeText(s.capacity_gib) : null].filter(Boolean).join(' · ') || '—'

/* The state disc both NodeLink lists draw (`NodeLinkList.tsx` too): the task vocabulary's glyphs
 * (`ui/feed/types.ts`), the tone `components/NodeLinkPicker.tsx` gives the status, and `data-status` for oracles. A
 * `last_error` rides the row's `notes`, which turn the disc red. */
const STATUS_GLYPH: Record<NodeLinkStatus, string> = { pending: '○', provisioning: '»', ready: '●', failed: '✕', deleting: '■' }
export const statusIndicator = (status: NodeLinkStatus): FeedIndicator => ({
  tone: STATUS_TONE[status] ?? 'neutral',
  glyph: STATUS_GLYPH[status] ?? '○',
  label: status,
  data: { status },
})

/* Slot widths (px): the file system id (`fs-0123456789abcdef`, mono), the cloud's word, "Regional · 2.5 TiB", a count. */
const FS_SLOT = 150
const CLOUD_SLOT = 48
const TIER_SLOT = 120
const COUNT_SLOT = 72

export default function StoreList(): JSX.Element | null {
  const can = useCan()
  const canDelete = can('credentials:edit')
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: storesKey(undefined),
    queryFn: () => listStores(),
    refetchInterval: q => (busy(q.state.data) ? POLL_MS : false),
  })
  const { busy: acting, error, run } = useRowActions<string>()
  const [deleting, setDeleting] = useState<NodeLinkStore | null>(null)

  const remove = (store: NodeLinkStore, inCloud: boolean) =>
    run(store.id, `Could not delete ${store.name}. Retry.`, async () => {
      const left = await deleteStore(store.id, inCloud)
      queryClient.setQueryData<NodeLinkStore[]>(storesKey(undefined), rows =>
        left === null ? rows?.filter(r => r.id !== store.id) : rows?.map(r => (r.id === store.id ? left : r)))
      // The NodeLink editors' storage reads (another org's are keyed under it) follow.
      void queryClient.invalidateQueries({ queryKey: storesKey(undefined) })
    })

  const rows = query.data
  if (query.isError) return <Banner severity="error">{apiDetail(query.error, 'Could not load storage.')}</Banner>
  if (!rows || rows.length === 0) return null

  const feedRows: FeedRowSpec[] = rows.map(store => {
    const used = store.nodelink_count > 0
    const why = used ? `Used by ${countOf(store.nodelink_count, 'NodeLink')}`
      : store.status === 'deleting' ? 'Being deleted' : undefined
    const tier = tierText(store)
    return {
      key: store.id,
      indicator: statusIndicator(store.status),
      title: store.name,
      titleText: store.name,
      data: { store: store.id },
      busy: acting.has(store.id),
      notes: store.last_error ? [{ key: 'error', text: store.last_error, tone: 'error' }] : undefined,
      slots: [
        { key: 'fs', width: FS_SLOT, gate: 'meta', align: 'left', mono: true, dim: true, title: store.file_system_id ?? undefined, node: store.file_system_id },
        { key: 'cloud', width: CLOUD_SLOT, gate: 'id', align: 'left', node: PROVIDER_LABELS[store.provider ?? 'aws'] },
        { key: 'tier', width: TIER_SLOT, align: 'left', testId: `nodelink-store-tier-${store.id}`, title: tier, node: tier },
        { key: 'count', width: COUNT_SLOT, align: 'right', mono: true, title: countOf(store.nodelink_count, 'NodeLink'), node: store.nodelink_count },
      ],
      actions: !canDelete ? undefined
        : store.status === 'deleting' && store.last_error ? [{
          key: 'retry', label: 'Retry', ariaLabel: `Retry deleting ${store.name}`,
          testId: `nodelink-store-retry-${store.id}`, onClick: () => void remove(store, true),
        }]
          : [{
            key: 'delete', label: 'Delete', ariaLabel: `Delete ${store.name}`, danger: true,
            disabled: why !== undefined, disabledReason: why,
            testId: `nodelink-store-delete-${store.id}`, onClick: () => setDeleting(store),
          }],
    }
  })

  return (
    <Box component="section" aria-labelledby="nodelink-stores-title" data-testid="nodelink-stores"
      sx={{ display: 'flex', flexDirection: 'column', gap: 1, pt: 1 }}>
      <SectionHeader title="Storage" titleId="nodelink-stores-title" />
      {error && <Banner severity="error" testId="nodelink-stores-error">{error}</Banner>}
      <FeedList
        ariaLabel="Storage"
        testId="nodelink-stores-table"
        sections={[{ key: 'stores', rows: feedRows, rail: false }]}
        columns={{
          title: { key: 'name', label: 'Name' },
          slots: [{ key: 'cloud', label: 'Cloud' }, { key: 'tier', label: 'Tier' }, { key: 'count', label: 'NodeLinks' }],
        }}
      />
      {deleting && (
        <StoreDeleteDialog
          store={deleting}
          onClose={() => setDeleting(null)}
          onDelete={inCloud => { setDeleting(null); void remove(deleting, inCloud) }}
        />
      )}
    </Box>
  )
}

/* The store's Delete: what goes and what stays (and bills) — never cut (ui-copy-standards § What never gets cut).
   The box is offered only where Jaah may delete the cloud object, unticked: a delete in the cloud deletes files, so
   it is chosen, never presumed, and asks for the name. A store with no cloud object says nothing about billing. */
export function StoreDeleteDialog({ store, onClose, onDelete }: {
  store: NodeLinkStore
  onClose: () => void
  onDelete: (inCloud: boolean) => void
}): JSX.Element {
  const [inCloud, setInCloud] = useState(false)
  const [typed, setTyped] = useState('')
  const provider = store.provider ?? 'aws'
  const cloud = PROVIDER_LABELS[provider]
  const object = CLOUD_OBJECT[provider]
  const offered = store.deletable_in_cloud === true && object !== undefined
  const message = !store.file_system_id ? 'Nothing of it is left in the cloud.'
    : !offered ? `It stays in ${cloud}.`
      : inCloud ? 'Its files are deleted.' : `It stays in ${cloud}, still billed.`
  const typedOk = !inCloud || typedNameMatches(typed, store.name)
  return (
    <FormDialog
      title={`Delete ${store.name}?`}
      submitLabel="Delete"
      destructive
      onSubmit={() => onDelete(offered && inCloud)}
      onClose={onClose}
      submitDisabled={!typedOk}
      submitDisabledReason={typedOk ? undefined : `Type ${store.name} to confirm`}
      maxWidth="xs"
      testId="nodelink-store-delete-dialog"
      submitTestId="nodelink-store-delete-confirm"
    >
      <Typography variant="body2" data-testid="nodelink-store-delete-message">{message}</Typography>
      {offered && (
        <FormControlLabel
          sx={{ mt: 1, alignItems: 'flex-start', '& .MuiFormControlLabel-label': { fontSize: '0.875rem', pt: 1 } }}
          control={(
            <Checkbox checked={inCloud} onChange={e => setInCloud(e.target.checked)} data-testid="nodelink-store-delete-in-cloud" />
          )}
          label={`Also delete the ${object} in ${cloud}`}
        />
      )}
      {offered && inCloud && (
        <Box sx={{ mt: 1.5 }}>
          <TypedNameConfirm name={store.name} value={typed} onChange={setTyped} action="the delete" testId="nodelink-store-delete-typed" />
        </Box>
      )}
    </FormDialog>
  )
}
