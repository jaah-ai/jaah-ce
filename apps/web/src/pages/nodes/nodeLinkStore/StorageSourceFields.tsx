import { type JSX, useEffect, useState } from 'react'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail } from '../../../api'
import { CLOUD_SWITCH, planSwitchOn } from '../../../lib/planSwitches'
import { readJsonObject, removeKey, writeJson } from '../../../lib/safeStorage'
import { useAuth } from '../../../auth'
import Banner from '../../../ui/Banner'
import { type Choice, ChoiceField } from '../../../ui/ChoiceField'
import CredentialSelect from '../../../ui/CredentialSelect'
import FormField from '../../../ui/FormField'
import FormTextField from '../../../ui/FormTextField'
import { TEXT_INPUT } from '../../../ui/formDensity'
import { fieldTapTargetSx, ID_SX } from '../../../ui/listStyles'
import type { CloudProvider, NodeLinkStore } from '../../../types'
import { CLOUD_CONNECTIONS_URL, listRegions } from '../../settings/cloudConnections/api'
import {
  CLOUD_STORAGES_KEY,
  type CreateStoreBody,
  type ExistingOptions,
  fetchAwsCredentialOptions,
  listCloudStorages,
  listConnectionExisting,
  listExistingStores,
  type NodeLinkStorageBody,
  resolveAwsCredentialOption,
} from './api'

/* WHERE A NODELINK LIVES — the one set of storage-source controls (the Storage tab's, and every later surface
 * that chooses a NodeLink's storage), two radio groups: the storage — one already in Jaah (another NodeLink's), a
 * new EFS the control plane creates, an EFS the org already has, or the operator's (Jaah-managed: no AWS field,
 * no access-point choice), and per cloud on the org's plan a new or existing GCP Filestore / Azure Files (NFS), placed
 * by one of the org's cloud connections — and on it an access point Jaah creates or an existing one (EFS only). Controlled: the caller holds the `StorageSourceValue`, turns it into the
 * `PUT …/{id}/storage` body with `storageBody`, and decides when to save. The AWS reads it needs (the org's
 * EFS objects, its AWS credentials) are its own. `managed` offers the Jaah-managed source only when
 * `available` — absent, not disabled, otherwise. */

export type CloudStorageSource = 'gcp-create' | 'gcp-existing' | 'azure-create' | 'azure-existing'
export type StorageSource = 'managed' | 'store' | 'create' | 'existing' | CloudStorageSource
export type ApMode = 'own' | 'adopt'

/** Storage on a cloud connection: `existing` `''` until picked (the existing sources only). */
export type CloudDraft = { name: string; connection_id: string; existing: string }

type CloudKey = 'gcp' | 'azure'
const CLOUD_SOURCES: Record<CloudStorageSource, { provider: CloudKey; adopt: boolean }> = {
  'gcp-create': { provider: 'gcp', adopt: false },
  'gcp-existing': { provider: 'gcp', adopt: true },
  'azure-create': { provider: 'azure', adopt: false },
  'azure-existing': { provider: 'azure', adopt: true },
}
const cloudOf = (source: StorageSource) => (source in CLOUD_SOURCES ? CLOUD_SOURCES[source as CloudStorageSource] : null)
/** Where Jaah creates new storage on each cloud: the source a NodeLink whose storage failed starts again from. */
export const CREATE_SOURCE: Record<CloudProvider, StorageSource> = { aws: 'create', gcp: 'gcp-create', azure: 'azure-create' }
/** The cloud a source's storage lives on; null for one the org does not place (Storage in Jaah, Jaah-managed). */
const providerOf = (source: StorageSource): CloudProvider | null =>
  source === 'create' || source === 'existing' ? 'aws' : cloudOf(source)?.provider ?? null
/** What each cloud's existing object is called, in its picker and its missing-field word. */
const EXISTING_NOUN: Record<CloudKey, string> = { gcp: 'Filestore instance', azure: 'File share' }

/* `mount_target_id` `''` = none, the select's own value for it; sent omitted. */
export type ExistingDraft = { name: string; credential_id: string; region: string; file_system_id: string; mount_target_id: string }

export type StorageSourceValue = {
  source: StorageSource
  /** The picked store under "Storage in Jaah". */
  storeId: string
  create: CreateStoreBody
  existing: ExistingDraft
  cloud: CloudDraft
  apMode: ApMode
  /** An access point picked from the ones AWS listed for the current file system; `''` = none. */
  apId: string
}

/** A fresh value: every field blank, the new storage named after `name`. */
export function emptyStorageSource(name: string, source: StorageSource, adopted = false): StorageSourceValue {
  return {
    source,
    storeId: '',
    create: { name, credential_id: '', region: '', subnet_id: '', ingress_cidr: '' },
    existing: { name, credential_id: '', region: '', file_system_id: '', mount_target_id: '' },
    cloud: { name, connection_id: '', existing: '' },
    apMode: adopted ? 'adopt' : 'own',
    apId: '',
  }
}

/* Each field's label as the form draws it, so a missing-field sentence names what the user sees. */
const CREATE_LABELS: Record<keyof CreateStoreBody, string> = {
  name: 'Storage name', credential_id: 'AWS credential', region: 'Region', subnet_id: 'Subnet', ingress_cidr: 'Allowed CIDR',
}

/** The labels of the fields `value` still needs, in form order; empty once `storageBody` can send it. */
export function storageMissing(value: StorageSourceValue): string[] {
  if (value.source === 'managed') return []
  const missing: string[] = []
  if (value.source === 'store' && !value.storeId) missing.push(STORE_LABEL)
  if (value.source === 'create') {
    for (const [key, label] of Object.entries(CREATE_LABELS)) {
      if (value.create[key as keyof CreateStoreBody].trim() === '') missing.push(label)
    }
  }
  if (value.source === 'existing') {
    const x = value.existing
    if (!x.name.trim()) missing.push('Storage name')
    if (!x.credential_id) missing.push('AWS credential')
    if (!x.region.trim()) missing.push('Region')
    if (!x.file_system_id) missing.push('File system')
  }
  const cloud = cloudOf(value.source)
  if (cloud) {
    if (!value.cloud.name.trim()) missing.push('Storage name')
    if (!value.cloud.connection_id) missing.push('Connection')
    if (cloud.adopt && !value.cloud.existing) missing.push(EXISTING_NOUN[cloud.provider])
  }
  if (hasAccessPointChoice(value.source) && value.apMode === 'adopt' && value.apId === '') missing.push('Access point')
  return missing
}

/** The line beside a disabled save naming `labels`, the fields it still needs: one wording on every NodeLink form. */
export const missingText = (labels: readonly string[]) => `Missing: ${labels.join(', ')}`

/** The `PUT …/storage` body `value` makes, or null while it is incomplete (`storageMissing`). */
export function storageBody(value: StorageSourceValue): NodeLinkStorageBody | null {
  if (storageMissing(value).length > 0) return null
  if (value.source === 'managed') return { managed: true }
  const adoptPart = hasAccessPointChoice(value.source) && value.apMode === 'adopt' ? { access_point_id: value.apId } : {}
  if (value.source === 'store') return { store_id: value.storeId, ...adoptPart }
  const cloud = cloudOf(value.source)
  if (cloud) {
    const c = value.cloud
    return {
      connection: { name: c.name.trim(), connection_id: c.connection_id, ...(cloud.adopt ? { existing: c.existing } : {}) },
    }
  }
  if (value.source === 'create') {
    return { create: Object.fromEntries(Object.entries(value.create).map(([k, v]) => [k, v.trim()])) as CreateStoreBody }
  }
  const x = value.existing
  return {
    existing: {
      name: x.name.trim(), credential_id: x.credential_id, region: x.region.trim(), file_system_id: x.file_system_id,
      ...(x.mount_target_id ? { mount_target_id: x.mount_target_id } : {}),
    },
    ...adoptPart,
  }
}

/** `choices` with `provider`'s own sources first, each group in its own order; null leaves them as they are. */
function leading(provider: CloudProvider | null, choices: Choice<StorageSource>[]): Choice<StorageSource>[] {
  if (!provider) return choices
  const own = (c: Choice<StorageSource>) => providerOf(c.value) === provider
  return [...choices.filter(own), ...choices.filter(c => !own(c))]
}

/** A new AWS EFS has no access point yet, and Jaah-managed's is always Jaah's: only these two choose. */
const hasAccessPointChoice = (source: StorageSource) => source === 'store' || source === 'existing'

/* The picker under "Storage in Jaah" is named for it, and so is its missing-field word. */
const STORE_LABEL = 'Storage in Jaah'

/** The two AWS EFS sources, worded once: Settings › NodeLink locations offers the same pair. */
export const NEW_EFS = { label: 'New AWS EFS', hint: 'Jaah creates a file system in AWS.' }
export const EXISTING_EFS = { label: 'Existing AWS EFS', hint: 'A file system you already have in AWS.' }

const MANAGED_SOURCE: Choice<StorageSource> = {
  value: 'managed', label: 'Jaah-managed', hint: 'Jaah runs it. Nothing to set up.', testId: 'nodelink-storage-source-managed',
}

const SOURCES: Choice<StorageSource>[] = [
  { value: 'store', label: STORE_LABEL, hint: 'Share a storage another NodeLink uses.', testId: 'nodelink-storage-source-store' },
  { value: 'create', ...NEW_EFS, testId: 'nodelink-storage-source-create' },
  { value: 'existing', ...EXISTING_EFS, testId: 'nodelink-storage-source-existing' },
]

/** The four cloud-connection sources, each offered only while its cloud is on the org's plan. */
const CLOUD_CHOICES: Choice<StorageSource>[] = [
  { value: 'gcp-create', label: 'New GCP Filestore', hint: 'Jaah creates a Filestore in GCP.', testId: 'nodelink-storage-source-gcp-create' },
  { value: 'gcp-existing', label: 'Existing GCP Filestore', hint: 'A Filestore you already have in GCP.', testId: 'nodelink-storage-source-gcp-existing' },
  { value: 'azure-create', label: 'New Azure Files (NFS)', hint: 'Jaah creates a share per NodeLink in Azure.', testId: 'nodelink-storage-source-azure-create' },
  { value: 'azure-existing', label: 'Existing Azure Files (NFS)', hint: 'A share you already have in Azure.', testId: 'nodelink-storage-source-azure-existing' },
]

const AP_MODES: Choice<ApMode>[] = [
  { value: 'own', label: 'New', hint: 'Jaah creates one.', testId: 'nodelink-ap-own' },
  { value: 'adopt', label: 'Existing', hint: 'Pick one on this file system.', testId: 'nodelink-ap-adopt' },
]

/* Region is typed: wait for a pause before asking AWS. */
const LOOKUP_DELAY_MS = 400

const GRID_SX = {
  display: 'grid',
  gridTemplateColumns: { xs: '1fr', md: 'repeat(2, minmax(0, 1fr))' },
  gap: 1.5,
} as const
/* Storage name, the odd one of five, takes a row of its own, so the four after it pair up. */
const FULL_ROW_SX = { gridColumn: '1 / -1' } as const

const named = (id: string, name: string | null) => (name ? `${id} · ${name}` : id)

/** A row in the "Storage in Jaah" picker. */
const storeLabel = (s: NodeLinkStore) => (s.file_system_id ? `${s.name} · ${s.file_system_id}` : `${s.name} · ${s.status}`)

/** Where Add secret lands, preselected to AWS; it comes back to `here` (`CredentialEditPage`'s `return`). */
export const addAwsCredentialUrl = (here: string) => `/credentials/new?type=aws&return=${encodeURIComponent(here)}`

/* THE DRAFT ACROSS ADD CREDENTIAL: leaving for `/credentials/new` unmounts the form, so the value is kept in
   session storage under the page it returns to, and that page's owner reads it back on mount
   (`readStorageDraft`) and then clears it. One draft at a time: a later leave replaces it; one never
   collected expires (`STORAGE_DRAFT_TTL_MS`) so a much later visit starts fresh. */
const STORAGE_DRAFT_KEY = 'nodelink-storage-draft'
const STORAGE_DRAFT_TTL_MS = 30 * 60_000

/** Keep `value` for `here` (the path and query Add secret returns to). */
export function keepStorageDraft(here: string, value: StorageSourceValue): void {
  writeJson('session', STORAGE_DRAFT_KEY, { here, at: Date.now(), value })
}

const strings = <T extends Record<string, string>>(base: T, kept: unknown): T => {
  const from = typeof kept === 'object' && kept !== null ? kept as Record<string, unknown> : {}
  return Object.fromEntries(Object.entries(base).map(([k, v]) => [k, typeof from[k] === 'string' ? from[k] : v])) as T
}

/** The draft kept for `here`, or null (none, another page's, expired, or unreadable). Does not clear it. Rebuilt
 *  over a blank value, so a draft another build wrote never leaves a field undefined. */
export function readStorageDraft(here: string): StorageSourceValue | null {
  const kept = readJsonObject('session', STORAGE_DRAFT_KEY)
  if (!kept || kept.here !== here || typeof kept.at !== 'number' || Date.now() - kept.at > STORAGE_DRAFT_TTL_MS) return null
  const value = typeof kept.value === 'object' && kept.value !== null ? kept.value as Record<string, unknown> : null
  const source = value?.source
  if (source !== 'managed' && source !== 'store' && source !== 'create' && source !== 'existing'
    && !(typeof source === 'string' && source in CLOUD_SOURCES)) return null
  const blank = emptyStorageSource('', source as StorageSource)
  return {
    source: source as StorageSource,
    storeId: typeof value?.storeId === 'string' ? value.storeId : '',
    create: strings(blank.create, value?.create),
    existing: strings(blank.existing, value?.existing),
    cloud: strings(blank.cloud, value?.cloud),
    apMode: value?.apMode === 'adopt' ? 'adopt' : 'own',
    apId: typeof value?.apId === 'string' ? value.apId : '',
  }
}

export function clearStorageDraft(): void {
  removeKey('session', STORAGE_DRAFT_KEY)
}

/** The path and query a page is at: what Add secret returns to, and the draft's key. */
export const herePath = (location: { pathname: string; search: string }) => `${location.pathname}${location.search}`

/** The org's AWS credentials, read once per mount: the picker itself only searches as the user types. */
const AWS_CREDENTIALS_KEY = ['nodelink-aws-credentials']

/* The AWS credential picker, and an inline way to add one when the org has none. With exactly one, it is
   picked for the user (the only choice, and the one just added on returning from Add secret). */
export function AwsCredentialField({ id, value, onPick, disabled, onLeave }: {
  id: string
  value: string
  onPick: (next: string) => void
  disabled: boolean
  /** Called as Add secret is followed, to keep what the form holds. */
  onLeave?: () => void
}): JSX.Element {
  const location = useLocation()
  const credentials = useQuery({
    queryKey: AWS_CREDENTIALS_KEY,
    queryFn: ({ signal }) => fetchAwsCredentialOptions('', signal),
    // Coming back from Add secret is a remount: read again, whatever the cache's age.
    refetchOnMount: 'always',
  })
  const rows = credentials.data
  const none = rows !== undefined && rows.length === 0
  const only = rows !== undefined && rows.length === 1 && !rows[0].disabled ? rows[0] : null
  useEffect(() => {
    if (only && value === '') onPick(only.id)
    // Once per roster read: a user who clears it keeps it cleared.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [only?.id])
  return (
    <FormField
      label="AWS credential"
      hint={none && (
        <Box component="span" data-testid={`${id}-none`}>
          No AWS credential.{' '}
          {/* A half-width column would otherwise break the link's two words apart. */}
          <Link component={RouterLink} to={addAwsCredentialUrl(herePath(location))} onClick={onLeave}
            data-testid={`${id}-add`} sx={{ whiteSpace: 'nowrap' }}>
            Add secret
          </Link>
        </Box>
      )}
    >
      <CredentialSelect
        id={id}
        value={value === '' ? null : value}
        onChange={next => onPick(next ?? '')}
        fetchOptions={fetchAwsCredentialOptions}
        resolveOption={resolveAwsCredentialOption}
        selected={only}
        disabled={disabled}
        placeholder="Search AWS credentials"
        noOptionsText="No AWS credential matches"
      />
    </FormField>
  )
}

/** The AWS region catalogue (static), cached under the key Cloud connections' reads share. */
const REGIONS_KEY = ['cloud-connection-regions']

/** Region, picked from the AWS catalogue on every NodeLink storage form. A stored or prefilled code the catalogue
 *  lacks still shows. Unreadable catalogue: the code is typed instead — silently where the caller may not read it
 *  (an editor who is not an org admin), with the failure as its error otherwise. No hint row while it reads: a hint under one
 *  cell of a two-column grid offsets the row beside it. */
export function RegionField({ id, value, onChange, disabled }: {
  id: string
  value: string
  onChange: (next: string) => void
  disabled: boolean
}): JSX.Element {
  const regions = useQuery({ queryKey: REGIONS_KEY, queryFn: listRegions, staleTime: Infinity, retry: false })
  if (regions.isError) {
    const forbidden = regions.error instanceof ApiError && regions.error.status === 403
    return (
      <FormTextField label="Region" id={id} value={value} onChange={onChange} disabled={disabled} placeholder="us-east-1"
        error={forbidden ? undefined : apiDetail(regions.error, 'Could not load the regions.')} slotProps={TEXT_INPUT} />
    )
  }
  const options = [
    { value: '', label: regions.isPending ? 'Loading…' : 'Choose a region' },
    ...(value && !regions.data?.some(r => r.code === value) ? [{ value, label: value }] : []),
    ...(regions.data ?? []).map(r => ({ value: r.code, label: `${r.code} — ${r.name}` })),
  ]
  return <FormTextField label="Region" id={id} value={value} onChange={onChange} disabled={disabled} options={options} />
}

/** What AWS lists for `credential` in `region` (file systems, access points, mount targets), read after a pause in
 *  typing and only while `wanted` with both set; any change drops the last answer. */
export function useExistingOptions(credential: string, region: string, wanted: boolean): {
  options: ExistingOptions | null
  error: string | null
  loading: boolean
} {
  const [options, setOptions] = useState<ExistingOptions | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const ready = wanted && credential !== '' && region !== ''
  useEffect(() => {
    setOptions(null)
    setError(null)
    if (!ready) return
    let live = true
    setLoading(true)
    const timer = window.setTimeout(() => {
      listExistingStores(credential, region)
        .then(found => { if (live) setOptions(found) })
        .catch(err => { if (live) setError(apiDetail(err, 'Could not read EFS from AWS.')) })
        .finally(() => { if (live) setLoading(false) })
    }, LOOKUP_DELAY_MS)
    return () => {
      live = false
      window.clearTimeout(timer)
      setLoading(false)
    }
  }, [ready, credential, region])
  return { options, error, loading }
}

export default function StorageSourceFields({
  orgStores,
  value,
  onChange,
  managed,
  disabled = false,
  keepDraft = false,
  onLeave: onLeaveOwner,
  ownerNamesStorage = false,
  preferProvider = null,
}: {
  /** The org's stores (`GET /api/org/nodelink-stores`); "Storage in Jaah" is offered only when there is one. */
  orgStores: readonly NodeLinkStore[]
  value: StorageSourceValue
  onChange: (next: StorageSourceValue) => void
  /** The Jaah-managed source: offered when `available`, in `location`. Null: not offered here. */
  managed: { available: boolean; location: string } | null
  disabled?: boolean
  /** Keep the value across Add secret (`keepStorageDraft`); the owner reads it back with `readStorageDraft`. */
  keepDraft?: boolean
  /** Called as Add secret is followed. A dialog closes here: the credential form opens in the side panel over
   *  the same page, so a modal left open would cover it. */
  onLeave?: () => void
  /** The owner sets the new storage's name (the Share dialog: the NodeLink's own), so Storage name is not drawn. */
  ownerNamesStorage?: boolean
  /** This cloud's sources lead the list (a failed NodeLink re-choosing: its own storage's cloud). */
  preferProvider?: CloudProvider | null
}): JSX.Element {
  const { source, existing, apMode } = value
  const location = useLocation()
  const onLeave = keepDraft || onLeaveOwner
    ? () => {
      if (keepDraft) keepStorageDraft(herePath(location), value)
      onLeaveOwner?.()
    }
    : undefined
  const pickedStore = orgStores.find(s => s.id === value.storeId) ?? null
  // A store being deleted takes no NodeLink (the server refuses it): offered only while it is the pick already.
  const offeredStores = orgStores.filter(s => s.status !== 'deleting' || s.id === value.storeId)
  const { admin } = useAuth()
  const cloudsOn = (['gcp', 'azure'] as const).filter(p => planSwitchOn(admin, CLOUD_SWITCH[p]))
  const cloudChoices = CLOUD_CHOICES.filter(c => cloudsOn.includes(CLOUD_SOURCES[c.value as CloudStorageSource].provider))

  /* Any change to where AWS is read voids an access point picked from the old listing. */
  const set = (patch: Partial<StorageSourceValue>) => onChange({ ...value, ...patch })

  /* ── The org's EFS objects (Existing AWS EFS; or an existing access point) ── */
  // Storage on GCP or Azure has no access point: only an EFS one offers the choice.
  const pickedEfs = source !== 'store' || (pickedStore?.provider ?? 'aws') === 'aws'
  const adopting = hasAccessPointChoice(source) && pickedEfs && apMode === 'adopt'
  const lookup = source === 'existing'
    ? { credential: existing.credential_id, region: existing.region.trim(), fs: existing.file_system_id }
    : source === 'store' && pickedStore
      ? { credential: pickedStore.credential_id, region: pickedStore.region, fs: pickedStore.file_system_id ?? '' }
      : null
  const wantsOptions = lookup !== null && (source === 'existing' || adopting)
  const { options, error: optionsError, loading: optionsLoading } = useExistingOptions(
    lookup?.credential ?? '', lookup?.region ?? '', wantsOptions,
  )

  const fileSystems = options?.file_systems ?? []
  const fsId = lookup?.fs ?? ''
  const accessPoints = (options?.access_points ?? []).filter(ap => ap.file_system_id === fsId)
  const mountTargets = (options?.mount_targets ?? []).filter(mt => mt.file_system_id === existing.file_system_id)
  // A pick no longer listed (a new file system) is no pick.
  const chosenAp = accessPoints.some(ap => ap.id === value.apId && ap.usable) ? value.apId : ''

  const setX = (key: keyof ExistingDraft, next: string) => {
    const draft = { ...existing, [key]: next }
    // A new file system, or a new place to look, voids the picks that hung off the old one.
    if (key === 'file_system_id' && next !== existing.file_system_id) draft.mount_target_id = ''
    if ((key === 'credential_id' || key === 'region') && next !== existing[key]) {
      draft.file_system_id = ''
      draft.mount_target_id = ''
    }
    set({ existing: draft, ...(key === 'name' ? {} : { apId: '' }) })
  }

  const setD = (key: keyof CreateStoreBody, next: string) => set({ create: { ...value.create, [key]: next } })

  return (
    <Box component="section" aria-label="Choose storage" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <ChoiceField<StorageSource>
        label="Storage"
        value={source}
        options={leading(preferProvider, [
          ...(managed?.available ? [MANAGED_SOURCE] : []),
          ...(offeredStores.length > 0 ? SOURCES : SOURCES.filter(s => s.value !== 'store')),
          ...cloudChoices,
        ])}
        // Another cloud's connection or object is no pick here.
        onChange={next => set({ source: next, apId: '', cloud: { ...value.cloud, connection_id: '', existing: '' } })}
        testId="nodelink-storage-source"
        disabled={disabled}
        row
      />

      {source === 'store' && (
        <FormField label={STORE_LABEL}>
          <TextField
            id="nodelink-storage-store"
            select
            value={value.storeId}
            onChange={e => {
              const next = orgStores.find(st => st.id === e.target.value)
              set({ storeId: e.target.value, apId: '', ...((next?.provider ?? 'aws') === 'aws' ? {} : { apMode: 'own' }) })
            }}
            disabled={disabled}
            sx={fieldTapTargetSx}
          >
            {offeredStores.map(s => <MenuItem key={s.id} value={s.id} sx={ID_SX}>{storeLabel(s)}</MenuItem>)}
          </TextField>
        </FormField>
      )}

      {source === 'create' && (
        <>
          <Box sx={GRID_SX}>
            {!ownerNamesStorage && (
              <Box sx={FULL_ROW_SX}>
                <FormTextField label="Storage name" id="nodelink-create-name" value={value.create.name}
                  onChange={v => setD('name', v)} disabled={disabled} slotProps={TEXT_INPUT} />
              </Box>
            )}
            <AwsCredentialField id="nodelink-create-credential" value={value.create.credential_id}
              onPick={v => setD('credential_id', v)} disabled={disabled} onLeave={onLeave} />
            <RegionField id="nodelink-create-region" value={value.create.region} onChange={v => setD('region', v)} disabled={disabled} />
            <FormTextField label="Subnet" id="nodelink-create-subnet" value={value.create.subnet_id}
              onChange={v => setD('subnet_id', v)} disabled={disabled} hint="e.g. subnet-0abc1234" slotProps={TEXT_INPUT} />
            <FormTextField label="Allowed CIDR" id="nodelink-create-cidr" value={value.create.ingress_cidr}
              onChange={v => setD('ingress_cidr', v)} disabled={disabled} hint="e.g. 10.0.0.0/16" slotProps={TEXT_INPUT} />
          </Box>
          {/* THE ONE LOAD-BEARING CAVEAT: nothing here creates that route, and without it no node mounts. */}
          <Typography variant="caption" data-testid="nodelink-network-hint" sx={{ color: 'text.secondary' }}>
            Your network must route your nodes to the mount target IP.
          </Typography>
        </>
      )}

      {source === 'existing' && (
        <Box sx={GRID_SX}>
          {!ownerNamesStorage && (
            <Box sx={FULL_ROW_SX}>
              <FormTextField label="Storage name" id="nodelink-existing-name" value={existing.name}
                onChange={v => setX('name', v)} disabled={disabled} slotProps={TEXT_INPUT} />
            </Box>
          )}
          <AwsCredentialField id="nodelink-existing-credential" value={existing.credential_id}
            onPick={v => setX('credential_id', v)} disabled={disabled} onLeave={onLeave} />
          <RegionField id="nodelink-existing-region" value={existing.region} onChange={v => setX('region', v)} disabled={disabled} />
          <FormField label="File system">
            <TextField
              id="nodelink-existing-fs"
              select
              value={fileSystems.some(fs => fs.id === existing.file_system_id) ? existing.file_system_id : ''}
              onChange={e => setX('file_system_id', e.target.value)}
              disabled={disabled || fileSystems.length === 0}
              sx={fieldTapTargetSx}
            >
              {fileSystems.map(fs => <MenuItem key={fs.id} value={fs.id} sx={ID_SX}>{named(fs.id, fs.name)}</MenuItem>)}
            </TextField>
          </FormField>
          <FormField label="Mount target">
            <TextField
              id="nodelink-existing-mt"
              select
              value={mountTargets.some(mt => mt.id === existing.mount_target_id) ? existing.mount_target_id : ''}
              onChange={e => setX('mount_target_id', e.target.value)}
              disabled={disabled || existing.file_system_id === ''}
              sx={fieldTapTargetSx}
              slotProps={{ select: { displayEmpty: true }, inputLabel: { shrink: true } }}
            >
              <MenuItem value="">None — resolved by DNS</MenuItem>
              {mountTargets.map(mt => (
                <MenuItem key={mt.id} value={mt.id} sx={ID_SX}>
                  {mt.availability_zone ? `${mt.ip} · ${mt.availability_zone}` : mt.ip}
                </MenuItem>
              ))}
            </TextField>
          </FormField>
        </Box>
      )}

      {cloudOf(source) && (
        <CloudStorageFields key={source} value={value.cloud} onChange={cloud => set({ cloud })} source={cloudOf(source)!}
          disabled={disabled} ownerNamesStorage={ownerNamesStorage} />
      )}

      {/* The access point: a new AWS EFS has none yet, so Jaah makes one there; so does Jaah-managed. */}
      {hasAccessPointChoice(source) && pickedEfs && (
        <>
          <ChoiceField<ApMode>
            label="Access point"
            value={apMode}
            options={AP_MODES}
            onChange={next => set({ apMode: next, apId: '' })}
            testId="nodelink-ap-mode"
            disabled={disabled}
            row
          />
          {apMode === 'adopt' && (
            <FormField label="Access point">
              <TextField
                id="nodelink-ap-pick"
                select
                value={chosenAp}
                onChange={e => set({ apId: e.target.value })}
                disabled={disabled || accessPoints.length === 0}
                sx={fieldTapTargetSx}
              >
                {accessPoints.map(ap => (
                  <MenuItem key={ap.id} value={ap.id} disabled={!ap.usable} sx={{ display: 'block' }} data-testid={`nodelink-ap-${ap.id}`}>
                    <Box component="span" sx={[...ID_SX, { display: 'block' }]}>{named(ap.id, ap.name)} · {ap.root_directory}</Box>
                    {ap.problem && (
                      <Typography component="span" variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                        {ap.problem}
                      </Typography>
                    )}
                  </MenuItem>
                ))}
              </TextField>
            </FormField>
          )}
        </>
      )}

      {optionsLoading && (
        <Typography variant="caption" data-testid="nodelink-existing-loading" sx={{ color: 'text.secondary' }}>Reading EFS…</Typography>
      )}
      {optionsError && <Banner severity="warning" testId="nodelink-existing-error">{optionsError}</Banner>}
      {options && source === 'existing' && fileSystems.length === 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>No file system in this region.</Typography>
      )}
    </Box>
  )
}

/* A GCP Filestore or Azure Files store: its name, the cloud connection that places it, and for an existing one the
   object to adopt. A new one says its minimum size and bill before Create (`min_size_note`, the server's). */
function CloudStorageFields({ value, onChange, source, disabled, ownerNamesStorage }: {
  value: CloudDraft
  onChange: (next: CloudDraft) => void
  source: { provider: CloudKey; adopt: boolean }
  disabled: boolean
  ownerNamesStorage: boolean
}): JSX.Element {
  const clouds = useQuery({ queryKey: CLOUD_STORAGES_KEY, queryFn: listCloudStorages, retry: false })
  const cloud = clouds.data?.find(c => c.provider === source.provider) ?? null
  const connections = cloud?.connections ?? []
  const only = connections.length === 1 ? connections[0].id : ''
  useEffect(() => {
    if (only && value.connection_id === '') onChange({ ...value, connection_id: only })
    // Once per listing: a user who clears it keeps it cleared.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [only])
  const existing = useQuery({
    queryKey: ['nodelink-connection-existing', value.connection_id],
    queryFn: () => listConnectionExisting(value.connection_id),
    enabled: source.adopt && value.connection_id !== '',
    retry: false,
  })
  const noun = EXISTING_NOUN[source.provider]
  const choices = existing.data ?? []
  useEffect(() => {
    // A pick this listing no longer offers usable (a kept draft, a share since changed) is no pick.
    if (existing.data && value.existing && !existing.data.some(c => c.id === value.existing && c.usable)) {
      onChange({ ...value, existing: '' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [existing.data])
  const label = cloud?.label ?? source.provider
  return (
    <>
      <Box sx={GRID_SX}>
        {!ownerNamesStorage && (
          <Box sx={FULL_ROW_SX}>
            <FormTextField label="Storage name" id="nodelink-cloud-name" value={value.name}
              onChange={v => onChange({ ...value, name: v })} disabled={disabled} slotProps={TEXT_INPUT} />
          </Box>
        )}
        <FormField
          label="Connection"
          hint={clouds.data && connections.length === 0 && (
            <Box component="span" data-testid="nodelink-cloud-connection-none">
              No {label} connection.{' '}
              <Link component={RouterLink} to={CLOUD_CONNECTIONS_URL} sx={{ whiteSpace: 'nowrap' }}>Add one</Link>
            </Box>
          )}
        >
          <TextField
            id="nodelink-cloud-connection"
            select
            value={connections.some(c => c.id === value.connection_id) ? value.connection_id : ''}
            onChange={e => onChange({ ...value, connection_id: e.target.value, existing: '' })}
            disabled={disabled || connections.length === 0}
            sx={fieldTapTargetSx}
          >
            {connections.map(c => <MenuItem key={c.id} value={c.id}>{`${c.name} · ${c.region}`}</MenuItem>)}
          </TextField>
        </FormField>
        {source.adopt && (
          <FormField label={noun}>
            <TextField
              id="nodelink-cloud-existing"
              select
              value={choices.some(c => c.id === value.existing && c.usable) ? value.existing : ''}
              onChange={e => onChange({ ...value, existing: e.target.value })}
              disabled={disabled || choices.length === 0}
              sx={fieldTapTargetSx}
            >
              {choices.map(c => (
                <MenuItem key={c.id} value={c.id} disabled={!c.usable} sx={{ display: 'block' }} data-testid={`nodelink-cloud-existing-${c.name}`}>
                  <Box component="span" sx={[...ID_SX, { display: 'block' }]}>{c.name}</Box>
                  {c.problem && (
                    <Typography component="span" variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                      {c.problem}
                    </Typography>
                  )}
                </MenuItem>
              ))}
            </TextField>
          </FormField>
        )}
      </Box>
      {!source.adopt && cloud?.min_size_note && (
        <Banner severity="info" testId="nodelink-cloud-min-size">{cloud.min_size_note}</Banner>
      )}
      {clouds.isError && <Banner severity="warning">{apiDetail(clouds.error, 'Could not load the connections.')}</Banner>}
      {existing.isError && <Banner severity="warning" testId="nodelink-cloud-existing-error">{apiDetail(existing.error, `Could not read ${label}.`)}</Banner>}
      {existing.isPending && existing.fetchStatus === 'fetching' && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>Reading {label}…</Typography>
      )}
      {existing.data && choices.length === 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>No {noun.toLowerCase()} in this region.</Typography>
      )}
    </>
  )
}
