/* The images one comment draft carries, staged before the comment exists. Each pasted/dropped file
 * uploads AT ONCE (`uploadCommentImage`), so Send only has ids to hand over (`image_ids`); the server
 * binds them to the comment. One instance per draft — the new comment and the in-place edit each own one.
 * ── Refusals ──
 * Type, size and count are checked here first so a wrong file never costs a round trip; the routes
 * re-decide and are the authority (the numbers are projected from them: `generated/inlinePreviewTypes`).
 * Reported through `onError` (the host's one error line), never thrown.
 * ── Lifetime ──
 * A local `blob:` preview is revoked when its row leaves (remove/reset/clear) and on unmount. An upload
 * that lands after its row was removed is dropped: its id reaches no comment.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { apiDetail, apiHref } from '../../api'
import {
  COMMENT_IMAGE_CONTENT_TYPES,
  MAX_COMMENT_IMAGE_SIZE,
  MAX_COMMENT_IMAGES,
} from '../../generated/inlinePreviewTypes'
import { formatBytes } from '../../lib/formatBytes'
import { uploadCommentImage } from '../../lib/uploadTaskAttachment'
import type { TaskCommentImage } from '../../types'

/** One thumbnail in the composer. `id` is null until the confirm answers. */
export type StagedImage = {
  key: string
  filename: string
  /** A `blob:` URL for a fresh pick; the content route for one the comment already has. */
  src: string
  id: number | null
  status: 'uploading' | 'ready' | 'failed'
}

/** Same-origin, so the session cookie rides a plain `<img>`/`<a>` (the live host's public route, via `apiHref`). */
export function commentImageUrl(taskId: number, imageId: number): string {
  return apiHref(`/api/tasks/${taskId}/attachments/${imageId}/content`)
}

function refusal(file: File): string | null {
  if (!COMMENT_IMAGE_CONTENT_TYPES.includes(file.type)) return `${file.name}: PNG, JPEG, GIF or WebP only`
  if (file.size > MAX_COMMENT_IMAGE_SIZE) return `${file.name}: over ${formatBytes(MAX_COMMENT_IMAGE_SIZE)}`
  return null
}

function release(image: StagedImage): void {
  if (image.src.startsWith('blob:')) URL.revokeObjectURL(image.src)
}

export function useCommentImages(taskId: number, onError: (message: string) => void) {
  const [images, setImages] = useState<StagedImage[]>([])
  const nextKey = useRef(0)
  /* The rows as of the last render plus any `add` staged since: the count cap and the unmount revoke
  both need them outside a state updater. */
  const latest = useRef(images)
  latest.current = images

  useEffect(() => () => latest.current.forEach(release), [])

  const add = useCallback((files: readonly File[]) => {
    for (const file of files) {
      const refused = refusal(file)
      if (refused) {
        onError(refused)
        continue
      }
      if (latest.current.length >= MAX_COMMENT_IMAGES) {
        onError(`At most ${MAX_COMMENT_IMAGES} images`)
        break
      }
      const key = `new-${++nextKey.current}`
      const staged: StagedImage = {
        key,
        // The clipboard names every pasted image the same; the name is only the alt text.
        filename: file.name || 'image.png',
        src: URL.createObjectURL(file),
        id: null,
        status: 'uploading',
      }
      latest.current = [...latest.current, staged]
      setImages(current => [...current, staged])
      uploadCommentImage(taskId, file).then(
        attachment => setImages(current => current.map(image =>
          image.key === key ? { ...image, id: attachment.id, status: 'ready' } : image)),
        err => {
          onError(apiDetail(err, 'Image upload failed'))
          setImages(current => current.map(image => image.key === key ? { ...image, status: 'failed' } : image))
        },
      )
    }
  }, [taskId, onError])

  const remove = useCallback((key: string) => {
    setImages(current => {
      current.filter(image => image.key === key).forEach(release)
      return current.filter(image => image.key !== key)
    })
  }, [])

  /** Drops the images a send carried (by id); any pasted while it was in flight stay. */
  const clear = useCallback((sent: readonly number[]) => {
    setImages(current => {
      const gone = (image: StagedImage) => image.id !== null && sent.includes(image.id)
      current.filter(gone).forEach(release)
      return current.filter(image => !gone(image))
    })
  }, [])

  /** Empties the draft (edit cancelled), or seeds it with a comment's own images (entering edit). */
  const reset = useCallback((existing: readonly TaskCommentImage[] = []) => {
    setImages(current => {
      current.forEach(release)
      return existing.map(image => ({
        key: `saved-${image.id}`,
        filename: image.filename,
        src: commentImageUrl(taskId, image.id),
        id: image.id,
        status: 'ready' as const,
      }))
    })
  }, [taskId])

  const ids = images.flatMap(image => (image.status === 'ready' && image.id !== null ? [image.id] : []))
  const uploading = images.some(image => image.status === 'uploading')
  return { images, ids, uploading, add, remove, clear, reset }
}
