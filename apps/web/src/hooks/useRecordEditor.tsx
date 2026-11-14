import { type ComponentProps, type FormEvent, type JSX, type ReactNode, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import Banner from '../ui/Banner'
import CancelSaveBar from '../ui/CancelSaveBar'
import { DetailPage } from '../ui/detail/DetailPage'
import { EditStatus } from '../ui/detail/DetailStatus'
import { FORM_SX } from '../ui/formDensity'
import { LeaveGuardDialog } from '../ui/UnsavedChangesDialog'
import { useBaselineDirty } from './useBaselineDirty'
import { useEditRecord } from './useEditRecord'
import { useLeaveGuard } from './useLeaveGuard'
import { useSubmit } from './useSubmit'
import { useUrlTab } from './useUrlTab'

/* The flow a settings record's edit page runs (`TagGroupEditPage`, `TagEditPage`, `ModelPriceEditPage`,
 * `EmailTemplateEditPage`): read the record (`id === null` reads nothing), seed the form once, Save (the bar's,
 * Enter's and the leave dialog's), then open the new row's page or stay on the saved row as the new baseline.
 * `write` validates and sends: the record as saved, or null with the refusal already on the page. `load` is
 * `useEditRecord`'s options, `fieldForDetail` `useSubmit`'s.
 * `idOf` is for a record addressed by a field of its own form (a model id): a loaded row answering `id` is not read
 * again, every read is another row (it replaces the draft and its baseline), a created row stays loaded for the
 * page it moves to, and a Save that renames the row moves to its new address — the leave dialog's Save does not. */
export function useRecordEditor<R, F, E extends string = never>(o: {
  id: string | null; fetch: (id: string) => Promise<R>; formOf: (row: R) => F; initial: F | null
  load: Parameters<typeof useEditRecord<R>>[3]; fieldForDetail?: (detail: string) => E | null
  readOnly?: boolean; creating: boolean; write: (form: F) => Promise<R | null>; urlOf: (row: R) => string
  idOf?: (row: R) => string
}) {
  const [loaded, setLoaded] = useState<R | null>(null)
  const [form, setForm] = useState<F | null>(o.initial)
  const readOnly = o.readOnly ?? false
  const submission = useSubmit<E>(o.fieldForDetail)
  /* The page's verbs (Delete, Activate): their own busy and refusal, apart from the form's. */
  const verb = useSubmit()
  const { dirty, reset: forgetBase } = useBaselineDirty(form ? JSON.stringify(form) : null)
  /* The row as the form and its new baseline (one batch with the re-seed). */
  const seed = (row: R) => {
    setLoaded(row)
    setForm(o.formOf(row))
    forgetBase()
  }
  /* A re-read (after a verb) refreshes the record, never the draft being typed. */
  const record = useEditRecord(loaded && o.idOf?.(loaded) === o.id ? null : o.id, o.fetch, row => {
    if (o.idOf) return seed(row)
    setLoaded(row)
    setForm(prev => prev ?? o.formOf(row))
  }, o.load)

  /* Validates, sends and stays on the saved row; a created one is its own page's to read, unless `idOf` keeps it. */
  async function store(): Promise<R | null> {
    const row = readOnly || !form ? null : await o.write(form)
    if (row && (!o.creating || o.idOf)) seed(row)
    return row
  }
  /* The Save, and the leave dialog's: true once the write landed. */
  async function save(): Promise<boolean> {
    const row = await store()
    if (!row) return false
    // `replace`: Back never lands on a create form that would add a second row.
    if (o.creating || (o.idOf && o.idOf(row) !== o.id)) guard.leaveNow(o.urlOf(row), { replace: true })
    return true
  }
  const guard = useLeaveGuard(dirty && !readOnly && !record.deleting, o.idOf ? async () => (await store()) !== null : save)

  const edit = (patch: Partial<F>) => {
    setForm({ ...form, ...patch } as F)
    submission.setErrors({})
    submission.setFormError('')
    submission.clearSaved()
  }
  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    void save()
  }
  /** The page while there is no record to edit yet (loading, not found, failed), else null. */
  const status = (trail: ReactNode, notFoundTitle: string): JSX.Element | null =>
    o.id !== null && (record.loading || record.notFound || record.loadError || !loaded || !form)
      ? <EditStatus trail={trail} loading={record.loading} notFound={record.notFound} loadError={record.loadError} notFoundTitle={notFoundTitle} />
      : null

  return { ...record, ...submission, verb, readOnly, creating: o.creating, loaded, form, setForm, dirty, guard, edit, submit, status }
}

/* The page they draw around their fields: `DetailPage` over the verbs' and the form's refusals, the tab strip
 * (`tabs[0]` is Overview), the form, the leave guard, under one Save bar. `prefix` heads every test id and tab id;
 * `noun` names the record in the strip's label and the leave dialog (`leaveSubject` when "This <noun> has" is not
 * how its changes read). */
export function RecordEditorFrame<T extends string, E extends string = string>({
  editor, prefix, noun, leaveSubject, tabs, cancelTo, saveLabel, saveDisabled, tabOf, notice, panels, children, ...page
}: {
  editor: Pick<ReturnType<typeof useRecordEditor>,
    'busy' | 'dirty' | 'saved' | 'submit' | 'formError' | 'deleteError' | 'guard' | 'confirmDialog' | 'verb' | 'readOnly' | 'creating'>
    & { errors: Partial<Record<E, string>> }
  prefix: string; noun: string; leaveSubject?: string; tabs: readonly TabDef<T>[]; cancelTo: string
  saveLabel?: ReactNode
  /** When Save stands down, if not simply while nothing changed (a copy's Create is live untouched). */
  saveDisabled?: boolean
  /** The tab holding a field: a refusal moves the page there, or its message sits on a hidden panel. */
  tabOf?: (field: E) => T
  /** Between the verbs' refusal and the form's. */
  notice?: ReactNode
  /** The tabs after Overview. */
  panels?: (tab: T) => ReactNode
  /** Overview's fields. A function when every tab holds fields: the page's own `TabPanel`s, all inside the form,
   *  under Save on each. */
  children: ReactNode | ((tab: T) => ReactNode)
} & Pick<ComponentProps<typeof DetailPage>, 'breadcrumb' | 'title' | 'titleAside' | 'subtitle' | 'actions'>): JSX.Element {
  const [tab, selectTab] = useUrlTab(tabs, tabs[0].id)
  const { errors } = editor
  const everyTab = typeof children === 'function'
  useEffect(() => {
    const first = Object.keys(errors)[0] as E | undefined
    if (first && tabOf) selectTab(tabOf(first))
  }, [errors, tabOf, selectTab])
  // Only Overview holds fields: another tab's rows open their own pages, so it carries no Save (AC4).
  const saveBar = !editor.readOnly && (everyTab || tab === tabs[0].id) && (
    <CancelSaveBar testId={`${prefix}-actions`} saveTestId={`${prefix}-save`} cancelTo={cancelTo} creating={editor.creating}
      busy={editor.busy} disabled={saveDisabled ?? !editor.dirty} saved={editor.saved} saveLabel={saveLabel}
      saveProps={{ onClick: () => editor.submit() }} />
  )
  const form = (
    <Box component="form" onSubmit={editor.submit} sx={everyTab ? undefined : FORM_SX} data-testid={`${prefix}-form`}>
      {everyTab ? children(tab) : children}
      {/* Enter in a field submits; the visible Save is the page's bar. */}
      <button type="submit" hidden />
    </Box>
  )
  return (
    <DetailPage {...page} actionBar={saveBar} testId={`${prefix}-editor`}>
      {editor.verb.formError && <Banner severity="error" testId={`${prefix}-action-error`}>{editor.verb.formError}</Banner>}
      {notice}
      {editor.formError && <Banner severity="error" testId={`${prefix}-form-error`}>{editor.formError}</Banner>}
      {editor.deleteError && <Banner severity="error">{editor.deleteError}</Banner>}

      <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label={`${noun} sections`} idPrefix={prefix} />

      {everyTab ? form : <TabPanel id={tabs[0].id} current={tab} idPrefix={prefix}>{form}</TabPanel>}
      {panels?.(tab)}

      <LeaveGuardDialog leave={editor.guard}
        message={`${leaveSubject ?? `This ${noun.toLowerCase()} has`} changes that have not been saved. Save them before leaving?`} />
      {editor.confirmDialog}
    </DetailPage>
  )
}
