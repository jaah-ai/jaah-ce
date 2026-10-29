import type { ReactNode } from 'react'
import { JAAH_CLOUD_DELETE_FACTS, liveSlots, nodeDeleteFacts } from '../lib/nodeDelete'
import type { NodeRecord } from '../types'
import type { ConfirmOptions } from './ConfirmDialog'
import NodeDeleteFacts from './NodeDeleteFacts'

/** ONE node's Delete confirm, shared by `/nodes` and `/nodes/:nodeId` (per-page copies went stale): the body is
 *  `lib/nodeDelete.ts`'s facts — a rented Jaah Cloud machine says which delete ends its rental (the server refuses
 *  this one while it is billed); any other node lists what goes. `identity` is each page's name for the row. */
export function nodeDeleteConfirm(
  node: Pick<NodeRecord, 'jaah_cloud' | 'status' | 'hosting' | 'counts'>,
  identity: ReactNode,
): ConfirmOptions {
  return {
    title: 'Delete node',
    identity,
    message: <NodeDeleteFacts facts={node.jaah_cloud ? JAAH_CLOUD_DELETE_FACTS : nodeDeleteFacts({
      online: node.status === 'online', coLocated: node.hosting === 'co_located', slots: liveSlots(node.counts),
    })} />,
    confirmLabel: 'Delete',
    destructive: true,
  }
}
