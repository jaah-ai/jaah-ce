import { type JSX, type ReactNode, useId } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Typography from '@mui/material/Typography'
import { FormDialog } from '../ui/FormDialog'
import { tapTargetSx } from '../ui/listStyles'

/* The wait for a chat app to redeem a link code (`hooks/useLinkCodeFlow`), shared by `components/SlackConnect`
 * and `components/TelegramConnect`: what to do, the button that opens the app, and a spinner saying the next
 * move is the person's. The host opens it while a code is out and drops it once the bot answers, so it closes
 * itself on success. Cancel (or Escape; a stray backdrop click does not) stops the wait here only: no route
 * revokes a code, so the bot still honours it until it expires. A status frame, not a form, so it has no
 * submit. */
export default function LinkCodeDialog({ title, instruction, openLabel, url, waiting, onCancel }: {
  title: string
  instruction: ReactNode
  /** The button that opens the app; drawn only when the API named a `url`. */
  openLabel: string
  url: string | null
  /** The spinner's label. */
  waiting: string
  onCancel: () => void
}): JSX.Element {
  const instructionId = useId()
  return (
    <FormDialog
      title={title}
      submitLabel=""
      hideSubmit
      onSubmit={() => undefined}
      onClose={onCancel}
      modal="backdrop"
      maxWidth="xs"
      testId="link-code-dialog"
      describedBy={instructionId}
    >
      <Typography id={instructionId} variant="body1" component="div">{instruction}</Typography>
      {url && (
        <Button variant="contained" href={url} target="_blank" rel="noopener" sx={[tapTargetSx, { alignSelf: 'flex-start' }]}>
          {openLabel}
        </Button>
      )}
      <Box role="status" sx={{ display: 'flex', alignItems: 'center', gap: 1.5, color: 'text.secondary' }}>
        <CircularProgress size={20} aria-hidden />
        <Typography variant="body2">{waiting}</Typography>
      </Box>
    </FormDialog>
  )
}

/** The code as the person types it: bold monospace, selected whole on click. */
export function LinkCode({ code }: { code: string }): JSX.Element {
  return (
    <Box component="code" sx={{ fontFamily: 'monospace', fontWeight: 600, fontSize: '1.05rem', letterSpacing: '0.08em', userSelect: 'all' }}>
      {code}
    </Box>
  )
}
