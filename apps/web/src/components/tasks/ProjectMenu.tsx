import Box from '@mui/material/Box'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Typography from '@mui/material/Typography'
import type { OrgRef, Project } from '../../types'
import { OrgQualifier, orgQualifierFor } from '../../lib/projectLabel'

// Imported: `ui/listStyles.ts` is its single home.
import { PHONE } from '../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import { SystemChip } from '../../ui/MarkChip'
import Tip from '../../ui/Tip'

/** One row: the `Project` fields the menu reads (a `GET /api/projects` row satisfies it). */
export type ProjectMenuRow = Pick<Project, 'id' | 'name' | 'system_kind'> & {
  /** Absent on rows from a source without it; present, it tells same-named projects apart (`lib/projectLabel.tsx`). */
  org?: OrgRef | null
}

// The ≥44px touch floor at phone width (`SIZE.tapTarget`). `whiteSpace: 'normal'` overrides MUI's `nowrap`: a long project name must
// wrap whole rather than overflow or clip the capped paper ("the full name, untruncated, at every width");
// `flexWrap` drops the reason chip to its own line. Also the breadcrumb's feature menu's (`TaskBreadcrumb.tsx`).
export const MENU_ITEM_SX = {
  whiteSpace: 'normal',
  flexWrap: 'wrap',
  gap: 0.5,
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const

/* The reason chip: a bordered pill, since it answers "why is the one I want dead". */
const REASON_SX = {
  // Slightly larger than `TAG_SX` (10px uppercase is the readable floor): this is the text to read.
  fontSize: FONT_SIZE.xxs,
  fontWeight: FONT_WEIGHT.bold,
  letterSpacing: LETTER_SPACING.caps,
  textTransform: 'uppercase',
  lineHeight: LINE_HEIGHT.tight,
  px: 0.5,
  py: 0.5,
  borderRadius: RADIUS.sm,
  border: '1px solid',
  flex: '0 0 auto',
} as const

/* A refused row keeps full opacity; only the NAME takes the disabled colour, so the reason stays at full
   contrast (MUI would fade the whole row, reason included). `pointerEvents: 'auto'` (as `pages/Nodes.tsx`,
   `GateForm.tsx`) so the `title` tooltip and `not-allowed` cursor work. Paired with a handler guard: MUI
   9's `useButton` already refuses the click (the guard is measured dead code), but we removed the CSS
   half, so the guard stays in case that JS half narrows. */
const REFUSED_SX = {
  '&.Mui-disabled': { opacity: 1, pointerEvents: 'auto', cursor: 'not-allowed' },
  // No hover: a hover state is a promise this row cannot keep.
  '&.Mui-disabled:hover': { backgroundColor: 'transparent' },
  '& .project-menu-name': { color: 'text.disabled' },
} as const

/* ── The task editor's scope picker ─────────────────────────────────────────────────────────────────
   The task editor's breadcrumb (`TaskBreadcrumb`: CREATE target or MOVE) — the `/tasks` board's selection is
   the sidebar's (`pane/ScopeDropdown.tsx` on the phone).
   It lists PROJECTS (`GET /api/projects`) by name; `MENU_ITEM_SX` keeps names whole.
   The host difference is a prop: `pickRefusal` (only the host knows the act's key). */
export default function ProjectMenu({
  anchorEl,
  onClose,
  projects,
  projectId,
  onPick,
  pickRefusal = () => null,
  testId,
  ariaLabel,
}: {
  anchorEl: HTMLElement | null
  onClose: () => void
  projects: ReadonlyArray<ProjectMenuRow>
  /** The currently-selected project, or `null` for none (the inline New task row before a target resolves). */
  projectId: number | null
  onPick: (id: number | null) => void
  /** Why this host would refuse picking a row (a lower-case chip caption), or `null` to accept.
   *
   *  The project list is wider than the set the caller may write in (a `projects:view` grant lists a
   *  project that holds no task key), so a pick could 403 on save. The automatic ladder already guards this
   *  (`shared.tsx::resolveCreateProject`'s `canCreateIn`); this is the same answer for the manual picker.
   *  Host-owned: the key depends on the act (`tasks:create` for a create
   *  target, `tasks:edit` on the DESTINATION for a move; a host whose list is already filtered passes none), and
   *  a hook here would force a permission mock on every test (as `TaskBreadcrumb`). Defaults to accepting
   *  everything. Never asked about the current row (picking it is a no-op). */
  pickRefusal?: (project: ProjectMenuRow) => string | null
  /** Forwarded to the Menu. */
  testId?: string
  /** The Menu list's accessible name. */
  ariaLabel?: string
}): JSX.Element {
  const qualifierOf = orgQualifierFor(projects)
  return (
    <Menu
      anchorEl={anchorEl}
      open={anchorEl !== null}
      onClose={onClose}
      data-testid={testId}
      slotProps={{
        list: { 'aria-label': ariaLabel },
        /* A ceiling for wide desktops; `min()` keeps MUI's phone cap underneath. */
        paper: { sx: { maxWidth: `min(${grid(63)}, calc(100% - ${grid(5)}))` } },
      }}
    >
      {projects.map(p => {
        const isCurrent = p.id === projectId
        // The host's refusal, never asked about the current row.
        const refusedBy = isCurrent ? null : pickRefusal(p)
        const refused = refusedBy !== null
        return (
          <Tip key={p.id} title={refused ? `${p.name} — ${refusedBy}` : undefined}><MenuItem
            selected={isCurrent}
            /* MUI `selected` on a `menuitem` emits only a class (`aria-selected` is for `role="option"`), so
               `aria-current` carries the current scope. */
            aria-current={isCurrent ? 'true' : undefined}
            // Listed either way (a vanished scope reads as a bug); the chip says why.
            disabled={refused}
            // The tooltip repeats the chip beside the name (it wraps below it at 390px).
            onClick={() => {
              // The guard behind `REFUSED_SX` (see its note).
              if (refused) return
              onClose()
              if (!isCurrent) onPick(p.id)
            }}
            sx={{ ...MENU_ITEM_SX, ...(refused ? REFUSED_SX : null) }}
          >
            {/* The class `REFUSED_SX` dims — the name greys, the reason does not. */}
            <Box component="span" className="project-menu-name" sx={{ minWidth: 0 }}>
              {p.name}
              <OrgQualifier org={qualifierOf(p)} />
            </Box>
            {p.system_kind && <SystemChip />}
            {refusedBy !== null && (
              /* Warning-toned: a missing grant is about YOU and not fixable alone. */
              <Typography
                component="span"
                variant="caption"
                data-testid="project-menu-refusal"
                sx={{ ...REASON_SX, color: 'warning.main', borderColor: 'warning.main' }}
              >
                {refusedBy}
              </Typography>
            )}
          </MenuItem></Tip>
        )
      })}
    </Menu>
  )
}
