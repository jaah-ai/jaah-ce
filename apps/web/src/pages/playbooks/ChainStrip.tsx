import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { PERSONA_GROUPS } from '../../components/tasks/shared'
import { PHONE } from '../../ui/listStyles'
import { type StageIn, type Vocabulary, labelOf } from './api'

/* THE CHAIN STRIP — a playbook's stages drawn the way the epic view groups today's epics: Prepare → Build →
 * Finish, with the epic view's own group labels (`PERSONA_GROUPS`, read-only). Prepare is every stage before
 * the first stage that runs once per planned item, Build is those per-item stages drawn as a REPEATED group
 * (a stacked "× N" chip), Finish is everything after. A playbook with no per-item stage is one plain row.
 * Not the real task list: a sketch of the chain, redrawn on every edit. At phone width each group
 * becomes its own stacked row. */

const PER_ITEM = 'per_planned_item'

type Group = { key: 'prepare' | 'coder' | 'finish'; stages: StageIn[] }

/** The three groups (empty ones dropped), or null when no stage runs per planned item. */
export function chainGroups(stages: readonly StageIn[]): Group[] | null {
  const first = stages.findIndex(s => s.runs === PER_ITEM)
  if (first < 0) return null
  const rest = stages.slice(first)
  const end = rest.findIndex(s => s.runs !== PER_ITEM)
  const build = end < 0 ? rest : rest.slice(0, end)
  const groups: Group[] = [
    { key: 'prepare', stages: stages.slice(0, first) },
    { key: 'coder', stages: build },
    { key: 'finish', stages: end < 0 ? [] : rest.slice(end) },
  ]
  return groups.filter(g => g.stages.length > 0)
}

const groupLabel = (key: Group['key']) => PERSONA_GROUPS.find(g => g.key === key)?.label ?? ''

function expertOf(stage: StageIn): string {
  return stage.expert.kind === 'pinned' ? stage.expert.expert : stage.expert.default
}

function StageChip({ stage, vocab, repeated }: { stage: StageIn; vocab?: Vocabulary; repeated?: boolean }) {
  const label = stage.label.trim() || 'Untitled stage'
  return (
    <Box
      data-testid="chain-stage"
      data-repeated={repeated ? 'true' : 'false'}
      sx={{
        position: 'relative',
        minWidth: 0,
        maxWidth: '100%',
        px: 1,
        py: 0.5,
        border: '1px solid',
        borderColor: repeated ? 'primary.main' : 'divider',
        borderRadius: 1,
        bgcolor: 'background.paper',
        // The "stacked" look of a repeated group: two offset sheets behind the chip.
        ...(repeated && {
          boxShadow: (t: { palette: { divider: string } }) =>
            `3px 3px 0 -1px ${t.palette.divider}, 6px 6px 0 -2px ${t.palette.divider}`,
          mr: '6px',
          mb: '6px',
        }),
      }}
    >
      <Typography variant="body2" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>
        {label}
      </Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary', display: 'block', overflowWrap: 'anywhere' }}>
        {labelOf(vocab, 'expert', expertOf(stage))}
      </Typography>
    </Box>
  )
}

const Arrow = () => (
  <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary', alignSelf: 'center', flex: '0 0 auto' }}>
    →
  </Box>
)

function Row({ stages, vocab, repeated }: { stages: StageIn[]; vocab?: Vocabulary; repeated?: boolean }) {
  return (
    <Box
      component="ol"
      sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.75, minWidth: 0 }}
    >
      {stages.map((s, i) => (
        <Box component="li" key={`${s.key}-${i}`} sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
          {i > 0 && <Arrow />}
          <StageChip stage={s} vocab={vocab} repeated={repeated} />
        </Box>
      ))}
    </Box>
  )
}

export default function ChainStrip({ stages, vocab }: { stages: readonly StageIn[]; vocab?: Vocabulary }): JSX.Element {
  const groups = chainGroups(stages)
  return (
    <Box
      data-testid="chain-strip"
      aria-label="Stage chain"
      role="group"
      sx={{ p: 1.25, border: '1px dashed', borderColor: 'divider', borderRadius: 1, minWidth: 0 }}
    >
      {stages.length === 0 ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>No stages yet.</Typography>
      ) : groups === null ? (
        <Row stages={[...stages]} vocab={vocab} />
      ) : (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'stretch',
            gap: 1,
            flexWrap: 'wrap',
            [PHONE]: { flexDirection: 'column' },
          }}
        >
          {groups.map((g, i) => (
            <Box key={g.key} sx={{ display: 'flex', gap: 1, minWidth: 0, [PHONE]: { flexDirection: 'column' } }}>
              {i > 0 && (
                <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary', alignSelf: 'center', [PHONE]: { display: 'none' } }}>
                  →
                </Box>
              )}
              <Box
                data-testid="chain-group"
                data-group={g.key}
                sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}
              >
                <Typography
                  variant="caption"
                  component="h3"
                  sx={{ m: 0, color: 'text.secondary', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.04em' }}
                >
                  {groupLabel(g.key)}
                  {g.key === 'coder' && (
                    <Box
                      component="span"
                      data-testid="chain-repeat"
                      title="Once for each planned item"
                      sx={{ ml: 0.75, textTransform: 'none' }}
                    >
                      × N
                    </Box>
                  )}
                </Typography>
                <Row stages={g.stages} vocab={vocab} repeated={g.key === 'coder'} />
              </Box>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  )
}
