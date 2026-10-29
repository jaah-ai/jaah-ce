import { type JSX, useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import { useSubmit } from '../../../hooks/useSubmit'
import { useAuth, useCan } from '../../../auth'
import { withMember } from '../../../lib/sets'
import Banner from '../../../ui/Banner'
import FormDialog from '../../../ui/FormDialog'
import FormTextField from '../../../ui/FormTextField'
import { monoSx, PHONE, tapTargetSx } from '../../../ui/listStyles'
import LoadingState from '../../../ui/LoadingState'
import { getCredential } from '../../credentials/api'
import {
  ACCOUNTS_MOUNT_PATH,
  getManagedStorage,
  listNodeLinks,
  listStores,
  nodeLinkUrl,
  type NodeLinkStorageBody,
  SHARE_SKIPPED_STATE,
  shareSubscriptions,
  shareTargets,
} from './api'
import { ADOPTED_POLICY_WARNING } from './NodeLinkStorageTab'
import StorageSourceFields, {
  emptyStorageSource, missingText, type StorageSourceValue, storageBody, storageMissing,
} from './StorageSourceFields'

/* SHARE SUBSCRIPTIONS BETWEEN NODES — Agents › Agents and the NodeLink list's one-step way to the NodeLink at
 * `ACCOUNTS_MOUNT_PATH` (`POST …/share-subscriptions`: create, storage, the nodes ticked, provision). Before Create
 * it lists the nodes the storage choice can go on (`POST …/share-subscriptions/targets`, re-read as the choice moves
 * to another place), all ticked, and those it cannot, greyed with why; Create sends the ticked ones (`node_ids`).
 * Shown to a `credentials:edit` holder whose plan has NodeLink; nothing otherwise (the NodeLink list carries the
 * plan notice). When the org has that NodeLink already, the button opens it. The copy names no provider. */

export const DEFAULT_NAME = 'Subscriptions'
export const SHARE_EXPLANATION = 'Sign in once on one node; every attached node reuses that sign-in.'
export const CONSENT_LABEL = 'I confirm my model provider allows one subscription on several devices'

/* The org's own list (`NodeLinkList` reads it too, unless the operator's every-org list). */
const NODELINKS_KEY = ['nodelinks']

/** Whether the action shows for this viewer: `credentials:edit` and the plan's NodeLink term. */
export function useShareSubscriptionsShown(): boolean {
  const can = useCan()
  const { admin } = useAuth()
  return can('credentials:edit') && admin?.nodelink_enabled === true
}

/** One word on the button, wherever it sits: the whole verb (~240px) pushed the Machines strip's last tabs behind
 *  its More at a 1024px window. The name and the tooltip keep the verb; the words kept lead the name. */
export default function ShareSubscriptionsAction(): JSX.Element | null {
  const navigate = useNavigate()
  const shown = useShareSubscriptionsShown()
  const links = useQuery({ queryKey: NODELINKS_KEY, queryFn: () => listNodeLinks(), enabled: shown })
  const [open, setOpen] = useState(false)
  if (!shown) return null
  const existing = links.data?.find(l => l.mount_path === ACCOUNTS_MOUNT_PATH)
  return (
    <>
      <Button
        type="button"
        variant="outlined"
        onClick={() => (existing ? navigate(nodeLinkUrl(existing.id)) : setOpen(true))}
        disabled={links.isPending}
        data-testid="share-subscriptions"
        aria-label="Share subscriptions"
        title="Share subscriptions between nodes"
        sx={tapTargetSx}
      >
        Share
      </Button>
      {open && <ShareSubscriptionsDialog onClose={() => setOpen(false)} />}
    </>
  )
}

function ShareSubscriptionsDialog({ onClose }: { onClose: () => void }): JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState(DEFAULT_NAME)
  // Unticked: a consent is given, never presumed.
  const [consent, setConsent] = useState(false)
  const { busy, formError: error, run } = useSubmit()

  const storesQuery = useQuery({ queryKey: ['nodelink-stores'], queryFn: () => listStores() })
  const managedQuery = useQuery({ queryKey: ['nodelink-managed-storage', 'new'], queryFn: () => getManagedStorage() })
  const stores = storesQuery.data
  // A failed read offers nothing: the other sources stand.
  const managed = managedQuery.data?.available ? { available: true, location: managedQuery.data.location ?? '' } : null
  const settled = stores !== undefined && !managedQuery.isPending

  // `picked` null = untouched: Jaah-managed when offered, else a storage in Jaah if the org has one, else New AWS
  // EFS. A new storage always takes the NodeLink's name, so the form draws no second name field.
  const [picked, setPicked] = useState<StorageSourceValue | null>(null)
  const storageName = name.trim() || DEFAULT_NAME
  const start = picked ?? {
    ...emptyStorageSource(storageName, managed ? 'managed' : (stores?.length ?? 0) > 0 ? 'store' : 'create'),
    storeId: stores?.[0]?.id ?? '',
  }
  const value: StorageSourceValue = {
    ...start, create: { ...start.create, name: storageName }, existing: { ...start.existing, name: storageName },
  }
  /* New AWS EFS's Region follows the picked AWS credential's own (non-secret) `region` while the Region is blank
     or still the one it filled: one fewer field between the user and Create. A Region the user typed stays; one
     they cleared stays clear until they pick another credential. A credential the viewer cannot read just
     prefills nothing. */
  const credentialId = value.source === 'create' ? value.create.credential_id : ''
  const credentialQuery = useQuery({
    queryKey: ['credential', credentialId],
    queryFn: () => getCredential(credentialId),
    enabled: credentialId !== '',
  })
  const credentialRegion = credentialQuery.data?.config.region?.trim() ?? ''
  const filledRegion = useRef('')
  useEffect(() => {
    const current = value.create.region.trim()
    if (credentialRegion && value.source === 'create' && (current === '' || current === filledRegion.current)) {
      filledRegion.current = credentialRegion
      setPicked({ ...value, create: { ...value.create, region: credentialRegion } })
    }
    // Once per credential's region: not on every keystroke in the Region field.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [credentialRegion, credentialId])
  const body = settled ? storageBody(value) : null
  /* The nodes this storage can go on: read again only when WHERE it is changes (a store, a connection, AWS,
     Jaah-managed), never per keystroke in its fields. */
  const place = targetPlace(body)
  const targetsQuery = useQuery({
    queryKey: ['nodelink-share-targets', place],
    queryFn: () => shareTargets(body as NodeLinkStorageBody),
    enabled: place !== null,
    retry: false,
  })
  // An older server's answer, or none: Create attaches every node it can, as before.
  const targets = Array.isArray(targetsQuery.data?.targets) ? targetsQuery.data : null
  const [unticked, setUnticked] = useState<ReadonlySet<string>>(new Set())
  /* Create waits for the nodes of the place now chosen: sent without `node_ids` it would attach a node the user
     unticked. A failed read lets it go without them only while nothing is unticked. */
  const nodesPending = place !== null && (targetsQuery.isPending || (targets === null && unticked.size > 0))
  /* Create stays disabled until these are filled: said beside it, not only on hover. */
  const missing = settled ? [...(name.trim() ? [] : ['Name']), ...storageMissing(value)] : []
  const disabledReason = missing.length > 0 ? missingText(missing)
    : nodesPending ? (targetsQuery.isPending ? 'Reading nodes…' : 'Could not list the nodes.')
      : consent ? undefined : 'Tick the confirmation to continue.'
  const pickedStore = stores?.find(s => s.id === value.storeId) ?? null
  /* Jaah takes over an adopted file system's policy on its first NodeLink there (the Storage tab's rule). */
  const policyTakeover = value.source === 'existing'
    || (value.source === 'store' && pickedStore?.adopted === true && pickedStore.nodelink_count === 0)

  const submit = () => {
    if (!body || nodesPending) return
    return run('Could not share. Check the fields and retry.', async () => {
      const made = await shareSubscriptions({
        name: name.trim(), consent: true, storage: body,
        ...(targets ? { node_ids: targets.targets.map(t => t.node_id).filter(id => !unticked.has(id)) } : {}),
      })
      await queryClient.invalidateQueries({ queryKey: NODELINKS_KEY })
      // The nodes it could not go on, for the NodeLink page's notice (`ShareSkippedNotice`).
      navigate(nodeLinkUrl(made.id), { state: { [SHARE_SKIPPED_STATE]: made.skipped } })
    })
  }

  return (
    <FormDialog
      title="Share subscriptions between nodes"
      submitLabel="Create"
      busyLabel="Creating…"
      busy={busy}
      error={error}
      onSubmit={() => void submit()}
      onClose={onClose}
      submitDisabled={!body || !name.trim() || nodesPending}
      submitDisabledReason={disabledReason}
      secondaryAction={disabledReason && (
        /* On a phone the line takes its own row above the buttons, so Cancel and Create stay side by side. */
        <Typography variant="caption" role="status" data-testid="share-subscriptions-missing"
          sx={{ color: 'text.secondary', order: -1, mr: 'auto', [PHONE]: { flexBasis: '100%', '&&': { ml: 0 } } }}>
          {disabledReason}
        </Typography>
      )}
      modal="backdrop"
      testId="share-subscriptions-dialog"
      submitTestId="share-subscriptions-create"
      errorTestId="share-subscriptions-error"
    >
      <Typography variant="body2" data-testid="share-subscriptions-explanation">{SHARE_EXPLANATION}</Typography>
      <FormTextField label="Name" id="share-subscriptions-name" value={name} onChange={setName} disabled={busy} required />
      <FormTextField label="Path" id="share-subscriptions-path" value={ACCOUNTS_MOUNT_PATH} onChange={() => {}} readOnly
        slotProps={{ htmlInput: { sx: monoSx } }} />
      {storesQuery.isError && <Banner severity="error">{apiDetail(storesQuery.error, 'Could not load storage.')}</Banner>}
      {settled
        ? <StorageSourceFields orgStores={stores} value={value} onChange={setPicked} managed={managed} disabled={busy}
          onLeave={onClose} ownerNamesStorage />
        : !storesQuery.isError && <LoadingState rows={2} />}
      {policyTakeover && <Banner severity="warning" testId="share-subscriptions-adopted-warning">{ADOPTED_POLICY_WARNING}</Banner>}
      {targets && (targets.targets.length > 0 || targets.skipped.length > 0) && (
        <Box component="fieldset" data-testid="share-subscriptions-nodes" sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
          <Typography component="legend" variant="body2" sx={{ fontWeight: 600, p: 0 }}>Nodes</Typography>
          {targets.targets.map(t => (
            <FormControlLabel
              key={t.node_id}
              sx={{ display: 'flex', ml: '-0.5rem', mr: 0 }}
              control={(
                <Checkbox
                  checked={!unticked.has(t.node_id)}
                  onChange={e => setUnticked(prev => withMember(prev, t.node_id, !e.target.checked))}
                  disabled={busy}
                  data-testid={`share-subscriptions-node-${t.node_id}`}
                  sx={theme => ({ p: '0.5rem', minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx })}
                />
              )}
              label={<Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{t.name}</Typography>}
            />
          ))}
          {targets.skipped.map(s => (
            <FormControlLabel
              key={s.node_id}
              disabled
              sx={{ display: 'flex', ml: '-0.5rem', mr: 0, alignItems: 'flex-start' }}
              control={(
                <Checkbox checked={false} disabled data-testid={`share-subscriptions-skipped-${s.node_id}`}
                  sx={theme => ({ p: '0.5rem', minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx })} />
              )}
              label={(
                <Typography variant="body2" sx={{ pt: '0.75rem', overflowWrap: 'anywhere' }}>
                  {s.name || s.node_id} — {s.reason}
                </Typography>
              )}
            />
          ))}
        </Box>
      )}
      {targetsQuery.isError && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="share-subscriptions-nodes-error">
          {apiDetail(targetsQuery.error, 'Could not list the nodes.')}{unticked.size === 0 && ' Create attaches every node it can.'}
        </Typography>
      )}
      <FormControlLabel
        // Pulls the box's padding back so its edge sits on the fields' left edge.
        sx={{ ml: '-0.5rem', mr: 0, alignItems: 'flex-start' }}
        control={(
          <Checkbox
            checked={consent}
            onChange={e => setConsent(e.target.checked)}
            disabled={busy}
            data-testid="share-subscriptions-consent"
            sx={theme => ({ p: '0.5rem', minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx })}
          />
        )}
        label={<Typography variant="body2" sx={{ pt: '0.75rem' }}>{CONSENT_LABEL}</Typography>}
      />
    </FormDialog>
  )
}

/** Where `body` puts the storage, as far as which nodes it can go on: its store, its connection, AWS or Jaah-managed.
 *  Null while incomplete. */
function targetPlace(body: NodeLinkStorageBody | null): string | null {
  if (!body) return null
  if ('managed' in body) return 'managed'
  if ('store_id' in body) return `store:${body.store_id}`
  if ('connection' in body) return `connection:${body.connection.connection_id}`
  return 'aws'
}
