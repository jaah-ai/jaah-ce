import { useEffect, useRef, useState } from 'react'
import { ApiError, post } from '../api'
import type { Command } from '../types'

export type Phase = 'idle' | 'confirming' | 'submitting' | 'done' | 'failed'

/* WHAT THE CONTROL PLANE SAID, as a value — the one home for issuing a command and READING ITS REPLY.
 *
 * `POST /api/commands` answers 200 with a `Command` whose `status` is the real outcome: 200 is not
 * success, `status === 'done'` is, and `result.reason` says why not. Separate from the hook for callers
 * with nowhere to show a phase (a ⋯ menu item closes on press). */
export type CommandOutcome = { ok: true } | { ok: false; reason: string }

export async function runCommand(
  commandType: string,
  args: Record<string, unknown>,
  nodeId?: string,
): Promise<CommandOutcome> {
  try {
    const cmd = await post<Command>('/api/commands', {
      command_type: commandType, args, ...(nodeId ? { node_id: nodeId } : {}),
    })
    if (cmd.status === 'done') return { ok: true }
    const reason =
      cmd.result && typeof cmd.result['reason'] === 'string' ? cmd.result['reason'] : cmd.status
    return { ok: false, reason }
  } catch (e) {
    return { ok: false, reason: e instanceof ApiError ? String(e.detail ?? e.message) : 'Request failed' }
  }
}

// Shared submit flow for a control command (a slot's command buttons and the ⋯ menu's command items). `confirm()`
// enters the inline "Sure?" state for destructive actions; `submit()` fires immediately.
//
// Dispatch is synchronous NATS request/reply (apps/api/app/routes/commands.py), so the
// POST returns a terminal status ('done' | 'failed' | 'rejected'); there is nothing to poll.
//
// `nodeId` is WHICH node runs the command; every worktree-scoped caller must pass it. Omitted, the API
// falls back to the CP's own NODE_ID (`enqueue_command`), and (project, number) names a slot on EACH node
// (`uq_worktrees_node_project_number`), so the command would hit the wrong machine's slot. It rides the
// body's top level: `node_id` is the dispatch address, `args` the node's payload.
export function useCommand(
  commandType: string, args: Record<string, unknown>, onDone?: () => void, nodeId?: string,
) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [errMsg, setErrMsg] = useState('')
  const resetTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (resetTimeoutRef.current) clearTimeout(resetTimeoutRef.current)
    }
  }, [])

  function scheduleReset(delayMs: number) {
    if (resetTimeoutRef.current) clearTimeout(resetTimeoutRef.current)
    resetTimeoutRef.current = setTimeout(() => { setPhase('idle'); setErrMsg('') }, delayMs)
  }

  function applyCommandResult(cmd: Command) {
    if (cmd.status === 'done') {
      setPhase('done')
      onDone?.()
      scheduleReset(4000)
    } else {
      const reason =
        cmd.result && typeof cmd.result['reason'] === 'string' ? cmd.result['reason'] : cmd.status
      setErrMsg(reason)
      setPhase('failed')
      scheduleReset(5000)
    }
  }

  async function submit() {
    setPhase('submitting')
    try {
      const cmd = await post<Command>('/api/commands', {
        command_type: commandType, args, ...(nodeId ? { node_id: nodeId } : {}),
      })
      applyCommandResult(cmd)
    } catch (e) {
      const msg = e instanceof ApiError ? String(e.detail ?? e.message) : 'Request failed'
      setErrMsg(msg)
      setPhase('failed')
      scheduleReset(5000)
    }
  }

  return { phase, setPhase, errMsg, submit }
}
