import { type JSX, useState } from 'react'
import { useSubmit } from '../../../hooks/useSubmit'
import FormTextField from '../../../ui/FormTextField'
import type { Tag, TagGroup } from '../../../types'
import { createTag, createTagGroup, fieldForDetail, updateTag, updateTagGroup, type TagFormField } from './api'
import FormDialog from '../../../ui/FormDialog'

/* Create or edit a TAG GROUP (name, description, tags per task) or one TAG inside a group (name, code,
 * description).
 * A group's `state` is deliberately not here: it decides whether the feature is visible anywhere, so it lives
 * on the group card as a button the operator cannot miss. Description is editable because the DeepSeek
 * classifier reads it to pick a task's tag.
 * A tag's code is optional on create — a WIRE decision: the server derives it when the key is ABSENT and
 * refuses present-and-empty, so a blank field is omitted. Deriving it here would duplicate the server's
 * grammar (NFKD folding, illegal-run collapse, 32-char bound).
 * A tag's group is not editable: the server refuses moves (task assignments would carry into a vocabulary
 * that never meant them).
 */

const CODE_HINT =
  'Blank derives it from the name. Lowercase, digits, “-” and “_”, max 32.'

const SELECT_MODE_HINT =
  'With One tag, picking a second replaces the first.'

/** A group (`null` to create one), or a tag in `group` (`null` to add one to it). */
export type TagDialogTarget =
  | { kind: 'group'; group: TagGroup | null }
  | { kind: 'tag'; group: TagGroup; tag: Tag | null }

export function TagDialog({
  target,
  onClose,
  onSaved,
}: {
  target: TagDialogTarget
  onClose: () => void
  /** Refetch the catalogue. Awaited before the dialog closes, so the list is never stale behind it. */
  onSaved: () => Promise<void>
}): JSX.Element {
  const tag = target.kind === 'tag' ? target.tag : null
  const group = target.kind === 'group' ? target.group : null
  const editing = target.kind === 'tag' ? tag : group
  const [name, setName] = useState(editing?.name ?? '')
  const [code, setCode] = useState(tag?.tag ?? '')
  const [description, setDescription] = useState(editing?.description ?? '')
  const [multiSelect, setMultiSelect] = useState(group?.multi_select ?? false)
  const { busy, errors, setErrors, formError, run } = useSubmit<TagFormField>(fieldForDetail)

  async function submit() {
    const trimmedName = name.trim()
    const trimmedCode = code.trim()
    // The server strips `name` but not `description`; whitespace would be an invisible blank line.
    const trimmedDescription = description.trim()
    if (target.kind === 'group') {
      if (!trimmedName) {
        // Refused client-side so the message lands on the field; same wording as the server's.
        setErrors({ name: 'Give the group a name — it is the heading the board’s Group-by control shows.' })
        return
      }
      await run(group ? 'Failed to save the tag group.' : 'Failed to create the tag group.', async () => {
        const body = { name: trimmedName, description: trimmedDescription, multi_select: multiSelect }
        if (group) await updateTagGroup(group.id, body)
        else await createTagGroup(body)
        await onSaved()
        onClose()
      })
      return
    }
    if (!trimmedName) {
      setErrors({ name: 'Give the tag a name.' })
      return
    }
    if (tag && !trimmedCode) {
      // On EDIT, blanking the code is a clear the server refuses; say so without the round trip.
      setErrors({ tag: 'A tag’s code cannot be cleared. Type one, or leave the existing code alone.' })
      return
    }
    await run(tag ? 'Failed to save the tag.' : 'Failed to add the tag.', async () => {
      if (tag) {
        await updateTag(tag.id, { tag: trimmedCode, name: trimmedName, description: trimmedDescription })
      } else {
        await createTag(target.group.id, {
          name: trimmedName,
          description: trimmedDescription,
          // OMITTED when blank (see the header), rather than `tag: undefined`, which reads as "sent, empty".
          ...(trimmedCode ? { tag: trimmedCode } : {}),
        })
      }
      await onSaved()
      onClose()
    })
  }

  const isGroup = target.kind === 'group'
  const idPrefix = isGroup ? 'tag-group' : 'tag'
  return (
    <FormDialog
      testId="tag-form-dialog"
      title={
        isGroup
          ? (group ? `Edit ${group.name}` : 'New tag group')
          : (tag ? `Edit ${tag.tag}` : `Add a tag to ${target.group.name}`)
      }
      submitLabel={isGroup ? (group ? 'Save group' : 'Create group') : (tag ? 'Save tag' : 'Add tag')}
      busy={busy}
      error={formError}
      onSubmit={() => void submit()}
      onClose={onClose}
    >
      <FormTextField
        label="Name"
        error={errors.name}
        id={`${idPrefix}-name`}
        value={name}
        onChange={setName}
      />

      {!isGroup && (
        <FormTextField label="Code" error={errors.tag} help={CODE_HINT} id="tag-code" value={code} onChange={setCode} />
      )}

      <FormTextField
        label="Description"
        error={errors.description}
        help={isGroup ? 'The classifier reads it.' : 'What earns a task this tag. The classifier reads it.'}
        id={`${idPrefix}-description`}
        value={description}
        onChange={setDescription}
        multiline
        minRows={2}
      />

      {isGroup && (
        <FormTextField
          label="Tags per task"
          error={errors.multi_select}
          help={SELECT_MODE_HINT}
          id="tag-group-multi-select"
          value={multiSelect ? 'multi' : 'single'}
          onChange={v => setMultiSelect(v === 'multi')}
          options={[{ value: 'single', label: 'One tag' }, { value: 'multi', label: 'Several tags' }]}
        />
      )}
    </FormDialog>
  )
}

export default TagDialog
