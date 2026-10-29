/* Five-star rating on a done Task: hover lights, click commits, a brief sparkle, then it collapses to
 * the average with a per-voter breakdown on hover.
 * The caller decides eligibility (`lib/taskRating.ts:isRatableTask`); no `state`/`persona` props here.
 * One replaceable vote per person: clicking the summary reopens the stars pre-filled; `Esc` backs out.
 * No client-side average: the `PUT` returns the server aggregate (`types.ts`'s `TaskRating`), so a
 * failed `PUT` leaves nothing false on screen — stars stay open on the last accepted vote with a hint.
 * Geometry is the host's; content stays within 36px height. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Rating, { ratingClasses } from '@mui/material/Rating'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import { apiDetail, get, put } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { formatAverage, ratingSummaryTitle, voteListTitle } from '../../lib/taskRating'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import type { TaskRating as Rating5 } from '../../types'
import RatingBadge from './RatingBadge'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'

/* Last spark delay (150) + spark run (440) = 590, so 600 is the first tick with nothing animating.
   Change either and this follows. */
const SPARKLE_MS = 600

/* Spark centre (% of the star strip), size px, delay ms. Some sit just outside the strip on purpose
   (so the strip must not clip). Kept small (4-6px vs 18px stars). */
const SPARKS: readonly { left: string; top: string; size: number; delay: number }[] = [
  { left: '6%', top: '8%', size: 5, delay: 0 },
  { left: '28%', top: '92%', size: 4, delay: 70 },
  { left: '46%', top: '-10%', size: 6, delay: 130 },
  { left: '66%', top: '86%', size: 4, delay: 40 },
  { left: '88%', top: '4%', size: 5, delay: 150 },
  { left: '100%', top: '54%', size: 4, delay: 100 },
] as const

/* A four-point sparkle via clip-path: cheaper than SVG, and distinct from the rating's stars. */
const SPARK_CLIP =
  'polygon(50% 0%, 60% 40%, 100% 50%, 60% 60%, 50% 100%, 40% 60%, 0% 50%, 40% 40%)'

/** Render phase of the control. */
type Phase =
  /** Nothing in flight. The stars respond, or the summary sits collapsed. */
  | 'idle'
  /** The `PUT` is out. The stars still show the picked value; a second click is ignored. */
  | 'saving'
  /** The `PUT` landed and the sparks are firing. Collapses to the average when `SPARKLE_MS` elapses. */
  | 'celebrating'

/* Read at vote time, not subscribed. The CSS also refuses to paint under the same query; this JS read
   stops the stars being held open 600ms for an animation that will not run. `test/setup.ts` stubs
   `matchMedia` at `matches: false`, so tests take the animating path. */
function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches === true
}

export default function TaskRating({
  taskId,
  canVote,
  hideLabel = false,
  sx,
  onRated,
}: {
  taskId: number
  /**
   * May the caller vote. False → a read-only summary with no pointer affordance. Presentation only; the
   * server refuses the `PUT` regardless.
   */
  canVote: boolean
  /**
   * Keep the "Rate result" caption for assistive tech only, where the host already says it (a dialog's title) or
   * has no room for it (beside the task page's tabs, which then name it on the stars' `title`).
   */
  hideLabel?: boolean
  /**
   * Host geometry, applied first.
   */
  sx?: SxProps<Theme>
  /** The aggregate after the caller's own vote lands, so a host showing it elsewhere (the task page's folded
   *  badge) moves with the vote rather than waiting for a reload. */
  onRated?: (rating: Rating5) => void
}): JSX.Element | null {
  const client = useQueryClient()
  const key = ['task-rating', taskId]
  const read = useApiRead(
    key,
    async () => takeTaskPageSeed<Rating5>(taskId, 'rating') ?? await get<Rating5>(`/api/tasks/${taskId}/rating`),
    'Could not load the rating',
  )
  /* Only a server answer FOR THIS TASK: the read holds the previous key's answer while the next loads (and
     through a failed read), and task A's average must not show on task B; a failed read draws nothing. A landed
     vote is written into the cache (`setQueryData` below), so a remount paints the voted aggregate, never the
     pre-vote one, and a slow GET landing after the PUT is superseded by the write. */
  const data = read.loading || read.error ? null : read.data
  /* The `PUT`'s refusal only (one short line, not a banner — 36px strip). */
  const [error, setError] = useState('')
  const [phase, setPhase] = useState<Phase>('idle')
  /* A reopened existing vote — distinct from never-voted because `Esc` behaves differently. */
  const [reopened, setReopened] = useState(false)
  /* Value in flight, kept apart from `data` (only ever a server response), so failure has nothing to roll back. */
  const [picked, setPicked] = useState<number | null>(null)

  /* The task on screen, read when a `PUT` lands: one for a task navigated away from is dropped. */
  const taskIdRef = useRef(taskId)
  taskIdRef.current = taskId
  // Collapse timer, cleared on unmount.
  const sparkleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    /* Reset all state on task change: `/tasks/:id` has no `key`, so this instance re-renders with a new
       `taskId`. Otherwise an in-flight vote leaves `phase` stuck at `'saving'` (its task check returns
       early), blocking every later click. */
    setError('')
    setPhase('idle')
    setReopened(false)
    setPicked(null)
  }, [taskId])

  useEffect(
    () => () => {
      if (sparkleTimerRef.current !== null) clearTimeout(sparkleTimerRef.current)
    },
    [],
  )

  const submit = useCallback(
    async (stars: number) => {
      // Reentry guard, not `disabled`: that greys the stars and swaps MUI inputs for spans, moving focus.
      if (phase !== 'idle') return
      setPicked(stars)
      setPhase('saving')
      setError('')
      try {
        const fresh = await put<Rating5>(`/api/tasks/${taskId}/rating`, { stars })
        if (taskIdRef.current !== taskId) return
        client.setQueryData(key, fresh)
        onRated?.(fresh)
        if (prefersReducedMotion()) {
          // No sparks, so collapse immediately.
          setPhase('idle')
          setReopened(false)
          setPicked(null)
          return
        }
        setPhase('celebrating')
        sparkleTimerRef.current = setTimeout(() => {
          sparkleTimerRef.current = null
          setPhase('idle')
          setReopened(false)
          setPicked(null)
        }, SPARKLE_MS)
      } catch (err) {
        if (taskIdRef.current !== taskId) return
        /* Stay open (`reopened`) to retry; drop `picked` so the stars show the last accepted vote, not the
           failed one (pinned by `TaskRating.test.tsx`). */
        setPhase('idle')
        setPicked(null)
        setReopened(true)
        setError(apiDetail(err, 'Could not save your rating'))
      }
    },
    [phase, taskId, onRated],
  )

  /* Nothing before the first response (a width-changing placeholder would shove the tabs), and nothing when
     the read fails: with no rating to show, a lone "unavailable" caption hung detached beside the tabs. */
  if (data === null) return null

  const { average, count, my_stars: myStars, votes } = data
  const showStars = canVote && (phase !== 'idle' || reopened || myStars === null)

  const errorHint = error !== '' && (
    /* Compact in-strip notice; server detail on `title`. */
    <Typography
      component="span"
      variant="caption"
      data-testid="task-rating-error"
      title={error}
      sx={{ color: 'error.main', whiteSpace: 'nowrap', lineHeight: 1 }}
    >
      not saved
    </Typography>
  )

  /* Nothing to show and nothing to vote: render nothing (not "Not rated yet"). Uses `RatingBadge`'s own
     test via `formatAverage`, so a null/non-finite average beside a positive count does not produce an
     invisible `ButtonBase` a screen reader still announces. */
  const nothingToShow = count <= 0 || formatAverage(average) === ''
  if (!showStars && nothingToShow) return error === '' ? null : <Box component="span">{errorHint}</Box>

  /* Breakdown as one plain string: with `describeChild`, MUI mirrors it to the child's native `title`
     when closed, reaching screen readers, touch and jsdom. `pre-line` renders the newlines. */
  const breakdown = [
    ratingSummaryTitle(count, average),
    voteListTitle(votes),
  ]
    .filter(line => line !== '')
    .join('\n')

  return (
    <Box
      component="span"
      data-testid="task-rating"
      sx={[
        {
          display: 'inline-flex',
          alignItems: 'center',
          gap: 0.75,
          // Sparks are positioned against this box and some sit outside it — no clipping.
          position: 'relative',
          minWidth: 0,
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {/* Names what the stars rate: the task's result. */}
      <Typography
        component="span"
        variant="caption"
        data-testid="task-rating-label"
        sx={hideLabel ? VISUALLY_HIDDEN : { color: 'text.secondary', whiteSpace: 'nowrap' }}
      >
        {showStars ? 'Rate result' : 'Result'}
      </Typography>
      {showStars ? (
        <>
          <Box
            component="span"
            sx={{ display: 'inline-flex', alignItems: 'center', position: 'relative' }}
            title={hideLabel ? 'Rate result' : undefined}
            /* `Esc` backs out of a reopened control only; before a first vote there is nothing to return to. */
            onKeyDown={event => {
              if (event.key === 'Escape' && reopened && phase === 'idle') {
                setReopened(false)
                setPicked(null)
                setError('')
              }
            }}
          >
            <Rating
              /* Unique per control: MUI groups the inputs into one radio group by `name`. */
              name={`task-rating-${taskId}`}
              data-testid="task-rating-stars"
              size="small"
              max={5}
              value={picked ?? myStars}
              onChange={(_event, value) => {
                /* MUI sends `null` on clicking the selected star. No un-vote (API takes 1..5), so never
                   send it; from `reopened`, collapse — the re-affirmed vote is already saved. */
                if (value !== null) {
                  void submit(value)
                } else if (reopened && phase === 'idle') {
                  setReopened(false)
                  setPicked(null)
                  setError('')
                }
              }}
              sx={[
                theme => ({
                  /* Filled tone matches `RatingBadge`'s amber, not MUI's `#faaf00`. */
                  color: '#b45309',
                  ...theme.applyStyles('dark', { color: '#fcd34d' }),
                  /* Quiet empty tone (`text.disabled`) beside tab navigation. */
                  [`& .${ratingClasses.iconEmpty}`]: { color: 'text.disabled' },
                  /* Keep MUI's fill-to-pointer; drop its `scale(1.2)` on hover/active, which jumps ~4px
                     and clips in a 36px strip. */
                  [`& .${ratingClasses.iconHover}`]: { transform: 'none' },
                  [`& .${ratingClasses.iconActive}`]: { transform: 'none' },
                  // `display: flex` removes the inline baseline gap.
                  display: 'flex',
                  fontSize: '1.125rem',
                  // Busy tint only; the reentry guard blocks the second click.
                  opacity: phase === 'saving' ? 0.75 : 1,
                  transition: theme.transitions.create('opacity', { duration: 120 }),
                  // No hover highlights during the celebration.
                  pointerEvents: phase === 'celebrating' ? 'none' : undefined,
                }),
              ]}
            />
            {phase === 'celebrating' && (
              <Box component="span" data-testid="task-rating-sparkle" aria-hidden="true">
                {SPARKS.map(spark => (
                  <Box
                    key={`${spark.left}-${spark.top}`}
                    component="span"
                    sx={theme => ({
                      position: 'absolute',
                      left: spark.left,
                      top: spark.top,
                      width: spark.size,
                      height: spark.size,
                      clipPath: SPARK_CLIP,
                      backgroundColor: '#b45309',
                      ...theme.applyStyles('dark', { backgroundColor: '#fde68a' }),
                      // Never eat a click.
                      pointerEvents: 'none',
                      opacity: 0,
                      /* Grow, then drift up and fade. `translate(-50%, …)` makes `left`/`top` the centre;
                         `forwards` holds opacity 0 so a spark does not flash again. */
                      '@keyframes taskRatingSpark': {
                        '0%': { opacity: 0, transform: 'translate(-50%, -50%) scale(0.2) rotate(0deg)' },
                        '30%': { opacity: 1, transform: 'translate(-50%, -60%) scale(1) rotate(35deg)' },
                        '100%': { opacity: 0, transform: 'translate(-50%, -150%) scale(0.3) rotate(90deg)' },
                      },
                      animation: `taskRatingSpark 440ms ${spark.delay}ms ease-out forwards`,
                      /* CSS guard independent of `prefersReducedMotion` (which only decides the delay). */
                      '@media (prefers-reduced-motion: reduce)': { display: 'none' },
                    })}
                  />
                ))}
              </Box>
            )}
          </Box>
          {/* Show the average beside the stars when others voted and this operator has not, so the vote
              is not cast blind. Once voted, the badge is the collapsed control. */}
          {myStars === null && count > 0 && <RatingBadge average={average} count={count} />}
        </>
      ) : (
        <Tooltip
          title={breakdown}
          /* `describeChild`: tooltip becomes the description, not the name, and MUI mirrors the string to
             the native `title` when closed (hand-setting `title` would double it). */
          describeChild
          enterDelay={200}
          slotProps={{ tooltip: { sx: { whiteSpace: 'pre-line' } } }}
        >
          {canVote ? (
            <ButtonBase
              data-testid="task-rating-summary"
              /* Name leads with the score and ends with the affordance; the breakdown follows as description. */
              aria-label={`${ratingSummaryTitle(count, average)}. Change your rating`}
              onClick={() => {
                setReopened(true)
                setError('')
              }}
              /* The 36px strip is below the 44px tap floor (`theme.custom.tapTargetPx`); padding widens the
                 hit area instead. */
              sx={{
                px: 0.5,
                py: 0.25,
                borderRadius: 0.5,
                cursor: 'pointer',
                '&:hover': { backgroundColor: 'action.hover' },
              }}
            >
              {/* `title={null}`: the Tooltip already writes this element's `title` (see `RatingBadge`). */}
              <RatingBadge average={average} count={count} title={null} testId="task-rating-badge" />
            </ButtonBase>
          ) : (
            /* Read-only: no button affordance; the breakdown still opens on hover. */
            <Box component="span" data-testid="task-rating-summary" sx={{ display: 'inline-flex' }}>
              <RatingBadge average={average} count={count} title={null} testId="task-rating-badge" />
            </Box>
          )}
        </Tooltip>
      )}

      {errorHint}
    </Box>
  )
}
