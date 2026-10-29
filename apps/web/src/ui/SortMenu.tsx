import { useState } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Tooltip from '@mui/material/Tooltip'

/* The ONLY sort affordance for a feed page, at every width: a feed row has no header row, so the
 * sortable keys live here (no `SortHeader` twin). Unconditional, or the page loses sorting above 480px.
 * The wordless ⇅ trigger names the current sort in its accessible name and its tooltip (hover and focus). A page whose sort can be `null`
 * (server order) passes `defaultItem`, the way back to null. */
export type SortMenuColumn<K extends string> = { key: K; label: string }

export default function SortMenu<K extends string>({
  noun,
  columns,
  sort,
  onSort,
  defaultItem,
}: {
  /** The plural the trigger names: "Sort projects — …". */
  noun: string
  columns: readonly SortMenuColumn<K>[]
  sort: { key: K; dir: 'asc' | 'desc' } | null
  onSort: (key: K) => void
  defaultItem?: { label: string; onSelect: () => void }
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const activeLabel = columns.find(c => c.key === sort?.key)?.label
  const dirWord = sort?.dir === 'asc' ? 'ascending' : 'descending'
  const summary = activeLabel ? `${activeLabel}, ${dirWord}` : 'the default order'

  return (
    <Box sx={{ display: 'inline-flex' }}>
      {/* `describeChild`: the trigger keeps its own name; the tooltip is its description. */}
      <Tooltip title={`Sorted by ${summary}`} describeChild>
        <IconButton
          aria-label={`Sort ${noun} — currently ${summary}`}
          aria-haspopup="true"
          aria-expanded={anchor ? true : undefined}
          onClick={e => setAnchor(e.currentTarget)}
        >
          <Box component="span" aria-hidden="true" sx={{ fontSize: '1rem', lineHeight: 1 }}>⇅</Box>
        </IconButton>
      </Tooltip>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {defaultItem && (
          <MenuItem
            selected={sort === null}
            onClick={() => { defaultItem.onSelect(); setAnchor(null) }}
          >
            {defaultItem.label}
          </MenuItem>
        )}
        {columns.map(c => {
          const active = sort?.key === c.key
          return (
            <MenuItem
              key={c.key}
              selected={active}
              aria-label={active ? `${c.label}, ${dirWord}` : c.label}
              onClick={() => { onSort(c.key); setAnchor(null) }}
            >
              {c.label}
              {active && (
                <Box component="span" aria-hidden="true" sx={{ ml: 1 }}>{sort?.dir === 'asc' ? '↑' : '↓'}</Box>
              )}
            </MenuItem>
          )
        })}
      </Menu>
    </Box>
  )
}
