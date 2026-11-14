import { useMemo, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { actAsOnce } from '../../lib/actAs'
import type { OrgRef } from '../../types'
import { Banner } from '../../ui/Banner'
import { EmptyState } from '../../ui/EmptyState'
import { LoadingState } from '../../ui/LoadingState'
import AddAccessDialog from './AddAccessDialog'
import MembersTable, { effectiveRole, toMemberRows, type AccessTableRow } from './MembersTable'
import { listMembers, listRoles, rolesForScope, type AccessRole, type AccessScope } from './api'
import type { OverflowItem } from '../../ui/feed/types'
import { useCanManage, useRemoveBinding } from './useBindings'
import SearchBox from '../../ui/SearchBox'
import { grid } from '../../theme'

// A scope's Access tab, self-contained: members, filter, Grant access, Remove (`useBindings.ts` holds the
// manage probe and the Remove flow, shared with a user's Access).

/* `otherOrg`: the scope is another org's (an operator's) — Grant access and Remove go as that org for the one
   request (`lib/actAs.ts::actAsOnce`). */
export default function AccessTab({ scope, otherOrg = null }: { scope: AccessScope; otherOrg?: OrgRef | null }): JSX.Element {
  const asOrg = actAsOnce(otherOrg)
  const members = useApiRead(['access-tab-members', scope.type, scope.id], () => listMembers(scope), 'Could not load members.')
  const roles = useApiRead(['access-roles'], listRoles, 'Could not load roles.')
  const canManage = useCanManage(scope)
  const [filter, setFilter] = useState('')
  const [adding, setAdding] = useState(false)
  /* A person or group who holds their role here only through the org or a group: the project roles above it. */
  const [raising, setRaising] = useState<{ row: AccessTableRow; roles: AccessRole[] } | null>(null)
  const { remove: removeAccess, error, clearError, confirmDialog } = useRemoveBinding(members.reload, asOrg)
  const operator = useAuth().admin?.is_operator === true
  /* An account: every role means "may use it", so the tab is a plain list of who — no roles. */
  const whoOnly = scope.type === 'account'

  const rows = useMemo(() => toMemberRows(members.data ?? [], operator), [members.data, operator])
  const roleRank = useMemo(
    () => Object.fromEntries((roles.data ?? []).map(r => [r.slug, r.rank])),
    [roles.data],
  )

  /* "Set project role": a project role above the one a member holds only through the org or a group — access is
     a union, so the higher role is the effective one and the org grant stays listed as its source. A lower role
     is not offered (the server refuses it, 409). A member granted here directly already has their own row. */
  const projectRoles = useMemo(() => rolesForScope(roles.data ?? [], 'project'), [roles.data])
  function raiseAction(row: AccessTableRow): OverflowItem | undefined {
    const kind = row.subject.kind
    if (scope.type !== 'project' || !canManage || kind === 'scope' || !row.subject.id) return undefined
    if (row.source.kind === 'direct') return undefined
    // A row with no active grant holds nothing: every project role is above it.
    const live = [row, ...(row.also ?? [])].some(g => g.status === 'active')
    const held = live ? (roleRank[effectiveRole(row, roleRank).slug] ?? 0) : 0
    const higher = projectRoles.filter(r => r.rank > held)
    if (!higher.length) return undefined
    return {
      key: 'set-role',
      label: 'Set project role',
      ariaLabel: `Set project role for ${row.subject.name}`,
      testId: 'member-set-role',
      onClick: () => setRaising({ row, roles: higher }),
    }
  }

  function remove(row: AccessTableRow) {
    const who = row.subject.kind === 'group' ? 'Its members lose' : 'They lose'
    if (whoOnly) return removeAccess(row.bindingId, `Remove ${row.subject.name}? ${who} this account.`)
    return removeAccess(row.bindingId, `Remove ${row.subject.name}'s ${row.role.name}? ${who} access granted by this binding.`)
  }

  const loadError = members.error || roles.error

  return (
    <Stack spacing={1} sx={{ minWidth: 0 }}>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {error && <Banner severity="error" onDismiss={clearError}>{error}</Banner>}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
        <SearchBox
          label="Filter"
          value={filter}
          onChange={setFilter}
          delayMs={0}
          ariaLabel="Filter"
          sx={{ flex: `1 1 ${grid(42)}`, maxWidth: grid(64) }}
        />
        {canManage && (
          /* Not before the role catalog: the dialog seeds its role from it once, at open. "Grant access", not
             "Add": it gives an EXISTING user or group a role here; a host's own "New user" creates one. */
          <Button variant="contained" data-testid="access-add" disabled={!roles.data} onClick={() => setAdding(true)}>
            {whoOnly ? 'Add' : 'Grant access'}
          </Button>
        )}
      </Box>
      {members.data === null ? (
        members.loading ? <LoadingState rows={5} /> : null
      ) : rows.length === 0 ? (
        <EmptyState title="No members." />
      ) : (
        <MembersTable rows={rows} filter={filter} roleRank={roleRank} onRemove={remove} rowAction={raiseAction} whoOnly={whoOnly} />
      )}
      {adding && (
        <AddAccessDialog
          scope={scope}
          roles={rolesForScope(roles.data ?? [], scope.type)}
          whoOnly={whoOnly}
          onClose={() => setAdding(false)}
          onAdded={members.reload}
          headers={asOrg}
        />
      )}
      {raising && raising.row.subject.id && (
        <AddAccessDialog
          scope={scope}
          title="Set project role"
          roles={raising.roles}
          preset={[{ key: `${raising.row.subject.kind}:${raising.row.subject.id}`, label: raising.row.subject.name }]}
          onClose={() => setRaising(null)}
          onAdded={members.reload}
          headers={asOrg}
        />
      )}
      {confirmDialog}
    </Stack>
  )
}
