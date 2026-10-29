// The Task page's create surface (`/tasks/new`, `/tasks/new-chat`): the same shell and form as edit, ONE tab.
import type { MutableRefObject, ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { useAuth } from '../../auth'
import { TabPanel, Tabs } from '../../components/Tabs'
import TaskEditForm from '../../components/tasks/TaskEditForm'
import type { useTaskCreateDefaults } from '../../components/tasks/taskDefaults'
import { PANE_QUERY_KEY } from '../../components/tasks/pane/queryKeys'
import { lastBoardScope } from '../../lib/boardScope'
import { taskDeepLinkPath } from '../../lib/taskDeepLink'
import { clearTaskDraft, inlineTaskDraftStorageKey } from '../../lib/taskDraftStorage'
import { queryClient } from '../../queryClient'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { OrgAside } from '../../ui/OrgIndicator'
import { OrgScopeInline } from '../../ui/OrgScopeControl'
import { duplicateBody } from './duplicateSeed'
import { CREATE_CHAT_TABS, CREATE_TABS, PAGE_SX, PHONE } from './taskDetailConstants'
import type { TaskDetailFiling } from './useTaskDetailFiling'
import type { TaskDetailRead } from './useTaskDetailLoad'
import { useCreateLiveCast } from '../../lib/liveCastSurface'

export default function TaskCreateView({ pageRef, taskCrumbs, isChat, prefilledBody, createDefaults, read, filing }: {
  pageRef: MutableRefObject<HTMLDivElement | null>
  taskCrumbs: ReactNode
  isChat: boolean
  /** The inline create row's typed text, via navigation state. */
  prefilledBody: string
  createDefaults: ReturnType<typeof useTaskCreateDefaults>
  read: TaskDetailRead
  filing: TaskDetailFiling
}) {
  const navigate = useNavigate()
  const { admin } = useAuth()
  const { duplicateSeed, duplicateWarning, setDuplicateWarning, setFormDirty } = read
  const { pickerProjects, prefill, draftProjectId, draftFeatureId } = filing
  const liveCast = useCreateLiveCast(draftProjectId, read.projects)
  const { defaults: taskCreateDefaults, loaded: taskCreateDefaultsLoaded } = createDefaults
  return (
    /* No `title`: the crumb is the `<h1>`. `headerTestId` is separate because `PAGE_SX` pins the header
       from outside and `e2e/page-header-mobile.spec.ts` locates the `<h1>` through it. */
    <DetailPage
      ref={pageRef}
      measure="fill"
      testId="task-page"
      headerTestId="task-page-header"
      breadcrumb={taskCrumbs}
      /* The VIEWER's org, labelled "Your org": there is no record yet, so `admin.org` names the operator,
         not a promise about the row (an unlabelled chip would be the confident-wrong-tenant failure
         `ui/OrgIndicator.tsx` refuses). `OrgAside` draws for an operator only and fails closed
         on a signed-out render. `titleAside` because it is part of the heading's "what you are looking
         at" and costs NO row — a taller header would break `e2e/task-detail-fit.spec.ts`'s fill.
         `hideOnPhone`: mobile web must not show the org name at all. */
      titleAside={(
        <>
          <OrgAside
            label="Your org"
            org={admin?.org}
            testId="task-page-org"
            chipTestId="task-page-org-chip"
            hideOnPhone
          />
          {/* The phone's org-scope chip, on the crumb row where it fits; the shell's bar steps aside. */}
          <OrgScopeInline />
        </>
      )}
      // The task page's spot (`TaskDetailHeaderActions`).
      actions={liveCast.control}
      sx={PAGE_SX}
    >
      {/* A single-item strip so create and edit read as one surface — hidden on a phone, where one tab
          under an `<h1>` saying "New Task" is 45px of pure restatement. */}
      <Box
        data-testid="task-page-tabs"
        sx={{ [`@media (max-width:${PHONE - 1}px)`]: { display: 'none' } }}
      >
        <Tabs
          tabs={isChat ? CREATE_CHAT_TABS : CREATE_TABS}
          tab="task"
          onSelect={() => { /* one tab — nothing to select */ }}
          label="Task sections"
          idPrefix="task"
        />
      </Box>

      <TabPanel id="task" current="task" idPrefix="task">
        {/* The same wrapper as edit, so both render at the same width. */}
        <DetailSection testId="task-page-panel" fill>
          {/* What the copy could not bring: a dismissible warning over a working form. */}
          {duplicateWarning && (
            <Banner
              severity="warning"
              testId="task-page-duplicate-warning"
              onDismiss={() => setDuplicateWarning('')}
            >
              {duplicateWarning}
            </Banner>
          )}
          <TaskEditForm
            mode="create"
            /* Which create surface. `mode` stays `"create"`; the form's `chat` prop owns the D3 fence
               keeping it out of edit mode. */
            chat={isChat}
            // Explicit: `readOnly` is required so no caller defaults into a writable panel. Create is
            // gated by `App.tsx`'s `/tasks/new` RequireFeature (`tasks:create`), with no type-level link.
            readOnly={false}
            projects={pickerProjects}
            // The inline row's typed text, or a duplicate's body — one body channel, never a seed field.
            // `body` is optional on `TaskOut`, hence the empty fallback.
            initialBody={duplicateSeed ? duplicateBody(duplicateSeed) : prefilledBody}
            duplicateSeed={duplicateSeed}
            // The `?persona=&project_id=` seed (`components/tasks/taskPrefill.ts`); `undefined` when none survived.
            prefill={prefill ?? undefined}
            // The operator's create defaults, handed over once resolved (`undefined` = nothing yet).
            createDefaults={taskCreateDefaultsLoaded ? taskCreateDefaults : undefined}
            /* The lifted props on create too: without `projectIdDraft` the POST would silently use a
               different project from the one the trail names. */
            projectIdDraft={draftProjectId ?? undefined}
            featureIdDraft={draftFeatureId}
            onDirtyChange={setFormDirty}
            onFiled={liveCast.onFiled}
            /* Save and Go both CLOSE the form (no longer "open the saved task"): the board
               the author came from, its scope kept (`lib/boardScope.ts::lastBoardScope`), the new row revealed
               (`taskDeepLinkPath`) — what Save and Go do on an existing task (`useTaskDetailActions::backToList`).
               `replace` so Back skips `/tasks/new`. The pane's cached rows predate the create and are persisted, so
               they are invalidated: the board shows the new row. */
            onSaved={created => {
              /* Every create spends the board's inline create row (idempotent with ⤢'s own discard in
                 `components/tasks/useInlineTaskCreate`). The key is this admin's, per
                 `lib/taskDraftStorage` — never `clearAllTaskDrafts`, which would empty others' buckets. */
              clearTaskDraft(inlineTaskDraftStorageKey(admin?.id ?? null))
              void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
              navigate(taskDeepLinkPath(created.id, created.project_id, lastBoardScope()), { replace: true })
            }}
            onCancel={() => navigate('/tasks')}
          />
        </DetailSection>
      </TabPanel>

    </DetailPage>
  )
}
