import { useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail, del, post } from '../api'
import { useAuth, useCan, useCanOnProject } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { cascadeFailureText } from '../lib/erase'
import { safeHref } from '../lib/safeHref'
import { FORGE_LABELS, type Command, type Project } from '../types'
import { CompactVerbLabel, compactVerbProps } from '../ui/CompactVerb'
import Banner from '../ui/Banner'
import ListStates from '../ui/feed/ListStates'
import LoadFailedState from '../ui/LoadFailedState'
import type { FeedNote, FeedRowSpec, OverflowItem } from '../ui/feed/types'
import { deleteItem, editItem } from '../components/tasks/rowActions'
import { useOpenDetailPath } from '../components/ListDetail'
import { ABOVE_PHONE, PROJECTS_LIST_CONTAINER_SX, PROJECTS_NO_ORG, SR_ONLY_SX, tapTargetSx } from '../ui/listStyles'
import { countOf } from '../lib/humanize'
import { OrgIndicator, orgSlots, orgTitle, useOrgVisible } from '../ui/OrgIndicator'
import PageHeader from '../ui/PageHeader'
import SortMenu from '../ui/SortMenu'
import { SystemChip } from '../ui/MarkChip'
import { useChangeOrgAction } from '../components/ChangeOrgDialog'
import { useConfirm } from '../components/ConfirmDialog'
import { useRowActions } from '../hooks/useBusyIds'
import { useProjects, useWorktrees } from '../hooks/useRosters'

type SortKey = 'name' | 'active'

/* THE TITLE AS NAME + ORG ON ONE LINE. The org is drawn only on a narrow list above the phone
   (`PROJECTS_NO_ORG`, beside an open project), where its slot is off. THE NAME WINS: at a 1024px window
   a full-width org cut the names to "org-a-pro…". The two WRAP inside a one-line clip (`TITLE_FIT_SX`): the org
   stays on the name's line only while the whole name and a few of its letters fit there, else it wraps onto the
   clipped line and the name keeps the row. On the line it grows into what the name leaves, up to its own width,
   ellipsised (its hover names it). */
const TITLE_ROW_SX = { display: 'inline-flex', alignItems: 'center', gap: 0.75, maxWidth: '100%', minWidth: 0 } as const
const TITLE_FIT_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 0.75,
  flex: '0 1 auto',
  minWidth: 0,
  // The name's own line box (`ui/feed/FeedList.tsx` TITLE_SX: line-height 1.3): a wrapped org is never seen.
  maxHeight: '1.3em',
  overflow: 'hidden',
} as const
const TITLE_NAME_SX = { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const
const TITLE_ORG_SX = {
  display: 'none',
  // Placed on the name's line at its floor, then grown up to its whole name.
  flex: '1 1 3.5em',
  maxWidth: 'max-content',
  minWidth: 0,
  overflow: 'hidden',
  fontSize: '0.6875rem',
  [ABOVE_PHONE]: { [PROJECTS_NO_ORG]: { display: 'inline-flex' } },
} as const

/** The active-slot count in words: the slot's hover and its accessible name. `null` active = not visible to
 *  this viewer (no `worktrees:view`). */
export function projectCountsLabel(active: number | null): string {
  return active === null ? 'Active slots hidden' : `${active} active slot${active === 1 ? '' : 's'}`
}
type SortDir = 'asc' | 'desc'

/* The two sortable keys, each a fact on the row. Read by `ui/SortMenu`. */
const SORTABLE_COLUMNS: { key: SortKey; label: string }[] = [
  { key: 'name', label: 'Project' },
  { key: 'active', label: 'Active WTs' },
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
  const { admin } = useAuth()
  const changeOrg = useChangeOrgAction('project')
  const worktreesQuery = useWorktrees({ enabled: canReadWorktrees })
  const projects = projectsQuery.data ?? []
  const worktrees = worktreesQuery.data ?? []
  // `canReadWorktrees &&` is belt-and-braces here (a disabled query is not loading); explicit so a
  // future upgrade cannot make the page wait on a query it does not run.
  const loading = projectsQuery.isLoading || (canReadWorktrees && worktreesQuery.isLoading)
  // Delete failures share the banner but surface only once the load has succeeded.
  const { busy: busyIds, withBusy, run, error: actionError, setError: setActionError } = useRowActions<number>()
  // A NON-ERROR notice for `Run Check` (a ⋯ item has no cell to report into).
  const [notice, setNotice] = useState('')
  // `canReadWorktrees &&` IS LOAD-BEARING HERE: disabling a query does not clear an error it already has
  // (a warm reload fires it from the persisted permission snapshot, 403s, then disables it).
  const loadFailed = projectsQuery.isError || (canReadWorktrees && worktreesQuery.isError)
  const error = loadFailed ? 'Failed to load projects' : actionError
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'name', dir: 'asc' })
  const [confirm, confirmDialog] = useConfirm()

  // `null` = not knowable (no `worktrees:view`), rendered as an em dash; 0 would be a lie.
  function activeCount(name: string): number | null {
    if (!canReadWorktrees) return null
    return worktrees.filter(w => w.project === name && w.state !== 'free').length
  }

  function toggleSort(key: SortKey) {
    setSort(s => s.key === key ? { key, dir: s.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'asc' })
  }

  const sorted = [...projects].sort((a, b) => {
    let av: string | number | null
    let bv: string | number | null
    if (sort.key === 'active') {
      // `?? -1`: an unknowable count sorts below zero (`null` would not type-check). Constant per render,
      // so the stable sort keeps server order.
      av = activeCount(a.name) ?? -1
      bv = activeCount(b.name) ?? -1
    } else {
      av = a.name
      bv = b.name
    }
    if (av < bv) return sort.dir === 'asc' ? -1 : 1
    if (av > bv) return sort.dir === 'asc' ? 1 : -1
    return 0
  })

  /* RUN CHECK, as the ⋯ fires it: a second spelling of `components/CommandButton.tsx::useCommand`'s reply
     read (a command fails unless the node says `done`; `result.reason` says why). This is the THIRD
     caller `components/WorktreePool.tsx::runSlotCommand` names — an open debt to move the read beside
     the hook. `run` makes the ⋯ inert meanwhile. */
  function runCheck(p: Project) {
    return run(p.id, `Could not run check on ${p.name}`, async () => {
      setNotice('')
      const cmd = await post<Command>('/api/commands', { command_type: 'check', args: { project_id: p.id } })
      if (cmd.status === 'done') {
        setNotice(`Check ran on ${p.name} — its output is in Audit > Nodes.`)
        return
      }
      const reason = cmd.result && typeof cmd.result['reason'] === 'string' ? cmd.result['reason'] : cmd.status
      setActionError(`Check on ${p.name} — ${reason}`)
    })
  }

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
  async function deleteProject(p: Project) {
    const ok = await confirm({
      title: 'Delete project',
      identity: p.name,
      message: `Delete ${p.name}? Its tasks are deleted (running ones stopped) and every slot directory it has, `
        + 'on every node, is erased — uncommitted work included.',
      confirmLabel: 'Delete',
      destructive: true,
      typed: { name: p.name, action: 'deletion' },
      run: {
        // Under `withBusy`, so the row's verbs are withdrawn while the erase runs (as every row write is).
        action: () => withBusy(p.id, async () => {
          try {
            if (erasesDirectories) await post<void>(`/api/projects/${p.id}/erase`)
            else await del(`/api/projects/${p.id}`)
          } catch (err) {
            await refetchRosters()
            throw err
          }
        }),
        failed: 'Failed to delete project',
        busyLabel: 'Deleting…',
        mapError: (err, fallback) => cascadeFailureText(err) ?? apiDetail(err, fallback),
      },
    })
    // The row stays busy (its verbs withdrawn) until the roster no longer lists it.
    if (ok) await withBusy(p.id, async () => { await refetchRosters() })
  }

  /* THE ROSTER AS FEED ROWS (`ui/feed/FeedList`): one markup, and the title takes what the trailing
     cluster leaves. FLEET is the DISC; ACTIVE WTS is one `active` slot (`—` = not
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
    // Not for a project with no repository.
    const repoHref = p.has_repo === false ? undefined : safeHref(p.web_url)
    // Generic when the forge is unknown; the server normally withholds `web_url` then anyway.
    const forgeLabel = p.forge ? FORGE_LABELS[p.forge] : 'Repo'
    const active = activeCount(p.name)
    const busy = busyIds.has(p.id)

    const actions: OverflowItem[] = [
      { key: 'check', icon: '✓', label: 'Check', ariaLabel: `Run Check on ${p.name}`, onClick: () => void runCheck(p) },
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
    const primaryRepo = p.has_repo !== false && (p.repos?.length ?? 0) > 1 ? p.repos?.find(r => r.is_primary) : undefined
    const counts = projectCountsLabel(active)
    const notes: FeedNote[] = [
      ...(primaryRepo
        ? [{ key: 'repos', tone: 'muted' as const, text: `${countOf(p.repos?.length ?? 0, 'repo')}: ${[primaryRepo, ...(p.repos ?? []).filter(r => r !== primaryRepo)].map(r => r.name).join(', ')}` }]
        : []),
    ]

    return {
      key: String(p.id),
      /* A NARROW LIST ABOVE THE PHONE (`PROJECTS_NO_ORG`: beside an open project) draws the org after the name,
         on its line (`TITLE_ORG_SX`), its slot off. Ink only: the slot stays the one carrier of the org's
         `data-org-*` (`ui/OrgIndicator.tsx`). */
      title: (
        /* The org rides the hover too (`name · org`): a long name wraps the inline org out of sight, and
           this title wins over the row's own `titleText` hover, which also names the row's controls, so it
           carries the row's notes too. */
        <Box component="span" sx={TITLE_ROW_SX} title={orgVisible ? [`${p.name} · ${orgTitle(p.org)}`, ...notes.map(n => n.text)].join('\n') : undefined}>
          <Box component="span" data-testid="project-title-fit" sx={TITLE_FIT_SX}>
            <Box component="span" data-testid="project-name" sx={TITLE_NAME_SX}>{p.name}</Box>
            {orgVisible && (
              <Box component="span" data-testid="project-org-inline" title={orgTitle(p.org)} sx={TITLE_ORG_SX}>
                <OrgIndicator org={p.org} inkOnly />
              </Box>
            )}
          </Box>
          {p.system_kind && <SystemChip />}
        </Box>
      ),
      notes: notes.length > 0 ? notes : undefined,
      titleText: p.name,
      href: navigable ? `/projects/${p.id}` : undefined,
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
           after the name (`TITLE_ORG_SX` above). WITHHELD FROM EVERY NON-OPERATOR (`ui/OrgIndicator.tsx::useOrgVisible`). */
        ...orgSlots(orgVisible, p.org, { gate: 'projectsOrg' }),
        {
          key: 'counts',
          width: 54,
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
            {/* Beside an open project the narrow list's Add is wordless at every width (`ui/CompactVerb`). */}
            {canCreate && (
              <Button
                component={RouterLink}
                to="/projects/new"
                variant="contained"
                color="primary"
                data-testid={besideDetail ? 'add-project-compact' : undefined}
                {...compactVerbProps('Add project', tapTargetSx, besideDetail)}
              >
                <CompactVerbLabel label="Add project" wordless={besideDetail} />
              </Button>
            )}
          </>
        }
      />

      {/* A delete's refusal is the confirm's own (`useConfirm` + `run`): behind its modal this banner would go
          unread. A failed read with no rows says it in `LoadFailedState` below, not twice. */}
      {error && !(loadFailed && !loading && sorted.length === 0) && (
        <Banner severity="error">{error}</Banner>
      )}

      {/* The ⋯'s Check outcome, until dismissed or replaced. */}
      {notice && (
        <Banner severity="info" testId="project-notice" onDismiss={() => setNotice('')}>{notice}</Banner>
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
        feed={{ ariaLabel: 'Projects', testId: 'projects-list', sections: [{ key: 'projects', rows: projectRows }] }}
      />

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
