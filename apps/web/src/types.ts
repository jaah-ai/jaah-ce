// The Task `state` vocabulary. `KnownTaskState` is GENERATED from `apps/api/app/task_lifecycle.py`, the
// single home of the vocabulary and the transition table. `TaskState` is the FIELD type and is closed.
// `tasks.state` is unconstrained `Text` server-side, and historical values outside the vocabulary are
// still rendered — through the retired-labels path typed on `string` (`stateLabel`/`statusForState`,
// components/tasks/shared.tsx), which is what lets this field type stay closed.
export type { KnownTaskState } from './generated/taskTransitions'
import type { KnownTaskState } from './generated/taskTransitions'
import type { NodeMicrovmDetail, OrgMicrovm, ProjectMicrovm } from './lib/microvm'
import type { NodeOs } from './lib/nodeOs'
import type { RosterSwitch } from './lib/planSwitches'

export type TaskState = KnownTaskState

/* One slot's repositories (`routes/portfolio.py::WorktreeOut.repos`). `reason` is open: a newer node's word is
   shown raw. */
export type SlotRepos = {
  primary_repo_id?: number | null
  repos: { id: number; path: string; repo_path: string; base: string; primary: boolean }[]
  kept: { id: number; path: string; repo_path: string; reason: string }[]
  failed: { id: number; repo_path: string; reason: string; detail?: string }[]
}

export type Worktree = {
  // A uuid: never key on it arithmetically or compare it with a number.
  id: string
  /* The assignment model's three facts (`routes/portfolio.py::WorktreeOut`): `static` is held at the
     assignment's count, `dynamic` is created for a task and destroyed once freed; `retiring` once its destroy is
     ordered; `destroy_error` the node's refusal. Optional: a cached roster can predate them. */
  kind?: 'static' | 'dynamic'
  retiring?: boolean
  // When the destroy was ordered (`WorktreeOut.retiring_since`); null unless `retiring`.
  retiring_since?: string | null
  destroy_error?: string | null
  // The node's redacted words on a destroy it refused, failed or parked (`slot_allocation.destroy_node_error`).
  destroy_node_error?: string | null
  /* False: a folder slot of a project with no repository — branch, ahead, behind and dirty arrive null and
     no git column, filter or action applies (`lib/worktree.ts::isFolderSlot`). Optional: a cached roster
     can predate it, and absent reads as a git worktree. */
  has_repo?: boolean
  /* The node-sensed manifest of a multi-repo slot (`fleet_observe.ObservedSlotRepos`). Absent or null: a
     single-repo slot. A tenant reader gets every `path` as `""`. */
  repos?: SlotRepos | null
  // The node's NAME (display, and the host slot URLs are built on); `node_uuid` is the identity every API call
  // addresses the node by (`?node=`, command args).
  node_id: string
  node_uuid: string
  // `project` is the display name; `project_id` is the identity every `/api/{console,worktrees}/…` path carries.
  project: string
  project_id: number
  /** The slot's org — its project's (`routes/portfolio.py::WorktreeOut.org`); tells same-named projects apart. */
  org?: OrgRef | null
  number: number
  path: string
  branch: string | null
  // The slot's occupancy, the only occupancy fact: `free` | `task` | `user` (`apps/api/app/slot_state.py`).
  // A task-owned slot is freed only by its task ending, being deleted or reset, or an operator's
  // explicit per-slot release — never by a session or runner. Runner liveness is a different question:
  // read it off the session fields (`lib/worktree.ts`'s `hasLiveSession`), never off `state`. Test
  // occupancy with `lib/poolSlot.ts`'s `isHeld` (`state !== 'free'`), never one specific non-free value.
  state: string
  // How long the slot has been in its current `state` (the reaper's `idle_hours_expr()` input with
  // `last_seen_live`). Named for the WIRE (`holder_since`): the column is `state_since`, but
  // `app/routes/portfolio.py` still emits `holder_since=wt.state_since`. Renaming the serializer is a
  // wire-contract change left as a follow-up.
  holder_since: string | null
  // Last sighting of a live session — the stale reaper's only input. `null` until the first.
  last_seen_live: string | null
  // The id of the Task a runner is working here; a hold's human label lives in `title`.
  task_id: number | null
  // The human who owns this tree when `state === 'user'` (FK to `admin_users.id`). Exactly one of
  // `task_id`/`owner_user_id` is non-null, matching `state` (a server CHECK); never re-derived here.
  owner_user_id: string | null
  // The owner's resolved display identity, beside the raw id (never the bare uuid). `null` unless `user`.
  owner_user_email: string | null
  stack_state: string | null
  ports: Record<string, number> | null
  urls: Record<string, string> | null
  last_active: string | null
  idle_hours: number | null
  ahead: number | null
  behind: number | null
  // Uncommitted work of any kind, tracked or untracked (`apps/jaahd/reconcile/context.py::wt_dirty`, the single
  // home of the rule; one column per `apps/api/app/models/`'s `Worktree.dirty`). null = unknown, never
  // "clean". Drives Sync-All eligibility (null = not eligible; `WorktreePool.tsx::isSyncEligible`).
  // Independent of `protected`.
  dirty: boolean | null
  // Verbatim `status` from `claude agents --json` — that CLI's value set ('idle', 'busy', 'waiting' seen
  // live). NULL/'' alone means "no live session". Test with lib/worktree.ts's `hasLiveSession`.
  session_status: string | null
  session_started_at: string | null
  // The live session's model id as the node sensed it (e.g. "claude-opus-4-8"), not the canonical family
  // (`app/models/worktrees.py::Worktree.model`; unlike `agent_sessions.model`). Display via `fmtModelBrief`.
  // `account` is the claude config dir it runs under, joined to ClaudeAccount.config_dir for the
  // model-hover. NULL when no live session.
  model: string | null
  account: string | null
  /** The account's display label (`WorktreeOut.account_name`); null when unresolved or not the reader's. */
  account_name?: string | null
  // Sensed off the node's tmux window; kept for older-node compat (BA D5) and read by nothing here — the
  // Title flows CP → node (BA R2).
  window_name: string | null
  // The slot's Title, shown as is (BA R8). Resolved CP-side, never null or `""`
  // (`apps/api/app/slot_title.py::resolved_title` is the single home for an absent stored Title). Render
  // with lib/worktree.ts's resolveTitle; never compose, rewrite or default it client-side.
  title: string
  // The raw stored name, beside the resolved `title` (`WorktreeOut`,
  // apps/api/app/routes/portfolio.py). Never blank: an unrenamed slot carries `<project>/<number>`.
  // `stored_opinion` (CP-side) decides whether it out-ranks a task-composed Title. Read-only here: the
  // Title editor seeds from the resolved value (`lib/slotTitle.ts::titleEditorSeed`).
  name: string
  // Free operator prose, raw, `""` when unset; renders as itself. Edited via the same `PUT .../title`
  // (lib/slotTitle.ts), where an omitted key means "leave it alone".
  description: string
  // AC9.6's "active task", decided CP-side (`WorktreeOut.task_active`) off a LEFT JOIN on `tasks`: true
  // iff `task_id` resolves to a live-state row. State names live only in
  // `apps/api/app/task_lifecycle.py`. A dangling `task_id` reads `false`. Gates the Title's task deep
  // link (BA R9).
  task_active: boolean
  // True iff the holding task is parked on a human answer — decided CP-side like `task_active`. The one
  // fact that separates a live runner nobody answered from a dead one: `session_status` shows `waiting`
  // for both and `idle_hours` grows either way. Dangling `task_id` reads `false`.
  task_awaiting_answer: boolean
  // The slot-scope half of "may I act here". Server-computed per row
  // (`apps/api/app/authz/visibility.py::worktree_editable_clause`, the single home); the browser lacks the inputs.
  // Key-blind: every control must AND it with its route's key predicate (`hooks/useWorktreeAccess.ts`:
  // the command bus takes `worktrees:edit` OR `system:create`; the console relay and `PUT …/title` take
  // `worktrees:edit`). Visible ⊃ editable: pool administrators see every slot, so `false` on screen is
  // ordinary. Neither half is the security boundary; the routes refuse on their own.
  may_edit: boolean
  // The provenance quartet (created/updated at and by). `updated_at` replaces the renamed `ingested_at` (same fact).
  // `Project`, `Rule` and `RuleGroup` keep `ingested_at`; the two names are not interchangeable.
  created_at: string
  updated_at: string
  // The actor's name, resolved CP-side at read time (`apps/api/app/actors.py`) — never a stored copy or
  // a bare uuid. Never null: an FK to a seeded system actor.
  created_by: string
  updated_by: string
  protected: boolean
  // When the CP last sensed the slot present. `null` = declared but not yet built — the only
  // field that distinguishes not-built from not-probed.
  observed_at: string | null
}

/* `POST /api/worktrees/{project}/{number}/erase` → 200 `SlotDeleteOut` (apps/api/app/routes/pool_slots.py);
   `erase` kept for external clients, the operator's verb is Delete. A result, never a refusal: no
   guard or `force` flag — the node erases whatever the tree holds. A node it cannot reach keeps the
   directory, reported as `node_erased: false` with a reason
   (`delete_one_slot`, step 4). Every 2xx is a success, but `node_erased: false` owes the operator a
   warning: the declaration is gone while the directory remains on that host. */
export type SlotDeleteResult = {
  // Always `true` on a 2xx; stated on the wire.
  row_deleted: boolean
  // Did the node confirm the directory is gone? `false` if it refused, failed, was offline, never
  // answered, or had no derivable clone path.
  node_erased: boolean
  // Why the directory survives, in the node's words — names the host carrying an orphan.
  node_detail: string | null
  // What was inside ("task <id>", "user <uuid>"), or `null`. Present means this Delete ended a live
  // runner or session (ledger closed, a durable `release_slot` intent minted) — tell the operator.
  holder_ended: string | null
}

// One node's view of an account. A login is per (account, node); an account absent from a node's list
// is simply not logged in there — a normal state.
export type AccountNode = {
  // The node's uuid — what a login's `node_id`, `?node=` and deprovision address. `node_name` is shown;
  // null only for a node that no longer exists.
  node_id: string
  node_name?: string | null
  config_dir: string | null
  quota: QuotaData | null
  observed_at: string | null
  // Does this node hold a session? The list covers the whole roster, so a missing node appears with
  // `present: false` (the actionable case). Never inferred from a null quota.
  present?: boolean
  // The node's reason a cell is bare (`extra.status`, `account_fold.account_node_records`).
  // `no_credentials` = no OAuth session in the dir (a first login, not a re-login). Null is NOT healthy —
  // see the Accounts table's State cell three-way split.
  status?: string | null
  // Why this node's last pass read no quota (`extra.no_reading_reason`, jaahd `collector.NO_READING_*`):
  // 'refresh_locked' | 'refresh_failed' | 'usage_failed'. Null = no reason given (not probed yet, or an
  // older node). Names which `no reading` it is; never makes a cell healthy.
  no_reading_reason?: string | null
  // When this node's Claude login dies outright (ISO-8601): the refresh token's fixed lifetime, which only a
  // re-login renews (`account_fold.account_node_records`). Null/absent = no expiry reported.
  refresh_expires_at?: string | null
  // The NODE's facts that decide whether it can serve the account at all (`account_fold._serving_facts`).
  // `nodelink_enabled`: the node reports a mounted token folder (`nodes.shared_accounts_root`); unread here.
  nodelink_enabled?: boolean
  // 'host' = a Multi-Node Host, which runs no work; 'co_located' = a node carried by the host `hosted_on`.
  hosting?: 'host' | 'co_located' | null
  hosted_on?: string | null
  // A node that runs work and has never published a successful probe of any account — a new node still coming up,
  // so its bare cells say nothing about the account (`account_fold._serving_facts`).
  provisioning?: boolean
  // Where this node reads the token: 'shared' (a mounted token-folder NodeLink) or 'local' (its own disk). Null/absent:
  // an older node that does not say — no label, never a guess (`account_fold.account_node_records`).
  token_source?: 'shared' | 'local' | null
  // After a login, a node on the same token-folder NodeLink that has not reported the token yet
  // (`app/account_sharing.py`): 'syncing' for 2 probe intervals, then 'not_synced' with `sync_reason`. Null otherwise.
  sync?: 'syncing' | 'not_synced' | null
  sync_reason?: string | null
}

// A token-folder NodeLink an account is shared through (`ClaudeAccount.shared_via`).
export type SharedVia = { id: string; name: string }

/* How a runner account authenticates:
 *   'oauth'   — a Claude subscription login: a `config_dir` and session per node, and a plan quota.
 *   'api_key' — an Anthropic API key in the credential broker: no login, node or dir, and no quota at
 *               all (per-token billing) — a fact about the kind, which quota surfaces must render
 *               (`Accounts.tsx`'s `NoQuotaCell`).
 * Fixed at create: `PUT /api/accounts/{id}` 422s a change. */
export type AccountAuthKind = 'oauth' | 'api_key'

export type ClaudeAccount = {
  /* No `auth_kind` here: `GET /api/resources` never sends it (`fleet_observe.ObserveAccountIn`). Read it only
     from `/api/accounts` (`Account.auth_kind`); an optional field here would invite reading `undefined`
     as evidence. */
  email: string | null
  display_name: string | null
  org: string | null
  tier: string | null
  // Host-probe status, plus two synthesized values: `'provisioning'` (a slug mid-login) and
  // `'no_credentials'` (a roster row with no probeable credential dir). Both render; neither has
  // identity or quota.
  status: string
  quota: QuotaData | null
  /* The node's word on whether this account has a quota to read at all (`account_fold.QUOTA_MODEL_KEY`):
     `'none'` is structural (a NodeLink account whose CLI may not run on the share, an API-key login, a login
     with no plan project), rendered "n/a" — never the dash's "not read yet". Absent/`'readable'`: normal. */
  quota_model?: string | null
  // Absolute path of the claude config dir the node reported probing — a sensed observation and the
  // join key to a worktree's `account` (model-hover). Not the dropped `accounts.config_dir` column,
  // which was one global answer to a per-node question; `Account` below has no such field.
  config_dir?: string
  // Registry slug (falls back to the config_dir basename) — the Accounts page's first column and the
  // source of the next acctN pre-fill.
  slug?: string
  // The `accounts` row id from GET /api/resources' join (normalized config_dir, then slug). Absent = no
  // row yet, so id-addressed writes are disabled with an explanatory title. A uuid: never do
  // arithmetic or `Number(...)` on it (`PUT /api/accounts/NaN`), and never show it (`Account.name` is).
  // Test absence with `== null`.
  account_id?: string | null
  /** `accounts.name`, the account's label; null for a sensed account with no roster row. */
  name?: string | null
  /** Whose personal account this is (`routes/portfolio.py`), even when its record is not this reader's to open;
      null for an org account or an unregistered login. */
  owner?: string | null
  /** The account's tenant (`routes/portfolio.py`; `org` above is the login's sensed organization); the one source for
      a row with no `GET /api/accounts` record — another org's account shared with the orgs an operator's filter
      selects. Null for an unregistered login. */
  tenant_org?: OrgRef | null
  /** `accounts.provider` of the joined roster row (`routes/portfolio.py`), for the same record-less shared row. */
  provider?: string | null
  // Pinned node's uuid; null/absent = unpinned. Not rendered, but seeds the re-login picker's default.
  node_id?: string | null
  // Per-node state, node-ordered — answers "which node still needs a login", which `node_id` cannot.
  nodes?: AccountNode[]
  // The token-folder NodeLinks at least one node reads this account's token from (`app/account_sharing.py`), by
  // name; empty when every node is local or silent.
  shared_via?: SharedVia[]
  // This account's live counts; `runners` is the fleet-held subset (unlike the global
  // ClaudeResources.runners).
  sessions?: { total: number; busy: number; idle: number }
  runners?: number
  // Live worktrees this account is active on (sessions-hover).
  worktrees?: AccountWorktree[]
  // Registry-projected lifecycle. `enabled` gates runner-launch selection (disabled rows render grey).
  enabled: boolean
  added_at?: string
}

// A record from GET /api/accounts/provisioning — the pollable add-account wizard state per slug.
export type ProvisioningRecord = {
  slug: string
  config_dir: string
  step: string
  /* The authorize URL, as ciphertext the control plane relays, sealed to this page's key (`peer_key`):
   * the URL exists in the clear only in this browser (`lib/loginSeal.ts`). Present ⇒
   * `step === 'awaiting_code'`. */
  sealed_url?: string
  /* The node's per-login public key, the one the pasted code is sealed to (`sealed_code`); rides with
   * `sealed_url`. */
  seal_key?: string
  /* The one-time code of a device-authorization login (RFC 8628): the operator types it into the page
   * the unsealed URL opens. Its absence is the flow discriminator the wizard renders on (a paste-code
   * login has none); optional/nullable because it appears only once the CLI prints it — a wait, not a
   * codeless flow. Not a secret: worthless without the login process's device code. */
  user_code?: string | null
  status: string
  error?: string
  started_at?: string
  // The node this flow runs on. A slug can have wizards on several nodes, so the poll must match both
  // or it can pair one node's URL with another's code.
  node_id?: string
}

export type AccountWorktree = {
  project: string
  number: number | null
  branch: string | null
  state: string
  session_status: string | null
}

export type QuotaData = {
  // Whether the collector can read this account; absent means fine:
  //   'auth_rejected'   — token rejected (401/402/403); percents NULL, UI shows a "re-login" badge.
  //   'refresh_blocked' — refresh refused unrecoverably; the account is fine, our reader is broken. Last
  //                       percents are still carried, shown dated with a badge rather than blank.
  status?: string
  // Why, only with 'auth_rejected': 'token' (re-login fixes it), 'billing' (402, payment), 'seat' (403,
  // an admin), 'unreadable' (the node read no credential at all — nothing was rejected). May be absent
  // (older node, fleet-wide fold) — fall back to cause-agnostic re-login wording. Values: the collector's
  // `_REJECTED_*`.
  authRejectedReason?: string | null
  // With 'unreadable' only: where the node looked for the credential (jaahd `collector.CREDENTIAL_PATH_KEY`).
  credentialPath?: string | null
  // When these percents were read (ISO-8601). Cached readings keep the original stamp, so age is real.
  observedAt?: string | null
  fiveHourPercent: number | null
  fiveHourResetsAt: string | null
  weeklyPercent: number | null
  weeklyResetsAt: string | null
  sonnetWeeklyPercent: number | null
  sonnetWeeklyResetsAt: string | null
  // The org's extra usage-based monthly USD cap, applying only after plan quotas run out — orthogonal
  // to the windows above (100 here can coexist with plenty of 5h/weekly quota).
  overQuotaSpendPercent: number | null
}

export type ClaudeResources = {
  accounts: ClaudeAccount[]
  sessions: { total: number; busy: number; idle: number }
  runners: number
  // Counted, never named (`routes/portfolio.py::get_resources`): accounts the caller may not see
  // (accounts are deny-by-default). Lets the Runners tab's empty state tell "none exist" from "none shared
  // with you". Always sent; optional only for older fixtures.
  hidden_account_count?: number
}

// ── Fleet credential-pool accounts (DB-SSOT provisioning) ──
// The write shape of POST /api/accounts (record-only create pinning `node_id`) and the read shape of
// GET /api/accounts; the table renders the joined `ClaudeAccount`, whose `account_id` points here.
// PUT edits name/description and enable/disable; POST …/deprovision removes. `slug` is the unique
// registry name. The *Percent fields are a read-only quota mirror. `node_id` is the pinned node's uuid, optional;
// `node_name` its display name.
// No `config_dir`: both bodies are `extra='forbid'`, so sending it is a 422. The path is derived per node
// from `{jaah_home}/auth/{slug}`; `ClaudeAccount.config_dir` (what a node probed) is a different fact.
/* How a login completes — the closed vocabulary of `ProviderChoice.login_completion`, spelled again
 * across the language boundary; authority `apps/jaahd/agent_providers.py` (`PASTE_CODE` /
 * `AWAIT_CREDENTIALS`), which says why it is declared, not inferred. Add a third mode there and here
 * together. Readers treat "not `paste_code`" as await-mode (the wait keeps running rather than asking for
 * a code nobody will get). */
export const PASTE_CODE = 'paste_code'
export const AWAIT_CREDENTIALS = 'await_credentials'
export type LoginCompletion = typeof PASTE_CODE | typeof AWAIT_CREDENTIALS

/* One itemized loss relative to Claude Code (`agent_providers.Degradation`). Rendered, not summarized:
 * `capability` is the heading, `consequence` the prose. `key` is a stable list key, never shown. */
export type ProviderDegradation = {
  key: string
  capability: string
  consequence: string
  /** A gap in the fleet's own harness (guards, hooks, subagents), not the vendor's product. */
  harness?: boolean
}

/* ── A provider's typed capability groups (`agent_providers.Capabilities`, architecture §3.4) ──
 * Served on the catalogue row as `capabilities`, field names as the dataclass. Every group is optional: an older
 * server serves none, and the Providers page renders an absent one as "—". */
export type RuntimeMode = 'approval-required' | 'auto-accept-edits' | 'auto' | 'full-access' | 'plan'

export type ProviderCapabilities = {
  /** Per auth kind: `acp` (switchable), `legacy_only` (always the terminal path) or `acp_only` (always ACP); the
   *  last two have no switch. */
  runtime?: Partial<Record<AccountAuthKind, 'acp' | 'legacy_only' | 'acp_only'>>
  sessions?: { assign_id: boolean; resume: 'resume' | 'load' | 'none'; list: boolean; close: boolean; additional_dirs: boolean }
  turns?: { steer: boolean; interrupt: boolean }
  requests?: { approval: boolean; user_input: 'elicitation' | 'mcp_tool' | 'command' | null }
  /** `native` ⊆ `supported`; a supported mode not native is "not enforced natively". */
  runtime_modes?: { supported: RuntimeMode[]; native: RuntimeMode[] }
  auth?: { kinds: string[]; key_delivery: string; key_in_env: false }
  usage?: { tokens: boolean; cost: boolean; rate_limit_reset: boolean }
  console?: { chat_view: boolean; terminal_view: boolean; toggle: boolean; reason?: string }
  gaps?: ProviderDegradation[]
}

/* One agent CLI an account can be registered against (`GET /api/accounts/providers`, Claude first;
 * `AgentProviderOut` in `apps/api/app/routes/accounts.py`). Also the picker's option type, so every
 * question about one option is answered in one place (as `NodeChoice` in Accounts.tsx). Omits dispatch
 * facts (`max_accounts_per_node`, `can_interview`, `mcp_tool_prefix`); `has_quota_model` is included for
 * the roster (PRD R5.3). */
export type ProviderChoice = {
  /** The DB value (`accounts.provider`, `args.provider`). */
  value: string
  /** Display label, from the server. */
  label: string
  /** Executable name on a node's PATH, compared against `NodeRecord.runner_clis`. */
  binary: string
  /* True ⇒ the credential is node-local (never on the NodeLink store), so the account is pinned to
     its login node. */
  node_pinned: boolean
  /* True ⇒ the login runs `binary` on the chosen node, so only nodes attesting it are offered. The add
     wizard's node filter keys on this, not on `value === 'claude'`. */
  needs_attested_binary: boolean
  /** How the login finishes (`LoginCompletion`) — declared by the vendor, Claude included. */
  login_completion: LoginCompletion
  /* The vendor's authorize origin the browser anchors the unsealed URL to (`agent_providers.
     authorize_url_prefix`); the control plane cannot anchor a URL it never sees. Null for Claude ⇒
     `safeHref` alone. */
  authorize_url_prefix: string | null
  /* True ⇒ the unsealed URL must EQUAL `authorize_url_prefix`, not merely start with it (a vendor whose
     authorize page takes no query: a `?user_code=` tail is a lure, refused). */
  authorize_url_exact: boolean
  /* False ⇒ the vendor publishes no quota, so no account can ever have a percentage — rendered as "not
     available", neither blank nor zero (PRD R5.3). Read by the accounts roster, hence fetched at page
     level too. */
  has_quota_model: boolean
  /* The quota cells' labels, short window first (`agent_providers.AgentProvider.quota_windows`): `[0]` names
     `fiveHourPercent`'s window, `[1]` `weeklyPercent`'s. One label ⇒ no weekly window, rendered "n/a". */
  quota_windows: string[]
  /** Losses relative to Claude Code, in page order; empty for Claude (the baseline). */
  degradations: ProviderDegradation[]
  /* The auth kinds an account of this provider may declare (`AgentProvider.auth_kinds`). Exactly
     `['api_key']` ⇒ Add account skips the login wizard for the key form (no login, no paste-code step). */
  auth_kinds: AccountAuthKind[]
  /** The kind Add account starts on for the caller's org (`tenant_defaults.default_auth_kind`: OAuth
   *  wherever allowed, in any org). Optional for fixtures; absent falls back to the first offered kind. */
  default_auth_kind?: AccountAuthKind
  /** The subset of `auth_kinds` the caller's org may add (its provider grid, `app/provider_auth_policy.py`); empty = the provider
   *  is not on the plan. Absent (an older server) = every supported kind. */
  allowed_auth_kinds?: AccountAuthKind[]
  /** True ⇒ each account carries its own runner cap (`Account.max_runners`), offered on create and edit. */
  per_account_runner_cap: boolean
  /** Where a new account's Auto-assign box starts (`Account.auto_assign`). */
  auto_assign_default: boolean
  /** Typed capability groups (`ProviderCapabilities`); absent on an older server. */
  capabilities?: ProviderCapabilities
}

export type Account = Provenance & {
  // A uuid path segment and nothing else — never shown or used in arithmetic (see `ClaudeAccount.account_id`).
  id: string
  slug: string
  /* The vendor CLI this credential logs into (`claude` for pre-column accounts). Immutable, like `slug`:
     served, never accepted on the PUT. */
  provider: string
  /* NOT NULL (0109 backfilled `COALESCE(name, slug)`; `create_account` defaults the same). The operator's
     display label — the only human-readable identity here. */
  name: string
  /* NOT NULL with a `''` default; screens still handle the empty case. */
  description: string
  node_id: string | null
  node_name?: string | null
  enabled: boolean
  /* See `AccountAuthKind`. Non-optional: guessing 'oauth' would offer an API-key account a re-login, node
     and quota it lacks. */
  auth_kind: AccountAuthKind
  /* Whether a key is stored — the only thing the API says about it (never returned or masked). Lets the
     edit form offer "Leave blank to keep the stored value." (as `CredentialEditPage` does). Always false
     for 'oauth'. */
  api_key_set: boolean
  /* The credential-broker row holding the key, or null — a handle, not a secret; not put into URLs here. */
  credential_id: string | null
  /* Auto-assign: may the scheduler pick this account for a task that allows any provider? Off ⇒ only
     tasks (and consoles) that name this account's provider run on it. */
  auto_assign: boolean
  /** This account's own concurrent-runner cap, or null for the fleet limit (`max_runners_per_account`). */
  max_runners: number | null
  five_hour_percent: number | null
  weekly_percent: number | null
  over_quota_spend_percent: number | null
  /** The standing break verdict (`auth_rejected`, `usage_limit`) and the runner's sentence behind it; both null
   *  once it lapses (`AccountOut.quota_status_detail`). */
  quota_status?: string | null
  quota_status_detail?: string | null
  /** When a node that held its session was deleted (`AccountOut.node_removed_at`); null for none. */
  node_removed_at?: string | null
  /** The owning tenant (`AccountOut.org`); `null` = unresolved. */
  org?: OrgRef | null
}

// One Tech Stack entry; the API enforces (layer, technology) uniqueness on write
// (apps/api/app/project_validation.py's has_duplicate_tech_stack_entry).
export type TechStackEntry = {
  layer: string
  technology: string
}

// Mirrors ProjectOut (apps/api/app/routes/projects.py). `port_vars` is a list of bare env-var names
// (e.g. ["FRONTEND_PORT", "API_PORT"]), not a KEY=NUM mapping.
// The governance fields at the bottom are non-nullable (NOT NULL, migrations 0102 and 0111): each is the
// only source for a decision a runner cannot decline (branch, prefix, gate command, stack command), and
// resolution is live API → on-disk cache → fail closed — so no `?? 'develop'` fallbacks. `access` is gone;
// its tiers are `branch_prefix` + `push_enabled`.
/* The org a row belongs to, mirrored from `apps/api/app/tenancy/labels.py::OrgRef` (the single home for
 * what the fields are for): `slug` is drawn, `name` is said, `id` is never drawn. Only
 * `ui/OrgIndicator.tsx` renders it.
 * Nullable everywhere as a real state — not because the column is (`org_id NOT NULL` on all tenant-owned
 * tables, and the seam enforces): a row written by a caller with no org (the agent plane) or under the
 * operator sentinel scope has no tenant to name. Never substitute the default org. */
export type OrgRef = {
  id: string
  slug: string
  name: string
}

/** `OrgRef` plus the viewer's OWN org's lifecycle and plan, on `/api/auth/me` only (`routes/auth.py::MeOrgRef`).
 *  Never draw `lifecycle` raw: read it through `lib/orgLifecycle.ts`. The plan fields are optional for fixtures
 *  and cached payloads; absent reads as "never ends", "not ended" and "nothing pending". */
export type MeOrgRef = OrgRef & {
  lifecycle: 'pending' | 'active' | 'rejected'
  /** When the org's plan ends; null = never. Read it through `components/PlanEndedBanner.tsx`. */
  plan_ends_at?: string | null
  /** The plan's end has passed: the API holds new work until the plan changes or its end moves. */
  plan_ended?: boolean
  /** A priced plan chosen at sign-up, awaiting checkout (the Billing page's "Complete checkout for …"). */
  pending_plan_key?: string | null
  pending_plan_name?: string | null
}

/** The forges a repo can live on (`apps/api/app/forges.py`). */
export type Forge = 'github' | 'gitlab' | 'bitbucket' | 'azure_devops'

/** The one display name per forge in the web app. */
export const FORGE_LABELS: Record<Forge, string> = {
  github: 'GitHub',
  gitlab: 'GitLab',
  bitbucket: 'Bitbucket',
  azure_devops: 'Azure DevOps',
}

/** One repository connected to a project (`routes/projects.py::ProjectRepoOut`). */
export type ProjectRepo = {
  id: number
  repo_url: string
  forge: Forge | null
  forge_base_url: string | null
  integration_branch: string
  is_primary: boolean
  /** `owner/name`. */
  name: string
  /** Its forge page; link this, never `repo_url`. */
  web_url: string | null
}

export type Project = ProjectMicrovm & {
  /** The owning tenant (`ui/OrgIndicator.tsx`). Null = not bound; see `OrgRef`. */
  org: OrgRef | null
  id: number
  name: string
  /** Set on the per-org built-in project (`'nodes'`: node automation tasks; never deleted, no repository).
      Null/absent on every ordinary project (`ui/MarkChip.tsx::SystemChip` marks it). */
  system_kind?: string | null
  /** Read-only, derived: true when the project has a repository connected (at least one `repos` row). False: tasks
      run in an empty folder slot and the git fields are hidden, kept unchanged in the row. Optional: a cached
      roster can predate it, and absent reads as true. */
  has_repo?: boolean
  repo_url: string | null
  /** Derived server-side from `repo_url`'s host, or the self-managed GitLab declaration. Null = the URL is
      missing or unparseable (the edit page asks for one). */
  forge: Forge | null
  /** Self-managed GitLab installed under a sub-path only; null otherwise. */
  forge_base_url: string | null
  /** The repo's web page, derived server-side — link this, never `repo_url` (which may be an ssh remote). */
  web_url: string | null
  /** Every connected repository (`routes/projects.py::ProjectRepoOut`); `repo_url`/`forge`/
      `forge_base_url`/`integration_branch` above are the primary's. Optional: a cached roster can predate it. */
  repos?: ProjectRepo[]
  /** A model TIER id from `GET /api/tasks/models`, or null for no project default. */
  default_model: string | null
  /** The project's default agent — the agent picker's two halves, at most one set (`ProjectUpdate`). An
      unpicked task runs it. Optional for older fixtures. */
  default_agent_id?: string | null
  default_provider?: string | null
  /** The persona this project's new tasks start as; null = the org's "Persona" default. Optional for older
      fixtures. */
  default_persona?: string | null
  /** Assigned to an enabled MicroVM node: New session opens a VM on it, repository or not. Optional: a cached
      roster can predate it. */
  on_microvm?: boolean
  port_vars: string[] | null
  port_block: number | null
  tech_stack: TechStackEntry[] | null
  notes: string | null
  /* Free text injected into the system prompt of every fleet agent dispatched here (a project-specific
     procedure, stated once). Nullable like `notes` — nothing fails closed on it. Not a governance column:
     any `projects:edit` holder writes it (`ProjectEditPage`'s Governance tab says why it renders there). */
  agent_instructions: string | null
  /* An epic's escalation path: a question inside an epic goes to the analyst (business-analysis phase)
     or the architect (every other phase and the epic row) — `app/notify_targets.py::_referred_role`; a
     standalone task's question goes to its creator, where an unset role also falls through. Nullable,
     unlike governance: "nobody named yet" is a normal, non-blocking state. UUID `users.id` strings, never
     numbers (see `ManagedUser.id`). */
  business_analyst_id: string | null
  architect_id: string | null
  /** The one branch runners rebase onto and open PRs into. No git/GitHub probe, no default anywhere. */
  integration_branch: string
  /** Stored WITH its trailing slash (`sn/`), or `''` for none. Every branch we create is `{prefix}{rest}`. */
  branch_prefix: string
  /** The always-on gate (pre-commit + Stop hook). */
  gate_quick_cmd: string
  /** The pre-PR gate. */
  gate_full_cmd: string
  /** The command that brings this project's local dev stack up (e.g. `COMPOSE_PROFILES=web ./dc up`). */
  stack_up_cmd: string
  /** The command that takes it back down. */
  stack_down_cmd: string
  /** Push allowed ⇔ the destination ref is under `branch_prefix` AND this is true. Also gates the clone's push URL. */
  push_enabled: boolean
  /** The `jira` credential for this project's Jira features, or null. The whole Jira config (site and
      optional key live on the credential). A uuid string — never `Number()` it (`NaN` clears it). */
  jira_credential_id: string | null
  /** The merge lane (`routes/projects.py::_project_out`). `merge_lane` whether it is active here; `merge_lane_reason` one line on why not, when offered;
      `merge_lane_offered` whether the org has the feature. Optional: a
      cached roster can predate them. */
  merge_lane?: boolean
  merge_lane_reason?: string | null
  merge_lane_offered?: boolean
  /** The project's own PRs-per-batch cap; null = the lane's default (`MergeLane.batch_max`). */
  merge_lane_batch_max?: number | null
  /** Whether the project's org holds Live Cast (`routes/projects.py::ProjectOut.live_cast_offered`): the edit page
      asks `GET …/live-cast` only then, since every Live Cast route 403s without it. Absent = no. */
  live_cast_offered?: boolean
  /** Why a task started here could not be placed (`routes/projects.py::ProjectOut.dispatch_block`); null = placeable. */
  dispatch_block?: string | null
  /** What `dispatch_block` lacks: `no_node` blocks every start; `no_account` only a coding agent's (an SDK agent
      needs no account). Null with a null block. */
  dispatch_block_kind?: 'no_node' | 'no_account' | null
  ingested_at: string
  updated_at: string
}

/* GET /api/tasks/tree — the task rail's one read (`apps/api/app/task_tree.py`, the pinned shape). Every node
 * carries `Counts` over the default board's top-level rows; `questions + failed + paused + waiting == stopped`,
 * `queued` is the slice of `in_progress` not yet running, and a parent's counts are the sum of its children's, so a
 * closed branch never hides a blocker. */
export type TaskTreeCounts = {
  stopped: number
  questions: number
  failed: number
  paused: number
  waiting: number
  in_progress: number
  // Absent from an older payload: read as 0 (`boardSections.ts`).
  queued?: number
  draft: number
  done: number
  total: number
}

export type TaskTreeFeature = {
  id: string
  name: string
  status: 'active' | 'shipped'
  lead_id: string
  /** `"<shape>-<colour>"` (`lib/featureIcon.ts`) or null for "No icon". */
  icon?: string | null
  /** Whether the rail draws its completion ring (`models.Feature.show_progress`, shared). */
  show_progress: boolean
  /** This user muted it (`lib/taskMute.ts::applyMutes`); set client-side, never served. */
  muted?: boolean
  shipped_at: string | null
  counts: TaskTreeCounts
}

export type TaskTreeProject = {
  id: number
  name: string
  /** `Project.system_kind`: set on the per-org built-in project; absent on an ordinary one. */
  system_kind?: string | null
  /** The project's org (`task_tree.py::ProjectNode.org`); names repeat across orgs (`lib/projectLabel.tsx`). */
  org?: OrgRef | null
  can_edit_project: boolean
  can_create_tasks: boolean
  can_edit_tasks: boolean
  features: TaskTreeFeature[]
  /** The project's "No feature" rows; null when there are none, so the rail draws no empty node. */
  unfiled: TaskTreeCounts | null
  /** Without this user's muted features' (`lib/taskMute.ts::applyMutes`). */
  counts: TaskTreeCounts
  /** This user muted it (`lib/taskMute.ts::applyMutes`); set client-side, never served. */
  muted?: boolean
}

/** `PipelineTask.stop` — the stop record (`routes/tasks/task_wire.py::StopOut`, pinned). */
export type TaskStop = {
  /** `balance` / `jaah_unavailable`: the system paused work on the Jaah account (`task_sections.StopKind`). */
  kind: 'question' | 'failed' | 'paused' | 'balance' | 'jaah_unavailable' | 'waiting_on_customer'
  text: string | null
  by: string | null
  at: string | null
  stage_task_id: number | null
  stage_persona: string | null
  stage_title: string | null
}

/** GET /api/features — one feature (`routes/features.py::FeatureOut`, the pinned shape; the fields a
 *  filing surface reads). */
export type FeatureSummary = {
  id: string
  name: string
  project_id: number
  project_name: string
  status: 'active' | 'shipped'
  /** `"<shape>-<colour>"` (`lib/featureIcon.ts`) or null for "No icon". */
  icon?: string | null
  can_edit: boolean
}

export type TaskTree = { projects: TaskTreeProject[]; counts: TaskTreeCounts; can_create_project: boolean }

/* GET /api/projects/{id}/members — everyone who can reach one project (direct grants, granted groups'
 * members, superadmins), ordered by email server-side. Not `/api/users`: that is the whole roster (it
 * would offer people who cannot open the project as its analyst/architect) and an account-admin read.
 * Deliberately thin. `first_name`/`last_name` may be null; a picker then renders the email, never an
 * invented name. */
export type ProjectMember = {
  id: string
  email: string
  first_name: string | null
  last_name: string | null
}

// ── Nodes view & edit ──

/* `nodes.infra` — free-form JSONB. The first six fields are human-authored; the rest is the launch
   record (`create_cloud_node`, `node_group_reconcile._launch_one`), which `DELETE /api/nodes/{id}` and
   `node_group_reconcile._terminate_one` read to terminate the machine. Open by design: the console must
   carry keys it does not render (`NodeDetailPage::carriedInfraOf` subtracts the six it owns), or a Save
   would erase them. The four launch keys are named for type safety and `readonly` (the API refuses them
   below superadmin). */
export type NodeInfra = {
  address?: string | null
  ssh_user?: string | null
  ssh_host?: string | null
  hardware?: string | null
  location?: string | null
  provider?: string | null
  readonly instance_id?: string | null
  readonly region?: string | null
  /** Operator only: the AWS account the instance runs in. */
  readonly account_id?: string | null
  readonly launched_by?: string | null
  readonly credential_id?: string | null
  /** The image the launch booted; absent on nodes launched before it was recorded. */
  readonly image_id?: string | null
  /** The instance's cloud (`clouds.INFRA_PROVIDER_KEY`); absent = AWS. Never `provider`, the form's free text. */
  readonly cloud_provider?: CloudProvider | null
  /** Where the instance runs beyond its region (`clouds.InstanceRef`): a zone; a GCP project or Azure `sub/group`. */
  readonly zone?: string | null
  readonly scope?: string | null
  [key: string]: unknown
}

/** What `NodeCounts.accounts` means, one home for the list and node pages (`0` must not read as "login
 *  gone"). A function because the plural marker sits mid-phrase. */
export const accountsCountHint = (n: number): string =>
  `enabled account${n === 1 ? '' : 's'} logged in on this node`

// Rollup counts (AC 1, 9). `accounts` = enabled accounts with a usable login on the node — not the ones
// in use, not a dispatch promise (`routes/nodes/reads.py::_counts_map`).
export type NodeCounts = {
  worktrees: number
  /** Leasable slots (the main clone excluded); absent from an older API. */
  task_slots?: number
  /** Slots being destroyed, included in `worktrees`; absent from an older API. */
  retiring_slots?: number
  runners: number
  accounts: number
}

/* A cloud-launched node's machine (`NodeOut.cloud`, `routes/nodes/node_wire.py::CloudOut`). Every org
   admin reads it. `actions` is the ONLY source of which verbs show (`components/CloudMachineSection.tsx`): the
   client never derives one from `state` or `provider`. Every write (`/api/nodes/{id}/cloud/...`) answers 202
   with this block. */
export type NodeCloudAction = 'stop' | 'start' | 'change_type' | 'grow_disk' | 'delete'
export type NodeCloudChange = {
  from_type: string
  to_type: string
  /* `stuck`: it stopped reporting (`cloud_lifecycle`); shown as failed, and the verbs are offered again. */
  state: 'running' | 'done' | 'failed' | 'stuck'
  error: string | null
  steps: { step: string; at: string; detail: string | null }[]
}
export type NodeCloud = {
  provider: 'aws' | 'gcp' | 'azure'
  provider_label: string
  account_noun: string
  instance_id: string
  region: string
  zone: string | null
  instance_type: string | null
  disk_gb: number | null
  /* Last known; null = unknown. `pending` is an older server's `starting`. */
  state: 'running' | 'stopping' | 'stopped' | 'starting' | 'pending' | null
  actions: NodeCloudAction[]
  /* The Change machine type picker's choices; [] when `change_type` is not offered. */
  instance_types: string[]
  change: NodeCloudChange | null
  /* The node growing its root filesystem into a grown disk, until it reports done; absent from an older server. */
  root_grow?: { size_gb: number; state: 'pending' | 'failed'; error: string | null } | null
  /* What the machine's boot log says while it has not connected (`infra.boot_report`): `stalled` when the
     reason is a cause, not progress. Absent from an older API; null until one is read. */
  boot_report?: { reason: string; at: string; stalled: boolean } | null
}
/* A refused stop or grow (409): the tasks running on the node, named so a forced stop says what it kills. */
export type NodeCloudStopRefusal = { detail: string; tasks: { id: number; title: string }[] }

// GET /api/nodes row — mirrors NodeOut (apps/api/app/routes/nodes/node_wire.py). Presence is the node's live socket
// row (`node_bus_sockets`, app/node_presence.py), shared by every control-plane task. "offline"
// greys a node's worktrees/runners but keeps their rows (D4: stale, not deleted); 'unknown' (never seen, or
// the presence registry inactive) renders distinctly and is never treated as offline.
export type NodeRecord = Provenance & {
  /* The row's uuid (see `NodeOut.id`). Null is real: `/api/nodes` unions presence-only hosts with no row,
     which no access reaches — drop a null, never fall back to the name. Use `node_id` for everything else; swapping them inverts the domain. */
  id: string | null
  // The name arrives twice by contract (0106 folded `node_id` + `display_name` into `name`): `node_id`,
  // the legacy presence key, goes in URLs and paths; `name` is shown. Neither is the uuid above.
  node_id: string
  status: 'online' | 'offline' | 'unknown'
  last_seen: string | null
  /* NOT NULL since 0106 — no `display_name || shortNode(node_id)`-style fallback. */
  name: string
  /* NOT NULL with a `''` default (0105); screens still handle empty. */
  description: string
  tags: string[]
  enabled: boolean
  /* Second half of one three-valued state: Enabled (true/false), Draining (true/true), Disabled
     (false/false). A draining node finishes its tasks, takes no new ones, and disables itself when empty.
     Required: coalescing absent to `false` would read "could not tell" as "not draining". */
  draining: boolean
  /* The operator allocated this machine to its tenant (`app/node_allocation.py`): the tenant uses it and places
     projects on it, but changes only its labels and service switches, and never deletes it. */
  operator_allocated: boolean
  /* A Jaah Cloud machine (`NodeOut.jaah_cloud`): Jaah runs it for the tenant, so like an allocated node the tenant
     changes only its labels, switches and NodeLinks (`pages/NodeDetailPage.tsx`'s `allocationLocked`). */
  jaah_cloud: boolean
  /* On a shared machine the operator runs that is not cloud-launched (`NodeOut.shared_machine`): locked like a Jaah
     Cloud machine, but worded as the operator's shared machine (`pages/NodeDetailPage.tsx`, the Nodes list's tag). */
  shared_machine: boolean
  /* The retired per-node shared-accounts switch (`nodes.shared_accounts_enabled`), still served; unread by
     the console: NodeLinks replaced it. */
  nodelink_enabled: boolean
  /* Does the node report the accounts token folder mounted (`NodeOut.accounts_nodelink`): a login on it reaches every node
     on that NodeLink. Absent from an older server: false. */
  accounts_nodelink?: boolean
  /* Agent-CLI binaries the daemon found on uid `jaah`'s PATH — what makes a provider login offerable
     here (a provider login runs its CLI on this node). `null` = never attested (old row or daemon); `[]` =
     attested, none installed. Both fail closed, but never collapse them. Sensed, not configured: the fix
     for an empty list is installing the binary. Optional in the type only (fixtures build whole
     literals); absent is "never attested", so the only allowed narrowing is `?? null` (`Accounts.tsx`'s
     `fetchNodes`). */
  runner_clis?: string[] | null
  /* Companion CLIs (executables a provider's CLI spawns, e.g. `pi` beside `pi-acp`) that are absent while their
     provider's CLI is present. Optional for an older server; absent reads `[]`. */
  missing_companions?: string[]
  /* The OS the node was CREATED as (`Node.os`, never changed) and what the machine reports (`arch`,
     `os_version`; null until it does). `unsupported` is `{capability: one-line reason}` for what that OS
     cannot do (`apps/api/app/node_platform.py`), `{}` for Linux: the console disables a control by
     reading it, never by branching on `os`. Optional in the type only (fixtures predate them); absent
     `os` reads Linux — the API's own answer for a presence-only host — and absent `unsupported` reads
     `{}`. */
  os?: NodeOs
  arch?: string | null
  os_version?: string | null
  /* A Linux node's reported distribution (`Ubuntu 26.04`, `Amazon Linux 2023`; `Linux` before it reports); null
     for any other OS. Optional for an older server; absent falls back to the OS name (`lib/nodeOs.ts`). */
  distro_label?: string | null
  unsupported?: Record<string, string>
  role: string | null
  // The node's jaah-home override (0072): root of its jaah-managed repos. null = no override, no
  // implicit default. Acted on — it lets a slot be placed on a differently-laid-out node.
  jaah_home: string | null
  infra: NodeInfra | null
  /* A cloud-launched machine's block; absent from an older API, null for any other node. */
  cloud?: NodeCloud | null
  first_seen: string | null
  /* When a never-connected node's install command fetched its bootstrap (`NodeOut.install_started_at`; null once it
     connected): `lib/nodeStatus.ts::nodeDeploy`. Optional for an older server. */
  install_started_at?: string | null
  config_reported_at: string | null
  counts: NodeCounts
  // ── Generation block: read-only and derived (NodeOut). `generation` is the node's reported release
  // (`config_snapshot->>'version'`), null if none. `generation_placement` is the server's verdict
  // (`app/node_liveness.py::place_generation`, the single home for version ordering; it zero-pads, so
  // `0.2` = `0.2.0`). Never re-derived client-side — no version parsing under src/.
  // Optional only because `NodeDetail` fixtures predate it; absent `generation_placement` reads
  // 'unknown', which renders no refresh control.
  generation?: string | null
  generation_placement?: 'current' | 'behind' | 'ahead' | 'unknown'
  // The published version compared against; null = nothing published, so nothing is `behind`.
  release_desired?: string | null
  // The release with no bundle for this node's OS (macOS/Windows); `release_desired` is then the newest one
  // published. Null when nothing is missing.
  release_missing_bundle?: string | null
  /* Auto-update (`NodeOut.autoupdate_*`): a control-plane setting, overridable per node. `enabled` is the
     EFFECTIVE value; `source` says where it came from (the node's own override, its host's, or
     Settings); `override` is this node's own (null = follows Settings); `default` is the value with no
     override of its own. Optional for cached payloads; absent `enabled` reads as on. */
  autoupdate_enabled?: boolean
  autoupdate_source?: 'node' | 'host' | 'settings'
  autoupdate_override?: boolean | null
  autoupdate_default?: boolean
  /* The node's CPU/RAM pressure (`NodeHostMetricsOut`). NULL = never reported — never an idle 0% bar
     (absence is not a state: unknown is never shown as zero). On the list model for one batched
     read (`app/node_host_metrics.py::latest_map`). */
  host_metrics: NodeHostMetrics | null
  /* The node's disk gate (`NodeOut.disk_gate`, `app/node_disk_events.py`): `paused` = no new runner or CI
     job starts here until usage drops. Null for a presence-only node; optional for cached payloads. */
  disk_gate?: NodeDiskGate | null
  /* The owning tenant (`NodeOut.org`); the roster's Org column is the operator's alone. Optional for
     cached payloads; absent reads as null. */
  org?: OrgRef | null
  /* Where the node runs (`NodeOut.hosting`): an AWS launch, a registered machine, a node co-located on a
     host (`hosted_on` names it), or the host itself — which the roster never draws as a node.
     `null` = the server cannot tell; the column stays blank. Optional for cached payloads. */
  hosting?: NodeHosting | null
  /** The host's name, for `co_located` only. */
  hosted_on?: string | null
  /* The node's NodeVault identity-key fingerprint, hex sha256 of its SPKI (`NodeOut.nodevault_identity_fp`).
     Null = no identity registered yet (an older daemon, or one that cannot run NodeVault). The roster shows
     its first 12, which `jaah nodevault fingerprint` prints on the node. Optional for cached payloads. */
  nodevault_identity_fp?: string | null
  /* Whether the node can hold NodeVault keys now (`NodeOut.nodevault_capable`) and when it last said so
     (`nodevault_capable_at`; null = not reported since that stamp existed). A fingerprint alone does not prove
     it: the key stays registered when a later report says not capable. Optional for cached payloads. */
  nodevault_capable?: boolean
  nodevault_capable_at?: string | null
}

export type NodeHosting = 'aws' | 'self_hosted' | 'co_located' | 'multi_node_host'

// GET /api/nodes/{node_id} — adds the daemon-published, allowlist-redacted config snapshot. Null = never
// reported (older daemon, AC 10): render "not reported", never an error.
export type NodeDetail = NodeRecord & NodeMicrovmDetail & {
  config_snapshot: Record<string, unknown> | null
  /* Fenced by an org move until it confirms the old org's cleanup: it takes no task meanwhile
     (`routes/nodes/node_wire.py::NodeDetailOut`). The operator may lift it (`POST .../lift-transfer-fence`). */
  org_transfer_cleanup_pending: boolean
  /* The updater's newest outcome (`NodeDetailOut.last_update`), beside the Version: why a node is behind. Null until
     the node shipped one, and for every tenant. Optional: fixtures predate it. */
  last_update?: NodeHealthUpdate | null
  /* `/dev/kvm` for this co-located node's session user (`NodeOut.session_kvm`). `session_kvm_editable` is the
     server's verdict (a node on a Multi-Node Host, read by its host's owner or the operator); false for every
     other reader, so a tenant on a shared host never learns its node is co-located. Optional: fixtures predate them. */
  session_kvm?: boolean
  session_kvm_editable?: boolean
}

// ── Node credentials ────────────────────────────────────────────────────────────────
// A node's token, served exactly once — on the 201 from `POST /api/nodes` and from
// `POST /api/nodes/{node_id}/token` (`NodeTokenIssuedOut`, apps/api/app/routes/nodes/register.py). The CP stores
// only `sha256(token)` (apps/api/app/node_credentials.py); re-issue (which revokes) is the only recovery.
// `onboarding_command` is server-generated (apps/api/app/node_bootstrap.py::render_fetch_command): one
// line that fetches the machine's bootstrap over this token into `sudo bash`. It carries the token.
export type NodeCredential = {
  node_id: string
  name: string
  token: string
  issued_at: string
  onboarding_command: string
  /* Which shell `onboarding_command` is pasted into (`node_bootstrap.FETCH_SHELLS`): `powershell` for a
     Windows node, `bash` otherwise. Optional in the type only (fixtures predate it); absent reads `bash`. */
  onboarding_shell?: 'bash' | 'powershell'
  // The headscale server `tailscale up --login-server` dials (`_tailnet_login_server`); null = no tailnet.
  tailnet_login_server: string | null
  /* Whether the line is run inside the tailnet join (`selfHostedScript`): only on a console the internet cannot
     reach (`register.py::_joins_tailnet_first`). Otherwise the bootstrap joins the tailnet itself. Absent = no. */
  onboarding_joins_tailnet?: boolean
}

/* `POST /api/nodes/{name}/tailnet-reenrol` → 201: a re-issued credential (the old token is dead) plus the
   end of the window in which the node's next bootstrap run is handed one new tailnet key. */
export type NodeReenrolled = NodeCredential & {
  tailnet_reenrol_until: string
}

/* `DELETE /api/nodes/{node_id}` → 200 with this body. Never refused: it deregisters the node, deletes its
   slot rows, ends their holders, asks the node to erase the directories (best effort), and terminates the
   EC2 instance when the CP launched it. Irreversible. None of these fields is an error channel: leftovers
   are warnings to read afterwards, not a failed request. */
export type NodeDeleteResult = {
  // Slot rows deleted with the node; `0` is ordinary.
  slots_deleted: number
  // One phrase per hold this delete ended ("task <id>", "user <uuid>") — the killed runners and sessions.
  holders_ended: string[]
  // One line per directory not erased (unreachable, wedged, refusing, no path) — an orphan on that host.
  node_erase_failures: string[]
  // `false` both for a node the CP did not launch and for a failed termination; render `instance_detail`
  // to say which.
  instance_terminated: boolean
  instance_detail: string | null
  // A host's delete with `?uninstall=true` (a self-hosted machine): whether the machine was told to uninstall Jaah,
  // and, when it was not (offline, an older daemon), why — with the command to run by hand.
  uninstall_requested?: boolean
  uninstall_detail?: string | null
}

// ── Creating a node from the control plane ──────────────────────────────────────────
// `POST /api/nodes/cloud` launches an EC2 instance; `POST /api/nodes/self-hosted` registers a machine the
// operator has. Both mint the one-time `NodeCredential` with the row.
// Cloud: the bootstrap goes to EC2 as user-data. Self-hosted: the operator runs
// `credential.onboarding_command`. Neither response carries a script.

/** An AWS credential a cloud connection may be built on (`GET /api/cloud-connections/options`).
 *  `region` is the credential's non-secret `region` config field (the registry's optional AWS field), so
 *  the connection dialog can open on it; null when the row never set one. */
export type CloudConnectionCredentialOption = {
  id: string
  name: string
  region: string | null
  /** The credential type (`aws`, `gcp`, `azure`): the dialog offers a provider only its `credential_type`'s. */
  type?: string
  /** The credential's org: the connection dialog offers an ordering org only ITS credentials. */
  org_id: string
  /** That org's name, so two same-named credentials of different orgs read apart. */
  org_name?: string | null
}

/** A connection as a LAUNCH surface offers it (the Add-node wizard, a node group, the operator's rental
 *  knob): what a picker renders ("name · AWS · region") and the credential it is keyed by. */
export type CloudConnectionOption = {
  id: string
  name: string
  provider: CloudProvider
  /** The provider's display name ("GCP") and what it calls an account ("project"), from the server. */
  provider_label: string
  account_noun: string
  region: string
  /** The org the connection belongs to: a launch picker offers only the ordering org's. */
  org_id: string
  credential_id: string
  /** JOINed; null only through the LEFT JOIN. */
  credential_name: string | null
}

/** Everything a cloud launch form may offer. An empty `instance_types` (the allow-list an operator manages in
 *  Products › Instance types, `app/instance_types.py`) is a deliberate off switch the form must state. */
export type CloudConnectionOptions = {
  instance_types: string[]
  /** The subset of `instance_types` a Windows launch may use (x86, nested virtualization or metal). */
  windows_instance_types?: string[]
  /** Every allowed type with its cloud and whether Windows runs on it: a launch picker offers a connection only
   *  its own cloud's. */
  allowed_types?: AllowedTypeOption[]
  /** What a blank `ami_id` launches ("Ubuntu 26.04 LTS"). */
  auto_image?: string
  credentials: CloudConnectionCredentialOption[]
  connections: CloudConnectionOption[]
}

/* ── Hosts (superadmin-only; wire and code name: vnode host) ──────────────────────────
   `routes/vnode_hosts.py`'s shapes. The Host badge and node count come only from these; a co-located
   node is an ordinary `NodeRecord` (its `hosting` column says where it runs, nothing more). */

/** One row of `GET /api/vnode-hosts` — every host for the operator, the caller's own org's otherwise.
 *  `host_node_id` and each `node_id` are `nodes.id` uuids. */
export type VnodeHost = {
  host_node_id: string
  /** The host's name and presence. This route is the host's only view: the roster never draws a host
   *  as a node. */
  name: string
  /** The org the host is filed under. Optional for cached payloads. */
  org?: OrgRef | null
  /** How the host itself runs; null = the server cannot tell. Optional for cached payloads. */
  hosting?: 'aws' | 'self_hosted' | null
  /** True when the host carries only its own org's nodes (a tenant host) — the server's rule, so the
   *  dialog fixes the org by it. Optional for cached payloads; absent = any org. */
  org_only?: boolean
  status: NodeRecord['status']
  last_seen: string | null
  vnode_count: number
  vnodes: { node_id: string | null; virtual_node_id: string }[]
  /** A host-level fault to act on, e.g. its AWS credential cannot list access points. */
  failure_reason?: string | null
  /** The most nodes the Shared hosting pool may place on this host; null = not offered for Shared. Only
   *  an operator host (`org_only === false`) carries one; a tenant host is always null. Optional for
   *  cached payloads. */
  shared_capacity?: number | null
}

export type VirtualNodeState = 'provisioning' | 'ready' | 'failed' | 'deleting'

/** One row of `GET /api/vnode-hosts/{host}/virtual-nodes` — a node co-located on the host.
 *  `node_id`/`node_name` are null once the node row is gone and the host is tearing it down. */
export type VirtualNode = {
  id: string
  node_id: string | null
  node_name: string | null
  org_id: string
  org_name: string | null
  slot: number
  state: VirtualNodeState
  failure_reason: string | null
  last_seen: string | null
  /* DISK AND INODE CAPS. The override is the operator's (`PATCH` below), null = the system
   * setting (`vnode_disk_cap_gb`/`vnode_inode_cap`); `effective_*` is what applies either way. Usage is the
   * host's last report, null until the first one. `over_cap` is the server's rule (`app/vnodes.py`): a
   * warning only — an over-cap node still gets work. Optional for cached payloads. */
  disk_cap_gb?: number | null
  inode_cap?: number | null
  effective_disk_cap_gb?: number
  effective_inode_cap?: number
  disk_used_kb?: number | null
  inodes_used?: number | null
  usage_measured_at?: string | null
  over_cap?: boolean
}

/** `PATCH /api/vnode-hosts/{host}/virtual-nodes/{id}` — operator only. A key left out is unchanged; null
 *  clears the override back to the system setting. */
export type VirtualNodeCapsPatch = { disk_cap_gb?: number | null; inode_cap?: number | null }

/** The cloud of a connection (`cloud_node_defaults.provider`). Only `aws` launches today: the route 422s
 *  the other two ("Only AWS is supported yet"), and the dialog greys them out. */
export type CloudProvider = 'aws' | 'gcp' | 'azure'

export type AllowedTypeOption = { instance_type: string; provider: CloudProvider; windows_capable: boolean }

/** One field of a provider's connection form (`clouds/base.py::ConnectionField`): rendered generically. `lookup`
 *  names the field whose options `GET /api/cloud-connections/lookup/{lookup}` lists (null = typed in); `parent` the
 *  field whose value narrows it; `auto_label` what blank resolves to on save. */
export type ConnectionField = {
  name: string
  label: string
  kind: 'text' | 'select' | 'multiselect' | 'int'
  required: boolean
  hint: string
  auto_label: string | null
  lookup: string | null
  parent: string | null
}

/** One cloud the caller's plan offers (`GET /api/cloud-connections/providers`), with its connection form. */
export type CloudProviderSpec = {
  key: CloudProvider
  label: string
  account_noun: string
  credential_type: string
  fields: ConnectionField[]
}

/** One cloud connection (`GET /api/cloud-connections`): WHERE a cloud node launches — a credential, a
 *  region and the network inside that account. Many connections may share a credential. The table is
 *  still `cloud_node_defaults` on the server (additive-migration rule); `routes/cloud_nodes.py` serves it. */
export type CloudConnectionRow = {
  id: string
  name: string
  description: string
  provider: CloudProvider
  credential_id: string
  /** The credential's name, JOINed. `null` is unreachable through the FK; typed only because
   *  the query is a LEFT JOIN. */
  credential_name: string | null
  region: string
  /** `''` = resolved at every launch to Canonical's newest `auto_image` for the instance's architecture;
   *  an `ami-…` pins an override. */
  ami_id: string
  /** When the pin was set; `null`/absent while `ami_id` is `''`. */
  ami_pinned_at?: string | null
  /** The pin is past the policy's 30-day re-pin, or its age is unknown (`routes/cloud_nodes.py::AMI_PIN_MAX_AGE`). */
  ami_pin_stale?: boolean
  /** What a blank `ami_id` launches ("Ubuntu 26.04 LTS"). */
  auto_image?: string
  /** Never `''` on the way back: a blank on the way IN is resolved when the connection is saved (the
   *  region's default VPC and its first default subnet; a found-or-created `jaah-node` egress-only group),
   *  so the row shows exactly what a launch uses. */
  subnet_id: string
  /** More subnets of `subnet_id`'s VPC, in order: a launch spreads across them all and falls back to the
   *  next when a zone has no capacity. `[]` = the one subnet. */
  extra_subnet_ids?: string[]
  security_group_id: string
  /** The resolved network as the provider reads it (`CloudProvider.network_of`), keyed by its connection field
   *  names: every cloud's, AWS's too. */
  network?: Record<string, unknown>
  /** Kept on the wire and in the DB, no longer offered in the form; `''` = no IAM role. */
  instance_profile: string
  root_volume_gb: number
  /** The owning tenant (`CloudConnectionOut.org`); `null` = unresolved. */
  org?: OrgRef | null
  created_at?: string | null
  updated_at?: string | null
  /** Resolved display names, never actor uuids. */
  created_by?: string | null
  updated_by?: string | null
}

/** One NodeLink storage (`StoreOut`, `GET /api/org/nodelink-stores`): many per org, each a file system in the
 *  org's own cloud account, reached under `credential_id`. Chosen per NodeLink in its Storage tab. One Jaah made
 *  and the cloud bills (`deletable_in_cloud`) stays when its last NodeLink leaves, until deleted under Storage;
 *  any other is deleted (row only) then. Ids fill in as a provision creates each object. */
export type NodeLinkStoreStatus = 'pending' | 'provisioning' | 'ready' | 'failed' | 'deleting'

export type NodeLinkStore = {
  id: string
  name: string
  /** The cloud it lives on; absent from an older server = AWS. */
  provider?: CloudProvider
  credential_id: string
  /** JOINed credential name; null when the caller cannot see it. */
  credential_name: string | null
  region: string
  subnet_id: string | null
  /** null only on an adopted store. */
  ingress_cidr: string | null
  /** `true` = "Use existing": a file system picked from the org's AWS account, never provisioned here. */
  adopted: boolean
  file_system_id: string | null
  file_system_arn: string | null
  security_group_id: string | null
  mount_target_id: string | null
  mount_target_ip: string | null
  status: NodeLinkStoreStatus
  last_error: string | null
  nodelink_count: number
  /** The cloud's own tier word ("Regional", "Zonal", "General Purpose"); null/absent when unknown. */
  tier?: string | null
  /** Provisioned (Filestore) or metered (EFS) size; null/absent when unknown. */
  capacity_gib?: number | null
  /** What this store costs ("about $115 a month"); null/absent when unknown. */
  cost_note?: string | null
  /** Jaah made it and the cloud bills it per store: Delete may also delete it in the cloud. */
  deletable_in_cloud?: boolean
  created_at?: string | null
  updated_at?: string | null
  /** When Jaah last wrote the file-system policy, and the access points it admits; null before the first write. */
  policy_written_at?: string | null
  policy_access_point_ids?: string[] | null
}

// ── Dynamic node groups ─────────────────────────────────────────────────────────────
// A desired capacity the CP holds by launching/terminating EC2 nodes on a timer — unlike
// `CloudConnectionRow` / `POST /api/nodes/cloud`, a one-off launch. The console renders the gap:
// `desired_total` vs `node_count`, when the loop last ran, and why it could not converge.
// Launch details come from the group's cloud connection (`connection_id`), which the form requires.

/** How the instance is bought. A reclaimed spot node is a gap to re-close, so markets are separate
 *  capacity lines. */
export type NodeGroupMarket = 'on_demand' | 'spot'

/** One (market, instance type) line holding `desired`. Unique on the pair server-side (a duplicate is a
 *  422; the form checks first). `node_count` shows which line is not converging. */
export type NodeGroupCapacityRow = {
  id: string
  market: NodeGroupMarket
  instance_type: string
  desired: number
  node_count: number
}

/** A project the group's nodes serve. `project_name` is denormalised. */
export type NodeGroupProjectRow = {
  id: string
  project_id: number
  project_name: string
}

/** One group as `GET /api/node-groups` returns it. */
export type NodeGroupRow = {
  id: string
  name: string
  description: string
  /** The cloud connection the group launches through (`routes/node_groups.py::NodeGroupOut`); the
   *  credential pair beside it is DERIVED from it on write and kept for the termination proof. */
  connection_id: string
  /** JOINed; null only through the LEFT JOIN, as `credential_name`. */
  connection_name: string | null
  credential_id: string
  /** JOINed; nullable for the LEFT-JOIN reason as `CloudConnectionRow.credential_name`. */
  credential_name: string | null
  /** The owning tenant of the group and every node it starts (`ui/OrgIndicator.tsx`). Both halves, as
   *  `Credential` carries them: raw `org_id` and its resolution `org` (`app/tenancy/labels.py::OrgRef`),
   *  null when dangling. Needed because the read is fleet-wide (`@superadmin_only(Tenancy.OPERATOR)`). */
  org_id: string
  org: OrgRef | null
  /** The only control here that spends money: true means the loop launches instances on its own timer. */
  enabled: boolean
  /** When the loop last considered this group. `null` = never — the loop is not running for it, which
   *  must never render like "ran and failed". */
  last_reconciled_at: string | null
  /** Why the last reconcile could not converge, the server's sentence; `''` (not null) when fine. The
   *  only symptom of a group silently launching nothing. */
  last_error: string
  capacity: NodeGroupCapacityRow[]
  projects: NodeGroupProjectRow[]
  /** The NodeLinks every node of the group is born attached to; `[]` = none. Optional: older reads lack it. */
  nodelink_ids?: string[]
  /** Server-derived totals, never re-summed here (another operator may add a line between reads). */
  desired_total: number
  node_count: number
  created_at: string | null
  updated_at: string | null
  /** Resolved display names, never actor uuids. */
  created_by: string | null
  updated_by: string | null
}

/** A project a group may target (`GET /api/node-groups/assignable-projects`). Every project is eligible:
 *  a project runs on a node through its assignment alone, nothing to enable. */
export type AssignableProject = {
  id: number
  name: string
}

// ── GitHub Actions runner pools ─────────────────────────────────
// One declarative pool per node: the CP holds the desired shape (repo, `github` credential, count,
// running or not); the node converges and alone reports what it sees (`GhaRunnerPoolOut` and its
// `observed` element). `project_id` … `state` are the operator's instruction (change on Apply);
// `phase`, `last_error`, `observed`, `observed_at` are the node's observation (its 60s pass) — never
// show the instruction as the state (`pages/nodes/ghaRunners/`).
// Nothing is collapsed: `last_error` is `''` never null, `observed` is `[]` never null, and
// `observed_at` is nullable ("never reported" is its own state).

/** What the daemon saw for one runner slot. Written only by the node's report route. */
export type GhaRunnerObserved = {
  /** Stable slot name, `gha-1` … `gha-N` — the identity an operator matches a row by. */
  slot: string
  /** Registered name at GitHub, `<node>-<slot>`. An identifier: mono, never truncated. */
  name: string
  /** The systemd unit, `actions.runner.<owner>-<repo>.<name>.service` — pasted into `systemctl status`. */
  unit: string
  /** systemd `ActiveState` as a boolean: the listener is up, not necessarily running a job. */
  active: boolean
  /** systemd `SubState` verbatim; `''` = unreadable, to be said, not rendered as a gap. */
  sub: string
  /** Registered at GitHub — a second axis (registered+paused, active+unregistered both occur). */
  registered: boolean
  /* The unit's memory knobs as systemd prints them (`OOMPolicy`, `MemoryMax`, `MemoryHigh`): a byte count,
     `infinity`, or a word. Absent from an older daemon's report. */
  oom_policy?: string
  memory_max?: string
  memory_high?: string
}

/** One job a slot ran, as the node's journal recorded it (newest first in `GhaRunnerPool.recent_jobs`).
 *  `result` is the runner's own word (`Succeeded`, `Failed`, `Canceled`, …); `null` while running or
 *  unrecorded. `shutdown_signal`: the runner logged a shutdown signal mid-job (killed, not failed). */
export type GhaRunnerJob = {
  job: string
  started_at: string
  ended_at: string | null
  result: string | null
  shutdown_signal: boolean
}

/** GitHub's view of one registered slot (`GhaRunnerActivityOut`, the API's GHA runners route):
 *  `busy` is GitHub's own Idle/Active. The job fields are `null` when idle or when the credential cannot
 *  read Actions — a busy runner with no job named is still Active. */
export type GhaRunnerActivity = {
  slot: string
  online: boolean
  busy: boolean
  job_name: string | null
  workflow_name: string | null
  job_url: string | null
}

/** The instruction. `absent` is only the server's answer (set by DELETE; the row deletes itself once the
 *  node reports zero runners). */
export type GhaRunnerPoolState = 'running' | 'paused' | 'absent'

/** Where the node stands. `pending` covers installing, draining and removing — read it with `state` and
 *  `observed`. */
export type GhaRunnerPoolPhase = 'pending' | 'converged' | 'error'

export type GhaRunnerPool = {
  /** The node name, as the route's path param spells it. */
  node: string
  /* Ids key the pickers (`ui/CredentialSelect`: `credentials.id`; project select: `projects.id`); names are
     joined server-side so renames never go stale. `projects.id` is an int and `credentials.id` a uuid —
     mirrored exactly (`GhaRunnerPoolWriteIn` declares `project_id: int`). */
  project_id: number
  project_name: string
  /** `projects.repo_url`, the registration scope — always a real URL on an existing row. */
  repo_url: string
  credential_id: string
  credential_name: string
  /** Desired runners (1…32) — the instruction; the host's count is `observed.length`. */
  count: number
  state: GhaRunnerPoolState
  /* Who paused it while its node is served `paused` (`node_disk_events.served_gha_state`): `operator` (this
     `state`), `disk` — the node's disk gate — or `memory` — its memory gate; both lift on their own while
     `state` stays `running`.
     Null otherwise; optional for cached payloads. */
  paused_by?: 'operator' | 'disk' | 'memory' | null
  phase: GhaRunnerPoolPhase
  /** The server's sentence about the last failed convergence; `''` when none, never null or reworded. */
  last_error: string
  observed: GhaRunnerObserved[]
  /** When the daemon last reported; `null` = never (not "reported an empty host"). */
  observed_at: string | null
  /* The host's reported hostname, the `<prefix>` of every runner name. `''` until the first report,
     which is load-bearing server-side (the desired-document route then acts on nothing). Not rendered
     (the full names already are); mirrored because this type follows the out-model. */
  runner_prefix: string
  /** `null` on a row not written since creation. */
  updated_at: string | null
  /* Slots above `count` that GitHub still lists for this host — lowering `count` removes nothing, and
     `observed` follows the count, so they appear only here. Derived at read time from the repo roster.
     `[]` means "none known", never "none exist": the server fails safe and silent (no prefix yet, bad
     credential, GitHub down, pool not `running`). Any entry means the pool is never `converged`. */
  orphaned_slots: string[]
  /** GitHub's view of every registered slot, read with the roster. `[]` = unknown (same fail-safe as
   *  `orphaned_slots`), never "all idle". */
  activity: GhaRunnerActivity[]
  /** Each slot's recent jobs, keyed by `GhaRunnerObserved.unit`, newest first. Optional for an older
   *  server. */
  recent_jobs?: Record<string, GhaRunnerJob[]>
}

/** `GET /api/nodes/{node}/gha-runners`. `{"pool": null}` is a 200 meaning "no pool" — distinguishable
 *  from a body that failed to parse. */
export type GhaRunnerPoolResponse = { pool: GhaRunnerPool | null }

// ── Node disk gate and disk history ─────────────────────────────
// `NodeDiskGateOut` / `NodeDiskEventsOut` (apps/api/app/routes/nodes/); the rule is `app/node_disk_events.py`.

/** `used_pct` is the newest FRESH reading; null when there is none, or withheld (a node on another org's
 *  host). `since` is when the gate last changed. */
export type NodeDiskGate = {
  state: 'open' | 'paused'
  since: string
  used_pct: number | null
}

/** `app/node_disk_events.py::FREED_KEYS` — the closed set of reclaim categories. */
export type NodeDiskFreedKey =
  | 'ci_images' | 'build_cache' | 'stopped_containers' | 'orphan_volumes'
  | 'runner_versions' | 'runner_temp' | 'plugin_dedup' | 'orphan_users' | 'stale_tmp'

/** One runner user's container engine as a reclaim left it. Every key is optional: a producer sends its own. */
export type NodeDiskEngine = {
  user: string
  bytes_before?: number | null
  bytes_after?: number | null
  skipped?: 'busy' | null
  error?: string | null
}

/** One row of the node's disk history, newest first. `freed_bytes` carries only the categories the reclaim
 *  reported; `detail` only the keys it reported. Gate rows (`pause`/`resume`) carry neither. */
export type NodeDiskEvent = {
  id: number
  at: string
  kind: 'reclaim' | 'pause' | 'resume'
  trigger: 'scheduled' | 'warn' | 'converge' | null
  /** After a reclaim; the reading at a pause/resume. Null when withheld. */
  used_pct: number | null
  freed_bytes: Partial<Record<NodeDiskFreedKey, number>>
  total_freed_bytes: number
  /** The node itself, or — on a co-located node — the host whose disk it shares. */
  source_node: { id: string; name: string }
  detail: {
    used_pct_before?: number
    engines?: NodeDiskEngine[]
    removed_users?: string[]
    remaining_bytes?: Record<string, number>
  }
}

export type NodeDiskEventsPayload = { events: NodeDiskEvent[] }

// ── Node host metrics ───────────────────────────────────────────
// CPU, RAM and disk pressure plus network throughput, sampled by the daemon and bucketed per node per minute
// (`NodeHostMetricsOut` / `NodeMetricsOut` / `NodeMetricsPointOut`, apps/api/app/routes/nodes/).
// "host_metrics", not "resources" (`GET /api/resources` is the account and session roll-up).
// Levels and ages are the server's: thresholds live in `app/node_host_metrics.py` (`WARN_PCT`,
// `CRITICAL_PCT`) and ages use the DB's `now()`. No percentage-to-threshold comparison in the web layer.
// Network rates are bytes per second and carry no level: throughput is not pressure.

/** `app/node_host_metrics.py`'s `LEVEL_*` verbatim. A stale reading reports `unknown`, never `ok`. */
export type NodeHostMetricsLevel = 'ok' | 'warn' | 'critical' | 'unknown'

/** One node's newest bucketed reading. */
export interface NodeHostMetrics {
  /** Null (both, with `cpu_count`/`mem_total_kb`/network, levels `unknown`) for a tenant whose node shares
   *  another org's host: those figures are the host's, so withheld (`routes/nodes/node_wire.py::_own_disk_only`). */
  cpu_pct: number | null
  mem_pct: number | null
  cpu_level: NodeHostMetricsLevel
  mem_level: NodeHostMetricsLevel
  /** `unknown` when the node never reported disk (which never hides the CPU/RAM verdict). */
  disk_level: NodeHostMetricsLevel
  /** The worst of cpu/mem/disk — the one figure the list badge paints. */
  level: NodeHostMetricsLevel
  /** Static box facts ("12% of 16 cores"); null when unreadable — percentages remain valid. */
  cpu_count: number | null
  mem_total_kb: number | null
  /** The span the mean actually covers (a daemon up 40s reports ~40), so the UI states it honestly. */
  window_secs: number
  samples: number
  /** ISO stamp of the minute bucket this reading was written into. */
  sampled_at: string
  /** DB-measured, never recomputed here. */
  age_seconds: number
  /** Server freshness verdict. True ⇒ both levels are already `unknown`. */
  stale: boolean
  /** The bound `stale` was decided against: `max(window_secs, FRESH_BOUND_SECONDS)`. Quote it rather
   *  than guessing `window_secs` (as `NodeHealth`'s `stale_bound_seconds`). */
  stale_bound_seconds: number
  /** Used share of the clone filesystem and the pair behind it — latest, not windowed. Null = never reported. */
  disk_pct: number | null
  disk_free_kb: number | null
  disk_total_kb: number | null
  /** Bytes per second received / sent, windowed like CPU. Null = never reported. */
  net_rx_bps: number | null
  net_tx_bps: number | null
  /** Used share of /tmp's inode table and its own level: `warn` above the server's threshold, never folded into
   *  `level`. Null / `unknown` when unreported. Optional: an older server sends neither. */
  tmp_inode_pct?: number | null
  tmp_inode_level?: NodeHostMetricsLevel
  /** Host load signals — latest, not windowed; null when unreported or withheld (foreign host, as CPU/RAM).
   *  Optional: an older server sends none. `/proc/loadavg`. */
  load_1?: number | null
  load_5?: number | null
  load_15?: number | null
  /** `/proc/meminfo` SwapTotal/SwapFree, kB; `swap_pct` the used share (null where no swap is configured). */
  swap_total_kb?: number | null
  swap_free_kb?: number | null
  swap_pct?: number | null
  /** `/proc/pressure/{cpu,memory,io}` `some avg60`, 0..100. */
  psi_cpu_pct?: number | null
  psi_mem_pct?: number | null
  psi_io_pct?: number | null
  /** Busiest processes by CPU and RSS (≤ 10; name only, never argv). The roster may leave it off; the
   *  `/metrics` payload's `latest` carries it. */
  top_processes?: NodeTopProcess[] | null
}

/** One entry of `NodeHostMetrics.top_processes`. `cpu_pct` is percent of ONE core (200 = two cores busy). */
export interface NodeTopProcess {
  name: string
  user: string
  cpu_pct: number
  rss_kb: number
}

/** One live session on a node or a node co-located on it (GET /api/nodes/{node_id}/sessions-usage).
 *  `cpu_pct` is percent of one core; `task` is null when the task is gone or another org's. */
export interface NodeSessionUsage {
  node: { id: string; name: string }
  task: { id: number; title: string } | null
  session_id: string
  pid?: number | null
  cpu_pct: number | null
  rss_kb: number | null
  procs: number | null
  sampled_at: string
  age_seconds: number
  stale: boolean
}

export interface NodeSessionsUsagePayload {
  sessions: NodeSessionUsage[]
}

/** GET /api/tasks/{task_id}/session-usage?hours=N — each session's bucketed usage, oldest-first. */
export interface TaskSessionUsagePoint {
  at: string
  cpu_pct: number | null
  rss_kb: number | null
}

export interface TaskSessionUsage {
  /** `name` is "" once the node is deleted (the peak outlives it). */
  node: { id: string; name: string } | null
  session_id: string
  points: TaskSessionUsagePoint[]
  peak_rss_kb: number | null
  peak_cpu_pct: number | null
  cpu_seconds: number | null
}

export interface TaskSessionUsagePayload {
  window_start: string
  window_end: string
  bucket_secs: number
  sessions: TaskSessionUsage[]
}

/** One bucket average; `at` is the bucket stamp, oldest-first. Disk/network are null for a bucket whose rows
 *  never carried them (an older daemon); every figure is null for the withheld-host reader above. */
export interface NodeMetricsPoint {
  at: string
  cpu_pct: number | null
  mem_pct: number | null
  disk_pct: number | null
  net_rx_bps: number | null
  net_tx_bps: number | null
  /** Sessions running on the node at any moment of the bucket; absent from an older server. */
  runners?: number | null
  /** Used share of /tmp's inode table. Optional: an older server sends none. */
  tmp_inode_pct?: number | null
  /** Host load bucket means; null where the bucket never carried them. Optional: an older server sends none. */
  load_1?: number | null
  swap_pct?: number | null
  psi_cpu_pct?: number | null
  psi_mem_pct?: number | null
  psi_io_pct?: number | null
}

/** GET /api/nodes/{node_id}/metrics?hours=N. `warn_pct`/`critical_pct` ride the payload so guide lines
 *  use the server's thresholds, never a hardcoded 75/90. */
export interface NodeMetricsPayload {
  node_id: string
  hours: number
  /** The server's bucket width; it widens with the range (`bucket_secs_for`). */
  bucket_secs: number
  warn_pct: number
  critical_pct: number
  /** The /tmp inode chart's lines: the Health finding's threshold and the alarm's. */
  tmp_inode_warn_pct: number
  tmp_inode_alarm_pct: number
  /** The x-axis domain, both ends from the DB clock (`app/node_host_metrics.py::window_bounds`); the
   *  chart derives neither end (not `Date.now()`, not the last point). A property of the request: valid
   *  even when `points` is empty. The oldest point may precede `window_start` by up to
   *  `bucket_secs - 60` (a straddling bucket); `toPlot` clamps it. */
  window_start: string
  window_end: string
  /** Oldest-first. Empty is legitimate and renders as prose, not an empty axis. */
  points: NodeMetricsPoint[]
  latest: NodeHostMetrics | null
}

// ── Node health ───────────────────────────────────────────────────────────────
// GET /api/nodes/{node_id}/health — liveness verdicts, reporting clocks and config drift (NodeHealthOut,
// apps/api/app/routes/nodes/health.py, field for field).
// Slot-intent shapes are not here: they live with their one reader in `pages/audit/intentsApi.ts` (the
// single home of the row, the page and the counts) — do not re-declare them.
// Every verdict and age is the server's (DB `now()`; `app/node_liveness.py::verdicts_for`): no age
// arithmetic or staleness comparison under `src/`, so a skewed laptop clock cannot misjudge a node.

/** One reporting clock. `at === null` = never written, which `stale` already reflects (worse than late). */
export type NodeHealthClock = {
  key: string
  label: string
  at: string | null
  age_seconds: number | null
  stale: boolean
  /** The bound `stale` was measured against. `null` = withheld (the enumeration clocks' operator-tier knob
   *  is not served to the tenant view; `apps/api/app/routes/nodes/health.py::HealthClock`). `stale` is always
   *  served. */
  bound_seconds: number | null
  /** What a stale reading here means — rendered verbatim. */
  meaning: string
}

/** A declared knob whose live value contradicts the declaration. `got === null` = the snapshot lacks the
 *  key (unset, or not in the daemon's allowlist). */
export type NodeConfigDrift = { key: string; want: string; got: string | null }

export type NodeHealth = {
  node_id: string
  enabled: boolean
  /** The drain half of the enable state (`app/models/nodes.py::Node.draining`; see `NodeRecord.draining`). The
   *  Verdict card reads it; `NodeHealthOut.draining` says why `verdicts` cannot answer for a drain. */
  draining: boolean
  /** `node_liveness`'s vocabulary: live | disabled | silent | not-observing | stale-release
   *  | config-drift. A list: several can hold at once, each with its own remedy. Empty only for an
   *  unregistered presence-only host. */
  verdicts: string[]
  healthy: boolean
  /** The verdict bound only (`node_liveness.STALE_AFTER_SECONDS`). Render each clock's own
   *  `bound_seconds` beside its age, never this. */
  stale_bound_seconds: number
  clocks: NodeHealthClock[]
  config_drift: NodeConfigDrift[]
  /** false = a host on the bus with no `nodes` row, hence no clocks or declaration; the UI says so. */
  registered: boolean
  /** Faults the node logged that no clock shows (a NodeLink store not answering, a wedged step); any one
   *  makes `healthy` false. Optional for an older server. */
  findings?: NodeHealthFinding[]
  /** Journal findings from the last 24h that are no longer active. Optional for an older server. */
  recent_findings?: NodeHealthFinding[]
  /** The node's self-update passes, newest first (at most 20). Optional for an older server. */
  updates?: NodeHealthUpdate[]
}

/** One self-update pass: the version it reached (`null` when unknown) and whether it converged. */
export type NodeHealthUpdate = {
  at: string
  version: string | null
  outcome: 'converged' | 'failed'
  /** The pass's own line — the failure's cause when `failed`. */
  detail: string
}

export type NodeHealthFinding = {
  /** `nodelink_store_unresponsive` | `step_wedged` | `memory_critical` | `cpu_critical` | `oom_kill` | `relaunch_storm`, or newer; rendered by its server-written words. */
  kind: string
  label: string
  /** The logged line itself. */
  detail: string
  first_at: string | null
  last_at: string
  /** What it costs, in one sentence. */
  meaning: string
  /** The tasks it is about (`relaunch_storm`); empty or absent otherwise. */
  task_ids?: number[]
  /** How many events it counts (`relaunch_storm`); null or absent otherwise. */
  count?: number | null
}

// ── Task tags ───────────────────────────────────────────────────────────────
// The web-side single home of the three tag shapes (server field for field), read by every tag
// surface. Ids are strings, `description` is `''` never null, `created_by`/`updated_by` are resolved
// display names (the `PortGridVar` conventions).

// The compact per-task tag ref carried on every task payload (server: TagRefOut).
export type TagRef = {
  id: string
  tag: string
  name: string
  group_id: string
  group_name: string
  group_multi_select: boolean
  group_position: number
  position: number
}

// One tag as an editable record (server: TagOut).
export type Tag = {
  id: string
  tag: string
  name: string
  description: string
  position: number
  tag_group_id: string
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

// A tag group with its tags (server: TagGroupOut) — the catalog every tag surface reads.
export type TagGroup = {
  id: string
  name: string
  description: string
  state: 'draft' | 'active'
  multi_select: boolean
  position: number
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
  tags: Tag[]
}

// GET /api/tasks/tag-counts — section totals over the same row set the list route would return for the
// filters, not the loaded window. `counts` is keyed by tag id and zero-filled for every tag of every
// active group; `untagged` is global (no tag in any active group).
export type TagCounts = {
  counts: Record<string, number>
  untagged: number
}

// GET /api/tasks/sources — the Source filter's vocabulary, in display order. `value` is what the filter
// sends (`source=`); `label` is the only word printed (labels are single-homed server-side).
export type TaskSourceOption = {
  value: string
  label: string
}

// GET /api/tasks/creators — the board's creator roster for the picker, scoped by the list route's
// project + hidden params.
export type TaskCreator = {
  // The user's uuid — what the filter sends (`created_by_id=`), unlike `PipelineTask.created_by`, a name
  // (see `Provenance`).
  id: string
  // The person's label, derived server-side from First + Last (else the email —
  // `apps/api/app/identities.py::person_label`), as `created_by` on a row, so
  // `components/tasks/AuthorBadge.tsx` matches the Activity Feed.
  name: string
  // Top-level tasks in scope this person created (same rows the list route returns).
  // A `0` is real and always the caller's own: the route appends the caller at `task_count: 0` when the
  // scope has none of theirs (`task_creators`, apps/api/app/routes/tasks/listing.py). Never use the count as a
  // presence test — `filter(c => c.task_count)` deletes exactly that row.
  task_count: number
}

// Mirrors TaskOut (apps/api/app/routes/tasks/task_wire.py) — the Task pipeline row. `persona` is the
// dispatch discriminator (epic/business_analysis/architecture/researcher/plan/coder/finish/
// test_optimizer/onboard/translate/marketer/prototyper/advisor, plus hidden system personas); NOT NULL. An epic is a
// non-dispatched container whose work is child Tasks ordered by dependency edges.
// `awaiting_response_options` is parsed server-side from the latest 🛑 FLEET BLOCKED comment.
// `children` is present only on the detail route when the parent has children.
// `Partial<Provenance>`: `created_by` rides every payload including the list (null = no actor resolved,
// rendered as an em dash); `updated_by` is detail + mutation only (`TaskListOut` `exclude=True`), so it is
// absent on board rows. Both are resolved names, never ids — see `Provenance` near the end of this file;
// the Author badge derives its initials from `created_by` (`lib/authorBadge.ts`).
/* The board's four sections (`apps/api/app/task_sections.py`'s `Section`), in display order. */
export type TaskSection = 'stopped' | 'in_progress' | 'draft' | 'done'

/* A stop record's kind (`TaskStop`), for the pane's ordering and glyphs. */
export type TaskStopKind = TaskStop['kind']

/* A person as a feature names one (`routes/features.py::UserRef`). `name` is derived server-side from
 * First + Last, else the email (`apps/api/app/identities.py::person_label`). */
export type FeatureUserRef = { id: string; name: string }

/* GET /api/features/{id} — `routes/features.py::FeatureOut`, the pinned shape. `lead` is labelled "Owner";
 * `can_edit` says whether THIS caller may edit, ship, reopen, delete and file into it. */
export type FeatureOut = {
  id: string
  name: string
  description: string
  project_id: number
  project_name: string
  lead: FeatureUserRef
  status: 'active' | 'shipped'
  /** `"<shape>-<colour>"` (`lib/featureIcon.ts`) or null for "No icon". */
  icon?: string | null
  /** Whether the task rail draws its completion ring; shared, off by default. */
  show_progress: boolean
  shipped_at: string | null
  shipped_by: FeatureUserRef | null
  created_at: string
  created_by: FeatureUserRef
  can_edit: boolean
}

export type PipelineTask = Partial<Provenance> & {
  /** The owning tenant (`ui/OrgIndicator.tsx`); null = not bound, see `OrgRef`. Not optional: list and
   *  detail both carry it. */
  org: OrgRef | null
  id: number
  // The project: which repo it ships to, the pool it draws from, and the task-access scope — who may see
  // and run this row. `useTaskAccess` keys every per-row permission on it, and the board scopes on it.
  project_id: number
  /** The feature this row is filed in (hierarchy level 3); null is "No feature". Optional: older fixtures
   *  omit it. Filed with `PUT /api/tasks/{id}` `{feature_id}` (a stage task moves with its epic). */
  feature_id?: string | null
  /** Why a Stopped row stopped (`routes/tasks/task_wire.py::StopOut`); null when it is not stopped. `kind: 'waiting_on_customer'`
   *  is "Waiting on customer" (D-waiting), whose `text` is the operator's note. */
  stop?: TaskStop | null
  // The board section (`apps/api/app/task_sections.py`, the one home of the mapping). A parent is placed
  // by its children, so an epic whose stage is asking is `stopped`. Read, never re-derived client-side.
  // Optional for older fixtures.
  section?: TaskSection
  // On a nested child only: its position in the epic's pipeline order (dependency depth, then
  // `phase_index`, then id). The epic's stages render in this order, never a persona list (D11 b).
  stage_rank?: number | null
  parent_task_id: number | null
  is_plannable: boolean
  /* ── The addressee of this task's notifications — not a permission (that is the project's).
     `assigned_to` is the resolved display name. `assigned_to_id` is the one actor uuid on this shape,
     because the form edits it: picked from `GET /api/projects/{id}/members`, sent on `POST /api/tasks` and
     `PUT /api/tasks/{id}` (null is a 422), and needed to preselect the current member. Both optional:
     `TaskListOut` excludes them; detail and mutations carry both (`null` on pre-field rows). */
  assigned_to_id?: string | null
  assigned_to?: string | null
  title: string
  // Optional: the list route omits it (with `awaiting_response_body`; ~42KB of a ~95KB board). Detail
  // reads, mutation responses and an epic's nested children carry it. Same for `metrics` and
  // `outcome_no_pr_evidence`.
  body?: string
  state: TaskState
  // Transition names (`generated/taskTransitions.ts`) this row may take now, computed server-side
  // (`TaskOut.available_transitions`). When absent, `components/tasks/controls.ts`'s `allowedTransitions`
  // falls back to the generated snapshot's `sources` — the same table, not a second derivation.
  available_transitions?: string[] | null
  // True exactly when the caller created this row and it is still `draft` — the API's own `tasks:create`
  // fallback condition, server-computed (it depends on who asked).
  // It lets a `tasks:create` holder without `tasks:edit` edit and delete their draft (PUT/PATCH,
  // tags/retag, dependencies, attachments, DELETE) — and unlocks no run verb (`/start`, `/force-start`,
  // `/reset`, `/stop`, `/complete`, `/answer`, `/answer-question`, `/ignore-question` stay `tasks:edit`).
  // Fail-closed: absent/false when the response could not name its caller (e.g. an epic's nested
  // children), so readers test `=== true`.
  // Presentation only (`components/RequireFeature.tsx`; key-to-verb map in `hooks/useTaskAccess.ts`).
  authored_draft?: boolean
  // May the caller set/clear `hidden` (`apps/api/app/routes/tasks/task_wire.py::_may_set_hidden`: the creator with
  // `tasks:edit`, or a superadmin)? Gates the edit form's Hidden checkbox. Presentation only.
  may_set_hidden?: boolean
  // The clause half of the task write door, serialised per row (`apps/api/app/authz/::
  // editable_task_clause`, evaluated by every write route). Normally agrees with the key-based answer in
  // `hooks/useTaskAccess.ts` (the clause is the project write key), but this stays the authority.
  // `false` = refused, vetoing the key arm of every per-row write predicate there; `true` adds nothing;
  // `null` = not computed (e.g. nested children) — the key answer stands. Only `=== false` refuses;
  // `undefined` reads as `null`. The `tasks:create` draft fallback is `authored_draft`, not this.
  // Presentation only; the routes refuse on their own.
  may_edit?: boolean | null
  github_synced_at: string | null
  // Optional like `body`; read only by GateForm on the detail page.
  awaiting_response_body?: string | null
  persona: string
  /* Where the row was filed from (`console`, `cron`, `agent`, `trigger:<kind>`), set once at filing and the
  board's Source filter value (`?source=`). `source_label` is the server's word for it — print that, never the
  code. Optional for older fixtures. */
  source?: string
  source_label?: string
  /* The template this row was started from (`routes/tasks/task_wire.py::TemplateRefOut`): `id` while the template
  exists, null once deleted; `name` kept either way. Null/absent = not from a template. */
  template?: { id: string | null; name: string } | null
  /* The trigger that filed this row (`routes/tasks/task_wire.py::TaskOut`): the live trigger's id (null once it is
  deleted) and its name, kept even after a delete; `''` = not filed by a trigger. Optional for older fixtures. */
  created_by_trigger_id?: string | null
  created_by_trigger_name?: string
  /* The automation that filed this row — or, on a detail read, its epic (`TaskOut.created_by_automation_id`):
  the live automation's id (null once deleted) and its kept name. Optional for older fixtures. */
  created_by_automation_id?: string | null
  created_by_automation_name?: string
  /* The trigger's advisory budget flag on this row (`TaskOut.trigger_budget_exceeded_usd`): the run's cost when it
  went over the trigger's budget (`trigger_budget_usd`), else null. Never a stop. Optional for older fixtures. */
  trigger_budget_exceeded_usd?: number | null
  trigger_budget_usd?: number | null
  /* The playbook this task runs (`TaskOut.playbook`), or null for an ordinary task. `simple`: one stage, so
  the row is dispatched as that stage's persona; else a staged run whose stages are its children. Fixed once
  the row is saved (the server refuses a change). Optional for older fixtures. */
  playbook?: TaskPlaybookRef | null
  /* The playbook stage this row IS (a stage child, or a simple playbook's one stage), or null. Its `label` is
  what a card names the row by, ahead of the persona label. */
  playbook_stage?: TaskPlaybookStage | null
  /* An auto-saved row its author has not Saved yet (`TaskOut.unsaved`). Read by the task form: a playbook
  may still be picked or changed only while this holds. Optional for older fixtures. */
  unsaved?: boolean
  // Kept off the default `/tasks` board — `hidden` is the sole UI-filter authority, never `persona`.
  // Mostly fleet bookkeeping, but a Runner cron can file the operator's own maintenance this way
  // (`app/cron_activity_runner.py`). Always sent; optional for older fixtures. Surfaces on
  // Needs-attention and behind "Show hidden".
  hidden?: boolean
  // Soft-archive stamp: non-null = off the default board (the toolbar's opt-in brings it back —
  // `components/tasks/TaskToolbar.tsx`), and sorted into the finished half whatever its `state`
  // (`isCompletedRow`). Always sent; optional for older fixtures.
  archived_at?: string | null
  awaiting_response_options: string[] | null
  // Dispatch-order lever: higher = sooner. Nullable; null reads as normal (0).
  priority: number | null
  // Autonomous-mode field, present on every TaskOut row.
  // `autonomous` is the raw column, not the dispatch value: on an epic child it is inert
  // (`_task_effective_autonomous` uses the parent epic's for the two interviewing personas and forces the
  // rest on), so no per-child control renders it.
  autonomous: boolean
  /** The tracker this task was created from and its issue id (`'jira'` + `'OAM-123'`), written by "From
      Jira"; both null together otherwise. A record of origin, never re-synced. */
  external_task_system: string | null
  external_task_id: string | null
  // Epic transition mode: 'auto' (dispatch a dep-satisfied child) | 'hold' (keep it `ready` until Run).
  // Read off the parent epic at dispatch; edited on the epic form (components/tasks/TaskForm.tsx,
  // default 'auto'). Always sent; extensible server-side.
  transition_mode?: string
  /** Project memory: the row's own On/Off (null = Default — its epic's, then System Settings), and what its
      last dispatch resolved (`memory_arm`, null until one does) and why (`memory_arm_source`, one of
      `app/project_memory/vocabulary.py::ARM_SOURCES`). Optional for older fixtures. */
  project_memory?: boolean | null
  memory_arm?: boolean | null
  memory_arm_source?: string | null
  /** Memory units that reached this task, per event kind; null when the payload resolved none. */
  memory_counts?: MemoryCounts | null
  /** This row's model pin — the top rung of dispatch model precedence (then the persona stage default,
      then the project's `default_model`). A closed set (`apps/api/app/model_catalog.py`, served by
      `GET /api/tasks/models`); the web never spells the identifiers. Written only by a superadmin
      (refused in `update_task`). Always sent; optional for older fixtures. */
  model?: string | null
  /** The vendor this task may run on (`tasks.bound_provider`, an `app/agent_providers.py` value): the
      operator's provider pin or its first dispatch's stamp. NULL = "Any". Written only by a superadmin
      (`TaskUpdate.bound_provider`). Optional for older fixtures. */
  bound_provider?: string | null
  /** The SDK agent picked for this task (`tasks.agent_id`), the agent picker's other half: at most one of the
      two is set. Optional for older fixtures. */
  agent_id?: string | null
  /** The one node that may place this task (`tasks.pinned_node_id`); NULL = any. Written by operators only
      (`TaskUpdate.pinned_node_id`). Optional for older fixtures. */
  pinned_node_id?: string | null
  // Terminal evidence from the runner's settle call (no later backstop derives them). A `done` row with a
  // null `outcome_pr_url` renders the detail page's "⚠ Unverified" banner (`pages/taskDetail/TaskDetailsPanel.tsx`),
  // the false-close guard.
  outcome_summary: string | null
  outcome_pr_url: string | null
  /** Every PR this task opened, across its project's repositories: the primary repository's first, then by
      opened time. Always sent; absent on an older payload, read as `[]` (`components/tasks/TaskPrList.tsx`'s
      `taskPrs`). `outcome_pr_url` stays the primary's merged PR. */
  prs?: TaskPr[]
  /** Whether this task works a repository, snapshotted at its first lease (`slot_allocation.task_has_repo`),
      so a project later gaining or losing its repositories never relabels it. False: no branch or PR applies.
      Optional for older fixtures; absent reads as true. */
  has_repo?: boolean
  // The no-PR terminal's reason and proof (one of duplicate / already_shipped / no_code_needed /
  // remediated, with evidence). The Outcome panel renders them at any state (an `active` row may already
  // have reported). The reason rides the list (`doneWarnReason`); the evidence is detail-only.
  outcome_no_pr_reason?: string | null
  outcome_no_pr_evidence?: string | null
  /** Where the runner runs (`live`), or last ran: node and slot by name (`app/runner_places.py`); null when it
      never launched. The list serves only a `ready` row's held slot. */
  ran_on?: { node: string; slot: number; live: boolean } | null
  /** The task's RUNNING MicroVM (`vm_id`, its `node`, and whether this viewer may type), null for a
      slot-backed run. Detail only. A VM task holds no worktree slot; its terminal opens by `vm:<vm_id>`. */
  microvm?: { vm_id: string; node: string; may_edit: boolean } | null
  /** The MicroVM node holding the task's last VM, set only when Start fresh elsewhere can run (not running,
      another node runs the project). Detail only. */
  vm_hold_node?: string | null
  /** The task's newest RUNNING agent session (`sessions` row, as `/api/sessions?task=` lists it): its
      `agent_session_id`, `runtime` (`legacy`/`acp`) and ISO `started_at`. An `acp` run records its session
      here, not necessarily in `runner_session_id`. Null when none runs. Detail only. */
  live_session?: { id: string; runtime: string; started_at: string } | null
  // Why a `system_failed` row failed, as its terminal writer stored it; null in every other state. Detail
  // only (the list omits it). Rendered by the detail page's failure banner (`failureCause`).
  failure_reason?: string | null
  /** Why this operator was never asked anything — a server-composed sentence, or null. Rendered verbatim
      by the Details tab's `interview-skipped` note (`pages/taskDetail/TaskDetailsPanel.tsx`): no rewording or
      truncation, as with `ProviderDegradations`. Derived at read time from `autonomous` and the bound
      provider, detail payload only; the web holds neither input.
      Null means "no such statement", not "the interview happened" — it is the value on nearly every row.
      Optional for older fixtures. */
  interview_skipped_reason?: string | null
  // Child count, on the top-level list + detail, so a collapsed row can tell a decomposed parent from a
  // childless false-close. Null on nested-child rows / older payloads.
  child_count?: number | null
  // Done-child count, the sibling of `child_count`, for the collapsed epic's progress bar.
  done_child_count?: number | null
  // The oldest open question among this row's non-archived children, or null. On list and detail.
  // Display-only, server-computed (`_gated_child_ids`, apps/api/app/routes/tasks/task_wire.py): collapsed epic rows
  // carry no children to scan, and otherwise an epic with a parked child reads "Running".
  // The epic's own `state` deliberately stays `active` — its state is its children's completion rollup,
  // never their gate. `taskStatus()` (components/tasks/shared.tsx) turns this into the Asking word.
  // A number so the `?` navigates to that child (`answerTargetId` / `answerHref`).
  gated_child_id?: number | null
  created_at: string
  updated_at: string
  // Detail route only, when the Task has children (each with its own `persona` + `state`).
  children?: PipelineTask[] | null
  // Present only on a `ready` epic child still gated behind an unmet `task_dependencies` prerequisite:
  // the blocking prerequisite's `persona`. `ready` with it set = queued, not dispatchable
  // (components/tasks/shared.tsx). Server-computed, batched per epic; never on the top-level list.
  // Absence means "genuinely dispatch-ready".
  waiting_on_persona?: string | null
  // On a `ready` non-epic row only: why it is not dispatching — "Available after approval", a full org
  // runner or worktree limit ("Waiting: organization runner limit (1/1)"), or the dispatcher's own last
  // decline ("Waiting: node runner limit (5/5)"). Server-derived (`app/org_limits.py::wait_reason`),
  // printed as-is; null = nothing known holds it back.
  wait_reason?: string | null
  // On the node wait of a single-task read: false when Add node is the remedy (the task's org has no node and none
  // on its way), true otherwise (`TaskOut.org_has_node`); null off the node wait.
  org_has_node?: boolean | null
  // When this row's current run started (`TaskOut`/`TaskListOut`), null while none has: the board's running age
  // (`components/tasks/listViews/rows.ts`). Optional for older fixtures and servers.
  run_started_at?: string | null
  // Unmet prerequisites (usually 0). Non-zero means the dispatch gate will not run this row
  // (`_has_unmet_prerequisite`, apps/api/app/routes/tasks/dispatch.py) and `taskStatus()` reads "Waiting".
  // Whether this row is blocked, where `waiting_on_persona` names which stage blocks a child. On every
  // row; optional for stubbed fixtures.
  unmet_prerequisite_count?: number
  // True only between an operator submitting an answer and the node acking the `deliver_answer` intent,
  // a window the API leaves the row `awaiting_response` (`answer_delivering_keys`, apps/api/app/
  // task_sections.py, the single home) and places it In progress, not Stopped. `taskStatus` renders
  // Delivering. Server-derived, so it clears exactly when delivery does. On every row; optional for
  // stubbed fixtures.
  answer_delivering?: boolean
  // While `answer_delivering`: the node's latest reason the delivery failed (redacted) and how many times
  // running it failed so. Null/0 until an attempt fails (`_answer_delivery_failures`, apps/api/app/routes/
  // tasks/task_wire.py). Optional for stubbed fixtures.
  answer_delivery_failure?: string | null
  answer_delivery_retries?: number
  // This row's own open question: one or more open question rounds, or parked `awaiting_response` with
  // no answer in flight. The sibling of `gated_child_id` (a child's), never a substitute.
  // Not a restatement of `state === 'awaiting_response'`: a runner may raise a round without parking, so
  // the row stays `active` with a human waiting. `taskStatus()` (components/tasks/shared.tsx) is the one
  // rule turning it into Asking. Optional: absent degrades to the legacy park read.
  has_open_question?: boolean
  // The fleet runner's `claude --session-id` (apps/api/app/routes/agent/task_terminal.py), persisted at launch; null
  // for a task that never dispatched. Identifies the session that ran the task. `_task_out` always sets it.
  runner_session_id?: string | null
  // The one-shot per-subtask Run stamp: set when a human presses Run, cleared by the next dispatch
  // claim. Non-null on a `ready` row = Starting, not Queued (`taskStatus`, components/tasks/shared.tsx).
  run_requested_at?: string | null
  // The Q&A trail, detail route only (null elsewhere), derived server-side from `activity_events`. See
  // QAEntry.
  qa_entries?: QAEntry[] | null
  // The live `task_questions` rounds, on the detail route and the Needs-attention view only. Unlike
  // `qa_entries`, this is what the multi-round GateForm renders and answers via input-requests/{qid}/answer.
  questions?: TaskQuestion[] | null
  // Staleness marker — not a lifecycle state. Set by this Task's own significant edit (title/body/
  // autonomous) or propagated from an upstream prerequisite's edit/re-run;
  // cleared only by a successful retry of this task. See components/tasks/TaskDependencies.tsx.
  stale_reason?: string | null
  stale_since?: string | null
  // Planner-assigned 1-based phase ordinal + name (e.g. "Foundation") on a `coder` child. Null otherwise
  // — including the analysis children, grouped into a display-only "Prepare" section, which must never
  // get a phase (`append_tasks` wires real dependency edges between phases). See
  // components/tasks/shared.tsx:epicChildGroups.
  phase_index?: number | null
  phase_name?: string | null
  // Set only on a `coder` child filed by Finish under its "LAST SHOT" rule; the value is the filing's
  // dedup key, and a non-empty key is the flag. It dispatches like any build child but belongs to Finish
  // (`components/tasks/shared.tsx:personaGroupOf`).
  finish_follow_up_key?: string | null
  // The epic's persisted Finish-phase aggregate. Null for a pre-Finish epic, an in-flight finish child,
  // or a non-epic (components/tasks/FinishSection.tsx renders the null).
  metrics?: EpicMetrics | null
  // Active-group tags (`TagRefOut`, `apps/api/app/task_tags.py`). Populated only by the list and detail
  // routes; mutation responses and nested children send `[]`. Ordered server-side
  // `(group_position, group_name, position, name)`.
  tags?: TagRef[] | null
  // The rating projected onto the row (`components/tasks/TaskRating.tsx`): mean and vote count, from the
  // same aggregate `GET /api/tasks/{id}/rating` serves (see `TaskRating` for the full shape).
  // The count is the presence test: unrated = `null` average and `0` count, and draws no badge
  // (`RatingBadge` returns `null` on `count === 0`; §"Absence is not a state"). Optional for fixtures.
  rating_average?: number | null
  rating_count?: number
  // Cost, projected for the Cost cell (`components/tasks/CostBadge.tsx`, via `lib/taskCost.ts`). Same
  // fields and vocabulary as `TaskRunMetrics`, their single home; `cost_usd` is a decimal string.
  // The difference is absence: a missing/`null` `cost_status` here means this payload did not resolve a
  // cost — only list and detail price a row; mutation responses omit it. Never read it as
  // `'unavailable'` (a claim that nothing could be priced), or a mutation response blanks a real figure;
  // `pages/Tasks.tsx`'s `replaceTaskEverywhere` owns that distinction.
  // The rolled-up subtree figure (the Details tab's "Token cost estimate"). Optional for
  // fixtures.
  cost_usd?: string | null
  cost_status?: 'measured' | 'partial' | 'unavailable' | null
  // True when `cost_usd` includes a cost an agent reported itself, not one priced from tokens (Efficiency's
  // `cost_source: 'agent_reported'`); `null`/absent exactly when `cost_status` is.
  cost_agent_reported?: boolean | null
  // Every token class (input, cache writes, cache reads, output) this Task and everything under it spent — the
  // tokens `cost_usd` is priced from, live while a runner runs (a few minutes behind). Drawn by the Tokens cell
  // (`lib/taskTokens.ts`). Absent/`null` = this payload resolved no capture (nothing captured, or a mutation
  // response) — never read it as 0; `pages/Tasks.tsx`'s `replaceTaskEverywhere` carries the last figure.
  tokens_total?: number | null
}

/* ── The five-star rating on a done Task ──────────────────────────────────────────────────────────
   `GET` and `PUT /api/tasks/{id}/rating` (body `{stars: 1..5}`) answer the same shape: the PUT returns
   the recomputed aggregate, so `components/tasks/TaskRating.tsx` never averages client-side.
   One replaceable vote per user (hence `PUT`). Any principal who can read the task may vote; the
   caller offers the control only on `done`. */

/** One vote. `user` is a resolved display name, never a uuid (see `Provenance`); rendered as prose by
 *  `lib/taskRating.ts:voteListTitle`. */
export type TaskRatingVote = {
  user: string
  /** Whole stars, 1..5. Never `0` — no vote is no row. */
  stars: number
}

export type TaskRating = {
  task_id: number
  /** The mean across `votes`, or `null` when none. */
  average: number | null
  count: number
  /** The caller's own vote or `null` — pre-fills the reopened stars. Server-computed per caller. */
  my_stars: number | null
  /** Every vote, including the caller's — do not append `my_stars`. */
  votes: TaskRatingVote[]
}

// Per-child interval from the `task:active`/`task:done` trail (`_epic_time_metrics`). `seconds` is null
// when either endpoint is missing.
export type FinishTimePerChild = {
  persona: string
  active_at: string | null
  done_at: string | null
  seconds: number | null
}

export type FinishTimeMetrics = {
  epic_start_at: string | null
  epic_end_at: string | null
  total_seconds: number | null
  per_child: Record<string, FinishTimePerChild>
}

// Per-phase token rollup (same-persona children summed). `capture_status` is "missing" when any child
// of the phase has no captured `Task.metrics` row, so a lost capture reads incomplete, not 0.
export type FinishTokenPerPhase = {
  tokens_in: number
  tokens_out: number
  capture_status: 'captured' | 'missing'
}

export type FinishTokenMetrics = {
  tokens_in: number
  tokens_out: number
  per_phase: Record<string, FinishTokenPerPhase>
}

export type FinishReExploration = {
  duplicate_target_count: number
  duplicated_targets: string[]
}

export type FinishDataLossEntry = {
  child_id: number
  persona: string
  missing_ancestor_ids: number[]
}

export type FinishBenchmarks = {
  re_exploration: FinishReExploration
  data_loss_between_phases: FinishDataLossEntry[]
}

export type FinishVerifyReport = {
  missing_analysis_artifacts: { child_id: number; persona: string }[]
  unresolved_coders: { child_id: number; state: TaskState }[]
  // Follow-ups filed under the finisher's LAST SHOT rule (`finish_follow_up_key`), reported apart from
  // `unresolved_coders` and not scored as failures: they cannot have run yet, and the report is never
  // recomputed. Optional: older reports lack it (`undefined` = none).
  filed_follow_ups?: { child_id: number; state: TaskState }[]
  orphan_fanout_children: { child_id: number; fanout_attempts: number }[]
  append_tasks_pending: boolean
  all_resolved: boolean
}

// Mirrors `execute_finish_phase`'s report shape (app/routes/tasks/epic_finish.py).
/** One pull request a task opened (`TaskOut.prs`). `repo_path` is `owner/name`. */
export type TaskPr = {
  repo_path: string
  pr_number: number
  pr_url: string
  state: 'open' | 'merged' | 'closed'
  is_primary: boolean
}

/** A PR one of an epic's build children recorded, as its Finish report lists it. */
export type FinishPr = TaskPr & { child_id: number }

export type FinishReport = {
  finish_task_id: number
  computed_at: string
  verify: FinishVerifyReport
  time: FinishTimeMetrics
  tokens: FinishTokenMetrics
  benchmarks: FinishBenchmarks
  /** Every PR the build children recorded; absent on an older report. */
  prs?: FinishPr[]
}

// The epic's `metrics` JSONB (models/tasks.py:Task.metrics). The top-level `tokens_in`/`tokens_out`/
// `total_seconds` mirror `finish_report.tokens` / `finish_report.time.total_seconds`.
export type EpicMetrics = {
  finish_report?: FinishReport
  tokens_in?: number
  tokens_out?: number
  total_seconds?: number | null
}

/* One offered option with the runner's meaning (`QuestionOptionOut`, `apps/api/app/routes/tasks/qa_render.py`).
   A display-only wire beside `options`, not a replacement: `options: string[]` is the answer wire
   (`POST /tasks/{id}/input-requests/{qid}/answer` validates against the stored strings). Index-aligned with
   `options`; a consumer that filtered the list (the gate drops blank-glyph options) must keep the
   source index.
   `description` and `preview` are nullable (the ordinary case); render nothing when null. `preview` is
   a monospace mockup (never re-wrap); `description` is prose.
   Not length-bounded on the wire (`routes/tasks._option_detail_text` is verbatim). Clamps live at render
   boundaries: `slack_blocks._OPTION_DETAIL_TOTAL_MAX`, `routes/tasks._QA_TRAIL_OPTION_TEXT_MAX_CHARS`,
   and the console's `GateForm` `OPTION_DETAIL_MAX_CHARS`, applied inside its two renderers.
   `recommended` resolves the "(Recommended)" convention server-side; do not badge it twice if the
   label also spells it. */
export type QuestionOption = {
  /* What gets posted: byte-for-byte the entry at the same index of `options` (the server exact-matches
     the stored list; a prettified label is refused). */
  label: string
  description: string | null
  preview: string | null
  recommended: boolean
  /* The option's answer is a typed value (e.g. "Change integration branch": the branch rides in the comment), so
     the gate will not send it with an empty comment. Optional for the rolling deploy: an older API
     omits it, and absent reads as false. */
  needs_comment?: boolean
}

// One entry in a Task's Q&A trail — detail route only, derived server-side from `activity_events`:
// oldest-first, resolved entries, then a trailing unresolved entry while the task is blocked. The
// persisted history, distinct from the live GateForm.
export type QAEntry = {
  question: string | null
  options: string[]
  /* The `task_questions` round this entry resolved — the id `POST /tasks/{id}/revise-question` takes.
     Snapshotted into the note at resolve time; null on older entries and the trailing unresolved one,
     which withholds Revise (two rounds may ask the same question verbatim). The round's current status
     lives on `PipelineTask.questions`, the authority. Optional for the rolling deploy (an older API sends
     neither this nor `status`; readers fall back to the pre-Revise rendering). */
  question_id?: number | null
  /* `answered`, `dismissed`, or `unresolved` — `resolution === null` cannot tell a dismissal from an
     unanswered ask. Pass to `resolvedVerb`, which degrades unknown values to "Resolved". Optional as
     above. */
  status?: string
  /* The round's short name (`AskUserQuestion` header), telling apart rounds raised in one batch. Null on
     older entries or when the runner gave none — render nothing. Short by convention only (see
     `TaskQuestion.header`); keep the whole value reachable. */
  header: string | null
  /* `options` with descriptions (see `QuestionOption`). Non-nullable (`[]` for free text), matching
     `options`, so the pair stays index-aligned. */
  option_details: QuestionOption[]
  resolution: string | null
  resolved_at: string | null
  /* Who settled it — a resolved display name, never an id (`Provenance`), spelled as on the Slack card.
     Null on the unresolved entry, an actorless historical note, or a deleted account: print no byline. */
  resolved_by: string | null
}

// One round in the live `task_questions` table (TaskQuestionOut, apps/api/app/routes/tasks/qa_render.py). `round`
// is a 1-based read-time ordinal (created_at,id order), never persisted. `status`: open/answered/
// dismissed; `chosen_resolution` is non-null once answered; `options` null for a free-text round.
export type TaskQuestion = {
  id: number
  task_id: number
  question: string
  options: string[] | null
  /* The round's short name — the `AskUserQuestion` `header` ("Missing input", "Verdicts"). Null renders
     nothing (the ordinal identifies the round).
     Short by convention, 200 by contract: the column is unbounded `Text` (`apps/api/app/models/tasks.py::
     TaskQuestion.header`), capped only by `max_length=200` at `apps/api/app/routes/agent/task_terminal.py::
     TaskQuestionIn.header` (a readable 422). A renderer may shorten what it shows (`GateForm`'s chip
     ellipsizes at 10rem) but must keep the full value reachable. Shown beside the ordinal, which is
     what the answer routes on. */
  header: string | null
  /* Display wire for the same options, index-aligned with `options` (see `QuestionOption`). Nullable in
     lockstep: free-text rounds and older rounds have none — render labels only, as before (this also
     keeps the legacy `awaiting_response_options` gate unchanged). */
  option_details: QuestionOption[] | null
  /* Whether this round accepts more than one option — the runner's `multiSelect`, verbatim, never
     inferred. `false` is a real value: one button per option (submit on click) vs a checkbox list with
     Submit. Meaningless on free text. A multi-choice answer posts `selections: string[]` and the server
     composes `chosen_resolution` (see `apps/api/app/routes/tasks/`). */
  multi_select: boolean
  chosen_resolution: string | null
  // The operator's free-text note posted with their choice; null if none, and always on `open`/`dismissed`.
  // Kept apart from `chosen_resolution` (`apps/api/app/routes/tasks/answers.py::_resolution_for_delivery`).
  comment: string | null
  status: string
  /* Who settled the round and when — `GateForm`'s byline ("Answered by Ada Lovelace · 3d ago"). Covers
     answers and dismissals (`models/tasks.py::TaskQuestion.resolved_by_id`); `status` says which. Both null
     while `open`; `resolved_by` alone may be null on a resolved round (unrecorded or deleted author) —
     render the time without a name. A display name, never an id (`Provenance`). */
  resolved_by: string | null
  resolved_at: string | null
  /* When the answer was handed to the runner. Null while open, or answered but HELD: the CP hands over every
     answer at once, when no round is left open (`routes/tasks/answers.py::_settle_gate_if_nothing_outstanding`).
     Handed over is not consumed — pickup is read off the agent's events (`lib/answerPickup.ts`). Optional
     for an older API pod. */
  delivered_at?: string | null
  round: number
  created_at: string
  /* The round reworded in plain terms by the control plane's summary model (`apps/api/app/question_plain.py`),
     written after the round is asked. Display only: answers still post the runner's own labels. Null until the
     rewording lands, or when it failed; optional for an older API. */
  plain?: QuestionPlain | null
}

/* `plain` on a round: a headline question, one line of why, and per option (index-aligned with `options`) a
   short label and gist. `options` is `[]` on a free-text round. */
export type QuestionPlain = {
  headline: string
  why: string | null
  options: QuestionPlainOption[]
}
export type QuestionPlainOption = { short: string; gist: string | null }

// Mirrors TaskDepRef / TaskDependenciesOut (apps/api/app/routes/tasks/dependencies.py): `prerequisites` block this
// task, `dependents` are blocked by it, `unmet_prerequisite_ids` gate dispatch, `ready` iff none unmet.
// Read live off GET /api/tasks/{id}/dependencies. See components/tasks/TaskDependencies.tsx.
export type TaskDepRef = {
  id: number
  title: string
  state: TaskState
  // As on PipelineTask: drives this shape's own Re-run control (`TaskDependencies.tsx`).
  available_transitions?: string[] | null
  done: boolean
  // Done, but its PR still waits in the merge lane: the dispatch gate still counts it unmet. Optional for older payloads.
  awaiting_merge?: boolean
  // See PipelineTask.stale_reason; lets the panel badge a dirty row without a second fetch.
  stale_reason?: string | null
  stale_since?: string | null
}

export type TaskDependencies = {
  task_id: number
  prerequisites: TaskDepRef[]
  dependents: TaskDepRef[]
  unmet_prerequisite_ids: number[]
  ready: boolean
  // The lists are filtered to what the caller can see, but `ready` and unmet are computed over the full
  // set — so `ready: false` can arrive with an empty `unmet_prerequisite_ids`. These counts (invisible
  // AND unmet prerequisites; invisible dependents) let the UI say "blocked by 3, 1 not visible to you".
  // Always sent (0 when nothing is hidden); optional for older payloads/fixtures.
  hidden_unmet_prerequisite_count?: number
  hidden_dependent_count?: number
}

// Mirrors TaskBodyVersionOut (apps/api/app/routes/tasks/create.py): a snapshot of a Task's `body` taken just
// before an overwrite — the prior description. `source`: 'manual', 'agent_filed_edit' (an advisor chat
// amending its draft), 'runner_break_refresh' or 'system_dedup_refresh'. `restored_from_id` is legacy,
// null on every live row, kept so historical rows deserialise. Newest first from
// GET /api/tasks/{id}/body-versions; `TaskVersions` is the sole reader.
export type TaskBodyVersion = {
  id: number
  body: string
  source: string
  restored_from_id: number | null
  created_at: string
}

// Mirrors AttachmentOut (apps/api/app/routes/attachments.py). Metadata only; content is read at dispatch
// by apps/api/app/dispatch_compose/, never fetched into the browser.
export type Attachment = {
  id: number
  filename: string
  content_type: string | null
  size_bytes: number
  created_at: string
  // `'attachment'` is the human's dispatch input; everything else is a runner deliverable (`'report'`
  // HTML, `'prd'`/`'architecture'` markdown, `'file'` any format — offered as a download). Vocabulary:
  // `apps/api/app/task_manifest.py::ARTIFACT_KINDS`, hand-mirrored in `components/tasks/artifactKinds.ts`.
  // A label, not a filter key: each route already returns only its kinds.
  kind: string
  /** Support-ticket attachments only (`'internal'` = staff-only); task rows never carry it. */
  visibility?: 'public' | 'internal'
  /** Support-ticket attachments only: may this reader remove it (the uploader, or staff). */
  may_delete?: boolean
  /** The task this row was attached FROM (a filer's own artifact, or a child's report on its epic). */
  source_task_id?: number | null
  /** The AWS region the bytes are stored in, and its readable label; null/absent when unknown. */
  region?: string | null
  region_label?: string | null
}

/* One row of `GET /api/tasks/:id/artifacts`: `current` flags the newest row per kind (latest-wins keeps
   the older ones, `app/task_artifacts.py::current_artifact`); derived at read time. Not a supersession
   claim for every kind: `prd`/`architecture`/`report` carry one live document, `file` does not — ask
   `components/tasks/artifactKinds.ts::isDownloadArtifactKind` first. */
export type Artifact = Attachment & {
  current: boolean
}

// Dispatch is synchronous request/reply: POST /api/commands returns a command already terminal
// ('done' | 'failed' | 'rejected'). created_at/completed_at come only from GET /api/command-log; the POST
// response carries neither.
export type Command = {
  id: number
  node_id: string
  command_type: string
  args: Record<string, unknown>
  status: string
  created_at?: string
  completed_at?: string | null
  result: Record<string, unknown> | null
}

/** `GET /api/command-log`'s envelope (`routes/portfolio.py::CommandPageOut`). `total` is the filtered
 *  count, not `items.length` — what makes an honest pager possible. */
export type CommandPage = {
  items: Command[]
  total: number
}

/* `GET /api/orgs` — one org (`routes/orgs.py::OrgOut`). `slug` is the stable handle (per-org port
 * allocation and Slack channel key on it, never changes); `name` is the mutable label.
 * `is_operator_plane` is served: org zero's id lives only in `apps/api/app/tenancy/ids.py`.
 * `enabled` is the first step of a two-step lifecycle; `routes/orgs.py` is the single home for what it
 * does and does not yet gate. */
export type Org = OrgMicrovm & {
  id: string
  slug: string
  name: string
  description: string
  enabled: boolean
  /** Org zero, the operator plane (`tenancy/ids.py::is_tenant_org` false); every other org is a tenant. */
  is_operator_plane: boolean
  /** How many `admin_users` are bound to this org — a count, never a roster. */
  user_count: number
  /** The role a domain join grants (`routes/orgs.py::OrgOut.default_role`); null = none. Optional for
   *  fixtures and cached payloads; absent reads as null. */
  default_role?: string | null
  /** Self-service orgs are born `pending` until the operator approves or rejects them. Optional for
   *  fixtures; absent reads as `active`. */
  lifecycle?: OrgLifecycle
  /** Present exactly when the org signed itself up; absent/null for an operator-created org. */
  signup?: OrgSignup | null
  /** The newest marketing-site enquiry whose email matches the sign-up email. */
  enquiry?: OrgEnquiry | null
  /** Approved at sign-up because the applicant's verified email is on an auto-approved domain
   *  (`signup_auto_approve_domains`), not by an operator. Optional for fixtures; absent reads as false. */
  auto_approved?: boolean
  /** A tenant org's plan name and effective end (overrides included), operator roster only: `plan_name` null =
   *  no plan (an operator org), `plan_ends_at` null = never ends. Optional for fixtures; absent reads as null. */
  plan_name?: string | null
  plan_ends_at?: string | null
  /** A coding agent can run for it: an enabled account, or on the Jaah account (`jaah_routing.orgs_with_a_coder`).
   *  Served on the operator's roster; null/absent = not read (another reader's view). */
  has_coder?: boolean | null
  /** Only on the answer of approve and of the welcome-email route: what THIS request's send did. `skipped` =
   *  no sign-up to welcome; null = no send was asked. */
  welcome_outcome?: WelcomeOutcome | 'skipped' | null
  /** SSO-only (`routes/orgs.py::OrgOut`): set, the org's members sign in only through this IdP from
   *  `sso_tenant`; null = not SSO-only. Optional for fixtures; absent reads as null. */
  sso_provider?: SsoProvider | null
  /** The Entra tenant ID, or the Google Workspace domain. */
  sso_tenant?: string | null
  /** When and by whom the IdP's MFA was attested; only then does an SSO sign-in count as MFA. */
  sso_mfa_attested_at?: string | null
  sso_mfa_attested_by?: string | null
  /** The operator's two-factor exemption (a demo/recording org): its members sign in without a code.
   *  Optional for fixtures; absent reads as false. */
  mfa_skip?: boolean
  /** Portfolio Compliance is on for the org (`routes/orgs.py::OrgOut.compliance_enabled`), operator roster only:
   *  the /compliance org picker offers these. Optional for fixtures; absent reads as false. */
  compliance_enabled?: boolean
  /** The org's console plan switches (`routes/orgs.py::OrgOut.plan_switches`), operator roster only: the act-as picker
   *  on a plan-gated page offers the orgs whose switch is on (`lib/planSwitches.tsx`). Optional for fixtures; an
   *  absent switch reads as off. */
  plan_switches?: Partial<Record<RosterSwitch, boolean>>
  /** The org's provider grid (`routes/orgs.py::OrgOut.providers`, cells `[row, auth]`), operator roster only: the
   *  act-as picker reads Agents and ACP registry agents off it (`lib/planSwitches.tsx::GridGate`). Empty otherwise. */
  providers?: string[][]
  /** HIPAA mode (`app/hipaa.py`): the operator's switch and the BAA date it was turned on under (`YYYY-MM-DD`), and
   *  the integrations whose vendor has no BAA (empty while off). Optional for fixtures; absent reads as off. */
  hipaa?: OrgHipaa | null
  /** The operator's isolation exception (`app/tenant_defaults.py`): a node on another org's host. False is a
   *  tenant's default. Optional for fixtures. (NodeLink is a plan term now: `OverridesTab`.) */
  colocation_allowed?: boolean
  /** Soft-deleted (`DELETE /api/orgs/{id}`): hidden, its members' sign-in refused, its data kept until Restore or
   *  Compliance Delete. Optional for fixtures; absent reads as null (live). */
  deleted_at?: string | null
  /** A staging org's parent, and a parent's live staging org (`app/staging_org.py`); null on every other org.
   *  Optional: absent from a server that does not serve them yet. */
  parent_org?: OrgRef | null
  staging_org?: OrgRef | null
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

/** `routes/orgs.py::HipaaOut`. */
export type OrgHipaa = {
  enabled: boolean
  baa_effective_date: string | null
  warnings: { surface: string; vendor: string; acknowledged_at: string | null; acknowledged_by: string | null }[]
}

/** `app/models/orgs.py::SSO_PROVIDERS`. */
export type SsoProvider = 'google' | 'microsoft'

export type OrgLifecycle = 'pending' | 'active' | 'rejected'

/** What one welcome-email send did: `not_configured` = no email integration is set up. */
export type WelcomeOutcome = 'sent' | 'not_configured' | 'failed'

export type OrgSignup = {
  provider: 'google' | 'github' | 'microsoft' | 'email'
  account_type: 'individual' | 'organization'
  /** An individual's occupation, an organization applicant's job title, and what either means to use it
   *  for. Null on a sign-up made before they were asked. */
  occupation: string | null
  job_title: string | null
  intended_use: string | null
  email: string
  owner_name: string | null
  signed_up_at: string
  decided_at: string | null
  /** A display name, never an id. */
  decided_by: string | null
  rejection_reason: string | null
  slack_posted_at: string | null
  slack_error: string | null
  hubspot_synced: boolean
  hubspot_error: string | null
  /** The LATEST welcome-email send: when, by whom (a display name), and its error (null = sent). */
  welcome_sent_at: string | null
  welcome_sent_by: string | null
  welcome_error: string | null
  /** Every send, newest first. */
  welcome_sends: { at: string; by: string | null; outcome: WelcomeOutcome }[]
  /** The plan applied for; null = "(default)" (or a sign-up from before the choice). Optional for fixtures. */
  plan_key?: string | null
  plan_name?: string | null
  /** A card was saved at sign-up, so approval starts the plan on it. Optional for fixtures; absent reads as false. */
  card_on_file?: boolean
  /** Why starting the plan at approval failed (Stripe's sentence); null = it did not fail. Optional for fixtures. */
  billing_error?: string | null
}

export type OrgEnquiry = { company: string; role: string; note: string; created_at: string }

// ── RBAC: superadmin user management (server contract in apps/api) ──

/* ── The feature-permission vocabulary ───────────────────────────────────────────────────────────
   The browser's one spelling of the permission catalog; `apps/api/app/authz/` is the server's single
   home (`FEATURES` x `ACTIONS` -> `PERMISSION_KEYS`, served by `GET /api/permissions`). No count is
   stated here — `types.permissionKeys.test.ts` states it.
   Divergence guard, both halves pre-merge: `types.permissionKeys.test.ts` pins this file (hermetic), and
   a server-side parity test parses this file and `authz/` and compares them in order, in the quick
   tier — so a server-only change is refused at commit.
   The union is derived from the two tuples; `PERMISSION_KEYS` is its runtime rendering (a union cannot
   be enumerated).
   Order is the catalog's (feature, then action), so checkboxes line up with the DB rows; `credentials`
   sits right after the `system` it was carved from (`apps/api/app/authz/catalog.py::FEATURES` says why).
   The nine after `compliance` are the org-admin tier, appended so no existing permission checkbox moves
   (the last two after the alphabetical seven, for the same reason). `accounts`, `audit`, `crons`,
   `nodes`, `sessions`, `workflows` were carved from `system:*` — `App.tsx` and `Sidebar.tsx` ask them now,
   and a migration gave every `system:<action>` grantee the tier key. `system` remains the settings
   subject (settings, command log, port grid, node-groups reads, cloud-node defaults). `tags` is asked by
   `routes/tags.py`'s authoring routes.
   `groups` and `users`: their `view` keys admit the Directory shell (`/users`, `/groups` and the
   editors) READ-ONLY. Writing it stays a superadmin's — no grantable key may admit a write to
   the grant graph (it must not be reachable from inside itself).
   The server tuple is the single home for that whole argument. */
export const FEATURES = [
  'finance', 'system', 'credentials', 'projects', 'tasks', 'worktrees', 'compliance',
  'accounts', 'audit', 'crons', 'nodes', 'sessions', 'tags', 'workflows', 'groups', 'users', 'agents',
  'templates',
] as const
export const ACTIONS = ['view', 'create', 'edit', 'delete'] as const

export type Feature = (typeof FEATURES)[number]
export type Action = (typeof ACTIONS)[number]

/** One permission key as `authz.PERMISSION_KEYS` spells it — e.g. `worktrees:edit`. Never rendered. */
export type PermissionKey = `${Feature}:${Action}`

/** Every key, in catalog order. */
export const PERMISSION_KEYS: readonly PermissionKey[] = FEATURES.flatMap(feature =>
  ACTIONS.map(action => `${feature}:${action}` as PermissionKey),
)

/** Keys the catalogue offers and nothing honours — the grid draws them disabled.
 *  `apps/api/app/authz/catalog.py::UNHONOURED_PERMISSION_KEYS` is the single home for the set and each key's
 *  reason (and for why `finance:view`, honoured on `/clients`, is instead in
 *  `CONSOLE_ONLY_PERMISSION_KEYS`). Mirrored here because the grid renders at module scope.
 *  The `users:*`/`groups:*` write halves are superadmin-only (`@superadmin_only` on `routes/users.py` /
 *  `routes/groups.py`), so offering them would promise a delegation no grant confers.
 *  Presentation only: the grant door still accepts these keys (deliberately), and a grant confers
 *  nothing; this only stops the operator being offered a delegation that cannot happen.
 *  Guarded by the server-side permission-catalog parity test (parses both homes, quick tier): whoever
 *  gives one of these a `@feature` route edits both. */
export const UNHONOURED_PERMISSION_KEYS: readonly PermissionKey[] = [
  ...(['crons', 'workflows'] as const).flatMap(feature =>
    ACTIONS.map(action => `${feature}:${action}` as PermissionKey),
  ),
  // Portfolio Compliance gates on `view` and `edit` only (the API's compliance authoring routes).
  'compliance:create', 'compliance:delete',
  // The identity shell — writes are superadmin-only (routes/users.py, routes/groups.py).
  'users:create', 'users:edit', 'users:delete',
  'groups:create', 'groups:edit', 'groups:delete',
  // Read-and-narrow subjects with no mutating door.
  'audit:create', 'audit:edit', 'audit:delete',
  'sessions:create', 'sessions:delete',
  // No route by design — apps/api/app/routes/nodes/__init__.py's docstring.
  'nodes:create',
  // No API route at all (finance:view and finance:edit gate the billing routes).
  'finance:create', 'finance:delete',
  // SDK agents gate on `view` and `edit` only (routes/agents.py).
  'agents:create', 'agents:delete',
  // Task templates gate their writes on `edit` alone (routes/task_templates.py).
  'templates:view', 'templates:create', 'templates:delete',
]

/* ── Group admin shapes (apps/api/app/routes/groups.py) ────────────────
   Mirror the admin-API contract verbatim. */

/* The reserved seeded group name, the web's one home for the literal and the comparison.
 * `apps/api/app/access_admin.py` (`BYPASS_GROUP_NAME` / `is_bypass_group_name`) stays authoritative — it
 * refuses create, rename, delete and members on it; this only keeps the
 * console from offering a 400. One home so the list page and the edit page agree on case.
 * `toLowerCase()` vs the server's `casefold()` diverge on a few characters (`ß`), only ever in the safe
 * direction (the UI offers, the server refuses) — never a bypass. */
export const RESERVED_GROUP_NAME = 'superadmin'

export function isReservedGroupName(name: string | null | undefined): boolean {
  return typeof name === 'string' && name.toLowerCase() === RESERVED_GROUP_NAME
}

/** A group's name as its viewer reads it. The Everyone group's STORED name may carry the org slug
 *  ("Everyone (acme)") because group names are unique across every org (`access.py::everyone_group`).
 *  Inside one org there is only one Everyone, so a tenant reads "Everyone"; the operator, who lists every
 *  org's, keeps the stored name that tells them apart. */
export function groupLabel(group: { name: string; is_everyone?: boolean }, operator: boolean): string {
  return group.is_everyone && !operator ? 'Everyone' : group.name
}

/** Why Everyone's Delete and its members' Revoke are off: the server refuses both (409,
 *  `access.EVERYONE_MANAGED_DETAIL`). One string for every disabled item that names it. */
export const EVERYONE_REASON = 'every user of the org belongs to it'

/* The provenance pair on every entity payload: resolved display names, never ids (the server joins
 * `created_by_id` / `updated_by_id` through `actors.actor_names`; machine rows read `System`). Print
 * them straight; `null` takes the em-dash. The *_id columns are absent from the wire entirely, so no
 * uuid can be interpolated. */
type Provenance = {
  created_by: string | null
  updated_by: string | null
}

/** One row of `GET /api/groups`, counts included. */
export type GroupSummary = Provenance & {
  id: string
  name: string
  /* NOT NULL with a `''` default (0105); screens still handle empty. */
  description: string
  member_count: number
  /** The group's own role bindings; 0 = it grants nothing. */
  binding_count: number
  /** The org's Everyone group (`GroupDetail.is_everyone`). */
  is_everyone?: boolean
  /** Created by the org's identity provider over SCIM (`GroupDetail.scim_managed`). Absent = no. */
  scim_managed?: boolean
  /** The owning org, for the operator's All-orgs roster (`ui/OrgIndicator.tsx`). */
  org_id?: string
  org?: OrgRef | null
}

/** One row of `GET /api/groups/{id}/members` (a `Page`); `id` is an admin-user id — the same row shape as a project member. */
export type GroupMember = ProjectMember

/** The response every group write returns — the whole group, so no refetch. */
export type GroupDetail = Provenance & {
  id: string
  name: string
  description: string
  /** The org the group belongs to; a membership or binding never crosses it. */
  org_id: string
  /** The org's Everyone group: every user is a member, managed by the system (`access.EVERYONE_*`). */
  is_everyone?: boolean
  /** Created by the org's identity provider over SCIM; its next sync overwrites edits here. Absent = no. */
  scim_managed?: boolean
  /** Distinct members; the roster itself is the paged `GET /api/groups/{id}/members`. */
  member_count: number
}

/* One org the signed-in person is a member of (`routes/auth.py::OrgMembershipOut`), on `AuthUser.memberships`. */
export type OrgMembership = {
  org_id: string
  org_slug: string
  org_name: string
  state: 'active' | 'pending_approval' | 'disabled'
  is_current: boolean
  is_operator_plane: boolean
  /* That org's `compliance_enabled`: lets a Compliance page the current org lacks name the org that has it. */
  compliance_enabled?: boolean
  /* The parent's name when that org is a staging org (`app/staging_org.py`); absent otherwise. */
  staging_of?: string | null
}

/** An invitation into another org awaiting the viewer's answer (`routes/auth.py::PendingInvitationOut`). */
export type PendingInvitation = {
  id: string
  org_name: string
  invited_by_name: string
  created_at: string
}

export type Membership = {
  id: number
  group: string
  resource_type: string | null
  resource_id: string | null
  /** The org's Everyone group (`groups.is_everyone`): label it with `groupLabel`; the server refuses its revoke. */
  is_everyone?: boolean
}

/* No display label on the wire: a person is labelled from `first_name`/`last_name` (the structured pair),
 * else the email — `lib/memberLabel.ts::memberName`, the console's half of the API's
 * `apps/api/app/identities.py::person_label`. `description` is free text, `''` when unset. */
export type ManagedUser = Provenance & {
  id: string
  email: string
  description: string
  first_name: string | null
  last_name: string | null
  is_superadmin: boolean
  activated_at: string | null
  /** When a superadmin disabled the account (it cannot sign in), or null while active. */
  disabled_at: string | null
  /** Who disabled it, as a display name; null while active. */
  disabled_by: string | null
  /** A confirmed TOTP factor exists. An admin's reset clears it; the user enrols again at next sign-in. */
  mfa_enrolled: boolean
  memberships: Membership[]
  /** The org this account belongs to (`routes/users.py::UserOut.org_id`). Optional for an older payload. */
  org_id?: string | null
  /** Where the invitation stands (`routes/users.py::_invitation_status`): `pending` — invited, has not
   *  signed in (or accepted) yet; `declined` — turned the invitation down; null — a working account.
   *  Access granted to a `pending` account applies the moment they accept. Optional for an older payload. */
  invitation_status?: 'pending' | 'declined' | null
  /** Managed by the org's identity provider over SCIM (`routes/users.py::UserOut.scim_managed`). Absent = no. */
  scim_managed?: boolean
}

/** What `POST /api/users` did about the invitation email (`routes/users.py::UserCreated`). The create never
 *  fails on email; `skipped` = the admin unticked "Send invitation email". */
export type InvitationOutcome = 'sent' | 'not_configured' | 'failed' | 'rate_limited' | 'skipped'

export type ManagedUserCreated = ManagedUser & { invitation: InvitationOutcome }

/** A delivery channel — `app/preferences.py`'s `NOTIFICATION_METHOD_TYPES`; `lib/notificationPrefs.ts`
 *  names it. A union so an unrenderable `type` is a compile error, not a blank row. */
export type NotificationMethodType = 'slack' | 'email' | 'telegram'

/** Where a user's alerts go: a channel plus their id there. At most one per type (the server enforces
 *  on write and dedups on read, `app/preferences.py::NotificationsPreferences`). For Slack a member id
 *  (`U0123ABCD`), refused on write if empty or malformed. For email ALWAYS `''`: delivery goes to the
 *  account's login email (`admin_users.email`), and a non-empty id is refused (422). For telegram ALWAYS
 *  `''`: delivery goes to the person's linked Telegram chat (`/api/auth/me/telegram-link`). */
export type NotificationMethod = {
  type: NotificationMethodType
  user_id: string
  /** Slack only: the member's display name, stored by Connect (`app/slack_link.py`); absent on an older link. */
  display_name?: string
}

/** `brief` reduces the Slack card to a headline plus a link — no question prose, options or answer
 *  buttons (`app/alert_relay.py::relay_alert`). */
export type NotificationEventDetail = 'full' | 'brief'

export type NotificationEventPreference = {
  enabled: boolean
  detail: NotificationEventDetail
  // false: not emailed (a one-click unsubscribe or the row's Email box); other channels unaffected.
  email: boolean
}

/**
 * `preferences.notifications`, as `GET /api/auth/me` and `GET /api/users/{id}` serve it. `methods` (where
 * to deliver) and `events` (the subscription) are independent; empty `methods` is the default and
 * delivers nothing.
 * No per-user credential: one deployment-wide `notifications_slack_credential_id`; whether it exists is
 * `Admin.notification_methods_available` (`auth.tsx`), never this document.
 * `events` is keyed by `string`: the server derives kinds from `alert_relay.ALERT_KINDS`, so a newer API
 * can send one this build does not list — it must ride along untouched (`lib/notificationPrefs.ts`).
 */
export type UserNotificationsPreference = {
  methods: NotificationMethod[]
  events: Record<string, NotificationEventPreference>
}

// Per-user console preferences (the `preferences` JSON on GET/PATCH /api/users/{id}); every key optional.
// Closed: the server's UserPreferences is extra=forbid (422 on other keys). `theme` drives [data-theme]
// (auth.tsx); `nav_collapsed_groups` is the retired sidebar fold set (no reader; kept so stored documents still validate); `notifications` is rendered by
// `components/NotificationsPanel`. Stored per user. Keys the console no longer reads are left out: a write
// merges over the stored document (`lib/userSettings.ts::mergedUserPreferences`), so they are carried as stored.
export type UserPreferences = {
  wide_mode?: boolean
  /** `null`/absent = never picked: the operator's default applies (`lib/colorScheme.ts`). */
  theme?: 'light' | 'dark' | null
  nav_collapsed_groups?: string[]
  notifications?: UserNotificationsPreference
  /** The "Get started" card's flags: side effects, not settings — absent from `UserSettingsDraft`, read
   *  and written only through `lib/onboarding.ts` (the single home for the step vocabulary). */
  onboarding_dismissed?: boolean
  onboarding_steps_done?: string[]
  /** The `/tasks` rail's pins and open nodes (`components/tasks/rail/TaskTree.tsx`), as `scopeKey`s. */
  task_rail_pins?: string[]
  task_rail_expanded?: string[]
  /** This user's muted projects and features, as `scopeKey`s (`lib/taskMute.ts`). */
  task_muted?: string[]
  /** Whether the board's Done section is open. Collapsed by default (AC-6.5). */
  task_done_expanded?: boolean
  /** The focused project (`lib/focusedProject.ts`); null or absent = none. */
  focused_project_id?: number | null
}

// GET /api/users/{id} — the ManagedUser row plus audit timestamps and editable `preferences`.
export type UserDetail = Provenance & {
  id: string
  email: string
  /** Proven by an SSO sign-in; email notifications go only to such an address. Absent reads as unknown. */
  email_verified?: boolean
  /** See `ManagedUser`: no `name` — the label is derived from the pair, else the email. */
  description: string
  first_name: string | null
  last_name: string | null
  is_superadmin: boolean
  activated_at: string | null
  /** When a superadmin disabled the account (it cannot sign in), or null while active. */
  disabled_at: string | null
  /** Who disabled it, as a display name; null while active. */
  disabled_by: string | null
  /** A confirmed TOTP factor exists. An admin's reset clears it; the user enrols again at next sign-in. */
  mfa_enrolled: boolean
  /** Linked to an account at the org's SAML/OIDC identity provider (`UserOut.sso_linked`). Absent = no. */
  sso_linked?: boolean
  /** The operator's password-door override (`routes/users.py::UserOut`): null follows the role rule. */
  password_login?: boolean | null
  /** Whether a password signs this account in at all — the server's `auth.password_refusal`, never re-derived. */
  password_door?: boolean
  /** The operator's second-factor exemption: set, sign-in asks for no code. */
  mfa_exempt?: boolean
  created_at: string | null
  last_seen_at: string | null
  memberships: Membership[]
  preferences: UserPreferences | null
  /** The credential this account's runners push with, or null — a common, valid state. A uuid
   *  string: never `Number()` it. Not optional: on `UserOut`, so a PATCH response re-seeds it. */
  git_credential_id: string | null
  /* `effective_permission_keys`: the keys authorization uses. Optional: the list route returns the
     narrower `UserOut`. */
  effective_permission_keys?: string[]
  /* Project ids this account can see under `projects:view` (all for a superadmin target), so the focused-
     project dropdown offers the edited user's roster rather than the viewer's.
     Optional: `PATCH /api/users/{id}` returns `UserOut` without it. `[]` is a real answer; `undefined`
     says nothing — never default it (`UserEditPage`'s `UserPatchResponse` note); branch on
     `=== undefined`. */
  accessible_project_ids?: number[]
  /* The events this user may see and set in their Alerts panel (`app/preferences.subscribable_events`).
     Optional for `accessible_project_ids`' reason: a PATCH response is silent about it. */
  notification_events?: string[]
  /** See `ManagedUser`. */
  org_id?: string | null
  /** See `ManagedUser`. */
  invitation_status?: 'pending' | 'declined' | null
}

// GET/PUT /api/settings — one live operator knob. `apps/api/app/operator_settings/` is the single home
// for the knob list, labels, grouping and bounds; the page renders what the server declares.
// `value` is effective now; `fallback` is what clearing the override reverts to (env var, else code
// default); `overridden` says which is live.
export type OperatorSetting = {
  key: string
  label: string
  help: string
  // Which tier owns the knob: `'operator'` (one fleet-wide value) or `'org'` (each tenant may override) —
  // `org > operator > env > default` (`app/operator_settings/`; wire field
  // `routes/settings.py::SettingKnobOut.tier`, the single home for why it is served). The tenant view
  // renders only `'org'` knobs (`pages/Settings.tsx`), so an absent tier would silently disclose or drop a
  // knob. Required: `effective_settings` emits it on every knob.
  tier: 'operator' | 'org'
  // The area: one category in the Settings list, in catalogue order.
  group: string
  // The section (one card) within the area, or null. A titled card only when the knobs carry one; an area
  // declares sections for all its knobs or none.
  section: string | null
  kind: 'int' | 'float' | 'bool' | 'str'
  unit: string | null
  // A closed value set for a `str` knob (the server rejects others), rendered as a select. Null otherwise.
  choices: string[] | null
  // Display words for `choices`, paired by position; absent/null = the page humanises the token itself.
  choice_labels?: string[] | null
  // A warning shown while that choice is selected, paired by position (`''` = none).
  choice_notes?: string[] | null
  // The value is an operator host's `vnode_hosts.node_id` (`''` = unconfigured), rendered as a
  // host select. Optional: absent on an older server's payload, which keeps it free text.
  vnode_host?: boolean
  // The value is a `cloud_node_defaults.id` (a cloud connection; `''` = unconfigured), rendered as a select
  // fed by `GET /api/cloud-connections/options`. Optional, like `vnode_host`.
  cloud_connection?: boolean
  // The value is the `data_regions` list (`Knob.data_regions`): edited on its own panel (`settings/dataRegions`),
  // never rendered as a row. Optional, like `vnode_host`.
  data_regions?: boolean
  // The value is a `credentials.id`; these are the type slugs the picker (`ui/CredentialSelect`) may offer.
  // Null for text-valued and non-string knobs. A list: e.g. a git credential may be `github` or `gitlab`
  // (`credentials/api.ts`'s GIT_CREDENTIAL_TYPES), so the page filters to the union.
  // Mutually exclusive with `choices` (`pages/Settings.tsx` renders the credential branch first).
  // Required and nullable, like `choices`: `effective_settings` emits it on every knob, so fixtures must
  // spell `credential_type: null`.
  credential_type: string[] | null
  value: string | number | boolean
  fallback: string | number | boolean
  overridden: boolean
  // The catalogue's own default (nothing stored anywhere). The tenant view needs it: `overridden` is
  // true only for the org's own row, so an operator-pinned knob arrives `overridden: false` with a
  // different `value` — how the read-only row tells "Set by the operator" from "Default"
  // (`pages/Settings.tsx`). Required and non-null (pinned by `tests/test_settings.py`'s wire-shape test).
  default: string | number | boolean
  minimum: number | null
  maximum: number | null
  // A tenant Org Admin may save this knob for its own org (`routes/settings.py::SettingKnobOut.tenant_editable`)
  // when the envelope says `org_editable`. Absent reads as not.
  tenant_editable?: boolean
}

// `PipelineTask.playbook` / `.playbook_stage` — `TaskOut`'s two playbook projections.
export type TaskPlaybookRef = { id: string, name: string, simple: boolean }
export type TaskPlaybookStage = {
  key: string
  label: string
  expert: string
  expert_label: string
  /** The persona the expert runs as natively — what the Persona select shows on a one-stage row. */
  expert_persona?: string | null
  deliverable: string
}

// An untouched `/tasks` create surface's starting values, resolved server-side from the three
// `Tasks > Defaults` knobs and delivered on the identity payload (`GET /api/auth/me` and siblings;
// `routes/auth.py::TaskCreateDefaultsOut` says why). Read through `components/tasks/taskDefaults`, the
// one home for the fences and the pre-payload behaviour.
export type TaskCreateDefaults = {
  autonomous: boolean
  complex: boolean
  persona: string
  // `project_memory_default`: named in the editor's "Default (On)" option, never a prefill. Optional for older
  // fixtures (`resolveProjectMemoryDefault` falls back to the knob's shipped On).
  project_memory?: boolean
}

// `TaskOut.memory_counts`: memory units per event kind (`project_memory.events.memory_counts`).
export type MemoryCounts = {
  injected: number
  read: number
  cited: number
  followed: number
}

// An untouched new-project form's gate and stack commands, resolved server-side from the org's
// `Projects > Defaults` knobs and delivered on the identity payload like `TaskCreateDefaults`. `''` = none.
export type ProjectCreateDefaults = {
  gate_quick_cmd: string
  gate_full_cmd: string
  stack_up_cmd: string
  stack_down_cmd: string
}

// The GET/PUT /api/settings envelope. `app_title` stays top-level (the login page's
// /api/public/settings twin reads it).
export type SettingsPayload = {
  app_title: string
  settings: OperatorSetting[]
  // Whether this viewer may save — the server's verdict (`routes/settings.py::SettingsOut.editable`, the
  // single home): the operator-plane orgs are two uuids in `apps/api/app/tenancy/ids.py`, never re-derived
  // here. Optional because the page handles an older server: `Settings.tsx`'s `canSave` starts `false`,
  // so absent renders read-only.
  editable?: boolean
  // The viewer is a tenant Org Admin (not the operator): it may save the knobs marked `tenant_editable`, for
  // its own org (`SettingsOut.org_editable`). Absent reads as no.
  org_editable?: boolean
}

// GET /api/port-grid — one per-service range start. `name` is the `.env` var (`FRONTEND_PORT`); a row
// makes the var tier 1 (deterministic from the slot number). Without a row it is tier 2, auto-assigned
// between `auto_port_floor` and `auto_port_ceiling` — why deleting a row is safe
// (`components/PortGridSection.tsx`). `projects` lists who declares it, resolved server-side.
export type PortGridVar = {
  id: string
  name: string
  // Always a string, `''` for none (`routes/port_grid.py::PortGridVarOut`).
  description: string
  range_start: number
  projects: string[]
  created_at: string | null
  updated_at: string | null
  // Resolved display names, joined through the actor id.
  created_by: string | null
  updated_by: string | null
}

// The GET /api/port-grid envelope: rows plus the five scalar grid knobs (also shown in the "Ports"
// settings group under Nodes), carried so the window column and overlap check need no second fetch. `warnings` are
// advisory: the rows are already saved — "saved, with warnings", never a rejected write.
export type PortGridPayload = {
  vars: PortGridVar[]
  block_size: number
  stride: number
  window: number
  auto_port_floor: number
  auto_port_ceiling: number
  warnings: string[]
}

// POST /api/port-grid/reband — one entry per node. Partial results are normal; render every entry.
export type RebandResult = {
  node_id: string
  // An open string (`routes/port_grid.py::reband` passes unrecognised replies through). `'ok'` is the only
  // success.
  status: string
  detail: string
}

// GET /api/tasks/{id}/control-messages (`routes/task_control_messages.py`): what reached the task's runner pane
// from outside it, oldest first. `message` is a control-plane notice, `answer` an operator's answer.
// `dropped`: retired undelivered (superseded, parked) — never coming.
export type TaskControlMessage = {
  id: number
  kind: 'message' | 'answer'
  created_at: string
  sender: string
  text: string
  delivered: boolean
  delivered_at: string | null
  dropped: boolean
}

// GET /api/tasks/{id}/run-metrics (`app/task_run_metrics.py`). Computed per request from the transition
// trail plus captured `metrics` JSONB — never persisted or agent-authored — so available for any task:
// a leaf reports its own numbers, a parent the rollup over `scope_task_ids`.
export type TaskRunMetrics = {
  task_id: number
  // The task plus every descendant covered, ascending, always including `task_id`.
  scope_task_ids: number[]
  // The "go" instant: the root's first transition out of `draft`. Null alone is not "never started" —
  // `clock_status` says which absence.
  started_at: string | null
  // 'measured' = the times are real. 'not_started' = still `draft` with no capture in scope.
  // 'unavailable' = it did run but the trail postdates it, so times are unknown while tokens and cost stay
  // valid.
  clock_status: 'measured' | 'unavailable' | 'not_started'
  // The latest `done` across the scope; null while anything runs (`in_progress`).
  ended_at: string | null
  in_progress: boolean
  // Go → done wall clock, all waiting included. Null when `started_at` is.
  total_seconds: number | null
  // Time spent in `active`, summed over the scope — queue, operator wait and `paused` are other states.
  // Includes a runner alive but stuck (indistinguishable in the DB).
  agentic_seconds: number | null
  tokens_in: number
  tokens_out: number
  // 'measured' = captures for every task that ran; 'partial' = some missing (worktree released before
  // the sweep); 'missing' = nothing captured, so zeros mean no data, not no spend. Never conflate
  // 'missing' with zero.
  token_status: 'measured' | 'partial' | 'missing'
  // Dollars as a decimal string (Decimal server-side, never a float). Null when 'unavailable'.
  cost_usd: string | null
  // 'measured' = every session reported its per-model, per-class split; 'partial' = some predate it and
  // their spend is excluded (a floor); 'unavailable' = nothing priceable.
  cost_status: 'measured' | 'partial' | 'unavailable'
  // Models with spend but no pricing row, excluded rather than guessed. Fix in Settings.
  unpriced_models: string[]
  // What ran, for a dispatched task; null/absent when never dispatched. `tier` = the tier id dispatched
  // (null for an exact override); `dispatched` = the concrete `--model` given to the CLI (null = the CLI's
  // default); `ran` = the model id the session recorded (null until seen).
  model?: { tier: string | null, dispatched: string | null, ran: string | null } | null
  // Over the task's own sessions (`task_session_usage_peaks`): peak RSS (kB), peak CPU (% of one core),
  // cumulative CPU seconds. Null when no session reported; absent from an older server.
  peak_rss_kb?: number | null
  peak_cpu_pct?: number | null
  cpu_seconds?: number | null
}

// One pricing row (`ModelPriceOut`, GET /api/model-prices). `name` is the canonical family (`opus`,
// `sonnet`, `haiku`, `fable`) — the join key, collapsed server-side by
// `app/model_prices.py::canonical_model`. Rates are $/MTok decimal strings (money, never floats).
export type ModelPrice = {
  id: string
  name: string
  description: string
  input_per_mtok: string
  cache_write_5m_per_mtok: string
  cache_write_1h_per_mtok: string
  cache_read_per_mtok: string
  output_per_mtok: string
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

// ── Email templates ─────────────────────────────────────────────────────────
// The operator's email template CONTENT (`app/routes/email_templates.py`); the layout and each type's field
// set are code (`app/email_templates.py`), so a type is served, never created. `fields` is keyed by the
// type's field keys, every key present (the server fills a missing one from its default).
export type EmailTemplateField = {
  key: string
  label: string
  default: string
  multiline: boolean
  required: boolean
}

export type EmailTemplatePlaceholder = { key: string; description: string }

export type EmailTemplateType = {
  key: string
  label: string
  description: string
  fields: EmailTemplateField[]
  placeholders: EmailTemplatePlaceholder[]
  /** The active row's id; `null` = the type sends its built-in defaults. */
  active_id: string | null
}

export type EmailTemplate = {
  id: string
  type: string
  name: string
  description: string
  fields: Record<string, string>
  is_active: boolean
  created_at: string | null
  updated_at: string | null
  created_by: string | null
  updated_by: string | null
}

// ── Task comments ────────────────────────────────────────────────────────────
// The Comments tab's wire (`app/routes/task_comments.py`):
//   · No author id: the badge derives from the name, nothing navigates to a person, and `author_is_me`
//     is the server's edit verdict.
//   · `mentions` is resolved server-side (`@Ann Lee` is one person or two words depending on the
//     roster); `lib/commentText.ts` chips exactly those.

/** One person a comment names (`lib/commentText.ts`'s `CommentMention`). A name only. */
export type TaskCommentMention = {
  name: string
}

/** An image a comment owns (bytes at `/api/tasks/{task}/attachments/{id}/content`). */
export type TaskCommentImage = {
  id: number
  filename: string
  content_type: string
}

export type TaskComment = {
  id: number
  /** Absent on a support-ticket comment, which carries `ticket_id` instead. */
  task_id?: number
  ticket_id?: number
  /** Ticket comments only; `'internal'` is staff-only and drawn distinctly. */
  visibility?: 'public' | 'internal'
  /** The raw typed text; the micro-format is parsed at render. */
  body: string
  author_name: string
  author_is_me: boolean
  /** Whether the delete control is drawn; the route re-decides on the call. */
  may_delete: boolean
  created_at: string | null
  /** NULL if never revised — the "edited" mark reads the absence. */
  edited_at: string | null
  mentions: TaskCommentMention[]
  /** Only emojis with a count, in `lib/reactions.ts` order; `names` alphabetical. Read as `?? []`: an
   *  older server omits it. */
  reactions: TaskCommentReaction[]
  /** Oldest first. Optional: a row from before images read as none. */
  images?: TaskCommentImage[]
}

/** One emoji's tally on a comment. `emoji` is a `lib/reactions.ts` key; `mine` = the caller reacted. */
export type TaskCommentReaction = {
  emoji: string
  count: number
  mine: boolean
  names: string[]
}

/** One person the composer's `@` picker may offer (GET /api/mention-candidates). `name` is derived
 *  server-side from First + Last, else the email (`apps/api/app/identities.py::person_label`). */
export type MentionCandidate = {
  id: string
  name: string
}

/** A running task the forecast offers to stop, lowest priority first. */
export type PreemptCandidate = {
  id: number
  title: string
  project_name: string
  priority: number
  priority_label: string
  state: string
  dispatched_at: string | null
}

/** `GET /api/tasks/{id}/dispatch-forecast` — would it dispatch now, and if not why and what could be
 *  stopped. Read at click time, never stored. */
export type DispatchForecast = {
  can_dispatch_now: boolean
  reasons: string[]
  /** True only when stopping a running task could plausibly help; false for a missing worktree or the
   *  task's own hold. */
  preemptable: boolean
  queue_position: number | null
  headroom: number
  running: PreemptCandidate[]
}

/** `GET /api/auth/me/trusted-devices` — a browser that skips the second factor until `expires_at`. The
 *  signed-in user's own, newest first; `current` marks the browser making the request. */
export type TrustedDevice = {
  id: number
  /** Server-derived from the user agent, e.g. "Chrome on macOS". */
  label: string
  created_at: string
  last_used_at: string | null
  expires_at: string
  current: boolean
}
