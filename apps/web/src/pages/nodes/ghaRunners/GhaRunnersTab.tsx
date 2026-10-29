import { type JSX, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Link from '@mui/material/Link'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, get } from '../../../api'
import { useCan } from '../../../auth'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useEventStream } from '../../../hooks/useEventStream'
import { useLiveRefetch } from '../../../hooks/useLiveRefetch'
import { absoluteTime, relativeAge } from '../../../lib/relativeTime'
import { withAdded, withRemoved } from '../../../lib/sets'
import type { GhaRunnerActivity, GhaRunnerJob, GhaRunnerObserved, GhaRunnerPool, Project } from '../../../types'
import Banner from '../../../ui/Banner'
import CredentialSelect, { type CredentialOption } from '../../../ui/CredentialSelect'
import { credentialMark } from '../../../ui/credentialMarks'
import { DetailCell, DetailSection } from '../../../ui/detail/DetailPage'
import EmptyState from '../../../ui/EmptyState'
import FieldRow from '../../../ui/FieldRow'
import FormField from '../../../ui/FormField'
import FormTextField from '../../../ui/FormTextField'
import { PHONE, fieldTapTargetSx, monoSx, tapTargetSx } from '../../../ui/listStyles'
import LoadingState from '../../../ui/LoadingState'
import RelativeTime from '../../../ui/RelativeTime'
import StatusChip, { type Tone } from '../../../ui/StatusChip'
import StatusDot from '../../../ui/StatusDot'
import { loadCredentialOptions } from '../../credentials/api'
import { deleteGhaRunnerPool, getGhaRunnerPool, putGhaRunnerPool } from './api'

/* /nodes/:nodeId › Runners — the node's GITHUB ACTIONS RUNNER POOL.
 *
 * The operator DECLARES the pool (project — its `repo_url` is the registration scope — a stored `github`
 * credential, a count, running or paused) and the node converges on its own 60s pass. Everything below
 * the form is the node's report; the console derives none of it.
 *
 * NO PROSE ON SCREEN: one line for the pool (status icon, active/declared, repository, report age, icon
 * verbs) and one row per runner (GitHub's Idle/Busy dot and word, the slot, the job a busy one is on). Apply shows
 * nothing for up to a minute and Pause leaves runners busy for as long as their jobs take, so
 * `convergence()` still gives every pending state a SENTENCE naming what is awaited and how many runners
 * remain — on the status icon's tooltip, never as a bare spinner.
 *
 * A self-hosted runner CANNOT be gracefully interrupted (actions/runner's unload handler cancels the job
 * under a hard 30s cap; actions/runner#3308), so Pause is a DRAIN: the CP asks GitHub which slots are
 * busy and the node touches only the others. The operator must be told nobody's build is being killed;
 * that sentence and its count are asserted by tests.
 *
 * NO PENDING STATE IS A DEAD END: `absent` is a requested removal, not a gone pool. DELETE is an
 * idempotent re-ask and PUT accepts `running` back, so the status section's recovery block always offers
 * a way out.
 *
 * FETCH: TanStack Query, refetch on SSE `entity: 'node'`, a fixed poll while the stream is down, PLUS a
 * short poll while `phase === 'pending'` (convergence completes on the daemon's timer and may emit no
 * event the browser hears). Writes are plain `async function`s + `await refetch()` (no `useMutation`).
 */

/** The poll fallback while the event stream is DOWN. Re-declared, not imported: a per-surface budget. */
const POLL_MS = 30000

/** The poll while the node has NOT converged, whatever the stream is doing. Runs only while something is
 *  in flight. */
const CONVERGE_POLL_MS = 5000

/* The server's CHECK on `desired_count`, knowingly duplicated so the field teaches "1 to 32" before a
   422 round trip. The server stays the authority. */
const MIN_COUNT = 1
const MAX_COUNT = 32

/** The default for a fresh node: one runner serialises every job. Nothing is written until submit. */
const DEFAULT_COUNT = '2'

const PANEL_SX = { display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1.5 } as const

/** The pool's one line: status, counts, repository, age, verbs. Wraps rather than compressing. */
const HEAD_SX = { display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'center', minWidth: 0 } as const

/** The verb cluster, pushed to the line's end. */
const ACTIONS_SX = { display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center', ml: 'auto' } as const

const MUTED_SX = { color: 'text.secondary', fontSize: '0.8125rem' } as const

/* THE RUNNER LIST — GitHub's own shape: a dot and its word (Idle, Busy, Offline), the slot, and the job a
   busy runner is on. The slot is the name (`gha-2`); the full registered name and the systemd unit ride on
   the state's tooltip. */
const LIST_SX = {
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
  m: 0,
  p: 0,
  listStyle: 'none',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
} as const

const ROW_SX = {
  display: 'flex',
  flexDirection: 'column',
  minWidth: 0,
  px: 1.5,
  py: 0.75,
  '& + &': { borderTop: '1px solid', borderColor: 'divider' },
} as const

/** The runner's one line: state, slot, job, remedy. */
const ROW_LINE_SX = { display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 } as const

/** The dot and its word, never wrapped; `minWidth` fits the common words, so most slots start at one edge. */
const STATE_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 0.75,
  flexShrink: 0,
  minWidth: '9ch',
  whiteSpace: 'nowrap',
  borderRadius: 1,
  fontSize: '0.8125rem',
  color: 'text.secondary',
} as const

/** A job is a link that may be long: it takes the row's spare width and ellipsizes, full text on hover. */
const JOB_SX = {
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  fontSize: '0.8125rem',
  color: 'text.secondary',
} as const

/* A SLOT'S DETAIL UNDER ITS LINE — the unit's memory knobs, then its recent jobs — indented past the dot so
   it reads as that runner's (flush on a phone, where the width is worth more). Wraps; never scrolls
   sideways at 360px. */
const SLOT_DETAIL_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 0.25,
  minWidth: 0,
  pl: 2.5,
  pt: 0.25,
  [PHONE]: { pl: 0 },
} as const

const JOB_LIST_SX = { display: 'flex', flexDirection: 'column', gap: 0.25, m: 0, p: 0, listStyle: 'none', minWidth: 0 } as const

const JOB_LINE_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.25, minWidth: 0, fontSize: '0.8125rem' } as const

/** Jobs shown per slot; the server may send more. */
const JOBS_SHOWN = 5

/** A systemd memory value in words: bytes as GiB, `infinity` as "no limit", anything else verbatim. */
export function memoryValue(raw: string): string {
  if (raw === 'infinity') return 'no limit'
  if (!/^[0-9]+$/.test(raw)) return raw
  const gib = Number(raw) / 2 ** 30
  return `${Number(gib.toFixed(gib >= 10 ? 0 : 1))} GiB`
}

/** The runner's own result word, toned: success for Succeeded, error for a job that did not finish. */
function jobTone(result: string | null): Tone {
  if (result === null) return 'neutral'
  if (/^succeeded$/i.test(result)) return 'success'
  if (/^(failed|canceled|cancelled|interrupted)$/i.test(result)) return 'error'
  return 'neutral'
}

const TONE_INK: Record<Tone, string> = {
  neutral: 'text.secondary',
  info: 'info.main',
  success: 'success.main',
  warning: 'warning.main',
  error: 'error.main',
}

/** The unit's memory knobs, those the node reported (an older daemon reports none). */
function SlotMemory({ runner }: { runner: GhaRunnerObserved }): JSX.Element | null {
  const knobs = [
    runner.oom_policy !== undefined && { key: 'OOMPolicy', word: 'OOM', raw: runner.oom_policy, value: runner.oom_policy },
    runner.memory_max !== undefined && { key: 'MemoryMax', word: 'max', raw: runner.memory_max, value: memoryValue(runner.memory_max) },
    runner.memory_high !== undefined && { key: 'MemoryHigh', word: 'high', raw: runner.memory_high, value: memoryValue(runner.memory_high) },
  ].filter((k): k is { key: string; word: string; raw: string; value: string } => Boolean(k))
  if (knobs.length === 0) return null
  return (
    <Box component="span" data-testid="gha-runner-memory" sx={[MUTED_SX, { display: 'flex', flexWrap: 'wrap', columnGap: 1.5 }]}>
      {knobs.map(k => (
        /* systemd's own spelling on hover, for pasting into `systemctl show`. */
        <Box component="span" key={k.key} data-testid={`gha-runner-${k.key}`} title={`${k.key}=${k.raw}`} sx={{ whiteSpace: 'nowrap' }}>
          {k.word} <Box component="span" sx={[monoSx, { color: 'text.primary' }]}>{k.value}</Box>
        </Box>
      ))}
    </Box>
  )
}

/** A slot's recent jobs, newest first: result, name, a "Shutdown signal" chip where the runner was told to
 *  stop mid-job (the job was killed, not failed), and when it ended (or started, while running). */
function SlotJobs({ jobs }: { jobs: GhaRunnerJob[] }): JSX.Element {
  if (jobs.length === 0) {
    return <Box component="span" data-testid="gha-runner-no-jobs" sx={MUTED_SX}>No recent jobs</Box>
  }
  return (
    <Box component="ul" aria-label="Recent jobs" data-testid="gha-runner-jobs" sx={JOB_LIST_SX}>
      {jobs.slice(0, JOBS_SHOWN).map((job, i) => {
        const word = job.result ?? (job.ended_at === null ? 'Running' : 'No result')
        const tone = job.result === null && job.ended_at === null ? 'info' : jobTone(job.result)
        return (
          <Box component="li" key={`${job.started_at}:${i}`} data-testid="gha-recent-job" data-result={word} sx={JOB_LINE_SX}>
            <Box component="span" data-testid="gha-recent-job-result" data-tone={tone} sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, color: TONE_INK[tone], fontWeight: 600, whiteSpace: 'nowrap' }}>
              <StatusDot tone={tone} size={8} />
              {word}
            </Box>
            <Box component="span" title={job.job} sx={[JOB_SX, { color: 'text.primary', flex: '1 1 8rem' }]}>{job.job}</Box>
            {job.shutdown_signal && (
              <StatusChip label="Shutdown signal" tone="warning" glyph="⚠" data-status="shutdown-signal" />
            )}
            <RelativeTime iso={job.ended_at ?? job.started_at} sx={[MUTED_SX, { whiteSpace: 'nowrap', ml: 'auto' }]} />
          </Box>
        )
      })}
    </Box>
  )
}

/** `https://github.com/o/r` -> `o/r`: the host repeats on every pool. */
const repoPath = (url: string): string => url.replace(/^https:\/\/github\.com\//, '')

type Draft = { project_id: string; credential_id: string; count: string }

const BLANK_DRAFT: Draft = { project_id: '', credential_id: '', count: DEFAULT_COUNT }

/* THE DRAFT HOLDS STRINGS (select and input values); the wire takes integers. Converted once, at submit,
   in `applyDraft` — the only place that can refuse a value that will not convert. */
const draftFrom = (pool: GhaRunnerPool): Draft => ({
  project_id: String(pool.project_id),
  credential_id: pool.credential_id,
  count: String(pool.count),
})

/** Why this project cannot host a pool, or `null`. `repo_url` IS the registration scope. `null` (never
 *  recorded) and `''` (a blank recorded) get different sentences — they are fixed in different places. */
function projectReason(project: Project): string | null {
  if (project.repo_url === null) return 'no repository URL recorded — nothing to register against'
  if (project.repo_url === '') return 'its repository URL is blank — nothing to register against'
  return null
}

/** Why this credential cannot mint runner tokens, or `null`.
 *
 *  THE TYPE CHECK IS NECESSARY AND NOWHERE NEAR SUFFICIENT: minting needs Administration: WRITE, and a
 *  Connect GitHub credential is typed `github` but can NEVER carry it (the console's App declares none).
 *  This only rules out the wrong TYPE; the server's save-time preflight proves the scope. */
function credentialReason(type: string, typeLabel: string): string | null {
  if (type === 'github') return null
  return `a ${typeLabel || type} credential — runner tokens are minted from a GitHub one`
}

/** One runner's dot: GitHub's palette (green Idle, amber Busy) and its word beside it; `detail` (full
 *  name, unit, sub-state) on the tooltip. `hollow` is a ring, for a state GitHub has not confirmed — so an
 *  unknown never reads as Idle. */
type RunnerDot = { color: string; word: string; detail: string; hollow?: boolean }

const DOT = {
  idle: 'success.main',
  active: 'warning.main',
  off: 'text.disabled',
  bad: 'error.main',
} as const

/** ONE RUNNER'S STATE. GitHub's `busy` decides Idle vs Busy when the roster read answered; without it
 *  the node's report stands alone. `active` and `registered` are two AXES (paused = registered +
 *  inactive; a half-finished install is the reverse), so the unhappy combination gets its own dot. */
function runnerDot(runner: GhaRunnerObserved, seen: GhaRunnerActivity | undefined): RunnerDot {
  const sub = runner.sub === '' ? 'no sub-state reported' : runner.sub
  const where = `${runner.name} · ${runner.unit} (${sub})`
  if (runner.active && !runner.registered) {
    return { color: DOT.bad, word: 'Unregistered', detail: `Never handed a job. ${where}` }
  }
  if (!runner.active) {
    return runner.registered
      ? { color: DOT.off, word: 'Stopped', detail: where }
      : { color: DOT.off, word: 'Gone', detail: where }
  }
  if (seen === undefined) {
    return { color: DOT.idle, word: 'Up', detail: `GitHub's view unknown. ${where}`, hollow: true }
  }
  if (seen.busy) return { color: DOT.active, word: 'Busy', detail: where }
  if (seen.online) return { color: DOT.idle, word: 'Idle', detail: where }
  return { color: DOT.off, word: 'Offline', detail: `GitHub reports it offline. ${where}` }
}

/** A slot GitHub lists ABOVE the declared count. The node removes one it still HOLDS after its job; one
 *  it does not is stranded — only a human can remove it in GitHub, so it gets its own word and its row a
 *  visible "Remove in GitHub" link. */
const STRANDED = 'Stranded'

function orphanDot(held: boolean): RunnerDot {
  return held
    ? { color: DOT.bad, word: 'Above the count', detail: 'Removed after its current job.', hollow: true }
    : { color: DOT.bad, word: STRANDED, detail: 'Above the declared count and gone from this host — still taking jobs.' }
}

/** `worded`: the label is drawn beside the glyph, not only on hover — a paused pool says WHO paused it at a
 *  glance ("Paused" vs "Paused (disk)"), since only one of the two wants a Resume. */
type Convergence = { tone: Tone; glyph: string; label: string; sentence: string; worded?: boolean }

/** The repository to NAME in prose, or `null`. `repo_url` is the project's and can be cleared after the
    pool exists, so a READ can meet an empty one; callers branch on `null` rather than interpolating. */
const namedRepo = (pool: GhaRunnerPool): string | null => pool.repo_url?.trim() || null

/* WHERE THIS POOL STANDS, AND WHAT IS BEING WAITED ON.
 *
 * `phase: 'pending'` covers installing, draining and removing, so it is never rendered as a word: each
 * arm produces a sentence naming the operation, the outstanding count and, where needed, that waiting
 * is correct.
 *
 * ERROR WINS OVER EVERYTHING THAT IS STILL A SHAPE TO REACH. `absent` is LEAVING, not converging, so it
 * keeps its own words and wears the error's tone, label and a clause instead. The error message itself
 * renders verbatim in the banner above, never restated here.
 *
 * `observed` IS AN ARGUMENT, never read off `pool`: the caller has already array-guarded it (`get<T>` is
 * an unchecked cast), and `null.length` here white-screens the node route. */
function convergence(pool: GhaRunnerPool, observed: GhaRunnerObserved[], active: number): Convergence {
  const total = observed.length
  const plural = (n: number, one: string, many: string) => (n === 1 ? one : many)

  if (pool.state === 'absent') {
    /* A FAILING REMOVAL IS STILL A REMOVAL: tone and label carry the failure; the closing clause says it
       is not disappearing on its own just now. */
    const failing = pool.phase === 'error'
    return {
      tone: failing ? 'error' : 'warning',
      glyph: failing ? '✕' : '»',
      label: failing ? 'Removal failing' : 'Removing',
      /* THE OPERATION IS NAMED ONLY WHERE THE LABEL DOES NOT: the tooltip reads "<label>. <sentence>", so
         "Removing" would otherwise say it twice. */
      sentence:
        (failing ? 'Removing these runners. ' : '')
        + (active > 0
          ? `${active} of ${total} ${plural(active, 'is', 'are')} still active; each goes once its job ends. `
          : total > 0
            ? `All ${total} have stopped; the node is unregistering them. `
            : '')
        + (failing ? 'The node\'s last removal pass failed; it retries on its next pass.' : ''),
    }
  }

  if (pool.phase === 'error') {
    return {
      tone: 'error',
      glyph: '✕',
      label: 'Not converging',
      sentence: 'The node\'s last convergence pass failed; it retries on its next pass.',
    }
  }

  /* PAUSED BY THE NODE'S DISK OR MEMORY: `state` stays `running` (the operator's instruction is untouched), the
     node is served `paused` until that gate lifts. Its own label, so it never reads as an operator pause that
     needs a Resume. */
  if (pool.paused_by === 'disk' || pool.paused_by === 'memory') {
    const disk = pool.paused_by === 'disk'
    return {
      tone: 'warning',
      glyph: '○',
      label: disk ? 'Paused (disk)' : 'Paused: memory critical',
      worded: true,
      sentence:
        (disk ? 'Disk nearly full' : 'Memory critical') + '; resumes on its own when usage drops.'
        + (active > 0
          ? ` ${active} of ${total} ${plural(total, 'runner', 'runners')} ${plural(active, 'is', 'are')} still finishing ${plural(active, 'its', 'their')} job.`
          : ''),
    }
  }

  if (pool.phase === 'converged') {
    if (pool.state === 'paused') {
      /* THE PROMISE IS ABOUT REGISTERED SLOTS, not rows: a `Gone` row is not registered. Where the two
         counts diverge the sentence names both — the difference is what Resume must mint a token for. */
      const registered = observed.filter(runner => runner.registered).length
      const lost = total - registered
      return {
        tone: 'neutral',
        glyph: '○',
        label: 'Paused',
        worded: true,
        sentence:
          registered === total
            ? `${total} ${plural(total, 'slot is', 'slots are')} still registered at GitHub.`
            : `${registered === 0 ? 'None' : `Only ${registered}`} of the `
              + `${total} ${plural(total, 'slot', 'slots')} ${plural(registered, 'is', 'are')} still `
              + `registered at GitHub; Resume registers the other ${plural(lost, 'one', `${lost}`)} afresh.`,
      }
    }
    return {
      tone: 'success',
      glyph: '●',
      label: 'Running',
      /* The count and repo are laid out beside the chip; only a missing repo is worth a sentence. */
      sentence: namedRepo(pool)
        ? ''
        : 'This pool’s project no longer records a repository URL, so nothing can register.',
    }
  }

  /* ── PENDING ── */
  if (pool.state === 'paused') {
    if (active > 0) {
      /* THE DRAINING SENTENCE carries the count, that a runner cannot be interrupted, and that NOTHING IS
         BEING KILLED. */
      return {
        tone: 'warning',
        glyph: '»',
        label: 'Draining',
        /* The noun agrees with the total, the verb with the count ("1 of 2 runners is"). */
        sentence:
          `${active} of ${total} ${plural(total, 'runner', 'runners')} `
          + `${plural(active, 'is', 'are')} still finishing `
          + `the job ${plural(active, 'it', 'they')} picked up — nothing is being killed and no build is lost.`,
      }
    }
    return {
      tone: 'info',
      glyph: '»',
      label: 'Pausing',
      sentence: 'Every runner has stopped; waiting for the node to report back.',
    }
  }

  return {
    tone: 'info',
    glyph: '»',
    label: 'Applying',
    sentence: `${active} of ${pool.count} ${plural(active, 'is', 'are')} up so far. The first pass is the slow one.`,
  }
}

/** The stored credentials as picker options, non-GitHub ones DISABLED with the reason rather than
 *  filtered out (as `pages/Settings.tsx` does). */
const fetchCredentialOptions = (query: string, signal: AbortSignal) =>
  loadCredentialOptions(query, signal, row => {
    const reason = credentialReason(row.type, row.type_label)
    const icon = credentialMark(row.type, row.type_label)
    return { id: row.id, name: row.name, hint: reason ?? row.type_label, disabled: reason !== null, icon }
  }, { sort: true })

/** `node` is the node uuid every request addresses; `nodeName` is what the words show. */
export function GhaRunnersTab({ node, nodeName }: { node: string; nodeName: string }): JSX.Element {
  /* TWO KEYS, THE SERVER'S SPLIT: `PUT` is `@feature("nodes","edit")`, `DELETE` is
     `@feature("nodes","delete")`. ABSENT FOR A PERMISSION, DISABLED FOR A STATE — `disabled` below means
     only "a write is in flight". */
  const can = useCan()
  const canEdit = can('nodes:edit')
  const canDelete = can('nodes:delete')

  /* SCOPED TO `node`: an unscoped consumer re-renders on every `task` event (`hooks/useEventStream`), and
     this `TabPanel` stays mounted off-screen. */
  const stream = useEventStream('/api/stream', ['node'])
  const [confirm, confirmDialog] = useConfirm()

  /* THE OPERATOR'S UNSAVED EDITS, or `null` for "showing the row". Not seeded by an effect: the 5s
     converge poll would wipe a half-typed count. */
  const [draft, setDraft] = useState<Draft | null>(null)
  /** Whether the setup form is open on a node with NO pool (distinct from `draft`). */
  const [setupOpen, setSetupOpen] = useState(false)
  /** Whether a refused submit has happened — what licenses the empty-field messages. */
  const [showErrors, setShowErrors] = useState(false)
  const [writeError, setWriteError] = useState('')
  /** Whether THIS session dropped the record (vs the node finishing): both end at `pool === null` but mean
   *  opposite things about the host, so the closing line is chosen on this. */
  const [purged, setPurged] = useState(false)
  /* A SET OF ACTION KEYS: three independent writes, so one boolean would spin all three buttons. */
  const [busy, setBusy] = useState<Set<string>>(new Set())

  const poolQuery = useQuery({
    queryKey: ['gha-runner-pool', node],
    queryFn: () => getGhaRunnerPool(node),
    /* THE CONVERGENCE POLL WINS OVER THE STREAM-DOWN FALLBACK. Reads the already-fetched phase. */
    refetchInterval: query => {
      const phase = query.state.data?.pool?.phase
      if (phase === 'pending') return CONVERGE_POLL_MS
      return stream.state === 'down' ? POLL_MS : false
    },
  })

  // Refetch on any `entity: 'node'` event. Re-checked despite the scoped subscription, so a widened scope
  // cannot make the request dishonest.
  useLiveRefetch(stream, () => void poolQuery.refetch(), null, { entity: 'node' })

  /* THE PROJECT ROSTER, FETCHED ONLY ONCE A FORM IS ON SCREEN; cached afterwards. */
  const formWanted = canEdit
  const projectsQuery = useQuery({
    queryKey: ['gha-runner-projects'],
    queryFn: () => get<Project[]>('/api/projects'),
    enabled: formWanted,
    /* A BOUNDED RETRY: the app default is `retry: false` (`queryClient.ts`), this query has no poll and
       the `TabPanel` never unmounts, so one transient 502 would disable the picker for the page's life.
       An outage is what the retry BUTTON (see `poolForm`) is for. */
    retry: 2,
  })

  const pool = poolQuery.data?.pool ?? null
  /* THE SHAPE IS CHECKED: `get<T>` is an unchecked cast, and a `.map is not a function` here
     white-screens the node route. */
  const observed = Array.isArray(pool?.observed) ? pool.observed : []
  const activeCount = observed.filter(runner => runner.active).length
  /* THE SAME GUARD: `orphaned_slots` is the newest field, so a cached pre-field body omits it. */
  const orphaned = Array.isArray(pool?.orphaned_slots) ? pool.orphaned_slots : []
  const projects = Array.isArray(projectsQuery.data) ? projectsQuery.data : []

  /* HAS THIS SESSION SEEN A POOL HERE? The row DELETES ITSELF once the node reports zero runners, so
     `pool` goes object -> `null` with no event; this drives the one "removed" line instead of silently
     reverting to "no pool yet".

     `isFetchedAfterMount` IS THE WHOLE GUARD: this key is persisted to localStorage for 24h
     (`queryClient.ts` + `main.tsx`) and the `TabPanel` mounts on every page load, so the first paint is a
     rehydrated entry, not an answer. Gating on the fetch (not on a successful DELETE) also covers a
     removal another seat asked for. */
  const [sawPool, setSawPool] = useState(false)
  const fetchedAfterMount = poolQuery.isFetchedAfterMount
  useEffect(() => {
    if (pool !== null && fetchedAfterMount) setSawPool(true)
  }, [pool, fetchedAfterMount])
  const justRemoved = sawPool && pool === null && !poolQuery.isLoading

  /* A REFUSED WRITE'S BANNER MUST CLEAR WHEN THE WORLD MOVES: `write()` only clears it at the next write,
     so a DELETE racing the row's own deletion left red and info banners disagreeing. MATERIALLY changed
     only — `observed_at` and the observed list move every pass and would wipe a 422 unread. What counts:
     the four declared fields, the phase, the server's error string, and the pool ceasing to exist. */
  const poolMark = pool === null
    ? 'none'
    : `${pool.project_id}/${pool.credential_id}/${pool.count}/${pool.state}/${pool.phase}/${pool.last_error}`
  useEffect(() => {
    setWriteError('')
  }, [poolMark])

  const loadError = poolQuery.isError
    ? apiDetail(poolQuery.error, 'Failed to load this node’s GitHub Actions runners.')
    : ''
  const projectsError = projectsQuery.isError
    ? apiDetail(projectsQuery.error, 'Failed to load the projects.')
    : ''

  const form = draft ?? (pool !== null ? draftFrom(pool) : BLANK_DRAFT)

  /* The saved credential's label, denormalised by the server's join. MEMOISED: `ui/CredentialSelect`
     watches this prop by identity. */
  const selectedCredential = useMemo<CredentialOption | null>(() => {
    if (pool === null || pool.credential_id !== form.credential_id) return null
    return { id: pool.credential_id, name: pool.credential_name }
  }, [pool, form.credential_id])

  const countRaw = form.count.trim()
  const countNum = Number(countRaw)
  /* SHAPE FIRST, THEN RANGE, each with its own message. The shape test is `+`, not `{1,2}`: a length
     bound in the shape told someone who typed `100` their characters were wrong. */
  const countInvalid =
    countRaw === ''
      ? 'Enter a runner count.'
      : !/^[0-9]+$/.test(countRaw)
        ? 'A whole number of runners, with no spaces or symbols.'
        : countNum < MIN_COUNT || countNum > MAX_COUNT
          ? `Must be between ${MIN_COUNT} and ${MAX_COUNT} runners.`
          : ''
  // Live once anything is typed; for the empty case only after a refused submit.
  const countError = countInvalid !== '' && (showErrors || countRaw !== '') ? countInvalid : ''
  const projectError = showErrors && form.project_id === '' ? 'Choose the repository these runners register against.' : ''
  const credentialError = showErrors && form.credential_id === '' ? 'Choose the GitHub credential.' : ''

  function setField<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft({ ...form, [key]: value })
    setShowErrors(false)
  }

  const running = (key: string) => busy.has(key)
  const anyBusy = busy.size > 0

  /** Every write goes through here: one guard, one banner, one refetch, one busy key. The permission is
   *  checked here too, since a programmatic submit bypasses the button. Presentation only — `authz_guard`
   *  decides on the route. */
  async function write(
    key: string,
    allowed: boolean,
    failure: string,
    action: () => Promise<unknown>,
  ): Promise<boolean> {
    if (!allowed) return false
    setWriteError('')
    setBusy(prev => withAdded(prev, key))
    try {
      await action()
      await poolQuery.refetch()
      return true
    } catch (err) {
      // The server's own sentence whenever it sent one.
      setWriteError(apiDetail(err, failure))
      return false
    } finally {
      setBusy(prev => withRemoved(prev, key))
    }
  }

  /** Apply — the form submitted whole. `state` carries FORWARD so editing a paused pool does not start
   *  it. */
  async function applyDraft() {
    if (!canEdit) return
    if (form.project_id === '' || form.credential_id === '' || countInvalid !== '') {
      setShowErrors(true)
      return
    }
    const state = pool !== null && pool.state === 'paused' ? 'paused' : 'running'
    const ok = await write('apply', canEdit, 'Failed to apply the runner pool.', () =>
      putGhaRunnerPool(node, {
        project_id: Number(form.project_id),
        credential_id: form.credential_id,
        count: countNum,
        state,
      }))
    if (ok) {
      setShowErrors(false)
      setSetupOpen(false)
    }
  }

  /** Pause / Resume / restore — the same upsert with `state` changed and the rest read off the ROW, not
   *  the form. `restore` writes the same `running` as Resume but is a different press (failure sentence,
   *  busy key): the one way back out of `absent`. */
  async function declareState(intent: 'pause' | 'resume' | 'restore') {
    if (!canEdit || pool === null) return
    const next = intent === 'pause' ? 'paused' : 'running'
    const failure =
      intent === 'pause' ? 'Failed to pause the runner pool.'
        : intent === 'resume' ? 'Failed to resume the runner pool.'
          : 'Failed to put the runner pool back — the removal is still the node’s instruction.'
    await write(intent, canEdit, failure,
      () => putGhaRunnerPool(node, {
        /* Straight off the row: DELETE only sets `desired_state`, so re-declaring these restores the
           shape the operator last chose. */
        project_id: pool.project_id,
        credential_id: pool.credential_id,
        count: pool.count,
        state: next,
      }))
  }

  async function removePool() {
    if (!canDelete || pool === null) return
    setWriteError('')
    /* THE SECOND DELETE IS NOT CONFIRMED AGAIN: it re-asks a removal already confirmed and underway. */
    const alreadyRemoving = pool.state === 'absent'
    /* THE NUMBER TO DESTROY IS THE ONE ON THE HOST, not the declared `count` (they diverge for a minute
       after every edit). With no report yet, no number is asserted. */
    const doomed = observed.length
    const scope = doomed > 0
      ? `All ${doomed} ${doomed === 1 ? 'runner' : 'runners'} on this host are`
      : 'Every runner this node is holding is'
    const ok = alreadyRemoving || await confirm({
      title: `Remove the GitHub Actions runners from ${nodeName}?`,
      /* THE NODE'S OWN NAME on the identity line — non-empty by construction, unlike `repo_url`, which
         rides along when set. Mono: tokens to match. */
      identity: (
        <Typography component="span" sx={monoSx}>
          {nodeName}{pool.repo_url === '' ? '' : ` · ${pool.repo_url}`}
        </Typography>
      ),
      /* WHAT ACTUALLY HAPPENS, in order: unregistered at GitHub, files deleted, each after its current
         job — a runner cannot be interrupted, and untold operators read the wait as a hang. */
      message:
        `${scope} unregistered from GitHub and their files are deleted from this `
        + 'host — each one only once it finishes the job it is currently on, because a self-hosted '
        + 'runner cannot be interrupted. Nothing in flight is killed, so this can take as long as the '
        + 'longest running job, and the pool stays on this page until the node reports that none are '
        + 'left. Put it back from that card while it runs. Setting it up again afterwards registers '
        + 'brand-new runners; nothing else on this node is touched.',
      confirmLabel: 'Remove runners',
      destructive: true,
    })
    if (!ok) return
    const done = await write('delete', canDelete, 'Failed to remove the runner pool.', async () => {
      try {
        await deleteGhaRunnerPool(node)
      } catch (err) {
        /* A 404 HERE IS THE REMOVAL HAVING ALREADY SUCCEEDED (the row deleted itself; the route's
           docstring says so). Swallowed for this status on this path only; `write()` still refetches.
           An invisible node is also a 404, unreachable from this button. */
        if (err instanceof ApiError && err.status === 404) return
        throw err
      }
    })
    if (done) {
      setDraft(null)
      setSetupOpen(false)
    }
  }

  /** THE LAST RESORT: `?force=true` drops the row without waiting for the node. Whatever is still
   *  registered stays registered, and this CP no longer holds the credential that could deregister it.
   *
   *  OFFERED ONLY FROM THE `absent` RECOVERY BLOCK. A staleness threshold was rejected (the server owns
   *  every threshold here) and so was `phase === 'error'` (a gone node never reaches it).
   *
   *  A CONFIRM ALWAYS: unlike the second DELETE, this destroys the record naming the credential. */
  async function purgePool() {
    if (!canDelete || pool === null) return
    const stranded = observed.length
    const ok = await confirm({
      title: `Drop the runner pool record for ${nodeName}?`,
      identity: (
        <Typography component="span" sx={monoSx}>
          {nodeName}{pool.repo_url === '' ? '' : ` · ${pool.repo_url}`}
        </Typography>
      ),
      /* THE ONE FACT THIS DIALOG EXISTS FOR: it does NOT remove the runners. The count is the node's
         last report, named as such. */
      message:
        'This removes the pool from the control plane WITHOUT removing anything from GitHub or from the '
        + 'host. '
        + (stranded > 0
          ? `The ${stranded} ${stranded === 1 ? 'runner' : 'runners'} the node last reported `
            + `${stranded === 1 ? 'stays' : 'stay'} registered on the repository and `
            + `${stranded === 1 ? 'keeps' : 'keep'} taking jobs`
          : 'Whatever is still registered on the repository stays registered and keeps taking jobs')
        + ', and the console will no longer hold the credential that could deregister them — you '
        + 'deregister them by hand, in the repository’s Settings → Actions → Runners, and delete their '
        + 'units on the host yourself. Use this only when the ordinary removal cannot finish: the node '
        + 'is gone, or nothing can read its units.',
      confirmLabel: 'Drop the record',
      destructive: true,
    })
    if (!ok) return
    const done = await write('purge', canDelete, 'Failed to drop the runner pool record.', async () => {
      try {
        await deleteGhaRunnerPool(node, { force: true })
      } catch (err) {
        // Same race and answer as `removePool`.
        if (err instanceof ApiError && err.status === 404) return
        throw err
      }
    })
    if (done) {
      /* After a forced drop the normal "no runners left on this host" line would describe a report
         nobody received, so this flag switches the closing line. */
      setPurged(true)
      setDraft(null)
      setSetupOpen(false)
    }
  }

  /* ── The form ── */

  // A saved project missing from this reader's roster would paint the select BLANK, so it is pushed in
  // as a disabled synthetic option named from `project_name` (as `NodeGroupDialog` does).
  const projectListed = projects.some(p => String(p.id) === form.project_id)
  const projectUnlisted = form.project_id !== '' && !projectListed

  const projectField = (
    <FormField
      label="Repository"
      error={projectError}
      hint={projectUnlisted ? 'Not among your projects; cannot be re-picked.' : undefined}
    >
      <TextField
        select
        id="gha-project"
        value={form.project_id}
        onChange={e => setField('project_id', e.target.value)}
        disabled={!canEdit || anyBusy || (projects.length === 0 && !projectUnlisted)}
        slotProps={{ htmlInput: { 'data-testid': 'gha-project-select' } }}
        sx={fieldTapTargetSx}
      >
        {projectUnlisted && (
          <MenuItem value={form.project_id} disabled>
            {pool?.project_name || `project ${form.project_id}`}
            <Box component="span" sx={{ ml: 1, color: 'text.disabled', fontSize: '0.75rem' }}>
              — not among the projects you can see
            </Box>
          </MenuItem>
        )}
        {projects.map(project => {
          const reason = projectReason(project)
          return (
            /* INELIGIBLE PROJECTS ARE OFFERED DISABLED WITH THE REASON on the item (the open menu covers
               the helper text). */
            <MenuItem key={project.id} value={String(project.id)} disabled={reason !== null}>
              {project.name}
              {reason !== null && (
                <Box component="span" sx={{ ml: 1, color: 'text.disabled', fontSize: '0.75rem' }}>
                  — {reason}
                </Box>
              )}
            </MenuItem>
          )
        })}
      </TextField>
    </FormField>
  )

  /* THE SHORT FORM of `RUNNER_ADMIN_SCOPE_HINT` (apps/api/app/github_api.py, its single home, which names
     the classic-PAT alternative too): the server's copy reaches an operator only after a failed save, this
     one while choosing. Keep the two FACTS in step — the scope, and that Connect cannot carry it. */
  const credentialField = (
    <FormField
      label="GitHub credential"
      error={credentialError}
      help="Fine-grained PAT for this repo: Administration read/write, Actions read. Not Connect."
    >
      <CredentialSelect
        id="gha-credential"
        testId="gha-credential-select"
        value={form.credential_id === '' ? null : form.credential_id}
        // `''` on the wire, `null` in the control.
        onChange={next => setField('credential_id', next ?? '')}
        fetchOptions={fetchCredentialOptions}
        selected={selectedCredential}
        disabled={!canEdit || anyBusy}
        placeholder="Search the stored credentials"
        noOptionsText="No credential matches"
      />
    </FormField>
  )

  const countField = (
    <FormTextField
      label="Runners"
      error={countError}
      id="gha-count"
      value={form.count}
      onChange={v => setField('count', v)}
      disabled={!canEdit || anyBusy}
      testId="gha-count-input"
      slotProps={{ htmlInput: { inputMode: 'numeric' } }}
      sx={monoSx}
    />
  )

  /** One icon verb: the word is the tooltip and the accessible name. The `span` keeps the tooltip
   *  reachable while the button is disabled (`.Mui-disabled` drops pointer events). */
  const verb = (
    testId: string,
    word: string,
    glyph: string,
    onClick: () => void,
    color: 'primary' | 'error' = 'primary',
  ) => (
    /* While a write runs the tooltip says which one, so a disabled verb is never unexplained. */
    <Tooltip title={running(testId) ? `${word}…` : anyBusy ? `${word} — a change is in flight` : word}>
      <span>
        <IconButton
          size="small"
          color={color}
          data-testid={testId}
          aria-label={word}
          onClick={onClick}
          disabled={anyBusy}
          sx={tapTargetSx}
        >
          <span aria-hidden="true">{glyph}</span>
        </IconButton>
      </span>
    </Tooltip>
  )

  const poolForm = (
    <DetailSection testId="gha-runners-form" divided={pool !== null}>
      {/* THE FAILED READ AN OPERATOR CAN ASK FOR AGAIN: the roster has no poll and the panel never
          unmounts. Inside the banner; not offered while a retry is in flight. */}
      {projectsError && (
        <Banner severity="error">
          {projectsError}{' '}
          <Button
            type="button"
            size="small"
            data-testid="gha-projects-retry"
            onClick={() => void projectsQuery.refetch()}
            disabled={projectsQuery.isFetching}
            sx={tapTargetSx}
          >
            {projectsQuery.isFetching ? 'Trying again…' : 'Try again'}
          </Button>
        </Banner>
      )}
      <FieldRow>
        {/* Bases fit each field's longest value; `ui/FieldRow` wraps. */}
        <DetailCell basis="18rem" max="26rem" sx={{ mb: 1.5 }}>{projectField}</DetailCell>
        <DetailCell basis="18rem" max="26rem" sx={{ mb: 1.5 }}>{credentialField}</DetailCell>
        <DetailCell basis="7rem" max="10rem" sx={{ mb: 1.5 }}>{countField}</DetailCell>
      </FieldRow>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <Button
          type="button"
          variant="contained"
          color="primary"
          size="small"
          data-testid="gha-apply"
          onClick={() => void applyDraft()}
          disabled={anyBusy}
          sx={tapTargetSx}
        >
          {running('apply') ? 'Applying…' : pool === null ? 'Create pool' : 'Apply'}
        </Button>
        {pool === null && (
          <Button
            type="button"
            size="small"
            data-testid="gha-cancel"
            onClick={() => { setSetupOpen(false); setDraft(null); setShowErrors(false) }}
            disabled={anyBusy}
            sx={tapTargetSx}
          >
            Cancel
          </Button>
        )}
      </Box>
    </DetailSection>
  )

  /* ── Render ── */

  /* THE GUARDED ARRAY GOES IN, never `pool` alone. */
  const state = pool !== null ? convergence(pool, observed, activeCount) : null
  /* GitHub's view per slot; the same array guard (`activity` is the newest field). */
  const activity = new Map(
    (Array.isArray(pool?.activity) ? pool.activity : []).map(entry => [entry.slot, entry] as const),
  )
  /* Each slot's recent jobs, keyed by its unit; the newest field, so guarded like `activity`. `null` when the
     server predates it, and a slot whose unit has no key (a co-located node, whose journal is never shipped):
     "No recent jobs" is a claim neither made, so nothing is said. A present `[]` is that claim. */
  const recentJobs: Record<string, GhaRunnerJob[]> | null =
    pool?.recent_jobs && typeof pool.recent_jobs === 'object' ? pool.recent_jobs : null
  /* HELD = what the node can still deregister itself: `_gha_surplus_slots`' rule (registered or active),
     so this row and the node's removal instruction cannot disagree. */
  const held = new Set(observed.filter(runner => runner.registered || runner.active).map(runner => runner.slot))
  /* ONE LIST: the node's slots, then any GitHub still lists above the count that the node no longer
     reports. An above-count slot the node still holds stays in its own row, marked. */
  const rows: { slot: string; dot: RunnerDot; seen: GhaRunnerActivity | undefined; runner?: GhaRunnerObserved }[] = [
    ...observed.map(runner => ({
      slot: runner.slot,
      runner,
      dot: orphaned.includes(runner.slot) ? orphanDot(held.has(runner.slot)) : runnerDot(runner, activity.get(runner.slot)),
      seen: activity.get(runner.slot),
    })),
    ...orphaned.filter(slot => !held.has(slot)).map(slot => ({
      slot, dot: orphanDot(false), seen: activity.get(slot),
    })),
  ]

  return (
    <Box component="section" data-testid="gha-runners-panel" sx={PANEL_SX}>
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {writeError && <Banner severity="error">{writeError}</Banner>}
      {/* THE SERVER'S OWN SENTENCE, VERBATIM. */}
      {pool !== null && pool.last_error !== '' && (
        <Banner severity="error" testId="gha-last-error">{pool.last_error}</Banner>
      )}
      {/* THE ROW DELETED ITSELF (`justRemoved`): INFO tone. Unless this session force-dropped the
          record, where the same `pool === null` likely means runners ARE still there. */}
      {justRemoved && (purged ? (
        <Banner severity="warning" testId="gha-purged">
          Record dropped. Runners still registered in GitHub keep taking jobs until removed there.
        </Banner>
      ) : (
        <Banner severity="info" testId="gha-removed">Runners removed.</Banner>
      ))}

      {poolQuery.isLoading && <LoadingState rows={3} />}

      {!poolQuery.isLoading && pool === null && !setupOpen && (
        <EmptyState
          title="No GitHub Runners"
          action={canEdit ? (
            <Button
              type="button"
              variant="contained"
              color="primary"
              size="small"
              data-testid="gha-setup"
              onClick={() => { setSetupOpen(true); setShowErrors(false) }}
              sx={tapTargetSx}
            >
              Set up GitHub Runners
            </Button>
          ) : undefined}
        />
      )}

      {pool !== null && state !== null && (
        <Box data-testid="gha-runners-status" sx={HEAD_SX}>
          {/* THE STATE IS AN ICON (a paused pool's also its word); what it is waiting on (pending states last minutes) is its tooltip. */}
          {/* Focusable and tap-opened, so the sentence reaches a keyboard and a phone, not only a mouse. */}
          {/* ONE TOOLTIP: the sentence reaches the chip as its accessible name, never a native `title` too,
              which would pop the browser's own tooltip over this one. */}
          <Tooltip title={state.sentence ? `${state.label}. ${state.sentence}` : `${state.label}.`} enterTouchDelay={0} leaveTouchDelay={6000}>
            <Box component="span" tabIndex={0} sx={{ display: 'inline-flex', borderRadius: 1 }}>
              <StatusChip
                iconOnly={!state.worded}
                label={state.label}
                tone={state.tone}
                glyph={state.glyph}
                data-status={`${pool.state}/${pool.phase}`}
                ariaLabel={state.sentence ? `${state.label}. ${state.sentence}` : `${state.label}.`}
                glyphTestId="gha-phase-glyph"
                labelTestId="gha-phase-label"
              />
            </Box>
          </Tooltip>
          <Box component="span" title="Up / declared" data-testid="gha-pool-count" sx={monoSx}>
            {activeCount}/{pool.count}
          </Box>
          {/* An identifier: mono. Not an empty cell when the project lost its URL. */}
          {namedRepo(pool) ? (
            <Link
              href={namedRepo(pool) ?? undefined}
              target="_blank"
              rel="noopener noreferrer"
              underline="hover"
              data-testid="gha-pool-repo"
              sx={[monoSx, { fontSize: '0.8125rem', overflowWrap: 'anywhere', minWidth: 0 }]}
            >
              {repoPath(namedRepo(pool) ?? '')}
            </Link>
          ) : (
            <Box component="span" title="No repository URL on this project" sx={MUTED_SX}>—</Box>
          )}
          {/* The form names the credential to an editor; a reader without the form still sees it. */}
          {!canEdit && <Box component="span" sx={MUTED_SX}>{pool.credential_name}</Box>}
          {/* AN AGE, NOT A TIMESTAMP ("is the daemon still reporting"); exact value on hover. */}
          <Box
            component="span"
            data-testid="gha-pool-reported"
            title={pool.observed_at === null ? 'Never reported' : `Reported ${absoluteTime(pool.observed_at)}`}
            sx={MUTED_SX}
          >
            {pool.observed_at === null ? '—' : relativeAge(pool.observed_at)}
          </Box>

          {/* ABSENT WEARS NO STEERING VERBS, but MUST wear a way back: restore, re-ask, and — last, the
              only one that abandons something — drop the record (`purgePool`; its confirm stops mistakes). */}
          {(canEdit || canDelete) && (
            <Box sx={ACTIONS_SX} data-testid={pool.state === 'absent' ? 'gha-absent-recovery' : undefined}>
              {pool.state === 'absent' ? (<>
                {canEdit && verb('gha-restore', 'Put the pool back', '↩', () => void declareState('restore'))}
                {canDelete && verb('gha-remove', 'Ask the node again', '↻', () => void removePool())}
                {canDelete && verb(
                  'gha-purge', 'Drop the record — runners stay registered in GitHub', '⊘',
                  () => void purgePool(), 'error',
                )}
              </>) : (<>
                {canEdit && pool.state === 'running'
                  && verb('gha-pause', 'Pause — after each current job', '⏸', () => void declareState('pause'))}
                {canEdit && pool.state === 'paused'
                  && verb('gha-resume', 'Resume', '▶', () => void declareState('resume'))}
                {canDelete && verb('gha-remove', 'Remove runners', '✕', () => void removePool(), 'error')}
              </>)}
            </Box>
          )}
        </Box>
      )}

      {pool !== null && (rows.length === 0 ? (
        /* Never reported vs reported empty. */
        <Typography variant="body2" data-testid="gha-no-runners" sx={MUTED_SX}>
          {pool.observed_at === null ? 'Not reported yet' : 'No runners'}
        </Typography>
      ) : (
        /* NAMED: otherwise announced as "list, 4 items". */
        <Box component="ul" aria-label={`Runners on ${nodeName}`} data-testid="gha-runners-list" sx={LIST_SX}>
          {rows.map(({ slot, dot, seen, runner }) => {
            const job = seen?.busy ? [seen.workflow_name, seen.job_name].filter(Boolean).join(' · ') : ''
            // GitHub writes this URL; linked only while it still points at GitHub.
            const jobUrl = seen?.job_url?.startsWith('https://github.com/') ? seen.job_url : null
            const slotJobs = runner && recentJobs && Array.isArray(recentJobs[runner.unit]) ? recentJobs[runner.unit] : null
            return (
              <Box component="li" key={slot} data-testid="gha-runner-row" data-status={dot.word} sx={ROW_SX}>
                <Box sx={ROW_LINE_SX}>
                  {/* THE WORD IS ON SCREEN; the detail (full name, unit, sub-state) is the tooltip — focusable
                      and tap-opened, and its one accessible name, never a native `title` too. */}
                  <Tooltip title={`${dot.word}. ${dot.detail}`} enterTouchDelay={0} leaveTouchDelay={6000}>
                    <Box
                      component="span"
                      tabIndex={0}
                      role="img"
                      aria-label={`${dot.word}. ${dot.detail}`}
                      data-testid="gha-runner-state"
                      sx={STATE_SX}
                    >
                      <StatusDot color={dot.color} hollow={dot.hollow} size={10} />
                      {dot.word}
                    </Box>
                  </Tooltip>
                  {/* A fixed column, so every job starts at one edge (`gha-10` is the widest slot). */}
                  <Box component="span" sx={[monoSx, { fontSize: '0.8125rem', flexShrink: 0, minWidth: '6ch' }]}>{slot}</Box>
                  {job !== '' && (jobUrl ? (
                    <Link
                      href={jobUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      underline="hover"
                      title={job}
                      data-testid="gha-runner-job"
                      sx={[JOB_SX, { color: 'primary.main' }]}
                    >
                      {job}
                    </Link>
                  ) : (
                    <Box component="span" title={job} data-testid="gha-runner-job" sx={JOB_SX}>{job}</Box>
                  ))}
                  {/* THE ONE REMEDY NO CONVERGE CAN REACH: a link, so it is reachable by tap and keyboard. */}
                  {dot.word === STRANDED && namedRepo(pool) && (
                    <Link
                      href={`${namedRepo(pool)}/settings/actions/runners`}
                      target="_blank"
                      rel="noopener noreferrer"
                      underline="hover"
                      color="error"
                      data-testid="gha-runner-stranded"
                      sx={{ fontSize: '0.8125rem', ml: 'auto', whiteSpace: 'nowrap' }}
                    >
                      Remove in GitHub ↗
                    </Link>
                  )}
                </Box>
                {/* A slot the node reports: its memory knobs and recent jobs. A slot only GitHub lists has
                    neither. */}
                {runner && (slotJobs || runner.oom_policy !== undefined || runner.memory_max !== undefined || runner.memory_high !== undefined) && (
                  <Box sx={SLOT_DETAIL_SX}>
                    <SlotMemory runner={runner} />
                    {slotJobs && <SlotJobs jobs={slotJobs} />}
                  </Box>
                )}
              </Box>
            )
          })}
        </Box>
      ))}

      {/* THE FORM: absent without `nodes:edit`, and withheld while the pool is `absent` (the recovery
          verbs' restore brings it back). */}
      {canEdit && pool !== null && pool.state !== 'absent' && poolForm}
      {canEdit && pool === null && setupOpen && poolForm}

      {confirmDialog}
    </Box>
  )
}

export default GhaRunnersTab
