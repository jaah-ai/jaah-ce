import { useId, type JSX } from 'react'
import Box from '@mui/material/Box'
import FormControlLabel from '@mui/material/FormControlLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import HelpLabel, { ExplainMoreButton } from './HelpLabel'
import { toExplain, type HelpProp } from './explain'
import { closeTapTips } from './Tip'
import { PHONE, TAP_FLOOR_DOWN } from './listStyles'
import { FONT_WEIGHT, SIZE } from '../theme'

/* A labelled radio group whose every option carries its one-line consequence as a HOVER on the option's label
 * (`ui/HelpLabel`; a decision is never a bare word, and never a caption line under it). A `null` help draws a
 * plain label; `testId` on an option lands on its radio input, which the help also describes. `help` on the
 * group explains the legend. `row` sets the options side by side, stacked again on a phone.
 * An option's hover opens ABOVE it and goes on a pick (wherever the click lands, radio or label), so it never sits
 * over the field the pick reveals below. */

export type Choice<V extends string> = { value: V; label: string; help: string | null; disabled?: boolean; testId?: string }

export function ChoiceField<V extends string>({
  label,
  help,
  value,
  options,
  onChange,
  testId,
  disabled = false,
  row = false,
}: {
  label: string
  /** The legend's explanation (`ui/HelpLabel`). */
  help?: HelpProp
  value: V
  options: readonly Choice<V>[]
  onChange: (next: V) => void
  testId?: string
  disabled?: boolean
  row?: boolean
}): JSX.Element {
  const labelId = useId()
  const tip = toExplain(help)?.tip
  return (
    <Box component="fieldset" data-testid={testId} sx={{ border: 0, m: 0, p: 0, minWidth: 0, position: 'relative' }}>
      <Typography id={labelId} component="legend" variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium, mb: 0 }}>
        <HelpLabel label={label} help={help} />
      </Typography>
      <RadioGroup row={row} aria-labelledby={labelId} aria-description={tip} value={value} onChange={e => { closeTapTips(); onChange(e.target.value as V) }}
        sx={row ? { columnGap: 3, [PHONE]: { flexDirection: 'column' } } : undefined}>
        {options.map(option => (
          <FormControlLabel
            key={option.value}
            value={option.value}
            disabled={disabled || option.disabled}
            control={<Radio size="small" slotProps={{ input: { 'data-testid': option.testId, 'aria-description': option.help ?? undefined } as object }} />}
            sx={{
              alignItems: 'flex-start', mr: 0, '& .MuiRadio-root': { pt: 0.5 }, '& .MuiFormControlLabel-label': { minWidth: 0 },
              // A 32px row: the phone's tap floor lifts it (`ui/listStyles.ts`).
              [TAP_FLOOR_DOWN]: { minHeight: SIZE.tapTarget },
            }}
            label={
              <Typography variant="body2" sx={{ py: 0.5, overflowWrap: 'anywhere', fontWeight: FONT_WEIGHT.medium }}>
                <HelpLabel label={option.label} help={option.help ?? undefined} placement="top" />
              </Typography>
            }
          />
        ))}
      </RadioGroup>
      <ExplainMoreButton label={label} help={help} />
    </Box>
  )
}
