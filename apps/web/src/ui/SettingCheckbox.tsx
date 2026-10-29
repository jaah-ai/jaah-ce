import type { ChangeEvent, JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Switch from '@mui/material/Switch'
import Typography from '@mui/material/Typography'
import HelpLabel from './HelpLabel'

/* The console's ONE boolean-setting control: `Label [x]`, its explanation a hover on the label (`help`). Label BEFORE
 * the box (`labelPlacement="start"`), keeping boxes in a readable column when the row wraps. One wrapper
 * so boolean settings cannot drift into different shapes.
 * Consumers: `components/tasks/TaskEditForm.tsx`, `pages/ProjectEditPage.tsx`, `pages/Settings.tsx`
 * (`labelHidden`: the row draws the label in its own column, so the box alone sits in the control slot,
 * still named and described for assistive tech). `control="switch"` draws a `Switch` in the box's place. */

export function SettingCheckbox({
  name,
  label,
  help,
  checked,
  disabled,
  onChange,
  indeterminate,
  labelHidden,
  id,
  testId = 'form-setting',
  dataRole = 'form-checkbox',
  control,
}: {
  name: string
  label: string
  /** A few words shown on hovering the label (`ui/HelpLabel`). */
  help?: string
  checked: boolean
  disabled: boolean
  onChange: (checked: boolean) => void
  indeterminate?: boolean
  /** Draw the box alone: the caller renders the visible label elsewhere. The accessible name stays. */
  labelHidden?: boolean
  /** Reaches the `<input>`, so a caller's `<label htmlFor>` can pair with it. */
  id?: string
  testId?: string
  dataRole?: string
  control?: 'switch'
}): JSX.Element {
  const input = {
    id,
    name,
    checked,
    disabled,
    onChange: (e: ChangeEvent<HTMLInputElement>) => onChange(e.target.checked),
    // Explicit accessible name: tests and e2e probes resolve by name, and it survives a visual trim.
    slotProps: { input: { 'aria-label': label, 'aria-description': help || undefined } },
  }
  const box = control === 'switch' ? <Switch {...input} /> : (
    <Checkbox
      {...input}
      indeterminate={indeterminate}
      // The touch-target floor, declared rather than inherited from MUI's default padding.
      sx={theme => ({ p: '0.5rem', minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx })}
    />
  )
  if (labelHidden) {
    return (
      <Box data-testid={testId} data-role={dataRole} sx={{ minWidth: 0, display: 'inline-flex' }}>
        {box}
      </Box>
    )
  }
  return (
    <Box data-testid={testId} data-role={dataRole} sx={{ minWidth: 0 }}>
      <FormControlLabel
        labelPlacement="start"
        // No `flex: 1`/space-between: keep label and box tight; the ROW distributes them (`columnGap`).
        sx={{ ml: 0, mr: 0, gap: '0.375rem' }}
        control={box}
        label={
          <Typography
            component="span"
            data-testid="form-label"
            sx={{ fontSize: '0.8125rem', fontWeight: 500, color: 'text.secondary' }}
          >
            <HelpLabel label={label} help={help} />
          </Typography>
        }
      />
    </Box>
  )
}

export default SettingCheckbox
