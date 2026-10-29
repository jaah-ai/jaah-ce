import { type JSX, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { apiDetail } from '../../../api'
import { useAuth, useCan } from '../../../auth'
import { PLAN_PAGE } from '../../../lib/companySetup'
import { enterActAs } from '../../../lib/actAs'
import { countOf } from '../../../lib/humanize'
import { isOrgAdminSeat } from '../../../lib/orgAdmin'
import Banner from '../../../ui/Banner'
import ListStates from '../../../ui/feed/ListStates'
import { matchesAny } from '../../../ui/FilterRow'
import type { FeedRowSpec } from '../../../ui/feed/types'
import { NoMatchesState } from '../../../ui/NoMatchesState'
import { orgSlots, useOrgColumn, useOrgVisible } from '../../../ui/OrgIndicator'
import SearchBox from '../../../ui/SearchBox'
import { listAllOrgsNodeLinks, listNodeLinks, type NodeLink, nodeLinkUrl, nodeLinkViewUrl } from './api'
import ShareSubscriptionsAction from './ShareSubscriptionsAction'
import StoreList, { statusIndicator } from './StoreList'

/* Networks > NodeLink: the org's NodeLinks, one `ui/feed/FeedList` row each (`GET /api/org/nodelinks`): the status
 * disc (`last_error` on its hover), the name, path, storage and node count. A row's name opens its editor
 * (`NodeLinkEditPage.tsx`, `/nodes/nodelinks/:id`), where storage, nodes and delete live. New needs
 * `credentials:edit` and the org's NodeLink plan term (`Admin.nodelink_enabled`); without the term the list
 * still shows, with the plan notice. Beside New: Share subscriptions between nodes (`ShareSubscriptionsAction`).
 * THE OPERATOR reads every org's (`GET …?all_orgs=true`, narrowed by the org filter) with an Org slot; another
 * org's name opens its editor read-only (`nodeLinkViewUrl`); its ⋯ › Act as opens it as that org's Owner, to
 * change it. Storage names the file system (`fs-…`) beside the store, and Search matches it, so a store AWS
 * reports can be found by its id. Under the list: the caller's own org's storage, with its Delete (`StoreList`). */

export const NEW_NODELINK_URL = '/nodes/nodelinks/new'

/* A delete waits on nodes to unmount, a provision on AWS: re-read while either is out. */
const POLL_MS = 15000

const busy = (rows: NodeLink[] | undefined) =>
  (rows ?? []).some(r => r.status === 'deleting' || r.status === 'provisioning')

/* Slot widths (px): `{jaah-home}/nodelink/datasets` (mono, ellipsised past it), a store name, the file system id, a count.
 * The path and the id go first on a phone — the NodeLink's own page has them. */
const PATH_SLOT = 220
const STORE_SLOT = 140
const FS_SLOT = 150
const COUNT_SLOT = 56

export default function NodeLinkList(): JSX.Element {
  const can = useCan()
  const { admin } = useAuth()
  const navigate = useNavigate()
  const planOn = admin?.nodelink_enabled === true
  const canCreate = can('credentials:edit') && planOn
  const operator = useOrgVisible()
  // The Org slot gives way on a narrow window, as every list's does (`useOrgColumn`).
  const orgColumn = useOrgColumn()
  const ownOrgId = admin?.org?.id
  const [search, setSearch] = useState('')

  const query = useQuery({
    queryKey: operator ? ['nodelinks', 'all-orgs'] : ['nodelinks'],
    queryFn: () => (operator ? listAllOrgsNodeLinks() : listNodeLinks()),
    refetchInterval: q => (busy(q.state.data) ? POLL_MS : false),
  })
  const rows = query.data?.filter(row =>
    matchesAny(search, row.name, row.mount_path, row.store_name, row.store_file_system_id, row.org?.name, row.org?.slug))
  /* Another org's row: viewed read-only, or opened as that org to change it (`lib/actAs.ts`). */
  const otherOrg = (row: NodeLink) => (row.org && ownOrgId && row.org.id !== ownOrgId ? row.org : null)
  /* The operator's row opens only when its org is resolved: their own org's editor, or another's read-only view. */
  const openable = (row: NodeLink) => !operator || (Boolean(ownOrgId) && Boolean(row.org))
  const urlOf = (row: NodeLink) => {
    const other = otherOrg(row)
    return other ? nodeLinkViewUrl(row.id, other.id) : nodeLinkUrl(row.id)
  }

  const actAs = (row: NodeLink) => {
    const org = otherOrg(row)
    if (!org) return
    enterActAs(org)
    navigate(nodeLinkUrl(row.id))
  }

  const feedRows: FeedRowSpec[] = (rows ?? []).map(row => {
    const other = otherOrg(row)
    return {
      key: row.id,
      indicator: statusIndicator(row.status),
      title: row.name,
      titleText: row.name,
      href: openable(row) ? urlOf(row) : undefined,
      // The whole row opens the record its name links to (the one destination it already advertises).
      onRowClick: openable(row) ? () => navigate(urlOf(row)) : undefined,
      rowClickLabel: openable(row) ? `Open ${row.name}` : undefined,
      data: { nodelink: row.id },
      notes: row.last_error ? [{ key: 'error', text: row.last_error, tone: 'error' }] : undefined,
      slots: [
        ...orgSlots(orgColumn, row.org, { testId: `nodelink-org-${row.id}` }),
        { key: 'path', width: PATH_SLOT, gate: 'phone', align: 'left', mono: true, dim: true, title: row.mount_path, node: row.mount_path },
        { key: 'store', width: STORE_SLOT, align: 'left', title: row.store_name ?? undefined, node: row.store_name ?? '—' },
        {
          key: 'fs', width: FS_SLOT, gate: 'phone', align: 'left', mono: true, dim: true, testId: `nodelink-fs-${row.id}`,
          title: row.store_file_system_id ?? undefined, node: row.store_file_system_id,
        },
        { key: 'nodes', width: COUNT_SLOT, align: 'right', mono: true, title: countOf(row.node_count, 'node'), node: row.node_count },
      ],
      actions: other ? [{
        key: 'act-as', label: 'Act as', ariaLabel: `Act as ${other.name} and open ${row.name}`,
        testId: `nodelink-act-as-${row.id}`, onClick: () => actAs(row),
      }] : undefined,
    }
  })

  const loadBanner = query.isError
    ? <Banner severity="error">{apiDetail(query.error, 'Could not load NodeLinks.')}</Banner>
    : null

  return (
    <Box data-testid="nodelink-list" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1, maxWidth: '72rem' }}>
      {!planOn && (
        <Banner severity="info" testId="nodelink-plan-off">
          NodeLink is not on your plan.{' '}
          {isOrgAdminSeat(admin)
            ? <Link component={RouterLink} to={PLAN_PAGE}>Change plan</Link>
            : 'Ask an admin to change it.'}
        </Banner>
      )}

      {canCreate && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 1 }}>
          <ShareSubscriptionsAction />
          <Button variant="contained" size="small" component={RouterLink} to={NEW_NODELINK_URL} data-testid="nodelink-new">
            New NodeLink
          </Button>
        </Box>
      )}

      {/* A re-read's failure says it over the rows it keeps; the first read's is the body (`failed`). */}
      {query.data !== undefined && loadBanner}

      {query.data && query.data.length > 0 && (
        <SearchBox
          value={search}
          onChange={setSearch}
          delayMs={150}
          ariaLabel="Search NodeLinks"
          placeholder={operator ? 'Name, path, storage, id, org' : 'Name, path, storage, id'}
          testId="nodelink-search"
          sx={{ maxWidth: '24rem' }}
        />
      )}
      <ListStates
        loading={query.isPending}
        skeleton={{ rows: 3 }}
        failed={query.data === undefined && loadBanner}
        empty={feedRows.length === 0}
        noMatch={query.data && query.data.length > 0 && <NoMatchesState title="No NodeLink matches" clearLabel="Clear search" onClear={() => setSearch('')} />}
        emptyTitle="No NodeLinks yet"
        feed={{
          ariaLabel: 'NodeLinks',
          testId: 'nodelink-table',
          sections: [{ key: 'nodelinks', rows: feedRows, rail: false }],
          columns: {
            title: { key: 'name', label: 'Name' },
            slots: [
              ...(orgColumn ? [{ key: 'org', label: 'Org' }] : []),
              { key: 'path', label: 'Path' },
              { key: 'store', label: 'Storage' },
              { key: 'nodes', label: 'Nodes' },
            ],
          },
        }}
      />

      <StoreList />
    </Box>
  )
}
