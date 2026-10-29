import Box from '@mui/material/Box'
import TextField from '@mui/material/TextField'
import type { JSX } from 'react'

import { fieldTapTargetSx } from './listStyles'

/* The basis holds a full `mm/dd/yyyy, --:-- --` and its picker glyph. Growing below `md`: a pair too wide for one
 * line stacks as a full-width column under the search rather than two stubs at its left. Capped from `md` up, where
 * the pair sits beside the row's other controls. */
const FIELD_PX = 196
const FIELD_SX = { m: 0, flex: `1 1 ${FIELD_PX}px`, minWidth: { xs: 0, md: FIELD_PX }, maxWidth: { md: 240 } } as const

const UNPARSEABLE_HELP = 'Not a date and time — this bound is not applied'

type Props = {
  from: string
  to: string
  onFrom: (value: string) => void
  onTo: (value: string) => void
  fromUnparseable: boolean
  toUnparseable: boolean
}

/* The From/To `datetime-local` pair of a filter row, as ONE item of it: the pair wraps together and shares its
 * line, so a narrow row never strands To on a line of its own under From (Audit at 640px). The caller
 * owns the zone contract (`lib/zonedTime.localInputToIso`); the pair only reports the typed value.
 * An unreadable bound says so on the field: `url` drops a key it cannot parse, which would otherwise show
 * a filled-in range over a result set that was never narrowed. */
export function DateTimeBoundFields({ from, to, onFrom, onTo, fromUnparseable, toUnparseable }: Props): JSX.Element {
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, flex: { xs: `1 1 ${2 * FIELD_PX}px`, md: '0 1 auto' }, minWidth: 0 }}>
      <TextField
        label="From"
        type="datetime-local"
        value={from}
        onChange={event => onFrom(event.target.value)}
        error={fromUnparseable}
        helperText={fromUnparseable ? UNPARSEABLE_HELP : undefined}
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { 'aria-label': 'From date and time' } }}
        sx={[FIELD_SX, fieldTapTargetSx]}
      />
      <TextField
        label="To"
        type="datetime-local"
        value={to}
        onChange={event => onTo(event.target.value)}
        error={toUnparseable}
        helperText={toUnparseable ? UNPARSEABLE_HELP : undefined}
        slotProps={{ inputLabel: { shrink: true }, htmlInput: { 'aria-label': 'To date and time' } }}
        sx={[FIELD_SX, fieldTapTargetSx]}
      />
    </Box>
  )
}
