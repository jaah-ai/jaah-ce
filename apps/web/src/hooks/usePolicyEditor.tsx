import { useCallback, type Dispatch, type SetStateAction } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { usePolicyQuery } from './usePolicyQuery'
import { useServerDraft } from './useServerDraft'
import { useSubmit, type Landed, type RunOptions } from './useSubmit'

/* THE ONE editor shell of an in-place policy section (Retention, Data residency, Data regions, SIEM export, IP
 * allowlist): `usePolicyQuery`'s read, `useServerDraft`'s draft seeded from `source(data)` (the data itself unless
 * the section edits one part of it) through `toDraft`, and `useSubmit`'s write lifecycle, wired as every section
 * wires them — an edit clears "Saved.", `cancel` is the draft's reset that also clears it and any field refusals,
 * and `save(failure, write)` runs the write, puts its answer in the query and commits the draft to it. `data` is
 * typed present once `gate` is null. `saveBar` is what `ui/CancelSaveBar` takes from here; a section adds its own
 * `dirty` and ids. Everything else is the three hooks' own, under their names. `source` must answer a STABLE
 * reference for unchanged data (a property, not a fresh object): `useServerDraft` re-seeds whenever it changes. */
export function usePolicyEditor<T, D, S = T, F extends string = never>({ source, toDraft, initial, fieldForDetail, ...query }: Parameters<typeof usePolicyQuery<T>>[0] & {
  source?: (data: T) => S
  toDraft: (saved: S) => D
  initial: D
  fieldForDetail?: (detail: string) => F | null
}): ReturnType<typeof usePolicyQuery<T>> & {
  draft: D
  edit: Dispatch<SetStateAction<D>>
  editing: boolean
  commit: (saved?: S) => void
  busy: boolean
  errors: Partial<Record<F, string>>
  setErrors: Dispatch<SetStateAction<Partial<Record<F, string>>>>
  formError: string
  saved: boolean
  clearSaved: () => void
  cancel: () => void
  save: (failure: string, write: () => Promise<T>, options?: RunOptions) => Promise<Landed<void>>
  saveBar: { busy: boolean; onDiscard: () => void; saved: boolean }
} {
  const client = useQueryClient()
  const read = usePolicyQuery<T>(query)
  const pick = source ?? ((data: T) => data as unknown as S)
  const { busy, errors, setErrors, formError, run, saved, clearSaved } = useSubmit<F>(fieldForDetail)
  const { draft, edit, editing, commit, reset } = useServerDraft(read.data === undefined ? undefined : pick(read.data), toDraft, initial, clearSaved)

  const cancel = useCallback(() => {
    reset()
    clearSaved()
    setErrors({})
  }, [reset, clearSaved, setErrors])

  const save = (failure: string, write: () => Promise<T>, options?: RunOptions) =>
    run(failure, async () => {
      const next = await write()
      client.setQueryData(query.queryKey, next)
      commit(pick(next))
    }, options)

  return { ...read, draft, edit, editing, commit, busy, errors, setErrors, formError, saved, clearSaved, cancel, save, saveBar: { busy, onDiscard: cancel, saved } }
}
