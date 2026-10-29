import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Checkbox from '@mui/material/Checkbox'
import MenuItem from '@mui/material/MenuItem'
import MenuList from '@mui/material/MenuList'
import Popover from '@mui/material/Popover'
import TextField from '@mui/material/TextField'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useLayoutEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent } from 'react'
import { useAuth } from '../auth'
import { useApplyOrgScope } from '../hooks/useOrgScopeUrl'
import { createExternalStore, useExternalStore } from '../lib/externalStore'
import { countOf } from '../lib/humanize'
import { ORG_IDS_MAX, useOrgSearch } from '../lib/orgs'
import { OPERATOR_PLANE_LABEL, scopeNames, scopeRoster, useOrgScope, type OrgScope, type ScopeOrg } from '../lib/orgScope'
import EmptyState from './EmptyState'
import { isEnabled } from '../lib/editionFeatures'
import { PHONE_QUERY, schemed } from './listStyles'

/* THE OPERATOR'S GLOBAL ORG FILTER PICKER (state in `lib/orgScope.ts`). Rendered only for
 * an operator (`is_operator`), never for anyone else.
 *
 * The trigger IS the scope indicator: "All orgs", or the first selected org's name in full plus "+N", with every
 * name on hover. A name is ellipsised only past 24ch, so the chip never shreds a name to fragments.
 *
 * The picker edits a DRAFT and commits it on Done or click-away (Escape discards it): one history entry and one
 * refetch per edit, not one per checkbox. "All orgs" and a row's "Only" commit at once. Search narrows by name or
 * slug; arrow keys move from the search box into the list, Space/Enter toggles a row, Tab reaches its "Only".
 *
 * SERVER-SEARCHED: the roster is paged, so the open picker reads one page — the first, or what the
 * search matches — and says when there is more than it shows. The ticked orgs head the unsearched list whether
 * or not that page carries them, so a choice is never out of sight. Nothing is read while the picker is shut.
 *
 * Where it sits: the expanded rail's top (`variant="rail"`), a compact count on the collapsed rail
 * (`"collapsed"`), and on phones a strip above every page (`"bar"`, `App.tsx::AuthLayout`) — the phone rail is a
 * bottom bar with no room, and the scope must stay visible there too. A page whose header has room on the phone draws
 * it there instead (`OrgScopeInline`), and the strip then steps aside. */

const ALL_ORGS = 'All orgs'

/** Picker order, for the chip's first name: the operator plane first, then by name. */
function byPickerOrder(a: ScopeOrg, b: ScopeOrg): number {
  const plane = Number(b.name === OPERATOR_PLANE_LABEL) - Number(a.name === OPERATOR_PLANE_LABEL)
  return plane || a.name.localeCompare(b.name)
}

/* Each row's "Only" (commit just that org, at once) shows on hover or focus where there is a pointer, and
 * always on touch and phone widths, which have no hover. Hidden by opacity, so it stays in the Tab order. */
const ONLY_CLASS = 'org-scope-only'
const ONLY_REVEAL_SX = {
  overflowY: 'auto',
  py: 0.5,
  [`& .${ONLY_CLASS}`]: { opacity: 0 },
  [`& .MuiMenuItem-root:hover .${ONLY_CLASS}, & .MuiMenuItem-root:focus-within .${ONLY_CLASS}`]: { opacity: 1 },
  [`@media (hover: none), (max-width: 600px)`]: { [`& .${ONLY_CLASS}`]: { opacity: 1 } },
} as const

/** The chip's words: "All orgs", or the first name and how many more. */
export function scopeChip(scope: OrgScope): { first: string; more: number } {
  if (!scope) return { first: ALL_ORGS, more: 0 }
  return { first: scope[0].name, more: scope.length - 1 }
}

export type OrgScopeVariant = 'rail' | 'collapsed' | 'bar'

export default function OrgScopeControl({ variant }: { variant: OrgScopeVariant }): JSX.Element | null {
  const { admin } = useAuth()
  const operator = admin?.is_operator === true
  const scope = useOrgScope()
  const apply = useApplyOrgScope()
  const anchor = useRef<HTMLButtonElement | null>(null)
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<OrgScope>(null)
  /* One page of the roster as the filter names and orders it (`scopeRoster`): the first `ORG_IDS_MAX`, or those
     the search matches. */
  const { query: roster, typed: query, searched, type: search, clear: clearSearch } =
    useOrgSearch({ enabled: operator && open, per: ORG_IDS_MAX })
  const page = useMemo(() => roster.data && scopeRoster(roster.data.items), [roster.data])
  const listRef = useRef<HTMLUListElement | null>(null)

  /* The page the server answered; unsearched, the ticked orgs lead it (they may sit on no page read). */
  const shown = useMemo(() => {
    const orgs = page ?? []
    if (searched) return orgs
    const ticked = [...(draft ?? [])].sort(byPickerOrder)
    const tickedIds = new Set(ticked.map(org => org.id))
    return [...ticked, ...orgs.filter(org => !tickedIds.has(org.id))]
  }, [page, searched, draft])
  const more = roster.data && page ? roster.data.total - page.length : 0

  if (!operator) return null

  const openPicker = () => {
    setDraft(scope)
    clearSearch()
    setOpen(true)
  }
  const commit = (next: OrgScope) => {
    setOpen(false)
    apply(next)
  }
  const draftIds = new Set((draft ?? []).map(org => org.id))
  const toggle = (org: ScopeOrg) => {
    const rest = (draft ?? []).filter(o => o.id !== org.id)
    // Kept in picker order, so the chip's first name is stable whatever order they were ticked in.
    const next = draftIds.has(org.id) ? rest : [...rest, org]
    next.sort(byPickerOrder)
    setDraft(next.length ? next : null)
  }
  const searchRef = useRef<HTMLInputElement | null>(null)
  const onSearchKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      listRef.current?.querySelector<HTMLElement>('[role^="menuitem"]')?.focus()
    }
  }
  // Up from the first row returns to the search box (MenuList would otherwise wrap to the last row).
  const onListKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowUp' && document.activeElement === listRef.current?.querySelector('[role^="menuitem"]')) {
      e.preventDefault()
      e.stopPropagation()
      searchRef.current?.focus()
    }
  }

  const { first, more: moreNamed } = scopeChip(scope)
  const full = scope ? scope.map(org => org.name).join(', ') : ALL_ORGS
  const label = scope ? `Org filter: ${scopeNames(scope)}` : 'Org filter: All orgs'

  const trigger = variant === 'collapsed'
    ? (
      <ButtonBase
        ref={anchor}
        data-testid="org-scope-trigger"
        onClick={openPicker}
        aria-label={`${label}. Change`}
        aria-haspopup="dialog"
        aria-expanded={open}
        sx={(t: Theme) => ({
          minWidth: 36,
          height: 26,
          px: 0.5,
          borderRadius: '4px',
          border: '1px solid',
          fontSize: '0.6875rem',
          fontWeight: 700,
          ...schemed(t, c => ({ color: scope ? c.accent : c.text, borderColor: c.buttonBorder })),
          '&.Mui-focusVisible': { ...schemed(t, c => ({ outline: `2px solid ${c.accent}` })), outlineOffset: '2px' },
        })}
      >
        {scope ? countOf(scope.length, 'org') : 'All'}
      </ButtonBase>
    )
    : (
      <ButtonBase
        ref={anchor}
        data-testid="org-scope-trigger"
        onClick={openPicker}
        aria-label={`${label}. Change`}
        aria-haspopup="dialog"
        aria-expanded={open}
        sx={(t: Theme) => ({
          display: 'flex',
          alignItems: 'center',
          gap: '0.375rem',
          minWidth: 0,
          maxWidth: '100%',
          minHeight: variant === 'bar' ? 32 : 30,
          padding: '0 0.5rem',
          borderRadius: '4px',
          border: '1px solid',
          fontSize: '0.8125rem',
          textAlign: 'left',
          ...(variant === 'rail'
            ? {
              width: '100%',
              ...schemed(t, c => ({ color: c.textStrong, borderColor: scope ? c.accent : c.buttonBorder })),
              '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover })),
              '&.Mui-focusVisible': { ...schemed(t, c => ({ outline: `2px solid ${c.accent}` })), outlineOffset: '2px' },
            }
            : {
              color: 'text.primary',
              borderColor: scope ? 'primary.main' : 'divider',
              bgcolor: 'background.paper',
              '&.Mui-focusVisible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: '2px' },
            }),
        })}
      >
        <Box component="span" className="org-scope-word" sx={{ flexShrink: 0, fontSize: '0.6875rem', fontWeight: 600, opacity: 0.75 }}>
          Orgs
        </Box>
        <Box
          component="span"
          data-testid="org-scope-chip"
          sx={{ minWidth: 0, maxWidth: '24ch', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontWeight: 600 }}
        >
          {first}
        </Box>
        {moreNamed > 0 && (
          <Box component="span" data-testid="org-scope-more" sx={{ flexShrink: 0, fontWeight: 600 }}>
            +{moreNamed}
          </Box>
        )}
        <Box component="span" aria-hidden="true" sx={{ flexShrink: 0, marginLeft: 'auto', fontSize: '0.625rem', opacity: 0.7 }}>
          ▾
        </Box>
      </ButtonBase>
    )

  return (
    <>
      <Tooltip title={open ? '' : `Showing: ${full}`} placement={variant === 'bar' ? 'bottom' : 'right'}>
        {trigger}
      </Tooltip>
      <Popover
        open={open}
        anchorEl={anchor.current}
        // Click-away keeps the edit (it is the picker's "done"); Escape discards it.
        onClose={(_e, reason) => (reason === 'escapeKeyDown' ? setOpen(false) : commit(draft))}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        slotProps={{
          paper: {
            role: 'dialog',
            'aria-label': 'Filter by org',
            'data-testid': 'org-scope-picker',
            sx: { width: 300, maxWidth: 'calc(100vw - 32px)', display: 'flex', flexDirection: 'column', maxHeight: 'min(480px, calc(100dvh - 96px))' },
          } as object,
        }}
      >
        <Box sx={{ p: 1, pb: 0.5 }}>
          <TextField
            autoFocus
            fullWidth
            size="small"
            placeholder="Search orgs"
            value={query}
            onChange={e => search(e.target.value)}
            onKeyDown={onSearchKey}
            inputRef={searchRef}
            slotProps={{ htmlInput: { 'aria-label': 'Search orgs', 'data-testid': 'org-scope-search' } }}
          />
        </Box>
        <MenuList ref={listRef} dense onKeyDownCapture={onListKey} sx={ONLY_REVEAL_SX} aria-label="Orgs">
          {!query.trim() && (
            <MenuItem
              data-testid="org-scope-all"
              role="menuitemcheckbox"
              aria-checked={draft === null}
              selected={draft === null}
              onClick={() => commit(null)}
            >
              <Checkbox size="small" checked={draft === null} tabIndex={-1} disableRipple sx={{ p: 0.5, mr: 1 }} />
              <Typography variant="body2" sx={{ fontWeight: 600 }}>{ALL_ORGS}</Typography>
            </MenuItem>
          )}
          {shown.map(org => {
            const checked = draftIds.has(org.id)
            return (
              <MenuItem
                key={org.id}
                data-testid={`org-scope-option-${org.slug}`}
                onClick={() => toggle(org)}
                aria-checked={checked}
                role="menuitemcheckbox"
              >
                <Checkbox size="small" checked={checked} tabIndex={-1} disableRipple sx={{ p: 0.5, mr: 1 }} />
                <Box sx={{ minWidth: 0, flex: 1 }}>
                  <Typography variant="body2" noWrap title={org.name}>{org.name}</Typography>
                  <Typography variant="caption" sx={{ color: 'text.secondary' }} noWrap component="div">{org.slug}</Typography>
                </Box>
                <Button
                  size="small"
                  className={ONLY_CLASS}
                  data-testid={`org-scope-only-${org.slug}`}
                  aria-label={`Only ${org.name}`}
                  onClick={e => {
                    e.stopPropagation()
                    commit([org])
                  }}
                  onMouseDown={e => e.stopPropagation()}
                  // Its keys stay its own (the list's arrow and typeahead handling assume focus on a row); Escape still closes.
                  onKeyDown={e => {
                    if (e.key !== 'Escape') e.stopPropagation()
                  }}
                  sx={{ flexShrink: 0, minWidth: 0, ml: 1, px: 1, py: 0 }}
                >
                  Only
                </Button>
              </MenuItem>
            )
          })}
        </MenuList>
        {roster.isSuccess && shown.length === 0 && <EmptyState title="No orgs match" />}
        {roster.isSuccess && more > 0 && (
          <Typography variant="caption" data-testid="org-scope-more-orgs" sx={{ color: 'text.secondary', px: 2, py: 0.5 }}>
            {countOf(more, 'more org')} · search to narrow
          </Typography>
        )}
        {roster.isLoading && <Typography variant="body2" sx={{ color: 'text.secondary', px: 2, py: 1 }}>Loading orgs…</Typography>}
        {roster.isError && <Typography variant="body2" color="error" sx={{ px: 2, py: 1 }}>Could not load orgs</Typography>}
        <Box sx={{ display: 'flex', justifyContent: 'space-between', gap: 1, p: 1, borderTop: 1, borderColor: 'divider' }}>
          <Button size="small" onClick={() => setDraft(null)} disabled={draft === null} data-testid="org-scope-clear">
            Clear
          </Button>
          <Button size="small" variant="contained" onClick={() => commit(draft)} data-testid="org-scope-done">
            Done
          </Button>
        </Box>
      </Popover>
    </>
  )
}

/* HOW MANY `OrgScopeInline`s ARE MOUNTED: while one is, the strip draws nothing — one scope chip, never two. A
   module store, not a context: the strip (`App.tsx`'s `mainTop`) and the page's header are siblings under the shell,
   and a count needs no provider wired around both. */
const inlineCount = createExternalStore(0)

/** The phone's scope strip, above every page, a record's included: the rail is a bottom bar there, and the scope
 *  must stay in sight and changeable — a record the filter hides is only reachable by changing it. */
export function OrgScopeBar(): JSX.Element | null {
  const { admin } = useAuth()
  const inline = useExternalStore(inlineCount, count => count > 0)
  // Without Multiple orgs, one org: nothing to filter (the rail's control is gated the same way, `Sidebar.tsx`).
  if (admin?.is_operator !== true || inline || !isEnabled('multi_org_tenancy')) return null
  return (
    <Box data-testid="org-scope-bar" sx={{ display: 'flex', alignItems: 'center', mb: 1, minWidth: 0 }}>
      <OrgScopeControl variant="bar" />
    </Box>
  )
}

/** The strip's chip, in a page header's own row: at 390px the strip cost the task editor a whole row
 *  above a crumb trail with room beside it. Phone-only (above it the rail carries the scope) and operator-only, like
 *  the strip, which it suppresses while mounted. It shrinks (the name ellipsises) and, in a wrapping row, takes a
 *  line of its own where even the shrunk chip does not fit. */
export function OrgScopeInline(): JSX.Element | null {
  const { admin } = useAuth()
  // Never without Multiple orgs: one org, nothing to filter (as `OrgScopeBar`).
  const shown = useMediaQuery(PHONE_QUERY) && admin?.is_operator === true && isEnabled('multi_org_tenancy')
  /* A layout effect, so the strip is gone before the first paint rather than a frame after. */
  useLayoutEffect(() => {
    if (!shown) return
    inlineCount.set(inlineCount.get() + 1)
    return () => inlineCount.set(inlineCount.get() - 1)
  }, [shown])
  if (!shown) return null
  return (
    <Box data-testid="org-scope-inline" sx={{ display: 'flex', alignItems: 'center', flex: '0 1 auto', minWidth: 0, maxWidth: '100%' }}>
      <OrgScopeControl variant="bar" />
    </Box>
  )
}
