// THE TASK PAGE'S LOADING SHELL, single-homed: the page's own loading branch
// (`TaskDetailPage.tsx`) and the shell's `<main>` and task pane while the page's chunk downloads (`taskPageFallback`,
// below) draw this one, so a row pressed on the board opens on the same header-and-tabs skeleton the page then keeps —
// never a bare spinner swapped for it.
//
// IN THE EAGER BUNDLE (App.tsx imports it): its imports stay the shell's own weight — MUI `Box`/`Skeleton`, the
// shared `DetailPage`/`LoadingState`/`Breadcrumb` primitives (already eager via `RouteTabLayout`/`SupportChat`), react-router, and
// `taskDetailConstants` (style objects only). Never the page or its hooks: those are `loadTaskDetailPage`'s chunk.
import type { ReactNode } from 'react'
import { matchPath } from 'react-router-dom'
import Box from '@mui/material/Box'
import Skeleton from '@mui/material/Skeleton'
import Breadcrumb from '../../components/Breadcrumb'
import LoadingState from '../../ui/LoadingState'
import { DetailPage } from '../../ui/detail/DetailPage'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import { LOADING_ACTIONS_SX, LOADING_TAB_WIDTHS, LOADING_TABS_SX, PAGE_SX, TITLE_SKELETON_SX } from './taskDetailConstants'

/* The trail when nothing about the row is known yet: "Tasks › ▭", the id the `<h1>`'s accessible name, so
   the heading never changes under a screen reader when the title lands. */
export function TaskTrailSkeleton({ id }: { id: string | undefined }) {
  return (
    <Breadcrumb
      items={[{ label: 'Tasks', to: '/tasks' }, {
        label: (
          <>
            <Box component="span" sx={VISUALLY_HIDDEN}>{id}</Box>
            <Skeleton variant="text" aria-hidden data-testid="task-page-title-skeleton" sx={TITLE_SKELETON_SX} />
          </>
        ),
      }]}
      currentComponent="h1"
    />
  )
}

/* The header (the trail, the cluster's place), a tab-strip skeleton and a skeleton body, in the boxes and `PAGE_SX` the
   loaded page uses (`LOADING_TABS_SX`), under their own `-loading` ids, so the page lands without a shift. The body
   keeps the console's one `loading-state`. */
export default function TaskPageSkeleton({ trail }: { trail: ReactNode }) {
  return (
    <DetailPage
      measure="fill"
      testId="task-page-loading"
      headerTestId="task-page-header-loading"
      breadcrumb={trail}
      actions={<Skeleton variant="rounded" aria-hidden data-testid="task-page-actions-skeleton" sx={LOADING_ACTIONS_SX} />}
      sx={PAGE_SX}
    >
      <Box data-testid="task-page-tabs-loading" aria-hidden sx={LOADING_TABS_SX}>
        {LOADING_TAB_WIDTHS.map((width, i) => <Skeleton key={i} variant="text" sx={{ width, flex: '0 0 auto' }} />)}
      </Box>
      <Box data-testid="task-page-loading-body" sx={{ flex: '1 1 auto', minHeight: 0 }}>
        <LoadingState rows={6} />
      </Box>
    </DetailPage>
  )
}

/* THE TASK PAGE'S FALLBACK: a saved task (`/tasks/<id>`) opened beside the board or loaded full page in
   `<main>` draws this skeleton while `loadTaskDetailPage`'s chunk downloads, the shell the page itself then
   keeps while its read lands. `undefined` for every other path — `/tasks/new` included, whose page draws the form's
   plain status, never this skeleton — so the caller's `ShellFallback` stands. */
export function taskPageFallback(pathname: string | undefined): ReactNode {
  const id = pathname ? matchPath('/tasks/:id', pathname)?.params.id : undefined
  if (id === undefined || !/^[1-9][0-9]*$/.test(id)) return undefined
  return <TaskPageSkeleton trail={<TaskTrailSkeleton id={id} />} />
}
