import { useEffect, useMemo, useRef, useState } from 'react'
import type { JSX } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, get, post, put } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useConfirm } from '../ConfirmDialog'
import { Banner } from '../../ui/Banner'
import { OPTION_ROW_SX, highlight } from '../../ui/AsyncLookup'
import { ELLIPSIS_SX, PHONE, fieldTapTargetSx, tapTargetSx } from '../../ui/listStyles'
import type { Tag, TagGroup, TagRef } from '../../types'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'

// The task DETAILS tab's TAG EDITOR — ONE autocomplete entry box across every ACTIVE tag group, plus
// Re-tag.
//
// Operator decisions: the box holds badges for the WHOLE catalog, each suggestion showing its group as a
// dim hint. A badge reads the tag's `tag` CODE ("feature"), never its `name` ("Features") — the name is
// the board's group-by SECTION HEADING. CREATING A TAG IS
// NOT OFFERED (it needs a group, name and classifier description): Settings › Tasks › Tags.
// Not inside a `<form>` any more, but every control keeps `type="button"` so it stays form-safe.
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

/** One offerable tag with its group — the provenance the hint, the single-select rule and the ✕'s name
 *  read. */
type TagOption = { tag: Tag; group: TagGroup }

/** The full-replace payload as a SET: sorted, de-duplicated, so pick order does not change the body. */
const tagIdsOf = (ids: Iterable<string>): string[] => [...new Set(ids)].sort()

/* The Details tab's section frame and heading; `TaskDependencies` is the other section on it. */
export const PANEL_SX = { display: 'flex', flexDirection: 'column', gap: '0.5rem' } as const
export const HEADING_SX = { fontWeight: 600, color: 'text.primary' } as const

/* THE HEADING IS A BAR: `Tags` left, Re-tag right, instead of a row of its own — it put the Details tab
   back inside `.app-main`'s scrollport (`e2e/task-detail-fit.spec.ts`; 123px -> 79px at 1280x800). */
const HEADING_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  flexWrap: 'wrap',
  gap: '0.25rem 0.75rem',
  minWidth: 0,
  /* Under 480px the bar dissolves into the panel's column, so Re-tag can follow what it acts on. */
  [PHONE]: { display: 'contents' },
} as const
const RETAG_SX = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'flex-end',
  flex: '1 1 20rem',
  minWidth: 0,
  /* On a phone the button comes LAST, after the box or the sentence (`order`; DOM order is unchanged).
     `0 0 auto` first: in the column, the `1 1 20rem` basis would be a 320px HEIGHT. */
  [PHONE]: { order: 1, flex: '0 0 auto', justifyContent: 'flex-start' },
} as const

/* THE ENTRY BOX IS CAPPED: full-width reads as a paragraph field. 44rem holds ~8 badges. */
const BOX_SX = { maxWidth: '44rem' } as const

/* A tag BADGE inside the entry box — a second, deliberately different size from the board's
   `TAG_CHIP_SX` (field type size vs a 176px cell). Not `ui/StatusChip`: it never shrinks, and an
   unbounded operator-authored code would hard-clip mid-character. */
const BADGE_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '0.125rem',
  maxWidth: '100%',
  minWidth: 0,
  pl: '6px',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  bgcolor: 'action.hover',
} as const

const BADGE_TEXT_SX = {
  ...ELLIPSIS_SX,
  fontSize: '0.75rem',
  fontWeight: 600,
  lineHeight: 1.6,
  color: 'text.secondary',
} as const

/* The ✕ on a badge. DELIBERATELY NOT `tapTargetSx`'s 44px phone floor: inside a text field it would make
   the field ~52px. Removal also has Backspace and arrow-onto-badge + Delete, so this is never the only
   way. */
const BADGE_REMOVE_SX = {
  p: '2px',
  fontSize: '0.875rem',
  lineHeight: 1,
  color: 'text.secondary',
} as const

/* A suggestion row (`OPTION_ROW_SX`): code left, group dim on the RIGHT EDGE. */
const OPTION_CODE_SX = { ...ELLIPSIS_SX, fontSize: '0.8125rem', color: 'text.primary' } as const
const OPTION_GROUP_SX = { fontSize: '0.75rem', color: 'text.secondary', flex: '0 0 auto' } as const

/** What an untagged task reads for a READER. An EDITOR sees the box's placeholder instead. */
const UNTAGGED = 'Untagged'

/** The catalog has groups but no tags: say so rather than a box that finds nothing. Reader and editor
 *  copies differ — a reader has no Re-tag or Settings. */
const NO_TAGS_HINT = 'No tags yet.'
const NO_TAGS_HINT_EDITABLE = `${NO_TAGS_HINT} Add them in Settings › Tasks › Tags.`

/** The box's prompt, which its accessible name must CONTAIN (WCAG 2.5.3). */
const INPUT_LABEL = 'Add a tag'

/** Match on the CODE or the NAME: an operator who knows "Features" must find `feature`. */
function matches(option: TagOption, query: string): boolean {
  return (
    option.tag.tag.toLowerCase().includes(query) || option.tag.name.toLowerCase().includes(query)
  )
}

function startsWith(option: TagOption, query: string): boolean {
  return (
    option.tag.tag.toLowerCase().startsWith(query) || option.tag.name.toLowerCase().startsWith(query)
  )
}

/* WHAT THE POPUP SHOWS: PREFIX MATCHES FIRST, then the rest, each in the catalog's `position` order —
   never re-sorted alphabetically over the operator's order. */
function filterTagOptions(options: TagOption[], state: { inputValue: string }): TagOption[] {
  const query = state.inputValue.trim().toLowerCase()
  if (!query) return options
  const hits = options.filter(option => matches(option, query))
  return [...hits.filter(o => startsWith(o, query)), ...hits.filter(o => !startsWith(o, query))]
}

export function TaskTags({
  taskId,
  tags,
  readOnly = false,
}: {
  taskId: number
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
  const [retagging, setRetagging] = useState(false)
  /* SEPARATE FROM `retagging`: the button is dead while its confirm is open but not yet "Re-tagging…".
     Without it a second press supersedes the dialog and the first `retag()` silently returns
     (`useConfirm`'s contract). */
  const [confirming, setConfirming] = useState(false)
  const [actionError, setActionError] = useState('')
  const [confirm, confirmDialog] = useConfirm()
  /* WHICH WRITE IS ALLOWED TO ANSWER: overlapping PUTs could land an earlier selection last; only the
     newest response applies. */
  const writeSeq = useRef(0)
  /* SERVER TRUTH, READABLE FROM AN ASYNC TAIL, so a prop re-seed during the PUT is not reverted away by
     a stale closure. */
  const storedRef = useRef(stored)
  storedRef.current = stored

  /* THE CATALOG, fetched once on mount; its order decides badge and suggestion order. The `state` filter
     is applied HERE AS WELL: "a DRAFTED group's tags are never offerable" is this panel's guarantee
     (pinned by its test), independent of the route's default. */
  const { data: groups, error: groupsError } = useApiRead(['task-tag-groups'], async () => {
    const rows = await get<TagGroup[]>('/api/tags/groups')
    /* NORMALISED ONCE: only the top level is shape-checked, so a null `tags` would throw later (as
       `pages/Tasks.tsx` handles too). */
    return Array.isArray(rows)
      ? rows.filter(group => group.state === 'active').map(group => ({ ...group, tags: group.tags ?? [] }))
      : []
  }, 'Failed to load tag groups')

  /* RE-SEED FROM THE PROP ONLY WHEN THE SERVER'S SET CHANGES, keyed on ids not array identity. This is
     what shows a runner's own tag write without a reload; our own writes are already applied. */
  const propSelectionKey = useMemo(() => tagIdsOf(tags.map(ref => ref.id)).join(','), [tags])
  useEffect(() => {
    const next = new Set(propSelectionKey ? propSelectionKey.split(',') : [])
    setStored(next)
    setSelected(new Set(next))
  }, [propSelectionKey])

  const active = groups ?? []

  /* EVERY OFFERABLE TAG, FLATTENED, in the operator's order — which also orders the badges (`picked`
     reads it), so a newly added tag does not jump to the end. */
  const options = useMemo<TagOption[]>(
    () => active.flatMap(group => group.tags.map(tag => ({ tag, group }))),
    [active],
  )

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

  function commit(next: TagOption[]) {
    void write(tagIdsOf([...resolveSingleSelect(next), ...unresolved]))
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
          `${picked.length} tag${picked.length === 1 ? '' : 's'} set now ` +
          `(${picked.map(option => option.tag.tag).join(', ')}) will be replaced by its choice.`,
        confirmLabel: 'Re-tag',
      })
      setConfirming(false)
      /* The codes were snapshotted before the await on purpose: the sentence the operator agreed to must
         not change under them. The write classifies against server truth. */
      if (!ok) return
    }
    setRetagging(true)
    setActionError('')
    /* CLAIMS THE WRITE SEQUENCE BEFORE THE REQUEST, so an in-flight pick cannot land over it. */
    writeSeq.current++
    try {
      const result = await post<TaskTagsResponse>(`/api/tasks/${taskId}/retag`)
      const settled = new Set((result?.tags ?? []).map(ref => ref.id))
      setStored(settled)
      setSelected(new Set(settled))
    } catch (err) {
      /* THE SERVER'S `detail`, VERBATIM, in an inline Banner (409 no active group / catalogue changed,
         503 unconfigured, 504 timeout, 502 unusable verdict). No toasts in this app. */
      setActionError(apiDetail(err, 'Re-tag failed'))
    } finally {
      setRetagging(false)
    }
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
  if (groups === null || groups.length === 0) return null

  /* FROZEN BY A CLASSIFICATION, NOT BY A PICK: Re-tag REPLACES the selection, so a mid-classification
     pick would be overwritten. */
  const frozen = retagging || confirming

  return (
    <Box sx={PANEL_SX} data-testid="task-tags">
      <Box sx={HEADING_ROW_SX}>
        <Typography variant="body2" sx={HEADING_SX}>
          Tags
        </Typography>
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
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: '0.375rem', minWidth: 0 }}>
            {picked.map(option => (
              <Box
                key={option.tag.id}
                component="span"
                title={`${option.tag.name} · ${option.group.name}`}
                sx={{ ...BADGE_SX, px: '6px' }}
              >
                {/* The test id sits on the TEXT node in both renders. */}
                <Box component="span" data-testid="task-tag-chip" sx={BADGE_TEXT_SX}>
                  {option.tag.tag}
                </Box>
              </Box>
            ))}
          </Box>
        ) : (
          <Untagged />
        )
      ) : (
        <Autocomplete
          multiple
          id={`task-tags-input-${taskId}`}
          options={options}
          value={picked}
          disabled={frozen}
          sx={BOX_SX}
          /* A PICKED TAG LEAVES THE LIST (removal is the ✕ and Backspace). */
          filterSelectedOptions
          /* THE FIRST MATCH IS PRE-HIGHLIGHTED, so type-then-Enter works. */
          autoHighlight
          openOnFocus
          /* NO GLOBAL CLEAR ✕: wiping every group's tags has no undo. */
          disableClearable
          isOptionEqualToValue={(a, b) => a.tag.id === b.tag.id}
          getOptionLabel={option => option.tag.tag}
          filterOptions={filterTagOptions}
          noOptionsText="No matching tag"
          onChange={(_event, next) => commit(next)}
          renderValue={(value, getItemProps) =>
            value.map((option, index) => {
              /* `key` out (React 19 spread-key warning), `disabled` out (invalid on a `<span>`); the rest is
                 what MUI's arrow-key/Backspace handling addresses badges by. */
              const { key, onDelete, disabled, ...itemProps } = getItemProps({ index })
              return (
                <Box
                  key={key}
                  {...itemProps}
                  component="span"
                  title={`${option.tag.name} · ${option.group.name}`}
                  sx={BADGE_SX}
                >
                  <Box component="span" data-testid="task-tag-chip" sx={BADGE_TEXT_SX}>
                    {option.tag.tag}
                  </Box>
                  <IconButton
                    // `type="button"` (see the file header).
                    type="button"
                    aria-label={`Remove tag ${option.tag.tag} from ${option.group.name}`}
                    disabled={disabled}
                    onClick={onDelete}
                    sx={BADGE_REMOVE_SX}
                  >
                    <Box component="span" aria-hidden="true" sx={{ lineHeight: 1 }}>
                      ×
                    </Box>
                  </IconButton>
                </Box>
              )
            })
          }
          renderOption={(props, option, state) => {
            const { key, ...rest } = props
            return (
              <Box component="li" key={key} {...rest}>
                <Box sx={OPTION_ROW_SX}>
                  <Box component="span" sx={OPTION_CODE_SX}>
                    {highlight(option.tag.tag, state.inputValue)}
                  </Box>
                  {/* THE GROUP, dim, on the right — the only thing saying which vocabulary a code is from. */}
                  <Box component="span" sx={OPTION_GROUP_SX}>
                    {option.group.name}
                  </Box>
                </Box>
              </Box>
            )
          }}
          renderInput={params => (
            <TextField
              {...params}
              /* `margin="none"`, against the theme's `MuiTextField` dense margin: `PANEL_SX`'s `gap` owns
                 the spacing (it cost the Details tab 12px). */
              margin="none"
              sx={fieldTapTargetSx}
              placeholder={picked.length === 0 ? `${INPUT_LABEL}…` : ''}
              slotProps={{
                ...params.slotProps,
                /* WCAG 2.5.3: the accessible name contains the visible prompt. No visible `<label>`: the
                   heading already says Tags. */
                htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT, 'aria-label': INPUT_LABEL },
              }}
            />
          )}
        />
      )}

      {actionError && <Banner severity="error">{actionError}</Banner>}
      {confirmDialog}
    </Box>
  )
}

/** What an untagged task reads for a reader: one string in one register. */
function Untagged(): JSX.Element {
  return (
    <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="task-tag-untagged">
      {UNTAGGED}
    </Typography>
  )
}
