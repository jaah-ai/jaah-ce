import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import TextField, { type TextFieldProps } from '@mui/material/TextField'
import type { SxProps, Theme } from '@mui/material/styles'

/* The org tabs' inline override edit (Settings, an org's own cron minimum): the draft field, Save/Cancel
 * while it is open, and Edit/Reset when it is not. */

/** A small autofocused field: Enter saves when `canSave`, Escape cancels. */
export function OverrideField({ canSave, onSave, onCancel, ...props }: TextFieldProps & {
  canSave: boolean
  onSave: () => void
  onCancel: () => void
}) {
  return (
    <TextField
      size="small"
      autoFocus
      onKeyDown={e => {
        if (e.key === 'Enter' && canSave) onSave()
        if (e.key === 'Escape') onCancel()
      }}
      {...props}
    />
  )
}

export function SaveCancel({ busy, canSave, onSave, onCancel, sx }: {
  busy: boolean
  canSave: boolean
  onSave: () => void
  onCancel: () => void
  sx?: SxProps<Theme>
}) {
  return (
    <Stack direction="row" spacing={1} sx={sx}>
      <Button size="small" variant="contained" disabled={busy || !canSave} onClick={() => canSave && onSave()}>Save</Button>
      <Button size="small" disabled={busy} onClick={onCancel}>Cancel</Button>
    </Stack>
  )
}

/** A table row's actions cell: Save/Cancel while its edit is open, else Edit (when `onEdit`) and, for an
 *  override, Reset. */
export function OverrideActions({ label, editing, busy, canSave, onSave, onCancel, onEdit, overridden, onReset, resetTitle }: {
  label: string
  editing: boolean
  busy: boolean
  canSave: boolean
  onSave: () => void
  onCancel: () => void
  onEdit?: () => void
  overridden: boolean
  onReset: () => void
  resetTitle?: string
}) {
  if (editing) return <SaveCancel busy={busy} canSave={canSave} onSave={onSave} onCancel={onCancel} sx={{ justifyContent: 'flex-end' }} />
  return (
    <Stack direction="row" spacing={1} sx={{ justifyContent: 'flex-end' }}>
      {onEdit && <Button size="small" disabled={busy} aria-label={`Edit ${label}`} onClick={onEdit}>Edit</Button>}
      {overridden && (
        <Button size="small" disabled={busy} aria-label={`Reset ${label}`} title={resetTitle} onClick={onReset}>Reset</Button>
      )}
    </Stack>
  )
}
