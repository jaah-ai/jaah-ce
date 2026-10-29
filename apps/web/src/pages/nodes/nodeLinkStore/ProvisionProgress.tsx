import type { JSX } from 'react'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import Typography from '@mui/material/Typography'
import { CHECK_PATH, CLOSE_PATH, Icon } from '../../../ui/icons'
import { SR_ONLY_SX } from '../../../ui/listStyles'
import type { CloudProvider } from '../../../types'
import type { ProvisionStep, ProvisionStepId, ProvisionStepState } from './api'

/* A NodeLink's provisioning, one line: the steps its storage's cloud walks (EFS: file system → mount target →
 * access point → policy), each done, current, waiting or failed (`nodelinks.progress`, served on the detail), in
 * that cloud's words. The server runs every pass; this only shows where it is. Two layouts on `WizardSteps`'
 * breakpoint (MUI `sm`, 600px): a row of every label in full, and below it a vertical list, so no label is ever
 * cut at 360px. */

const LABELS: Record<CloudProvider, Partial<Record<ProvisionStepId, string>>> = {
  aws: { file_system: 'File system', mount_target: 'Mount target', access_point: 'Access point', policy: 'Policy' },
  gcp: { file_system: 'Filestore instance', mount_target: 'IP address', access_point: 'Folder' },
  azure: { file_system: 'Subnet access', access_point: 'File share' },
}

/** A step's label on `provider`'s storage; a step that cloud does not name keeps EFS's word. */
export const stepLabel = (step: ProvisionStepId, provider: CloudProvider | null | undefined) =>
  LABELS[provider ?? 'aws'][step] ?? LABELS.aws[step] ?? step

/* What a screen reader hears after the label; the marker says it visually. */
const SPOKEN: Record<ProvisionStepState, string> = {
  done: 'done',
  current: 'in progress',
  waiting: 'waiting',
  failed: 'failed',
}

const MARK = 18

function Glyph({ path }: { path: string }) {
  return (
    <Icon viewBox="0 0 24 24" size={14}>
      <path d={path} fill="currentColor" />
    </Icon>
  )
}

function Marker({ state }: { state: ProvisionStepState }) {
  if (state === 'current') return <CircularProgress size={MARK - 2} thickness={5} aria-hidden="true" sx={{ m: '1px' }} />
  const filled = state === 'done' || state === 'failed'
  return (
    <Box
      aria-hidden="true"
      sx={{
        width: MARK,
        height: MARK,
        flexShrink: 0,
        borderRadius: '50%',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: 'common.white',
        bgcolor: state === 'done' ? 'success.main' : state === 'failed' ? 'error.main' : 'transparent',
        border: filled ? 'none' : '2px solid',
        borderColor: 'divider',
      }}
    >
      {state === 'done' && <Glyph path={CHECK_PATH} />}
      {state === 'failed' && <Glyph path={CLOSE_PATH} />}
    </Box>
  )
}

export default function ProvisionProgress({
  steps,
  provider,
  testId = 'nodelink-progress',
}: {
  steps: readonly ProvisionStep[]
  /** The storage's cloud (`NodeLink.store_provider`); absent = AWS. */
  provider?: CloudProvider | null
  testId?: string
}): JSX.Element {
  return (
    <Box
      component="ol"
      aria-label="Provisioning"
      data-testid={testId}
      sx={{
        listStyle: 'none',
        m: 0,
        p: 0,
        display: 'flex',
        flexDirection: { xs: 'column', sm: 'row' },
        flexWrap: { sm: 'wrap' },
        alignItems: { xs: 'flex-start', sm: 'center' },
        gap: { xs: 1, sm: 1.5 },
      }}
    >
      {steps.map(({ step, state }, i) => (
        <Box
          component="li"
          key={step}
          data-testid={`${testId}-${step}`}
          data-state={state}
          aria-current={state === 'current' ? 'step' : undefined}
          sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}
        >
          <Marker state={state} />
          <Typography
            variant="body2"
            sx={{
              whiteSpace: 'nowrap',
              fontWeight: state === 'current' || state === 'failed' ? 600 : 400,
              color: state === 'waiting' ? 'text.secondary' : state === 'failed' ? 'error.main' : 'text.primary',
            }}
          >
            {stepLabel(step, provider)}
            <Box component="span" sx={SR_ONLY_SX}>{`: ${SPOKEN[state]}`}</Box>
          </Typography>
          {i < steps.length - 1 && (
            <Box aria-hidden="true" sx={{ display: { xs: 'none', sm: 'block' }, width: 24, height: 2, ml: 0.75, bgcolor: 'divider' }} />
          )}
        </Box>
      ))}
    </Box>
  )
}
