import { useCallback, useEffect, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { Theme } from '@mui/material/styles'
import { useLocation, useNavigate } from 'react-router-dom'
import { apiDetail, del, patch, post } from '../../api'
import { useAuth } from '../../auth'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { useConfirm } from '../../components/ConfirmDialog'
import { countOf } from '../../lib/humanize'
import { NO_VALUE } from '../../lib/relativeTime'
import { useRowSelection } from '../../hooks/useRowSelection'
import { fullName, NO_NAME } from '../../lib/memberLabel'
import { EVERYONE_REASON, groupLabel, type ManagedUser } from '../../types'
import { CompactVerbLabel, compactVerbProps } from '../../ui/CompactVerb'
import { DeclinedChip, DisabledChip, ScimChip } from '../../ui/MarkChip'
import Banner from '../../ui/Banner'
import BulkActionBar, { type BulkAction } from '../../ui/BulkActionBar'
import BulkRunReports from '../../ui/BulkRunReports'
import ListStates from '../../ui/feed/ListStates'
import type { FeedColumn, FeedRowSpec, FeedSlot, OverflowItem } from '../../ui/feed/types'
import { deleteItem } from '../../components/tasks/rowActions'
import { ListPager } from '../../ui/ListPager'
import { PEOPLE_LIST_CONTAINER_SX, PEOPLE_NO_GROUPS, PHONE_QUERY, TAP_FLOOR_DOWN, tapTargetSx } from '../../ui/listStyles'
import { SelectAllBox, SelectRowBox, selectSlot } from '../../ui/RowSelect'
import NoMatchesState from '../../ui/NoMatchesState'
import PageHeader from '../../ui/PageHeader'
import { setPageParams, useListPage, useListPageQuery } from '../../hooks/useListPage'
import { useBulkRuns } from '../../hooks/useBulkRuns'
import { bulkIdentity } from '../../lib/bulkRun'
import { useRowActions } from '../../hooks/useBusyIds'
import GroupPickDialog, { type PickedGroup } from './GroupPickDialog'
import { UserFilterStrip } from './UserFilterStrip'
import { hasUserFilters, userFilterParams, useUserFilters } from './userFilters'
import { NEW_USER_NOTICE, type Notice } from './userCreateNotice'
import { PRESS_SWAP_SX, PressSwap } from '../../ui/pressSwap'

/* DIRECTORY > PEOPLE — the account roster, first panel of `UsersPage.tsx`. No `PageHeader` title: the
 * Directory tab names the page; Add user rides the Directory tab strip and opens `/users/new`
 * (`UserNewPage.tsx`), whose outcome comes back in router state and shows in the notice banner.
 * Write controls render only for an org admin (tenant or operator — the server scopes which org's rows
 * they reach); a `users:view` holder reads the same roster.
 * SERVER-PAGED: `GET /api/users` answers one page of the filtered, sorted set and its `total`
 * (`hooks/useListPage`); the strip's filters, the sorted column and the page all live in the URL. Nothing
 * here holds the whole roster, so nothing here counts over it — the last-superadmin floor is the server's
 * alone (`assert_superadmin_floor`), its refusal shown in its own words.
 * Picked rows (the Select slot, org admins only) feed the bulk bar (`ui/BulkActionBar`): the per-row calls
 * fanned out over the selection (`hooks/useBulkRuns.ts`), never a bulk endpoint, as `pages/Nodes.tsx`. */

/* The server's sort vocabulary (`routes/users.py::_USER_SORTS`). */
const USER_SORTS = ['email', 'name', 'groups', 'admin'] as const
/** A tenant has no Superadmin column, so no sort by it either. */
const TENANT_SORTS = ['email', 'name', 'groups'] as const

/* The one inline row control, priced for a feed row: no 44px vertical floor (it would double the row),
 * but WCAG 2.5.8's 24x24 minimum across the whole touch band, like `ui/feed/FeedList.tsx`'s ⋯. Keyed on
 * `TAP_FLOOR_DOWN` (≤599.95px), not `PHONE`: `theme.ts` treats below-`sm` as touch but raises no
 * `MuiButton`, so 481–599px would sit at 20px (`ui/feed/geometry.ts`'s `DISCLOSURE_TOUCH_MIN`). It matters
 * more here: superadmin has no ⋯ fallback. `minWidth: 0` keeps MUI's 64px out of the slot. */
/* ONE MARK SIZE for every row mark — the title's marks (SCIM, Declined, Disabled) and the Admin slot's tag: a status
   pill's height and label size (`ui/StatusChip.tsx`), still whole in the 56px narrow slot (0.625rem was
   too small to read). `MarkChip` keeps MUI's 24px elsewhere; this roster sizes its own (`MARKS_SX`). */
const MARK_HEIGHT = 20
const MARK_FONT_SIZE = '0.6875rem'
const MARKS_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  flexShrink: 0,
  '& .MuiChip-root': { height: MARK_HEIGHT },
  '& .MuiChip-label': { fontSize: MARK_FONT_SIZE },
} as const
const SUPERADMIN_SX = {
  width: '100%',
  minWidth: 0,
  px: 0.5,
  py: 0,
  minHeight: MARK_HEIGHT,
  fontSize: MARK_FONT_SIZE,
  lineHeight: 1.4,
  [TAP_FLOOR_DOWN]: { minHeight: 24 },
} as const

/* ONE ACCENT: "yes" alone is outlined, in the primary ink, kept (dimmed) on the signed-in operator's own chip,
   which cannot be revoked; "no" is muted text with no button chrome — still the toggle, `aria-pressed`. */
/* Dark: MUI's half-alpha `primary.main` outline reads ~2:1 on the dark page (AA wants 3:1), and the ink itself 3.98:1 on
   dark paper (AA text wants 4.5:1); `primary.light` at full ink reads 6.4:1 and clears both. */
const SUPERADMIN_ADMIN_SX = [
  {
    ...SUPERADMIN_SX,
    '&.Mui-disabled': { color: 'primary.main', borderColor: 'primary.main', opacity: 0.6 },
  },
  (theme: Theme) => theme.applyStyles('dark', {
    '&, &.Mui-disabled': { color: 'primary.light', borderColor: 'primary.light' },
  }),
] as const
const SUPERADMIN_STANDARD_SX = { ...SUPERADMIN_SX, color: 'text.secondary' } as const
// What the flag shows while pointed at: the change a click makes (`ui/pressSwap.tsx`).
const REVOKE_PRESS = { glyph: '⊘', label: 'Revoke' }
const GRANT_PRESS = { glyph: '✓', label: 'Grant' }

/** The muted "(you)" beside the signed-in operator's address: never cut, so a cut address still says whose it is. */
const SELF_SX = { color: 'text.secondary', whiteSpace: 'nowrap', flexShrink: 0 } as const
const SELF_TAG = '\u00a0(you)'

/* ONE LINE PER ADDRESS: the address ellipsises, whole on hover, so a narrow roster never splits it mid-domain;
   "(you)" and the marks keep their width beside it. */
const EMAIL_ROW_SX = { display: 'flex', alignItems: 'center', minWidth: 0, maxWidth: '100%' } as const
const EMAIL_SX = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const

/* The groups count: wide enough for its header word ("GROUPS", ~44px at the header's 11px), not just the digits. */
const GROUPS_SLOT = 52

/** A bulk run's row word: the address (`BulkRunOptions.identity`). Module-level, so `runBulk` keeps its identity. */
const userIdentity = (u: ManagedUser): string => u.email

/* The bulk verbs as one closed set: each id keys the button, its greying run and its report. */
const BULK_ENABLE = 'enable'
const BULK_DISABLE = 'disable'
const BULK_ADD_GROUP = 'add-group'
const BULK_REMOVE_GROUP = 'remove-group'
const BULK_DELETE = 'delete'
type BulkVerbId =
  | typeof BULK_ENABLE | typeof BULK_DISABLE | typeof BULK_ADD_GROUP | typeof BULK_REMOVE_GROUP | typeof BULK_DELETE

/* The header row (`FeedList`'s `columns`). A count and a flag read best biggest/granted first. */
const TITLE_COLUMN: FeedColumn = { key: 'email', label: 'Email', sortKey: 'email' }
const SLOT_COLUMNS: FeedColumn[] = [
  { key: 'name', label: 'Name', sortKey: 'name' },
  { key: 'groups', label: 'Groups', sortKey: 'groups', firstDir: 'desc' },
  /* The narrow roster's 56px slot holds "Admin" (the toggle's own word, "Grant admin access"), never a cut-off
     "Super"; the hover and the reader keep the whole word. */
  {
    key: 'superadmin',
    label: (
      <>
        <Box component="span" sx={{ [PEOPLE_NO_GROUPS]: { display: 'none' } }}>Superadmin</Box>
        <Box component="span" sx={{ display: 'none', [PEOPLE_NO_GROUPS]: { display: 'inline' } }}>Admin</Box>
      </>
    ),
    ariaLabel: 'Superadmin',
    sortKey: 'admin',
    firstDir: 'desc',
  },
]

/** Does the account hold a membership of this group (by name: a membership row carries the group's name)? */
const inGroup = (u: ManagedUser, group: PickedGroup) => u.memberships.some(m => m.group === group.name)

function membershipLabel(m: { resource_type: string | null; resource_id: string | null }): string {
  if (!m.resource_type && !m.resource_id) return 'global'
  // Scope fields are independently optional server-side, so render a half-filled one plainly.
  if (m.resource_type && !m.resource_id) return m.resource_type
  if (!m.resource_type && m.resource_id) return m.resource_id
  return `${m.resource_type}:${m.resource_id}`
}

type MembershipForm = { user: ManagedUser; loading: boolean; error: string }

export default function UsersTab() {
  const { admin } = useAuth()
  // Writes are `@org_admin` (`routes/users.py`); the bypass flag is the operator's alone
  // (`_refuse_superadmin_subject`: only a superadmin changes it, and a tenant org holds none).
  const canWrite = isOrgAdminSeat(admin)
  const isOperator = admin?.is_operator === true

  /* THE READ: filters (URL) → page state (URL) → one page. A tenant's stale `?org=` is ignored, not sent: the
     control is the operator's alone. The serialized filters are the page's reset key — a narrowed set starts
     on page one. */
  const [urlFilters, patchFilters, clearFilters] = useUserFilters()
  const filters = isOperator ? urlFilters : { ...urlFilters, org: '' }
  const filtered = hasUserFilters(filters)
  const list = useListPage({ sorts: isOperator ? USER_SORTS : TENANT_SORTS, resetKey: JSON.stringify(filters) })
  const path = `/api/users?${setPageParams(userFilterParams(filters, { operator: isOperator }), list)}`
  const { rows: users, total, loading, error: loadError, refetch } = useListPageQuery<ManagedUser>({
    queryKey: ['users', 'page', path],
    path,
  })
  const fetchUsers = useCallback(async () => { await refetch() }, [refetch])

  const [notice, setNotice] = useState<Notice | null>(null)
  const [membershipForm, setMembershipForm] = useState<MembershipForm | null>(null)
  const [groupBulk, setGroupBulk] = useState<'add' | 'remove' | null>(null)
  const [confirm, confirmDialog] = useConfirm()
  const { busy: busyIds, withBusy, run, error: actionError, setError: setActionError } = useRowActions<string>()
  const location = useLocation()
  const navigate = useNavigate()

  /* `UserNewPage`'s outcome, handed over in router state; cleared so a reload does not re-show it. The
     roster stays mounted behind that editor, so the handoff also re-reads it: the new row is the news. */
  useEffect(() => {
    const state = location.state as Record<string, Notice | null | undefined> | null
    if (!state || !(NEW_USER_NOTICE in state)) return
    const handoff = state[NEW_USER_NOTICE]
    if (handoff) setNotice(handoff)
    setActionError('')
    void fetchUsers()
    navigate(location.pathname + location.search, { replace: true, state: null })
  }, [location, navigate])

  async function handleAddMembership(group: PickedGroup) {
    if (!membershipForm) return
    const { user } = membershipForm
    setMembershipForm(f => (f ? { ...f, loading: true, error: '' } : null))
    try {
      /* By NAME, resolved in the user's own org (`routes/users.py::add_membership`). */
      await post(`/api/users/${user.id}/memberships`, { group: group.name })
      setMembershipForm(null)
      await fetchUsers()
    } catch (err) {
      setMembershipForm(f => (f ? { ...f, loading: false, error: apiDetail(err, 'Failed to add membership') } : null))
    }
  }

  // `userId` is a uuid; `membershipId` is a `user_group_memberships` row id, still an integer.
  function removeMembership(userId: string, membershipId: number) {
    return run(userId, 'Failed to remove membership', async () => {
      await del(`/api/users/${userId}/memberships/${membershipId}`)
      await fetchUsers()
    })
  }

  /* The app's modal confirm: a feed row is one line by contract (as `pages/Nodes.tsx`). */
  async function confirmAndDelete(user: ManagedUser) {
    const ok = await confirm({
      title: 'Delete user',
      identity: user.email,
      message: `Delete ${user.email}? The account and every group membership it holds go with it.`,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    await run(user.id, 'Failed to delete user', async () => {
      await del(`/api/users/${user.id}`)
      await fetchUsers()
    })
  }

  /* POST `/{id}/invitation`: 409 when already activated, disabled or email unconfigured; 502 on a Gmail
     failure — the server's sentence is shown whole either way. */
  function resendInvitation(user: ManagedUser) {
    setNotice(null)
    return withBusy(user.id, async () => {
      try {
        const { sent_to } = await post<{ sent_to: string }>(`/api/users/${user.id}/invitation`)
        setNotice({ severity: 'success', text: `Invitation sent to ${sent_to}` })
      } catch (err) {
        setNotice({ severity: 'error', text: apiDetail(err, `Failed to send the invitation to ${user.email}`) })
      }
    })
  }

  function setSuperadmin(user: ManagedUser, value: boolean) {
    return run(user.id, 'Failed to update admin access', async () => {
      await patch(`/api/users/${user.id}`, { is_superadmin: value })
      await fetchUsers()
    })
  }

  /* Why a write is refused, in words ('' if not): feeds whether it acts, its name and its hover
     (`OverflowItem`'s `disabled` + `disabledReason`). Only what this row alone decides: the last-superadmin
     floor counts the whole directory, which no page holds, so the server refuses it (`assert_superadmin_floor`)
     and its words reach the banner. Both ids are UUID STRINGS: a mixed comparison would say "not me" and
     silently re-enable self-delete. */
  const isSelf = (user: ManagedUser) => user.id === admin?.id
  const deleteBlockedReason = (user: ManagedUser): string => (isSelf(user) ? 'you cannot delete your own account' : '')
  const demoteBlockedReason = (user: ManagedUser): string =>
    user.is_superadmin && isSelf(user) ? 'you cannot revoke your own admin access' : ''

  /* ── THE SELECTION AND ITS VERBS ──────────────────────────────────────────────────────────────────
     Picked rows by uuid, PER PAGE: pruned to the rows on screen, so a page turn, a filter or a delete that
     moves a row out of sight drops it — a bulk verb never acts on rows nobody can see. */
  const canSelect = canWrite
  const shownIds = useMemo(() => new Set(users.map(u => u.id)), [users])
  const {
    selected, toggle: toggleSelected, clear, toggleAll: toggleSelectAll, allShownSelected, someShownSelected,
  } = useRowSelection(shownIds)
  /* The phone's select mode: the Select column is hidden there until a pick starts, so a
     strip button shows it; Done ends the mode and drops the pick. Wider screens always show the column. */
  const phone = useMediaQuery(PHONE_QUERY)
  const [picking, setPicking] = useState(false)
  const clearSelection = useCallback(() => { clear(); setPicking(false) }, [clear])
  /* Derived from the rows as they are NOW, so each verb decides against current state. */
  const selectedUsers = users.filter(u => selected.has(u.id))

  const { runs, reports, runBulk, runReason, verb } = useBulkRuns<ManagedUser, BulkVerbId>({
    identity: userIdentity, onBatchClean: clearSelection,
  })
  /* Each verb's eligible rows, by the same rule as the row's own control; the server refuses the rest
     (a superadmin a tenant may not touch, an owner an admin may not) and the report says so per row. */
  const enableable = selectedUsers.filter(u => u.disabled_at !== null)
  const disableable = selectedUsers.filter(u => u.disabled_at === null && !isSelf(u))
  const deletable = selectedUsers.filter(u => !isSelf(u))

  /* The per-row calls the user page makes (`UserEditPage`'s disable/enable, the row's delete) and the group's
     own member routes — by group ID, exact: a membership never crosses an org, and the server says so per row. */
  const bulkEnable = (u: ManagedUser) => post(`/api/users/${u.id}/enable`)
  const bulkDisable = (u: ManagedUser) => post(`/api/users/${u.id}/disable`)
  const bulkDelete = (u: ManagedUser) => del(`/api/users/${u.id}`)

  function runGroupBulk(mode: 'add' | 'remove', group: PickedGroup) {
    setGroupBulk(null)
    if (mode === 'add') {
      const rows = selectedUsers.filter(u => !inGroup(u, group))
      void runBulk(BULK_ADD_GROUP, { doing: `Adding to ${group.name}`, done: `Added to ${group.name}` }, rows,
        u => post(`/api/groups/${group.id}/members/${u.id}`)).then(fetchUsers)
    } else {
      const rows = selectedUsers.filter(u => inGroup(u, group))
      void runBulk(BULK_REMOVE_GROUP, { doing: `Removing from ${group.name}`, done: `Removed from ${group.name}` }, rows,
        u => del(`/api/groups/${group.id}/members/${u.id}`)).then(fetchUsers)
    }
  }

  /* Inline: Enable, Disable, Delete (the bar keeps three, `BulkActionBar`'s `maxInline`); the group verbs ride its ⋯. */
  const bulkActions: BulkAction[] = canSelect ? [
    verb({
      id: BULK_ENABLE, label: 'Enable', icon: '✓', rows: enableable, noneWhy: 'No picked user is disabled',
      doing: 'Enabling', done: 'Enabled', run: bulkEnable, after: fetchUsers,
    }),
    verb({
      id: BULK_DISABLE, label: 'Disable', icon: '⊘', destructive: true, rows: disableable, noneWhy: 'No picked user can be disabled',
      confirm: () => confirm({
        title: `Disable ${countOf(disableable.length, 'user')}?`,
        message: 'Each is signed out and cannot sign in until enabled.',
        identity: bulkIdentity(disableable, userIdentity),
        confirmLabel: 'Disable',
        destructive: true,
      }),
      doing: 'Disabling', done: 'Disabled', run: bulkDisable, after: fetchUsers,
    }),
    verb({
      id: BULK_DELETE, label: 'Delete', icon: '🗑', destructive: true, rows: deletable, noneWhy: 'No picked user can be deleted',
      confirm: () => confirm({
        title: `Delete ${countOf(deletable.length, 'user')}?`,
        message: 'Each account and every group membership it holds go with it.',
        identity: bulkIdentity(deletable, userIdentity),
        confirmLabel: 'Delete',
        destructive: true,
        typed: { name: String(deletable.length), action: 'the delete' },
      }),
      doing: 'Deleting', done: 'Deleted', run: bulkDelete, after: fetchUsers,
    }),
    {
      id: BULK_ADD_GROUP,
      label: 'Add to group',
      icon: '+',
      disabled: !!runs[BULK_ADD_GROUP],
      disabledReason: runReason(BULK_ADD_GROUP),
      onRun: () => setGroupBulk('add'),
    },
    {
      id: BULK_REMOVE_GROUP,
      label: 'Remove from group',
      icon: '−',
      disabled: !!runs[BULK_REMOVE_GROUP],
      disabledReason: runReason(BULK_REMOVE_GROUP),
      onRun: () => setGroupBulk('remove'),
    },
  ] : []

  /* The roster as feed rows: memberships become a COUNT (full `group (scope)` strings on the slot's
     `title`, verbs in the ⋯); Delete stays a disabled ⋯ item where it cannot run. The superadmin TOGGLE
     stays inline — a menu would hide the state the roster is scanned for. */
  /* A page where nobody has a name drops the Name column rather than print a 150px column of dashes. */
  const anyName = users.some(u => fullName(u))
  const userRows: FeedRowSpec[] = users.map((u): FeedRowSpec => {
    const self = isSelf(u)
    const disabled = u.disabled_at !== null
    const deleteBlocked = deleteBlockedReason(u)
    const demoteBlocked = demoteBlockedReason(u)
    const flag = u.is_superadmin ? 'yes' : 'no'
    const busy = busyIds.has(u.id)
    const memberships = u.memberships.map(m => `${groupLabel({ name: m.group, is_everyone: m.is_everyone }, isOperator)} (${membershipLabel(m)})`)
    // The count leaves out Everyone: every account of the org holds it, so it would only add 1 to every row.
    const groupCount = u.memberships.filter(m => !m.is_everyone).length
    const declined = u.invitation_status === 'declined'
    const invitable = u.invitation_status ? true : u.activated_at === null

    const actions: OverflowItem[] = [
      {
        key: 'add-membership',
        icon: '+',
        label: 'Add membership',
        // Every item names its row (one menu per account).
        ariaLabel: `Add a membership for ${u.email}`,
        onClick: () => setMembershipForm({ user: u, loading: false, error: '' }),
      },
        // One item per membership, keyed on its row id (what DELETE takes), so duplicates revoke separately.
      ...u.memberships.map((m, i) => {
        const group = groupLabel({ name: m.group, is_everyone: m.is_everyone }, isOperator)
        return {
          key: `revoke-${m.id}`,
          icon: '×',
          // An unscoped membership's verb names the group alone; a scoped one keeps its scope.
          label: m.resource_type || m.resource_id ? `Revoke ${memberships[i]}` : `Revoke ${group}`,
          // The accessible name the old chip's revoke button carried, unchanged for oracles.
          ariaLabel: `Revoke the ${group} membership`,
          // Everyone holds every user of the org; the server refuses the revoke (409).
          disabled: m.is_everyone === true,
          disabledReason: m.is_everyone ? EVERYONE_REASON : undefined,
          onClick: () => { if (!m.is_everyone) void removeMembership(u.id, m.id) },
        }
      }),
      /* Only while an invitation is open or was declined (`invitation_status`); without one, an account
         nobody has claimed yet — activation is what the invitation asks for. */
      ...(invitable ? [{
        key: 'resend-invitation',
        icon: '✉',
        label: declined ? 'Reinvite' : 'Resend',
        ariaLabel: `Resend the invitation to ${u.email}`,
        disabled: disabled,
        disabledReason: disabled ? 'the account is disabled' : undefined,
        onClick: () => { if (!disabled) void resendInvitation(u) },
      }] : []),
      // Names the account; `RowActionsOverflow` appends the reason when disabled. `disabled` is the
      // affordance; the guard is the invariant.
      /* The tick's second home: the phone hides the Select column until a pick starts (`peopleSelectIdle`). */
      ...(canSelect ? [{
        key: 'pick',
        icon: selected.has(u.id) ? '☑' : '☐',
        label: selected.has(u.id) ? 'Deselect' : 'Select',
        ariaLabel: `${selected.has(u.id) ? 'Deselect' : 'Select'} ${u.email}`,
        onClick: () => toggleSelected(u.id),
      }] : []),
      deleteItem(u.email, () => { if (!deleteBlocked) void confirmAndDelete(u) }, { disabled: Boolean(deleteBlocked), disabledReason: deleteBlocked }),
    ]

    const name = fullName(u)
    const slots: FeedSlot[] = [
      ...(anyName ? [{
        /* The name PAIR ("First Last", `lib/memberLabel.ts::fullName`) — the one label a person has; no
           email fallback here, the Email column already prints it. Both blank → em-dash. */
        key: 'name',
        width: 150,
        gate: 'peopleName' as const,
        align: 'left' as const,
        testId: 'col-user-name',
        /* The Email title's size (`FeedList`'s `TITLE_SX`, 13px): a name is read as the address is, not as a meta
           value. The phone's 12px never applies — the slot is gone below 900px (`peopleName`). */
        fontSize: '0.8125rem',
        /* A nameless account prints the placeholder, dimmed; the slot keeps its width (`ui/feed/geometry.ts`). */
        title: name || NO_NAME,
        dim: !name,
        node: name || NO_VALUE,
      }] : []),
      {
        key: 'groups',
        width: GROUPS_SLOT,
        gate: 'peopleGroups',
        align: 'center',
        // At the email's body size, not the cluster's 11px: a three-digit count still fits the slot.
        fontSize: '0.8125rem',
        testId: 'col-user-groups',
        // One `group (scope)` per membership, in server order.
        title: memberships.length > 0
          ? `Groups / scopes: ${memberships.join(' · ')}`
          : 'No group memberships',
        // A count, so none is 0, not a dash.
        node: groupCount,
      },
      /* The platform superadmin flag: the operator's alone to read and set (a tenant org holds none), so a tenant
         never sees the column. */
      ...(isOperator ? [{
        key: 'superadmin',
        width: 100,
        gate: 'peopleAdmin' as const,
        align: 'center' as const,
        testId: 'col-user-superadmin',
        node: (
          <Button
            type="button"
            variant={u.is_superadmin ? 'outlined' : 'text'}
            color={u.is_superadmin ? 'primary' : 'inherit'}
            disabled={busy || Boolean(demoteBlocked)}
            aria-pressed={u.is_superadmin}
            title={
              u.is_superadmin
                ? (demoteBlocked ? `Cannot revoke — ${demoteBlocked}` : 'Revoke admin access')
                : 'Grant admin access'
            }
            aria-label="Superadmin"
            onClick={() => void setSuperadmin(u, !u.is_superadmin)}
            sx={[...(u.is_superadmin ? SUPERADMIN_ADMIN_SX : [SUPERADMIN_STANDARD_SX]), busy || demoteBlocked ? null : PRESS_SWAP_SX]}
          >
            {/* Pointed at, the flag shows the change a click makes (`ui/pressSwap.tsx`). */}
            {busy || demoteBlocked
              ? flag
              : <PressSwap rest={flag} press={u.is_superadmin ? REVOKE_PRESS : GRANT_PRESS} />}
          </Button>
        ),
      }] : []),
      /* SELECT, last (`ui/RowSelect.tsx`): the tick that feeds the bulk bar. Off the narrow roster above the phone
         (`peopleSelect`: beside an open user, where its width goes to the email); the phone shows it only while a
         pick is in progress (`peopleSelectIdle`), started from the strip's Select or a row's ⋯ menu. Its column
         header is the select-all tick, so the two line up by construction. */
      ...(canSelect ? [selectSlot('col-user-select',
        <SelectRowBox testId="user-select" checked={selected.has(u.id)} onChange={() => toggleSelected(u.id)} name={u.email} />,
        picking || selected.size > 0 ? 'peopleSelect' : 'peopleSelectIdle',
      )] : []),
    ]

    return {
      key: u.id,
      indicator: {
        /* The mark is whether a human has CLAIMED the account (invites activate by Google-linking) — not
           superadmin, which has its own toggle. `● / ○` = filled live, hollow declared (as `/nodes`); a disabled
           account's disc is muted, never green — it cannot sign in. */
        tone: !disabled && !u.invitation_status && u.activated_at ? 'success' : 'neutral',
        glyph: !u.invitation_status && u.activated_at ? '●' : '○',
        /* An org invitation (`invitation_status`) outranks `activated_at`: the person may hold a live
           account elsewhere and still not have joined this org. */
        label: u.invitation_status === 'pending' ? 'invited — not accepted yet'
          : declined ? 'invitation declined'
            : u.activated_at ? 'activated' : 'invited — not activated yet',
        data: { activated: !u.invitation_status && Boolean(u.activated_at) },
      },
      /* The address is the title (the name pair may be blank); "(you)" and the marks are separate elements beside it. */
      title: (
        <Box component="span" sx={EMAIL_ROW_SX}>
          <Box component="span" title={u.email} sx={EMAIL_SX}>{u.email}</Box>
          {self && <Box component="span" sx={SELF_SX}>{SELF_TAG}</Box>}
          {(u.scim_managed || declined || disabled) && (
            <Box component="span" sx={MARKS_SX}>
              {u.scim_managed && <ScimChip />}
              {declined && <DeclinedChip />}
              {disabled && <DisabledChip />}
            </Box>
          )}
        </Box>
      ),
      titleText: `${u.email}${self ? ' (you)' : ''}${declined ? ' (declined)' : ''}${disabled ? ' (disabled)' : ''}`,
      href: `/users/${u.id}`,
      busy,
      data: { superadmin: u.is_superadmin, self, disabled },
      slots,
      actions: canWrite ? actions : undefined,
    }
  })

  /* The Select column's header IS the select-all tick (this page's rows only), so it sits over the rows'. */
  const slotColumns = SLOT_COLUMNS.filter(c => (anyName || c.key !== 'name') && (isOperator || c.key !== 'superadmin'))
  const columns = {
    title: TITLE_COLUMN,
    slots: canSelect ? [...slotColumns, {
      key: 'select',
      label: (
        <SelectAllBox
          testId="users-select-all"
          allSelected={allShownSelected}
          someSelected={someShownSelected}
          onChange={toggleSelectAll}
          shown={`${shownIds.size} users on this page`}
        />
      ),
    }] : slotColumns,
  }

  /* What the bulk group dialog would do for a pick, from the rows as they are now. */
  const groupBulkRows = (mode: 'add' | 'remove', group: PickedGroup) =>
    selectedUsers.filter(u => (mode === 'add' ? !inGroup(u, group) : inGroup(u, group)))

  const error = actionError || (loadError ? apiDetail(loadError, 'Failed to load users') : '')

  return (
    /* The roster's own width gates its slots (`PEOPLE_LIST_CONTAINER`): an open user beside it narrows it. */
    <Box sx={PEOPLE_LIST_CONTAINER_SX}>
      {/* Titleless: its action rides the People sub-strip (or the Directory strip when it is the only tab) (`components/TabActionsSlot`). */}
      <PageHeader
        actions={canWrite ? (
          <Stack direction="row" spacing={1}>
            {phone && canSelect && (
              <Button
                variant="outlined"
                data-testid="users-select-mode"
                aria-pressed={picking || selected.size > 0}
                onClick={() => (picking || selected.size > 0 ? clearSelection() : setPicking(true))}
                sx={tapTargetSx}
              >
                {picking || selected.size > 0 ? 'Done' : 'Select'}
              </Button>
            )}
            <Button variant="contained" color="primary" onClick={() => navigate('/users/new')} {...compactVerbProps('Add user', tapTargetSx)}>
              <CompactVerbLabel label="Add user" />
            </Button>
          </Stack>
        ) : undefined}
      />

      <UserFilterStrip filters={filters} onChange={patchFilters} operator={isOperator} />

      {error && <Banner severity="error">{error}</Banner>}
      {notice && (
        <Banner severity={notice.severity} testId="invitation-notice" onDismiss={() => setNotice(null)}>
          {notice.text}
        </Banner>
      )}

      {canSelect && (
        <>
          <BulkActionBar count={selectedUsers.length} onClear={clearSelection} actions={bulkActions} label="Bulk user actions" />
          <BulkRunReports actions={bulkActions} runs={runs} reports={reports} />
        </>
      )}

      {membershipForm && (
        <GroupPickDialog
          title="Add membership"
          submitLabel="Add"
          busyLabel="Adding…"
          busy={membershipForm.loading}
          error={membershipForm.error}
          /* No scope inputs: the request body takes none (the users routes' `add_membership`). */
          blockedReason={group => (inGroup(membershipForm.user, group) ? 'Already a member' : '')}
          onSubmit={group => void handleAddMembership(group)}
          onClose={() => setMembershipForm(null)}
          testId="add-membership-dialog"
        />
      )}

      {groupBulk && (
        <GroupPickDialog
          title={groupBulk === 'add' ? `Add ${countOf(selectedUsers.length, 'user')} to group` : `Remove ${countOf(selectedUsers.length, 'user')} from group`}
          submitLabel={groupBulk === 'add' ? 'Add' : 'Remove'}
          destructive={groupBulk === 'remove'}
          summary={group => {
            const n = groupBulkRows(groupBulk, group).length
            return groupBulk === 'add' ? `${n} to add` : `${n} to remove`
          }}
          blockedReason={group => (groupBulkRows(groupBulk, group).length > 0 ? ''
            : groupBulk === 'add' ? 'All are members' : 'None is a member')}
          onSubmit={group => runGroupBulk(groupBulk, group)}
          onClose={() => setGroupBulk(null)}
          testId={`bulk-${groupBulk}-group-dialog`}
        />
      )}

      {/* A failed read with no rows: its banner is above, nothing below — unless the page is past the last. */}
      <ListStates
        loading={loading}
        skeleton={{ rows: 5 }}
        failed={loadError !== null && users.length === 0 && !(total !== null && total > 0)}
        empty={users.length === 0}
        /* A page past the last (rows deleted, a stale link) is not an empty roster: the pager snaps it back. */
        noMatch={total !== null && total > 0
          ? <ListPager page={list.page} per={list.per} total={total} onPage={list.setPage} onPer={list.setPer} testId="users-pager" />
          : filtered && <NoMatchesState onClear={clearFilters} testId="users-clear-filters" />}
        emptyTitle="No users yet"
        emptyScoped
        feed={{
          ariaLabel: 'Users',
          testId: 'users-list',
          sections: [{ key: 'users', rows: userRows }],
          columns,
          sort: { key: list.sort, dir: list.dir },
          onSort: list.toggleSort,
        }}
      >
        <ListPager page={list.page} per={list.per} total={total} onPage={list.setPage} onPer={list.setPer} testId="users-pager" />
      </ListStates>

      {/* The app's modal confirm (see `confirmAndDelete`). */}
      {confirmDialog}
    </Box>
  )
}
