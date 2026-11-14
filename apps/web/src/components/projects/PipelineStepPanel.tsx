import { Fragment, type JSX, type ReactNode, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Typography from '@mui/material/Typography'
import { apiDetail, post } from '../../api'
import {
  ACTION_LABEL, APPROVALS_MAX, type AllowedHours, type PipelineStep, type StepAction, type StepRules, type Who,
} from '../../lib/pipeline'
import { fmtTimestamp } from '../../lib/relativeTime'
import { viewerZone } from '../../lib/zonedTime'
import type { ProjectMember } from '../../types'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS, SIZE } from '../../theme'
import type { LookupOption } from '../../ui/AsyncLookup'
import { DetailCell } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import HelpLabel from '../../ui/HelpLabel'
import SegmentedControl from '../../ui/SegmentedControl'
import SelectField from '../../ui/SelectField'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { PHONE, PIPELINE_STEPS_CONTAINER, PIPELINE_STEPS_NARROW, tapTargetSx } from '../../ui/listStyles'
import { type LinkedField, AUTO_BUTTON_SX, LinkedControl } from './PipelineEditor'
import WhoPicker from './PipelineWho'

/* ONE STEP of Project settings › Pipeline (`PipelineSettings.tsx`) as a panel in place of a table row: a header that
 * names the step (action, from → to) and sums up its rules, opening the panel; the panel holds the step's rules in the
 * groups of the pipeline rules mockup — Who (who can press, how it runs), Gates (a merge or release step's ordered gate list), Approvals (count,
 * approvers, requester can't approve, timeout and what then, instructions), Timing (wait after arrival, allowed
 * hours), Concurrency (release steps only) and Paused (only while the step is paused by itself, with Resume). Every
 * rule is a field of `lib/pipeline.ts::StepRules`; a 422 naming one (its `field`) shows under it. */

export type StepDraft = { who: Who | null; approvals: number; auto: boolean; rules: StepRules }

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'] as const
const UNITS = { minutes: 1, hours: 60, days: 1440 } as const
type Unit = keyof typeof UNITS
const unitOf = (minutes: number | null): Unit => (!minutes ? 'hours'
  : minutes % UNITS.days === 0 ? 'days' : minutes % UNITS.hours === 0 ? 'hours' : 'minutes')
/** "1h", "3d", "45m": a duration in the step's summary. */
const shortDuration = (minutes: number): string => {
  const unit = unitOf(minutes)
  return `${minutes / UNITS[unit]}${unit[0]}`
}
/** Every IANA zone the browser knows, the given one first kept when the browser lacks it. */
function zones(current: string): string[] {
  const all = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf?.('timeZone') ?? ['UTC']
  const list = all.includes('UTC') ? all : ['UTC', ...all]
  return current && !list.includes(current) ? [current, ...list] : list
}
export const DEFAULT_HOURS = (): AllowedHours => ({ days: [0, 1, 2, 3, 4], from: '09:00', to: '17:00', timezone: viewerZone() })

/** The days as words: "Mon–Fri", "Mon, Wed, Fri". */
export function daysText(days: readonly number[]): string {
  const sorted = [...days].sort((a, b) => a - b)
  if (sorted.length === 7) return 'Every day'
  const run = sorted.length > 2 && sorted.every((d, i) => i === 0 || d === sorted[i - 1] + 1)
  return run ? `${DAYS[sorted[0]]}–${DAYS[sorted[sorted.length - 1]]}` : sorted.map(d => DAYS[d]).join(', ')
}

/** What the collapsed header says about the step's rules, each part a word or two. */
export function stepSummary(step: StepDraft, action: StepAction, paused: boolean): string[] {
  const r = step.rules
  return [
    step.auto ? 'By itself' : 'By hand',
    ...(step.approvals > 0 ? [`${step.approvals} ${step.approvals === 1 ? 'approval' : 'approvals'}`] : []),
    ...(r.soak_minutes > 0 ? [`Wait ${shortDuration(r.soak_minutes)}`] : []),
    ...(r.allowed_hours ? [`${daysText(r.allowed_hours.days)} ${r.allowed_hours.from}–${r.allowed_hours.to}`] : []),
    ...(action === 'release' && r.concurrency === 'in_order' ? ['In order'] : []),
    ...(paused ? ['Paused'] : []),
  ]
}

const PANEL_SX = {
  border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, bgcolor: 'background.paper', minWidth: 0,
  containerType: 'inline-size', containerName: PIPELINE_STEPS_CONTAINER,
} as const
const HEADER_SX = { display: 'flex', alignItems: 'center', gap: 1, px: 1.5, minWidth: 0 } as const
const TOGGLE_SX = {
  flex: 1, minWidth: 0, display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', alignContent: 'center', columnGap: 1, rowGap: 0.5,
  justifyContent: 'flex-start', textAlign: 'left', py: 1, minHeight: SIZE.tapTarget, borderRadius: RADIUS.sm,
  '&:focus-visible': { bgcolor: 'action.hover' },
} as const
const BODY_SX = { px: 1.5, pb: 1.5, display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 } as const
const GROUP_SX = { borderTop: '1px solid', borderColor: 'divider', pt: 1.5, display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 } as const
/* A field cell: two or three to a line on a desktop panel, the whole line when the panel is narrow. */
const CELL_SX = { [PIPELINE_STEPS_NARROW]: { flexBasis: '100%', maxWidth: 'none' }, [PHONE]: { flexBasis: '100%', maxWidth: 'none' } } as const
const LABEL_SX = { fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.medium, color: 'text.secondary', mb: 0.5 } as const
const DAY_ROW_SX = { display: 'flex', flexWrap: 'wrap', columnGap: 1.5, rowGap: 0.5 } as const

/** A labelled control that is not a text field (a picker button, a segmented control, a checkbox row). */
function Labeled({ label, help, error, testId, children }: {
  label: string; help?: string; error?: string; testId?: string; children: ReactNode
}): JSX.Element {
  return (
    <Box sx={{ minWidth: 0 }} data-testid={testId}>
      <Typography component="div" sx={LABEL_SX}><HelpLabel label={label} help={help} /></Typography>
      {children}
      {error && <FieldError message={error} />}
    </Box>
  )
}

function FieldError({ message }: { message: string }): JSX.Element {
  return <Typography variant="body2" color="error" aria-live="polite" sx={{ mt: 0.5 }} data-testid="pipeline-field-error">{message}</Typography>
}

function Group({ title, help, testId, children }: { title: string; help?: string; testId: string; children: ReactNode }): JSX.Element {
  return (
    <Box component="section" aria-label={title} data-testid={testId} sx={GROUP_SX}>
      <Typography component="h4" variant="subtitle2" sx={{ fontWeight: FONT_WEIGHT.semibold }}>
        <HelpLabel label={title} help={help} />
      </Typography>
      {children}
    </Box>
  )
}

/** A length of time as a number and a unit (minutes, hours, days), stored in minutes. Empty: `empty` (null or 0). */
function DurationField({ id, label, minutes, empty, onChange, disabled, readOnly, error, placeholder, testId }: {
  id: string; label: string; minutes: number | null; empty: 0 | null; onChange: (next: number | null) => void
  disabled: boolean; readOnly: boolean; error?: string; placeholder: string; testId: string
}): JSX.Element {
  // The unit is the person's pick: deriving it from the value would jump from minutes to hours as 60 is typed.
  const [unit, setUnit] = useState<Unit>(() => unitOf(minutes))
  const toMinutes = (raw: string, u: Unit): number | null => {
    const n = Math.round(Number(raw) * UNITS[u])
    return raw.trim() === '' || !Number.isFinite(n) || n <= 0 ? empty : n
  }
  // The typed text is kept as typed ("0.5", "0"); it is re-derived only when the value changes from outside (Discard,
  // Start from…, a fresh read), with the unit that shows it whole.
  const [text, setText] = useState(() => (minutes ? String(minutes / UNITS[unit]) : ''))
  useEffect(() => {
    if (toMinutes(text, unit) === minutes) return
    const u = unitOf(minutes)
    setUnit(u)
    setText(minutes ? String(minutes / UNITS[u]) : '')
  }, [minutes])
  return (
    <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start', minWidth: 0 }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <FormTextField id={id} label={label} type="number" value={text} placeholder={placeholder} error={error}
          onChange={raw => { setText(raw); onChange(toMinutes(raw, unit)) }} disabled={disabled} readOnly={readOnly} testId={testId}
          slotProps={{ htmlInput: { min: 0, step: 'any' } }} />
      </Box>
      <SelectField ariaLabel={`${label} unit`} value={unit} disabled={disabled || readOnly} testId={`${testId}-unit`}
        sx={{ mt: 1, flexShrink: 0 }}
        onChange={next => {
          const u = next as Unit
          setUnit(u)
          if (text) onChange(toMinutes(text, u))
        }}
        options={[{ value: 'minutes', label: 'minutes' }, { value: 'hours', label: 'hours' }, { value: 'days', label: 'days' }]} />
    </Box>
  )
}

export function PipelineStepPanel({
  projectId, stepKey, step, action, fromName, toName, defaultWho, stored, names, members, onName, canEdit, busy, open,
  onToggle, onChange, errors, gates, laneLink, onResumed, linked,
}: {
  projectId: number
  /** The step's stable key (its target stage's draft key), for field ids. */
  stepKey: string
  step: StepDraft
  action: StepAction
  fromName: string
  toName: string
  /** Who may press a step whose `who` is null: its action's default roles. */
  defaultWho: Who
  /** The step as read, when its stage is stored: its pause. */
  stored?: PipelineStep
  names: Record<string, string>
  members: () => Promise<ProjectMember[]>
  onName: (option: LookupOption) => void
  canEdit: boolean
  busy: boolean
  open: boolean
  onToggle: () => void
  onChange: (patch: Partial<StepDraft>) => void
  /** A refused save's message per field (`approvers`, `allowed_hours.timezone`, …). */
  errors: Record<string, string>
  /** A merge or release step's gate list (`components/pipeline/gates/GateList.tsx`), its count for the header and a
   *  line under it; null on a step that takes none (D1). */
  gates: { list: ReactNode; count: number; note?: ReactNode } | null
  /** The Merge lane link of a merge step, drawn beside the header. */
  laneLink: ReactNode | null
  onResumed: () => void
  /** On a project linked to an org template: Who, Approvals and how it runs, each locked or open
   *  (`PipelineEditor.tsx::LinkedControl`). */
  linked?: (field: 'who' | 'approvals' | 'auto') => LinkedField
}): JSX.Element {
  const off = !canEdit || busy
  /** Locked by the linked template. */
  const locked = (field: 'who' | 'approvals' | 'auto') => linked?.(field).locked === true
  /** The control, with its template lock or "Changed here" on a linked project. */
  const link = (field: 'who' | 'approvals' | 'auto', control: ReactNode) => {
    const l = linked?.(field)
    return l ? <LinkedControl link={l} field={field} where={toName || 'this stage'} fill={field === 'who'} canReset={!off}>{control}</LinkedControl> : control
  }
  const label = ACTION_LABEL[action]
  const verb = label.toLowerCase()
  const r = step.rules
  const who = step.who ?? defaultWho
  const setRules = (patch: Partial<StepRules>) => onChange({ rules: { ...r, ...patch } })
  const hours = r.allowed_hours
  const setHours = (patch: Partial<AllowedHours>) => hours && setRules({ allowed_hours: { ...hours, ...patch } })
  const pause = stored?.pause ?? null
  const id = (field: string) => `pipeline-${stepKey}-${field}`
  const hoursError = errors['allowed_hours.days'] ?? errors['allowed_hours']
  const [resumeError, setResumeError] = useState('')
  const [resuming, setResuming] = useState(false)
  const resume = async () => {
    if (!stored) return
    setResuming(true)
    setResumeError('')
    try {
      await post(`/api/projects/${projectId}/pipeline/steps/${stored.to_stage_id}/resume`)
      onResumed()
    } catch (err) {
      setResumeError(apiDetail(err, 'Could not resume this step'))
    } finally {
      setResuming(false)
    }
  }
  const summary = [...stepSummary(step, action, pause !== null), ...(gates ? [`${gates.count} ${gates.count === 1 ? 'gate' : 'gates'}`] : [])]
  return (
    <Box sx={PANEL_SX} data-testid="pipeline-step" data-action={action} role="group" aria-label={`${label}: ${fromName} → ${toName}`}>
      <Box sx={HEADER_SX}>
        <ButtonBase sx={TOGGLE_SX} onClick={onToggle} aria-expanded={open}
          aria-controls={id('rules')} data-testid="pipeline-step-toggle">
          <Box component="span" aria-hidden sx={{ color: 'text.secondary', width: grid(1.5), flexShrink: 0 }}>{open ? '▾' : '▸'}</Box>
          <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }} data-testid="pipeline-step-name">{label}</Box>
          <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>{fromName} → {toName}</Box>
          {/* Beside the stages on a desktop, after a dot; on its own line, indented under the name, on a phone. */}
          <Box component="span" aria-hidden sx={{ color: 'text.secondary', [PHONE]: { display: 'none' } }}>·</Box>
          <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm, [PHONE]: { flexBasis: '100%', pl: 2.5 } }}
            data-testid="pipeline-step-summary">
            {summary.map((part, n) => (
              <Fragment key={part}>{n > 0 && <span aria-hidden> · </span>}<Box component="span" sx={{ whiteSpace: 'nowrap' }}>{part}</Box></Fragment>
            ))}
          </Box>
        </ButtonBase>
        {laneLink}
      </Box>
      {open && (
        <Box sx={BODY_SX} id={id('rules')}>
          <Group title="Who" testId="pipeline-step-group-who">
            <FieldRow rowGap={1.5}>
              <DetailCell basis={grid(30)} max={grid(48)} sx={CELL_SX}>
                <Labeled label="Can press">
                  {link('who', (
                    <WhoPicker who={who} names={names} title={`Who can ${verb}`} label="Can press" disabled={off || locked('who')}
                      members={members} onName={onName} onChange={next => onChange({ who: next })} />
                  ))}
                </Labeled>
              </DetailCell>
              <DetailCell basis={grid(24)} max={grid(32)} sx={CELL_SX}>
                <Labeled label="Runs">
                  {link('auto', (
                    <SegmentedControl ariaLabel={`How ${verb} runs`} value={step.auto ? 'auto' : 'hand'} disabled={off || locked('auto')}
                      testId="pipeline-step-auto" buttonSx={AUTO_BUTTON_SX} sx={{ whiteSpace: 'nowrap' }}
                      onChange={v => onChange({ auto: v === 'auto' })}
                      options={[
                        { value: 'hand', label: 'By hand', help: 'Someone presses it.', testId: 'pipeline-step-by-hand' },
                        { value: 'auto', label: 'By itself', help: 'Runs as soon as work arrives.', testId: 'pipeline-step-by-itself' },
                      ]} />
                  ))}
                </Labeled>
              </DetailCell>
            </FieldRow>
          </Group>

          {gates && (
            <Group title="Gates" help="Run in order, one at a time. A required gate that fails stops the step; an advisory one only warns."
              testId="pipeline-step-group-gates">
              {gates.list}
              {gates.note}
            </Group>
          )}

          <Group title="Approvals" testId="pipeline-step-group-approvals">
            <FieldRow rowGap={1.5}>
              <DetailCell basis={grid(16)} max={grid(24)} sx={CELL_SX}>
                <Labeled label="Needed">
                  {link('approvals', (
                    <SelectField id={id('approvals')} ariaLabel={`Approvals needed to ${verb}`} value={String(step.approvals)}
                      onChange={v => onChange({ approvals: Number(v) })} disabled={off || locked('approvals')} testId="pipeline-step-approvals"
                      options={Array.from({ length: APPROVALS_MAX + 1 }, (_, n) => ({ value: String(n), label: String(n) }))} />
                  ))}
                </Labeled>
              </DetailCell>
              {step.approvals > 0 && (
                <DetailCell basis={grid(36)} max={grid(56)} sx={CELL_SX}>
                  <Labeled label="Approvers" error={errors.approvers} testId="pipeline-step-approvers">
                    <SettingCheckbox name={id('approvers-same')} label="Same as who can press" labelPlacement="end" dense
                      checked={r.approvers === null} disabled={off} testId="pipeline-step-approvers-same"
                      onChange={same => setRules({ approvers: same ? null : { roles: [...who.roles], users: [...who.users] } })} />
                    {r.approvers !== null && (
                      <Box sx={{ mt: 0.5 }}>
                        <WhoPicker who={r.approvers} names={names} title={`Approvers of ${verb}`} label="Approvers" disabled={off}
                          members={members} onName={onName} testPrefix="pipeline-approvers"
                          onChange={next => setRules({ approvers: next })} />
                      </Box>
                    )}
                  </Labeled>
                </DetailCell>
              )}
            </FieldRow>
            {step.approvals > 0 && (
              <>
                <SettingCheckbox name={id('requester')} label="Requester can't approve" labelPlacement="end" dense
                  checked={r.requester_cant_approve} disabled={off}
                  testId="pipeline-step-requester" onChange={on => setRules({ requester_cant_approve: on })} />
                <FieldRow rowGap={1.5}>
                  <DetailCell basis={grid(30)} max={grid(40)} sx={CELL_SX}>
                    <DurationField id={id('timeout')} label="Approval timeout" minutes={r.approval_timeout_minutes} empty={null}
                      placeholder="None" disabled={busy} readOnly={!canEdit} testId="pipeline-step-timeout"
                      error={errors.approval_timeout_minutes}
                      onChange={next => setRules({ approval_timeout_minutes: next })} />
                  </DetailCell>
                  {r.approval_timeout_minutes !== null && (
                    <DetailCell basis={grid(24)} max={grid(32)} sx={CELL_SX}>
                      <FormTextField id={id('on-timeout')} label="On timeout" value={r.on_timeout} disabled={busy}
                        readOnly={!canEdit} testId="pipeline-step-on-timeout"
                        onChange={v => setRules({ on_timeout: v as StepRules['on_timeout'] })}
                        options={[
                          { value: 'cancel', label: 'Cancel', help: 'The request is canceled.' },
                          { value: 'remind', label: 'Keep waiting', help: 'Approvers are reminded.' },
                        ]} />
                    </DetailCell>
                  )}
                </FieldRow>
                <FormTextField id={id('instructions')} label="Instructions for approvers" value={r.instructions} multiline
                  minRows={2} disabled={busy} readOnly={!canEdit} testId="pipeline-step-instructions" error={errors.instructions}
                  slotProps={{ htmlInput: { maxLength: 1000 } }} onChange={instructions => setRules({ instructions })} />
              </>
            )}
          </Group>

          <Group title="Timing" testId="pipeline-step-group-timing">
            <Box sx={{ maxWidth: grid(40) }}>
              <DurationField id={id('soak')} label="Wait after arrival" minutes={r.soak_minutes} empty={0} placeholder="None"
                disabled={busy} readOnly={!canEdit} testId="pipeline-step-soak" error={errors.soak_minutes}
                onChange={next => setRules({ soak_minutes: next ?? 0 })} />
            </Box>
            <SettingCheckbox name={id('hours-on')} label="Allowed hours" labelPlacement="end" dense checked={hours !== null}
              disabled={off} testId="pipeline-step-hours"
              onChange={on => setRules({ allowed_hours: on ? DEFAULT_HOURS() : null })} />
            {hours && (
              <>
                <Labeled label="Days" error={hoursError}>
                  <Box sx={DAY_ROW_SX} role="group" aria-label="Days">
                    {DAYS.map((day, n) => (
                      <SettingCheckbox key={day} name={id(`day-${n}`)} label={day} labelPlacement="end" dense
                        checked={hours.days.includes(n)} disabled={off} testId={`pipeline-step-day-${n}`}
                        onChange={on => setHours({ days: on ? [...hours.days, n].sort((a, b) => a - b) : hours.days.filter(d => d !== n) })} />
                    ))}
                  </Box>
                </Labeled>
                <FieldRow rowGap={1.5}>
                  <DetailCell basis={grid(18)} max={grid(24)} sx={CELL_SX}>
                    <FormTextField id={id('from')} label="From" type="time" value={hours.from} disabled={busy} readOnly={!canEdit}
                      testId="pipeline-step-hours-from" error={errors['allowed_hours.from']} onChange={from => setHours({ from })} />
                  </DetailCell>
                  <DetailCell basis={grid(18)} max={grid(24)} sx={CELL_SX}>
                    <FormTextField id={id('to')} label="To" type="time" value={hours.to} disabled={busy} readOnly={!canEdit}
                      testId="pipeline-step-hours-to" error={errors['allowed_hours.to']} onChange={to => setHours({ to })} />
                  </DetailCell>
                  <DetailCell basis={grid(30)} max={grid(40)} sx={CELL_SX}>
                    <FormTextField id={id('zone')} label="Time zone" value={hours.timezone} disabled={busy} readOnly={!canEdit}
                      testId="pipeline-step-hours-zone" error={errors['allowed_hours.timezone']}
                      onChange={timezone => setHours({ timezone })}
                      options={zones(hours.timezone).map(z => ({ value: z, label: z }))} />
                  </DetailCell>
                </FieldRow>
              </>
            )}
          </Group>

          {action === 'release' && (
            <Group title="Concurrency" testId="pipeline-step-group-concurrency"
              help="Never deploys an older version over a newer one.">
              <Labeled label="Order" error={errors.concurrency}>
                <SegmentedControl ariaLabel="Order" value={r.concurrency} disabled={off}
                  testId="pipeline-step-concurrency" buttonSx={AUTO_BUTTON_SX} sx={{ whiteSpace: 'nowrap' }}
                  onChange={concurrency => setRules({ concurrency })}
                  options={[
                    { value: 'newest', label: 'Newest wins', help: 'A newer release replaces one still waiting.', testId: 'pipeline-step-newest' },
                    { value: 'in_order', label: 'In order', help: 'Each release waits its turn.', testId: 'pipeline-step-in-order' },
                  ]} />
              </Labeled>
            </Group>
          )}

          {pause && (
            <Group title="Paused" testId="pipeline-step-group-paused">
              <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1.5 }}>
                <Typography variant="body2" sx={{ flex: '1 1 auto', minWidth: 0 }} data-testid="pipeline-step-pause">
                  {`Paused after release #${pause.release_id}${pause.version ? ` (${pause.version})` : ''}`}
                  {pause.by_name ? ` by ${pause.by_name}` : ''}{`, ${fmtTimestamp(pause.at)}. `}
                  <Box component="span" sx={{ color: 'text.secondary' }}>{`Lifts when ${fromName} has every change ${toName} has.`}</Box>
                </Typography>
                <Button size="small" variant="outlined" disabled={resuming || off} onClick={() => void resume()} sx={tapTargetSx}
                  data-testid="pipeline-step-resume">
                  Resume
                </Button>
              </Box>
              {resumeError && <FieldError message={resumeError} />}
            </Group>
          )}
        </Box>
      )}
    </Box>
  )
}

export default PipelineStepPanel
