import { type JSX, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useChangeOrgAction } from '../../../components/ChangeOrgDialog'
import { apiDetail } from '../../../api'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useRowActions } from '../../../hooks/useBusyIds'
import Banner from '../../../ui/Banner'
import ListStates from '../../../ui/feed/ListStates'
import type { FeedRowSpec } from '../../../ui/feed/types'
import { deleteItem, editItem } from '../../../components/tasks/rowActions'
import { ID_SX, tapTargetSx } from '../../../ui/listStyles'
import { orgSlots, useOrgVisible, useScopedOtherOrg } from '../../../ui/OrgIndicator'
import type { CloudConnectionRow } from '../../../types'
import { CLOUD_CONNECTIONS_KEY, deleteCloudConnection, listCloudConnections, PROVIDER_LABELS } from './api'
import ConnectionDialog from './ConnectionDialog'
import { ConnectionImageText, connectionImage } from './connectionImage'

/* Networks > Connections (`pages/nodes/NetworksPage.tsx`) — WHERE a cloud node launches: a credential, a region and the
 * network inside that account. Many connections may share a credential (a region each, say). OWNS ITS
 * WRITES; the rows are a react-query read (`CLOUD_CONNECTIONS_KEY`), so a write, or a Change-org move
 * (which invalidates every query), refetches them. The row shows what operators SCAN (name, credential,
 * provider, image, region, root volume);
 * the network ids live in ⋯ → Edit. */

/* ── The row's trailing cluster, priced by gate strength (`ui/feed/geometry`) ──────────────────────
   PROVIDER and REGION survive every width (with ⋯, 166px: a 390px phone keeps the name ~180); the credential
   gates at 768px (`id`), the volume at 900px (`meta`). The provider left the credential's slot, so the cluster
   is no wider than before at 1024. */
const PROVIDER_SLOT = 48
const REGION_SLOT = 80
const CREDENTIAL_SLOT = 128
const VOLUME_SLOT = 60
/* THE IMAGE: a 21-character AMI id plus its age, or "Newest Ubuntu 26.04 LTS", at the cluster's
   11px mono. Present from 1280px (`FEED_NO_CONNECTION_IMAGE` prices it); the whole sentence rides the row's hover
   too, so it reads at every width. */
const IMAGE_SLOT = 196

export function CloudConnectionsSection(): JSX.Element {
  const queryClient = useQueryClient()
  /* SHAPE-CHECKED: `get<T>` is an unchecked cast, and a non-list must be a load failure, never a white screen.
     Every mount re-reads (`components/addNode/AddNodeDialog.tsx` also creates connections). */
  const query = useQuery({
    queryKey: CLOUD_CONNECTIONS_KEY,
    refetchOnMount: 'always',
    queryFn: async () => {
      const next: unknown = await listCloudConnections()
      if (!Array.isArray(next)) throw new Error('The cloud connections came back in an unexpected shape.')
      return next as CloudConnectionRow[]
    },
  })
  const rows = query.data ?? null
  const loadError = query.error ? apiDetail(query.error, 'Failed to load the cloud connections.') : ''
  // One row write: busy while in flight, refetch after, the refusal over the list.
  const { busy: busyIds, run, error: rowError, setError: setRowError } = useRowActions<string>()
  const [confirm, confirmDialog] = useConfirm()
  const [dialog, setDialog] = useState<{ row: CloudConnectionRow | null } | null>(null)
  const orgVisible = useOrgVisible()
  const scopedOrg = useScopedOtherOrg()
  // The operator's "Change org…", gated inside the hook. No detail page, so no `href`; a move
  // invalidates every query, this list's included.
  const changeOrg = useChangeOrgAction('cloud_connection')

  async function reload() {
    await queryClient.invalidateQueries({ queryKey: CLOUD_CONNECTIONS_KEY })
    setRowError('')
  }

  async function removeRow(row: CloudConnectionRow) {
    /* THE CONFIRM STATES THE CONSEQUENCE: running nodes are untouched (details were resolved at their
       launch); what is lost is launching NEW nodes on this connection. */
    const ok = await confirm({
      title: `Delete ${row.name}?`,
      identity: <Typography component="span" sx={ID_SX}>{row.credential_name || row.credential_id} · {row.region}</Typography>,
      message: 'Nothing running is touched. New launches on this connection stop.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    await run(row.id, `Failed to delete ${row.name}.`, async () => {
      await deleteCloudConnection(row.id)
      await reload()
    })
  }

  const showEmptyState = rows !== null && rows.length === 0

  const addButton = (
    <Button
      type="button"
      variant="contained"
      color="primary"
      size="small"
      data-testid="cloud-connections-add"
      disabled={rows === null}
      onClick={() => setDialog({ row: null })}
      sx={tapTargetSx}
    >
      Add connection
    </Button>
  )

  const connectionRows: FeedRowSpec[] = (rows ?? []).map((row): FeedRowSpec => {
    /* `credential_name` is nullable on the wire (LEFT JOIN); unreachable via the FK but guarded, or the
       cell reads "null". The uuid is the honest stand-in. */
    const account = row.credential_name || `credential ${row.credential_id}`
    const provider = PROVIDER_LABELS[row.provider] ?? row.provider
    /* AWS names its subnet and security group; another cloud its resolved network, field by field. */
    const network = row.provider === 'aws'
      ? `subnet ${row.subnet_id} · security group ${row.security_group_id}`
      : Object.entries(row.network ?? {})
        .map(([name, value]) => `${name} ${Array.isArray(value) ? value.join(', ') : String(value ?? '')}`)
        .join(' · ')
    const changeOrgItem = changeOrg.menuItem({ id: row.id, name: row.name, org: row.org })
    const image = connectionImage(row)
    const imageNode = (text: string) => <ConnectionImageText stale={image.stale}>{text}</ConnectionImageText>
    return {
      key: row.id,
      indicator: {
        /* MARKS THE KIND, NOT A VERDICT — only a launch can judge a connection, so no tone. `●`, not `☁`:
           at 11px a cloud is a smudge. */
        tone: 'neutral',
        glyph: '●',
        label: 'Cloud connection',
      },
      title: <Box component="span" sx={{ fontWeight: 600 }}>{row.name}</Box>,
      titleText: row.name,
      ariaLabel: row.description ? `${row.name} — ${account} · ${provider} — ${row.description}` : `${row.name} — ${account} · ${provider}`,
      busy: busyIds.has(row.id),
      notes: [{ key: 'image', tone: 'muted', text: `Image: ${image.sentence}` }],
      slots: [
        /* Widest gate first, so it falls off the left before the `meta`/`id` slots. */
        {
          key: 'image',
          width: IMAGE_SLOT,
          gate: 'connectionImage',
          align: 'right',
          mono: true,
          title: `Image: ${image.sentence}`,
          node: imageNode(image.short),
          data: { stale: image.stale },
          testId: 'cloud-connection-image',
        },
        ...orgSlots(orgVisible, row.org, { gate: 'meta' }),
        {
          key: 'volume',
          width: VOLUME_SLOT,
          gate: 'meta',
          align: 'right',
          mono: true,
          title: `Root volume ${row.root_volume_gb} GB`,
          node: `${row.root_volume_gb} GB`,
        },
        {
          /* The credential's `title` carries the network plumbing — also in ⋯ → Edit. */
          key: 'credential',
          width: CREDENTIAL_SLOT,
          gate: 'id',
          align: 'right',
          dim: true,
          title: network ? `${account} · ${network}` : account,
          node: account,
        },
        {
          key: 'provider',
          width: PROVIDER_SLOT,
          align: 'right',
          title: provider,
          node: provider,
          testId: 'cloud-connection-provider',
        },
        {
          key: 'region',
          width: REGION_SLOT,
          align: 'right',
          mono: true,
          title: `Launches in ${row.region}`,
          node: row.region,
        },
      ],
      actions: [
        editItem(row.name, () => setDialog({ row })),
        ...(changeOrgItem ? [changeOrgItem] : []),
        deleteItem(row.name, () => void removeRow(row)),
      ],
    }
  })

  return (
    <Box
      component="section"
      data-testid="cloud-connections-section"
      sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}
    >
      {/* No heading: the Connections tab names the section. ONE ADD CONTROL AT A TIME: the empty state
          carries it when empty (two same-named buttons are indistinguishable); this right-aligned row keeps
          it in every other state. */}
      {!showEmptyState && <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{addButton}</Box>}

      {loadError && <Banner severity="error">{loadError}</Banner>}
      {rowError && <Banner severity="error">{rowError}</Banner>}

      {/* No rows once loaded = the load failed: its banner is above, nothing below. */}
      <ListStates
        loading={rows === null && !loadError}
        skeleton={{}}
        failed={rows === null}
        empty={showEmptyState}
        noMatch={false}
        emptyTitle="No cloud connections yet"
        emptyAction={addButton}
        feed={{
          ariaLabel: 'Cloud connections',
          testId: 'cloud-connections-list',
          sections: [{ key: 'connections', rows: connectionRows }],
        }}
      />

      {dialog && (
        <ConnectionDialog
          row={dialog.row}
          onClose={() => setDialog(null)}
          onSaved={reload}
          /* An add under an org filter on one other org offers that org's credentials, so the connection lands
             there — a connection takes its credential's org. */
          {...(dialog.row === null && scopedOrg ? { orgId: scopedOrg.id, orgName: scopedOrg.name } : {})}
        />
      )}

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}

export default CloudConnectionsSection
