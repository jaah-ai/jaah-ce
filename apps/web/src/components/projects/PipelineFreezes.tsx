import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { type FreezeNow, type FreezeNowOut, putFreezeNow } from '../../lib/pipeline'
import { fmtTimestamp } from '../../lib/relativeTime'
import { fromLocalInput, toLocalInput, type Freeze, type FreezeIn, type FreezeRepeat, type FreezeScope } from '../../lib/releases'
import { FONT_SIZE, FONT_WEIGHT, grid } from '../../theme'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import HelpLabel from '../../ui/HelpLabel'
import { NoneMark } from '../../ui/NoneMark'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { PHONE, tapTargetSx } from '../../ui/listStyles'

/* PROJECT SETTINGS › PIPELINE › FREEZES (`PipelineSettings.tsx`): Freeze now at the top — a checkbox that applies at
 * once (`PUT …/release/freeze-now`, audited server-side) — then each freeze: name, start and end, how it repeats
 * (never, daily, weekly, monthly), which stages it covers (every stage after the integration stage, or picked
 * branch stages — D-9: a freeze only holds stages that deploy to a branch), and what it lets through (hand-pressed
 * steps, releases already queued). The freezes ride along in the page's one Save; a 422 naming a freeze's field
 * (`index`, `field`) shows under that field. */

export type FreezeRow = {
  key: string; id?: string; name: string; starts: string; ends: string; timezone: string
  repeat: FreezeRepeat; scope: FreezeScope; stage_ids: string[]; let_pressed: boolean; let_queued: boolean
}

export const freezeRowOf = (f: Freeze): FreezeRow => ({
  key: f.id, id: f.id, name: f.name, starts: toLocalInput(f.starts_at), ends: toLocalInput(f.ends_at), timezone: f.timezone,
  repeat: f.repeat ?? (f.weekly ? 'weekly' : 'never'), scope: f.scope ?? 'after_integration', stage_ids: f.stage_ids ?? [],
  let_pressed: f.let_pressed ?? false, let_queued: f.let_queued ?? true,
})
export const newFreezeRow = (key: string, timezone: string): FreezeRow => ({
  key, name: 'Freeze', starts: '', ends: '', timezone, repeat: 'never', scope: 'after_integration', stage_ids: [],
  let_pressed: false, let_queued: true,
})
export const freezeBody = (rows: FreezeRow[]): FreezeIn[] => rows.map(r => ({
  ...(r.id ? { id: r.id } : {}),
  name: r.name.trim(),
  starts_at: fromLocalInput(r.starts) ?? r.starts,
  ends_at: fromLocalInput(r.ends) ?? r.ends,
  weekly: r.repeat === 'weekly',
  timezone: r.timezone,
  repeat: r.repeat,
  scope: r.scope,
  stage_ids: r.scope === 'stages' ? r.stage_ids : [],
  let_pressed: r.let_pressed,
  let_queued: r.let_queued,
}))
/** What Save would refuse before it is pressed: an empty field, or picked stages with none picked. */
export const freezeIncomplete = (f: FreezeRow): boolean =>
  !f.starts || !f.ends || !f.name.trim() || (f.scope === 'stages' && f.stage_ids.length === 0)

const FREEZE_ROW_SX = { '& + &': { borderTop: '1px solid', borderColor: 'divider', pt: 1.5 } } as const
/* On a phone each freeze field takes the full width: a date-time beside the name crams its value. */
const FREEZE_CELL_SX = { [PHONE]: { flexBasis: '100%', maxWidth: 'none' } } as const
const LABEL_SX = { fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.medium, color: 'text.secondary', mb: 0.5 } as const
const CHECK_ROW_SX = { display: 'flex', flexWrap: 'wrap', columnGap: 2, rowGap: 0.5 } as const

const REPEATS: { value: FreezeRepeat; label: string; help?: string }[] = [
  { value: 'never', label: 'Never' },
  { value: 'daily', label: 'Daily' },
  { value: 'weekly', label: 'Weekly' },
  { value: 'monthly', label: 'Monthly', help: 'Shorter months use their last day.' },
]

export function PipelineFreezes({
  projectId, rows, canEdit, busy, branchStages, integration, freezeNow, errors, onAdd, onPatch, onRemove, onFreezeNow,
}: {
  projectId: number
  rows: FreezeRow[]
  canEdit: boolean
  busy: boolean
  /** The stored stages that deploy to a branch: what a freeze may cover. */
  branchStages: { id: string; name: string }[]
  /** The integration stage's name: "after" it is a freeze's default scope. */
  integration: string | null
  freezeNow: FreezeNow | null
  /** A refused save's message per freeze key, per field. */
  errors: Record<string, Record<string, string>>
  onAdd: () => void
  onPatch: (key: string, patch: Partial<FreezeRow>) => void
  onRemove: (key: string) => void
  onFreezeNow: (out: FreezeNowOut) => Promise<void>
}): JSX.Element {
  const off = !canEdit || busy
  // Short, in a cell that never shrinks below its basis, so the side panel shows it whole.
  const after = `After ${integration ?? 'the first branch'}`
  /* The pick shows at once while its write runs; a refusal drops it back to the stored state. Held while Save runs:
     its answer, read before the switch, would put the old state back. */
  const [switching, setSwitching] = useState<boolean | null>(null)
  const [nowError, setNowError] = useState('')
  const switchNow = async (on: boolean) => {
    setSwitching(on)
    setNowError('')
    try {
      await onFreezeNow(await putFreezeNow(projectId, on))
    } catch (err) {
      setNowError(apiDetail(err, 'Could not switch Freeze now'))
    } finally {
      setSwitching(null)
    }
  }
  return (
    <DetailSection title="Freezes" divided testId="pipeline-freezes"
      help="No release starts inside a freeze; a scheduled one waits for its end."
      actions={canEdit && (
        <Button size="small" disabled={off || rows.length >= 50} data-testid="pipeline-freeze-add" sx={tapTargetSx} onClick={onAdd}>
          Add freeze
        </Button>
      )}>
      <Stack spacing={1.5}>
        <Box data-testid="pipeline-freeze-now-row">
          <SettingCheckbox name="freeze-now" label="Freeze now" labelPlacement="end" dense checked={switching ?? freezeNow !== null}
            disabled={!canEdit || busy || switching !== null} testId="pipeline-freeze-now"
            help={`Stops every release after ${integration ?? 'the first branch'} until switched off.`}
            onChange={on => void switchNow(on)} />
          {nowError && <Typography variant="body2" color="error" aria-live="polite">{nowError}</Typography>}
        </Box>
        {rows.length === 0 ? <NoneMark /> : rows.map(f => {
          const err = errors[f.key] ?? {}
          return (
            <Box key={f.key} data-testid="pipeline-freeze" sx={FREEZE_ROW_SX}>
              <Stack spacing={1}>
                <FieldRow rowGap={1.5} align="center">
                  <DetailCell basis={grid(24)} max={grid(32)} sx={FREEZE_CELL_SX}>
                    <FormTextField id={`freeze-${f.key}-name`} label="Name" value={f.name} onChange={name => onPatch(f.key, { name })}
                      readOnly={!canEdit} disabled={busy} testId="pipeline-freeze-name" error={err.name} />
                  </DetailCell>
                  <DetailCell basis={grid(32.5)} max={grid(40)} sx={FREEZE_CELL_SX}>
                    <FormTextField id={`freeze-${f.key}-starts`} label="Starts" type="datetime-local" value={f.starts}
                      onChange={starts => onPatch(f.key, { starts })} readOnly={!canEdit} disabled={busy} error={err.starts_at}
                      slotProps={{ inputLabel: { shrink: true } }} testId="pipeline-freeze-starts" />
                  </DetailCell>
                  <DetailCell basis={grid(32.5)} max={grid(40)} sx={FREEZE_CELL_SX}>
                    <FormTextField id={`freeze-${f.key}-ends`} label="Ends" type="datetime-local" value={f.ends}
                      onChange={ends => onPatch(f.key, { ends })} readOnly={!canEdit} disabled={busy} error={err.ends_at}
                      slotProps={{ inputLabel: { shrink: true } }} testId="pipeline-freeze-ends" />
                  </DetailCell>
                </FieldRow>
                <FieldRow rowGap={1.5}>
                  <DetailCell basis={grid(18)} max={grid(24)} sx={FREEZE_CELL_SX}>
                    <FormTextField id={`freeze-${f.key}-repeat`} label="Repeats" value={f.repeat} readOnly={!canEdit} disabled={busy}
                      onChange={repeat => onPatch(f.key, { repeat: repeat as FreezeRepeat })} testId="pipeline-freeze-repeat"
                      error={err.repeat} options={REPEATS} />
                  </DetailCell>
                  <DetailCell basis={grid(32)} max={grid(48)} sx={[{ flexShrink: 0 }, FREEZE_CELL_SX]}>
                    <FormTextField id={`freeze-${f.key}-scope`} label="Stages" value={f.scope} readOnly={!canEdit} disabled={busy}
                      onChange={scope => onPatch(f.key, { scope: scope as FreezeScope })} testId="pipeline-freeze-scope"
                      options={[{ value: 'after_integration', label: after },
                        // Picked stages only while some stage deploys to a branch: none to pick would hold Save for good.
                        ...(branchStages.length > 0 || f.scope === 'stages' ? [{ value: 'stages', label: 'Picked stages' }] : [])]} />
                    {f.scope === 'stages' && (
                      <Box sx={[CHECK_ROW_SX, { mt: 0.5 }]} role="group" aria-label="Stages it covers" data-testid="pipeline-freeze-stages">
                        {branchStages.map(s => (
                          <SettingCheckbox key={s.id} name={`freeze-${f.key}-stage-${s.id}`} label={s.name} labelPlacement="end" dense
                            checked={f.stage_ids.includes(s.id)} disabled={off} testId="pipeline-freeze-stage"
                            onChange={on => onPatch(f.key, {
                              stage_ids: on ? [...f.stage_ids, s.id] : f.stage_ids.filter(x => x !== s.id),
                            })} />
                        ))}
                      </Box>
                    )}
                    {err.stage_ids && <Typography variant="body2" color="error" aria-live="polite" data-testid="pipeline-field-error">{err.stage_ids}</Typography>}
                  </DetailCell>
                  <DetailCell basis={grid(36)} max={grid(56)} sx={FREEZE_CELL_SX}>
                    <Typography component="div" id={`freeze-${f.key}-let`} sx={LABEL_SX}><HelpLabel label="Let through" /></Typography>
                    <Box sx={CHECK_ROW_SX} role="group" aria-labelledby={`freeze-${f.key}-let`}>
                      <SettingCheckbox name={`freeze-${f.key}-pressed`} label="Hand-pressed steps" labelPlacement="end" dense
                        checked={f.let_pressed} disabled={off} testId="pipeline-freeze-let-pressed"
                        onChange={let_pressed => onPatch(f.key, { let_pressed })} />
                      <SettingCheckbox name={`freeze-${f.key}-queued`} label="Releases already queued" labelPlacement="end" dense
                        checked={f.let_queued} disabled={off} testId="pipeline-freeze-let-queued"
                        help="A release queued before the freeze began finishes." onChange={let_queued => onPatch(f.key, { let_queued })} />
                    </Box>
                  </DetailCell>
                  {canEdit && (
                    <Box sx={{ alignSelf: 'center', ml: 'auto' }}>
                      <Button color="error" size="small" disabled={off} sx={tapTargetSx} data-testid="pipeline-freeze-remove"
                        onClick={() => onRemove(f.key)}>
                        Remove
                      </Button>
                    </Box>
                  )}
                </FieldRow>
              </Stack>
            </Box>
          )
        })}
      </Stack>
    </DetailSection>
  )
}

/** "Frozen now by Ana at Nov 12, 14:02. Every release after Dev waits." */
export const freezeNowText = (now: FreezeNow, integration: string | null): string =>
  `Frozen now${now.by_name ? ` by ${now.by_name}` : ''} at ${fmtTimestamp(now.at)}. `
  + `Every release after ${integration ?? 'the first branch'} waits.`

export default PipelineFreezes
