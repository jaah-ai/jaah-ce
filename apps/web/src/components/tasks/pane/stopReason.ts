/** Why the system paused a task on the Jaah account (`PipelineTask.stop`) — the row's words after "Paused". */
export const SYSTEM_PAUSE_CAUSE: Record<'balance' | 'jaah_unavailable', string> = {
  balance: 'balance below floor',
  jaah_unavailable: 'Jaah account unavailable',
}
