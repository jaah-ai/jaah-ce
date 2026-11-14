import InputAdornment from '@mui/material/InputAdornment'

/* The ONE `slotProps` of a dollar-amount `TextField`: a `$` start adornment and the decimal keypad on a phone.
 * Spread or pass as `slotProps={DOLLAR_SLOTS}`; merge with a field's own slots when it has any. */
export const DOLLAR_SLOTS = {
  input: { startAdornment: <InputAdornment position="start">$</InputAdornment> },
  htmlInput: { inputMode: 'decimal' },
} as const

/* The ONE `slotProps` of a whole-percent share (0–100): a `%` end adornment and the integer bounds. */
export const PERCENT_SLOTS = {
  inputLabel: { shrink: true },
  htmlInput: { min: 0, max: 100, step: 1 },
  input: { endAdornment: <InputAdornment position="end">%</InputAdornment> },
} as const
