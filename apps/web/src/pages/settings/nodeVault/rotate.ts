import { ApiError } from '../../../api'
import { keyHashOf, wrapKeyChain, type NodeVaultKey } from '../../../lib/nodeVaultKeys'
import { getSealKey, startRotation, type NodeVaultNode } from './api'

/* Rotation's Distribute submit. The NEW key is wrapped in the browser
 * under the org's CURRENT NodeVault public key — a chain blob only a node holding the current key can open
 * (AC-6.2) — and POSTed with the new PUBLIC key. The CP fans it out: a node holding the current key opens the
 * chain, any other node gets a peer's wrap. Nothing else leaves the page. */

export const KEY_CHANGED = 'Your current key changed while this page was open. Reload the page and start again.'

/** The `operation_id` of the running operation a 409 names (AC-6.6), when the refusal is that one. */
export function runningOperationId(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const id = (err.body as { operation_id?: unknown } | null)?.operation_id
  return typeof id === 'string' ? id : null
}

/** Nodes online right now that hold `keyHash` — the rekey and the chain both need one. */
export function onlineHolders(nodes: NodeVaultNode[], keyHash: string | null | undefined): NodeVaultNode[] {
  if (!keyHash) return []
  return nodes.filter(n => n.online && n.grants.some(g => g.key_hash === keyHash && g.state === 'delivered'))
}

/** POST /api/nodevault/rotation. `currentHash` is the current key the page SHOWS: the public key the chain is
 *  wrapped under must hash to it, so the admin rotates away from the key they were looking at. */
export async function submitRotation(opts: {
  key: NodeVaultKey
  orgId: string
  source: 'generate' | 'upload'
  currentHash: string
}): Promise<string> {
  const { public_key: currentPublicKey } = await getSealKey()
  if (await keyHashOf(currentPublicKey) !== opts.currentHash) throw new Error(KEY_CHANGED)
  const chainBlob = await wrapKeyChain(opts.key, { orgId: opts.orgId, currentPublicKey })
  const out = await startRotation({
    public_key: opts.key.publicKey, source: opts.source, backup_confirmed: true, chain_blob: chainBlob,
  })
  return out.operation_id
}
