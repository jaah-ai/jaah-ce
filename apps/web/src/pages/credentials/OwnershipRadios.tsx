import FormControl from '@mui/material/FormControl'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormLabel from '@mui/material/FormLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import type { SxProps, Theme } from '@mui/material/styles'
import type { ReactNode } from 'react'
import HelpLabel from '../../ui/HelpLabel'
import { PHONE } from '../../ui/listStyles'
import type { CredentialOwnership } from './api'
import { OWNERSHIP_OPTIONS, ownershipOf } from './ownership'

/* A phone stacks the three: in a row at 360px System wrapped onto a line of its own, and with the gaps cut to
 * fit, each label ran into the next radio. */
const PHONE_COLUMN_SX = { [PHONE]: { flexDirection: 'column' } } as const
/* `oneLine`: a narrower dialog's 448px still holds the three in a row once MUI's 16px gap after each is cut. */
const PHONE_ROW_SX = { [PHONE]: { flexWrap: 'nowrap', '& .MuiFormControlLabel-root': { mr: 1 } } } as const

/* The Owner choice both credential forms show (`CredentialEditPage`, `settings/cloudConnections/
 * AwsCredentialFields`). Radios, not a select: three choices, all visible. Each explains itself on hover.
 * `personalOwner` (the member picker, `OwnerMemberLookup`) is drawn under the radios while Personal is chosen,
 * `orgHome` (the Home picker, `credentialForm.tsx::HomeProjectField`) while Organization is. */
export default function OwnershipRadios({ labelId, value, onChange, disabled, sx, personalOwner, orgHome, oneLine = false }: {
  labelId: string
  value: CredentialOwnership
  onChange: (next: CredentialOwnership) => void
  disabled?: boolean
  sx?: SxProps<Theme>
  personalOwner?: ReactNode
  orgHome?: ReactNode
  /** Keep the three in a row on a phone too (a form where a stack would push its submit row below the fold). */
  oneLine?: boolean
}) {
  return (
    <FormControl sx={sx}>
      <FormLabel id={labelId}>Owner</FormLabel>
      <RadioGroup row aria-labelledby={labelId} sx={oneLine ? PHONE_ROW_SX : PHONE_COLUMN_SX} value={value} onChange={event => onChange(ownershipOf(event.target.value))}>
        {OWNERSHIP_OPTIONS.map(option => (
          <FormControlLabel
            key={option.value}
            value={option.value}
            aria-description={option.help}
            control={<Radio size="small" disabled={disabled} />}
            label={<HelpLabel label={option.label} help={option.help} />}
          />
        ))}
      </RadioGroup>
      {value === 'personal' && personalOwner}
      {value === 'org' && orgHome}
    </FormControl>
  )
}
