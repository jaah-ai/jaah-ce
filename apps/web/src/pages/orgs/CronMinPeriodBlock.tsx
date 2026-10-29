import { type JSX } from 'react'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { SectionHeader } from '../../ui/SectionHeader'
import { useAuth } from '../../auth'
import { useInlineEdit } from '../../hooks/useInlineEdit'
import Banner from '../../ui/Banner'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadingState from '../../ui/LoadingState'
import { OverrideField, SaveCancel } from './OverrideControls'
import { cronPeriodError, getOrgCronMinPeriod, patchOrgCronMinPeriod, periodWords } from './api'

/* AN ORG'S OWN CRON MINIMUM PERIOD, the org admin's raise-only `own` (Settings > Organizations; never under what
 * the operator set — `routes/orgs.py`, `app/cron_min_period.py`). The operator's per-org `override` is a row of
 * the customer's Overrides tab (`OverridesTab.tsx`), same read, same PATCH. The API is the boundary: a tenant sending
 * `override` is refused there. Input in seconds, shown in words. */

export default function CronMinPeriodBlock({ orgId, title = 'Cron minimum period' }: {
  orgId: string
  title?: string
}): JSX.Element {
  const edit = useInlineEdit({
    queryKey: ['org-cron-min-period', orgId],
    load: () => getOrgCronMinPeriod(orgId),
    write: (value: number | null) => patchOrgCronMinPeriod(orgId, { own: value }),
    loadError: 'Failed to load the cron minimum.',
    saveError: 'Failed to save the cron minimum.',
  })
  const { error, draft, setDraft, busy, save } = edit
  const state = edit.data ?? null
  const editing = edit.editing !== null

  const trimmed = draft.trim()
  const parsed = /^\d+$/.test(trimmed) ? Number(trimmed) : null
  // The lowest value the draft may carry: what the operator set above it.
  const fieldError = state === null ? undefined : cronPeriodError(trimmed, parsed, state.upstream)
  const valid = state !== null && trimmed !== '' && fieldError === undefined
  const saveDraft = () => valid && save(parsed)
  const current = state === null ? null : state.own
  const titleId = 'org-cron-min-period-own-title'

  return (
    <Stack component="section" spacing={1} data-testid="org-cron-min-period" data-tier="own" aria-labelledby={titleId}>
      <SectionHeader title={title} titleId={titleId} />
      {error && <Banner severity="error" testId="org-cron-min-period-error">{error}</Banner>}
      {state === null ? (
        !error && <LoadingState rows={2} />
      ) : (
        <KeyValueList align="center">
          <KeyValue label="Effective" strong valueTestId="org-cron-min-period-effective">{periodWords(state.effective)}</KeyValue>

          <KeyValue label="Your minimum">
            {editing ? (
              <Stack direction="row" spacing={1} sx={{ alignItems: 'flex-start', flexWrap: 'wrap' }}>
                <OverrideField
                  value={draft}
                  onChange={e => setDraft(e.target.value)}
                  canSave={valid}
                  onSave={saveDraft}
                  onCancel={edit.cancel}
                  error={fieldError !== undefined}
                  // What the org follows with nothing set here, as the placeholder, never pre-filled (saving
                  // a pre-filled default would pin it as a value).
                  placeholder={String(state.upstream)}
                  helperText={fieldError ?? `Seconds, at least ${state.upstream}`}
                  slotProps={{ htmlInput: { inputMode: 'numeric', 'aria-label': 'Your minimum (seconds)' } }}
                  sx={{ width: 200 }}
                />
                <SaveCancel busy={busy} canSave={valid} onSave={saveDraft} onCancel={edit.cancel} />
              </Stack>
            ) : (
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
                <Typography component="span" variant="body2" data-testid="org-cron-min-period-own">
                  {current === null ? 'None' : periodWords(current)}
                </Typography>
                <Button size="small" disabled={busy} aria-label="Edit your minimum"
                  onClick={() => edit.startEdit('own', current === null ? '' : String(current))}>
                  Edit
                </Button>
                {current !== null && (
                  <Button size="small" disabled={busy} aria-label="Clear your minimum" onClick={() => save(null)}>
                    Clear
                  </Button>
                )}
              </Stack>
            )}
          </KeyValue>

          <KeyValue
            label={state.upstream_source === 'operator' ? 'Operator minimum' : 'Set by your operator'}
            valueTestId="org-cron-min-period-upstream"
          >
            {periodWords(state.upstream)}
          </KeyValue>
        </KeyValueList>
      )}
    </Stack>
  )
}

/* Settings > Tasks > Schedules: the viewer's own org's raise-only value. The page mounts this for an org admin
 * with an org bound (`Settings.tsx::panelShown`). */
export function OwnCronMinPeriodSection(): JSX.Element | null {
  const { admin } = useAuth()
  const orgId = admin?.org?.id
  if (!orgId) return null
  return <CronMinPeriodBlock orgId={orgId} />
}
