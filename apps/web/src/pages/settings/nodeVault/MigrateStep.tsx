import { useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import EmptyState from '../../../ui/EmptyState'
import { useApiRead } from '../../../hooks/useApiRead'
import { useSubmit } from '../../../hooks/useSubmit'
import { withMember } from '../../../lib/sets'
import Banner from '../../../ui/Banner'
import LoadingState from '../../../ui/LoadingState'
import { chooseMigration, getMigrationCandidates } from './api'
import { NoteList } from './ResultStep'
import { FONT_WEIGHT } from '../../../theme'

/* The Migrate step (AC-4.7): every eligible credential still on KMS, Select all + one box each, and the
 * ineligible ones listed apart with the server's reason. Move sends the choice to the waiting setup
 * (`POST /api/nodevault/migrations`); Skip finishes without moving anything. While the plan is frozen only
 * Skip is offered (§3.5). Moving makes Jaah's server open each secret one last time to re-seal it (R-d).
 * In a rotation the same choice moves them straight onto the NEW key (AC-6.3): the operation's key. */

export default function MigrateStep({
  frozen,
  onChosen,
  rotation = false,
}: {
  frozen: boolean
  onChosen: () => void | Promise<void>
  /** A rotation's Migrate: the credentials go onto the new key. */
  rotation?: boolean
}): JSX.Element {
  // Untouched (`null`), every movable credential is selected; a re-read never resets a choice made.
  const [picked, setSelected] = useState<Set<string> | null>(null)
  const { data: candidates, error: loadError } = useApiRead(
    ['nodevault-migration-candidates'], getMigrationCandidates, 'Couldn’t load your credentials. Reload the page.',
  )
  const { busy, formError: error, run } = useSubmit()

  if (loadError) return <Banner severity="error">{loadError}</Banner>
  if (!candidates) return <LoadingState />

  const movable = candidates.eligible.filter(e => e.state === 'kms')
  const selected = picked ?? new Set(movable.map(m => m.id))
  const allSelected = movable.length > 0 && selected.size === movable.length
  const someSelected = selected.size > 0 && !allSelected

  const submit = (skip: boolean) => run('Couldn’t start the move. Try again.', async () => {
    // "All" lets the server record every ineligible credential as skipped with its reason (AC-4.8).
    await chooseMigration(skip ? { skip: true } : allSelected ? { all: true } : { credential_ids: [...selected] })
    // Awaited: the step stays busy until the parent's refetch unmounts it, so a second press cannot re-post.
    await onChosen()
  })

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }} data-testid="nodevault-migrate-step">
      {rotation && (
        <Typography variant="body2">
          These credentials aren’t in NodeVault yet. Move them straight onto the new key, or skip.
        </Typography>
      )}
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        Moving a credential means Jaah’s server opens it one last time to re-seal it.
      </Typography>
      {frozen && <Banner severity="warning">NodeVault isn’t included in your plan. You can only skip.</Banner>}

      {movable.length === 0 ? (
        <EmptyState inline title="No credentials to move." />
      ) : (
        <Box>
          <FormControlLabel
            control={
              <Checkbox
                checked={allSelected}
                indeterminate={someSelected}
                disabled={frozen}
                onChange={e => setSelected(e.target.checked ? new Set(movable.map(m => m.id)) : new Set())}
              />
            }
            label={<Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold }}>Select all ({movable.length})</Typography>}
          />
          <Box component="ul" sx={{ listStyle: 'none', m: 0, p: 0, pl: 3 }} data-testid="nodevault-eligible">
            {movable.map(c => (
              <li key={c.id}>
                <FormControlLabel
                  control={<Checkbox size="small" checked={selected.has(c.id)} disabled={frozen} onChange={e => setSelected(withMember(selected, c.id, e.target.checked))} />}
                  label={<Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{c.name}</Typography>}
                />
              </li>
            ))}
          </Box>
        </Box>
      )}

      <NoteList title={`Can’t move (${candidates.ineligible.length})`} testId="nodevault-ineligible"
        items={candidates.ineligible.map(c => ({ id: c.id, name: c.name, note: c.reason }))} />

      {error && <Banner severity="error">{error}</Banner>}
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        {movable.length > 0 && (
          <Button variant="contained" disabled={busy || frozen || selected.size === 0} onClick={() => void submit(false)}>
            Move {selected.size}
          </Button>
        )}
        <Button variant={movable.length > 0 ? 'text' : 'contained'} disabled={busy} onClick={() => void submit(true)}>
          {movable.length > 0 ? 'Skip' : 'Next'}
        </Button>
      </Box>
    </Box>
  )
}
