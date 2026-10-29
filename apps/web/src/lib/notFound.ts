import { useEffect } from 'react'
import { createExternalStore, useExternalStore } from './externalStore'

/* Whether the shell is showing the catch-all page (`components/UnknownRoute`). The rail and the phone bar light
   nothing then: `/settings/billing` is no Settings page, so lighting Settings would point at a page that is not
   there. A count, not a flag, so a remount's mount-before-unmount never clears it. */
const shown = createExternalStore(0)

/** True while a not-found page is on screen. */
export function useNotFoundShown(): boolean {
  return useExternalStore(shown, count => count > 0)
}

/** Marks a not-found page as shown for as long as the calling component is mounted. */
export function useMarkNotFound(): void {
  useEffect(() => {
    shown.set(shown.get() + 1)
    return () => shown.set(shown.get() - 1)
  }, [])
}
