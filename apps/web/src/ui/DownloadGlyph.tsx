import type { JSX } from 'react'
import { Icon } from './icons'

/* The download arrow, one copy (third copy triggers extraction — `docs/policies/context-architecture.md`).
 * Drawn: no icon package here, and Unicode arrows render as emoji or sit off the text baseline.
 * `size` is the only knob (toolbar 16, 24px chip, 12px label). `currentColor` inherits the button's
 * states; `aria-hidden` because every caller's control already names the action. `display: block`
 * removes the inline-SVG baseline gap. */
export function DownloadGlyph({ size = 16 }: { size?: number }): JSX.Element {
  return (
    <Icon size={size} sx={{ display: 'block', fill: 'currentColor' }}>
      <path d="M7.25 1.75a.75.75 0 0 1 1.5 0v5.69l1.72-1.72a.75.75 0 1 1 1.06 1.06l-3 3a.75.75 0 0 1-1.06 0l-3-3a.75.75 0 0 1 1.06-1.06l1.72 1.72V1.75Z" />
      <path d="M2.75 10.5a.75.75 0 0 1 .75.75v1.5h9v-1.5a.75.75 0 0 1 1.5 0v1.75a1.25 1.25 0 0 1-1.25 1.25H3.25A1.25 1.25 0 0 1 2 13v-1.75a.75.75 0 0 1 .75-.75Z" />
    </Icon>
  )
}
