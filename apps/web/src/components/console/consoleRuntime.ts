import { useQuery } from '@tanstack/react-query'
import { get, post } from '../../api'

/* The console slot's live agent session as the CP reports it (`GET /api/console/{project_id}/{number}/runtime`):
 * which task and session the slot holds, and what the Chat view may do with it. Every `reason` is the CP's own
 * copy, shown verbatim beside the control it disables; it is "" whenever `available` is true. */

export type RuntimeAbility = { available: boolean; reason: string }

export type ConsoleRuntime = {
  task_id: number | null
  session_id: string | null
  runtime: 'acp' | 'legacy' | null
  status: 'running' | 'ended' | 'unknown' | null
  provider: string | null
  provider_label: string | null
  chat: RuntimeAbility
  steer: RuntimeAbility
  interrupt: RuntimeAbility
  control: RuntimeAbility
  toggle: RuntimeAbility
}

export type RuntimeControlType = 'steer' | 'interrupt' | 'answer' | 'close'

export type RuntimeControlOut = { intent_id: string; type: RuntimeControlType }

/** Re-read so a session that starts or ends while the console is open moves the Chat tab with it. */
const RUNTIME_POLL_MS = 5_000
/** No task on the slot: a session starting is rarer than one ending, so look less often. */
const IDLE_RUNTIME_POLL_MS = 30_000

function runtimePath(projectId: number, number: number, nodeId: string | undefined): string {
  const node = nodeId ? `?node=${encodeURIComponent(nodeId)}` : ''
  return `/api/console/${projectId}/${number}/runtime${node}`
}

/** The runtime query's key. */
function consoleRuntimeKey(projectId: number, number: number, nodeId: string | undefined) {
  return ['console-runtime', projectId, number, nodeId ?? null] as const
}

export function useConsoleRuntime(
  { projectId, number, nodeId, enabled }: { projectId: number; number: number; nodeId?: string; enabled: boolean },
): ConsoleRuntime | undefined {
  const query = useQuery({
    queryKey: consoleRuntimeKey(projectId, number, nodeId),
    queryFn: () => get<ConsoleRuntime>(runtimePath(projectId, number, nodeId)),
    enabled,
    // A refusal (no such slot yet, a blip mid hand-off) shows no switch until the next read, never a retry storm.
    retry: false,
    // An error keeps the live cadence: a stopped poll hid the switch for the console's whole life.
    refetchInterval: q => (
      q.state.status !== 'error' && q.state.data?.task_id == null ? IDLE_RUNTIME_POLL_MS : RUNTIME_POLL_MS
    ),
  })
  return query.data
}

/** Steer, interrupt, answer or close the slot's live session. `text` only for steer/answer. */
export function sendRuntimeControl(
  { projectId, number, nodeId }: { projectId: number; number: number; nodeId?: string },
  body: { session_id: string; type: RuntimeControlType; text?: string },
): Promise<RuntimeControlOut> {
  return post<RuntimeControlOut>(runtimePath(projectId, number, nodeId), { text: null, ...body })
}
