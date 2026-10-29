import type { Theme } from '@mui/material/styles'
import type { SidebarColors } from '../theme'

/* The console's ONE list-page style vocabulary. A page imports these; it never re-spells them.
 *
 * Anything that should reach EVERY table without opt-in (header caption, row hover, density) is a
 * `styleOverrides` entry in `theme.ts`. What lives HERE depends on the page's own columns:
 * which cell leads the phone card, where the gutter is reclaimed, which string truncates.
 *
 * Values, not components: `sx` accepts them directly or spread; the theme-reading ones are callbacks. */

/* THE PHONE BREAKPOINT: 480px, not MUI's `sm` (600px). It is the width the mobile e2e suite samples
 * around (320/360/390/479 engaged, 481 disengaged). A media-query STRING rather than
 * `theme.breakpoints.down(...)` because half the call sites have no theme in scope. */
export const PHONE = '@media (max-width:480px)'

/** The same query for `useMediaQuery`, so a page that must branch in JS reads the identical width. */
export const PHONE_QUERY = '(max-width:480px)'

/** The phone's fixed bottom navigation bar (`components/Sidebar.tsx`): what page content must keep clear of. */
export const BOTTOM_NAV_HEIGHT = 60

/** Marks a page exactly its scrollport tall that scrolls inside itself (`ui/detail/DetailPage`'s `fill`
 *  measure, Settings' pinned chrome): its scrollport keeps no Help-launcher clearance under it
 *  (`ui/launcherSeat.ts`'s `LAUNCHER_CLEARANCE_SX`), which would only overflow it. */
export const SHELL_FILL_ATTR = 'data-shell-fill'
/** Marks the one section of such a page that absorbs its height (`ui/detail/DetailPage`'s `DetailSection fill`): a tab
 *  panel holding none overflows the page instead, so the launcher's clearance follows that panel (`LAUNCHER_CLEARANCE_SX`). */
export const SECTION_FILL_ATTR = 'data-section-fill'

/* THE ACTIVITY FEED'S TWO COLUMN GATES. The row is a title plus FIXED-WIDTH
   slots, so whatever the cluster costs comes out of the title: gated only at `PHONE`, the title went
   331px at 480 -> ZERO at 481 with the ⋯ off-viewport (a shredded title; a docked Web Console leaves
   the board at ~490px). Each stage is priced against a 240px title floor. Shared terms: 246 shell + 34
   row inset + 48 row gaps + 6 id/title gap + 240 floor = 574.
     · FULL SET adds disclosure 18 + id 40 + persona 78 + author 24 + age 28 + actions 30 = 792, plus
       rating 36 + 8 gap = 836, plus cost 64 + 8 gap = 908 -> 899 KEPT ON MEASUREMENT: at 900 the title
       holds 258.5px against the 240 floor, one line at every width, scrollWidth - clientWidth 0.
       THE MARGIN IS NOW MEASURED ONLY: a further slot must be bisected in a browser on a row carrying
       every slot, reading row HEIGHT.
     · MINUS persona (78) -> 714 -> 768 KEPT.
     · Below 768: state, title, age, ⋯ plus the epic disclosure (`ui/feed/FeedList.tsx`'s
       `DISCLOSURE_SX`; its title cost is single-homed in `e2e/tasks-feed.spec.ts`'s `TITLE_FLOOR_PX`).
       `PHONE` is a strict SUBSET of that band.
   Both boundaries keep their measured value; the sums are conservative (the sidebar is 40px narrower
   than they assume). SPELLED AS max-width `sx` HIDE GATES, matching the `[PHONE]: { display: 'none' }`
   idiom, because these slots reserve width and nothing asserts their DOM absence. */
/* `FEED_NO_META`: the name is kept because two list surfaces and one e2e spec read it by name. The /tasks
   activity row no longer rides it — its persona, tokens, rating and cost follow the board (`BOARD_NO_*` below). */
export const FEED_NO_META = '@media (max-width:899px)'
/** `FEED_NO_META`'s complement: where a `meta` slot IS drawn, so a value it carries can leave the title. */
export const FEED_META = '@media (min-width:900px)'
export const FEED_NO_ID = '@media (max-width:767px)'

/* SECRETS' HEALTH COLUMNS — Last used and Last test as two 64px columns from 1440px up. Measured with a
   long name (`e2e/credentials-health.spec.ts`): the pair (140px with gaps) left it 184px at 1024 and 194px at 1280
   (beside the description) against its 200 floor; below 1440 both fold into one 72px cell (`lib/credentialRow.tsx`'s
   `HEALTH_FOLDED_SLOT`). */
export const CREDENTIALS_HEALTH_FOLDED = '@media (max-width:1439px)'
export const CREDENTIALS_HEALTH_COLUMNS = '@media (min-width:1440px)'

/* AUDIT › ACTIVITY'S OPERATOR ORG SLOT (130px + 8px gap) — present from 1366px up. Priced against the
   sentence: at 1280x900 the title box measured 452px with three slots (`e2e/audit-users.spec.ts`'s
   `DESKTOP_SUMMARY_BOX_FLOOR`, 400), so the org slot would have taken it to ~314px there; from 1366 the
   86px more viewport leaves ~400. Below it the org rides the actor slot's `title`, so it is never lost. */
export const FEED_NO_AUDIT_ORG = '@media (max-width:1365px)'

/* AUDIT › ACTIVITY'S OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason below, on `pages/audit/UsersTab.tsx`:
   beside an open event the list narrows (~340px at a 1280px window) while the window gates kept Action and Actor,
   and the sentence went to nothing. Priced on a ~260px sentence beside the fixed disc, age and ⋯ (~120px): the org
   (130) drops first, then Action (150, the sentence restates it), then Actor (170, the open event's pane names it).
   Inert elsewhere. */
export const AUDIT_LIST_CONTAINER = 'audit-list'
export const AUDIT_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: AUDIT_LIST_CONTAINER } as const
export const AUDIT_NO_ORG = `@container ${AUDIT_LIST_CONTAINER} (max-width:829.95px)`
export const AUDIT_NO_ACTION = `@container ${AUDIT_LIST_CONTAINER} (max-width:699.95px)`
export const AUDIT_NO_ACTOR = `@container ${AUDIT_LIST_CONTAINER} (max-width:549.95px)`
/* The filter strip's fold, as Automations' (`AUTOMATIONS_FOLD_FILTERS`): Search (170px floor), Type
   (~190) and the From/To pair (2×196) need ~780px for one line; below this the pair already wraps to a row of its
   own, and beside an open event (~340px at 1280) the fields stood ~400px tall over the list. Type and the range
   move into the Filters menu a phone uses (`pages/audit/PagedLogView.tsx`). */
export const AUDIT_FOLD_FILTERS = `@container ${AUDIT_LIST_CONTAINER} (max-width:699.95px)`

/* MACHINES › CONNECTIONS' IMAGE SLOT (196px + 8px gap) — present from 1280px up. Measured: at 1024 the
   cluster (image, org, volume, credential, region, ⋯) left the connection's name no width at all; from 1280 the 256px
   more viewport gives it ~250. Below, the row's hover carries the image. */
export const FEED_NO_CONNECTION_IMAGE = '@media (max-width:1279px)'

/* THE /tasks BOARD'S OWN WIDTH, AS A QUERY CONTAINER — for gates that must follow the BOARD, not
   the window. The supported desktop band is ≥1024px (the supported viewport widths), and
   inside it the board still narrows: the docked Web Console takes up to all but 320px of it
   (`lib/consoleDock.ts`'s `MAIN_MIN_PX`). A viewport `@media` gate cannot see that, so at a 1280px window
   with the console docked the persona and cost stayed and the title shredded.
   `pages/Tasks.tsx` puts `TASK_BOARD_CONTAINER_SX` on the page root; the `BOARD_*` queries below match its
   inline size (the page's content box, header and rows alike). The task page's Subtasks list
   (`components/tasks/SubtaskList.tsx`) is its own container, so its rows gate on the pane's width. A surface
   without that container never matches them. */
export const TASK_BOARD_CONTAINER = 'task-board'
export const TASK_BOARD_CONTAINER_SX = { containerType: 'inline-size', containerName: TASK_BOARD_CONTAINER } as const

/* THE TASK PAGE'S TRAIL, AS A QUERY CONTAINER: `pages/taskDetail/taskDetailConstants.ts` puts it on the header's lead
   (basis 0, so its width never follows its content) — the trail, plus the org aside where one is drawn beside it; `components/tasks/TaskBreadcrumb.tsx` drops the System chip
   on a narrow trail. */
export const TASK_TRAIL_CONTAINER = 'task-trail'

/* THE HEADER'S ONE LINE (`components/tasks/pane/PaneHeader.tsx`): search, the creator strip and the action
   cluster share a row. As the board narrows the creator strip goes FIRST (it is a shortcut; the same filter is
   in Filter & view), then the search box shrinks toward its floor. Measured: the full row needs ~700px. */
export const BOARD_NO_CREATORS = `@container ${TASK_BOARD_CONTAINER} (max-width:719.95px)`

/* THE ACTIVITY ROW'S DROP ORDER (`components/tasks/listViews/feedRow.tsx`), operator's order: PERSONA first,
   then TOKENS, then COST, then RATING. With no console docked a 1024px window leaves a ~780px board, so the persona is
   already gone at the desktop floor and returns from a ~1100px window. Each boundary sits where the title would fall under ~370px — measured on a row
   carrying every slot at a 1440px window, the board narrowed: 397 / 381 / 371px just above the persona, cost
   and rating gates (figures on each slot), BEFORE the tokens slot: its 48px now comes off the title above its
   own gate (~349px just above the persona gate), and the floor holds again from the tokens gate down. The
   tokens gate sits halfway between the persona's and the cost's: tokens are the SECONDARY figure
   beside the dollars (the Table's reading too), so they leave before the cost. Below
   the last the row keeps the window-gated slots (id, author, org), age and ⋯; `e2e/tasks-feed.spec.ts`
   asserts the order. */
/* THE PR SLOT GOES BEFORE ALL OF THEM: it is the row's widest (132px) and the task page lists every PR.
   Placed 120px above the persona's gate, so the title keeps the width it had just above that gate. */
export const BOARD_NO_PR = `@container ${TASK_BOARD_CONTAINER} (max-width:979.95px)`
export const BOARD_NO_PERSONA = `@container ${TASK_BOARD_CONTAINER} (max-width:859.95px)`
export const BOARD_NO_TOKENS = `@container ${TASK_BOARD_CONTAINER} (max-width:809.95px)`
export const BOARD_NO_COST = `@container ${TASK_BOARD_CONTAINER} (max-width:759.95px)`
export const BOARD_NO_RATING = `@container ${TASK_BOARD_CONTAINER} (max-width:679.95px)`

/* THE ROW'S LAST TWO DROPS, ON A BOARD NARROWER STILL: beside an editor and a docked console
   (components/ListDetail.tsx) the board can reach the split's list floor, ~285px of content at a 1440px window.
   The org (72px) goes first, then the author (24px), so the title keeps ~140px (~20 characters) there instead
   of the 1-2 it kept with both. Window-gated slots they also ride, so these only add. */
export const BOARD_NO_ORG = `@container ${TASK_BOARD_CONTAINER} (max-width:519.95px)`
export const BOARD_NO_AUTHOR = `@container ${TASK_BOARD_CONTAINER} (max-width:399.95px)`

/* THE /nodes LIST'S OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on /nodes: an
   open node beside the list (components/ListDetail.tsx) narrows the roster while the window stays wide, and
   window gates kept every slot while the NAME shrank to nothing. `pages/Nodes.tsx` puts the container on its
   page root. EVERY slot drops before the name: OS → counts → org → hosting → enabled → version →
   load. Load is the last of them and has no window gate, so the phone keeps the CPU/RAM/disk meter (an
   owner decision); the version, the operator's reason to scan the desktop list, goes just before it. The 28px
   last-seen age never drops. Each boundary keeps the name at its 150px floor or more with every slot left of it present:
   171px of fixed row (select, age, gaps) + 150 + the slots still shown (OS 56, counts 64, org 128, hosting 88,
   enabled 84, version 64, load 76 — measured on the operator's full row, `e2e/nodes-roster-width.spec.ts`; re-measure
   when a slot or the shell's padding changes). Beside an open node at a 1440px window (546px) the row keeps load,
   version, age and enabled. The Hosts table rides the same boundaries
   (`geometry.ts`'s `host*` gates: origin, org, node count, state, version, load) and the Groups row three of them
   (`group*`: shapes, AWS account, org) — neither cluster is wider than the node row's, so the same widths
   keep their names too. Window gates still apply on top (enabled and version are `id`, gone at ≤767px). Inert
   off /nodes.
   LOAD'S BOUNDARY SITS BETWEEN THE TWO PHONES, ON PURPOSE: the page pads 1rem a side there (`App.tsx::
   APP_MAIN_SX`), so a 448px phone's roster is 416px (the meter stays) and a 360px one's 328px, where the meter
   would leave a host name ~110px, under the roster's 150px name floor: the name wins and the meter goes. */
export const NODES_LIST_CONTAINER = 'nodes-list'
export const NODES_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: NODES_LIST_CONTAINER } as const
export const NODES_NO_OS = `@container ${NODES_LIST_CONTAINER} (max-width:880.95px)`
export const NODES_NO_COUNTS = `@container ${NODES_LIST_CONTAINER} (max-width:824.95px)`
export const NODES_NO_ORG = `@container ${NODES_LIST_CONTAINER} (max-width:760.95px)`
export const NODES_NO_HOSTING = `@container ${NODES_LIST_CONTAINER} (max-width:632.95px)`
export const NODES_NO_ENABLED = `@container ${NODES_LIST_CONTAINER} (max-width:544.95px)`
export const NODES_NO_VERSION = `@container ${NODES_LIST_CONTAINER} (max-width:460.95px)`
export const NODES_NO_LOAD = `@container ${NODES_LIST_CONTAINER} (max-width:396.95px)`

/* THE WORKFLOW-RUN LEDGER'S OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `pages/workflows/WorkflowRunsTab.tsx`: beside an open run (components/ListDetail.tsx) the ledger narrows to
   ~546px at a 1440px window, and window gates kept every slot while the run's reading shrank to "Finis…".
   The run id (160px, and the open pane shows it whole) goes first, then the workflow name (116px); the
   duration, task and age never drop. Beside an open run the reading keeps ~185px. Inert elsewhere. */
export const WORKFLOW_RUNS_CONTAINER = 'workflow-runs'
export const WORKFLOW_RUNS_CONTAINER_SX = { containerType: 'inline-size', containerName: WORKFLOW_RUNS_CONTAINER } as const
export const RUNS_NO_ID = `@container ${WORKFLOW_RUNS_CONTAINER} (max-width:719.95px)`
export const RUNS_NO_WORKFLOW = `@container ${WORKFLOW_RUNS_CONTAINER} (max-width:459.95px)`

/* AUDIT > PERSONAS' OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `pages/personas/PersonasTab.tsx`: beside an open persona at a 1440px window the roster is ~546px against a
   ~730px cluster (Delivery shows its label), so the label went to 0px and the model clipped to "Hi"/"Star" at the
   pane edge. The focus (200px at its floor, and the open pane shows it whole) goes first, then the raw name with
   the model; delivery never drops. Each boundary is the cluster with that slot present plus a ~200px label: the focus
   still shows on the ~770px roster at a 1024px window, not a dead band. Inert elsewhere. */
export const PERSONAS_LIST_CONTAINER = 'personas-list'
export const PERSONAS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: PERSONAS_LIST_CONTAINER } as const
export const PERSONAS_NO_FOCUS = `@container ${PERSONAS_LIST_CONTAINER} (max-width:720.95px)`
export const PERSONAS_NO_LEAD = `@container ${PERSONAS_LIST_CONTAINER} (max-width:560.95px)`

/* AUTOMATIONS' OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `pages/automations/AutomationList.tsx`: beside an open automation the roster narrows while the window stays
   wide, and window gates kept Target · Action and When — at 1024px the Name went to 0px and each row stood ~260px
   tall on its two-line description; at 1440px the description was cramped to ~120px. Both columns drop together
   below a list wide enough for a ~220px name beside them, and the muted line under the name carries
   When · Target · Action as on a narrow window. Inert elsewhere. */
export const AUTOMATIONS_LIST_CONTAINER = 'automations-list'
export const AUTOMATIONS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: AUTOMATIONS_LIST_CONTAINER } as const
export const AUTOMATIONS_NO_META = `@container ${AUTOMATIONS_LIST_CONTAINER} (max-width:699.95px)`
/* TEMPLATES' OWN WIDTH, AS A QUERY CONTAINER — Automations' reason, on `pages/templates/TemplatesManagePage.tsx`:
   beside an open template the list narrows while the window stays wide, and window gates kept Purpose and Origin —
   at 1440px the name was squeezed to ~90px and cut. Both drop below a list wide enough for a ~250px name beside
   them and Status and Starts. Inert elsewhere. */
export const TEMPLATES_LIST_CONTAINER = 'templates-list'
export const TEMPLATES_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: TEMPLATES_LIST_CONTAINER } as const
export const TEMPLATES_NO_META = `@container ${TEMPLATES_LIST_CONTAINER} (max-width:639.95px)`
/* The filter strip's fold: Search (120px floor) and When, Status, Owner (120px each) need ~520px, so
   below this When and Owner move into a Filters menu — beside an open automation (~546px at 1440, ~320px at 1024)
   the strip stays one line instead of stranding Owner, or Status and Owner, on a second. */
export const AUTOMATIONS_FOLD_FILTERS = `@container ${AUTOMATIONS_LIST_CONTAINER} (max-width:579.95px)`

/* DIRECTORY > PEOPLE'S OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `pages/users/UsersTab.tsx`: beside an open user at a 1024px window the roster is ~320px, its ~395px cluster
   overflowed, the EMAIL (the row's only identity) went to 0px and the Admin chips clipped at the pane edge.
   The name pair goes first, then the groups count and, above the phone (beside an open user), the Select tick
   (its 26px goes to the email); Admin (the toggle, no ⋯ fallback) never drops.
   Each boundary is the cluster with that slot present plus a 150px email. Inert elsewhere. */
export const PEOPLE_LIST_CONTAINER = 'people-list'
export const PEOPLE_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: PEOPLE_LIST_CONTAINER } as const
export const PEOPLE_NO_NAME = `@container ${PEOPLE_LIST_CONTAINER} (max-width:549.95px)`
export const PEOPLE_NO_GROUPS = `@container ${PEOPLE_LIST_CONTAINER} (max-width:399.95px)`
/* The operator's Status and Org fold behind one Filters button below the width Search + Status + Org need on one
   line (~190 + 120 + 180 + gaps): at 390px Org took a row of its own, as it did beside an open user. */
export const PEOPLE_FOLD_FILTERS = `@container ${PEOPLE_LIST_CONTAINER} (max-width:519.95px)`
/* DIRECTORY > GROUPS' OWN WIDTH, AS A QUERY CONTAINER — People's reason above, on
   `pages/users/GroupsTab.tsx`: beside an open group at a 1440px window (~546px) the 200px description kept its
   width and the NAME (the row's identity) shrank to a letter and an ellipsis. The description goes first, then the
   binding count (the disc still says whether the group grants anything); the member count never drops. Each
   boundary is the cluster with that slot present plus a 150px name. Inert elsewhere. */
export const GROUPS_LIST_CONTAINER = 'groups-list'
export const GROUPS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: GROUPS_LIST_CONTAINER } as const
export const GROUPS_NO_DESCRIPTION = `@container ${GROUPS_LIST_CONTAINER} (max-width:649.95px)`
export const GROUPS_NO_BINDINGS = `@container ${GROUPS_LIST_CONTAINER} (max-width:379.95px)`
/* All orgs adds the ~120px Org chip, so the binding count goes sooner there: beside an open group at 1440px (~424px)
   it left the org-named Everyone title ~90px, "Everyone · E2E Compa…" on two lines. */
export const GROUPS_NO_BINDINGS_WITH_ORG = `@container ${GROUPS_LIST_CONTAINER} (max-width:499.95px)`
/* The Admin toggle's width in that last band: 80px → 56px, still the "standard" chip and the
   "ADMIN" header whole, the 24px going to the email beside an open user. */
export const PEOPLE_ADMIN_NARROW = 56

/* DIRECTORY > PROJECTS' OWN WIDTH, AS A QUERY CONTAINER: beside an open project at a 1024px window
   the list is ~320px and the 128px org slot left the name 63px ("org-a-pr…"). At or below 419.95px the org
   leaves the cluster for the title, after the name (`pages/Projects.tsx`: one row height, and the
   name ellipsises before the org). Above the phone
   band only (`ui/feed/geometry.ts`'s `projectsOrg`): the phone's one-line row already reads its names. Inert
   elsewhere. */
export const PROJECTS_LIST_CONTAINER = 'projects-list'
export const PROJECTS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: PROJECTS_LIST_CONTAINER } as const
export const PROJECTS_NO_ORG = `@container ${PROJECTS_LIST_CONTAINER} (max-width:419.95px)`

/* AUDIT > SESSIONS' OWN WIDTH, AS A QUERY CONTAINER: the container the Node slot's `cqw` cap reads
   (`pages/audit/SessionsTab.tsx`), so a long node name takes the row's spare width without crowding a narrowed list. */
export const SESSIONS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: 'sessions-list' } as const

/* DIRECTORY > SECRETS' OWN WIDTH, AS A QUERY CONTAINER: below ~400px (a phone, or beside an open
   credential) the Type column and a one-line name cannot both read, so the type rides the name's hover
   (`lib/credentialRow.tsx`). Also the container the Type slot's `cqw` cap reads. */
export const CREDENTIALS_LIST_CONTAINER = 'credentials-list'
export const CREDENTIALS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: CREDENTIALS_LIST_CONTAINER } as const
export const CREDENTIALS_TYPE_UNDER = `@container ${CREDENTIALS_LIST_CONTAINER} (max-width:399.95px)`
/* The same list under ~400px packs its filter strip compact (`pages/credentials/CredentialFilterStrip.tsx`):
   Search with Only mine and Flat/Tree on one line, the pickers three to the next. Its own decision, its own query. */
export const CREDENTIALS_FILTERS_COMPACT = `@container ${CREDENTIALS_LIST_CONTAINER} (max-width:399.95px)`

/* MACHINES › BRAINS' OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `pages/credentials/Accounts.tsx`: beside an open account at a 1024px window the roster is ~320px, its ~575px
   cluster overflowed the pane by ~300px and the account NAME went to 0px. Drop order: sessions → 7d → org →
   provider (on the name's hover instead, as on the phone) → the enable toggle (the ⋯ item covers it); the
   5h reading and the ⋯ never drop. Each boundary is the cluster with that slot present plus the 74px lead and a
   150px name; the cluster's state slot is 56px, wide enough for the word "Disabled". The expanded node rows' two quota readings go with the toggle. Inert elsewhere; the roster is the
   container, so Sessions also drops on the full-width roster at a 1024-1055px window (~768px), where it left the
   name ~119px. */
export const ACCOUNTS_LIST_CONTAINER = 'accounts-list'
export const ACCOUNTS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: ACCOUNTS_LIST_CONTAINER } as const
export const ACCOUNTS_NO_SESSIONS = `@container ${ACCOUNTS_LIST_CONTAINER} (max-width:818.95px)`
export const ACCOUNTS_NO_WEEK = `@container ${ACCOUNTS_LIST_CONTAINER} (max-width:750.95px)`
export const ACCOUNTS_NO_ORG = `@container ${ACCOUNTS_LIST_CONTAINER} (max-width:652.95px)`
export const ACCOUNTS_PROVIDER_UNDER = `@container ${ACCOUNTS_LIST_CONTAINER} (max-width:518.95px)`
export const ACCOUNTS_NO_ENABLED = `@container ${ACCOUNTS_LIST_CONTAINER} (max-width:413.95px)`
/* The org column once 7d has dropped (`ACCOUNTS_NO_WEEK`) and until it drops itself: 72px, which still reads a short
   org whole ("Operator", ~55px) and ellipsizes a long one (full name on `title`). At 128px it sat mostly blank on a
   ~650px roster while the account name ellipsized. */
export const ACCOUNTS_ORG_NARROW = 72

/* THE TASK ARTIFACTS LIST'S OWN WIDTH, AS A QUERY CONTAINER: in the task side pane (~520px at a
   1366px window) the window gates kept the Kind (124px) and Current (78px) slots and left the filename ~60px
   ("coverag…"). Both facts have a second home on the row's disc (shape = kind, tone = currency) and in the
   viewer's identity strip, so the Kind goes first, then the currency; size·age and the download never drop
   here. Each boundary keeps the filename ≥ ~250px. Inert elsewhere. */
export const ARTIFACTS_LIST_CONTAINER = 'task-artifacts'
export const ARTIFACTS_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: ARTIFACTS_LIST_CONTAINER } as const
export const ARTIFACTS_NO_KIND = `@container ${ARTIFACTS_LIST_CONTAINER} (max-width:719.95px)`
export const ARTIFACTS_NO_CURRENCY = `@container ${ARTIFACTS_LIST_CONTAINER} (max-width:599.95px)`

/* THE MERGE LANE'S RUNS LIST'S OWN WIDTH, AS A QUERY CONTAINER: in the project side pane (~410px) the lead id
   column took room a run's title needed. Below ~600px it drops — `FEED_NO_ID` is a window band, and a 1024px
   window kept it beside a ~384px list — the id then opening the run's title, as on the phone
   (`components/projects/ProjectMergeLaneTab.tsx`). Inert elsewhere. */
export const MERGE_LANE_RUNS_CONTAINER = 'merge-lane-runs'
export const MERGE_LANE_RUNS_CONTAINER_SX = { containerType: 'inline-size', containerName: MERGE_LANE_RUNS_CONTAINER } as const
export const LANE_NO_ID = `@container ${MERGE_LANE_RUNS_CONTAINER} (max-width:599.95px)`

/* SETTINGS > EMAIL > A CONNECTION'S SENT LIST'S OWN WIDTH, AS A QUERY CONTAINER: beside an open task or run
   at a 1280px window the window gates kept To, the status chip and the source (~460px of cluster) and the subject went
   to 0px. The status chip goes first (the row's disc already says Sent or Failed), then the source (the row's ⋯ menu
   carries it, as on the phone), where To also narrows and the subject wraps to two lines. Inert elsewhere. */
export const EMAIL_SENDS_CONTAINER = 'email-sends'
export const EMAIL_SENDS_CONTAINER_SX = { containerType: 'inline-size', containerName: EMAIL_SENDS_CONTAINER } as const
export const SENDS_NO_STATUS = `@container ${EMAIL_SENDS_CONTAINER} (max-width:719.95px)`
export const SENDS_NO_SOURCE = `@container ${EMAIL_SENDS_CONTAINER} (max-width:599.95px)`

/* AUTOMATION > SLOTS' OWN WIDTH, AS A QUERY CONTAINER — the BOARD's reason above, on
   `components/WorktreePool.tsx`: beside a docked console at a 1280px window the pool is ~330px against a flat
   row's ~460px cluster, the title (the slot's identity) went to 0px and the cluster overflowed the card's left
   edge. The node goes first, then the project (the flat row then prefixes it to the slot number, so the
   identity stays whole), then git and stack (the `meta` pair), then the session. Each boundary is the cluster
   with that slot present plus a 150px title; one ladder for both outlines (the grouped one has no project or
   node slot, so it meets only the last two). Inert elsewhere. */
export const POOL_LIST_CONTAINER = 'pool-list'
export const POOL_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: POOL_LIST_CONTAINER } as const
export const POOL_NO_NODE = `@container ${POOL_LIST_CONTAINER} (max-width:610.95px)`
export const POOL_NO_PROJECT = `@container ${POOL_LIST_CONTAINER} (max-width:532.95px)`
export const POOL_NO_META = `@container ${POOL_LIST_CONTAINER} (max-width:450.95px)`
export const POOL_NO_ID = `@container ${POOL_LIST_CONTAINER} (max-width:332.95px)`

/* THE WIDTH FROM WHICH A LIST ROW SHOWS ITS ORG SLOT — 1185px. Bisected on a tagged task row, reading row HEIGHT
   against a control run. Its reader is `ui/OrgIndicator.tsx::useOrgColumn`. A BARE QUERY because it is read by `useMediaQuery`, not composed into an `sx` media block. */
export const ORG_QUERY = '(min-width:1185px)'

/* THE THEME'S OWN TAP FLOOR, AS A QUERY, so a list row can pull BACK against it. `theme.ts`'s
   `MuiIconButton` raises min size to 44 below `sm`, and that media rule BEATS a plain `sx` width of the
   same specificity — the pullback must sit in a media query too. `sm`, not `PHONE`, because the theme's
   boundary is `sm` (else 481-599px would be ragged); a row pulls back the WIDTH, NEVER the height (44px
   vertical is the a11y contract). */
export const TAP_FLOOR_DOWN = '@media (max-width:599.95px)'

/* The COMPLEMENT of `PHONE`: the only correct way to spell "this exists for the phone card".
 *
 * Write a phone-only control base-visible and hidden ABOVE the phone — never `display: 'none'` with a
 * `[PHONE]` reveal. jsdom applies the base declaration and ignores `@media`, so the reveal spelling
 * leaves the control out of the accessibility tree and invisible to `getByRole`.
 *
 * The LOGICAL NEGATION of `PHONE`, not `(min-width:481px)`: a fractional width (zoom, fractional DPR)
 * like 480.5px satisfies neither 480/481 query, showing both halves. Negation cannot leave a gap. */
export const ABOVE_PHONE = `@media not all and ${PHONE_QUERY}`

/* A PAGE STATUS'S PHONE HOME (Playbooks' Preview, Customers' Test mode): from 481px up the chip rides the tab strip;
   on a phone it would push a tab off the strip's row, so it takes a line at the top of the panel instead. */
export const PHONE_STATUS_LINE_SX = { display: 'none', [PHONE]: { display: 'flex', alignItems: 'center', gap: 1, mt: 0.5, mb: 1.5 } } as const

/* THE BAND A FEED ROW CAN AFFORD LEAST — >480px and ≤1023px, a MIDDLE-band hide, DERIVED.
 *
 * The shell's 330px is charged at 481px and NOT at 480 (the rail collapses), so the phone row is richer
 * than the 481 row. The band is exactly the unsupported gap of
 * the supported viewport widths (448px phone, 1024px+), so the slot is present at
 * EVERY SUPPORTED WIDTH. Measured control title minus floor (40px below 768, 200 above) vs a 72px slot +
 * 6px gap: 448 -> 237 left PAYS; 481 -> 12 Cannot; 900 -> 180 Cannot (`FEED_NO_META` lets the full
 * cluster in); 1024 -> 304 PAYS.
 *
 * NESTED OFF `ABOVE_PHONE`, not `(min-width:481px)` — see `ABOVE_PHONE` for the fractional-width gap.
 * RE-DERIVE HERE if the shell, the feed cluster or either title floor moves, and take it to a browser. */
export const SIDEBAR_SQUEEZE_SX = {
  [ABOVE_PHONE]: { '@media (max-width:1023px)': { display: 'none' } },
} as const

/* THE FEED ORG SLOT'S MOBILE-HIDDEN VARIANT (mobile web must not show the org name at all —
 * an owner decision reversing the `SIDEBAR_SQUEEZE_SX` phone reveal above for this ONE slot).
 * Same 1023px upper edge, UNNESTED off `ABOVE_PHONE`: hidden at every width ≤1023px, phone band included,
 * present only at the ≥1024 supported desktop floor `SIDEBAR_SQUEEZE_SX` already opens on.
 * Scoped to the feed's org slot alone — `SIDEBAR_SQUEEZE_SX` itself is unchanged and still governs
 * `pages/Projects.tsx`, which this task does not touch. */
export const ORG_HIDDEN_ON_MOBILE_SX = {
  '@media (max-width:1023px)': { display: 'none' },
} as const

/** A row's Select checkbox: `p: '2px'` puts the MUI root at 24x24 — the phone row height and WCAG 2.5.8's
 *  minimum. `ui/RowSelect.tsx` draws it; `components/tasks/listViews/feedRow.tsx` fits it to the feed's slot. */
export const SELECT_BOX_SX = { p: '2px' } as const

/* Truncate one line and say so with an ellipsis. `minWidth: 0` is the load-bearing half — without it a
 * flex or table child refuses to shrink below its min-content and the ellipsis never engages. */
export const ELLIPSIS_SX = {
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
  minWidth: 0,
} as const

/** A long token (a URL, an ARN, an issuer) wraps inside its column instead of widening it. */
export const wrapSx = { minWidth: 0, overflowWrap: 'anywhere', wordBreak: 'break-word' } as const
export const anywhereSx = { minWidth: 0, overflowWrap: 'anywhere' } as const

/** The mono stack for ids, ports, durations and quota numbers — `theme.typography.mono`, read once. */
export const monoSx = (theme: Theme) => ({ fontFamily: theme.typography.mono.fontFamily })

/** A cloud id (`fs-…`, `subnet-…`, a region): mono with tabular digits, so a column of them aligns. */
export const ID_SX = [monoSx, { fontVariantNumeric: 'tabular-nums' }]

/* The 44px touch floor for a CONTROL, at phone width only. `?? 44` covers MUI's stock theme (a unit
 * test without the app's provider); `theme.custom.tapTargetPx` is the single home of the number. */
export const tapTargetSx = (theme: Theme) => ({
  minHeight: { xs: theme.custom?.tapTargetPx ?? 44, sm: 'auto' },
})

/* The same floor for a TEXT FIELD, which needs both halves.
 *
 * It must land on `.MuiInputBase-input` (the native `<input>`), not only the wrapper: the flex wrapper
 * centres a still-40px input in a 44px box, so "the box measures 44" is not evidence — assert on the
 * inner node.
 *
 * SCOPED TO `down('sm')` AS ONE BLOCK: an unscoped `boxSizing: 'border-box'` collides with MUI's
 * `height: 1.4375em` and collapses every DESKTOP field to ~26px while mobile looks perfect — so check a
 * change here at desktop width too. `down('sm')`, not `PHONE`: this is about a finger, and a 540px
 * tablet has the same finger. */
export const fieldTapTargetSx = (theme: Theme) => ({
  [theme.breakpoints.down('sm')]: {
    '& .MuiInputBase-root': { minHeight: theme.custom?.tapTargetPx ?? 44 },
    '& .MuiInputBase-input': { minHeight: theme.custom?.tapTargetPx ?? 44, boxSizing: 'border-box' },
    /* A `TextField select` NEEDS ITS OWN ARM: MUI's compound `&.MuiSelect-select` min-height rule has the
       same specificity as a bare descendant selector and lands later in the sheet, so the raise above
       never reaches the select's display node. This three-class descendant outranks it. It costs no
       layout (the wrapper is already 44 and the border is an absolute `fieldset`); re-measure the inner
       node, not just the wrapper, if changed. */
    '& .MuiInputBase-root .MuiSelect-select': { minHeight: theme.custom?.tapTargetPx ?? 44, boxSizing: 'border-box' },
    /* An AUTOCOMPLETE (`ui/AsyncLookup`, `TaskTags`) NEEDS THE OPPOSITE ARM: its root pads ~6px around the
       input, so a 44px input made a 56px box beside 44px selects. The root (the tap target: a click anywhere
       focuses the input) keeps the floor above; this three-class descendant outranks the input raise. */
    '& .MuiAutocomplete-inputRoot .MuiAutocomplete-input': { minHeight: 0 },
  },
})

/* A per-row action button — the console's ONE size for controls inside a data row.
 *
 * Every value is a REDUCTION against MUI's small Button, so no `sx`-increase justification is owed; the
 * tap floor rides LAST so nothing shadows it. An array: how MUI composes an object with a callback.
 *
 * A DESTRUCTIVE row action pairs this with `variant="outlined" color="error"`, never `contained` — the
 * app's row-action language is low-emphasis.
 *
 * NO `minWidth`: MUI's stock 64px applies, clearing 44 horizontally. An icon-only caller that strips it
 * owes its own `minWidth`. */
export const ROW_ACTION_SX = [
  { fontSize: '0.75rem', lineHeight: 1.5, py: '3px', px: 1, minHeight: 0 },
  tapTargetSx,
]

/* FULL BLEED AT PHONE WIDTH — the table reclaims the shell's gutter and squares off its own frame.
 *
 * THE TABLE IDIOM'S CONSTANT ONLY: a table cannot drop a column, so the gutter is off its widest cell.
 * A `ui/feed/FeedList` row drops slots instead (`ui/feed/geometry.ts`) and must NOT bleed — a converted
 * list spreading this runs edge-to-edge beside inset siblings.
 *
 * Squaring the container is not decoration: a box pulled past the viewport edge keeps drawing its
 * border and radius off-screen. Goes on a WRAPPER around the table (`& > .MuiTableContainer-root` is
 * `ui/DataTable`'s shell).
 *
 * COMPOSE IT AS AN ARRAY (`sx={[FULL_BLEED_SX, PAGE_CARD_SX]}`): an object SPREAD of two objects that
 * both carry `[PHONE]` silently drops the first one's block. */
export const FULL_BLEED_SX = {
  [PHONE]: {
    mx: '-1rem',
    '& > .MuiTableContainer-root': {
      borderLeft: 0,
      borderRight: 0,
      borderRadius: 0,
      boxShadow: 'none',
    },
  },
} as const

/* THE PHONE CARD — the same columns, restacked, NOT dropped.
 *
 * MUI's horizontal scroll measured badly: at 390px zero rows were fully visible. Below `PHONE` the row
 * becomes a card and `thead` is hidden; the DOM is IDENTICAL at both widths, keeping the interactive
 * element count equal by construction. Hiding `thead` drops headings from the accessibility tree,
 * which `CellLabel` (`ui/CellLabel.tsx`) puts back.
 *
 * WHICH CELL TAKES ITS OWN LINE IS THE PAGE'S CALL: the page passes its own rules to `phoneCardSx`. */
export const PHONE_CARD_SX = {
  [PHONE]: {
    /* `display: block` on the TABLE itself: as `display: table` it sizes to its widest min-content and
       hangs past the viewport. Blocking it is what makes `width: 100%` bind. */
    '& table': { display: 'block', width: '100%' },
    '& thead': { display: 'none' },
    '& tbody': { display: 'block' },
    '& tbody tr': {
      display: 'flex',
      flexWrap: 'wrap',
      alignItems: 'center',
      columnGap: 1,
      px: 1,
      py: 0.125,
      width: '100%',
      borderBottom: '1px solid',
      borderColor: 'divider',
    },
    '& tbody td': {
      display: 'flex',
      alignItems: 'center',
      gap: 0.5,
      minWidth: 0,
      maxWidth: '100%',
      // The card's separator is the row border above; a rule under every stacked value would draw one
      // line per column, per row.
      border: 0,
      px: 0,
      py: 0.25,
    },
  },
} as const

/** `PHONE_CARD_SX` plus a page's own phone `rules` (e.g. `'& tbody td[data-col="x"]': { flex: '1 1 100%' }` for a
 *  full line), merged under its one `[PHONE]` key; `row` adds to the card row's own rule. */
export function phoneCardSx(rules: Record<string, object>, row?: object) {
  const card = PHONE_CARD_SX[PHONE]
  return { [PHONE]: { ...card, ...(row && { '& tbody tr': { ...card['& tbody tr'], ...row } }), ...rules } }
}

/* THE JAAH AI TOKEN SECTIONS' PHONE CARD (`pages/clients/JaahTenantsUsageSection.tsx`, `JaahReconciliationSection.tsx`): the org cell takes the whole
 * first line, the rest wrap beneath it. `flexDirection: 'row'` undoes a right-aligned cell's `row-reverse`, so
 * each value's `CellLabel` leads it. */
export const ORG_PHONE_CARD_SX = phoneCardSx({
  '& tbody td': { ...PHONE_CARD_SX[PHONE]['& tbody td'], flexDirection: 'row' },
  '& tbody td[data-col="org"]': { flex: '1 1 100%' },
}, { py: 0.75, rowGap: 0.25 })

/* VISUALLY HIDDEN BUT STILL ANNOUNCED.
 *
 * `main.tsx`'s `caption-probe` spells this inline on purpose: it is a style ORACLE for two e2e specs,
 * and pinning an oracle to a tunable constant stops it measuring. Leave it.
 *
 * `clip` is kept beside `clipPath` (inert where `clipPath` is honoured; re-pointing must not subtract
 * from a caller).
 *
 * THE CALLER MUST GIVE IT A POSITIONED ANCESTOR: otherwise its containing block is the initial one, it
 * ESCAPES `.app-layout`'s `overflow: hidden`, and the document itself scrolls (the invariant
 * `e2e/app-shell-scroll.spec.ts` holds). `position: relative` on the owning cell fixes it. */
export const SR_ONLY_SX = {
  position: 'absolute',
  width: '1px',
  height: '1px',
  p: 0,
  m: '-1px',
  border: 0,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  clipPath: 'inset(50%)',
  whiteSpace: 'nowrap',
} as const

/* The rail's colour-scheme resolver. Lives here, not in `components/Sidebar.tsx`: this is the shared
   style vocabulary both `ui/` and `components/` compose from (`ui/**` must not import `components/**`). */
/** The rail is CHROME: its colours come from `theme.custom.sidebar`, but FOLLOW the colour scheme. The
 *  palette's `mode` is frozen under `cssVariables`, so the dark half goes through
 *  `theme.applyStyles('dark', …)`.
 *
 *  EVERY colour the RAIL paints goes through here; a hardcoded literal in rail styling is a light-theme
 *  bug by construction. Add a token in `theme.ts` instead of a hex.
 *
 *  ONE CALL PER OBJECT — never spread two side by side: each returns its dark half under the SAME
 *  `[data-theme="dark"] &` key, so the second REPLACES the first's dark rule. Ask for every property in
 *  a single `pick`. */
export function schemed(t: Theme, pick: (c: SidebarColors) => Record<string, string | number>) {
  return { ...pick(t.custom.sidebar.light), ...t.applyStyles('dark', pick(t.custom.sidebar.dark)) }
}

/* A ROW MARKED AS THE ONE ON SCREEN — a deep-linked arrival, or the editor open beside the list
   (components/ListDetail.tsx). A WASH, not a slot mark, so it survives every width; deepens its own hover.
   `ui/feed/FeedList.tsx` reads it. */
export const ROW_MARKED_SX = {
  bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.10)',
  '&:hover': { bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.16)' },
} as const

/* A person's or an agent's own words as typed: line breaks kept, a long token broken anywhere rather than widening
 * the pane (`components/agentEvents.tsx` and its readers `TaskActivity`/`console/ChatView`, `components/support/SupportChat.tsx`,
 * the support tabs). */
export const PRE_WRAP_SX = { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', wordBreak: 'break-word' } as const

/* A tab strip's phone density: a reduction against MUI's 16px tab padding; `flex: '1 1 auto'` spreads the
 * labels. On a wrapper `Box` (`components/Tabs` takes no `sx`). More takes the same sides: its width probe is a
 * tab, so a wider button cut the last tab the fit kept (Directory beside a compact verb). */
export const PHONE_TAB_STRIP_SX = {
  [PHONE]: {
    '& .MuiTab-root': { paddingLeft: '6px', paddingRight: '6px', flex: '1 1 auto', whiteSpace: 'nowrap' },
    '& .tabs-more': { paddingLeft: '6px', paddingRight: '6px' },
  },
} as const

/* The billing record pages' tab strip (`pages/org/billing/{SubscriptionPage,HostingDetailPage,JaahAiPage}.tsx`):
 * a gap under it, the labels spread across a phone. */
export const TAB_STRIP_SX = {
  mb: 1.5,
  [PHONE]: { '& .MuiTab-root': { flex: '1 1 auto' } },
} as const

/* THE BILLING LIST'S OWN WIDTH, AS A QUERY CONTAINER — the /nodes reason above, on /org/billing: an open record
   beside the list (components/ListDetail.tsx) narrows it to ~400px on a 1440px or 1920px window, where the window
   gates kept every slot and the name shrank to nothing. Under 600px the list takes the phone row — disc, name,
   amount, the note on the row's hover. `pages/org/BillingPage.tsx` puts the container on the list. */
export const BILLING_LIST_CONTAINER = 'billing-list'
export const BILLING_LIST_CONTAINER_SX = { containerType: 'inline-size', containerName: BILLING_LIST_CONTAINER } as const
export const BILLING_NARROW = `@container ${BILLING_LIST_CONTAINER} (max-width:599.95px)`
