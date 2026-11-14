import type { JSX } from 'react'
import { useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { ApiError, pickFrom } from '../../../api'
import { useCan } from '../../../auth'
import Breadcrumb from '../../../components/Breadcrumb'
import type { TabDef } from '../../../components/Tabs'
import { RecordEditorFrame, useRecordEditor } from '../../../hooks/useRecordEditor'
import { EntityDescriptionField, EntityNameField } from '../../../ui/EntityNameFields'
import FormTextField from '../../../ui/FormTextField'
import { ID_SX, tapTargetSx } from '../../../ui/listStyles'
import type { Tag, TagGroup } from '../../../types'
import {
  createTag, deleteTag, fieldForDetail, listTagGroups, TAGS_SETTINGS_URL, tagGroupUrl, tagUrl, tagUsage, updateTag, type TagFormField,
} from './api'
import { confirmedCount, tagDeleteConfirm } from './TagGroupCard'
import { TAG_GROUP_KEY, TAGS_ROWS_KEY } from './TagsSection'

/* One TAG inside a group: `/settings/tags/:groupId/tags/new` (`mode="create"`) and
 * `/settings/tags/:groupId/tags/:tagId`. Name, code, description.
 * The code is optional on create — a WIRE decision: the server derives it when the key is ABSENT and refuses
 * present-and-empty, so a blank field is omitted. Deriving it here would duplicate the server's grammar (NFKD
 * folding, illegal-run collapse, 32-char bound).
 * The group is not editable: the server refuses moves (task assignments would carry into a vocabulary that never
 * meant them). Keys per verb: `TagGroupCard.tsx`'s header. */

const TABS: readonly TabDef<'overview'>[] = [{ id: 'overview', label: 'Overview' }]

const CODE_HELP = {
  tip: 'Key a task’s tag is stored under.',
  more: 'Blank derives it from the name. Lowercase letters, digits, “-” and “_”, at most 32 characters.',
}

type Form = { name: string; tag: string; description: string }

const BLANK: Form = { name: '', tag: '', description: '' }
const formOf = (tag: Tag): Form => ({ name: tag.name, tag: tag.tag, description: tag.description })

type Loaded = { group: TagGroup; tag: Tag | null }

/** No single-tag read route: the list is read and the group (and tag) picked by id; either missing is not found. */
async function getTagIn(groupId: string, tagId: string | null): Promise<Loaded> {
  const group = await pickFrom(listTagGroups, g => g.id === groupId, 'Tag not found')
  const tag = tagId === null ? null : group.tags.find(t => t.id === tagId)
  if (tag === undefined) throw new ApiError(404, 'Tag not found')
  return { group, tag }
}

export default function TagEditPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { groupId = '', tagId } = useParams<{ groupId: string; tagId: string }>()
  const creating = mode === 'create'
  const queryClient = useQueryClient()
  const can = useCan()
  const canDelete = can('tags:delete')
  const readOnly = creating ? !can('tags:create') : !can('tags:edit')

  /* Create mode reads too: the group's name heads the page, and an unknown group is not found. */
  const editor = useRecordEditor<Loaded, Form, TagFormField>({
    id: `${groupId}/${creating ? '' : tagId ?? ''}`,
    fetch: () => getTagIn(groupId, creating ? null : tagId ?? ''),
    formOf: row => (row.tag ? formOf(row.tag) : BLANK),
    initial: null,
    load: { loadFallback: 'Failed to load the tag.' },
    fieldForDetail, readOnly, creating, write,
    urlOf: row => tagUrl(row.group.id, row.tag?.id ?? ''),
  })
  const { loaded, form, busy, errors, setErrors, run, guard, edit, status, confirm, verb: removal } = editor

  const group = loaded?.group ?? null
  const tag = loaded?.tag ?? null

  /* Validates and sends: the group with the tag as saved, or null (the page says why). */
  async function write(form: Form): Promise<Loaded | null> {
    if (!group) return null
    const name = form.name.trim()
    const code = form.tag.trim()
    // The server strips `name` but not `description`; whitespace would be an invisible blank line.
    const description = form.description.trim()
    if (!name) {
      setErrors({ name: 'Give the tag a name.' })
      return null
    }
    if (tag && !code) {
      // On EDIT, blanking the code is a clear the server refuses; say so without the round trip.
      setErrors({ tag: 'A tag’s code cannot be cleared. Type one, or leave the existing code alone.' })
      return null
    }
    const landed = await run(tag ? 'Failed to save the tag.' : 'Failed to add the tag.', async () => {
      const row = tag
        ? await updateTag(tag.id, { tag: code, name, description })
        : await createTag(group.id, {
          name,
          description,
          // OMITTED when blank (see the header), rather than `tag: undefined`, which reads as "sent, empty".
          ...(code ? { tag: code } : {}),
        })
      // The background list re-reads.
      void queryClient.invalidateQueries({ queryKey: TAGS_ROWS_KEY })
      void queryClient.invalidateQueries({ queryKey: TAG_GROUP_KEY })
      return row
    })
    return landed && { group, tag: landed.value }
  }

  const groupCrumb = { label: group?.name ?? 'Tag group', to: `${tagGroupUrl(groupId)}?tab=tags` }
  const pending = status(<Breadcrumb items={[{ label: 'Tags', to: TAGS_SETTINGS_URL }, groupCrumb, { label: 'Tag' }]} />, 'Tag not found')
  if (pending || !group || !form) return pending ?? <></>

  async function handleDelete() {
    if (!group || !tag || !canDelete || removal.busy) return
    const count = await confirmedCount(() => tagUsage(tag.id))
    if (!(await confirm(tagDeleteConfirm(group, tag, count)))) return
    await removal.run('Failed to delete the tag.', async () => {
      await deleteTag(tag.id)
      void queryClient.invalidateQueries({ queryKey: TAGS_ROWS_KEY })
      void queryClient.invalidateQueries({ queryKey: TAG_GROUP_KEY })
      // This URL now names a missing tag; its group's Tags tab is where it went from.
      guard.leaveNow(groupCrumb.to, { replace: true })
    })
  }

  const actions = tag && canDelete && (
    <Button variant="outlined" color="error" onClick={() => void handleDelete()} disabled={busy || removal.busy}
      aria-label={`Delete tag ${tag.tag}`} data-testid="tag-delete" sx={tapTargetSx}>
      Delete
    </Button>
  )

  return (
    <RecordEditorFrame editor={editor} prefix="tag" noun="Tag" tabs={TABS} cancelTo={groupCrumb.to}
      breadcrumb={<Breadcrumb items={[{ label: 'Tags', to: TAGS_SETTINGS_URL }, groupCrumb, { label: tag?.name ?? 'New tag' }]} />}
      title={tag?.name ?? `Add a tag to ${group.name}`}
      subtitle={tag && <Box component="span" sx={ID_SX} data-testid="tag-code-display">{tag.tag}</Box>}
      actions={actions}
    >
      <EntityNameField required={false} error={errors.name} help="Shown on tasks carrying this tag." id="tag-name"
        value={form.name} onChange={v => edit({ name: v })} disabled={readOnly} autoFocus={creating} />
      <FormTextField label="Code" error={errors.tag} help={CODE_HELP} id="tag-code" value={form.tag}
        onChange={v => edit({ tag: v })} disabled={readOnly} />
      <EntityDescriptionField error={errors.description} help="What earns a task this tag. The classifier reads it."
        id="tag-description" value={form.description} onChange={v => edit({ description: v })} disabled={readOnly} />
    </RecordEditorFrame>
  )
}
