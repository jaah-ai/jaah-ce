// The Task page's create surface (`/tasks/new`, `/tasks/new-chat`): the same shell and form as edit, ONE tab.
import { useState, type MutableRefObject, type ReactNode } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { ApiError, apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { TabPanel, Tabs } from '../../components/Tabs'
import TaskEditForm from '../../components/tasks/TaskEditForm'
import type { useTaskCreateDefaults } from '../../components/tasks/taskDefaults'
import { PANE_QUERY_KEY } from '../../components/tasks/pane/queryKeys'
import { personaLabel } from '../../components/tasks/personaRegistry'
import { stripAttachmentImage } from '../../components/tasks/TaskSpecFields'
import { DEFAULT_PERSONA } from '../../components/tasks/taskPersona'
import type { TaskPrefill } from '../../components/tasks/taskPrefill'
import type { PipelineTask } from '../../types'
import { deadTokensIn, inlineAttachmentTokens, type AttachmentCopies } from '../../lib/taskDuplicate'
import { editionModule } from '../../lib/editionModule'
import { isEnabled } from '../../lib/editionFeatures'
import { savedTaskReturn } from '../../lib/taskReturn'
import { clearTaskDraft, inlineTaskDraftStorageKey } from '../../lib/taskDraftStorage'
import { queryClient } from '../../queryClient'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import { OrgAside } from '../../ui/OrgIndicator'
import { OrgScopeInline } from '../../ui/OrgScopeControl'
import { duplicateBody } from './duplicateSeed'
import { PHONE } from '../../ui/listStyles'
import { CREATE_CHAT_TABS, CREATE_TABS, PAGE_SX } from './taskDetailConstants'
import type { TaskDetailFiling } from './useTaskDetailFiling'
import type { TaskDetailRead } from './useTaskDetailLoad'
import { readTicketSeedParam } from './useTaskDetailNavState'
import { useCreateLiveCast } from '../../lib/liveCastSurface'

/** What the create page asks of a support ticket; `pages/support/ticketTaskSeed.ts` satisfies it. */
type SeedTicket = { id: number; title: string; description: string }
export type TicketTaskSeed = {
  getTicket: (id: string) => Promise<SeedTicket>
  linkTicketTask: (ticketId: number, taskId: number) => Promise<unknown>
  ticketPath: (id: number) => string
  ticketTaskBody: (ticket: SeedTicket, origin: string) => string
  /** The ticket's files as staged copies; the body is the seeded Description (which images it shows inline). */
  loadTicketFiles: (ticketId: number, body: string) => Promise<AttachmentCopies>
}
/* The ticket seam, absent where the edition has no Help surface: `?ticket=` is then ignored (`lib/editionModule.ts`). */
const TICKETS: TicketTaskSeed | undefined = editionModule(isEnabled('support_assistant'),
  () => import.meta.glob<TicketTaskSeed>('../support/ticketTaskSeed.ts', { eager: true, import: 'ticketTaskSeed' }))

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
  /* ── `?ticket=<id>`: the ticket page's Create task (operator only, as that page offers it) ──────────────
     Captured once at mount, like the other seeds. The ticket is read through the ticket page's own query (one
     cache entry), and the form waits for it: it seeds in `useState` initialisers, so a late body is ignored. */
  const [searchParams] = useSearchParams()
  const [ticketId] = useState(() => (isChat || TICKETS === undefined ? null : readTicketSeedParam(searchParams)))
  const ticketRead = useQuery({
    queryKey: ['support-ticket', String(ticketId)],
    queryFn: () => TICKETS!.getTicket(String(ticketId)),
    enabled: ticketId !== null,
    retry: false,
  })
  const ticket = ticketRead.data
  const ticketBodyRaw = ticket ? TICKETS!.ticketTaskBody(ticket, window.location.origin) : null
  /* The ticket's files, copied BEFORE the form mounts (it seeds once). Never an error: what did not come across is a
     warning over a working form, as a duplicate's is. */
  const filesRead = useQuery({
    queryKey: ['support-ticket-files', String(ticketId)],
    // Never rejects: an unexpected throw reads as an unlisted file set (a warning), not an endless spinner.
    queryFn: () => TICKETS!.loadTicketFiles(ticket!.id, ticketBodyRaw!)
      .catch((): AttachmentCopies => ({ inlineImages: new Map(), files: [], unavailable: [], attachmentsUnknown: true })),
    enabled: ticket !== undefined,
    retry: false,
    staleTime: Infinity,
    gcTime: 0,
    // `File`s and a `Map` do not survive the persisted cache's JSON (`queryClient.ts::shouldPersistQuery`).
    meta: { persist: false },
  })
  const ticketFiles = filesRead.data
  // Task created, link refused: the create stands (the old support route left the same unlinked row).
  const [linkError, setLinkError] = useState('')
  const [filesWarningDismissed, setFilesWarningDismissed] = useState(false)
  if (ticketId !== null && (ticket === undefined || ticketFiles === undefined)) {
    return (
      <DetailStatus
        trail={taskCrumbs}
        loading={!ticketRead.isError || ticketRead.isFetching}
        failedTitle="Ticket unavailable"
        error={apiDetail(ticketRead.error, 'Could not load the ticket.')}
        onRetry={() => { void ticketRead.refetch() }}
      />
    )
  }
  /* The ticket as it reads NOW, as a regular unattended task (operator decision; the axes still offer the rest).
     It outranks a `?persona=` seed: this link names one target. An image whose bytes did not come across is
     stripped (every one, when the list itself could not be read), so the task never shows a permanent
     `⚠ image unavailable`. */
  const ticketBody = ticketBodyRaw !== null && ticketFiles !== undefined
    ? (ticketFiles.attachmentsUnknown ? inlineAttachmentTokens(ticketBodyRaw) : deadTokensIn(ticketBodyRaw, ticketFiles.unavailable))
        .reduce(stripAttachmentImage, ticketBodyRaw)
    : null
  const seed: TaskPrefill | undefined = ticketBody !== null
    ? {
        ...prefill,
        persona: { value: DEFAULT_PERSONA, label: personaLabel(DEFAULT_PERSONA) },
        autonomous: true,
        body: ticketBody,
        staged: { inlineImages: ticketFiles!.inlineImages, files: ticketFiles!.files.map(entry => entry.file) },
      }
    : prefill ?? undefined
  const ticketFilesWarning = !ticketFiles ? ''
    : ticketFiles.attachmentsUnknown ? 'Ticket files could not be listed; none are included.'
    : ticketFiles.unavailable.length > 0
      ? `Not included (could not be read): ${ticketFiles.unavailable.join(', ')}.`
      : ''

  /* After a ticket create: link it, then land on the ticket so the operator sees the link. A refused link stays
     here with the reason; Save again PUTs the same row (`createdTask`) and retries the link. */
  async function linkToTicket(created: PipelineTask, id: number) {
    setLinkError('')
    try {
      await TICKETS!.linkTicketTask(id, created.id)
    } catch (err) {
      // 409 on a retry: the first link landed and only its answer was lost, so it is linked.
      if (!(err instanceof ApiError && err.status === 409)) {
        setLinkError(`Task #${created.id} created, not linked to the ticket: ${apiDetail(err, 'link refused')}. Save to retry.`)
        return
      }
    }
    void queryClient.invalidateQueries({ queryKey: ['support-ticket', String(id)] })
    navigate(TICKETS!.ticketPath(id), { replace: true })
  }

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
        sx={{ [PHONE]: { display: 'none' } }}
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
          {ticketFilesWarning && !filesWarningDismissed && (
            <Banner severity="warning" testId="task-page-ticket-files-warning" onDismiss={() => setFilesWarningDismissed(true)}>
              {ticketFilesWarning}
            </Banner>
          )}
          {linkError && (
            <Banner severity="error" testId="task-page-ticket-link-error" onDismiss={() => setLinkError('')}>
              {linkError}
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
            initialBody={duplicateSeed ? duplicateBody(duplicateSeed) : ticketBody ?? prefilledBody}
            duplicateSeed={duplicateSeed}
            // The `?persona=&project_id=` seed (`components/tasks/taskPrefill.ts`), or the ticket's; `undefined` when none.
            prefill={seed}
            // The operator's create defaults, handed over once resolved (`undefined` = nothing yet).
            createDefaults={taskCreateDefaultsLoaded ? taskCreateDefaults : undefined}
            /* The lifted props on create too: without `projectIdDraft` the POST would silently use a
               different project from the one the trail names. */
            projectIdDraft={draftProjectId ?? undefined}
            featureIdDraft={draftFeatureId}
            onDirtyChange={setFormDirty}
            onFiled={liveCast.onFiled}
            /* Save and Go both CLOSE the form (no longer "open the saved task"): the board
               the author came from, its scope kept, the new row revealed on its stage (`lib/taskReturn.ts`) —
               what Save and Go do on an existing task (`useTaskDetailActions::backToList`).
               `replace` so Back skips `/tasks/new`. The pane's cached rows predate the create and are persisted, so
               they are invalidated: the board shows the new row. */
            onSaved={created => {
              /* Every create spends the board's inline create row (idempotent with ⤢'s own discard in
                 `components/tasks/useInlineTaskCreate`). The key is this admin's, per
                 `lib/taskDraftStorage` — never `clearAllTaskDrafts`, which would empty others' buckets. */
              clearTaskDraft(inlineTaskDraftStorageKey(admin?.id ?? null))
              const { path, state } = savedTaskReturn(created, { created: true })
              void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
              if (ticket) {
                void linkToTicket(created, ticket.id)
                return
              }
              navigate(path, { replace: true, state })
            }}
            onCancel={() => navigate('/tasks')}
          />
        </DetailSection>
      </TabPanel>

    </DetailPage>
  )
}
