import { useCallback, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { apiDetail, get, put } from '../api'
import {
  byName,
  type AssignmentDraft,
  type AssignmentSide,
  type AssignmentsResponse,
  assignmentsBody,
  assignmentsPath,
  draftFromResponse,
  outcomeNote,
} from '../lib/assignments'
import { COMPANY_SETUP_QUERY } from '../lib/companySetup'
import { WORKTREES_KEY } from './useRosters'

/** The query key head; both sides share it so a save on one invalidates the other (`lib/assignments.ts`). */
export const ASSIGNMENTS_QUERY = 'assignments'

/** What the last write said: its outcome, or why it failed. */
export type AssignmentNote = { tone: 'success' | 'error'; text: string }

/* One side's assignments, applied INSTANTLY: every tick, untick and override is its own replace-set PUT, the page
has no Save for them. The list shows the change at once (optimistic) and its row stays busy until the answer;
writes are SERIALIZED, each carrying the whole set as it stands after its own change, so a fast second tick never
races the first. Once the last write answers, the list follows the server again; a failed one drops the writes
queued behind it and re-reads, so the list never claims a change that did not land. `subject` is the node's name or the project's id; null disables
the read (create mode, a page still loading). */
export function useAssignments(side: AssignmentSide, subject: string | number | null) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [ASSIGNMENTS_QUERY, side, subject],
    queryFn: () => get<AssignmentsResponse>(assignmentsPath(side, subject as string | number)),
    enabled: subject !== null,
  })
  const saved = useMemo(() => (query.data ? draftFromResponse(side, query.data) : null), [side, query.data])

  // Non-null only while writes are in flight: the state the queued writes will leave.
  const [optimistic, setOptimistic] = useState<AssignmentDraft | null>(null)
  // Writes in flight per row: a row ticked then unticked stays busy until its LAST write answers.
  const [busy, setBusy] = useState<ReadonlyMap<string, number>>(new Map())
  const [note, setNote] = useState<AssignmentNote | null>(null)
  const chain = useRef<Promise<void>>(Promise.resolve())
  /* One RUN of writes, for one subject. A failure or a subject change starts a new run: the old run's queued
     writes are dropped (each carries the whole set, so a refused change would ride every later one), and its
     answers no longer touch this list — a renamed node must never receive the old node's set. */
  const run = useRef({ epoch: 0, inFlight: 0, intended: null as AssignmentDraft | null })
  const [seenSubject, setSeenSubject] = useState(subject)
  if (seenSubject !== subject) {
    setSeenSubject(subject)
    run.current = { epoch: run.current.epoch + 1, inFlight: 0, intended: null }
    setOptimistic(null)
    setBusy(new Map())
    setNote(null)
  }
  const current = optimistic ?? saved

  const write = useCallback((id: string, next: AssignmentDraft) => {
    if (subject === null) return
    const key = [ASSIGNMENTS_QUERY, side, subject]
    const epoch = run.current.epoch
    run.current.intended = next
    run.current.inFlight += 1
    setOptimistic(next)
    setBusy(prev => new Map(prev).set(id, (prev.get(id) ?? 0) + 1))
    setNote(null)
    // A read already in flight must not land after this write's answer and flip the row back.
    void queryClient.cancelQueries({ queryKey: key })
    const body = assignmentsBody(side, next)
    const settle = (failed: boolean) => {
      if (run.current.epoch !== epoch) return
      run.current.inFlight -= 1
      setBusy(prev => {
        const left = new Map(prev)
        const n = (left.get(id) ?? 1) - 1
        if (n > 0) left.set(id, n)
        else left.delete(id)
        return left
      })
      if (failed) {
        // Drop what is queued behind it, and show the server's truth again.
        run.current = { epoch: epoch + 1, inFlight: 0, intended: null }
        setBusy(new Map())
        setOptimistic(null)
        void queryClient.invalidateQueries({ queryKey: key })
      } else if (run.current.inFlight === 0) {
        run.current.intended = null
        setOptimistic(null)
      }
    }
    chain.current = chain.current.then(async () => {
      if (run.current.epoch !== epoch) return
      try {
        const answer = await put<AssignmentsResponse>(assignmentsPath(side, subject), body)
        queryClient.setQueryData(key, answer)
        if (run.current.epoch === epoch) {
          const said = outcomeNote(answer.outcome)
          setNote(said ? { tone: 'success', text: said } : null)
        }
        // The other side and the pool refetch; an assignment also finishes the setup card's Assign nodes step.
        void queryClient.invalidateQueries({ queryKey: [ASSIGNMENTS_QUERY], predicate: q => q.queryKey[1] !== side })
        void queryClient.invalidateQueries({ queryKey: WORKTREES_KEY })
        void queryClient.invalidateQueries({ queryKey: [COMPANY_SETUP_QUERY] })
        settle(false)
      } catch (err) {
        if (run.current.epoch === epoch) setNote({ tone: 'error', text: apiDetail(err, 'Failed to save') })
        settle(true)
      }
    })
  }, [queryClient, side, subject])

  /** The state the next change applies to: what the queued writes will leave, else the server's. */
  const base = () => run.current.intended ?? saved

  /** Tick. A new row holds no static override. */
  const assign = (id: string) => {
    const from = base()
    const row = from?.available.find(c => c.id === id)
    if (!from || !row) return
    write(id, {
      available: from.available.filter(c => c.id !== id),
      selected: [...from.selected, {
        id, name: row.name, ...(row.microvm ? { microvm: true } : {}),
        staticSlots: null, held: 0, live: [], viaGroup: null, removable: true,
      }].sort(byName),
    })
  }

  /** Untick. A group-carried row is never removed here (D14): only the group line removes it. */
  const unassign = (id: string) => {
    const from = base()
    const row = from?.selected.find(item => item.id === id)
    if (!from || !row || !row.removable) return
    write(id, {
      available: [...from.available, { id, name: row.name, ...(row.microvm ? { microvm: true } : {}) }].sort(byName),
      selected: from.selected.filter(item => item.id !== id),
    })
  }

  /** The static-slots override; null = none. */
  const setOverride = (id: string, staticSlots: number | null) => {
    const from = base()
    const row = from?.selected.find(item => item.id === id)
    if (!from || !row || row.staticSlots === staticSlots) return
    write(id, { ...from, selected: from.selected.map(item => (item.id === id ? { ...item, staticSlots } : item)) })
  }

  return {
    current,
    busy: new Set(busy.keys()) as ReadonlySet<string>,
    note,
    loading: query.isPending && subject !== null,
    // Data wins over `isError`: a cached answer is still the best one to show.
    error: query.isError && !query.data,
    onboardingActive: query.data?.onboarding_active ?? false,
    assign,
    unassign,
    setOverride,
  }
}
