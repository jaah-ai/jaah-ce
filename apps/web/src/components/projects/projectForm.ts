import type { Forge, OrgRef, Project, ProjectCreateDefaults, ProjectRepo, TechStackEntry } from '../../types'
import { ApiError, apiDetail, post } from '../../api'
import { writeAsOnce } from '../../lib/actAs'
import { strOrNull } from '../../lib/strings'
import { loadRepoState } from '../../ui/RepoUrlLookup'
import { connectInPopup, openConnectPopup } from './githubOAuthPopup'

/* THE PROJECT FORM MODEL — the draft (`ProjectFormState`), its seeders, the request body (`buildBody`) and the
 * predicates that decide what renders, what is checked and what is sent. Two readers: the edit page
 * (`pages/ProjectEditPage.tsx`) and the create wizard (`pages/ProjectCreateWizard.tsx`), which shows a few of
 * these fields and sends every other one at its `emptyForm` seed. */

type ProjectFormMode = 'create' | 'edit'

export type ProjectFormState = {
  mode: ProjectFormMode
  id?: number
  name: string
  /* The connected repositories, sent as the whole set (`ProjectUpdate.repos`); a row with a blank URL is
  no repository and is not sent (`filledRepos`). None filled is a project with no repository (`formHasRepo`). `null`:
  a cached row that predates `repos` — nothing to edit, and `buildBody` omits the key so the stored set is left
  alone. The primary's branch is the project's integration branch (a governance field: `primaryBranchLocked`). */
  repos: RepoDraft[] | null
  default_model: string
  /* The default agent, the agent picker's two halves (`''`/null = none). Edit only: `ProjectCreate` has neither. */
  default_agent_id: string | null
  default_provider: string
  /* The persona new tasks start as (`''` = the org's "Persona" default). Edit only, as the default agent. */
  default_persona: string
  port_vars: string
  port_block: string
  branch_prefix: string
  gate_quick_cmd: string
  gate_full_cmd: string
  /* PRs per merge-lane batch (`merge_lane_batch_max`); `''` = the lane's default. Edit only, not governance. */
  merge_lane_batch_max: string
  stack_up_cmd: string
  stack_down_cmd: string
  /* Tri-state: a checkbox has no empty state, so without `null` an unknown flag would render and submit
  as `false`. `formFromProject` says when unknown happens; `buildBody` makes the null unsendable. */
  push_enabled: boolean | null
  /* A `users.id` UUID string, or `''` for nobody: a select's value is always a string, and `null` would
  make it uncontrolled. `buildBody` is the one place `''` becomes the column's null. */
  business_analyst_id: string
  architect_id: string
  /* The whole Jira config: the `credentials` row, or `null`. Null rather than `''` because
  `ui/AsyncLookup` already models nothing-chosen as null; the uuid key is the column value. */
  jira_credential_id: string | null
  tech_stack: TechStackEntry[]
  notes: string
  /* A plain string like `notes`, not a governance string, though it renders on Governance: any
  `projects:edit` holder may write it. `buildBody` is where that matters. */
  agent_instructions: string
}

/* A new project's governance fields, as migrations 0102 and 0112 gave existing rows; the create wizard sends
 * them unshown. A seed, not a fallback: nothing downstream re-derives a default (`integration_branch` has no
 * `?? 'develop'`). The gate and stack commands are the org's (`createCommands`), not constants. */
const NEW_PROJECT_GOVERNANCE = {
  branch_prefix: '',
  push_enabled: true,
} as const

/* The org's `Projects > Defaults` gate and stack commands off the identity payload
 * (`admin.project_create_defaults`), fenced per field; absent (older server, no `me` yet) is `''`, i.e. none. */
export function createCommands(raw: Partial<ProjectCreateDefaults> | null | undefined): ProjectCreateDefaults {
  const str = (v: unknown) => (typeof v === 'string' ? v : '')
  return {
    gate_quick_cmd: str(raw?.gate_quick_cmd),
    gate_full_cmd: str(raw?.gate_full_cmd),
    stack_up_cmd: str(raw?.stack_up_cmd),
    stack_down_cmd: str(raw?.stack_down_cmd),
  }
}

export function emptyForm(mode: ProjectFormMode, commands: ProjectCreateDefaults): ProjectFormState {
  return {
    mode, name: '', repos: [newRepoDraft(true)],
    default_model: '', default_agent_id: null, default_provider: '', default_persona: '', merge_lane_batch_max: '',
    port_vars: '',
    port_block: '', ...NEW_PROJECT_GOVERNANCE, ...commands,
    // Not seeded: there is no default person, and the create path does not accept them (see `namesHumans`).
    business_analyst_id: '', architect_id: '',
    jira_credential_id: null,
    /* Not seeded: a default would be prose nobody wrote in every runner's system prompt. */
    tech_stack: [], notes: '', agent_instructions: '',
  }
}

export function formFromProject(p: Project): ProjectFormState {
  return {
    mode: 'edit', id: p.id, name: p.name,
    repos: p.repos ? p.repos.map(repoDraftFromRow) : null,
    default_model: p.default_model ?? '',
    default_agent_id: p.default_agent_id ?? null,
    default_provider: p.default_provider ?? '',
    default_persona: p.default_persona ?? '',
    merge_lane_batch_max: p.merge_lane_batch_max != null ? String(p.merge_lane_batch_max) : '',
    port_vars: (p.port_vars ?? []).join(', '),
    port_block: p.port_block != null ? String(p.port_block) : '',
    /* `?? ''` on six NOT NULL columns, never a house default: the persisted roster can predate the columns
    right after a deploy. It is a controlled-input guard, and an empty required field honestly blocks submit;
    seeding a default would write a value nobody chose. */
    branch_prefix: p.branch_prefix ?? '',
    gate_quick_cmd: p.gate_quick_cmd ?? '',
    gate_full_cmd: p.gate_full_cmd ?? '',
    stack_up_cmd: p.stack_up_cmd ?? '',
    stack_down_cmd: p.stack_down_cmd ?? '',
    /* `?? null`, never `?? false`: a pre-deploy cached row lacks the key, and `false` would make an unrelated
    Save revoke push and dispatch `sync_access_guard`. `null` renders indeterminate and `buildBody` omits it. */
    push_enabled: p.push_enabled ?? null,
    /* A real conversion: the column is nullable and null means "— None —"; a pre-column row reads the same. */
    business_analyst_id: p.business_analyst_id ?? '',
    architect_id: p.architect_id ?? '',
    /* The warm-reload guard: absent key → null ("no Jira"), keeping the picker controlled. */
    jira_credential_id: p.jira_credential_id ?? null,
    /* Coerced per field, not just per array: `list[dict[str, str]]` once accepted `[{"layer": "frontend"}]`,
    and `buildBody`'s `.trim()` on `undefined` threw inside `handleSubmit`'s try, showing "Failed to save
    project" with no request. The API now refuses it (`routes/projects.py::TechStackEntry`); existing rows
    must still render and re-save. `String(...)`, not a cast. */
    tech_stack: (p.tech_stack ?? []).map(t => ({
      layer: t?.layer != null ? String(t.layer) : '',
      technology: t?.technology != null ? String(t.technology) : '',
    })),
    notes: p.notes ?? '',
    // Nullable column and warm-reload guard at once.
    agent_instructions: p.agent_instructions ?? '',
  }
}

/* Hosts whose forge the server derives (`apps/api/app/forges.py`, the authority). Mirrored here for display
 * only — whether to ask for a forge; a drift costs a shown/hidden picker, never a wrong save. */
const HOST_FORGE: Readonly<Record<string, Forge>> = {
  'github.com': 'github', 'www.github.com': 'github', 'gitlab.com': 'gitlab', 'bitbucket.org': 'bitbucket',
  'dev.azure.com': 'azure_devops', 'ssh.dev.azure.com': 'azure_devops',
}
const KNOWN_FORGE_HOSTS = new Set(Object.keys(HOST_FORGE))

/* The host of `https://host/…`, `ssh://[user@]host[:port]/…` or scp-like `[user@]host:path`; null when none
 * parses (empty, or a bare word — nothing to ask about yet). */
function repoHost(url: string): string | null {
  const t = url.trim()
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\/(?:[^@/]*@)?([^/:?#]+)/i.exec(t)
  if (withScheme) return withScheme[1].toLowerCase()
  const scpLike = /^(?:[^@/\s]+@)?([^/:\s]+):(?!\/)/.exec(t)
  return scpLike ? scpLike[1].toLowerCase() : null
}

/* Ask for a forge only where the host cannot name one. One predicate for what renders and what `buildBody`
 * sends. */
export function forgePickerShown(repoUrl: string): boolean {
  const host = repoHost(repoUrl)
  return host !== null && !KNOWN_FORGE_HOSTS.has(host) && !host.endsWith('.visualstudio.com')
}

/* ONE CONNECTED REPOSITORY IN THE DRAFT. `key` is the row's identity in the table (a stored row's id, or a
 * local one for a row not saved yet); `id` is what the PUT names; `saved` is the server's derivation for a
 * stored row (its `owner/name`, forge page and forge), dropped once its URL is edited. */
export type RepoDraft = {
  key: string
  id?: number
  repo_url: string
  /* The self-managed GitLab declaration, or `''` for none. The only forge an operator ever names: a known
  host derives its own server-side. Sent only while `forgePickerShown` (`repoBody`). */
  forge: '' | 'gitlab'
  forge_base_url: string
  integration_branch: string
  is_primary: boolean
  saved?: { repo_url: string; integration_branch: string; name: string; web_url: string | null; forge: Forge | null }
  /* The URL its forge reported with no branch at all (`RepoRowFields`); never sent. */
  empty?: string
  /* "Create a new repository": Create or Save makes it (`createNewRepos`) and the row takes its URL; never sent. */
  create?: NewRepoDraft
}

/** Was this row's URL, as it stands, found with no branch at all? The create wizard initializes it on Create. */
export function isEmptyRepo(repo: RepoDraft): boolean {
  return repo.empty !== undefined && repo.empty === repo.repo_url
}

/* A row's "Create a new repository": where (`owner` on the connected `root`) and what it is called. Create (the
 * wizard) or Save (the edit page) makes it private with main and develop (`POST /api/git-repos`), then the project. */
export type NewRepoDraft = {
  root: string
  owner: string
  name: string
  /* The owner's hand-off (`RepoOwner.createUrl`): the person creates it on the forge, and Create only initializes it. */
  create_url?: string
  /* With `create_url`, an OAuth App is designated (`GitRepoOwners.oauth_connect`): Create first connects GitHub
  through it in a popup, then makes the repository with that token, the person's own rights (`githubOAuthPopup`). */
  oauth_connect?: boolean
  /* The row's branch before "Create a new repository" set NEW_REPO_BRANCH: "Use an existing repository" puts it back. */
  prior_branch?: string
}

// The characters every forge accepts in a repository name (GitHub's set, the narrowest).
const NEW_REPO_NAME_RE = /^[A-Za-z0-9._-]{1,100}$/

/** Why `name` cannot be a new repository's name, or null. */
export function newRepoNameError(name: string): string | null {
  const t = name.trim()
  if (t === '') return null
  if (t.length > 100) return 'At most 100 characters.'
  if (!NEW_REPO_NAME_RE.test(t) || t === '.' || t === '..') return 'Letters, digits, ".", "-" and "_" only.'
  return null
}

/* A new row's branch: the seed every project had before repos (migration 0102), not a guess at a forge's
 * default. Typed over at will — except the primary's for a viewer who may not author governance, for whom the
 * server compares it with exactly this value on create (`_repos_author_the_branch`). */
export const NEW_REPO_BRANCH = 'develop'

let draftSeq = 0
export function newRepoDraft(isPrimary: boolean): RepoDraft {
  draftSeq += 1
  return { key: `new-${draftSeq}`, repo_url: '', forge: '', forge_base_url: '', integration_branch: NEW_REPO_BRANCH, is_primary: isPrimary }
}

function repoDraftFromRow(r: ProjectRepo): RepoDraft {
  return {
    key: `id-${r.id}`, id: r.id, repo_url: r.repo_url,
    /* Seeded from any `gitlab`, gitlab.com's included: on a known host the picker is hidden and `repoBody`
    sends null, so the seed only matters where it is a declaration. */
    forge: r.forge === 'gitlab' ? 'gitlab' : '',
    forge_base_url: r.forge_base_url ?? '',
    integration_branch: r.integration_branch,
    is_primary: r.is_primary,
    saved: { repo_url: r.repo_url, integration_branch: r.integration_branch, name: r.name, web_url: r.web_url, forge: r.forge },
  }
}

/** The row's forge: the server's for a stored, unedited URL when it names one; otherwise what its host or
 * declaration says (a row stored without a forge, e.g. seeded, still reads its host). */
export function repoForge(repo: RepoDraft): Forge | null {
  if (repo.create) return HOST_FORGE[repo.create.root] ?? null
  if (repo.saved && repo.saved.repo_url === repo.repo_url && repo.saved.forge !== null) return repo.saved.forge
  const host = repoHost(repo.repo_url)
  if (host === null) return null
  if (host.endsWith('.visualstudio.com')) return 'azure_devops'
  return HOST_FORGE[host] ?? (repo.forge === 'gitlab' ? 'gitlab' : null)
}

/** `owner/name` (every path segment, `.git` dropped): the server's for a stored, unedited URL, else the URL's path. */
export function repoName(repo: RepoDraft): string {
  if (repo.create) return `${repo.create.owner}/${repo.create.name.trim()}`
  if (repo.saved && repo.saved.repo_url === repo.repo_url) return repo.saved.name
  const t = repo.repo_url.trim()
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\/[^/]+\/(.*)$/i.exec(t)
  const scpLike = withScheme ? null : /^(?:[^@/\s]+@)?[^/:\s]+:(?!\/)(.*)$/.exec(t)
  const path = withScheme?.[1] ?? scpLike?.[1] ?? t
  return path.replace(/[?#].*$/, '').replace(/\/+$/, '').replace(/\.git$/i, '') || t
}

/** The row's forge page: the server's for a stored, unedited URL; an https URL as typed otherwise; else none. */
export function repoWebUrl(repo: RepoDraft): string | null {
  if (repo.create) return null
  if (repo.saved && repo.saved.repo_url === repo.repo_url) return repo.saved.web_url
  const t = repo.repo_url.trim()
  return /^https:\/\/[^/]+\/.+/i.test(t) ? t.replace(/\.git$/i, '') : null
}

/** Removing the primary while others remain, as the server words it (AS4: `_PICK_ANOTHER_PRIMARY`). */
export const PICK_ANOTHER_PRIMARY = 'Pick another primary first.'
/** The banner over a refusal that lives on the rows. */
export const FIX_MARKED_REPOS = 'Fix the marked repositories.'

/** A non-empty set needs exactly one primary: after a row leaves, the first remaining one takes it. */
export function withPrimary(repos: RepoDraft[]): RepoDraft[] {
  if (repos.length === 0 || repos.some(r => r.is_primary)) return repos
  return repos.map((r, i) => ({ ...r, is_primary: i === 0 }))
}

/** The per-row messages a table draws: on a field (shown on that control) or on the row itself. */
export type RepoRowError = { repo_url?: string; integration_branch?: string; owner?: string; name?: string; row?: string }
export type RepoRowErrors = Record<string, RepoRowError>
/** A row's field errors (all but `row`), which keep it open. */
export const REPO_FIELD_ERRORS = ['repo_url', 'integration_branch', 'owner', 'name'] as const

/** The form's own check before a save: every row needs a URL and a branch, or — a new one — an owner and a
 *  valid name. Keyed by `RepoDraft.key`. */
export function blankRepoFields(repos: readonly RepoDraft[]): RepoRowErrors {
  const errors: RepoRowErrors = {}
  for (const repo of repos) {
    const nameError = repo.create && (repo.create.name.trim() === '' ? 'Required' : newRepoNameError(repo.create.name))
    const e: RepoRowError = repo.create
      ? {
        ...(repo.create.owner ? {} : { owner: 'Required' }),
        ...(nameError ? { name: nameError } : {}),
      }
      : {
        ...(repo.repo_url.trim() ? {} : { repo_url: 'Required' }),
        ...(repo.integration_branch.trim() ? {} : { integration_branch: 'Required' }),
      }
    if (REPO_FIELD_ERRORS.some(f => e[f])) errors[repo.key] = e
  }
  return errors
}

/** A new repository's create failed: the row it was for, and the server's sentence. */
export class NewRepoError extends Error {
  constructor(message: string, readonly key: string) {
    super(message)
  }
}

type MadeRepo = { url: string; forge: string; root: string; base_url?: string | null; init_error?: string | null }

/* Make each row's new repository, in order (`POST /api/git-repos`), then give each new row found empty — when its
 * URL was entered, or now when `probe` (the wizard), in case that answer had not come — main and develop
 * (`POST /api/git-repos/initialize`; a cut-short initialization is finished: `forge_repo_init.initialize`). Each
 * made row at once becomes an existing one on its URL (`onRows`; marked empty when its initialization failed), so a retried
 * Create or Save never makes it twice. Returns the rows to send; throws a `NewRepoError` naming a row it could not
 * make, else the forge's own refusal. `org`: another org's project (an operator's) — every read and write
 * here goes as that org, through its forge connections (`lib/actAs.ts::writeAsOnce`). */
export async function prepareRepos(
  rows: RepoDraft[], projectName: string, probe: boolean, onRows: (rows: RepoDraft[]) => void, org: OrgRef | null = null,
): Promise<RepoDraft[]> {
  const asOrg = writeAsOnce(org)
  let out = rows
  const replace = (key: string, next: RepoDraft) => {
    out = out.map(x => (x.key === key ? next : x))
    onRows(out)
  }
  /* A hand-off owner with an OAuth App: connect through it first (the popup opened before any `await`), then make
  the repository as any other, with that token. */
  const viaOAuth = rows.filter(r => r.create?.create_url && r.create.oauth_connect)
  // The connect is the signed-in person's own, never another org's: that org connects as itself.
  if (viaOAuth.length && org) throw new NewRepoError(`Act as ${org.name} to connect GitHub for it.`, viaOAuth[0].key)
  if (viaOAuth.length) {
    try {
      await connectInPopup(openConnectPopup())
    } catch (err) {
      throw new NewRepoError(apiDetail(err, 'GitHub could not be connected.'), viaOAuth[0].key)
    }
    for (const r of viaOAuth) replace(r.key, { ...r, create: { ...r.create!, create_url: undefined, oauth_connect: undefined } })
  }
  for (const r of out) {
    if (!r.create) continue
    let made: MadeRepo
    try {
      made = r.create.create_url
        ? await handedOff(r.create, projectName, asOrg)
        : await post<MadeRepo>('/api/git-repos', { root: r.create.root, owner: r.create.owner, name: r.create.name.trim() }, ...asOrg)
    } catch (err) {
      const why = apiDetail(err, 'Failed to create the repository')
      // The forge's 403/404 ("may not do this here", `forge_repo_init._call`): not made yet, or out of the App's reach.
      throw new NewRepoError(
        r.create.create_url && why.includes('may not do this here') ? `${why.replace(/[.!?]?$/, '.')} ${HANDOFF_FIX}` : why, r.key,
      )
    }
    const { create: _create, ...row } = r
    replace(r.key, {
      ...row,
      repo_url: made.url,
      forge: made.forge === 'gitlab' && made.root !== 'gitlab.com' ? 'gitlab' : '',
      forge_base_url: made.base_url ?? '',
      // Made with main and develop; one whose initialization failed is finished below on the retry.
      empty: made.init_error ? made.url : undefined,
    })
    if (made.init_error) throw new NewRepoError(made.init_error, r.key)
  }
  for (const r of withPrimary(filledRepos(out))) {
    if (r.id !== undefined) continue
    const url = r.repo_url.trim()
    if (!isEmptyRepo(r)) {
      if (!probe || (await loadRepoState(url, org)).empty !== true) continue
      // Marked before writing: a cut-short initialization reads as non-empty, and the retry must still finish it.
      replace(r.key, { ...r, empty: r.repo_url })
    }
    await post('/api/git-repos/initialize', { url, name: projectName.trim() }, ...asOrg)
  }
  return out
}

/* A hand-off owner's repository, made on the forge by the person (`NewRepoFields::handoffLink`): only initialized here.
 * One not made yet, or out of the App's reach, is the forge's refusal plus `HANDOFF_FIX`; the row stays new for the retry.
 * One the forge already gave other branches is refused: the project's develop would not exist. */
async function handedOff(draft: NewRepoDraft, projectName: string, asOrg: [] | [HeadersInit]): Promise<MadeRepo> {
  const path = `${draft.owner}/${draft.name.trim()}`
  const url = `https://${draft.root}/${path}`
  const { initialized } = await post<{ initialized: boolean }>('/api/git-repos/initialize', { url, name: projectName.trim() }, ...asOrg)
  // Another branch than main (an older repository, or one GitHub seeded on master): develop would not exist.
  if (!initialized) throw new Error(`${path} already has branches. Create it empty on GitHub, or use it as an existing repository.`)
  return { url, forge: 'github', root: draft.root }
}

const HANDOFF_FIX = 'Create it on GitHub first. If the App has only selected repositories, add it there.'

/* The server's short words for a code, where its sentence is longer than the row needs (ui-copy-standards). */
const REPO_CODE_MESSAGES: Record<string, string> = {
  duplicate_repo: 'Already connected to this project.',
  integration_branch_required: 'Required',
  primary_removed: PICK_ANOTHER_PRIMARY,
}

/* A per-row 422 (`routes/projects.py::_validated_repos`: `loc = ['body','repos',<i>,<field>]`) as messages on
 * the rows that were sent, keyed by `RepoDraft.key`; null for any other error. `sent` is the list as sent, so
 * index `i` is `sent[i]`. */
export function repoRowErrors(err: unknown, sent: readonly RepoDraft[]): RepoRowErrors | null {
  if (!(err instanceof ApiError && err.status === 422 && Array.isArray(err.detail))) return null
  const errors: RepoRowErrors = {}
  for (const item of err.detail as { loc?: unknown[]; msg?: unknown; type?: unknown }[]) {
    const loc = item?.loc
    if (!Array.isArray(loc) || loc[0] !== 'body' || loc[1] !== 'repos' || typeof loc[2] !== 'number') continue
    const repo = sent[loc[2]]
    if (!repo) continue
    const raw = typeof item.msg === 'string' ? item.msg.replace(/^repo_url:\s*/, '') : 'Refused.'
    let message = (typeof item.type === 'string' ? REPO_CODE_MESSAGES[item.type] : undefined) ?? raw
    // An unrecognised host whose forge is still unset: the fix is the picker, not the URL.
    if (loc[3] === 'repo_url' && item.type === 'invalid_repo_url' && forgePickerShown(repo.repo_url) && repo.forge === '') {
      message = 'Unrecognised host — set Forge, or fix the URL.'
    }
    const field = loc[3] === 'repo_url' || loc[3] === 'integration_branch' ? loc[3] : 'row'
    errors[repo.key] = { ...errors[repo.key], [field]: errors[repo.key]?.[field] ?? message }
  }
  return Object.keys(errors).length ? errors : null
}

/* The seven governance fields as a detachable fragment. On the PUT they are tier-authored
 * (`_require_authority_to_author_governance`, app/routes/projects.py: a superadmin, or an org-admin with
 * `nodes:edit` on a project in their org); everything else is `projects:edit`.
 * The API tests authorship (`field in payload`), not change, so re-sending the stored values 403s. An
 * ordinary holder must send a body naming none of the seven. One fragment, one decision.
 * Six here: the seventh, `integration_branch`, is the primary row's branch in `repos`, which the server counts
 * as authored only when it changes (`_repos_author_the_branch`); the table locks it instead. */
function governanceBody(form: ProjectFormState): Record<string, unknown> {
  return {
    /* The five text fields go out `.trim()`, never `strOrNull`: they are NOT NULL (a null is a 422).
    `branch_prefix`'s `''` is a real value (no prefix), as is a blank gate or stack command (none). The
    trailing-slash normalisation (`sn` → `sn/`) is the API's; send the typed value. */
    branch_prefix: form.branch_prefix.trim(),
    gate_quick_cmd: form.gate_quick_cmd.trim(),
    gate_full_cmd: form.gate_full_cmd.trim(),
    stack_up_cmd: form.stack_up_cmd.trim(),
    stack_down_cmd: form.stack_down_cmd.trim(),
    /* Spread-if-known: `null` is "the row has not told us", and an omitted key leaves the column alone under
    `exclude_unset`. `false` would be a guess with a side effect; `null` is a 422. */
    ...(form.push_enabled !== null ? { push_enabled: form.push_enabled } : {}),
  }
}

/* May this form name the two humans? One predicate for what `buildBody` sends and what renders.
 * Edit only: the create path does not accept the keys (a 422), and the candidates come from
 * `GET /api/projects/{id}/members`, which needs an id. */
export function namesHumans(form: ProjectFormState): boolean {
  return form.mode === 'edit'
}

/* One `ProjectRepoIn`. The forge pair is always sent, null off the picker: a known host derives its forge, and a
declaration left behind by a URL edit must not outlive the host it was made for. */
function repoBody(repo: RepoDraft): Record<string, unknown> {
  const declared = forgePickerShown(repo.repo_url) && repo.forge === 'gitlab'
  return {
    ...(repo.id !== undefined ? { id: repo.id } : {}),
    repo_url: repo.repo_url.trim(),
    forge: declared ? 'gitlab' : null,
    forge_base_url: declared ? strOrNull(repo.forge_base_url) : null,
    integration_branch: repo.integration_branch.trim(),
    is_primary: repo.is_primary,
  }
}

/** The rows that are repositories: a blank URL is none, and is neither checked nor sent; a new one is one. */
export function filledRepos(repos: readonly RepoDraft[]): RepoDraft[] {
  return repos.filter(r => r.create !== undefined || r.repo_url.trim() !== '')
}

/* Does the draft work a repository — at least one row with a URL? The project has one exactly when it has a
 * repository row (`Project.has_repo`). `null` repos (a cached row that predates them) reads as yes: nothing here
 * can say otherwise, and the stored git fields stay shown and sent. */
export function formHasRepo(form: ProjectFormState): boolean {
  return form.repos === null || filledRepos(form.repos).length > 0
}

/* What a project with no repository does not use: the form hides these and `buildBody` omits them, never
 * clears them (the API ignores the git ones on a project with no repository — `routes/projects.py::_REPO_ONLY_FIELDS`).
 * Wider than the API's set: the port fields size a git worktree's stack, which a folder slot does not run.
 * Also read by the field-error mapping, so a refusal on a hidden field focuses nothing. */
const REPO_ONLY_KEYS: ReadonlySet<string> = new Set([
  'repo_url', 'forge', 'forge_base_url', 'integration_branch', 'branch_prefix', 'push_enabled',
  'gate_quick_cmd', 'gate_full_cmd', 'stack_up_cmd', 'stack_down_cmd',
  'port_vars', 'port_block', 'merge_lane_batch_max',
])

/** Whether this form hides `key` — one predicate for what renders, what is checked and what is sent. */
export function isHidden(form: ProjectFormState, key: string): boolean {
  return !formHasRepo(form) && REPO_ONLY_KEYS.has(key)
}

/** A validation 422's first refused body field, as its `loc` (`['body', field, …]`); undefined otherwise. */
export function refusedBodyLoc(err: unknown): unknown[] | undefined {
  if (!(err instanceof ApiError && Array.isArray(err.detail))) return undefined
  return err.detail.find((item: { loc?: unknown[] }) => Array.isArray(item.loc) && item.loc[0] === 'body')?.loc
}

function defaultAgentBody(form: ProjectFormState): Record<string, unknown> {
  if (form.default_agent_id) return { default_agent_id: form.default_agent_id }
  return form.default_provider ? { default_provider: form.default_provider } : { default_agent_id: null, default_provider: null }
}

// Shared body builder for POST /api/projects and PUT /api/projects/{id}
// (ProjectCreate / ProjectUpdate mirror each other). `includeGovernance` has no default: a new submit path
// must state which it is.
export function buildBody(form: ProjectFormState, includeGovernance: boolean): Record<string, unknown> {
  const portVars = form.port_vars.split(',').map(s => s.trim()).filter(Boolean)
  const techStack = form.tech_stack
    .map(t => ({ layer: t.layer.trim(), technology: t.technology.trim() }))
    .filter(t => t.layer || t.technology)
  const body: Record<string, unknown> = {
    name: form.name.trim(),
    /* The whole set, never the legacy `repo_url`/`integration_branch` beside it (`_split_repos` refuses the pair).
    Blank rows dropped, the primary kept on what remains; `[]` removes every repository. */
    ...(form.repos !== null ? { repos: withPrimary(filledRepos(form.repos)).map(repoBody) } : {}),
    default_model: strOrNull(form.default_model),
    /* Edit only: `ProjectCreate` has no such field; any `projects:edit` holder sets it. Empty is an explicit null:
    the default. */
    ...(form.mode === 'edit' ? { merge_lane_batch_max: form.merge_lane_batch_max.trim() ? Number(form.merge_lane_batch_max) : null } : {}),
    /* Edit only, as `merge_lane_batch_max`. One half per write (`_DEFAULT_AGENT_OR_PROVIDER`): an agent sends
    `default_agent_id` (the server clears the provider); otherwise `default_provider`, whose explicit null clears
    both a stored provider and — when an agent was the default — that agent. */
    ...(form.mode === 'edit' ? defaultAgentBody(form) : {}),
    /* Edit only, as the default agent; empty is an explicit null: the org default. */
    ...(form.mode === 'edit' ? { default_persona: strOrNull(form.default_persona) } : {}),
    port_vars: portVars.length ? portVars : null,
    port_block: form.port_block.trim() ? Number(form.port_block) : null,
    ...(includeGovernance ? governanceBody(form) : {}),
    /* Only where accepted, via `strOrNull`: `''` ("— None —") must go as an explicit null to clear the column
    (an omitted key changes nothing). Spread, since the API tests `field in payload`. */
    ...(namesHumans(form)
      ? {
        business_analyst_id: strOrNull(form.business_analyst_id),
        architect_id: strOrNull(form.architect_id),
      }
      : {}),
    /* On all three paths, never omitted: the explicit `null` is what clears it on a PUT. Not governance: any
    `projects:edit` holder sets it, bounded by the credential's read visibility (`_validated_jira_credential`). */
    jira_credential_id: form.jira_credential_id,
    tech_stack: techStack.length ? techStack : null,
    notes: strOrNull(form.notes),
    /* On all three paths, deliberately outside `governanceBody`: that fragment is dropped for a
    non-superadmin, while any `projects:edit` holder may write this. `strOrNull` so an emptied box clears
    the column. */
    agent_instructions: strOrNull(form.agent_instructions),
  }
  for (const key of Object.keys(body)) if (isHidden(form, key)) delete body[key]
  return body
}

/** The server's verdict on this viewer's authority over the seven — `GET /api/projects/{id}/
 *  governance-authority`, or the id-less twin for create (`app/routes/projects.py::GovernanceAuthorityOut`).
 *  One boolean: one server check covers all seven. */
export type GovernanceAuthority = { may_author_governance: boolean }

/* May this form author the seven? One predicate for what `buildBody` sends and what renders.
 * It reads the server's verdict rather than mirroring the rule: the API also requires a write object
 * grant on a node in the org (`_has_authority_to_author_governance`, app/routes/projects.py), a DB fact
 * the browser cannot see, so a mirror gave some org-admins a live tab and a 403 on Save.
 * `undefined` (pending or failed; a `projects:view`-only viewer is refused) reads as locked, never yes:
 * locked fields are unchanged, so omitting them saves exactly as an ordinary holder would. The notice is
 * drawn only on a real `false`.
 * Presentation only; the PUT is the boundary (`components/RequireFeature.tsx`). `form` stays in the
 * signature for a future mode with its own answer. */
export function governanceIsReadOnly(
  form: ProjectFormState,
  /** The verdict, or `undefined` while unanswered. Create asks the id-less route: the guard admits a create
   *  only if the caller administers a node in their org. */
  mayAuthorGovernance: boolean | undefined,
): boolean {
  void form
  return mayAuthorGovernance !== true
}
