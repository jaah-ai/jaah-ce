import { type JSX, useEffect, useState } from 'react'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { Mono } from '../../../ui/DetailFields'
import useMediaQuery from '@mui/material/useMediaQuery'
import { apiDetail } from '../../../api'
import { useAuth } from '../../../auth'
import type { CloudProvider } from '../../../types'
import { STATUS_TONE } from '../../../components/NodeLinkPicker'
import { useRowActions } from '../../../hooks/useBusyIds'
import Banner from '../../../ui/Banner'
import { DetailField } from '../../../ui/detail/DetailPage'
import { monoSx, PHONE_QUERY } from '../../../ui/listStyles'
import StatusChip from '../../../ui/StatusChip'
import {
  getManagedStorage,
  getNodeLink,
  getStoreNetwork,
  getStorePolicy,
  listExistingStores,
  listStores,
  MANAGED_LOCATION_LABEL,
  type NodeLinkDetail,
  type NodeLinkStatus,
  provisionNodeLink,
  refreshStore,
  repairStorePolicy,
  setNodeLinkStorage,
  setStoreCredential,
  setStoreMountTarget,
  sizeText,
  type StorePolicy,
} from './api'
import { PROVIDER_LABELS } from '../../settings/cloudConnections/api'
import { getLocationPolicy, listLocations, LOCATIONS_KEY, locationPolicyKey, locationUrl } from '../../settings/nodelinkLocations/api'
import PolicyWritten from './PolicyWritten'
import ProvisionProgress from './ProvisionProgress'
import { PolicyPanel, PolicyToggle, RepairedNote } from './StorePolicy'
import StorageSourceFields, {
  AwsCredentialField,
  clearStorageDraft,
  CREATE_SOURCE,
  emptyStorageSource,
  herePath,
  readStorageDraft,
  type StorageSourceValue,
  storageBody,
} from './StorageSourceFields'

/* A NodeLink's Storage tab. Until it has an access point, the admin chooses where it lives — the storage of
 * another NodeLink (Storage in Jaah), a new file system the control plane creates (New AWS EFS), or one the org
 * already has (Existing AWS EFS) — and its access point: one Jaah creates, or an existing one on that file system
 * (`StorageSourceFields`). Save (`PUT …/{id}/storage`), then Provision: a request the server finishes, its
 * progress line advancing as the editor polls. Once it has an access point the choice is fixed (moving it is
 * a delete and a new NodeLink); the tab shows the storage's facts with Refresh and Repair policy (which says how
 * many access points the rewritten policy admits), and Show policy reads the file system's policy live from AWS
 * (`GET …/policy`), re-read by either button while open (`StorePolicy.tsx`). That read runs on open too: its Policy
 * fact says whether the live policy is current (the one Jaah would write now), and Policy written names the last
 * policy Jaah wrote — kept even when the request that wrote it failed after. Change re-picks the storage's AWS
 * credential (`PATCH …/nodelink-stores/{id}`), which the server checks sees the file system; on adopted storage the
 * Mount target IP's Change picks another of the file system's mount targets, or none (same PATCH).
 * JAAH-MANAGED (the operator's storage, offered when `GET …/managed-storage` says so): Save starts provisioning
 * itself, and its facts are the NodeLink's own only — no AWS id of the operator's reaches this tab. THE OPERATOR
 * alone also gets Show policy there, read through its location (`GET /api/operator/nodelink-locations/…/policy`),
 * and its Storage fact links to the location's card in Settings > NodeLink storage, where that store's facts and
 * Repair policy live. `orgId`: the operator's read-only view of another org's NodeLink reads that org's stores. */

type Busy = 'save' | 'provision' | 'refresh' | 'repair' | 'credential' | 'mount-target'

/** Adopted storage's first NodeLink rewrites the file system's policy (`nodelinks.write_policy`). */
export const ADOPTED_POLICY_WARNING =
  'Jaah will own this file system’s policy, replacing your own statements. The AWS credential needs ' +
  'CreateAccessPoint, DeleteAccessPoint, DescribeAccessPoints, PutFileSystemPolicy and TagResource ' +
  '(DescribeFileSystemPolicy to show the policy here).'

const STORES_KEY = ['nodelink-stores']
/** The org's stores' query key; the Nodes tab shares the read. */
export const storesKey = (orgId: string | undefined) => (orgId ? [...STORES_KEY, orgId] : STORES_KEY)
const policyKey = (storeId: string | undefined) => ['nodelink-store-policy', storeId]
const networkKey = (storeId: string | undefined) => ['nodelink-store-network', storeId]
/** The editor's own poll (`NodeLinkEditPage`'s `POLL_MS`). */
const POLL_MS = 15000

/** A store's facts in its own cloud's words: a Filestore instance or an Azure resource, not EFS's file
 *  system and mount target. */
const STORE_TERMS: Record<CloudProvider, { fileSystem: string; address: string; root: string }> = {
  aws: { fileSystem: 'File system', address: 'Mount target IP', root: 'Root' },
  gcp: { fileSystem: 'Filestore instance', address: 'IP address', root: 'Folder' },
  azure: { fileSystem: 'Azure resource', address: 'IP address', root: 'Folder' },
}

const FACTS_SX = { display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(3, minmax(0, 1fr))' }, gap: 1.5 } as const

/** The live policy against the one Jaah would write now; a failed read says why (AWS's text, a 502's detail). */
function PolicyState({ query }: { query: { data?: StorePolicy; isPending: boolean; isError: boolean; error: unknown } }): JSX.Element {
  if (query.isError) {
    return (
      <Box data-testid="nodelink-store-policy-state">
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>Unknown</Typography>
        <Typography variant="caption" component="div" data-testid="nodelink-store-policy-error"
          sx={{ color: 'error.main', overflowWrap: 'anywhere' }}>
          {apiDetail(query.error, 'Could not read the policy.')}
        </Typography>
      </Box>
    )
  }
  if (query.isPending || !query.data) return <Typography variant="body2" sx={{ color: 'text.secondary' }}>Reading…</Typography>
  const { current, reason } = query.data
  if (current === null) return <Typography variant="body2" data-testid="nodelink-store-policy-state">Not Jaah's yet</Typography>
  return (
    <Box data-testid="nodelink-store-policy-state">
      <StatusChip label={current ? 'Current' : 'Outdated'} tone={current ? 'success' : 'warning'} data-status={current ? 'current' : 'outdated'} />
      {reason && <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>{reason}</Typography>}
    </Box>
  )
}

export default function NodeLinkStorageTab({
  detail,
  canEdit,
  orgId,
  onChange,
}: {
  detail: NodeLinkDetail
  /** `credentials:edit` and the plan term. */
  canEdit: boolean
  /** Another org's NodeLink, viewed read-only by the operator. */
  orgId?: string
  onChange: (row: NodeLinkDetail) => void
}): JSX.Element {
  const queryClient = useQueryClient()
  const operator = useAuth().admin?.is_operator === true
  const provisioning = detail.status === 'provisioning'
  // The store's facts move with the server's passes too: re-read them at the editor's poll.
  const storesQuery = useQuery({
    queryKey: storesKey(orgId),
    queryFn: () => listStores(orgId),
    refetchInterval: provisioning ? POLL_MS : false,
  })
  const stores = storesQuery.data
  // A managed store is never in the org's list: nothing here reads it.
  const store = detail.store_managed ? null : stores?.find(s => s.id === detail.store_id) ?? null
  const { busy, error: actionError, setError, run } = useRowActions<Busy>()
  const busyNow = busy.size > 0
  // The last Repair's answer: how many access points the policy now admits.
  const [repaired, setRepaired] = useState<number | null>(null)
  const [showPolicy, setShowPolicy] = useState(false)
  // A managed store's location, for the operator's read of its policy; nobody else may list locations.
  const locationsQuery = useQuery({
    queryKey: LOCATIONS_KEY,
    queryFn: listLocations,
    enabled: operator && detail.store_managed && detail.store_id !== null,
  })
  const managedLocation = detail.store_managed
    ? locationsQuery.data?.find(row => row.store?.id === detail.store_id && row.store.file_system_id)?.location ?? null
    : null
  // An EFS store's own controls (policy, routing, Refresh, credential and mount target): GCP and Azure have none.
  const efs = store !== null && (store.provider ?? 'aws') === 'aws'
  // EFS storage, the store read or not (Jaah-managed is EFS too): what the policy words apply to.
  const provider = detail.store_provider ?? store?.provider ?? 'aws'
  const efsStorage = detail.store_managed || provider === 'aws'
  const terms = STORE_TERMS[store?.provider ?? 'aws']
  // Read on open (the route is `credentials:edit`): its Current / Outdated is a fact of the tab, not of Show policy.
  const policyQuery = useQuery({
    queryKey: policyKey(store?.id),
    queryFn: () => getStorePolicy(store?.id ?? ''),
    enabled: canEdit && efs && Boolean(store?.file_system_id),
    retry: false,
  })
  // Same gate as the policy read: an AWS read through the storage's credential. A failed read warns of nothing.
  const networkQuery = useQuery({
    queryKey: networkKey(store?.id),
    queryFn: () => getStoreNetwork(store?.id ?? ''),
    enabled: canEdit && efs && Boolean(store?.file_system_id),
    retry: false,
  })
  // The credential Change holds while open, '' until one is picked; null = closed.
  const [credential, setCredential] = useState<string | null>(null)
  // The mount target Change holds while open, '' = none; null = closed.
  const [mountTarget, setMountTarget] = useState<string | null>(null)
  const mountTargetsQuery = useQuery({
    queryKey: ['nodelink-store-mount-targets', store?.id],
    queryFn: () => listExistingStores(store?.credential_id ?? '', store?.region ?? ''),
    enabled: mountTarget !== null && store !== null,
    retry: false,
  })
  const mountTargets = (mountTargetsQuery.data?.mount_targets ?? []).filter(mt => mt.file_system_id === store?.file_system_id)
  // A phone shows the facts an admin acts on; the rest wait behind More.
  const phone = useMediaQuery(PHONE_QUERY)
  const [moreFacts, setMoreFacts] = useState(false)
  const folded = phone && !moreFacts

  /* The choice is open only before an access point exists (`PUT …/storage`'s own rule). */
  const choosing = detail.access_point_id === null && (detail.status === 'pending' || detail.status === 'failed')

  const managedQuery = useQuery({
    queryKey: ['nodelink-managed-storage', detail.id],
    queryFn: () => getManagedStorage(detail.id),
    enabled: choosing && canEdit,
  })
  // A failed read offers nothing: the other sources stand.
  const managed = managedQuery.data?.available
    ? { available: true, location: managedQuery.data.location ?? '' }
    : null
  const managedSettled = !managedQuery.isPending || !(choosing && canEdit)
  // Not offered where these nodes run (not set up, outside the org's data region, full): the server's why, which
  // names the region it would need. Nodes it could never serve say nothing.
  const managedWhyNot = managedQuery.data && !managedQuery.data.available && managedQuery.data.location !== null
    ? managedQuery.data.reason
    : null

  // `source` null = untouched: the saved store if any, else Jaah-managed when offered, else a storage in Jaah if
  // the org has one, else New AWS EFS.
  const orgStoreId = detail.store_managed ? null : detail.store_id
  // Back from Add credential: the draft kept as the user left (`keepStorageDraft`), an unsaved edit again.
  const location = useLocation()
  const [restored] = useState(() => readStorageDraft(herePath(location)))
  useEffect(() => { if (restored) clearStorageDraft() }, [restored])
  const [picked, setPicked] = useState<Omit<StorageSourceValue, 'source'> & { source: StorageSourceValue['source'] | null }>(
    () => restored
      ?? ({ ...emptyStorageSource(detail.name, 'create', detail.access_point_adopted), source: null, storeId: orgStoreId ?? '' }),
  )
  // The saved storage cannot be built on (being deleted, gone): start again from a new one on its own cloud. A failed
  // one stays chosen, so Retry runs it again with nothing to save (its own cloud's sources still list first).
  const savedStore = orgStoreId ? stores?.find(s => s.id === orgStoreId) : undefined
  const savedUnusable = orgStoreId !== null && stores !== undefined
    && (savedStore === undefined || savedStore.status === 'deleting')
  const untouched = detail.store_managed ? 'managed'
    : savedUnusable ? CREATE_SOURCE[provider]
      : detail.store_id ? 'store'
        : managed ? 'managed'
          : (stores?.length ?? 0) > 0 ? 'store' : 'create'
  const value: StorageSourceValue = {
    ...picked,
    source: picked.source ?? untouched,
    storeId: picked.storeId || orgStoreId || (stores?.[0]?.id ?? ''),
  }
  const pickedStore = stores?.find(s => s.id === value.storeId) ?? null
  // An edit since the last save: Provision waits for a save.
  const [dirty, setDirty] = useState(restored !== null)

  const pick = (next: StorageSourceValue) => {
    setPicked(next)
    setDirty(true)
    setError('')
  }

  const body = storageBody(value)

  /* Jaah takes over an adopted file system's policy on its first NodeLink there. */
  const policyTakeover = value.source === 'existing'
    || (value.source === 'store' && pickedStore?.adopted === true
      && pickedStore.nodelink_count - (pickedStore.id === detail.store_id ? 1 : 0) === 0)

  const reloadStores = () => queryClient.invalidateQueries({ queryKey: STORES_KEY })

  const save = () => run('save', 'Could not save. Check the fields and retry.', async () => {
    if (!body) return
    const saved = await setNodeLinkStorage(detail.id, body)
    // The saved choice is now an existing store: show it there, so a second Save cannot move the NodeLink.
    // A managed one is in no list: it stays Jaah-managed.
    await reloadStores()
    setPicked(prev => saved.store_managed
      ? { ...prev, source: 'managed', apId: '' }
      : { ...prev, source: 'store', storeId: saved.store_id ?? '', apId: '' })
    setDirty(false)
    onChange(saved)
  })

  /* A refused pass (a cloud error the route raises) still records the error and the steps on the server, and its
     answer carries neither: re-read the NodeLink then, so the line and the error show where it stands without a
     reload. A landed pass answers with the row itself; the editor polls on while it is `provisioning`. */
  const provision = () => run('provision', 'Could not provision. Retry.', async () => {
    try {
      onChange(await provisionNodeLink(detail.id))
    } catch (err) {
      await getNodeLink(detail.id, orgId).then(onChange, () => undefined)
      throw err
    } finally {
      await reloadStores()
    }
  })

  const reloadPolicy = () => Promise.all([
    queryClient.invalidateQueries({ queryKey: policyKey(store?.id) }),
    queryClient.invalidateQueries({ queryKey: networkKey(store?.id) }),
  ])

  const refresh = () => run('refresh', efs ? 'Could not read AWS. Retry.' : 'Could not refresh. Retry.', async () => {
    setRepaired(null)
    if (store) await refreshStore(store.id)
    await Promise.all([reloadStores(), reloadPolicy()])
  })

  const repair = () => run('repair', 'Could not repair the policy. Retry.', async () => {
    setRepaired(null)
    if (!store) return
    try {
      const out = await repairStorePolicy(store.id)
      setRepaired(out.access_point_count)
    } finally {
      // A failure after the write still changed the policy: show what was written and what AWS holds now. Settled,
      // so a failed re-read never replaces Repair's own error.
      await Promise.allSettled([reloadStores(), reloadPolicy()])
    }
  })

  const saveCredential = () => run('credential', 'Could not change the credential. Retry.', async () => {
    if (!store || !credential) return
    await setStoreCredential(store.id, credential)
    setCredential(null)
    await Promise.all([reloadStores(), reloadPolicy()])
  })

  const saveMountTarget = () => run('mount-target', 'Could not change the mount target. Retry.', async () => {
    if (!store || mountTarget === null) return
    await setStoreMountTarget(store.id, mountTarget)
    setMountTarget(null)
    await reloadStores()
  })

  const unfinished = ['pending', 'provisioning', 'failed'].includes(detail.status)
  // Only EFS has a file-system policy.
  const outOfPolicy = efsStorage && detail.status === 'ready' && !detail.in_policy
  const canProvision = canEdit && detail.store_id !== null && !dirty && (unfinished || outOfPolicy)

  return (
    <Box data-testid="nodelink-storage" sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 2, maxWidth: '72rem' }}>
      {detail.progress && <ProvisionProgress steps={detail.progress} provider={detail.store_managed ? 'aws' : detail.store_provider} />}
      {/* A `pending` row's error is what it waits for, not a failure. */}
      {detail.last_error && (
        <Banner severity={detail.status === 'pending' ? 'warning' : 'error'} testId="nodelink-last-error">{detail.last_error}</Banner>
      )}
      {outOfPolicy && <Banner severity="warning" testId="nodelink-policy">Not in the file-system policy.</Banner>}
      {actionError && <Banner severity="error" testId="nodelink-storage-error">{actionError}</Banner>}
      {storesQuery.isError && <Banner severity="error">{apiDetail(storesQuery.error, 'Could not load storage.')}</Banner>}

      {detail.store_managed && (
        <Box component="dl" data-testid="nodelink-store-facts" sx={FACTS_SX}>
          <DetailField label="Storage">
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75 }}>
              {operator && detail.store_location ? (
                <Link component={RouterLink} to={locationUrl(detail.store_location)} variant="body2" underline="hover"
                  data-testid="nodelink-store-location" sx={{ overflowWrap: 'anywhere' }}>
                  {detail.store_name ?? 'Jaah-managed'} · {MANAGED_LOCATION_LABEL[detail.store_location]}
                </Link>
              ) : (
                <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{detail.store_name ?? 'Jaah-managed'}</Typography>
              )}
              {detail.store_status && (
                <StatusChip label={detail.store_status} tone={STATUS_TONE[detail.store_status as NodeLinkStatus] ?? 'neutral'}
                  data-status={detail.store_status} />
              )}
            </Box>
          </DetailField>
          <DetailField label="Access point"><Mono block testId="nodelink-ap">{detail.access_point_id || '—'}</Mono></DetailField>
          <DetailField label="Root"><Mono block>{detail.root_directory || '—'}</Mono></DetailField>
          <DetailField label="In policy"><Typography variant="body2">{detail.in_policy ? 'Yes' : 'No'}</Typography></DetailField>
        </Box>
      )}

      {/* Right above the facts, whose first row holds Mount target IP and its Change (the fix): never below the fold. */}
      {networkQuery.data?.warning && <Banner severity="warning" testId="nodelink-store-network">{networkQuery.data.warning}</Banner>}
      {store && (
        <Box component="dl" data-testid="nodelink-store-facts" sx={FACTS_SX}>
          <DetailField label="Storage">
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75 }}>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{store.name}</Typography>
              <StatusChip label={store.status} tone={STATUS_TONE[store.status] ?? 'neutral'} data-status={store.status} />
            </Box>
          </DetailField>
          {!folded && <DetailField label={terms.fileSystem}><Mono block testId="nodelink-store-fs">{store.file_system_id || '—'}</Mono></DetailField>}
          <DetailField label={terms.address}>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 0.75 }}>
              {/* None: nodes find the file system by its DNS name (adopted storage may run without one). */}
              {!store.mount_target_ip && (store.adopted || store.status === 'ready')
                ? <Typography variant="body2" data-testid="nodelink-store-mount-ip">None — resolved by DNS</Typography>
                : <Mono block testId="nodelink-store-mount-ip">{store.mount_target_ip || '—'}</Mono>}
              {canEdit && efs && store.adopted && mountTarget === null && (
                <Button size="small" onClick={() => setMountTarget(store.mount_target_id ?? '')} disabled={busyNow}
                  data-testid="nodelink-store-mount-target-change" sx={{ minWidth: 0, py: 0 }}>
                  Change
                </Button>
              )}
            </Box>
          </DetailField>
          {!folded && <DetailField label="Region"><Mono block>{store.region || '—'}</Mono></DetailField>}
          {store.status === 'ready' && store.tier && <DetailField label="Tier"><Typography variant="body2" data-testid="nodelink-store-tier">{store.tier}</Typography></DetailField>}
          {store.status === 'ready' && store.capacity_gib != null && (
            <DetailField label="Size">
              <Typography variant="body2" data-testid="nodelink-store-size">{sizeText(store.capacity_gib)}</Typography>
              {store.cost_note && (
                <Typography variant="caption" component="div" data-testid="nodelink-store-cost" sx={{ color: 'text.secondary' }}>
                  {store.cost_note}
                </Typography>
              )}
            </DetailField>
          )}
          {/* Access points and the file-system policy are EFS's: a Filestore or Azure share has neither. */}
          {efs && (
            <DetailField label="Access point">
              <Mono block testId="nodelink-ap">{detail.access_point_id || '—'}</Mono>
              {detail.access_point_adopted && <Typography variant="caption" sx={{ color: 'text.secondary' }}>Existing</Typography>}
            </DetailField>
          )}
          {!folded && (
            <>
              <DetailField label={terms.root}><Mono block>{detail.root_directory || '—'}</Mono></DetailField>
              <DetailField label="Adopted"><Typography variant="body2">{store.adopted ? 'Yes' : 'No'}</Typography></DetailField>
              {efs && <DetailField label="In policy"><Typography variant="body2">{detail.in_policy ? 'Yes' : 'No'}</Typography></DetailField>}
            </>
          )}
          <DetailField label={`${PROVIDER_LABELS[store.provider ?? 'aws']} credential`}>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 0.75 }}>
              <Typography variant="body2" data-testid="nodelink-store-credential" sx={{ overflowWrap: 'anywhere' }}>
                {store.credential_name ?? '—'}
              </Typography>
              {canEdit && efs && credential === null && (
                <Button size="small" onClick={() => setCredential('')} disabled={busyNow}
                  data-testid="nodelink-store-credential-change" sx={{ minWidth: 0, py: 0 }}>
                  Change
                </Button>
              )}
            </Box>
          </DetailField>
          {canEdit && efs && store.file_system_id && <DetailField label="Policy"><PolicyState query={policyQuery} /></DetailField>}
          {!folded && store.policy_written_at && <DetailField label="Policy written"><PolicyWritten store={store} testId="nodelink-store-policy-written" /></DetailField>}
        </Box>
      )}
      {store && phone && (
        <Box>
          <Button size="small" onClick={() => setMoreFacts(open => !open)} aria-expanded={moreFacts}
            data-testid="nodelink-store-facts-more" sx={{ minWidth: 0 }}>
            {moreFacts ? 'Less' : 'More'}
          </Button>
        </Box>
      )}
      {store && credential !== null && (
        <Box data-testid="nodelink-store-credential-form" sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: 1 }}>
          <Box sx={{ flex: '1 1 20rem', maxWidth: '28rem', minWidth: 0 }}>
            <AwsCredentialField id="nodelink-store-credential-pick" value={credential} onPick={setCredential} disabled={busyNow} />
          </Box>
          <Button variant="contained" size="small" onClick={() => void saveCredential()}
            disabled={busyNow || credential === '' || credential === store.credential_id} data-testid="nodelink-store-credential-save">
            Save
          </Button>
          <Button size="small" onClick={() => { setCredential(null); setError('') }} disabled={busyNow}>Cancel</Button>
        </Box>
      )}
      {store && mountTarget !== null && (
        <Box data-testid="nodelink-store-mount-target-form" sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-end', gap: 1 }}>
          <TextField
            id="nodelink-store-mount-target-pick"
            select
            label="Mount target"
            size="small"
            value={mountTarget === '' || mountTargets.some(mt => mt.id === mountTarget) ? mountTarget : ''}
            onChange={e => setMountTarget(e.target.value)}
            disabled={busyNow || mountTargetsQuery.isPending}
            helperText={mountTargetsQuery.isError ? apiDetail(mountTargetsQuery.error, efs ? 'Could not read EFS from AWS.' : 'Could not read.') : undefined}
            error={mountTargetsQuery.isError}
            sx={{ flex: '1 1 20rem', maxWidth: '28rem', minWidth: 0 }}
            slotProps={{ select: { displayEmpty: true }, inputLabel: { shrink: true } }}
          >
            <MenuItem value="">None — resolved by DNS</MenuItem>
            {mountTargets.map(mt => (
              <MenuItem key={mt.id} value={mt.id} sx={monoSx}>
                {mt.availability_zone ? `${mt.ip} · ${mt.availability_zone}` : mt.ip}
              </MenuItem>
            ))}
          </TextField>
          <Button variant="contained" size="small" onClick={() => void saveMountTarget()}
            disabled={busyNow || mountTargetsQuery.isPending || mountTarget === (store.mount_target_id ?? '')}
            data-testid="nodelink-store-mount-target-save">
            Save
          </Button>
          <Button size="small" onClick={() => { setMountTarget(null); setError('') }} disabled={busyNow}>Cancel</Button>
        </Box>
      )}
      {store?.last_error && store.last_error !== detail.last_error && (
        <Banner severity="error" testId="nodelink-store-error">{store.last_error}</Banner>
      )}

      {choosing && canEdit && stores !== undefined && managedSettled && (
        <>
          <StorageSourceFields orgStores={stores} value={value} onChange={pick} managed={managed} disabled={busyNow} keepDraft
            preferProvider={orgStoreId ? provider : null} />
          {managedWhyNot && (
            <Typography variant="caption" component="div" data-testid="nodelink-managed-unavailable"
              sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
              {managedWhyNot}
            </Typography>
          )}
          {policyTakeover && <Banner severity="warning" testId="nodelink-adopted-warning">{ADOPTED_POLICY_WARNING}</Banner>}
        </>
      )}

      {canEdit && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
          {choosing && (
            <Button variant="contained" size="small" onClick={() => void save()} disabled={busyNow || body === null || (detail.store_id !== null && !dirty)} data-testid="nodelink-storage-save">
              Save
            </Button>
          )}
          {/* While `provisioning` the server runs every pass: nothing to press. */}
          {(choosing || unfinished || outOfPolicy) && detail.status !== 'deleting' && !provisioning && (
            <Button variant="outlined" size="small" onClick={() => void provision()} disabled={busyNow || !canProvision}
              data-testid="nodelink-provision">
              {detail.status === 'failed' || outOfPolicy ? 'Retry' : 'Provision'}
            </Button>
          )}
          {choosing && dirty && detail.store_id !== null && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>Save before provisioning.</Typography>
          )}
          {efs && store?.file_system_id && (
            <Button variant="outlined" size="small" onClick={() => void refresh()} disabled={busyNow} data-testid="nodelink-store-refresh">
              Refresh from AWS
            </Button>
          )}
          {efs && store?.status === 'ready' && detail.access_point_id !== null && (
            <Button variant="outlined" size="small" onClick={() => void repair()} disabled={busyNow} data-testid="nodelink-store-repair">
              Repair policy
            </Button>
          )}
          {efs && store?.file_system_id && (
            <PolicyToggle open={showPolicy} onToggle={() => setShowPolicy(open => !open)} testId="nodelink-store-policy-toggle" />
          )}
          {repaired !== null && <RepairedNote count={repaired} testId="nodelink-store-repaired" />}
        </Box>
      )}
      {/* The operator's only: a managed store's policy, by its location. Not gated on `canEdit`: it writes nothing. */}
      {managedLocation && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
          <PolicyToggle open={showPolicy} onToggle={() => setShowPolicy(open => !open)} testId="nodelink-store-policy-toggle" />
        </Box>
      )}

      {showPolicy && efs && store?.file_system_id && (
        <PolicyPanel queryKey={policyKey(store.id)} read={() => getStorePolicy(store.id)} testId="nodelink-store-policy" />
      )}
      {showPolicy && managedLocation && (
        <PolicyPanel queryKey={locationPolicyKey(managedLocation)} read={() => getLocationPolicy(managedLocation)}
          testId="nodelink-store-policy" />
      )}
    </Box>
  )
}
