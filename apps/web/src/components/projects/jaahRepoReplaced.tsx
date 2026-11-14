import { type JSX, type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import DialogContentText from '@mui/material/DialogContentText'
import Link from '@mui/material/Link'
import { ApiError } from '../../api'
import { FormDialog } from '../../ui/FormDialog'
import { tapTargetSx } from '../../ui/listStyles'
import { FONT_SIZE } from '../../theme'
import Toast from '../../ui/Toast'
import TypedNameConfirm, { typedNameMatches } from '../../ui/TypedNameConfirm'

/* A project Save that replaces or removes its Jaah-made repository: the server answers 409 `jaah_repo_replaced`
 * (`apps/api/app/jaah_repo_replace.py::conflict_detail`) and writes nothing; the page asks here, then sends the same
 * PUT again with `jaah_repo` (and, while an app runs, `jaah_repo_confirm`: its address, typed). */

export const JAAH_REPO_REPLACED = 'jaah_repo_replaced'

export type JaahRepoChoice = 'move' | 'retire'

/** The 409's detail, as the server sends it. */
export interface JaahRepoReplaced {
  reason: 'replace' | 'remove'
  offer_move: boolean
  jaah_repo: string
  app_address: string | null
}

/** The dialog's answer: the choice, and the app's address when one runs (null when none does). */
export interface JaahRepoAnswer {
  choice: JaahRepoChoice
  confirm: string | null
}

/** The 409 detail when `err` is one, else null. */
export function jaahRepoReplaced(err: unknown): JaahRepoReplaced | null {
  if (!(err instanceof ApiError) || err.status !== 409) return null
  const detail = err.detail as Partial<JaahRepoReplaced> & { code?: unknown } | null
  if (!detail || typeof detail !== 'object' || detail.code !== JAAH_REPO_REPLACED) return null
  return {
    reason: detail.reason === 'remove' ? 'remove' : 'replace',
    offer_move: detail.offer_move === true,
    jaah_repo: typeof detail.jaah_repo === 'string' ? detail.jaah_repo : '',
    app_address: typeof detail.app_address === 'string' && detail.app_address ? detail.app_address : null,
  }
}

/** Title, body and buttons per case. Calm words only: archive, turn off — never deleted, destroyed or permanently. */
function copyFor(d: JaahRepoReplaced): { title: string; body: string; submit: string; submitChoice: JaahRepoChoice; alt?: string } {
  const app = d.app_address !== null
  if (d.reason === 'remove') {
    return { title: 'Remove the Jaah repository?', body: app ? "We'll archive it and turn off its app." : "We'll archive it.",
      submit: 'Remove', submitChoice: 'retire' }
  }
  if (d.offer_move) {
    return { title: 'Move the code and history to the new repository?',
      body: app ? "Either way, we'll archive the Jaah repository and turn off its app." : "Either way, we'll archive the Jaah repository.",
      submit: 'Yes', submitChoice: 'move', alt: 'No' }
  }
  return { title: 'Switch repository?',
    body: app ? "We'll archive the Jaah repository and turn off its app." : "We'll archive the Jaah repository.",
    submit: 'Switch', submitChoice: 'retire' }
}

export function JaahRepoDialog({ detail, onAnswer }: {
  detail: JaahRepoReplaced
  /** The answer, or null for Cancel / Escape. */
  onAnswer: (answer: JaahRepoAnswer | null) => void
}): JSX.Element {
  const copy = copyFor(detail)
  const [typed, setTyped] = useState('')
  const address = detail.app_address
  const typedOk = address === null || typedNameMatches(typed, address)
  const answer = (choice: JaahRepoChoice) => onAnswer({ choice, confirm: address === null ? null : typed.trim() })
  return (
    <FormDialog
      title={copy.title}
      submitLabel={copy.submit}
      onSubmit={() => { if (typedOk) answer(copy.submitChoice) }}
      onClose={() => onAnswer(null)}
      submitAutoFocus={address === null}
      submitDisabledReason={typedOk ? undefined : `Type ${address} to confirm`}
      maxWidth="xs"
      testId="jaah-repo-dialog"
      submitTestId="jaah-repo-submit"
      cancelTestId="jaah-repo-cancel"
      secondaryAction={copy.alt && (
        <Button
          type="button"
          variant="outlined"
          disabled={!typedOk}
          onClick={() => answer('retire')}
          sx={tapTargetSx}
          data-testid="jaah-repo-alt"
        >
          {copy.alt}
        </Button>
      )}
    >
      <DialogContentText component="div" sx={{ fontSize: FONT_SIZE.base }}>{copy.body}</DialogContentText>
      {address !== null && (
        <Box>
          <TypedNameConfirm name={address} value={typed} onChange={setTyped} action="turning off the app" testId="jaah-repo-typed" />
        </Box>
      )}
    </FormDialog>
  )
}

/** `const [ask, dialog] = useJaahRepoQuestion()`: render `{dialog}` once, `await ask(detail)` — the answer, or null on
 *  Cancel. An unmount answers null, so an awaiter never hangs. */
export function useJaahRepoQuestion(): [(detail: JaahRepoReplaced) => Promise<JaahRepoAnswer | null>, ReactNode] {
  const [pending, setPending] = useState<{ id: number; detail: JaahRepoReplaced } | null>(null)
  const resolveRef = useRef<((a: JaahRepoAnswer | null) => void) | null>(null)
  const idRef = useRef(0)
  const settle = useCallback((a: JaahRepoAnswer | null) => {
    resolveRef.current?.(a)
    resolveRef.current = null
    setPending(null)
  }, [])
  const ask = useCallback((detail: JaahRepoReplaced) => new Promise<JaahRepoAnswer | null>(resolve => {
    resolveRef.current?.(null)
    resolveRef.current = resolve
    idRef.current += 1
    setPending({ id: idRef.current, detail })
  }), [])
  useEffect(() => () => { resolveRef.current?.(null); resolveRef.current = null }, [])
  const dialog = pending ? <JaahRepoDialog key={pending.id} detail={pending.detail} onAnswer={settle} /> : null
  return [ask, dialog]
}

const moveTaskListeners = new Set<(taskId: number) => void>()

/** A Save that answered `move` started this task: the shell's `MoveTaskToast` links it. Not navigation state, so it
 *  reaches the page whichever way the Save left (the form's Save, the leave dialog's Save, a held Back). */
export function announceMoveTask(taskId: number): void {
  moveTaskListeners.forEach(listener => listener(taskId))
}

/** "Moving code — view task", once per `announceMoveTask`. Mounted in the app shell, which outlives the edit page. */
export function MoveTaskToast(): JSX.Element {
  const [shown, setShown] = useState<{ taskId: number; seq: number } | null>(null)
  useEffect(() => {
    const listener = (taskId: number) => setShown({ taskId, seq: Date.now() })
    moveTaskListeners.add(listener)
    return () => { moveTaskListeners.delete(listener) }
  }, [])
  return (
    <Toast
      toast={shown && {
        text: <>Moving code — <Link component={RouterLink} to={`/tasks/${shown.taskId}`} color="inherit">view task</Link></>,
        seq: shown.seq,
      }}
      onClose={() => setShown(null)}
      testId="move-task-toast"
    />
  )
}
