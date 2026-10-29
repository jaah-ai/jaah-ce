import { useState } from 'react'
import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Link from '@mui/material/Link'
import ListItemText from '@mui/material/ListItemText'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import { useSubmit } from '../../hooks/useSubmit'
import FormDialog from '../../ui/FormDialog'
import HelpLabel from '../../ui/HelpLabel'
import { DEFAULT_ROLE_SLUG, addBindings, rolesForScope } from './api'
import type { AccessRole, AccessScope, PickerEntry } from './api'
import PrincipalMultiSelect from './PrincipalMultiSelect'
import ScopePicker, { type ScopeOption } from './ScopePicker'

// Who + Role + Expires, ONE `POST /api/access/bindings` for every principal (all or nothing). DM1: the
// role is pre-set, so type a name + Enter submits once the picker reports an unambiguous selection.
// Expires is absent at org scope: the server refuses an org-scope expiry.
// Two modes, one write: a scope's Access tab fixes the scope and picks WHO; a principal's page (a group's
// Bindings tab) fixes the principal and picks WHERE, the roles narrowing to what that scope carries.

type ScopeMode = { scope: AccessScope; roles: AccessRole[] }
type PrincipalMode = {
  principal: PickerEntry
  /** Every role; narrowed here to the picked scope's type. */
  roles: AccessRole[]
  scopeOptions: ScopeOption[]
  scopeLoading: boolean
}

/** The picked day's END in the viewer's zone, as ISO. */
const endOfDay = (date: string) => new Date(`${date}T23:59:59.999`).toISOString()

const today = () => {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** Developer where the scope carries it, else the WEAKEST role on offer (the list is highest rank first):
 *  a default must never be the most powerful choice, e.g. Org Owner at org scope. */
const defaultRole = (roles: AccessRole[]) =>
  roles.find(r => r.slug === DEFAULT_ROLE_SLUG)?.slug ?? roles[roles.length - 1]?.slug ?? ''

export function AddAccessDialog(props: (ScopeMode | PrincipalMode) & {
  onClose: () => void
  onAdded: () => Promise<void>
  /** An operator's one-request act-as on another org's scope (`lib/actAs.ts::actAsOnce`). */
  headers?: HeadersInit
}): JSX.Element {
  const { onClose, onAdded, headers } = props
  const fixed = 'principal' in props ? props.principal : null
  const [picked, setPicked] = useState<ScopeOption | null>(null)
  const scope: AccessScope | null = 'scope' in props ? props.scope : picked && { type: picked.type, id: picked.id }
  const roles = 'scope' in props ? props.roles : scope ? rolesForScope(props.roles, scope.type) : []
  const [principals, setPrincipals] = useState<PickerEntry[]>(() => (fixed ? [fixed] : []))
  const [role, setRole] = useState(() => defaultRole(roles))
  const [expires, setExpires] = useState('')
  const { busy, formError: error, run } = useSubmit()
  const showExpiry = scope !== null && scope.type !== 'org'

  const pickScope = (next: ScopeOption | null) => {
    setPicked(next)
    // A role the new scope cannot carry must not ride the submit.
    if ('scopeOptions' in props) {
      const valid = next ? rolesForScope(props.roles, next.type) : []
      setRole(current => (valid.some(r => r.slug === current) ? current : defaultRole(valid)))
    }
  }

  const submit = async (who: PickerEntry[]) => {
    if (!who.length || !role || !scope) return
    await run('Add failed', async () => {
      const body = {
        principals: who.map(({ kind, id }) => ({ kind, id })),
        role,
        scope,
        expires_at: showExpiry && expires ? endOfDay(expires) : null,
      }
      await (headers ? addBindings(body, headers) : addBindings(body))
      await onAdded()
      onClose()
    })
  }

  return (
    <FormDialog
      title="Add access"
      submitLabel="Add"
      busyLabel="Add"
      busy={busy}
      error={error}
      submitDisabled={!principals.length || !role || !scope}
      onSubmit={() => void submit(principals)}
      onClose={onClose}
    >
      {'scope' in props ? (
        <>
          <PrincipalMultiSelect
            key={`${props.scope.type}:${props.scope.id}`}
            scope={props.scope}
            value={principals}
            onChange={setPrincipals}
            onEnterComplete={who => void submit(who)}
            autoFocus
            help="Members or groups; pick several."
          />
          {/* The picker finds only the org's members; someone new is invited first (a new org has none). */}
          {props.scope.type === 'org' && (
            <Link component={RouterLink} to={`/users/new?org=${encodeURIComponent(props.scope.id)}`} data-testid="access-invite">
              Invite by email
            </Link>
          )}
        </>
      ) : (
        <ScopePicker
          idPrefix="add-access"
          options={props.scopeOptions}
          loading={props.scopeLoading}
          value={picked}
          onChange={pickScope}
        />
      )}
      <TextField
        select
        label={<HelpLabel label="Role" help="What they may do at this scope." />}
        value={role}
        disabled={!roles.length}
        onChange={e => setRole(e.target.value)}
        slotProps={{
          select: { renderValue: slug => roles.find(r => r.slug === slug)?.name ?? '' },
        }}
      >
        {roles.map(r => (
          <MenuItem key={r.slug} value={r.slug}>
            <ListItemText primary={r.name} secondary={r.description} />
          </MenuItem>
        ))}
      </TextField>
      {showExpiry && (
        <TextField
          type="date"
          label={<HelpLabel label="Expires" help="Access ends after this day; blank never expires." />}
          value={expires}
          onChange={e => setExpires(e.target.value)}
          helperText={expires ? undefined : 'Never'}
          slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: today() } }}
        />
      )}
    </FormDialog>
  )
}

export default AddAccessDialog
