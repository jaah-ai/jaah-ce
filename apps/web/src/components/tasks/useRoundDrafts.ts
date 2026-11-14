import { useCallback, useEffect, useRef, useState } from 'react'
import { api, get, put } from '../../api'
import type { Attachment, TaskQuestion } from '../../types'

/* ── UNSENT ANSWERS, KEPT ON THE SERVER ──
 * What a person picked, typed and attached on the open rounds survives closing the task: each change is saved
 * (`PUT /api/tasks/{id}/questions/{qid}/draft`, `apps/api/app/routes/tasks/question_drafts.py`) after a short pause,
 * and the pages seed from `GET …/question-drafts` once on mount. Send resolves the rounds, which clears them
 * server-side. Unmount sends a save still waiting (leaving the page sends it with `keepalive`), and the next mount's read
 * waits for every save in flight, so a quick close-and-reopen cannot read what was there before the last edit. A failed save or read is not shown: it
 * must never stop the person answering, and Send posts what is on screen. */

/** A page's draft. `picked` is SOURCE indices into the round's `options`; `note` is the free-text answer on a
 *  round with no options, else the comment riding with the pick. */
export type Draft = { picked: number[]; note: string; files: Attachment[] }
export const EMPTY_DRAFT: Draft = { picked: [], note: '', files: [] }

type SavedDraft = { question_id: number; picked: number[]; note: string; files: Attachment[] }

const SAVE_DELAY_MS = 400

// Saves on the wire, across mounts, so a read never overtakes the write before it.
const inFlight = new Set<Promise<unknown>>()

/* The saved drafts of the rounds still open, picks the round no longer offers dropped. Exported for the unit test. */
export function restoredDrafts(saved: SavedDraft[], rounds: TaskQuestion[]): Record<number, Draft> {
  const out: Record<number, Draft> = {}
  for (const s of saved) {
    const round = rounds.find(r => r.id === s.question_id)
    if (!round) continue
    const count = round.options?.length ?? 0
    out[s.question_id] = { picked: s.picked.filter(i => i >= 0 && i < count), note: s.note, files: s.files }
  }
  return out
}

export function useRoundDrafts(taskId: number, rounds: TaskQuestion[], readOnly: boolean) {
  const [drafts, setDrafts] = useState<Record<number, Draft>>({})
  // The drafts as last rendered, plus every patch since (kept in step by `updateDraft`).
  const latest = useRef(drafts)
  latest.current = drafts
  const roundsRef = useRef(rounds)
  roundsRef.current = rounds

  // Read once per task; a page edited before the read landed keeps the edit.
  useEffect(() => {
    if (readOnly) return
    let live = true
    Promise.allSettled([...inFlight])
      .then(() => get<SavedDraft[]>(`/api/tasks/${taskId}/question-drafts`))
      .then(saved => {
        if (!live || !Array.isArray(saved)) return
        const restored = restoredDrafts(saved, roundsRef.current)
        setDrafts(prev => ({ ...restored, ...prev }))
      })
      .catch(() => undefined)
    return () => { live = false }
  }, [taskId, readOnly])

  // One waiting save per round; unmount sends whatever is still waiting.
  const pending = useRef(new Map<number, { timer: ReturnType<typeof setTimeout>; draft: Draft }>())
  // Each round's saves go out one after another, so an older one can never land last.
  const queues = useRef(new Map<number, Promise<unknown>>())
  const send = useCallback((questionId: number, leaving = false) => {
    const entry = pending.current.get(questionId)
    if (!entry) return
    clearTimeout(entry.timer)
    pending.current.delete(questionId)
    const { picked, note, files } = entry.draft
    const path = `/api/tasks/${taskId}/questions/${questionId}/draft`
    const body = { picked, note, attachment_ids: files.map(f => f.id) }
    if (leaving) {
      // The page is going away (reload, tab close): `keepalive` lets the request outlive it; no queue to wait on.
      void api(path, { method: 'PUT', body: JSON.stringify(body), keepalive: true }).catch(() => undefined)
      return
    }
    // Chained inside a promise, so a throw (or a stubbed `put`) lands in the same catch.
    const save = (queues.current.get(questionId) ?? Promise.resolve())
      .then(() => put(path, body))
      .catch(() => undefined)
    queues.current.set(questionId, save)
    inFlight.add(save)
    void save.finally(() => inFlight.delete(save))
  }, [taskId])
  useEffect(() => {
    const waiting = pending.current
    const leave = () => { for (const questionId of [...waiting.keys()]) send(questionId, true) }
    window.addEventListener('pagehide', leave)
    return () => {
      window.removeEventListener('pagehide', leave)
      for (const questionId of [...waiting.keys()]) send(questionId)
    }
  }, [send])

  const schedule = useCallback((questionId: number, draft: Draft) => {
    const prior = pending.current.get(questionId)
    if (prior) clearTimeout(prior.timer)
    pending.current.set(questionId, { draft, timer: setTimeout(() => send(questionId), SAVE_DELAY_MS) })
  }, [send])

  /* Merged into the LATEST draft, never one captured at render: an upload finishing must not undo a note typed
     while it ran. */
  const updateDraft = useCallback((questionId: number, patch: Partial<Draft>) => {
    setDrafts(prev => {
      const draft = { ...(prev[questionId] ?? EMPTY_DRAFT), ...patch }
      return { ...prev, [questionId]: draft }
    })
    if (readOnly) return
    // Outside the updater (React may run it twice); the pending entry is the latest merge, built the same way.
    const base = pending.current.get(questionId)?.draft ?? latest.current[questionId] ?? EMPTY_DRAFT
    schedule(questionId, { ...base, ...patch })
  }, [readOnly, schedule])

  return { drafts, updateDraft }
}
