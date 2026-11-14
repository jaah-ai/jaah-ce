import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import FormTextField from '../../ui/FormTextField'
import SelectField from '../../ui/SelectField'
import { withArticle, type TrackerContainer } from './api'

/* THE TRACKER-PROJECT PICKER — one component, two callers: the connection's Mapping tab (one container per row) and
 * the project page's Issue trackers section (`multiple`: every container the project links). `containers` is the
 * tracker's own list; while it is null or `error` is set (no adapter yet, a broken sign-in) the pick is a typed id
 * (comma-separated when `multiple`) with the reason under it. A saved id the list no longer names stays pickable
 * as itself, so a save never drops it silently. Names no tracker. */
export default function ContainerSelect({ containers, error, value, onChange, multiple = false, disabled = false, label = 'Tracker project', testId }: {
  containers: readonly TrackerContainer[] | null
  error?: string
  value: readonly string[]
  onChange: (ids: string[]) => void
  multiple?: boolean
  disabled?: boolean
  label?: string
  testId: string
}): JSX.Element {
  if (containers === null || error) {
    // Ids are trimmed on save, not per keystroke. Enter in the project page's picker must not submit the page's form.
    return (
      <Box onKeyDown={multiple ? e => { if (e.key === 'Enter') e.preventDefault() } : undefined}>
        <FormTextField label={label} value={value.join(',')} disabled={disabled} placeholder={multiple ? 'ENG, OPS' : 'ENG'}
          hint={error || undefined} testId={`${testId}-input`}
          onChange={text => onChange(multiple ? text.split(',') : [text])} />
      </Box>
    )
  }
  const options = [
    ...containers,
    ...value.filter(id => id && !containers.some(c => c.id === id)).map(id => ({ id, name: id })),
  ]
  if (!multiple) {
    return (
      <SelectField dense ariaLabel={label} value={value[0] ?? ''} disabled={disabled} testId={`${testId}-select`}
        options={[{ value: '', label: `Pick ${withArticle(label)}` }, ...options.map(c => ({ value: c.id, label: c.name }))]}
        onChange={id => onChange([id])} />
    )
  }
  return (
    <Box>
      <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>{label}</Typography>
      <Box role="group" aria-label={label} data-testid={`${testId}-group`}
        sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0 }}>
        {options.length === 0 && <Box component="span" sx={{ color: 'text.secondary', py: 1 }}>None to pick</Box>}
        {options.map(c => (
          <FormControlLabel
            key={c.id}
            disabled={disabled}
            sx={{ mr: 0, minWidth: 0, '& .MuiFormControlLabel-label': { overflowWrap: 'anywhere' } }}
            control={(
              <Checkbox size="small" checked={value.includes(c.id)}
                onChange={(_, on) => onChange(on ? [...value, c.id] : value.filter(id => id !== c.id))} />
            )}
            label={c.name}
          />
        ))}
      </Box>
    </Box>
  )
}
