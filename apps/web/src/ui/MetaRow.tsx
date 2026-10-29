import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { JSX, ReactNode } from 'react'
import { briefTimestamp, fmtTimestamp, NO_VALUE } from '../lib/relativeTime'

/* The console's one read-only meta strip (`dt`/`dd` caption row: when a record happened, who touched
 * it), shared by `/users/:id` and `/groups/:id` so the "none" em-dash and null-actor rendering cannot
 * drift. A blank cell reads as a bug, so the placeholder is not optional. */

/** A `dl`; flex-wrap so a narrow card reflows rather than scrolls. A wrapped line keeps the caption's own
 *  leading, so the strip stacks as one block (the slot drawer puts the actors under the timestamps). */
export function MetaRow({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Box
      component="dl"
      sx={{
        display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0.25, m: 0, mt: 0.5,
        pt: 0.5, borderTop: 1, borderColor: 'divider',
      }}
    >
      {children}
    </Box>
  )
}

/** One label/value pair, on one line ("Created by System"): stacked, two short pairs read as two cramped
 *  columns beside a form. The value wraps under its label only when the pair outgrows the row.
 *  `title` carries the exact stored value where the rendered one is formatted. */
export function MetaItem({
  label,
  value,
  title,
}: {
  label: string
  value: string
  title?: string
}): JSX.Element {
  return (
    <Box sx={{ minWidth: 0, display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 0.5 }}>
      <Typography variant="caption" component="dt" sx={{ color: 'text.secondary' }}>{label}</Typography>
      <Typography variant="caption" component="dd" sx={{ m: 0, color: 'text.primary' }} title={title ?? ''}>
        {value}
      </Typography>
    </Box>
  )
}

/** A timestamp pair — Created / Activated / Last seen / Updated. `brief` (a drawer-width strip) shows
 *  `briefTimestamp`, with the full stamp on `title`, so the actors keep the timestamps' line. */
export function MetaField({ label, iso, brief }: { label: string; iso: string | null; brief?: boolean }): JSX.Element {
  return brief
    ? <MetaItem label={label} value={briefTimestamp(iso)} title={iso ? fmtTimestamp(iso) : ''} />
    : <MetaItem label={label} value={fmtTimestamp(iso)} title={iso ?? ''} />
}

/* A `created_by` / `updated_by` pair. The API sends a resolved display name, never an id
 * (`apps/api/app/actors.py::actor_names`; machine rows arrive as `System`), so there is no id fallback.
 * Null or empty renders the em-dash. */
export function MetaActor({ label, actor }: { label: string; actor: string | null }): JSX.Element {
  return <MetaItem label={label} value={actor || NO_VALUE} />
}

/** A record's `created_by` / `updated_by` pair: one item when one actor did both ("Created and updated by
 *  System"), never the same name twice. The single home for that collapse on every record page. */
export function MetaActors({ createdBy, updatedBy }: { createdBy: string | null; updatedBy: string | null }): JSX.Element {
  return createdBy === updatedBy
    ? <MetaActor label="Created and updated by" actor={createdBy} />
    : (
      // One flex item, so the pair wraps together: never "Created by" on one line and "Updated by" on the next.
      <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0.25 }}>
        <MetaActor label="Created by" actor={createdBy} />
        <MetaActor label="Updated by" actor={updatedBy} />
      </Box>
    )
}

export default MetaRow
