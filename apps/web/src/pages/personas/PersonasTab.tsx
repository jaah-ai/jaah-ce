import Box from '@mui/material/Box'
import Tooltip from '@mui/material/Tooltip'
import { useApiRead } from '../../hooks/useApiRead'
import Banner from '../../ui/Banner'
import LoadFailedState from '../../ui/LoadFailedState'
import FeedList from '../../ui/feed/FeedList'
import { GATE_SX, monoFamily } from '../../ui/feed/geometry'
import type { FeedColumn, FeedIndicator, FeedRowSpec } from '../../ui/feed/types'
import LoadingState from '../../ui/LoadingState'
import { fmtTier } from '../../lib/models'
import { ELLIPSIS_SX, PERSONAS_LIST_CONTAINER_SX } from '../../ui/listStyles'
import { listPersonas, type PersonaSummary } from './api'
import { DELIVERY_LABEL, labelOf } from './vocabulary'

/* Automation, the PERSONAS tab body: every persona a runner can be dispatched as.
 * Headerless: the shell owns the page's `<h1>`, title and subtitle.
 * A gateway: each row links to `PersonaDetailPage` and carries only what picks the link — purpose,
 * model, delivery, reachability.
 * Never legitimately empty (declaration-table facts, `apps/api/app/persona_registry.py`), so no rows
 * means the read failed: one failure state, no empty state.
 * `tasks:create` at the route (`App.tsx`'s `RequireFeature` around `/audit/personas`); no second gate. */

/** ≈ the roster's settled height (a closed table). */
const SKELETON_ROWS = 12

/** Wording for a persona whose charter key has no role row (the server answers null, not a 500, so the
 *  operator sees which half is registered). */
const NO_BRIEF = 'No role brief registered.'
const NO_DELIVERY = 'not registered'

/* The focus is one ellipsised line; the whole of it on hover AND keyboard focus, so the span is a tab stop.
 * Up to ~2500 chars: the measure is capped and a long one scrolls (MUI tooltips are hoverable). A paragraph
 * that long reads on the console's paper surface, not the dark 11px hint pill. */
const FOCUS_TIP_SX = {
  maxWidth: '60ch',
  maxHeight: '50vh',
  overflowY: 'auto',
  bgcolor: 'background.paper',
  color: 'text.primary',
  border: 1,
  borderColor: 'divider',
  boxShadow: 3,
  typography: 'body2',
  p: 1.5,
} as const
const FOCUS_TEXT_SX = { ...ELLIPSIS_SX, display: 'block' } as const

/** The focus takes the roster's width less ~300px of title and the rest of the cluster, from its 200px floor (the
 *  width `PERSONAS_NO_FOCUS` is priced at): so the title is capped and the focus sits beside the label, with no
 *  dead band between them or after Delivery. A cap through the slot, not `titleMaxWidth`: a capped title lets a
 *  row's ⚙ marker push its slots off the header's. `cqw` is the roster's own width (`PERSONAS_LIST_CONTAINER_SX`). */
const FOCUS_WIDTH = 'max(200px, calc(100cqw - 600px))'

/** Delivery's slot: its longest label, "Control-plane record", whole. */
const DELIVERY_WIDTH = 144

/** Each column named once over the rows; Model and Delivery are short tokens that otherwise read as noise. */
const COLUMNS: { title: FeedColumn; slots: FeedColumn[] } = {
  title: { key: 'title', label: 'Persona' },
  slots: [
    { key: 'focus', label: 'Focus' },
    { key: 'model', label: 'Model' },
    { key: 'delivery', label: 'Delivery' },
  ],
}

/** `business_analysis` is "Analyze", but `coder` is just "Coder": the raw name only says something new where it is
 *  not the label respelled (case, `_`, `-`, spaces aside). */
export function nameDiffersFromLabel(name: string, label: string): boolean {
  const letters = (text: string) => text.toLowerCase().replace(/[^a-z0-9]/g, '')
  return letters(name) !== letters(label)
}

const NAME_SX = { ml: 0.5, color: 'text.secondary', fontFamily: monoFamily, fontSize: '0.6875rem', ...GATE_SX.personasLead } as const

function FocusText({ focus }: { focus: string }) {
  return (
    <Tooltip title={focus} describeChild slotProps={{ tooltip: { sx: FOCUS_TIP_SX } }}>
      <Box component="span" tabIndex={0} sx={FOCUS_TEXT_SX}>{focus}</Box>
    </Tooltip>
  )
}

/** The reach mark: `dispatchable` (can it reach a runner at all; absent from the charter map it is an
 *  error verdict) and the narrower `operator_selectable` (may a HUMAN pick it) → three marks, so "why
 *  can I not choose this?" gets an answer. `label` is announced (the row has no `ariaLabel`), `title` is
 *  the hover sentence. `system_persona` is a separate MARKER, not a permission. */
function reachMark(persona: PersonaSummary): FeedIndicator {
  if (!persona.dispatchable) {
    return {
      tone: 'warning',
      glyph: '✕',
      label: 'not dispatchable',
      title: 'Not dispatchable.',
      data: { reach: 'not dispatchable' },
    }
  }
  if (persona.operator_selectable) {
    return {
      tone: 'success',
      glyph: '●',
      label: 'operator',
      title: 'Operator-selectable.',
      data: { reach: 'operator' },
    }
  }
  return {
    tone: 'info',
    glyph: '»',
    label: 'pipeline only',
    title: 'Pipeline only.',
    data: { reach: 'pipeline only' },
  }
}

export default function PersonasTab({ enabled = true }: {
  /* Whether this read is wanted yet: `TabPanel` keeps every panel mounted, so the caller (`WorkflowsPage`)
   * latches it false→true. Defaults to true so a non-tab caller cannot forget to ask. */
  enabled?: boolean
} = {}) {
  const { data: personas, loading, error, reload: load } = useApiRead(
    ['personas'],
    () => listPersonas().then(payload => payload.personas),
    'Failed to load the persona roster',
    { enabled },
  )

  /* An unopened panel renders NOTHING: it stays in the DOM, and a never-resolving skeleton fails
   * `e2e/app-shell-scroll.spec.ts`'s no-surviving-`loading-state` sweep. */
  if (!enabled) return null

  if (loading) return <LoadingState rows={SKELETON_ROWS} />

  /* No rows can only mean the read failed (see header). */
  if (personas === null || personas.length === 0) {
    return <LoadFailedState title="Persona roster unavailable" error={error} onRetry={() => void load()} testId="personas-retry" />
  }

  /* The raw `snake_case` name is the dispatch discriminator an operator sees in Task rows and logs; it
   * follows the label, and only where it is not the label respelled (`nameDiffersFromLabel`), so no column of
   * repeats. The focus's width caps the title (`FOCUS_WIDTH`), so the focus sits beside it. Focus is one ellipsised line
   * (`focus` can be 1500–2500 chars); the full string is its tooltip (`FocusText`). Beside an open persona the
   * roster's own width drops the focus, then the name with the model (`ui/listStyles.ts`'s `PERSONAS_NO_*`), so the
   * label and delivery stay whole. */
  const personaRows: FeedRowSpec[] = personas.map((persona): FeedRowSpec => ({
    key: persona.name,
    indicator: reachMark(persona),
    title: nameDiffersFromLabel(persona.name, persona.label)
      ? <>{persona.label}{' '}<Box component="span" data-testid="persona-name" sx={NAME_SX}>{persona.name}</Box></>
      : persona.label,
    titleText: persona.label,
    href: `/workflows/personas/${persona.name}`,
    markers: persona.system_persona
      ? [{
          key: 'system',
          glyph: '⚙',
          // A marker's label is both hover text and announced name, so it says what the mark means.
          label: 'system persona',
        }]
      : undefined,
    slots: [
      {
        key: 'focus',
        width: FOCUS_WIDTH,
        gate: 'personasFocus',
        align: 'left',
        testId: 'persona-focus',
        node: persona.focus ? <FocusText focus={persona.focus} /> : NO_BRIEF,
      },
      {
        // The tier's label (`fmtTier`), monospaced like the raw persona name; it drops with that name.
        key: 'model',
        width: 72,
        gate: 'personasLead',
        align: 'right',
        mono: true,
        testId: 'persona-model',
        title: `Model tier: ${fmtTier(persona.default_model)}`,
        node: fmtTier(persona.default_model),
      },
      {
        /* The persona page's label (`vocabulary.ts`); the raw `db`/`pr`/`outcome` token rides the hover. */
        key: 'delivery',
        width: DELIVERY_WIDTH,
        align: 'right',
        dim: persona.delivery === null,
        testId: 'persona-delivery',
        title: persona.delivery === null ? 'Delivery not registered' : `Delivers: ${persona.delivery}`,
        node: persona.delivery === null ? NO_DELIVERY : labelOf(DELIVERY_LABEL, persona.delivery),
      },
    ],
  }))

  return (
    <Box sx={PERSONAS_LIST_CONTAINER_SX}>
      {error && <Banner severity="error">{error}</Banner>}
      <FeedList
        ariaLabel="Personas"
        testId="personas-list"
        sections={[{ key: 'personas', rows: personaRows }]}
        columns={COLUMNS}
      />
    </Box>
  )
}
