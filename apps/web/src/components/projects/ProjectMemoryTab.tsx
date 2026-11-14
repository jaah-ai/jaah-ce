import { type JSX, useId, useLayoutEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { apiDetail } from '../../api'
import { customerUrl } from '../../lib/customers'
import { countOf } from '../../lib/humanize'
import { offPlan, OFF_PLAN_ONCE, planSwitchOn } from '../../lib/planSwitches'
import { BILLING_PAGE } from '../../lib/companySetup'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { shortSha } from '../../lib/shortSha'
import { useAuth } from '../../auth'
import { useRowActions } from '../../hooks/useBusyIds'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS, SIZE } from '../../theme'
import { danger, useConfirm } from '../ConfirmDialog'
import { useWideDetailPane } from '../ListDetail'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import FormTextField from '../../ui/FormTextField'
import LoadingState from '../../ui/LoadingState'
import SearchBox from '../../ui/SearchBox'
import StatusChip from '../../ui/StatusChip'
import {
  MEMORY_FILTERS_CONTAINER,
  MEMORY_FILTERS_WIDE,
  memoryFiltersOneRow,
  monoSx,
  PHONE,
  tapTargetSx,
} from '../../ui/listStyles'
import { OverflowMenu } from '../../ui/OverflowMenu'
import {
  feedChips,
  getFeedStatus,
  memoryFeedsKey,
  heldCount,
  isHeld,
  NEEDS_REPRO,
  groupChunks,
  listUnits,
  MEMORY_KINDS,
  MEMORY_MODEL_SETTINGS,
  MEMORY_SORTS,
  MEMORY_STATUS_OPTIONS,
  NO_MODEL,
  purgedNotice,
  purgeUnit,
  releaseUnit,
  retireUnit,
  STATUS_HELD,
  STATUS_LIVE,
  type MemorySort,
  type MemoryUnit,
} from './projectMemory'
import { TaskLink } from '../../ui/DetailFields'
import LinkButton from '../../ui/LinkButton'
import Tip from '../../ui/Tip'

/* The project edit page's MEMORY tab (D-6): what runners of this project have learned, and the two operator
 * actions on it. Feed chips first (is capture running at all, and why not), then the unit table.
 * Each unit is a list item: its title on a line of its own, two lines then an ellipsis with the whole title on
 * hover — never clipped to a few characters — and its labelled facts wrapping beneath, so the
 * page reads the same in the edit drawer, on desktop and at 360px (one column). Its counts are one group that stays
 * on one line from `sm` up (a phone may wrap it rather than scroll sideways): Injected, Seen, Used, Wrong — distinct
 * tasks each, defined once on the server — and Sort orders by any of them.
 * Status opens on Live (`STATUS_LIVE`), so the list starts on units still in play; All is one pick away, and Held
 * (`STATUS_HELD`) lists the accepted units the rules-only canon check holds back; the held count above the list links
 * there. The filters sit on one row when the tab is wide enough, else Search on a row of its own and Kind, Status and
 * Sort sharing the next (`filtersSx`) — on a phone and in the ~440px list-detail pane alike. A row prints its status
 * reason in prose, muted and last in its facts — a phone cannot hover — and the chip's hover names only the status. A
 * directory's code-map chunks of one generation fold into one row (`groupChunks`) that expands to the rest — under
 * Newest only, so a count sort's ranking is never hidden inside a fold. Body, or a tap on the title, expands the row:
 * the whole title and the unit's text (its `Repro:` line included). A title the clamp actually cuts gets a ▾ Show more
 * cue beside it; one that fits gets none.
 * Its rows read best wide, so in the list/detail split it asks for the widest pane (`useWideDetailPane`).
 * A project with more than one repository (`repos`) gets a Repository filter and a repository label on each row;
 * a single-repo project shows neither.
 * A row's actions sit on its title's line, so a row never spends a line on them: Body, then — with `projects:edit`
 * (`canEdit`) — Retire one tap away (and Release on a held unit: it clears the hold) and Purge behind the row's ⋯
 * menu, asking first, in-page, because it cannot be undone.
 * Nothing optimistic: both refetch the list.
 * Off the org's plan (`project_memory_enabled`), the memory routes answer 403: the tab says so calmly instead of
 * an error, in one line naming what it would list and where the plan changes — the org's Overrides tab when `orgId` is
 * given (an operator, on any org's project), Billing for a tenant's org admin. The refusal is the answer for the
 * page open (`OFF_PLAN_ONCE`, the status kept past the tab's unmount): re-entering the tab asks nothing again.
 * Purge alone stays open off the plan: an editor gets one field to purge a unit by its `mem-N` ref,
 * since with the list gated the ref a prompt or transcript cites is all an erasure request has. */

/* What a count sort over all-zero counts says, so it does not look broken. */
const NONE_COUNTED: Record<Exclude<MemorySort, 'newest'>, string> = {
  injected: 'No unit injected yet.',
  seen: 'No unit seen yet.',
  used: 'No unit used yet.',
  wrong: 'No unit flagged wrong.',
}

/* The filters: one row when the tab is wide enough for all of them, else Search and the Repository filter (when there
 * is one) on rows of their own and Kind, Status and Sort a third each of the next — never one select stranded under
 * the rest. Measured on the tab, not the viewport, as the side nav and the list/detail split take a varying share.
 * On a wide tab (`MEMORY_FILTERS_WIDE`) Kind, Status and Sort stop growing at the row's end and Search and Repository take the
 * rest, so a select never stretches across a wide page. */
const filtersSx = (count: number) => ({
  display: 'grid',
  gap: 1,
  alignItems: 'flex-start',
  gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
  [memoryFiltersOneRow(count)]: { gridTemplateColumns: `minmax(${grid(32)}, 2fr) repeat(${count - 1}, minmax(${grid(21)}, 1fr))` },
  [MEMORY_FILTERS_WIDE]: { gridTemplateColumns: `minmax(${grid(32)}, 1fr) repeat(3, ${grid(26.5)})${count > 4 ? ` minmax(${grid(21)}, 1fr)` : ''}` },
})
const FILTER_SX = { minWidth: 0 }
/** A filter that takes a whole row until the tab fits them all on one. */
const fullRowSx = (count: number) => ({ minWidth: 0, gridColumn: '1 / -1', [memoryFiltersOneRow(count)]: { gridColumn: 'auto' } })

/* THE LIST'S SURFACE, ONE VALUE: the list paints it and the title's "more" cue fades into it, so the fade matches the
   rows on any pane — the project pane's own `background.default`, or a paper drawer, where an unpainted list showed
   the paper and the fade a shade off it. */
const LIST_SURFACE = 'var(--memory-list-surface)'
export const LIST_SX = {
  listStyle: 'none', m: 0, p: 0, border: 1, borderColor: 'divider', borderRadius: RADIUS.sm,
  '--memory-list-surface': (theme: Theme) => (theme.vars ?? theme).palette.background.default,
  bgcolor: LIST_SURFACE,
} as const

/** Two lines, then an ellipsis — the clamp every unit title gets. */
export const TITLE_CLAMP_SX = {
  display: '-webkit-box',
  WebkitLineClamp: 2,
  WebkitBoxOrient: 'vertical',
  overflow: 'hidden',
  overflowWrap: 'anywhere',
  minWidth: 0,
} as const
/** The same box with the clamp lifted: an expanded row shows the whole title. */
const TITLE_OPEN_SX = { overflowWrap: 'anywhere', minWidth: 0 } as const

export default function ProjectMemoryTab({
  projectId,
  canEdit,
  repos = [],
  orgId = null,
}: {
  projectId: number
  canEdit: boolean
  /** The project's org, for an operator (its own org's included): an off-plan notice links to its Overrides tab. */
  orgId?: string | null
  /** The project's connected repositories' `owner/name`, primary first. */
  repos?: string[]
}): JSX.Element {
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  const [q, setQ] = useState('')
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState<string>(STATUS_LIVE)
  const [sort, setSort] = useState<MemorySort>('newest')
  const [repo, setRepo] = useState('')
  useWideDetailPane()

  const multiRepo = repos.length > 1
  const unitsKey = ['project-memory-units', projectId, q, kind, status, sort, repo] as const
  // One small row per project, kept for the page open: its off-plan 403 is what stops every later ask.
  const feeds = useQuery({
    queryKey: memoryFeedsKey(projectId),
    queryFn: () => getFeedStatus(projectId),
    gcTime: Infinity,
    ...OFF_PLAN_ONCE,
  })
  /* The list waits for feed-status unless the viewer's own plan already says yes (a tenant on it): an off-plan
     project then never asks the gated list (a 403 per open). An operator viewing a tenant cannot know the
     tenant's plan, so it waits. Any other feed-status failure releases it: only the plan's 403 means "off". */
  const planKnownOn = admin?.is_operator !== true && planSwitchOn(admin, 'project_memory_enabled')
  const units = useQuery({
    queryKey: unitsKey,
    queryFn: () => listUnits(projectId, { q, kind, status, sort, repo }),
    ...OFF_PLAN_ONCE,
    enabled: planKnownOn || feeds.isSuccess || (feeds.failureCount > 0 && !offPlan(feeds.failureReason)),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['project-memory-units', projectId] })

  const { busy, error, notice, confirmDialog, retire, release, purge } = useUnitActions<MemoryUnit>({
    projectIdOf: () => projectId,
    refresh: released => Promise.all([
      refresh(), released && queryClient.invalidateQueries({ queryKey: memoryFeedsKey(projectId) }),
    ]),
  })

  const filtered = q !== '' || kind !== '' || repo !== '' || (status !== '' && status !== STATUS_LIVE)
  const listed = units.data ?? []
  /* A count sort over all-zero counts orders like Newest; say why rather than let it look broken. */
  const noneCounted = sort !== 'newest' && listed.length > 0 && listed.every(u => u[sort] === 0)
    ? NONE_COUNTED[sort] : null

  if (offPlan(feeds.error)) {
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
        <OffPlanNotice orgId={orgId} />
        {canEdit && <PurgeByRef projectId={projectId} />}
      </Box>
    )
  }

  return (
    <Box data-testid="project-memory-tab" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
      {confirmDialog}
      <FeedStatus loading={feeds.isLoading} error={feeds.error} data={feeds.data ?? null}
        onShowHeld={() => {
          // The count is the whole project's, so the list it opens drops the other filters.
          setQ('')
          setKind('')
          setRepo('')
          setStatus(STATUS_HELD)
        }} />

      <Box sx={{ containerType: 'inline-size', containerName: MEMORY_FILTERS_CONTAINER }}>
        <Box data-testid="memory-filters" sx={filtersSx(multiRepo ? 5 : 4)}>
          <SearchBox
            label="Search"
            ariaLabel="Search memory units"
            placeholder="Title, text or mem-N"
            value={q}
            onChange={setQ}
            sx={fullRowSx(multiRepo ? 5 : 4)}
            testId="memory-search"
          />
          <FormTextField
            label="Kind"
            value={kind}
            onChange={setKind}
            sx={FILTER_SX}
            testId="memory-kind-filter"
            options={[{ value: '', label: 'All' }, ...MEMORY_KINDS.map(k => ({ value: k, label: k }))]}
          />
          <FormTextField
            label="Status"
            value={status}
            onChange={setStatus}
            sx={FILTER_SX}
            testId="memory-status-filter"
            options={MEMORY_STATUS_OPTIONS}
          />
          <FormTextField
            label="Sort"
            value={sort}
            onChange={v => setSort(v as MemorySort)}
            sx={FILTER_SX}
            testId="memory-sort"
            options={MEMORY_SORTS}
          />
          {multiRepo && (
            <FormTextField
              label="Repository"
              value={repo}
              onChange={setRepo}
              sx={fullRowSx(5)}
              testId="memory-repo-filter"
              options={[{ value: '', label: 'All' }, ...repos.map(r => ({ value: r, label: r }))]}
            />
          )}
        </Box>
      </Box>

      {error && <Banner severity="error">{error}</Banner>}
      {notice && <Banner severity="success">{notice}</Banner>}
      {units.error && <Banner severity="error">{apiDetail(units.error, 'Could not load memory units')}</Banner>}

      {/* An empty list not yet fetched since mount is a persisted cache (a reload): skeleton, not "No live units". */}
      {units.isPending || (listed.length === 0 && units.isFetching && !units.isFetchedAfterMount) ? (
        <LoadingState rows={3} />
      ) : listed.length === 0 ? (
        filtered ? <EmptyState title="No matching units" />
          : status === STATUS_LIVE ? (
            <EmptyState
              title="No live units"
              action={<Button type="button" size="small" onClick={() => setStatus('')} sx={tapTargetSx}>Show all</Button>}
            />
          ) : <EmptyState title="No memory yet" />
      ) : (
        <>
          {noneCounted && (
            <Typography variant="body2" data-testid="memory-sort-none" sx={{ color: 'text.secondary' }}>
              {noneCounted}
            </Typography>
          )}
          <Box
            component="ul"
            aria-label="Memory units"
            data-testid="memory-units-list"
            sx={LIST_SX}
          >
            {/* Folding only under Newest: under a count sort each chunk keeps its own ranked place. */}
            {(sort === 'newest' ? groupChunks(listed) : listed.map(head => ({ head, rest: [] }))).map(({ head, rest }) => {
              const row = (unit: MemoryUnit, chunks?: MemoryUnit[]) => (
                <UnitRow
                  key={unit.id}
                  unit={unit}
                  chunks={chunks}
                  canEdit={canEdit}
                  busy={busy.has(unit.id)}
                  onRetire={() => void retire(unit)}
                  onRelease={() => void release(unit)}
                  onPurge={() => void purge(unit)}
                  renderChunk={chunk => row(chunk)}
                />
              )
              return row(head, rest)
            })}
          </Box>
        </>
      )}
    </Box>
  )
}

/** Off the plan, one line: what the tab would list and who turns it on — the operator in the org's Overrides tab, a
 *  tenant's org admin in Billing (a plan change), anyone else by asking an org admin. `allProjects`: no project is
 *  picked (Audit › Memory's All projects), so the line names "your projects". */
export function OffPlanNotice({ orgId, allProjects = false }: { orgId: string | null; allProjects?: boolean }): JSX.Element {
  const { admin } = useAuth()
  const link = (to: string, label: string) => <MuiLink component={RouterLink} to={to}>{label}</MuiLink>
  return (
    <Typography variant="body2" data-testid="project-memory-off-plan" sx={{ color: 'text.secondary' }}>
      Memory (what runners learn on {allProjects ? 'your projects' : 'this project'}) isn't in your plan.{' '}
      {orgId ? <>Turn it on in {link(customerUrl(orgId, { tab: 'overrides' }), 'Overrides')}.</>
        : isOrgAdminSeat(admin) ? <>Turn it on in {link(BILLING_PAGE, 'Billing')}.</>
        : 'An admin can turn it on.'}
    </Typography>
  )
}

/** Off the plan: purge one unit by its `mem-N` ref, the list being out of reach. Asks first, like a row's Purge. */
function PurgeByRef({ projectId }: { projectId: number }): JSX.Element {
  const [ref, setRef] = useState('')
  const { busy, error, notice, run } = useRowActions()
  const [confirm, confirmDialog] = useConfirm()
  const unit = ref.trim()

  async function purge() {
    if (!unit) return
    if (!await confirm(danger('Purge', {
      title: `Purge ${unit}?`,
      message: 'Deletes the unit for good and removes it from every stored runner prompt. This cannot be undone.',
    }))) return
    await run(unit, `Could not purge ${unit}`, () => purgeUnit(projectId, encodeURIComponent(unit)), out => {
      setRef('')
      return purgedNotice(out)
    })
  }

  return (
    <Box data-testid="memory-purge-by-ref" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      {confirmDialog}
      <Typography variant="body2">Erase a stored unit by its ref.</Typography>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <FormTextField
          label="Unit"
          help="Purge deletes it and scrubs it from stored prompts."
          placeholder="mem-N"
          value={ref}
          onChange={setRef}
          // Inside the project page's `<form>`: Enter purges here rather than submitting the page.
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); void purge() } }}
          sx={{ flex: `0 1 ${grid(32)}`, minWidth: grid(21) }}
          testId="memory-purge-ref"
          slotProps={{ htmlInput: { autoComplete: 'off', spellCheck: false } }}
        />
        <Button type="button" color="error" variant="outlined" onClick={() => void purge()}
          disabled={!unit || busy.size > 0} sx={tapTargetSx}>
          Purge
        </Button>
      </Box>
      {error && <Banner severity="error">{error}</Banner>}
      {notice && <Banner severity="success">{notice}</Banner>}
    </Box>
  )
}

export function UnitRow({
  unit,
  project,
  chunks = [],
  canEdit,
  busy,
  onRetire,
  onRelease,
  onPurge,
  renderChunk,
}: {
  unit: MemoryUnit
  /** Audit › Memory's org-wide list: the unit's project, a press that filters the list to it. */
  project?: { name: string; onPick: () => void }
  /** The directory's other code-map chunks, folded under this one until expanded. */
  chunks?: MemoryUnit[]
  canEdit: boolean
  busy: boolean
  onRetire: () => void
  /** Clears a canon hold (`isHeld`). */
  onRelease: () => void
  onPurge: () => void
  renderChunk: (chunk: MemoryUnit) => JSX.Element
}): JSX.Element {
  const [open, setOpen] = useState(false)
  /* Expanded: the title unclamped and the body shown. Body and a tap on the title both toggle it, so a phone,
     which cannot hover the clamped title, still reaches the whole of it. */
  const [expanded, setExpanded] = useState(false)
  const bodyOpen = expanded && Boolean(unit.body)
  /* Whether the clamped title hides any of itself: only then does it get a "more" cue. Measured while clamped
     (an expanded title fits by definition, so the last clamped answer stands) and again whenever it resizes. */
  const titleRef = useRef<HTMLParagraphElement>(null)
  const [clipped, setClipped] = useState(false)
  useLayoutEffect(() => {
    const el = titleRef.current
    if (!el || expanded) return
    const measure = () => setClipped(el.scrollHeight > el.clientHeight)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [expanded, unit.title])
  const chunksId = useId()
  const bodyId = useId()
  /* A list item, not a table row: the title owns the whole first line at every width (the edit page also
  opens in a ~550px drawer, where a title column shredded titles to a few letters), and the facts wrap
  beneath it, each with its label. */
  return (
    <Box
      component="li"
      data-testid="memory-unit-row"
      data-unit={unit.ref}
      sx={{ px: 1.5, py: 1, '&:not(:last-of-type)': { borderBottom: 1, borderColor: 'divider' } }}
    >
      {/* The actions share the title's line, so they never cost a row a line of their own. */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
        <Tip title={unit.title}><Typography ref={titleRef} variant="body2" component="p" data-testid="memory-unit-title"
          data-expanded={expanded} role="button" tabIndex={0} aria-expanded={expanded}
          onClick={() => { if (!window.getSelection()?.toString()) setExpanded(e => !e) }}
          onKeyDown={e => {
            if (e.key !== 'Enter' && e.key !== ' ') return
            e.preventDefault()
            setExpanded(open => !open)
          }}
          sx={[expanded ? TITLE_OPEN_SX : TITLE_CLAMP_SX, { fontWeight: FONT_WEIGHT.medium, m: 0, flex: 1, alignSelf: 'center', cursor: 'pointer', position: 'relative' }]}>
          {unit.title}
          {/* The cue sits ON the clamp's last line, faded in over the cut text; the title itself is the control,
              so the cue is not a second button beside Body. */}
          {clipped && !expanded && (
            <Box component="span" aria-hidden data-testid="memory-title-more"
              sx={{ position: 'absolute', right: 0, bottom: 0, pl: 3, color: 'primary.main', fontSize: FONT_SIZE.sm,
                lineHeight: 'inherit', background: `linear-gradient(to right, transparent, ${LIST_SURFACE} ${grid(4)})` }}>
              more
            </Box>
          )}
        </Typography></Tip>
        {(unit.body || canEdit) && (
          <Box component="span" data-testid="memory-unit-actions"
            sx={{ display: 'inline-flex', alignItems: 'center', flexShrink: 0, my: -0.5 }}>
            {/* One label in both states, so opening the body never reflows the title: the glyph and
                `aria-expanded` carry the state. On a phone the glyph alone, giving the title the width. */}
            {unit.body && (
              <Button type="button" size="small" onClick={() => setExpanded(e => !e)} aria-expanded={bodyOpen}
                aria-label="Body" aria-controls={bodyOpen ? bodyId : undefined} data-testid="memory-body-toggle"
                sx={[tapTargetSx, { minWidth: 0, gap: 0.5, [PHONE]: { minWidth: SIZE.tapTarget } }]}>
                <Box component="span" sx={{ [PHONE]: { display: 'none' } }}>Body</Box>
                <Box component="span" aria-hidden sx={{ display: 'inline-block', width: '1ch', textAlign: 'center' }}>
                  {bodyOpen ? '▴' : '▾'}
                </Box>
              </Button>
            )}
            {canEdit && isHeld(unit) && (
              <Button type="button" size="small" onClick={onRelease} disabled={busy} data-testid="memory-unit-release"
                aria-label={`Release ${unit.ref}`} sx={[tapTargetSx, { minWidth: 0 }]}>
                Release
              </Button>
            )}
            {canEdit && unit.status !== 'retired' && (
              <Button type="button" size="small" onClick={onRetire} disabled={busy} sx={[tapTargetSx, { minWidth: 0 }]}>
                Retire
              </Button>
            )}
            {/* Purge cannot be undone: behind the menu, a deliberate second tap, then its confirm. */}
            {canEdit && <OverflowMenu
              ariaLabel={`More actions for ${unit.ref}`}
              testId="memory-unit-menu"
              size="small"
              busy={busy}
              sx={tapTargetSx}
              items={[{ key: 'purge', label: 'Purge', ariaLabel: `Purge ${unit.ref}`, danger: true, testId: 'memory-unit-purge',
                onClick: onPurge }]}
            />}
          </Box>
        )}
      </Box>
      <Box
        sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, mt: 0.5,
          fontSize: FONT_SIZE.md, color: 'text.secondary' }}
      >
        <Box component="span" sx={[monoSx, { color: 'text.primary' }]}>
          <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{unit.ref}</Box> {unit.kind}
        </Box>
        {project && (
          <LinkButton onClick={project.onPick} data-testid="memory-unit-project" title={`Only ${project.name}`}
            sx={{ fontSize: 'inherit', overflowWrap: 'anywhere', textAlign: 'left' }}>
            {project.name}
          </LinkButton>
        )}
        {unit.repo && (
          <Box component="span" data-testid="memory-unit-repo"
            sx={[monoSx, { overflowWrap: 'anywhere', minWidth: 0 }]}>
            {unit.repo}
          </Box>
        )}
        <StatusChip
          label={unit.demoted_unused ? 'unused' : isHeld(unit) ? 'held'
            : unit.status_reason === NEEDS_REPRO ? NEEDS_REPRO
            : unit.injectable && unit.status === 'accepted' ? 'injectable' : unit.status}
          tone={unit.demoted_unused || isHeld(unit) || unit.status_reason === NEEDS_REPRO || unit.status === 'flagged'
            ? 'warning' : unit.status === 'accepted' ? 'success' : 'neutral'}
          title={unit.status}
        />
        <Fact label="Trust" value={unit.trust.toFixed(2)} />
        <Box component="span" data-testid="memory-unit-counts"
          sx={{ display: 'inline-flex', flexWrap: { xs: 'wrap', sm: 'nowrap' }, columnGap: 1,
            whiteSpace: { xs: 'normal', sm: 'nowrap' } }}>
          <Fact label="Injected" value={unit.injected} unit="tasks" title="In the prompt" />
          <Fact label="Seen" value={unit.seen} unit="tasks" title="Shown by the hook or at session start" />
          <Fact label="Used" value={unit.used} unit="tasks" title="Read or cited" />
          <Fact label="Wrong" value={unit.wrong} unit="tasks" title="Flagged wrong" />
        </Box>
        <Box component="span" sx={{ display: 'inline-flex', gap: 0.5, alignItems: 'baseline' }}>
          Source
          {unit.source_task_id === null ? (
            <Box component="span" sx={{ color: 'text.primary' }}>{unit.feed}</Box>
          ) : (
            <>
              <TaskLink taskId={unit.source_task_id} />
              {/* Its own labelled link, set off from the task's, so "#12 session" never reads as one phrase. */}
              {unit.source_session_id && (
                <>
                  <Box component="span" aria-hidden="true">·</Box>
                  <Tip title={`Session ${unit.source_session_id}`}><MuiLink
                    component={RouterLink}
                    to={`/sessions?task=${unit.source_task_id}`}
                    underline="hover"
                  >
                    transcript
                  </MuiLink></Tip>
                </>
              )}
            </>
          )}
          {/* The code-map generation; on other units the sha is noise. */}
          {unit.kind === 'codemap' && unit.source_sha && (
            <Tip title={unit.source_sha}><Box component="span" sx={monoSx}>{shortSha(unit.source_sha)}</Box></Tip>
          )}
        </Box>
        {/* The reason in words, unless the chip already reads it (needs repro): muted and last, after the chip
            and the counts it would otherwise split. */}
        {unit.status_reason && unit.status_reason !== NEEDS_REPRO && (
          <Box component="span" data-testid="memory-unit-reason" sx={{ overflowWrap: 'anywhere', minWidth: 0 }}>
            {unit.status_reason}
          </Box>
        )}
        {chunks.length > 0 && (
          <Button type="button" size="small" onClick={() => setOpen(o => !o)} aria-expanded={open}
            aria-controls={open ? chunksId : undefined} data-testid="memory-chunks-toggle" sx={tapTargetSx}>
            {open ? 'Hide' : 'Show'} {countOf(chunks.length, 'more chunk')}
          </Button>
        )}
      </Box>
      {bodyOpen && (
        <Typography id={bodyId} variant="body2" component="div" data-testid="memory-unit-body"
          sx={{ mt: 1, pl: 1, borderLeft: 2, borderColor: 'divider', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {unit.body}
        </Typography>
      )}
      {open && (
        <Box component="ul" id={chunksId} aria-label={`More chunks of ${unit.title}`} data-testid="memory-chunks"
          sx={{ listStyle: 'none', m: 0, mt: 1, p: 0, borderLeft: 2, borderColor: 'divider' }}>
          {chunks.map(renderChunk)}
        </Box>
      )}
    </Box>
  )
}

/** A labelled figure; the unit rides in the accessible name, what it counts in the hover. */
function Fact({ label, value, unit, title }: {
  label: string
  value: number | string
  unit?: string
  title?: string
}): JSX.Element {
  return (
    <Tip title={title}><Box component="span" aria-label={unit ? `${label}: ${value} ${unit}` : `${label}: ${value}`}>
      {label} <Box component="span" sx={[monoSx, { color: 'text.primary' }]}>{value}</Box>
    </Box></Tip>
  )
}

function FeedStatus({
  loading,
  error,
  data,
  onShowHeld,
}: {
  loading: boolean
  error: unknown
  data: Parameters<typeof feedChips>[0] | null
  /** Switches the list's Status filter to Held. */
  onShowHeld: () => void
}): JSX.Element | null {
  const [showActive, setShowActive] = useState(false)
  if (loading) return null
  if (error || data === null) {
    return <Banner severity="error">{apiDetail(error, 'Could not load feed status')}</Banner>
  }
  const chips = feedChips(data)
  const noModel = chips.some(c => c.reason === NO_MODEL)
  const held = heldCount(data)
  /* The running feeds fold into one "N active" chip — only a stopped one needs reading — and a press lays them
     out. The Code map stays out of the fold: it names the generation runners are given. */
  const folded = chips.filter(c => !c.reason && c.key !== 'codemap')
  const listed = chips.filter(c => showActive || c.reason || c.key === 'codemap')
  const foldNames = folded.map(c => c.label).join(', ')
  return (
    <Box data-testid="memory-feed-status" sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      {/* The chips wrap among themselves; the fix link keeps its place at the row's end, never wrapped below them.
          Twice the chips' gap: borderless, at theirs it read flush against the last chip. */}
      <Box data-testid="memory-feed-row" sx={{ display: 'flex', flexWrap: 'nowrap', gap: 1.5, alignItems: 'center' }}>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center', minWidth: 0 }}>
          {folded.length > 0 && (
            <Box component="span" data-testid="memory-feed-active" data-expanded={showActive}>
              <StatusChip
                label={`${folded.length} active`}
                tone="success"
                title={`Running: ${foldNames}`}
                ariaLabel={`${showActive ? 'Hide' : 'Show'} the ${folded.length} running feeds: ${foldNames}`}
                onClick={() => setShowActive(open => !open)}
                press={showActive ? { glyph: '▴', label: 'Hide' } : { glyph: '▾', label: 'Show' }}
                expanded={showActive}
              />
            </Box>
          )}
          {listed.map(c => (
            <Box component="span" key={c.key} data-testid={`memory-feed-${c.key}`} data-state={c.state}>
              <StatusChip label={`${c.label}: ${c.state}`} ariaLabel={`${c.label}: ${c.state}`} tone={c.tone} title={c.title} />
            </Box>
          ))}
        </Box>
        {/* The stopped chips' reason is their hover; the fix is one short link beside them, which a phone can tap. */}
        {noModel && (
          <Tip title={`${NO_MODEL}: ${MEMORY_MODEL_SETTINGS.label}`}><MuiLink component={RouterLink} to={MEMORY_MODEL_SETTINGS.href} variant="caption" underline="hover" data-testid="memory-feed-fix"
            sx={{ flexShrink: 0, whiteSpace: 'nowrap' }}>
            Set memory model
          </MuiLink></Tip>
        )}
      </Box>
      {held !== null && (
        <Tip title="Accepted, but not given to runners until the canon check clears them (canon check inactive)"><Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="memory-held">
          Held by canon check:{' '}
          <LinkButton variant="caption" onClick={onShowHeld}
            aria-label={`Show ${countOf(held, 'held unit')}`}
            data-testid="memory-held-link" sx={{ verticalAlign: 'baseline' }}>
            {countOf(held, 'unit')}
          </LinkButton>
        </Typography></Tip>
      )}
    </Box>
  )
}

/** A unit row's Retire, Release and Purge, for every list of units. `refresh(released)` reloads the list after one. */
export function useUnitActions<U extends MemoryUnit>({ projectIdOf, refresh, identitySuffix = () => '' }: {
  projectIdOf: (unit: U) => number
  refresh: (released: boolean) => Promise<unknown>
  identitySuffix?: (unit: U) => string
}) {
  const { busy, error, notice, run } = useRowActions()
  const [confirm, confirmDialog] = useConfirm()

  const retire = (unit: U) => run(unit.id, `Could not retire ${unit.ref}`, async () => {
    await retireUnit(projectIdOf(unit), unit.id)
    await refresh(false)
  }, () => `${unit.ref} retired`)

  const release = (unit: U) => run(unit.id, `Could not release ${unit.ref}`, async () => {
    await releaseUnit(projectIdOf(unit), unit.id)
    await refresh(true)
  }, () => `${unit.ref} released`)

  async function purge(unit: U) {
    if (!await confirm(danger('Purge', {
      title: `Purge ${unit.ref}?`,
      identity: `${unit.ref} · ${unit.kind} — ${unit.title}${identitySuffix(unit)}`,
      message:
        'Deletes the unit for good and removes it from every stored runner prompt. Use it for sensitive data; ' +
        'otherwise retire. This cannot be undone.',
    }))) return
    await run(unit.id, `Could not purge ${unit.ref}`, async () => {
      const out = await purgeUnit(projectIdOf(unit), unit.id)
      await refresh(false)
      return out
    }, purgedNotice)
  }

  return { busy, error, notice, confirmDialog, retire, release, purge }
}
