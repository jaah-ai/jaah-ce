import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormHelperText from '@mui/material/FormHelperText'
import Typography from '@mui/material/Typography'
import type { NodeLink, NodeLinkStatus } from '../pages/nodes/nodeLinkStore/api'
import type { CloudProvider } from '../types'
import { monoSx } from '../ui/listStyles'
import StatusChip, { type Tone } from '../ui/StatusChip'

/* The ONE NodeLink multi-select: which of the org's NodeLinks a node (or every node of a group) is born
 * attached to. A checkbox per NodeLink — the name (ellipsed, whole in `title`) over its mount-path template.
 * A NodeLink being deleted is no choice (`nodelinks.birth_set` refuses it), so it is never listed. Here one not yet
 * `ready` stays pickable — its status chip and "Mounts once ready." say it lands later — though Add node passes
 * only ready ones (`addableNodeLinks`).
 * Consumers: `components/addNode/AddNodeDialog.tsx` (its options narrowed by `addableNodeLinks`),
 * `pages/nodes/nodeGroups/NodeGroupDialog.tsx`. */

/** A NodeLink (or storage) status's tone — here, in Networks > NodeLink's list (`nodeLinkStore/NodeLinkList.tsx`)
 *  and its editor (`nodeLinkStore/NodeLinkEditPage.tsx`); `ready` draws no chip here. */
export const STATUS_TONE: Record<NodeLinkStatus, Tone> = {
  pending: 'neutral', provisioning: 'info', ready: 'success', failed: 'error', deleting: 'warning',
}

/** The options a picker offers: every NodeLink but those being deleted. */
export const pickableNodeLinks = (links: NodeLink[]) => links.filter(l => l.status !== 'deleting')

/** What Add node offers: `ready` NodeLinks only, none on Jaah-managed storage (`nodelinks.birth_set`
 *  refuses it), and with `cloud` (a Your Cloud launch's connection) only those whose storage is on that cloud — the
 *  server refuses another cloud's (409 `CROSS_CLOUD`). No `store_provider` is AWS. */
export const addableNodeLinks = (links: NodeLink[], cloud?: CloudProvider | null) =>
  links.filter(l => l.status === 'ready' && !l.store_managed && (!cloud || (l.store_provider ?? 'aws') === cloud))

export function NodeLinkPicker({
  options,
  value,
  onChange,
  disabled = false,
  id,
  label = 'NodeLinks',
  error,
}: {
  options: NodeLink[]
  /** The picked ids. */
  value: string[]
  onChange: (ids: string[]) => void
  disabled?: boolean
  id: string
  label?: string
  /** The invalid-state message, under the list. */
  error?: string
}): JSX.Element {
  const shown = pickableNodeLinks(options)
  const toggle = (linkId: string, on: boolean) =>
    // Kept in the options' order, so the body does not depend on click order.
    onChange(shown.map(l => l.id).filter(x => (x === linkId ? on : value.includes(x))))
  return (
    <Box component="fieldset" id={id} data-testid={id} sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
      <Typography
        component="legend"
        sx={{ p: 0, mb: 0.5, fontSize: '0.8125rem', fontWeight: 500, color: error ? 'error.main' : 'text.secondary' }}
      >
        {label}
      </Typography>
      {shown.map(link => (
        <Box
          key={link.id}
          component="label"
          data-testid={`${id}-option`}
          sx={{ display: 'flex', alignItems: 'center', gap: 0.5, minWidth: 0, cursor: disabled ? 'default' : 'pointer' }}
        >
          <Checkbox
            checked={value.includes(link.id)}
            disabled={disabled}
            onChange={e => toggle(link.id, e.target.checked)}
            // Named by the NodeLink alone: the path is its description, not its name.
            slotProps={{ input: { 'aria-label': link.name, 'aria-describedby': `${id}-path-${link.id}` } }}
            sx={theme => ({ p: '0.5rem', minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx })}
          />
          <Box sx={{ minWidth: 0, flex: 1 }}>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, minWidth: 0 }}>
              {/* Wraps rather than ellipsizes: a narrow dialog must never shred a name. */}
              <Typography component="span" variant="body2" title={link.name} sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                {link.name}
              </Typography>
              {link.status !== 'ready' && (
                <StatusChip label={link.status} tone={STATUS_TONE[link.status] ?? 'neutral'} data-status={link.status} />
              )}
            </Box>
            <Typography
              component="span"
              id={`${id}-path-${link.id}`}
              variant="caption"
              title={link.mount_path}
              sx={[monoSx, { display: 'block', color: 'text.secondary', overflowWrap: 'anywhere' }]}
            >
              {link.mount_path}
            </Typography>
            {link.status !== 'ready' && (
              <Typography component="span" variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                Mounts once ready.
              </Typography>
            )}
          </Box>
        </Box>
      ))}
      {error && <FormHelperText error data-testid={`${id}-error`} sx={{ m: 0 }}>{error}</FormHelperText>}
    </Box>
  )
}

export default NodeLinkPicker
