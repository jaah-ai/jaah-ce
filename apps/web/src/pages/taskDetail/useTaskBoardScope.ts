import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useListDetail } from '../../components/ListDetail'
import { SCOPE_PARAM, taskPageScope } from '../../lib/taskScope'

/* THE BOARD BESIDE A TASK SURVIVES A RELOAD: `/tasks/:id` carries the board's `?scope=` (`lib/taskScope.ts::taskPageScope`
   decides which), written with `replace` so Back never stops on the bare URL. A cold load reads the list beside the
   task from that param (`lib/listDetail.ts`, `carry`). Returns the scope the URL carries, for Back on a phone.
   `projectId` is undefined until the task's row is known; `skip` holds the write (create mode, the Live Cast viewer,
   another org's task). */
export function useTaskBoardScope(projectId: number | null | undefined, skip: boolean): string | null {
  const [searchParams, setSearchParams] = useSearchParams()
  const listLocation = useListDetail()?.listLocation ?? null
  const urlScope = searchParams.get(SCOPE_PARAM)
  const boardScope = listLocation?.pathname === '/tasks' ? new URLSearchParams(listLocation.search).get(SCOPE_PARAM) : null
  const next = skip ? null : taskPageScope(urlScope, boardScope, projectId)
  /* Keyed on the whole query too: another rewrite in the same commit (a retired `?tab=` landing) builds on the same
     snapshot and may drop this one, and the changed query then writes it again. */
  const search = searchParams.toString()
  useEffect(() => {
    if (next === null) return
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      params.set(SCOPE_PARAM, next)
      return params
    }, { replace: true })
  }, [next, search, setSearchParams])
  return urlScope ?? next
}
