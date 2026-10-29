import { useMemo, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { actAsOnce } from '../../lib/actAs'
import type { OrgRef } from '../../types'
import { Banner } from '../../ui/Banner'
import { EmptyState } from '../../ui/EmptyState'
import { LoadingState } from '../../ui/LoadingState'
import AddAccessDialog from './AddAccessDialog'
import MembersTable, { toMemberRows, type AccessTableRow } from './MembersTable'
import { listMembers, listRoles, rolesForScope, type AccessScope } from './api'
import { useCanManage, useRemoveBinding } from './useBindings'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'

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
  const { remove: removeAccess, error, clearError, confirmDialog } = useRemoveBinding(members.reload, asOrg)
  const operator = useAuth().admin?.is_operator === true

  const rows = useMemo(() => toMemberRows(members.data ?? [], operator), [members.data, operator])
  const roleRank = useMemo(
    () => Object.fromEntries((roles.data ?? []).map(r => [r.slug, r.rank])),
    [roles.data],
  )

  function remove(row: AccessTableRow) {
    const who = row.subject.kind === 'group' ? 'Its members lose' : 'They lose'
    return removeAccess(row.bindingId, `Remove ${row.subject.name}'s ${row.role.name}? ${who} access granted by this binding.`)
  }

  const loadError = members.error || roles.error

  return (
    <Stack spacing={1} sx={{ minWidth: 0 }}>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {error && <Banner severity="error" onDismiss={clearError}>{error}</Banner>}
      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
        <TextField
          label="Filter"
          size="small"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          slotProps={{ htmlInput: NO_AUTOFILL_INPUT }}
          sx={{ flex: '1 1 16rem', maxWidth: '24rem' }}
        />
        {canManage && (
          /* Not before the role catalog: the dialog seeds its role from it once, at open. "Grant access", not
             "Add": it gives an EXISTING user or group a role here; a host's own "New user" creates one. */
          <Button variant="contained" data-testid="access-add" disabled={!roles.data} onClick={() => setAdding(true)}>
            Grant access
          </Button>
        )}
      </Box>
      {members.data === null ? (
        members.loading ? <LoadingState rows={5} /> : null
      ) : rows.length === 0 ? (
        <EmptyState title="No members." />
      ) : (
        <MembersTable rows={rows} filter={filter} roleRank={roleRank} onRemove={remove} />
      )}
      {adding && (
        <AddAccessDialog
          scope={scope}
          roles={rolesForScope(roles.data ?? [], scope.type)}
          onClose={() => setAdding(false)}
          onAdded={members.reload}
          headers={asOrg}
        />
      )}
      {confirmDialog}
    </Stack>
  )
}
