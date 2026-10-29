import { useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { CopyButton } from './CopyButton'
import CopyableValue from './CopyableValue'

/* One authorization URL offered three ways: a button, `user-select: all` text, and a Copy button. A
 * popup blocker can swallow `target=_blank`, and the Clipboard API is refused in insecure contexts, so no
 * single exit works everywhere. The text and Copy exits are `ui/CopyableValue`, which
 * holds their reasoning. `inline` shows only the button and a Copy verb
 * (`pages/credentials/AccountEditPage.tsx`'s sign-in). The text appears once a copy is refused, the one exit
 * left where neither the tab nor the clipboard works. `url` has already passed `lib/safeHref`; callers
 * decide once, so this does not re-validate. */
export function AuthorizeLink({
  url,
  label = 'Click to authorize ↗',
  inline = false,
}: {
  /** An `http(s)` URL that has already passed `safeHref`. */
  url: string
  /** The button's verb; a caller that knows the vendor names it. */
  label?: string
  /** The button and a "Copy link" verb on one row, without the URL text. */
  inline?: boolean
}): JSX.Element {
  const [copyRefused, setCopyRefused] = useState(false)
  const button = (
    <Button
      variant="contained"
      color="primary"
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      sx={{ alignSelf: 'flex-start' }}
    >
      {label}
    </Button>
  )
  if (inline && !copyRefused) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1.5 }}>
        {button}
        <CopyButton variant="text" text={() => url} label="Copy link" copiedLabel="Copied" failedLabel="Copy link" flashMs={2000}
          onFailed={() => setCopyRefused(true)} />
      </Box>
    )
  }
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, mb: 1 }}>
      {button}
      {/* `1 1 12rem` and `break-all` are this caller's: an authorize URL is a long unbreakable token. */}
      <CopyableValue
        value={url}
        copyLabel="Copy link"
        sx={{ flex: '1 1 12rem', fontSize: '0.75rem', wordBreak: 'break-all' }}
      />
    </Box>
  )
}

export default AuthorizeLink
