import { useEffect, useRef, useState } from 'react'

/* ── A CLIPBOARD WRITE THAT SAYS WHETHER IT WORKED ──
 * The one home of the mechanism behind `ui/CopyButton` (and `ui/CopyableValue` and `lib/credentialRow`'s
 * copy-name icon through it) and `pages/Login`'s escape link; `copyText` alone backs `ConsolePane`'s copies.
 * THREE REFUSALS, ALL `failed`, NEVER A THROW: no `navigator.clipboard` (insecure origin, jsdom), a `.catch`
 * (permission), and the `try` — `writeText` may be missing or throw SYNCHRONOUSLY, and React error
 * boundaries do not catch event-handler throws, so the operator would get no flash at all.
 * THE TEXT IS A THUNK, built only on click and inside the `try` (a transcript is costly to render per
 * keystroke, and a throw building it must not escape either).
 * THE PROMISE OUTLIVES THE MOUNT: a flash landing after cleanup would schedule an orphaned timer. The guard
 * is re-armed on every mount — StrictMode runs mount → cleanup → mount, and a guard only ever cleared
 * would swallow every flash in dev. The timer is cleared on unmount: every host is dismissible. */

export type CopyState = 'idle' | 'copied' | 'failed'

export function copyText(text: () => string, onSettled: (state: 'copied' | 'failed') => void): void {
  let write: Promise<void> | undefined
  try {
    write = navigator.clipboard?.writeText(text())
  } catch {
    onSettled('failed')
    return
  }
  if (write === undefined) { onSettled('failed'); return }
  void write.then(() => onSettled('copied')).catch(() => onSettled('failed'))
}

/**
 * The state flashes to `copied`/`failed` for `flashMs`, then back to `idle`. The third element flashes an
 * outcome learned elsewhere (`ConsoleTerminal`'s pane copy, whose write happens when the node answers).
 */
export function useCopyFlash(
  flashMs: number,
): [CopyState, (text: () => string) => void, (state: 'copied' | 'failed') => void] {
  const [state, setState] = useState<CopyState>('idle')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [])

  function flash(next: 'copied' | 'failed') {
    if (!mountedRef.current) return
    setState(next)
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = setTimeout(() => { timerRef.current = null; setState('idle') }, flashMs)
  }

  return [state, text => copyText(text, flash), flash]
}
