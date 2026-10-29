import type { Admin } from '../../auth'
import type { CredentialOwnership } from './api'
import { mintUuid } from '../../lib/mintUuid'

/* The credential-ownership and id-minting helpers both credential forms share: the routed
 * `CredentialEditPage` and the inline `settings/cloudConnections/AwsCredentialFields`. One home, so the
 * two forms cannot drift on who may own a row, how the choice is worded, or how a new row's id is minted. */

/* Who may file a credential under the org or the control plane (`authz.creates_org_rows`): an operator, or
 * an Org Owner/Admin on either plane. Presentation only — the server decides again on the write. */
export function createsOrgRows(admin: Admin | null | undefined): boolean {
  if (!admin) return false
  return admin.is_operator === true || admin.org_role === 'owner' || admin.org_role === 'admin'
}

/** Who owns a new credential (`routes/credentials/credential_wire.py::CredentialOut.ownership`). The help is the hover. */
export const OWNERSHIP_OPTIONS: { value: CredentialOwnership; label: string; help: string }[] = [
  { value: 'personal', label: 'Personal', help: 'Yours: tasks you start can use it, and projects you attach it to.' },
  {
    value: 'org',
    label: 'Organization',
    help: 'Shared: Owners and Admins, their tasks, and tasks on projects it is attached to.',
  },
  {
    value: 'system',
    label: 'System',
    help: 'For the control plane only (cloud nodes, Git apps, integrations). Owners, Admins and operators see it; tasks never do.',
  },
]

export function ownershipOf(value: string): CredentialOwnership {
  return value === 'org' || value === 'system' ? value : 'personal'
}

/** The form-level error when no secret can be sealed here: the seal key did not load, or the page is
 *  not a secure context (no `crypto.subtle`). Save and Test are withheld; reading stays possible. */
export const SEAL_UNAVAILABLE = 'Secrets cannot be encrypted in this browser session'

/** A v4 UUID for a row about to be created (`lib/mintUuid.ts`). `''` when the browser cannot make one —
 *  no `crypto` at all, so no `crypto.subtle` either — and the form renders read-only under
 *  `SEAL_UNAVAILABLE` rather than crashing in render; nothing is ever POSTed under an empty id. */
export function mintCredentialId(): string {
  return mintUuid() ?? ''
}
