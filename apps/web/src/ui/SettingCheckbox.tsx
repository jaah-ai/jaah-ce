import type { ChangeEvent, JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import HelpLabel, { ExplainMoreButton } from './HelpLabel'
import { hasMore, toExplain, type HelpProp } from './explain'
import { FONT_SIZE, FONT_WEIGHT } from '../theme'
import { PHONE } from './listStyles'

/* The console's ONE boolean-setting control: `Label [x]`, its explanation a hover on the label (`help`). Label BEFORE
 * the box (`labelPlacement="start"`), keeping boxes in a readable column when the row wraps. One wrapper
 * so boolean settings cannot drift into different shapes.
 * Every boolean setting sits on it (grep the import), bar a box that leads an inline sentence or sits under a
 * field as its (default). `labelHidden`: the row draws the label in
 * its own column, so the box alone sits in the control slot, still named and described for assistive tech.
 * A checkbox, never a switch: one look for every on/off setting. `dense`: the 44px tap floor at phone width only, a
 * tight box on desktop, for a table or menu row and a setting whose row must not grow.
 * `labelPlacement="end"` for a dialog option whose box leads its label. */

export function SettingCheckbox({
  name,
  label,
  help,
  checked,
  disabled = false,
  onChange,
  indeterminate,
  labelHidden,
  id,
  describedBy,
  testId = 'form-setting',
  dataRole = 'form-checkbox',
  labelPlacement = 'start',
  dense = false,
}: {
  name: string
  label: string
  /** The label's explanation (`ui/HelpLabel`): a tooltip, and a pop-up when it says more. */
  help?: HelpProp
  checked: boolean
  disabled?: boolean
  onChange: (checked: boolean) => void
  indeterminate?: boolean
  /** Draw the box alone: the caller renders the visible label elsewhere. The accessible name stays. */
  labelHidden?: boolean
  /** Reaches the `<input>`, so a caller's `<label htmlFor>` can pair with it. */
  id?: string
  /** The `id` of a visible note the input is described by (why it is disabled, what it seals): `aria-describedby`. */
  describedBy?: string
  testId?: string
  dataRole?: string
  /** `'end'` puts the box first: a dialog's option or an inline sentence, where the setting shape would read oddly. */
  labelPlacement?: 'start' | 'end'
  /** The tap floor at phone width only (`ui/listStyles` `PHONE`); a tight box on desktop. */
  dense?: boolean
}): JSX.Element {
  const explain = toExplain(help)
  const tip = explain?.tip
  /* The tip lines up with the label's start, never centred on it: a short label at a form's left edge would push a
     centred tip past the form and off a phone's edge. Above when it carries "More" (`ui/HelpLabel`). */
  const tipPlacement = hasMore(explain) ? 'top-start' : 'bottom-start'
  const input = {
    id,
    name,
    checked,
    disabled,
    onChange: (e: ChangeEvent<HTMLInputElement>) => onChange(e.target.checked),
    // Explicit accessible name: tests and e2e probes resolve by name, and it survives a visual trim.
    slotProps: { input: { 'aria-label': label, 'aria-description': tip || undefined, 'aria-describedby': describedBy } },
  }
  const box = (
    <Checkbox
      {...input}
      indeterminate={indeterminate}
      // The touch-target floor, declared rather than inherited from MUI's default padding.
      sx={theme => dense
        ? { p: 0, [PHONE]: { p: 1, minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx } }
        : { p: 1, minWidth: theme.custom.tapTargetPx, minHeight: theme.custom.tapTargetPx }}
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
    <Box data-testid={testId} data-role={dataRole} sx={{ minWidth: 0, position: 'relative' }}>
      <FormControlLabel
        labelPlacement={labelPlacement}
        // No `flex: 1`/space-between: keep label and box tight; the ROW distributes them (`columnGap`).
        sx={{ ml: 0, mr: 0, gap: 1 }}
        control={box}
        label={
          <Typography
            component="span"
            data-testid="form-label"
            sx={{ fontSize: FONT_SIZE.md, fontWeight: FONT_WEIGHT.medium, color: 'text.secondary' }}
          >
            <HelpLabel label={label} help={help} placement={tipPlacement} />
          </Typography>
        }
      />
      <ExplainMoreButton label={label} help={help} />
    </Box>
  )
}

export default SettingCheckbox
