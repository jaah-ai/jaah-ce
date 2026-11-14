import { useEffect, useMemo, useRef } from 'react'
import { ApiError, get, getBlob } from '../../api'
import { withRetryParam } from '../../net/href'
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

/* ONE PAINT CACHE PER FORM, for the images it staged (token → File, read through `lookup`): the task form, the
 * automation spec fields and the new-ticket page each paint pasted images before any row exists. The PROMISE of
 * the URL is cached, set before it settles, so two resolves of one token mint one URL and a release finds it
 * pending. STAGED-FIRST: `RichTextEditor`'s `insertImageFiles` inserts the node optimistically, so resolving an
 * own-staged token off the server raced the upload and latched "⚠ image unavailable" (permanently, via
 * `listPromise`'s cache); only a token the form did NOT stage goes to `fallback`. `resolve` is ONE identity reading
 * `lookup`/`fallback` through refs: tiptap reads the resolver once (`attachmentImage`'s `addNodeView()`), so a
 * later identity never reaches the node view. Every URL is released on unmount, or each pins its File for the
 * tab's lifetime (the task form is re-keyed on every Save). */
export function useStagedImagePaint(
  lookup: (token: string) => File | undefined,
  fallback?: AttachmentResolver,
): { resolve: AttachmentResolver; release: (token: string) => void } {
  const lookupRef = useRef(lookup)
  lookupRef.current = lookup
  const fallbackRef = useRef(fallback)
  fallbackRef.current = fallback
  const paintedRef = useRef<Map<string, Promise<string>>>(new Map())
  useEffect(() => () => {
    for (const url of paintedRef.current.values()) void url.then(releasePaintedUrl, () => {})
    paintedRef.current.clear()
  }, [])
  return useMemo(() => ({
    resolve: async (token: string) => {
      const painted = paintedRef.current
      const cached = painted.get(token)
      if (cached) return cached
      const file = lookupRef.current(token)
      if (file) {
        // Inert even for the uploader's own file (M9): `paintableImageUrl`.
        const url = paintableImageUrl(file, file.type)
        painted.set(token, url)
        return url
      }
      const fallback = fallbackRef.current
      if (!fallback) throw new Error(`no staged image for token: ${token}`)
      return fallback(token)
    },
    /** Drop one token's paint (a failed upload stored nothing under it). */
    release: (token: string) => {
      const url = paintedRef.current.get(token)
      if (!url) return
      void url.then(releasePaintedUrl, () => {})
      paintedRef.current.delete(token)
    },
  }), [])
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

  /* One attempt: the list (cached once it answers), then the bytes. `attempt` 1 asks with `ApiImg`'s cache-busting
   * `?_r=1` (`net/href.tsx::withRetryParam`), a new request on a fresh connection. */
  async function resolveOnce(token: string, attempt: number): Promise<string> {
    const attachments = await listAttachments()
    const match = attachments.find(a => a.filename === token)
    if (!match) throw new AttachmentNotFoundError(token)

    const contentUrl = `/api/tasks/${taskId}/attachments/${match.id}/content`
    const stored = { contentUrl, filename: match.filename }
    // The STORED type decides (the bucket answers with it too); refused before any fetch, since a
    // non-image is never painted. A row with no stored type falls back to the response's.
    if (match.content_type && !imageKind(match.content_type)) throw new NotAnImageError(stored)
    const blob = await getBlob(withRetryParam(contentUrl, attempt)).catch(err => {
      throw new Error(`failed to fetch attachment content: ${token}`, { cause: err })
    })
    const url = await paintableImageUrl(blob, match.content_type || blob.type, stored)
    // The one place the numeric id is known; recorded so the node view offers the STORED file.
    storedByObjectUrl.set(url, stored)
    return url
  }

  return async function resolve(token: string): Promise<string> {
    const cached = blobUrlCache.get(token)
    if (cached) return cached
    /* The same ONE automatic retry an `<img>` of an API path gets (`net/href.tsx::useRetriedUrl`): a dropped
     * connection on the first request must not latch "⚠ image unavailable". A refusal that asking again cannot
     * change (`isFinal`) is not retried. */
    let url: string
    try {
      url = await resolveOnce(token, 0)
    } catch (err) {
      if (isFinal(err)) throw err
      url = await resolveOnce(token, 1)
    }
    blobUrlCache.set(token, url)
    return url
  }
}

/** No stored attachment carries the token's filename. */
class AttachmentNotFoundError extends Error {
  constructor(token: string) {
    super(`attachment not found for token: ${token}`)
    this.name = 'AttachmentNotFoundError'
  }
}

/** A failure a second request would only repeat: not an image, no such attachment, or a 4xx answer other than
 *  408/429 (a timeout or a rate limit can pass). */
function isFinal(err: unknown): boolean {
  if (err instanceof NotAnImageError || err instanceof AttachmentNotFoundError) return true
  const cause = err instanceof ApiError ? err : err instanceof Error ? err.cause : undefined
  return cause instanceof ApiError && cause.status >= 400 && cause.status < 500
    && cause.status !== 408 && cause.status !== 429
}
