import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useCan } from '../../../auth'
import { type ConfirmOptions, danger } from '../../../components/ConfirmDialog'
import { useSubmit } from '../../../hooks/useSubmit'
import { countOf } from '../../../lib/humanize'
import Banner from '../../../ui/Banner'
import StatusChip from '../../../ui/StatusChip'
import { ABOVE_PHONE, PHONE, ROW_ACTION_SX, monoSx } from '../../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, SIZE, grid } from '../../../theme'
import type { Tag, TagGroup } from '../../../types'
import {
  deleteTag, deleteTagGroup, newTagUrl, reorderTags, tagGroupUrl, tagGroupUsage, tagUrl, tagUsage, updateTagGroup,
} from './api'

/* ONE GROUP, ONE CARD, WITH ITS OWN TAGS INSIDE IT — the requirement: "what does *By type* offer?" is
 * answered by being on screen, not by changing a filter on two flat rosters.
 *
 * The card owns every write scoped to ONE group (activation, deletes, tag reorder). The group reorder
 * (needs the whole list) is the panel's. Editing is a page: the group's name links to `TagGroupEditPage`,
 * each tag's name to `TagEditPage`, and Add tag to the new-tag page.
 *
 * ── WHICH KEY EACH CONTROL ASKS ──
 * THREE SEPARATELY GRANTABLE KEYS, named after the WRITE (as `TagsSection.tsx`), checked against
 * `apps/api/app/routes/tags.py` — cited by symbol, never line:
 *   `tags:edit`   — Activate / Move to draft (`update_tag_group`), the tag arrows (`reorder_tags`).
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

/* The confirmation's count is QUERIED, never guessed; a failed query says the number could not be read
 * rather than becoming a silent zero. Shared with the record pages' Delete. */
export async function confirmedCount(read: () => Promise<{ task_count: number }>): Promise<number | null> {
  try {
    return (await read()).task_count
  } catch {
    return null
  }
}

/** The group delete's confirmation, from its queried task count (`null`: unread). */
export function groupDeleteConfirm(group: TagGroup, count: number | null): ConfirmOptions {
  const impact = count === null ? 'The task count could not be read, so this may also remove tag assignments from tasks.'
    : count === 0 ? 'No task carries one of its tags, so nothing else changes.'
      : `Its tags also come off ${countOf(count, 'task')}.`
  return danger('Delete group', {
    title: 'Delete tag group',
    identity: `${group.name} · ${countOf(group.tags.length, 'tag')}`,
    message: `This deletes the group and every tag in it. ${impact} This cannot be undone.`,
  })
}

/** A tag delete's confirmation, from its queried task count (`null`: unread). */
export function tagDeleteConfirm(group: TagGroup, tag: Tag, count: number | null): ConfirmOptions {
  const impact = count === null ? 'The task count could not be read, so this may also remove the tag from tasks.'
    : count === 0 ? 'No task carries it, so nothing else changes.'
      : `It also comes off ${countOf(count, 'task')}.`
  return danger('Delete tag', { title: 'Delete tag', identity: `${group.name} · ${tag.tag}`, message: `This deletes “${tag.name}”. ${impact} This cannot be undone.` })
}

/* Arrow buttons: 44px `minWidth` because the touch target is min(width, height); MUI's stock 64px would
 * triple a glyph button. */
const ARROW_SX = [...ROW_ACTION_SX, { minWidth: SIZE.tapTarget, px: 0.5, fontSize: FONT_SIZE.base }]

/** The up/down pair a group and a tag share: disabled in flight (`locked`) or at an end of its list; the
 *  caller gates it on `tags:edit`. `subject` completes "Move <subject> up" (the group name, `tag <code>`). */
function MoveArrows({ subject, locked, canUp, canDown, onMove }: {
  subject: string; locked: boolean; canUp: boolean; canDown: boolean; onMove: (direction: -1 | 1) => void
}): JSX.Element {
  const arrow = (direction: -1 | 1, word: string, glyph: string, can: boolean) => (
    <Button type="button" variant="text" disabled={locked || !can} onClick={() => onMove(direction)} aria-label={`Move ${subject} ${word}`} sx={ARROW_SX}>
      <Box component="span" aria-hidden="true">{glyph}</Box>
    </Button>
  )
  return <>{arrow(-1, 'up', '↑', canUp)}{arrow(1, 'down', '↓', canDown)}</>
}

/** A group's and a tag's Delete: `label` names what goes (`Delete <group>`, `Delete tag <code>`). */
function DeleteButton({ label, locked, onClick }: { label: string; locked: boolean; onClick: () => void }): JSX.Element {
  return (
    <Button type="button" variant="outlined" color="error" disabled={locked} onClick={onClick} aria-label={label} sx={ROW_ACTION_SX}>
      Delete
    </Button>
  )
}

/* A wrapping row, vertically centred: the card's header, its name cluster and a tag row. */
const WRAP_ROW_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center' } as const

/* The tag row: code, name, description, actions, and the wrap does the rest (≤480px: description, then
 * actions, each on its own line). Nothing is truncated — a shortened code names nothing. */
const TAG_ROW_SX = {
  ...WRAP_ROW_SX, columnGap: 1, rowGap: 0.5, px: 1.5, py: 0.5, borderTop: '1px solid', borderColor: 'divider',
}

export function TagGroupCard({ group, canMoveUp, canMoveDown, panelBusy, onMoveGroup, onChanged, confirm }: {
  group: TagGroup
  canMoveUp: boolean
  canMoveDown: boolean
  /** A write the PANEL has in flight (the group reorder), which this card's `busy` cannot see. */
  panelBusy: boolean
  /** Whole-list group reorder (needs every group's id). */
  onMoveGroup: (direction: -1 | 1) => Promise<void>
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
  const showActions = canEditTags || canDeleteTags

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
    void run(
      isActive ? 'Failed to move the group to draft.' : 'Failed to activate the group.',
      () => updateTagGroup(group.id, { state: isActive ? 'draft' : 'active' }),
    )
  }

  /* Both deletes: the confirmation quotes the count `usage` reads, then the write. */
  async function deleteAfterConfirm(
    usage: () => Promise<{ task_count: number }>, confirmOf: (count: number | null) => ConfirmOptions,
    fallback: string, write: () => Promise<unknown>,
  ) {
    if (!canDeleteTags || locked) return
    if (!(await confirm(confirmOf(await confirmedCount(usage))))) return
    void run(fallback, write)
  }
  const handleDeleteGroup = () => deleteAfterConfirm(
    () => tagGroupUsage(group.id), count => groupDeleteConfirm(group, count), 'Failed to delete the tag group.', () => deleteTagGroup(group.id))
  const handleDeleteTag = (tag: Tag) => deleteAfterConfirm(
    () => tagUsage(tag.id), count => tagDeleteConfirm(group, tag, count), 'Failed to delete the tag.', () => deleteTag(tag.id))

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
      <Box sx={{ ...WRAP_ROW_SX, columnGap: 1.5, rowGap: 1, px: 1.5, py: 1, borderBottom: '1px solid', borderColor: 'divider' }}>
        <Box sx={{ ...WRAP_ROW_SX, columnGap: 1, rowGap: 0.5, flex: `1 1 ${grid(32)}`, minWidth: 0 }}>
          <Typography
            variant="subtitle2"
            component="h3"
            /* `break-word`, never `anywhere`: wrap at spaces, split only an unbreakable token. */
            sx={{ fontWeight: FONT_WEIGHT.semibold, minWidth: 0, overflowWrap: 'break-word' }}
          >
            {/* The group's page: a real link, so Tab + Enter opens it like any other. */}
            <Link component={RouterLink} to={tagGroupUrl(group.id)} underline="hover" color="inherit">
              {group.name}
            </Link>
          </Typography>
          <StatusChip label={group.state} tone={isActive ? 'success' : 'warning'} data-status={group.state} />
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {group.multi_select ? 'Several tags per task' : 'One tag per task'} · {countOf(tags.length, 'tag')}
          </Typography>
        </Box>

        {/* THE WHOLE CLUSTER IS CONDITIONAL: an empty `Stack` would hold the name row open. */}
        {showActions && (
          <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap', flex: '0 0 auto', alignItems: 'center' }}>
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
            {/* Rendered = key held, so `canMoveUp`/`canMoveDown` are purely positional (see the header). */}
            {canEditTags && (
              <MoveArrows subject={group.name} locked={locked} canUp={canMoveUp} canDown={canMoveDown} onMove={d => void onMoveGroup(d)} />
            )}
            {canDeleteTags && <DeleteButton label={`Delete ${group.name}`} locked={locked} onClick={() => void handleDeleteGroup()} />}
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
                sx={[monoSx, { fontSize: FONT_SIZE.md, fontWeight: FONT_WEIGHT.semibold, flex: '0 0 auto', [ABOVE_PHONE]: { minWidth: grid(18.5) } }]}
              >
                {tag.tag}
              </Typography>
              <Typography
                variant="body2"
                sx={{ fontWeight: FONT_WEIGHT.medium, flex: '0 1 auto', minWidth: 0, [ABOVE_PHONE]: { minWidth: grid(24) } }}
              >
                <Link component={RouterLink} to={tagUrl(group.id, tag.id)} underline="hover" color="inherit">
                  {tag.name}
                </Link>
              </Typography>
              <Typography
                variant="body2"
                sx={{
                  color: 'text.secondary',
                  flex: `1 1 ${grid(26.5)}`,
                  minWidth: 0,
                  overflowWrap: 'break-word',
                  [PHONE]: { flex: '1 1 100%' },
                }}
              >
                {tag.description}
              </Typography>
              {/* CONDITIONAL AS A CLUSTER: an empty Stack takes a full line at phone width. */}
              {showActions && (
                <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'nowrap', flex: '0 0 auto', [PHONE]: { flex: '1 1 100%' } }}>
                  {/* Absent without the key, disabled at the ends — same split as the group arrows. */}
                  {canEditTags && (
                    <MoveArrows subject={`tag ${tag.tag}`} locked={locked} canUp={index > 0} canDown={index < tags.length - 1}
                      onMove={d => moveTag(index, d)} />
                  )}
                  {canDeleteTags && <DeleteButton label={`Delete tag ${tag.tag}`} locked={locked} onClick={() => void handleDeleteTag(tag)} />}
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
            variant="outlined"
            component={RouterLink}
            to={newTagUrl(group.id)}
            disabled={locked}
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
