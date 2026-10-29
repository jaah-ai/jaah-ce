import { type FormEvent, type JSX, useCallback, useState } from 'react'
import { Link as RouterLink, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { ApiError } from '../../../api'
import { useAuth, useCan } from '../../../auth'
import Breadcrumb from '../../../components/Breadcrumb'
import { STATUS_TONE } from '../../../components/NodeLinkPicker'
import { TabPanel, Tabs, type TabDef } from '../../../components/Tabs'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useEditRecord } from '../../../hooks/useEditRecord'
import { usePoll } from '../../../hooks/usePoll'
import { useSubmit } from '../../../hooks/useSubmit'
import { useUrlTab } from '../../../hooks/useUrlTab'
import { enterActAs } from '../../../lib/actAs'
import Banner from '../../../ui/Banner'
import { CopyButton } from '../../../ui/CopyButton'
import { DetailPage } from '../../../ui/detail/DetailPage'
import DetailStatus from '../../../ui/detail/DetailStatus'
import FormActionBar from '../../../ui/FormActionBar'
import FormTextField from '../../../ui/FormTextField'
import { FORM_SX, TEXT_INPUT } from '../../../ui/formDensity'
import { monoSx, tapTargetSx } from '../../../ui/listStyles'
import SavedNote from '../../../ui/SavedNote'
import StatusChip from '../../../ui/StatusChip'
import SubmitButton from '../../../ui/SubmitButton'
import {
  createNodeLink,
  defaultMountPath,
  deleteNodeLink,
  deleteNodeLinkAnyway,
  getNodeLink,
  listStores,
  NODELINK_STORE_URL,
  type NodeLinkBody,
  type NodeLinkDetail,
  nodeLinkUrl,
  shadowsExisting,
  updateNodeLink,
} from './api'
import NodeLinkNodesTab from './NodeLinkNodesTab'
import NodeLinkStorageTab, { storesKey } from './NodeLinkStorageTab'
import ProvisionProgress from './ProvisionProgress'
import ShareSkippedNotice from './ShareSkippedNotice'

/* One NodeLink's editor: `/nodes/nodelinks/new` (`mode="create"`, General only) and `/nodes/nodelinks/:nodelinkId`.
 * General saves the name, description and mount path; Storage chooses where it lives and provisions it; Nodes
 * attaches it. Every refusal is the server's own sentence (`app/nodelinks.py`). Writes need `credentials:edit`
 * and the plan term; finishing a stuck delete needs `credentials:edit` alone (the server allows it to a lapsed
 * org). `?org=<id>` (`nodeLinkViewUrl`, the operator's list): another org's NodeLink, read as that org and
 * read-only everywhere; Act as opens it as that org's Owner to change it. */

type Tab = 'general' | 'storage' | 'nodes'

const TABS: readonly TabDef<Tab>[] = [
  { id: 'general', label: 'General' },
  { id: 'storage', label: 'Storage' },
  { id: 'nodes', label: 'Nodes' },
]

/** The general form's measure. */
/** THE LOAD-BEARING CAVEATS of a path outside `{jaah-home}/nodelink/` (`nodelinks.shadows_existing`). */
export const SHADOW_WARNING =
  'Hides what is already at this path while mounted. Files here can’t be executed (noexec). ' +
  'A later path change remounts it under live runners (lazy unmount).'

/* The prefixes of `nodelinks.DUPLICATE` / `OVERLAP` and the name and path validators' 422s: which field a refusal names. */
const NAME_REFUSALS = ['A NodeLink named', 'The name must']
const PATH_REFUSALS = ['This path overlaps', 'The mount path', 'Each folder of the mount path']

function fieldOfRefusal(text: string): 'name' | 'path' | null {
  if (NAME_REFUSALS.some(p => text.startsWith(p))) return 'name'
  if (PATH_REFUSALS.some(p => text.startsWith(p))) return 'path'
  return null
}

/* A path is a token: mono in the input itself (the root's font does not reach MUI's input). */
const MONO_INPUT_SX = (theme: Theme) => ({ '& .MuiInputBase-input': monoSx(theme) })

/* A delete waits on nodes to unmount, a `provisioning` row on the server's next pass (the `nodelink-provision`
   cron, which runs with this page closed too): re-read while one is out. */
const POLL_MS = 15000

export default function NodeLinkEditPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { nodelinkId } = useParams<{ nodelinkId: string }>()
  const creating = mode === 'create'
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const can = useCan()
  const { admin } = useAuth()
  const [searchParams] = useSearchParams()
  // Another org's, viewed by the operator: every read names it, nothing writes (the server refuses anyone else).
  const viewedOrgId = (() => {
    const org = searchParams.get('org')
    return org && org !== admin?.org?.id ? org : undefined
  })()
  const planOn = admin?.nodelink_enabled === true
  const canEdit = can('credentials:edit') && planOn && !viewedOrgId
  const canTeardown = can('credentials:edit') && !viewedOrgId

  const [detail, setDetail] = useState<NodeLinkDetail | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [path, setPath] = useState(defaultMountPath(''))
  // New: the path follows the name until the user edits it.
  const [pathTouched, setPathTouched] = useState(!creating)
  const { busy: saving, errors, setErrors, formError, setFormError, run, saved, clearSaved } = useSubmit(fieldOfRefusal)
  const { busy: acting, error: actionError, run: act } = useRowActions<'delete' | 'anyway'>()

  const seed = useCallback((row: NodeLinkDetail) => {
    setDetail(row)
    setName(row.name)
    setDescription(row.description)
    setPath(row.mount_path)
    setPathTouched(true)
  }, [])

  /* Nothing is fetched for a NodeLink already held: after a create the router reuses this instance. */
  const { loading, notFound, loadError, confirm, confirmDialog } = useEditRecord(
    creating || detail?.id === nodelinkId ? null : nodelinkId ?? null, id => getNodeLink(id, viewedOrgId), seed,
    { loadFallback: 'Could not load the NodeLink.', notFoundStatuses: [404, 422] },
  )

  const [tab, selectTab] = useUrlTab(creating ? [TABS[0]] : TABS, 'general')

  /** Any write: the list re-reads on its next visit. */
  const changed = useCallback((row: NodeLinkDetail) => {
    setDetail(row)
    void queryClient.invalidateQueries({ queryKey: ['nodelinks'] })
  }, [queryClient])

  const gone = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['nodelinks'] })
    navigate(NODELINK_STORE_URL, { replace: true })
  }, [navigate, queryClient])

  /* ── Poll while provisioning or deleting ── */
  const status = detail?.status
  usePoll(async () => {
    if (!detail) return
    try {
      changed(await getNodeLink(detail.id, viewedOrgId))
    } catch (err) {
      // A delete that finished between polls: the row is gone.
      if (err instanceof ApiError && err.status === 404) gone()
    }
  }, status === 'provisioning' || status === 'deleting' ? POLL_MS : null)

  if (!creating && (loading || notFound || loadError || !detail)) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'NodeLinks', to: NODELINK_STORE_URL }, { label: 'NodeLink' }]} />}
        loading={loading || (!notFound && !loadError)}
        notFound={notFound && 'NodeLink not found'}
        error={loadError || 'Could not load the NodeLink.'}
      />
    )
  }

  const isDeleting = detail?.status === 'deleting'
  const readOnly = !canEdit || isDeleting

  /* ── General ── */
  const changes: Partial<NodeLinkBody> = {}
  if (detail) {
    if (name.trim() !== detail.name) changes.name = name.trim()
    if (description.trim() !== detail.description) changes.description = description.trim()
    if (path.trim() !== detail.mount_path) changes.mount_path = path.trim()
  }
  const complete = name.trim() !== '' && path.trim() !== ''
  const unchanged = detail !== null && Object.keys(changes).length === 0

  const changeName = (next: string) => {
    setName(next)
    if (!pathTouched) setPath(defaultMountPath(next))
    setErrors(prev => ({ ...prev, name: undefined }))
    setFormError('')
    clearSaved()
  }

  const changePath = (next: string) => {
    setPath(next)
    setPathTouched(true)
    setErrors(prev => ({ ...prev, path: undefined }))
    setFormError('')
    clearSaved()
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run(detail ? 'Could not save. Retry.' : 'Could not create the NodeLink. Retry.', async () => {
      if (!detail) {
        const created = await createNodeLink({
          name: name.trim(),
          // Untouched, the server derives it from its UNIQUE slug (a bare slug could overlap a sibling's).
          ...(pathTouched ? { mount_path: path.trim() } : {}),
          ...(description.trim() ? { description: description.trim() } : {}),
        })
        seed(created)
        changed(created)
        // `replace`: Back after creating lands on the list, never a create form that would make a second one.
        navigate(nodeLinkUrl(created.id, 'storage'), { replace: true })
      } else {
        const row = await updateNodeLink(detail.id, changes)
        seed(row)
        changed(row)
      }
    })
  }

  /* ── Delete ── */
  async function remove() {
    if (!detail) return
    // Its storage outlives it when Jaah made it and the cloud bills it, and this is its last NodeLink: said so.
    const kept = detail.store_managed || !detail.store_id || !detail.store_file_system_id ? null : await queryClient
      .fetchQuery({ queryKey: storesKey(viewedOrgId), queryFn: () => listStores(viewedOrgId) })
      .then(stores => stores.find(s => s.id === detail.store_id && s.deletable_in_cloud && s.nodelink_count <= 1) ?? null)
      .catch(() => null)
    // Kept: the bill moves to the storage itself, deleted under Storage — said once, not again per file.
    const billed = kept ? '.' : ', which is still billed.'
    const stillBilled = kept ? '.' : ' and are still billed.'
    const keptNote = kept
      ? ` ${kept.provider === 'gcp' ? 'The Filestore instance' : 'The EFS file system'} stays, still billed. Delete it under Storage.`
      : ''
    const ok = await confirm({
      title: `Delete ${detail.name}?`,
      identity: <Typography component="span" sx={monoSx}>{detail.name}</Typography>,
      // What a delete destroys, and what it keeps — never cut (ui-copy-standards § What never gets cut).
      // Jaah-managed storage is the operator's: no EFS of the tenant's holds the files, nothing bills them for it.
      // No storage made (none chosen, or its create refused before the cloud held one): nothing is left to bill.
      message: (detail.store_managed
        ? 'Nodes unmount it. The access point is deleted. Files stay on Jaah-managed storage.'
        : !detail.store_file_system_id
          ? 'Nodes unmount it. It has no storage, so nothing stays billed.'
        : detail.store_provider === 'gcp'
          ? `Nodes unmount it. Its folder and files stay on your Filestore${billed}`
        : detail.store_provider === 'azure'
          ? 'Nodes unmount it. Its file share and files stay in Azure and are still billed.'
        : detail.access_point_adopted
          ? `Nodes unmount it. Its access point stays in AWS. Files stay in your EFS${stillBilled}`
          : `Nodes unmount it. The access point is deleted. Files stay in your EFS${stillBilled}`) + keptNote,
      confirmLabel: 'Delete',
      destructive: true,
      typed: { name: detail.name, action: 'the delete' },
    })
    if (!ok) return
    await act('delete', `Could not delete ${detail.name}. Retry.`, async () => {
      const left = await deleteNodeLink(detail.id)
      if (left === null) gone()
      else changed(left)
    })
  }

  async function removeAnyway() {
    if (!detail) return
    const ok = await confirm({
      title: `Delete ${detail.name} anyway?`,
      identity: <Typography component="span" sx={monoSx}>{detail.name}</Typography>,
      message: `${detail.store_provider === 'gcp' || detail.store_provider === 'azure'
        ? 'Its files stay in the cloud.'
        : detail.access_point_adopted ? 'Its access point stays in AWS.' : 'The access point is deleted now.'} `
        + 'Nodes that never reported may keep a stale mount until their next converge.',
      confirmLabel: 'Delete anyway',
      destructive: true,
    })
    if (!ok) return
    await act('anyway', `Could not delete ${detail.name}. Retry.`, async () => {
      const left = await deleteNodeLinkAnyway(detail.id)
      if (left === null) gone()
      else changed(left)
    })
  }

  /* Changing another org's NodeLink: open it as that org (`lib/actAs.ts`), as the list's Act as does. */
  const actAs = (row: NodeLinkDetail) => {
    if (!row.org) return
    enterActAs(row.org)
    navigate(nodeLinkUrl(row.id))
  }

  const heading = detail?.name ?? 'New NodeLink'
  const actions = detail && (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {!isDeleting && canEdit && (
        <Button variant="outlined" color="error" onClick={() => void remove()} disabled={acting.size > 0}
          data-testid="nodelink-delete" sx={tapTargetSx}>
          Delete
        </Button>
      )}
      {isDeleting && canTeardown && (
        <>
          <Button variant="outlined" onClick={() => void act('delete', `Could not delete ${detail.name}. Retry.`, async () => {
            const left = await deleteNodeLink(detail.id)
            if (left === null) gone()
            else changed(left)
          })} disabled={acting.size > 0} data-testid="nodelink-delete-retry" sx={tapTargetSx}>
            Retry delete
          </Button>
          <Button variant="outlined" color="error" onClick={() => void removeAnyway()} disabled={acting.size > 0}
            data-testid="nodelink-delete-anyway" sx={tapTargetSx}>
            Delete anyway
          </Button>
        </>
      )}
    </Box>
  )

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'NodeLinks', to: NODELINK_STORE_URL }, { label: heading }]} />}
      title={heading}
      titleAside={detail && (
        <StatusChip label={detail.status} tone={STATUS_TONE[detail.status] ?? 'neutral'} data-status={detail.status} />
      )}
      /* One line, ellipsised, full path on hover and copy: wrapped mid-segment on a phone it pushed Delete under
         it. `containerType` keeps its width out of the header's sizing. */
      subtitle={detail && (
        <Box component="span" sx={{ display: 'flex', alignItems: 'center', gap: 0.5, containerType: 'inline-size' }}>
          <Box component="span" title={detail.mount_path} data-testid="nodelink-mount-path"
            sx={[monoSx, { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }]}>
            {detail.mount_path}
          </Box>
          <CopyButton variant="icon" text={() => detail.mount_path} label="Copy mount path" />
        </Box>
      )}
      actions={actions}
      testId="nodelink-editor"
    >
      {/* Under the header on every tab; the Storage tab shows it beside the storage's own facts instead. */}
      {tab !== 'storage' && detail?.progress && (
        <ProvisionProgress steps={detail.progress} provider={detail.store_managed ? 'aws' : detail.store_provider}
          testId="nodelink-header-progress" />
      )}
      {actionError && <Banner severity="error" testId="nodelink-action-error">{actionError}</Banner>}
      {viewedOrgId && detail?.org && (
        <Banner severity="info" testId="nodelink-view-only">
          {detail.org.name}’s NodeLink · Read-only.{' '}
          <Button size="small" color="warning" onClick={() => actAs(detail)} title={`Open it as ${detail.org.name}'s Owner`}
            data-testid="nodelink-view-act-as" sx={{ minWidth: 0, py: 0 }}>
            Act as
          </Button>
        </Banner>
      )}
      <ShareSkippedNotice />

      {!creating && <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="NodeLink sections" idPrefix="nodelink" />}

      <TabPanel id="general" current={tab} idPrefix="nodelink" label={creating ? 'General' : undefined}>
        <Box component="form" onSubmit={submit} sx={FORM_SX} data-testid="nodelink-form">
          {formError && <Banner severity="error" testId="nodelink-form-error">{formError}</Banner>}
          <FormTextField
            label="Name"
            id="nodelink-name"
            value={name}
            onChange={changeName}
            disabled={readOnly}
            error={errors.name}
            required
            autoFocus={creating}
            slotProps={{ htmlInput: { ...TEXT_INPUT.htmlInput, maxLength: 100 } }}
          />
          <FormTextField
            label="Description"
            id="nodelink-description"
            value={description}
            onChange={v => { setDescription(v); setFormError(''); clearSaved() }}
            disabled={readOnly}
            multiline
            minRows={2}
          />
          <FormTextField
            label="Mount path"
            id="nodelink-path"
            value={path}
            onChange={changePath}
            disabled={readOnly}
            error={errors.path}
            required
            slotProps={{ htmlInput: { ...TEXT_INPUT.htmlInput, maxLength: 255 } }}
            sx={MONO_INPUT_SX}
          />
          {shadowsExisting(path) && (
            <Banner severity="warning" testId="nodelink-shadow-warning">{SHADOW_WARNING}</Banner>
          )}
          <FormActionBar testId="nodelink-actions" stretchTestIds={['nodelink-save']}>
            <SavedNote show={saved} sx={{ alignSelf: 'center', mr: 'auto' }} />
            <Button component={RouterLink} to={NODELINK_STORE_URL} variant="text" sx={tapTargetSx}>
              {readOnly ? 'Back' : 'Cancel'}
            </Button>
            {!readOnly && (
              <SubmitButton testId="nodelink-save" busy={saving} busyLabel={creating ? 'Creating…' : 'Saving…'}
                disabled={!complete || unchanged}>
                {creating ? 'Create' : 'Save'}
              </SubmitButton>
            )}
          </FormActionBar>
        </Box>
      </TabPanel>

      {detail && tab === 'storage' && (
        <TabPanel id="storage" current={tab} idPrefix="nodelink">
          <NodeLinkStorageTab detail={detail} canEdit={canEdit} orgId={viewedOrgId} onChange={changed} />
        </TabPanel>
      )}

      {detail && tab === 'nodes' && (
        <TabPanel id="nodes" current={tab} idPrefix="nodelink">
          <NodeLinkNodesTab detail={detail} orgId={viewedOrgId} onChange={changed} />
        </TabPanel>
      )}

      {confirmDialog}
    </DetailPage>
  )
}
