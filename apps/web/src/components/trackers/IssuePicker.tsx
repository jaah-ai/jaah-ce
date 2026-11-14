import { useRef, useState, type JSX, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { listProjectTrackers, projectTrackersKey } from '../../pages/trackers/api'
import type { TaskTrackerIssue } from '../../types'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import { credentialMark } from '../../ui/credentialMarks'
import { BUTTON_SX } from '../../ui/formDensity'
import { FormField } from '../../ui/FormField'
import LoadingState from '../../ui/LoadingState'
import { Tip, TIP_INTERACTIVE_LEAVE_MS } from '../../ui/Tip'
import { RADIUS, grid } from '../../theme'
import TrackerIssueChip from './TrackerIssueChip'
import {
  connectionLabel, getTrackerIssue, searchTrackerIssues, type TrackerIssueHit, type TrackerIssuePick, type TrackerIssueText,
} from './trackerIssuesApi'

/* THE TASK FORM'S "FROM ISSUE" PICKER: one lookup over every tracker connection the project links (key or title
 * words), limited server-side to the linked tracker projects. Picking fills the Description and, on Save, links the
 * task (`tracker_issue`). Tracker-neutral: the mark comes from `ui/credentialMarks` keyed by `tracker`, every word
 * from the server's `tracker_label`.
 *
 * The state lives on the FORM (`useIssuePick`), the field renders it (`IssuePicker`): the form places the field in
 * one of two slots, and crossing slots remounts the field — a pick and an armed replace-or-keep survive that. */

/** The link the form holds: what Save sends, and what the closed field shows. */
export type HeldIssue = Pick<TaskTrackerIssue, 'connection_id' | 'tracker' | 'tracker_label' | 'key' | 'url'> & {
  /** The picked issue's title, while known (a stored link carries none). */
  title?: string
  origin?: string
  /** The project whose linked trackers hold it: a link is that project's or nobody's (`heldElsewhere`). */
  projectId: number | null
}

/** Is the held link another project's? A pick, a stored link and a duplicate's copy all belong to the project they
 *  came from; once the form's project moves, the link is neither shown nor sent (the server would refuse the save). */
export const heldElsewhere = (held: HeldIssue | null, projectId: number | null): boolean =>
  held !== null && held.projectId !== projectId

/* What the pick is doing, one discriminated union (the states exclude each other): the read, the replace-or-keep it
 * may ask first, the outcome, the failure. The pick rewrites the Description, so every stage is visible. */
export type IssuePickState =
  | { kind: 'idle' }
  | { kind: 'loading'; key: string }
  /* The issue is in hand and the Description is not empty: the replace waits for an answer. `previous` restores the
   * field on Keep mine, making it a true cancel. */
  | { kind: 'confirm'; issue: TrackerIssueText; previous: HeldIssue | null }
  | { kind: 'done'; issue: TrackerIssueText }
  | { kind: 'error'; message: string }

const sameIssue = (a: { connection_id: string; key: string } | null | undefined,
  b: { connection_id: string; key: string } | null | undefined): boolean =>
  (a ?? null) === null ? (b ?? null) === null : b != null && a!.connection_id === b.connection_id && a!.key === b.key

/** The `tracker_issue` a write sends, or nothing: a tracker-started link is the tracker's (never sent), an unchanged
 *  one is left alone so unrelated saves cannot touch it, and a pick still being read or answered is not one yet
 *  (an auto-save mid-pick would link an issue whose text was never accepted). */
export function issueField(pick: Pick<IssuePick, 'held' | 'state'>, stored: TaskTrackerIssue | null | undefined):
  { tracker_issue: TrackerIssuePick | null } | Record<string, never> {
  const { held, state } = pick
  if (state.kind === 'loading' || state.kind === 'confirm') return {}
  if (stored?.origin === 'intake' || sameIssue(held, stored)) return {}
  return { tracker_issue: held ? { connection_id: held.connection_id, key: held.key } : null }
}

/** The Description a pick writes: the issue's title, then its description when it has one. */
export const issueText = (issue: TrackerIssueText): string =>
  issue.body_markdown.trim() === '' ? issue.title : `${issue.title}\n\n${issue.body_markdown}`

/** The form-held pick: the link, the stage, and the three acts on it. `body()` reads the Description NOW (the
 *  question spans an await); `setBody` writes it. */
export function useIssuePick({ stored, storedProjectId, body, setBody }: {
  stored: TaskTrackerIssue | null | undefined
  /** The project `stored` belongs to (the row's own). */
  storedProjectId: number | null
  body: () => string
  setBody: (text: string) => void
}) {
  const [held, setHeld] = useState<HeldIssue | null>(() => stored ? { ...stored, projectId: storedProjectId } : null)
  const [state, setState] = useState<IssuePickState>({ kind: 'idle' })
  /* Every pick, clear and Keep mine bumps it: a read that answers after a later act is dropped, never applied over
   * it (a slow pick resolving after a clear would bring the link and its text back). */
  const turn = useRef(0)

  function apply(issue: TrackerIssueText) {
    setBody(issueText(issue))
    setState({ kind: 'done', issue })
  }

  /* The value is set FIRST so the field shows the choice during the read; a failed read puts it back (a link whose
   * text never arrived would be false). Nothing is saved here: Save sends the link. */
  async function choose(projectId: number, next: HeldIssue | null) {
    const mine = ++turn.current
    const previous = held
    setHeld(next)
    if (next === null) {
      setState({ kind: 'idle' })
      return
    }
    setState({ kind: 'loading', key: next.key })
    let issue: TrackerIssueText
    try {
      issue = await getTrackerIssue(projectId, next.connection_id, next.key)
    } catch (err) {
      if (mine !== turn.current) return
      setHeld(previous)
      setState({
        kind: 'error',
        message: `${next.key} was not loaded: ${apiDetail(err, 'The tracker could not be reached.')} The Description is unchanged.`,
      })
      return
    }
    if (mine !== turn.current) return
    setHeld({ ...next, title: issue.title, url: issue.url })
    // Replacing typed prose is unrecoverable, so it waits for an answer; an empty Description fills at once.
    if (body().trim() !== '') {
      setState({ kind: 'confirm', issue, previous })
      return
    }
    apply(issue)
  }

  function keep(previous: HeldIssue | null) {
    turn.current++
    setHeld(previous)
    setState({ kind: 'idle' })
  }

  return { held, state, choose, apply, keep }
}

export type IssuePick = ReturnType<typeof useIssuePick>

/** Does the project link at least one tracker project? Reads `GET /api/projects/{id}/trackers` (cached with the
 *  project page's Issue trackers section). Null while unknown. */
export function useProjectHasTrackers(projectId: number | null): boolean | null {
  const read = useQuery({
    queryKey: projectTrackersKey(projectId ?? 0),
    queryFn: () => listProjectTrackers(projectId!),
    enabled: projectId !== null,
    staleTime: 60_000,
    retry: false,
  })
  if (projectId === null) return false
  if (!read.data) return read.isError ? false : null
  return (read.data.links ?? []).some(link => link.containers.length > 0)
}

const optionKey = (issue: { connection_id: string; key: string }) => `${issue.connection_id}/${issue.key}`

/* The closed field: the key and, once read, the title. */
const heldOption = (held: HeldIssue): LookupOption => ({
  key: optionKey(held),
  label: held.title ? `${held.key} · ${held.title}` : `${held.tracker_label} ${held.key}`,
  icon: credentialMark(held.tracker, held.tracker_label),
})

/* A search hit: the mark, the key and the whole title (wrapping, never clipped), the tracker's word as the hint. */
const hitOption = (hit: TrackerIssueHit): LookupOption => ({
  key: optionKey(hit),
  label: `${hit.key} · ${hit.title}`,
  hint: hit.tracker_label,
  icon: credentialMark(hit.tracker, hit.tracker_label),
})

/** The field. `stowed` is the placement (`TaskSpecFields`): a cell in the settings row, or a strip inside Advanced.
 *  `titleFill` is the Title an edit will take from the pick (the form decides when). `heldHidden`: the form will not
 *  send the held link (a duplicate's copy outside its project), so the field shows none — shown ⇔ submitted. */
export default function IssuePicker({ projectId, pick, disabled, stowed, titleFill, heldHidden = false, reason }: {
  projectId: number
  pick: IssuePick
  disabled: boolean
  stowed: boolean
  titleFill: string
  heldHidden?: boolean
  /** Why the field is disabled, on hover of its cell (the form's "link a tracker" when the project links none). */
  reason?: ReactNode
}): JSX.Element {
  const { state } = pick
  const held = heldHidden ? null : pick.held
  // The hits the last search answered, so a picked row's tracker words reach the held link.
  const hits = useRef(new Map<string, TrackerIssueHit>())

  const cellSx = stowed
    ? { mb: 1, pb: 1, borderBottom: '1px solid', borderColor: 'divider' }
    /* A flex cell: a key and the front of its title, a floor and a cap each `min()`-ed against the real space (a
     * bare min-width overflowed the form at 481-490px). The popup is where titles are read in full. */
    : { flex: `1 1 ${grid(48)}`, minWidth: `min(${grid(34.5)}, 100%)`, maxWidth: `min(${grid(69)}, 100%)` }

  // A link the tracker started is the tracker's: shown, never changed here.
  if (held?.origin === 'intake') {
    return (
      <Box data-testid="task-form-issue" sx={cellSx}>
        <Typography variant="caption" component="p" sx={{ color: 'text.secondary', mb: 0.5 }}>Issue</Typography>
        <TrackerIssueChip issue={held} />
      </Box>
    )
  }

  /* The reason rides the cell, not the disabled input (which takes no pointer events); interactive, as it may hold a
   * link to click. Always wrapped, so a reason coming or going never remounts the field. */
  return (
    <Tip title={reason} disableInteractive={!reason} leaveDelay={reason ? TIP_INTERACTIVE_LEAVE_MS : undefined}><Box data-testid="task-form-issue" sx={cellSx}>
      <FormField
        label="From issue"
        help="Fills the Description from the issue and links it."
        error={state.kind === 'error' ? state.message : undefined}
      >
        <AsyncLookup
          id="task-from-issue"
          testId="task-form-issue-lookup"
          value={held ? heldOption(held) : null}
          onChange={next => {
            if (next === null) return void pick.choose(projectId, null)
            // A row the last answer no longer names (a superseded search): ignored, never read as a clear.
            const hit = hits.current.get(next.key)
            if (hit) {
              void pick.choose(projectId, {
                connection_id: hit.connection_id, tracker: hit.tracker, tracker_label: hit.tracker_label, key: hit.key,
                url: hit.url, title: hit.title, projectId,
              })
            }
          }}
          fetchOptions={(query, signal) => searchTrackerIssues(projectId, query, signal).then(({ results, errors }) => {
            hits.current = new Map((results ?? []).map(hit => [optionKey(hit), hit]))
            // Each failed connection is one line ABOVE the results; the others still list.
            const failed = (errors ?? []).map((err): LookupOption => ({
              key: `error/${err.connection_id}`, label: `${connectionLabel(err)}: ${err.detail}`, tone: 'error',
            }))
            const found = (results ?? []).map(hitOption)
            // Every tracker failed or none matched: the list still says "No match" under the failures.
            return failed.length > 0 && found.length === 0
              ? [...failed, { key: 'no-match', label: 'No match', tone: 'muted' }]
              : [...failed, ...found]
          })}
          placeholder="Issue key or title"
          ariaLabel="From issue — key or title"
          // One character would spend a tracker round-trip on an unusable list.
          minChars={2}
          noOptionsText="No match"
          wrap
          disabled={disabled}
        />
      </FormField>

      {state.kind === 'loading' && (
        <Box sx={{ mt: 0.5 }}>
          <LoadingState label={`Loading ${state.key}…`} />
        </Box>
      )}

      {/* Replace-or-keep, only when the fill would overwrite typed prose. Both buttons `type="button"` (inside the
          form); Replace takes focus (the pick was an Enter). */}
      {state.kind === 'confirm' && (
        <Box
          role="group"
          aria-label="Replace the Description from the issue"
          data-testid="task-form-issue-confirm"
          sx={{
            mt: 1, py: 1, px: 2, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1,
            border: '1px solid', borderColor: 'warning.main', borderRadius: RADIUS.md, justifyContent: 'flex-end',
          }}
        >
          <Typography variant="caption" sx={{ flex: `1 1 ${grid(40)}`, minWidth: 0, color: 'text.primary', wordBreak: 'break-word' }}>
            {`Replace the Description with ${state.issue.key} — “${state.issue.title}”? What is written there now is lost.`}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flex: '0 0 auto' }}>
            <Button sx={BUTTON_SX} type="button" size="small" variant="contained" color="primary" autoFocus
              data-testid="task-form-issue-replace" onClick={() => pick.apply(state.issue)}>
              Replace
            </Button>
            <Button sx={BUTTON_SX} type="button" size="small" variant="text" color="inherit"
              data-testid="task-form-issue-keep" onClick={() => pick.keep(state.previous)}>
              Keep mine
            </Button>
          </Box>
        </Box>
      )}

      {state.kind === 'done' && (
        <Typography component="p" variant="caption" role="status" data-testid="task-form-issue-status"
          sx={{ mt: 0.5, mb: 0, wordBreak: 'break-word', color: 'text.secondary' }}>
          {`Description filled from ${state.issue.key}.`}
          {titleFill !== '' && ` The Title will be “${titleFill}”.`}
        </Typography>
      )}
    </Box></Tip>
  )
}
