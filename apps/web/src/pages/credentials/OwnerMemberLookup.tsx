import { useQuery } from '@tanstack/react-query'
import { isEnabled } from '../../lib/editionFeatures'
import { searchUsers, userOption } from '../../lib/users'
import AsyncLookup, { type LookupOption } from '../../ui/AsyncLookup'
import FormField from '../../ui/FormField'

/* WHOSE personal credential a new one is (D6): an Org Owner/Admin, or an operator acting as the org, may
 * make it for any member — the server's `authz.validated_owner_user`, which decides again on the write.
 * Fed by the console's one user search (`lib/users.ts`: the People route, server-searched on name and email
 * and capped, so an org of thousands stays usable at 360px). Empty means "me": nothing is sent and the server
 * makes it the caller's. Unreadable roster (the first page refused): the field is not drawn, and the
 * credential is the caller's as before; so in the community edition, whose one user owns everything. `stacked` keeps each email whole under its name on a phone. */

export type OwnerMember = LookupOption

export default function OwnerMemberLookup({ value, onChange, selfId, disabled }: {
  value: OwnerMember | null
  onChange: (next: OwnerMember | null) => void
  /** The signed-in user: offered as "You", the same as leaving the field empty. */
  selfId: string | undefined
  disabled?: boolean
}) {
  // The first page answers whether this viewer may read the roster at all; the picker then searches per keystroke.
  const probe = useQuery({
    queryKey: ['users', 'owner-probe'], queryFn: () => searchUsers('', undefined, { enabled: true }), retry: false, enabled: isEnabled('users_invites'),
  })
  if (!isEnabled('users_invites') || probe.isError) return null
  const option = (user: Parameters<typeof userOption>[0]): LookupOption => {
    const row = userOption(user)
    return user.id === selfId ? { ...row, label: `You (${row.label})` } : row
  }
  return (
    <FormField label="Owned by">
      <AsyncLookup
        id="credential-owner-member"
        value={value}
        onChange={onChange}
        fetchOptions={async (query, signal) => (await searchUsers(query, signal, { enabled: true })).map(option)}
        fetchOnFocus
        stacked
        placeholder="You"
        ariaLabel="Owned by"
        noOptionsText="No matching member"
        disabled={disabled || probe.isPending}
      />
    </FormField>
  )
}
