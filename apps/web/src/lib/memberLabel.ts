import type { ProjectMember } from '../types'
import type { LookupOption } from '../ui/AsyncLookup'

/* How a person is named — the console's half of the ONE derivation ("First Last", else the email);
 * `apps/api/app/identities.py::person_label` is the API's half, and every `name` the API fills on a
 * principal (a task creator, a mention candidate, a ticket's requester, …) is that same rule. There is
 * no stored, editable label any more: a person IS their name pair, else their address.
 * Two picker shapes, the one home for both, so a project's role pickers (`pages/ProjectEditPage.tsx`)
 * and a task's Assigned to lookup (`components/tasks/TaskEditForm.tsx`) agree. Takes `ProjectMember`;
 * `auth.tsx`'s `Admin`, `ManagedUser` and `UserDetail` pass structurally.
 * `first_name`/`last_name` are the structured pair (either may be null); without them the EMAIL is shown,
 * never a name invented from it (`ProjectMember`'s header). */

/** The PAIR alone — "First Last", or `''` with no name. For a surface that prints the address in its own
 *  slot (the People roster's Name column) and must not repeat it. */
export function fullName(m: Pick<ProjectMember, 'first_name' | 'last_name'>): string {
  return [m.first_name, m.last_name].filter(Boolean).join(' ').trim()
}

/** Has this person no name at all? Then a surface that would print a name says so ({@link NO_NAME}) or asks for
 *  one ({@link ADD_NAME}, linking to `/profile`) instead of passing the email off as a name. */
export function isNameless(m: Pick<ProjectMember, 'first_name' | 'last_name'>): boolean {
  return !m.first_name?.trim() && !m.last_name?.trim()
}

/** A nameless person's name slot. */
export const NO_NAME = 'No name set'

/** The prompt that leads a nameless viewer to Profile. */
export const ADD_NAME = 'Add your name'

/** The SHORT form — "First Last", or the email with no name. For a closed control with a second, dimmer
 *  slot for the address (`ui/AsyncLookup`'s `hint`). */
export function memberName(m: ProjectMember): string {
  return fullName(m) || m.email
}

/** The ONE-LINE form — "First Last — email". The email is not redundant: names repeat, and the address is
 *  the identifier used across the console (and the only one for a nameless account). For a native
 *  `<option>` (no second slot) and for client-side matching on either half.
 *  NOT TRUNCATED here: a native `<option>` cannot ellipsise, and cutting in JS would also clip the OPEN
 *  list, where the choice is made. */
export function memberLabel(m: ProjectMember): string {
  const name = memberName(m)
  return name === m.email ? m.email : `${name} — ${m.email}`
}

/** A member as a lookup row (the Assigned to and Push as pickers): NAME as label, ADDRESS as the dim hint (two
 *  columns, not {@link memberLabel}'s one-line "Name — email", which a 12rem field cannot show). The hint is
 *  withheld when the label already IS the email, or there is none (an org roster row carries a name only).
 *  `key` is the uuid the wire takes. */
export function memberOption(m: ProjectMember): LookupOption {
  const label = memberName(m)
  return { key: m.id, label, hint: label === m.email || m.email === '' ? undefined : m.email }
}
