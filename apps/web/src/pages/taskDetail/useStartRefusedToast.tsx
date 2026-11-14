// A start refused on the way in: the template Start dialog files the task, closes, and opens it here even when its
// start is refused, handing the refusal as navigation state so the reader learns why the task is still a draft.
import { useEffect, useState, type JSX } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import Toast from '../../ui/Toast'
import { START_REFUSED_STATE } from './startRefusedState'

/** The refusal as an error toast, for the task it was handed with only: moving to another task drops it. Read once
 *  on arrival (a string only — the state is untrusted), then cleared from the history entry so a reload or Back does
 *  not repeat it. */
export function useStartRefusedToast(): JSX.Element {
  const location = useLocation()
  const navigate = useNavigate()
  const { id } = useParams<{ id: string }>()
  const handed: unknown = (location.state as Record<string, unknown> | null)?.[START_REFUSED_STATE]
  const [refusal, setRefusal] = useState<{ taskId: string | undefined; text: string; seq: number } | null>(null)
  useEffect(() => {
    if (typeof handed !== 'string') return
    setRefusal({ taskId: id, text: handed, seq: Date.now() })
    navigate(location.pathname + location.search + location.hash, { replace: true, state: null })
  }, [handed])
  const shown = refusal !== null && refusal.taskId === id ? refusal : null
  return (
    <Toast
      toast={shown && { text: shown.text, severity: 'error', seq: shown.seq }}
      onClose={() => setRefusal(null)}
      testId="task-start-refused-toast"
    />
  )
}
