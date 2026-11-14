import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import Checkbox from '@mui/material/Checkbox'
import FormControl from '@mui/material/FormControl'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormGroup from '@mui/material/FormGroup'
import FormHelperText from '@mui/material/FormHelperText'
import FormLabel from '@mui/material/FormLabel'
import Typography from '@mui/material/Typography'
import ExpandToggleGlyph from '../../components/ExpandToggleGlyph'
import { capitalize } from '../../lib/humanize'
import Banner from '../../ui/Banner'
import { DetailField } from '../../ui/detail/DetailPage'
import { FactsGrid } from '../../ui/FactsGrid'
import FormField from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import HelpLabel from '../../ui/HelpLabel'
import SelectField from '../../ui/SelectField'
import type { Explain, HelpProp } from '../../ui/explain'
import { FIELD_GRID_SX, monoSx, PHONE, STAGE_CARD_CONTAINER, STAGE_CARD_NARROW, tapTargetSx } from '../../ui/listStyles'
import {
  type Derived,
  type DependsSpec,
  type ExpertSpec,
  type Issue,
  type StageIn,
  type Vocabulary,
  labelOf,
} from './api'
import { FONT_WEIGHT, grid, LINE_HEIGHT, SIZE } from '../../theme'
import Tip from '../../ui/Tip'

/* ONE STAGE CARD — a playbook stage in the editor's vertical list (`PlaybookEditPage.tsx`). Header: number,
 * the FULL label (wraps, never truncated), expert × deliverable and tier, then the card's controls (collapse,
 * move up/down — keyboard-reachable buttons, disabled at the ends, as `settings/tags/TagGroupCard.tsx` —,
 * Preview prompt, Remove). Body: the editable fields, then the read-only facts the server DERIVES
 * (`derived`: mode, ends when, tier, receives / hands off, and each allowed expert's deliverable). Every
 * enum renders through the vocabulary's labels; no raw token reaches the screen. The card's issues (placed
 * by `stage_index`) show inline: an error on its field, and every message in the card's list so a collapsed
 * card still says what is wrong. */

const STAGE_SENIORITIES = ['inherit', 'junior', 'mid', 'senior'] as const

/* Each stage field's explanation (`ui/HelpLabel`), from the playbook engine (`apps/api/app/playbook_engine.py`,
   `playbook_vocabulary.py`) and the reader page below. */
const PLAYBOOK_DOCS = 'console-reference/automation/playbooks'
const KEY_HELP: Explain = {
  tip: 'How other stages refer to this one; unique per playbook.',
  more: 'Lowercase letters, digits and _. Created by and Waits for on other stages name this stage by its key.',
  docs: PLAYBOOK_DOCS,
}
const EXPERT_KIND_HELP: Explain = {
  tip: 'Fix one expert, or let the starter pick one.',
  more: 'The expert is the persona the stage runs as, which sets its prompt. With User selects, the deliverable follows the picked expert.',
  docs: PLAYBOOK_DOCS,
}
const DELIVERABLE_HELP: Explain = {
  tip: 'What the stage hands back; decides when it ends.',
  more: 'The deliverable sets the stage\'s mode (can change code or read-only), when it ends and what it may file. Changing it resets May file.',
  docs: PLAYBOOK_DOCS,
}
const RUNS_HELP: Explain = {
  tip: 'One task, or one task per item the plan lists.',
  more: 'Stages that run per item group into Build × N in the chain, between Prepare and Finish.',
  docs: PLAYBOOK_DOCS,
}
const WAITS_FOR_HELP: Explain = {
  tip: 'Stages that must finish before this one starts.',
  more: 'The stage also receives the PRD or Architecture document handed off by the stages it waits for.',
  docs: PLAYBOOK_DOCS,
}
/* One tip per field, shared by the editor and the read-only facts. */
const LABEL_HINT = 'Names the stage in the chain and on this card.'
const SENIORITY_HINT = "Picks the stage's model tier: Low, Standard or High."
const CREATED_HINT = 'Task exists at start, or a Plan stage files it.'
const INTERVIEWS_HINT = 'Whether the stage may stop to ask you questions.'
const MAY_FILE_HINT = 'Work this stage may create; the deliverable sets the choices.'
const INSTRUCTIONS_HINT = "Appended to this stage's prompt; see Preview prompt."
const FOLLOWS_EXPERT_HINT = 'Each allowed expert hands back its own deliverable.'
/* The derived facts' tips: read-only, set by the deliverable (`playbook_vocabulary.py`). */
const DERIVED_HINT = {
  ends_when: "When this stage's task counts as finished.",
  mode: 'Whether the stage may change code or only read.',
  tier: 'Resolved from Seniority and the playbook default.',
  receives: 'Documents handed to this stage by earlier stages.',
  hands_off: 'Documents this stage passes to the stages after it.',
} as const

/** What picking each option does: a line of its select's pop-up, keyed by the vocabulary key. */
const OPTION_HELP: Record<string, Record<string, string>> = {
  expert_kind: {
    pinned: 'Always runs as the expert chosen in Which expert.',
    chosen_at_start: 'Whoever starts the playbook picks from the allowed experts.',
  },
  deliverable: {
    pull_request: 'Can change code; ends when its pull request merges.',
    document: 'Read-only; ends when its document is uploaded.',
    mockup: 'Read-only; ends when its HTML report is uploaded.',
    plan: 'Read-only; files the per-item and finish stages.',
    review: 'Read-only; may file follow-ups into the implement stage.',
    conversation: 'Ends when the operator completes it.',
  },
  seniority: {
    inherit: "Uses the playbook's Default seniority.",
    junior: 'Low model tier.',
    mid: 'Standard model tier.',
    senior: 'High model tier.',
  },
  created: {
    at_start: "The stage's task is created when the playbook starts.",
    by: 'An earlier Plan stage files it, named in Created by.',
  },
  runs: {
    once: 'One task for this stage.',
    per_planned_item: 'One task for each item the Plan stage lists.',
  },
  depends_on: {
    none: 'Starts as soon as it is created.',
    all_earlier: 'Starts after every stage above it finishes.',
    stages: 'Starts after the stages ticked below finish.',
  },
  interviews: {
    follows_full_auto: "Asks only while the task's Full Auto is off.",
    never: 'Runs as Full Auto; never stops to ask.',
  },
}
const ICON_BUTTON_SX = [tapTargetSx, { minWidth: SIZE.tapTarget, px: 0.5, fontSize: grid(3), lineHeight: LINE_HEIGHT.tight }]

/* The card is a size container: its header goes compact when the CARD is narrow — a phone, and the editor's
   ~560px pane beside the Playbooks list at a desktop viewport — not only at the phone viewport. */

/** A header button's word on a wide card, a wordless glyph on a narrow one; the button carries the word on `aria-label`. */
function NarrowGlyph({ glyph, label }: { glyph: string; label: string }): JSX.Element {
  return (
    <>
      <Box component="span" aria-hidden="true" sx={{ display: 'none', fontSize: grid(3), lineHeight: LINE_HEIGHT.tight, [STAGE_CARD_NARROW]: { display: 'inline' } }}>{glyph}</Box>
      <Box component="span" sx={{ [STAGE_CARD_NARROW]: { display: 'none' } }}>{label}</Box>
    </>
  )
}

export type StageCardProps = {
  /** The editor's stable client id for this stage (survives reorders); stamped as `data-cid`. */
  cid?: number
  stage: StageIn
  index: number
  count: number
  /** Every stage of the playbook, for the created-by and depends-on pickers. */
  stages: readonly StageIn[]
  derived?: Derived
  issues: readonly Issue[]
  vocab?: Vocabulary
  readOnly: boolean
  collapsed: boolean
  onToggle: () => void
  onChange: (stage: StageIn) => void
  onMove: (delta: -1 | 1) => void
  onRemove: () => void
  onPreview: () => void
}

/* On `ui/FactsGrid`; `typography`/`overflowWrap` are inherited by each `DetailField`'s value. */
const FACTS_SX = { typography: 'body2', overflowWrap: 'anywhere', gap: 1 } as const

function expertKey(e: ExpertSpec): string {
  return e.kind === 'pinned' ? e.expert : e.default
}

function stageName(s: StageIn): string {
  return s.label.trim() || s.key || 'Untitled stage'
}

export function stageSummary(stage: StageIn, derived: Derived | undefined, vocab?: Vocabulary): string {
  const expert = labelOf(vocab, 'expert', expertKey(stage.expert))
  const deliverable =
    stage.expert.kind === 'chosen_at_start'
      ? 'follows the expert'
      : labelOf(vocab, 'deliverable', stage.deliverable) || 'no deliverable'
  const tier = derived?.tier ? `${labelOf(vocab, 'tier', derived.tier)} tier` : ''
  return [`${expert || 'No expert'} × ${deliverable}`, tier].filter(Boolean).join(' · ')
}

/** A vocabulary label without its field's own leading words, so the value shows whole under its label: under
 *  "Runs", "Runs once" reads "Once"; under "Asks questions", "Asks when Full Auto is off" reads "When Full Auto
 *  is off". The server's labels are written to stand alone, so the trim is the editor's (this card's and the playbook's selects), not the vocabulary's. */
export function withoutFieldWords(label: string, field: string): string {
  const words = field.toLowerCase().split(/\s+/)
  for (let k = words.length; k > 0; k--) {
    const lead = `${words.slice(0, k).join(' ')} `
    if (label.toLowerCase().startsWith(lead) && label.length > lead.length) {
      const rest = label.slice(lead.length)
      return capitalize(rest)
    }
  }
  return label
}

function listOf(vocab: Vocabulary | undefined, field: string, keys: readonly string[] | undefined): string {
  return keys && keys.length > 0 ? keys.map(k => labelOf(vocab, field, k)).join(', ') : 'Nothing'
}

/** A list of sentence-case labels read as one phrase: every item after the first starts lowercase. */
function phraseOf(vocab: Vocabulary | undefined, field: string, keys: readonly string[] | undefined): string {
  if (!keys || keys.length === 0) return 'Nothing'
  return keys.map((k, i) => {
    const label = labelOf(vocab, field, k)
    return i === 0 ? label : label.charAt(0).toLowerCase() + label.slice(1)
  }).join(', ')
}

export default function StageCard(props: StageCardProps): JSX.Element {
  const { stage, index, count, stages, derived, issues, vocab, readOnly, collapsed } = props
  const { onToggle, onChange, onMove, onRemove, onPreview } = props
  const n = index + 1
  const name = stageName(stage)
  const errors = issues.filter(i => i.severity === 'error')
  const fieldError = (field: string) => errors.filter(i => i.field === field).map(i => i.message).join(' ') || undefined
  const hasError = errors.length > 0
  const bodyId = `stage-${index}-body`

  const set = (patch: Partial<StageIn>) => onChange({ ...stage, ...patch })
  const others = stages.map((s, i) => ({ s, i })).filter(({ i }) => i !== index)
  const experts = vocab?.experts ?? []
  const labels = vocab?.labels ?? {}

  function setExpertKind(kind: string) {
    if (kind === stage.expert.kind) return
    const current = expertKey(stage.expert) || experts[0]?.key || ''
    if (kind === 'pinned') {
      const native = derived?.deliverable_by_expert?.[current] ?? stage.deliverable ?? vocab?.deliverables[0]?.key ?? null
      set({ expert: { kind: 'pinned', expert: current }, deliverable: native, may_file: null })
    } else {
      set({ expert: { kind: 'chosen_at_start', default: current, allowed: [current] }, deliverable: null, may_file: null })
    }
  }

  function setDepends(kind: string) {
    const next: DependsSpec =
      kind === 'stages' ? { kind: 'stages', keys: [] } : kind === 'all_earlier' ? { kind: 'all_earlier' } : { kind: 'none' }
    set({ depends_on: next })
  }

  const select = (
    id: string,
    label: string,
    value: string,
    options: readonly { key: string; label: string; help?: string }[],
    onPick: (v: string) => void,
    field: string,
    help?: HelpProp,
  ) => (
    <FormField
      label={label}
      help={help}
      choices={options.flatMap(o => (o.help ? [{ label: o.label, help: o.help }] : []))}
      error={fieldError(field)}
    >
      <SelectField
        id={id}
        value={options.some(o => o.key === value) ? value : ''}
        onChange={onPick}
        testId={id}
        options={options.map(o => ({ value: o.key, label: o.label }))}
      >
        {/* An unmatched value shows blank, as before, rather than the first option it is not. */}
        {!options.some(o => o.key === value) && <option value="" disabled />}
      </SelectField>
    </FormField>
  )

  /* `fieldLabel` is the select's own label: its words are trimmed off each option (`withoutFieldWords`). */
  const labelled = (field: string, keys: readonly string[], fieldLabel = '') =>
    keys.map(key => ({ key, label: withoutFieldWords(labelOf(vocab, field, key), fieldLabel), help: OPTION_HELP[field]?.[key] }))
  const valueOf = (field: string, key: string, fieldLabel: string) => withoutFieldWords(labelOf(vocab, field, key), fieldLabel)

  const mayFileOptions = derived?.may_file_options ?? []
  const mayFileChecked = stage.may_file ?? mayFileOptions

  function toggleMayFile(key: string, on: boolean) {
    const next = mayFileOptions.filter(k => (k === key ? on : mayFileChecked.includes(k)))
    set({ may_file: next.length === mayFileOptions.length ? null : next })
  }

  const editFields = (
    <Box sx={FIELD_GRID_SX}>
      <FormTextField
        label="Label" required error={fieldError('label')}
        help={LABEL_HINT}
        id={`stage-${index}-label`}
        value={stage.label}
        onChange={v => set({ label: v })}
        testId={`stage-${index}-label`}
      />
      <FormTextField
        label="Key" required error={fieldError('key')} help={KEY_HELP}
        id={`stage-${index}-key`}
        value={stage.key}
        onChange={v => set({ key: v })}
        testId={`stage-${index}-key`}
        slotProps={{ htmlInput: { spellCheck: false } }}
      />

      {select(`stage-${index}-expert-kind`, 'Expert', stage.expert.kind,
        labelled('expert_kind', ['pinned', 'chosen_at_start']), setExpertKind, 'expert_kind', EXPERT_KIND_HELP)}
      {stage.expert.kind === 'pinned'
        ? select(`stage-${index}-expert`, 'Which expert', stage.expert.expert, experts,
            v => set({ expert: { kind: 'pinned', expert: v } }), 'expert', 'The persona this stage runs as; sets its prompt.')
        : select(`stage-${index}-expert-default`, 'Default expert', stage.expert.default,
            experts.filter(e => (stage.expert as { allowed: string[] }).allowed.includes(e.key)),
            v => set({ expert: { ...(stage.expert as Extract<ExpertSpec, { kind: 'chosen_at_start' }>), default: v } }),
            'expert', 'Preselected when the playbook starts; one of the allowed experts.')}

      {stage.expert.kind === 'chosen_at_start' && (
        <FormControl component="fieldset" sx={{ gridColumn: '1 / -1', minWidth: 0 }}>
          <FormLabel component="legend" sx={{ typography: 'caption' }}>
            <HelpLabel label="Allowed experts" help="Only these are offered when the playbook starts." />
          </FormLabel>
          <FormGroup row data-testid={`stage-${index}-allowed`}>
            {experts.map(e => {
              const chosen = stage.expert as Extract<ExpertSpec, { kind: 'chosen_at_start' }>
              const on = chosen.allowed.includes(e.key)
              return (
                <FormControlLabel
                  key={e.key}
                  label={e.label}
                  sx={{ mr: 2, minHeight: SIZE.tapTarget }}
                  control={
                    <Checkbox
                      size="small"
                      checked={on}
                      onChange={(_, v) => {
                        const allowed = experts.map(x => x.key).filter(k => (k === e.key ? v : chosen.allowed.includes(k)))
                        set({ expert: { ...chosen, allowed } })
                      }}
                    />
                  }
                />
              )
            })}
          </FormGroup>
        </FormControl>
      )}

      {stage.expert.kind === 'pinned' ? (
        select(`stage-${index}-deliverable`, 'Deliverable', stage.deliverable ?? '',
          (vocab?.deliverables ?? []).map(d => ({ key: d.key, label: d.label, help: OPTION_HELP.deliverable[d.key] })),
          v => set({ deliverable: v, may_file: null }), 'deliverable', DELIVERABLE_HELP)
      ) : (
        <FormTextField
          label="Deliverable" error={fieldError('deliverable')} id={`stage-${index}-deliverable`}
          help={FOLLOWS_EXPERT_HINT}
          value="Follows the expert" onChange={() => {}} slotProps={{ htmlInput: { readOnly: true } }}
        />
      )}

      {select(`stage-${index}-seniority`, 'Seniority', stage.seniority,
        labelled('seniority', STAGE_SENIORITIES), v => set({ seniority: v }), 'seniority',
        SENIORITY_HINT)}
      {select(`stage-${index}-created`, 'Created', stage.created.kind,
        labelled('created', ['at_start', 'by'], 'Created'),
        v => set({ created: v === 'by' ? { kind: 'by', stage: others[0]?.s.key ?? '' } : { kind: 'at_start' } }),
        'created', CREATED_HINT)}
      {stage.created.kind === 'by' &&
        select(`stage-${index}-created-by`, 'Created by', stage.created.stage,
          others.map(({ s }) => ({ key: s.key, label: stageName(s) })),
          v => set({ created: { kind: 'by', stage: v } }), 'created', "The Plan stage that files this stage's task.")}
      {select(`stage-${index}-runs`, 'Runs', stage.runs, labelled('runs', ['once', 'per_planned_item'], 'Runs'),
        v => set({ runs: v }), 'runs', RUNS_HELP)}
      {select(`stage-${index}-depends`, 'Waits for', stage.depends_on.kind,
        labelled('depends_on', ['none', 'all_earlier', 'stages'], 'Waits for'), setDepends, 'depends_on', WAITS_FOR_HELP)}
      {select(`stage-${index}-interviews`, 'Asks questions', stage.interviews,
        labelled('interviews', ['follows_full_auto', 'never'], 'Asks questions'), v => set({ interviews: v }), 'interviews',
        INTERVIEWS_HINT)}

      {stage.depends_on.kind === 'stages' && (
        <FormControl component="fieldset" sx={{ gridColumn: '1 / -1', minWidth: 0 }}>
          <FormLabel component="legend" sx={{ typography: 'caption' }} error={Boolean(fieldError('depends_on'))}>
            <HelpLabel label="Stages it waits for" help="This stage starts only after every ticked stage finishes." />
          </FormLabel>
          <FormGroup row data-testid={`stage-${index}-depends-keys`}>
            {others.map(({ s, i }) => {
              const keys = (stage.depends_on as { keys: string[] }).keys
              return (
                <FormControlLabel
                  key={`${s.key}-${i}`}
                  label={stageName(s)}
                  sx={{ mr: 2, minHeight: SIZE.tapTarget }}
                  control={
                    <Checkbox
                      size="small"
                      checked={keys.includes(s.key)}
                      onChange={(_, v) =>
                        set({
                          depends_on: {
                            kind: 'stages',
                            keys: others.map(o => o.s.key).filter(k => (k === s.key ? v : keys.includes(k))),
                          },
                        })
                      }
                    />
                  }
                />
              )
            })}
          </FormGroup>
        </FormControl>
      )}

      {mayFileOptions.length > 0 && (
        <FormControl component="fieldset" sx={{ gridColumn: '1 / -1', minWidth: 0 }}>
          <FormLabel component="legend" sx={{ typography: 'caption' }} error={Boolean(fieldError('may_file'))}>
            <HelpLabel label="May file" help={MAY_FILE_HINT} />
          </FormLabel>
          <FormGroup data-testid={`stage-${index}-may-file`}>
            {mayFileOptions.map(k => (
              <FormControlLabel
                key={k}
                label={labelOf(vocab, 'may_file', k)}
                sx={{ minHeight: SIZE.tapTarget }}
                control={<Checkbox size="small" checked={mayFileChecked.includes(k)} onChange={(_, v) => toggleMayFile(k, v)} />}
              />
            ))}
          </FormGroup>
          {fieldError('may_file') && <FormHelperText error>{fieldError('may_file')}</FormHelperText>}
        </FormControl>
      )}

      <Box sx={{ gridColumn: '1 / -1' }}>
        <FormTextField
          label="Extra instructions" error={fieldError('extra_instructions')}
          help={INSTRUCTIONS_HINT}
          id={`stage-${index}-instructions`}
          multiline
          minRows={2}
          value={stage.extra_instructions}
          onChange={v => set({ extra_instructions: v })}
          testId={`stage-${index}-instructions`}
        />
      </Box>
    </Box>
  )

  const configFacts = (
    <FactsGrid minTile={grid(32)} sx={FACTS_SX} testId={`stage-${index}-config`}>
      <DetailField label="Key" help={KEY_HELP}><Box component="code" sx={monoSx}>{stage.key}</Box></DetailField>
      <DetailField label="Expert" help={EXPERT_KIND_HELP}>
        {stage.expert.kind === 'pinned'
          ? labelOf(vocab, 'expert', stage.expert.expert)
          : `${labels.expert_kind?.chosen_at_start ?? ''}: ${listOf(vocab, 'expert', stage.expert.allowed)} (default ${labelOf(vocab, 'expert', stage.expert.default)})`}
      </DetailField>
      <DetailField label="Deliverable" help={stage.expert.kind === 'pinned' ? DELIVERABLE_HELP : FOLLOWS_EXPERT_HINT}>
        {stage.expert.kind === 'pinned' ? labelOf(vocab, 'deliverable', stage.deliverable) : 'Follows the expert'}
      </DetailField>
      <DetailField label="Seniority" help={SENIORITY_HINT}>{labelOf(vocab, 'seniority', stage.seniority)}</DetailField>
      <DetailField label="Created" help={CREATED_HINT}>
        {stage.created.kind === 'by'
          ? `By ${stageName(stages.find(s => s.key === (stage.created as { stage: string }).stage) ?? { ...stage, label: stage.created.stage })}`
          : valueOf('created', 'at_start', 'Created')}
      </DetailField>
      <DetailField label="Runs" help={RUNS_HELP}>{valueOf('runs', stage.runs, 'Runs')}</DetailField>
      <DetailField label="Waits for" help={WAITS_FOR_HELP}>
        {stage.depends_on.kind === 'stages'
          ? stage.depends_on.keys.map(k => stageName(stages.find(s => s.key === k) ?? { ...stage, label: k })).join(', ') || 'Nothing'
          : valueOf('depends_on', stage.depends_on.kind, 'Waits for')}
      </DetailField>
      <DetailField label="Asks questions" help={INTERVIEWS_HINT}>{valueOf('interviews', stage.interviews, 'Asks questions')}</DetailField>
      {mayFileOptions.length > 0 && <DetailField label="May file" help={MAY_FILE_HINT}>{phraseOf(vocab, 'may_file', mayFileChecked)}</DetailField>}
      {stage.extra_instructions && (
        <DetailField label="Extra instructions" help={INSTRUCTIONS_HINT} fullRow>
          <Box component="span" sx={{ whiteSpace: 'pre-wrap' }}>{stage.extra_instructions}</Box>
        </DetailField>
      )}
    </FactsGrid>
  )

  const byExpert = derived?.by_expert ?? {}
  const derivedFacts = (
    <Box data-testid={`stage-${index}-derived`}>
      <Typography variant="caption" component="h4" sx={{ m: 0, mb: 0.5, fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary' }}>
        Set by the deliverable
      </Typography>
      <FactsGrid minTile={grid(32)} sx={FACTS_SX}>
        {stage.expert.kind === 'pinned' && (
          <>
            <DetailField label="Ends when" help={DERIVED_HINT.ends_when} testId={`stage-${index}-ends-when`}>{labelOf(vocab, 'ends_when', derived?.ends_when) || '—'}</DetailField>
            <DetailField label="Mode" help={DERIVED_HINT.mode} testId={`stage-${index}-mode`}>{labelOf(vocab, 'mode', derived?.mode) || '—'}</DetailField>
          </>
        )}
        <DetailField label="Model tier" help={DERIVED_HINT.tier} testId={`stage-${index}-tier`}>{labelOf(vocab, 'tier', derived?.tier) || '—'}</DetailField>
        <DetailField label="Receives" help={DERIVED_HINT.receives}>{listOf(vocab, 'hand_off', derived?.receives)}</DetailField>
        {stage.expert.kind === 'pinned' && <DetailField label="Hands off" help={DERIVED_HINT.hands_off}>{listOf(vocab, 'hand_off', derived?.hands_off)}</DetailField>}
      </FactsGrid>
      {stage.expert.kind === 'chosen_at_start' && Object.keys(byExpert).length > 0 && (
        <Box component="ul" data-testid={`stage-${index}-by-expert`} sx={{ m: 0, mt: 1, pl: 0, listStyle: 'none', display: 'grid', gap: 0.5 }}>
          {Object.entries(byExpert).map(([expert, facts]) => (
            <Box component="li" key={expert} sx={{ typography: 'body2', overflowWrap: 'anywhere' }}>
              <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{labelOf(vocab, 'expert', expert)}</Box>
              {': '}
              {[
                labelOf(vocab, 'deliverable', derived?.deliverable_by_expert?.[expert]),
                labelOf(vocab, 'mode', facts.mode),
                labelOf(vocab, 'ends_when', facts.ends_when),
                facts.hands_off.length > 0 ? `hands off ${listOf(vocab, 'hand_off', facts.hands_off)}` : '',
              ].filter(Boolean).join(' · ')}
            </Box>
          ))}
        </Box>
      )}
    </Box>
  )

  // Messages: every warning, and every error its field does not show — all of them when collapsed or
  // read-only (no fields render), one with no field, or a filing error with no filing options to mark.
  const shownOnField = (i: Issue) =>
    !readOnly && !!i.field && !(i.field === 'may_file' && mayFileOptions.length === 0)
  const listed = issues.filter(i => i.severity === 'warning' || collapsed || !shownOnField(i))

  return (
    <Card
      variant="outlined"
      component="li"
      data-testid="stage-card"
      data-index={index}
      data-cid={props.cid}
      data-has-error={hasError ? 'true' : 'false'}
      aria-label={`Stage ${n}: ${name}`}
      sx={{ listStyle: 'none', borderColor: hasError ? 'error.main' : undefined, minWidth: 0, containerType: 'inline-size', containerName: STAGE_CARD_CONTAINER }}
    >
      <Box
        sx={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'flex-start',
          gap: 1,
          px: 1.5,
          py: 1,
          borderBottom: collapsed ? 0 : 1,
          borderColor: 'divider',
          /* A narrow card's controls are glyphs (`NarrowGlyph`), so at the editor's pane the header is one row. A
             phone keeps it to two lines: toggle, the title over its summary, and the controls as a 2×2 block
             beside them, rather than a third row of buttons under the text. */
          [PHONE]: { display: 'grid', gridTemplateColumns: 'auto minmax(0, 1fr) auto', alignItems: 'start' },
        }}
      >
        <Button
          type="button"
          variant="text"
          color="inherit"
          onClick={onToggle}
          aria-expanded={!collapsed}
          aria-controls={collapsed ? undefined : bodyId}
          aria-label={`${collapsed ? 'Expand' : 'Collapse'} stage ${n}`}
          data-testid={`stage-${index}-toggle`}
          sx={ICON_BUTTON_SX}
        >
          <ExpandToggleGlyph expanded={!collapsed} />
        </Button>
        <Box sx={{ flex: `1 1 ${grid(37)}`, minWidth: 0, pt: 0.5 }}>
          <Typography variant="subtitle1" component="h3" sx={{ fontWeight: FONT_WEIGHT.semibold, lineHeight: LINE_HEIGHT.snug, overflowWrap: 'anywhere' }} data-testid={`stage-${index}-title`}>
            <Box component="span" sx={{ color: 'text.secondary', mr: 0.5 }}>{n}.</Box>
            {name}
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid={`stage-${index}-summary`}>
            {stageSummary(stage, derived, vocab)}
          </Typography>
        </Box>
        <Box
          sx={{
            display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5, ml: 'auto',
            [PHONE]: { display: 'grid', gridTemplateColumns: readOnly ? 'auto' : 'repeat(2, auto)', ml: 0 },
          }}
        >
          <Tip title="Preview prompt"><Button
            type="button"
            variant="outlined"
            size="small"
            onClick={onPreview}
            aria-label="Preview prompt"
            data-testid={`stage-${index}-preview`}
            sx={[tapTargetSx, { [STAGE_CARD_NARROW]: { minWidth: SIZE.tapTarget, px: 0.5 }, [PHONE]: { gridRow: 2, gridColumn: 1 } }, readOnly && { [PHONE]: { gridRow: 1 } }]}
          >
            <NarrowGlyph glyph="👁" label="Preview prompt" />
          </Button></Tip>
          {!readOnly && (
            <>
              <Button
                type="button"
                variant="text"
                disabled={index === 0}
                onClick={() => onMove(-1)}
                aria-label={`Move ${name} up`}
                data-testid={`stage-${index}-up`}
                sx={[...ICON_BUTTON_SX, { [PHONE]: { gridRow: 1, gridColumn: 1 } }]}
              >
                <Box component="span" aria-hidden="true">↑</Box>
              </Button>
              <Button
                type="button"
                variant="text"
                disabled={index === count - 1}
                onClick={() => onMove(1)}
                aria-label={`Move ${name} down`}
                data-testid={`stage-${index}-down`}
                sx={[...ICON_BUTTON_SX, { [PHONE]: { gridRow: 1, gridColumn: 2 } }]}
              >
                <Box component="span" aria-hidden="true">↓</Box>
              </Button>
              <Button
                type="button"
                variant="text"
                color="error"
                onClick={onRemove}
                aria-label={`Remove ${name}`}
                data-testid={`stage-${index}-remove`}
                sx={[tapTargetSx, { [STAGE_CARD_NARROW]: { minWidth: SIZE.tapTarget, px: 0.5 }, [PHONE]: { gridRow: 2, gridColumn: 2 } }]}
              >
                <NarrowGlyph glyph="🗑" label="Remove" />
              </Button>
            </>
          )}
        </Box>
      </Box>

      {listed.length > 0 && (
        <Box sx={{ px: 1.5, pt: 1, display: 'grid', gap: 0.5 }} data-testid={`stage-${index}-issues`}>
          {listed.map((i, k) => (
            <Banner key={k} severity={i.severity === 'error' ? 'error' : 'warning'}>{i.message}</Banner>
          ))}
        </Box>
      )}

      {!collapsed && (
        <Box id={bodyId} sx={{ p: 1.5, display: 'grid', gap: 2 }}>
          {readOnly ? configFacts : editFields}
          <Box sx={{ pt: 1.5, borderTop: '1px solid', borderColor: 'divider' }}>{derivedFacts}</Box>
        </Box>
      )}
    </Card>
  )
}
