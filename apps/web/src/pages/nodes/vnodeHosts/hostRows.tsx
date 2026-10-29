import { compactCount } from '../../../lib/humanize'
import { NO_VALUE, absoluteTime } from '../../../lib/relativeTime'
import type { FeedNote, FeedSlot } from '../../../ui/feed/types'
import type { NodeHosting, VirtualNode, VnodeHost } from '../../../types'

/* The pieces the node tables need beyond an ordinary node row: the Hosting column every node row shares
 * (`pages/Nodes.tsx`), and a co-located node's poll cadence and usage against its caps (the host's page). What a host row says about its
 * machine is `../hosts/hostFacts.tsx`. */

/* WHERE A NODE RUNS, as a roster column — AWS, Your machine (the Add node way's own words), or the name of the host
   it sits on. 88px: `Your machine` is ~66px at the cluster's 11px, and a host name ellipsises with the whole on `title`.
   `wide` by window (gone ≤1023px, phone included): below that the row cannot pay 96px (slot + gap) without
   cutting the NAME under its floor (`ui/listStyles.ts`'s `SIDEBAR_SQUEEZE_SX` prices the middle band; the
   phone row keeps one slot, `e2e/nodes-mobile.spec.ts`), and by the roster's own width (`NODES_NO_HOSTING`).
   Blank when the server cannot tell. */
export const HOSTING_SLOT = 88

export function hostingSlot(hosting: NodeHosting | null | undefined, hostedOn?: string | null): FeedSlot {
  const text = hosting === 'aws' ? 'AWS'
    : hosting === 'self_hosted' ? 'Your machine'
    : hosting === 'co_located' ? (hostedOn ?? '')
    : ''
  return {
    key: 'hosting',
    width: HOSTING_SLOT,
    gate: 'nodesHosting',
    align: 'left',
    testId: 'col-node-hosting',
    title: text || undefined,
    node: text || null,
  }
}

/* Transitional rows poll (the host emits no console event for them); otherwise a host's list refetches only
   when its vnode set changes (`vnodeSignature`), never per `node` event. */
const TRANSITIONAL_POLL_MS = 5000

export const vnodePollInterval = (rows: readonly VirtualNode[] | undefined): number | false =>
  (rows ?? []).some(v => v.state === 'provisioning' || v.state === 'deleting') ? TRANSITIONAL_POLL_MS : false

/** A signature of the host's vnode set, folded into its query key so a change refetches the list. */
export const vnodeSignature = (host: VnodeHost): string =>
  host.vnodes.map(v => `${v.virtual_node_id}:${v.node_id ?? ''}`).join(',')

/* ── DISK AND INODE CAPS ──────────────────────────────────────────────────────────────────
   A co-located node's usage against its effective caps, a note on its row: the roster's trailing cluster is
   priced to the pixel (`ui/listStyles.ts`), so it takes no column. The over-cap rule is the server's (`over_cap`); the console never re-derives it. */

const KB_PER_GB = 1024 * 1024

/** GB with one decimal, the trailing `.0` dropped ("12.3", "20"). */
export const formatGb = (gb: number): string => (Math.round(gb * 10) / 10).toString()

export const OVER_CAP_TITLE = 'Over its disk or inode cap: raise the cap or free space'

/** A co-located node's usage against its caps, a note on its row ("Disk 12.3 / 20 GB · Inodes 400K / 1.3M ·
 *  Measured …"); none from a payload without caps, or while it is being torn down. Over a cap is a warning (the disc
 *  says so), never an error: an over-cap node still gets work. */
export function vnodeUsageNote(v: VirtualNode): FeedNote | null {
  if (v.effective_disk_cap_gb == null || v.effective_inode_cap == null || v.state === 'deleting') return null
  const disk = v.disk_used_kb == null ? NO_VALUE : formatGb(v.disk_used_kb / KB_PER_GB)
  const inodes = v.inodes_used == null ? NO_VALUE : compactCount(v.inodes_used)
  const custom = (override: number | null | undefined) => (override == null ? '' : ' (custom)')
  const measured = v.usage_measured_at ? `Measured ${absoluteTime(v.usage_measured_at)}` : 'Not measured yet'
  return {
    key: 'usage',
    tone: v.over_cap ? 'warning' : 'muted',
    text: [
      `Disk ${disk} / ${formatGb(v.effective_disk_cap_gb)} GB${custom(v.disk_cap_gb)}`,
      `Inodes ${inodes} / ${compactCount(v.effective_inode_cap)}${custom(v.inode_cap)}`,
      ...(v.over_cap ? [OVER_CAP_TITLE] : []),
      measured,
    ].join(' · '),
  }
}
