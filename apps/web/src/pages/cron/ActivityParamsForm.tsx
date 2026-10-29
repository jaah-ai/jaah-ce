import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import Banner from '../../ui/Banner'
import type { ActivityType, ParamsProperty } from './api'

/* THE SYSTEM-FUNCTION PARAMS FORM — an automation's system `activity_params`, READ-ONLY, as values and never
 * as raw JSON. Control-plane machinery: what it does when it runs is not edited in the console. Rendered by
 * the automation editor's system action (`pages/automations/actions/index.ts`).
 *
 * LABELS FROM THE SERVER'S SCHEMA (`app/cron_activities.py`, via `GET /api/crons/activity-types`); VALUES FROM
 * WHAT IS STORED.
 */

export type ActivityParamsFormProps = {
  /** The registered descriptor, or null for an activity THIS BUILD does not register (rolling deploy,
   *  retired): stored values, with why. */
  activity: ActivityType | null
  /** Whether the catalogue has answered — distinct from `activity` being null (absence-is-not-a-state). */
  catalogueLoaded: boolean
  /** The row's raw `activity_type`, needed for the null case above (there is no descriptor to name). */
  activityType: string
  params: Record<string, unknown>
}

export default function ActivityParamsForm({ activity, catalogueLoaded, activityType, params }: ActivityParamsFormProps) {
  return (
    <Box data-testid="cron-activity-params">
      {!catalogueLoaded ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Loading parameters&hellip;
        </Typography>
      ) : (
        <>
          {activity === null ? (
            <Banner severity="warning">{`Unknown activity ${activityType} — parameters shown as stored.`}</Banner>
          ) : (
            <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
              Set by the control plane — read-only.
            </Typography>
          )}
          <StoredParams params={params} properties={activity?.params?.properties ?? {}} />
        </>
      )}
    </Box>
  )
}

/* WHAT IS STORED, READ AS VALUES — rows come from the STORED object, not the schema: a
 * key the schema no longer declares still shows (it is live in the runtime); a non-scalar is stringified. */
function StoredParams({
  params,
  properties,
}: {
  params: Record<string, unknown>
  properties: Record<string, ParamsProperty>
}) {
  const names = Object.keys(params)
  if (names.length === 0) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        No parameters.
      </Typography>
    )
  }
  return (
    <Stack spacing={0.75} sx={{ mt: 1 }} data-testid="cron-params-readonly">
      {names.map(name => (
        <Box key={name} sx={{ minWidth: 0 }}>
          {/* THE LABEL RENDERS AT EVERY WIDTH: `ScheduleSummary`'s caption, not `ui/CellLabel` (which is
              `display: none` above the phone breakpoint and labelled nothing at 1280px). */}
          <Typography
            variant="caption"
            component="span"
            sx={{ display: 'block', color: 'text.secondary' }}
          >
            {properties[name]?.title ?? name}
          </Typography>
          <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }} data-testid={`cron-param-value-${name}`}>
            {displayValue(params[name])}
          </Typography>
        </Box>
      ))}
    </Stack>
  )
}

/** A stored value as one line: `''`/`null` read as an em-dash; anything else non-scalar as JSON. */
function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'string' || typeof value === 'number') return String(value)
  return JSON.stringify(value)
}
