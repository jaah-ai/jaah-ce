import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { ReactNode } from 'react'
import type { QuestionOption } from '../../types'
import { CodeBlock } from '../../ui/CodeBlock'

/* ── THE PARTS EVERY QUESTION-ROUND SURFACE RENDERS WITH ────────────────────────────────────────────
   One home for the rules about the same agent-authored strings: an option's label, description, mono
   preview, `(Recommended)` mark, and a round's header. Each rule was measured (a three-line clamp that
   unclamps at <=480px, a bounded `title` fallback, a `role="img"` name for box-drawing mockups, a 4000-char
   backstop); a second spelling is a divergence.
   Its own file because `QATrail` is rendered by `GateForm` and by the Q&A subtab; the shared parts inside
   either would create an import cycle. */

/* The option-label bound — ONE definition for every placement (single-choice button, multi-choice row,
   settled trail's option list).
   Three LINES and an ellipsis, never a character clamp; `title={opt}` is the unclamped fallback.
   `anywhere`/`break-word` keep paths and URLs inside the panel (R1).
   UNCLAMPED AT <=480px: touch has no hover to recover a cut label, and the panel is not a scrollport
   there (`SCROLLPORT_PHONE_SX`). `display: block`, not an unclamped `-webkit-box`.
   The `@media` key is LAST: media queries add no specificity, so order decides (`SCROLLPORT_PHONE_SX`);
   anything spread after this re-declares `display`/`overflow` at every width. */
export const OPTION_LABEL_SX = {
  display: '-webkit-box',
  WebkitBoxOrient: 'vertical',
  WebkitLineClamp: 3,
  overflow: 'hidden',
  overflowWrap: 'anywhere',
  wordBreak: 'break-word',
  '@media (max-width:480px)': { display: 'block', WebkitLineClamp: 'unset', overflow: 'visible' },
} as const

/* ── What an option MEANS, not just what it is called ───────────────────────────────────────────────
   The Answer tab once rendered three bare labels where the terminal picker showed three lines of
   explanation each (`QuestionOption.description` was stored but unread).
   ALWAYS VISIBLE under its label (the operator's choice): not a tooltip (no hover on phones), not behind
   an expand toggle (the decision turns on it).
   ONE definition for every placement (option button, multi-choice row, `QATrail`).
   Three lines with `title` for the rest; UNCLAMPED at <=480px (no hover, no scrollport —
   `SCROLLPORT_PHONE_SX`). The clamp is the only height mechanism.
   `inherit` AT 0.82 OPACITY, NOT `text.secondary` (MEASURED): translucent black over a `contained`
   `primary.main` button was unreadable and darkened the button, though every assertion passed (a
   failure only a look at the rendered page catches). Inheriting mutes relative to its own surface
   (`primary.contrastText` or `text.primary`) — one rule, not per-placement colours. Size comes from
   `variant="caption"` at the placements.
   The `@media` key is LAST, for the same specificity-order reason. */
const OPTION_DESCRIPTION_SX = {
  display: '-webkit-box',
  WebkitBoxOrient: 'vertical',
  WebkitLineClamp: 3,
  overflow: 'hidden',
  overflowWrap: 'anywhere',
  wordBreak: 'break-word',
  color: 'inherit',
  opacity: 0.82,
  /* Full width of its holder, so the clamp counts against a definite line length (inside a column
     Button an auto-width `-webkit-box` would clamp against its own content). `maxWidth`: R1. */
  width: '100%',
  maxWidth: '100%',
  // Normal case and weight explicitly: on a `contained` Button it would inherit uppercase and 500 weight.
  textTransform: 'none',
  fontWeight: 400,
  '@media (max-width:480px)': { display: 'block', WebkitLineClamp: 'unset', overflow: 'visible' },
} as const

/* An option's `preview`, a box-drawing MOCKUP: never re-wrap (`pre`, not `pre-wrap`); width is paid by ITS
   OWN horizontal scroll, never the panel (R1; `min-width: 0` lets it shrink).
   BOUNDED and scrolling (`maxHeight`); `[data-testid="gate-option-preview"]` is in
   `e2e/task-detail-fit.spec.ts`'s sanctioned-scrollport list (runner-set height).
   `overscrollBehavior: 'auto'` explicitly: `contain` has been undone once (`SCROLLPORT_SX`,
   `app-shell-scroll.spec.ts` fails any contain/none box).
   Mono, wrap off and the cap come from `ui/CodeBlock`. */

/* The `(Recommended)` badge on the label line; the flag arrives resolved (`QuestionOption.recommended`).
   Not `StatusChip` (the console's STATUS pill, beside `open`/`answered`): a soft filled tag, visibly a
   different kind of mark. */
const RECOMMENDED_SX = {
  flexShrink: 0,
  px: 0.67,
  py: 0.08,
  borderRadius: 1,
  fontSize: '0.6rem',
  fontWeight: 700,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
  lineHeight: 1.6,
  color: 'success.dark',
  backgroundColor: 'success.light',
  whiteSpace: 'nowrap',
} as const

/* The round's short name beside its ordinal (`TaskQuestion.header`) — one definition for the open round,
   the history and the Q&A trail, deliberately neutral so it never becomes a second accent.
   It tells a batch's rounds apart ("Missing input", "Verdicts"); beside the ordinal, never instead of it
   (the ordinal is what an answer routes on). */
const ROUND_HEADER_SX = {
  flexShrink: 0,
  px: 0.67,
  py: 0.08,
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  fontSize: '0.65rem',
  fontWeight: 600,
  lineHeight: 1.6,
  color: 'text.secondary',
  maxWidth: '10rem',
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const

// Label and badge on one line, wrapping (R1). `alignItems: 'baseline'` keeps the badge by the FIRST line.
export const LABEL_LINE_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'baseline',
  gap: 0.5,
  minWidth: 0,
  // Full width, so the label's 3-line clamp counts against this box.
  width: '100%',
} as const

/* One option's detail by its SOURCE index: `option_details` is index-aligned with `options` by
   construction. Callers must pass the raw index, not a rendered-row position (the multi-choice branch
   filters blank-glyph options with `hasGlyph`, which would pair a wrong description — cf. the `ticked`
   note). Missing is normal (old rounds, short arrays, bare labels): `undefined`, rendering nothing. */
export function optionDetail(details: QuestionOption[] | null | undefined, source: number): QuestionOption | undefined {
  return details?.[source]
}

/* Blank is absent: whitespace-only text would be an empty node with real padding (read as a rendering
   fault). Exported so every surface asks the same question — e.g. `QATrail`'s header row, since
   `RoundHeaderChip` renders nothing for a blank header and its wrapper would still pay the gap. */
export function nonBlank(text: string | null | undefined): string | null {
  return text && text.trim() ? text : null
}

/* An option's description to show, or null: blank, or its label again (trimmed, case-insensitive) — the label
   already says it, and a second copy under it reads as a rendering fault. */
export function optionDescription(label: string, detail: QuestionOption | undefined): string | null {
  const description = nonBlank(detail?.description)
  return description && description.trim().toLowerCase() !== label.trim().toLowerCase() ? description : null
}

/* ── How much of one agent-authored string this surface renders ────────────────────────────────
   The console had no bound: Slack caps option detail within `slack_blocks._OPTION_DETAIL_TOTAL_MAX`, the
   runner prompt at `routes/tasks._QA_TRAIL_OPTION_TEXT_MAX_CHARS` (600), but here the column is unbounded
   `Text` (`models.py`), projected verbatim (`routes/tasks._option_detail_text`), and the CSS clamp is
   removed at <=480px. A ~250 KB description (one `cat`-ed log) would bury the answer controls, with a
   second copy in `title`.
   4000 characters: ~50 terminal lines, far above any real `AskUserQuestion` description; ten options at
   the ceiling is 40 KB. The marker is not charged against the ceiling (unlike Slack's wire limit), and
   the cut is VISIBLE, so it never reads as the runner's half sentence. */
export const OPTION_DETAIL_MAX_CHARS = 4000
export const OPTION_DETAIL_TRUNCATED_MARK = '… (truncated)'

/* One detail string as rendered, applied INSIDE `OptionDescription`/`OptionPreview` so the render boundary
   itself is bounded — including `title`, the second copy. */
export function clampDetail(text: string): string {
  if (text.length <= OPTION_DETAIL_MAX_CHARS) return text
  /* Never mid-astral: `slice` counts UTF-16 units, and a lone high surrogate renders as the replacement
     glyph. Back off one unit to drop the whole character. */
  const lastUnit = text.charCodeAt(OPTION_DETAIL_MAX_CHARS - 1)
  const end = lastUnit >= 0xd800 && lastUnit <= 0xdbff ? OPTION_DETAIL_MAX_CHARS - 1 : OPTION_DETAIL_MAX_CHARS
  return text.slice(0, end) + OPTION_DETAIL_TRUNCATED_MARK
}

/* Whether to paint the Recommended badge: the label may already end in `(Recommended)` (the server sets the
   flag without stripping the suffix), and the badge must not repeat it. Decides the badge only.
   A mirror of `routes/tasks._label_marks_recommended` (the authority, which also resolves the flag via
   `_question_option_details`), also spelled in `slack_blocks._recommended_label` and the question
   capture tool's `recommended_index`: exact-case suffix, trailing whitespace tolerated,
   nothing else — `trimEnd().endsWith(…)` mirroring `label.rstrip().endswith(_RECOMMENDED_LABEL_SUFFIX)`.
   (Known seam: Python's `rstrip` also strips `\x1c`–`\x1f`; JS's `trimEnd` does not.)
   It MUST match: a looser test once suppressed the console badge while Slack printed `Blue (recommended)
   (Recommended)`. A change to `_label_marks_recommended` must be walked over here by hand. */
const RECOMMENDED_LABEL_SUFFIX = '(Recommended)'
export function showsRecommended(label: string, detail: QuestionOption | undefined): boolean {
  return Boolean(detail?.recommended) && !label.trimEnd().endsWith(RECOMMENDED_LABEL_SUFFIX)
}

/* The three detail parts as components, so placements share the render, not just the rule.
   `component="span"` on the first two: they sit in a `<button>` and a `<label>`, which may not contain a
   `<p>`. Exported as the single-home mechanism (`QATrail` imports them rather than re-spelling the clamp,
   `title` fallback, `role="img"` name and length bound). */
export function OptionDescription({ text, id }: { text: string; id?: string }) {
  // Bounded HERE, at the render boundary, for every placement at once — see `OPTION_DETAIL_MAX_CHARS`.
  const shown = clampDetail(text)
  return (
    <Typography
      component="span"
      variant="caption"
      id={id}
      data-testid="gate-option-description"
      /* The unclamped fallback where there is hover (the <=480px arm covers the rest). Un-CSS-clamped only:
         it carries the same length bound, or a raw 250 KB would reopen the hole. */
      title={shown}
      sx={OPTION_DESCRIPTION_SX}
    >
      {shown}
    </Typography>
  )
}

export function OptionPreview({ text }: { text: string }) {
  // Bounded like the description, by the same constant (same unbounded path).
  const shown = clampDetail(text)
  return (
    <CodeBlock
      testId="gate-option-preview"
      wrap={false}
      bordered
      maxHeight="12rem"
      /* A short label, not the content: box-drawing art read aloud is noise. `role="img"` + a name
         announces one object; the description says what the option means. */
      role="img"
      aria-label="Monospace preview of this option's layout"
      /* Not part of the hit target: `FormControlLabel` forwards label clicks to the checkbox, so scrolling
         the mockup would TICK the option. The row's label text stays the target. */
      onClick={e => e.stopPropagation()}
      sx={{ mt: 0.5, py: 0.75, fontSize: '0.7rem', lineHeight: 1.5, color: 'text.secondary', overscrollBehavior: 'auto' }}
    >
      {shown}
    </CodeBlock>
  )
}

export function RecommendedBadge({ id }: { id?: string }) {
  return (
    <Box component="span" id={id} data-testid="gate-option-recommended" sx={RECOMMENDED_SX}>
      Recommended
    </Box>
  )
}

/* The round's short name beside its ordinal (open header row and `QATrail` entries). Rendered only when
   there is one; a header-less round is unchanged. */
export function RoundHeaderChip({ header }: { header: string | null }) {
  if (!nonBlank(header)) return null
  return (
    <Box
      component="span"
      data-testid="gate-round-header"
      /* The full name where the 10rem ellipsis cut it — a real fallback: ~12 chars is only the tool's
         convention (`models/tasks.py::TaskQuestion.header`: "nothing here enforces it"); the one ceiling is 200
         (`routes/agent/task_terminal.py::TaskQuestionIn.header`). Not `OPTION_DETAIL_MAX_CHARS`-clamped: it already has a
         far tighter ceiling. */
      title={header ?? undefined}
      sx={ROUND_HEADER_SX}
    >
      {header}
    </Box>
  )
}

/* ── ROUNDS FROM ONE `AskUserQuestion` CALL, UNDER ONE HEADER ──────────────────────────────────────
   One call with several questions lands as one round per question, posted back to back
   (`apps/jaahd/acp/host.py::_post_questions`, the capture hook's one `input.request` per question): the wire
   carries no batch id, so neighbours with the same header raised within `BATCH_WINDOW_MS` of each other read
   as one call. Render-only: grouping changes no ordinal, key or route — each round is still its own block,
   answered by its own id; only the repeated header chip collapses into the group's one label. */
export const BATCH_WINDOW_MS = 10_000

type BatchRound = { header: string | null; created_at: string }

/** `items` in their order, split into runs of neighbours from one call sharing one non-blank header. An item
 *  whose `roundOf` is undefined (no live row) is always a run of its own. */
export function batchGroups<T>(items: T[], roundOf: (item: T) => BatchRound | undefined): T[][] {
  const groups: T[][] = []
  let prev: BatchRound | undefined
  for (const item of items) {
    const round = roundOf(item)
    const joins = prev !== undefined && round !== undefined && nonBlank(round.header) !== null
      && round.header === prev.header
      && Math.abs(Date.parse(round.created_at) - Date.parse(prev.created_at)) <= BATCH_WINDOW_MS
    if (joins) groups[groups.length - 1].push(item)
    else groups.push([item])
    prev = round
  }
  return groups
}

/* A group of rounds under its one header: the label, then the rounds as their own list (a `li` in the
   host's list, so the outer list's count is its groups). `ordered` for the Answer tab's `ol`; `gap` the host
   list's own, so grouped rounds sit as far apart as ungrouped ones. */
export function RoundBatch({ header, ordered = false, gap, testId, children }: {
  header: string | null
  ordered?: boolean
  gap: number
  testId: string
  children: ReactNode
}) {
  return (
    <Box component="li" data-testid={testId} sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0 }}>
      <Box sx={{ display: 'flex' }}>
        <RoundHeaderChip header={header} />
      </Box>
      <Box
        component={ordered ? 'ol' : 'ul'}
        role="list"
        sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap }}
      >
        {children}
      </Box>
    </Box>
  )
}

/* ── WHICH OPTIONS A HUMAN COULD SEE AND AIM AT ─────────────────────────────────────────────────────
   A MIRROR of `_has_glyph` in `apps/api/app/notify_text.py` (authoritative; the Slack card drops such
   options and `routes/tasks._gate_answer_or_422` imports it to validate `selections`). A change there must
   be walked over here by hand, or the console offers a row the route refuses. Without it a blank
   runner-authored option is tickable and the route 422s the WHOLE body.
   `\p{Z}`/`\p{C}`/`\p{M}` is the category half. `u` flag and `for…of` code-POINT iteration, so an astral
   character is not two lone surrogates (which `\p{C}` would match, hiding every emoji option). */
// Non-invisible-category characters that still render as nothing (braille blank, Hangul/Khmer fillers),
// verbatim from the server's `_BLANK_GLYPHS`. Spelled as escapes so they are visible in a diff.
// The characters that are NOT in an invisible category and still render as nothing — the braille blank
// (`So`) and the Hangul/Khmer fillers (`Lo`). Enumerated verbatim from the server's `_BLANK_GLYPHS`
// because no category test can catch them, and a category test alone is exactly the hole the first
// version of `_has_glyph` shipped with. Spelled as ESCAPES for the same reason the list exists at all:
// pasted literally, these are eleven invisible characters in a source file — unreadable in a diff and
// undetectable in a review.
const BLANK_GLYPHS = '\u2800\u3164\u115f\u1160\u17b4\u17b5\uffa0\u180e\u200b\u2060\ufeff'
const NO_STANDALONE_GLYPH = /[\p{Z}\p{C}\p{M}]/u
export function hasGlyph(text: string): boolean {
  for (const ch of text) {
    if (!NO_STANDALONE_GLYPH.test(ch) && !BLANK_GLYPHS.includes(ch)) return true
  }
  return false
}

/* Max options one answer may carry — a MIRROR of `_MAX_SELECTIONS` in `apps/api/app/routes/tasks/answers.py`
   (Pydantic `max_length`); a drift there must move this. Mirrored because the refusal is unreadable (a
   list `detail` that `apiDetail` drops to "Failed to post answer") and rounds have no option cap
   (`agent.TaskQuestionIn.options`). */
export const MAX_SELECTIONS = 25
