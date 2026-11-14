import { RETURN_PARAM } from '../../lib/nestedCreate'
import type { OrderCommand } from './api'

/* THE ADD NODE PAGE's ADDRESS (`components/addNode/AddNodePage.tsx`, `/nodes/new`, beside the Nodes list). Every way
   in builds it here: `?host=` starts on a host's Configure with that host picked, `?org=` on that org (honoured for
   the operator only), `?way=` with a way already picked, `?return=` where Cancel and Close go back to (the opener's
   page; without it, the Nodes list). `?step=` is the page's own mirror of the step on screen, and `?order=` an
   order's live progress. The retired `/nodes?add=1[&org=]` still lands here (`pages/Nodes.tsx`). */
export const ADD_NODE_PATH = '/nodes/new'
export const ADD_NODE_PARAM = 'add'
export const ADD_NODE_ORG_PARAM = 'org'
export const ADD_NODE_HOST_PARAM = 'host'
export const ADD_NODE_WAY_PARAM = 'way'
export const ADD_NODE_STEP_PARAM = 'step'
export const ADD_NODE_ORDER_PARAM = 'order'
/** Kept for the links that name it: the page itself. */
export const NODES_ADD_PATH = ADD_NODE_PATH

export function addNodePath(opts: { host?: string | null; org?: string | null; way?: string | null; returnTo?: string | null; order?: string | null } = {}): string {
  const params = new URLSearchParams()
  if (opts.host) params.set(ADD_NODE_HOST_PARAM, opts.host)
  if (opts.org) params.set(ADD_NODE_ORG_PARAM, opts.org)
  if (opts.way) params.set(ADD_NODE_WAY_PARAM, opts.way)
  if (opts.order) params.set(ADD_NODE_ORDER_PARAM, opts.order)
  if (opts.returnTo) params.set(RETURN_PARAM, opts.returnTo)
  const search = params.toString()
  return search ? `${ADD_NODE_PATH}?${search}` : ADD_NODE_PATH
}

export function nodesAddPath(orgId?: string | null): string {
  return addNodePath({ org: orgId })
}

/* AN ORDER'S ONE-TIME INSTALL COMMAND, handed to the page that shows its progress. Module memory only, never the
   URL or storage: the command carries a join credential the server serves once. A reload shows the progress
   without it (the host's row offers a fresh one). */
const commands = new Map<string, OrderCommand>()

export function handOffOrderCommand(orderId: string, command: OrderCommand | null): void {
  if (command) commands.set(orderId, command)
  else commands.delete(orderId)
}

/** The handed-off command for `orderId`; the page drops it (`handOffOrderCommand(id, null)`) as it leaves. */
export function orderCommandFor(orderId: string): OrderCommand | null {
  return commands.get(orderId) ?? null
}
