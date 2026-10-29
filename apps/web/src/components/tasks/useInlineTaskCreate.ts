/* The ONE HOME for the inline one-line create's WRITES — state, resolved target project, and the two-POST
 * "save and start", for both launches: a task (▶) and an advisor chat (💬). The `/tasks` board's create row
 * (`FeedNewTaskRow`) is its surface; a copy would diverge (one omitting `persona` and silently taking the
 * server's default). Callers own only markup. Renders nothing.
 */
import { type FormEvent, type KeyboardEvent, type MutableRefObject, type ReactNode, useCallback, useEffect, useState } from 'react'
import { apiDetail, post } from '../../api'
import { useAuth, useCanOnProject } from '../../auth'
import { useTabOrg } from '../../lib/activeOrg'
import { enterActAs, useActingAs } from '../../lib/actAs'
import {
  CHAT_LAUNCH_TITLE,
  INLINE_TASK_PREFIXES,
  type InlineTaskPrefix,
  joinInlinePrefix,
  readInlinePrefixLine,
} from './inlineTaskPrefix'
import {
  clearPendingInlineCreate,
  clearTaskDraft,
  inlineTaskDraftStorageKey,
  loadInlineTaskDraft,
  loadPendingInlineCreate,
  saveInlineTaskDraft,
  savePendingInlineCreate,
  armPendingInlineCreate,
  TASK_DRAFT_CLEARED,
} from '../../lib/taskDraftStorage'
import type { OrgRef, PipelineTask, Project } from '../../types'
import type { FilingTarget } from './featureFiling'
import { CONTROLS, startBody } from './controls'
import { usePushIdentity } from './PushIdentityDialog'
import {
  type CreatableProject, type CreateScopeFeature, actAsNeededFor, createRefusal, refusedInOrg, resolveCreateTarget,
} from './shared'
import { useFocusedProjectId } from '../../hooks/useFocusedProjectId'
import { useTaskCreateDefaults, withProjectPersona } from './taskDefaults'
import { ADVISOR_PERSONA, DEFAULT_PERSONA } from './taskPersona'
import { TASK_INVOLVEMENTS, type TaskInvolvement, involvementForTask } from './TaskAxisControls'

/* THE EXPAND CONTROL'S WORDS, shared by both surfaces. Avoids "new task" (the toolbar's create button's name).
 * The tooltip advertises the double-press gesture to MOUSE users only — a `title` never reaches touch, so the
 * phone half is unadvertised (the ⤢ button does the same thing). A spoken home would need this row's
 * `aria-describedby`, already used by the prefix chip's hint. */
export const EXPAND_LABEL = 'Open the full editor'
export const EXPAND_TITLE = 'Open the full editor — or double-click the row'

// The target is decided by `resolveCreateTarget` and NAMED on the row, which offers a picker to change it: the
// board's feature scope files into that feature, a project (or its "Other") scope into that project with no
// feature, All projects into the ladder's default project (the viewer's own org first) with no feature. The
// pick (`pickedProjectId`) outranks the ladder. `null` = nothing creatable: the surface offers no row at all.
export type InlineTaskCreateInput = {
  // `GET /api/projects`: seeing a project is not being able to file into it, so every rung is further
  // narrowed by `tasks:create` (see `resolveCreateProject`).
  projects: Array<CreatableProject & Pick<Project, 'name' | 'system_kind' | 'default_persona'> & { org?: OrgRef | null }>
  // Did `GET /api/projects` ANSWER? `[]` is first paint, a failed fetch AND a new org with no project;
  // only this tells the last apart.
  rosterAnswered: boolean
  // Did that read FAIL? Unanswered and not failed is still loading: the row says so, never "unavailable".
  rosterFailed?: boolean
  // The project the board shows (`scopeProjectId`), the ladder's first rung; null for All projects.
  boardProjectId: number | null
  // The feature the board is scoped to (a rail feature node), filed into when the ladder lands in ITS
  // project; elsewhere the row is filed with no feature, since the API refuses a mismatched pair.
  // A SHIPPED one takes no new task: the row files to its project (`resolveCreateTarget`).
  feature?: CreateScopeFeature
  // The created task, already started. The host merges/refetches; the surface only clears itself.
  onCreated: (created: PipelineTask) => void
  /* May the 💬 exist — `tasks:create` + `worktrees:edit`, single-homed on `useCanCreateChats`
     (`hooks/useTaskAccess.ts`). A PROP so it cannot drift from the toolbar's New Chat. DEFAULTS FALSE. */
  canChat?: boolean
  /* "Take these words to the full editor" — the host navigates. The hook owns it because handing over is a
     MOVE: the stored draft must be discarded in the same gesture, or the sentence stays in the create row
     one Start away from filing twice. */
  onExpand: (body: string, target: FilingTarget | null) => void
  /** The operator's pick on the row's project picker, held by the host so ＋ and ⤢ carry it too; null for none. */
  pickedProjectId?: number | null
  onPickProject?: (id: number) => void
  /** Where the row would file NOW, written for the host's ＋, which must open the editor on the project the row
   *  names (the ladder's answer, not only a hand pick). */
  targetRef?: MutableRefObject<FilingTarget | null>
}

export type InlineTaskCreate = {
  /** The typed words WITHOUT the chipped prefix (the stored line keeps it; see `./inlineTaskPrefix`). */
  text: string
  setText: (next: string) => void
  /** `text.trim()` — the POSTed body and what "open the full editor" receives. Never includes the prefix. */
  trimmed: string
  submitting: boolean
  error: string
  /** The org of the project a create was just refused in for acting as another org (`ActAsToCreate`). */
  refusedOrg: OrgRef | null
  /** The resolved target project, null when there is nothing creatable at all. */
  projectId: number | null
  /** The resolved feature, null for none (the project itself). */
  featureId: string | null
  /** The resolved project's row — what the row names — or null with `projectId`. */
  targetProject: InlineTaskCreateInput['projects'][number] | null
  /** What the row's picker lists: only projects this viewer may create in (a dead row is noise here). */
  pickerProjects: InlineTaskCreateInput['projects']
  /** Pick the target by hand (the host's `onPickProject`); a no-op without one. */
  pickProject: (id: number) => void
  /** Arm the press held for an Act as: the row's own `ActAsToCreate` calls it as it switches the tab. */
  armHeldCreate: () => void
  /** `projectId === null`: no project at all, no `tasks:create` on any, or a roster
   *  that has not answered (pending and failed look the same). */
  noProject: boolean
  /** Does the surface draw the row at all? Not when the roster ANSWERED and nothing is creatable (a row that
   *  can only refuse is noise); an unanswered roster keeps it, disabled, saying so — a don't-know claims
   *  nothing (absence is not a state). */
  offered: boolean
  /** Both controls gate on this, so no row offers one live and one dead control. */
  canAct: boolean
  /** WHICH press is in flight, so only the pressed button spins. `null` when idle. */
  intent: InlineTaskCreateIntent | null
  /** Save AND start as `launchIntent`; calls `preventDefault` on the optional form event (Enter). */
  startTyped: (e?: FormEvent) => Promise<void>
  /** Hand the line to the full editor: DISCARDS the stored draft, then calls the host's `onExpand` with
   *  `trimmed`. Every surface and gesture must go through this, never the host's `onExpand` directly. */
  expandTyped: () => void
  /** `null` for an ordinary line. Vocabulary and parse: `./inlineTaskPrefix`. */
  prefix: InlineTaskPrefix | null
  /** The prefix's launch, or `'start'` — so the spinner lands on the right button. */
  launchIntent: InlineTaskCreateIntent
  /** Drop the chip, keeping the typed words. */
  clearPrefix: () => void
  /** Backspace at caret 0 with no selection drops the chip. On the input (where the caret is); single-homed. */
  onInputKeyDown: (e: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  /** Must say what THIS row's Enter does; includes the no-project case. */
  placeholder: string
  /** Moves with the mode: an `aria-label` overrides the placeholder, so this is all an AT user gets. */
  inputLabel: string
  /** For `aria-describedby`; `undefined` when there is no chip. */
  clearHint?: string
  /** Moves with the prefix: a ▶ "start this task" beside a Chat chip contradicts it. */
  startLabel: string
  startTitle: string
  /** The armed launch's involvement, read off the payload it will send (`createBodyFor`), so the row's
   *  caption cannot disagree with the create. `chat` while a Chat chip is up. */
  involvement: (typeof TASK_INVOLVEMENTS)[number]
  /** Is the 💬 offered — may chat AND there is text. NOT `canAct`: it must not vanish mid-write under the finger. */
  chatOffered: boolean
  /** The same two POSTs with `persona: 'advisor'`, `autonomous: false`. No form event: Enter is Start's. */
  chatTyped: () => Promise<void>
  /** Shared by both surfaces; the tooltip is the Chat chip ▶'s sentence. */
  chatLabel: string
  chatTitle: string
  /** An acting operator's "push as whom" ask (`PushIdentityDialog.tsx`), opened by both launches; the row renders it. */
  pushIdentityDialog: ReactNode
}

/** The two launches differ ONLY in the create payload (`createAndStart`); everything downstream is one path. */
export type InlineTaskCreateIntent = 'start' | 'chat'

export function useInlineTaskCreate({
  projects,
  rosterAnswered,
  rosterFailed = false,
  boardProjectId,
  feature,
  onCreated,
  canChat = false,
  onExpand,
  pickedProjectId = null,
  onPickProject,
  targetRef,
}: InlineTaskCreateInput): InlineTaskCreate {
  const { admin } = useAuth()
  /* THE TYPED LINE OUTLIVES THE MOUNT (a remount of the surface). Storage medium, value
     shape and admin scoping: `lib/taskDraftStorage.ts`; this hook owns only when to read and write.
     KEY AND TEXT ARE ONE PIECE OF STATE: `admin` resolves async and can change, and held apart the
     write-through would copy one operator's draft into another's bucket. */
  const draftKey = inlineTaskDraftStorageKey(admin?.id ?? null)
  const [draft, setDraft] = useState(() => ({ key: draftKey, text: loadInlineTaskDraft(draftKey) }))
  /* Re-read DURING RENDER (React's derive-from-changed-input adjustment): React re-runs before commit, so
     nothing sees the mismatched pair. An effect would be one commit late. */
  if (draft.key !== draftKey) setDraft({ key: draftKey, text: loadInlineTaskDraft(draftKey) })
  /* THE CANONICAL LINE (`chat:fix the login bug`) — persisted, and the channel for every write; chip and
     box derive from it (`./inlineTaskPrefix` says why the chip is not its own state). */
  const storedText = draft.text
  const setStoredText = (next: string) => setDraft({ key: draftKey, text: next })


  /* WHICH press is writing, so only that button spins; `submitting` derives from it. */
  const [intent, setIntent] = useState<InlineTaskCreateIntent | null>(null)
  const [error, setError] = useState('')
  const [refusedOrg, setRefusedOrg] = useState<OrgRef | null>(null)
  const submitting = intent !== null
  /* Create-time defaults via the HOOK, not a prop — the list stays ignorant of creating tasks. */
  const { defaults, loaded: defaultsLoaded } = useTaskCreateDefaults()

  /* The one gate per launch, read by the 💬 and the typed prefixes. A `Record` so a third launch is a type
     error until its gate is stated. */
  const intentAllowed: Record<InlineTaskCreateIntent, boolean> = { start: true, chat: canChat }
  /* A gated-off prefix never converts (absent, never dead): `Chat: ...` without the grant files a task. */
  const availablePrefixes = INLINE_TASK_PREFIXES.filter(candidate => intentAllowed[candidate.intent])
  /* `readInlinePrefixLine`, not `splitInlinePrefix` (see its header): `body` then cannot re-parse, so dropping
     the chip is one write. */
  const { prefix, body } = readInlinePrefixLine(storedText, availablePrefixes)
  // What the BOX shows and what a caller sets — the words alone.
  const text = body
  /* EVERY WRITE GOES BACK OUT AS A CANONICAL LINE (chip + new box value, re-read), so raising a chip and
     typing a character are one code path.
     A SECOND `chat:` UNDER A LIVE CHIP IS ABSORBED — accepted: `chat:` at the line's front is a MODE, never
     two words, so a task titled `chat: …` is deliberately not typeable here. */
  const setText = (next: string) => {
    // New words are a new ask: a press held for an Act as, and the question it raised, no longer stand.
    clearPendingInlineCreate(draftKey)
    if (refusedOrg !== null) { setRefusedOrg(null); setError('') }
    const line = prefix === null ? next : joinInlinePrefix(prefix, next)
    const read = readInlinePrefixLine(line, availablePrefixes)
    setStoredText(read.prefix === null ? read.body : joinInlinePrefix(read.prefix, read.body))
  }

  /* WRITE-THROUGH ON EVERY KEYSTROKE; an emptied box DELETES (the store's contract: `''` is never stored).
     THE MODE IS PART OF WHAT IS STORED — `chat:` over an empty box is kept, or a remount (list-style
     switch) silently drops the chip and Enter files a task. A visible remembered mode is the lesser cost.
     This is also the clear on a successful create (`setStoredText('')`); no second clear call exists. */
  useEffect(() => {
    if (draft.text === '') {
      if (loadInlineTaskDraft(draft.key) !== '') clearTaskDraft(draft.key)
    } else saveInlineTaskDraft(draft.key, draft.text)
  }, [draft])
  /* ANOTHER SURFACE TOOK THE WORDS (＋, or the editor's Save/Go beside a still-mounted board): the box empties too,
     or the moved sentence stays one ▶ away from filing twice. */
  useEffect(() => {
    const onCleared = (e: Event) => {
      if ((e as CustomEvent<string>).detail === draftKey) setDraft(prev => (prev.text === '' ? prev : { key: draftKey, text: '' }))
    }
    window.addEventListener(TASK_DRAFT_CLEARED, onCleared)
    return () => window.removeEventListener(TASK_DRAFT_CLEARED, onCleared)
  }, [draftKey])
  /* The prefix IS the mode: the chip takes the row over, one primary action. */
  const launchIntent: InlineTaskCreateIntent = prefix?.intent ?? 'start'

  /* DROP THE CHIP, KEEPING THE WORDS (the operator's call): one write of `body` as the whole line. */
  const clearPrefix = () => { if (prefix !== null) setStoredText(body) }

  /* The KEYBOARD half of chip removal (the × is the pointer half): Backspace at caret 0 with nothing
     selected. On the input — the chip is not a tab stop. `preventDefault` only when consumed;
     `selectionStart !== 0` also rejects the `null` of non-text inputs. */
  function onInputKeyDown(e: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) {
    if (e.key !== 'Backspace' || prefix === null || submitting) return
    const field = e.currentTarget
    if (field.selectionStart !== 0 || field.selectionEnd !== 0) return
    e.preventDefault()
    clearPrefix()
  }

  /* `GET /api/projects` is wider than what `POST /api/tasks` accepts, so the grant map narrows the ladder (see
     `resolveCreateProject`). */
  const canOnProject = useCanOnProject()
  /* A SYSTEM project takes no console create (`create.py` refuses one), whatever the grant map says — an org
     admin holds keys on it — so it is never a rung nor a picker row. */
  const systemIds = projects.filter(p => p.system_kind).map(p => p.id).join(' ')
  const canCreateIn = useCallback(
    (id: number) => canOnProject('tasks:create', id) && !systemIds.split(' ').includes(String(id)),
    [canOnProject, systemIds],
  )
  const focusedProjectId = useFocusedProjectId()
  // The org this tab is pinned to (`lib/activeOrg.ts`) — the operator's own unless acting — rung 3's preference.
  const ownOrgId = useTabOrg()
  const resolved = resolveCreateTarget(
    { projects, boardProjectId, feature, focusedProjectId, ownOrgId, pickedProjectId }, canCreateIn)
  const { id: projectId, featureId } = resolved
  const targetProject = projects.find(p => p.id === projectId) ?? null
  if (targetRef) targetRef.current = projectId === null ? null : { projectId, featureId }
  const pickerProjects = projects.filter(p => canCreateIn(p.id))
  /* A new target is a new ask: the last one's refusal (and a press held for its Act as) no longer applies. */
  const pickProject = (id: number) => {
    setError('')
    setRefusedOrg(null)
    clearPendingInlineCreate(draftKey)
    onPickProject?.(id)
  }
  const noProject = projectId === null
  const offered = !(noProject && rosterAnswered)
  const trimmed = text.trim()
  const canAct = projectId !== null && trimmed !== '' && !submitting

  /* WHAT THE ROW SAYS IT WILL DO: the placeholder is the box's only visible label, so it follows the armed
     launch; the button name/tooltip follow too (AT users have only the name). With no project the row is drawn
     only while the roster has not answered (`offered`), so that is the one thing it can say. */
  const placeholder = noProject
    ? rosterFailed ? 'Project list unavailable' : 'Loading projects…'
    : prefix?.placeholder ?? 'New task…'
  const startLabel = prefix?.startLabel ?? 'Save and start this task'
  const startTitle = prefix?.startTitle ?? 'Create this task and start it'
  // The caption's mode, off the very payload Start sends (`createBodyFor`), so the two cannot disagree.
  const launchBody = createBodyFor(launchIntent)
  const involvementValue: TaskInvolvement =
    involvementForTask(String(launchBody.persona), launchBody.autonomous === true)
  const involvement = TASK_INVOLVEMENTS.find(i => i.value === involvementValue) ?? TASK_INVOLVEMENTS[0]
  /* The box's accessible name moves too (an `aria-label` overrides the placeholder). `clearHint` rides
     `aria-describedby` so the mode's escape is announced, not hover-only. */
  const inputLabel = prefix?.inputLabel ?? 'Task description'
  const clearHint = prefix?.clearHint
  /* The tooltip is `CHAT_LAUNCH_TITLE`, the Chat chip ▶'s sentence. */
  const chatLabel = 'Launch this as a chat'
  const chatTitle = CHAT_LAUNCH_TITLE

  /* BOTH TERMS IN ONE ANSWER, and not `canAct` (a button vanishing under the pressing finger); it disables
     on `submitting` instead. Declared above the writes to avoid a temporal-dead-zone trap from hoisted
     functions. FALSE WHILE A CHAT CHIP IS UP (▶ already is the chat launch); keyed on `launchIntent` so a
     future non-chat prefix keeps the 💬. */
  const chatOffered = canChat && projectId !== null && trimmed !== '' && launchIntent !== 'chat'

  /* THE ONE WRITE PATH: save AND start, the same two writes as the create form's Go. `'queue'` because a new
     row has no run-intent to declare (unlike `startIntentFor`, `controls.ts`). A start failing after a
     successful create is reported as such and the text is KEPT. The two launches differ only in the
     create payload, hence one function. */
  const pushIdentity = usePushIdentity()
  async function createAndStart(
    nextIntent: InlineTaskCreateIntent,
    createBody: Record<string, unknown>,
    targetId: number | null = projectId,
  ) {
    setError('')
    setRefusedOrg(null)
    clearPendingInlineCreate(draftKey)
    const project = projects.find(p => p.id === targetId)
    /* ONE PRESS: another org's project needs this tab to act as that org, and the API would only 422.
       The press is held ARMED (`savePendingInlineCreate`) and the tab acts as that org now; the effect below files
       it there — Push-as asked, words and project kept — once the identity and roster land. */
    const actAsOrg = actAsNeededFor(project, admin?.is_operator === true)
    if (project && actAsOrg) {
      savePendingInlineCreate(draftKey, { projectId: project.id, intent: nextIntent, orgId: actAsOrg.id })
      armPendingInlineCreate(draftKey)
      enterActAs(actAsOrg)
      return
    }
    /* AN ACTING OPERATOR IS ASKED WHOM TO PUSH AS before the create, so Cancel files nothing; the answer rides the
       start. Not acting, no await at all: the write starts in the same tick as the press. */
    const pushAs = pushIdentity.acting && targetId !== null
      ? await pushIdentity.ask({ projectId: targetId, title: trimmed })
      : {}
    if (!pushAs) return
    setIntent(nextIntent)
    let created: PipelineTask
    try {
      const filing = featureId !== null && targetId === projectId ? { feature_id: featureId } : {}
      created = await post<PipelineTask>('/api/tasks', {
        project_id: targetId,
        ...filing,
        body: trimmed,
        ...createBody,
      })
    } catch (err) {
      setError(createRefusal(err, nextIntent === 'chat' ? 'Failed to create chat' : 'Failed to create task', project,
        admin?.is_operator === true))
      setRefusedOrg(refusedInOrg(err, project))
      setIntent(null)
      return
    }
    try {
      const started = await post<PipelineTask>(CONTROLS.start.path(created.id), startBody('queue', pushAs))
      /* THE WHOLE LINE via `setStoredText`: `setText('')` would write the prefix back, leaving a chip over
         an empty box. */
      setStoredText('')
      onCreated(started)
    } catch (err) {
      setError(
        `Task #${created.id} was created but did not start (${apiDetail(err, 'the start failed')}) — `
        + 'press Start on its row.',
      )
      // The row exists, so the host must know; only the text stays for a correct-and-retry.
      onCreated(created)
    } finally {
      setIntent(null)
    }
  }

  /* THE CREATE PAYLOAD PER LAUNCH — the one home, read by Enter, ▶ and 💬.
     `'start'`: every default key is SENT, matching the full form's untouched Go (an absent persona
       would take the org's default on the server, never what the row showed). READ AT SUBMIT TIME (defaults resolve async with the identity
       payload). `transition_mode` is read off the parent epic at dispatch, so not prefilled. Complex means
       EPIC (`TaskAxisControls`' `TASK_DEPTHS`); the stored default is still named `complex`.
     `'chat'`: `persona: 'advisor'` (conversational; settles only on the operator's Complete) and
       `autonomous: false`. NOT `defaults`-seeded in any term; `transition_mode` omitted (no parent). */
  /* THE ORDINARY GESTURE NEVER SILENTLY BECOMES A CHAT: `advisor` is a legal `task_default_persona`, but ▶ /
     Enter is gated on `tasks:create` alone while a chat needs `worktrees:edit` too. Unguarded, it could 403
     or mix persona and `autonomous` from different settings. Only the chat arm creates a conversation. */
  function createBodyFor(nextIntent: InlineTaskCreateIntent, targetId: number | null = projectId): Record<string, unknown> {
    if (nextIntent === 'chat') return { persona: ADVISOR_PERSONA, autonomous: false }
    // The target project's own default persona over the org's (`withProjectPersona`).
    const seed = withProjectPersona(defaults, projects.find(p => p.id === targetId)?.default_persona)
    const persona = seed.persona === ADVISOR_PERSONA ? DEFAULT_PERSONA : seed.persona
    return {
      autonomous: seed.autonomous,
      transition_mode: 'auto',
      persona: seed.complex ? 'epic' : persona,
    }
  }

  /* ENTER AND ▶, as `launchIntent`. With no chip up this is the plain task create. */
  async function startTyped(e?: FormEvent) {
    e?.preventDefault()
    if (!canAct) return
    await createAndStart(launchIntent, createBodyFor(launchIntent))
  }

  /* THE 💬 — hidden while a Chat chip is up (`chatOffered`), since ▶ already is this write. */
  async function chatTyped() {
    if (!chatOffered || submitting) return
    await createAndStart('chat', createBodyFor('chat'))
  }

  /* THE HELD PRESS GOES THROUGH once this tab acts as the org it was held for and the roster lists the project as
     creatable — one click (Act as), not a second ▶. ONLY AN ACT AS PRESSED ON THIS ROW'S QUESTION arms it
     (`armHeldCreate`): acting as that org from anywhere else never files it. Acting as another org, or emptied
     words, drops it. */
  const actingOrgId = useActingAs()?.id ?? null
  const armHeldCreate = () => armPendingInlineCreate(draftKey)
  useEffect(() => {
    const pending = loadPendingInlineCreate(draftKey)
    if (pending === null || !pending.armed || submitting || !rosterAnswered || !defaultsLoaded) return
    if (actingOrgId !== pending.orgId || trimmed === '') {
      if (actingOrgId !== null) clearPendingInlineCreate(draftKey)
      return
    }
    if (!projects.some(p => p.id === pending.projectId && canCreateIn(p.id))) return
    void createAndStart(pending.intent, createBodyFor(pending.intent, pending.projectId), pending.projectId)
  }, [draftKey, actingOrgId, submitting, rosterAnswered, defaultsLoaded, trimmed, projects, canCreateIn])

  /* THE ⤢, from any surface or the double-press gesture. It DISCARDS the line (a move, the operator's
     call); the words are now in the editor's Description.
     THE STORE IS CLEARED DIRECTLY: the host's `onExpand` navigates, unmounting this row in the same commit,
     and React runs no effect for a leaving component. `clearTaskDraft` is idempotent.
     THE WHOLE LINE GOES, prefix included (`setStoredText`). `submitting` is the one guard (never navigate
     out from under a POST); an EMPTY box is allowed — that is when the full editor is most wanted. */
  function expandTyped() {
    if (submitting) return
    clearTaskDraft(draftKey)
    clearPendingInlineCreate(draftKey)
    setStoredText('')
    /* Another org's project: the editor opens acting as it, so its Go files there with the Push-as ask;
       the words and project ride the navigation state, which outlives the identity re-read. */
    const actAsOrg = actAsNeededFor(targetProject ?? undefined, admin?.is_operator === true)
    if (actAsOrg) enterActAs(actAsOrg)
    onExpand(trimmed, projectId === null ? null : { projectId, featureId })
  }

  return {
    text, setText, trimmed, submitting, error, refusedOrg, projectId, featureId, targetProject, pickerProjects,
    pickProject, armHeldCreate, noProject, offered, canAct, intent,
    startTyped, expandTyped, chatOffered, chatTyped, chatLabel, chatTitle,
    prefix, launchIntent, clearPrefix, onInputKeyDown,
    placeholder, inputLabel, clearHint, startLabel, startTitle, involvement,
    pushIdentityDialog: pushIdentity.dialog,
  }
}
