import { useState, type JSX } from 'react'
import { get } from '../../api'
import type { Page } from '../../hooks/useListPage'
import { isReservedGroupName, type GroupSummary } from '../../types'
import { AsyncLookup, type LookupOption } from '../../ui/AsyncLookup'
import FormDialog from '../../ui/FormDialog'
import FormField from '../../ui/FormField'

/* PICK ONE REAL GROUP, BY SERVER SEARCH — `GET /api/groups?q=&per=20`, the Groups roster's own route.
 * One dialog for the row's Add membership and the bulk bar's Add to group / Remove from group: the caller owns
 * the write and says, per pick, what it would do (`summary`) and why it cannot (`blockedReason`).
 * Two groups are never offered, as the server refuses both: the org's Everyone group (its membership is the
 * system's, 409) and the reserved bypass group (`isReservedGroupName`, 400). */

const GROUP_SEARCH_PER = 20

/** A picked group: its id (the members routes) and name (the membership route). */
export type PickedGroup = { id: string; name: string }

async function searchGroups(q: string, signal: AbortSignal): Promise<LookupOption[]> {
  const params = new URLSearchParams({ per: String(GROUP_SEARCH_PER) })
  if (q) params.set('q', q)
  const page = await get<Page<GroupSummary>>(`/api/groups?${params}`, { signal })
  return page.items
    .filter(g => !g.is_everyone && !isReservedGroupName(g.name))
    .map(g => ({ key: g.id, label: g.name, hint: g.description || undefined }))
}

export default function GroupPickDialog({
  title,
  submitLabel,
  busyLabel,
  destructive = false,
  busy = false,
  error = '',
  summary,
  blockedReason,
  onSubmit,
  onClose,
  testId,
}: {
  title: string
  submitLabel: string
  busyLabel?: string
  destructive?: boolean
  busy?: boolean
  error?: string
  /** What the submit would do with this group, in a few words ("3 to add"); shown under the picker. */
  summary?: (group: PickedGroup) => string
  /** Why the submit cannot run for this group ('' when it can). */
  blockedReason?: (group: PickedGroup) => string
  onSubmit: (group: PickedGroup) => void
  onClose: () => void
  testId?: string
}): JSX.Element {
  const [picked, setPicked] = useState<LookupOption | null>(null)
  const group = picked ? { id: picked.key, name: picked.label } : null
  const blocked = group ? blockedReason?.(group) ?? '' : 'Pick a group'
  return (
    <FormDialog
      title={title}
      submitLabel={submitLabel}
      busyLabel={busyLabel}
      busy={busy}
      error={error}
      destructive={destructive}
      submitDisabled={Boolean(blocked)}
      submitDisabledReason={blocked || undefined}
      onSubmit={() => { if (group && !blocked) onSubmit(group) }}
      onClose={onClose}
      maxWidth="xs"
      testId={testId}
    >
      <FormField label="Group" hint={group ? summary?.(group) : undefined}>
        <AsyncLookup
          id={`${testId ?? 'group-pick'}-group`}
          testId={`${testId ?? 'group-pick'}-group`}
          value={picked}
          onChange={setPicked}
          fetchOptions={searchGroups}
          fetchOnFocus
          placeholder="Search groups"
          noOptionsText="No group matches"
        />
      </FormField>
    </FormDialog>
  )
}
