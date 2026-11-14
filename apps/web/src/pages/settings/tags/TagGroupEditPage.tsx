import type { JSX } from 'react'
import { useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { pickFrom } from '../../../api'
import { useCan } from '../../../auth'
import Breadcrumb from '../../../components/Breadcrumb'
import { TabPanel, type TabDef } from '../../../components/Tabs'
import { RecordEditorFrame, useRecordEditor } from '../../../hooks/useRecordEditor'
import { countOf } from '../../../lib/humanize'
import Banner from '../../../ui/Banner'
import { EntityDescriptionField, EntityNameField } from '../../../ui/EntityNameFields'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../../ui/feed/types'
import FormTextField from '../../../ui/FormTextField'
import { ID_SX, tapTargetSx } from '../../../ui/listStyles'
import NewButton from '../../../ui/NewButton'
import StatusChip from '../../../ui/StatusChip'
import type { TagGroup } from '../../../types'
import {
  createTagGroup, deleteTagGroup, fieldForDetail, listTagGroups, newTagUrl, TAGS_SETTINGS_URL, tagGroupUrl, tagGroupUsage,
  tagUrl, updateTagGroup, type TagFormField,
} from './api'
import { confirmedCount, groupDeleteConfirm } from './TagGroupCard'
import { TAG_GROUP_KEY, TAGS_ROWS_KEY } from './TagsSection'

/* One TAG GROUP: `/settings/tags/new` (`mode="create"`) and `/settings/tags/:groupId`. Overview holds its name,
 * description and tags-per-task; Tags lists its tags, each opening `TagEditPage`. The group's `state` is not a
 * field: it decides whether the feature is visible anywhere, so it is a page verb (Activate / Move to draft) the
 * operator cannot miss. Description is editable because the DeepSeek classifier reads it to pick a task's tag.
 * Reordering and deleting single tags stay on the Settings list's group card (`TagGroupCard`), beside the order
 * they change. Keys per verb: `TagGroupCard.tsx`'s header. */

type Tab = 'overview' | 'tags'

const TABS: readonly TabDef<Tab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'tags', label: 'Tags' },
]

const TAGS_PER_TASK_OPTIONS = [
  { value: 'single', label: 'One tag', help: 'Picking a second tag replaces the first.' },
  { value: 'multi', label: 'Several tags', help: 'A task may carry any number of them.' },
]

type Form = { name: string; description: string; multi_select: boolean }

const BLANK: Form = { name: '', description: '', multi_select: false }
const formOf = (group: TagGroup): Form => ({ name: group.name, description: group.description, multi_select: group.multi_select })

/** No single-group read route: the list (drafts included) is read and the group picked by id. */
const getTagGroup = (id: string) => pickFrom(listTagGroups, g => g.id === id, 'Tag group not found')

export default function TagGroupEditPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { groupId } = useParams<{ groupId: string }>()
  const creating = mode === 'create'
  const queryClient = useQueryClient()
  const can = useCan()
  const canEditTags = can('tags:edit')
  const canDeleteTags = can('tags:delete')
  const canCreateTags = can('tags:create')
  const readOnly = creating ? !canCreateTags : !canEditTags

  const editor = useRecordEditor<TagGroup, Form, TagFormField>({
    id: creating ? null : groupId ?? null, fetch: getTagGroup, formOf, initial: creating ? BLANK : null,
    load: { loadFallback: 'Failed to load the tag group.', cacheKey: id => [...TAG_GROUP_KEY, id] },
    fieldForDetail, readOnly, creating, write, urlOf: row => tagGroupUrl(row.id),
  })
  const { loaded: group, form, busy, errors, setErrors, run, guard, edit, status, reload, confirm, verb } = editor

  /** The background list re-reads after every write here. */
  const refreshList = () => queryClient.invalidateQueries({ queryKey: TAGS_ROWS_KEY })

  /* Validates and sends: the group as saved, or null (the page says why). */
  async function write(form: Form): Promise<TagGroup | null> {
    const name = form.name.trim()
    // The server strips `name` but not `description`; whitespace would be an invisible blank line.
    const description = form.description.trim()
    if (!name) {
      // Refused client-side so the message lands on the field; same wording as the server's.
      setErrors({ name: 'Give the group a name — it is the heading the board’s Group-by control shows.' })
      return null
    }
    const landed = await run(creating ? 'Failed to create the tag group.' : 'Failed to save the tag group.', async () => {
      const body = { name, description, multi_select: form.multi_select }
      const row = creating ? await createTagGroup(body) : await updateTagGroup(group?.id ?? '', body)
      void refreshList()
      return row
    })
    return landed?.value ?? null
  }

  const pending = status(<Breadcrumb items={[{ label: 'Tags', to: TAGS_SETTINGS_URL }, { label: 'Tag group' }]} />, 'Tag group not found')
  if (pending) return pending
  const draft = form ?? BLANK

  const isActive = group?.state === 'active'

  function toggleState() {
    if (!group || !canEditTags) return
    void verb.run(
      isActive ? 'Failed to move the group to draft.' : 'Failed to activate the group.',
      async () => {
        await updateTagGroup(group.id, { state: isActive ? 'draft' : 'active' })
        void refreshList()
        await reload()
      },
    )
  }

  async function handleDelete() {
    if (!group || !canDeleteTags || verb.busy) return
    const count = await confirmedCount(() => tagGroupUsage(group.id))
    if (!(await confirm(groupDeleteConfirm(group, count)))) return
    await verb.run('Failed to delete the tag group.', async () => {
      await deleteTagGroup(group.id)
      void refreshList()
      // This URL now names a missing group; the list is where it went from.
      guard.leaveNow(TAGS_SETTINGS_URL, { replace: true })
    })
  }

  const heading = group?.name ?? 'New tag group'
  const locked = busy || verb.busy

  const actions = group && (canEditTags || canDeleteTags) && (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {/* THE ONE CLICK THAT TURNS THE FEATURE ON: `contained` while draft. */}
      {canEditTags && (
        <Button variant={isActive ? 'outlined' : 'contained'} onClick={toggleState} disabled={locked}
          aria-label={isActive ? `Move to draft — ${group.name}` : `Activate ${group.name}`}
          data-testid="tag-group-state" sx={tapTargetSx}>
          {isActive ? 'Move to draft' : 'Activate'}
        </Button>
      )}
      {canDeleteTags && (
        <Button variant="outlined" color="error" onClick={() => void handleDelete()} disabled={locked}
          aria-label={`Delete ${group.name}`} data-testid="tag-group-delete" sx={tapTargetSx}>
          Delete
        </Button>
      )}
    </Box>
  )

  const tagRows: FeedRowSpec[] = (group?.tags ?? []).map(tag => ({
    key: tag.id,
    // The code whole beside the name: a lead column clipped it to "front…".
    title: <>{tag.name} <Box component="span" sx={{ ...ID_SX, color: 'text.secondary' }}>{tag.tag}</Box></>,
    titleText: tag.name,
    href: tagUrl(group?.id ?? '', tag.id),
    notes: tag.description ? [{ key: 'description', text: tag.description, tone: 'muted' }] : undefined,
    data: { tag: tag.tag },
  }))

  return (
    <RecordEditorFrame editor={editor} prefix="tag-group" noun="Tag group" tabs={creating ? TABS.slice(0, 1) : TABS}
      cancelTo={TAGS_SETTINGS_URL}
      breadcrumb={<Breadcrumb items={[{ label: 'Tags', to: TAGS_SETTINGS_URL }, { label: heading }]} />}
      title={heading}
      titleAside={group && (
        <StatusChip label={group.state} tone={isActive ? 'success' : 'warning'} data-status={group.state} />
      )}
      subtitle={group && `${group.multi_select ? 'Several tags per task' : 'One tag per task'} · ${countOf(group.tags.length, 'tag')}`}
      actions={actions}
      notice={group && !isActive && (
        <Banner severity="warning">
          Draft — not offered outside this page yet.{canEditTags && ' Activate the group to publish it.'}
        </Banner>
      )}
      panels={tab => group && (
        <TabPanel id="tags" current={tab} idPrefix="tag-group">
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 2 }}>
            {canCreateTags && (
              <Box>
                <NewButton label="New tag" to={newTagUrl(group.id)} testId="tag-group-new-tag" sx={tapTargetSx} />
              </Box>
            )}
            <FeedList
              ariaLabel={`Tags in ${group.name}`}
              testId="tag-group-tags"
              emptyMessage={canCreateTags
                ? 'No tags yet. Add at least one — a group with no tags cannot be activated.'
                : 'No tags yet. A group with no tags cannot be activated.'}
              sections={group.tags.length > 0 ? [{ key: 'tags', rail: false, rows: tagRows }] : []}
            />
            {group.tags.length > 1 && canEditTags && (
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Reorder them on the Tags list.
              </Typography>
            )}
          </Box>
        </TabPanel>
      )}
    >
      <EntityNameField error={errors.name} help="Names the group in Settings › Tags." id="tag-group-name"
        value={draft.name} onChange={v => edit({ name: v })} disabled={readOnly} autoFocus={creating} />
      <EntityDescriptionField error={errors.description} help="The classifier reads it." id="tag-group-description"
        value={draft.description} onChange={v => edit({ description: v })} disabled={readOnly} />
      <FormTextField label="Tags per task" error={errors.multi_select} help="How many of this group’s tags one task carries."
        id="tag-group-multi-select" value={draft.multi_select ? 'multi' : 'single'}
        onChange={v => edit({ multi_select: v === 'multi' })} options={TAGS_PER_TASK_OPTIONS} disabled={readOnly} />
    </RecordEditorFrame>
  )
}
