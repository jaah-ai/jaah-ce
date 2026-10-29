import { useId, type ReactNode } from 'react'
import SvgIcon from '@mui/material/SvgIcon'
import { LOGO_THEME, type LogoTheme } from './logoTheme'

type BrandMarkProps = {
  /** Rendered width/height of the glyph in px. */
  size?: number
  className?: string
}

/**
 * The Jaah mark in the active logo theme (`logoTheme.ts`) — the one component callers render.
 */
export default function BrandMark(props: BrandMarkProps) {
  const Glyph = GLYPH[LOGO_THEME]
  return <Glyph {...props} />
}

/**
 * The standard Jaah mark — four agent slots stacked into a J on a rounded tile (three up the stem, one lit foot:
 * the slot that delivered) — single-homed for the collapsed sidebar; `BrandWordmark` is the whole name.
 * Geometry is jaah.ai's `icon.svg` (the marketing site's); the colours are the console's instead of its
 * ink-on-paper: the tile takes the `--sk-mark-*` gradient (inherited from any ancestor, fallbacks for a
 * dark ground) and the slots are white, so the mark carries its own contrast on any ground.
 */
function StandardMark(props: BrandMarkProps) {
  const grad = `sk-grad-${useId().replace(/:/g, '')}`

  return (
    <MarkSvg {...props}>
      <defs>
        <linearGradient id={grad} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="var(--sk-mark-0, #38bdf8)" />
          <stop offset="0.55" stopColor="var(--sk-mark-1, #3b82f6)" />
          <stop offset="1" stopColor="var(--sk-mark-2, #a855f7)" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="6" fill={`url(#${grad})`} />
      <g fill="#ffffff">
        <rect x="17.25" y="3" width="7" height="7" rx="1.5" opacity="0.5" />
        <rect x="17.25" y="12.5" width="7" height="7" rx="1.5" opacity="0.5" />
        <rect x="17.25" y="22" width="7" height="7" rx="1.5" opacity="0.5" />
        <rect x="7.75" y="22" width="7" height="7" rx="1.5" />
      </g>
    </MarkSvg>
  )
}

/**
 * The Halloween mark — the same J as a jack-o'-lantern: the tile is a pumpkin (a green stem, faint ribs), the
 * slots are carved holes, and the slot that delivered glows like the candle inside. Its colours are its own,
 * not the `--sk-mark-*` stops, so it reads the same on every ground.
 */
function HalloweenMark(props: BrandMarkProps) {
  const id = useId().replace(/:/g, '')
  const skin = `sk-pumpkin-${id}`
  const glow = `sk-candle-${id}`

  return (
    <MarkSvg {...props}>
      <defs>
        <linearGradient id={skin} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#fdba74" />
          <stop offset="0.55" stopColor="#f97316" />
          <stop offset="1" stopColor="#c2410c" />
        </linearGradient>
        <radialGradient id={glow} cx="11.25" cy="26.75" r="6" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#fef08a" />
          <stop offset="1" stopColor="#f59e0b" />
        </radialGradient>
      </defs>
      <path d="M15 4.5C15 2.5 16 1 18.5.5l.7 1.3C17.6 2.4 17 3.3 17 4.5Z" fill="#15803d" />
      <rect y="3.5" width="32" height="28.5" rx="8" fill={`url(#${skin})`} />
      <path d="M10.5 4.5Q7.5 18 10.5 31.5M21.5 4.5Q24.5 18 21.5 31.5" stroke="#9a3412" strokeWidth="0.6" opacity="0.35" />
      <g fill="#431407">
        <rect x="17.25" y="6.5" width="7" height="6.5" rx="1.5" />
        <rect x="17.25" y="15" width="7" height="6.5" rx="1.5" />
        <rect x="17.25" y="23.5" width="7" height="6.5" rx="1.5" />
      </g>
      <rect x="7.75" y="23.5" width="7" height="6.5" rx="1.5" fill={`url(#${glow})`} />
    </MarkSvg>
  )
}

/** The 32-unit tile every mark draws on. `SvgIcon`, not a bare <svg>: `titleAccess` carries the accessible name
 *  (SvgIcon is `aria-hidden` otherwise). `fill: 'none'` cancels its `currentColor` default. */
function MarkSvg({ size = 24, className, children }: BrandMarkProps & { children: ReactNode }) {
  return (
    <SvgIcon className={className} viewBox="0 0 32 32" titleAccess="Jaah"
      sx={{ width: size, height: size, fontSize: size, display: 'block', flexShrink: 0, fill: 'none' }}>
      {children}
    </SvgIcon>
  )
}

const GLYPH: Record<LogoTheme, (props: BrandMarkProps) => JSX.Element> = {
  standard: StandardMark,
  halloween: HalloweenMark,
}
