import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import type { ReactNode } from 'react'
import { ChoiceField } from '../../ui/ChoiceField'
import { PHONE } from '../../ui/listStyles'
import type { CredentialOwnership } from './api'
import { OWNERSHIP_OPTIONS, ownershipOf } from './ownership'

/* `oneLine`: the choices stay in a row on a phone once the gap between them is cut; without it `ui/ChoiceField`
 * stacks them there. Its one user is the custom connections tab (`custom/ConnectionsTab`): Personal and
 * Organization in a row above the inputs, where a stack would push the Connect row below the fold. */
const PHONE_ROW_SX = { '& .MuiRadioGroup-root': { [PHONE]: { flexDirection: 'row', flexWrap: 'nowrap', columnGap: 1 } } } as const

/* The Owner choice the credential form (`CredentialEditPage`), the custom connections tab (`custom/ConnectionsTab`)
 * and the account editor (`AccountEditPage`, `options` without System: an account has no system owner) show.
 * Radios, not a select: every choice visible (`ui/ChoiceField`). Each explains itself on hover.
 * `personalOwner` (the member picker, `OwnerMemberLookup`) is drawn under the radios while Personal is chosen,
 * `orgHome` (the Home picker, `credentialForm.tsx::HomeProjectField`) while Organization is. */
export default function OwnershipRadios({ value, onChange, disabled, sx, personalOwner, orgHome, oneLine = false, options }: {
  /** Unused: `ui/ChoiceField` names its own legend. Kept so callers need no change. */
  labelId: string
  value: CredentialOwnership
  onChange: (next: CredentialOwnership) => void
  disabled?: boolean
  sx?: SxProps<Theme>
  personalOwner?: ReactNode
  orgHome?: ReactNode
  /** Keep the choices in a row on a phone too (a form where a stack would push its submit row below the fold). */
  oneLine?: boolean
  /** The owners offered, in `OWNERSHIP_OPTIONS` order; all three when omitted. */
  options?: readonly CredentialOwnership[]
}) {
  return (
    <Box sx={[{ display: 'flex', flexDirection: 'column' }, oneLine && PHONE_ROW_SX, ...(Array.isArray(sx) ? sx : [sx])]}>
      <ChoiceField
        label="Owner"
        help={{
          tip: 'Decides who, and which tasks, can use it.',
          docs: 'using-jaah/credentials/how-credentials-work',
        }}
        row
        disabled={disabled}
        value={value}
        onChange={next => onChange(ownershipOf(next))}
        options={OWNERSHIP_OPTIONS.filter(option => !options || options.includes(option.value))}
      />
      {value === 'personal' && personalOwner}
      {value === 'org' && orgHome}
    </Box>
  )
}
