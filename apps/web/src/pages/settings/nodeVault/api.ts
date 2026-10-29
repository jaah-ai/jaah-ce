import { get, post } from '../../../api'
import { AUDIT_TAB_PATHS } from '../../../routeGroups'
import { NODEVAULT_CAPABILITY_LABEL, NODEVAULT_INCAPABLE_HINT } from '../../../lib/nodeVaultCapability'
import type { NodeVaultKeyBlob } from '../../../lib/nodeVaultKeys'

/* Settings → NodeVault's wire (`apps/api/app/routes/nodevault.py`). Every
 * route is org Owner/Admin. Nothing here carries a private key: the setup POST takes the PUBLIC key and
 * one ciphertext blob per node, built by `lib/nodeVaultKeys.ts`. */

export type GrantState = 'waiting' | 'delivered' | 'failed' | 'retire_pending'

export type NodeVaultGrant = {
  key_hash: string
  state: GrantState
  /** A closed vocabulary (`models.NODEVAULT_GRANT_ERRORS`); worded by `grantError`. */
  last_error: string | null
  /** No online node can make this delivery yet (AC-3.4). */
  waiting_for_holder: boolean
}

export type NodeVaultNode = {
  id: string
  name: string
  online: boolean
  /** Base64 SPKI DER of the node's identity key; null until the node registers one. */
  identity_key: string | null
  identity_fp: string | null
  capable: boolean
  grants: NodeVaultGrant[]
}

export type OperationKind = 'setup' | 'rotation' | 'migration' | 'retire'
/** The server-owned stepper position (`durable/nodevault.py` STEP_*). */
export type OperationStep = 'distribute' | 'promote' | 'migrate_choice' | 'migrating' | 'rekey' | 'retiring' | 'result'

export type OperationCounts = {
  migrated: number
  skipped: number
  failed: number
  rekeyed: number
  /** A rotation's rekey phase: credentials no node could move to the new key. */
  rekey_failed?: number
  total: number
  nodes_pending: number
  /** Only on `GET /api/nodevault/operations/{id}`. */
  nodes_holding?: number
  nodes_total?: number
}

export type NodeVaultOperation = {
  id: string
  kind: OperationKind
  state: 'running' | 'done' | 'failed'
  step: OperationStep
  key_hash: string | null
  prev_key_hash: string | null
  waiting_for_holder: boolean
  counts: OperationCounts
}

export type NodeVaultStatus = {
  allowed: boolean
  frozen: boolean
  setup: boolean
  current_key: { hash: string; created_at: string; credential_count: number } | null
  previous_keys: { hash: string; created_at: string; state: 'previous' | 'retiring'; credential_count: number }[]
  nodes: NodeVaultNode[]
  operation: NodeVaultOperation | null
  /** Start over is offered: the caller is the org Owner, nothing runs, and no node holds the current key. */
  can_start_over: boolean
}

export type OperationItem = {
  credential_id: string
  name: string
  phase: 'migrate' | 'rekey'
  state: 'pending' | 'sealed' | 'done' | 'skipped' | 'failed'
  reason: string | null
  /** The reason as a sentence, from the server. */
  message: string | null
}

export type OperationDetail = NodeVaultOperation & { items: OperationItem[] }

export type MigrationCandidates = {
  eligible: { id: string; name: string; type: string; state: 'kms' | 'nodevault'; key_hash: string | null }[]
  ineligible: { id: string; name: string; type: string; reason: string }[]
}

export type SetupBody = {
  public_key: string
  source: 'generate' | 'upload'
  backup_confirmed: true
  blobs: Record<string, NodeVaultKeyBlob>
}

export type RotationBody = {
  public_key: string
  source: 'generate' | 'upload'
  backup_confirmed: true
  /** The new key wrapped under the org's CURRENT NodeVault public key (`wrapKeyChain`): ciphertext only. */
  chain_blob: NodeVaultKeyBlob
}

export type MigrationChoice = { credential_ids: string[] } | { all: true } | { skip: true }

export const getNodeVault = () => get<NodeVaultStatus>('/api/nodevault')
export const getMigrationCandidates = () => get<MigrationCandidates>('/api/nodevault/credentials')
export const getOperation = (id: string) => get<OperationDetail>(`/api/nodevault/operations/${id}`)
export const startSetup = (body: SetupBody) => post<{ operation_id: string }>('/api/nodevault/setup', body)
export const startRotation = (body: RotationBody) => post<{ operation_id: string }>('/api/nodevault/rotation', body)
export const retireKey = (keyHash: string) => post<{ operation_id: string }>(`/api/nodevault/keys/${encodeURIComponent(keyHash)}/retire`)
/** The org's current NodeVault PUBLIC key — what a rotation's chain blob is wrapped under. */
export const getSealKey = () => get<{ key_id: string; algorithm: string; public_key: string }>('/api/nodevault/seal-key')
export const chooseMigration = (choice: MigrationChoice) =>
  post<{ operation_id: string }>('/api/nodevault/migrations', choice)
export const retryItem = (operationId: string, credentialId: string) =>
  post<{ state: string }>(`/api/nodevault/operations/${operationId}/items/${credentialId}/retry`)
export const startOver = () => post<{ state: string }>('/api/nodevault/start-over')
export const abandonOperation = (id: string) => post<{ state: string }>(`/api/nodevault/operations/${id}/abandon`)
export const retryGrant = (nodeId: string, keyHash: string) =>
  post<{ state: string }>(`/api/nodevault/grants/${nodeId}/${keyHash}/retry`)
export const resetIdentity = (nodeId: string) =>
  post<{ node_id: string; grants_removed: number }>(`/api/nodevault/nodes/${nodeId}/reset-identity`)

/** Audit › Activity searched for NodeVault: the free text matches the `nodevault.*` kinds and the
 *  `/api/nodevault` paths (`pages/audit/UsersTab.tsx` reads `?q=` from the URL). */
export const NODEVAULT_AUDIT_URL = `${AUDIT_TAB_PATHS.users}?q=nodevault`

/** The nodes a Setup key is wrapped for: exactly the set the CP asks blobs for (registered identity AND
 *  capable — `routes/nodevault.py::start_nodevault_setup`). */
export function keyRecipients(nodes: NodeVaultNode[]): NodeVaultNode[] {
  return nodes.filter(n => n.capable && n.identity_key !== null)
}

/** A node's grant for one key, if it has one. */
export function grantFor(node: NodeVaultNode, keyHash: string | null | undefined): NodeVaultGrant | undefined {
  return keyHash ? node.grants.find(g => g.key_hash === keyHash) : undefined
}

/* `last_error` words a node's IDENTITY explains: the blob was wrapped to an identity key the node no longer
 * holds (its keystore was wiped and it minted a new one, which the CP refuses — `agent_nodevault.py`). Only
 * an admin's reset re-enrols it (§3.5). */
const IDENTITY_ERRORS = new Set(['wrong_recipient', 'missing_wrapping_key'])

export function identityChanged(node: NodeVaultNode): boolean {
  return node.grants.some(g => g.state === 'failed' && g.last_error !== null && IDENTITY_ERRORS.has(g.last_error))
}

const GRANT_ERRORS: Record<string, string> = {
  offline: 'Node offline',
  timeout: 'Node didn’t answer in time',
  crypto_unavailable: 'Node can’t run NodeVault encryption. Update Jaah on it.',
  rejected: 'Node refused the key',
  hash_mismatch: 'Node received the wrong key',
  wrong_recipient: 'Identity key changed — reset to re-enroll',
  missing_wrapping_key: 'Identity key changed — reset to re-enroll',
  open_failed: 'Node couldn’t open the key',
  malformed: 'Node received a damaged key',
}

/** A grant error word as words; never the raw token. */
export function grantError(word: string | null): string {
  return word ? GRANT_ERRORS[word] ?? 'Delivery failed' : 'Delivery failed'
}

export type GrantView = { label: string; tone: 'success' | 'info' | 'warning' | 'error' | 'neutral'; detail?: string; retry: boolean }

/** What one node shows for one key (AC-4.5): Delivered / Waiting (offline) / Failed — retrying / Failed. */
export function grantView(node: NodeVaultNode, grant: NodeVaultGrant | undefined): GrantView {
  if (!grant) {
    if (node.identity_key === null) return { label: 'Not enrolled', tone: 'neutral', detail: 'Node hasn’t registered an identity key yet', retry: false }
    if (!node.capable) return { label: NODEVAULT_CAPABILITY_LABEL.not_capable, tone: 'neutral', detail: NODEVAULT_INCAPABLE_HINT, retry: false }
    return { label: 'No key', tone: 'neutral', retry: false }
  }
  switch (grant.state) {
    case 'delivered':
      return { label: 'Delivered', tone: 'success', retry: false }
    case 'failed':
      return { label: 'Failed', tone: 'error', detail: grantError(grant.last_error), retry: !identityChanged(node) }
    case 'retire_pending':
      return { label: 'Removing', tone: 'info', retry: false }
    case 'waiting':
    default:
      if (!node.online) return { label: 'Waiting (offline)', tone: 'neutral', retry: false }
      if (grant.waiting_for_holder) return { label: 'Waiting for a node that holds the key', tone: 'warning', retry: false }
      if (grant.last_error !== null && grant.last_error !== 'offline') {
        return { label: 'Failed — retrying', tone: 'warning', detail: grantError(grant.last_error), retry: false }
      }
      return { label: 'Delivering', tone: 'info', retry: false }
  }
}
