import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { get, post, put } from '../api'
import { useAuth } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { usePlanSwitch } from '../lib/planSwitches'
import Breadcrumb from '../components/Breadcrumb'
import BudgetField from '../components/budget/BudgetField'
import { GroupBindingsPanel, GroupMembersPanel } from '../components/access/GroupAccess'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useEditRecord } from '../hooks/useEditRecord'
import { useSubmit } from '../hooks/useSubmit'
import { useUrlTab } from '../hooks/useUrlTab'
import type { GroupDetail } from '../types'
import { groupLabel, isReservedGroupName } from '../types'
import Banner from '../ui/Banner'
import { DetailPage } from '../ui/detail/DetailPage'
import DetailStatus from '../ui/detail/DetailStatus'
import FormField from '../ui/FormField'
import FormTextField from '../ui/FormTextField'
import { ScimChip } from '../ui/MarkChip'
// The read-only meta strip shared with `UserEditPage` (see `ui/MetaRow`).
import { MetaActors, MetaRow } from '../ui/MetaRow'
import OrgSelect, { ORG_FIELD_LABEL } from '../ui/OrgSelect'
import SectionCard from '../ui/SectionCard'
import SubmitButton from '../ui/SubmitButton'

/* Group new/edit page (`/groups/new` with `mode="create"`, and `/groups/:id`). An Org Owner/Admin (or an
 * operator — the server scopes the rows) edits; a `groups:view` holder sees every tab read-only. Only an
 * operator picks the new group's org; a tenant's is their own. It mirrors `UserEditPage`, which carries
 * the reasoning for the shared chrome and posture.
 * Details saves the name and description, and below it the group's monthly budget saves on its own; Members adds and removes one member at a time; Bindings is the
 * roles the group holds (`components/access/GroupAccess`). A write re-reads the whole `GroupDetail`.
 */

const TABS = ['details', 'members', 'bindings'] as const
type Tab = (typeof TABS)[number]

const GROUP_TABS: readonly TabDef<Tab>[] = [
  { id: 'details', label: 'Details' },
  { id: 'members', label: 'Members' },
  { id: 'bindings', label: 'Bindings' },
]

/** Parity with `UserEditPage`'s form card (the 480px cap is reasoned there). */
const FORM_CARD_SX = { mt: 1, maxWidth: 480 } as const

/** The access tabs' width: a bindings table reads at desktop without a scrollport. */
const ACCESS_CARD_SX = { mt: 1, maxWidth: 960 } as const

/* The access tabs' sentence before the group exists: copy, not a disabled control, which answers "why is
 * this greyed out". */
function CreateModeNotice({ children }: { children: ReactNode }) {
  return (
    <Typography variant="body2" sx={{ color: 'text.secondary' }}>
      Save the group&apos;s details first — {children}
    </Typography>
  )
}

/* The reserved group gets no write controls: the server refuses its writes (`routes/groups.py`'s
 * `_reject_reserved_group`, plus the members guard). The server stays authoritative; the UI stops
 * offering dead actions and says why. */
function ReservedGroupNotice() {
  return (
    <Banner severity="info">
      This group is reserved: it cannot be renamed or deleted, and its members cannot be changed. It
      confers no bypass.
    </Banner>
  )
}

export default function GroupEditPage({ mode }: { mode?: 'create' }) {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const creating = mode === 'create'
  const { admin } = useAuth()
  // Writes are `@org_admin` (`routes/groups.py`).
  const canWrite = isOrgAdminSeat(admin)
  const isOperator = admin?.is_operator === true
  // `BudgetField` draws nothing off the plan; its card goes with it rather than standing empty.
  const budgetsOn = usePlanSwitch('budgets_enabled')

  const [detail, setDetail] = useState<GroupDetail | null>(null)
  // The tab lives in the URL (hooks/useUrlTab.ts).
  const [tab, selectTab] = useUrlTab(GROUP_TABS, 'details')

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const { busy: detailsSaving, formError: detailsError, run: runDetails, saved: detailsSaved, clearSaved: clearDetailsSaved } = useSubmit()
  /* Create only, never seeded from `detail`: `PUT /api/groups/{id}` accepts no `org_id`, and having no
  `seedDetails` line keeps the update body from growing the key. */
  const [orgId, setOrgId] = useState('')
  const [orgError, setOrgError] = useState('')
  // Sticky once opened, so switching tabs does not re-walk the org.
  const [bindingsOpened, setBindingsOpened] = useState(false)
  // An effect, not onSelect: a link can land on the tab.
  useEffect(() => {
    if (tab === 'bindings') setBindingsOpened(true)
  }, [tab])


  /* Per-tab seeders: a landed save may only re-seed the tab that wrote (see the header). */
  const seedDetails = useCallback((g: GroupDetail) => {
    setName(g.name)
    // `description` is NOT NULL; it arrives as `''`.
    setDescription(g.description)
  }, [])

  // A fresh load or a create seeds the details form too: nothing can be discarded.
  const hydrate = useCallback((g: GroupDetail) => {
    setDetail(g)
    seedDetails(g)
  }, [seedDetails])

  /* No pre-fetch id shape guard (`UserEditPage` has the reasoning): the API's 422 is not-found. Nothing is
  fetched for a group already held, so after a create — the router reuses this instance when `navigate`
  swaps to `/groups/:id` — no redundant GET flashes loading or "Failed to load group" over a fine create. */
  const { loading, notFound, loadError: error } = useEditRecord(
    creating || detail?.id === id ? null : id ?? null, groupId => get<GroupDetail>(`/api/groups/${groupId}`), hydrate,
    { loadFallback: 'Failed to load group', fixedLoadError: true, notFoundStatuses: [404, 422] },
  )

  /* Whether the grant tabs are live derives from `detail`, not `mode`: after a create the two disagree
   * until the router swaps elements. */
  const subject = detail

  /* Via the single home for the rule (`types.ts`), off the loaded name so a rename is reflected. */
  const reserved = isReservedGroupName(subject?.name)
  /* Every write control is off for the reserved group AND for a viewer who is not a superadmin. */
  const locked = reserved || !canWrite

  /* After a member write: re-read the group (its members), leaving the details draft alone. */
  const reloadGroup = useCallback(async () => {
    if (!subject) return
    try {
      setDetail(await get<GroupDetail>(`/api/groups/${subject.id}`))
    } catch {
      // The write's own banner already said what happened; a stale list is the lesser harm.
    }
  }, [subject])

  async function handleDetailsSave(e: FormEvent) {
    e.preventDefault()
    /* The create arm's own precondition, checked before the saving flag goes up. */
    if (subject === null && isOperator && orgId === '') {
      setOrgError('Pick the organization this group belongs to.')
      return
    }
    setOrgError('')
    await runDetails('Failed to save the group — check the name and try again.', async () => {
      if (subject === null) {
        const created = await post<GroupDetail>('/api/groups', {
          // Accepted on the POST only (boundary B28): the operator's pick, or the tenant superadmin's own org.
          org_id: (isOperator ? orgId : admin?.org?.id) || undefined,
          name: name.trim(),
          // A string, possibly empty — never `null` (`groups.description` is NOT NULL, default `''`).
          description: description.trim(),
        })
        hydrate(created)
        /* `replace`: Back after creating lands on the list, never a create form that would make a second group. */
        navigate(`/groups/${created.id}`, { replace: true })
      } else {
        // Only changed fields: `PUT /api/groups/{id}` reads `model_fields_set`, so omitted fields are left alone.
        const body: Record<string, unknown> = {}
        if (name !== subject.name) body.name = name
        if (description !== subject.description) body.description = description
        const saved = await put<GroupDetail>(`/api/groups/${subject.id}`, body)
        setDetail(saved)
        seedDetails(saved)
      }
    })
  }

  if (loading || notFound || error || (!creating && !subject)) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'Groups', to: '/groups' }, { label: 'Group' }]} />}
        loading={loading}
        notFound={notFound && 'Group not found'}
        error={error || 'Failed to load group'}
      />
    )
  }

  const heading = subject ? groupLabel(subject, isOperator) : 'New group'

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'Groups', to: '/groups' }, { label: heading }]} />}
      title={heading}
      titleAside={subject?.scim_managed ? <ScimChip /> : undefined}
    >
      <Tabs
        tabs={GROUP_TABS}
        tab={tab}
        onSelect={selectTab}
        label="Group sections"
        idPrefix="group"
        sticky="default"
      />

      <TabPanel id="details" current={tab} idPrefix="group">
        <Box component="form" onSubmit={e => void handleDetailsSave(e)} sx={FORM_CARD_SX}>
          <SectionCard>
            <Stack spacing={2}>
              {detailsError && <Banner severity="error">{detailsError}</Banner>}
              {detailsSaved && <Banner severity="success">Saved</Banner>}
              {reserved && <ReservedGroupNotice />}

              {/* `required` rides `htmlInput`; MUI's `required` prop appends an asterisk. */}
              <FormTextField
                label="Name"
                id="group-name"
                type="text"
                /* Everyone cannot be renamed (409), so its field shows the org's label and never edits the stored name. */
                value={subject?.is_everyone ? heading : name}
                disabled={locked || subject?.is_everyone === true}
                onChange={v => {
                  clearDetailsSaved()
                  setName(v)
                }}
                slotProps={{ htmlInput: { required: true, maxLength: 64 } }}
              />
              {/* `multiline`: the column is unbounded, and a single line clips a sentence. */}
              <FormTextField
                label="Description"
                id="group-description"
                multiline
                rows={3}
                value={description}
                disabled={locked}
                onChange={v => {
                  clearDetailsSaved()
                  setDescription(v)
                }}
              />

              {/* Only before the group exists, and only for an operator: its tenant is decided on the POST, and `PUT`
              accepts no `org_id`, so there is no field rather than a disabled one (as `NodeGroupDialog`). */}
              {subject === null && isOperator && (
                <FormField label={ORG_FIELD_LABEL} error={orgError} hint="Fixed after creation.">
                  <OrgSelect
                    id="group-org"
                    value={orgId === '' ? null : orgId}
                    onChange={next => {
                      clearDetailsSaved()
                      setOrgId(next)
                      setOrgError('')
                    }}
                    disabled={locked}
                  />
                </FormField>
              )}

              {/* Provenance, only once a group exists (empty em-dashes would read as unfilled fields). The same
              `ui/MetaRow` strip and position as `UserEditPage`'s Profile tab. Actor names only: `GroupDetail` carries
              no timestamps. */}
              {subject && (
                <MetaRow>
                  <MetaActors createdBy={subject.created_by} updatedBy={subject.updated_by} />
                </MetaRow>
              )}

              {canWrite && (
                <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
                  <SubmitButton busy={detailsSaving} disabled={reserved}>
                    {subject ? 'Save details' : 'Create group'}
                  </SubmitButton>
                </Box>
              )}
            </Stack>
          </SectionCard>
        </Box>
        {/* Outside the details form: its own Save, to its own route. Any group, the reserved one included — a
        budget is not a write to the group. Org admins only (`BudgetField` draws nothing for anyone else). */}
        {subject && canWrite && budgetsOn && (
          <Box sx={{ ...FORM_CARD_SX, mt: 2 }}>
            <SectionCard title="Budget">
              <BudgetField scope="group" scopeId={subject.id} />
            </SectionCard>
          </Box>
        )}
      </TabPanel>

      <TabPanel id="members" current={tab} idPrefix="group">
        <Box sx={ACCESS_CARD_SX}>
          <SectionCard>
            {reserved && <ReservedGroupNotice />}
            {!subject ? (
              <CreateModeNotice>members can only be added to a group that exists.</CreateModeNotice>
            ) : (
              <GroupMembersPanel group={subject} canWrite={!locked} onChanged={reloadGroup} />
            )}
          </SectionCard>
        </Box>
      </TabPanel>

      <TabPanel id="bindings" current={tab} idPrefix="group">
        <Box sx={ACCESS_CARD_SX}>
          <SectionCard>
            {!subject ? (
              <CreateModeNotice>a group that exists can be given roles.</CreateModeNotice>
            ) : (
              // Mounted on first open only: it walks every scope of the org to find the group's bindings.
              tab === 'bindings' || bindingsOpened ? <GroupBindingsPanel group={subject} /> : null
            )}
          </SectionCard>
        </Box>
      </TabPanel>
    </DetailPage>
  )
}
