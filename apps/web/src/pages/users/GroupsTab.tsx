import { useMemo } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail, del } from '../../api'
import { useAuth } from '../../auth'
import { useIsOrgAdmin } from '../../hooks/useIsOrgAdmin'
import { useConfirm } from '../../components/ConfirmDialog'
import { NO_VALUE } from '../../lib/relativeTime'
import { EVERYONE_REASON, groupLabel, isReservedGroupName, type GroupSummary } from '../../types'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import { ScimChip } from '../../ui/MarkChip'
import { orgSlots, useOrgVisible } from '../../ui/OrgIndicator'
import Banner from '../../ui/Banner'
import FilterBar from '../../ui/FilterBar'
import { FilterRow } from '../../ui/FilterRow'
import ListStates from '../../ui/feed/ListStates'
import { contentSlotWidth } from '../../ui/feed/geometry'
import type { FeedColumn, FeedRowSpec } from '../../ui/feed/types'
import ListPager from '../../ui/ListPager'
import NoMatchesState from '../../ui/NoMatchesState'
import SearchBox from '../../ui/SearchBox'
import { FILTER_STRIP_SX } from './UserFilterStrip'
import { deleteItem } from '../../components/tasks/rowActions'
import { GROUPS_LIST_CONTAINER_SX, GROUPS_NO_DESCRIPTION, tapTargetSx } from '../../ui/listStyles'
import PageHeader from '../../ui/PageHeader'
import { setPageParams, useListPage, useListPageQuery } from '../../hooks/useListPage'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { useRowActions } from '../../hooks/useBusyIds'

/* DIRECTORY > GROUPS — the group roster, second panel of `UsersPage.tsx`'s shell, which owns the heading
 * and the mount-on-first-open latch (`TabPanel` keeps every panel mounted). Create and Delete render only
 * for an org admin (rule at `pages/users/UsersTab.tsx`); a `groups:view` holder reads it read-only. */

/** The reserved group's refusal in the server's terms — one string for the ⋯ item's `disabledReason`,
 *  the row mark and the guard (the API answers this delete with a 400). */
const RESERVED_REASON = 'this group is reserved (it confers no bypass)'

/* SCIM groups created before the server's description gained its period (`app/scim.py::GROUP_DESCRIPTION`) keep
   the old text in the row; it reads as the seeded groups' sentences do. Display only: the stored text is unchanged. */
const LEGACY_SCIM_DESCRIPTION = 'Managed by your identity provider'
const groupDescription = (g: GroupSummary): string =>
  g.scim_managed && g.description === LEGACY_SCIM_DESCRIPTION ? `${g.description}.` : g.description

/* SERVER-PAGED (`hooks/useListPage`): search, page, size and order are query parameters the server
 * applies to the page AND `total`; nothing here filters or sorts. The keys carry a `g` prefix because the
 * People panel stays mounted beside this one on the same location (`UsersPage.tsx`'s latches): a shared `?q=`
 * would re-query the hidden roster on every word typed here. */
const PREFIX = 'g'
const Q_KEY = `${PREFIX}q`
const SORTS = ['name', 'members', 'bindings'] as const

/* The header over the slots below, matched by slot key. Counts open biggest-first: "which group is largest /
 * grants most" is the question a count column is pressed for. Description is a heading only (no server order). */
const COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: { key: 'name', label: 'Name', sortKey: 'name' },
  slots: [
    { key: 'org', label: 'Org' },
    { key: 'description', label: 'Description' },
    { key: 'bindings', label: 'Bindings', sortKey: 'bindings', firstDir: 'desc' },
    { key: 'members', label: 'Members', sortKey: 'members', firstDir: 'desc' },
  ],
}

/* A SCIM group's title: the name ellipsises, the mark beside it keeps its width (`pages/Projects.tsx`'s title row). */
const TITLE_ROW_SX = { display: 'inline-flex', alignItems: 'center', maxWidth: '100%', minWidth: 0 } as const
const TITLE_NAME_SX = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const

/* The description's priced width and its cap, in percent of the roster's width (`ui/feed/geometry::contentSlotWidth`). */
const DESCRIPTION_SLOT = 200
const DESCRIPTION_MAX_CQW = 40

const SEARCH_SX = { flex: '1 1 220px', minWidth: 190, maxWidth: 360, m: 0 } as const

export default function GroupsTab() {
  const canWrite = useIsOrgAdmin()
  const isOperator = useAuth().admin?.is_operator === true
  // All orgs: an operator's roster spans tenants, so each row names its org (`ui/OrgIndicator.tsx`).
  const showOrg = useOrgVisible()
  const columns = showOrg ? COLUMNS : { ...COLUMNS, slots: COLUMNS.slots.filter(column => column.key !== 'org') }
  const [params, setFilters] = useUrlFilters()
  const q = params.get(Q_KEY) ?? ''
  const list = useListPage({ prefix: PREFIX, sorts: SORTS, resetKey: q })
  const { page, per, sort, dir } = list
  const path = useMemo(() => {
    const query = new URLSearchParams()
    if (q.trim()) query.set('q', q.trim())
    return `/api/groups?${setPageParams(query, { page, per, sort, dir }).toString()}`
  }, [q, page, per, sort, dir])
  // Counts ride the summary row (`routes/groups.py::GroupSummary`): one request per page, no per-row fan-out.
  const { rows: groups, total, loading, error, refetch } = useListPageQuery<GroupSummary>({
    queryKey: ['groups', 'page', path],
    path,
  })
  const loadError = error ? apiDetail(error, 'Failed to load groups') : ''
  const [confirm, confirmDialog] = useConfirm()
  const { busy: busyIds, run, error: actionError } = useRowActions<string>()

  /* Modal confirm, not an in-row strip: a feed row is one line by contract (see `pages/users/UsersTab.tsx`). */
  async function confirmAndDelete(group: GroupSummary) {
    const label = groupLabel(group, isOperator)
    const ok = await confirm({
      title: 'Delete group',
      identity: label,
      message: `Delete ${label}? Every membership and access binding it carries goes with it, for every member.`,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    // The server's 400 detail (e.g. the reserved-group refusal) reaches the operator verbatim.
    await run(group.id, 'Failed to delete group', async () => {
      await del(`/api/groups/${group.id}`)
      await refetch()
    })
  }

  /* The member count keeps its slot at every width (the question this roster is scanned with, and a column the
   * reader may have sorted by). Description yields first (≤899px, or a roster narrowed by an open group), the
   * binding count next (≤767px — the disc still says whether the group grants anything): the name keeps the width
   * (`ui/listStyles.ts`'s `GROUPS_NO_*`). Full text on `title` and the Details tab. Widths fit the uppercase header
   * plus its sort arrow. */
  /* At full width the description takes the spare width up to its widest value (capped at 40% of the roster, never
   * under its priced 200px) and wraps to two lines past it, so it reads without hovering. */
  const descriptionWidth = contentSlotWidth(groups.map(g => groupDescription(g) || NO_VALUE), DESCRIPTION_SLOT, DESCRIPTION_MAX_CQW)
  const groupRows: FeedRowSpec[] = groups.map((g): FeedRowSpec => {
    /* All orgs holds one Everyone per tenant: its title names the org, so the rows read apart without the chip. */
    const label = showOrg && g.is_everyone && g.org ? `${groupLabel(g, false)} · ${g.org.name}` : groupLabel(g, isOperator)
    const reserved = isReservedGroupName(g.name)
    const undeletable = reserved || g.is_everyone === true
    const grants = g.binding_count > 0
    return {
      key: g.id,
      indicator: {
        /* State = does the group grant anything (a role binding); one granting nothing is what an auditor looks for.
         * Reserved has its own `■` mark: the superadmin group confers no bypass (that keys off
         * `admin_users.is_superadmin`) and cannot be deleted, so "inert" would invite a refused delete. */
        tone: reserved ? 'info' : grants ? 'success' : 'neutral',
        glyph: reserved ? '■' : grants ? '●' : '○',
        label: reserved
          ? `reserved — ${RESERVED_REASON} and it cannot be deleted`
          : grants
            ? `grants ${g.binding_count} role${g.binding_count === 1 ? '' : 's'}`
            : 'grants nothing yet',
        data: { reserved },
      },
      title: g.scim_managed ? (
        <Box component="span" sx={TITLE_ROW_SX}>
          <Box component="span" sx={TITLE_NAME_SX}>{label}</Box>
          <ScimChip />
        </Box>
      ) : label,
      titleText: label,
      /* Narrowed beside an open group the title wraps to two lines, not "Everyon…": it carries the org's name. */
      titleWrapAt: GROUPS_NO_DESCRIPTION,
      href: `/groups/${g.id}`,
      busy: busyIds.has(g.id),
      slots: [
        ...orgSlots(showOrg, g.org, { testId: 'col-group-org', unresolvedId: g.org_id }),
        {
          key: 'description',
          width: descriptionWidth,
          lines: 2,
          gate: 'groupsDescription',
          align: 'left',
          testId: 'col-group-description',
          title: groupDescription(g) || 'No description',
          node: groupDescription(g) || NO_VALUE,
        },
        {
          key: 'bindings',
          width: 64,
          gate: showOrg ? 'groupsBindingsWithOrg' : 'groupsBindings',
          align: 'right',
          mono: true,
          testId: 'col-group-bindings',
          title: `${g.binding_count} bindings`,
          node: g.binding_count,
        },
        {
          key: 'members',
          width: 64,
          align: 'right',
          mono: true,
          testId: 'col-group-members',
          title: `${g.member_count} members`,
          node: g.member_count,
        },
      ],
      actions: canWrite ? [
        // Names WHICH group for screen readers; `RowActionsOverflow` appends `disabledReason` on a refused row.
        deleteItem(label, () => { if (!undeletable) void confirmAndDelete(g) }, {
          disabled: undeletable,
          disabledReason: reserved ? RESERVED_REASON : EVERYONE_REASON,
        }),
      ] : undefined,
    }
  })

  return (
    /* The roster's own width gates its slots (`GROUPS_LIST_CONTAINER`): an open group beside it narrows it. */
    <Box sx={GROUPS_LIST_CONTAINER_SX}>
      {/* Titleless: its action rides the People sub-strip (or the Directory strip when it is the only tab) (`components/TabActionsSlot`). A route, not a dialog:
      creating a group means filling its member and access tabs (`/groups/new`). */}
      <PageHeader
        actions={canWrite ? (
          <Button component={RouterLink} to="/groups/new" variant="contained" color="primary" {...compactVerbProps('Create group', tapTargetSx)}>
            <CompactVerbLabel label="Create group" />
          </Button>
        ) : undefined}
      />

      {/* The floated Search label's clearance, shared with People's strip (`UserFilterStrip.tsx`). */}
      <Box data-testid="group-filters" sx={FILTER_STRIP_SX}>
        <FilterBar>
          <FilterRow>
            <SearchBox
              value={q}
              onChange={next => setFilters({ [Q_KEY]: next })}
              label="Search"
              placeholder="Name or description"
              ariaLabel="Search groups"
              testId="group-search"
              sx={SEARCH_SX}
            />
          </FilterRow>
        </FilterBar>
      </Box>

      {(actionError || loadError) && <Banner severity="error">{actionError || loadError}</Banner>}

      {/* Empty vs no-match off the SERVER's total: an empty page under a search is the search's doing.
          No total once loaded = the load failed: its banner is above, nothing below. */}
      <ListStates
        loading={loading && total === null}
        skeleton={{ rows: 5 }}
        failed={total === null}
        empty={total === 0}
        noMatch={q && <NoMatchesState title="No matching groups" clearLabel="Clear search" onClear={() => setFilters({ [Q_KEY]: '' })} testId="groups-clear" />}
        emptyTitle="No groups yet"
        emptyScoped
        feed={{
          ariaLabel: 'Groups',
          testId: 'groups-list',
          sections: [{ key: 'groups', rows: groupRows }],
          columns,
          sort: { key: list.sort, dir: list.dir },
          onSort: list.toggleSort,
        }}
      >
        <ListPager page={list.page} per={list.per} total={total} onPage={list.setPage} onPer={list.setPer} testId="groups-pager" />
      </ListStates>

      {confirmDialog}
    </Box>
  )
}
