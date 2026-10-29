import { useId, type JSX } from 'react'
import Box from '@mui/material/Box'
import FormControlLabel from '@mui/material/FormControlLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import { PHONE, TAP_FLOOR_DOWN } from './listStyles'

/* A labelled radio group whose every option carries its one-line consequence under it (a decision is never a
 * bare word). A `null` hint draws nothing; `testId` on an option lands on its radio input. `row` sets the options
 * side by side, stacked again on a phone. */

export type Choice<V extends string> = { value: V; label: string; hint: string | null; disabled?: boolean; testId?: string }

export function ChoiceField<V extends string>({
  label,
  value,
  options,
  onChange,
  testId,
  disabled = false,
  row = false,
}: {
  label: string
  value: V
  options: readonly Choice<V>[]
  onChange: (next: V) => void
  testId?: string
  disabled?: boolean
  row?: boolean
}): JSX.Element {
  const labelId = useId()
  return (
    <Box component="fieldset" data-testid={testId} sx={{ border: 0, m: 0, p: 0, minWidth: 0 }}>
      <Typography id={labelId} component="legend" variant="body2" sx={{ fontWeight: 500, mb: 0.25 }}>
        {label}
      </Typography>
      <RadioGroup row={row} aria-labelledby={labelId} value={value} onChange={e => onChange(e.target.value as V)}
        sx={row ? { columnGap: 3, [PHONE]: { flexDirection: 'column' } } : undefined}>
        {options.map(option => (
          <FormControlLabel
            key={option.value}
            value={option.value}
            disabled={disabled || option.disabled}
            control={<Radio size="small" slotProps={{ input: { 'data-testid': option.testId } as object }} />}
            sx={{
              alignItems: 'flex-start', mr: 0, '& .MuiRadio-root': { pt: 0.5 }, '& .MuiFormControlLabel-label': { minWidth: 0 },
              // A hint-less option is a 32px row: the phone's tap floor lifts it (`ui/listStyles.ts`).
              [TAP_FLOOR_DOWN]: { minHeight: 44 },
            }}
            label={
              <Box sx={{ py: 0.5, overflowWrap: 'anywhere' }}>
                <Typography variant="body2" sx={{ fontWeight: 500 }}>{option.label}</Typography>
                {option.hint && <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>{option.hint}</Typography>}
              </Box>
            }
          />
        ))}
      </RadioGroup>
    </Box>
  )
}
