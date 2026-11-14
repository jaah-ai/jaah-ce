/* ── A node's shape, as the console speaks it ─────────────────────────────────────────────────────────
 * How a node runs its sessions: sharing the node (`legacy`), one VM per session (`microvm`), or the whole node
 * in one VM (`node_vm`). The console's ONE home for what each shape is CALLED and its one-line hint. Which
 * shapes a picker offers is the server's (`GET /api/node-orders/shapes`, a node's `shape_options`), never
 * derived here. */

export type Shape = 'legacy' | 'microvm' | 'node_vm'

export const SHAPE_LABEL: Record<Shape, string> = {
  legacy: 'Legacy',
  microvm: 'MicroVM',
  node_vm: 'Whole-node VM',
}

export const SHAPE_HINT: Record<Shape, string> = {
  legacy: 'Sessions share the node',
  microvm: 'Each session in its own VM',
  node_vm: 'The whole node in one VM',
}
