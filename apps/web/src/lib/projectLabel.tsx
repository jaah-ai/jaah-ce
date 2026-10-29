import Box from '@mui/material/Box'
import type { JSX } from 'react'
import type { OrgRef } from '../types'
import { monoFamily } from '../ui/feed/geometry'
import { orgTitle } from '../ui/OrgIndicator'
import { VISUALLY_HIDDEN } from '../ui/visuallyHidden'

/* TWO PROJECTS, ONE NAME. A project name is unique only within its org, so an operator viewing every org can
 * meet two rows both called `web`. Every surface listing projects across orgs (the task rail, the project
 * menu, the `/worktrees` project bars) tells them apart HERE: the org's slug, muted, after the name — only
 * on a collision, so a single-org viewer sees no change. `OrgRef`'s rule holds (`types.ts`): `slug` is
 * drawn, `name` is said, `id` is never drawn. */

type NamedRow = { name: string; org?: OrgRef | null }

/** The org to draw beside each row's name among `rows` (what one surface shows): the row's `OrgRef` when
 *  another org's row shares its exact name, else `null`. Rows are compared by org, not count, so the same
 *  project listed twice (a pinned copy, many slots) is no collision. A row with no org draws nothing. */
export function orgQualifierFor<T extends NamedRow>(rows: readonly T[]): (row: T) => OrgRef | null {
  const orgsByName = new Map<string, Set<string>>()
  for (const row of rows) {
    const orgs = orgsByName.get(row.name) ?? new Set<string>()
    orgs.add(row.org?.id ?? '')
    orgsByName.set(row.name, orgs)
  }
  return row => ((orgsByName.get(row.name)?.size ?? 0) > 1 ? row.org ?? null : null)
}

/** The accessible name of a project row: the org's NAME is said when its slug is drawn. */
export function spokenProjectName(name: string, org: OrgRef | null): string {
  return org ? `${name} (${org.name})` : name
}

/** The drawn qualifier: the slug, muted and mono (an identifier), after the name. It YIELDS FIRST — in a
 *  single-line host it shrinks and ellipsises before the name does; in a wrapping host it moves to the next
 *  line whole. Says the org's name to assistive tech (the slug is `aria-hidden`), for hosts whose accessible
 *  name comes from content; a host with its own `aria-label` uses `spokenProjectName`. */
export function OrgQualifier({ org }: { org: OrgRef | null }): JSX.Element | null {
  if (!org) return null
  return (
    <Box
      component="span"
      data-testid="project-org-qualifier"
      data-org-slug={org.slug}
      title={orgTitle(org)}
      sx={theme => ({
        display: 'inline-block',
        verticalAlign: 'bottom',
        flex: '0 1000 auto',
        minWidth: 0,
        maxWidth: '100%',
        ml: 0.75,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        fontFamily: monoFamily(theme),
        fontSize: '0.9em',
        fontWeight: 400,
        letterSpacing: 0,
        textTransform: 'none',
        color: 'text.secondary',
      })}
    >
      <span aria-hidden="true">{org.slug}</span>
      <Box component="span" sx={VISUALLY_HIDDEN}>{` (${org.name})`}</Box>
    </Box>
  )
}
