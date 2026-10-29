import { Fragment, type Node as ProseMirrorNode, Slice } from '@tiptap/pm/model'
import { Mapping } from '@tiptap/pm/transform'
import type { Transaction } from '@tiptap/pm/state'
import type { Editor } from '@tiptap/core'
import { nanoid } from 'nanoid'
import { apiFetch } from '../../api'
import {
  type AttachmentResolver,
  attachmentToken,
  imageKind,
  isAttachmentSrc,
  paintedBytes,
  storedAttachmentFor,
} from './attachmentResolver'

/* ── COPYING AN `attachment:` IMAGE FROM ONE BODY TO ANOTHER ──
 *
 * A body stores an image as `attachment:<token>`, and the token means something only to the task (or
 * ticket) whose attachment list holds it. Copied into another body as-is it resolves to nothing:
 * `⚠ image unavailable`. So a copied image carries WHERE ITS BYTES ARE, and a paste somewhere that cannot
 * resolve the token to those same bytes fetches them and stores its OWN copy under a fresh token — the
 * destination then stands alone, whatever happens to the source (operator decision).
 *
 *  1. Every image node view records `token → source` for its editor as it resolves (`recordImageSource`).
 *  2. A copy (`transformCopied`, `attachmentImage.ts`) stamps that source onto the clipboard's image nodes
 *     as `data-attachment-source` (the node's `source` attribute; never in markdown).
 *  3. A paste (`RichTextEditor`'s `handlePaste`) re-homes each stamped image here (`pasteCopiedImages`).
 *
 * The source is clipboard input, which any web page can write, so a pasted one is honoured only on the
 * allowlist below (`isCopyableSource`): our own attachment content routes, this tab's own painted URLs
 * (read from memory — the CSP refuses a `blob:`/`data:` fetch). The fetch runs as the viewer, and only
 * bytes the server types as an image are copied. */

/** The node attribute, and its clipboard HTML attribute. */
export const SOURCE_ATTR = 'data-attachment-source'

const CONTENT_ROUTES = [
  /^\/api\/tasks\/\d+\/attachments\/\d+\/content$/,
  /^\/api\/support\/tickets\/\d+\/attachments\/\d+\/content$/,
]

/** Whether a source names bytes this console may fetch to copy an image: an attachment content route,
 *  a `blob:` this origin minted, or an image `data:` URL (`paintableImageUrl`'s SVG form). */
export function isCopyableSource(source: string): boolean {
  if (CONTENT_ROUTES.some(route => route.test(source))) return true
  if (source.startsWith(`blob:${window.location.origin}/`)) return true
  return source.startsWith('data:image/')
}

/** The source of a URL a resolver returned: the STORED file behind a painted task image, else the URL
 *  itself (a ticket's content route, or a staged image's `blob:`/`data:`). */
export function sourceOfResolved(url: string): string {
  return storedAttachmentFor(url)?.contentUrl ?? url
}

/* Per editor, so the same token in two bodies (a duplicated task keeps its source's filenames) never
 * answers for the wrong one. Entries outlive their node, so a cut then paste still knows its source. */
const sourcesByEditor = new WeakMap<Editor, Map<string, string>>()

export function recordImageSource(editor: Editor, token: string, resolvedUrl: string): void {
  const source = sourceOfResolved(resolvedUrl)
  if (!isCopyableSource(source)) return
  let sources = sourcesByEditor.get(editor)
  if (!sources) sourcesByEditor.set(editor, (sources = new Map()))
  sources.set(token, source)
}

/** Rebuild `fragment` with every image node replaced by `fn(node)` (`null` drops it), in document order. */
export function mapImageNodes(
  fragment: Fragment,
  fn: (node: ProseMirrorNode) => ProseMirrorNode | null,
): Fragment {
  const out: ProseMirrorNode[] = []
  fragment.forEach(node => {
    if (node.type.name === 'image') {
      const next = fn(node)
      if (next) out.push(next)
    } else {
      if (node.isLeaf) return void out.push(node)
      const content = mapImageNodes(node.content, fn)
      // A parent emptied by a dropped image, where its content rule forbids empty (a blockquote), goes too.
      if (content.size === 0 && node.content.size > 0 && !node.type.validContent(content)) return
      out.push(node.copy(content))
    }
  })
  return Fragment.fromArray(out)
}

/** The copy half: stamp each `attachment:` image in a copied slice with where its bytes are. */
export function stampCopiedSources(slice: Slice, editor: Editor): Slice {
  const sources = sourcesByEditor.get(editor)
  if (!sources) return slice
  const content = mapImageNodes(slice.content, node => {
    const src = String(node.attrs.src ?? '')
    const source = isAttachmentSrc(src) ? sources.get(attachmentToken(src)) : undefined
    return source ? node.type.create({ ...node.attrs, source }, node.content, node.marks) : node
  })
  return new Slice(content, slice.openStart, slice.openEnd)
}

/** Whether a pasted slice carries a copied image this paste has to re-home. */
export function hasCopiedImages(slice: Slice): boolean {
  let found = false
  mapImageNodes(slice.content, node => {
    if (isAttachmentSrc(String(node.attrs.src ?? '')) && node.attrs.source) found = true
    return node
  })
  return found
}

// `img-<nanoid>.<ext>`, embedded in both the markdown body and the uploaded filename, so they stay
// linked with no body rewrite.
export function mintImageFilename(file: File): string {
  const name = file.name || ''
  const dot = name.lastIndexOf('.')
  const extFromName = dot > 0 && dot < name.length - 1 ? name.slice(dot + 1) : ''
  const extFromType = file.type.split('/')[1] ?? ''
  const ext = (extFromName || extFromType || 'png').toLowerCase().replace(/[^a-z0-9]/g, '') || 'png'
  return `img-${nanoid(10)}.${ext}`
}

/** The copied image's bytes as a `File` named after its old token. Throws when they cannot be read or
 *  are not an image. */
async function fetchCopiedImage(source: string, token: string): Promise<File> {
  let blob: Blob
  if (source.startsWith('/api/')) {
    const resp = await apiFetch(source, { credentials: 'same-origin' })
    if (!resp.ok) throw new Error(`could not read ${token}`)
    blob = await resp.blob()
  } else {
    // A painted URL of THIS tab; one minted elsewhere (another tab, since released) is gone.
    const painted = paintedBytes(source)
    if (!painted) throw new Error(`${token} is no longer in this tab`)
    blob = painted
  }
  if (!imageKind(blob.type)) throw new Error(`${token} is not an image`)
  return new File([blob], token, { type: blob.type })
}

/** Whether `token` already resolves HERE to the copied bytes (a cut and paste within one body, or a copy
 *  between two views of the same task): then the paste keeps it rather than storing a duplicate. */
async function resolvesHereTo(token: string, source: string, resolve?: AttachmentResolver): Promise<boolean> {
  if (!resolve) return false
  try {
    return sourceOfResolved(await resolve(token)) === source
  } catch {
    return false
  }
}

type Plan = { keep: true } | { keep: false; file: File; filename: string } | null

/** The paste (or drop) half. Each copied image the destination cannot already resolve gets its bytes
 *  fetched and a fresh token; one that cannot be copied is left out. Inserts the slice over `from`–`to`
 *  (where the paste happened, mapped through any edit made while the bytes were read), THEN hands each new
 *  image to `onUploadImage` (a pasted file's order: the node first, so an over-cap strip finds it).
 *  Returns how many images were left out. */
export async function pasteCopiedImages(
  editor: Editor,
  slice: Slice,
  range: { from: number; to: number },
  resolve: AttachmentResolver | undefined,
  onUploadImage: (file: File, filename: string) => Promise<void>,
): Promise<number> {
  const mapping = new Mapping()
  const track = ({ transaction }: { transaction: Transaction }) => mapping.appendMapping(transaction.mapping)
  editor.on('transaction', track)
  try {
    return await rehome(editor, slice, range, mapping, resolve, onUploadImage)
  } finally {
    editor.off('transaction', track)
  }
}

async function rehome(
  editor: Editor,
  slice: Slice,
  range: { from: number; to: number },
  mapping: Mapping,
  resolve: AttachmentResolver | undefined,
  onUploadImage: (file: File, filename: string) => Promise<void>,
): Promise<number> {
  const copied: ProseMirrorNode[] = []
  mapImageNodes(slice.content, node => {
    if (isAttachmentSrc(String(node.attrs.src ?? '')) && node.attrs.source) copied.push(node)
    return node
  })
  const plans: Plan[] = await Promise.all(copied.map(async (node): Promise<Plan> => {
    const token = attachmentToken(String(node.attrs.src))
    const source = String(node.attrs.source)
    if (!isCopyableSource(source)) return null
    if (await resolvesHereTo(token, source, resolve)) return { keep: true }
    try {
      const file = await fetchCopiedImage(source, token)
      return { keep: false, file, filename: mintImageFilename(file) }
    } catch {
      return null
    }
  }))
  if (editor.isDestroyed) return 0

  let at = 0
  const uploads: { file: File; filename: string }[] = []
  const content = mapImageNodes(slice.content, node => {
    if (!(isAttachmentSrc(String(node.attrs.src ?? '')) && node.attrs.source)) return node
    const plan = plans[at++]
    if (plan === null) return null
    if (plan.keep) return node.type.create({ ...node.attrs, source: null }, node.content, node.marks)
    uploads.push(plan)
    return node.type.create(
      { ...node.attrs, src: `attachment:${plan.filename}`, source: null }, node.content, node.marks,
    )
  })
  const { view } = editor
  const from = mapping.map(range.from, -1)
  const to = Math.max(from, mapping.map(range.to, 1))
  // The metas ProseMirror's own paste sets, so paste rules and listeners treat this as one.
  view.dispatch(
    view.state.tr.replaceRange(from, to, new Slice(content, slice.openStart, slice.openEnd))
      .setMeta('paste', true).setMeta('uiEvent', 'paste').scrollIntoView(),
  )
  for (const { file, filename } of uploads) void onUploadImage(file, filename)
  return plans.filter(plan => plan === null).length
}
