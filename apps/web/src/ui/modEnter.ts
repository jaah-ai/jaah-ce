/* THE SUBMIT SHORTCUT (Cmd/Ctrl+Enter) as a capture handler, shared by the task form's Go (`TaskEditForm`) and a
   gate's answer (`GateSteps`): CAPTURE, and stopped there, so the editor's own Mod-Enter (StarterKit's hard break)
   and the note field do not also act on the key. Named in prose as `MOD_ENTER_LABEL` (`./modifierKey`). */
import type { KeyboardEvent } from 'react'

/** True while an IME is confirming: `keyCode` 229 is what an IME reports on browsers that do not set `isComposing`
 *  on keydown. Such a key is never a shortcut, a pick or a send. */
export function isImeKey(e: KeyboardEvent): boolean {
  return e.nativeEvent.isComposing || e.keyCode === 229
}

/** An `onKeyDownCapture` running `run` on a plain Cmd/Ctrl+Enter from inside the element, while `enabled`. A dialog
 *  the element opens is portaled: React still routes its keys here, the DOM does not, so those pass untouched. */
export function modEnterCapture(run: () => void, enabled: boolean): (e: KeyboardEvent<HTMLElement>) => void {
  return e => {
    if (e.key !== 'Enter' || !(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || isImeKey(e)) return
    if (!(e.target instanceof Node) || !e.currentTarget.contains(e.target)) return
    if (!enabled) return
    e.preventDefault()
    e.stopPropagation()
    run()
  }
}
