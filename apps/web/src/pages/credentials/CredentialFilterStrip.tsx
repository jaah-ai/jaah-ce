import { useEffect, useMemo, useState, type JSX } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { ApiError, apiDetail } from '../../api'
import { useCan } from '../../auth'
import { FilterRow, FilterSearch, FilterSelect } from '../../ui/FilterRow'
import PaneModeToggle from '../../components/tasks/pane/PaneModeToggle'
import FilterBar from '../../ui/FilterBar'
import AsyncMultiLookup from '../../ui/AsyncMultiLookup'
import type { LookupOption } from '../../ui/AsyncLookup'
import { CREDENTIALS_FILTERS_COMPACT, tapTargetSx } from '../../ui/listStyles'
import { CREDENTIAL_HEALTHS, MAX_OWNER_IDS, type CredentialView } from '../../lib/credentialFilterStorage'
import { useOrgVisible } from '../../ui/OrgIndicator'
import {
  listCredentialOwners,
  type CredentialOwner,
  type CredentialTypeSpec,
} from './api'

/* THE CREDENTIALS ROSTER'S FILTER STRIP — search plus five narrowings, all SERVER-side.
 * Nothing here filters: each control becomes a query parameter the server applies to page AND `total`.
 * Two axes have no honest client form (owner ids are not on the wire; `secrets` derives
 * from an encrypted column).
 * No Org picker (R5.6, one decision, one control): an operator's org narrowing comes from the global
 * filter (`lib/orgScope.ts`), which the server applies to this roster like every operator read.
 *
 * ── Layout contract (pinned, measured) ──────────────────────────────────────────────
 * `ui/FilterBar` gets exactly ONE child, `ui/FilterRow`'s `FilterRow`: the bar stacks each direct child
 * full-width below 600px (once measured as a 31% chrome regression).
 * Nothing goes to the bar's `overflow` menu: every control stays visible at both supported widths (448
 * and 1024+). Budget pinned as a DELTA in `e2e/credentials-filters.spec.ts`.
 * The order is the packing: Search leads; Only-mine follows the pickers; Flat/Tree closes the row
 * as the one non-narrowing control — the shared `PaneModeToggle`.
 * NO STRANDED CONTROL: the pickers grow from `PICKER_PX` (capped at `PICKER_MAX_PX`) and Search
 * from `SEARCH_PX`, so a wrapped line fills its width; Only-mine and Flat/Tree wrap as ONE unit. At 1024px
 * the whole strip is one line. COMPACT UNDER A ~400px LIST (`CREDENTIALS_FILTERS_COMPACT`): beside an open
 * editor (a ~320px list) the default packing took three or four lines, so there Only-mine and Flat/Tree ride
 * Search's line and the pickers narrow to share the next (`COMPACT_SX`): two lines, three with Owner on a tight list.
 * Visual order only (`order`); the DOM, and so the tab order, stays the one above.
 *
 * OWNER IS A SERVER-SEARCHED MULTI-PICK (`ui/AsyncMultiLookup`): a native select cannot hold the
 * thousands of owners an operator sees across tenants. The roster is the picked owners' UNION; the view keeps
 * ids only, so chips get their names from the pick itself or, after a reload, from `/owners?ids=`.
 * Type and Secrets stay single-valued `FilterSelect`s over a short, fixed vocabulary.
 */

/** The pickers' flex basis and cap: every Secrets option whole at the basis; the cap keeps a long option (an
 *  email in Owner) from widening its control onto a line of its own. */
const PICKER_PX = 140
const PICKER_MAX_PX = 190
/** Search's basis: with three pickers, Only-mine and Flat/Tree it packs a 1024px window's 768px strip. */
const SEARCH_PX = 160

/** The compact band's flex bases: three pickers and their two gaps fit a ~300px line; Search plus the
 *  Only-mine/Flat-Tree unit (~140px) fit another. */
const COMPACT_PICKER_PX = 88
const COMPACT_SEARCH_PX = 110

/** Three chips of ordinary names side by side. */
const OWNER_CHIPS_PX = 360

/** The Owner box: a picker's basis, no cap. Holding chips it asks for room for a long name: on a wide strip the
 *  line still holds every control; on a phone (and beside an open editor) Owner takes a line of its own. */
const OWNER_SX = {
  flex: `1 1 ${PICKER_PX}px`,
  minWidth: PICKER_PX,
  [CREDENTIALS_FILTERS_COMPACT]: { order: 2, flex: `1 1 ${COMPACT_PICKER_PX * 2}px` },
} as const
const OWNER_WITH_CHIPS_SX = { ...OWNER_SX, flexBasis: OWNER_CHIPS_PX, [CREDENTIALS_FILTERS_COMPACT]: { order: 2, flexBasis: '100%' } } as const

/* THE COMPACT PACKING — widths, padding and order only; every control keeps its height (the phone's 44px floor).
   Pickers are the TextFields holding a `<select>`. The native select's right padding is MUI's `&&&` rule, so it
   is out-specified the same way. A closed value that still does not fit ellipsises (the open list stays whole). */
const COMPACT_SX = {
  [CREDENTIALS_FILTERS_COMPACT]: {
    '& .MuiTextField-root:not(:has(select))': { flex: `1 1 ${COMPACT_SEARCH_PX}px`, minWidth: COMPACT_SEARCH_PX },
    '& .MuiTextField-root:has(select)': { order: 2, flex: `1 1 ${COMPACT_PICKER_PX}px`, minWidth: COMPACT_PICKER_PX, maxWidth: 'none' },
    '& .MuiNativeSelect-select.MuiNativeSelect-select.MuiNativeSelect-select': {
      pl: 1,
      pr: 3,
      fontSize: '0.8125rem',
      textOverflow: 'ellipsis',
    },
    '& .MuiNativeSelect-icon': { right: 2 },
  },
} as const

/* A HOVER THAT IS NOT FOCUS: opening an editor reflows this strip, so a picker lands under the pointer
   that just clicked a row, and MUI's near-white hover outline then read as a focused field on arrival. The strip's
   hover outline is a translucent text tone instead, a step above rest; focus keeps the primary ring. */
const HOVER_SX = {
  '& .MuiOutlinedInput-root:hover:not(.Mui-focused):not(.Mui-disabled):not(.Mui-error) .MuiOutlinedInput-notchedOutline': {
    borderColor: 'rgba(var(--mui-palette-text-primaryChannel) / 0.45)',
  },
} as const

/* The Only-mine/Flat-Tree unit: after Search in the compact band, ahead of the pickers. */
const TOGGLES_SX = {
  display: 'flex',
  gap: 1,
  alignItems: 'center',
  flexShrink: 0,
  [CREDENTIALS_FILTERS_COMPACT]: { order: 1, gap: 0.5 },
} as const
const ONLY_MINE_COMPACT_SX = { [CREDENTIALS_FILTERS_COMPACT]: { px: 1, minWidth: 0 } } as const

/** The route's `Literal` arms. Short, as the picker's own label already says "Values": a longer value was cut
 *  in the strip beside an open secret. */
const SECRETS_OPTIONS = [
  { value: 'yes', label: 'Stored' },
  { value: 'no', label: 'Not stored' },
]

/** Enough owners to tell "more than one" apart. */
const OWNER_PROBE_LIMIT = 2
/** The picker's page: the route's default and the AC's ceiling. */
const OWNER_SEARCH_LIMIT = 20

/** The empty box: no owner picked narrows nothing. */
const OWNER_PLACEHOLDER = 'All owners'

/** An owner as a picker row: the name, and how many credentials they own (capped on a search). */
function ownerOption(owner: CredentialOwner): LookupOption {
  const count = owner.credential_count.toLocaleString('en-US')
  const shown = owner.count_capped ? `${count}+` : count
  return { key: owner.id, label: owner.name, hint: `${shown} secret${owner.credential_count === 1 && !owner.count_capped ? '' : 's'}` }
}

/** The picker's search: name or email, prefix matches first (`routes/credentials/reads.py`). */
function ownerOptions(q: string, signal: AbortSignal): Promise<LookupOption[]> {
  return listCredentialOwners({ q, limit: OWNER_SEARCH_LIMIT, signal }).then(rows => rows.map(ownerOption))
}

/** What a chip reads for an id: the name it was picked with, else the one `/owners?ids=` resolved. Never the id:
 *  `…` while resolving, "Unknown owner" for one the caller can no longer see, and a failed lookup says so. */
function useOwnerChips(ownerIds: readonly string[], enabled: boolean): {
  chips: LookupOption[]
  remember: (picked: LookupOption[]) => void
} {
  /* Every name learned, by id: from a pick, or folded in from a resolution, so removing one chip never sends the
     others back to "…". */
  const [names, setNames] = useState<Record<string, string>>({})
  const unresolved = ownerIds.filter(id => !(id in names))
  const resolved = useQuery({
    queryKey: ['credential-owners', 'ids', [...unresolved].sort()],
    queryFn: () => listCredentialOwners({ ids: unresolved }),
    enabled: enabled && unresolved.length > 0,
  })
  const answered = Array.isArray(resolved.data) ? resolved.data : undefined
  useEffect(() => {
    if (answered?.length) setNames(prev => ({ ...prev, ...Object.fromEntries(answered.map(o => [o.id, o.name])) }))
  }, [answered])
  const missing = resolved.isError ? 'Name unavailable' : resolved.isPending && enabled ? '…' : 'Unknown owner'
  const idsKey = ownerIds.join(',')
  const chips = useMemo(
    () => ownerIds.map(id => ({ key: id, label: names[id] ?? missing })),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [idsKey, names, missing],
  )
  return {
    chips,
    /* Only a NEW pick carries a real name; a held chip may still show a placeholder, which must not stick. */
    remember: next =>
      setNames(prev => ({
        ...prev,
        ...Object.fromEntries(next.filter(o => !ownerIds.includes(o.key)).map(o => [o.key, o.label])),
      })),
  }
}

/** The route's `RosterHealth` arms, in the words the strip shows. */
const HEALTH_LABELS: Record<(typeof CREDENTIAL_HEALTHS)[number], string> = {
  expired: 'Expired',
  expiring: 'Expiring (7 days)',
  never_used: 'Never used',
  test_failed: 'Last test failed',
}
const HEALTH_OPTIONS = CREDENTIAL_HEALTHS.map(value => ({ value, label: HEALTH_LABELS[value] }))

/** What Search matches, said in its placeholder: the operator's cross-tenant read (D1 — not narrowed to its own
 *  rows) also matches org and owner email (`apps/api/app/routes/credentials/roster.py::reads_as_operator`). */
export function searchPlaceholder(operator: boolean, mine: boolean): string {
  return operator && !mine ? 'Search name, description, org, owner email' : 'Search name or description'
}

/** One option list plus fetch state. `forbidden` is separate from `error`: `/owners` needs
 *  `credentials:view` (narrower than the roster), so a 403 is an ENTITLEMENT answer, while a 500 is an
 *  outage the operator must be told about. */
type RosterState<T> = { items: T[]; error: string; forbidden: boolean; loading: boolean }

/** Fetch one filter roster, classifying a 403 apart. `enabled: false` fetches nothing for an axis the viewer
 *  may not use (it can move after mount: `useCan` settles when `GET /api/auth/me` lands on a warm reload). */
function useFilterRoster<T>(
  queryKey: readonly unknown[],
  fetcher: () => Promise<T[]>,
  failure: string,
  enabled = true,
): RosterState<T> {
  const query = useQuery({ queryKey, queryFn: fetcher, enabled })
  const forbidden = query.error instanceof ApiError && query.error.status === 403
  return {
    items: query.data ?? [],
    error: query.isError && !forbidden ? apiDetail(query.error, failure) : '',
    forbidden,
    loading: query.isPending,
  }
}

/** The owner probe, read ONCE by the PAGE and handed down: two entries are enough to know a picker earns its
 *  place, and a 403 hides it. The picker searches on its own (`ownerOptions`). */
export function useCredentialFilterRosters(): CredentialFilterRosters {
  /* `/api/credentials/owners` is `credentials:view` only; a keyless reader (the roster is open to everyone)
     would only earn a 403 — not asked, the picker is simply absent. */
  const canListOwners = useCan()('credentials:view')
  return {
    owners: useFilterRoster<CredentialOwner>(
      ['credential-owners', 'probe'],
      () => listCredentialOwners({ limit: OWNER_PROBE_LIMIT }),
      'Failed to load the owner list',
      canListOwners,
    ),
  }
}

export type CredentialFilterRosters = {
  owners: RosterState<CredentialOwner>
}

export type CredentialFilterStripProps = {
  /** The view as the page holds it; every control is controlled off this, with no local filter state. */
  view: CredentialView
  /** One field at a time, so a caller cannot accidentally reset four axes by forgetting to spread. */
  onChange: (patch: Partial<CredentialView>) => void
  /** The type vocabulary, already fetched by the page (`useCredentialTypes`). */
  types: CredentialTypeSpec[]
  /** That fetch's failure, in the server's words, rendered on the Type control. */
  typesError: string
  typesLoading: boolean
  /** The owner option list from the page (`useCredentialFilterRosters`). */
  rosters: CredentialFilterRosters
}

export default function CredentialFilterStrip({
  view,
  onChange,
  types,
  typesError,
  typesLoading,
  rosters: { owners },
}: CredentialFilterStripProps): JSX.Element {
  /* The owner picker's existence (X2, R2.1). The `mine` toggle is PERMANENT (needs no roster). No picker
     on a 403 or a roster of ONE (it would narrow to what is already shown); neither is an error. A held
     selection keeps it, so it can be removed. */
  const canListOwners = useCan()('credentials:view')
  const showOwnerPicker =
    canListOwners && !owners.forbidden && (owners.items.length > 1 || Boolean(owners.error) || view.ownerIds.length > 0)
  const ownerChips = useOwnerChips(view.ownerIds, showOwnerPicker)
  const operator = useOrgVisible()

  return (
    /* A bare `<div>` (no box of its own) carrying the testid, so the chrome-budget oracle can remove and
       re-measure (it adds zero pixels), and the compact band's descendant rules. */
    <Box data-testid="credential-filters" sx={[HOVER_SX, COMPACT_SX]}>
      <FilterBar>
        <FilterRow>
          {/* SEARCH shares line one with Type at 448px. Debounced by the PAGE (R2.9), so the input never lags. */}
          <FilterSearch value={view.q} onChange={q => onChange({ q })} placeholder={searchPlaceholder(operator, view.mine)} ariaLabel="Search secrets" minWidth={SEARCH_PX} />

          <FilterSelect
            label="Type"
            ariaLabel="Filter by type"
            value={view.type}
            onChange={next => onChange({ type: next })}
            allLabel="All types"
            options={types.map(t => ({ value: t.slug, label: t.label }))}
            minWidth={PICKER_PX}
            maxWidth={PICKER_MAX_PX}
            grow
            disabled={typesLoading || Boolean(typesError)}
            errorText={typesError || undefined}
          />

          {showOwnerPicker && (
            /* Grows past the pickers' cap: chips need the room, and wrap inside the box when it runs out. */
            <Box data-testid="credential-owner-filter" sx={view.ownerIds.length > 0 ? OWNER_WITH_CHIPS_SX : OWNER_SX}>
              <AsyncMultiLookup
                id="credential-owner-filter"
                label="Owner"
                ariaLabel="Filter by owner"
                itemNoun="owner"
                placeholder={OWNER_PLACEHOLDER}
                value={ownerChips.chips}
                onChange={next => {
                  ownerChips.remember(next)
                  // At most what one `?ids=` resolves after a reload (`credentialFilterStorage.ts` keeps as many).
                  onChange({ ownerIds: next.slice(0, MAX_OWNER_IDS).map(o => o.key) })
                }}
                fetchOptions={ownerOptions}
                fetchOnFocus
                noOptionsText="No owner matches"
                margin="none"
                disabled={Boolean(owners.error)}
                error={Boolean(owners.error)}
                helperText={owners.error || undefined}
              />
            </Box>
          )}

          <FilterSelect
            label="Values"
            ariaLabel="Filter by stored values"
            value={view.secrets}
            onChange={next => onChange({ secrets: next === 'yes' || next === 'no' ? next : '' })}
            allLabel="Any"
            options={SECRETS_OPTIONS}
            minWidth={PICKER_PX}
            maxWidth={PICKER_MAX_PX}
            grow
          />

          <FilterSelect
            label="Health"
            ariaLabel="Filter by health"
            value={view.health}
            onChange={next => onChange({ health: CREDENTIAL_HEALTHS.find(health => health === next) ?? '' })}
            allLabel="Any"
            options={HEALTH_OPTIONS}
            minWidth={PICKER_PX}
            maxWidth={PICKER_MAX_PX}
            grow
          />

          {/* One wrap unit, so Flat/Tree never lands alone on a line. */}
          <Box data-testid="credential-filter-toggles" sx={TOGGLES_SX}>
            {/* ONLY MINE — a toggle, not an Owner entry: every principal can ask it (X2), and it is about the
                session's identity, which only the server can decide (`api.ts::listCredentials`).
                `aria-pressed` makes it a toggle for assistive tech. */}
            <Button
              type="button"
              onClick={() => onChange({ mine: !view.mine })}
              aria-pressed={view.mine}
              variant={view.mine ? 'contained' : 'outlined'}
              color="primary"
              sx={[tapTargetSx, ONLY_MINE_COMPACT_SX]}
            >
              Only mine
            </Button>

            {/* FLAT / TREE — the view mode, narrowing nothing, so it rides last. The shared
                wordless toggle (`components/tasks/pane/PaneModeToggle.tsx`): pressed = Tree,
                `aria-pressed` carries the state, 44px at phone width. Its wire spelling is `hierarchy`; this
                roster's is `tree`, mapped here at the one seam. */}
            <PaneModeToggle
              value={view.mode === 'tree' ? 'hierarchy' : 'flat'}
              onChange={next => onChange({ mode: next === 'hierarchy' ? 'tree' : 'flat' })}
            />
          </Box>
        </FilterRow>
      </FilterBar>
    </Box>
  )
}
