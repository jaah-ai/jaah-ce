import { useEffect, useRef, useState } from 'react'
import Chip from '@mui/material/Chip'
import Button from '@mui/material/Button'
import { put, apiDetail } from '../api'
import { useCan } from '../auth'
import { PHONE } from '../ui/listStyles'
import { PRESS_SWAP_SX, PressSwap } from '../ui/pressSwap'

// What the toggle shows while pointed at: the flip a click makes (`ui/pressSwap.tsx`).
const DISABLE_PRESS = { glyph: '⊘', label: 'Disable' }
const ENABLE_PRESS = { glyph: '✓', label: 'Enable' }

// The node `enabled` control as ONE shared toggle, in the node page's Overview actions row
// (`pages/NodeDetailPage.tsx`); the `/nodes` table only reads the state and offers the verbs in its ⋯. A TOGGLE,
// not a badge: it sits beside a delete action. A small outlined button carrying its STATE WORD, like the
// labelled verbs beside it (a bare glyph read as decoration there). No confirmation (one click, instantly reversible); `busy`
// guards double writes.
//
// `enabled` + `draining` spell Enabled / Draining / Disabled (`types.ts::NodeRecord` is the single home);
// Draining renders as a read-only pill.
//
// A node present on the bus but never registered has no `nodes` row and `PUT` 404s, so the caller passes
// registered-ness: `first_seen !== null` ⟺ registered (`Node.first_seen` is non-nullable in
// `apps/api/app/models/`; null only for a presence-only entry `GET /api/nodes` unions in).
/* THE WRITES, exported so each verb has ONE implementation across its homes (the toggle, the row's ⋯
 * menu, the node page). `PUT /api/nodes/{node_id}` applies `exclude_unset=True`
 * (`apps/api/app/routes/nodes/update.py`), so each body carries ONLY the keys that verb owns — re-sending a
 * whole record would overwrite a concurrent edit.
 *
 * THE ASYMMETRY IS NOT A TYPO: Enable sends `{ enabled: true, draining: false }` because it is also the
 * cancel-a-drain verb (without it, Enable on a draining node is a silent no-op). Disable sends
 * `{ enabled: false }` alone because the server clears `draining` itself. */
export function setNodeEnabled(nodeId: string, enabled: boolean): Promise<unknown> {
  return put(
    `/api/nodes/${encodeURIComponent(nodeId)}`,
    enabled ? { enabled: true, draining: false } : { enabled: false },
  )
}

/* DRAIN: no new tasks; the node disables ITSELF when empty. `enabled` is not sent (a draining node IS
 * enabled); the server 422s `draining: true` on a disabled node. */
export function setNodeDraining(nodeId: string): Promise<unknown> {
  return put(`/api/nodes/${encodeURIComponent(nodeId)}`, { draining: true })
}

/* THE DRAINING PILL'S WORDS: the state AND what happens next without anyone touching it. Exported so the
 * pill and its tests hold one promise. */
export const DRAINING_REASON =
  'Draining — no new tasks; it will disable itself when its tasks finish'

export const NOT_REGISTERED_REASON =
  'Not registered — connected but has no record, so it cannot be enabled or disabled'

/* ── WHO MAY PRESS IT ──
 * `PUT /api/nodes/{node_id}` is `@feature("nodes", "edit")` (`routes/nodes/update.py::update_node`), and
 * view-without-edit is a grant set a tenant holds (`apps/api/app/authz/`).
 * Gated IN THE COMPONENT, not per call site: one verb, one answer. `useCanSetNodeEnabled` is exported for
 * the ⋯ item that calls `setNodeEnabled` without rendering this control. Presentation only
 * (`components/RequireFeature.tsx`); `authz_guard` decides on the route.
 *
 * DISABLED, NOT ABSENT — an exception to "absent for a permission, disabled for a state" (`pages/Nodes.tsx`
 * applies that to Delete): this control is the ONLY rendering of `enabled`, so withholding it would
 * assert "no enabled state". The state stays (glyph, colour, `aria-pressed`, `data-state`) and the
 * control goes dead with a reason that names the missing grant, never "busy". A read-only glyph was
 * rejected: it changes size, role and tab order between roles and drops `aria-pressed`. */
export const NOT_PERMITTED_REASON =
  'needs the nodes:edit permission — ask an operator'

/** Whether this viewer may write `enabled` at all — `nodes:edit`, the key `update_node` declares. */
export function useCanSetNodeEnabled(): boolean {
  return useCan()('nodes:edit')
}

export default function NodeEnableToggle({
  nodeId,
  enabled,
  draining,
  registered,
  onToggled,
  onError,
}: {
  nodeId: string
  /** The node's `enabled` as the server last reported it. */
  enabled: boolean
  /** The node's `draining` as the server last reported it — the pill arm below, not a toggle position. */
  draining: boolean
  /** `first_seen !== null` on the node — see the discriminator note above. */
  registered: boolean
  /** Called with the new value once the write lands, so the caller can refresh its own list. */
  onToggled?: (enabled: boolean) => void
  /** Called with a human-readable reason when the write fails; the icon rolls back either way. */
  onError?: (message: string) => void
}) {
  // The optimistic value, held SEPARATELY from the prop: while held it wins, so a mid-flight refetch
  // carrying the pre-toggle value cannot flip the icon back; on failure dropping it restores the
  // server's value.
  const canEdit = useCanSetNodeEnabled()
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  // Unmount guard: a refresh can remove this list row while its write is in flight.
  const aliveRef = useRef(true)
  useEffect(() => () => { aliveRef.current = false }, [])

  // Hand authority back when the server AGREES with the optimistic value, not when the request resolves.
  useEffect(() => {
    if (optimistic !== null && enabled === optimistic) setOptimistic(null)
  }, [enabled, optimistic])

  const shown = optimistic ?? enabled

  async function submit() {
    // Guards the WRITE too: `disabled` stops a real click; this catches a programmatic one.
    if (busy || !registered || !canEdit) return
    const next = !shown
    setBusy(true)
    setOptimistic(next)
    try {
      await setNodeEnabled(nodeId, next)
      if (!aliveRef.current) return
      onToggled?.(next)
    } catch (err) {
      if (!aliveRef.current) return
      setOptimistic(null)
      onError?.(apiDetail(err, `Failed to ${next ? 'enable' : 'disable'} ${nodeId}`))
    } finally {
      if (aliveRef.current) setBusy(false)
    }
  }

  /* ── WHY DRAINING IS A PILL AND NOT A THIRD TOGGLE POSITION ──
   * Draining has TWO next moves (cancel back to Enabled, Disable now) that a two-position toggle cannot
   * express, so it renders as the FACT; the verbs live in the row's ⋯ menu (`pages/Nodes.tsx`) and the
   * node page's actions row (`pages/NodeDetailPage.tsx`). It renders for every reader (no verb to
   * withhold). Unreachable for an unregistered node (the caller passes `false`). The state is a WORD and
   * a different SHAPE from the outlined button, so the three states differ before any colour is read. */
  if (draining) {
    return (
      <Chip
        size="small"
        variant="outlined"
        color="warning"
        /* The same `data-state` attribute the other arms publish. Not `ui/StatusChip`: its prop list is
           closed and spells `data-status`. */
        data-state="draining"
        /* `role="img"` so `aria-label` is actually the accessible NAME (a bare `<div>` drops it). Inert:
           no tab stop, no click handler. */
        role="img"
        aria-label={DRAINING_REASON}
        title={DRAINING_REASON}
        label="Draining"
      />
    )
  }

  // The accessible name states the CURRENT state and the action a click performs; `aria-pressed` carries
  // the state independently. Without `nodes:edit` it still leads with the STATE, then why it is dead.
  // Unregistered: the reason is the whole answer (`enabled` describes no live node).
  const name =
    !registered ? NOT_REGISTERED_REASON
    : !canEdit ? `${shown ? 'Enabled' : 'Disabled'} — ${NOT_PERMITTED_REASON}`
    : shown ? 'Enabled — click to disable'
    : 'Disabled — click to enable'

  const inert = busy || !registered || !canEdit
  // Shape and word, not colour: ✓ and ⊘ differ in form (greyscale, CVD, forced colours). ⊘ not ✕: ✕ is the
  // app's DELETE glyph and sits beside this.
  const restFace = <><span aria-hidden="true">{shown ? '✓' : '⊘'}</span>{shown ? 'Enabled' : 'Disabled'}</>

  return (
    <Button
      type="button"
      size="small"
      variant="outlined"
      color={shown ? 'success' : 'inherit'}
      // The state as a machine-readable attribute (see NodeEnableToggle.test.tsx's positive controls).
      data-state={shown ? 'enabled' : 'disabled'}
      // Disabled while its own write is in flight, and for a reader without `nodes:edit`.
      disabled={inert}
      aria-pressed={shown}
      aria-label={name}
      title={name}
      onClick={e => {
      // The row navigates on click; this control does not.
        e.stopPropagation()
        void submit()
      }}
      /* The same box as the verbs beside it (`NodeDetailPage.tsx::ACTION_BUTTON_SX`): one line, a 44px phone
         touch floor. The DISABLED arm keeps pointer events so the reason on `title` is reachable (MUI's
         `pointer-events: none` swallows the hover); a disabled button still fires no click. */
      sx={{
        whiteSpace: 'nowrap',
        gap: 0.5,
        '&.Mui-disabled': { pointerEvents: 'auto', cursor: 'not-allowed' },
        [PHONE]: { minHeight: 44 },
        ...(!inert && PRESS_SWAP_SX),
      }}
    >
      {/* Pointed at, an editable toggle shows the flip it makes (`ui/pressSwap.tsx`). */}
      {inert ? restFace : <PressSwap rest={restFace} press={shown ? DISABLE_PRESS : ENABLE_PRESS} />}
    </Button>
  )
}
