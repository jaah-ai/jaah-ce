import type { JSX } from 'react'
import Button from '@mui/material/Button'
import InputAdornment from '@mui/material/InputAdornment'

/* The Show/Hide toggle at the end of a secret field — one spelling for `pages/NewPasswordFields.tsx` and
 * `pages/credentials/credentialForm.tsx::RegistryFieldInput`. The caller owns `shown` and the input's
 * `type`; this only draws the button and names it. `what` is the field in sentence case ("password"). */
export function ShowHideAdornment({ shown, onToggle, what }: { shown: boolean; onToggle: () => void; what: string }): JSX.Element {
  return (
    <InputAdornment position="end">
      <Button
        size="small"
        onClick={onToggle}
        aria-label={shown ? `Hide ${what}` : `Show ${what}`}
        sx={{ minWidth: 0, textTransform: 'none' }}
      >
        {shown ? 'Hide' : 'Show'}
      </Button>
    </InputAdornment>
  )
}
