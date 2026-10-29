import Box from '@mui/material/Box'
import type { Theme } from '@mui/material/styles'
import { featureIconColour, parseFeatureIcon } from '../../lib/featureIcon'

/* A feature's icon (`lib/featureIcon.ts`): a filled circle, square or equilateral triangle in the token's
 * colour for the active scheme — `applyStyles('dark')`, never a `palette.mode` ternary (frozen under
 * `cssVariables`). Null or an unknown token renders NOTHING, so a feature with no icon takes no space.
 * Always `aria-hidden`: it decorates a name that is already text. `inline` is for a name inside a text run (a
 * single-line ellipsised title, a crumb): it sits on the text's middle with its own trailing gap, so the text
 * after it loses exactly icon + gap and nothing else. */
export default function FeatureIcon({
  icon,
  size = 10,
  testId = 'feature-icon',
  inline = false,
}: {
  icon: string | null | undefined
  size?: number
  testId?: string
  inline?: boolean
}) {
  const parsed = parseFeatureIcon(icon)
  if (!parsed) return null
  const { shape, colour } = parsed
  return (
    <Box
      component="svg"
      viewBox="0 0 10 10"
      aria-hidden="true"
      focusable="false"
      data-testid={testId}
      data-icon={icon}
      sx={(theme: Theme) => ({
        width: size,
        height: size,
        display: inline ? 'inline-block' : 'block',
        flex: '0 0 auto',
        ...(inline && { verticalAlign: 'middle', mr: '0.4em', position: 'relative', top: '-0.06em' }),
        color: featureIconColour(colour, 'light'),
        ...theme.applyStyles('dark', { color: featureIconColour(colour, 'dark') }),
      })}
    >
      {/* Each shape fills the box's optical weight: the square is inset so it does not read heavier than
          the dot; the triangle is equilateral (side 10, height ≈ 8.66) and centred vertically. */}
      {shape === 'dot' && <circle cx="5" cy="5" r="5" fill="currentColor" />}
      {shape === 'square' && <rect x="0.5" y="0.5" width="9" height="9" rx="1" fill="currentColor" />}
      {shape === 'triangle' && <polygon points="5,0.67 10,9.33 0,9.33" fill="currentColor" />}
    </Box>
  )
}
