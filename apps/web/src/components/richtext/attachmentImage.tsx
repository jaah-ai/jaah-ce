import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Modal from '@mui/material/Modal'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import TiptapImage, { type ImageOptions } from '@tiptap/extension-image'
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from '@tiptap/react'
import type { MarkdownSerializerState } from 'prosemirror-markdown'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin } from '@tiptap/pm/state'
import { type ReactNode, type RefObject, useEffect, useRef, useState } from 'react'
import { SOURCE_ATTR, recordImageSource, stampCopiedSources } from './attachmentCopy'
import {
  type AttachmentResolver,
  NotAnImageError,
  type StoredAttachment,
  attachmentToken,
  isAttachmentSrc,
} from './attachmentResolver'
// The shared arrow (`ui/DownloadGlyph`); 15px beside this control's 12px label.
import { DownloadGlyph } from '../../ui/DownloadGlyph'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import Tip from '../../ui/Tip'

type AttachmentImageOptions = ImageOptions & { resolveAttachmentSrc?: AttachmentResolver }

/* An image shows at most this wide unless a person resized it, and never wider than its own pixels: a small
   screenshot keeps its size, a large one stops filling the editor. Click it for full size. */
export const IMAGE_DEFAULT_MAX_PX = 480
/* The narrowest a drag-resize goes: below this a screenshot is unreadable and its handle hard to grab. */
const IMAGE_MIN_PX = 80
/* The widest a saved width goes: past any screen, so a stray value can never leak into the alt as text. */
const IMAGE_MAX_PX = 4000

function clampWidth(width: number): number {
  return Math.min(IMAGE_MAX_PX, Math.max(IMAGE_MIN_PX, Math.round(width)))
}

/* A RESIZED image's width rides its markdown ALT text as `![name|600](attachment:…)` (the Obsidian convention):
   the console renders markdown with `html: false`, so `<img width>` is not available, and the alt is the one
   slot every `![…](attachment:token)` matcher (`stripAttachmentImage`, the support chat's token, the API's
   short-title strip) already accepts as free text. */
const ALT_WIDTH = /^(.*)\|(\d{1,5})$/s

export function splitAltWidth(raw: string | null): { alt: string | null, width: number | null } {
  const found = raw ? ALT_WIDTH.exec(raw) : null
  if (!found) return { alt: raw, width: null }
  return { alt: found[1], width: clampWidth(Number(found[2])) }
}

// The image NODE VIEW turns a stored `attachment:<token>` src into something paintable (the scheme never
// loads). Extends `@tiptap/extension-image` (same `name: 'image'`), keeping the schema and tiptap-markdown's
// image serializer identical in editor and renderer (AC-9).
export const AttachmentImage = TiptapImage.extend<AttachmentImageOptions>({
  addOptions() {
    return {
      ...(this.parent?.() as ImageOptions),
      resolveAttachmentSrc: undefined,
    }
  },
  addAttributes() {
    return {
      ...this.parent?.(),
      /* Width suffix off the alt (`splitAltWidth`). An `<img width>` counts only on an in-editor copy (it
         carries `SOURCE_ATTR`): a web page's own width would undo the default cap. */
      alt: {
        default: null,
        parseHTML: (element: HTMLElement) => splitAltWidth(element.getAttribute('alt')).alt,
      },
      width: {
        default: null,
        parseHTML: (element: HTMLElement) => {
          const own = element.hasAttribute(SOURCE_ATTR) ? Number(element.getAttribute('width')) : 0
          return own > 0 ? clampWidth(own) : splitAltWidth(element.getAttribute('alt')).width
        },
      },
      /* Where a COPIED image's bytes are: set only on the clipboard (`attachmentCopy.ts`), never in markdown
         (the serializer below writes src/alt/title alone). */
      source: {
        default: null,
        parseHTML: (element: HTMLElement) => element.getAttribute(SOURCE_ATTR),
        renderHTML: (attributes: { source?: string | null }) =>
          attributes.source ? { [SOURCE_ATTR]: attributes.source } : {},
      },
    }
  },
  addProseMirrorPlugins() {
    const editor = this.editor
    return [new Plugin({ props: { transformCopied: slice => stampCopiedSources(slice, editor) } })]
  },
  addStorage() {
    return {
      markdown: {
        // The width rides the alt (`ALT_WIDTH`).
        // The bundled `image` serializer never calls `state.closeBlock(node)`; ours is a BLOCK node
        // (`inline: false`, where CON-3's no-base64 gate lives), so an image right before a heading glued
        // them onto one line and the `#` became literal text. Upstream body plus the missing closeBlock.
        serialize(state: MarkdownSerializerState, node: ProseMirrorNode) {
          state.write(
            `![${state.esc(node.attrs.alt || '')}${node.attrs.width ? `|${clampWidth(node.attrs.width)}` : ''}](${String(node.attrs.src).replace(/[()]/g, '\\$&')}` +
              (node.attrs.title ? ` "${String(node.attrs.title).replace(/"/g, '\\"')}"` : '') +
              ')',
          )
          state.closeBlock(node)
        },
      },
    }
  },
  addNodeView() {
    const resolve = this.options.resolveAttachmentSrc
    return ReactNodeViewRenderer((props: NodeViewProps) => (
      <AttachmentImageView {...props} resolve={resolve} />
    ))
  },
})

/* A NATIVE mousedown stop on image chrome: ProseMirror listens on `view.dom` and React 18 delegates to the root,
   so a synthetic `stopPropagation` runs after PM already made a NodeSelection (one Backspace from deletion).
   `stopPropagation`, not `preventDefault` — the control's own activation is the point. */
function useStopEditorMousedown(ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const stopAtTarget = (event: Event) => event.stopPropagation()
    el.addEventListener('mousedown', stopAtTarget)
    return () => el.removeEventListener('mousedown', stopAtTarget)
  }, [ref])
}

/* The quiet footer control under a non-image file (Download): text.secondary at rest. */
const footerControlSx = (theme: Theme) => ({
  display: 'inline-flex',
  alignItems: 'center',
  gap: 0.5,
  py: 0.5,
  px: 1,
  border: '1px solid transparent',
  borderRadius: RADIUS.md,
  background: 'none',
  color: 'text.secondary',
  font: 'inherit',
  fontSize: FONT_SIZE.sm,
  fontWeight: FONT_WEIGHT.medium,
  lineHeight: LINE_HEIGHT.snug,
  textDecoration: 'none',
  cursor: 'pointer',
  userSelect: 'none',
  '&:hover': { color: 'text.primary', backgroundColor: 'action.hover', borderColor: 'divider' },
  '&:focus-visible': {
    color: 'text.primary',
    backgroundColor: 'action.hover',
    outline: `2px solid ${(theme.vars ?? theme).palette.primary.main}`,
    outlineOffset: 2,
  },
  // The 44px tap floor at phone width (`ui/listStyles`' `tapTargetSx`), with padding grown both ways.
  [theme.breakpoints.down(481)]: {
    minHeight: theme.custom?.tapTargetPx ?? SIZE.tapTarget,
    py: 0,
    px: 2,
  },
})

/** The row under a stored file that is not an image. A block on its own line and a full block gap below.
 *  `mb`: about the editor's paragraph rhythm (`.rte-prosemirror > p`). A picture has no footer: the picture
 *  itself opens full size. Chrome, not content: `contentEditable={false}` keeps the caret out. */
function ImageFooter({ children }: { children: ReactNode }) {
  return (
    <Box component="span" contentEditable={false} sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mt: 0, mb: 2.5 }}>
      {children}
    </Box>
  )
}

/** SAVE THE STORED FILE — the only route to a non-image attachment's bytes, in editor and renderer alike.
 *  A link at the content route (as `TaskArtifacts`' `FileDownloadCard`), which already answers
 *  `Content-Disposition: attachment` unconditionally (`app/routes/attachments.py::user_task_attachment_content`,
 *  no `?download=1`); the browser streams it; same-origin, so the cookie rides it. */
function SaveImageLink({ stored }: { stored: StoredAttachment }) {
  const linkRef = useRef<HTMLAnchorElement>(null)
  useStopEditorMousedown(linkRef)

  return (
    <Box
      component="a"
      ref={linkRef}
      href={stored.contentUrl}
      download={stored.filename}
      // `noopener`: the house default on hand-built links.
      rel="noopener"
      // Drags belong to the wrapper's `data-drag-handle`.
      draggable={false}
      contentEditable={false}
      data-testid="rte-image-download"
      /* The name carries the filename (as `TaskArtifacts`); the visible word stays one word. */
      aria-label={`Download image ${stored.filename}`}
      sx={footerControlSx}
    >
      <DownloadGlyph size={SIZE.iconXs} />
      Download
    </Box>
  )
}

/** FULL SIZE, as Slack: a dark full-screen overlay, the image at its own size or shrunk to fit the window
 *  (never blown up). Esc, the close button or a click anywhere closes it. */
function ImageLightbox({ src, alt, open, onClose }: { src: string, alt: string, open: boolean, onClose: () => void }) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      aria-label={alt || 'Image'}
      slotProps={{ backdrop: { sx: { backgroundColor: (t: Theme) => `color-mix(in srgb, ${(t.vars ?? t).palette.common.black} 88%, transparent)` } } }}
    >
      <Box
        data-testid="rte-image-lightbox"
        onClick={event => { event.stopPropagation(); onClose() }}
        sx={{ position: 'fixed', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', p: { xs: 1, sm: 4 }, outline: 'none' }}
      >
        <Box
          component="img"
          src={src}
          alt={alt}
          sx={{ display: 'block', maxWidth: '100%', maxHeight: '100%', objectFit: 'contain', borderRadius: RADIUS.sm, boxShadow: 24 }}
        />
        <IconButton
          aria-label="Close"
          onClick={event => { event.stopPropagation(); onClose() }}
          sx={{
            position: 'fixed', top: grid(2), right: grid(2), color: 'common.white',
            backgroundColor: (t) => `color-mix(in srgb, ${(t.vars ?? t).palette.common.white} 12%, transparent)`,
            '&:hover': { backgroundColor: (t) => `color-mix(in srgb, ${(t.vars ?? t).palette.common.white} 24%, transparent)` },
          }}
        >
          <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <path d="M3 3l10 10M13 3 3 13" />
          </svg>
        </IconButton>
      </Box>
    </Modal>
  )
}

/** The editor-only bottom-right corner grip: drag to set the image's width, saved into the markdown on release
 *  (`ALT_WIDTH`). Pointer events, so mouse, pen and touch all resize. */
function ResizeHandle({ imgRef, onCommit }: { imgRef: RefObject<HTMLImageElement | null>, onCommit: (width: number) => void }) {
  const ref = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    const handle = ref.current
    if (!handle) return
    // The drag in flight, so a view torn down mid-drag drops its window listeners without committing.
    let abortDrag: (() => void) | null = null
    const start = (event: PointerEvent) => {
      const img = imgRef.current
      if (!img || event.button !== 0) return
      // Neither a NodeSelection nor the node's own drag starts from the grip.
      event.preventDefault()
      event.stopPropagation()
      const startX = event.clientX
      const startWidth = img.getBoundingClientRect().width
      const container = img.closest('.ProseMirror') as HTMLElement | null
      const padding = container ? parseFloat(getComputedStyle(container).paddingLeft) + parseFloat(getComputedStyle(container).paddingRight) : 0
      const maxWidth = container?.clientWidth ? container.clientWidth - (padding || 0) : Number.POSITIVE_INFINITY
      let width = Math.round(startWidth)
      const move = (e: PointerEvent) => {
        width = Math.round(Math.min(maxWidth, Math.max(IMAGE_MIN_PX, startWidth + e.clientX - startX)))
        img.style.width = `${width}px`
        img.style.maxWidth = '100%'
      }
      const detach = () => {
        window.removeEventListener('pointermove', move)
        window.removeEventListener('pointerup', end)
        window.removeEventListener('pointercancel', end)
        abortDrag = null
      }
      const end = () => {
        detach()
        // The node's width (via `sx`) takes over again, so an undo or a later change shows true.
        img.style.width = ''
        img.style.maxWidth = ''
        if (width !== Math.round(startWidth)) onCommit(width)
      }
      abortDrag = detach
      window.addEventListener('pointermove', move)
      window.addEventListener('pointerup', end)
      window.addEventListener('pointercancel', end)
    }
    const stop = (event: Event) => { event.preventDefault(); event.stopPropagation() }
    handle.addEventListener('pointerdown', start)
    handle.addEventListener('mousedown', stop)
    return () => {
      abortDrag?.()
      handle.removeEventListener('pointerdown', start)
      handle.removeEventListener('mousedown', stop)
    }
  }, [imgRef, onCommit])

  return (
    <Tip title="Drag to resize"><Box
      component="span"
      ref={ref}
      contentEditable={false}
      draggable={false}
      data-testid="rte-image-resize"
      aria-hidden="true"
      className="rte-image-resize"
      sx={theme => ({
        position: 'absolute',
        right: `-${grid(1)}`,
        bottom: `-${grid(1)}`,
        width: grid(2),
        height: grid(2),
        borderRadius: RADIUS.round,
        backgroundColor: 'background.paper',
        border: `2px solid ${(theme.vars ?? theme).palette.primary.main}`,
        cursor: 'nwse-resize',
        touchAction: 'none',
        opacity: 0,
        transition: 'opacity 120ms',
        // Always visible where nothing hovers (touch).
        '@media (hover: none)': { opacity: 1, width: grid(3.5), height: grid(3.5), right: `-${grid(1.5)}`, bottom: `-${grid(1.5)}` },
      })}
    /></Tip>
  )
}

function AttachmentImageView({ node, editor, selected, updateAttributes, resolve }: NodeViewProps & { resolve?: AttachmentResolver }) {
  const src = node.attrs.src as string
  const alt = (node.attrs.alt as string | null) ?? ''
  const title = (node.attrs.title as string | null) ?? undefined
  const width = (node.attrs.width as number | null) ?? null
  const isAttachment = isAttachmentSrc(src)
  const editable = editor.isEditable
  const imgRef = useRef<HTMLImageElement>(null)

  const [resolvedSrc, setResolvedSrc] = useState<string | null>(isAttachment ? null : src)
  const [failed, setFailed] = useState(false)
  // A stored file that is not a paintable image (`paintableImageUrl`): offered as a download, never painted.
  const [notImage, setNotImage] = useState<StoredAttachment | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!isAttachment) {
      setResolvedSrc(src)
      setFailed(false)
      return
    }
    if (!resolve) {
      setFailed(true)
      return
    }
    let cancelled = false
    setResolvedSrc(null)
    setFailed(false)
    setNotImage(null)
    resolve(attachmentToken(src))
      .then(url => {
        // Recorded even if this view is gone: a copy of the image still needs its source.
        recordImageSource(editor, attachmentToken(src), url)
        if (!cancelled) setResolvedSrc(url)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setFailed(true)
        if (err instanceof NotAnImageError && err.stored) setNotImage(err.stored)
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, isAttachment, resolve])

  // A drag-resize lands in the node (and so the markdown) once, on release.
  const updateRef = useRef(updateAttributes)
  updateRef.current = updateAttributes
  const [onCommit] = useState(() => (next: number) => updateRef.current({ width: next }))

  return (
    // TipTap's own wrapper, plus a `data-testid`; the image styling sits on the inner Box.
    <NodeViewWrapper as="span" data-testid="rte-image-wrapper" data-drag-handle>
      <Box component="span" sx={{ display: 'inline-block', maxWidth: '100%' }}>
        {resolvedSrc ? (
          <>
            <Box
              component="span"
              sx={{
                position: 'relative',
                display: 'inline-block',
                maxWidth: '100%',
                verticalAlign: 'top',
                // The paragraph gap below a picture (`ImageFooter`'s `mb`).
                mb: 2.5,
                '&:hover .rte-image-resize': { opacity: 1 },
                ...(selected ? { '& .rte-image-resize': { opacity: 1 } } : {}),
              }}
            >
              <Tip title={title}><Box
                component="img"
                ref={imgRef}
                src={resolvedSrc}
                alt={alt}
                data-width={width ?? undefined}
                // A click opens it full size, in the editor too (resize stays on the corner handle); the read
                // view also opens it on Enter/Space.
                onClick={event => { event.stopPropagation(); setExpanded(true) }}
                role={editable ? undefined : 'button'}
                tabIndex={editable ? undefined : 0}
                aria-label={editable ? undefined : `Open ${alt || 'image'} full size`}
                onKeyDown={editable ? undefined : event => {
                  if (event.key !== 'Enter' && event.key !== ' ') return
                  event.preventDefault()
                  setExpanded(true)
                }}
                sx={{
                  display: 'block',
                  /* A person's width when they resized it; otherwise the image's own width up to the default
                     cap. Never past the column. */
                  width: width ? `${width}px` : 'auto',
                  maxWidth: width ? '100%' : `min(100%, ${IMAGE_DEFAULT_MAX_PX}px)`,
                  height: 'auto',
                  borderRadius: RADIUS.md,
                  cursor: 'zoom-in',
                  outline: selected && editable ? '2px solid' : 'none',
                  outlineColor: 'primary.main',
                  outlineOffset: 2,
                  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main' },
                }}
              /></Tip>
              {editable ? <ResizeHandle imgRef={imgRef} onCommit={onCommit} /> : null}
            </Box>
            <ImageLightbox src={resolvedSrc} alt={alt} open={expanded} onClose={() => setExpanded(false)} />
          </>
        ) : notImage ? (
          <>
            <Typography
              variant="caption"
              component="span"
              data-testid="rte-image-not-image"
              sx={{ display: 'inline-block', py: 0.5, px: 1, borderRadius: RADIUS.sm, backgroundColor: 'action.hover', color: 'text.secondary' }}
            >
              {notImage.filename} is not an image
            </Typography>
            <ImageFooter>
              <SaveImageLink stored={notImage} />
            </ImageFooter>
          </>
        ) : failed ? (
          <Typography
            variant="caption"
            component="span"
            data-testid="rte-image-broken"
            sx={{ display: 'inline-block', py: 0.5, px: 1, borderRadius: RADIUS.sm, backgroundColor: 'action.hover', color: 'error.main' }}
          >
            ⚠ image unavailable
          </Typography>
        ) : (
          <Typography
            variant="caption"
            component="span"
            data-testid="rte-image-loading"
            sx={{ display: 'inline-block', py: 0.5, px: 1, borderRadius: RADIUS.sm, backgroundColor: 'action.hover', color: 'text.secondary' }}
          >
            Loading image…
          </Typography>
        )}
      </Box>
    </NodeViewWrapper>
  )
}
