import { ApiError } from '../../../api'
import { wrapKeyForNode, type NodeVaultKey, type NodeVaultKeyBlob } from '../../../lib/nodeVaultKeys'
import { getNodeVault, keyRecipients, startSetup, type NodeVaultNode } from './api'

/* Setup's Distribute submit, and its race with a moving node set. The key is wrapped in the
 * browser for every node the CP will ask a blob for, and POSTed with the PUBLIC key. If the node set moved
 * between the read and the POST, the CP answers 422 `{missing, extra}`: refetch, rewrap for the new set and
 * resubmit — up to `MAX_RESUBMITS` times — before the admin sees an error. Only ciphertext and the public
 * key leave the page. */

export const MAX_RESUBMITS = 3

export const NODES_KEPT_CHANGING = 'Your nodes kept changing while the key was sent. Try again.'

export async function wrapForNodes(
  key: NodeVaultKey, orgId: string, nodes: NodeVaultNode[],
): Promise<Record<string, NodeVaultKeyBlob>> {
  const blobs: Record<string, NodeVaultKeyBlob> = {}
  for (const node of keyRecipients(nodes)) {
    blobs[node.id] = await wrapKeyForNode(key, { orgId, nodeId: node.id, identityKey: node.identity_key as string })
  }
  return blobs
}

function isNodeSetRace(err: unknown): boolean {
  if (!(err instanceof ApiError) || err.status !== 422) return false
  const body = err.body as { missing?: unknown; extra?: unknown } | null
  return Array.isArray(body?.missing) && Array.isArray(body?.extra)
}

/** POST /api/nodevault/setup, resubmitting on the node-set race. Resolves with the operation id; throws the
 *  last error otherwise (the race exhausted as `NODES_KEPT_CHANGING`). */
export async function submitSetup(opts: {
  key: NodeVaultKey
  orgId: string
  source: 'generate' | 'upload'
  nodes: NodeVaultNode[]
}): Promise<string> {
  let nodes = opts.nodes
  for (let attempt = 0; ; attempt++) {
    const blobs = await wrapForNodes(opts.key, opts.orgId, nodes)
    try {
      const out = await startSetup({ public_key: opts.key.publicKey, source: opts.source, backup_confirmed: true, blobs })
      return out.operation_id
    } catch (err) {
      if (!isNodeSetRace(err)) throw err
      if (attempt >= MAX_RESUBMITS) throw new Error(NODES_KEPT_CHANGING)
      nodes = (await getNodeVault()).nodes
    }
  }
}
