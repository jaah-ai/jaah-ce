import type { JSX } from 'react'
import { StatusChip } from './StatusChip'

/* THE ONE "ENABLED" STATE PILL: a green ✓, no word. Enabled is the normal state, so it reads as a quiet
 * mark and the exceptions (Disabled, Draining, …) keep their words beside it. "Enabled" stays the
 * accessible name; `title` adds the surface's own sentence. Every read-only enabled-state display uses
 * this — never a `StatusChip` spelling "Enabled" — so they all look the same. */
const GLYPH = '✓'

export function EnabledChip({ title }: { title?: string }): JSX.Element {
  return (
    <StatusChip
      iconOnly
      label="Enabled"
      tone="success"
      glyph={GLYPH}
      title={title ?? 'Enabled'}
      // The name always leads with the state, even when `title` is only the surface's sentence.
      ariaLabel={!title || title.startsWith('Enabled') ? title ?? 'Enabled' : `Enabled — ${title}`}
      data-status="enabled"
    />
  )
}

export default EnabledChip
