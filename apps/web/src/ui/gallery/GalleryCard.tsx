import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import Typography from '@mui/material/Typography'
import { useState, type JSX, type ReactNode } from 'react'

/* ONE GALLERY CARD — a 16:9 image over a name, a one-line summary, a meta line and the card's actions (the Gallery
 * archetype, the style guide's; the template card's anatomy is the Task Templates PRD §7).
 *  - IMAGE: `object-fit: cover` in a fixed 16:9 box, so nothing stretches; no image, or one that fails to load,
 *    draws the caller's `placeholder` in the same box — never a broken-image glyph.
 *  - NAME: at most two lines, wrapped at word boundaries (never mid-word), the full name on hover (`title`).
 *  - SUMMARY: one line, ellipsised, the full text on hover.
 *  - ACTIONS: every button and icon button in `actions` and `aside` is at least 44px tall at every width. */
export const GALLERY_TAP_PX = 44

export default function GalleryCard({
  name,
  summary,
  image,
  imageAlt = '',
  placeholder,
  meta,
  aside,
  actions,
  testId,
}: {
  name: string
  summary: string
  /** The image's URL; null draws `placeholder`. */
  image: string | null
  /** Empty by default: the image illustrates the name beside it. */
  imageAlt?: string
  placeholder: ReactNode
  /** The line under the summary (e.g. "Coder · Official"). */
  meta?: ReactNode
  /** Beside the name (e.g. a favorite toggle). */
  aside?: ReactNode
  actions?: ReactNode
  testId?: string
}): JSX.Element {
  // The URL that failed, not a flag: a new `image` gets its own try.
  const [failed, setFailed] = useState<string | null>(null)
  const showImage = image !== null && failed !== image
  return (
    <Card
      variant="outlined"
      data-testid={testId}
      sx={{ display: 'flex', flexDirection: 'column', width: '100%', minWidth: 0 }}
    >
      <Box
        data-testid={testId && `${testId}-media`}
        sx={{ aspectRatio: '16 / 9', overflow: 'hidden', bgcolor: 'action.hover', flex: '0 0 auto' }}
      >
        {showImage ? (
          <Box
            component="img"
            src={image}
            alt={imageAlt}
            loading="lazy"
            onError={() => setFailed(image)}
            sx={{ display: 'block', width: '100%', height: '100%', objectFit: 'cover' }}
          />
        ) : placeholder}
      </Box>
      <Box sx={{ display: 'flex', flexDirection: 'column', flex: '1 1 auto', gap: 0.5, p: 2, pt: 1.5, minWidth: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 0 }}>
          <Typography
            component="h3"
            variant="subtitle1"
            title={name}
            data-testid={testId && `${testId}-name`}
            sx={{
              flex: '1 1 auto',
              minWidth: 0,
              fontWeight: 600,
              lineHeight: 1.3,
              // Two lines reserved, so a one-line name keeps its row of cards level.
              minHeight: '2.6em',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
              overflowWrap: 'normal',
              wordBreak: 'normal',
              hyphens: 'none',
              alignSelf: 'center',
            }}
          >
            {name}
          </Typography>
          {aside && <Box sx={{ flex: '0 0 auto', mt: -1, mr: -1, ...TAP_SX }}>{aside}</Box>}
        </Box>
        <Typography
          variant="body2"
          noWrap
          title={summary}
          data-testid={testId && `${testId}-summary`}
          sx={{ color: 'text.secondary' }}
        >
          {summary}
        </Typography>
        {meta && (
          <Typography variant="caption" component="div" noWrap sx={{ color: 'text.secondary' }}>
            {meta}
          </Typography>
        )}
        {actions && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 'auto', pt: 1, ...TAP_SX }}>
            {actions}
          </Box>
        )}
      </Box>
    </Card>
  )
}

const TAP_SX = {
  '& .MuiButton-root': { minHeight: GALLERY_TAP_PX },
  '& .MuiIconButton-root': { minHeight: GALLERY_TAP_PX, minWidth: GALLERY_TAP_PX },
} as const
