import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Divider from '@mui/material/Divider'
import type { Theme } from '@mui/material/styles'
import { EditorContent, useEditor, useEditorState, type Editor } from '@tiptap/react'
import type { Slice } from '@tiptap/pm/model'
import { useEffect, useRef, useState } from 'react'
import { buildExtensions } from './extensions'
import type { AttachmentResolver } from './attachmentResolver'
import { hasCopiedImages, mintImageFilename, pasteCopiedImages } from './attachmentCopy'
import { Banner } from '../../ui/Banner'
import { MoreHorizIcon } from '../../ui/icons'
import { clipboardFiles } from '../../lib/clipboardFiles'
import { paperGround } from '../../theme'

// Reusable rich-text authoring: TipTap over markdown storage (DEC-1: `value`/`onChange` always carry
// markdown, never HTML), capped to the REQ-1 set (DEC-7). Owns no page layout beyond its container.
export interface RichTextEditorProps {
  value: string
  onChange: (markdown: string) => void
  // Uploads a file under the token filename minted by `mintImageFilename`. Left to the caller because an
  // existing task uploads immediately while a create stages until save.
  onUploadImage: (file: File, filename: string) => Promise<void>
  // Resolves an `attachment:<token>` src to a paintable URL; omit only when there are no attachments.
  resolveAttachmentSrc?: AttachmentResolver
  placeholder?: string
  autoFocus?: boolean
  className?: string
  /* Enter starts a new line, not a paragraph (`extensions.ts::EnterLineBreak`). */
  lineBreaks?: boolean
}

function insertImageFiles(
  editor: Editor,
  files: File[],
  onUploadImage: (file: File, filename: string) => Promise<void>,
) {
  for (const file of files) {
    if (!file.type.startsWith('image/')) continue
    const filename = mintImageFilename(file)
    editor.chain().focus().setImage({ src: `attachment:${filename}`, alt: file.name || 'image' }).run()
    void onUploadImage(file, filename)
  }
}

/* The editor's size container and the two widths its toolbar's one-row rule breaks at: the whole labelled set
   on one row, then the labelled primaries plus `⋯`. Measured on the rendered toolbar. */
const RTE_CONTAINER = 'rte'
const RTE_FULL_ROW_PX = 760
const RTE_LABELLED_ROW_PX = 460

// Chrome shared by every toolbar button. A function, not a wrapper component, so callers can extend
// `sx` per button.
function toolbarBtnSx(theme: Theme) {
  return {
    minWidth: 32,
    minHeight: 32,
    padding: '4px 8px',
    border: '1px solid transparent',
    borderRadius: 1.5, // 6px (theme.shape.borderRadius 4 × 1.5)
    backgroundColor: 'transparent',
    color: 'text.secondary',
    fontSize: '0.8125rem',
    lineHeight: 1.2,
    '&:hover': { backgroundColor: 'action.hover', borderColor: 'divider' },
    // The active state is `aria-pressed`, not a class.
    '&[aria-pressed="true"]': {
      backgroundColor: 'action.selected',
      borderColor: 'primary.main',
      color: 'primary.main',
    },
    // 44×44 tap floor at ≤480px exactly; `down('sm')` (600px) would widen the range.
    [theme.breakpoints.down(481)]: { minWidth: 44, minHeight: 44 },
  }
}

export default function RichTextEditor({
  value,
  onChange,
  onUploadImage,
  resolveAttachmentSrc,
  placeholder,
  autoFocus,
  className,
  lineBreaks = false,
}: RichTextEditorProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  // At ≤480px the six secondaries collapse behind `⋯` (AC-3); this flag un-hides them in place. The media
  // query does the hiding.
  const [moreOpen, setMoreOpen] = useState(false)
  // Images a paste copied from another body but could not (`attachmentCopy.ts`): left out, and said so here.
  const [pasteError, setPasteError] = useState('')
  // Read by the paste handler, registered once at creation.
  const resolveRef = useRef(resolveAttachmentSrc)
  resolveRef.current = resolveAttachmentSrc
  // A ref, so paste/drop handlers registered at creation always call the latest `onUploadImage`.
  const onUploadImageRef = useRef(onUploadImage)
  onUploadImageRef.current = onUploadImage

  // handlePaste/handleDrop are registered before `editor` exists as a binding; the ref is set by the time
  // they fire.
  const editorRef = useRef<Editor | null>(null)

  // The current placeholder, read by the accessor handed to `buildExtensions`.
  const placeholderRef = useRef(placeholder)
  placeholderRef.current = placeholder

  function rehomeCopiedImages(slice: Slice, range: { from: number; to: number }) {
    setPasteError('')
    pasteCopiedImages(editorRef.current!, slice, range, resolveRef.current, onUploadImageRef.current)
      .then(leftOut => {
        if (leftOut > 0) setPasteError(copyFailedMessage(leftOut))
      })
      .catch(() => setPasteError('Paste failed — try again.'))
  }

  const editor = useEditor({
    // The accessor, never the string — see the placeholder effect below.
    extensions: buildExtensions({
      editable: true,
      resolveAttachmentSrc,
      placeholder: placeholder === undefined ? undefined : () => placeholderRef.current ?? '',
      lineBreaks,
    }),
    content: value,
    autofocus: autoFocus ?? false,
    editorProps: {
      attributes: { class: 'rte-prosemirror', 'aria-label': 'Rich text editor' },
      handlePaste: (view, event, slice) => {
        const files = clipboardFiles(event.clipboardData)
        if (files.length > 0) {
          event.preventDefault()
          insertImageFiles(editorRef.current!, files, onUploadImageRef.current)
          return true
        }
        // Images copied out of another task or ticket: each gets its own stored copy here.
        if (!hasCopiedImages(slice)) return false
        event.preventDefault()
        const { from, to } = view.state.selection
        rehomeCopiedImages(slice, { from, to })
        return true
      },
      handleDrop: (view, event, slice, moved) => {
        const files = Array.from(event.dataTransfer?.files ?? []).filter(f => f.type.startsWith('image/'))
        if (files.length > 0) {
          event.preventDefault()
          insertImageFiles(editorRef.current!, files, onUploadImageRef.current)
          return true
        }
        // Dragged in from another editor (a move within this one keeps its own tokens).
        if (moved || !hasCopiedImages(slice)) return false
        const at = view.posAtCoords({ left: event.clientX, top: event.clientY })
        if (!at) return false
        event.preventDefault()
        rehomeCopiedImages(slice, { from: at.pos, to: at.pos })
        return true
      },
    },
    onUpdate: ({ editor }) => {
      // StrictMode's dev double-invoke can fire a transaction before `storage.markdown` is populated.
      if (!editor.storage.markdown) return
      onChange(editor.storage.markdown.getMarkdown())
    },
  })

  editorRef.current = editor

  /* The toolbar's pressed state, read from the editor on every transaction. tiptap v3's `useEditor` does not
  re-render on a transaction, so `isActive` read at render time showed the state of the last React render: a caret
  move alone (no content change, so no `onChange`) left Italic pressed in plain text, or unpressed inside italic.
  `useEditorState` re-renders only when this snapshot changes. With a collapsed caret `isActive` reads the stored
  marks, then the marks at the caret: what the next typed text gets. */
  const pressed = useEditorState({
    editor,
    selector: ({ editor: e }) => ({
      bold: e?.isActive('bold') ?? false,
      italic: e?.isActive('italic') ?? false,
      h1: e?.isActive('heading', { level: 1 }) ?? false,
      h2: e?.isActive('heading', { level: 2 }) ?? false,
      bulletList: e?.isActive('bulletList') ?? false,
      orderedList: e?.isActive('orderedList') ?? false,
      taskList: e?.isActive('taskList') ?? false,
      blockquote: e?.isActive('blockquote') ?? false,
      code: e?.isActive('code') ?? false,
      codeBlock: e?.isActive('codeBlock') ?? false,
      link: e?.isActive('link') ?? false,
    }),
  })

  // Push an external `value` only when it diverges from the editor's markdown; an unconditional
  // `setContent` resets the cursor on every keystroke.
  useEffect(() => {
    if (!editor || !editor.storage.markdown) return
    if (editor.storage.markdown.getMarkdown() !== value) {
      editor.commands.setContent(value, { emitUpdate: false })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, editor])

  /* The placeholder tracks its prop. `useEditor` runs `buildExtensions` once and tiptap copies options
  into plugin context, so only the accessor sees the current prop; this empty transaction forces a
  decoration pass (a React re-render does not touch the view). Not a new editor: recreating loses text,
  cursor and undo, and `TaskEditForm`'s persona-driven placeholder changes while the operator types. */
  useEffect(() => {
    if (!editor || editor.isDestroyed) return
    editor.view.dispatch(editor.state.tr)
  }, [editor, placeholder])

  // No manual `editor?.destroy()`: `useEditor` owns the lifecycle, and an extra destroy races it and
  // leaves `editor.storage.markdown` undefined.

  function handleFilesPicked(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? [])
    e.target.value = '' // allow re-picking the same file
    if (!editor || files.length === 0) return
    insertImageFiles(editor, files, onUploadImageRef.current)
  }

  if (!editor || !pressed) return null

  return (
    <Box
      className={className}
      data-testid="rte"
      sx={(theme: Theme) => ({
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minHeight: 320,
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 2, // 8px
        // The paper's own ground: plain `background.paper` reads as a darker box on elevated dark paper.
        backgroundColor: paperGround(theme),
        overflow: 'hidden',
        /* A size container, so the toolbar's one-row rule follows the EDITOR's width, not the viewport's: in the
           list's split pane a wide viewport hosts a narrow editor, and the toolbar wrapped to two rows there. */
        containerType: 'inline-size',
        containerName: RTE_CONTAINER,
        // Fullscreen overlay at ≤480px.
        [theme.breakpoints.down(481)]: {
          position: 'fixed',
          inset: 0,
          zIndex: 60,
          border: 'none',
          borderRadius: 0,
        },
      })}
    >
      <Box
        role="toolbar"
        aria-label="Formatting"
        data-testid="rte-toolbar"
        data-expanded={moreOpen}
        sx={(theme: Theme) => ({
          position: 'sticky',
          top: 0,
          zIndex: 2,
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          gap: 0.666, // 0.25rem
          padding: 1.33, // 0.5rem
          borderBottom: '1px solid',
          borderColor: 'divider',
          backgroundColor: paperGround(theme),
          flexShrink: 0,
          // AC-3 one-row mobile toolbar: primaries swap to compact glyphs, secondaries hide until `⋯`.
          [theme.breakpoints.down(481)]: {
            gap: 0.333, // 2px
            padding: 0.666, // 4px
            '& [data-role="rte-more"]': { display: 'inline-flex' },
            '& [data-role~="rte-primary"] [data-testid="rte-btn-full"]': { display: 'none' },
            '& [data-role~="rte-primary"] [data-testid="rte-btn-compact"]': {
              display: 'inline',
              fontSize: '0.9375rem',
              lineHeight: 1,
            },
            '& [data-role~="rte-secondary"]': { display: 'none' },
            '&[data-expanded="true"] [data-role~="rte-secondary"]': { display: 'inline-flex' },
          },
          // AC-3(b): below 360px Link demotes into the overflow — six targets fit at 320px, seven do not.
          [theme.breakpoints.down(360)]: {
            '& [data-role~="rte-secondary-narrow"]': { display: 'none' },
            '&[data-expanded="true"] [data-role~="rte-secondary-narrow"]': { display: 'inline-flex' },
          },
          /* The same one row at any viewport, by the editor's own width: the full set needs
             ~`RTE_FULL_ROW_PX`; narrower, the secondaries fold behind `⋯` instead of wrapping onto a second row,
             and narrower still the primaries take their compact glyphs too. */
          [`@container ${RTE_CONTAINER} (max-width: ${RTE_FULL_ROW_PX - 1}px)`]: {
            '& [data-role="rte-more"]': { display: 'inline-flex' },
            '& [data-role~="rte-secondary"]': { display: 'none' },
            '&[data-expanded="true"] [data-role~="rte-secondary"]': { display: 'inline-flex' },
          },
          [`@container ${RTE_CONTAINER} (max-width: ${RTE_LABELLED_ROW_PX - 1}px)`]: {
            '& [data-role~="rte-primary"] [data-testid="rte-btn-full"]': { display: 'none' },
            '& [data-role~="rte-primary"] [data-testid="rte-btn-compact"]': { display: 'inline', lineHeight: 1 },
            '& hr': { display: 'none' },
          },
        })}
      >
        <ToolbarButton editor={editor} label="Bold" compact="B" role="rte-primary" aria="Bold" run={e => e.chain().focus().toggleBold().run()} active={pressed.bold} />
        <ToolbarButton editor={editor} label="Italic" compact="I" role="rte-primary" aria="Italic" run={e => e.chain().focus().toggleItalic().run()} active={pressed.italic} />
        <Divider orientation="vertical" flexItem sx={(theme: Theme) => ({ mx: 0.666, [theme.breakpoints.down(481)]: { display: 'none' } })} />
        <ToolbarButton editor={editor} label="H1" role="rte-secondary" aria="Heading 1" run={e => e.chain().focus().toggleHeading({ level: 1 }).run()} active={pressed.h1} />
        <ToolbarButton editor={editor} label="H2" compact="H2" role="rte-primary" aria="Heading 2" run={e => e.chain().focus().toggleHeading({ level: 2 }).run()} active={pressed.h2} />
        <Divider orientation="vertical" flexItem sx={(theme: Theme) => ({ mx: 0.666, [theme.breakpoints.down(481)]: { display: 'none' } })} />
        <ToolbarButton editor={editor} label="• List" compact="•" role="rte-primary" aria="Bullet list" run={e => e.chain().focus().toggleBulletList().run()} active={pressed.bulletList} />
        <ToolbarButton editor={editor} label="1. List" role="rte-secondary" aria="Numbered list" run={e => e.chain().focus().toggleOrderedList().run()} active={pressed.orderedList} />
        <ToolbarButton editor={editor} label="☑ Checklist" compact="☑" role="rte-primary" aria="Checklist" run={e => e.chain().focus().toggleTaskList().run()} active={pressed.taskList} />
        <Divider orientation="vertical" flexItem sx={(theme: Theme) => ({ mx: 0.666, [theme.breakpoints.down(481)]: { display: 'none' } })} />
        <ToolbarButton editor={editor} label="❝" role="rte-secondary" aria="Quote" run={e => e.chain().focus().toggleBlockquote().run()} active={pressed.blockquote} />
        <ToolbarButton editor={editor} label="Code" role="rte-secondary" aria="Inline code" run={e => e.chain().focus().toggleCode().run()} active={pressed.code} />
        <ToolbarButton editor={editor} label="{ }" role="rte-secondary" aria="Code block" run={e => e.chain().focus().toggleCodeBlock().run()} active={pressed.codeBlock} />
        <ToolbarButton
          editor={editor}
          label="🔗 Link"
          compact="🔗"
          // 7 × 44px + 6 × 2px gaps = 320px, which fits at 360 but not at 320.
          role="rte-primary rte-secondary-narrow"
          aria="Link"
          run={e => {
            const existing = e.getAttributes('link').href as string | undefined
            // eslint-disable-next-line no-alert
            const url = window.prompt('Link URL', existing ?? 'https://')
            if (url === null) return
            if (url.trim() === '') {
              e.chain().focus().unsetLink().run()
              return
            }
            e.chain().focus().extendMarkRange('link').setLink({ href: url.trim() }).run()
          }}
          active={pressed.link}
        />
        <Button
          type="button"
          variant="text"
          color="inherit"
          data-testid="rte-toolbar-btn"
          data-role="rte-secondary"
          aria-label="Insert image"
          onClick={() => fileInputRef.current?.click()}
          sx={toolbarBtnSx}
        >
          <Box component="span" data-testid="rte-btn-full">🖼 Image</Box>
        </Button>
        {/* Mobile overflow control (AC-3). Each action renders exactly once; this un-hides secondaries in place
        so the expanded state never occludes the selection (AC-3c). `data-testid="rte-toolbar-btn"` is
        load-bearing: it supplies the 44px floor and the e2e touch-target sweep. Hidden while the whole set fits one row. */}
        <Button
          type="button"
          variant="text"
          color="inherit"
          data-testid="rte-toolbar-btn"
          data-role="rte-more"
          aria-label="More formatting"
          aria-expanded={moreOpen}
          onMouseDown={e => e.preventDefault()} // keep editor selection/focus intact on click
          onClick={() => setMoreOpen(v => !v)}
          sx={(theme: Theme) => ({
            ...toolbarBtnSx(theme),
            // Shown ≤480px or in a narrow editor (the toolbar's container rule); the 44px floor is repeated because a breakpoint key can be assigned only once.
            display: 'none',
            [theme.breakpoints.down(481)]: { minWidth: 44, minHeight: 44, display: 'inline-flex' },
          })}
        >
          <MoreHorizIcon />
        </Button>
      </Box>
      <Box
        component="input"
        ref={fileInputRef}
        type="file"
        accept="image/*"
        multiple
        aria-label="Insert image"
        // `e2e/tasks-form-no-gates.spec.ts` drives file selection through this handle.
        data-testid="rte-file-input"
        sx={{ display: 'none' }}
        onChange={handleFilesPicked}
      />
      {pasteError && <Banner severity="error" onDismiss={() => setPasteError('')}>{pasteError}</Banner>}
      <EditorContent editor={editor} className="rte-content" />
    </Box>
  )
}

export function copyFailedMessage(count: number): string {
  return count === 1
    ? 'Image not copied — no access, or deleted.'
    : `${count} images not copied — no access, or deleted.`
}

function ToolbarButton({
  editor,
  label,
  compact,
  role,
  aria,
  run,
  active,
}: {
  editor: Editor
  label: string
  // The ≤480px glyph for a primary action (AC-3(d)); omitted for a secondary.
  compact?: string
  // Space-separated role tokens matched with `~=`: "rte-primary", "rte-secondary", or (Link) both
  // "rte-primary" and "rte-secondary-narrow".
  role: string
  aria: string
  run: (editor: Editor) => void
  // From the editor's own state (`useEditorState` in the parent), never read here at render time.
  active: boolean
}) {
  return (
    <Button
      type="button"
      variant="text"
      color="inherit"
      data-testid="rte-toolbar-btn"
      data-role={role}
      aria-label={aria}
      aria-pressed={active}
      onMouseDown={e => e.preventDefault()} // keep editor selection/focus intact on click
      onClick={() => run(editor)}
      sx={toolbarBtnSx}
    >
      {/* The media query swaps which span shows; `aria-label` stays the accessible name (AC-3f). */}
      <Box component="span" data-testid="rte-btn-full">{label}</Box>
      {compact && (
        <Box component="span" data-testid="rte-btn-compact" aria-hidden="true" sx={{ display: 'none' }}>
          {compact}
        </Box>
      )}
    </Button>
  )
}
