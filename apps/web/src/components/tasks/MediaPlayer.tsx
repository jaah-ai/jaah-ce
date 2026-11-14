import type { JSX } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import Tip from '../../ui/Tip'

/* ── AUDIO AND VIDEO, PLAYED BY THE BROWSER ITSELF ──
 * The Artifacts tab's and the attachment viewer's one player: the browser's own `<audio>`/`<video>` with its
 * native controls, no plugin and no autoplay. `src` is the content route, which answers a media name with a signed
 * `inline` storage URL (`app/routes/attachments.py::media_redirect`), so a file of any size streams and seeks by
 * Range. `preload="metadata"` reads the duration (and a video's first frame), never the whole file. `onError` is the
 * host's fallback when the browser cannot play the file (a codec it lacks).
 */
export function MediaPlayer({
  kind,
  src,
  name,
  testId,
  onError,
  sx,
}: {
  kind: 'audio' | 'video'
  src: string
  name: string
  testId: string
  onError?: () => void
  sx?: SxProps<Theme>
}): JSX.Element {
  return (
    <Tip title={name}><Box
      component={kind}
      src={src}
      controls
      preload="metadata"
      playsInline={kind === 'video' ? true : undefined}
      aria-label={name}
      data-testid={testId}
      onError={onError}
      sx={sx}
    /></Tip>
  )
}
