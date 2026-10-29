// A duplicate create's seed, as the form and its warning line read it: the copied body and what the copy lacked.
import { stripAttachmentImage } from '../../components/tasks/TaskEditForm'
import { deadInlineTokens, type TaskDuplicateSeed } from '../../lib/taskDuplicate'

/** The copied row's description, with every inline image whose bytes did not come across stripped. */
export function duplicateBody(seed: TaskDuplicateSeed): string {
  /* Strip inline images whose bytes did not come across, so the saved copy does not render a permanent
     broken image (`stripAttachmentImage`). */
  let body = seed.source.body ?? ''
  for (const token of deadInlineTokens(seed)) body = stripAttachmentImage(body, token)
  return body
}

/* What a copy fell short of, in words — or `''` when it carried everything. Two distinct claims:
 *   · `attachmentsUnknown` — the list would not load, so we cannot say what is missing.
 *   · `unavailable` — the list NAMED these and their bytes would not come back (naming makes it actionable).
 * Neither is an error: both are a warning over a working form. */
export function duplicateShortfall(sourceId: number, seed: TaskDuplicateSeed): string {
  if (seed.attachmentsUnknown) {
    return `Copied task #${sourceId}, but its attachments could not be listed — `
      + 'none of them are included, and any image in the description will be missing.'
  }
  if (seed.unavailable.length === 0) return ''
  return `Copied task #${sourceId}, but ${seed.unavailable.length} attachment(s) could not be read `
    + `and are not included: ${seed.unavailable.join(', ')}.`
}
