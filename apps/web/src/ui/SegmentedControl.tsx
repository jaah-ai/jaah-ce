import type { JSX, ReactNode } from 'react'
import ToggleButton from '@mui/material/ToggleButton'
import ToggleButtonGroup from '@mui/material/ToggleButtonGroup'
import type { SxProps, Theme } from '@mui/material/styles'

/* `disabled` + `describedBy`: one option off, its reason in visible text elsewhere, named by id. */
export type SegmentedOption<T> = { value: T; label: ReactNode; testId?: string; disabled?: boolean; describedBy?: string }

/* The console's ONE exactly-one-of-N picker: MUI's `ToggleButtonGroup` look with RADIO semantics
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
  buttonSx = { px: 1, py: 0.25, fontSize: '0.75rem' },
}: {
  value: T
  options: readonly SegmentedOption<T>[]
  onChange: (next: T) => void
  ariaLabel?: string
  ariaLabelledBy?: string
  /** Visible text explaining the whole picker, named by id. */
  ariaDescribedBy?: string
  testId?: string
  /** `primary` where MUI's faint grey selected tint would not read. */
  color?: 'primary'
  fullWidth?: boolean
  disabled?: boolean
  sx?: SxProps<Theme>
  buttonSx?: SxProps<Theme>
}): JSX.Element {
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
      sx={sx}
      onChange={(_event, next: T | null) => { if (next !== null) onChange(next) }}
    >
      {options.map(option => (
        <ToggleButton
          key={option.value}
          value={option.value}
          role="radio"
          aria-checked={value === option.value}
          aria-pressed={undefined}
          data-testid={option.testId}
          disabled={option.disabled}
          aria-describedby={option.describedBy}
          /* 26px: WCAG 2.2 SC 2.5.8's 24x24 floor (small renders ~22px). `minHeight`, so it grows no further. */
          sx={[{ minHeight: 26 }, ...(Array.isArray(buttonSx) ? buttonSx : [buttonSx])]}
        >
          {option.label}
        </ToggleButton>
      ))}
    </ToggleButtonGroup>
  )
}
