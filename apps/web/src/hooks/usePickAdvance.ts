import { useEffect, useRef } from 'react'

/* A POINTER PICK MOVES ON — one radio click on a stepper's one-choice step is the answer AND the Next.
 *
 * Spread `groupProps` on the radio group and call `picked()` from its change handler: a pick made with a mouse or
 * a finger runs `advance` after `PICK_ADVANCE_MS`, long enough to see the pick land. A keyboard pick only selects
 * (arrowing through a group changes the pick on every key and must not page), as does any pick the caller does
 * not report. `advance` is read when the timer fires, so it sees the state the pick set.
 *
 * THE GUARD: a person who picks and then presses Next out of habit would skip the step after this one. So
 * `guarding()` is true from the pick until `PICK_GUARD_MS` after the advance: the caller's Next / Send / shortcut
 * returns early while it holds. `cancel()` drops a pending advance (call it on any other navigation); unmount
 * drops it too. Used by the task Q&A pages (`components/tasks/GateSteps.tsx`), the add-node wizard and the
 * change-plan wizard. */

export const PICK_ADVANCE_MS = 300
export const PICK_GUARD_MS = 600
// A pick this long after a press is not that press's: a stray press (a disabled row, a button in the group) is forgotten.
const PRESS_MS = 1000

export function usePickAdvance(advance: () => void) {
  const latest = useRef(advance)
  latest.current = advance
  const pressedAt = useRef(0)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const guardUntil = useRef(0)

  const cancel = () => {
    if (timer.current !== null) clearTimeout(timer.current)
    timer.current = null
    guardUntil.current = 0
  }
  useEffect(() => () => { if (timer.current !== null) clearTimeout(timer.current) }, [])

  return {
    groupProps: {
      onPointerDown: () => { pressedAt.current = Date.now() },
      onKeyDown: () => { pressedAt.current = 0 },
    },
    /** The pick just made: moves on after the pause when it came from a pointer. */
    picked: () => {
      if (Date.now() - pressedAt.current > PRESS_MS) return
      pressedAt.current = 0
      cancel()
      guardUntil.current = Date.now() + PICK_ADVANCE_MS + PICK_GUARD_MS
      timer.current = setTimeout(() => {
        timer.current = null
        latest.current()
      }, PICK_ADVANCE_MS)
    },
    /** True while a press of Next would skip the step a pick is moving to. */
    guarding: () => Date.now() < guardUntil.current,
    cancel,
  }
}
