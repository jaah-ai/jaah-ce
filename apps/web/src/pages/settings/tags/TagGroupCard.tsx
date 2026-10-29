import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useCan } from '../../../auth'
import type { ConfirmOptions } from '../../../components/ConfirmDialog'
import { useSubmit } from '../../../hooks/useSubmit'
import Banner from '../../../ui/Banner'
import StatusChip from '../../../ui/StatusChip'
import { ABOVE_PHONE, PHONE, ROW_ACTION_SX, monoSx } from '../../../ui/listStyles'
import type { Tag, TagGroup } from '../../../types'
import { deleteTag, deleteTagGroup, reorderTags, tagGroupUsage, tagUsage, updateTagGroup } from './api'

/* ONE GROUP, ONE CARD, WITH ITS OWN TAGS INSIDE IT — the requirement: "what does *By type* offer?" is
 * answered by being on screen, not by changing a filter on two flat rosters.
 *
 * The card owns every write scoped to ONE group (activation, deletes, tag reorder). The group reorder
 * (needs the whole list) and the dialogs (one at a time) are the panel's.
 *
 * ── WHICH KEY EACH CONTROL ASKS ──
 * THREE SEPARATELY GRANTABLE KEYS, named after the WRITE (as `TagsSection.tsx`), checked against
 * `apps/api/app/routes/tags.py` — cited by symbol, never line:
 *   `tags:edit`   — Activate / Move to draft and group Edit (`update_tag_group`), a tag's Edit
 *                   (`update_tag`), the tag arrows (`reorder_tags`).
 *   `tags:delete` — group Delete (`delete_tag_group`), a tag's Delete (`delete_tag`).
 *   `tags:create` — Add tag (`create_tag`).
 *
 * THE READ IS NEVER GATED: a reader keeps the whole card, draft groups included.
 * ABSENT FOR A PERMISSION, DISABLED FOR A STATE (in flight: `locked`; or at the end of the list).
 * `canMoveUp`/`canMoveDown` arrive pre-ANDed with the panel's `tags:edit`, so a rendered arrow's disabled
 * state is purely positional. No per-card explanation; instead, instructions a reader cannot follow
 * ("Activate the group…", "Add at least one") are trimmed for them.
 * PRESENTATION ONLY (`components/RequireFeature.tsx`); `authz_guard` decides on every /api/tags route.
 */

/** The delete confirmations quote a REAL count from the usage endpoint; this is only its grammar. */
function taskCountPhrase(count: number): string {
  return `${count} ${count === 1 ? 'task' : 'tasks'}`
}

/* Arrow buttons: 44px `minWidth` because the touch target is min(width, height); MUI's stock 64px would
 * triple a glyph button. */
const ARROW_SX = [...ROW_ACTION_SX, { minWidth: 44, px: 0.5, fontSize: '0.875rem' }]

/** The up/down pair a group and a tag share: disabled in flight (`locked`) or at an end of its list; the
 *  caller gates it on `tags:edit`. `subject` completes "Move <subject> up" (the group name, `tag <code>`). */
function MoveArrows({ subject, locked, canUp, canDown, onMove }: {
  subject: string
  locked: boolean
  canUp: boolean
  canDown: boolean
  onMove: (direction: -1 | 1) => void
}): JSX.Element {
  return (
    <>
      <Button type="button" variant="text" disabled={locked || !canUp} onClick={() => onMove(-1)} aria-label={`Move ${subject} up`} sx={ARROW_SX}>
        <Box component="span" aria-hidden="true">↑</Box>
      </Button>
      <Button type="button" variant="text" disabled={locked || !canDown} onClick={() => onMove(1)} aria-label={`Move ${subject} down`} sx={ARROW_SX}>
        <Box component="span" aria-hidden="true">↓</Box>
      </Button>
    </>
  )
}

/* The tag row: code, name, description, actions, and the wrap does the rest (≤480px: description, then
 * actions, each on its own line). Nothing is truncated — a shortened code names nothing. */
const TAG_ROW_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 1,
  rowGap: 0.5,
  px: 1.5,
  py: 0.75,
  borderTop: '1px solid',
  borderColor: 'divider',
}

export function TagGroupCard({
  group,
  canMoveUp,
  canMoveDown,
  panelBusy,
  onMoveGroup,
  onEditGroup,
  onAddTag,
  onEditTag,
  onChanged,
  confirm,
}: {
  group: TagGroup
  canMoveUp: boolean
  canMoveDown: boolean
  /** A write the PANEL has in flight (the group reorder), which this card's `busy` cannot see. */
  panelBusy: boolean
  /** Whole-list group reorder (needs every group's id). */
  onMoveGroup: (direction: -1 | 1) => Promise<void>
  onEditGroup: () => void
  onAddTag: () => void
  onEditTag: (tag: Tag) => void
  /** Refetch the catalogue after a write this card made. */
  onChanged: () => Promise<void>
  confirm: (options: ConfirmOptions) => Promise<boolean>
}): JSX.Element {
  const { busy, formError: error, run: submit } = useSubmit()

  const isActive = group.state === 'active'
  const tags = group.tags
  // ONE disabled predicate for every control: this card's write or the panel's.
  const locked = busy || panelBusy

  /* The three keys, one per verb (see the header). */
  const can = useCan()
  const canEditTags = can('tags:edit')
  const canCreateTags = can('tags:create')
  const canDeleteTags = can('tags:delete')
  /* WHETHER THE ROW OF CONTROLS EXISTS AT ALL, header and tag row separately: an empty `Stack` still
     takes a line (`flex: '1 1 100%'` at phone width) or holds the name row open. */
  const showGroupActions = canEditTags || canDeleteTags
  const showTagActions = canEditTags || canDeleteTags

  /** Every write: disable the card, run, refetch, show the server's sentence on failure. `fallback` only
   *  when no `detail` was sent. */
  function run(fallback: string, write: () => Promise<unknown>) {
    return submit(fallback, async () => {
      await write()
      await onChanged()
    })
  }

  function toggleState() {
    // Unreachable through the UI; the guard on the write itself does not depend on every control being
    // gated (as the panel's `moveGroup`). Same on the three below.
    if (!canEditTags) return
    const next = isActive ? 'draft' : 'active'
    void run(
      isActive ? 'Failed to move the group to draft.' : 'Failed to activate the group.',
      () => updateTagGroup(group.id, { state: next }),
    )
  }

  /* The confirmation's count is QUERIED, never guessed; a failed query says the number could not be read
   * rather than becoming a silent zero. */
  async function confirmedCount(read: () => Promise<{ task_count: number }>): Promise<number | null> {
    try {
      return (await read()).task_count
    } catch {
      return null
    }
  }

  async function handleDeleteGroup() {
    if (!canDeleteTags) return
    if (locked) return
    const count = await confirmedCount(() => tagGroupUsage(group.id))
    const impact =
      count === null
        ? 'The task count could not be read, so this may also remove tag assignments from tasks.'
        : count === 0
          ? 'No task carries one of its tags, so nothing else changes.'
          : `Its tags also come off ${taskCountPhrase(count)}.`
    const ok = await confirm({
      title: 'Delete tag group',
      identity: `${group.name} · ${tags.length} ${tags.length === 1 ? 'tag' : 'tags'}`,
      message: `This deletes the group and every tag in it. ${impact} This cannot be undone.`,
      confirmLabel: 'Delete group',
      destructive: true,
    })
    if (!ok) return
    void run('Failed to delete the tag group.', () => deleteTagGroup(group.id))
  }

  async function handleDeleteTag(tag: Tag) {
    if (!canDeleteTags) return
    if (locked) return
    const count = await confirmedCount(() => tagUsage(tag.id))
    const impact =
      count === null
        ? 'The task count could not be read, so this may also remove the tag from tasks.'
        : count === 0
          ? 'No task carries it, so nothing else changes.'
          : `It also comes off ${taskCountPhrase(count)}.`
    const ok = await confirm({
      title: 'Delete tag',
      identity: `${group.name} · ${tag.tag}`,
      message: `This deletes “${tag.name}”. ${impact} This cannot be undone.`,
      confirmLabel: 'Delete tag',
      destructive: true,
    })
    if (!ok) return
    void run('Failed to delete the tag.', () => deleteTag(tag.id))
  }

  /* ONE ATOMIC WRITE: the complete list in one request. Two per-row PUTs, interrupted, would leave two rows
   * sharing a `position`. */
  function moveTag(index: number, direction: -1 | 1) {
    if (!canEditTags) return
    const order = tags.map(t => t.id)
    const target = index + direction
    // Unreachable through the UI (end arrows disabled); guards a body carrying `[null, "…"]`.
    if (target < 0 || target >= order.length) return
    ;[order[index], order[target]] = [order[target], order[index]]
    void run('Failed to reorder the tags.', () => reorderTags(group.id, order))
  }

  return (
    <Card variant="outlined" data-testid="tag-group-card" data-group-state={group.state}>
      <Box
        sx={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'center',
          columnGap: 1.5,
          rowGap: 1,
          px: 1.5,
          py: 1,
          borderBottom: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Box
          sx={{
            display: 'flex',
            flexWrap: 'wrap',
            alignItems: 'center',
            columnGap: 1,
            rowGap: 0.5,
            flex: '1 1 12rem',
            minWidth: 0,
          }}
        >
          <Typography
            variant="subtitle2"
            component="h3"
            /* `break-word`, never `anywhere`: wrap at spaces, split only an unbreakable token. */
            sx={{ fontWeight: 600, minWidth: 0, overflowWrap: 'break-word' }}
          >
            {group.name}
          </Typography>
          <StatusChip
            label={group.state}
            tone={isActive ? 'success' : 'warning'}
            data-status={group.state}
          />
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {group.multi_select ? 'Several tags per task' : 'One tag per task'} ·{' '}
            {tags.length} {tags.length === 1 ? 'tag' : 'tags'}
          </Typography>
        </Box>

        {/* THE WHOLE CLUSTER IS CONDITIONAL: an empty `Stack` would hold the name row open. */}
        {showGroupActions && (
          <Stack
            direction="row"
            spacing={0.5}
            useFlexGap
            sx={{ flexWrap: 'wrap', flex: '0 0 auto', alignItems: 'center' }}
          >
            {/* THE ONE CLICK THAT TURNS THE FEATURE ON: `contained` while draft (both seeded groups ship
                draft). EVERY `aria-label` ON THIS CARD CONTAINS ITS VISIBLE TEXT AS ONE UNBROKEN RUN
                (WCAG 2.5.3), so the group name is appended, never spliced in. */}
            {canEditTags && (
              <Button
                type="button"
                variant={isActive ? 'text' : 'contained'}
                color="primary"
                disabled={locked}
                onClick={toggleState}
                aria-label={isActive ? `Move to draft — ${group.name}` : `Activate ${group.name}`}
                sx={ROW_ACTION_SX}
              >
                {isActive ? 'Move to draft' : 'Activate'}
              </Button>
            )}
            {canEditTags && (
              <Button
                type="button"
                variant="text"
                disabled={locked}
                onClick={onEditGroup}
                aria-label={`Edit ${group.name}`}
                sx={ROW_ACTION_SX}
              >
                Edit
              </Button>
            )}
            {/* Rendered = key held, so `canMoveUp`/`canMoveDown` are purely positional (see the header). */}
            {canEditTags && (
              <MoveArrows subject={group.name} locked={locked} canUp={canMoveUp} canDown={canMoveDown} onMove={d => void onMoveGroup(d)} />
            )}
            {canDeleteTags && (
              <Button
                type="button"
                variant="outlined"
                color="error"
                disabled={locked}
                onClick={() => void handleDeleteGroup()}
                aria-label={`Delete ${group.name}`}
                sx={ROW_ACTION_SX}
              >
                Delete
              </Button>
            )}
          </Stack>
        )}
      </Box>

      {/* THE DRAFT WARNING IS A FACT FOR EVERY READER; its second sentence, an instruction, only for whoever
          can press Activate. */}
      {!isActive && (
        <Typography variant="caption" sx={{ display: 'block', px: 1.5, pt: 1, color: 'warning.main' }}>
          Draft — not offered outside this page yet.
          {canEditTags && ' Activate the group to publish it.'}
        </Typography>
      )}

      {group.description && (
        <Typography variant="body2" sx={{ px: 1.5, pt: 1, color: 'text.secondary' }}>
          {group.description}
        </Typography>
      )}

      {error && (
        <Box sx={{ px: 1.5, pt: 1 }}>
          <Banner severity="error">{error}</Banner>
        </Box>
      )}

      {/* Bottom padding here when `Add tag` is gone (its `py: 1` otherwise closes the card). */}
      <Box sx={{ pt: 1, pb: canCreateTags ? 0 : 1 }}>
        {tags.length === 0 ? (
          /* Copy matches what this reader can do: the fact stays, "Add at least one" only for a composer. */
          <Typography variant="body2" sx={{ px: 1.5, pb: 0.5, color: 'text.secondary' }}>
            {canCreateTags
              ? 'No tags yet. Add at least one — a group with no tags cannot be activated.'
              : 'No tags yet. A group with no tags cannot be activated.'}
          </Typography>
        ) : (
          tags.map((tag, index) => (
            <Box key={tag.id} data-testid="tag-row" sx={TAG_ROW_SX}>
              <Typography
                component="span"
                /* A FLOOR, not a width, above the phone so codes and names align as columns; a longer
                   code still renders WHOLE. Dropped at phone width. */
                sx={[monoSx, { fontSize: '0.8125rem', fontWeight: 600, flex: '0 0 auto', [ABOVE_PHONE]: { minWidth: '7rem' } }]}
              >
                {tag.tag}
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: 500, flex: '0 1 auto', minWidth: 0, [ABOVE_PHONE]: { minWidth: '9rem' } }}
              >
                {tag.name}
              </Typography>
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  flex: '1 1 10rem',
                  minWidth: 0,
                  overflowWrap: 'break-word',
                  [PHONE]: { flex: '1 1 100%' },
                }}
              >
                {tag.description}
              </Typography>
              {/* CONDITIONAL AS A CLUSTER: an empty Stack takes a full line at phone width. */}
              {showTagActions && (
                <Stack
                  direction="row"
                  spacing={0.5}
                  useFlexGap
                  sx={{ flexWrap: 'nowrap', flex: '0 0 auto', [PHONE]: { flex: '1 1 100%' } }}
                >
                  {canEditTags && (
                    <Button
                      type="button"
                      variant="text"
                      disabled={locked}
                      onClick={() => onEditTag(tag)}
                      aria-label={`Edit tag ${tag.tag}`}
                      sx={ROW_ACTION_SX}
                    >
                      Edit
                    </Button>
                  )}
                  {/* Absent without the key, disabled at the ends — same split as the group arrows. */}
                  {canEditTags && (
                    <MoveArrows subject={`tag ${tag.tag}`} locked={locked} canUp={index > 0} canDown={index < tags.length - 1}
                      onMove={d => moveTag(index, d)} />
                  )}
                  {canDeleteTags && (
                    <Button
                      type="button"
                      variant="outlined"
                      color="error"
                      disabled={locked}
                      onClick={() => void handleDeleteTag(tag)}
                      aria-label={`Delete tag ${tag.tag}`}
                      sx={ROW_ACTION_SX}
                    >
                      Delete
                    </Button>
                  )}
                </Stack>
              )}
            </Box>
          ))
        )}
      </Box>

      {/* THE ROW AND ITS PADDING GO TOGETHER (else 16px of empty card). */}
      {canCreateTags && (
        <Box sx={{ px: 1.5, py: 1 }}>
          <Button
            type="button"
            variant="outlined"
            disabled={locked}
            onClick={onAddTag}
            aria-label={`Add tag to ${group.name}`}
            sx={ROW_ACTION_SX}
          >
            Add tag
          </Button>
        </Box>
      )}
    </Card>
  )
}

export default TagGroupCard
