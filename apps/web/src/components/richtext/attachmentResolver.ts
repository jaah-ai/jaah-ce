import { apiFetch, get } from '../../api'
import type { Attachment } from '../../types'

// `attachment:<token>` → displayable `blob:` URL via the user-session content route
// (GET /api/tasks/{taskId}/attachments/{attachmentId}/content). The body stores only the token filename,
// so one list fetch (GET /api/tasks/{taskId}/attachments) maps filename → id first.
// List and blob URLs are cached per resolver instance, so re-renders never re-fetch.
export const ATTACHMENT_SCHEME = 'attachment:'

export function isAttachmentSrc(src: string): boolean {
  return src.startsWith(ATTACHMENT_SCHEME)
}

export function attachmentToken(src: string): string {
  return src.slice(ATTACHMENT_SCHEME.length)
}

export type AttachmentResolver = (token: string) => Promise<string>

/* PAINT AN UPLOAD WITHOUT LETTING IT RUN ON THE CONSOLE'S ORIGIN (white-box finding M9). A `blob:` URL has
 * the ORIGIN OF THE PAGE THAT MINTED IT and the type its Blob carries, so a stored `text/html` or
 * `image/svg+xml` minted as-is runs as the console the moment someone opens the image in a new tab —
 * and any task attachment can be named by `attachment:<token>` in a body, whatever its type. So:
 *  * raster (png/jpeg/gif/webp/avif/bmp/ico) → a blob RE-TYPED to that raster type: the browser can only
 *    paint it;
 *  * svg → a `data:` URL: a browser refuses to navigate a tab to one, and a data: document has an
 *    opaque origin — it paints in the `<img>` (CSP `img-src data:`) and reaches nothing;
 *  * anything else → `NotAnImageError`: the node view shows the stored file's download link instead.
 * The console CSP (`apps/web/nginx-console-csp.conf`), which a blob: document inherits, is the second layer. */
const RASTER_TYPES = new Set([
  'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'image/bmp', 'image/x-icon',
  'image/vnd.microsoft.icon',
])
const SVG_TYPE = 'image/svg+xml'

/** `'raster'`/`'svg'` for a paintable media type (parameters and case ignored), else `null`. */
export function imageKind(type: string | null | undefined): 'raster' | 'svg' | null {
  const media = (type ?? '').split(';')[0].trim().toLowerCase()
  if (RASTER_TYPES.has(media)) return 'raster'
  return media === SVG_TYPE ? 'svg' : null
}

/** Thrown for bytes that are not a paintable image; carries the stored file when there is one. */
export class NotAnImageError extends Error {
  constructor(readonly stored?: StoredAttachment) {
    super('not a paintable image')
    this.name = 'NotAnImageError'
  }
}

/** A URL that paints `bytes` as the image `type` names and can run nothing on this origin (see above);
 *  a refusal carries `stored`, the file the node view offers instead. */
export async function paintableImageUrl(
  bytes: Blob,
  type: string | null | undefined,
  stored?: StoredAttachment,
): Promise<string> {
  const kind = imageKind(type)
  if (kind === null) throw new NotAnImageError(stored)
  const media = (type ?? '').split(';')[0].trim().toLowerCase()
  const typed = new Blob([bytes], { type: media })
  const url = kind === 'raster'
    ? URL.createObjectURL(typed)
    : await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(reader.error ?? new Error('could not read the SVG'))
      reader.readAsDataURL(typed)
    })
  bytesByPaintedUrl.set(url, typed)
  return url
}

/* Painted URL → the bytes it paints. A COPIED image whose source is a painted URL (one staged in a form,
 * with no stored row yet) is re-read from here: the console CSP's `connect-src` refuses `fetch` of a
 * `blob:`/`data:` URL (`attachmentCopy.ts`). Dropped with the URL in `releasePaintedUrl`. */
const bytesByPaintedUrl = new Map<string, Blob>()

/** The bytes behind a URL this tab painted, or `undefined`. */
export function paintedBytes(url: string): Blob | undefined {
  return bytesByPaintedUrl.get(url)
}

/** Release a URL `paintableImageUrl` minted: a `blob:` pins its bytes until revoked; a `data:` holds nothing. */
export function releasePaintedUrl(url: string): void {
  bytesByPaintedUrl.delete(url)
  if (url.startsWith('blob:')) URL.revokeObjectURL(url)
}

/** The STORED file behind a resolved image — what a "save this" control needs and a `blob:` URL lacks. */
export interface StoredAttachment {
  /** The content route; same-origin, so the cookie rides a plain anchor. */
  contentUrl: string
  /** The stored filename (the `attachment:` token, as the route's `Content-Disposition` sends it). */
  filename: string
}

/* Painted (`blob:`/`data:`) URL → the stored file it was minted from. A module-scoped side table because the resolved STRING
 * is the only thing that reaches the image node view (via `extensions.ts` → `AttachmentImage.configure`,
 * with no task id), and composed resolvers (`TaskEditForm`'s staged-first wrapper) forward only that
 * string. Keyed on the unique `blob:` URL, not the token (tokens could repeat across tasks). No new
 * lifetime: the blob URLs are never revoked anyway. STAGED images are absent on purpose: no server row, so
 * nothing to save. */
const storedByObjectUrl = new Map<string, StoredAttachment>()

/** The stored file for a resolved src, or `undefined` for anything not resolved off the server. */
export function storedAttachmentFor(src: string | null | undefined): StoredAttachment | undefined {
  return src ? storedByObjectUrl.get(src) : undefined
}

export function createAttachmentResolver(taskId: number): AttachmentResolver {
  let listPromise: Promise<Attachment[]> | null = null
  const blobUrlCache = new Map<string, string>()

  function listAttachments(): Promise<Attachment[]> {
    if (!listPromise) {
      listPromise = get<Attachment[]>(`/api/tasks/${taskId}/attachments`).catch(err => {
        listPromise = null // a transient failure shouldn't poison every future resolve
        throw err
      })
    }
    return listPromise
  }

  return async function resolve(token: string): Promise<string> {
    const cached = blobUrlCache.get(token)
    if (cached) return cached

    const attachments = await listAttachments()
    const match = attachments.find(a => a.filename === token)
    if (!match) throw new Error(`attachment not found for token: ${token}`)

    const contentUrl = `/api/tasks/${taskId}/attachments/${match.id}/content`
    const stored = { contentUrl, filename: match.filename }
    // The STORED type decides (the bucket answers with it too); refused before any fetch, since a
    // non-image is never painted. A row with no stored type falls back to the response's.
    if (match.content_type && !imageKind(match.content_type)) throw new NotAnImageError(stored)
    const resp = await apiFetch(contentUrl, { credentials: 'same-origin' })
    if (!resp.ok) throw new Error(`failed to fetch attachment content: ${token}`)
    const blob = await resp.blob()
    const url = await paintableImageUrl(blob, match.content_type || blob.type, stored)
    blobUrlCache.set(token, url)
    // The one place the numeric id is known; recorded so the node view offers the STORED file.
    storedByObjectUrl.set(url, stored)
    return url
  }
}
