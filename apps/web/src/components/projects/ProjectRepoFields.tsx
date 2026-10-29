import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { type ReactNode, useEffect, useRef, useState } from 'react'
import type { OrgRef } from '../../types'
import FieldRow from '../../ui/FieldRow'
import { type GitRepo } from '../../ui/RepoUrlLookup'
import ProjectReposTable, { ROW_ACTION_SX, RepoRowFields, type RepoErrorFields, withoutFieldErrors } from './ProjectReposTable'
import { blankRepoFields, newRepoDraft, repoName, type ProjectFormState, type RepoDraft, type RepoRowErrors } from './projectForm'

/* The repository fields — the Repositories table (`ProjectReposTable`) — shared by the create wizard's Repository step
 * and the edit page's General tab. No row with a URL is a project with no repository (`projectForm.ts::formHasRepo`).
 * The props are where the two differ. `single` (the wizard): while there is one repository, its fields stand bare —
 * no table, no Done/Cancel (Next commits them) — and "Add another repository" opens the table. With `canCreate`,
 * any new row, the first or an added one, may be "Create a new repository" (`RepoRowFields`). */
export default function ProjectRepoFields({
  form, patch, repoErrors, onRepoErrorsChange, primaryBranchLocked, storedBranch,
  flipLocked = false, nameCell, nameFromRepo, single = false, initEmpty = false, canCreate = false, org = null,
}: {
  form: ProjectFormState
  patch: (next: Partial<ProjectFormState>) => void
  /** Per-row messages, keyed by `RepoDraft.key` (`projectForm.ts::repoRowErrors`, `blankRepoFields`). */
  repoErrors: RepoRowErrors
  onRepoErrorsChange: (next: RepoRowErrors) => void
  /** The viewer may not author governance, so the primary's branch (the integration branch) stays as stored. */
  primaryBranchLocked: boolean
  /** The project's stored integration branch (`ProjectReposTable.storedBranch`). */
  storedBranch: string
  /** The viewer may not connect the first repository or remove the last (`ProjectReposTable.flipLocked`). */
  flipLocked?: boolean
  /* A cell on its own row before the table (the edit page's Name). */
  nameCell?: ReactNode
  /* Picking a listed repo, or typing its URL, for the primary names the project after it (the create wizard) — while
     Name is empty, or still the name an earlier pick gave it; a typed Name is never replaced. */
  nameFromRepo?: boolean
  /* One repository shows as bare fields, not a table (the create wizard). */
  single?: boolean
  /* Create or Save initializes an empty repository (`RepoRowFields.initEmpty`). */
  initEmpty?: boolean
  /* A new row may be a repository Create or Save makes (`RepoDraft.create`). */
  canCreate?: boolean
  /* Another org's project: its forge lookups go as that org (`RepoRowFields.org`). */
  org?: OrgRef | null
}) {
  const autoName = useRef('')
  // "Add another repository" leaves with the bare fields: focus goes on to the new row's URL.
  const focusField = useFocusTarget(form.repos?.length)
  const nameAfter = (repoName: string) => {
    if (!nameFromRepo) return
    const named = form.name.trim()
    if (named !== '' && named !== autoName.current) return
    autoName.current = repoName
    patch({ name: repoName })
  }
  const pickRepo = (repo: GitRepo, row: RepoDraft) => {
    if (row.is_primary) nameAfter(repo.name)
  }
  const newName = (name: string, row: RepoDraft) => {
    if (row.is_primary) nameAfter(name)
  }
  const repos = form.repos
  if (single && repos !== null && repos.length === 1) {
    const row = repos[0]
    const update = (next: Partial<RepoDraft>, clears: RepoErrorFields) => {
      const updated = { ...row, ...next }
      patch({ repos: [updated] })
      // A typed URL names the project too, by its last path segment; a pick or a new repository names it below.
      if (next.repo_url !== undefined && !updated.create) newName(repoName(updated).split('/').pop() ?? '', updated)
      const rest = withoutFieldErrors(repoErrors, row.key, clears)
      if (rest !== repoErrors) onRepoErrorsChange(rest)
    }
    return (
      <Box data-testid="project-repo-single">
        {nameCell && <FieldRow rowGap="0.75rem">{nameCell}</FieldRow>}
        <RepoRowFields
          row={row}
          index={0}
          error={repoErrors[row.key]}
          branchLocked={row.is_primary && primaryBranchLocked}
          update={update}
          onPick={picked => pickRepo(picked, row)}
          initEmpty={initEmpty}
          canCreate={canCreate}
          onNewName={name => newName(name, row)}
          org={org}
          /* Only once the row is whole: the table opens a filled row closed, and a closed row must not hide a blank field. */
          actions={blankRepoFields([row])[row.key] === undefined && (
            <Button
              type="button"
              variant="text"
              size="small"
              id="project-repo-add"
              data-testid="project-repo-add"
              onClick={() => {
                patch({ repos: [...repos, newRepoDraft(false)] })
                focusField('project-repo-1-url')
              }}
              sx={ROW_ACTION_SX}
            >
              Add another repository
            </Button>
          )}
        />
      </Box>
    )
  }
  return (
    <>
      {nameCell && <FieldRow rowGap="0.75rem">{nameCell}</FieldRow>}
      {/* `mb`: the table ends flush, so the field after it (the edit page's Default agent) needs the form's row gap. */}
      {form.repos !== null && (
        <Box sx={{ mb: 1.5 }}>
          <ProjectReposTable
            repos={form.repos}
            onChange={repos => patch({ repos })}
            errors={repoErrors}
            onErrorsChange={onRepoErrorsChange}
            primaryBranchLocked={primaryBranchLocked}
            storedBranch={storedBranch}
            flipLocked={flipLocked}
            onPick={pickRepo}
            initEmpty={initEmpty}
            canCreate={canCreate}
            onNewName={newName}
            org={org}
          />
        </Box>
      )}
    </>
  )
}

/* Focus a refused field once it is actually shown. A hidden step/tab's controls cannot focus, and showing
 * one may commit a render later, so the request stays pending until the element renders (`offsetParent ===
 * null` is hidden); `shown` (the current step/tab) in the deps re-runs it then. Returns the requester. */
export function useFocusTarget(shown: unknown): (inputId: string) => void {
  const [focusTarget, setFocusTarget] = useState<string | null>(null)
  useEffect(() => {
    if (focusTarget === null) return
    const el = document.getElementById(focusTarget)
    if (el === null || el.offsetParent === null) return
    el.focus()
    setFocusTarget(null)
  }, [focusTarget, shown])
  return setFocusTarget
}
