/* THE CREDENTIAL ROSTER'S LEAF ROW — the ONE producer of a credential row, for Flat mode
 * (`pages/credentials/Credentials.tsx`) and Tree mode (`pages/credentials/credentialTree.ts`), published in `lib/` so
 * neither page grows a second row builder.
 * PURE, AND IT CALLS NO HOOK (like `nameTree.ts`/`docsTree.ts`): viewport and visibility verdicts
 * arrive as plain deps (`showDescription`, `showOrg`); the CALLER owns the hooks. `CopyRunnerPathButton` uses
 * hooks, but `credentialFeedRow`'s own path does not.
 * The slot widths below were BISECTED IN A BROWSER per `ui/feed/geometry.ts` / `ORG_SLOT`'s rule; the
 * table below is the authority. Two scope reductions pay for the owner and org slots at 1024px:
 *   1. THE UUID IS NOT DRAWN AT ALL: nothing in the system takes a credential id as input —
 *      every picker is `ui/CredentialSelect` — so the id column and "Copy id" were removed.
 *   2. THE DESCRIPTION IS ABSENT BELOW 1280px, composed away by the caller (`DESCRIPTION_SLOT`).
 */
import Box from '@mui/material/Box'
import type { JSX, ReactNode } from 'react'
import { deleteItem, editItem, type OverflowItem } from '../components/tasks/rowActions'
import { countOf } from './humanize'
import type { Credential } from '../pages/credentials/api'
import { contentSlotWidth } from '../ui/feed/geometry'
import type { FeedIndicator, FeedMarker, FeedRowSpec, FeedSlot } from '../ui/feed/types'
import { CopyButton } from '../ui/CopyButton'
import { FEED_META } from '../ui/listStyles'
import { orgSlots, orgTitle } from '../ui/OrgIndicator'
import { copyText } from '../ui/useCopyFlash'
import { shortHash } from './nodeVaultKeys'
import { absoluteTime, compactAge, NO_VALUE, relativeAge } from './relativeTime'

/* ── THE CLUSTER, RE-DERIVED IN A BROWSER ────────────────────────────────────────────────────────────
 * Chromium, `/credentials`, first row fixtured with the longest realistic value in every slot (119-char
 * name, 124-char description, `Aleksandra Konstantinova`, `northwind`, `JIRA`); `getBoundingClientRect()`
 * reads with every slot present.
 *
 *   viewport    448     1024    1280    1440     what it is
 *   ──────────────────────────────────────────────────────────────────────────────────────────────────
 *   row         416      780    1036    1196
 *   TITLE       163      378     408     568     the flex remainder — this is the number that matters
 *   floor        40      200     200     200     `e2e/tasks-feed.spec.ts`'s two floors
 *   clears     +123     +178    +208    +368
 *   ──────────────────────────────────────────────────────────────────────────────────────────────────
 *   descr         0        0     220     220     composed in at ≥1280 only (`DESCRIPTION_QUERY`)
 *   owner         0      120     120     120     `meta`    — dropped at 448, second home in the ⋯
 *   copy name    24       24      24      24     `titleAction` + 4px margin; never drawn on touch
 *   org           0      128     128     128     `meta`    — dropped at 448 (it left the title ~70px),
 *                                                            second home in the ⋯
 *   type         88       88      88      88     `credentialsType` (hangs under the name below a ~400px list); off
 *                                                  the phone, widens to the page's widest label (`TYPE_MAX_CQW`)
 *   ⋯            24       30      30      30     `ACTIONS_SLOT` 30, pulled back to 24 on the phone
 *   gaps          9       24      30      30     3px x3 at 448 (`ROW_SX`'s `[PHONE]` half-gap), else 6px
 *   inset        25       28      28      28     `RAIL_X` + `ROW_PAD_L` + `pr: 0.5` (derived)
 *   ──────────────────────────────────────────────────────────────────────────────────────────────────
 *   row height   24       30      30      30     one-line title, ellipsised; below a ~400px list the
 *                                                  type hangs under it and every Flat row is two lines
 *   doc scroll    0        0       0       0     nothing overflows the document
 *   row scroll    0        0       0       0     nor its own box
 *
 * Re-read after the 80px id slot was removed: the title gained that slot and its 6px gap and paid 28px
 * for the copy control (24 + 4px margin), which is budgeted whether or not it shows — hidden, not removed —
 * so hover never reflows the row (1024: 319 + 86 − 28 ≈ 378).
 * The shell measures 244px vs the 246 `ui/listStyles.ts` charges (conservative, safe direction).
 * 1024 IS THE BINDING WIDTH: every gated slot is live there (`FEED_NO_META` ≤899 sits in the unsupported
 * gap).
 * RE-DERIVE, NEVER RE-COPY: bisect in a browser with every slot present and READ THE ROW HEIGHT too (a
 * width-only oracle stays green through a two-line row). Standing check: the credentials-feed end-to-end spec. */

/** THE OWNER — 120px, what R1.4's identifiability bar costs: ~18 characters at 0.6875rem, enough for the
 *  ordinary name; longer ones ellipsise with the full string on `title` AND an uncapped ⋯ item (R1.5).
 *  NO INITIALS BADGE: the acceptance fails "an owner truncated to a first initial" even when the row fits.
 *  Not wider: priced on what it must SHOW, not the slack beside it. */
const OWNER_SLOT = 120

/** THE TENANT — 128px on the `meta` gate: the token draws the org NAME, priced in `ui/OrgIndicator.tsx`'s
 *  `ORG_SLOT` on `Northwind Trading`. Dropped at 448, where it left the title ~70px; its second home there
 *  is the ⋯'s unconditional Org item.
 *  Oracled by `e2e/credentials-feed.spec.ts` on the TOKEN's own box (it ellipsises internally, so the
 *  slot's `scrollWidth` never moves) at 1024 and 1440. `CredentialRowDeps.showOrg` also withholds the slot from every
 *  non-operator viewer at every width. */
const ORG_SLOT_CREDENTIALS = 128

/** THE TYPE — 88px floor, on every row a ~400px list can afford (`credentialsType`): what an operator scans a roster BY. Sized to the page's widest label
 *  (`credentialTypeSlotWidth`), so "GitHub OAuth App (connect and sign-in)" reads whole on a desktop. */
const TYPE_SLOT = 88
/** At most this percent of the list's width (`cqw`): the registry's longest label (~245px) fits a 1024px window's
 *  780px list and still leaves the title its 200px floor. */
const TYPE_MAX_CQW = 32

/** The Type cell's text. Beside an open record pane the list is ~550px and the cap (~176px) ellipsised
 *  "GitHub OAuth App (connect and…", so a trailing qualifier is dropped there ("GitHub OAuth App"); the cell's
 *  `title` keeps the whole label. No two registry labels collide once trimmed. */
export function credentialTypeLabel(label: string, besideDetail = false): string {
  return besideDetail ? label.replace(/\s*\([^()]*\)$/, '') : label
}

/** The Type column's one width for every row on the page (`ui/feed/geometry.ts::contentSlotWidth`); the host list
 *  sets `containerType: 'inline-size'` for the `cqw` cap. */
export function credentialTypeSlotWidth(rows: readonly Credential[], besideDetail = false): number | string {
  return contentSlotWidth(rows.map(row => credentialTypeLabel(row.type_label, besideDetail)), TYPE_SLOT, TYPE_MAX_CQW)
}

/** THE DESCRIPTION — 220px, COMPOSED IN only from 1280px up (SCOPE REDUCTION 2).
 *  NOT A `SlotGate`: every gate fires below 1024, so a gated slot is present at 1024 — exactly where the
 *  row has nothing to give. An omitted slot reserves no width.
 *  READ WITH `useMediaQuery` IN THE CALLER, never as an `sx` media block, so the slot is ABSENT from the DOM
 *  below the gate rather than hidden. jsdom's `matchMedia` stub (`src/test/setup.ts`) returns `matches: false`; opt in with `vi.stubGlobal`.
 *  WHY 1280: at 1024 it would leave 93px of title (< 200 floor); at 1280 the title measures 349 with it,
 *  clearing by 149. Not narrowed below 220 (R1.4 protects the text). Second home below 1280: the ⋯ info
 *  item and `/credentials/:id`. */
const DESCRIPTION_SLOT = 220
export const DESCRIPTION_QUERY = '(min-width:1280px)'

/** THE HEALTH CELLS — Last used and Last test, 64px each, as columns from 1440px
 *  (`credentialsHealthColumns`): `Never used` and `Failed · 23h` are the widest values at 11px. Narrower, the pair left
 *  a long name under its 200 floor (184px at 1024, 194 at 1280; `ui/listStyles.ts`), so below 1440 both fold into ONE
 *  72px cell (`credentialsHealth`: `Used 3d ✕`), on the phone too — the row stays one line
 *  (a one-line row stays one line; its prose goes on hover). Not sortable; the expiry is a marker on the name instead
 *  (absent costs nothing). Beside an open record pane all of it leaves, like the owner and org, and the hover note
 *  carries it. */
const HEALTH_SLOT = 64
const HEALTH_FOLDED_SLOT = 72
/** An expiry this close or closer is called out ("Expires in 3d"); `roster._health`'s `expiring` uses the same 7. */
const EXPIRY_SOON_DAYS = 7
const DAY_MS = 86_400_000

/** Last used: `Used 3d` / `Never used`, with the sentence for its `title` and the row's hover. */
function lastUsed(row: Credential): { text: string; sentence: string } {
  if (!row.last_used_at) return { text: 'Never used', sentence: 'Never used' }
  return {
    text: `Used ${compactAge(row.last_used_at)}`,
    sentence: `Last used ${relativeAge(row.last_used_at)} (${absoluteTime(row.last_used_at)})`,
  }
}

/** Last test: `OK · 2h` / `Failed · 2h`, or `—` never tested. `ok` is null when there is no verdict. */
function lastTest(row: Credential): { text: string; sentence: string; ok: boolean | null } {
  if (!row.last_tested_at || row.last_test_ok == null) return { text: NO_VALUE, sentence: 'Never tested', ok: null }
  const verdict = row.last_test_ok ? 'OK' : 'Failed'
  return {
    text: `${verdict} · ${compactAge(row.last_tested_at)}`,
    sentence: `Last test ${verdict.toLowerCase()} ${relativeAge(row.last_tested_at)} (${absoluteTime(row.last_tested_at)})`,
    ok: row.last_test_ok,
  }
}

/** The expiry badge: `Expired`, `Expires in Nd` within `EXPIRY_SOON_DAYS`, else none. Exported for its tests. */
export function credentialExpiry(expiresAt: string | null | undefined, now: number = Date.now()): { text: string; expired: boolean } | null {
  if (!expiresAt) return null
  const at = Date.parse(expiresAt)
  if (Number.isNaN(at)) return null
  if (at <= now) return { text: 'Expired', expired: true }
  const days = Math.ceil((at - now) / DAY_MS)
  return days <= EXPIRY_SOON_DAYS ? { text: `Expires in ${days}d`, expired: false } : null
}

const TEST_TONE_SX = { ok: { color: 'success.main' }, failed: { color: 'error.main' } } as const
const EXPIRY_TONE_SX = { expired: { color: 'error.main', fontWeight: 600 }, soon: { color: 'warning.main', fontWeight: 600 } } as const

/** A verdict in its tone: green OK, red Failed; the words say it too. */
function testNode(text: string, ok: boolean | null): ReactNode {
  if (ok === null) return text
  return <Box component="span" sx={ok ? TEST_TONE_SX.ok : TEST_TONE_SX.failed}>{text}</Box>
}

/** The ⋯ item's copy mark is `❐` (U+2750), not the app's usual `⧉` (U+29C9), which is TOFU on this stack
 *  (probed against a private-use codepoint). In a menu the glyph is decoration beside "Copy runner path"; the row's
 *  own control draws an SVG instead (`CopyRunnerPathButton`). */
const COPY_GLYPH = '❐'

/** The ⋯ item's label, and the head of the inline control's accessible name. */
const COPY_PATH_LABEL = 'Copy runner path'

/** Between a home's segments, and after the last one before the name. */
export const HOME_SEPARATOR = '›'

/** `['Acme', 'homejab']` → `Acme › homejab ›`, the locked prefix drawn before a name (roster and form alike);
 *  `''` for no home. */
export function credentialHomeLabel(path: readonly string[] | null | undefined): string {
  return path && path.length > 0 ? `${path.join(` ${HOME_SEPARATOR} `)} ${HOME_SEPARATOR}` : ''
}

/** The path a runner names this credential by: `org/<name>`, `projects/<project>/<name>` (an org row homed on a
 *  project) or `me/<name>` (the VIEWER's own personal row). `null` for a system row — tasks never see it — for a
 *  colleague's personal row, which no task of the viewer's can reach, and for a row whose ownership the payload
 *  does not say. A personal row is the viewer's when its home names their email (`home_path` is `[org, owner
 *  email]`). */
export function credentialRunnerPath(
  row: Pick<Credential, 'name' | 'ownership' | 'home_project' | 'home_path'>,
  viewerEmail?: string | null,
): string | null {
  if (row.ownership === 'personal') {
    const ownerEmail = row.home_path?.[1]
    const mine = Boolean(viewerEmail && ownerEmail && ownerEmail.toLowerCase() === viewerEmail.toLowerCase())
    return mine ? `me/${row.name}` : null
  }
  if (row.ownership !== 'org') return null
  return row.home_project ? `projects/${row.home_project.toLowerCase()}/${row.name}` : `org/${row.name}`
}

/** The home as a muted lead inside the title: it shrinks first (`flexShrink` far above the name's), so a narrow
 *  row cuts the home to an ellipsis before it cuts a letter of the name. The whole home rides its `title`. */
const HOME_PREFIX_SX = {
  flex: '0 1000 auto',
  // Never a percentage cap: the title is shrink-to-fit, so `50%` of it clipped the home with room to spare.
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  color: 'text.secondary',
  mr: 0.5,
} as const
const TITLE_WITH_HOME_SX = { display: 'inline-flex', maxWidth: '100%', minWidth: 0, verticalAlign: 'bottom' } as const
const NAME_AFTER_HOME_SX = { flex: '0 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const

/** ⋯ description preview cap — 31 characters, read at the narrowest supported viewport. `MenuItem` never
 *  wraps and the paper is viewport-capped (416px at 448), so an uncapped long description CLIPPED. The
 *  widest item, `Org id: <uuid>` (44 chars), fits; `Description: ` spends 13 of those 44.
 *  Org id is fixed-width and never capped (the menu is its only readable home). The full string still
 *  rides the item's `title` and accessible name, and `/credentials/:id` holds the value. */
const DESCRIPTION_MENU_CAP = 31

/** A NodeVault row's mark beside its name (AC-5.5). A marker, not a slot: it costs a row without it nothing,
 *  and the 1024px cluster above has no width left. The key hash rides its hover sentence and a ⋯ info item
 *  (the touch home); `/credentials/:id` prints it under the NodeVault box. */
const NODEVAULT_GLYPH = '◈'

/** `NodeVault · key 1a2b3c4d5e6f`, the mark's hover sentence and the ⋯ item's label. */
function nodeVaultLabel(row: Credential): string {
  return row.nodevault_key_hash ? `NodeVault · key ${shortHash(row.nodevault_key_hash)}` : 'NodeVault'
}

/** The whole hash, where there is room for it (a hover, the ⋯ item's reason). */
function nodeVaultKeySentence(hash: string): string {
  return `NodeVault key ${hash}`
}

function capped(value: string): string {
  return value.length <= DESCRIPTION_MENU_CAP ? value : `${value.slice(0, DESCRIPTION_MENU_CAP - 1)}…`
}

/* COPY THIS CREDENTIAL'S RUNNER PATH, SAYING WHICH OF "COPIED" AND "COULD NOT" ACTUALLY HAPPENED — without
 * the flash, success and a refused clipboard are the same nothing (`ui/CopyButton`'s `icon` face).
 * A `FeedRowSpec.titleAction`: it sits beside the name, outside the link, revealed on row hover and
 * keyboard focus, and is never drawn on touch — there the ⋯ "Copy runner path" is the route. */
export function CopyRunnerPathButton({ path }: { path: string }): JSX.Element {
  return (
    <CopyButton
      variant="icon"
      testId="credential-copy-path"
      text={() => path}
      label={`${COPY_PATH_LABEL} ${path}`}
      copiedLabel={`Copied ${path}`}
      failedLabel="Copy unavailable — this browser refused the clipboard"
    />
  )
}

/** Everything the row needs beyond the credential — plain values or callbacks, never hook results. */
export type CredentialRowDeps = {
  /** Is the description composed in at this viewport (`useMediaQuery(DESCRIPTION_QUERY)`)? `false` omits
   *  the slot ENTIRELY and moves the value into the ⋯ menu. See `DESCRIPTION_SLOT`. */
  showDescription: boolean
  /** May this viewer see the tenant (`ui/OrgIndicator.tsx::useOrgVisible`, the single home)? `false` omits
   *  the slot ENTIRELY AND drops the ⋯ org item (which names the tenant in plain text). */
  showOrg: boolean
  /** Beside an open record pane: the list is ~470px at 1280, and the owner and org slots (248px) left the name
   *  ~22px ("zz-t…"). `true` omits both slots; their ⋯ items stay, so nothing loses its home. */
  besideDetail?: boolean
  /** The Type column's width (`credentialTypeSlotWidth`); absent = the 88px floor. */
  typeWidth?: number | string
  /** A write is in flight against THIS row. */
  busy: boolean
  /** `credentials:edit` — the FALLBACK only. `Credential.editable` OUTRANKS IT wherever present: the server
   *  also admits keyless callers to rows they BROUGHT (`own_credential=True`), which a browser cannot
   *  compute (`created_by` is a display NAME). `editable: null` means "not asked", never "read-only". */
  canEdit: boolean
  /** `credentials:delete` — offers Delete on every row. Without it, Delete is still offered on the viewer's
   *  OWN row (`ownership: 'personal'` + `editable: true`, i.e. it is theirs): the server admits that
   *  DELETE keylessly (`own_credential=True`). */
  canDelete: boolean
  /** A callback, not `OverflowItem.to`: the caller's router owns navigation (`useNavigate`). */
  onEdit: (row: Credential) => void
  onDelete: (row: Credential) => void
  /** The operator's "Change org…" item (`components/ChangeOrgDialog.tsx::useChangeOrgAction`'s `menuItem`),
   *  passed in because the hook is the caller's; `null` (or absent) offers none. */
  changeOrg?: (row: Credential) => OverflowItem | null
  /** The signed-in user's email (`useAuth().admin.email`): a personal row offers its runner path only to its owner.
   *  Absent = no personal row offers one. */
  viewerEmail?: string | null
}

/** AN OWNER THAT DID NOT RESOLVE — ONE WORDING, EVERY SEAT, AND IT DIAGNOSES NOTHING.
 *  `owner` never names a deleted user: a deleted owner sets `owner_id` NULL, so the server falls back to the
 *  creator, and a creator cannot be deleted (`created_by_id` is `ondelete="RESTRICT"`; `DELETE
 *  /api/users/{id}` 409s). `owner: null` means OUTSIDE THE BOUND SCOPE, for every seat:
 *  `app/actors.py::actor_names` goes through the tenancy read seam, and `authz.feature` pins
 *  `Tenancy.ORG_SCOPED` even for a superadmin. The one true diagnosis ("outside your organization") would
 *  disclose operator-plane identity to a tenant, so the label states the absence and stops.
 *  The unresolved ⋯ item passes NO `disabledReason` (`components/tasks/rowActions.tsx` appends it to the
 *  name, which would repeat the label). THE COLUMN STAYS: a tenant-brought credential's creator does
 *  resolve (`own_credential` in `app/routes/credentials/`). */
const UNRESOLVED_OWNER_LABEL = 'Owner: not known'

/** Where the org slot is drawn (`meta`), the home's leading org segment would say the tenant twice per row and
 *  cut the name first: it hides there, and the slot's band is its one home. A home that is the org
 *  alone hides whole. Below the band the slot drops and the segment is the row's only tenant mark. */
const ORG_SEGMENT_WIDE_HIDDEN_SX = { [FEED_META]: { display: 'none' } } as const

/** The drawn home: the whole label, or with its org segment wrapped to hide where `orgSlotShown`. */
function credentialHome(path: readonly string[], home: string, orgSlotShown: boolean): ReactNode {
  if (!orgSlotShown) return home
  const [org, ...rest] = path
  return (
    <>
      <Box component="span" data-testid="credential-home-org" sx={ORG_SEGMENT_WIDE_HIDDEN_SX}>
        {`${org} ${HOME_SEPARATOR}${rest.length > 0 ? ' ' : ''}`}
      </Box>
      {rest.length > 0 && credentialHomeLabel(rest)}
    </>
  )
}

/** The drawn title: the muted home, the name, and a NodeVault row's short key hash. A row with neither home nor
 *  hash is its bare name. `orgSlotShown`: the row also draws the org slot (`credentialHome`). */
function credentialTitle(row: Credential, orgSlotShown: boolean): ReactNode {
  const home = credentialHomeLabel(row.home_path)
  const hash = row.nodevault && row.nodevault_key_hash
    ? (
      <Box
        component="span"
        data-testid="credential-nodevault-hash"
        title={nodeVaultKeySentence(row.nodevault_key_hash)}
        sx={{ ml: 1, fontFamily: 'monospace', fontSize: '0.6875rem', color: 'text.disabled' }}
      >
        {shortHash(row.nodevault_key_hash)}
      </Box>
    )
    : null
  if (!home) return hash ? <>{row.name}{hash}</> : row.name
  const path = row.home_path ?? []
  const orgOnly = orgSlotShown && path.length === 1
  return (
    <Box component="span" sx={TITLE_WITH_HOME_SX}>
      <Box
        component="span"
        data-testid="credential-home"
        title={home}
        sx={orgOnly ? [HOME_PREFIX_SX, ORG_SEGMENT_WIDE_HIDDEN_SX] : HOME_PREFIX_SX}
      >
        {credentialHome(path, home, orgSlotShown)}
      </Box>
      <Box component="span" sx={NAME_AFTER_HOME_SX}>{row.name}{hash}</Box>
    </Box>
  )
}

/** THE CREDENTIAL AS A `ui/feed` ROW. Keyed on `id`: neither `type` nor `name` is unique (N1). It always
 *  carries its stored-secret disc, which the return type promises (`FeedRowSpec.indicator` is optional). */
export function credentialFeedRow(row: Credential, deps: CredentialRowDeps): FeedRowSpec & { indicator: FeedIndicator } {
  const secretCount = row.secrets_set.length
  /* `owner`, not `created_by`: an Org Owner/Admin may make a personal row FOR a member, so its creator is not
     whose it is (`routes/credentials/credential_wire.py::CredentialOut.owner`). An older payload without the field draws the
     creator, which was its owner before the picker. */
  const owner = (row.owner === undefined ? row.created_by : row.owner) ?? null
  /* THREE ORG STATES from the wire pair: `org` resolved; `org` unresolved with `org_id` set (dangling — no
     FK, so representable); both absent. `apps/api/app/routes/credentials/credential_wire.py::CredentialOut` is the single
     home. A SHAPE CHECK, the same one `ui/OrgIndicator.tsx`'s `isOrgRef` makes, so an older API's truthy
     non-`OrgRef` never reads as resolved and draws a wrong "not bound to a tenant". */
  const orgResolved = typeof row.org?.slug === 'string' && typeof row.org?.id === 'string'
  const danglingOrgId = orgResolved ? null : (row.org_id || null)
  /** The ⋯ item draws the name; the full sentence is `orgTitle`'s, on `disabledReason`. */
  const orgLabel = orgResolved ? (row.org as { name: string }).name : 'none — not bound to a tenant'

  /* ── THE ⋯ MENU'S ONE RULE: a value not on the row, or on it but unreadable, gets a DISABLED INFO ITEM
     with its full string (R1.5's second home; a `title` is unreachable on touch).
     The owner's item is unconditional because its slot's gate is CSS this pure function cannot see; the
     description's appears only when `showDescription` is false. `rowActions.tsx` puts `disabledReason` on
     `title` and the accessible name, so the WHOLE string goes there and only the label is capped. */
  const runnerPath = credentialRunnerPath(row, deps.viewerEmail)
  const actions: OverflowItem[] = []
  if (runnerPath !== null) {
    actions.push({
      /* "COPY RUNNER PATH" LEADS AND IS UNGATED: touch's only route to the clipboard (the inline control is
         hover-revealed). The path a task names it by, in Tree mode too. None on a system row: no task sees it. */
      key: 'copy-name',
      icon: COPY_GLYPH,
      label: COPY_PATH_LABEL,
      // Leads with the drawn label (WCAG 2.5.3).
      ariaLabel: `${COPY_PATH_LABEL} ${runnerPath}`,
      // No flash in a closing menu; the inline control is where the answer lives.
      onClick: () => { copyText(() => runnerPath, () => {}) },
    })
  }
  actions.push(
    {
      /* The owner's label is capped too (display names are unbounded); the full string rides `disabledReason`. */
      key: 'owner',
      icon: '☺',
      label: owner ? `Owner: ${capped(owner)}` : UNRESOLVED_OWNER_LABEL,
      ariaLabel: owner ? `Owner: ${capped(owner)}` : UNRESOLVED_OWNER_LABEL,
      disabled: true,
      // No reason on the unresolved arm — see `UNRESOLVED_OWNER_LABEL`.
      disabledReason: owner ? `Owner: ${owner}` : undefined,
      onClick: () => {},
    },
  )
  if (!deps.showDescription && row.description) {
    actions.push({
      key: 'description',
      icon: '¶',
      label: `Description: ${capped(row.description)}`,
      // Leads with the drawn label (WCAG 2.5.3).
      ariaLabel: `Description: ${capped(row.description)}`,
      disabled: true,
      disabledReason: `Description: ${row.description}`,
      onClick: () => {},
    })
  }
  if (row.nodevault) {
    actions.push({
      key: 'nodevault',
      icon: NODEVAULT_GLYPH,
      label: nodeVaultLabel(row),
      ariaLabel: nodeVaultLabel(row),
      disabled: true,
      disabledReason: row.nodevault_key_hash ? nodeVaultKeySentence(row.nodevault_key_hash) : 'NodeVault',
      onClick: () => {},
    })
  }
  if (deps.showOrg) {
    /* THE TENANT ITEM, withheld with the slot when `deps.showOrg` is false (it names the org in plain
       text). Otherwise unconditional: slugs run to 32 chars (`apps/api/app/routes/orgs.py`) and the slot
       draws ~12, and the record page (`pages/credentials/CredentialEditPage.tsx`) shows no org field.
       `orgTitle` spells all three arms, the same string the slot carries. */
    actions.push({
      key: 'org',
      icon: '⌂',
      label: danglingOrgId ? `Org id: ${danglingOrgId}` : `Org: ${capped(orgLabel)}`,
      ariaLabel: `Org: ${capped(orgLabel)}`,
      disabled: true,
      disabledReason: orgTitle(row.org ?? null, danglingOrgId ?? undefined),
      onClick: () => {},
    })
  }
  /* Another org's row offers Edit too: the operator edits it on that org's behalf, the page sending each
     write as that org (`pages/credentials/CredentialEditPage.tsx`), and the server says so in `editable`. */
  if (row.editable ?? deps.canEdit) {
    // THE SERVER'S VERDICT FIRST — `??`, not `||`, so an explicit `false` withholds Edit.
    /* A callback, not an href item (its `target="_blank"` would full-reload an in-app route). Kept beside
       the title link because the gate differs: view-only holders must not be offered the verb. */
    actions.push(editItem(row.name, () => deps.onEdit(row)))
  }
  const changeOrg = deps.changeOrg?.(row)
  if (changeOrg) actions.push(changeOrg)
  if (deps.canDelete || (row.ownership === 'personal' && row.editable === true)) {
    actions.push(deleteItem(row.name, () => deps.onDelete(row)))
  }

  /* THE TRAILING CLUSTER in `SlotGate` order — description (composed, ≥1280 only, no gate), Last used and Last test
     (`credentialsHealthColumns`, ≥1440), owner (`meta`), org (`meta`), the folded health cell (`credentialsHealth`,
     <1440, beside the ungated tail as that union prescribes for a complement), type (ungated). Beside an open record
     pane, type alone (`besideDetail`). */
  const used = lastUsed(row)
  const tested = lastTest(row)
  const expiry = credentialExpiry(row.expires_at)
  const slots: FeedSlot[] = []
  if (deps.showDescription) {
    slots.push({
      key: 'description',
      width: DESCRIPTION_SLOT,
      align: 'left',
      testId: 'col-credential-description',
      title: row.description || 'No description',
      node: row.description || NO_VALUE,
    })
  }
  if (!deps.besideDetail) slots.push(
    /* HEALTH — Last used and Last test as columns from 1440px (`HEALTH_SLOT`). */
    {
      key: 'last-used',
      width: HEALTH_SLOT,
      gate: 'credentialsHealthColumns',
      align: 'left',
      testId: 'col-credential-last-used',
      title: used.sentence,
      node: used.text,
    },
    {
      key: 'last-test',
      width: HEALTH_SLOT,
      gate: 'credentialsHealthColumns',
      align: 'left',
      testId: 'col-credential-last-test',
      title: tested.sentence,
      node: testNode(tested.text, tested.ok),
    },
    {
      /* THE OWNER — a personal row's owner, else its creator, by display name (`CredentialOut.owner`). NEVER ACCESS
         (access is the credential's Access tab, N7). Unresolved → `NO_VALUE` with words. */
      key: 'owner',
      width: OWNER_SLOT,
      gate: 'meta',
      align: 'left',
      testId: 'col-credential-owner',
      title: owner ? `Owner: ${owner}` : UNRESOLVED_OWNER_LABEL,
      node: owner || NO_VALUE,
    },
    /* WHICH TENANT OWNS THE ROW — composed from `ui/OrgIndicator.tsx`'s `orgSlot` (token, `title`,
       `data-org-*`, blank and dangling-id arms). `unresolvedId` only where the pair says it names no row.
       Draws the name, the slug on `title` (`ui/OrgIndicator.tsx`). */
    ...orgSlots(deps.showOrg, row.org ?? null, {
      width: ORG_SLOT_CREDENTIALS,
      gate: 'meta',
      unresolvedId: danglingOrgId || undefined,
    }),
    /* Both health facts in one cell below 1440px, the verdict as its mark — or, on a row that expires soon or has,
       the expiry badge's words, which the name's marker drops there (a long name left ~5px at 448 beside them). */
    {
      key: 'health',
      width: HEALTH_FOLDED_SLOT,
      gate: 'credentialsHealth',
      align: 'left',
      testId: 'col-credential-health',
      title: [expiry?.text, used.sentence, tested.sentence].filter(Boolean).join(' · '),
      node: expiry
        ? <Box component="span" sx={expiry.expired ? EXPIRY_TONE_SX.expired : EXPIRY_TONE_SX.soon}>{expiry.text}</Box>
        : (
          <>
            {used.text}
            {tested.ok !== null && (
              <>
                {' '}
                <Box component="span" role="img" aria-label={tested.ok ? 'last test OK' : 'last test failed'}>
                  {testNode(tested.ok ? '✓' : '✕', tested.ok)}
                </Box>
              </>
            )}
          </>
        ),
    },
  )
  slots.push(
    {
      /* The registry's human label, never the raw slug. Off a list under ~400px (`credentialsType`), where the
         row's note carries it on hover (`notes` below). */
      key: 'type',
      gate: 'credentialsType',
      width: deps.typeWidth ?? TYPE_SLOT,
      // A phone row that still draws it (a list ≥400px) has no title to spare: the 88px the cluster table prices.
      phoneWidth: TYPE_SLOT,
      align: 'right',
      testId: 'col-credential-type',
      title: `Type: ${row.type_label}`,
      node: credentialTypeLabel(row.type_label, deps.besideDetail),
    },
  )

  /* THE EXPIRY BADGE hugs the name in its tone (`FeedMarker.word`): an expired credential is the one fact the operator
     must not have to hover for. Below 1440px the folded health cell carries its words and the name keeps the mark. A
     row that expires later, or never, carries none. */
  const markers: FeedMarker[] = []
  if (row.nodevault) {
    markers.push({ key: 'nodevault', glyph: NODEVAULT_GLYPH, label: nodeVaultLabel(row), testId: 'credential-nodevault-mark' })
  }
  if (expiry) {
    markers.push({
      key: 'expiry',
      glyph: '⏱',
      label: expiry.text,
      title: `${expiry.text} (${absoluteTime(row.expires_at)})`,
      hug: true,
      tone: expiry.expired ? 'error' : 'warning',
      // The words only where the health columns stand; below, the folded cell says them.
      word: { text: expiry.text, gate: 'credentialsHealthColumns' },
      testId: 'credential-expiry',
    })
  }

  return {
    key: row.id,
    indicator: {
      /* Whether the credential holds its secrets (`secrets_set`, key names only). A COUNT, not a verdict:
         which keys are secret lives in `apps/api/app/credentials/registry.py`, so "none stored" may be fine.
         `● / ○` = provisioned / declared-only (as `/nodes`, `/users`). */
      tone: secretCount > 0 ? 'success' : 'neutral',
      glyph: secretCount > 0 ? '●' : '○',
      label: secretCount > 0
        ? countOf(secretCount, 'stored secret')
        : 'no stored secret',
      data: { secrets: secretCount },
    },
    markers: markers.length > 0 ? markers : undefined,
    /* The home (`home_path`) leads the name, muted, so an operator viewing every org tells the operator's
       system rows from a tenant's system, project and personal ones at a glance; it truncates first, never the
       name. A NodeVault row's short key hash follows the name, dim and mono, so the roster SHOWS which key
       seals it (AC-5.5); the clamp cuts it before the name. Tree mode redraws the title as the last name
       segment under a section per home, so there the mark's hover and the ⋯ item carry the hash. */
    // The org slot is drawn when the viewer may see tenants and no record pane has taken its width.
    title: credentialTitle(row, deps.showOrg && !deps.besideDetail && orgResolved),
    titleText: row.name,
    /* The title is one line, ellipsised, so every row keeps one height; the full name is on hover
       (`titleText`) and its runner path in the ⋯ menu's Copy runner path.
       THE TYPE'S SECOND HOME: the hover, whole, for a list under ~400px (a phone, or beside an open credential)
       where the slot leaves the cluster. */
    notes: [
      { key: 'type', tone: 'muted', text: `Type: ${row.type_label}` },
      // The health's second home: the hover, whole, where its cells leave (beside an open record pane).
      { key: 'health', tone: 'muted', text: `${used.sentence} · ${tested.sentence}` },
    ],
    // The FULL runner path, whatever the title draws (Tree mode draws the last segment). None on a system row.
    titleAction: runnerPath !== null ? <CopyRunnerPathButton path={runnerPath} /> : undefined,
    href: `/credentials/${row.id}`,
    busy: deps.busy,
    slots,
    actions,
  }
}
