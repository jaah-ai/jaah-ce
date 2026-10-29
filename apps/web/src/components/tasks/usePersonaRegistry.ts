import { useEffect, useState } from 'react'
import { get } from '../../api'
import {
  isPersonaRegistryLoaded,
  type PersonaRegistryPayload,
  setPersonaRegistry,
} from './personaRegistry'

// The one fetch of the persona vocabulary. Kept apart from `personaRegistry.ts`, a leaf in the `/tasks`
// chunk that must not import `api.ts`/React. Mounted once on `App.tsx`'s `AuthLayout` (kept mounted
// across navigations); the returned flag is what re-renders the tree when the module cache fills.
// Retries: a failure is not survivable for the picker (`personaOptions()` goes empty and `TaskEditForm`
// cannot change a persona), and the effect runs once per page load. Bounded backoff rides out an api
// restart without turning a down backend into a request loop.
const RETRY_DELAYS_MS = [1_000, 4_000, 10_000] as const

/** Where the fetch stands: `failed` once every retry is spent, so a caller waiting on it can stop waiting. */
export type PersonaRegistryStatus = 'loading' | 'loaded' | 'failed'

export function usePersonaRegistry(): boolean {
  return usePersonaRegistryStatus() === 'loaded'
}

export function usePersonaRegistryStatus(): PersonaRegistryStatus {
  const [status, setStatus] = useState<PersonaRegistryStatus>(() => (isPersonaRegistryLoaded() ? 'loaded' : 'loading'))

  useEffect(() => {
    // Static per deployment: once loaded, never refetch (also lets tests seed the cache).
    if (isPersonaRegistryLoaded()) return
    let live = true
    // Held so unmount cuts a pending backoff and ends the retry chain.
    let timer: ReturnType<typeof setTimeout> | undefined

    const attempt = async (n: number): Promise<void> => {
      if (!live) return
      try {
        const payload = await get<PersonaRegistryPayload>('/api/tasks/personas')
        if (!live) return
        // A malformed 200 is a failure: seeding from it would mark loaded and stop retries.
        if (!Array.isArray(payload?.personas)) throw new Error('malformed persona roster payload')
        setPersonaRegistry(payload.personas, payload.picker_hint ?? '')
        setStatus('loaded')
      } catch (err) {
        if (!live) return
        const delay = RETRY_DELAYS_MS[n]
        if (delay === undefined) {
          /* Out of attempts: logged, not bannered — only one form field suffers. */
          console.error('the persona vocabulary could not be loaded:', err)
          setStatus('failed')
          return
        }
        timer = setTimeout(() => { void attempt(n + 1) }, delay)
      }
    }

    void attempt(0)
    return () => {
      live = false
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [])

  return status
}
