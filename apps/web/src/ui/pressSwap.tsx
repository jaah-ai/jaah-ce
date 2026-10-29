import type { JSX, ReactNode } from 'react'

/* THE PRESS SWAP: a pressable status mark shows its STATE at rest and its ACTION while pointed at or focused:
 * a Running disc that opens the console turns into the console icon, a Draft that starts turns
 * into ▶, an Enabled toggle into ⊘ Disable. Pure CSS on two sibling spans, so hover never re-renders a row and
 * no state is kept. Hover only where the device has one (`hover: hover`): a touch tap would flash the swap as it
 * fires. Keyboard focus swaps too (`:focus-visible`, and MUI's `.Mui-focusVisible` on a clickable Chip or
 * Button). Hosts: `./StatusChip.tsx` (`press`), `./feed/FeedList.tsx`'s disc (`pressGlyph`), and the console's
 * state toggles. A disabled control never swaps: its host leaves the sx off. */
const REST_CLASS = 'press-swap-rest'
const PRESS_CLASS = 'press-swap-press'

const SHOW_PRESS = {
  [`& .${REST_CLASS}`]: { display: 'none' },
  [`& .${PRESS_CLASS}`]: { display: 'inline-flex' },
} as const

/* On the pressable element itself, the one whose hover and focus count; both spans sit inside it. Each face owns
   the glyph-to-word gap (a pill's 4px), so a host without one (a text Button) still reads "✓ Grant". */
export const PRESS_SWAP_SX = {
  [`& .${REST_CLASS}`]: { display: 'inline-flex', alignItems: 'center', gap: 0.5 },
  [`& .${PRESS_CLASS}`]: { display: 'none', alignItems: 'center', gap: 0.5 },
  '@media (hover: hover)': { '&:hover': SHOW_PRESS },
  '&:focus-visible, &.Mui-focusVisible': SHOW_PRESS,
} as const

/* What a press shows: its mark and, on a pill wide enough for a word, the word ("▶ Start"). */
export type PressFace = { glyph: ReactNode; label?: string }

/* The two faces. The press face is `aria-hidden`: the host's accessible name already says what pressing does. */
export function PressSwap({ rest, press }: { rest: ReactNode; press: PressFace }): JSX.Element {
  return (
    <>
      <span className={REST_CLASS} data-testid="rest-face">{rest}</span>
      <span className={PRESS_CLASS} aria-hidden="true" data-testid="press-face">
        <span>{press.glyph}</span>
        {press.label !== undefined && <span>{press.label}</span>}
      </span>
    </>
  )
}
