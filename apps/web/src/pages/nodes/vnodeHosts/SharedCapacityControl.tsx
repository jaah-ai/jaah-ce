import { useEffect, useState, type FormEvent, type JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { useSubmit } from '../../../hooks/useSubmit'
import type { VnodeHost } from '../../../types'
import BusyButton from '../../../ui/BusyButton'
import { fieldTapTargetSx, tapTargetSx as tapHeightSx } from '../../../ui/listStyles'
import { setSharedCapacity } from './api'

/* An operator host's place in the Shared hosting pool, drawn as its own section of the host's page
 * (`pages/nodes/hosts/HostDetailPage.tsx`). The controls ARE the current state; Save appears only once the draft differs
 * from the server's value. No optimism: the row shows what the server answered, after the host list
 * refetches. Wraps, so it fits a 390px phone. */

const DEFAULT_CAPACITY = 10
const MIN = 1
const MAX = 99

const parseCount = (raw: string): number | null => {
  const n = Number(raw.trim())
  return raw.trim() !== '' && Number.isInteger(n) && n >= MIN && n <= MAX ? n : null
}

/* The shared height floor plus a width floor: a checkbox and a short Save are narrower than they are tall. */
const tapTargetSx = (theme: Theme) => ({
  ...tapHeightSx(theme),
  [theme.breakpoints.down('sm')]: { minWidth: theme.custom.tapTargetPx },
})

/* The box's ink flush with the section's left edge: a negative margin of the padding around the 20px `small` icon
   (4px a side, or half the slack of the phone's tap-target width). The hit area stays whole. */
const checkboxFlushSx = (theme: Theme) => ({
  ml: -0.5,
  [theme.breakpoints.down('sm')]: { ml: `${(20 - theme.custom.tapTargetPx) / 2}px` },
})

/* The box's ink the section rhythm's ~12px under its heading, as `checkboxFlushSx` puts it at the left edge: the
   phone's tap target centres the 20px icon in 44px, so the form rises by half that slack and the hit area overlaps
   only the gap above it. */
const formFlushSx = (theme: Theme) => ({
  [theme.breakpoints.down('sm')]: { mt: `${(20 - theme.custom.tapTargetPx) / 2}px` },
})

export function SharedCapacityControl({
  host,
  onSaved,
}: {
  host: VnodeHost
  onSaved: () => Promise<unknown>
}): JSX.Element {
  const saved = host.shared_capacity ?? null
  const [on, setOn] = useState(saved !== null)
  const [count, setCount] = useState(String(saved ?? DEFAULT_CAPACITY))
  const { busy: saving, formError: error, setFormError: setError, run } = useSubmit()

  // A refetch that changes the server's value resets the draft to it.
  useEffect(() => {
    setOn(saved !== null)
    setCount(String(saved ?? DEFAULT_CAPACITY))
  }, [saved])

  const parsed = parseCount(count)
  const invalid = on && parsed === null
  const dirty = on !== (saved !== null) || (on && parsed !== saved)

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!dirty || invalid || saving) return
    await run('Could not save. Try again.', async () => {
      await setSharedCapacity(host.host_node_id, on ? parsed : null)
      await onSaved()
    })
  }

  return (
    <Box
      component="form"
      noValidate
      onSubmit={submit}
      data-testid="shared-capacity"
      aria-busy={saving || undefined}
      // The feed title is one ellipsised line; this region wraps instead. `inline-block` stops the row's
      // hover underline (`ui/feed/FeedList.tsx`) reaching these labels: a decoration does not propagate into
      // an inline-block, and a form is not a link. Body2, as the sections above it read.
      sx={[{ whiteSpace: 'normal', display: 'inline-block', width: '100%', typography: 'body2' }, formFlushSx]}
    >
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
        <FormControlLabel
          sx={{ ml: 0, mr: 0 }}
          disabled={saving}
          control={
            <Checkbox
              size="small"
              checked={on}
              onChange={e => {
                setOn(e.target.checked)
                setError('')
                if (e.target.checked && parseCount(count) === null) setCount(String(DEFAULT_CAPACITY))
              }}
              slotProps={{ input: { 'data-testid': 'shared-capacity-toggle' } as Record<string, string> }}
              sx={[{ p: 0.5 }, tapTargetSx, checkboxFlushSx]}
            />
          }
          label={<Typography component="span" sx={{ fontSize: 'inherit' }}>Offer for shared hosting</Typography>}
        />
        {on && (
          <Box
            component="span"
            sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.75, color: 'text.secondary' }}
          >
            up to
            <TextField
              size="small"
              value={count}
              disabled={saving}
              error={invalid}
              onChange={e => { setCount(e.target.value); setError('') }}
              // `numeric` raises a digit keypad; not `type="number"`, whose wheel nudges change the value.
              slotProps={{
                htmlInput: {
                  inputMode: 'numeric',
                  maxLength: 2,
                  'aria-label': 'Max customer nodes at once',
                  'aria-invalid': invalid || undefined,
                  'data-testid': 'shared-capacity-count',
                },
              }}
              sx={[
                { width: '3.5rem', '& .MuiInputBase-input': { py: 0.375, textAlign: 'center', fontVariantNumeric: 'tabular-nums' } },
                fieldTapTargetSx,
              ]}
            />
            {/* N counts Shared hosting's customer nodes on this host at once: placed ones plus reserved orders. */}
            customer nodes at once
          </Box>
        )}
        {dirty && (
          <BusyButton
            type="submit"
            size="small"
            variant="outlined"
            busy={saving}
            disabled={invalid}
            data-testid="shared-capacity-save"
            sx={[{ minWidth: 0, py: 0.125 }, tapTargetSx]}
          >
            Save
          </BusyButton>
        )}
      </Box>
      {invalid ? (
        <Typography variant="caption" component="p" role="alert" color="error" data-testid="shared-capacity-error">
          Enter 1–99.
        </Typography>
      ) : error ? (
        <Typography variant="caption" component="p" role="alert" color="error" data-testid="shared-capacity-error">
          {error}
        </Typography>
      ) : null}
    </Box>
  )
}
