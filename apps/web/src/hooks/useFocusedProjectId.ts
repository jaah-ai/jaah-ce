import { useAuth } from '../auth'
import { readFocusedProjectId } from '../lib/focusedProject'

/** `lib/focusedProject.ts::readFocusedProjectId` for the signed-in user. */
export function useFocusedProjectId(): number | null {
  return readFocusedProjectId(useAuth().admin?.preferences)
}
