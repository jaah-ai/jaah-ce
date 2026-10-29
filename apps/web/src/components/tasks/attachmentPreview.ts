import { MAX_PROXY_BYTES, PREVIEW_SUFFIX_TYPES } from '../../generated/inlinePreviewTypes'
import { TEXT_PREVIEW_MAX_BYTES } from './artifactKinds'

/** How the full-screen viewer (`AttachmentViewer`) renders a human's attachment. */
export type AttachmentPreviewMode = 'image' | 'pdf' | 'text'

/** The viewer's renderer for a file, or `null` when it only downloads. Decided by the NAME, as the server decides
 *  what `?preview=1` serves (`PREVIEW_SUFFIX_TYPES`, generated). Text past `TEXT_PREVIEW_MAX_BYTES` is not decoded
 *  into a string. A PDF past `MAX_PROXY_BYTES` would be a presigned S3 URL in a FRAME, which the console's
 *  `frame-src` refuses (the Artifacts tab's same rule, `artifactKinds.ts`); an image is fine there (`img-src`). */
export function attachmentPreviewMode(filename: string, sizeBytes: number): AttachmentPreviewMode | null {
  const dot = filename.lastIndexOf('.')
  const served = dot > 0 ? PREVIEW_SUFFIX_TYPES[filename.slice(dot).toLowerCase()] : undefined
  if (served === undefined) return null
  if (served.startsWith('image/')) return 'image'
  if (served === 'application/pdf') return sizeBytes <= MAX_PROXY_BYTES ? 'pdf' : null
  return sizeBytes <= TEXT_PREVIEW_MAX_BYTES ? 'text' : null
}
