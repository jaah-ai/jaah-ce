import { ApiError, apiDetail, post } from '../../api'
import type { PipelineTask } from '../../types'
import type { RoundSend } from './GateSteps'

/* A round's answer as `GateSteps` hands it to the host (`RoundSend`, mapped by `roundAnswer.ts`): TEXT (an
   option label verbatim, or prose) or a multi-choice round's TICK SET. A union, not a fourth argument: the
   route takes `answer` XOR `selections` (422 on both). The EMPTY ARRAY IS A VALUE ("none apply", recorded
   with a sentinel resolution — not a skip), so every hop branches on TYPE, never length. */
export type GateAnswer = string | { selections: string[] }

/** The answer half of a batch entry: `answer` XOR `selections` (the route 422s on both), then the optional
 * comment and files. Branch on the payload's TYPE, not its content: an EMPTY `selections` is a valid answer
 * ("none apply"), which a truthiness test would drop or turn into a 422. */
function roundBody(answer: GateAnswer, comment?: string, attachmentIds?: number[]) {
  return {
    ...(typeof answer !== 'string'
      ? { selections: answer.selections }
      // An empty answer with files is a file-only answer: omitted, and the server words it.
      : answer || !attachmentIds ? { answer } : {}),
    ...(comment ? { comment } : {}),
    ...(attachmentIds ? { attachment_ids: attachmentIds } : {}),
  }
}

/** One entry's fate in a batch, in the order sent; `failed` carries its own route's status and detail. */
export type RoundResult = {
  question_id: number
  outcome: 'answered' | 'dismissed' | 'failed'
  status_code: number | null
  detail: unknown
}

/** Answer and skip several open rounds in ONE call, POST /tasks/{id}/input-requests/answers. Best-effort on the
 * server: each entry is saved or refused on its own, and the runner is woken once with everything saved. */
export function postRoundAnswers(
  taskId: number, sends: RoundSend[],
): Promise<{ task: PipelineTask; results: RoundResult[] }> {
  return post(`/api/tasks/${taskId}/input-requests/answers`, {
    answers: sends.map(s => s.ignore
      ? { question_id: s.questionId, ignore: true }
      : { question_id: s.questionId, ...roundBody(s.answer, s.comment, s.attachmentIds) }),
  })
}

/** The refused entries of a batch as one short line the form can show, or '' when every entry was saved. A
 * server fault (5xx) has no reason the person can act on, so it reads as the plain "send again"; a refusal
 * (4xx) keeps its own route's reason, which says what to change. */
export function failedRoundsMessage(results: RoundResult[]): string {
  const failed = results.filter(r => r.outcome === 'failed')
  if (failed.length === 0) return ''
  const head = failed.length === 1 ? '1 answer wasn’t saved' : `${failed.length} answers weren’t saved`
  const refused = failed.filter(r => r.status_code !== null && r.status_code < 500)
  const again = refused.length < failed.length ? ' Press Send again.' : ''
  if (refused.length === 0) return `${head}.${again}`
  // Each detail read as its own route's refusal would be (`apiDetail`: a string, a `message`, a validation list).
  const why = refused.map(r => apiDetail(new ApiError(r.status_code ?? 0, r.detail), `error ${r.status_code ?? ''}`.trim()))
  // One closing stop, whether or not the last reason brought its own.
  return `${head}: ${why.join('; ').replace(/[.!?]$/, '')}.${again}`
}

/** Throw the batch's refused entries as the form's whole error. An `ApiError` so `apiDetail` shows the line as is,
 * not behind the caller's "Failed to send answers" (which would say the same thing twice). */
export function throwIfRoundsFailed(results: RoundResult[]): void {
  const message = failedRoundsMessage(results)
  if (message) throw new ApiError(0, message)
}
