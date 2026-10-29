import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import TiptapImage, { type ImageOptions } from '@tiptap/extension-image'
import { NodeViewWrapper, ReactNodeViewRenderer, type NodeViewProps } from '@tiptap/react'
import type { MarkdownSerializerState } from 'prosemirror-markdown'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin } from '@tiptap/pm/state'
import { useEffect, useRef, useState } from 'react'
import { SOURCE_ATTR, recordImageSource, stampCopiedSources } from './attachmentCopy'
import {
  type AttachmentResolver,
  NotAnImageError,
  type StoredAttachment,
  attachmentToken,
  isAttachmentSrc,
  storedAttachmentFor,
} from './attachmentResolver'
// The shared arrow (`ui/DownloadGlyph`); 15px beside this control's 12px label.
import { DownloadGlyph } from '../../ui/DownloadGlyph'

type AttachmentImageOptions = ImageOptions & { resolveAttachmentSrc?: AttachmentResolver }

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
        // The bundled `image` serializer never calls `state.closeBlock(node)`; ours is a BLOCK node
        // (`inline: false`, where CON-3's no-base64 gate lives), so an image right before a heading glued
        // them onto one line and the `#` became literal text. Upstream body plus the missing closeBlock.
        serialize(state: MarkdownSerializerState, node: ProseMirrorNode) {
          state.write(
            `![${state.esc(node.attrs.alt || '')}](${String(node.attrs.src).replace(/[()]/g, '\\$&')}` +
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

/** SAVE THE STORED FILE — the only route to a pasted image's bytes, in editor and renderer alike.
 *  A link at the content route (as `TaskArtifacts`' `FileDownloadCard`), which already answers
 *  `Content-Disposition: attachment` unconditionally (`app/routes/attachments.py::user_task_attachment_content`,
 *  no `?download=1`); the browser streams it; same-origin, so the cookie rides it.
 *  BELOW THE PICTURE, ALWAYS VISIBLE: a hover overlay would cover the image, is unreachable by touch, and
 *  would sit on its editor selection region. Quiet at rest (`text.secondary`). */
function SaveImageLink({ stored }: { stored: StoredAttachment }) {
  const linkRef = useRef<HTMLAnchorElement>(null)

  useEffect(() => {
    const el = linkRef.current
    if (!el) return
    /* A NATIVE listener on the anchor: ProseMirror listens on `view.dom` and React 18 delegates to the root,
       so a synthetic `stopPropagation` runs after PM already made a NodeSelection (one Backspace from
       deletion). `draggable={false}` hands drags to the wrapper's `data-drag-handle`. `stopPropagation`,
       not `preventDefault` — the anchor's activation is the point. */
    const stopAtTarget = (event: Event) => event.stopPropagation()
    el.addEventListener('mousedown', stopAtTarget)
    return () => el.removeEventListener('mousedown', stopAtTarget)
  }, [])

  return (
    <Box
      component="a"
      ref={linkRef}
      href={stored.contentUrl}
      download={stored.filename}
      // `noopener`: the house default on hand-built links.
      rel="noopener"
      draggable={false}
      /* Chrome, not content: keeps the caret (and keystrokes) out. */
      contentEditable={false}
      data-testid="rte-image-download"
      /* The name carries the filename (as `TaskArtifacts`); the visible word stays one word. */
      aria-label={`Download image ${stored.filename}`}
      title={`Download ${stored.filename}`}
      sx={theme => ({
        /* A block on its own line, `fit-content` wide (`inline-flex` margins sat against a line box). ~2px
           to its image, a full block gap below, so it reads as THIS image's footer. `mb` in px: 16px is the
           editor's paragraph rhythm (`.rte-prosemirror > p`), not a multiple of the 6px spacing unit. */
        display: 'flex',
        width: 'fit-content',
        alignItems: 'center',
        gap: 0.75,
        mt: '2px',
        mb: '16px',
        padding: '3px 8px',
        border: '1px solid transparent',
        borderRadius: 1.5,
        color: 'text.secondary',
        fontSize: '0.75rem',
        fontWeight: 500,
        lineHeight: 1.4,
        textDecoration: 'none',
        cursor: 'pointer',
        userSelect: 'none',
        '&:hover': { color: 'text.primary', backgroundColor: 'action.hover', borderColor: 'divider' },
        '&:focus-visible': {
          color: 'text.primary',
          backgroundColor: 'action.hover',
          outline: `2px solid ${theme.palette.primary.main}`,
          outlineOffset: 2,
        },
        // The 44px tap floor at phone width (`ui/listStyles`' `tapTargetSx`), with padding grown both ways.
        [theme.breakpoints.down(481)]: {
          minHeight: theme.custom?.tapTargetPx ?? 44,
          padding: '0 12px',
        },
      })}
    >
      <DownloadGlyph size={15} />
      Download
    </Box>
  )
}

function AttachmentImageView({ node, editor, resolve }: NodeViewProps & { resolve?: AttachmentResolver }) {
  const src = node.attrs.src as string
  const alt = (node.attrs.alt as string | null) ?? ''
  const title = (node.attrs.title as string | null) ?? undefined
  const isAttachment = isAttachmentSrc(src)

  const [resolvedSrc, setResolvedSrc] = useState<string | null>(isAttachment ? null : src)
  const [failed, setFailed] = useState(false)
  // A stored file that is not a paintable image (`paintableImageUrl`): offered as a download, never painted.
  const [notImage, setNotImage] = useState<StoredAttachment | null>(null)

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

  const stored = storedAttachmentFor(resolvedSrc)

  return (
    // TipTap's own wrapper, plus a `data-testid`; the image styling sits on the inner Box.
    <NodeViewWrapper as="span" data-testid="rte-image-wrapper" data-drag-handle>
      <Box component="span" sx={{ display: 'inline-block', maxWidth: '100%' }}>
        {resolvedSrc ? (
          <>
            <Box
              component="img"
              src={resolvedSrc}
              alt={alt}
              title={title}
              sx={{ display: 'block', maxWidth: '100%', height: 'auto', borderRadius: 1.5 }}
            />
            {/* Only with a stored file (not a staged create-mode image, external src or broken branch). Read
                at render: the resolver records the mapping as it returns the URL (`attachmentResolver.ts`). */}
            {stored ? <SaveImageLink stored={stored} /> : null}
          </>
        ) : notImage ? (
          <>
            <Typography
              variant="caption"
              component="span"
              data-testid="rte-image-not-image"
              sx={{ display: 'inline-block', padding: '4px 8px', borderRadius: 1, backgroundColor: 'action.hover', color: 'text.secondary' }}
            >
              {notImage.filename} is not an image
            </Typography>
            <SaveImageLink stored={notImage} />
          </>
        ) : failed ? (
          <Typography
            variant="caption"
            component="span"
            data-testid="rte-image-broken"
            sx={{ display: 'inline-block', padding: '4px 8px', borderRadius: 1, backgroundColor: 'action.hover', color: 'error.main' }}
          >
            ⚠ image unavailable
          </Typography>
        ) : (
          <Typography
            variant="caption"
            component="span"
            data-testid="rte-image-loading"
            sx={{ display: 'inline-block', padding: '4px 8px', borderRadius: 1, backgroundColor: 'action.hover', color: 'text.secondary' }}
          >
            Loading image…
          </Typography>
        )}
      </Box>
    </NodeViewWrapper>
  )
}
