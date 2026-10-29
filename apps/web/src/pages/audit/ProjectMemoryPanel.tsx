import { type JSX, type ReactNode, useEffect, useMemo, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import MenuItem from '@mui/material/MenuItem'
import MuiLink from '@mui/material/Link'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { BILLING_PAGE } from '../../lib/companySetup'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { personaOptions } from '../../components/tasks/personaRegistry'
import { offPlan } from '../../lib/planSwitches'
import Banner from '../../ui/Banner'
import CellLabel from '../../ui/CellLabel'
import DataTable from '../../ui/DataTable'
import EmptyState from '../../ui/EmptyState'
import LoadingState from '../../ui/LoadingState'
import SectionCard from '../../ui/SectionCard'
import { monoSx, PHONE, PHONE_CARD_SX, phoneCardSx } from '../../ui/listStyles'
import { DEFAULT_PERIOD_DAYS, type PeriodDays } from './efficiencyApi'
import {
  getMemoryComparison,
  listComparisonProjects,
  fmt,
  percent,
  withUnit,
  type ComparisonArm,
  type MemoryComparison,
  type Spread,
  type UnitKindKey,
} from './memoryComparisonApi'

/* ══ AUDIT > SESSIONS > EFFICIENCY > PROJECT MEMORY ══ (R-AB-4, AC-20)
 * One question: on this project, do runners with project memory ON do better than runners with it OFF?
 * Both arms side by side, every figure with its unit; an arm the server says it cannot support (empty, or
 * under `min_n` sessions) shows the server's sentence in place of its figures — never a 0. Below them, ON split
 * by whether a unit was injected at all, and the read rate per kind (the kill metric). With no session in either
 * arm, both ON/OFF tables give way to one line, and so does the read rate when every count in it is 0.
 * With no project to compare (none listed yet, none with memory on) it shows its empty state alone: no project or
 * persona picker, no period. A project off its org's plan (`state: "off_by_plan"`, or an older server's 403) is a
 * state, said in one line — never a comparison of zero sessions — with no persona or period, and the project picker
 * only when another project could answer differently.
 * THE PERIOD IS THE PAGE'S (`EfficiencyTab`'s picker, passed as `days`): a second identical 7d/14d/30d picker
 * stacked under the first left the operator guessing which one drove what. The card names the window it reads.
 * Reachable with `sessions:view` alone: its own project list (`/api/memory/comparison/projects`), not
 * `/api/projects`. The page computes nothing — the server's memory comparison owns every figure. */

/** Where an arm's sessions got their memory setting, as the phrase after a count ("13 by default"). */
const SOURCE_LABEL: Record<string, string> = {
  task: 'set on the task', epic: 'from the epic', default: 'by default', holdout: 'in the holdout', killed: 'by the kill switch',
  plan: 'off by plan', unknown: 'from an unknown source',
}

/** An arm's sources in words, or `null` when every session in the table shares one source (the line would only
 *  repeat the Sessions row). */
function sourceLine(arm: ComparisonArm, shared: boolean): string | null {
  if (shared) return null
  return Object.entries(arm.sources).map(([s, n]) => `${n} ${SOURCE_LABEL[s] ?? s}`).join(', ') || 'no sessions'
}

const OFF_BY_PLAN = "Off — not in your plan."

export default function ProjectMemoryPanel({ days = DEFAULT_PERIOD_DAYS }: {
  /** The page's period; this card has no picker of its own. */
  days?: PeriodDays
}): JSX.Element {
  const [projectId, setProjectId] = useState<number | null>(null)
  const [persona, setPersona] = useState('')

  const projects = useQuery({ queryKey: ['memory-comparison-projects'], queryFn: listComparisonProjects })
  useEffect(() => {
    if (projectId === null && projects.data && projects.data.length > 0) setProjectId(projects.data[0].id)
  }, [projectId, projects.data])

  const comparison = useQuery({
    queryKey: ['memory-comparison', projectId, days, persona],
    queryFn: () => getMemoryComparison(projectId as number, days, persona),
    enabled: projectId !== null,
    retry: (count, err) => !offPlan(err) && count < 3,
  })
  const personas = useMemo(() => personaOptions(), [])

  const period = (
    <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="memory-comparison-period">
      {`Last ${days} days`}
    </Typography>
  )

  /* Nothing to compare: the empty state alone, with no picker or period that could change nothing. */
  const listed = (projects.data ?? []).length > 0
  const offByPlan = offPlan(comparison.error) || comparison.data?.state === 'off_by_plan'
  const comparable = listed && !offByPlan
  let body: ReactNode
  if (projects.error) {
    body = <Banner severity="error">{apiDetail(projects.error, 'Could not load projects')}</Banner>
  } else if (projects.isLoading) {
    body = <LoadingState />
  } else if ((projects.data ?? []).length === 0) {
    body = <EmptyState title="No project has memory on" action={<TurnOnMemory />} />
  } else if (offByPlan) {
    body = <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="memory-comparison-off-plan">{OFF_BY_PLAN}</Typography>
  } else if (comparison.error) {
    body = <Banner severity="error">{apiDetail(comparison.error, 'Could not load the comparison')}</Banner>
  } else if (!comparison.data) {
    body = <LoadingState />
  } else {
    body = <Comparison data={comparison.data} />
  }

  return (
    <SectionCard title="Project memory" actions={comparable ? period : undefined}>
      <Box data-testid="memory-comparison" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
        {/* Off the plan only another project could change the answer: the project picker alone, when there is one. */}
        {(comparable || (offByPlan && (projects.data ?? []).length > 1)) && <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          <TextField
            select
            size="small"
            label="Project"
            value={projectId ?? ''}
            onChange={e => setProjectId(Number(e.target.value))}
            sx={{ flex: '1 1 12rem', minWidth: 0 }}
            slotProps={{ select: { inputProps: { 'data-testid': 'memory-comparison-project' } } }}
          >
            {(projects.data ?? []).map(p => <MenuItem key={p.id} value={p.id}>{p.name}</MenuItem>)}
          </TextField>
          {comparable && <TextField
            select
            size="small"
            label="Persona"
            value={persona}
            onChange={e => setPersona(e.target.value)}
            sx={{ flex: '0 1 11rem', minWidth: '9rem' }}
            slotProps={{ inputLabel: { shrink: true }, select: { displayEmpty: true, inputProps: { 'data-testid': 'memory-comparison-persona' } } }}
          >
            <MenuItem value="">All</MenuItem>
            {personas.map(p => <MenuItem key={p.value} value={p.value}>{p.label}</MenuItem>)}
          </TextField>}
        </Box>}
        {body}
      </Box>
    </SectionCard>
  )
}

/** Where memory is turned on: it is a plan feature, so an org's Plan (an operator, from Customers) or Billing (a
 *  tenant's org admin); anyone else asks an org admin. */
function TurnOnMemory(): JSX.Element {
  const { admin } = useAuth()
  const link = (to: string, label: string) => <MuiLink component={RouterLink} to={to}>{label}</MuiLink>
  return (
    <Typography variant="body2" data-testid="memory-comparison-turn-on" sx={{ color: 'text.secondary' }}>
      {admin?.is_operator === true ? <>Turn it on in an org's Plan, from {link('/customers', 'Customers')}.</>
        : isOrgAdminSeat(admin) ? <>Turn it on in {link(BILLING_PAGE, 'Billing')}.</>
        : 'An admin can turn it on.'}
    </Typography>
  )
}

function Comparison({ data }: { data: MemoryComparison }): JSX.Element {
  const basis = data.basis === 'holdout'
    ? 'OFF: holdout and default tasks · ON: default tasks'
    : "No holdout: split by each session's memory setting"
  const noSessions = data.off.sessions + data.on.sessions === 0
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="memory-comparison-window">
        <Box component="span" sx={[monoSx, { color: 'text.primary', fontWeight: 600 }]}>
          {data.since} – {data.until}
        </Box>
        {' · '}{basis}
      </Typography>
      {/* No session in either arm: one line, not two tables of "0 sessions" and notices. */}
      {noSessions ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="memory-comparison-no-sessions">
          No sessions in this window.
        </Typography>
      ) : <ArmTable title="ON vs OFF" left={data.off} right={data.on} testId="memory-comparison-headline" />}
      {data.orientation_status === 'unavailable' && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="memory-orientation-unavailable">
          Calls to first edit and duplicate reads not measured: {data.orientation_reason}
        </Typography>
      )}
      {/* All zeros says nothing. */}
      {data.before_sessions + data.mixed_tasks > 0 && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="memory-comparison-excluded">
          Not counted: {withUnit(data.before_sessions, 'session')} from before project memory;{' '}
          {withUnit(data.mixed_tasks, 'task')} that ran with memory both on and off.
        </Typography>
      )}
      {!noSessions && (
        <ArmTable title="ON, split by delivery" left={data.on_injected} right={data.on_empty} testId="memory-comparison-split" />
      )}
      {/* Units written stand without a session, so the read rate stays unless every count in it is 0 too. */}
      {!(noSessions && data.read_rate.every(row => row.injected + row.used + row.written + row.units_read === 0)) && (
        <ReadRate data={data} />
      )}
    </Box>
  )
}

type MetricRow = { key: string; label: string; cell: (arm: ComparisonArm) => ReactNode }

function spreadCell(spread: Spread | null, singular: string, plural?: string, extra?: string): ReactNode {
  if (spread === null) return <Muted>No sessions measured</Muted>
  return (
    <Figure
      main={withUnit(spread.median, singular, plural)}
      sub={`IQR ${fmt(spread.q1)}–${fmt(spread.q3)}${extra ? ` · ${extra}` : ''}`}
    />
  )
}

function unitsCell(arm: ComparisonArm, kind: UnitKindKey): ReactNode {
  const figure = arm.units?.[kind]
  if (!figure || figure.median === null) return <Muted>None</Muted>
  return (
    <Figure
      main={withUnit(figure.median, 'unit')}
      sub={`IQR ${fmt(figure.q1 ?? 0)}–${fmt(figure.q3 ?? 0)} · total ${withUnit(figure.total, 'unit')}`}
    />
  )
}

const METRICS: readonly MetricRow[] = [
  { key: 'injected', label: 'Units injected per task (median)', cell: arm => unitsCell(arm, 'injected') },
  { key: 'read', label: 'Units read per task (median)', cell: arm => unitsCell(arm, 'read') },
  { key: 'cited', label: 'Units cited per task (median)', cell: arm => unitsCell(arm, 'cited') },
  {
    key: 'first_edit',
    label: 'Tool calls to first edit (median)',
    cell: arm => arm.orientation === null
      ? <Muted>Not measured</Muted>
      : spreadCell(arm.orientation.calls_to_first_edit, 'call', 'calls',
        `${withUnit(arm.orientation.no_edit, 'session')} with no edit`),
  },
  {
    key: 'duplicate_reads',
    label: 'Duplicate reads (median)',
    cell: arm => arm.orientation === null
      ? <Muted>Not measured</Muted>
      : spreadCell(arm.orientation.duplicate_reads, 'read'),
  },
]

/* ON A PHONE the comparison stacks: each metric's label takes a whole line and its two figures sit side by side
   under it, so a label is never squeezed into a third of 390px (three to four lines each). `data-side` keeps a
   figure in its arm's column when the other arm's notice spans the rows (`rowSpan` means nothing to a grid). */
const ARM_PHONE_SX = {
  [PHONE]: {
    '& table': { display: 'block', width: '100%' },
    '& thead, & tbody': { display: 'block' },
    '& tr': {
      display: 'grid',
      gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
      columnGap: 1.5,
      py: 0.5,
      borderBottom: '1px solid',
      borderColor: 'divider',
    },
    '& th, & td': { width: 'auto', border: 0, px: 0, py: 0.25 },
    /* The header row is one more stacked row: the sticky header's band (`ui/DataTable`'s `stickyHeader`) would
       paint each cell apart, its gap reading as a rule between the two arms. */
    '& thead th': { position: 'static', backgroundColor: 'transparent' },
    '& th:not([data-side])': { gridColumn: '1 / -1' },
    '& [data-side="0"]': { gridColumn: 1 },
    '& [data-side="1"]': { gridColumn: 2 },
  },
} as const

function ArmTable({ title, left, right, testId }: {
  title: string
  left: ComparisonArm
  right: ComparisonArm
  testId: string
}): JSX.Element {
  const arms = [left, right]
  const shared = new Set(arms.flatMap(arm => Object.keys(arm.sources))).size <= 1
  return (
    <Box sx={ARM_PHONE_SX}>
      <DataTable ariaLabel={title} testId={testId} nested>
        <TableHead>
          <TableRow>
            <TableCell sx={{ width: '30%' }}>{title}</TableCell>
            {arms.map((arm, side) => (
              <TableCell key={arm.key} data-arm={arm.key} data-side={side} sx={{ width: '35%', verticalAlign: 'bottom' }}>
                <Box component="span" sx={{ display: 'block', fontWeight: 600 }}>{arm.label}</Box>
                {sourceLine(arm, shared) !== null && (
                  /* Sentence case: the head row's uppercase would turn the words back into a code ("DEFAULT 13"). */
                  <Box component="span" data-testid="memory-arm-sources" sx={{ display: 'block', fontWeight: 400, color: 'text.secondary', textTransform: 'none' }}>
                    {sourceLine(arm, shared)}
                  </Box>
                )}
              </TableCell>
            ))}
          </TableRow>
        </TableHead>
        <TableBody>
          <TableRow>
            <Label>Sessions</Label>
            {arms.map((arm, side) => <Value key={arm.key} arm={arm} side={side}>{withUnit(arm.sessions, 'session')}</Value>)}
          </TableRow>
          <TableRow>
            <Label>Tasks</Label>
            {arms.map((arm, side) => <Value key={arm.key} arm={arm} side={side}>{withUnit(arm.tasks, 'task')}</Value>)}
          </TableRow>
          {METRICS.map((metric, index) => (
            <TableRow key={metric.key}>
              <Label>{metric.label}</Label>
              {arms.map((arm, side) => {
                if (arm.notice === null) return <Value key={arm.key} arm={arm} side={side}>{metric.cell(arm)}</Value>
                // The arm's sentence once, across every figure row: the figures do not exist, so nothing is shown.
                if (index > 0) return null
                return (
                  <TableCell
                    key={arm.key}
                    rowSpan={METRICS.length}
                    data-arm={arm.key}
                    data-side={side}
                    data-testid="memory-arm-notice"
                    sx={{ verticalAlign: 'top', color: 'text.secondary', fontStyle: 'italic' }}
                  >
                    {arm.notice}
                  </TableCell>
                )
              })}
            </TableRow>
          ))}
        </TableBody>
      </DataTable>
    </Box>
  )
}

function Label({ children }: { children: ReactNode }): JSX.Element {
  return <TableCell component="th" scope="row" sx={{ verticalAlign: 'top', fontWeight: 500 }}>{children}</TableCell>
}

function Value({ arm, side, children }: { arm: ComparisonArm; side: number; children: ReactNode }): JSX.Element {
  return <TableCell data-arm={arm.key} data-side={side} sx={{ verticalAlign: 'top', overflowWrap: 'anywhere' }}>{children}</TableCell>
}

function Figure({ main, sub }: { main: string; sub: string }): JSX.Element {
  return (
    <>
      <Box component="span" sx={[monoSx, { display: 'block' }]}>{main}</Box>
      <Box component="span" sx={{ display: 'block', color: 'text.secondary', fontSize: '0.75rem' }}>{sub}</Box>
    </>
  )
}

function Muted({ children }: { children: ReactNode }): JSX.Element {
  return <Box component="span" sx={{ color: 'text.secondary' }}>{children}</Box>
}

function ReadRate({ data }: { data: MemoryComparison }): JSX.Element {
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>Read rate per kind</Typography>
      <Box sx={READ_RATE_PHONE_SX}>
        <DataTable ariaLabel="Read rate per kind" testId="memory-read-rate" nested>
          <TableHead>
            <TableRow>
              <TableCell>Kind</TableCell>
              <TableCell align="right">Injected</TableCell>
              <TableCell align="right">Used</TableCell>
              <TableCell align="right">Read rate</TableCell>
              <TableCell align="right">Written</TableCell>
              <TableCell align="right">Read</TableCell>
              <TableCell align="right">Written per read</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {data.read_rate.map(row => (
              <TableRow key={row.kind} data-kind={row.kind}>
                <TableCell sx={{ fontWeight: 600 }}>{row.kind}</TableCell>
                <Rate label="Injected">{withUnit(row.injected, 'unit')}</Rate>
                <Rate label="Used">{withUnit(row.used, 'unit')}</Rate>
                <Rate label="Read rate">{percent(row.read_rate) ?? <Absent>none injected</Absent>}</Rate>
                <Rate label="Written">{withUnit(row.written, 'unit')}</Rate>
                <Rate label="Read">{withUnit(row.units_read, 'unit')}</Rate>
                <Rate label="Written per read">
                  {row.write_to_read === null ? <Absent>none read</Absent> : `${row.write_to_read.toFixed(1)}×`}
                </Rate>
              </TableRow>
            ))}
          </TableBody>
        </DataTable>
      </Box>
    </Box>
  )
}

function Rate({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <TableCell align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
      <CellLabel>{label}</CellLabel>
      <span>{children}</span>
    </TableCell>
  )
}

/* A figure that does not exist ("none injected"): prose, not a number — the body face, dimmed like a ledger's
   `NO_VALUE`, so it never reads as one more figure in the mono column. */
function Absent({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Box component="span" data-absent sx={theme => ({ fontFamily: theme.typography.fontFamily, color: 'text.disabled' })}>
      {children}
    </Box>
  )
}

/* ON A PHONE each kind is a card: its name a whole line, then its six figures three to a line, each label over its
   value — as the ON/OFF split above stacks, never a run-on "value Label value Label" line that wraps
   mid-pair. */
const READ_RATE_PHONE_SX = phoneCardSx({
  '& tbody td': {
    ...PHONE_CARD_SX[PHONE]['& tbody td'],
    flexDirection: 'column',
    alignItems: 'flex-start',
    gap: 0,
    textAlign: 'left',
    '& > .MuiTypography-caption': { display: 'block' },
  },
  '& tbody td:first-of-type': { gridColumn: '1 / -1' },
}, {
  display: 'grid',
  gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
  columnGap: 1.5,
  rowGap: 0.5,
  py: 0.75,
})
