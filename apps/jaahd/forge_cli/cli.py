"""Argument parsing, the verb dispatch, and the exit-code mapping (contract: this package's docstring)."""
from __future__ import annotations

import argparse
import dataclasses
import http.client
import json
import os
import re
import sys
import time

import agent_socket  # apps/jaahd/agent_socket.py — the node vocabulary client, beside this package

from . import login as login_mod
from . import policy
from .adapters import for_login
from .adapters.base import MERGED_MAX, PAGE_MAX, parse_time
from .adapters.gitlab import GATE_VARIABLE
from .model import (EXIT_FORGE, FORGE_ERROR_CAP, AlreadyMergeable, CliExit, CredentialRefused, ForgeError, Misuse, is_id,
                    required_rollup, rollup)
from .transport import Transport

MIN_INTERVAL_S = 60
NONE_GRACE_POLLS = 5
_CLOSE_SYNONYMS = ("abandon", "decline")
# A forge login, username or account email (`[bot]` for a GitHub app). Bitbucket splices it into a BBQL
# literal: escaped there, and nothing that is no username is admitted anywhere.
_AUTHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+\-\[\]]*")
# A full commit id: SHA-1 or SHA-256 object format. A prefix pins nothing a forge would compare.
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# `checks --sha`: a commit id or a prefix the forge resolves — it only reads.
_ANY_SHA = re.compile(r"[0-9a-f]{7,64}")
STATUS_STATES = ("pending", "success", "failure")
# GitHub refuses a commit status description longer than this.
STATUS_DESCRIPTION_MAX = 140


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse would exit 2 — which is our POLICY code
        raise Misuse(message)

    def exit(self, status=0, message=None):  # `--help` still exits cleanly
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


class _RefuseSecretValue(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        raise Misuse("a secret's value comes only from --value-file FILE (or - for stdin), never the command line")


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="jaah-forge", description="One PR CLI for GitHub, GitLab, Bitbucket and Azure DevOps.")
    groups = p.add_subparsers(dest="group", required=True, parser_class=_Parser)

    pr = groups.add_parser("pr").add_subparsers(dest="verb", required=True, parser_class=_Parser)
    create = pr.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--body-file", required=True)
    create.add_argument("--draft", action="store_true")
    create.add_argument("--base")
    create.add_argument("--head")
    view = pr.add_parser("view")
    view.add_argument("target", nargs="?", metavar="NUMBER|BRANCH")
    view.add_argument("--json", action="store_true")
    view.add_argument("--body", action="store_true", help="also print the PR's description as `body`")
    view.add_argument("--ignore-check", action="append", default=[], metavar="NAME",
                      help="leave this check out of `checks` and `merge_state` (GitHub; repeatable)")
    checks = pr.add_parser("checks")
    checks.add_argument("number", type=int)
    checks.add_argument("--watch", action="store_true")
    checks.add_argument("--interval", type=int, default=MIN_INTERVAL_S)
    listing = pr.add_parser("list")
    listing.add_argument("--state", choices=("open", "closed", "all", "merged"), default="open")
    listing.add_argument("--since", metavar="ISO-8601", help="with --state merged (required there): merged at or after")
    listing.add_argument("--author")
    listing.add_argument("--head")
    listing.add_argument("--base")
    listing.add_argument("--limit", type=int, default=30)
    auto = pr.add_parser("auto-merge")
    auto.add_argument("number", type=int)
    auto.add_argument("--disable", action="store_true",
                      help="disarm the PR's auto-merge instead (the merge lane takes sole ownership of a PR it queues)")
    for name in ("ready", "stats", "provenance"):
        pr.add_parser(name).add_argument("number", type=int)
    merge = pr.add_parser("merge")
    merge.add_argument("number", type=int)
    merge.add_argument("--sha", help="merge only if the PR's head is still this commit (the merge lane's tested head)")
    status = pr.add_parser("status")
    status.add_argument("number", type=int)
    status.add_argument("--context", required=True, help="the status's name, e.g. merge-lane")
    status.add_argument("--state", required=True, choices=STATUS_STATES)
    status.add_argument("--description", default="")
    status.add_argument("--sha", help="set it only if the PR's head is still this commit")
    close = pr.add_parser("close")
    close.add_argument("number", type=int)
    close.add_argument("--body-file", required=True, help="why it is closed — posted as a comment first")
    edit = pr.add_parser("edit")
    edit.add_argument("number", type=int)
    edit.add_argument("--title")
    edit.add_argument("--body-file")
    comment = pr.add_parser("comment")
    comment.add_argument("number", type=int)
    comment.add_argument("--body-file", required=True)

    groups.add_parser("checks").add_argument("--sha", required=True, help="the commit whose checks to list")

    ci = groups.add_parser("ci").add_subparsers(dest="verb", required=True, parser_class=_Parser)
    ci.add_parser("log").add_argument("check_id")

    run = groups.add_parser("run").add_subparsers(dest="verb", required=True, parser_class=_Parser)
    runs = run.add_parser("list")
    runs.add_argument("--workflow", help="the CI config file (e.g. ci.yml); every workflow's runs when omitted")
    runs.add_argument("--branch")
    runs.add_argument("--limit", type=int, default=20)
    run.add_parser("view").add_argument("run_id")
    rerun = run.add_parser("rerun")
    rerun.add_argument("run_id")
    rerun.add_argument("--failed", action="store_true", help="re-run only the failed jobs")
    dispatch = run.add_parser("dispatch")
    dispatch.add_argument("workflow", help="GitHub: the workflow file (e.g. mirrors.yml); GitLab: the job the run is "
                          "judged on, `*` for the whole pipeline; Bitbucket: a custom pipeline; Azure: a pipeline id")
    dispatch.add_argument("--ref", required=True, help="the branch the run builds")
    dispatch.add_argument("--sha", help="fail unless the started run builds this commit (the branch did not move); "
                          "the run itself is left running")
    dispatch.add_argument("-f", dest="inputs", action="append", default=[], metavar="KEY=VALUE",
                          help="a workflow input (repeatable)")

    api = groups.add_parser("api").add_subparsers(dest="verb", required=True, parser_class=_Parser)
    api.add_parser("get", help="GET <path> under the forge's API base").add_argument("path")

    for group in VARIABLE_KINDS:
        kind = groups.add_parser(group).add_subparsers(dest="verb", required=True, parser_class=_Parser)
        env_help = "a deployment environment's, not the repository's"
        lister = kind.add_parser("list")
        lister.add_argument("--env", help=env_help)
        setter = kind.add_parser("set")
        setter.add_argument("name")
        setter.add_argument("--env", help=env_help)
        if group == "var":
            values = setter.add_mutually_exclusive_group(required=True)
            values.add_argument("--value")
            values.add_argument("--value-file", metavar="FILE|-")
        else:  # a secret never rides the command line, where `ps` and the transcript would show it
            setter.add_argument("--value-file", metavar="FILE|-", required=True)
            # Declared so `--value` is refused outright, never read as an abbreviation of `--value-file`.
            setter.add_argument("--value", action=_RefuseSecretValue, nargs="?", help=argparse.SUPPRESS)
        deleter = kind.add_parser("delete")
        deleter.add_argument("name")
        deleter.add_argument("--env", help=env_help)
        if group == "secret":
            for verb in (lister, setter, deleter):
                verb.add_argument("--dependabot", action="store_true",
                                  help="the store Dependabot reads (GitHub only), not the CI's")
    return p


# `var` / `secret` groups -> the adapter's `kind`.
VARIABLE_KINDS = {"var": "variable", "secret": "secret"}
# The one name shape every forge accepts (GitHub's is the narrowest: letters, digits, `_`, no leading digit).
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}")
_ENV_MAX = 255


def _read_value(args) -> str:
    """The value to set: `--value`, or `--value-file` (`-` = stdin) less ONE trailing newline (`echo` adds one)."""
    if getattr(args, "value", None) is not None:
        value = args.value
    else:
        try:
            if args.value_file == "-":
                value = sys.stdin.read()
            else:
                with open(args.value_file, encoding="utf-8") as handle:
                    value = handle.read()
        except OSError as exc:
            # Never the path: a secret typed where its file belongs would land on stderr.
            raise Misuse(f"cannot read --value-file: {exc.strerror or type(exc).__name__}") from None
        value = value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith("\n") else value
    if not value:
        raise Misuse("the value is empty: no forge stores an empty variable or secret")
    return value


def _variables(args, ctx: "Context") -> None:
    kind = VARIABLE_KINDS[args.group]
    name = getattr(args, "name", None)
    if name is not None and not _VARIABLE_NAME.fullmatch(name):
        raise Misuse(f"{name!r} is not a variable name: letters, digits and `_`, not starting with a digit")
    env = args.env
    if env is not None and (not env.strip() or env != env.strip() or len(env) > _ENV_MAX):
        raise Misuse(f"--env {env!r} is not an environment name")
    if env == "*":
        raise Misuse("--env '*' is every environment: omit --env for the repository level")
    dependabot = getattr(args, "dependabot", False)
    if dependabot and env is not None:
        raise Misuse("--dependabot is the repository's Dependabot store, which has no environments: omit --env")
    adapter = ctx.adapter()
    if dependabot and adapter.login.forge != "github":
        raise Misuse(f"secret --dependabot: Dependabot secrets are GitHub-only, not {adapter.login.label}")
    value = _read_value(args) if args.verb == "set" else None
    scope = "dependabot" if dependabot else f"environment:{env}" if env is not None else "repository"
    store = {"dependabot": True} if dependabot else {}
    rights = adapter.DEPENDABOT_RIGHTS if dependabot else adapter.VARIABLE_RIGHTS
    try:
        if args.verb == "list":
            ctx.emit(adapter.variables(kind, env, **store))
        elif args.verb == "set":
            ctx.emit({"kind": kind, "name": name, "scope": scope,
                      **adapter.set_variable(kind, name, value, env, **store)})
        else:
            ctx.emit({"kind": kind, "name": name, "scope": scope, "deleted": True,
                      **adapter.delete_variable(kind, name, env, **store)})
    except (ForgeError, CredentialRefused) as exc:
        if exc.status != 403 or exc.rate_limited:
            raise
        # The forge refused the right, not the repository: say exactly which right a person must grant, and keep
        # the forge's own words, which tell one missing grant from another.
        said = " ".join((exc.detail or "").split())[:FORGE_ERROR_CAP]
        raise CredentialRefused(
            f"{adapter.login.label} refused this login's {kind}s on {adapter.login.repo_path} (HTTP 403"
            f"{': ' + said if said else ''}). It needs {rights} — a person must grant that, "
            f"then re-run") from None


def _misuse_hint(argv: list[str]) -> "str | None":
    if len(argv) >= 2 and argv[0] == "pr" and argv[1] in _CLOSE_SYNONYMS:
        return f"no such verb 'pr {argv[1]}': use `pr close <n> --body-file <why.md>` on every forge"
    if len(argv) >= 2 and argv[0] == "run" and argv[1] not in ("list", "view", "rerun", "dispatch", "-h", "--help"):
        return (f"no such verb 'run {argv[1]}': `run` reads (`run list`, `run view`), re-runs (`run rerun`) "
                "and starts a workflow (`run dispatch`); it never cancels")
    if len(argv) >= 2 and argv[0] == "api" and argv[1] not in ("get", "-h", "--help"):
        return f"no such verb 'api {argv[1]}': `api` is read-only (`api get <path>`)"
    return None


def _read_body(path: str) -> str:
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise Misuse(f"cannot read --body-file {path!r}: {exc.strerror or exc}") from None


class Context:
    """Everything a verb touches outside its arguments — the seams tests replace."""

    def __init__(self, *, environ=None, transport=None, governance=None, cwd=None, sleep=time.sleep,
                 stdout=None, stderr=None, release_check=None) -> None:
        self.environ = os.environ if environ is None else environ
        # `(head, base) -> answer`: the control plane's `release.authorize` (`_release_ok`); no base is the dispatch
        # question, which says so (`policy.check_dispatch`).
        self.release_check = release_check or (
            lambda head, base: agent_socket.call("release.authorize", {"head": head, "base": base} if base
                                                 else {"head": head, "purpose": "dispatch"}))
        self.transport = transport or Transport()
        self.governance = governance or policy.governance
        self.cwd = cwd or os.getcwd()
        self.sleep = sleep
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr

    def adapter(self):
        login = login_mod.fetch(self.environ, self.cwd)
        policy.check_checkout(self.cwd, login)
        return for_login(login, self.transport, lambda: login_mod.fetch(self.environ, self.cwd, fresh=True),
                         lambda: login_mod.report_rejected(self.environ, login))

    def emit(self, value) -> None:
        self.stdout.write(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")


def _limit(value: int) -> int:
    if not 1 <= value <= PAGE_MAX:
        raise Misuse(f"--limit must be 1..{PAGE_MAX}: one page is the most a list reads")
    return value


def _head(ctx: Context, explicit: "str | None") -> str:
    head = explicit or policy.current_branch(ctx.cwd)
    if not head:
        raise Misuse("no branch is checked out here; pass --head")
    return head


def _observe(ctx: Context, pr, *, opened: bool) -> None:
    """Tell this runner's task about the PR the forge just answered for (`pull_request.observed`; the CP upserts
    its `task_prs` row). Best-effort and runners only: the forge call already happened, so nothing here may
    change this command's output or exit code. The checkout, never an argument, names the repository — the
    daemon judges it against the runner's own slot. `opened`: `pr create` — only a PR the task opened gets a row;
    a view updates one."""
    if not ctx.environ.get("CONTROL_TASK_ID"):
        return
    try:
        agent_socket.call("pull_request.observed", {"pr_url": pr.url, "pr_number": pr.number, "state": pr.state,
                                               "opened": opened, "checkout": ctx.cwd})
    except Exception as exc:  # noqa: BLE001 — a lost observation must never fail the forge command
        print(f"jaah-forge: note: this PR was not recorded on the task ({type(exc).__name__})", file=ctx.stderr)


def _release_ok(ctx: Context):
    """`policy.check_branches`' release question, for a runner only: the control plane answers whether this task
    carries an open release from `head` into `base` (`release.authorize`, `app/releases/runs.py::authorize`). Any
    failure to ask is a no (fail closed)."""
    def ask(head: str, base: str) -> bool:
        if not ctx.environ.get("CONTROL_TASK_ID"):
            return False
        try:
            return bool((ctx.release_check(head, base) or {}).get("ok"))
        except Exception:  # noqa: BLE001 — an unanswered question refuses, never lets the PR through
            return False
    return ask


# What a gate reports when the branch moved between the change it judges and the run the forge started.
BRANCH_MOVED = "The branch moved before the run started"


def _check_started(started, sha: str) -> None:
    """`run dispatch --sha`: the started run must build `sha` — a forge starts a run on a branch's head, which may
    have moved past the change being judged. Either id may be abbreviated (Bitbucket's), so a prefix matches."""
    built = (started.head_sha or "").lower()
    if len(built) < 7:
        raise ForgeError(f"cannot tell whether the branch moved: the forge did not name the commit run "
                         f"{started.id or '(unnamed)'} builds, so it cannot be matched to {sha}")
    if not (built.startswith(sha) or sha.startswith(built)):
        raise ForgeError(f"{BRANCH_MOVED}: run {started.id} builds {built}, not {sha}")


def run(args, ctx: Context) -> None:
    verb = " ".join(filter(None, (args.group, getattr(args, "verb", None))))  # `checks` is a group with no verb
    policy.check_shadow(verb, args, ctx.environ)
    if args.group in VARIABLE_KINDS:
        _variables(args, ctx)
    elif verb == "pr create":
        body = _read_body(args.body_file)
        head = _head(ctx, args.head)
        # The login (the local broker) names the repo whose rules apply; every refusal is decided before the
        # forge itself is called.
        adapter = ctx.adapter()
        config = ctx.governance(ctx.cwd, adapter.login)
        base = args.base or config.integration_branch
        policy.check_branches(config, head, base, _release_ok(ctx))
        policy.check_text(config, adapter.login.forge, args.title, body, ctx.environ)
        pr = adapter.create(title=args.title, body=body, head=head, base=base, draft=args.draft)
        ctx.emit(pr.to_json())
        _observe(ctx, pr, opened=True)
    elif verb == "pr view":
        adapter = ctx.adapter()
        target = args.target
        number = int(target) if target is not None and is_id(target) else adapter.find(_head(ctx, target))
        # Only GitHub carries a status the lane posts (`pr status`), so elsewhere there is nothing to leave out.
        ignore = frozenset(args.ignore_check) if adapter.login.forge == "github" else frozenset()
        pr = adapter.view(number, ignore=ignore) if ignore else adapter.view(number)
        ctx.emit({**pr.to_json(), "body": pr.extra.get("body") or ""} if args.body else pr.to_json())
        _observe(ctx, pr, opened=False)
    elif verb == "pr checks":
        if args.interval < MIN_INTERVAL_S:
            raise Misuse(f"--interval must be at least {MIN_INTERVAL_S}s: every runner shares the forge's rate limit")
        adapter = ctx.adapter()
        polls = 0
        while True:
            items = adapter.checks(args.number)
            state = required_rollup(items)
            polls += 1
            # Right after a push a forge may not have registered any check yet: `none` is waited out briefly.
            if not args.watch or not (state == "pending" or (state == "none" and polls < NONE_GRACE_POLLS)):
                break
            ctx.sleep(args.interval)
        # `checks` is what gates the merge; `checks_all` counts the optional checks too.
        ctx.emit({"number": args.number, "checks": state, "checks_all": rollup(items),
                  "items": [c.to_json() for c in items]})
    elif verb == "pr auto-merge" and args.disable:
        # Disarming never lands work, so no push policy: any caller may disarm.
        adapter = ctx.adapter()
        pr = adapter.view(args.number)
        policy.check_open(pr)
        ctx.emit((adapter.disable_auto_merge(pr) if pr.auto_merge else pr).to_json())
    elif verb in ("pr auto-merge", "pr merge"):
        sha = (getattr(args, "sha", None) or "").lower()
        if sha and not _SHA.fullmatch(sha):
            raise Misuse(f"--sha {args.sha!r} is not a full commit id")
        adapter = ctx.adapter()
        config = ctx.governance(ctx.cwd, adapter.login)
        policy.check_push(config)
        pr = adapter.view(args.number)
        policy.check_target(config, pr, _release_ok(ctx))
        if verb == "pr merge":
            if sha:
                # The full head commit: a forge whose view abbreviates it (Bitbucket) resolves it first.
                pr = dataclasses.replace(pr, extra={**pr.extra, "head_sha": adapter.head_commit(pr)})
                policy.check_head(config, pr, sha)
                pr = dataclasses.replace(pr, extra={**pr.extra, "head_sha": sha})
            policy.check_mergeable(config, pr)
            ctx.emit(adapter.merge(pr).to_json())
        else:
            try:
                done = adapter.auto_merge(pr)
            except AlreadyMergeable:
                # Nothing left to wait for, so nothing to arm: merge now, under `pr merge`'s own checks.
                pr = adapter.view(args.number)
                policy.check_target(config, pr, _release_ok(ctx))
                policy.check_mergeable(config, pr)
                ctx.stderr.write(f"jaah-forge: PR #{pr.number} can merge now, so it was merged instead of armed\n")
                ctx.emit(adapter.merge(pr).to_json())
                return
            if not done.auto_merge:
                ctx.stderr.write(
                    f"jaah-forge: auto-merge is not armed ({adapter.login.label} has no native auto-merge, "
                    f"or this repository turned it off) — watch with "
                    f"`jaah-forge pr checks {pr.number} --watch`, then `jaah-forge pr merge {pr.number}`\n")
            ctx.emit(done.to_json())
    elif verb == "pr edit":
        if args.title is None and args.body_file is None:
            raise Misuse("pr edit needs --title and/or --body-file")
        body = _read_body(args.body_file) if args.body_file is not None else None
        adapter = ctx.adapter()
        config = ctx.governance(ctx.cwd, adapter.login)
        policy.check_text(config, adapter.login.forge, args.title or "", body or "", ctx.environ)
        ctx.emit(adapter.edit(args.number, title=args.title, body=body).to_json())
    elif verb == "pr comment":
        body = _read_body(args.body_file)
        adapter = ctx.adapter()
        policy.check_quick_actions(None, adapter.login.forge, body)
        ctx.emit(adapter.comment(args.number, body))
    elif verb == "pr status":
        sha = (args.sha or "").lower()
        if sha and not _SHA.fullmatch(sha):
            raise Misuse(f"--sha {args.sha!r} is not a full commit id")
        if not args.context.strip():
            raise Misuse("--context names the status, e.g. merge-lane")
        adapter = ctx.adapter()
        if adapter.login.forge != "github":
            raise Misuse(f"pr status: commit statuses are GitHub-only so far, not {adapter.login.label}")
        # A green status can let a PR merge, so it is judged like a merge: push allowed, the PR open and governed.
        config = ctx.governance(ctx.cwd, adapter.login)
        policy.check_push(config)
        pr = adapter.view(args.number)
        policy.check_target(config, pr, _release_ok(ctx))
        if sha:
            policy.check_head(config, pr, sha)
        ctx.emit({"number": pr.number, **adapter.status(sha or pr.extra["head_sha"], state=args.state,
                                                         context=args.context.strip(),
                                                         description=args.description[:STATUS_DESCRIPTION_MAX])})
    elif verb == "pr ready":
        adapter = ctx.adapter()
        pr = adapter.view(args.number)
        policy.check_open(pr)
        ctx.emit((adapter.ready(pr.number) if pr.draft else pr).to_json())
    elif verb == "pr close":
        body = _read_body(args.body_file)
        if not body.strip():
            raise Misuse("pr close needs a reason in --body-file: a close nobody explained reads as done")
        adapter = ctx.adapter()
        policy.check_quick_actions(None, adapter.login.forge, body)
        pr = adapter.view(args.number)
        policy.check_open(pr)
        # The reason goes up FIRST: a close emits no notification of its own (PR #1827).
        adapter.comment(pr.number, body)
        ctx.emit(adapter.close(pr.number).to_json())
    elif verb == "pr stats":
        ctx.emit(ctx.adapter().diff_stats(args.number).to_json())
    elif verb == "pr provenance":
        try:
            ctx.emit(ctx.adapter().provenance(args.number).to_json())
        except NotImplementedError:
            raise Misuse("unsupported_forge: this forge reports no PR provenance") from None
    elif verb == "checks":
        sha = args.sha.lower()
        if not _ANY_SHA.fullmatch(sha):
            raise Misuse(f"--sha {args.sha!r} is not a commit id (7 to 64 hex digits)")
        ctx.emit({"sha": sha, "checks": [c.to_json() for c in ctx.adapter().checks_for_sha(sha)]})
    elif verb == "pr list" and args.state == "merged":
        since = parse_time(args.since)
        if args.since is None:
            raise Misuse("--state merged needs --since <ISO-8601>: a merged list is a window, never the whole history")
        if since is None:
            raise Misuse(f"--since {args.since!r} is not an ISO-8601 time")
        if not 1 <= args.limit <= MERGED_MAX:
            raise Misuse(f"--limit must be 1..{MERGED_MAX} with --state merged")
        if args.author is not None or args.head is not None or args.base is not None:
            raise Misuse("--state merged takes no --author, --head or --base")
        ctx.emit(ctx.adapter().merged_prs(since, args.limit))
    elif verb == "pr list":
        if args.since is not None:
            raise Misuse("--since needs --state merged")
        limit = _limit(args.limit)
        if args.author is not None and not _AUTHOR.fullmatch(args.author):
            raise Misuse(f"--author {args.author!r} is not a forge username")
        for flag, value in (("--head", args.head), ("--base", args.base)):
            if value is not None and not policy.is_branch_name(value):
                raise Misuse(f"{flag} {value!r} is not a branch name")
        ctx.emit(ctx.adapter().prs(args.state, args.author, limit, head=args.head, base=args.base))
    elif verb == "run list":
        limit = _limit(args.limit)
        if args.workflow is not None and not args.workflow.strip():
            raise Misuse("--workflow names the CI config file (e.g. ci.yml); omit it for every workflow")
        ctx.emit([r.to_json() for r in ctx.adapter().runs(args.workflow, args.branch, limit)])
    elif verb == "run view":
        head, jobs, notes = ctx.adapter().run(args.run_id)
        ctx.emit({**head.to_json(), "jobs": [j.to_json() for j in jobs],
                  "annotations": [a.to_json() for a in notes]})
    elif verb == "run rerun":
        adapter = ctx.adapter()
        config = ctx.governance(ctx.cwd, adapter.login)
        policy.check_push(config)  # before the network; `check_rerun` re-checks it beside the branch
        head = adapter.run_head(args.run_id)
        policy.check_rerun(config, head, ctx.environ.get("CONTROL_TASK_ID") or "")
        new_id = adapter.rerun(head, args.failed)
        ctx.emit({"id": new_id, "rerun_of": head.id, "branch": head.branch, "failed_only": args.failed})
    elif verb == "run dispatch":
        workflow = args.workflow.strip()
        sha = (args.sha or "").lower()
        if not workflow or ".." in workflow or any(ord(c) < 32 for c in workflow):
            raise Misuse(f"{args.workflow!r} is not a workflow name (e.g. mirrors.yml)")
        if sha and not _ANY_SHA.fullmatch(sha):
            raise Misuse(f"--sha {args.sha!r} is not a commit id (7 to 64 hex digits)")
        if not policy.is_branch_name(args.ref):
            raise Misuse(f"--ref {args.ref!r} is not a branch name")
        inputs: dict[str, str] = {}
        for pair in args.inputs:
            key, sep, value = pair.partition("=")
            if not sep or not key.strip():
                raise Misuse(f"-f {pair!r} is not key=value")
            if key.strip() in inputs:
                raise Misuse(f"-f names input {key.strip()!r} twice")
            inputs[key.strip()] = value
        adapter = ctx.adapter()
        forge = adapter.login.forge
        # A workflow file and a custom pipeline are names in a URL path or a selector, never a path of their own;
        # a GitLab job name is a variable's value, so it may carry `/`.
        if forge != "gitlab" and "/" in workflow:
            raise Misuse(f"{args.workflow!r} is not a workflow name (e.g. mirrors.yml)")
        if forge == "gitlab" and GATE_VARIABLE in inputs:
            raise Misuse(f"-f {GATE_VARIABLE} is set from the workflow argument: drop it")
        policy.check_dispatch(ctx.governance(ctx.cwd, adapter.login), args.ref,
                              ctx.environ.get("CONTROL_TASK_ID") or "", _release_ok(ctx))
        started = adapter.dispatch(workflow, args.ref, inputs)
        ctx.emit({"workflow": workflow, "ref": args.ref, "inputs": inputs, "dispatched": True, **started.to_json()})
        if sha:
            _check_started(started, sha)
    elif verb == "ci log":
        ctx.emit(ctx.adapter().ci_log(args.check_id))
    elif verb == "api get":
        adapter = ctx.adapter()
        resp = adapter.client.raw("GET", args.path, accept="application/json")
        try:
            ctx.emit(json.loads(resp.body))
        except ValueError:
            ctx.emit(resp.body.decode("utf-8", errors="replace"))
    else:  # pragma: no cover — argparse admits nothing else
        raise Misuse(f"no such verb {verb!r}")


def main(argv: "list[str]", ctx: "Context | None" = None, stderr=None) -> int:
    ctx = ctx or Context()
    if stderr is not None:
        ctx.stderr = stderr
    try:
        hint = _misuse_hint(argv)
        if hint:
            raise Misuse(hint)
        args = _parser().parse_args(argv)
        run(args, ctx)
    except CliExit as exc:
        print(f"jaah-forge: {exc.message}", file=ctx.stderr)
        return exc.code
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, AssertionError,
            http.client.HTTPException) as exc:
        # An answer shaped unlike the forge's documented one. The type only: an exception's text can quote
        # a request header, and the Authorization header is one.
        print(f"jaah-forge: unexpected answer from the forge ({type(exc).__name__})", file=ctx.stderr)
        return EXIT_FORGE
    return 0
