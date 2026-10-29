import { useId } from 'react'
import SvgIcon from '@mui/material/SvgIcon'

type BrandWordmarkProps = {
  /** Rendered height in px; the width follows the 386×230 grid. Below ~32px a tile drops under 3px and
   *  the word turns to texture — use `BrandMark` there. */
  height: number
  className?: string
}

/* jaah.ai's wordmark (the marketing site's `Wordmark.tsx`): "jaah" in a 3-wide pixel alphabet, one
   rounded tile per [column, row] cell on a 26-unit pitch. `LIT` is the dot of the j — the slot that
   delivered — painted flat in `--sk-mark-core`; the rest take the `--sk-mark-*` gradient. */
const PITCH = 26
const LIT: readonly [number, number] = [2, 1]
const CELLS: readonly (readonly [number, number])[] = [
  // j
  [2, 2], [2, 3], [2, 4], [2, 5], [2, 6], [2, 7], [0, 8], [1, 8], [2, 8],
  // a
  [4, 2], [5, 2], [6, 2], [6, 3], [4, 4], [5, 4], [6, 4], [4, 5], [6, 5], [4, 6], [5, 6], [6, 6],
  // a
  [8, 2], [9, 2], [10, 2], [10, 3], [8, 4], [9, 4], [10, 4], [8, 5], [10, 5], [8, 6], [9, 6], [10, 6],
  // h
  [12, 0], [12, 1], [12, 2], [13, 2], [14, 2], [12, 3], [14, 3], [12, 4], [14, 4], [12, 5], [14, 5],
  [12, 6], [14, 6],
]

/**
 * The Jaah wordmark, single-homed for the expanded sidebar and the login card. Colours come from the
 * same `--sk-mark-*` custom properties as `BrandMark` (the callers set them per scheme from
 * `theme.custom.sidebar`), with dark-ground fallbacks.
 */
export default function BrandWordmark({ height, className }: BrandWordmarkProps) {
  const grad = `sk-wm-${useId().replace(/:/g, '')}`
  const width = (height * 386) / 230
  const tile = (x: number, y: number, fill: string) => (
    <rect key={`${x}-${y}`} x={x * PITCH} y={y * PITCH} width="22" height="22" rx="4.4" fill={fill} />
  )

  return (
    <SvgIcon
      className={className}
      viewBox="0 0 386 230"
      titleAccess="Jaah"
      sx={{ width, height, fontSize: height, display: 'block', flexShrink: 0, fill: 'none' }}
    >
      <defs>
        <linearGradient id={grad} x1="0" y1="0" x2="386" y2="230" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="var(--sk-mark-0, #38bdf8)" />
          <stop offset="0.55" stopColor="var(--sk-mark-1, #3b82f6)" />
          <stop offset="1" stopColor="var(--sk-mark-2, #a855f7)" />
        </linearGradient>
      </defs>
      {CELLS.map(([x, y]) => tile(x, y, `url(#${grad})`))}
      {tile(LIT[0], LIT[1], 'var(--sk-mark-core, #e0f2fe)')}
    </SvgIcon>
  )
}
