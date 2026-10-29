import type { NodeRecord } from '../types'
import { bootEstimateS } from './bootEstimate'

/** A node's presence as a SHAPE (filled / hollow / question), so it survives greyscale: colour may reinforce a
 *  state, never carry it. The roster (`pages/Nodes.tsx`) and the node's page (`pages/NodeDetailPage.tsx`) share it. */
export const NODE_STATUS_GLYPH: Record<NodeRecord['status'], string> = { online: '●', offline: '○', unknown: '?' }

/** A node's presence as the word every surface shows: the roster's Status filter, the node's page, the pickers. */
export const NODE_STATUS_LABEL: Record<NodeRecord['status'], string> = { online: 'Online', offline: 'Offline', unknown: 'Unknown' }

/** The `?` disc's hover words: `unknown` alone does not say the node has sent no report. */
export const NODE_UNKNOWN_TITLE = 'No report yet'

/** How long a deploy may stay silent before its spinner stops and the node reads as stalled — at the least; a machine
 *  whose usual boot is long (Windows, bare metal: `bootEstimate.ts`) gets half as long again as that boot. */
export const NODE_DEPLOY_STALL_MS = 15 * 60_000
const STALL_BOOT_FACTOR = 1.5

/** The stall bound for this node's machine: its usual boot, half again, never under `NODE_DEPLOY_STALL_MS`. */
export function nodeDeployStallMs(node: Pick<NodeRecord, 'os' | 'cloud'>): number {
  return Math.max(NODE_DEPLOY_STALL_MS, bootEstimateS(node.os, node.cloud?.instance_type) * 1000 * STALL_BOOT_FACTOR)
}

/** A deploying node's words: the disc's label and the header pill's, its hover sentence, and its stalled pair. */
export const NODE_DEPLOYING_LABEL = 'Deploying'
export const NODE_DEPLOYING_TITLE = 'Deploying — waiting for the node to connect'
export const NODE_DEPLOY_STALLED_LABEL = 'Stalled'
/** A stalled deploy's disc: a shape of its own, not the never-reported `?`. */
export const NODE_DEPLOY_STALLED_GLYPH = '!'
/** A stalled deploy's stock hover sentence, at its node's bound. */
export function nodeDeployStalledTitle(stallMs: number): string {
  return `Not connected after ${Math.round(stallMs / 60_000)} min`
}

/** `reason`: the machine's own boot report (`NodeCloud.boot_report`) — a cause when `stalled`, else progress; null
 *  until the control plane has read one. */
export type NodeDeploy = { since: string; stalled: boolean; reason: string | null; stallMs: number }

/** A node that never connected but is on its way: a cloud machine Jaah launched and has not stopped,
 *  from its row's creation (`first_seen` is then only that) — the launch counts before its instance exists
 *  (`infra.launched_by`, no `cloud` block yet) — or a machine whose install command fetched its bootstrap
 *  (`install_started_at`; a node nobody installed yet is not deploying). Stalled at the clock's budget, or at once
 *  when the boot report names a cause. Null otherwise: every surface that shows the spinner asks this
 *  one rule. */
export function nodeDeploy(
  node: Pick<NodeRecord, 'status' | 'last_seen' | 'first_seen' | 'cloud' | 'install_started_at' | 'infra' | 'os'>,
  now: number = Date.now(),
): NodeDeploy | null {
  if (node.last_seen || node.status === 'online') return null
  const cloudStopped = node.cloud?.state === 'stopped' || node.cloud?.state === 'stopping'
  const launched = Boolean(node.cloud) || node.infra?.launched_by === 'control-plane'
  const since = launched ? (cloudStopped ? null : node.first_seen) : node.install_started_at ?? null
  if (!since) return null
  const report = node.cloud?.boot_report ?? null
  const stallMs = nodeDeployStallMs(node)
  return {
    since,
    stalled: Boolean(report?.stalled) || now - Date.parse(since) >= stallMs,
    reason: report?.reason || null,
    stallMs,
  }
}

/** A deploy's hover words: the boot report's reason when there is one, else the stock sentence. */
export function nodeDeployTitle(deploy: NodeDeploy): string {
  return deploy.reason ?? (deploy.stalled ? nodeDeployStalledTitle(deploy.stallMs) : NODE_DEPLOYING_TITLE)
}
