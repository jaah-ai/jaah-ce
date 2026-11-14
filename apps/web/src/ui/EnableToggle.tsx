import { type MouseEvent, useEffect, useRef, useState } from 'react'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import { apiDetail } from '../api'
import { PHONE } from './listStyles'
import { FONT_SIZE, SIZE, grid } from '../theme'
import { PRESS_SWAP_SX, PressSwap } from './pressSwap'

/* THE CONSOLE'S ONE `enabled` TOGGLE (nodes, the account roster, the account page). A toggle, not a badge: one
 * click, no confirmation (instantly reversible); `busy` guards double writes. The state reads by shape and word,
 * not colour: ✓ / ⊘ (not ✕, the app's delete glyph), `aria-pressed` and `data-state`.
 *
 * Optimistic: the flipped value is held APART from the prop and wins while held, so a mid-flight refetch carrying
 * the old value cannot flip it back; it hands authority back when the prop AGREES, and is dropped on a refusal
 * (the server's reason goes to `onError`).
 *
 * `disabledReason` makes it inert, still showing its state, with the reason as its name: disabled, not absent,
 * because this control is often the only rendering of `enabled`. Faces: `button` (a small outlined button, the
 * size of the verbs beside it) and `chip` (a compact pill for a feed row's slot). */

const DISABLE_PRESS = { glyph: '⊘', label: 'Disable' }
const ENABLE_PRESS = { glyph: '✓', label: 'Enable' }

/* A disabled control keeps pointer events so its `title` (the reason) is reachable; it still fires no click. */
const DISABLED_HOVER = { '&.Mui-disabled': { pointerEvents: 'auto', cursor: 'not-allowed' } } as const

const BUTTON_SX = { whiteSpace: 'nowrap', gap: 0.5, ...DISABLED_HOVER, [PHONE]: { minHeight: SIZE.tapTarget } } as const

/* "Disabled" at the 11px face, 3px padding and its border fit a 56px slot. The label lays the two swap faces
   out in a row only while live; a plain label keeps its ellipsis. */
const CHIP_SX = { height: grid(3), maxWidth: '100%', '& .MuiChip-label': { px: 0.5, fontSize: FONT_SIZE.xs }, ...DISABLED_HOVER } as const
const CHIP_SWAP_SX = { '& .MuiChip-label': { display: 'inline-flex' } } as const

export default function EnableToggle({
  enabled,
  write,
  onToggled,
  onError,
  failMessage = next => `Failed to ${next ? 'enable' : 'disable'}`,
  disabledReason,
  warning,
  variant = 'button',
}: {
  enabled: boolean // as the server last reported it
  write: (next: boolean) => Promise<unknown>
  onToggled?: (next: boolean) => void // once the write lands, so the caller can refetch
  onError?: (message: string) => void // the refusal in words (`apiDetail` over `failMessage`)
  failMessage?: (next: boolean) => string
  disabledReason?: string // set: inert, with this as its name
  warning?: string // appended to the Disable name, read when it matters
  variant?: 'button' | 'chip'
}) {
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const [busy, setBusy] = useState(false)
  // A refresh can remove the row while its write is in flight.
  const aliveRef = useRef(true)
  // Set on mount too: StrictMode mounts, cleans up and mounts again (`ui/useCopyFlash.ts`'s shape).
  useEffect(() => { aliveRef.current = true; return () => { aliveRef.current = false } }, [])
  useEffect(() => {
    if (optimistic !== null && enabled === optimistic) setOptimistic(null)
  }, [enabled, optimistic])

  const shown = optimistic ?? enabled
  const inert = busy || disabledReason !== undefined

  async function submit() {
    // `disabled` stops a real click; this catches a programmatic one.
    if (inert) return
    const next = !shown
    setBusy(true)
    setOptimistic(next)
    try {
      await write(next)
      if (aliveRef.current) onToggled?.(next)
    } catch (err) {
      if (!aliveRef.current) return
      setOptimistic(null)
      onError?.(apiDetail(err, failMessage(next)))
    } finally {
      if (aliveRef.current) setBusy(false)
    }
  }

  const name = disabledReason
    ?? (shown ? `Enabled — click to disable${warning ? `. ${warning}` : ''}` : 'Disabled — click to enable')
  const press = shown ? DISABLE_PRESS : ENABLE_PRESS
  const common = {
    'data-state': shown ? 'enabled' : 'disabled',
    disabled: inert,
    'aria-pressed': shown,
    'aria-label': name,
    title: name,
    // A clickable row around it navigates; this control does not.
    onClick: (e: MouseEvent) => { e.stopPropagation(); void submit() },
  }

  if (variant === 'chip') {
    // The roster's pill: Enabled is the normal state, so a bare ✓; the exception keeps its word.
    const rest = shown ? '✓' : 'Disabled'
    return (
      <Chip
        {...common}
        component="button"
        type="button"
        size="small"
        variant="outlined"
        color={shown ? 'success' : 'default'}
        clickable
        label={inert ? rest : <PressSwap rest={rest} press={press} />}
        sx={[CHIP_SX, !inert && PRESS_SWAP_SX, !inert && CHIP_SWAP_SX]}
      />
    )
  }

  const rest = <><span aria-hidden="true">{shown ? '✓' : '⊘'}</span>{shown ? 'Enabled' : 'Disabled'}</>
  return (
    <Button
      {...common}
      type="button"
      size="small"
      variant="outlined"
      color={shown ? 'success' : 'inherit'}
      sx={{ ...BUTTON_SX, ...(!inert && PRESS_SWAP_SX) }}
    >
      {inert ? rest : <PressSwap rest={rest} press={press} />}
    </Button>
  )
}
