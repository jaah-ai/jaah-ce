import type { JSX, ReactNode } from 'react'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import { OptionTooltip } from './HelpLabel'
import type { SxProps, Theme } from '@mui/material/styles'
import { FONT_SIZE, RADIUS, SEGMENT_TRACK_GROUND, grid } from '../theme'

/* `disabled` + `describedBy`: one option off, its reason in visible text elsewhere, named by id. `help`: what picking
 * it does (at most `TIP_MAX_WORDS` words, `ui/explain.ts`), the option's hover and accessible description.
 * `ariaLabel`: the option's accessible name where its `label` is a glyph; a `help` equal to it is not repeated as
 * the description. */
export type SegmentedOption<T> = {
  value: T
  label: ReactNode
  testId?: string
  disabled?: boolean
  describedBy?: string
  help?: string
  ariaLabel?: string
}

/** The track's recessed ground, a theme token; re-exported for the paperclip beside the task form's axes. */
export { SEGMENT_TRACK_GROUND }

/* ── The look: a recessed round track, the chosen option a raised chip ──
 * The operator's pick among the console's two segmented pickers. Heights are the buttons' own: the
 * track's hairline is an inset outline (no layout) and each button keeps a transparent 1px border, which also
 * insets the chip, so the track is no taller than the outlined bar it replaced.
 * A `color="primary"` picker's chip is filled (`theme.ts` `MuiToggleButton`), where LIGHT vs DARK must be unmistakable.
 */
const TRACK_SX = (theme: Theme) => ({
  borderRadius: RADIUS.round,
  backgroundColor: SEGMENT_TRACK_GROUND,
  outline: `1px solid ${theme.vars.palette.divider}`,
  outlineOffset: -1,
  maxWidth: '100%',
  /* MUI's grouped buttons overlap their borders by a pixel; the chips sit side by side. */
  '&& .MuiToggleButtonGroup-grouped': { m: 0 },
})

/* A locked picker: MUI marks only the buttons `Mui-disabled`, never the group. The chip keeps its fill, so the value
 * still reads, but drops the primary tint. */
const DISABLED_TRACK_SX = (theme: Theme) => ({
  opacity: 0.6,
  /* `&&`: outweighs the chip rule below whichever style loaded last (a picker first drawn locked). */
  '&& .MuiToggleButton-root.Mui-selected': { color: theme.vars.palette.text.secondary, boxShadow: 'none' },
})

/* The flex cell is the span (`OptionTooltip`'s listener), not the button. `1 1 auto`: in a content-sized group a zero
 * basis collapses the options to their padding. */
const CELL_SX = { flex: '1 1 auto', minWidth: 0, display: 'flex' } as const
/** `fullWidth`: equal cells, whatever their labels. */
const EQUAL_CELL_SX = { ...CELL_SX, flex: '1 1 0' } as const

const SEGMENT_SX = (theme: Theme) => ({
  width: '100%',
  minWidth: 0,
  m: 0,
  border: '1px solid transparent',
  backgroundClip: 'padding-box',
  borderRadius: `${RADIUS.round} !important`,
  transition: 'background-color 140ms, color 140ms, box-shadow 140ms',
  '&:hover': { backgroundColor: SEGMENT_TRACK_GROUND },
  '&.Mui-selected:not(.MuiToggleButton-primary)': {
    backgroundColor: theme.vars.palette.background.paper,
    color: theme.vars.palette.primary.main,
    boxShadow: 1,
    '&:hover': { backgroundColor: theme.vars.palette.background.paper },
  },
  '&.Mui-disabled': { borderColor: 'transparent' },
})

/* Every option's default face; a caller's `buttonSx` lands after it and overrides only the keys it names. */
const DEFAULT_BUTTON_SX = { px: 1, py: 0, fontSize: FONT_SIZE.sm } as const

/* The console's ONE exactly-one-of-N picker: MUI's `ToggleButtonGroup` with RADIO semantics
 * (`role="radiogroup"` + `aria-checked`; each option is its own tab stop — MUI has no arrow-key roving). Exactly one option
 * applies, so MUI's `null` (the selected option re-pressed) is swallowed. `aria-pressed={undefined}` REMOVES
 * MUI's toggle attribute (caller props spread after its own) — `NodeDetailPage.test.tsx` counts
 * `button[aria-pressed]`. Name it with `ariaLabel` or `ariaLabelledBy`. */
export default function SegmentedControl<T extends string | number>({
  value,
  options,
  onChange,
  ariaLabel,
  ariaLabelledBy,
  ariaDescribedBy,
  testId,
  color,
  fullWidth,
  disabled,
  sx,
  buttonSx: callerButtonSx,
}: {
  value: T
  options: readonly SegmentedOption<T>[]
  onChange: (next: T) => void
  ariaLabel?: string
  ariaLabelledBy?: string
  /** Visible text explaining the whole picker, named by id. */
  ariaDescribedBy?: string
  testId?: string
  /** A filled chip where the pick must be unmistakable. */
  color?: 'primary'
  fullWidth?: boolean
  disabled?: boolean
  sx?: SxProps<Theme>
  /** Merged AFTER the default face, so a caller adds only what differs (a phone tap floor) and keeps the rest. */
  buttonSx?: SxProps<Theme>
}): JSX.Element {
  const callerSx = Array.isArray(sx) ? sx : [sx]
  const buttonSx = [SEGMENT_SX, DEFAULT_BUTTON_SX, ...(Array.isArray(callerButtonSx) ? callerButtonSx : [callerButtonSx])]
  return (
    <ToggleButtonGroup
      value={value}
      exclusive
      size="small"
      role="radiogroup"
      aria-label={ariaLabel}
      aria-labelledby={ariaLabelledBy}
      aria-describedby={ariaDescribedBy}
      data-testid={testId}
      color={color}
      fullWidth={fullWidth}
      disabled={disabled}
      sx={[TRACK_SX, !!disabled && DISABLED_TRACK_SX, ...callerSx]}
      onChange={(_event, next: T | null) => { if (next !== null) onChange(next) }}
    >
      {/* Tooltip-wrapped buttons are safe: the group publishes its props through React context, not `cloneElement`. */}
      {options.map(option => (
        <OptionTooltip key={option.value} help={option.help} cellSx={fullWidth ? EQUAL_CELL_SX : CELL_SX}>
        <ToggleButton
          value={option.value}
          role="radio"
          aria-checked={value === option.value}
          aria-pressed={undefined}
          aria-label={option.ariaLabel}
          data-testid={option.testId}
          disabled={option.disabled}
          aria-describedby={option.describedBy}
          aria-description={option.help === option.ariaLabel ? undefined : option.help}
          /* 24px: WCAG 2.2 SC 2.5.8's 24x24 floor (small renders ~22px). `minHeight`, so it grows no further. */
          sx={[{ minHeight: grid(4) }, ...(Array.isArray(buttonSx) ? buttonSx : [buttonSx])]}
        >
          {option.label}
        </ToggleButton>
        </OptionTooltip>
      ))}
    </ToggleButtonGroup>
  )
}
