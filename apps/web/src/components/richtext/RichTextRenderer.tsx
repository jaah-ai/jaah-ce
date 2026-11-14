import { EditorContent, useEditor } from '@tiptap/react'
import { useEffect } from 'react'
import { buildExtensions } from './extensions'
import { splitMachineMarkers } from './machineMarkers'
import type { AttachmentResolver } from './attachmentResolver'

// Read-only counterpart to RichTextEditor on the SAME extensions (`buildExtensions`, `editable: false`,
// AC-9): a real ProseMirror instance, not raw HTML injection, so only schema nodes render and a hostile
// paste stays inert text (`html: false`, extensions.ts).
export interface RichTextRendererProps {
  content: string
  resolveAttachmentSrc?: AttachmentResolver
  /** See `buildExtensions`. Off for a documentation reader, where bare filenames are not links. */
  linkify?: boolean
  /** Written by someone other than the viewer (a tenant's ticket description): only `attachment:` images
   *  render and links show as plain-text URLs. See `extensions.ts`' `UntrustedContent`. */
  untrusted?: boolean
}

export default function RichTextRenderer({
  content,
  resolveAttachmentSrc,
  linkify,
  untrusted,
}: RichTextRendererProps) {
  // A system body's machine markers are for its writer's matching, not for reading (`machineMarkers.ts`).
  const shown = splitMachineMarkers(content).visible
  const editor = useEditor({
    extensions: buildExtensions({ editable: false, resolveAttachmentSrc, linkify, untrusted }),
    content: shown,
    editable: false,
    editorProps: {
      attributes: { class: 'rte-prosemirror rte-prosemirror-readonly', 'aria-label': 'Rendered content' },
    },
  })

  useEffect(() => {
    // See RichTextEditor.tsx's onUpdate comment: `storage.markdown` guard is a StrictMode dev-only race.
    if (editor && editor.storage.markdown && editor.storage.markdown.getMarkdown() !== shown) {
      editor.commands.setContent(shown, { emitUpdate: false })
    }
  }, [shown, editor])

  // No manual `editor?.destroy()` cleanup — see RichTextEditor.tsx's comment: `useEditor` already owns
  // the create/destroy lifecycle, including the StrictMode-safe deferred destroy.

  if (!editor) return null
  return <EditorContent editor={editor} className="rte-content" />
}
