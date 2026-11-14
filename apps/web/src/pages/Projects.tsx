import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { apiDetail, del, get, post } from '../api'
import { useAuth, useCan, useCanOnProject } from '../auth'
import { lockedForMember } from '../lib/jaahManaged'
import { isEnabled } from '../lib/editionFeatures'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { cascadeFailureText } from '../lib/erase'
import { safeHref } from '../lib/safeHref'
import { FORGE_LABELS, type Project } from '../types'
import Banner from '../ui/Banner'
import ListStates from '../ui/feed/ListStates'
import LoadFailedState from '../ui/LoadFailedState'
import type { FeedNote, FeedRowSpec, OverflowItem } from '../ui/feed/types'
import { deleteItem, editItem } from '../components/tasks/rowActions'
import { useOpenDetailPath } from '../components/ListDetail'
import { ABOVE_PHONE, PHONE, PROJECTS_LIST_CONTAINER_SX, PROJECTS_NO_ORG, SR_ONLY_SX, tapTargetSx, ELLIPSIS_SX } from '../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT } from '../theme'
import { countOf } from '../lib/humanize'
import { OrgIndicator, orgSlots, orgTitle, useOrgVisible } from '../ui/OrgIndicator'
import PageHeader from '../ui/PageHeader'
import SortMenu from '../ui/SortMenu'
import { flipSort, rankRows } from '../ui/SortableHead'
import { useChangeOrgAction } from '../components/ChangeOrgDialog'
import { danger, useConfirm } from '../components/ConfirmDialog'
import { useRowActions } from '../hooks/useBusyIds'
import type { SortDir } from '../hooks/useListPage'
import { useProjects, useWorktrees } from '../hooks/useRosters'
import { useProjectCreateLabel } from '../hooks/useProjectCreateLabel'
import { useTaskTree } from '../hooks/useTaskTree'
import { MUTED_PREF, readMuted, toggledMuted } from '../lib/taskMute'
import { EyeIcon, EyeOffIcon } from '../ui/icons'
import NewButton from '../ui/NewButton'
import Tip from '../ui/Tip'

type SortKey = 'name' | 'questions' | 'running' | 'queued' | 'active'

/** The task-count columns (`GET /api/tasks/tree`'s project counts, top-level tasks). Running is `in_progress`
 *  without its queued slice (`types.ts::TaskTreeCounts`). */
type TaskColumn = 'questions' | 'running' | 'queued'
const TASK_COLUMNS: { key: TaskColumn; label: string; noun: [string, string]; width: number; gate: 'projectsTasks' | 'projectsQuestions' }[] = [
  { key: 'questions', label: 'Questions', noun: ['question', 'questions'], width: 84, gate: 'projectsQuestions' },
  { key: 'running', label: 'Running', noun: ['running', 'running'], width: 70, gate: 'projectsTasks' },
  { key: 'queued', label: 'Queued', noun: ['queued', 'queued'], width: 64, gate: 'projectsTasks' },
]

/* THE TITLE AS NAME + ORG ON ONE LINE. The org is drawn on the phone and on a narrow list above it
   (`PROJECTS_NO_ORG`, beside an open project), where its slot is off: at 448px its 128px slot cut the names to "org-a-probe-p…". THE NAME WINS: at a 1024px window
   a full-width org cut the names to "org-a-pro…". The two WRAP inside a one-line clip (`TITLE_FIT_SX`): the org
   stays on the name's line only while the whole name and a few of its letters fit there, else it wraps onto the
   clipped line and the name keeps the row. On the line it grows into what the name leaves, up to its own width,
   ellipsised (its hover names it). */
const TITLE_ROW_SX = { display: 'inline-flex', alignItems: 'center', gap: 0.5, maxWidth: '100%', minWidth: 0 } as const
const TITLE_FIT_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 0.5,
  flex: '0 1 auto',
  minWidth: 0,
  // The name's own line box (`ui/feed/FeedList.tsx` TITLE_SX: line-height 1.3): a wrapped org is never seen.
  maxHeight: '1.3em',
  overflow: 'hidden',
} as const
const TITLE_NAME_SX = ELLIPSIS_SX
const TITLE_ORG_SX = {
  display: 'none',
  // Placed on the name's line at its floor, then grown up to its whole name.
  flex: '1 1 3.5em',
  maxWidth: 'max-content',
  minWidth: 0,
  overflow: 'hidden',
  fontSize: FONT_SIZE.xs,
  [PHONE]: { display: 'inline-flex' },
  [ABOVE_PHONE]: { [PROJECTS_NO_ORG]: { display: 'inline-flex' } },
} as const

/** The active-slot count in words: the slot's hover and its accessible name. `null` active = not visible to
 *  this viewer (no `worktrees:view`). */
export function projectCountsLabel(active: number | null): string {
  return active === null ? 'Active slots hidden' : countOf(active, 'active slot')
}

/* The sortable keys, each a fact on the row. Read by `ui/SortMenu` (the phone, where the header row is off) and the
   header row. */
const SORTABLE_COLUMNS: { key: SortKey; label: string; firstDir: SortDir }[] = [
  { key: 'name', label: 'Project', firstDir: 'asc' },
  // A count reads best largest first.
  ...TASK_COLUMNS.map(c => ({ key: c.key, label: c.label, firstDir: 'desc' as const })),
  { key: 'active', label: 'Active slots', firstDir: 'desc' },
]

export default function Projects() {
  /* Edit is a ⋯ item (a data record), so it navigates via the router; the row's TITLE is the real
     `RouterLink` to the same `/projects/:id`. */
  const navigate = useNavigate()
  // Persisted react-query cache; the 'worktrees' key matches `components/WorktreePool.tsx` for dedupe.
  const projectsQuery = useProjects()
  // `GET /api/worktrees` is `worktrees:view`; a `projects:view`-only account must not 403 here and see
  // "Failed to load projects" over a loaded list.
  const can = useCan()
  const canReadWorktrees = can('worktrees:view')
  const canCreate = can('projects:create')
  const canEditProject = useCanOnProject()
  const { admin, updatePreferences } = useAuth()
  const createLabel = useProjectCreateLabel(canCreate)
  const changeOrg = useChangeOrgAction('project')
  const worktreesQuery = useWorktrees({ enabled: canReadWorktrees })
  /* A system project (`system_kind`: the org's Nodes project) is never listed: its tasks are reached from the
     node's Automations tab. */
  const projects = (projectsQuery.data ?? []).filter(p => !p.system_kind)
  const worktrees = worktreesQuery.data ?? []
  // `canReadWorktrees &&` is belt-and-braces here (a disabled query is not loading); explicit so a
  // future upgrade cannot make the page wait on a query it does not run.
  const loading = projectsQuery.isLoading || (canReadWorktrees && worktreesQuery.isLoading)
  // Delete failures share the banner but surface only once the load has succeeded.
  const { busy: busyIds, withBusy, error: actionError } = useRowActions<number>()
  // `canReadWorktrees &&` IS LOAD-BEARING HERE: disabling a query does not clear an error it already has
  // (a warm reload fires it from the persisted permission snapshot, 403s, then disables it).
  const loadFailed = projectsQuery.isError || (canReadWorktrees && worktreesQuery.isError)
  const error = loadFailed ? 'Failed to load projects' : actionError
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'name', dir: 'asc' })
  const [confirm, confirmDialog] = useConfirm()

  /* The task counts, from the rail's own read (one cache): `null` without `tasks:view` or before it lands. */
  const canReadTasks = can('tasks:view')
  const { tree } = useTaskTree(canReadTasks)
  const countsById = new Map((tree?.projects ?? []).map(t => [t.id, t.counts]))
  function taskCount(p: Project, column: TaskColumn): number | null {
    const counts = countsById.get(p.id)
    if (!counts) return null
    const queued = counts.queued ?? 0
    if (column === 'questions') return counts.questions
    return column === 'queued' ? queued : Math.max(0, counts.in_progress - queued)
  }

  /* HIDE / SHOW (`lib/taskMute.ts`): this user's, the same entry as the rail's Hide. A hidden project stays listed
     here, greyed, so it can be shown again. */
  const hiddenKeys = readMuted(admin?.preferences)
  const isHidden = (p: Project) => hiddenKeys.includes(`project:${p.id}`)
  function toggleHidden(p: Project) {
    void updatePreferences({ [MUTED_PREF]: toggledMuted(hiddenKeys, `project:${p.id}`) })
  }

  // `null` = not knowable (no `worktrees:view`), rendered as an em dash; 0 would be a lie.
  function activeCount(name: string): number | null {
    if (!canReadWorktrees) return null
    return worktrees.filter(w => w.project === name && w.state !== 'free').length
  }

  /** One rule for the header row and `SortMenu`: the sorted column flips; another starts at its `firstDir`. */
  function toggleSort(key: SortKey) {
    setSort(s => flipSort(s, key, SORTABLE_COLUMNS.find(c => c.key === key)?.firstDir ?? 'asc'))
  }

  // An unknowable count (`null`) ranks last either way.
  const sorted = rankRows(projects, p => (
    sort.key === 'name' ? p.name : sort.key === 'active' ? activeCount(p.name) : taskCount(p, sort.key)
  ), sort.dir)

  // THE OPERATOR'S ARM erases every worktree directory of this project on every node, then deletes the
  // row; no per-slot refusal vetoes it. A partial failure keeps the project and names the unaddressable
  // slots (`cascadeFailureText`) inside the still-open confirm; both lists are refetched either way.
  // A tenant superadmin is refused that route, so their Delete is `DELETE /api/projects/{id}`, which runs
  // the same erase for a project that declares slots (`routes/projects.py::delete_project`). Either way the
  // project's tasks go with it: nothing it holds refuses the delete.
  // The confirm's gate is the project's name typed out (`typed`), since Delete physically erases every
  // worktree directory; the write runs under the dialog (`run`), so a refusal is read where it was pressed.
  const erasesDirectories = admin?.is_operator === true
  // Gated like saveProject's refetch: a superadmin without `worktrees:view` would 403 on this list.
  const refetchRosters = () => Promise.all([
    projectsQuery.refetch(),
    ...(canReadWorktrees ? [worktreesQuery.refetch()] : []),
  ])
  /* An Easy Mode project's app goes with it (`easy_mode_hosting`): its address is the typed gate, and the tenant
     DELETE carries it as `confirm` (422 otherwise). A failed lookup falls back to the name; the server still refuses. */
  async function hostedAppAddress(projectId: number): Promise<string | null> {
    if (!isEnabled('easy_mode_hosting')) return null
    try {
      const apps = await get<{ address: string | null }[]>(`/api/hosted-apps?project_id=${projectId}`)
      return apps.find(a => a.address)?.address ?? null
    } catch {
      return null
    }
  }

  async function deleteProject(p: Project) {
    const address = await hostedAppAddress(p.id)
    const ok = await confirm(danger('Delete', {
      title: 'Delete project',
      identity: address ?? p.name,
      message: `Delete ${p.name}? Its tasks are deleted (running ones stopped) and every slot directory it has, `
        + 'on every node, is erased — uncommitted work included.'
        + (address ? ` Its app at ${address} is deleted too; the repo stays.` : ''),
      typed: { name: address ?? p.name, action: 'deletion' },
      run: {
        // Under `withBusy`, so the row's verbs are withdrawn while the erase runs (as every row write is).
        action: () => withBusy(p.id, async () => {
          try {
            if (erasesDirectories) await post<void>(`/api/projects/${p.id}/erase`)
            else await del(`/api/projects/${p.id}${address ? `?confirm=${encodeURIComponent(address)}` : ''}`)
          } catch (err) {
            await refetchRosters()
            throw err
          }
        }),
        failed: 'Failed to delete project',
        busyLabel: 'Deleting…',
        mapError: (err, fallback) => cascadeFailureText(err) ?? apiDetail(err, fallback),
      },
    }))
    // The row stays busy (its verbs withdrawn) until the roster no longer lists it.
    if (ok) await withBusy(p.id, async () => { await refetchRosters() })
  }

  /* THE ROSTER AS FEED ROWS (`ui/feed/FeedList`): one markup, and the title takes what the trailing
     cluster leaves. FLEET is the DISC; ACTIVE SLOTS is one `active` slot (`—` = not
     knowable); PORT BAND is `meta`-gated; REPO is a ⋯ item with a real `href`; every other verb is in
     the ⋯. */
  /* Org visibility, read once (a hook cannot run in the `.map`); `ui/OrgIndicator.tsx::useOrgVisible`. */
  const orgVisible = useOrgVisible()
  /* Beside an open project the list is narrow: Add goes icon-only so Sort and Add keep one row. */
  const besideDetail = useOpenDetailPath() !== null

  const projectRows: FeedRowSpec[] = sorted.map((p): FeedRowSpec => {
    /* GATED ON `projects:edit`, NOT `projects:view` (AC12): a `projects:view`-only holder sees the row
       but `/projects/{id}` 404s for them. Gates the ⋯'s Edit too. */
    const navigable = canEditProject('projects:edit', p.id)
    // `web_url` derives from free text; React does not block `javascript:` (`lib/safeHref.ts`).
    // Not for a project with no repository, nor for a member of one Jaah manages (`lib/jaahManaged.ts`).
    const repoShown = p.has_repo !== false && !lockedForMember(p, admin)
    const repoHref = repoShown ? safeHref(p.web_url) : undefined
    // Generic when the forge is unknown; the server normally withholds `web_url` then anyway.
    const forgeLabel = p.forge ? FORGE_LABELS[p.forge] : 'Repo'
    const active = activeCount(p.name)
    const busy = busyIds.has(p.id)

    const hidden = isHidden(p)
    const actions: OverflowItem[] = [
      { key: 'hide', label: hidden ? 'Show' : 'Hide', ariaLabel: `${hidden ? 'Show' : 'Hide'} ${p.name}`, onClick: () => toggleHidden(p) },
    ]
    if (repoHref) {
      actions.push({ key: 'repo', icon: '↗', label: forgeLabel, ariaLabel: `Open the ${p.forge ? `${forgeLabel} ` : ''}repo for ${p.name}`, href: repoHref })
    }
    if (navigable) {
      // Named for its project, so four menus are not four bare "Edit"s.
      actions.push(editItem(p.name, () => navigate(`/projects/${p.id}`)))
    }
    const changeOrgItem = changeOrg.menuItem({ id: p.id, name: p.name, org: p.org, href: `/projects/${p.id}` })
    if (changeOrgItem) actions.push(changeOrgItem)
    if (isOrgAdminSeat(admin)) {
      // AN ORG ADMIN ON EITHER PLANE, because the server admits them through TWO doors: the operator's
      // `POST /api/projects/{id}/erase` (`@superadmin_only(Tenancy.OPERATOR)`, `routes/projects.py`), and
      // for a tenant's Org Owner/Admin `DELETE /api/projects/{id}` (`require_project_deleter`, an
      // `org_admin_writer`; its slots then need the slot writer's bar). `projects:delete` alone is refused by both, so it gets no control.
      actions.push(deleteItem(p.name, () => void deleteProject(p)))
    }

    // More than one repository: every one on the row's hover, primary first, so the list never implies one repo.
    const primaryRepo = repoShown && (p.repos?.length ?? 0) > 1 ? p.repos?.find(r => r.is_primary) : undefined
    const counts = projectCountsLabel(active)
    const notes: FeedNote[] = [
      ...(primaryRepo
        ? [{ key: 'repos', tone: 'muted' as const, text: `${countOf(p.repos?.length ?? 0, 'repo')}: ${[primaryRepo, ...(p.repos ?? []).filter(r => r !== primaryRepo)].map(r => r.name).join(', ')}` }]
        : []),
    ]

    return {
      key: String(p.id),
      /* THE EYE, the first column: shown or hidden for this user, and the press that flips it. */
      indicator: {
        tone: 'neutral',
        glyph: hidden ? <EyeOffIcon /> : <EyeIcon />,
        label: hidden ? 'Hidden' : 'Shown',
        onPress: () => toggleHidden(p),
        pressLabel: `${hidden ? 'Show' : 'Hide'} ${p.name}`,
        data: { hidden },
      },
      muted: hidden,
      /* THE PHONE AND A NARROW LIST ABOVE IT (`PROJECTS_NO_ORG`: beside an open project) draw the org after the name,
         on its line (`TITLE_ORG_SX`), its slot off. Ink only: the slot stays the one carrier of the org's
         `data-org-*` (`ui/OrgIndicator.tsx`). */
      title: (
        /* The org rides the hover too (`name · org`): a long name wraps the inline org out of sight, and
           this title wins over the row's own `titleText` hover, which also names the row's controls, so it
           carries the row's notes too. */
        <Tip title={orgVisible ? [`${p.name} · ${orgTitle(p.org)}`, ...notes.map(n => n.text)].join('\n') : undefined}><Box component="span" sx={TITLE_ROW_SX}>
          <Box component="span" data-testid="project-title-fit" sx={TITLE_FIT_SX}>
            <Box component="span" data-testid="project-name" sx={TITLE_NAME_SX}>{p.name}</Box>
            {orgVisible && (
              <Tip title={orgTitle(p.org)}><Box component="span" data-testid="project-org-inline" sx={TITLE_ORG_SX}>
                <OrgIndicator org={p.org} inkOnly />
              </Box></Tip>
            )}
          </Box>
        </Box></Tip>
      ),
      notes: notes.length > 0 ? notes : undefined,
      titleText: p.name,
      href: navigable ? `/projects/${p.id}` : null,
      busy,
      data: { project: p.name },
      slots: [
        /* No port-vars slot: an unlabelled, truncated token; the project page shows and edits them. */
        /* WHICH TENANT OWNS THE ROW (`ui/OrgIndicator.tsx` draws it and prices `ORG_SLOT`). FIRST in the
           tail, beside the title.
           IT RIDES `squeeze`, MEASURED at the old 72px beside a 96px ports slot (control run with
           `[data-testid="col-org"]` hidden), title control -> with slot: 448 274->199, 481 58->0, 1024
           513->435, 1440 929->851. At 481 the title
           goes to ZERO, so it cannot be ungated; `SIDEBAR_SQUEEZE_SX` is the shared band. On top, the list's
           own width (`projectsOrg`): beside an open project the name had 63px, so a narrow list draws the org
           after the name (`TITLE_ORG_SX` above), as the phone does (`projectsOrg`). WITHHELD FROM EVERY NON-OPERATOR (`ui/OrgIndicator.tsx::useOrgVisible`). */
        ...orgSlots(orgVisible, p.org, { gate: 'projectsOrg' }),
        /* Questions waiting, Running, Queued: `—` without `tasks:view` (or before the tree lands), never a false 0.
           A waiting question takes the warning colour: it is the one asking for someone. */
        ...TASK_COLUMNS.map(column => {
          const n = taskCount(p, column.key)
          const words = n === null ? `${column.label} unknown` : countOf(n, column.noun[0], column.noun[1])
          return {
            key: column.key,
            width: column.width,
            gate: column.gate,
            align: 'center' as const,
            mono: true,
            testId: `col-project-${column.key}`,
            title: words,
            node: (
              <>
                <Box component="span" aria-hidden="true" sx={column.key === 'questions' && n ? { color: 'warning.main', fontWeight: FONT_WEIGHT.semibold } : undefined}>
                  {n ?? '—'}
                </Box>
                <Box component="span" sx={SR_ONLY_SX}>{words}</Box>
              </>
            ),
          }
        }),
        /* Off the phone: its width goes to the name; the project page lists the slots. Right of Questions despite the
           meta/id/phone/ungated order: `display: none` leaves no gap, so Questions stays flush with the ⋯. */
        {
          key: 'counts',
          width: 54,
          gate: 'phone',
          align: 'center',
          mono: true,
          testId: 'col-project-counts',
          // `active` is `null` without `worktrees:view`; the words say so rather than implying zero.
          title: counts,
          node: (
            <>
              <Box component="span" aria-hidden="true">
                {active ?? '—'}
              </Box>
              <Box component="span" sx={SR_ONLY_SX}>{counts}</Box>
            </>
          ),
        },
      ],
      actions,
    }
  })

  return (
    /* The list's own width gates the org (`PROJECTS_LIST_CONTAINER`): an open project beside it narrows it. */
    <Box sx={PROJECTS_LIST_CONTAINER_SX}>
      <PageHeader
        testId="projects-page-header"
        actions={
          <>
            <SortMenu noun="projects" columns={SORTABLE_COLUMNS} sort={sort} onSort={toggleSort} />
            {/* ONLY FOR A VIEWER WHO CAN PRESS IT: `POST /api/projects` is `projects:create`. */}
            {/* Add or New by the viewer's Git connections (`hooks/useProjectCreateLabel`). */}
            {/* Beside an open project the narrow list's Add is wordless at every width (`ui/CompactVerb`). */}
            {canCreate && (
              <NewButton label={createLabel} to="/projects/new" wordless={besideDetail} sx={tapTargetSx}
                testId={besideDetail ? 'add-project-compact' : undefined} />
            )}
          </>
        }
      />

      {/* A delete's refusal is the confirm's own (`useConfirm` + `run`): behind its modal this banner would go
          unread. A failed read with no rows says it in `LoadFailedState` below, not twice. */}
      {error && !(loadFailed && !loading && sorted.length === 0) && (
        <Banner severity="error">{error}</Banner>
      )}

      <ListStates
        loading={loading}
        skeleton={{ rows: 5 }}
        failed={loadFailed && sorted.length === 0 && (
          <LoadFailedState
            title="Projects unavailable"
            error={error}
            onRetry={() => { void projectsQuery.refetch(); if (canReadWorktrees) void worktreesQuery.refetch() }}
          />
        )}
        empty={sorted.length === 0}
        noMatch={false}
        /* TWO TITLES: the roster is grant-scoped, so empty can mean "no project" or "none shared with you". */
        emptyTitle={canCreate ? 'No projects yet' : 'No projects visible'}
        emptyScoped
        feed={{
          ariaLabel: 'Projects',
          testId: 'projects-list',
          sections: [{ key: 'projects', rows: projectRows, rail: false }],
          /* The header row is off on the phone, where `SortMenu` carries the order. */
          columns: {
            title: { key: 'name', label: 'Project', sortKey: 'name' },
            slots: [
              ...(orgVisible ? [{ key: 'org', label: 'Org' }] : []),
              ...TASK_COLUMNS.map(c => ({ key: c.key, label: c.label, sortKey: c.key })),
              { key: 'counts', label: 'Slots', ariaLabel: 'Active slots', sortKey: 'active' },
            ],
          },
          sort,
          onSort: (key: string) => toggleSort(key as SortKey),
        }}
      />

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
