"""jaah-forge — ONE pull-request CLI for every forge (GitHub, GitLab, Bitbucket Cloud, Azure DevOps).

Installed as `/usr/bin/jaah-forge` by the jaah package (a launcher into this package under
/usr/lib/jaahd). Stdlib only. This docstring is the single home of the CLI's contract.

VERBS — the whole surface; there is no write under `api`, and `run`'s writes are `rerun` and `dispatch` (no
cancel):
  pr create --title T --body-file F [--draft] [--base B] [--head H]
  pr view [N|BRANCH] [--json] [--body] [--ignore-check NAME]...   pr checks N [--watch [--interval S]]   (S >= 60)
  pr list [--state open|closed|all] [--author A] [--head B] [--base B] [--limit N]
  pr auto-merge N [--disable]     pr merge N [--sha S]      pr ready N
  pr edit N [--title T] [--body-file F]                pr comment N --body-file F
  pr close N --body-file F        pr stats N
  pr status N --context C --state pending|success|failure [--description D] [--sha S]
  run list [--workflow FILE] [--branch B] [--limit N]  run view <run-id>   run rerun <run-id> [--failed]
  run dispatch <workflow> --ref B [-f KEY=VALUE]...
  ci log <check-id>               api get <path>

Every verb prints JSON on stdout (`ci log` prints the log text). `pr view` prints the normalised shape
`model.PullRequest.to_json` names, identical on every forge; given a BRANCH (or nothing: the checked-out
branch) it views that branch's newest PR. `--body` adds the description as `body` (opt-in: a watch loop polling
`pr view` should not re-read a long description every minute).

REQUIRED CHECKS. `checks` in `pr view` and `pr checks` rolls up only the checks that GATE the merge — GitHub
branch protection's required checks (every check where protection names none), GitLab's jobs not allowed
to fail, Azure's blocking policies, every check on Bitbucket. `pr checks` also prints `checks_all` (every
check) and marks each item `required`. A red OPTIONAL check therefore never holds `pr merge`, and GitHub's
`UNSTABLE` (mergeable, an optional check red) maps to `merge_state: clean`, and its `BEHIND` with no red or pending
required check (strict protection: the head must first be brought up to date with the base) to `behind`. A `pr list` row on GitHub cannot
name the required set, so its rollup counts every check. `pr auto-merge` on a PR the forge says can merge
NOW (GitHub refuses to arm auto-merge in `clean`/`unstable` status) merges it directly, under `pr merge`'s
own checks, and says so on stderr. `pr auto-merge N --disable` disarms it instead — no push policy, since it never
lands work; on an unarmed PR it writes nothing.

CHECKOUT. Every verb acts on the repository the login reaches; run in a git checkout whose forge `origin` names
another repository (by name), it exits 2 before any forge call (`policy.check_checkout`), so it never answers about
a repository other than the one you are in.

PR OBSERVATION. Inside a runner (`CONTROL_TASK_ID` set), `pr create` and `pr view` then post `pull_request.observed`
`{pr_url, pr_number, state, opened, checkout}` over the node socket, so the task's PR list follows what the forge
said; only `pr create` adds a PR to the task (`opened`), a view updates one it already has.
Best-effort: a failure is one stderr note and never changes the output or exit code. The daemon stamps the
repository off `checkout` within the runner's own slot (`cred_broker._observed_repo_id`).

PINNED MERGE. `pr merge N --sha S` merges only the head S (a full commit id): when `view`'s head is not S it
exits 2 before any write, and otherwise sends S as the forge's own head pin (GitHub `sha`, GitLab `sha`,
Azure `lastMergeSourceCommit`), so a push landing between the view and the merge is refused by the forge.
Bitbucket's merge takes no head commit: `--sha` there exits 5. The merge lane (`merge_lane.py`) merges
only this way, at the head it tested.

COMMIT STATUS (GitHub only so far; elsewhere exit 5). `pr status N --context C --state S` sets commit status C on
the PR's head — on S only while the head is still S with `--sha` (exit 2 otherwise) — and prints `{number, sha,
context, state, url}`; the description is cut to GitHub's 140 characters. A green status can let a PR merge, so it is
judged like `pr merge`: push allowed, the PR open, its branches the project's. The merge lane posts its verdict this
way (`merge-lane`, `merge_lane.py::LANE_STATUS`), and a runner on a lane project may not (the PR guard's
family 0b). `pr view --ignore-check NAME` leaves that check out of `checks` and `merge_state` — a `BLOCKED` PR held
only by an ignored required check, with no review missing, views `clean` — so the lane's own `pending` never holds
the PR it is judging. Other forges carry no such status, so there it leaves nothing out.

PR WRITES BEYOND MERGE. `pr ready N` marks a draft ready for review (a no-op on a non-draft). `pr close N
--body-file F` closes an open PR unmerged — GitLab closes, Bitbucket declines, Azure abandons — and posts
F as a comment FIRST: a close notifies nobody on its own, and an unexplained close reads as done (PR #1827).

DIFF STATS. `pr stats N` prints `model.DiffStats.to_json` — `{status, reason, additions, deletions,
files_changed, excluded_additions, excluded_deletions, forge}`, the `pr_stats` wire the daemon stamps on a
`merged_pr` settle (`apps/jaahd/pr_stats_capture.py`). Read off the forge's per-file list (GitHub `pulls/N/files`,
GitLab `merge_requests/N/diffs` — lines counted off each diff —, Bitbucket `pullrequests/N/diffstat`, Azure the last
iteration's `changes`), at most 3,000 files (`partial` past that). Files `loc_filter.is_excluded` names
(lockfiles, vendored, build output, generated) count in `files_changed` and in `excluded_*` only. Azure DevOps
serves no per-file line counts: there `status` is `partial`, files are counted and every line count is null.

CI RE-RUN. `run rerun <run-id>` re-runs a run on a branch of THIS repo that is the runner's own (`policy.check_rerun`)
— never the integration branch's runs, a tag's, a fork's or another repo's (the run it reads
names no branch for those; on GitHub that takes one more call, to confirm the name is a branch), and never with `push_enabled=false` — printing `{id, rerun_of, branch, failed_only}`.
GitHub re-runs in place (`id` == `rerun_of`). GitLab `--failed` is the pipeline's `/retry` in place;
without it, a fresh pipeline on the branch (a new `id`: retrying finished jobs one by one would replay a
played manual job); an MR pipeline's branch is its MR's source branch. Azure retries in place with
`--failed` and otherwise queues a copy of the build (a new `id`); Bitbucket queues a copy of the pipeline
and cannot re-run only the failed steps.

CI DISPATCH (GitHub only so far; elsewhere exit 5). `run dispatch <workflow> --ref B -f K=V ...` starts a
`workflow_dispatch` run of the workflow file on branch B with those inputs (each `-f` a `key=value`, a key at most
once) and prints `{workflow, ref, inputs, dispatched: true}`, plus `id` where GitHub names the new run. B is the
integration branch (a hand-run workflow: `run dispatch mirrors.yml --ref develop -f mirror=jaah-ce`) or your own
branch, as for `run rerun` — never `master` or another branch no task delivers to — and never with
`push_enabled=false` (`policy.check_dispatch`). A dispatched run is no PR's check run: it never satisfies one.

READ-ONLY LISTS. `pr list` prints the newest PRs first, each `{number, title, head, base, author, state,
merge_state, checks, url, auto_merge}` (`--state` defaults to `open`; `closed` is every settled PR, merged ones included;
`merge_state`/`checks` null where the listing names no checks rollup — per forge below).
`run list` prints a workflow's newest runs first — every workflow's when `--workflow` is omitted — each
`model.Run.to_json` — `{id, head_sha, event, status, conclusion, created_at, branch}`, `status` one of
`queued|in_progress|completed` and `conclusion`
`success|failure|cancelled|skipped` once completed, else null; `event` is the forge's own trigger name.
`run view` prints that run's shape plus `jobs` (`model.Job.to_json`) and `annotations`
(`model.Annotation.to_json`: `{level, job, title, message}`, level `notice|warning|error`) — the lines a
job emits for the run summary, such as a deploy's `::warning::` that it was HELD. `--limit` is 1..100
(default 20 runs, 30 PRs): a list is read by paging, one page whenever no client-side filter drops rows
and at most ten when one does (a list may then come back short), and never by polling. Per forge:
  - GitHub: `--workflow` is the workflow file (its basename is used); a run is a workflow run, its jobs'
    check-run annotations are the annotations. `--branch` costs one more call: GitHub's branch filter
    intermittently answers stale, so the unfiltered newest page is merged in. `pr list` pages the repository's PR connection, views
    included — one call unless `--author` (filtered here) needs more.
  - GitLab: a run is a pipeline. A project has one CI config, so a `.yml`/`.yaml` `--workflow` names every
    pipeline, and any other value filters by `workflow:name`. GitLab emits no annotation lines; a job that
    failed but was allowed to is reported as a `warning`.
  - Bitbucket: a run is a pipeline, its id the build number `ci log` takes, its jobs the steps. A `.yml`
    `--workflow` names every pipeline, any other value a custom pipeline's selector pattern. Bitbucket
    Pipelines has no annotation channel: `annotations` is always empty. `run view` (like `ci log`) finds
    the build number among the newest 100 pipelines.
  - Azure DevOps: `--workflow` is the YAML file a build definition OF THIS REPO reads; a run is a build, its jobs the
    timeline's Job records, its annotations every record's issues (`##vso[task.logissue]`).
  GitLab and Azure list no checks rollup, so no reliable merge state: `pr list` there reads the listing alone — one call per page,
  `auto_merge` off the listing (GitLab `merge_when_pipeline_succeeds`/`auto_merge_enabled`, Azure
  `autoCompleteSetBy`) — and prints `merge_state` and `checks` as null (`pr view N` names them). Bitbucket lists
  no mergeability either and views each listed PR in turn.

EXIT CODES (`model.py`): 0 ok · 2 policy refusal (stderr carries the delivery recipe) · 3 forge error
(message capped at 200 chars) · 4 credential refused (the broker's reason verbatim) · 5 misuse.

WHERE THE FORGE COMES FROM. Only the credential broker's `git_credential` login (`login.py`) names the
forge, its `api_base`, `repo_path` and `web_url`, and the token — the same per-task login git's credential
helper answers with, resolved by the broker from the calling process's ancestry. Nothing is read from a
remote URL or a flag, and nothing is written to disk: no token file, no persisted login, no cache.

POLICY BEFORE NETWORK (`policy.py`): every refusal that can be decided locally is decided before the
broker or the forge is asked anything.
"""
