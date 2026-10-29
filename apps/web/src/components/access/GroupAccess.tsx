import { useMemo, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import { apiDetail } from '../../api'
import { setPageParams, useListPage, useListPageQuery } from '../../hooks/useListPage'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { memberName } from '../../lib/memberLabel'
import type { GroupDetail, GroupMember } from '../../types'
import { Banner } from '../../ui/Banner'
import { EmptyState } from '../../ui/EmptyState'
import FeedList from '../../ui/feed/FeedList'
import type { FeedColumn, FeedRowSpec } from '../../ui/feed/types'
import FilterBar from '../../ui/FilterBar'
import FormDialog from '../../ui/FormDialog'
import { FilterRow } from '../../ui/FilterRow'
import ListPager from '../../ui/ListPager'
import { TAP_FLOOR_DOWN } from '../../ui/listStyles'
import { LoadingState } from '../../ui/LoadingState'
import { NoMatchesState } from '../../ui/NoMatchesState'
import SearchBox from '../../ui/SearchBox'
import { useConfirm } from '../ConfirmDialog'
import AddAccessDialog from './AddAccessDialog'
import MembersTable, { toPrincipalRows, type AccessTableRow, type PrincipalBindingRow } from './MembersTable'
import PrincipalMultiSelect from './PrincipalMultiSelect'
import { SCOPE_TYPE_LABELS, useScopeOptions, type ScopeOption } from './ScopePicker'
import {
  addGroupMember,
  listMembers,
  listRoles,
  removeBinding,
  removeGroupMember,
  searchPrincipals,
  type PickerEntry,
} from './api'

// A group's two access tabs: Members (the paged roster `GET /api/groups/{id}/members`, per-member add/remove —
// `POST/DELETE /api/groups/{id}/members/{uid}`)
// and Bindings (the roles the group holds, written through the same Add dialog as a scope's Access tab,
// with this group fixed as the principal).

/* THE MEMBERS PANEL IS SERVER-PAGED (`hooks/useListPage`): a group can hold thousands, so the roster
 * is `GET /api/groups/{id}/members` one page at a time, searched and ordered by the server. Its URL keys carry an
 * `m` prefix: the edit page owns `?tab=` and may grow keys of its own on the same location. */
const MEMBER_PREFIX = 'm'
const MEMBER_Q_KEY = `${MEMBER_PREFIX}q`
const MEMBER_SORTS = ['name', 'email'] as const

/* Email is the one slot: dropped at `FEED_NO_META` (≤899px), where the row's hover carries it (`notes`). */
const MEMBER_COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: { key: 'name', label: 'Name', sortKey: 'name' },
  slots: [{ key: 'email', label: 'Email', sortKey: 'email' }],
}
const EMAIL_SLOT_WIDTH = 260
/* The inline Remove: a small text button, "REMOVE" whole; 44px tall on touch (the ⋯ it replaced had that floor). */
const REMOVE_SLOT_WIDTH = 72
const REMOVE_BUTTON_SX = { minWidth: 0, px: 0.75, py: 0, lineHeight: 1.5, [TAP_FLOOR_DOWN]: { minHeight: 44 } } as const

const MEMBER_SEARCH_SX = { flex: '1 1 220px', minWidth: 190, maxWidth: 360, m: 0 } as const

export function GroupMembersPanel({
  group,
  canWrite,
  onChanged,
}: {
  group: GroupDetail
  canWrite: boolean
  /** Re-reads the group after a write (its `member_count`). */
  onChanged: () => Promise<void>
}): JSX.Element {
  const [addOpen, setAddOpen] = useState(false)
  const [adding, setAdding] = useState<PickerEntry[]>([])
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  // Everyone's membership is the system's: every user of the org, added and removed automatically.
  const managed = group.is_everyone === true
  const editable = canWrite && !managed
  const orgId = group.org_id

  const [params, setFilters] = useUrlFilters()
  const q = params.get(MEMBER_Q_KEY) ?? ''
  const list = useListPage({ prefix: MEMBER_PREFIX, sorts: MEMBER_SORTS, resetKey: q })
  const { page, per, sort, dir } = list
  const path = useMemo(() => {
    const query = new URLSearchParams()
    if (q.trim()) query.set('q', q.trim())
    return `/api/groups/${group.id}/members?${setPageParams(query, { page, per, sort, dir }).toString()}`
  }, [group.id, q, page, per, sort, dir])
  const members = useListPageQuery<GroupMember>({ queryKey: ['group-members', group.id, path], path })
  const loadError = members.error ? apiDetail(members.error, 'Could not load the members.') : ''

  /* After a write, every read of the count moves: this page (the rows), the group (its `member_count`) and the
   * Groups roster's pages (`pages/users/GroupsTab.tsx`), which stay mounted behind an open group. */
  const queryClient = useQueryClient()
  const refresh = () => Promise.all([
    members.refetch(),
    onChanged(),
    queryClient.invalidateQueries({ queryKey: ['groups', 'page'] }),
  ])

  async function add(who: PickerEntry[]) {
    const users = who.filter(p => p.kind === 'user')
    if (!users.length) return
    // An empty fallback: the per-member sentence built below IS the banner (`apiDetail` returns it unchanged).
    await run('', async () => {
      try {
        // One request per member (the API is per member); sequential, so a refusal names who failed.
        for (const u of users) {
          try {
            await addGroupMember(group.id, u.id)
          } catch (err) {
            throw new Error(`${u.name}: ${apiDetail(err, 'could not be added')}`)
          }
        }
        setAdding([])
        setAddOpen(false)
      } finally {
        await refresh()
      }
    })
  }

  async function remove(m: GroupMember) {
    const name = memberName(m)
    const ok = await confirm({
      title: 'Remove member',
      message: `Remove ${name} from ${group.name}? They lose every role the group holds.`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    setError('')
    try {
      await removeGroupMember(group.id, m.id)
    } catch (err) {
      setError(apiDetail(err, 'Could not remove the member.'))
    }
    await refresh()
  }

  const rows: FeedRowSpec[] = members.rows.map((m): FeedRowSpec => {
    const name = memberName(m)
    return {
      key: m.id,
      title: name,
      titleText: name,
      href: `/users/${m.id}`,
      data: { member: m.id },
      // The email's second home where its slot drops; a name that IS the email says it already.
      notes: name !== m.email ? [{ key: 'email', text: m.email, tone: 'muted' }] : undefined,
      slots: [
        { key: 'email', width: EMAIL_SLOT_WIDTH, gate: 'meta', align: 'left', title: m.email, node: m.email, testId: 'col-member-email' },
        /* Remove inline, as the org Access tab's (`MembersTable`): it is the row's only verb, so a ⋯ menu would
         * hold one item. Ungated and not `compact`: it has no second home, so it keeps the phone tap floor. */
        ...(editable ? [{
          key: 'remove',
          width: REMOVE_SLOT_WIDTH,
          align: 'right' as const,
          node: (
            <Button
              size="small"
              color="error"
              data-testid="group-member-remove"
              aria-label={`Remove ${name} from this group`}
              onClick={() => void remove(m)}
              sx={REMOVE_BUTTON_SX}
            >
              Remove
            </Button>
          ),
        }] : []),
      ],
    }
  })

  return (
    <Stack spacing={1.5} sx={{ minWidth: 0 }}>
      {error && !addOpen && <Banner severity="error" onDismiss={() => setError('')}>{error}</Banner>}
      {managed && (
        <Banner severity="info">
          Members are managed automatically.
        </Banner>
      )}
      {/* One button, as a scope's Grant access: the picker opens on demand, so the roster starts near the top. */}
      {editable && (
        <Box>
          <Button variant="contained" data-testid="group-members-open" onClick={() => { setError(''); setAddOpen(true) }}>
            Add member
          </Button>
        </Box>
      )}
      {editable && addOpen && (
        <FormDialog
          title="Add members"
          submitLabel="Add"
          busyLabel="Adding…"
          busy={busy}
          error={error}
          submitDisabled={!adding.length}
          submitTestId="group-members-add"
          onSubmit={() => void add(adding)}
          onClose={() => { setAddOpen(false); setAdding([]); setError('') }}
        >
          {/* Users only (`usersOnly`): a group is not a member of a group, so none is offered. Existing members are NOT filtered out: the
              console holds one page of members, never all of them, and hiding only the visible page's would
              make the picker's answer depend on which page is open. The server is authoritative instead —
              adding a member already in the group is a no-op there. */}
          <PrincipalMultiSelect
            key={orgId}
            scope={{ type: 'org', id: orgId }}
            value={adding}
            onChange={setAdding}
            onEnterComplete={who => void add(who)}
            usersOnly
            autoFocus
          />
        </FormDialog>
      )}
      {/* The search only once there is something to search: a group with no members has no roster to narrow. */}
      {(q || (members.total ?? 0) > 0) && (
        <FilterBar>
          <FilterRow>
            <SearchBox
              value={q}
              onChange={next => setFilters({ [MEMBER_Q_KEY]: next })}
              label="Search"
              placeholder="Name or email"
              ariaLabel="Search members"
              testId="group-member-search"
              sx={MEMBER_SEARCH_SX}
            />
          </FilterRow>
        </FilterBar>
      )}
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {members.loading && members.total === null ? (
        <LoadingState rows={3} />
      ) : members.total === 0 ? (
        q ? (
          <NoMatchesState
            title="No matching members"
            clearLabel="Clear search"
            onClear={() => setFilters({ [MEMBER_Q_KEY]: '' })}
            testId="group-members-clear"
          />
        ) : (
          <EmptyState title="No members." />
        )
      ) : members.total !== null ? (
        <Box>
          <FeedList
            ariaLabel="Members"
            testId="group-members"
            sections={[{ key: 'members', rows }]}
            columns={MEMBER_COLUMNS}
            sort={{ key: list.sort, dir: list.dir }}
            onSort={list.toggleSort}
          />
          <ListPager page={page} per={per} total={members.total} onPage={list.setPage} onPer={list.setPer} testId="group-members-pager" />
        </Box>
      ) : null}
      {confirmDialog}
    </Stack>
  )
}

/** Where this group holds a role: `GET /api/access/members` over every scope of its org, keeping the rows
 *  bound here to this group. No principal-keyed read exists, so the scopes are walked — the org's rows
 *  first, the rest in parallel; a scope the reader cannot read is skipped. */
/** How many scope reads the walk keeps in flight at once. */
const WALK_CONCURRENCY = 6

async function groupBindings(
  groupId: string,
  scopes: ScopeOption[],
): Promise<{ rows: PrincipalBindingRow[]; unread: number }> {
  let unread = 0
  const readOne = async (scope: ScopeOption) => {
    let rows: Awaited<ReturnType<typeof listMembers>>
    try {
      rows = await listMembers({ type: scope.type, id: scope.id })
    } catch {
      unread += 1
      return []
    }
    return rows
      .filter(r => r.principal.kind === 'group' && r.principal.id === groupId && r.source.kind === 'direct')
      .map(({ principal: _p, ...rest }): PrincipalBindingRow => ({
        ...rest,
        scope: {
          type: scope.type,
          id: scope.id,
          // The type leads, so a name sort groups the rows by kind of scope.
          name: `${SCOPE_TYPE_LABELS[scope.type]} · ${scope.name}`,
        },
      }))
  }
  const out: PrincipalBindingRow[][] = new Array(scopes.length)
  let next = 0
  await Promise.all(Array.from({ length: Math.min(WALK_CONCURRENCY, scopes.length) }, async () => {
    while (next < scopes.length) {
      const i = next++
      out[i] = await readOne(scopes[i])
    }
  }))
  return { rows: out.flat(), unread }
}

export function GroupBindingsPanel({ group }: { group: GroupDetail }): JSX.Element {
  const orgId = group.org_id
  const { options, loading } = useScopeOptions(orgId)
  const roles = useApiRead(['access-roles'], listRoles, 'Could not load roles.')
  // Only the rows known to be in this group's org: a membership never crosses one, nor does a binding.
  const scopes = useMemo(
    () => (!loading ? options.filter(o => o.orgId === orgId || o.type === 'account') : null),
    [orgId, loading, options],
  )
  const bindings = useApiRead(
    ['group-access-bindings', group.id, scopes],
    () => groupBindings(group.id, scopes ?? []),
    'Could not load this group’s bindings.',
    { enabled: scopes !== null },
  )
  // The same probe as a scope's Access tab: 200 → may manage the org's bindings (a refusal keeps the answer).
  const canManage = useApiRead(
    ['group-access-can-manage', orgId],
    () => searchPrincipals({ type: 'org', id: orgId }, '').then(() => true),
    'Could not check access.',
  ).data === true
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')
  const [confirm, confirmDialog] = useConfirm()

  const operator = useAuth().admin?.is_operator === true
  const rows = useMemo(() => toPrincipalRows(bindings.data?.rows ?? [], operator), [bindings.data, operator])
  const roleRank = useMemo(() => Object.fromEntries((roles.data ?? []).map(r => [r.slug, r.rank])), [roles.data])

  async function remove(row: AccessTableRow) {
    const ok = await confirm({
      title: 'Remove binding',
      message: `Remove ${group.name}'s ${row.role.name} on ${row.subject.name}? Its members lose access granted by this binding.`,
      confirmLabel: 'Remove',
      destructive: true,
    })
    if (!ok) return
    setError('')
    try {
      await removeBinding(row.bindingId)
    } catch (err) {
      setError(apiDetail(err, 'Could not remove the binding.'))
      return
    }
    await bindings.reload()
  }

  const loadError = bindings.error || roles.error
  const unread = bindings.data?.unread ?? 0
  const principal: PickerEntry = { kind: 'group', id: group.id, name: group.name }

  return (
    <Stack spacing={1} sx={{ minWidth: 0 }}>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {error && <Banner severity="error" onDismiss={() => setError('')}>{error}</Banner>}
      {canManage && (
        <Box>
          <Button variant="contained" data-testid="access-add" disabled={!roles.data || !scopes} onClick={() => setAdding(true)}>
            Grant access
          </Button>
        </Box>
      )}
      {unread > 0 && (
        <Banner severity="warning">
          {unread === 1 ? '1 scope' : `${unread} scopes`} could not be read, so this list may be incomplete.
        </Banner>
      )}
      {bindings.data === null ? (
        <LoadingState rows={3} />
      ) : rows.length === 0 ? (
        <EmptyState title="No bindings." />
      ) : (
        <MembersTable rows={rows} mode="principal" roleRank={roleRank} onRemove={canManage ? remove : undefined} />
      )}
      {adding && scopes && (
        <AddAccessDialog
          principal={principal}
          roles={roles.data ?? []}
          scopeOptions={scopes}
          scopeLoading={false}
          onClose={() => setAdding(false)}
          onAdded={bindings.reload}
        />
      )}
      {confirmDialog}
    </Stack>
  )
}
