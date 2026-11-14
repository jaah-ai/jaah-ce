import { Fragment, useEffect, useRef, type JSX, type KeyboardEvent } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'
import { FORGE_LABELS, type OrgRef } from '../../types'
import Banner from '../../ui/Banner'
import { credentialMark } from '../../ui/credentialMarks'
import type { GitRepo } from '../../ui/RepoUrlLookup'
import { NewRepoChip, PrimaryChip } from '../../ui/MarkChip'
import { tapTargetSx } from '../../ui/listStyles'
import Tip from '../../ui/Tip'
import { danger, useConfirm } from '../ConfirmDialog'
import {
  FLIP_LOCKED_REASON, LAST_REPO_REMOVED, PRIMARY_BLOCKED_REASON, RepoRowFields, useRepoEditing,
} from './repoEditing'
import { repoForge, repoName, type RepoDraft, type RepoPolicySeed, type RepoRowErrors } from './projectForm'

/* THE REPOSITORY LINE (the edit page, and the create wizard's Repository step through `ProjectRepoFields`):
 * "Repository: acme/web, acme/api · Add", each name a button that opens that repository's inline editor in place (not a dialog): its URL lookup and integration branch (the one place a
 * repository's integration branch is edited), Set primary, and a ✕ that removes it. The editing rules are
 * `useRepoEditing`'s; every change joins the form's draft. Primary is marked only
 * while there are two or more. A Jaah-managed project's repository reads "{name} at Jaah"; Transfer (today's flow:
 * pick your own repository, Save asks what happens to the Jaah one) is offered to a viewer who may change it. */

const LINE_SX = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 0.5, rowGap: 0.5, minWidth: 0,
  fontSize: FONT_SIZE.base,
} as const
const NAME_SX = { display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0, maxWidth: '100%' } as const
const LINK_SX = { overflowWrap: 'anywhere', textAlign: 'left', fontSize: 'inherit', verticalAlign: 'baseline' } as const
const MARK_SX = { display: 'inline-flex', flexShrink: 0, color: 'text.secondary', '& svg, & > *': { width: SIZE.iconSm, height: SIZE.iconSm } } as const
const EDITOR_SX = {
  mt: 1, p: 1.5, border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.sm, minWidth: 0, maxWidth: grid(117),
} as const

export default function ProjectRepoLine({
  repos, onChange, errors, onErrorsChange, primaryBranchLocked, storedBranch, flipLocked = false, initEmpty = false,
  canCreate = false, org = null, storedHasRepo = false, onBranchPolicy, projectPolicy, readOnly = false, atJaah = false,
  onTransfer, onPick, onNewName,
}: {
  repos: RepoDraft[]
  onChange: (next: RepoDraft[]) => void
  errors: RepoRowErrors
  onErrorsChange: (next: RepoRowErrors) => void
  primaryBranchLocked: boolean
  storedBranch: string
  flipLocked?: boolean
  initEmpty?: boolean
  canCreate?: boolean
  org?: OrgRef | null
  /** The saved project has a repository: an empty line then means Save removes it, so it warns. */
  storedHasRepo?: boolean
  onBranchPolicy?: (row: RepoDraft) => void
  projectPolicy?: RepoPolicySeed
  /** Names only (a Jaah-managed project's member): nothing opens. */
  readOnly?: boolean
  /** A Jaah-managed project: each name reads "{name} at Jaah". */
  atJaah?: boolean
  /** Offered with `atJaah`: the viewer may put the project on a repository of their own. */
  onTransfer?: () => void
  /** A listed repository was picked for this row (the wizard names the project after the primary). */
  onPick?: (repo: GitRepo, row: RepoDraft) => void
  /** A new repository's name was typed for this row. */
  onNewName?: (name: string, row: RepoDraft) => void
}): JSX.Element {
  const editing = useRepoEditing({
    repos, onChange, errors, onErrorsChange, primaryBranchLocked, storedBranch, flipLocked, projectPolicy,
  })
  const { isOpen, update, done, cancel, remove, makePrimary, primaryBlocked } = editing
  const rootRef = useRef<HTMLDivElement>(null)
  const [confirm, confirmDialog] = useConfirm()
  /* A "New" row an earlier, failed Save already made on the forge (`prepareRepos` kept its URL): removing it here
     leaves that repository on the forge, so it asks first. */
  const removeRow = async (row: RepoDraft) => {
    if (row.create && row.repo_url.trim() !== '') {
      const yes = await confirm(danger('Remove', {
        title: `Remove ${repoName(row)}?`,
        message: 'It was already created on the forge. Removing it here keeps it there; delete it on the forge if unwanted.',
        cancelLabel: 'Keep',
      }))
      if (!yes) return
    }
    remove(row)
    focusNext.current = 'project-repo-add'
  }
  /* Where focus goes next: an opened editor's URL, or a closed one's name. Applied once the render shows it. */
  const focusNext = useRef<string | null>(null)
  useEffect(() => {
    const id = focusNext.current
    if (id === null) return
    // One attempt per request: a target not shown (Add hidden while a blank row is open) falls back to the first name.
    focusNext.current = null
    const root = rootRef.current
    const el = root?.querySelector<HTMLElement>(`#${CSS.escape(id)}`)
      ?? root?.querySelector<HTMLElement>('[data-testid="project-repo-link"], #project-repo-add')
    el?.focus()
  })
  const linkId = (row: RepoDraft) => `project-repo-link-${row.key}`
  const urlId = (index: number) => `project-repo-${index}-url`

  const openOrClose = (row: RepoDraft, index: number) => {
    if (isOpen(row)) {
      if (done(row)) focusNext.current = linkId(row)
      return
    }
    editing.openRow(row)
    focusNext.current = urlId(index)
  }
  const add = () => {
    editing.add()
    focusNext.current = urlId(repos.length)
  }
  const transfer = () => {
    onTransfer?.()
    editing.startOver()
    focusNext.current = urlId(0)
  }

  const several = repos.length > 1
  const openRows = repos.map((row, index) => ({ row, index })).filter(({ row }) => isOpen(row))
  const showAdd = !readOnly && !editing.adding && !editing.addLocked

  return (
    <Box ref={rootRef} data-testid="project-repo-line-root" sx={{ minWidth: 0 }}>
      <Box data-testid="project-repo-line" sx={LINE_SX}>
        <Typography component="span" sx={{ fontWeight: FONT_WEIGHT.semibold, fontSize: 'inherit', mr: 0.5 }}>
          Repository:
        </Typography>
        {repos.length === 0 && <Typography component="span" sx={{ fontSize: 'inherit' }}>None</Typography>}
        {repos.map((row, i) => {
          const name = repoName(row) || 'New repository'
          const forge = repoForge(row)
          const open = isOpen(row)
          return (
            <Fragment key={row.key}>
              <Box component="span" sx={NAME_SX} data-testid="project-repo-item" data-primary={row.is_primary || undefined}>
                {forge !== null && (
                  <Tip title={FORGE_LABELS[forge]}>
                    <Box component="span" role="img" aria-label={FORGE_LABELS[forge]} sx={MARK_SX}>
                      {credentialMark(forge, FORGE_LABELS[forge])}
                    </Box>
                  </Tip>
                )}
                {readOnly
                  ? <Box component="span" data-testid="project-repo-link" sx={{ overflowWrap: 'anywhere' }}>{name}</Box>
                  : (
                    <Link
                      component="button"
                      type="button"
                      id={linkId(row)}
                      underline="hover"
                      aria-expanded={open}
                      aria-controls={open ? `project-repo-editor-${row.key}` : undefined}
                      onClick={() => openOrClose(row, i)}
                      data-testid="project-repo-link"
                      sx={LINK_SX}
                    >
                      {name}
                    </Link>
                  )}
                {atJaah && <Box component="span" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }} data-testid="project-repo-at-jaah">at Jaah</Box>}
                {several && row.is_primary && <Box component="span" sx={{ display: 'inline-flex' }} data-testid="project-repo-mark"><PrimaryChip /></Box>}
                {row.create && <Box component="span" sx={{ display: 'inline-flex' }}><NewRepoChip /></Box>}
                {i < repos.length - 1 && <Box component="span" aria-hidden="true" sx={{ ml: -0.5 }}>,</Box>}
              </Box>
            </Fragment>
          )
        })}
        {showAdd && (
          <>
            {repos.length > 0 && <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary' }}>·</Box>}
            <Link component="button" type="button" underline="hover" onClick={add} id="project-repo-add"
              data-testid="project-repo-add" sx={LINK_SX}>
              Add
            </Link>
          </>
        )}
        {atJaah && onTransfer && (
          <Button type="button" variant="outlined" size="small" onClick={transfer} data-testid="project-transfer-repo"
            sx={[tapTargetSx, { ml: 1 }]}>
            Transfer
          </Button>
        )}
      </Box>

      {/* A refusal on a closed row (which row is primary) reads under the line, naming the repository. */}
      {repos.filter(r => !isOpen(r) && errors[r.key]?.row).map(r => (
        <Box key={r.key} role="alert" data-testid="project-repo-error" sx={{ color: 'error.main', fontSize: FONT_SIZE.sm, mt: 0.5 }}>
          {repoName(r)}: {errors[r.key]?.row}
        </Box>
      ))}
      {repos.length === 0 && storedHasRepo && (
        <Box sx={{ mt: 1 }}><Banner severity="warning" role="status" testId="project-repos-last-removed">{LAST_REPO_REMOVED}</Banner></Box>
      )}

      {openRows.map(({ row, index }) => {
        const name = repoName(row) || 'new repository'
        const close = (how: 'done' | 'cancel') => {
          if (how === 'cancel') cancel(row)
          else if (!done(row)) return
          focusNext.current = row.id !== undefined || row.repo_url ? linkId(row) : 'project-repo-add'
        }
        const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
          // A lookup's own Escape (closing its list) marks the event handled.
          if (e.key !== 'Escape' || e.defaultPrevented) return
          e.preventDefault()
          close('cancel')
        }
        return (
          <Box
            key={row.key}
            id={`project-repo-editor-${row.key}`}
            role="group"
            aria-label={`Repository ${name}`}
            data-testid="project-repo-editor"
            onKeyDown={onKeyDown}
            sx={EDITOR_SX}
          >
            <RepoRowFields
              row={row}
              index={index}
              error={errors[row.key]}
              branchLocked={row.is_primary && primaryBranchLocked}
              update={(next, clears) => update(row.key, next, clears)}
              onPick={picked => onPick?.(picked, row)}
              onNewName={name => onNewName?.(name, row)}
              initEmpty={initEmpty}
              canCreate={canCreate}
              org={org}
              onBranchPolicy={onBranchPolicy && (() => onBranchPolicy(row))}
            />
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, mt: 1 }}>
              <Button type="button" variant="outlined" size="small" onClick={() => close('done')} sx={tapTargetSx}
                data-testid="project-repo-done">
                Done
              </Button>
              <Button type="button" variant="text" size="small" onClick={() => close('cancel')} sx={tapTargetSx}>
                Cancel
              </Button>
              {several && !row.is_primary && (
                <Tip title={primaryBlocked(row) ? `Can't: ${PRIMARY_BLOCKED_REASON}` : undefined}>
                  <span>
                    <Button type="button" variant="text" size="small" onClick={() => makePrimary(row)}
                      disabled={primaryBlocked(row)} data-testid="project-repo-set-primary" sx={tapTargetSx}>
                      Set primary
                    </Button>
                  </span>
                </Tip>
              )}
              <Box sx={{ flex: 1 }} />
              <Tip title={editing.removeLocked ? `Can't: ${FLIP_LOCKED_REASON}` : `Remove ${name}`}>
                <span>
                  <IconButton
                    type="button"
                    aria-label={`Remove ${name}`}
                    onClick={() => void removeRow(row)}
                    disabled={editing.removeLocked}
                    data-testid="project-repo-remove"
                    sx={{ ...tapTargetSx, color: 'error.main' }}
                  >
                    <Box component="span" aria-hidden="true" sx={{ fontSize: FONT_SIZE.lg, lineHeight: LINE_HEIGHT.tight }}>✕</Box>
                  </IconButton>
                </span>
              </Tip>
            </Box>
          </Box>
        )
      })}
      {confirmDialog}
    </Box>
  )
}
