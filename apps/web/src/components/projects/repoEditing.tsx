import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import { useEffect, useRef, useState, type JSX, type ReactNode } from 'react'
import { FONT_SIZE, grid, SIZE } from '../../theme'
import type { OrgRef } from '../../types'
import { DetailCell } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import { LockFilledIcon, ShieldFilledIcon, ShieldIcon } from '../../ui/icons'
import RepoUrlLookup, { type GitRepo, loadRepoState } from '../../ui/RepoUrlLookup'
import { tapTargetSx } from '../../ui/listStyles'
import { TONE_COLOR } from '../../ui/statusTone'
import NewRepoFields from './NewRepoFields'
import {
  blankRepoFields, branchRules, forgePickerShown, isEmptyRepo, NEW_REPO_BRANCH, newRepoDraft, PICK_ANOTHER_PRIMARY, REPO_FIELD_ERRORS, repoForge,
  repoName, withPrimary,
  type RepoDraft, type RepoPolicySeed, type RepoRowError, type RepoRowErrors,
} from './projectForm'
import Tip from '../../ui/Tip'

/* EDITING A PROJECT'S REPOSITORIES — the rules (`useRepoEditing`) and one repository's fields (`RepoRowFields`)
 * behind the Repository line (`ProjectRepoLine.tsx`: the edit page, and the create wizard's Repository step through
 * `ProjectRepoFields`). They edit the draft only; Create or Save sends the whole list (`projectForm.ts::buildBody`).
 *
 * Errors are the caller's (`errors`, keyed by `RepoDraft.key`): the form's own blank check and the server's per-row
 * 422 (`repoRowErrors`) land on the row they name, never only in a banner. */

/* THE MARK beside a repository's branch field (`RepoRowFields`, where the host opens a Branch policy: the edit page's
 * `ProjectRepoLine` editor): its Branch policy at a glance, and the way to it (Customize › Branches, that repository
 * picked). A filled lock (warning) for read only, a filled shield (info) for a prefix, an outlined muted shield with
 * none. The tip is the state; the accessible name adds whose it is. */
function BranchRulesButton({ row, name, onOpen }: { row: RepoDraft; name: string; onOpen: () => void }): JSX.Element {
  const rules = branchRules(row)
  const tip = rules === 'read_only' ? 'Read only' : rules === 'prefix' ? `Prefix ${row.branch_prefix.trim()}` : 'No branch rules'
  const Mark = rules === 'read_only' ? LockFilledIcon : rules === 'prefix' ? ShieldFilledIcon : ShieldIcon
  return (
    <Tip describeChild={false} title={tip}>
      <IconButton
        size="small"
        aria-label={`Branch policy for ${name}: ${tip}`}
        data-testid="project-repo-rules"
        data-rules={rules}
        onClick={onOpen}
        sx={{ p: 0.5, flexShrink: 0, color: TONE_COLOR[rules === 'read_only' ? 'warning' : rules === 'prefix' ? 'info' : 'neutral'] }}
      >
        <Mark size={SIZE.iconSm} />
      </IconButton>
    </Tip>
  )
}

/* Why the first repository cannot be connected, or the last removed, by a viewer without the assignment gate. */
export const FLIP_LOCKED_REASON = 'only an admin who can create and delete projects can do this'
/* Why a row cannot become primary for a viewer who may not author governance. */
export const PRIMARY_BLOCKED_REASON = 'its branch differs from the integration branch, which only an owner, or an admin with node-edit permission, can change'
/* Shown while every stored repository is removed: Save then turns the project into a folder project. */
export const LAST_REPO_REMOVED = 'Save leaves this project without a repository: tasks run in an empty folder.'

/* THE EDITING RULES of a project's repository list (the Repository line, `ProjectRepoLine.tsx`): which rows are
 * open (each with its value before it opened; null = added in this edit, so Cancel removes it), Add, Done, Cancel,
 * Remove and Make primary. It edits the draft only. */
export function useRepoEditing({
  repos, onChange, errors, onErrorsChange, primaryBranchLocked, storedBranch, flipLocked = false, projectPolicy,
}: {
  repos: RepoDraft[]
  onChange: (next: RepoDraft[]) => void
  errors: RepoRowErrors
  onErrorsChange: (next: RepoRowErrors) => void
  /** The viewer may not author governance: the primary's branch stays as stored, and a row whose branch differs
   *  cannot become primary. */
  primaryBranchLocked: boolean
  /** The project's stored integration branch: what a locked viewer's new first row starts on. */
  storedBranch: string
  /** The viewer lacks the assignment gate: no first repository, and the last one stays. */
  flipLocked?: boolean
  projectPolicy?: RepoPolicySeed
}) {
  /* A row not saved and still blank (the wizard's first row) starts open: there is nothing to show yet. */
  const [open, setOpen] = useState<Record<string, RepoDraft | null>>(() => Object.fromEntries(
    repos.filter(r => r.id === undefined && r.repo_url === '' && !r.create).map(r => [r.key, null]),
  ))
  const setErrors = onErrorsChange

  /* A row stays open while it has a field error to show. */
  const isOpen = (r: RepoDraft) => r.key in open || REPO_FIELD_ERRORS.some(f => errors[r.key]?.[f])

  const withoutError = (key: string, field?: RepoErrorFields): RepoRowErrors => {
    if (field !== undefined) return withoutFieldErrors(errors, key, field)
    if (!errors[key]) return errors
    const rest = { ...errors }
    delete rest[key]
    return rest
  }

  const update = (key: string, next: Partial<RepoDraft>, clears: RepoErrorFields) => {
    onChange(repos.map(r => (r.key === key ? { ...r, ...next } : r)))
    setErrors(withoutError(key, clears))
  }

  const closeRow = (key: string) => setOpen(o => {
    const rest = { ...o }
    delete rest[key]
    return rest
  })

  const openRow = (row: RepoDraft) => setOpen(o => ({ ...o, [row.key]: row }))

  function add(): RepoDraft {
    const row = newRepoDraft(repos.length === 0, repos.find(r => r.is_primary) ?? projectPolicy)
    // A first row becomes primary: for a locked viewer it must keep the stored branch, or the save is a 403.
    if (row.is_primary && primaryBranchLocked) row.integration_branch = storedBranch
    onChange([...repos, row])
    setOpen(o => ({ ...o, [row.key]: null }))
    return row
  }

  /** One new primary row in place of every row, open (a Jaah-managed project's Transfer). */
  function startOver(): RepoDraft {
    const row = newRepoDraft(true, repos.find(r => r.is_primary) ?? projectPolicy)
    if (primaryBranchLocked) row.integration_branch = storedBranch
    onChange([row])
    setOpen({ [row.key]: null })
    return row
  }

  /** Closes the row when its fields are filled; answers whether it closed. */
  function done(row: RepoDraft): boolean {
    const blank = blankRepoFields([row])[row.key]
    if (blank) {
      setErrors({ ...errors, [row.key]: { ...errors[row.key], ...blank } })
      return false
    }
    // The fields are filled: a server refusal on them is the next save's to answer, not a reason to stay open.
    setErrors(withoutError(row.key))
    closeRow(row.key)
    return true
  }

  function remove(row: RepoDraft) {
    // AS4: the primary goes last, or after another row takes it — the server refuses the same (`primary_removed`).
    if (row.is_primary && repos.length > 1) {
      setErrors({ ...errors, [row.key]: { ...errors[row.key], row: PICK_ANOTHER_PRIMARY } })
      return
    }
    onChange(withPrimary(repos.filter(r => r.key !== row.key)))
    setErrors(withoutError(row.key))
    closeRow(row.key)
  }

  function cancel(row: RepoDraft) {
    const before = open[row.key]
    if (before === null) {
      // Added in this edit (or the wizard's seeded row): Cancel takes it back out, and a primary passes on.
      onChange(withPrimary(repos.filter(r => r.key !== row.key)))
    } else if (before !== undefined) {
      // Which row is primary may have moved while this one was open: that is not this edit's to undo.
      onChange(repos.map(r => (r.key === row.key ? { ...before, is_primary: r.is_primary } : r)))
    } else if (row.saved) {
      // Opened by a server refusal, never by Edit: back to what is stored. A row never saved keeps its values.
      const { repo_url, integration_branch } = row.saved
      onChange(repos.map(r => (r.key === row.key ? { ...r, repo_url, integration_branch } : r)))
    }
    setErrors(withoutError(row.key))
    closeRow(row.key)
  }

  function makePrimary(row: RepoDraft) {
    onChange(repos.map(r => ({ ...r, is_primary: r.key === row.key })))
    // A row-level refusal is about which row is primary; that has just changed.
    const next = { ...errors }
    for (const key of Object.keys(next)) {
      const { row: _row, ...fields } = next[key]
      if (Object.values(fields).some(Boolean)) next[key] = fields
      else delete next[key]
    }
    setErrors(next)
  }

  return {
    isOpen, openRow, update, add, startOver, done, remove, cancel, makePrimary,
    /** A viewer who may not author governance may not make primary a row whose branch differs. */
    primaryBlocked: (row: RepoDraft) => primaryBranchLocked && row.integration_branch.trim() !== storedBranch.trim(),
    /** The last repository may not be removed by a viewer without the assignment gate. */
    removeLocked: flipLocked && repos.length === 1,
    addLocked: flipLocked && repos.length === 0,
    /* A new row still blank is the one being added: a second Add would only stack blanks. */
    adding: repos.some(r => open[r.key] === null && r.repo_url === ''),
  }
}

/** The field error(s) an edit clears. */
export type RepoErrorFields = keyof RepoRowError | (keyof RepoRowError)[]

/** `errors` without `key`'s `fields`; the row's entry goes once nothing is left on it. */
export function withoutFieldErrors(errors: RepoRowErrors, key: string, fields: RepoErrorFields): RepoRowErrors {
  const e = errors[key]
  const names = Array.isArray(fields) ? fields : [fields]
  if (!e || !names.some(f => e[f])) return errors
  const rest = { ...errors }
  const left = { ...e }
  for (const f of names) delete left[f]
  if (Object.values(left).some(Boolean)) rest[key] = left
  else delete rest[key]
  return rest
}

const ROW_ACTIONS_SX = { display: 'flex', flexWrap: 'wrap', columnGap: 2, mt: 0.5, ml: -1 } as const

/** A row action ("Create a new repository", "Use an existing repository", "Add another repository"): plain text, as
 *  the row's links. No hover fill: each swaps the row, so the next action lands under the resting pointer (or a
 *  phone's sticky hover) and a tinted box there read as a field. */
export const ROW_ACTION_SX = [
  tapTargetSx,
  { '&:hover': { backgroundColor: 'transparent', textDecoration: 'underline' } },
] as const

/* One repository's fields — URL, integration branch, and the forge picker for a host no forge is known for; or,
 * for a new row with `canCreate`, "Create a new repository" swaps them for an owner and a name (`NewRepoFields`)
 * that Create or Save makes (`projectForm.ts::prepareRepos`). The line's open editor and the create wizard's single
 * repository (`ProjectRepoFields`) both render it, so one repository or several read the same. `update` patches
 * the row and clears the named field errors; `actions` join the row's own link line. */
export function RepoRowFields({
  row, index, error, branchLocked, update, onPick, initEmpty = false, canCreate = false, onNewName, actions, org = null,
  onBranchPolicy, fill = false,
}: {
  row: RepoDraft
  index: number
  error: RepoRowErrors[string]
  /** The primary's branch, kept as stored for a viewer who may not author governance. */
  branchLocked: boolean
  update: (next: Partial<RepoDraft>, clears: RepoErrorFields) => void
  onPick?: (repo: GitRepo) => void
  /** Create or Save initializes an empty repository: say so under a URL found empty. */
  initEmpty?: boolean
  /** A row not saved yet may instead be a new repository (`RepoDraft.create`). */
  canCreate?: boolean
  onNewName?: (name: string) => void
  actions?: ReactNode
  /** Another org's project (an operator's): the repositories, owners and states offered are that org's
   *  forge connections', read as it. */
  org?: OrgRef | null
  /** Opens the row's Branch policy: a shield beside its branch (`BranchRulesButton`). */
  onBranchPolicy?: () => void
  /** The URL fills its form's column (the create wizard's one-column form) instead of the edit page's capped cell,
   *  whose right edge lines up with Name above it. */
  fill?: boolean
}): JSX.Element {
  const e = error ?? {}
  const idBase = `project-repo-${index}`
  /* A picked or typed URL fills the branch with the repo's default branch — only while it is still the new row's
     seed: a typed branch, a stored row's and a locked primary's are kept. A repository with no branch at all keeps
     the seed and is marked `empty` (`projectForm.ts::isEmptyRepo`). The answer lands on the latest row. */
  const latest = useRef({ row, update })
  useEffect(() => { latest.current = { row, update } })
  const asked = useRef('')
  // The URL the forge answered for through the person's connection: it is reachable, whatever the list says.
  const [answered, setAnswered] = useState('')
  const prefillBranch = (url: string) => {
    if (row.id !== undefined || url === asked.current) return
    asked.current = url
    void loadRepoState(url, org).then(({ default_branch: branch, empty }) => {
      const now = latest.current
      if (now.row.repo_url !== url) return
      if (branch !== null || empty !== null) setAnswered(url)
      // Initializing adds main and develop; the branch keeps the seed (develop) or what was typed.
      if (empty === true) {
        now.update({ empty: url }, 'repo_url')
        return
      }
      if (branch === null || branchLocked || now.row.integration_branch !== NEW_REPO_BRANCH) return
      now.update({ integration_branch: branch }, 'integration_branch')
    })
  }
  const emptyNote = initEmpty && isEmptyRepo(row)
    ? <Box component="span" data-testid={`${idBase}-empty`}>Empty repository: main and develop will be added.</Box>
    : undefined
  if (row.create) {
    const draft = row.create
    return (
      <Box data-testid={`${idBase}-new`}>
        <NewRepoFields
          idBase={idBase}
          draft={draft}
          error={e}
          org={org}
          onChange={next => {
            update({ create: next }, next.owner !== draft.owner || next.root !== draft.root ? 'owner' : 'name')
            onNewName?.(next.name.trim())
          }}
        />
        <Box sx={ROW_ACTIONS_SX}>
          <Button type="button" variant="text" size="small" data-testid={`${idBase}-existing`} sx={ROW_ACTION_SX}
            onClick={() => update({
              create: undefined,
              ...(draft.prior_branch !== undefined ? { integration_branch: draft.prior_branch } : {}),
            }, ['owner', 'name'])}>
            Use an existing repository
          </Button>
          {actions}
        </Box>
        {e.row && <RowError message={e.row} />}
      </Box>
    )
  }
  const createShown = canCreate && row.id === undefined && row.repo_url.trim() === ''
  return (
    <>
      <FieldRow align="start" rowGap={2}>
        <DetailCell basis={grid(48)} max={fill ? undefined : grid(80)}>
          <RepoUrlLookup
            id={`${idBase}-url`}
            label="Repo URL"
            help="Cloned into every slot; pull requests open against it."
            required
            requiredMark={false}
            value={row.repo_url}
            onChange={v => update({ repo_url: v }, 'repo_url')}
            onPick={picked => {
              update({
                repo_url: picked.url,
                forge: picked.forge === 'gitlab' && picked.root !== 'gitlab.com' ? 'gitlab' : '',
                forge_base_url: picked.base_url ?? '',
              }, 'repo_url')
              onPick?.(picked)
              prefillBranch(picked.url)
            }}
            onBlur={url => { if (repoForge({ ...row, repo_url: url }) !== null) prefillBranch(url) }}
            error={e.repo_url}
            warnUnlisted={answered !== row.repo_url && !isEmptyRepo(row)}
            hint={emptyNote}
            org={org}
          />
        </DetailCell>
        {/* No branch before a repository: the field appears once the URL has text. */}
        {row.repo_url.trim() !== '' && <DetailCell basis={grid(26.5)} max={grid(42.5)}>
          <FormTextField
            id={`${idBase}-branch`}
            label="Integration branch"
            help={branchLocked
              ? {
                tip: 'Pull requests open against it; locked for you.',
                more: "The primary's branch is the project's integration branch. Only an owner, or an admin with node-edit permission, can change it.",
                docs: 'console-reference/directory/projects',
              }
              : 'Pull requests open against it.'}
            required
            requiredMark={false}
            placeholder="e.g. main"
            value={row.integration_branch}
            onChange={v => update({ integration_branch: v }, 'integration_branch')}
            readOnly={branchLocked}
            error={e.integration_branch}
          />
        </DetailCell>}
        {/* Level with the input, not its help line: `pt` clears the outlined field's top inset. */}
        {onBranchPolicy && row.repo_url.trim() !== '' && (
          <Box sx={{ pt: 0.5, flexShrink: 0 }}>
            <BranchRulesButton row={row} name={repoName(row)} onOpen={onBranchPolicy} />
          </Box>
        )}
      </FieldRow>
      {forgePickerShown(row.repo_url) && (
        <FieldRow rowGap={2}>
          <DetailCell basis={grid(37)} max={grid(58.5)}>
            <FormTextField
              id={`${idBase}-forge`}
              label="Forge"
              help="Which forge API opens pull requests for this host."
              value={row.forge}
              onChange={v => update({ forge: v === 'gitlab' ? 'gitlab' : '' }, 'repo_url')}
              slotProps={{ inputLabel: { shrink: true } }}
              options={[
                { value: '', label: 'Not set', help: "Jaah doesn't know this host's forge." },
                { value: 'gitlab', label: 'GitLab (self-managed)', help: "Pull requests open through this host's GitLab API." },
              ]}
            />
          </DetailCell>
          {row.forge === 'gitlab' && (
            <DetailCell basis={grid(48)} max={grid(80)}>
              <FormTextField
                id={`${idBase}-forge-base-url`}
                label="GitLab base URL"
                value={row.forge_base_url}
                onChange={v => update({ forge_base_url: v }, 'repo_url')}
                placeholder="https://example.org/gitlab"
                help={{ tip: "GitLab's root address when installed under a sub-path.", more: 'Leave it empty when GitLab is at the host root.' }}
              />
            </DetailCell>
          )}
        </FieldRow>
      )}
      {(createShown || actions) && (
        <Box sx={ROW_ACTIONS_SX}>
          {createShown && (
            <Button type="button" variant="text" size="small" data-testid={`${idBase}-create`} sx={ROW_ACTION_SX}
              onClick={() => update({
                create: { root: '', owner: '', name: '', ...(branchLocked ? {} : { prior_branch: row.integration_branch }) },
                ...(branchLocked ? {} : { integration_branch: NEW_REPO_BRANCH }),
              }, ['repo_url', 'integration_branch'])}>
              Create a new repository
            </Button>
          )}
          {actions}
        </Box>
      )}
      {e.row && <RowError message={e.row} />}
    </>
  )
}

function RowError({ message }: { message: string }): JSX.Element {
  return (
    <Box role="alert" data-testid="project-repo-error" sx={{ color: 'error.main', fontSize: FONT_SIZE.sm, mt: 0.5 }}>
      {message}
    </Box>
  )
}
