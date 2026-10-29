/* What a comment thread (and its attachments) hangs off: a Task or a support Ticket. One component
 * (`components/tasks/TaskComments`, `TaskAttachmentsField`) serves both; only the route base differs,
 * and every suffix under it (`/comments`, `/mention-candidates`, `/attachments/…`) is the same. */

export type CommentSubject = { kind: 'task' | 'ticket'; id: number }

/** Who may read a ticket comment or attachment. Task rows never carry one. */
export type CommentVisibility = 'public' | 'internal'

export function subjectBase(subject: CommentSubject): string {
  return subject.kind === 'ticket' ? `/api/support/tickets/${subject.id}` : `/api/tasks/${subject.id}`
}
