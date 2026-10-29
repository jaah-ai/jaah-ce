import type { JSX } from 'react'
import type { SxProps, Theme } from '@mui/material/styles'
import Typography from '@mui/material/Typography'

/** THE "Saved." NOTE beside a form's Save button, shown while `useSubmit`'s `saved` holds. `detail` follows it: what
 *  the save changed that the user did not type. */
export default function SavedNote({ show, detail, sx }: { show: boolean; detail?: string; sx?: SxProps<Theme> }): JSX.Element | null {
  if (!show) return null
  return <Typography variant="body2" role="status" sx={[{ color: 'text.secondary' }, ...(Array.isArray(sx) ? sx : [sx])]}>{detail ? `Saved. ${detail}` : 'Saved.'}</Typography>
}
