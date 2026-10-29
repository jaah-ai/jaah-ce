import { type Dispatch, type SetStateAction, useCallback, useRef, useState } from 'react'
import { apiDetail } from '../api'

/* One write's lifecycle: in-flight flag, the refusal, and — for a form whose `api.ts` can attribute a
 * refusal to a field — the per-field messages. `run` clears both, runs the write, and routes a rejection
 * through `apiDetail`: to the field `fieldForDetail` names, else to `formError`. A second `run` while one
 * is in flight is ignored — read off a ref, so a `run` a stale closure holds (a GSI callback) still sees the
 * live flag. Client-side validation stays at the call site and writes `setErrors`/`setFormError` itself.
 * `saved` is true only after the latest `run` succeeded (render it with `ui/SavedNote`); a form calls
 * `clearSaved` on edit. `mapError` words the refusal in place of `apiDetail` (a decline's own sentence, a fixed
 * string, a branch on the status) — it runs inside the catch, so a site's post-refusal side effect (a stale quote
 * dropped) belongs there too; '' shows nothing. */
export type RunOptions = { mapError?: (err: unknown, fallback: string) => string }

export function useSubmit<F extends string = never>(
  fieldForDetail?: (detail: string) => F | null,
): {
  busy: boolean
  errors: Partial<Record<F, string>>
  setErrors: Dispatch<SetStateAction<Partial<Record<F, string>>>>
  formError: string
  setFormError: Dispatch<SetStateAction<string>>
  run: (fallback: string, write: () => Promise<unknown>, options?: RunOptions) => Promise<void>
  saved: boolean
  clearSaved: () => void
} {
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<Partial<Record<F, string>>>({})
  const [formError, setFormError] = useState('')
  const [saved, setSaved] = useState(false)
  const inFlight = useRef(false)
  // Read at call time, so `run` stays one stable function across renders (safe in a `useCallback` dep list).
  const fieldFor = useRef(fieldForDetail)
  fieldFor.current = fieldForDetail

  const run = useCallback(async (fallback: string, write: () => Promise<unknown>, options?: RunOptions): Promise<void> => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setErrors({})
    setFormError('')
    setSaved(false)
    try {
      await write()
      setSaved(true)
    } catch (err) {
      const detail = options?.mapError ? options.mapError(err, fallback) : apiDetail(err, fallback)
      const field = fieldFor.current?.(detail)
      if (field) setErrors({ [field]: detail } as Partial<Record<F, string>>)
      else setFormError(detail)
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [])

  const clearSaved = useCallback(() => setSaved(false), [])

  return { busy, errors, setErrors, formError, setFormError, run, saved, clearSaved }
}
