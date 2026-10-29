import { useEffect, type JSX } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { DetailCell } from '../../ui/detail/DetailPage'
import FieldRow from '../../ui/FieldRow'
import FormTextField from '../../ui/FormTextField'
import { get } from '../../api'
import { useAuth } from '../../auth'
import { actingAs, readAsOnce } from '../../lib/actAs'
import type { OrgRef } from '../../types'
import { type GitRepoSource } from '../../ui/RepoUrlLookup'
import { REPO_OWNERS_KEY } from './githubOAuthPopup'
import { newRepoNameError, type NewRepoDraft, type RepoRowError } from './projectForm'

/* A repository row's "Create a new repository" (`RepoRowFields`): an owner the person's own git connections may
 * create in and a name. Owners are each forge's own account, orgs, groups, workspaces or Azure projects, with or without
 * repositories (`GET /api/git-repos/owners`); Create makes the repository private, with main and develop
 * (`POST /api/git-repos`, `app/forge_repo_init.py`) — or, for an owner the GitHub App may not create in, links
 * github.com's new-repository page and Create only adds main and develop (`handoffLink`). */

export type GitRepoOwners = {
  owners: {
    root: string; forge: string; owner: string; own?: boolean; blocked?: string | null; install_url?: string | null
    account_id?: number | null; create_url?: string | null
  }[]
  sources: GitRepoSource[]
  /* An OAuth App is designated, said only when an owner has `create_url` (`routes/git_connections.py`). */
  oauth_connect?: boolean
}
/* `blocked`: why the connection cannot create here (`forge_repos` `Source.blocked`); listed, but not pickable.
 * `installUrl`: the GitHub App's install page, for an owner it is not installed on, opened on that owner where its
 * GitHub account id is known (`installLink`).
 * `createUrl`: github.com's new-repository page, for an owner the App may not create in (`Source.handoff`): the person
 * creates it there and Create only initializes it (`projectForm.ts::prepareRepos`). */
export type RepoOwner = {
  root: string; owner: string; label: string; own: boolean; blocked: string | null; installUrl: string | null
  createUrl: string | null
}

/** The forge's new-repository page for `draft`, filled in: owner, name and private. */
export function handoffLink(draft: NewRepoDraft): string | null {
  if (!draft.create_url) return null
  const name = draft.name.trim()
  return `${draft.create_url}${name ? `&name=${encodeURIComponent(name)}` : ''}&visibility=private`
}

/** The App's install page opened on the owner's own GitHub account (`suggested_target_id`), not GitHub's account
 *  picker; the bare page when the id is unknown. */
export function installLink(installUrl: string, accountId: number | null | undefined): string {
  if (accountId == null) return installUrl
  const url = new URL(installUrl)
  url.searchParams.set('suggested_target_id', String(accountId))
  return url.toString()
}

/** Every owner, one per connected root: the person's own accounts first, then the orgs, groups, workspaces and
 *  projects, each run sorted; the forge rides the label only when more than one root answers. */
export function repoOwners(data: GitRepoOwners | null): RepoOwner[] {
  const owners = data?.owners ?? []
  const manyRoots = new Set(owners.map(o => o.root)).size > 1
  const forgeLabel = (root: string) => data?.sources.find(s => s.root === root)?.forge_label ?? root
  const seen = new Map<string, RepoOwner>()
  for (const o of owners) {
    const value = ownerValue(o)
    if (!seen.has(value)) {
      seen.set(value, {
        root: o.root, owner: o.owner, label: manyRoots ? `${o.owner} · ${forgeLabel(o.root)}` : o.owner, own: o.own === true,
        blocked: o.blocked || null, installUrl: o.blocked && o.install_url ? installLink(o.install_url, o.account_id) : null,
        createUrl: o.blocked ? null : o.create_url || null,
      })
    }
  }
  return [...seen.values()].sort((a, b) => Number(b.own) - Number(a.own) || a.label.localeCompare(b.label))
}

/** One line per forge whose owner read failed, came back limited or was cut short ("GitHub: …"): its list is incomplete. */
export function ownerSourceNotes(data: GitRepoOwners | null): { root: string; text: string; failed: boolean }[] {
  return (data?.sources ?? [])
    .filter(s => s.state === 'failed' || (s.detail ?? '') !== '' || s.truncated)
    .map(s => ({
      root: s.root,
      text: `${s.forge_label}: ${s.detail || (s.state === 'failed' ? 'owners could not be read.' : 'only the first owners are listed.')}`,
      failed: s.state === 'failed',
    }))
}

/** The notes as lines under the owner and name fields (or under the "no owner" sentence): each blocked owner and
 *  why, with the App's install link where that is the fix, then each forge's limit; null when there are none. The
 *  blocked reason also rides the option's label, but a phone's native picker truncates it. `picked`: an owner is
 *  chosen, so the blocked ones are no longer a choice to explain and drop out. `onInstall` runs as the
 *  install link opens GitHub's tab. */
function ownerNotes(
  data: GitRepoOwners | null, owners: RepoOwner[], picked: boolean, onInstall: () => void,
): JSX.Element | null {
  const blocked = picked ? [] : owners.filter(o => o.blocked !== null)
  const notes = ownerSourceNotes(data)
  if (blocked.length === 0 && notes.length === 0) return null
  return (
    <>
      {blocked.map(o => (
        <Box key={ownerValue(o)} component="span" data-testid="project-new-repo-blocked-note" sx={{ display: 'block' }}>
          {o.label}: {o.blocked}.
          <InstallLink owner={o} onInstall={onInstall} />
        </Box>
      ))}
      {notes.map(n => (
        <Box key={n.root} component="span" data-testid="project-new-repo-source-note"
          sx={{ display: 'block', color: n.failed ? 'error.main' : undefined }}>
          {n.text}
        </Box>
      ))}
    </>
  )
}

function InstallLink({ owner, onInstall }: { owner: RepoOwner; onInstall: () => void }): JSX.Element | null {
  if (!owner.installUrl) return null
  return (
    <>
      {' '}
      <Link href={owner.installUrl} target="_blank" rel="noopener noreferrer" onClick={onInstall}>
        Install the App on {owner.owner}
      </Link>
    </>
  )
}

/** Every owner blocked: one bullet per owner, its reason on its own line (a phone cannot hover a picker's label),
 *  then each forge's limit. */
function BlockedOwnerList({ data, owners, onInstall }: {
  data: GitRepoOwners | null; owners: RepoOwner[]; onInstall: () => void
}): JSX.Element {
  return (
    <>
      {/* Caption size, the fix link on a line of its own, and the reason's trailing "here" dropped (the owner leads
          the bullet, so it says nothing): each reason stays one line on a 390px phone. */}
      <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2 }}>
        {owners.map(o => (
          <Typography key={ownerValue(o)} component="li" variant="caption" data-testid="project-new-repo-blocked-note"
            sx={{ display: 'list-item', color: 'text.secondary', '& a': { display: 'block' } }}>
            <Box component="span" sx={{ color: 'text.primary' }}>{o.label}</Box>: {o.blocked?.replace(/\s+here\.?\s*$/, '')}
            <InstallLink owner={o} onInstall={onInstall} />
          </Typography>
        ))}
      </Box>
      {ownerSourceNotes(data).map(n => (
        <Typography key={n.root} variant="body2" data-testid="project-new-repo-source-note"
          sx={{ color: n.failed ? 'error.main' : 'text.secondary', mt: 0.5 }}>
          {n.text}
        </Typography>
      ))}
    </>
  )
}

/** No owner can be picked: the read failed, lists none, or blocks every one. Null while it loads. */
function noOwnerToPick({ data, failed }: { data: GitRepoOwners | null; failed: boolean }): boolean | null {
  if (failed) return true
  if (data === null) return null
  return repoOwners(data).every(o => o.blocked !== null)
}

/** Whether a "Create a new repository" row has no owner to pick (`noOwnerToPick`), for a wizard whose Next it
 *  disables. Reads nothing unless `enabled`; shares the row's own read. */
export function useNoOwnerToPick(enabled: boolean): boolean {
  const read = useRepoOwners(enabled)
  return enabled && noOwnerToPick(read) === true
}

const OWNERS_STALE_MS = 30_000

/** The owners read once per page, shared by every open row: `data` null while it loads, `failed` once it could not be
 *  read. Fresh for `OWNERS_STALE_MS`, then read again on focus: a permission fixed on the forge in another tab shows on
 *  return, without a reload that would cost the draft. A failed read caches nothing, so the next row opened asks again.
 *  `markStale` makes the next focus read again however fresh the read is: an install started now shows on return,
 *  even within `OWNERS_STALE_MS`. It reads nothing itself, which would land fresh while the person is still on GitHub. */
function useRepoOwners(enabled = true, org: OrgRef | null = null): { data: GitRepoOwners | null; failed: boolean; markStale: () => void } {
  /* Keyed by who is signed in (and the org acted as): act-as switches identity without a remount. Another org's
     project (`org`) reads that org's owners, as it. */
  const { admin } = useAuth()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: [REPO_OWNERS_KEY, admin?.id ?? '', org?.id ?? actingAs()?.id ?? ''],
    queryFn: async (): Promise<GitRepoOwners> => {
      const body = await get<Partial<GitRepoOwners> | null>('/api/git-repos/owners', ...readAsOnce(org))
      return {
        owners: Array.isArray(body?.owners) ? body.owners : [], sources: Array.isArray(body?.sources) ? body.sources : [],
        oauth_connect: body?.oauth_connect === true,
      }
    },
    staleTime: OWNERS_STALE_MS,
    refetchOnWindowFocus: true,
    enabled,
  })
  const markStale = () => { void queryClient.invalidateQueries({ queryKey: [REPO_OWNERS_KEY], refetchType: 'none' }) }
  return { data: query.data ?? null, failed: query.isError, markStale }
}

const NOTES_CONTAINER = 'new-repo-row'

const ownerValue = (o: { root: string; owner: string }) => `${o.root}/${o.owner}`

export default function NewRepoFields({ draft, onChange, idBase, error, org = null }: {
  draft: NewRepoDraft
  onChange: (next: NewRepoDraft) => void
  /** The row's id prefix (`project-repo-<index>`): the fields are `<idBase>-owner` and `<idBase>-name`. */
  idBase: string
  /** The form's own check on Next or Done (`projectForm.ts::blankRepoFields`). */
  error?: RepoRowError
  /** Another org's project: that org's owners, read as it. */
  org?: OrgRef | null
}): JSX.Element {
  const notesId = `${idBase}-owner-notes`
  const { data, failed, markStale } = useRepoOwners(true, org)
  const owners = repoOwners(data)
  const picked = draft.owner !== '' ? ownerValue(draft) : ''
  const notes = ownerNotes(data, owners, picked !== '', markStale)
  const handoff = handoffLink(draft)
  /* The hand-off follows the owners as read now, not as they were when picked: a restored draft, or an App granted
  Administration since, must not send Create down the other path. */
  const current = owners.find(o => ownerValue(o) === picked)?.createUrl ?? undefined
  const oauth = current !== undefined && data?.oauth_connect === true ? true : undefined
  const stale = data !== null && picked !== '' && (current !== draft.create_url || oauth !== draft.oauth_connect)
  useEffect(() => { if (stale) onChange({ ...draft, create_url: current, oauth_connect: oauth }) })
  if (failed) {
    return (
      <Typography variant="body2" role="alert" data-testid="project-new-repo-failed" sx={{ color: 'error.main' }}>
        Your forge connections could not be read, so there is no owner to pick. Reload the page to try again.
      </Typography>
    )
  }
  if (data !== null && owners.length === 0) {
    return (
      <>
        {/* Another org's project: the viewer's own Profile > Git would connect the operator, not that org. */}
        <Typography variant="body2" data-testid="project-new-repo-none" sx={{ color: 'text.secondary' }}>
          {org ? (
            <>No forge connection of {org.name} lists an owner to create in. Act as {org.name} to connect one in Profile &gt; Git.</>
          ) : (
            <>
              No forge connection lists an owner to create in. Connect one in{' '}
              <Link component={RouterLink} to="/profile?tab=git-accounts">Profile &gt; Git</Link>.
            </>
          )}
        </Typography>
        {notes && <Typography variant="body2" component="div" sx={{ color: 'text.secondary', mt: 0.5 }}>{notes}</Typography>}
      </>
    )
  }
  /* The way out is the row's own "Use an existing repository" action under this, drawn as in every other state;
  a sentence here repeating it in another style read as a second, different control. */
  if (owners.length > 0 && owners.every(o => o.blocked !== null)) {
    return (
      <>
        <Typography variant="body2" data-testid="project-new-repo-all-blocked" sx={{ color: 'text.secondary' }}>
          No owner can be created in until one of these is fixed:
        </Typography>
        <BlockedOwnerList data={data} owners={owners} onInstall={markStale} />
      </>
    )
  }
  /* The notes span both cells, under the row, not as the Owner field's hint: in the list pane the Owner cell is
  narrow, and a note there wrapped to five lines. They stay the Owner select's description. Once the
  row is too narrow for both cells (2 × 14rem + the 1rem gap) it stacks, and the notes move up under Owner by
  flex `order`, so they never read as notes about the name. */
  return (
    <FieldRow align="start" rowGap="0.75rem" sx={{ containerType: 'inline-size', containerName: NOTES_CONTAINER }}>
      <DetailCell basis="14rem" max="22rem">
        <FormTextField
          id={`${idBase}-owner`}
          label="Owner"
          help="The account, organization, group, workspace or project it is created in."
          value={picked}
          onChange={v => {
            const o = owners.find(x => ownerValue(x) === v)
            onChange({
              ...draft, root: o?.root ?? '', owner: o?.owner ?? '', create_url: o?.createUrl ?? undefined,
              oauth_connect: o?.createUrl && data?.oauth_connect === true ? true : undefined,
            })
          }}
          options={owners.map(o => ({
            value: ownerValue(o), label: o.blocked ? `${o.label} (${o.blocked})` : o.label, disabled: o.blocked !== null,
          }))}
          disabled={data === null}
          error={error?.owner}
          slotProps={{ inputLabel: { shrink: true }, ...(notes ? { htmlInput: { 'aria-describedby': notesId } } : {}) }}
        >
          <option value="">{data === null ? 'Loading…' : 'Pick an owner'}</option>
        </FormTextField>
      </DetailCell>
      <DetailCell basis="14rem" max="22rem" sx={{ order: 2 }}>
        <FormTextField
          id={`${idBase}-name`}
          label="Repository name"
          help={handoff && !draft.oauth_connect ? 'As named on GitHub.' : 'Created private, with main and develop.'}
          value={draft.name}
          onChange={v => onChange({ ...draft, name: v })}
          error={newRepoNameError(draft.name) ?? error?.name}
        />
      </DetailCell>
      {handoff && draft.oauth_connect && (
        <Typography variant="body2" data-testid="project-new-repo-oauth"
          sx={{ flex: '1 1 100%', order: 4, color: 'text.secondary', mx: '14px' }}>
          Create opens GitHub to approve once, then makes it with your own access.
        </Typography>
      )}
      {handoff && !draft.oauth_connect && (
        <Typography variant="body2" data-testid="project-new-repo-handoff"
          sx={{ flex: '1 1 100%', order: 4, color: 'text.secondary', mx: '14px' }}>
          The GitHub App may not create repositories in {draft.owner}.{' '}
          <Link href={handoff} target="_blank" rel="noopener noreferrer">Create it on GitHub</Link>, empty, then
          Create adds main and develop.
        </Typography>
      )}
      {notes && (
        <Typography
          id={notesId}
          variant="body2"
          component="div"
          sx={{
            flex: '1 1 100%', order: 3, color: 'text.secondary', mx: '14px', mt: '-0.25rem',
            [`@container ${NOTES_CONTAINER} (width < 29rem)`]: { order: 1 },
          }}
        >
          {notes}
        </Typography>
      )}
    </FieldRow>
  )
}
