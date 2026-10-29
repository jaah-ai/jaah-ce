import { Link as RouterLink } from 'react-router-dom'
import Link from '@mui/material/Link'
import { compactAge, NO_VALUE } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import { TaskLink } from '../../ui/DetailFields'
import { AGE_SLOT } from '../../ui/feed/geometry'
import type { FeedNote, FeedRowSpec, FeedSlot, OverflowItem } from '../../ui/feed/types'
import type { CronRun } from './api'
import { OUTCOME_EXPLANATION, OUTCOME_GLYPH, OUTCOME_TONE, STILL_RUNNING, readRun, readSpans, skipClause } from './vocabulary'

/* ONE FIRING AS A FEED ROW — Audit > Schedule's fleet-wide ledger.
 * NOT ONE WORD OF THE VOCABULARY IS DECIDED HERE: `./vocabulary.ts` (`readRun`, `readSpans`, `OUTCOME_*`)
 * is its home.
 * THE ROW: the sentence is the title (two lines), the outcome the state mark (the skip reason, in words, its hover). The note
 * and "+N more lines" are its hover (`notes`). Trailing: the caller's `lead` slots (gate them `meta`), then
 * duration, the filed task and the age. The task slot is `id`-gated, ALLOWED ONLY BECAUSE THE ⋯ CARRIES THE
 * SAME DESTINATION (`ui/feed/types.ts`); `to`, not `href` (an `href` item opens a new tab). */

/* THE CRON'S LINK CARRIES NO TAP FLOOR: a 44px minimum on an inline word would outgrow its row
 * (`ui/feed/geometry.ts`); the row is the large target. */
const CRON_LINK_SX = { color: 'inherit', fontWeight: 600 } as const

export function cronRunRow(run: CronRun, {
  zone,
  testIdPrefix,
  cron,
  lead = [],
}: {
  /** The zone the schedule was evaluated in — the CP's, not the viewer's; every instant names it. */
  zone: string
  /** `cron-run` → `cron-run-fired`, `cron-run-task-link`, … */
  testIdPrefix: string
  /** A fleet-wide ledger: the firing's cron, linked ahead of the sentence (two crons can share one). */
  cron?: { id: string; name: string }
  lead?: FeedSlot[]
}): FeedRowSpec {
  const reading = readRun(run)
  const spans = readSpans(run)
  const firedAt = formatZoned(run.fired_at, zone)
  const where = 'open this firing on the cron’s Runs tab'

  /* A note (`readRun`) and the journal count can both apply; without the count a headline reads complete. */
  const notes: FeedNote[] = []
  if (reading.note) notes.push({ key: 'note', text: reading.note, tone: 'muted' })
  if (reading.moreLines > 0) {
    notes.push({
      key: 'more',
      tone: 'muted',
      text: reading.moreLines === 1
        ? `+1 more line — ${where} to read it`
        : `+${reading.moreLines} more lines — ${where} to read them`,
    })
  }

  const actions: OverflowItem[] = []
  if (run.task_id !== null) {
    actions.push({ key: 'task', icon: '↗', label: `Open task #${run.task_id}`, ariaLabel: `Open task #${run.task_id}`, to: `/tasks/${run.task_id}` })
  }

  return {
    key: run.id,
    indicator: {
      tone: OUTCOME_TONE[run.outcome] ?? 'neutral',
      glyph: OUTCOME_GLYPH[run.outcome] ?? '?',
      label: run.outcome,
      title: run.skip_reason
        ? `${run.outcome} — ${skipClause(run.skip_reason)}`
        : OUTCOME_EXPLANATION[run.outcome] ? `${run.outcome} — ${OUTCOME_EXPLANATION[run.outcome]}` : run.outcome,
      /* The token an oracle reads. */
      data: { outcome: run.outcome, skip: run.skip_reason ?? undefined },
    },
    title: cron
      ? (
        <>
          <Link component={RouterLink} to={`/crons/${cron.id}`} underline="hover" sx={CRON_LINK_SX} data-testid={`${testIdPrefix}-cron-link`}>
            {cron.name}
          </Link>
          {` — ${reading.sentence}`}
        </>
      )
      : reading.sentence,
    titleText: cron ? `${cron.name} — ${reading.sentence}` : reading.sentence,
    /* Two lines at every width: a skipped firing's sentence ("… the previous firing had not returned yet …") was
       cut mid-word on one line at phone width. */
    titleLines: 2,
    /* Names the instant — the only thing distinguishing rows of one cadence — and makes the disc decorative. */
    ariaLabel: cron
      /* The sentence too: two crons can share one name's cadence, and the sentence tells them apart. */
      ? `${cron.name} — ${run.outcome} — ${reading.sentence}`
      : `Firing at ${firedAt} — ${run.outcome}`,
    slots: [
      ...lead,
      {
        key: 'duration',
        width: 62,
        gate: 'id',
        align: 'right',
        mono: true,
        /* `NO_VALUE` and `running…` are prose, so dim. */
        dim: spans.duration === STILL_RUNNING || spans.duration === NO_VALUE,
        testId: `${testIdPrefix}-duration`,
        node: spans.duration,
      },
      {
        /* A real anchor (middle-click, copy link) to the task's own page; `FeedList` ignores row presses
           inside an `<a>`. The verb is the name, `#id` the ink. */
        key: 'task',
        width: 58,
        gate: 'id',
        align: 'right',
        testId: `${testIdPrefix}-task`,
        node: run.task_id === null ? null : (
          <TaskLink taskId={run.task_id} aria-label={`Open task #${run.task_id}`} testId={`${testIdPrefix}-task-link`} />
        ),
      },
      {
        /* `compactAge` (≤4 chars fits `AGE_SLOT`) with the zoned instant on hover. */
        key: 'fired',
        width: AGE_SLOT,
        align: 'right',
        mono: true,
        dim: true,
        testId: `${testIdPrefix}-fired`,
        title: `Fired ${firedAt}`,
        node: compactAge(run.fired_at),
      },
    ],
    notes: notes.length > 0 ? notes : undefined,
    actions: actions.length > 0 ? actions : undefined,
  }
}
