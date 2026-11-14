import { useState } from 'react'
import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Link from '@mui/material/Link'
import { useSubmit } from '../../hooks/useSubmit'
import type { LookupOption } from '../../ui/AsyncLookup'
import AsyncMultiLookup from '../../ui/AsyncMultiLookup'
import FormDialog from '../../ui/FormDialog'
import FormField from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import SelectField from '../../ui/SelectField'
import { addBindings, defaultRole, principalOf, rolesForScope, searchPrincipals, toLookupOption } from './api'
import type { AccessRole, AccessScope, PickerEntry } from './api'
import ScopePicker, { type ScopeOption } from './ScopePicker'

// Who + Role + Expires, ONE `POST /api/access/bindings` for every principal (all or nothing). DM1: the
// role is pre-set, so type a name + Enter submits once the picker reports an unambiguous selection.
// Expires is absent at org scope: the server refuses an org-scope expiry.
// Two modes, one write: a scope's Access tab fixes the scope and picks WHO; a principal's page (a group's
// Bindings tab) fixes the principal and picks WHERE, the roles narrowing to what that scope carries.

/** `preset`: who the dialog is for, fixed (a member row's "Set project role": the roles are narrowed to them);
 *  `title` names that act. */
type ScopeMode = {
  scope: AccessScope
  roles: AccessRole[]
  preset?: LookupOption[]
  title?: string
  /** Who only: the scope's first role, no Role or Expires (an account, where every role means "may use it"). */
  whoOnly?: boolean
}
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
  const [principals, setPrincipals] = useState<LookupOption[]>(
    () => (fixed ? [toLookupOption(fixed)] : 'preset' in props && props.preset ? props.preset : []),
  )
  const [role, setRole] = useState(() => defaultRole(roles))
  const [expires, setExpires] = useState('')
  const { busy, formError: error, run } = useSubmit()
  const whoOnly = 'scope' in props && props.whoOnly === true
  const showExpiry = scope !== null && scope.type !== 'org' && !whoOnly

  const pickScope = (next: ScopeOption | null) => {
    setPicked(next)
    // A role the new scope cannot carry must not ride the submit.
    if ('scopeOptions' in props) {
      const valid = next ? rolesForScope(props.roles, next.type) : []
      setRole(current => (valid.some(r => r.slug === current) ? current : defaultRole(valid)))
    }
  }

  const submit = async (who: LookupOption[]) => {
    if (!who.length || !role || !scope) return
    await run('Grant failed', async () => {
      const body = {
        principals: who.map(principalOf),
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
      testId="add-access-dialog"
      title={('title' in props && props.title) || (whoOnly ? 'Add' : 'Grant access')}
      submitLabel={whoOnly ? 'Add' : 'Grant'}
      busyLabel={whoOnly ? 'Add' : 'Grant'}
      busy={busy}
      error={error}
      submitDisabled={!principals.length || !role || !scope}
      onSubmit={() => void submit(principals)}
      onClose={onClose}
    >
      {'scope' in props && props.preset?.length ? (
        <FormTextField label="Who" value={props.preset.map(p => p.label).join(', ')} onChange={() => {}} readOnly />
      ) : 'scope' in props ? (
        <>
          {/* Users and groups of the scope's org (`GET /api/access/principals`); the route takes no signal — the
              lookup itself discards a superseded answer. */}
          <AsyncMultiLookup
            key={`${props.scope.type}:${props.scope.id}`}
            id="add-access-who"
            testId="principal-select"
            fetchOnFocus
            label="Who"
            help="Members or groups; pick several."
            value={principals}
            onChange={setPrincipals}
            fetchOptions={q => searchPrincipals(props.scope, q).then(rows => rows.map(toLookupOption))}
            onEnterComplete={who => void submit(who)}
            stacked
            autoFocus
            /* An open list covers Grant: a pick closes it; typing or a click reopens it for the next. */
            closeOnSelect
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
      {/* Each role's description, once its option's second line, is a line of the label's pop-up. */}
      {!whoOnly && (
        <FormField label="Role" help="What they may do at this scope."
          choices={roles.flatMap(r => (r.description ? [{ label: r.name, help: r.description }] : []))}>
          <SelectField
            value={role}
            disabled={!roles.length}
            onChange={setRole}
            options={roles.map(r => ({ value: r.slug, label: r.name }))}
          />
        </FormField>
      )}
      {showExpiry && (
        <FormTextField
          type="date"
          label="Expires"
          help="Access ends after this day; blank never expires."
          value={expires}
          onChange={setExpires}
          hint={expires ? undefined : 'Never'}
          slotProps={{ htmlInput: { min: today() } }}
        />
      )}
    </FormDialog>
  )
}

export default AddAccessDialog
