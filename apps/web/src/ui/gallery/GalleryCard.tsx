import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useState, type JSX, type ReactNode } from 'react'
import { GALLERY_NARROW, PHONE } from '../listStyles'
import { FONT_WEIGHT, RADIUS, SIZE, grid } from '../../theme'
import Tip from '../Tip'

/* ONE GALLERY ITEM, AS A ROW — a small thumbnail, the name, a one-line summary, a meta line and the item's actions,
 * on one line (the Gallery archetype, the style guide's; drawn inside `ui/gallery/CardGrid` with `rows`).
 *  - THUMBNAIL: `object-fit: cover` in a fixed-height box of `mediaRatio` (square for a vendor mark, 16:9 for a
 *    picture), so nothing stretches; no image, or one that fails to load, draws the caller's `placeholder` in the
 *    same box — never a broken-image glyph.
 *  - NAME and SUMMARY: one line each, ellipsised, the full text on hover (`title`). In a narrow gallery (a phone, a
 *    narrow pane: `GALLERY_NARROW`) they stack beside the thumbnail, and the meta and actions share the line below.
 *  - ACTIONS: every button and icon button in `actions` and `aside` is at least 44px tall on a phone. */
export const GALLERY_TAP_PX = SIZE.tapTarget

/** The thumbnail's height: one line of a compact row. */
export const GALLERY_THUMB_PX = SIZE.thumb

/** A wide row's columns: thumbnail, name, summary, meta, aside, actions. `ui/gallery/CardGrid` with `rows` lays its
 *  rows on these as one subgrid, so each column is as wide as its widest cell (capped) and the summary takes the
 *  rest. The name is served first: whole up to half the row, so the summary is the one cut short. */
export const GALLERY_ROW_COLUMNS = `auto fit-content(50%) minmax(0, 1fr) fit-content(${grid(42.5)}) auto auto`

/** A narrow row's columns: thumbnail, the name over the summary, aside. */
export const GALLERY_NARROW_COLUMNS = 'auto minmax(0, 1fr) auto'

export default function GalleryCard({
  name,
  summary,
  image,
  placeholder,
  meta,
  aside,
  actions,
  testId,
  attrs,
  mediaRatio = '16 / 9',
}: {
  name: string
  summary: string
  /** The image's URL; null draws `placeholder`. */
  image: string | null
  placeholder: ReactNode
  /** Beside the summary (e.g. "Coder · Official"). */
  meta?: ReactNode
  /** Beside the actions (e.g. a favorite toggle). */
  aside?: ReactNode
  actions?: ReactNode
  testId?: string
  /** The row's own attributes beyond `testId` (a `data-*` key, `aria-busy`), for a page that marks its rows. */
  attrs?: Record<string, string | boolean | undefined>
  /** The thumbnail's CSS `aspect-ratio`. */
  mediaRatio?: string
}): JSX.Element {
  // The URL that failed, not a flag: a new `image` gets its own try.
  const [failed, setFailed] = useState<string | null>(null)
  const showImage = image !== null && failed !== image
  return (
    <Box
      {...attrs}
      data-testid={testId}
      sx={{
        display: 'grid',
        gridTemplateColumns: GALLERY_ROW_COLUMNS,
        gridTemplateAreas: '"media name summary meta aside actions"',
        alignItems: 'center',
        columnGap: 1.5,
        width: '100%',
        minWidth: 0,
        px: 1.5,
        py: 0.5,
        '&:hover': { bgcolor: 'action.hover' },
        [GALLERY_NARROW]: {
          gridTemplateColumns: GALLERY_NARROW_COLUMNS,
          gridTemplateAreas: '"media name aside" "media summary aside" "foot foot foot"',
          rowGap: 0,
          py: 1,
        },
        ...TAP_SX,
      }}
    >
      <Box
        data-testid={testId && `${testId}-media`}
        sx={{
          gridArea: 'media',
          height: GALLERY_THUMB_PX,
          aspectRatio: mediaRatio,
          overflow: 'hidden',
          borderRadius: RADIUS.sm,
          flex: '0 0 auto',
        }}
      >
        {showImage ? (
          <Box
            component="img"
            src={image}
            /* Empty: the image illustrates the name beside it. */
            alt=""
            loading="lazy"
            onError={() => setFailed(image)}
            sx={{ display: 'block', width: '100%', height: '100%', objectFit: 'cover' }}
          />
        ) : placeholder}
      </Box>
      <Tip title={name}><Typography
        component="h3"
        variant="body2"
        noWrap
        data-testid={testId && `${testId}-name`}
        sx={{ gridArea: 'name', fontWeight: FONT_WEIGHT.semibold }}
      >
        {name}
      </Typography></Tip>
      <Tip title={summary}><Typography
        variant="body2"
        noWrap
        data-testid={testId && `${testId}-summary`}
        sx={{ gridArea: 'summary', color: 'text.secondary' }}
      >
        {summary}
      </Typography></Tip>
      {aside && <Box sx={{ gridArea: 'aside', display: 'flex', alignItems: 'center' }}>{aside}</Box>}
      {/* Meta and verbs: two grid cells in a wide row, one shared line under the text in a narrow one. */}
      <Box sx={{ display: 'contents', [GALLERY_NARROW]: { gridArea: 'foot', display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 } }}>
        {meta && (
          <Typography variant="caption" component="div" noWrap sx={{ gridArea: 'meta', color: 'text.secondary', minWidth: 0, [GALLERY_NARROW]: { flex: '1 1 auto' } }}>
            {meta}
          </Typography>
        )}
        {actions && (
          <Box sx={{ gridArea: 'actions', display: 'flex', alignItems: 'center', gap: 0.5, justifyContent: 'flex-end', [GALLERY_NARROW]: { ml: 'auto', flex: '0 1 auto', flexWrap: 'wrap' } }}>
            {actions}
          </Box>
        )}
      </Box>
    </Box>
  )
}

/* Compact verbs on a wide screen; the 44px tap floor on a phone. */
const TAP_SX = {
  '& .MuiButton-root': { py: 0, [PHONE]: { minHeight: GALLERY_TAP_PX } },
  '& .MuiIconButton-root': { p: 0.5, [PHONE]: { minHeight: GALLERY_TAP_PX, minWidth: GALLERY_TAP_PX } },
} as const
