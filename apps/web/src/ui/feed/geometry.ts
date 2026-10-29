/* The feed row's geometry. Band boundaries (`PHONE`, `FEED_NO_META`, `FEED_NO_ID`, `TAP_FLOOR_DOWN`) are
 * single-homed in `ui/listStyles.ts`; a measured breakpoint moves in its own change with browser evidence. */
import type { Theme } from '@mui/material/styles'
import {
  ABOVE_PHONE,
  ACCOUNTS_NO_ENABLED,
  ACCOUNTS_NO_ORG,
  ACCOUNTS_NO_SESSIONS,
  ACCOUNTS_NO_WEEK,
  ACCOUNTS_ORG_NARROW,
  ACCOUNTS_PROVIDER_UNDER,
  ARTIFACTS_NO_CURRENCY,
  ARTIFACTS_NO_KIND,
  AUDIT_NO_ACTION,
  AUDIT_NO_ACTOR,
  AUDIT_NO_ORG,
  AUTOMATIONS_NO_META,
  TEMPLATES_NO_META,
  BOARD_NO_AUTHOR,
  BOARD_NO_COST,
  BOARD_NO_ORG,
  BOARD_NO_PERSONA,
  BOARD_NO_PR,
  BOARD_NO_RATING,
  BOARD_NO_TOKENS,
  CREDENTIALS_TYPE_UNDER,
  CREDENTIALS_HEALTH_COLUMNS,
  CREDENTIALS_HEALTH_FOLDED,
  FEED_NO_ID,
  GROUPS_NO_BINDINGS,
  GROUPS_NO_BINDINGS_WITH_ORG,
  GROUPS_NO_DESCRIPTION,
  FEED_NO_META,
  LANE_NO_ID,
  NODES_NO_COUNTS,
  NODES_NO_ENABLED,
  NODES_NO_HOSTING,
  NODES_NO_LOAD,
  NODES_NO_ORG,
  NODES_NO_OS,
  NODES_NO_VERSION,
  ORG_HIDDEN_ON_MOBILE_SX,
  PEOPLE_ADMIN_NARROW,
  PEOPLE_NO_GROUPS,
  PEOPLE_NO_NAME,
  PERSONAS_NO_FOCUS,
  PERSONAS_NO_LEAD,
  PHONE,
  POOL_NO_ID,
  POOL_NO_META,
  POOL_NO_NODE,
  POOL_NO_PROJECT,
  PROJECTS_NO_ORG,
  RUNS_NO_ID,
  RUNS_NO_WORKFLOW,
  FEED_NO_AUDIT_ORG,
  FEED_NO_CONNECTION_IMAGE,
  SENDS_NO_SOURCE,
  SENDS_NO_STATUS,
  SIDEBAR_SQUEEZE_SX,
} from '../listStyles'

/* Desktop band. `ROW_H` 30px ≈ 20 rows on a laptop screen. `RAIL_X` puts a depth-0 disc 2px off the row's
 * left edge (spacing cut to the minimum). Its focus/marked rings reach 3px out, so they overhang the
 * row by 1px — inside a host's own inset (the /tasks list's scrollport pads 4px), never clipped by the row itself. */
export const RAIL_X = 11
export const NODE = 18
export const ROW_H = 30
/* One step: the feed's expansion is FLAT, one depth below a root. */
export const CHILD_INDENT = 18
/* From the rail, not the edge; the phone band re-prices it and both must move together. 13 leaves 4px between
 * a depth-0 disc (right edge `RAIL_X + NODE / 2` = 20) and the first cell, the lead. */
export const ROW_PAD_L = 13

/* Phone band (owner decision: denser on mobile). Rail, disc and indent move with the row: all are
 * measured off the same left edge. */
export const RAIL_X_PHONE = 11
export const NODE_PHONE = 15
export const ROW_H_PHONE = 24
export const CHILD_INDENT_PHONE = 10
export const ROW_PAD_L_PHONE = 11

/* THE PHONE NARROWS THE INDENT PAST THREE STEPS: a tree (the Credentials roster's) can nest deeper, and at
 * 390px every step comes out of the title. Three full steps, not two: at two a child landed on its parent's own
 * column and stopped reading as nested; at three, measured at 390px, a phase title keeps ~224px (~37 characters).
 * Deeper levels still step, by `CHILD_INDENT_PHONE_DEEP`: a flat cap drew depth 4 on its parent's column, so
 * child and sibling read the same. Depth 6 costs 48px. A flat list never reaches the narrow steps. */
export const PHONE_MAX_INDENT_STEPS = 3
export const CHILD_INDENT_PHONE_DEEP = 6
const phoneIndent = (depth: number) =>
  Math.min(depth, PHONE_MAX_INDENT_STEPS) * CHILD_INDENT_PHONE +
  Math.max(0, depth - PHONE_MAX_INDENT_STEPS) * CHILD_INDENT_PHONE_DEEP

export const rowPadLeft = (depth: number) => ({
  pl: `${RAIL_X + ROW_PAD_L + depth * CHILD_INDENT}px`,
  [PHONE]: { pl: `${RAIL_X_PHONE + ROW_PAD_L_PHONE + phoneIndent(depth)}px` },
})

/* Depth 0: disc centred on the rail to mask the thread, so priced off rail x and disc diameter. Depth 1
 * steps clear of the rail so the child reads as a branch. */
export const nodeLeft = (depth: number) => ({
  left: `${RAIL_X - NODE / 2 + depth * CHILD_INDENT}px`,
  [PHONE]: { left: `${RAIL_X_PHONE - NODE_PHONE / 2 + phoneIndent(depth)}px` },
})

/* Fixed slots, reserved on every row even when empty, so values line up down the list. */
export const AGE_SLOT = 28
export const ACTIONS_SLOT = 30
export const DISCLOSURE_SLOT = 18
/* The 44px tap floor: `theme.ts` raises every `MuiIconButton` to 44x44 below `TAP_FLOOR_DOWN` via a
 * media query in the component's class, so a plain `sx` size loses. A pullback must name BOTH axes,
 * reach that class, and cover the whole band — `[PHONE]` (≤480) alone leaves 481-599.95 at 44, and
 * `[PHONE]` must ride LAST (at a fractional 480.5px a `(min-width:481px)` band does not match).
 * This constant is the pullback floor: WCAG 2.2 AA 2.5.8's 24x24. */
export const DISCLOSURE_TOUCH_MIN = 24

/** Gate a slot on the band that can afford it: `meta` drops at ≤899px, `id` at ≤767px, `phone` at ≤480px,
 *  `squeeze` in the MIDDLE band (>480px and ≤1023px), `wide` at ≤1023px (phone band included: no org name on mobile),
 *  absent = never. Order window-gated slots meta, id, phone, ungated, so they fall off the LEFT and survivors stay flush right.
 *  `wide`/`squeeze`, `meta` and `id` also drop by the LIST's own width, in that order (`./fit`): a narrow column in a
 *  wide window drops them rather than clip the row.
 *  `boardPersona` / `boardTokens` / `boardCost` / `boardRating` follow the /tasks BOARD's width, not the window
 *  (`ui/listStyles.ts`'s `BOARD_NO_*`, container queries — inert where no board container encloses the row); they drop in the order
 *  `components/tasks/listViews/feedRow.tsx`'s trailing-cluster comment gives, not by position.
 *  `nodes*` / `host*` / `group*` follow the /nodes roster's width the same way (`NODES_NO_*`), on top of
 *  their window gates — by priority, not position: the rightmost Enabled drops before load and version, the
 *  same width on every row, so columns still line up.
 *  `people*` / `projectsOrg` follow Directory's People and Projects lists the same way (`PEOPLE_NO_*`, `PROJECTS_NO_ORG`);
 *  `groups*` Directory's Groups list (`GROUPS_NO_*`; not /nodes' `group*`);
 *  `peopleAdmin` narrows instead of dropping (the toggle has no ⋯ fallback); `credentialsType` Secrets' Type
 *  (`CREDENTIALS_TYPE_UNDER`, on the name's hover there); `credentialsHealthColumns` / `credentialsHealth` Secrets'
 *  health as two columns or one folded cell (`CREDENTIALS_HEALTH_*`, complements: place the folded one beside the tail); `acct*` Agents › Agents' roster (`ACCOUNTS_*`);
 *  `artifact*` the task Artifacts list (`ARTIFACTS_NO_*`); `laneId` the merge lane's Runs (`LANE_NO_ID`);
 *  `personas*` Audit › Personas' roster (`PERSONAS_NO_*`); `pool*` Automation › Slots (`POOL_NO_*`);
 *  `automationsMeta` the Automations roster (`AUTOMATIONS_NO_META`); `templatesMeta` the template manage list (`TEMPLATES_NO_META`); `audit*` Audit › Activity (`AUDIT_NO_*`);
 *  `sends*` a Gmail connection's Sent list (`SENDS_NO_*`). */
export type SlotGate =
  | 'meta'
  | 'id'
  | 'phone'
  | 'squeeze'
  | 'wide'
  | 'boardPr'
  | 'boardPersona'
  | 'boardTokens'
  | 'boardCost'
  | 'boardRating'
  | 'boardOrg'
  | 'boardAuthor'
  | 'boardAge'
  | 'boardId'
  | 'nodesOs'
  | 'nodesCounts'
  | 'nodesOrg'
  | 'nodesHosting'
  | 'nodesEnabled'
  | 'nodesLoad'
  | 'nodesVersion'
  | 'hostOrigin'
  | 'hostState'
  | 'hostNodes'
  | 'groupShapes'
  | 'groupAccount'
  | 'groupOrg'
  | 'runsId'
  | 'runsWorkflow'
  | 'auditOrg'
  | 'auditAction'
  | 'auditActor'
  | 'connectionImage'
  | 'peopleName'
  | 'peopleGroups'
  | 'peopleAdmin'
  | 'peopleSelect'
  | 'peopleSelectIdle'
  | 'groupsDescription'
  | 'groupsBindings'
  | 'groupsBindingsWithOrg'
  | 'projectsOrg'
  | 'credentialsType'
  | 'credentialsHealthColumns'
  | 'credentialsHealth'
  | 'acctSessions'
  | 'acctWeek'
  | 'acctOrg'
  | 'acctProvider'
  | 'acctEnabled'
  | 'artifactKind'
  | 'artifactCurrency'
  | 'laneId'
  | 'personasFocus'
  | 'personasLead'
  | 'poolNode'
  | 'poolProject'
  | 'poolMeta'
  | 'poolId'
  | 'automationsMeta'
  | 'templatesMeta'
  | 'sendsStatus'
  | 'sendsSource'

/* `squeeze` cannot be ordered monotonically (present at the narrowest band, absent in the middle): place
 * it immediately left of the ungated tail. Why the middle band is poor: `ui/listStyles.ts`'s
 * `SIDEBAR_SQUEEZE_SX`.
 * `wide` IS monotonic (hidden below its floor, like `meta`/`id`, just at the 1023px floor instead) —
 * `ORG_HIDDEN_ON_MOBILE_SX` is `SIDEBAR_SQUEEZE_SX` with its phone reveal removed (the org slot must
 * not show on mobile at all).
 * A union, not `unknown`: `squeeze` is two nested media queries (its lower bound is the negation of
 * `PHONE`), and `unknown` would drop the type guarantee on `meta`/`id`. */
type GateRule = { display: 'none' } | { width: number }
export const GATE_SX: Record<SlotGate, Record<string, GateRule | Record<string, GateRule>>> = {
  meta: { [FEED_NO_META]: { display: 'none' } },
  id: { [FEED_NO_ID]: { display: 'none' } },
  /* Off the phone band only: for a slot whose fact the route moves into the title there (the merge lane's runs). */
  phone: { [PHONE]: { display: 'none' } },
  squeeze: SIDEBAR_SQUEEZE_SX,
  wide: ORG_HIDDEN_ON_MOBILE_SX,
  boardPr: { [BOARD_NO_PR]: { display: 'none' } },
  boardPersona: { [BOARD_NO_PERSONA]: { display: 'none' } },
  boardTokens: { [BOARD_NO_TOKENS]: { display: 'none' } },
  boardCost: { [BOARD_NO_COST]: { display: 'none' } },
  boardRating: { [BOARD_NO_RATING]: { display: 'none' } },
  /* `wide` and `id` respectively, plus the board's own last drops (`ui/listStyles.ts`'s `BOARD_NO_ORG` /
     `BOARD_NO_AUTHOR`). */
  boardOrg: { ...ORG_HIDDEN_ON_MOBILE_SX, [BOARD_NO_ORG]: { display: 'none' } },
  /* The author is `id`'s band, phone included, and the narrow board: on a phone its 24px went to the
     title, which cut off near 30 characters; the ⋯ menu and the row's name still carry the creator. */
  boardAuthor: { [FEED_NO_ID]: { display: 'none' }, [BOARD_NO_AUTHOR]: { display: 'none' } },
  /* The age leaves WITH the author, the provenance pair: at a 1024px window the split sits at its pane floor
     and the board at ~350px, where the 28px age cost the title four characters. Above the phone only: the phone keeps its one
     time cue. The task page's Subtasks list (its own board container) follows the same rule in a split. A pickable
     row drops this gate while a pick is in progress (`feedRow.tsx`), since the bulk tick rides the age slot. */
  boardAge: { [ABOVE_PHONE]: { [BOARD_NO_AUTHOR]: { display: 'none' } } },
  /* The task id lead leaves with them too: beside an open task at a 1280px window the board is ~388px and its
     34px went to a title that wraps to two lines there (`titleWrapAt`). The id stays in the row's link (its URL), its
     accessible name, and the open task's trail — as on the phone, where `id` already drops it. */
  boardId: { [FEED_NO_ID]: { display: 'none' }, [BOARD_NO_AUTHOR]: { display: 'none' } },
  /* /nodes: each slot's window gate plus the roster's own width (`ui/listStyles.ts`'s `NODES_NO_*`, drop
     order OS → counts → org → hosting → enabled → version → load). Load has no window gate: the phone keeps it. */
  nodesOs: { ...ORG_HIDDEN_ON_MOBILE_SX, [NODES_NO_OS]: { display: 'none' } },
  nodesCounts: { [FEED_NO_META]: { display: 'none' }, [NODES_NO_COUNTS]: { display: 'none' } },
  nodesOrg: { ...ORG_HIDDEN_ON_MOBILE_SX, [NODES_NO_ORG]: { display: 'none' } },
  nodesHosting: { ...ORG_HIDDEN_ON_MOBILE_SX, [NODES_NO_HOSTING]: { display: 'none' } },
  nodesEnabled: { [FEED_NO_ID]: { display: 'none' }, [NODES_NO_ENABLED]: { display: 'none' } },
  nodesLoad: { [NODES_NO_LOAD]: { display: 'none' } },
  nodesVersion: { [FEED_NO_ID]: { display: 'none' }, [NODES_NO_VERSION]: { display: 'none' } },
  /* /nodes › Hosts, the same container: origin, then org (`nodesOrg`), node count, state, version
     (`nodesVersion`), load (`nodesLoad`). The count goes before state, so the ungated age costs the
     name nothing beside an open host; the host's page carries the count. */
  hostOrigin: { ...ORG_HIDDEN_ON_MOBILE_SX, [NODES_NO_COUNTS]: { display: 'none' } },
  hostState: { [FEED_NO_META]: { display: 'none' }, [NODES_NO_ENABLED]: { display: 'none' } },
  hostNodes: { [NODES_NO_HOSTING]: { display: 'none' } },
  /* /nodes › Groups, the same container: the shapes go first, then the AWS account, then the org —
     each `meta` by window, as before. */
  groupShapes: { [FEED_NO_META]: { display: 'none' }, [NODES_NO_OS]: { display: 'none' } },
  groupAccount: { [FEED_NO_META]: { display: 'none' }, [NODES_NO_ORG]: { display: 'none' } },
  groupOrg: { [FEED_NO_META]: { display: 'none' }, [NODES_NO_HOSTING]: { display: 'none' } },
  /* The workflow-run ledger (`WORKFLOW_RUNS_CONTAINER`): `meta` by window, plus the ledger's own width. */
  runsId: { [FEED_NO_META]: { display: 'none' }, [RUNS_NO_ID]: { display: 'none' } },
  runsWorkflow: { [FEED_NO_META]: { display: 'none' }, [RUNS_NO_WORKFLOW]: { display: 'none' } },
  /* Audit › Activity (`ui/listStyles.ts`'s `FEED_NO_AUDIT_ORG`, `AUDIT_NO_*`): the operator's org first, then Action
     (`meta` by window), then Actor (`id` by window) — each also by the list's own width, beside an open event. */
  auditOrg: { [FEED_NO_AUDIT_ORG]: { display: 'none' }, [AUDIT_NO_ORG]: { display: 'none' } },
  auditAction: { [FEED_NO_META]: { display: 'none' }, [AUDIT_NO_ACTION]: { display: 'none' } },
  auditActor: { [FEED_NO_ID]: { display: 'none' }, [AUDIT_NO_ACTOR]: { display: 'none' } },
  /* Networks › Connections' image (`ui/listStyles.ts`'s `FEED_NO_CONNECTION_IMAGE`): the first to drop. */
  connectionImage: { [FEED_NO_CONNECTION_IMAGE]: { display: 'none' } },
  /* Directory › People (`ui/listStyles.ts`'s `PEOPLE_NO_*`): the name pair, then the groups count — each on
     its window gate too. */
  peopleName: { [FEED_NO_META]: { display: 'none' }, [PEOPLE_NO_NAME]: { display: 'none' } },
  peopleGroups: { [FEED_NO_ID]: { display: 'none' }, [PEOPLE_NO_GROUPS]: { display: 'none' } },
  /* Its Admin toggle, narrowed (not dropped) on the narrow roster (`PEOPLE_ADMIN_NARROW`), so the email keeps the width. */
  peopleAdmin: { [PEOPLE_NO_GROUPS]: { width: PEOPLE_ADMIN_NARROW } },
  /* Its Select tick, off the narrow roster above the phone (beside an open user): the phone keeps its bulk verbs. */
  peopleSelect: { [ABOVE_PHONE]: { [PEOPLE_NO_GROUPS]: { display: 'none' } } },
  /* …and off the phone until a pick starts: its 26px went to the address; the ⋯ menu's Select is the way in,
     after which `peopleSelect` shows every row's tick. */
  peopleSelectIdle: { [PHONE]: { display: 'none' }, [ABOVE_PHONE]: { [PEOPLE_NO_GROUPS]: { display: 'none' } } },
  /* Directory › Groups (`ui/listStyles.ts`'s `GROUPS_NO_*`): the description, then the binding count — each on its
     window gate too. */
  groupsDescription: { [FEED_NO_META]: { display: 'none' }, [GROUPS_NO_DESCRIPTION]: { display: 'none' } },
  groupsBindings: { [FEED_NO_ID]: { display: 'none' }, [GROUPS_NO_BINDINGS]: { display: 'none' } },
  /* …sooner beside the Org column (All orgs). */
  groupsBindingsWithOrg: { [FEED_NO_ID]: { display: 'none' }, [GROUPS_NO_BINDINGS_WITH_ORG]: { display: 'none' } },
  /* Directory › Projects' org: `squeeze` by window, and off a narrow list ABOVE the phone band (beside an open
     project), where `pages/Projects.tsx` draws it in the title after the name instead. The phone keeps its one-line row. */
  projectsOrg: { [ABOVE_PHONE]: { ...SIDEBAR_SQUEEZE_SX[ABOVE_PHONE], [PROJECTS_NO_ORG]: { display: 'none' } } },
  /* Directory › Secrets' Type, off a list narrower than ~400px, where `lib/credentialRow.tsx` puts it on the name's hover. */
  credentialsType: { [CREDENTIALS_TYPE_UNDER]: { display: 'none' } },
  /* Secrets' Last used and Last test columns, from 1440px; below it, the one folded cell (its complement) stands in. */
  credentialsHealthColumns: { [CREDENTIALS_HEALTH_FOLDED]: { display: 'none' } },
  credentialsHealth: { [CREDENTIALS_HEALTH_COLUMNS]: { display: 'none' } },
  /* Agents › Agents (`ui/listStyles.ts`'s `ACCOUNTS_*`): each on its window gate too. The provider is off the
     phone and a narrow roster, where `pages/credentials/Accounts.tsx` puts it on the name's hover. */
  acctSessions: { [FEED_NO_META]: { display: 'none' }, [ACCOUNTS_NO_SESSIONS]: { display: 'none' } },
  acctWeek: { [FEED_NO_ID]: { display: 'none' }, [ACCOUNTS_NO_WEEK]: { display: 'none' } },
  /* The org narrows where 7d drops (`ACCOUNTS_ORG_NARROW`), then drops itself. */
  acctOrg: {
    [FEED_NO_META]: { display: 'none' },
    [ACCOUNTS_NO_WEEK]: { width: ACCOUNTS_ORG_NARROW },
    [ACCOUNTS_NO_ORG]: { display: 'none' },
  },
  acctProvider: { [PHONE]: { display: 'none' }, [ACCOUNTS_PROVIDER_UNDER]: { display: 'none' } },
  acctEnabled: { [FEED_NO_ID]: { display: 'none' }, [ACCOUNTS_NO_ENABLED]: { display: 'none' } },
  /* The task Artifacts list (`ui/listStyles.ts`'s `ARTIFACTS_NO_*`): `meta` by window, plus the list's own width —
     the Kind first, then the currency. */
  artifactKind: { [FEED_NO_META]: { display: 'none' }, [ARTIFACTS_NO_KIND]: { display: 'none' } },
  artifactCurrency: { [FEED_NO_META]: { display: 'none' }, [ARTIFACTS_NO_CURRENCY]: { display: 'none' } },
  /* The merge lane's Runs' lead id (`FeedRowSpec.lead.gate`): the window's `id` band, plus a narrow list (`LANE_NO_ID`). */
  laneId: { [FEED_NO_ID]: { display: 'none' }, [LANE_NO_ID]: { display: 'none' } },
  /* Audit › Personas (`ui/listStyles.ts`'s `PERSONAS_NO_*`): the focus, then the lead name and the model — each on its
     window gate too. */
  personasFocus: { [FEED_NO_META]: { display: 'none' }, [PERSONAS_NO_FOCUS]: { display: 'none' } },
  personasLead: { [FEED_NO_ID]: { display: 'none' }, [PERSONAS_NO_LEAD]: { display: 'none' } },
  /* Automation › Slots (`ui/listStyles.ts`'s `POOL_NO_*`): node, project, the git/stack pair, then the session —
     each on its window gate too (`meta`, or `id` for the session). */
  poolNode: { [FEED_NO_META]: { display: 'none' }, [POOL_NO_NODE]: { display: 'none' } },
  poolProject: { [FEED_NO_META]: { display: 'none' }, [POOL_NO_PROJECT]: { display: 'none' } },
  poolMeta: { [FEED_NO_META]: { display: 'none' }, [POOL_NO_META]: { display: 'none' } },
  poolId: { [FEED_NO_ID]: { display: 'none' }, [POOL_NO_ID]: { display: 'none' } },
  /* The Automations roster's Target · Action and When (`ui/listStyles.ts`'s `AUTOMATIONS_NO_META`): `meta` by window,
     plus a narrow list (beside an open automation), where `pages/automations/AutomationList.tsx` puts them on the name's hover. */
  automationsMeta: { [FEED_NO_META]: { display: 'none' }, [AUTOMATIONS_NO_META]: { display: 'none' } },
  /* The template manage list's Purpose and Origin (`ui/listStyles.ts`'s `TEMPLATES_NO_META`): `meta` by window, plus a
     narrow list (beside an open template). */
  templatesMeta: { [FEED_NO_META]: { display: 'none' }, [TEMPLATES_NO_META]: { display: 'none' } },
  /* A Gmail connection's Sent list (`ui/listStyles.ts`'s `SENDS_NO_*`): the status chip, then the source — each on its
     window gate too, and both off a narrow list (beside an open task or run). */
  sendsStatus: { [FEED_NO_META]: { display: 'none' }, [SENDS_NO_STATUS]: { display: 'none' } },
  sendsSource: { [PHONE]: { display: 'none' }, [SENDS_NO_SOURCE]: { display: 'none' } },
}

/* A COLUMN SIZED TO ITS WIDEST VALUE: a priced slot ellipsised "acme, acme-ops, org-a-pro…" at 1440px
 * beside a title cell with hundreds of pixels to spare. Measured at the slot's default face (0.6875rem on the
 * page's own font); never below `floor` (the slot's priced width) nor above `share` percent of the LIST's width
 * (`cqw`, so the host sets `containerType: 'inline-size'` on the list: a detail pane or docked console narrowing
 * the list narrows the cap, and the floor wins wherever the list is narrow). Every row gets the one width, so the
 * column still lines up. jsdom measures nothing: the floor alone. A `mono` slot passes its `family`
 * (`monoFamily(theme)`): measured on the page font, a monospace id comes out short and still ellipsises. */
let measureCanvas: HTMLCanvasElement | null = null
function widestText(texts: readonly string[], family?: string): number {
  // A body with no layout box (jsdom) has nothing to measure against; asking its canvas only logs "not implemented".
  if (texts.length === 0 || typeof document === 'undefined' || !document.body?.clientWidth) return 0
  try {
    measureCanvas ??= document.createElement('canvas')
    const ctx = measureCanvas.getContext('2d')
    if (!ctx) return 0
    const rootPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16
    ctx.font = `400 ${rootPx * 0.6875}px ${family ?? getComputedStyle(document.body).fontFamily}`
    return Math.max(...texts.map(text => ctx.measureText(text).width))
  } catch {
    return 0
  }
}
/* 2px of slack: canvas widths are fractional and the cell's box rounds. */
const MEASURE_SLACK = 2

export function contentSlotWidth(texts: readonly string[], floor: number, share: number, family?: string): number | string {
  const widest = Math.ceil(widestText(texts, family)) + MEASURE_SLACK
  if (widest <= floor) return floor
  return `min(${widest}px, max(${floor}px, ${share}cqw))`
}

/* `?.`: `theme.typography.mono` is this app's token, absent under MUI's stock theme (tests without the
 * app provider), where reading `.fontFamily` threw. */
export const monoFamily = (theme: Theme) => theme.typography.mono?.fontFamily ?? 'monospace'
