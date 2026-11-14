import type { Admin } from '../../auth'
import type { CredentialOwnership } from './api'
import { mintUuid } from '../../lib/mintUuid'

/* The credential-ownership and id-minting helpers: who may own a row, how the choice is worded, and how a new
 * row's id is minted, in one home for `CredentialEditPage` and every other form that files a credential. */

/* Who may file a credential under the org or the control plane (`authz.creates_org_rows`): an operator, or
 * an Org Owner/Admin on either plane. Presentation only — the server decides again on the write. */
export function createsOrgRows(admin: Admin | null | undefined): boolean {
  if (!admin) return false
  return admin.is_operator === true || admin.org_role === 'owner' || admin.org_role === 'admin'
}

/* Who may run a credential's Test: `POST /api/credentials/test` stays `credentials:edit` and refuses a tenant org
 * outright (it makes the CONTROL PLANE dial a supplied URL), so only an operator holding that key. Every test-only
 * control — the Test button, the list's Tested column, the "Last test failed" filter — keys on this one rule, so a
 * viewer who can never test sees none of them. Presentation only — the server decides again. */
export function testsCredentials(admin: Admin | null | undefined, canEdit: boolean): boolean {
  return canEdit && admin?.is_operator === true
}

/** Who owns a new credential (`routes/credentials/credential_wire.py::CredentialOut.ownership`). The help is the hover. */
export const OWNERSHIP_OPTIONS: { value: CredentialOwnership; label: string; help: string }[] = [
  { value: 'personal', label: 'Personal', help: 'One member’s: their tasks and the projects they attach it to.' },
  { value: 'org', label: 'Organization', help: 'Shared: Owners, Admins, their tasks and attached projects.' },
  { value: 'system', label: 'System', help: 'Control plane only (cloud nodes, Git apps); tasks never.' },
]

/** An ownership's one-word label (`OWNERSHIP_OPTIONS`), as the list's Owner column names an org or system row. */
export function ownershipLabel(value: CredentialOwnership): string {
  return OWNERSHIP_OPTIONS.find(o => o.value === value)?.label ?? value
}

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
