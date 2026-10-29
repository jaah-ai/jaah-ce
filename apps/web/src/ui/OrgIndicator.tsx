import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useCallback, type JSX } from 'react'
import { useAuth } from '../auth'
import { isEnabled } from '../lib/editionFeatures'
import { useOrgScope } from '../lib/orgScope'
import Banner from './Banner'
import type { OrgRef } from '../types'
import { monoFamily } from './feed/geometry'
import { ORG_QUERY, PHONE } from './listStyles'
import type { FeedSlot } from './feed/types'

/* The org indicator — the console's one answer to "which tenant does this belong to", used via
 * `ui/feed` slots, `ui/DataTable` cells and `ui/detail` headers; pages pick placement and width, the ink
 * is this file's. Drawn for an operator only — see `useOrgVisible`.
 * Draws the org's `name` (what a person reads); the slug rides the `title` beside it. The id is never
 * drawn (it rides `data-org-id` for oracles) except in the unresolved state (`unresolvedId`).
 * The tint groups, the text identifies: hues come from a closed list, so orgs can share one; low alpha
 * behind `text.primary` keeps contrast a theme decision.
 * No org is a real state: the resolved org on the wire can be NULL (`app/authz/enforce.py::_bind_read_scope`
 * binds only for principals with an org — e.g. the agent plane, or the operator sentinel scope). It
 * renders as a dimmed em dash, never as the default org. */

/** Tint hues, spaced around the wheel. A closed list, not `hash % 360`: yellow-greens vanish at 14%
 *  alpha on light paper. Hues repeat (two of four orgs collide ~43% of the time), which is acceptable
 *  only because the tint is never the identifying signal — nothing may derive a tenant from a colour,
 *  and no test may assert two orgs differ in hue. */
export const ORG_HUES = [212, 258, 340, 12, 30, 92, 168, 190, 236, 288, 318, 50]

/** Stable hue per org id. FNV-1a over the whole id: UUIDv7 ids share their timestamp prefix and differ
 *  in the last few hex chars, which a weak mixer collapses (the four real ids land on four hues, pinned
 *  in the test file — still not a promise). `>>> 0` reduces over the unsigned value. Total over any
 *  string. */
export function orgHue(orgId: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < orgId.length; i += 1) {
    hash ^= orgId.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return ORG_HUES[(hash >>> 0) % ORG_HUES.length]
}

/** Shape check, not truthiness: a caller indexing a JSON map with a raw URL value (`?repo=constructor`)
 *  gets a truthy `Object.prototype.constructor` that would throw on field read. */
function isOrgRef(org: OrgRef | null | undefined): org is OrgRef {
  return !!org && typeof org.id === 'string' && typeof org.slug === 'string'
}

/** The announced words, shared by slot `title` and chip. Three states: resolved org; `unresolvedId`
 *  (read only when `org` did not resolve) naming a tenant row that is gone — not "unknown", matching
 *  `routes/tenancy_exclusions.py::_org_label`; or no org. */
export function orgTitle(org: OrgRef | null | undefined, unresolvedId?: string): string {
  if (isOrgRef(org)) return `Org: ${org.name} (${org.slug})`
  if (unresolvedId) return `Org ${unresolvedId} — no such org; the tenant row is gone`
  return 'No org — this row is not bound to a tenant'
}

export function OrgIndicator({
  org,
  testId,
  inkOnly = false,
  plain = false,
  unresolvedId,
}: {
  /** The row's org, or `null`/`undefined` when unbound — see the docblock. */
  org: OrgRef | null | undefined
  testId?: string
  /* The row's `org_id` when it names no `orgs` row — the one case this file draws an id (there is no
   * slug or name left, and a blank would read as the no-org state; as `routes/tenancy_exclusions.py::
   * _org_label` decides server-side). Default-absent, so earlier callers are unchanged. Ignored whenever
   * `org` resolves. Not tinted (it belongs to no group); dim + mono, the raw-identifier register. */
  unresolvedId?: string
  /** Draw only: emit no `title`/`data-*`. For `orgSlot`, whose slot box (`ui/feed/FeedList.tsx`) already
   *  carries them and is where `data-testid="col-org"` resolves — the outermost element owns them, so
   *  `[data-org-id]` never matches twice. Standalone uses omit it. */
  inkOnly?: boolean
  /** The name as plain text, untinted: for a list of facts whose other values are plain text (the host page's
   *  Status), where a tinted token stands out as a different kind of thing. */
  plain?: boolean
}): JSX.Element {
  if (!isOrgRef(org)) {
    /* Tenant gone, id is all there is. ~230px at 11px, so it ellipsises in a slot; a surface drawing it
       in a slot owes the id a ⋯ info item (`lib/credentialRow.tsx`), since `title` is unreachable by touch. */
    if (unresolvedId) {
      return (
        <Box
          component="span"
          data-testid={testId}
          data-org-id={inkOnly ? undefined : unresolvedId}
          data-org-unresolved={inkOnly ? undefined : 'true'}
          title={inkOnly ? undefined : orgTitle(org, unresolvedId)}
          sx={theme => ({
            display: 'inline-block',
            maxWidth: '100%',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            fontFamily: monoFamily(theme),
            fontSize: 'inherit',
            color: 'text.disabled',
          })}
        >
          {unresolvedId}
        </Box>
      )
    }
    return (
      <Box
        component="span"
        data-testid={testId}
        data-org-id={inkOnly ? undefined : ''}
        title={inkOnly ? undefined : orgTitle(org)}
        sx={{ color: 'text.disabled' }}
      >
        —
      </Box>
    )
  }
  const hue = orgHue(org.id)
  return (
    <Box
      component="span"
      data-testid={testId}
      data-org-id={inkOnly ? undefined : org.id}
      data-org-slug={inkOnly ? undefined : org.slug}
      title={inkOnly ? undefined : orgTitle(org)}
      sx={{
        display: 'inline-block',
        maxWidth: '100%',
        /* Never shrinks or wraps its text (`StatusChip`'s rule); a too-narrow box ellipsises visibly, so
           callers price their own width against real names. */
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        fontSize: 'inherit',
        color: 'text.primary',
        /* An `overflow: hidden` inline-block sits on its bottom edge, not its text's baseline: `top` lines it up
           with the label beside it. */
        ...(plain ? { verticalAlign: 'top' } : {
          px: 0.5,
          borderRadius: 0.75,
          fontWeight: 600,
          lineHeight: 1.5,
          bgcolor: `hsl(${hue} 72% 50% / 0.16)`,
          boxShadow: `inset 0 0 0 1px hsl(${hue} 72% 50% / 0.34)`,
        }),
      }}
    >
      {org.name}
    </Box>
  )
}

/** Default feed-slot width. The one home for this arithmetic (`pages/Projects.tsx` and
 *  `e2e/org-indicator.spec.ts` point here). Priced for a NAME, not a slug: `Northwind Trading` is
 *  104px of 600-weight `system-ui` ink at 0.6875rem/11px in headless Chromium (wider on other platforms'
 *  `system-ui`, ~10%), plus `px: 0.5` (3px a side) — 128px reads it, and `Acme Logistics`, whole. The outline is an inset `box-shadow`, costing no layout. Longer names ellipsise, the
 *  full name and slug on `title`. A default, not a law: re-derive per surface, in a browser
 *  (`ui/feed/geometry.ts`). */
export const ORG_SLOT = 128

/** The indicator as a `ui/feed` slot. Ungated by default: `meta`/`id` gates hide on narrowing, where it
 *  is graded. Exported for `OrgIndicator.test.tsx`, which pins `orgSlots(true, …)[0]` equal to this. */
export function orgSlot(
  org: OrgRef | null | undefined,
  /** `unresolvedId` passes straight to the component (see its prop doc). */
  opts: { width?: number; gate?: FeedSlot['gate']; testId?: string; unresolvedId?: string } = {},
): FeedSlot {
  return {
    key: 'org',
    width: opts.width ?? ORG_SLOT,
    gate: opts.gate,
    align: 'left',
    title: orgTitle(org, opts.unresolvedId),
    testId: opts.testId ?? 'col-org',
    /* The slot is the one spelling of title and data (token is `inkOnly`). It carries the raw `org_id`
       even when unresolved — the only handle left; resolved id first, so both halves cannot disagree. */
    data: { 'org-id': org?.id ?? opts.unresolvedId ?? '', 'org-slug': org?.slug ?? '' },
    node: <OrgIndicator org={org} unresolvedId={opts.unresolvedId} inkOnly />,
  }
}

/* ── Who may see which tenant a row belongs to ──────────────────────────────────────────────────────
 *
 * The one verdict for every surface. Only an operator reads across tenants; for anyone else the read
 * seam (`app/authz/enforce.py::_bind_read_scope`) already narrows to the viewer's tenant, so the column repeats
 * one value and costs title width. Composed here by `orgSlots`, `OrgAside` and `useOrgColumn`.
 * Other places consult it without drawing a token (credentials menu/filter/roster) — grep for the hook, not the token, and re-derive counts rather than carrying them.
 * `is_operator` decides, never `is_superadmin` (a tenant superadmin sees only its own org's rows).
 * Fails closed on an absent viewer or an absent flag: read strictly (`=== true`), since a replayed
 * `loadAuthSnapshot` validates only `id` and `email`.
 * Not covered: the `resolving` window. `admin` seeds from the snapshot, so a just-demoted superadmin
 * sees the token for one paint — deliberate, as `App.tsx`'s `RequireSuperadmin`; holding would blank
 * the column on every warm reload.
 * Presentation only, never the boundary (`components/RequireFeature.tsx`): payloads still ship
 * `org.name` (`app/tenancy/labels.py::OrgRef`).
 * Never without Multiple orgs: one org, nothing to tell apart. */
export function useOrgVisible(): boolean {
  const { admin } = useAuth()
  return admin?.is_operator === true && isEnabled('multi_org_tenancy')
}

/** Whether a list row draws its org slot (`pages/nodes/nodeLinkStore/NodeLinkList.tsx`).
 *  Width half: `ORG_QUERY` (1185px; derivation at the constant), read in JS rather than an `sx` media
 *  block; below the gate the slot is absent from the DOM. jsdom's `matchMedia` stub answers `false`; reach the other
 *  branch with `vi.stubGlobal`.
 *  Role half: `useOrgVisible`.
 *  Both hooks run unconditionally (no `&&` short-circuit — rules of hooks); call before any early return. */
export function useOrgColumn(): boolean {
  const wideEnough = useMediaQuery(ORG_QUERY)
  const orgVisible = useOrgVisible()
  return wideEnough && orgVisible
}

/** The org slot or none, spread into `slots` (the conditional-column shape `pages/workflows/
 *  WorkflowRunsTab.tsx` uses). `visible` is passed in: callers build slots inside `.map()` (and
 *  `lib/credentialRow.tsx` is not a component), so read `useOrgVisible()` once and hand it down (or via
 *  `CredentialRowDeps.showOrg`). Withheld means absent — an empty slot still reserves width. */
export function orgSlots(
  visible: boolean,
  org: OrgRef | null | undefined,
  opts: { width?: number; gate?: FeedSlot['gate']; testId?: string; unresolvedId?: string } = {},
): FeedSlot[] {
  return visible ? [orgSlot(org, opts)] : []
}

/** The tenant beside a detail page's title, for an operator only. Shared by `pages/taskDetail/TaskDetailHeader.tsx`
 *  and `pages/taskDetail/TaskCreateView.tsx`, `pages/ProjectEditPage.tsx`, `pages/ProjectDocsPage.tsx`.
 *  The label is part of the answer (a header has no column heading): `Org` for the record's tenant,
 *  `Your org` for the viewer's (`auth.tsx`); absent where the chip already reads as the record's tenant
 *  (`pages/ProjectEditPage.tsx`, where the token's `title` names it). Self-gates, unlike the pure-ink
 *  `OrgIndicator`, so `null` removes the whole statement. `wrap` for `pages/ProjectCreateWizard.tsx`'s two-line aside; default off.
 *  Handles stay the caller's: `e2e/org-indicator.spec.ts` reads four distinct chip testids. */
export function OrgAside({
  label,
  org,
  chipTestId,
  testId,
  labelTestId,
  wrap = false,
  hideOnPhone = false,
}: {
  /** The word that introduces the token — `Org` for the record's tenant, `Your org` for the viewer's. Absent:
   *  the token alone. */
  label?: string
  org: OrgRef | null | undefined
  /** The token's handle — required; surfaces' handles are not derivable. */
  chipTestId: string
  /** Wrapper handle (`pages/taskDetail/TaskDetailHeader.tsx`). */
  testId?: string
  /** Caption handle (`pages/ProjectEditPage.tsx` tells create from edit by it). */
  labelTestId?: string
  wrap?: boolean
  /** Drop the whole aside at `PHONE` (≤480px): `/tasks/:id`'s two asides must not show the
   *  org name on mobile web. Default false: `pages/ProjectEditPage.tsx` and `pages/ProjectDocsPage.tsx` keep
   *  showing it at every width. */
  hideOnPhone?: boolean
}): JSX.Element | null {
  const visible = useOrgVisible()
  if (!visible) return null
  return (
    <Box
      data-testid={testId}
      sx={{
        display: 'flex',
        alignItems: 'center',
        gap: 0.5,
        minWidth: 0,
        /* 0.8125rem: `pages/taskDetail/TaskDetailHeader.tsx`'s measured breadcrumb size, which keeps its header height
           at 1280 and 1440. Unified for all four callers (caption and token now share one size); header
           height is set by the `h6` title, re-rendered at 1440 and 448. A subordinate caption would be a
           prop here, not a fifth aside. */
        fontSize: '0.8125rem',
        lineHeight: 1.5,
        ...(wrap ? { flexWrap: 'wrap' } : {}),
        ...(hideOnPhone ? { [PHONE]: { display: 'none' } } : {}),
      }}
    >
      {/* `nowrap` on the label only; the token owns its own no-wrap and ellipsis. */}
      {label && (
        <Typography
          component="span"
          variant="inherit"
          data-testid={labelTestId}
          sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}
        >
          {label}
        </Typography>
      )}
      <OrgIndicator org={org} testId={chipTestId} />
    </Box>
  )
}

export default OrgIndicator

/* ── Another org's row ──────────────────────────────────────────────────────────────────────────────────
 *
 * An operator READS every org's rows and EDITS them on that org's behalf: an unacting write is refused
 * (`app/authz/enforce.py::OtherOrgDenial`), so every write a detail page sends for another org's row carries the
 * row's org for that one request (`lib/actAs.ts::actAsOnce`, or `useActAsRows` for every write below the row), and
 * `ActingForNotice` says whose name the changes carry. A picker offers only rows a write may target: the operator's own
 * org's on a create, the row's org's on another org's row. */

/** The line over a detail page an operator EDITS for another org (the operator acts on that org's behalf).
 *  Every write that page sends goes as the row's org for that one request (`lib/actAs.ts::actAsOnce`), so it says
 *  whose name the changes carry. */
export function ActingForNotice({ org, testId }: { org: OrgRef | null; testId: string }): JSX.Element | null {
  if (!org) return null
  return <Banner severity="info" testId={testId}>Changes are made as {org.name}.</Banner>
}

/** The row's org when it is NOT the viewing operator's own, else `null`. `null` for a non-operator (the
 *  read seam already confines them to their org), for an unknown viewer org, and for a row with no org. */
export function useOtherOrg(org: OrgRef | null | undefined): OrgRef | null {
  return useOtherOrgOf()(org)
}

/** `useOtherOrg` as a callback, for a surface judging many rows (the board's row verbs and Delete,
 *  `hooks/useTaskAccess.ts`). */
export function useOtherOrgOf(): (org: OrgRef | null | undefined) => OrgRef | null {
  const { admin } = useAuth()
  const own = admin?.is_operator === true ? admin.org?.id : undefined
  return useCallback(
    (org: OrgRef | null | undefined) => (!own || !isOrgRef(org) || org.id === own ? null : org),
    [own],
  )
}

/** The org an operator's filter names when it names exactly ONE org other than their own (`?orgs=org-b`), else
 *  `null` — `null` for a non-operator too. An add opened under such a filter starts on that org, so what
 *  it makes lands where the operator is looking. */
export function useScopedOtherOrg(): OrgRef | null {
  const { admin } = useAuth()
  const scope = useOrgScope()
  const own = admin?.is_operator === true ? admin.org?.id : undefined
  return own && scope?.length === 1 && scope[0].id !== own ? scope[0] : null
}

/** Narrows a picker's rows to the viewing operator's own org — the only org a create may target. Identity
 *  for a non-operator. A row whose payload carries no `org` field at all is kept (nothing to judge by). */
export function useOwnOrgRows(): <T extends { org?: OrgRef | null }>(rows: T[]) => T[] {
  const { admin } = useAuth()
  const own = admin?.is_operator === true ? admin.org?.id : undefined
  return useCallback(
    <T extends { org?: OrgRef | null }>(rows: T[]): T[] =>
      own ? rows.filter(row => row.org === undefined || row.org?.id === own) : rows,
    [own],
  )
}
