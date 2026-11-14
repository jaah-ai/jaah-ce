import Box from '@mui/material/Box'
import { LINE_HEIGHT, SIZE } from '../theme'
import { OverflowMenu, type OverflowItem } from './OverflowMenu'

/* A feed page's whole sort vocabulary, at every width: a header row (`FeedList`'s `columns`) heads only the
 * keys that ARE columns, so an order with no column (Created) lives here. Reachable wherever the list draws,
 * or the page loses an order. `compact` fits it into a 24px header row (`FeedColumns.actions`).
 * The wordless ⇅ trigger names the current sort in its accessible name and its tooltip (hover and focus). A page whose sort can be `null`
 * (server order) passes `defaultItem`, the way back to null. A `ui/OverflowMenu`: the active item is
 * selected, arrowed in its label and worded in its name. */
export type SortMenuColumn<K extends string> = { key: K; label: string }

export default function SortMenu<K extends string>({
  noun,
  columns,
  sort,
  onSort,
  defaultItem,
  compact = false,
}: {
  /** The plural the trigger names: "Sort projects — …". */
  noun: string
  columns: readonly SortMenuColumn<K>[]
  sort: { key: K; dir: 'asc' | 'desc' } | null
  onSort: (key: K) => void
  defaultItem?: { label: string; onSelect: () => void }
  /** A 24px trigger, for a column header row. */
  compact?: boolean
}) {
  const activeLabel = columns.find(c => c.key === sort?.key)?.label
  const dirWord = sort?.dir === 'asc' ? 'ascending' : 'descending'
  const summary = activeLabel ? `${activeLabel}, ${dirWord}` : 'the default order'

  const items: OverflowItem[] = columns.map(c => {
    const active = sort?.key === c.key
    return {
      key: c.key,
      label: active ? `${c.label} ${sort?.dir === 'asc' ? '↑' : '↓'}` : c.label,
      ariaLabel: active ? `${c.label}, ${dirWord}` : c.label,
      selected: active,
      onClick: () => onSort(c.key),
    }
  })
  if (defaultItem) {
    items.unshift({ key: '\0default', label: defaultItem.label, ariaLabel: defaultItem.label, selected: sort === null, onClick: defaultItem.onSelect })
  }

  return (
    <OverflowMenu
      ariaLabel={`Sort ${noun} — currently ${summary}`}
      glyph={{
        icon: <Box component="span" aria-hidden="true" sx={{ fontSize: compact ? SIZE.iconXs : SIZE.iconSm, lineHeight: LINE_HEIGHT.tight }}>⇅</Box>,
        tooltip: `Sorted by ${summary}`,
      }}
      sx={compact ? { p: 0.5 } : undefined}
      items={items}
    />
  )
}
