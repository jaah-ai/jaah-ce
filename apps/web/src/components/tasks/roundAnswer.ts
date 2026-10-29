import { post } from '../../api'
import type { PipelineTask } from '../../types'
import type { GateAnswer } from './GateForm'

/** Answer one OPEN round via POST /tasks/{id}/input-requests/{questionId}/answer; the path's id pins the exact
 * round so a concurrent new one cannot be answered by mistake. Without an id it falls back to the legacy
 * POST /tasks/{id}/answer-question, which targets the oldest open round. `answer` is text or a `multi_select`
 * tick set; the route takes `answer` XOR `selections` (422 on both). Shared by the task page and the console's
 * Chat view so both send the same body. */
export function postRoundAnswer(
  taskId: number, answer: GateAnswer, questionId?: number, comment?: string, attachmentIds?: number[],
): Promise<PipelineTask> {
  const path = questionId !== undefined
    ? `/api/tasks/${taskId}/input-requests/${questionId}/answer`
    : `/api/tasks/${taskId}/answer-question`
  return post<PipelineTask>(path, {
    /* Branch on the payload's TYPE, not its content: an EMPTY `selections` is a valid answer ("none
       apply"), which a truthiness test would drop or turn into a 422. */
    ...(typeof answer !== 'string'
      ? { selections: answer.selections }
      // An empty answer with files is a file-only answer: omitted, and the server words it.
      : answer || !attachmentIds ? { answer } : {}),
    ...(comment ? { comment } : {}),
    ...(attachmentIds ? { attachment_ids: attachmentIds } : {}),
  })
}
