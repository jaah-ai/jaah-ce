import { useRef, useState } from 'react'
import type { JSX, KeyboardEvent, ReactNode, SyntheticEvent } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { getText, useApiRewriterVersion } from '../../api'
import { ApiFrame, ApiImg, apiDownloadProps, apiLinkHref } from '../../net/href'
import { useApiRead } from '../../hooks/useApiRead'
import { formatBytes } from '../../lib/formatBytes'
import { CodeBlock } from '../../ui/CodeBlock'
import { DownloadGlyph } from '../../ui/DownloadGlyph'
import { DialogFrame } from '../../ui/FormDialog'
import { ChevronLeftIcon, ChevronRightIcon, CloseIcon, MinusIcon, PlusIcon } from '../../ui/icons'
import { LoadingState } from '../../ui/LoadingState'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import { alpha, type Theme } from '@mui/material/styles'
import { FONT_WEIGHT, PAPER_GROUND, RADIUS, SIZE, grid } from '../../theme'
import type { AttachmentPreviewMode } from './attachmentPreview'
import { browserShowsPdf, PDF_DOWNLOADS_ONLY_NOTE } from './artifactKinds'
import { MediaPlayer } from './MediaPlayer'
import Tip from '../../ui/Tip'

/* ── THE FULL-SCREEN ATTACHMENT VIEWER ──
 * Slack's shape: the file fills the screen on a dark ground, a bar names it and carries Download and close, and
 * arrows (or ←/→) step through the host's previewable files. Images zoom (the bar's −/+, a click toggles fit and
 * actual size, `-`/`+`/`0` keys); audio and video play in the browser's own player. Only files `attachmentPreviewMode` names are ever items: the rest download from
 * their chip. The bytes come from `?preview=1` (`app/routes/attachments.py::attachment_preview`), which serves
 * them inline under the rendering CSP.
 */

export type ViewerItem = {
  id: number
  name: string
  size: number
  mode: AttachmentPreviewMode
  /** The `?preview=1` path, unrewritten: `getText` rewrites it itself, `net/href.tsx` for a `src`. */
  previewPath: string
  /** The download's API path (`downloadLinkProps`). */
  downloadPath: string
}

const ZOOM_STEP = 1.5
const MIN_ZOOM = 0.1
const MAX_ZOOM = 8

/* Dark and OPAQUE in both themes, as every photo viewer is: the file is the subject, not the console. The theme
   paints Paper through `PAPER_GROUND`, so the variable is set too. */
const BAR_BUTTON_SX = { color: 'inherit', [PHONE]: { minWidth: SIZE.tapTarget, minHeight: SIZE.tapTarget } } as const

const STEP_SX = {
  position: 'absolute',
  top: '50%',
  transform: 'translateY(-50%)',
  zIndex: 1,
  width: grid(8),
  height: grid(8),
  color: 'common.white',
  backgroundColor: (t: Theme) => alpha(t.palette.common.white, 0.12),
  '&:hover': { backgroundColor: (t: Theme) => alpha(t.palette.common.white, 0.24) },
  [PHONE]: { width: grid(6.5), height: grid(6.5) },
} as const

export default function AttachmentViewer({
  items,
  openId,
  onOpenIdChange,
}: {
  items: ViewerItem[]
  /** The shown file's id; `null` (or an id no longer listed — the file was removed) keeps the viewer closed. */
  openId: number | null
  onOpenIdChange: (id: number | null) => void
}): JSX.Element {
  // Its `src`s are rewritten on the live host (`net/href.tsx`).
  useApiRewriterVersion()
  const index = items.findIndex(i => i.id === openId)
  const item = index >= 0 ? items[index] : null
  /* `null` = fit to the screen; a number = that many image pixels per CSS pixel. Held WITH its file's id, so any
     other file (a step, a reopen, the open one removed) starts at fit without a reset to forget. */
  const [zoomed, setZoomed] = useState<{ id: number; zoom: number } | null>(null)
  const zoom = zoomed !== null && zoomed.id === item?.id ? zoomed.zoom : null
  const setZoom = (next: number | null) => setZoomed(next === null || item === null ? null : { id: item.id, zoom: next })
  // The image's own width once decoded (state, not a ref read in render); until then a zoom keeps the fitted layout.
  const [natural, setNatural] = useState<{ id: number; width: number } | null>(null)
  const naturalWidth = natural !== null && natural.id === item?.id ? natural.width : 0
  const imgRef = useRef<HTMLImageElement | null>(null)

  const show = (next: number) => {
    if (next >= 0 && next < items.length) onOpenIdChange(items[next].id)
  }
  const close = () => onOpenIdChange(null)
  // The scale now on screen, so a step from "fit" starts where the eye is.
  const scaleNow = () => {
    if (zoom !== null) return zoom
    const img = imgRef.current
    return img && naturalWidth > 0 ? img.clientWidth / naturalWidth : 1
  }
  const zoomBy = (factor: number) => setZoom(Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, scaleNow() * factor)))
  // Fit ↔ actual size.
  const toggleZoom = () => setZoom(zoom === null ? 1 : null)

  const onKeyDown = (e: KeyboardEvent) => {
    // A modified key is the browser's (Ctrl/⌘ +/−/0 page zoom, Alt+← back), never the viewer's.
    if (e.ctrlKey || e.metaKey || e.altKey) return
    // A focused player's own keys (←/→ seek): stepping as well would unmount what is playing.
    if (e.target instanceof HTMLMediaElement) return
    if (e.key === 'ArrowLeft') show(index - 1)
    else if (e.key === 'ArrowRight') show(index + 1)
    else if (item?.mode !== 'image') return
    else if (e.key === '+' || e.key === '=') zoomBy(ZOOM_STEP)
    else if (e.key === '-') zoomBy(1 / ZOOM_STEP)
    else if (e.key === '0') setZoom(null)
    else return
    e.preventDefault()
  }

  return (
    <DialogFrame
      testId="attachment-viewer-dialog"
      open={item !== null}
      onClose={close}
      fullScreen
      onKeyDown={onKeyDown}
      labelledBy="attachment-viewer-name"
      rootTestId="attachment-viewer"
      paperSx={(t: Theme) => ({ [PAPER_GROUND]: t.palette.common.black, backgroundColor: 'common.black', color: 'common.white', backgroundImage: 'none' })}
    >
      {item && (
        <>
          <Box
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 2,
              py: 1,
              minWidth: 0,
              borderBottom: '1px solid',
              borderBottomColor: t => alpha(t.palette.common.white, 0.12),
              [PHONE]: { px: 1, gap: 0.5 },
            }}
          >
            <Box sx={{ flex: 1, minWidth: 0 }}>
              <Tip title={item.name}><Typography id="attachment-viewer-name" noWrap sx={{ fontWeight: FONT_WEIGHT.semibold }}>
                {item.name}
              </Typography></Tip>
              <Typography variant="caption" noWrap component="div" sx={{ opacity: 0.7 }} data-testid="attachment-viewer-position">
                {items.length > 1 ? `${index + 1} of ${items.length} · ` : ''}{formatBytes(item.size)}
              </Typography>
            </Box>
            {item.mode === 'image' && (
              <Box sx={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
                <IconButton aria-label="Zoom out" onClick={() => zoomBy(1 / ZOOM_STEP)} sx={BAR_BUTTON_SX}>
                  <MinusIcon size={SIZE.iconSm} />
                </IconButton>
                {/* The level, and a reset to fit: one control, as the label is what it resets. */}
                <Button
                  aria-label="Fit to screen"
                  onClick={() => setZoom(null)}
                  data-testid="attachment-viewer-zoom"
                  sx={{ color: 'inherit', minWidth: grid(9), fontVariantNumeric: 'tabular-nums', [PHONE]: { display: 'none' } }}
                >
                  {zoom === null ? 'Fit' : `${Math.round(zoom * 100)}%`}
                </Button>
                <IconButton aria-label="Zoom in" onClick={() => zoomBy(ZOOM_STEP)} sx={BAR_BUTTON_SX}>
                  <PlusIcon size={SIZE.iconSm} />
                </IconButton>
              </Box>
            )}
            <IconButton
              component="a"
              {...downloadLinkProps(item.downloadPath, item.name)}
              data-testid="attachment-viewer-download"
              sx={BAR_BUTTON_SX}
            >
              <DownloadGlyph size={SIZE.iconSm} />
            </IconButton>
            <IconButton aria-label="Close" onClick={close} data-testid="attachment-viewer-close" sx={BAR_BUTTON_SX}>
              <CloseIcon size={SIZE.iconSm} />
            </IconButton>
          </Box>

          <Box sx={{ position: 'relative', flex: 1, minHeight: 0 }}>
            {/* Absent at either end rather than disabled: a dead arrow over the file is clutter. */}
            {index > 0 && (
              <IconButton
                aria-label="Previous file"
                onClick={() => show(index - 1)}
                sx={{ ...STEP_SX, left: grid(2.5), [PHONE]: { ...STEP_SX[PHONE], left: grid(1) } }}
              >
                <ChevronLeftIcon size={SIZE.iconMd} />
              </IconButton>
            )}
            {index < items.length - 1 && (
              <IconButton
                aria-label="Next file"
                onClick={() => show(index + 1)}
                sx={{ ...STEP_SX, right: grid(2.5), [PHONE]: { ...STEP_SX[PHONE], right: grid(1) } }}
              >
                <ChevronRightIcon size={SIZE.iconMd} />
              </IconButton>
            )}
            {/* Absolutely filled, so the percentages below resolve against a definite box. */}
            <Box sx={{ position: 'absolute', inset: 0, display: 'flex', overflow: 'auto', p: 2, [PHONE]: { p: 1 } }}>
              {item.mode === 'image' ? (
                <Box
                  component={ApiImg}
                  // `key` so a step REPLACES the element (else the previous image shows under the new name).
                  key={item.id}
                  ref={imgRef}
                  src={item.previewPath}
                  alt={item.name}
                  onClick={toggleZoom}
                  onLoad={(e: SyntheticEvent<HTMLImageElement>) => setNatural({ id: item.id, width: e.currentTarget.naturalWidth })}
                  data-testid="attachment-viewer-image"
                  /* `margin: auto` centres in both axes yet, unlike `align-items: center`, never clips a zoomed
                     image's top-left out of the scroll range. */
                  sx={zoom === null || naturalWidth === 0
                    ? { m: 'auto', maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', cursor: 'zoom-in' }
                    : { m: 'auto', flexShrink: 0, maxWidth: 'none', width: naturalWidth * zoom, cursor: 'zoom-out' }}
                />
              ) : item.mode === 'pdf' && !browserShowsPdf() ? (
                /* A browser set to download PDFs blocks a framed one (`browserShowsPdf`): offer the file instead. */
                <Box data-testid="attachment-viewer-pdf-download" sx={{ m: 'auto', textAlign: 'center', color: 'common.white' }}>
                  <Typography variant="body2">{PDF_DOWNLOADS_ONLY_NOTE}</Typography>
                  <Button
                    component="a"
                    {...downloadLinkProps(item.downloadPath, item.name)}
                    variant="contained"
                    startIcon={<DownloadGlyph />}
                    sx={[{ mt: 2 }, tapTargetSx]}
                  >
                    Download
                  </Button>
                </Box>
              ) : item.mode === 'pdf' ? (
                /* No `sandbox`: Chrome's PDF viewer will not start in a sandboxed frame. Why that is safe for a
                   PDF, and for nothing else, is argued once at the `pdf` branch of `TaskArtifacts.tsx`. */
                <Tip key={item.id} title={item.name}><Box
                  component={ApiFrame}
                  src={item.previewPath}
                  data-testid="attachment-viewer-pdf"
                  sx={{ width: '100%', height: '100%', border: 0, backgroundColor: 'common.white', borderRadius: RADIUS.sm }}
                /></Tip>
              ) : item.mode === 'audio' || item.mode === 'video' ? (
                <MediaPlayer
                  key={item.id}
                  kind={item.mode}
                  src={apiLinkHref(item.previewPath)}
                  name={item.name}
                  testId={`attachment-viewer-${item.mode}`}
                  /* On a phone the step arrows overlay the edges: the player keeps clear of them (39px + 6px each
                     side), or they cover its controls. */
                  sx={item.mode === 'video'
                    ? { m: 'auto', maxWidth: '100%', maxHeight: '100%', backgroundColor: 'common.black', [PHONE]: { maxWidth: `calc(100% - ${grid(15)})` } }
                    : { m: 'auto', width: '100%', maxWidth: SIZE.measure, [PHONE]: { width: `calc(100% - ${grid(15)})` } }}
                />
              ) : (
                <TextPreview key={item.id} item={item} />
              )}
            </Box>
          </Box>
        </>
      )}
    </DialogFrame>
  )
}

/** A download anchor's shared attributes for API `path`, for every "save this file" link in the task views (this
 *  bar, the Artifacts tab, the attachment chips): a click saves through `net/href.tsx::apiDownload`. `download`
 *  names the Save dialog; the label names the file, so four links are not four identical buttons and it is a
 *  superset of a visible "Download" (WCAG 2.5.3). `noopener`: the house default on hand-built links. */
export function downloadLinkProps(path: string, name: string) {
  return { ...apiDownloadProps(path, name), rel: 'noopener', 'aria-label': `Download ${name}` }
}

/** A fetched text body as read, or one italic line when it is known-empty — distinct from a failed read, which
 *  the caller handles first. The bytes are React children, never markup: a `<script>` renders as its characters.
 *  `children` replaces the plain rendering (the Artifacts tab's markdown); `text.primary`, as a dimmed log is
 *  unreadable. */
export function TextBody({ text, empty, children }: { text: string; empty: string; children?: ReactNode }): ReactNode {
  if (text.trim() === '') return <Typography variant="body2" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>{empty}</Typography>
  return children ?? <CodeBlock tone="plain" size="medium" sx={{ color: 'text.primary' }}>{text}</CodeBlock>
}

/* A `.json` is re-indented when it parses (a one-line API dump is unreadable); every other text is shown as is. */
function readable(name: string, text: string): string {
  if (!name.toLowerCase().endsWith('.json')) return text
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

/* On paper, at a reading measure, scrolling inside the viewer. */
function TextPreview({ item }: { item: ViewerItem }): JSX.Element {
  const read = useApiRead(['attachment-preview-text', item.previewPath], () => getText(item.previewPath), 'Failed to load')
  const body = read.error
    ? <Typography variant="body2">Could not load {item.name} — download it instead.</Typography>
    : read.data === null ? <LoadingState /> : <TextBody text={readable(item.name, read.data)} empty="This file is empty." />
  return (
    <Box
      data-testid="attachment-viewer-text"
      sx={{
        m: 'auto',
        width: '100%',
        maxWidth: grid(183),
        minHeight: '100%',
        px: 2,
        py: 1.5,
        borderRadius: RADIUS.sm,
        backgroundColor: 'background.paper',
        color: 'text.primary',
        [PHONE]: { px: 1 },
      }}
    >
      {body}
    </Box>
  )
}
