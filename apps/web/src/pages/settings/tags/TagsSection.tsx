import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useCan } from '../../../auth'
import { useSettingsRows } from '../../../hooks/useSettingsRows'
import Banner from '../../../ui/Banner'
import EmptyState from '../../../ui/EmptyState'
import { tapTargetSx } from '../../../ui/listStyles'
import type { TagGroup } from '../../../types'
import { listTagGroups, NEW_TAG_GROUP_URL, reorderTagGroups } from './api'
import TagGroupCard from './TagGroupCard'
import LoadingState from '../../../ui/LoadingState'
import SectionHeader from '../../../ui/SectionHeader'
import NewButton from '../../../ui/NewButton'

/* Settings > Tasks > Tags — the tag vocabulary, and the only surface that can see a DRAFT group.
 * Not a knob (not in `app/operator_settings/`): a CRUD table, mounted as a synthetic section anchored by
 * one of the area's knob keys (see `Settings.tsx`), owning its own fetch and writes — routing them through
 * the page's `edits` map would make "Save (n)" miscount and Discard claim to undo a delete.
 * The panel owns the catalogue fetch and the group ORDER (a whole-list write); everything scoped to one group
 * belongs to `TagGroupCard`. Creating and editing a group or a tag are pages (`TagGroupEditPage`, `TagEditPage`),
 * which refresh this list through `TAGS_ROWS_KEY`.
 * Every write asks a `tags` key (`routes/tags.py`: create on `POST /api/tags/groups`, edit on
 * `PUT /api/tags/group-order` and updates, delete on deletes); the read asks `tasks:view` + `tags:view`
 * (`?include_draft=true`). Why a taxonomy is a tenant's subject: `apps/api/app/authz/`.
 * Write affordances are ABSENT without their key (the card gates its own controls; its header holds the
 * per-control key table). The group arrows arrive pre-ANDed with this panel's `tags:edit`, so a drawn
 * arrow is a pure POSITION fact. Presentation, never the boundary: see `components/RequireFeature.tsx`. */

/** The list's load-failure text, which also keys its read (`hooks/useSettingsRows`). */
const TAGS_LOAD_FAILED = 'Failed to load the tag groups.'
/** The list's react-query key: the record pages invalidate it after a write. */
export const TAGS_ROWS_KEY = ['settings-rows', TAGS_LOAD_FAILED] as const
/** One group's page read (`TagGroupEditPage`): a tag's page refreshes it too, as it may sit behind that tag. */
export const TAG_GROUP_KEY = ['tag-group'] as const

export function TagsSection(): JSX.Element {
  /* This panel's own write keys: `canCompose` = `tags:create` (New group), `canReorder` = `tags:edit`
   * (group arrows). No delete is rendered here. */
  const can = useCan()
  const canCompose = can('tags:create')
  const canReorder = can('tags:edit')

  /* The read's keys. `Settings.tsx` appends this section for every reader of the page (admitted on
   * `system:view`), and `listTagGroups()` calls `GET /api/tags/groups?include_draft=true`, which
   * `routes/tags.py::list_tag_groups` gates on `tasks:view` + `tags:view` (single home for why) — either
   * alone is a 403 on this panel's only request. `useCan` is allow-all for a superadmin (`auth.tsx`). */
  const canReadVocabulary = can('tasks:view') && can('tags:view')

  /* No request for a refused reader. Guarded in `load`, not only at mount, because every card's
   * `onChanged` calls it too. `rowError` here is the group-reorder failure. */
  const {
    rows: groups, loadError, rowError: orderError, load, busyIds, rowWrite, confirm, confirmDialog,
  } = useSettingsRows<TagGroup>({
    fetch: async () => ({ rows: await listTagGroups() }),
    failed: TAGS_LOAD_FAILED,
    enabled: canReadVocabulary,
  })
  /** The whole-list order write, busy under one key. */
  const reordering = busyIds.has('order')

  /* One atomic write over the COMPLETE list: per-row writes can be interrupted, and shared `position`s
   * fall back to the alphabetical tie-break. */
  async function moveGroup(index: number, direction: -1 | 1) {
    // Unreachable via the UI (arrows withheld), but the write itself must not depend on the card's flag.
    if (!canReorder) return
    if (!groups || reordering) return
    const order = groups.map(g => g.id)
    const target = index + direction
    // Unreachable via the UI (end buttons disabled); prevents a `[null, "…"]` body the server would refuse.
    if (target < 0 || target >= order.length) return
    ;[order[index], order[target]] = [order[target], order[index]]
    await rowWrite('order', 'Failed to reorder the tag groups.', () => reorderTagGroups(order))
  }

  /* `null` without `tags:create`, not disabled: disabled means "not right now", this is "not by you".
   * Header row and empty state share this value, one at a time: the empty state carries it while there are no
   * groups, the header once there are some, or when a first load failed and no empty state draws. Never during
   * the load, so it does not jump into the empty state. */
  const newGroupButton = (worded = false) => canCompose ? (
    <NewButton label="New group" to={NEW_TAG_GROUP_URL} worded={worded} sx={tapTargetSx} />
  ) : null

  const headerCaption = !canCompose && !canReorder && 'Creating groups and ordering them need tag grants you do not hold.'
  const headerAction = groups === null ? (loadError ? newGroupButton() : null) : groups.length > 0 ? newGroupButton() : null

  /* A refused reader gets a sentence, not a blank tab (Settings mounts this subtab unconditionally). New
   * group goes too: a group is born DRAFT, and composing into a list this panel cannot show has no
   * visible result. */
  if (!canReadVocabulary) {
    return (
      <Box data-testid="tags-section" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        <Typography
          variant="body2"
          data-testid="tags-withheld"
          sx={{ color: 'text.secondary', maxWidth: '72ch' }}
        >
          Reading the tag vocabulary needs task and tag grants you do not hold. Ask your administrator.
        </Typography>
      </Box>
    )
  }

  return (
    <Box data-testid="tags-section" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {/* One sentence for a reader offered none of this panel's controls, so fewer buttons read as a
      boundary, not a half-rendered page. Not a `Banner` (nothing failed). It claims only this panel's two
      writes: the three `tags` keys are separately grantable, so "read-only" could be contradicted.
      Plural subject to match the pronoun. */}
      {(headerCaption || headerAction) && <SectionHeader caption={headerCaption} action={headerAction} />}

      {loadError && <Banner severity="error">{loadError}</Banner>}
      {orderError && <Banner severity="error">{orderError}</Banner>}

      {groups === null && !loadError && <LoadingState />}

      {groups !== null && groups.length === 0 && (
        <EmptyState title="No tag groups yet" action={newGroupButton(true) ?? undefined} />
      )}

      {groups?.map((group, index) => (
        <TagGroupCard
          key={group.id}
          group={group}
          /* Pre-ANDed with `tags:edit`: the reorder is the panel's write. The card also reads `tags:edit` itself
           * and HIDES its arrows without it (`TagGroupCard.tsx`'s `canEditTags`), so a drawn arrow means position. */
          canMoveUp={canReorder && index > 0}
          canMoveDown={canReorder && index < groups.length - 1}
          /* A group reorder is a panel write the card's `busy` never sees: without this a second arrow click is
           * silently dropped by the re-entry guard, and a concurrent delete turns the reorder into a 422. */
          panelBusy={reordering}
          onMoveGroup={direction => moveGroup(index, direction)}
          onChanged={load}
          confirm={confirm}
        />
      ))}

      {confirmDialog}
    </Box>
  )
}

export default TagsSection
