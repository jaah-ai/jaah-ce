import { useEffect, useMemo, useRef, useState } from 'react'
import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail, post, put } from '../../api'
import { useTagGroups } from '../../hooks/useTagGroups'
import { countOf } from '../../lib/humanize'
import { useSubmit } from '../../hooks/useSubmit'
import { useConfirm } from '../ConfirmDialog'
import { Banner } from '../../ui/Banner'
import type { LookupOption } from '../../ui/AsyncLookup'
import { AsyncMultiLookup, CHIP_SX, CHIP_TEXT_SX } from '../../ui/AsyncMultiLookup'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import { FONT_WEIGHT, grid } from '../../theme'
import type { Tag, TagGroup, TagRef } from '../../types'
import type { Explain } from '../../ui/explain'
import HelpLabel, { ExplainMoreButton } from '../../ui/HelpLabel'
import Tip from '../../ui/Tip'

// The task DETAILS tab's TAG EDITOR — ONE `ui/AsyncMultiLookup` entry box across every ACTIVE tag group, plus
// Re-tag. The box owns the chips, ✕, Backspace, rows and field; this file owns the catalogue and the writes.
//
// Operator decisions: the box holds badges for the WHOLE catalog, each suggestion showing its group as a
// dim hint. A badge reads the tag's `tag` CODE ("feature"), never its `name` ("Features") — the name is
// the board's group-by SECTION HEADING. CREATING A TAG IS
// NOT OFFERED (it needs a group, name and classifier description): Settings › Tasks › Tags.
// Every control is `type="button"`, so the panel is safe inside a `<form>`.
//
// THE CATALOG IS FETCHED (`GET /api/tags/groups`, active-only, `position` order); THE SELECTION is the
// `tags` prop off the page's task payload. `TaskDetailPage` refetches on every `task` SSE event (both
// writes emit `action="tagged"`), and a tag write does not move `updated_at`, so the prop must re-seed a
// mounted panel (`propSelectionKey`).
// THAT PROP IS TRUSTWORTHY ONLY BECAUSE THE PAGE RESCUES IT: per-task mutation routes serialise `tags`
// as `[]`, so each of `TaskDetailPage`'s mutation merges re-attaches the loaded tags (see its
// `submitAnswer`); otherwise the next write here would DELETE unseen groups. Anything writing a mutation
// response onto a `PipelineTask` owes `tags` the same clause.
// THE OPTIONS COME FROM THE CATALOG, never from `tags` (or the editor could only remove).

/** What both writes answer with: the resulting refs only (see the server's `TaskTagsOut`). */
type TaskTagsResponse = { tags: TagRef[] }

/** One offerable tag with its group — the provenance the hint, the single-select rule and the hover read —
 *  and the box's row for it (`key` the id, `label` the code, `hint` the group). */
type TagOption = { tag: Tag; group: TagGroup; lookup: LookupOption }

/** The full-replace payload as a SET: sorted, de-duplicated, so pick order does not change the body. */
const tagIdsOf = (ids: Iterable<string>): string[] => [...new Set(ids)].sort()

/* The Details tab's section frame and heading; `TaskDependencies` is the other section on it. */
export const PANEL_SX = { display: 'flex', flexDirection: 'column', gap: 1 } as const
export const HEADING_SX = { fontWeight: FONT_WEIGHT.semibold, color: 'text.primary' } as const

/* The heading's explanation: the section is the setting. */
const TAGS_HELP: Explain = {
  tip: 'Labels that group the board; Re-tag reclassifies it.',
  more: 'Tags come from the tag groups published in Settings. Re-tag classifies the task again from its description and outcome, and asks first when that would replace tags set by hand.',
  docs: 'console-reference/tasks/task-detail',
}

/* THE HEADING IS A BAR: `Tags` left, Re-tag right, instead of a row of its own — it put the Details tab
   back inside `.app-main`'s scrollport (`e2e/task-detail-fit.spec.ts`; 123px -> 79px at 1280x800). */
const HEADING_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  flexWrap: 'wrap',
  rowGap: 0.5,
  columnGap: 2,
  minWidth: 0,
  /* Under 480px the bar dissolves into the panel's column, so Re-tag can follow what it acts on. */
  [PHONE]: { display: 'contents' },
} as const
const RETAG_SX = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-end',
  flex: `1 1 ${grid(53)}`,
  minWidth: 0,
  /* On a phone the button comes LAST, after the box or the sentence (`order`; DOM order is unchanged).
     `0 0 auto` first: in the column, the `1 1 grid(53)` basis would be a 318px HEIGHT. */
  [PHONE]: { order: 1, flex: '0 0 auto', justifyContent: 'flex-start' },
} as const

/* THE ENTRY BOX IS CAPPED: full-width reads as a paragraph field. grid(117) (702px) holds ~8 badges. */
const BOX_SX = { maxWidth: grid(117) } as const

/** What an untagged task reads for a READER. An EDITOR sees the box's placeholder instead. */
const UNTAGGED = 'Untagged'

/** The catalog has groups but no tags: say so rather than a box that finds nothing. Reader and editor
 *  copies differ — a reader has no Re-tag or Settings. */
const NO_TAGS_HINT = 'No tags yet.'
const NO_TAGS_HINT_EDITABLE = `${NO_TAGS_HINT} Add them in Settings › Tasks › Tags.`

/** The box's prompt, which its accessible name must CONTAIN (WCAG 2.5.3). */
const INPUT_LABEL = 'Add a tag'

/** Match on the CODE or the NAME: an operator who knows "Features" must find `feature`. */
const searchedOf = (option: TagOption): string[] => [option.tag.tag.toLowerCase(), option.tag.name.toLowerCase()]

/* WHAT THE POPUP SHOWS: PREFIX MATCHES FIRST, then the rest, each in the catalog's `position` order —
   never re-sorted alphabetically over the operator's order. The empty query matches every tag. */
function filterTagOptions(options: TagOption[], typed: string): TagOption[] {
  const query = typed.toLowerCase()
  const prefix = (option: TagOption) => searchedOf(option).some(text => text.startsWith(query))
  const hits = options.filter(option => searchedOf(option).some(text => text.includes(query)))
  return [...hits.filter(prefix), ...hits.filter(option => !prefix(option))]
}

export function TaskTags({
  taskId,
  orgId,
  tags,
  readOnly = false,
}: {
  taskId: number
  /** The task's own org: the catalogue read is that org's (`useTagGroups`'s `orgId`), never the viewer's. */
  orgId: string | null
  /** The task's CURRENT active-group tags, off the task payload the page already holds. */
  tags: TagRef[]
  /* READ-ONLY IS ABSENCE, NOT DISABLEMENT (`TaskDependsOn`'s `readOnly` contract): a `tasks:view` admin
     sees every tag and loses the box, the ✕ and Re-tag. */
  readOnly?: boolean
}): JSX.Element | null {
  /* TWO SETS: `stored` is server truth, `selected` moves instantly on a pick. A refused write reverts
     `selected` to `stored`; the box is never frozen between picks, so it keeps focus. */
  const [stored, setStored] = useState<Set<string>>(() => new Set(tags.map(ref => ref.id)))
  const [selected, setSelected] = useState<Set<string>>(() => new Set(tags.map(ref => ref.id)))
  const [busy, setBusy] = useState(false)
  const { busy: retagging, formError: actionError, setFormError: setActionError, run: runRetag } = useSubmit()
  /* SEPARATE FROM `retagging`: the button is dead while its confirm is open but not yet "Re-tagging…".
     Without it a second press supersedes the dialog and the first `retag()` silently returns
     (`useConfirm`'s contract). */
  const [confirming, setConfirming] = useState(false)
  const [confirm, confirmDialog] = useConfirm()
  /* WHICH WRITE IS ALLOWED TO ANSWER: overlapping PUTs could land an earlier selection last; only the
     newest response applies. */
  const writeSeq = useRef(0)
  /* SERVER TRUTH, READABLE FROM AN ASYNC TAIL, so a prop re-seed during the PUT is not reverted away by
     a stale closure. */
  const storedRef = useRef(stored)
  storedRef.current = stored

  /* THE CATALOG (`hooks/useTagGroups.ts`, the board's read); its order decides badge and suggestion order. The
     `state` filter is applied HERE AS WELL: "a DRAFTED group's tags are never offerable" is this panel's guarantee
     (pinned by its test), independent of the route's default. A null `tags` would throw later, so `?? []`.
     THE TASK'S ORG'S catalogue, never the viewer's: an operator on another org's task is offered only tags its
     write can resolve. */
  const groupsQuery = useTagGroups({ orgId })
  const groupsError = groupsQuery.error ? apiDetail(groupsQuery.error, 'Failed to load tag groups') : ''
  const active = useMemo<TagGroup[]>(
    () => (groupsQuery.data ?? []).filter(group => group.state === 'active').map(group => ({ ...group, tags: group.tags ?? [] })),
    [groupsQuery.data],
  )

  /* RE-SEED FROM THE PROP ONLY WHEN THE SERVER'S SET CHANGES, keyed on ids not array identity. This is
     what shows a runner's own tag write without a reload; our own writes are already applied. */
  const propSelectionKey = useMemo(() => tagIdsOf(tags.map(ref => ref.id)).join(','), [tags])
  useEffect(() => {
    const next = new Set(propSelectionKey ? propSelectionKey.split(',') : [])
    setStored(next)
    setSelected(new Set(next))
  }, [propSelectionKey])

  /* EVERY OFFERABLE TAG, FLATTENED, in the operator's order — which also orders the badges (`picked`
     reads it), so a newly added tag does not jump to the end. */
  const options = useMemo<TagOption[]>(
    () => active.flatMap(group => group.tags.map(tag => ({ tag, group, lookup: { key: tag.id, label: tag.tag, hint: group.name } }))),
    [active],
  )
  const byId = useMemo(() => new Map(options.map(option => [option.tag.id, option])), [options])

  const picked = useMemo(() => options.filter(option => selected.has(option.tag.id)), [options, selected])

  /* A SELECTED ID THIS CATALOG CANNOT RESOLVE IS KEPT, NOT DROPPED: a group published after mount would
     otherwise have its tags deleted on a guess. (A drafted group is protected server-side,
     `app/task_tags.py`.) The cost: a drafted group's ids get a 422 until reload — a refusal beats a
     silent deletion. */
  const unresolved = useMemo(() => {
    const known = new Set(options.map(option => option.tag.id))
    return [...selected].filter(id => !known.has(id))
  }, [options, selected])

  /* ONE PICK IN A SINGLE-SELECT GROUP, enforced client-side (the ask's rule): the newest pick wins —
     MUI appends, so walk BACKWARDS. Otherwise the server 422s. */
  function resolveSingleSelect(next: TagOption[]): string[] {
    const kept: string[] = []
    const seen = new Set<string>()
    for (const option of [...next].reverse()) {
      if (!option.group.multi_select) {
        if (seen.has(option.group.id)) continue
        seen.add(option.group.id)
      }
      kept.push(option.tag.id)
    }
    return kept
  }

  async function write(nextIds: string[]) {
    /* THE PAYLOAD IS THE WHOLE SELECTION ACROSS EVERY GROUP (a FULL REPLACE), plus the unresolved ids. */
    const seq = ++writeSeq.current
    setSelected(new Set(nextIds))
    setBusy(true)
    setActionError('')
    try {
      const result = await put<TaskTagsResponse>(`/api/tasks/${taskId}/tags`, { tag_ids: nextIds })
      if (seq !== writeSeq.current) return
      // Server truth, never the optimistic set.
      const settled = new Set((result?.tags ?? []).map(ref => ref.id))
      setStored(settled)
      setSelected(new Set(settled))
    } catch (err) {
      if (seq !== writeSeq.current) return
      setActionError(apiDetail(err, 'Failed to save tags'))
      setSelected(new Set(storedRef.current))
    } finally {
      if (seq === writeSeq.current) setBusy(false)
    }
  }

  function commit(next: LookupOption[]) {
    const resolved = next.flatMap(lookup => byId.get(lookup.key) ?? [])
    void write(tagIdsOf([...resolveSingleSelect(resolved), ...unresolved]))
  }

  async function retag() {
    /* THE CONFIRM FIRES ONLY WHEN THERE IS SOMETHING TO LOSE: Re-tag OVERWRITES hand-set tags with no
       provenance record, so it names (by code) the tags it will replace. */
    if (picked.length > 0) {
      setConfirming(true)
      const ok = await confirm({
        title: 'Re-tag this task?',
        message:
          `The classifier replaces this task's tags in every active group. ` +
          `${countOf(picked.length, 'tag')} set now ` +
          `(${picked.map(option => option.tag.tag).join(', ')}) will be replaced by its choice.`,
        confirmLabel: 'Re-tag',
      })
      setConfirming(false)
      /* The codes were snapshotted before the await on purpose: the sentence the operator agreed to must
         not change under them. The write classifies against server truth. */
      if (!ok) return
    }
    /* THE SERVER'S `detail`, VERBATIM, in an inline Banner (409 no active group / catalogue changed,
       503 unconfigured, 504 timeout, 502 unusable verdict). No toasts in this app. */
    await runRetag('Re-tag failed', async () => {
      /* CLAIMS THE WRITE SEQUENCE BEFORE THE REQUEST, so an in-flight pick cannot land over it. */
      writeSeq.current++
      const result = await post<TaskTagsResponse>(`/api/tasks/${taskId}/retag`)
      const settled = new Set((result?.tags ?? []).map(ref => ref.id))
      setStored(settled)
      setSelected(new Set(settled))
    })
  }

  // A failed catalog read costs the whole editor, so it says so.
  if (groupsError) {
    return (
      <Box sx={PANEL_SX} data-testid="task-tags">
        <Typography variant="body2" sx={HEADING_SX}>
          Tags
        </Typography>
        <Banner severity="error">{groupsError}</Banner>
      </Box>
    )
  }
  // Not loaded yet: render nothing (`TaskDependsOn`'s posture). No active group: nothing to show or
  // re-tag into (the server 409s), so no card either; Settings › Tasks › Tags is where groups are made.
  if (groupsQuery.data === undefined || active.length === 0) return null

  /* FROZEN BY A CLASSIFICATION, NOT BY A PICK: Re-tag REPLACES the selection, so a mid-classification
     pick would be overwritten. */
  const frozen = retagging || confirming

  return (
    <Box sx={PANEL_SX} data-testid="task-tags">
      <Box sx={HEADING_ROW_SX}>
        <Typography variant="body2" sx={HEADING_SX}>
          <HelpLabel label="Tags" help={TAGS_HELP} />
        </Typography>
        <ExplainMoreButton label="Tags" help={TAGS_HELP} />
        {/* RE-TAG RENDERS WHATEVER THE CATALOG SAYS (at least one active group, or there is no card). */}
        {!readOnly && (
          <Box sx={RETAG_SX}>
            <Button
              // `type="button"` on every control (see the header).
              type="button"
              sx={tapTargetSx}
              variant="outlined"
              color="inherit"
              /* DISABLED WITH A BUSY LABEL WHILE IN FLIGHT: a classification can take the whole summary
                 timeout, and re-presses would race. A tag write in flight disables it too. */
              disabled={frozen || busy}
              onClick={() => void retag()}
            >
              {retagging ? 'Re-tagging…' : 'Re-tag'}
            </Button>
          </Box>
        )}
      </Box>

      {options.length === 0 ? (
        <>
          {readOnly && picked.length === 0 && <Untagged />}
          <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="task-tags-empty-catalog">
            {readOnly ? NO_TAGS_HINT : NO_TAGS_HINT_EDITABLE}
          </Typography>
        </>
      ) : readOnly ? (
        /* A READER GETS THE BADGES ONLY, not a disabled box. */
        picked.length > 0 ? (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', minWidth: 0 }}>
            {picked.map(option => (
              <Tip key={option.tag.id} title={hoverOf(option)}><Box component="span" sx={{ ...CHIP_SX, px: 1 }}>
                {/* The test id sits on the TEXT node in both renders. */}
                <Box component="span" data-testid="task-tag-chip" sx={CHIP_TEXT_SX}>
                  {option.tag.tag}
                </Box>
              </Box></Tip>
            ))}
          </Box>
        ) : (
          <Untagged />
        )
      ) : (
        <Box sx={BOX_SX}>
          <AsyncMultiLookup
            id={`task-tags-input-${taskId}`}
            value={picked.map(option => option.lookup)}
            onChange={commit}
            /* The cached catalogue, filtered here: the box's `filterOptions` passes rows through untouched. */
            fetchOptions={query => Promise.resolve(filterTagOptions(options, query).map(option => option.lookup))}
            fetchOnFocus
            disabled={frozen}
            noOptionsText="No matching tag"
            /* WCAG 2.5.3: the accessible name contains the visible prompt. No visible `<label>`: the heading
               already says Tags. */
            ariaLabel={INPUT_LABEL}
            placeholder={`${INPUT_LABEL}…`}
            itemNoun="tag"
            chipTitle={lookup => { const option = byId.get(lookup.key); return option ? hoverOf(option) : lookup.label }}
            chipTestId="task-tag-chip"
            /* Against the theme's dense margin: `PANEL_SX`'s `gap` owns the spacing (it cost the Details tab 12px). */
            margin="none"
          />
        </Box>
      )}

      {actionError && <Banner severity="error">{actionError}</Banner>}
      {confirmDialog}
    </Box>
  )
}

/** A badge's hover: the reader's only route to the display name and group. */
const hoverOf = (option: TagOption): string => `${option.tag.name} · ${option.group.name}`

/** What an untagged task reads for a reader: one string in one register. */
function Untagged(): JSX.Element {
  return (
    <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="task-tag-untagged">
      {UNTAGGED}
    </Typography>
  )
}
