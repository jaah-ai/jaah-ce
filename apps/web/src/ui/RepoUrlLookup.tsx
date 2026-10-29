import { useEffect, useMemo, useState } from 'react'
import type { JSX, ReactNode } from 'react'
import Autocomplete, { createFilterOptions } from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import TextField from '@mui/material/TextField'
import { get } from '../api'
import { useAuth } from '../auth'
import { actingAs, readAsOnce } from '../lib/actAs'
import type { OrgRef } from '../types'
import { highlight, OPTION_ROW_SX } from './AsyncLookup'
import HelpLabel from './HelpLabel'
import { ELLIPSIS_SX, fieldTapTargetSx } from './listStyles'
import { NO_AUTOFILL_INPUT } from './noAutofill'
import { paperGround } from '../theme'

/* The Repo URL field: type or paste any URL, or pick one of the repositories the signed-in person's own git
 * connections reach (`GET /api/git-repos`, every connected forge and every org/workspace in it), grouped by
 * owner. A free-text combobox, not a picker: a forge that is not connected, or a list that failed, leaves a
 * plain URL field.
 *
 * A typed URL on a forge whose list was read in full, but missing from it, reads "Not available to your
 * <forge> connection" under the field once it loses focus — a warning, never a refusal (the save decides). The
 * list is read again once for that URL first: one created after the form opened (a new repo, then this form) is
 * then listed, not warned about.
 *
 * `org`: another org's project (an operator's) — the list, and each repository's state, are that org's
 * connections', read as it (`lib/actAs.ts::readAsOnce`). */

export type GitRepo = {
  url: string
  forge: string
  root: string
  owner: string
  name: string
  /** A self-managed GitLab under a sub-path: the project form's "GitLab base URL". */
  base_url?: string | null
}

export type GitRepoSource = {
  root: string
  forge: string
  forge_label: string
  state: 'ok' | 'failed'
  detail?: string | null
  truncated: boolean
}

export type GitRepos = { repos: GitRepo[]; sources: GitRepoSource[] }

/* One read shared by every field and dialog for a minute: reading every forge costs seconds. Keyed by who is
   signed in (and the org acted as), because sign-out and act-as switch identity without a page load and the
   list is one person's. Re-read after a minute, so a connection made in another tab shows up. */
const CACHE_MS = 60_000
/* A field focused on a list older than this reads it again (showing the old one meanwhile): a repo created after
   the form opened is then offered as its name is typed. */
const STALE_ON_FOCUS_MS = 15_000
let cached: { who: string; at: number; promise: Promise<GitRepos> } | null = null

/** Who a read answers for: the signed-in person, and the org acted as (the tab's, or `org` for this one read). */
const readerKey = (adminId: string | undefined, org: OrgRef | null): string => `${adminId ?? ''}:${org?.id ?? actingAs()?.id ?? ''}`

function loadRepos(who: string, org: OrgRef | null): Promise<GitRepos> {
  if (cached === null || cached.who !== who || Date.now() - cached.at > CACHE_MS) {
    // An answer of another shape (an older API) reads as an empty list, never a crash.
    const promise = get<Partial<GitRepos> | null>('/api/git-repos', ...readAsOnce(org)).then(body => ({
      repos: Array.isArray(body?.repos) ? body.repos : [],
      sources: Array.isArray(body?.sources) ? body.sources : [],
    }))
    cached = { who, at: Date.now(), promise }
    promise.catch(() => { if (cached?.promise === promise) cached = null })
  }
  return cached.promise
}

/** `url`'s default branch and whether it has no branch at all, as its forge reports them through the person's own
 *  connection (`GET /api/git-repos/default-branch`); null for what cannot be read. */
export type RepoState = { default_branch: string | null; empty: boolean | null }

export function loadRepoState(url: string, org: OrgRef | null = null): Promise<RepoState> {
  return get<{ default_branch?: string | null; empty?: boolean | null } | null>(
    `/api/git-repos/default-branch?url=${encodeURIComponent(url.trim())}`,
    ...readAsOnce(org),
  )
    .then(body => ({
      default_branch: typeof body?.default_branch === 'string' && body.default_branch !== '' ? body.default_branch : null,
      empty: typeof body?.empty === 'boolean' ? body.empty : null,
    }))
    .catch(() => ({ default_branch: null, empty: null }))
}

/** Test seam: forget the shared read. */
export function resetRepoLookupCache(): void {
  cached = null
}

/** A comparable form of a repo URL: no scheme, user, `.git`, query or trailing slash; ssh `host:path` as
 *  `host/path`; lowercase and percent-decoded (every forge routes repo paths case-insensitively). */
export function repoKey(url: string): string {
  let s = url.trim()
  const scp = s.includes('://') ? null : /^(?:[^@/\s]+@)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):(?!\/\/)(.+)$/.exec(s)
  s = scp ? `${scp[1]}/${scp[2]}` : s.replace(/^[a-z+]+:\/\//i, '').replace(/^[^@/]+@/, '')
  s = s.replace(/[?#].*$/, '').replace(/\/+$/, '').replace(/\.git$/i, '')
  try {
    s = decodeURIComponent(s)
  } catch {
    // A stray `%` is kept as typed.
  }
  return s.toLowerCase().replace(/^www\.github\.com\//, 'github.com/')
}

/** The forge whose full list should hold `url` but does not, or null (listed, or nothing to check against). */
export function unlistedForge(url: string, data: GitRepos | null, forges?: readonly string[]): string | null {
  if (data === null || url.trim() === '') return null
  const key = repoKey(url)
  const source = data.sources.find(s => key.startsWith(`${s.root}/`) && (!forges || forges.includes(s.forge)))
  if (!source || source.state !== 'ok' || source.truncated) return null
  const listed = data.repos.some(repo => {
    const own = repoKey(repo.url)
    return key === own || key.startsWith(`${own}/`)
  })
  return listed ? null : source.forge_label
}

const matchRepo = createFilterOptions<GitRepo>({ stringify: repo => `${repo.owner}/${repo.name} ${repo.url}` })

const GROUP_LABEL_SX = { ...ELLIPSIS_SX, flex: '1 1 auto', fontSize: '0.8125rem', color: 'text.primary' } as const
const GROUP_HINT_SX = { ...ELLIPSIS_SX, flex: '0 1 auto', fontSize: '0.75rem', color: 'text.secondary' } as const

export default function RepoUrlLookup({
  id,
  label,
  help,
  value,
  onChange,
  onPick,
  onBlur,
  forges,
  warnUnlisted = true,
  hint,
  error,
  required,
  requiredMark = true,
  placeholder,
  disabled,
  org = null,
}: {
  id: string
  label: string
  /** A few words shown on hovering the label (`ui/HelpLabel`), as `ui/FormField` does. */
  help?: string
  value: string
  onChange: (url: string) => void
  /** Also called when a listed repository is picked (after `onChange`), for the fields it implies. */
  onPick?: (repo: GitRepo) => void
  /** Called with the field's value as it loses focus. */
  onBlur?: (url: string) => void
  /** Offer only these forges' repositories (`'github'`, …); all when absent. */
  forges?: readonly string[]
  warnUnlisted?: boolean
  hint?: ReactNode
  error?: string
  required?: boolean
  requiredMark?: boolean
  /** Shown while there is no list to search (no forge connected, or it failed). */
  placeholder?: string
  disabled?: boolean
  /** Another org's project: that org's repositories, read as it. */
  org?: OrgRef | null
}): JSX.Element {
  const { admin } = useAuth()
  // The org rides `who`, so the reads below re-run when it changes.
  const who = readerKey(admin?.id, org)
  const [data, setData] = useState<GitRepos | null>(null)
  const [loading, setLoading] = useState(true)
  const [focused, setFocused] = useState(false)
  const [readAt, setReadAt] = useState(0)

  useEffect(() => {
    let live = true
    setData(null)
    setLoading(true)
    loadRepos(who, org)
      .then(next => { if (live) { setData(next); setReadAt(Date.now()) } })
      // No list is a plain URL field; the field says nothing about it.
      .catch(() => {})
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [who])

  const repos = useMemo(
    () => (data?.repos ?? []).filter(repo => !forges || forges.includes(repo.forge)),
    [data, forges],
  )
  // The forge rides the group header only when the list spans more than one root.
  const manyRoots = new Set(repos.map(repo => repo.root)).size > 1
  const groupOf = (repo: GitRepo) => (manyRoots ? `${repo.owner}\u0000${repo.root}` : repo.owner)

  const unlisted = warnUnlisted && !focused ? unlistedForge(value, data, forges) : null
  // The URL a fresh read was made for: the warning waits for it, so the list it judges is current.
  const unlistedKey = unlisted ? repoKey(value) : ''
  const [rechecked, setRechecked] = useState('')
  useEffect(() => {
    if (!unlistedKey || rechecked === unlistedKey) return
    let live = true
    resetRepoLookupCache()
    loadRepos(who, org)
      .then(next => { if (live) setData(next) })
      .catch(() => {})
      .finally(() => { if (live) setRechecked(unlistedKey) })
    return () => { live = false }
  }, [unlistedKey, rechecked, who])
  const missing = unlisted && rechecked === unlistedKey ? unlisted : null
  const need = Boolean(required)
  // A forge whose read failed offers nothing, which looked like a lookup that does not work: say so.
  const failed = (data?.sources ?? []).find(src => src.state === 'failed' && (!forges || forges.includes(src.forge)))
  const shownHint = missing ? (
    <>
      <Box component="span" sx={{ color: 'warning.main', display: 'block' }} data-testid={`${id}-unlisted`}>
        Not available to your {missing} connection.
      </Box>
      {hint}
    </>
  ) : failed ? (
    <>
      <Box component="span" sx={{ display: 'block' }} data-testid={`${id}-unread`}>
        Couldn't read your {failed.forge_label} repos{failed.detail ? `: ${failed.detail}` : '.'}
      </Box>
      {hint}
    </>
  ) : hint

  return (
    /* `ui/FormField`'s contract by hand (the error REPLACES the hint): it clones its props onto the child,
       and `Autocomplete` would put them on its root `div`, not its `TextField`. */
    <Autocomplete<GitRepo, false, false, true>
      id={id}
      freeSolo
      fullWidth
      size="small"
      options={repos}
      groupBy={groupOf}
      value={value}
      inputValue={value}
      disabled={disabled}
      loading={loading}
      autoHighlight={false}
      openOnFocus
      /* A picked URL in the box would filter the list down to itself: the full list is offered again until
         the person types. */
      filterOptions={(options, state) => {
        const typed = state.inputValue.trim()
        if (typed === '' || options.some(repo => repo.url === typed)) return options
        return matchRepo(options, state)
      }}
      getOptionLabel={option => (typeof option === 'string' ? option : option.url)}
      isOptionEqualToValue={(option, current) => option.url === (typeof current === 'string' ? current : current.url)}
      onInputChange={(_event, next, reason) => {
        // MUI's `reset` echoes the picked label; the value is already the picked URL.
        if (reason === 'input' || reason === 'clear') onChange(next)
      }}
      onChange={(_event, next) => {
        if (next === null) {
          onChange('')
        } else if (typeof next === 'string') {
          onChange(next)
        } else {
          onChange(next.url)
          onPick?.(next)
        }
      }}
      onFocus={() => {
        setFocused(true)
        if (readAt && Date.now() - readAt > STALE_ON_FOCUS_MS) {
          setReadAt(Date.now())
          resetRepoLookupCache()
          loadRepos(who, org).then(setData).catch(() => {})
        }
      }}
      onBlur={() => {
        setFocused(false)
        onBlur?.(value)
      }}
      onKeyDown={event => {
        // Enter picks a highlighted row or keeps the typed URL; it never submits the surrounding form.
        if (event.key === 'Enter') event.preventDefault()
      }}
      renderGroup={params => {
        const [owner, root] = params.group.split('\u0000')
        return (
          <li key={params.key}>
            <Box
              sx={{
                ...OPTION_ROW_SX, position: 'sticky', top: '-8px', zIndex: 1, px: 2, py: 0.75,
                bgcolor: paperGround, borderBottom: 1, borderColor: 'divider', fontWeight: 600,
              }}
            >
              <Box component="span" sx={{ ...GROUP_LABEL_SX, fontWeight: 600 }}>{owner}</Box>
              {root && <Box component="span" sx={GROUP_HINT_SX}>{root}</Box>}
            </Box>
            <Box component="ul" sx={{ p: 0 }}>{params.children}</Box>
          </li>
        )
      }}
      renderOption={(props, option, state) => {
        const { key: _muiKey, ...rest } = props
        return (
          <Box component="li" key={option.url} {...rest} title={option.url} aria-label={`${option.owner}/${option.name}`}>
            <Box component="span" sx={{ ...ELLIPSIS_SX, pl: 1, fontSize: '0.8125rem' }}>
              {highlight(option.name, state.inputValue)}
            </Box>
          </Box>
        )
      }}
      slotProps={{ listbox: { sx: { maxHeight: '22rem' } } }}
      renderInput={params => (
        <TextField
          {...params}
          sx={fieldTapTargetSx}
          label={help ? <HelpLabel label={label} help={help} /> : label}
          required={need && requiredMark}
          error={Boolean(error)}
          helperText={error || shownHint}
          placeholder={repos.length > 0 ? 'Search or paste a URL' : placeholder}
          slotProps={{
            ...params.slotProps,
            inputLabel: { shrink: true },
            input: {
              ...params.slotProps.input,
              endAdornment: (
                <>
                  {loading && focused && <CircularProgress size={16} thickness={5} data-testid={`${id}-busy`} />}
                  {params.slotProps.input.endAdornment}
                </>
              ),
            },
            htmlInput: {
              ...params.slotProps.htmlInput,
              ...NO_AUTOFILL_INPUT,
              ...(help ? { 'aria-description': help } : {}),
              ...(need && !requiredMark ? { required: true } : {}),
            },
          }}
        />
      )}
    />
  )
}
