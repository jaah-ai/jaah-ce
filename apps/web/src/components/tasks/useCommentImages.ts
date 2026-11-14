/* The images one comment draft carries, staged before the comment exists. Each pasted/dropped file
 * uploads AT ONCE (`uploadCommentImage`), so Send only has ids to hand over (`image_ids`); the server
 * binds them to the comment. One instance per draft — the new comment and the in-place edit each own one.
 * ── Refusals ──
 * Type, size and count are checked here first so a wrong file never costs a round trip; the routes
 * re-decide and are the authority (the numbers are projected from them: `generated/inlinePreviewTypes`).
 * Reported through `onError` (the host's one error line), never thrown.
 * Staging, previews and their lifetime are `hooks/useStagedUploads`.
 */
import { useCallback } from 'react'

import {
  COMMENT_IMAGE_CONTENT_TYPES,
  MAX_COMMENT_IMAGE_SIZE,
  MAX_COMMENT_IMAGES,
} from '../../generated/inlinePreviewTypes'
import { type StagedUpload, useStagedUploads } from '../../hooks/useStagedUploads'
import { formatBytes } from '../../lib/formatBytes'
import { uploadCommentImage } from '../../lib/uploadTaskAttachment'
import type { TaskCommentImage } from '../../types'

/** One thumbnail in the composer: a `blob:` URL for a fresh pick, the content route's API path for one
 *  the comment already has (`ApiImg`). */
export type StagedImage = StagedUpload & { src: string }

/** A comment image's API path, for `ApiImg` and `apiLinkHref` (`net/href.tsx`, rewritten on the live host). */
export function commentImagePath(taskId: number, imageId: number): string {
  return `/api/tasks/${taskId}/attachments/${imageId}/content`
}

export function useCommentImages(taskId: number, onError: (message: string) => void) {
  const { files, uploading, add, remove, prune, replace } = useStagedUploads({
    refuse: (file, staged) => {
      if (!COMMENT_IMAGE_CONTENT_TYPES.includes(file.type)) return { message: `${file.name}: PNG, JPEG, GIF or WebP only` }
      if (file.size > MAX_COMMENT_IMAGE_SIZE) return { message: `${file.name}: over ${formatBytes(MAX_COMMENT_IMAGE_SIZE)}` }
      return staged >= MAX_COMMENT_IMAGES ? { message: `At most ${MAX_COMMENT_IMAGES} images`, stop: true } : null
    },
    upload: file => uploadCommentImage(taskId, file),
    failure: () => 'Image upload failed',
    onError,
  })

  /** Drops the images a send carried (by id); any pasted while it was in flight stay. */
  const clear = useCallback((sent: readonly number[]) => {
    prune(image => image.id === null || !sent.includes(image.id))
  }, [prune])

  /** Empties the draft (edit cancelled), or seeds it with a comment's own images (entering edit). */
  const reset = useCallback((existing: readonly TaskCommentImage[] = []) => {
    prune(() => false)
    replace(existing.map(image => ({
      key: `saved-${image.id}`,
      filename: image.filename,
      contentType: image.content_type,
      src: commentImagePath(taskId, image.id),
      id: image.id,
      status: 'ready',
    })))
  }, [taskId, prune, replace])

  const images = files as StagedImage[]
  const ids = images.flatMap(image => (image.status === 'ready' && image.id !== null ? [image.id] : []))
  return { images, ids, uploading, add, remove, clear, reset }
}
