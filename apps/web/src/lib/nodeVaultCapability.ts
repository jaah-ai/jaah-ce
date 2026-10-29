/* A node's NodeVault capability in the fleet's own words (`NodeOut.nodevault_capable`, the daemon's crypto probe):
 * one home for the label the node page, the Nodes filter and Settings → NodeVault's grant list all show. */

export const NODEVAULT_CAPABILITY_LABEL = {
  capable: 'Holds keys',
  not_capable: 'Can’t hold keys',
} as const

/** What fixes a node that can't hold keys. */
export const NODEVAULT_INCAPABLE_HINT = 'Update Jaah on this node'
