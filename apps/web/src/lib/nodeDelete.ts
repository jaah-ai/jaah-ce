/* The words for deleting a node, single-homed for both surfaces (`/nodes` ⋯ item and `/nodes/:nodeId` ✕).
 * `DELETE /api/nodes/{node_id}` drops every slot row the node declared, ends their holders, asks the node
 * to erase those directories best effort, and terminates the cloud instance when the CP launched it — the
 * confirm must say all of that.
 * The 200 becomes a short toast (`summariseNodeDelete`): `warning` only when an uninstall could not be sent,
 * `info` otherwise, never `error` — the delete happened in full and there is nothing to retry. */
import { ApiError } from '../api'
import type { NodeCounts, NodeDeleteResult } from '../types'
import { moneyText } from './billing'

/** A node's slots less the ones already retiring (`NodeCounts.retiring_slots`): what its Overview counts and its
 *  delete takes — a retiring slot is being destroyed already. */
export function liveSlots(counts: NodeCounts): number {
  return counts.worktrees - (counts.retiring_slots ?? 0)
}

/** What deleting ONE node does, as a few scannable facts (the confirm renders them as a list). `slots` is
 *  the node's live slot count when known (`liveSlots`); `online` adds the still-heartbeating fact. A co-located node
 *  has no instance of its own: its home on the host is erased instead
 *  (`pages/nodes/hosts/hostVerbs.tsx::confirmAndDeleteVnode`). */
export function nodeDeleteFacts(
  { online, coLocated = false, slots }: { online: boolean; coLocated?: boolean; slots?: number },
): string[] {
  return [
    slots === 0
      ? 'No slots on it to delete'
      : `${slots === undefined ? 'Every slot' : slots === 1 ? '1 slot' : `${slots} slots`} on it deleted; ` +
        'folders erased, best effort',
    'Runners and console sessions in them killed, not asked',
    coLocated ? 'Its home on the host erased' : 'Its instance terminated, if Jaah launched it',
    ...(online
      ? [coLocated
          ? 'Still heartbeating — that doesn’t stop the delete'
          : 'Still heartbeating — deleted anyway; shows as unregistered while it reports']
      : []),
    'Can’t be undone',
  ]
}

/** A rented Jaah Cloud machine's Delete (`NodeOut.jaah_cloud`): the rental, not this page, ends it — the server
 *  refuses a node delete while a billed hosting names the machine (`routes/nodes/delete.py::HOSTED_MACHINE`). */
export const JAAH_CLOUD_DELETE_FACTS: readonly string[] = [
  'A Jaah Cloud rental holds this machine, so Delete here is refused while it is billed',
  'Ending the rental deletes it: the instance is terminated and unused days are credited',
  'End it from the customer’s Products tab (the tenant: Billing)',
]

/** The bulk bar's question for its picked nodes (`pages/Nodes.tsx`); the names and count ride the confirm. */
export function nodesDeleteQuestion(online: boolean): string {
  return (
    'This deregisters each node, deletes every slot row declared on it, ends whatever was holding those slots — a ' +
    'task runner or a console session is killed, not asked — and asks the node to erase those directories, best ' +
    'effort. If the control plane launched a machine, its instance is TERMINATED. ' +
    (online
      ? 'One or more are still heartbeating; that does not stop any of the above, and each stays visible as an ' +
        'unregistered node while it keeps reporting. '
      : '') +
    'None of it can be undone; a node the server protects (allocated, hosted, or still carrying nodes) is refused and listed.'
  )
}

/** The operator's 409 for a hosted machine (the api's hosted-machine refusal, `_for_operator`): the customer whose hosting it
 *  is and where its Products tab removes it. Null for anything else — a tenant's 409 is a plain sentence. */
export type HostedDelete = { message: string; customer: { id: string; name: string | null }; rental_id: string; href: string }

export function hostedDelete(err: unknown): HostedDelete | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const d = err.detail as Partial<HostedDelete> | null
  return d && typeof d === 'object' && typeof d.href === 'string' && d.customer && typeof d.message === 'string'
    ? (d as HostedDelete)
    : null
}

/** Why a host's Delete is withheld: the server refuses it with this sentence (409, vnodes' FK
 *  is RESTRICT) while the host carries a live node; a node still being removed goes with the host. Empty when
 *  the host carries none. */
export function hostDeleteBlockedReason(vnodeCount: number): string {
  return vnodeCount > 0
    ? `Delete its ${vnodeCount} node${vnodeCount === 1 ? '' : 's'} first.`
    : ''
}

/** A host's own question: a host holds no slots or runners, so `nodeDeleteFacts`' facts would be
 *  false. Asked only once the host is empty. A self-hosted machine is also told to uninstall Jaah. */
export function hostDeleteQuestion(name: string, online: boolean, selfHosted = false): string {
  return (
    `Delete host ${name}? This deregisters it and, if the control plane launched it, TERMINATES its ` +
    'instance. ' +
    (selfHosted ? 'Jaah is uninstalled from the machine. ' : '') +
    (online ? 'It is still heartbeating; that does not stop the delete. ' : '') +
    'This cannot be undone.'
  )
}

/** What deleting a host with its last node does, by where the machine comes from — the line under the node
 *  confirm's "Also delete host" checkbox. `refundOnDelete` is a Jaah-hosted hosting's own word on it. */
export function hostGoesConsequence(origin: 'cloud' | 'self_hosted' | 'rented', refundOnDelete?: boolean): string {
  if (origin === 'cloud') return 'Its cloud instance is terminated.'
  if (origin === 'self_hosted') return 'Jaah is uninstalled from the machine.'
  return refundOnDelete
    ? 'The hosting ends; unused time is refunded to your card.'
    : 'The hosting ends; unused days are credited to your next bill.'
}

/** A Jaah-hosted host's ended hosting as the notice. `refundedCents` is the delete's own answer. Hostings bill
 *  in USD alone (`app/org_plans.py::CURRENCY`), and their wire carries no currency. */
export function summariseHostingEnd(name: string, refundedCents: number | undefined): NodeDeleteNotice {
  const money = refundedCents && refundedCents > 0
    ? `Refunded ${moneyText(refundedCents, 'usd')} to your card.`
    : 'Unused days are credited to your next bill.'
  return { text: `${name}'s hosting has ended. ${money}`, severity: 'info' }
}

/** A completed delete's notice. ONE type for both surfaces and the `/nodes/:nodeId` → `/nodes` handoff
 *  (`NODE_DELETE_HANDOFF`), so "when is a delete amber" has one answer. Never `error`: nothing failed. */
export type NodeDeleteNotice = { text: string; severity: 'info' | 'warning' }

/** The react-router location `state` key for the delete handoff, shared by writer and reader. */
export const NODE_DELETE_HANDOFF = 'nodeDeleteNotice'

/** The 200 as a toast: the name and "deleted", nothing else. Slot rows, ended holders, directories
 *  the node could not erase and a terminated-or-not instance are not reported — the confirm already said all of
 *  it. Amber only for an uninstall that was not sent: Jaah stays on the machine until removed by hand, and
 *  `uninstall_detail` says how. Tolerates an absent body (`api()` answers `undefined` for a 204, `null` for an
 *  unparseable 200, both reachable across a rolling deploy): the node IS deleted, so throwing here would report
 *  `Failed to delete` about an irreversible act that succeeded. */
export function summariseNodeDelete(
  name: string, result: NodeDeleteResult | null | undefined,
): NodeDeleteNotice {
  const deleted = `${name} deleted.`
  const notSent = result && typeof result === 'object' && !result.uninstall_requested && result.uninstall_detail
  return notSent
    ? { text: `${deleted} ${result.uninstall_detail}`, severity: 'warning' }
    : { text: deleted, severity: 'info' }
}
