import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type JSX, type ReactNode, type RefObject } from 'react'
import { useClientPage } from '../../hooks/usePageClamp'
import { credentialMark } from '../../ui/credentialMarks'
import { safeHref } from '../../lib/safeHref'
import { FORGE_LABELS, type Forge, type OrgRef } from '../../types'
import { DataTable } from '../../ui/DataTable'
import { DetailCell, DetailSection } from '../../ui/detail/DetailPage'
import { EmptyState } from '../../ui/EmptyState'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import { NewRepoChip, PrimaryChip } from '../../ui/MarkChip'
import { PageNav } from '../../ui/Pager'
import RepoUrlLookup, { type GitRepo, loadRepoState } from '../../ui/RepoUrlLookup'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'
import { editItem, RowActionsOverflow, type OverflowItem } from '../tasks/rowActions'
import NewRepoFields from './NewRepoFields'
import {
  blankRepoFields, forgePickerShown, isEmptyRepo, NEW_REPO_BRANCH, newRepoDraft, PICK_ANOTHER_PRIMARY, REPO_FIELD_ERRORS, repoForge,
  repoName, repoWebUrl, withPrimary,
  type RepoDraft, type RepoRowError, type RepoRowErrors,
} from './projectForm'

/* THE REPOSITORIES TABLE — a project's connected repositories, one row each: `owner/name` linking to
 * its forge page, the forge, the integration branch, and the Primary mark (New on one Create or Save makes). Shared by the edit page's General tab
 * and the create wizard's Repository step (both through `ProjectRepoFields`). It edits the draft only; the page's
 * Save sends the whole list (`projectForm.ts::buildBody`).
 *
 * A row opens in place to edit its URL, branch and — for a host no forge is known for — the forge picker. Errors
 * are the caller's (`errors`, keyed by `RepoDraft.key`): the form's own blank check and the server's per-row 422
 * (`repoRowErrors`) land on the row they name, never only in a banner.
 *
 * Ten per page (`useClientPage`, as `components/access/MembersTable.tsx`); no pager up to ten. At the phone width
 * the name wraps rather than ellipsising (its tail is what tells two repos apart), after a `/` or `-` and never
 * inside a word, which only a word too long for any phone may (`NameSegments`). Below `sm` the forge shrinks to its
 * monogram, and at `PHONE` its column goes: its ~70px is what kept the name to a letter or two per line beside the
 * mark, and the link opens the forge anyway. The Primary mark sits on the name's FIRST line:
 * the name wraps in what the mark leaves, the mark never shrinks or wraps, and it follows the first line's last
 * character, not the cell's far edge (`useWidestLine`). In a table under 400px above the phone (the side
 * pane at a 1024px window) a name too long to share its line goes on alone, the mark under it. */

export const REPOS_PAGE_SIZE = 10

const WRAP_SX = { overflowWrap: 'anywhere', wordBreak: 'normal', minWidth: 0 } as const
/* `break-word`, not `anywhere`: it leaves a word's whole width in the column's minimum, so the table gives the
   name room for "acme/" rather than breaking it a letter at a time beside the mark. */
const NAME_WRAP_SX = { overflowWrap: 'break-word', wordBreak: 'normal', minWidth: 0 } as const
/* A segment with a word (between hyphens, where the browser breaks too) longer than this may break anywhere: kept
   whole, it would widen the table past a phone. */
const LONG_WORD = 20
/* Sized to its label (`1%` + no wrap), not a fixed 110px: beside the side pane's ~450px table those pixels were the
   name's, which wrapped to four lines beside the mark. */
const FORGE_COLUMN_SX = { width: { xs: 40, sm: '1%' }, whiteSpace: 'nowrap', [PHONE]: { display: 'none' } } as const
/* A NARROW TABLE ABOVE THE PHONE (the side pane's, ~450px at a 1366px window): gated on the table's own width, not
   the window's, the cells' gutters halve and Branch drops to 20% (still "develop" on one line), so a 32-character
   `owner/name` takes two lines beside the mark rather than four. The phone keeps its own layout. */
const REPOS_CONTAINER = 'project-repos'
const NARROW_TABLE_SX = {
  containerType: 'inline-size',
  containerName: REPOS_CONTAINER,
  '@media (min-width:481px)': {
    [`@container ${REPOS_CONTAINER} (max-width:599.95px)`]: {
      '& th, & td': { px: 1 },
      '& [data-col="branch"]': { width: '20%' },
    },
    /* Narrower still (the pane at a 1024px window, ~320px): Forge goes, as on the phone, and Branch keeps its 24%,
       which 20% of that would cut to "develo p". */
    [`@container ${REPOS_CONTAINER} (max-width:399.95px)`]: {
      '& [data-col="forge"]': { display: 'none' },
      '& [data-col="branch"]': { width: '24%' },
      /* The mark wraps under the name only when the whole name does not fit beside it: a short `owner/acme` keeps
         it on its line. */
      '& [data-name-line]': { flexWrap: 'wrap', rowGap: 0.25 },
    },
  },
} as const

/* `owner/name` with a break opportunity after each `/`. */
function NameSegments({ name }: { name: string }): JSX.Element {
  return (
    <>
      {name.split('/').map((segment, i, all) => (
        <Fragment key={i}>
          {segment.split('-').some(word => word.length > LONG_WORD)
            ? <Box component="span" sx={WRAP_SX}>{segment}</Box>
            : segment}
          {i < all.length - 1 && <>/<wbr /></>}
        </Fragment>
      ))}
    </>
  )
}

const NAME_LINE_SX = { display: 'flex', alignItems: 'flex-start', gap: 0.5, minWidth: 0 } as const

/* A wrapped name's box spans the whole room it was given, so the mark after it sat at the cell's far edge, a gap
   after a shorter first line. Cap the box at its widest line (a cap, not a width: the column may still shrink). */
function useWidestLine(ref: RefObject<HTMLElement | null>, text: string): void {
  useLayoutEffect(() => {
    const el = ref.current
    const room = el?.parentElement
    if (!el || !room || typeof ResizeObserver === 'undefined') return
    const fit = () => {
      el.style.maxWidth = ''
      const range = document.createRange()
      range.selectNodeContents(el)
      const rects = [...range.getClientRects()]
      if (rects.length === 0) return
      const width = Math.max(...rects.map(r => r.right)) - Math.min(...rects.map(r => r.left))
      el.style.maxWidth = `${Math.ceil(width)}px`
    }
    fit()
    // A web font swapping in re-wraps the text without resizing the room.
    void document.fonts?.ready.then(fit)
    const observer = new ResizeObserver(fit)
    observer.observe(room)
    return () => observer.disconnect()
  }, [ref, text])
}

/* The name, linking to its forge page when it has one. */
function RepoName({ name, href }: { name: string, href: string | undefined }): JSX.Element {
  const ref = useRef<HTMLElement>(null)
  useWidestLine(ref, name)
  return href
    ? <Link ref={ref as RefObject<HTMLAnchorElement>} href={href} target="_blank" rel="noopener noreferrer"
        sx={NAME_WRAP_SX} data-testid="project-repo-link"><NameSegments name={name} /></Link>
    : <Box ref={ref} component="span" sx={NAME_WRAP_SX} data-testid="project-repo-link"><NameSegments name={name} /></Box>
}
const MARK_SX = { flexShrink: 0, whiteSpace: 'nowrap', display: 'inline-flex' } as const
/* Add stays one line: squeezed beside the heading it moves under it (`DetailSection` wraps) rather than folding in two. */
const NO_WRAP_SX = { whiteSpace: 'nowrap', flexShrink: 0 } as const
/* On a phone the branch takes its own width up to a cap (past it, it wraps) so the name gets the rest of the row;
 * a fixed share would hold a short `develop` in room the name needs. */
const BRANCH_SX = { width: { xs: 'max-content', sm: 'auto' }, maxWidth: { xs: '12ch', sm: 'none' } } as const
/* Phone gutters beside the branch: the default 16px a side would take the name's room. */
const TIGHT_SX = { px: { xs: 1, sm: 2 } } as const

function ForgeCell({ forge }: { forge: Forge | null }): JSX.Element {
  if (forge === null) return <Box component="span" title="Not recognised" sx={{ color: 'text.secondary' }}>—</Box>
  const label = FORGE_LABELS[forge]
  return (
    <>
      <Box component="span" sx={{ display: { xs: 'none', sm: 'inline' } }}>{label}</Box>
      <Box
        component="span"
        role="img"
        aria-label={label}
        title={label}
        sx={{
          display: { xs: 'inline-flex', sm: 'none' }, alignItems: 'center', color: 'text.secondary',
        }}
      >
        {/* Below `sm` the name would take the repository's width: the console's one forge mark instead
            (`ui/credentialMarks.tsx`, a monogram where none is drawn). */}
        {credentialMark(forge, label)}
      </Box>
    </>
  )
}

/* Why the first repository cannot be connected, or the last removed, by a viewer without the assignment gate. */
const FLIP_LOCKED_REASON = 'only an admin who can create and delete projects can do this'

export default function ProjectReposTable({
  repos,
  onChange,
  errors,
  onErrorsChange,
  primaryBranchLocked,
  storedBranch,
  readOnly = false,
  flipLocked = false,
  onPick,
  initEmpty = false,
  canCreate = false,
  onNewName,
  org = null,
}: {
  repos: RepoDraft[]
  onChange: (next: RepoDraft[]) => void
  errors: RepoRowErrors
  onErrorsChange: (next: RepoRowErrors) => void
  /* The primary's branch is the project's integration branch, a governance field: a viewer who may not author
     governance keeps it as stored, and may not make primary a row whose branch differs. */
  primaryBranchLocked: boolean
  /* The project's stored integration branch — what the server compares the primary's with
     (`_repos_author_the_branch`): a locked viewer's new first row starts on it, and Make primary needs it. */
  storedBranch: string
  /** Another org's project: shown, never changed. */
  readOnly?: boolean
  /** The viewer may not connect the first repository or remove the last: either re-places the project's slots
      (`routes/projects.py::require_assignment_writer`), so it is refused here, not as a 403 on Save. */
  flipLocked?: boolean
  /** A listed repository was picked for this row (the wizard names the project after the primary). */
  onPick?: (repo: GitRepo, row: RepoDraft) => void
  /** Create initializes an empty repository (`RepoRowFields.initEmpty`). */
  initEmpty?: boolean
  /** A new row may be a repository Create or Save makes (`RepoRowFields.canCreate`). */
  canCreate?: boolean
  /** A new repository's name was typed for this row (the wizard names the project after the primary). */
  onNewName?: (name: string, row: RepoDraft) => void
  /** Another org's project: its forge lookups go as that org (`RepoRowFields.org`). */
  org?: OrgRef | null
}): JSX.Element {
  /* Rows open for editing, each with its value before it opened (null: added in this edit, so Cancel removes it). */
  /* A row not saved and still blank (the wizard's first row) starts open: there is nothing to show yet. */
  const [open, setOpen] = useState<Record<string, RepoDraft | null>>(() => Object.fromEntries(
    readOnly ? [] : repos.filter(r => r.id === undefined && r.repo_url === '' && !r.create).map(r => [r.key, null]),
  ))
  const { current, setPage, totalPages, start, pageItems: pageRows, label } = useClientPage(repos, REPOS_PAGE_SIZE)

  /* A refusal on a row of another page turns to it: an error nobody can see is no error message. Only for errors
     that arrive from outside (a save): clearing one while typing, or this table's own refusals, never pages. */
  const ownErrors = useRef<RepoRowErrors | null>(null)
  useEffect(() => {
    if (errors === ownErrors.current) return
    const index = repos.findIndex(r => errors[r.key])
    if (index !== -1 && (index < start || index >= start + REPOS_PAGE_SIZE)) setPage(Math.floor(index / REPOS_PAGE_SIZE) + 1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [errors])
  const setErrors = (next: RepoRowErrors) => {
    ownErrors.current = next
    onErrorsChange(next)
  }

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

  function add() {
    const row = newRepoDraft(repos.length === 0)
    // A first row becomes primary: for a locked viewer it must keep the stored branch, or the save is a 403.
    if (row.is_primary && primaryBranchLocked) row.integration_branch = storedBranch
    onChange([...repos, row])
    setOpen(o => ({ ...o, [row.key]: null }))
    setPage(Math.ceil((repos.length + 1) / REPOS_PAGE_SIZE))
  }

  function done(row: RepoDraft) {
    const blank = blankRepoFields([row])[row.key]
    if (blank) {
      setErrors({ ...errors, [row.key]: { ...errors[row.key], ...blank } })
      return
    }
    // The fields are filled: a server refusal on them is the next save's to answer, not a reason to stay open.
    setErrors(withoutError(row.key))
    closeRow(row.key)
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
      if (REPO_FIELD_ERRORS.some(f => fields[f])) next[key] = fields
      else delete next[key]
    }
    setErrors(next)
  }

  function actions(row: RepoDraft, name: string): OverflowItem[] {
    const items: OverflowItem[] = [editItem(name, () => setOpen(o => ({ ...o, [row.key]: row })))]
    if (!row.is_primary) {
      const branchChanges = primaryBranchLocked && row.integration_branch.trim() !== storedBranch.trim()
      items.push({
        key: 'primary', icon: '★', label: 'Make primary', ariaLabel: `Make ${name} primary`,
        onClick: () => makePrimary(row),
        ...(branchChanges
          ? { disabled: true, disabledReason: "its branch differs from the integration branch, which only someone who can change Governance can change" }
          : {}),
      })
    }
    items.push({
      key: 'remove', icon: '✕', label: 'Remove', ariaLabel: `Remove ${name}`, danger: true, onClick: () => remove(row),
      ...(flipLocked && repos.length === 1 ? { disabled: true, disabledReason: FLIP_LOCKED_REASON } : {}),
    })
    return items
  }

  function editRow(row: RepoDraft, index: number): JSX.Element {
    const e = errors[row.key] ?? {}
    return (
      <TableRow key={row.key} data-testid="project-repo-row" data-editing="true" data-primary={row.is_primary || undefined}>
        <TableCell colSpan={4} sx={{ py: 1.5 }}>
          <RepoRowFields
            row={row}
            index={index}
            error={e}
            branchLocked={row.is_primary && primaryBranchLocked}
            update={(next, clears) => update(row.key, next, clears)}
            onPick={picked => onPick?.(picked, row)}
            initEmpty={initEmpty}
            canCreate={canCreate}
            onNewName={name => onNewName?.(name, row)}
            org={org}
          />
          <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
            <Button type="button" variant="outlined" size="small" onClick={() => done(row)} sx={tapTargetSx}
              data-testid="project-repo-done">
              Done
            </Button>
            <Button type="button" variant="text" size="small" onClick={() => cancel(row)} sx={tapTargetSx}>
              Cancel
            </Button>
          </Box>
        </TableCell>
      </TableRow>
    )
  }

  function shownRow(row: RepoDraft): JSX.Element {
    const name = repoName(row)
    const href = safeHref(repoWebUrl(row))
    const e = errors[row.key]
    return (
      <TableRow key={row.key} data-testid="project-repo-row" data-primary={row.is_primary || undefined}>
        <TableCell sx={{ ...NAME_WRAP_SX, pr: { xs: 1, sm: 2 } }}>
          <Box sx={NAME_LINE_SX} data-testid="project-repo-name-line" data-name-line>
            <RepoName name={name} href={href} />
            {row.is_primary && <Box component="span" sx={MARK_SX} data-testid="project-repo-mark"><PrimaryChip /></Box>}
            {row.create && <Box component="span" sx={MARK_SX}><NewRepoChip /></Box>}
          </Box>
          {e?.row && <RowError message={e.row} />}
        </TableCell>
        <TableCell sx={FORGE_COLUMN_SX} data-col="forge" data-testid="project-repo-forge"><ForgeCell forge={repoForge(row)} /></TableCell>
        <TableCell sx={{ ...WRAP_SX, ...TIGHT_SX, fontFamily: 'monospace', fontSize: '0.8125rem' }}
          data-testid="project-repo-branch">
          <Box sx={BRANCH_SX}>{row.integration_branch}</Box>
        </TableCell>
        <TableCell padding="none" sx={{ width: 44, textAlign: 'right' }}>
          {!readOnly && <RowActionsOverflow name={name} busy={false} items={actions(row, name)} sx={{}} />}
        </TableCell>
      </TableRow>
    )
  }

  const addLocked = flipLocked && repos.length === 0
  /* A new row still blank is the one being added: a second Add would only stack blanks. */
  const adding = repos.some(r => open[r.key] === null && r.repo_url === '')
  /* With every row on the page open there is no column to head: the header would only float over the fields. */
  const allOpen = !readOnly && pageRows.every(isOpen)
  const addButton = readOnly ? undefined : (
    <Button type="button" variant="outlined" size="small" onClick={add} sx={[tapTargetSx, NO_WRAP_SX]} id="project-repo-add"
      data-testid="project-repo-add" disabled={addLocked} title={addLocked ? FLIP_LOCKED_REASON : undefined}>
      Add repository
    </Button>
  )

  return (
    <DetailSection
      title="Repositories"
      help="Runners work in all of them. Gate, stack and ports run in the primary."
      headingLevel="h3"
      actions={repos.length > 0 && !adding ? addButton : undefined}
      testId="project-repos"
    >
      {repos.length === 0 ? (
        <EmptyState title="No repository connected" action={addButton} />
      ) : (
        <>
          <Box sx={NARROW_TABLE_SX}>
            <DataTable ariaLabel="Repositories" nested testId="project-repos-table">
              <TableHead sx={allOpen ? VISUALLY_HIDDEN : undefined}>
                <TableRow>
                  <TableCell sx={{ pr: { xs: 1, sm: 2 } }}>Repository</TableCell>
                  <TableCell sx={FORGE_COLUMN_SX} data-col="forge">Forge</TableCell>
                  <TableCell sx={{ ...TIGHT_SX, width: { xs: '1%', sm: '24%' } }} data-col="branch">Branch</TableCell>
                  <TableCell sx={{ width: 44 }}><Box component="span" sx={VISUALLY_HIDDEN}>Actions</Box></TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {pageRows.map((row, i) => (isOpen(row) && !readOnly ? editRow(row, start + i) : shownRow(row)))}
              </TableBody>
            </DataTable>
          </Box>
          <PageNav
            page={current}
            totalPages={totalPages}
            setPage={setPage}
            testId="project-repos-pager"
            label={label}
          />
        </>
      )}
    </DetailSection>
  )
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
  if (left.row || REPO_FIELD_ERRORS.some(f => left[f])) rest[key] = left
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
 * that Create or Save makes (`projectForm.ts::prepareRepos`). The table's open row and the create wizard's single
 * repository (`ProjectRepoFields`) both render it, so one repository or several read the same. `update` patches
 * the row and clears the named field errors; `actions` join the row's own link line. */
export function RepoRowFields({
  row, index, error, branchLocked, update, onPick, initEmpty = false, canCreate = false, onNewName, actions, org = null,
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
      <FieldRow align="start" rowGap="0.75rem">
        <DetailCell basis="18rem" max="30rem">
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
        {row.repo_url.trim() !== '' && <DetailCell basis="10rem" max="16rem">
          <FormTextField
            id={`${idBase}-branch`}
            label="Integration branch"
            help={branchLocked
              ? "The project's integration branch. Only someone who can change Governance can change it."
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
      </FieldRow>
      {forgePickerShown(row.repo_url) && (
        <FieldRow rowGap="0.75rem">
          <DetailCell basis="14rem" max="22rem">
            <FormTextField
              id={`${idBase}-forge`}
              label="Forge"
              help="Which forge API opens pull requests for this host."
              value={row.forge}
              onChange={v => update({ forge: v === 'gitlab' ? 'gitlab' : '' }, 'repo_url')}
              slotProps={{ inputLabel: { shrink: true } }}
              options={[{ value: '', label: 'Not set' }, { value: 'gitlab', label: 'GitLab (self-managed)' }]}
            />
          </DetailCell>
          {row.forge === 'gitlab' && (
            <DetailCell basis="18rem" max="30rem">
              <FormTextField
                id={`${idBase}-forge-base-url`}
                label="GitLab base URL"
                value={row.forge_base_url}
                onChange={v => update({ forge_base_url: v }, 'repo_url')}
                placeholder="https://example.org/gitlab"
                help="Only if GitLab is installed under a sub-path."
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
    <Box role="alert" data-testid="project-repo-error" sx={{ color: 'error.main', fontSize: '0.75rem', mt: 0.5 }}>
      {message}
    </Box>
  )
}
