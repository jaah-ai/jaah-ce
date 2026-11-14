import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import { useOfficialZone } from '../../../lib/officialZone'
import { assigneeLabel } from '../../../lib/taskAssignees'
import { dateRangeIsActive, dateRangePeriodLabel, instantWindowLabel } from '../../../lib/taskDateRange'
import { PHONE } from '../../../ui/listStyles'
import { SIZE } from '../../../theme'
import type { TaskToolbarProps } from '../TaskToolbar'
import { personaLabel } from '../personaRegistry'
import { stateLabel } from '../shared'
import Tip from '../../../ui/Tip'

/* EVERY ACTIVE FILTER AS A REMOVABLE CHIP, plus "Clear all" — under the pane header, so "why is this list
 * short?" is answered on screen without opening the menu. Exactly the filters the menu's badge counts
 * (`TaskToolbar`'s `activeFilters`) plus Search; Focus has neither, its own lit button says it is on. An assignee or source pick is
 * one chip per pick, so each can go alone. Group by draws rows differently and is not
 * a filter. Nothing renders while nothing narrows.
 * SELECT ALL RIDES BESIDE THEM: a narrowed board is where a bulk pick starts ("State: Draft" → delete
 * them), so the pick-every-match verb is offered here, before any row is ticked — not only in the bulk dock. The
 * page words it (`ChipSelectAll`): a number only where it is the count the press would pick. */

/** The board's Select all, as the chips row offers it. */
export type ChipSelectAll = {
  label: string
  onRun: () => void
}

type ChipProps = Pick<
  TaskToolbarProps,
  | 'q'
  | 'onQClear'
  | 'dateRange'
  | 'dateFrom'
  | 'dateTo'
  | 'onDateRangeChange'
  | 'assignees'
  | 'currentUserId'
  | 'assigneeIds'
  | 'onAssigneeIdsChange'
  | 'creators'
  | 'creatorIds'
  | 'onCreatorIdsChange'
  | 'taskSources'
  | 'sources'
  | 'onSourcesChange'
  | 'stateFilter'
  | 'onStateFilterChange'
  | 'persona'
  | 'onPersonaClear'
  | 'windowAfter'
  | 'windowBefore'
  | 'onWindowClear'
  | 'showHidden'
  | 'onShowHiddenChange'
  | 'onClearFilters'
>

const CHIP_SX = { maxWidth: '100%', [PHONE]: { height: SIZE.thumb } } as const

export default function FilterChips(props: ChipProps & { selectAll?: ChipSelectAll }) {
  const zone = useOfficialZone()
  const { q, dateRange, dateFrom, dateTo, assignees, currentUserId, assigneeIds, taskSources, sources, showHidden } = props
  const { windowAfter, windowBefore } = props
  const chips: Array<{ key: string; label: string; remove: () => void }> = []
  if (q.trim() !== '') chips.push({ key: 'q', label: `Search: “${q.trim()}”`, remove: props.onQClear })
  /* A link's exact window stands in for the Period choice (`Tasks.tsx`'s `dateBounds`), so it takes that chip's
     place; clearing it hands the board back to the Period choice underneath. */
  if (windowAfter !== '' || windowBefore !== '') {
    chips.push({
      key: 'window',
      label: `Period: ${instantWindowLabel(windowAfter, windowBefore, zone)}`,
      remove: props.onWindowClear,
    })
  } else if (dateRangeIsActive(dateRange, dateFrom, dateTo, zone)) {
    chips.push({
      key: 'period',
      label: `Period: ${dateRangePeriodLabel(dateRange, dateFrom, dateTo)}`,
      remove: () => props.onDateRangeChange('all'),
    })
  }
  for (const id of assigneeIds) {
    const assignee = assignees.find(c => c.id === id)
    chips.push({
      key: `assignee:${id}`,
      label: `Assignee: ${assignee ? assigneeLabel(assignee, currentUserId) : 'Someone'}`,
      remove: () => props.onAssigneeIdsChange(assigneeIds.filter(other => other !== id)),
    })
  }
  for (const id of props.creatorIds) {
    const creator = props.creators.find(c => c.id === id)
    chips.push({
      key: `creator:${id}`,
      label: `Creator: ${creator ? assigneeLabel(creator, currentUserId) : 'Someone'}`,
      remove: () => props.onCreatorIdsChange(props.creatorIds.filter(other => other !== id)),
    })
  }
  /* One chip per source, worded by the server's label; before the vocabulary answers it says only "Source",
     never the raw code. */
  for (const code of sources) {
    const source = taskSources.find(s => s.value === code)
    chips.push({
      key: `source:${code}`,
      label: source ? `Source: ${source.label}` : 'Source',
      remove: () => props.onSourcesChange(sources.filter(other => other !== code)),
    })
  }
  if (props.stateFilter !== '') {
    chips.push({ key: 'state', label: `State: ${stateLabel(props.stateFilter)}`, remove: () => props.onStateFilterChange('') })
  }
  if (props.persona !== '') {
    chips.push({ key: 'persona', label: `Persona: ${personaLabel(props.persona)}`, remove: props.onPersonaClear })
  }
  if (showHidden) chips.push({ key: 'hidden', label: 'Hidden shown', remove: () => props.onShowHiddenChange(false) })
  if (chips.length === 0) return null

  return (
    <Box
      data-testid="pane-filter-chips"
      role="group"
      aria-label="Active filters"
      sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5, mb: 1 }}
    >
      {chips.map(chip => (
        <Tip key={chip.key} title={chip.label}><Chip
          size="small"
          variant="outlined"
          label={chip.label}
          onDelete={chip.remove}
          data-testid={`pane-chip-${chip.key}`}
          sx={CHIP_SX}
        /></Tip>
      ))}
      <Button
        size="small"
        variant="text"
        onClick={props.onClearFilters}
        data-testid="pane-chip-clear-all"
        sx={{ minWidth: 0, px: 1, [PHONE]: { minHeight: SIZE.tapTarget } }}
      >
        Clear all
      </Button>
      {props.selectAll && (
        <Button
          size="small"
          variant="text"
          onClick={props.selectAll.onRun}
          data-testid="pane-chip-select-all"
          sx={{ minWidth: 0, px: 1, [PHONE]: { minHeight: SIZE.tapTarget } }}
        >
          <Box component="span" aria-hidden="true">☑&nbsp;</Box>
          {props.selectAll.label}
        </Button>
      )}
    </Box>
  )
}
