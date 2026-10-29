import type { JSX } from 'react'
import Box from '@mui/material/Box'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { fieldTapTargetSx } from './listStyles'

/* THE TYPED-NAME GATE of an act a mis-aimed click must not satisfy (a project's erase, an org move): the
 * entity's name typed out. One home for the field, its visible instruction (beside the field, never only in the
 * disabled button's tooltip) and what "matches" means, so the gate cannot differ between two dialogs.
 * Leading/trailing blanks are forgiven — the server's own check (`tenancy/transfer.py::execute`) strips both
 * sides too. */

/** Does `typed` name `name`? */
export function typedNameMatches(typed: string, name: string): boolean {
  return typed.trim() === name.trim()
}

export function TypedNameConfirm({
  name,
  value,
  onChange,
  action,
  testId,
  autoFocus = true,
}: {
  /** The name the field must match, shown as the placeholder. */
  name: string
  value: string
  onChange: (next: string) => void
  /** The act, for the accessible name: "Type <name> to confirm <action>". */
  action: string
  testId?: string
  autoFocus?: boolean
}): JSX.Element {
  return (
    <Box>
      <Typography variant="body2" sx={{ mb: 0.5 }} data-testid={testId && `${testId}-hint`}>
        Type <strong>{name}</strong> to confirm.
      </Typography>
      <TextField
        type="text"
        fullWidth
        autoFocus={autoFocus}
        autoComplete="off"
        placeholder={name}
        value={value}
        onChange={e => onChange(e.target.value)}
        slotProps={{ htmlInput: { spellCheck: false, 'aria-label': `Type ${name} to confirm ${action}`, 'data-testid': testId } }}
        sx={fieldTapTargetSx}
      />
    </Box>
  )
}

export default TypedNameConfirm
