import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { useActingAs } from '../../lib/actAs'
import { memberLabel, memberName, memberOption } from '../../lib/memberLabel'
import type { ProjectMember } from '../../types'
import { AsyncLookup } from '../../ui/AsyncLookup'
import { matchesAny } from '../../ui/FilterRow'
import FormDialog from '../../ui/FormDialog'
import FormField from '../../ui/FormField'
import { tapTargetSx } from '../../ui/listStyles'

/* WHOSE GIT ACCOUNT AN ACTING OPERATOR'S START PUSHES WITH. An operator acting as an org (`lib/actAs.ts`) is no
 * member of it, so the server cannot record them as the task's starter and the runner's push is refused — late,
 * at push time. Start asks first: push as one of the project's members (`push_as_id` on `start`/`force-start`,
 * accepted from an acting operator only), or start without push, knowingly. Not acting, nothing is asked.
 * Only the members the server accepts are offered (`?may_start=true`, the `push_as_id` vetting); an assignee
 * outside that list is said, never swapped silently for another member. The member is told by a comment.
 * Members are read here, not through the query cache: the task editor mounts without a `QueryClient`. */

/** What a Start is about to start: the project it pushes to, the row's words, and its assignee (the default). */
export type PushIdentityTarget = { projectId: number; title: string; assigneeId?: string | null }
/** The answer, spread into the start body: `push_as_id` set, or absent (not acting, or "Start without push"). */
export type PushIdentity = { push_as_id?: string }

/** The project's members who may start tasks there: the ask's list, and an acting create's default assignee
 *  (`TaskEditForm`). */
export function readStarters(projectId: number): Promise<ProjectMember[]> {
  return get<ProjectMember[]>(`/api/projects/${projectId}/members?may_start=true`)
    .then(rows => (Array.isArray(rows) ? rows : []))
}

export default function PushIdentityDialog({
  orgName,
  target,
  onStart,
  onCancel,
}: {
  orgName: string
  target: PushIdentityTarget
  /** "Start" — a member's id; "Start without push" — undefined. */
  onStart: (pushAsId: string | undefined) => void
  onCancel: () => void
}) {
  const [members, setMembers] = useState<ProjectMember[] | null>(null)
  const [failed, setFailed] = useState(false)
  const [picked, setPicked] = useState('')
  const [assigneeOut, setAssigneeOut] = useState(false)

  useEffect(() => {
    let live = true
    readStarters(target.projectId)
      .then(list => {
        if (!live) return
        setMembers(list)
        const out = Boolean(target.assigneeId) && !list.some(m => m.id === target.assigneeId)
        setAssigneeOut(out)
        // An ineligible assignee leaves the pick empty: the operator chooses, nothing is chosen for them.
        setPicked(out ? '' : list.find(m => m.id === target.assigneeId)?.id ?? list[0]?.id ?? '')
      })
      .catch(() => {
        if (!live) return
        setMembers([])
        setFailed(true)
      })
    return () => { live = false }
  }, [target.projectId, target.assigneeId])

  const loading = members === null
  const none = members !== null && members.length === 0
  const pickedMember = members?.find(m => m.id === picked)
  const assignee = target.assigneeId ? members?.find(m => m.id === target.assigneeId) : undefined

  return (
    <FormDialog
      open
      title={`Start “${target.title}”`}
      submitLabel="Start"
      submitAutoFocus
      hideSubmit={loading || none}
      submitDisabled={!picked}
      onSubmit={() => onStart(picked)}
      onClose={onCancel}
      testId="push-identity-dialog"
      paperSx={{ maxWidth: 420 }}
      contentSx={{ gap: 1.5, fontSize: '0.875rem' }}
      secondaryAction={
        <Button type="button" onClick={() => onStart(undefined)} variant="outlined" color="inherit" sx={tapTargetSx}>
          Start without push
        </Button>
      }
    >
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        You’re acting as {orgName} but aren’t a member, so a runner you start can’t push. The member you
        push as is told.
      </Typography>
      {/* The acting default assignee is set off-screen on a phone (below the editor): said here, before it starts.
      An ineligible one is the warning below instead. */}
      {assignee && (
        <Typography variant="body2" data-testid="push-identity-assignee">
          Assigned to {memberName(assignee)}.
        </Typography>
      )}
      {assigneeOut && (
        <Typography variant="body2" data-testid="push-identity-assignee-out" sx={{ color: 'warning.main' }}>
          The assignee can’t start tasks in this project. Pick who to push as.
        </Typography>
      )}
      {loading ? (
        <Typography variant="body2">Loading members…</Typography>
      ) : none ? (
        <Typography variant="body2">
          {failed ? 'Members failed to load.' : 'No member may start tasks here.'}
        </Typography>
      ) : (
        /* The Assigned to picker's rows (`memberOption`): the name, the email dim beside it — a native option has
        one line, so it printed "Name — email", or a bare address. */
        <FormField label="Push as">
          <AsyncLookup
            id="push-identity-member"
            testId="push-identity-member"
            value={pickedMember ? memberOption(pickedMember) : null}
            onChange={next => setPicked(next?.key ?? '')}
            fetchOptions={query =>
              Promise.resolve(members.filter(m => matchesAny(query, memberLabel(m))).map(memberOption))
            }
            fetchOnFocus
            minChars={0}
            placeholder="Pick a member"
            noOptionsText="No matching members"
            stacked
            disableClearable
          />
        </FormField>
      )}
    </FormDialog>
  )
}

/** THE ONE HOME of the ask, for every Start door (`useTaskActions`, the task editor). `ask` resolves `{}` at once
 *  when the tab is not acting; otherwise it opens `dialog` and resolves the choice, or null on Cancel. Render
 *  `dialog` where the page renders its other modals. `acting` lets a door skip a read only the ask needs. */
export function usePushIdentity(): {
  acting: boolean
  ask: (target: PushIdentityTarget) => Promise<PushIdentity | null>
  dialog: ReactNode
} {
  const acting = useActingAs()
  const [asking, setAsking] = useState<PushIdentityTarget | null>(null)
  const resolveRef = useRef<((answer: PushIdentity | null) => void) | null>(null)

  function settle(answer: PushIdentity | null) {
    const resolve = resolveRef.current
    resolveRef.current = null
    setAsking(null)
    resolve?.(answer)
  }

  // Stable while the act-as org is, so a door's `useCallback` may depend on it.
  const ask = useCallback((target: PushIdentityTarget): Promise<PushIdentity | null> => {
    if (!acting) return Promise.resolve({})
    // A second ask (a double click) supersedes the first, which reads as a Cancel.
    resolveRef.current?.(null)
    return new Promise(resolve => {
      resolveRef.current = resolve
      setAsking(target)
    })
  }, [acting])

  const dialog = acting && asking && (
    <PushIdentityDialog
      orgName={acting.name}
      target={asking}
      onStart={id => settle(id ? { push_as_id: id } : {})}
      onCancel={() => settle(null)}
    />
  )

  return { acting: acting !== null, ask, dialog }
}
