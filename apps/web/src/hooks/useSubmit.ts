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
 * dropped) belongs there too; '' shows nothing. `initialFormError` seeds the refusal a page carries across a
 * remount (a create's refusal shown on the editor it landed on), so it is on the first paint. `run` resolves
 * `{ value }` once the write landed and `null` when it was refused or skipped — boxed, so a `void` write still reads
 * as landed (`{ value: undefined }` is truthy) and a caller narrows with one `if (!landed)`. */
export type RunOptions = { mapError?: (err: unknown, fallback: string) => string }
export type Landed<T> = { value: T } | null

/** The arms every `fieldForDetail` shares, for its fallback: the standard entity shape's `name`/`description`
 *  refusals lead with the column (`app/entity_shape.py`), and a unique-name 409 says what "already exists"
 *  (so it lands on `name`). A site's own arms run first — their order is load-bearing, documented at each. */
export function commonRefusalField(detail: string): 'name' | 'description' | null {
  if (/already exists/i.test(detail)) return 'name'
  if (/^name\b/i.test(detail)) return 'name'
  if (/^description\b/i.test(detail)) return 'description'
  return null
}

export function useSubmit<F extends string = never>(
  fieldForDetail?: (detail: string) => F | null,
  initialFormError = '',
): {
  busy: boolean
  errors: Partial<Record<F, string>>
  setErrors: Dispatch<SetStateAction<Partial<Record<F, string>>>>
  formError: string
  setFormError: Dispatch<SetStateAction<string>>
  run: <T>(fallback: string, write: () => Promise<T>, options?: RunOptions) => Promise<Landed<T>>
  saved: boolean
  clearSaved: () => void
} {
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<Partial<Record<F, string>>>({})
  const [formError, setFormError] = useState(initialFormError)
  const [saved, setSaved] = useState(false)
  const inFlight = useRef(false)
  // Read at call time, so `run` stays one stable function across renders (safe in a `useCallback` dep list).
  const fieldFor = useRef(fieldForDetail)
  fieldFor.current = fieldForDetail

  const run = useCallback(async <T,>(fallback: string, write: () => Promise<T>, options?: RunOptions): Promise<Landed<T>> => {
    if (inFlight.current) return null
    inFlight.current = true
    setBusy(true)
    setErrors({})
    setFormError('')
    setSaved(false)
    try {
      const value = await write()
      setSaved(true)
      return { value }
    } catch (err) {
      const detail = options?.mapError ? options.mapError(err, fallback) : apiDetail(err, fallback)
      const field = fieldFor.current?.(detail)
      if (field) setErrors({ [field]: detail } as Partial<Record<F, string>>)
      else setFormError(detail)
      return null
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }, [])

  const clearSaved = useCallback(() => setSaved(false), [])

  return { busy, errors, setErrors, formError, setFormError, run, saved, clearSaved }
}
