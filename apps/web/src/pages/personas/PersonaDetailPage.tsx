import { type ReactNode } from 'react'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { ApiError } from '../../api'
import { fmtTier } from '../../lib/models'
import Breadcrumb from '../../components/Breadcrumb'
import RichTextRenderer from '../../components/richtext/RichTextRenderer'
import { stateLabel } from '../../components/tasks/shared'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useUrlTab } from '../../hooks/useUrlTab'
import { AUDIT_TAB_PATHS } from '../../routeGroups'
import Banner from '../../ui/Banner'
import { Mono } from '../../ui/DetailFields'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadFailedState from '../../ui/LoadFailedState'
import MissingRecordState from '../../ui/MissingRecordState'
import { monoSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import PageHeader from '../../ui/PageHeader'
import SectionCard from '../../ui/SectionCard'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import {
  getPersona,
  type PersonaDetail,
  type PersonaManifestItem,
  type PersonaRoleSection,
} from './api'
import {
  DELIVERY_LABEL, DONE_CRITERION_LABEL, LICENCE_SHAPE_LABEL, LICENCE_SHAPE_NOTE, MANIFEST_LEVEL_LABEL,
  MANIFEST_LEVEL_NOTE, MODE_LABEL, REFERENCE_LABEL, TERMINAL_LABEL,
} from './vocabulary'

/* Audit > Personas > ONE PERSONA — `/workflows/personas/:persona`. `tasks:create` at the route
 * (`App.tsx`'s `RequireFeature` around it); no page-local duplicate gate.
 * Purpose (operator): "all information about this persona … rendered from current code data structures — the
 * purpose is to not have to look in the code." So: EVERY field (a curated subset sends readers back to the
 * source for the one it dropped), grouped by the QUESTION it answers, long prose rendered as prose IN THE BODY
 * (never a JSON dump, never in the header — see the block above `lede`).
 * VOCABULARY HELP IS A LOOKUP WITH A FALLBACK TO THE RAW TOKEN, never a fall-through `switch`: the closed sets
 * are Python-owned (`pages/personas/api.ts`), so a newer persona renders AS ITSELF.
 * A 404 IS A STATE (as `WorkflowDetailPage`/`NodeDetailPage`): a named not-found with the way back.
 */

/** The roster this page is reached from: Audit's Personas tab (`routeGroups.ts`). */
const PERSONAS_TAB = AUDIT_TAB_PATHS.personas

/** First crumb the PAGE, second the TAB; both links, since both are places. */
const AUDIT_PAGE = AUDIT_TAB_PATHS.users

/* THE TWO TABS: the scannable knobs first, the role brief (thousands of characters, READ once) second, so a
 * phone opens on the facts. Every panel stays mounted (`TabPanel`); `?tab=brief` addresses the second. */
type PersonaTab = 'overview' | 'brief'
const PERSONA_TABS: readonly TabDef<PersonaTab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'brief', label: 'Role brief' },
]

/* ── The focus paragraph: a one-line lede in the header, every character of it in the body ────────────
 * Do not "put the focus back" as the subtitle: `focus` is `PERSONA_CHARTER` prose (~2500 chars on `plan`), so as
 * a subtitle it pushed every knob below the fold and its markdown-SHAPED markers (`**x**`, backticks) read as a
 * broken renderer. So the SUBTITLE carries the lede (first sentence, clamped, its inline markers dropped:
 * `plainLede`) and the BODY carries the whole string, formatted (`Prose`). */

/** At or below this a focus IS its own lede — no body block (`coder`'s case). */
const LEDE_WHOLE_MAX = 140

/** An early full stop is usually an abbreviation ("i.e. ", "R3. "); the search continues past it. The CSS clamp
 *  is the backstop. */
const LEDE_FLOOR = 40

/** The whole focus when short, otherwise its first SENTENCE (a character cut reads as data loss). The full text
 *  renders in `Identity`. */
function lede(focus: string): string {
  if (focus.length <= LEDE_WHOLE_MAX) return focus
  const found = focus.slice(LEDE_FLOOR).search(/[.!?](?:\s|$)/)
  return found < 0 ? focus : focus.slice(0, LEDE_FLOOR + found + 1)
}

/** The lede as header text: markdown's inline markers (`**`, `__`, backticks) dropped, since a subtitle is plain. */
const plainLede = (focus: string): string => lede(focus).replace(/\*\*|__|`/g, '')

/* CLAMPED ON TOP OF THE SENTENCE CUT, so the header stays short even for a 400-char first sentence. The lede
 * WRAPS (a one-line clamp cut ordinary first sentences mid-word) and its `title` carries the whole of it. */
const SUBTITLE_SX = {
  display: '-webkit-box',
  WebkitBoxOrient: 'vertical',
  WebkitLineClamp: { xs: 4, sm: 2 },
  overflow: 'hidden',
  overflowWrap: 'anywhere',
} as const

/** PROMPT PROSE, FORMATTED: the focus paragraph and every role-brief body are markdown, rendered read-only through
 *  `RichTextRenderer` (the TipTap schema: lists, bold and inline code render; nothing injects markup). Capped in `ch`
 *  so a paragraph keeps a readable measure; `overflowWrap: 'anywhere'` is a mobile REQUIREMENT (a long path would
 *  scroll a 390px page sideways). */
function Prose({ text, testId }: { text: string; testId: string }) {
  return (
    <Box
      data-testid={testId}
      sx={{
        maxWidth: '96ch',
        overflowWrap: 'anywhere',
        typography: 'body2',
        /* Flush with its label: the renderer's editor-pane padding (`styles.css` `.rte-content`) and the first and
           last paragraphs' margins left a ~40px gap under "What it is for" and indented the prose off the label. */
        '& .rte-content': { p: 0 },
        '& .rte-prosemirror > :first-child': { mt: 0 },
        '& .rte-prosemirror > :last-child': { mb: 0 },
      }}
    >
      <RichTextRenderer content={text} linkify={false} />
    </Box>
  )
}

export default function PersonaDetailPage() {
  const { persona: name = '' } = useParams<{ persona: string }>()
  const [tab, selectTab] = useUrlTab(PERSONA_TABS, 'overview')

  /* NOT-FOUND IS A RESOLVED READ via `ApiError`'s STATUS, never a sniffed "404" in the text: a 404 returns a
     null detail, and only a genuinely failed read reaches `error`. */
  const { data, loading, error, reload: load } = useApiRead(
    ['persona-detail', name],
    async () => {
      try {
        return { detail: await getPersona(name) }
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return { detail: null }
        throw err
      }
    },
    'Failed to load this persona',
  )
  const detail = data?.detail ?? null
  const missing = data !== null && data.detail === null

  if (loading) return <LoadingState rows={6} />

  if (missing) {
    return (
      <MissingRecordState
        breadcrumb={<Trail current={name} />}
        heading="Unknown persona"
        title={`No persona is declared as “${name}”`}
        headerTestId="persona-detail-header"
        backTo={PERSONAS_TAB}
        backLabel="Back to personas"
      />
    )
  }

  if (detail === null) {
    return <LoadFailedState title="Persona unavailable" error={error} onRetry={() => void load()} testId="persona-detail-retry" />
  }

  return (
    <Box>
      {/* THE SUBTITLE IS THE FOCUS'S LEDE, NOT THE FOCUS (see the block above `lede`). A NULL IS WORDED here
          and only here (the server returns null, not a 500, so a half-registered persona shows which half
          is missing); `Identity`'s focus block is skipped. */}
      <PageHeader
        breadcrumb={<Trail current={detail.label} />}
        title={detail.label}
        subtitle={
          <Box
            component="span"
            sx={SUBTITLE_SX}
            data-testid="persona-focus-lede"
            title={detail.focus === null ? undefined : plainLede(detail.focus)}
          >
            {detail.focus === null
              ? 'No role brief registered.'
              : plainLede(detail.focus)}
          </Box>
        }
        testId="persona-detail-header"
      />

      {error && <Banner severity="error">{error}</Banner>}

      <Tabs tabs={PERSONA_TABS} tab={tab} onSelect={selectTab} label="Persona sections" idPrefix="persona" />

      <TabPanel id="overview" current={tab} idPrefix="persona">
        <Stack spacing={1.5} sx={{ pt: 1.5 }}>
          <SectionCard title="Identity">
            <Identity detail={detail} />
          </SectionCard>

          <SectionCard title="Runtime">
            <Runtime detail={detail} />
          </SectionCard>

          <SectionCard title="Delivery">
            <Delivery detail={detail} />
          </SectionCard>

          <SectionCard title="Hand-over">
            <Manifest items={detail.manifest} />
          </SectionCard>

          <SectionCard title="May create">
            <Licence licence={detail.task_creation_licence} />
          </SectionCard>
        </Stack>
      </TabPanel>

      {/* THE ROLE BRIEF ON ITS OWN TAB: everything on Overview is SCANNED, this is READ once; thousands of
          characters beside the knobs would bury them on a phone. */}
      <TabPanel id="brief" current={tab} idPrefix="persona">
        <Box sx={{ pt: 1.5 }}>
          <SectionCard title="Role brief">
            <RoleBrief sections={detail.role_sections} />
          </SectionCard>
        </Box>
      </TabPanel>
    </Box>
  )
}

/** Two link crumbs, then the current persona (plain text per `components/Breadcrumb`'s contract). */
function Trail({ current }: { current: string }) {
  return (
    <Breadcrumb
      items={[
        { label: 'Audit', to: AUDIT_PAGE },
        { label: 'Personas', to: PERSONAS_TAB },
        { label: current },
      ]}
    />
  )
}

/* ── The five fact sections ──────────────────────────────────────────────────────────────────────────
 * Through `ui/KeyValueList` (a `dl`, label over value on phones), not `ui/MetaRow`'s strip: many values are
 * SENTENCES and the page's primary content (the same call `WorkflowDetailPage`'s `Facts` makes). */

function Identity({ detail }: { detail: PersonaDetail }) {
  return (
    <KeyValueList testId="persona-identity">
      {/* THE RAW NAME, monospaced: the dispatch discriminator a Task's `persona` column holds. */}
      <KeyValue label="Persona">
        <Mono testId="persona-name">{detail.name}</Mono>
      </KeyValue>

      {/* THE CHARTER KEY is a separate fact: it can differ from the persona name (`coder` runs under
          `implementer`). */}
      <KeyValue label="Charter key">
        <Mono testId="persona-charter-key">{detail.charter_key}</Mono>
      </KeyValue>

      {/* THE THREE REACH FLAGS AS ONE FACT: `dispatchable` (can a runner be this persona at all),
          `operator_selectable` (may a HUMAN pick it — narrower), `system_persona` (a MARKER, never a
          permission). Pills in both directions, on one wrapping line. */}
      <KeyValue label="Who can dispatch it">
        <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Flag on={detail.dispatchable} label="dispatchable" onTone="success" />
          <Flag on={detail.operator_selectable} label="operator-selectable" onTone="success" />
          <Flag on={detail.system_persona} label="system persona" onTone="info" />
        </Stack>
      </KeyValue>

      {/* THE FULL FOCUS, LAST IN THIS CARD AND SPANNING BOTH COLUMNS (see the block above `lede`). Here so the
          page still publishes the whole field, in the first card; LAST within it so the page opens on
          scannable facts, not a slab. Omitted when `lede`
          already returned the whole string, or when null (the header says which half is unregistered). */}
      {detail.focus !== null && lede(detail.focus) !== detail.focus && (
        <KeyValue label="What it is for">
          <Prose text={detail.focus} testId="persona-focus" />
        </KeyValue>
      )}
    </KeyValueList>
  )
}

function Runtime({ detail }: { detail: PersonaDetail }) {
  return (
    <KeyValueList testId="persona-runtime">
      {/* THE MODEL IS A DEFAULT, not a property: a per-request override, the task's pin and a card default all
          beat it (there is no per-persona pin). Here, not in Identity: what the runner runs on, and it
          pairs the half-width facts in both cards (an odd count orphans one beside an empty half). */}
      <KeyValue label="Default model">
        <Typography variant="body2" data-testid="persona-model">{fmtTier(detail.default_model)}</Typography>
      </KeyValue>

      <KeyValue label="Mode">
        {detail.mode === null ? (
          <Missing testId="persona-mode" />
        ) : (
          <Term id={detail.mode} labels={MODE_LABEL} testId="persona-mode" />
        )}
      </KeyValue>

      {/* THREE STATES: `autonomous_default` null means the row's own setting stands; "No" would claim a default
          the CP does not make. */}
      <KeyValue label="Autonomy default">
        <Typography variant="body2" data-testid="persona-autonomous">
          {detail.autonomous_default === null
            ? 'Not defaulted'
            : detail.autonomous_default ? 'Autonomous' : 'Attended'}
        </Typography>
      </KeyValue>

      {/* The two interview flags together: the second (WHERE questioning happens) only means anything given the
          first. */}
      <KeyValue label="Does it stop and ask">
        <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <Flag on={detail.interviews} label="interviews" onTone="info" />
          <Flag on={detail.console_interview} label="console interview" onTone="info" />
        </Stack>
      </KeyValue>
    </KeyValueList>
  )
}

function Delivery({ detail }: { detail: PersonaDetail }) {
  /* ITERATED OFF `terminal_types` (the roster's field, so both screens name the same endings) AND LOOKED UP IN
     `terminals` (only for REFERENCE obligations); an ending without a references row still renders. */
  const references = new Map(detail.terminals.map(t => [t.type, t.required_references]))

  return (
    <KeyValueList testId="persona-delivery">
      <KeyValue label="Result channel">
        {detail.delivery === null ? (
          <Missing testId="persona-delivery-channel" />
        ) : (
          <Term id={detail.delivery} labels={DELIVERY_LABEL} testId="persona-delivery-channel" />
        )}
      </KeyValue>

      {/* A NULL DONE CRITERION IS THE SHARPEST: nothing can move this persona's row to done — hence per-field
          `Missing` sentences. */}
      <KeyValue label="Done criterion">
        {detail.done_criterion === null ? (
          <Missing
            testId="persona-done-criterion"
            why="Cannot complete."
          />
        ) : (
          <Term id={detail.done_criterion} labels={DONE_CRITERION_LABEL} testId="persona-done-criterion" />
        )}
      </KeyValue>

      {/* The SOURCE states a settle may be made from (`Persona.settle_states`); every terminal lands `done`.
          Several is normal (DB-direct from `awaiting_response`; `finish` from parked/system-failed). */}
      <KeyValue label="States it can complete from">
        <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap' }} data-testid="persona-settle-states">
          {detail.settle_states.length === 0
            ? <Typography variant="body2" sx={{ color: 'text.secondary' }}>None</Typography>
            : detail.settle_states.map(state => <Term key={state} id={state} label={stateLabel(state)} />)}
        </Stack>
      </KeyValue>

      {/* How the session itself ends once settled: beside the settle states, the other half-width fact here. */}
      <KeyValue label="Session close">
        <Flag on={detail.autoclose} label="autoclose" onTone="neutral" />
        {!detail.autoclose && <Note>Parked at the prompt is normal here, not stuck.</Note>}
      </KeyValue>

      {/* Each ending is a token, a sentence and obligations. */}
      <KeyValue label="How it can end">
        {detail.terminal_types.length === 0 ? (
          <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="persona-terminals">
            None
          </Typography>
        ) : (
          <Stack spacing={1} sx={{ mt: 0.25 }} data-testid="persona-terminals">
            {detail.terminal_types.map(type => {
              const required = references.get(type) ?? []
              return (
                <Box key={type} data-testid="persona-terminal">
                  <Term id={type} labels={TERMINAL_LABEL} />
                  {/* The references wrap in their own column, so a wrapped one lines up under the first. */}
                  {required.length > 0 && (
                    <Box sx={{ display: 'flex', columnGap: 1, alignItems: 'baseline' }}>
                      <Typography variant="body2" sx={{ color: 'text.secondary', flexShrink: 0 }}>Refused without</Typography>
                      <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 1, rowGap: 0.25, alignItems: 'baseline', minWidth: 0 }}>
                        {required.map(ref => <Term key={ref} id={ref} labels={REFERENCE_LABEL} />)}
                      </Box>
                    </Box>
                  )}
                </Box>
              )
            })}
          </Stack>
        )}
      </KeyValue>
    </KeyValueList>
  )
}

/** WHAT THE PERSONA MUST UPLOAD BEFORE IT MAY SETTLE. An EMPTY manifest is worded ("owes nothing" — what a
 *  reader chasing a refused settle needs), not `ui/EmptyState` (whole-panel). A stack of blocks, not a table,
 *  for a list usually one item long. */
function Manifest({ items }: { items: PersonaManifestItem[] }) {
  if (items.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="persona-manifest">
        None
      </Typography>
    )
  }

  return (
    <Stack spacing={1} data-testid="persona-manifest">
      {items.map(item => (
        <Box key={item.kind} data-testid="persona-manifest-item">
          {/* The kind always identifies the row; a null label is dropped, never invented. */}
          <Term id={item.kind} label={item.label ?? undefined} />
          {/* THE LEVEL, NOT THE BOOLEAN (`MANIFEST_LEVEL_NOTE`), on its own line and named like every other token; an
              unknown level still shows, as itself. */}
          <Box>
            <Term id={item.level} labels={MANIFEST_LEVEL_LABEL} testId="persona-manifest-level" />
          </Box>
          {/* An EMPTY extension list means "not format-pinned", not "nothing accepted". */}
          <Note>
            {[
              Object.hasOwn(MANIFEST_LEVEL_NOTE, item.level) && MANIFEST_LEVEL_NOTE[item.level],
              item.extensions.length === 0 ? 'Any format.' : `Accepted formats: ${item.extensions.join(', ')}.`,
            ].filter(Boolean).join(' ')}
          </Note>
        </Box>
      ))}
    </Stack>
  )
}

/** WHAT THE PERSONA MAY FILE FOR SOMEBODY ELSE. NULL is the common case and a fact ("why did this stage not
 *  open a follow-up task?"). */
function Licence({ licence }: { licence: PersonaDetail['task_creation_licence'] }) {
  if (licence === null) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="persona-licence">
        None
      </Typography>
    )
  }

  return (
    <Box data-testid="persona-licence">
      <Stack spacing={0.75}>
        {licence.shapes.map(shape => (
          <Box key={shape} data-testid="persona-licence-shape" data-status={shape}>
            <Term id={shape} labels={LICENCE_SHAPE_LABEL} />
            {Object.hasOwn(LICENCE_SHAPE_NOTE, shape) && <Note>{LICENCE_SHAPE_NOTE[shape]}</Note>}
          </Box>
        ))}
      </Stack>
      {/* The constraint (the bound it must stay inside) as labelled body text, not a chip tooltip. */}
      <Box sx={{ mt: 0.75 }} data-testid="persona-licence-bound">
        <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block' }}>Bound</Typography>
        <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
          {licence.constraint.charAt(0).toUpperCase() + licence.constraint.slice(1)}
        </Typography>
      </Box>
    </Box>
  )
}

/** THE ROLE BRIEF — the markdown the runner is actually handed, formatted (`Prose`): its lists, bold and backticked
 *  identifiers read as such rather than as punctuation. */
function RoleBrief({ sections }: { sections: PersonaRoleSection[] }) {
  if (sections.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="persona-role-sections">
        None
      </Typography>
    )
  }

  return (
    <Stack spacing={1.5} data-testid="persona-role-sections">
      {sections.map(section => (
        <Box key={section.key} data-testid="persona-role-section">
          <Stack direction="row" spacing={0.75} sx={{ alignItems: 'baseline', flexWrap: 'wrap', rowGap: 0.25 }}>
            <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>
              {section.title}
            </Typography>
            {/* The raw key beside the title: the name actually in the Python literal. */}
            <Typography variant="caption" sx={[monoSx, { color: 'text.secondary' }]}>{section.key}</Typography>
          </Stack>
          <Box sx={{ mt: 0.5 }}>
            <Prose text={section.body} testId="persona-role-body" />
          </Box>
        </Box>
      ))}
    </Stack>
  )
}

/* ── The small shared pieces ─────────────────────────────────────────────────────────────────────── */

/** ONE CLOSED-SET TOKEN: its label in the body font, the raw id beside it in small mono. A token the map does not
 *  know (a newer server's) renders as itself, in mono, once. `label` overrides the lookup. */
function Term({ id, labels, label = labels && Object.hasOwn(labels, id) ? labels[id] : undefined, testId }: {
  id: string
  labels?: Readonly<Record<string, string>>
  label?: string
  testId?: string
}) {
  return (
    <Box component="span" data-testid={testId} sx={{ display: 'inline-flex', alignItems: 'baseline', gap: 0.75, flexWrap: 'wrap' }}>
      {label !== undefined && <Typography variant="body2" component="span">{label}</Typography>}
      <Typography
        variant={label !== undefined ? 'caption' : 'body2'}
        component="span"
        sx={[monoSx, label !== undefined && { color: 'text.secondary' }]}
      >
        {id}
      </Typography>
    </Box>
  )
}

/** What a token MEANS — never a tooltip (unreachable by touch). */
function Note({ children }: { children: ReactNode }) {
  return (
    <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
      {children}
    </Typography>
  )
}

/** A PROJECTED FACT THE CONTROL PLANE HAD NO ROW FOR, worded rather than blanked: the server `.get()`s each
 *  differently-populated table so a half-registered persona shows WHICH half is missing, and a blank would
 *  look like a render failure. `why` IS PER FIELD: each absence has a different consequence. */
function Missing({ why, testId }: { why?: string; testId?: string }) {
  return (
    <>
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid={testId}>
        Not registered
      </Typography>
      {why && <Note>{why}</Note>}
    </>
  )
}

/** A boolean knob with BOTH answers visible (a true-only flag makes "off" look unrendered). Off is a muted pill
 *  reading `no …`. */
function Flag({ on, label, onTone }: { on: boolean; label: string; onTone: Tone }) {
  return (
    <StatusChip
      label={on ? label : `no ${label}`}
      tone={on ? onTone : 'neutral'}
      data-status={`${label}:${on ? 'yes' : 'no'}`}
    />
  )
}
