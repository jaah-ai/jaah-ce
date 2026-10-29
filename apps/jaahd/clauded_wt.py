#!/usr/bin/env python3
"""clauded_wt.py — open an INTERACTIVE clauded in an EXISTING worktree, as a tab in this node's fleet
tmux console. Backs the shorthand "clauded {project} {worktree}".

    python3 apps/jaahd/clauded_wt.py <project> [N]

    <project>  portfolio name, shorthand OK (client-analysis -> client-analysis). Resolved to a directory
               via the control-plane API: THIS node's worktree row (`/api/agent/worktrees` → `path`), falling
               back to the clone path computed LOCALLY from the project's `id` plus this node's
               own `JAAH_HOME` (`env_helpers.resolve_clone_root`) when the slot has no row.
    N          worktree number -> the existing pool worktree (the `main` clone's numbered
               sibling by convention). Omit -> the main clone itself.

Unlike a fleet task run, this attaches NO Stop hook: the session is interactive and parks at the
prompt for you to drive. It only ever opens an EXISTING worktree — it never creates one (errors if the
path is missing)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

import agent_providers  # the provider vocabulary: what a tab with no named provider is
import agents  # THE runner-adapter registry — this tab's argv comes from it, never from a literal
import cloud_modules  # the `compliance` feature's Architectural Guard, by name
# The single home of outward-surface env var names; a stdlib-only leaf.
import environment
import session_user  # the uid-split seam: `_meta` must be created as the session user, not the daemon
# The single home of the securestorage env var's spelling; a stdlib-only leaf, so imported at module
# scope. Never re-spell the name: the CLI matches exactly, so a typo is a tab on nobody's credentials.
import shared_storage

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def console_model(provider: str, card_value: str) -> str:
    """The `--model` a console tab passes for the project card's `default_model`, which holds a
    vendor-neutral TIER (`apps/api/app/model_catalog.py`): the provider's moving alias for it
    (`agent_providers.<X>.tier_aliases`), or "" — the CLI's own default — for a provider with no alias,
    whose catalog resolution is the control plane's and happens only for a dispatched task. A value that is
    not a tier passes through unchanged."""
    record = {p.value: p for p in agent_providers.PROVIDERS}.get(provider)
    tiers = {tier for p in agent_providers.PROVIDERS for tier, _alias in p.tier_aliases}
    value = card_value.strip()
    if value.lower() not in tiers:
        return value
    return dict(record.tier_aliases).get(value.lower(), "") if record is not None else ""


def console_argv(adapter, model: str) -> str:
    """This tab's own CLI invocation, as one quoted shell word sequence.

    The argv is the adapter's, whole — this function knows no binary and no flag. `shq_nested`, not
    `shq`: every element lands inside `main`'s `bash -lc '...'`, where `shq` would break out to the
    host shell. It REFUSES what it cannot spell, so `main` calls this before its first side effect."""
    from fleet_launch import shq_nested

    return " ".join(shq_nested(a) for a in adapter.base_argv(model=model))


def console_inner(
    *, argv: str, guard_prefix: str, auth_export: str, secure_export: str,
    rc_label: str, settings_flag: str, key_preflight: str = "",
) -> str:
    """The whole `bash -lc '...'` command the tmux window runs.

    Every argument arrives ALREADY QUOTED for that nesting; `settings_flag` is not re-quoted (that would
    corrupt it). `--rc` is the one literal: the interactive-console marker, owned by no adapter method;
    an unregistered provider is refused before this point."""
    return (
        f"bash -lc '{guard_prefix}{auth_export}{secure_export}{key_preflight}"
        f"exec {argv} --rc {rc_label}"
        f"{(' ' + settings_flag) if settings_flag else ''}'"
    )


# How long a new API-key tab waits for the broker to serve its key before refusing to start: the pane's
# key grant is recorded by `ctl.py tmux-window` just AFTER the pane spawns, so the first ask can precede it.
_KEY_PREFLIGHT_TRIES = 30
_KEY_PREFLIGHT_SLEEP = "0.5"


def key_preflight(api_key_account: str) -> str:
    """The `bash -lc` fragment that waits (bounded) until the broker serves this tab's key, and stops the
    tab loudly if it never does — else the CLI starts and fails on its first request, far from the cause.
    The key goes to /dev/null. Every interpolated element is `shq_nested` (it lands inside `'...'`)."""
    import account_auth
    from fleet_launch import shq_nested

    helper = (f"{shq_nested(account_auth.CREDENTIAL_HELPER)} "
              f"{shq_nested(account_auth.NODE_CREDENTIAL_PREFIX + api_key_account)}")
    return (
        f"n=0; until {helper} >/dev/null 2>&1; do n=$((n+1)); "
        f"if [ $n -ge {_KEY_PREFLIGHT_TRIES} ]; then "
        f"echo \"clauded-wt: the credential broker served no API key for this tab - not starting\" >&2; "
        f"exit 1; fi; sleep {_KEY_PREFLIGHT_SLEEP}; done; "
    )


def bedrock_preflight(plan) -> str:
    """`key_preflight`'s Jaah-account twin: wait (bounded) until the broker serves this tab's session
    credentials, and stop the tab loudly if it never does. The credentials go to /dev/null."""
    import bedrock_launch
    from fleet_launch import shq_nested

    helper = bedrock_launch.export_command(plan, shq_nested)
    return (
        f"n=0; until {helper} >/dev/null 2>&1; do n=$((n+1)); "
        f"if [ $n -ge {_KEY_PREFLIGHT_TRIES} ]; then "
        f"echo \"clauded-wt: the credential broker served no Bedrock credentials for this tab - not starting\" >&2; "
        f"exit 1; fi; sleep {_KEY_PREFLIGHT_SLEEP}; done; "
    )


def _write_bedrock_settings(plan, *, wt: str, name: str, with_hooks: bool, guard: bool) -> tuple[str, str]:
    """`_write_api_key_settings`' Jaah-account twin: the tab's hooks (a numbered slot's) plus
    `bedrock_launch.settings` (`fleet_dispatch.with_bedrock_settings`), written as the session user."""
    import fleet_dispatch

    return _write_tab_settings(wt, name, with_hooks, guard, agent_providers.DEFAULT_PROVIDER,
                               fleet_dispatch.with_bedrock_settings, bedrock=plan)


def _write_api_key_settings(provider: str, key_helper: str, *, wt: str, name: str,
                            with_hooks: bool, guard: bool) -> tuple[str, str]:
    """`(error, settings flag)` for an API-key tab: the `role="user"` hooks a numbered slot's tab carries
    (the same `build_settings` call `ctl.py autoclose-flag` makes), plus the adapter's `api_key_settings`
    (`fleet_dispatch.with_api_key_settings`), written as the session user into `_meta`. The main clone's
    tab carries only the key setting."""
    import fleet_dispatch

    return _write_tab_settings(wt, name, with_hooks, guard, provider,
                               fleet_dispatch.with_api_key_settings, provider=provider, key_helper=key_helper)


def _write_tab_settings(wt: str, name: str, with_hooks: bool, guard: bool, hooks_provider: str,
                        compose: Callable[..., tuple[str, str]], **account) -> tuple[str, str]:
    """`(error, settings flag)` for one tab's `_meta/clauded-<slot>.settings.json`, written as the session user:
    the `role="user"` hooks when `with_hooks`, composed with the account half by `compose(*hooks, settings_path=…,
    **account)` (`fleet_dispatch.with_api_key_settings` / `with_bedrock_settings`)."""
    import fleet_dispatch
    import fleet_launch

    meta = os.path.join(os.path.dirname(wt), "_meta")
    dir_err = session_user.ensure_dir_as_session_user(meta, mode=fleet_dispatch.META_DIR_MODE)
    if dir_err:
        return f"could not create {meta}: {dir_err}", ""
    settings_path = os.path.join(meta, f"clauded-{os.path.basename(wt)}.settings.json")
    hooks = (fleet_launch.build_settings(
        settings_path, "", wt, "user", hooks_dir=str(fleet_dispatch.HOOKS_DIR), autoclose=False,
        project_name=name, provider=hooks_provider, guard=guard) if with_hooks else (None, ""))
    content, flag = compose(*hooks, settings_path=settings_path, **account)
    err = session_user.write_file_as_session_user(settings_path, content, mode=fleet_dispatch.META_FILE_MODE)
    if err:
        return f"could not write {settings_path}: {err}", ""
    return "", flag


def parse_guard_env(raw: str) -> tuple[dict[str, str], str]:
    """`(exports, error)` for `--guard-env`: a JSON object of `guard_bundle.ENV_NAMES` keys to non-empty strings.
    `({}, "")` when absent. Anything else is refused rather than half-applied: an export the hooks do not know is
    not a guard."""
    if not raw:
        return {}, ""
    import json

    guard_bundle = cloud_modules.load("guard_bundle")
    if guard_bundle is None:
        return {}, "names the Architectural Guard, which this edition does not carry"
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}, "is not JSON"
    if (not isinstance(parsed, dict) or not parsed or not set(parsed) <= set(guard_bundle.ENV_NAMES)
            or not all(isinstance(v, str) and v for v in parsed.values())):
        return {}, f"must map {', '.join(guard_bundle.ENV_NAMES)} to non-empty strings"
    return dict(parsed), ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Leading flags, any order, each the CP's decision: `--config-dir` (the credential account; absent
    # is refused below — no node-side fallback: the CP is the sole lease authority), `--session` (the tmux
    # session; absent falls back to this node's default), `--securestorage-dir` (an older CP's view of the token store; accepted
    # and unread — this node resolves the store itself, `shared_storage.securestorage_dir`), `--provider`
    # (`accounts.provider`; absent is the catalogue default, and `agents.get_adapter` raises for a
    # provider with no adapter rather than opening another binary), `--api-key-account` (an API-key
    # account's SLUG, in place of `--config-dir`: such an account has no config dir, and its key is served
    # per request by the tab's `apiKeyHelper`, never exported), `--bedrock-plan` (the Jaah-account block,
    # K2, as JSON, in place of both: Claude Code on the platform Bedrock account, its credentials per
    # request through `awsCredentialExport`, never exported), `--guard-env` (the Architectural Guard's exports as a
    # JSON object, from the daemon's `guard_bundle.prepare_launch`; absent is an unguarded tab).
    config_dir = ""
    api_key_account = ""
    bedrock_raw = ""
    securestorage_dir = ""
    provider = agent_providers.DEFAULT_PROVIDER
    session_override = None
    guard_raw = ""
    while argv[:1] and argv[0] in ("--config-dir", "--api-key-account", "--securestorage-dir", "--session",
                                   "--provider", "--bedrock-plan", "--guard-env"):
        flag, argv = argv[0], argv[1:]
        if not argv:
            print(f"clauded-wt: {flag} needs a value", file=sys.stderr)
            return 2
        value, argv = argv[0], argv[1:]
        if flag == "--config-dir":
            config_dir = value
        elif flag == "--api-key-account":
            api_key_account = value
        elif flag == "--securestorage-dir":
            securestorage_dir = value
        elif flag == "--provider":
            provider = value
        elif flag == "--bedrock-plan":
            bedrock_raw = value
        elif flag == "--guard-env":
            guard_raw = value
        else:
            session_override = value
    if not argv:
        print(
            "usage: clauded_wt.py (--config-dir <dir> | --api-key-account <slug> | --bedrock-plan <json>) "
            "[--securestorage-dir <dir>] [--session <name>] "
            "[--provider <name>] [--guard-env <json>] <project> [worktree-number]",
            file=sys.stderr,
        )
        return 2
    guard_env, guard_err = parse_guard_env(guard_raw)
    if guard_err:
        print(f"clauded-wt: --guard-env {guard_err}", file=sys.stderr)
        return 2
    project = argv[0]
    number = argv[1] if len(argv) > 1 else ""
    if number and not number.isdigit():
        print(f"worktree must be a number, got: {number}", file=sys.stderr)
        return 2

    # Preflight the binaries the launch cannot proceed without, so no session is reported that never opened.
    if not shutil.which("tmux"):
        print("tmux required", file=sys.stderr)
        return 1
    # The adapter names the binary and builds the exec line. An unregistered provider is refused here
    # rather than opening a tab on the wrong binary.
    try:
        adapter = agents.get_adapter(provider)
    except agents.UnknownProvider as unknown:
        print(f"clauded-wt: {unknown}", file=sys.stderr)
        return 1
    binary = adapter.base_argv(model="")[0]
    # Ask the SESSION USER's PATH: the CLI is a per-user install in `~<session>/.local/bin`, which the
    # daemon's unit PATH must never include, so a bare which() would falsely refuse on uid-split nodes.
    # Not `runner_clis._resolves`: `which` checks "can THIS process exec it" (permissive — refusing a
    # console the user could open is the costly error), while `runner_clis` fails closed for the
    # runner's uid on a remote dispatch decision. Two consumers, two correct predicates.
    if not shutil.which(binary, path=session_user.delegated_path()):
        print(f"{binary} required", file=sys.stderr)
        return 1

    # Single-home fleet config (knobs + credential-pool paths); `source` semantics — see
    # env_helpers.load_fleet_env. Anything already in the environment wins.
    from env_helpers import fleet_ctl_path, load_fleet_env, resolve_clone_root

    load_fleet_env()
    ctl = fleet_ctl_path()

    import control_plane_api

    # The roster comes over the daemon's socket; `ctl.match_project_row` owns the matching.
    # `allow_shorthand=True` only because this is the operator-typed entry point. In-function import:
    # `ctl` drags the daemon core in.
    from ctl import project_via_socket

    proj, why = project_via_socket(project, allow_shorthand=True)
    if proj is None:
        # `why` tells a typo/ambiguous shorthand from an unreadable roster; this stderr is shown to the
        # operator verbatim.
        print(f"clauded-wt: could not resolve project '{project}': {why}", file=sys.stderr)
        return 1
    name = proj.get("name") or project
    if guard_env and proj.get("integration_branch"):
        # The base the push backstop measures a new branch from: the project row's integration branch, the
        # value a runner's slot carries as its `base`.
        guard_env[cloud_modules.load("guard_bundle").ENV_BASE] = str(proj["integration_branch"])
    # THIS node's main clone, computed from its JAAH_HOME + the project's id (the API has no
    # repo-root column). `""` when either input is unusable.
    clone = os.path.expanduser(resolve_clone_root(proj.get("id")))
    if not clone:
        print(
            f"cannot locate a clone for '{name}': needs the project's id on its roster row "
            f"(got {proj.get('id')!r}) and {environment.POOL_ROOT_ENV} set on this node",
            file=sys.stderr,
        )
        return 1

    # The row's path wins over the derived one: it records where the slot actually is (a relocated slot
    # is only discoverable there). Node-scoped, since a slot is (node_id, project, number). No row falls
    # back to the derivation, so a transport blip is not a refusal.
    wt_record = control_plane_api.agent_api_worktree(
        name, number or 0, control_plane_api.this_node_id(),
        project_id=proj.get("id") if isinstance(proj.get("id"), int) else None,
    )
    row_path = os.path.expanduser((wt_record or {}).get("path") or "")
    # A FOLDER slot — only an explicit `has_repo: false` on the row, so an older CP's record (no
    # field) and a transport blip keep the git rules. It is opened as an empty folder, never a git tree.
    folder = number not in ("", "0", None) and (wt_record or {}).get("has_repo") is False

    # Pick the worktree: main clone when N is omitted; else pool worktree N.
    if not number:
        wt, tag = row_path or clone, "main"
    else:
        # Derived only when the row is missing; shape mirrored from `worktree_mechanics.core.PoolConfig.wt_path`:
        # a `.../main` clone's slots are its numbered siblings. `<clone>-N` is the defensive fallback shape.
        base = clone.rstrip("/")
        derived = (
            f"{os.path.dirname(base)}/{number}" if os.path.basename(base) == "main" else f"{base}-{number}"
        )
        wt, tag = row_path or derived, f"wt{number}"
        if folder:
            # A console's own folder slot is made here, empty, on first open: the CP minted the row for this
            # session (`routes/console.py::console_new_session`) and nothing else materialises it.
            from worktree_mechanics import core, lifecycle

            try:
                lifecycle.ensure_folder(wt, name)
            except core.ResetRefused as exc:
                print(f"clauded-wt: {exc}", file=sys.stderr)
                return 1
        if not os.path.isdir(wt):
            print(
                f"no existing worktree #{number} for {name} at {wt} on node "
                f"{control_plane_api.this_node_id() or '<unset NODE_ID>'} "
                f"(create one: python3 apps/jaahd/ctl.py create {name})",
                file=sys.stderr,
            )
            return 1
    # A linked worktree's `.git` is a FILE. Accepted as it stands: asked through git, the daemon's uid is
    # refused the runner user's tree as "dubious ownership" (a macOS node), and trusting every
    # tree instead would let a slot's `.git/config` run code as the daemon.
    if not folder and not os.path.exists(os.path.join(wt, ".git")):
        check = subprocess.run(["git", "-C", wt, "rev-parse", "--git-dir"], capture_output=True)
        if check.returncode != 0:
            print(f"not a git worktree: {wt}", file=sys.stderr)
            return 1
    # abspath, NOT realpath: must match bash's LOGICAL `pwd`, or prepare-trust derives a different key than
    # set-title/the reconciler for a clone reached through a symlink.
    wt = os.path.abspath(wt)

    # Window label via the one composer, `fleet_launch.slot_window_name`; the Title arrives already
    # resolved on the worktree record. `name`/`number`, not `basename(wt)`, which collides across projects.
    resolved_title = ""
    try:
        import json

        wts = subprocess.run(
            [sys.executable, ctl, "worktrees"],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        if wts.returncode == 0:
            for rec in json.loads(wts.stdout or "[]"):
                if rec.get("_project") == name and rec.get("_number") == (number or "0"):
                    resolved_title = rec.get("title") or ""
                    break
    except Exception:
        resolved_title = ""

    from fleet_launch import slot_window_name

    win = slot_window_name(name, number, resolved_title)

    # The Jaah account: one validated plan, and nothing else beside it (D6 — the tenant's own accounts are
    # never used while it is on Jaah).
    bedrock = None
    if bedrock_raw:
        import bedrock_launch
        import json

        try:
            bedrock = bedrock_launch.parse_plan(json.loads(bedrock_raw))
        except (ValueError, bedrock_launch.PlanRefused) as refused:
            print(f"clauded-wt: --bedrock-plan cannot be launched: {refused}", file=sys.stderr)
            return 2
        if (config_dir or api_key_account or securestorage_dir
                or adapter is not agents.get_adapter(agent_providers.DEFAULT_PROVIDER)):
            print("clauded-wt: --bedrock-plan names the Jaah account, which runs Claude alone — no other "
                  "account, store or provider goes beside it", file=sys.stderr)
            return 2
    # The CP applied the quota/enabled gate when it chose the account. REFUSED WHEN ABSENT:
    # an unexported `CLAUDE_CONFIG_DIR` opens the session on the default, unauthenticated, ungated dir.
    if config_dir and api_key_account:
        print("clauded-wt: --config-dir and --api-key-account name two accounts — the control plane names "
              "one", file=sys.stderr)
        return 2
    if securestorage_dir and api_key_account:
        # A token store holds an OAuth token; an API-key tab has none to relocate.
        print("clauded-wt: --securestorage-dir belongs to an OAuth account, not --api-key-account",
              file=sys.stderr)
        return 2
    if not config_dir and not api_key_account and bedrock is None:
        print(
            "clauded-wt: no --config-dir — the control plane names the credential account for a console "
            "create, and refusing is the only safe answer without one (launching would use the default, "
            "ungated credential dir). An older control plane, or no account currently qualifies.",
            file=sys.stderr,
        )
        return 2

    from fleet_launch import (
        PROJECT_ID_ENV, PROJECT_NAME_ENV, RUNNER_PATH_PREFIX, bundle_bin, shq_nested, sources_compose_guard,
        tmux_session,
    )

    # Empty for an API-key account, so no `CLAUDE_CONFIG_DIR` is exported: the tab runs in the session
    # user's own `~/.claude`, as an API-key runner does (`fleet_dispatch.build_auth_lines`).
    adir = config_dir
    if bedrock is not None:
        import bedrock_launch

        # This node's Jaah home: the managed layer and the transcripts, never a credential. Without one the
        # tab still authenticates (the plan is in its environment) but opens in the session user's home.
        adir = bedrock_launch.ensure_config_dir()
        if not adir:
            print("clauded-wt: no Jaah home could be created on this node — the tab opens in the session "
                  "user's ~/.claude, without the managed plugins", file=sys.stderr)
        else:
            import fleet_dispatch

            # No roster sweep converges this home (it is no account), so the tab does, as a runner does.
            fleet_dispatch.converge_account_layer(adir)
    key_helper = ""
    if api_key_account:
        import fleet_dispatch

        key_helper = fleet_dispatch.api_key_helper_command(provider, api_key_account)
        if not key_helper:
            print(f"clauded-wt: provider {provider!r} takes no API key through its settings file — "
                  f"refusing to open a tab for API-key account {api_key_account!r}", file=sys.stderr)
            return 1
    # Where this account's token is: the token store, else its config dir (`shared_storage.token_dir`).
    # Only the store is named to the tab; a token only in the config dir exports nothing.
    securestorage_dir = shared_storage.securestorage_dir(config_dir) if config_dir else ""
    # A `shared_token_link` provider whose credential is in the store opens in its per-node home with the
    # credential linked into it. A failure is said, not fatal: the tab then opens unauthenticated.
    if securestorage_dir:
        import fleet_dispatch

        link_refusal = fleet_dispatch.ensure_shared_credential_link(adir, securestorage_dir, provider)
        if link_refusal:
            print(f"clauded-wt: {link_refusal}", file=sys.stderr)

    # Resolve the tmux session ONCE and pass it down explicitly: `_tmux_safe_env()` narrows what a
    # daemon-spawned launcher inherits, so re-deriving it in `ctl.py`/`vis_window` could disagree.
    session = tmux_session(session_override)

    # On the Jaah account the model is the plan's — one of the tenant's own profiles — never the card's.
    model = bedrock.model_arg if bedrock is not None else console_model(provider, subprocess.run(
        [sys.executable, ctl, "card-model", name],
        capture_output=True, text=True, env=os.environ.copy(),
    ).stdout.strip())
    # A Jaah tab names its own session id, so the live meter (`bedrock_usage_tail`) knows its transcript.
    native_session_id = str(uuid.uuid4()) if bedrock is not None else ""

    # `shq_nested`, NOT `shq`, at every site below: each lands inside the `bash -lc '...'`, where `shq`
    # would hand the rest of `default_model`/`projects.name` to the host shell (RCE). Judged before the
    # first side effect (`prepare-trust`, `_meta`), so a refusal leaves no trusted worktree behind.
    guard = str(_REPO_ROOT / "tools" / "compose-env-guard.sh")
    try:
        # The account env var is the adapter's answer, the console's half of
        # `fleet_dispatch.build_auth_lines`. `env()` returns nothing when no account was named.
        auth_export = "".join(
            f"export {var}={shq_nested(value)}; "
            for var, value in adapter.env(config_dir=adir, worktree=wt).items()
        )
        if bedrock is not None:
            import bedrock_launch

            # Per tab, inside its own `bash -lc` — never on the tmux server (see `bedrock_launch`).
            auth_export += (f"unset {' '.join(bedrock_launch.CLEARED_AWS_VARS)}; " + "".join(
                f"export {var}={shq_nested(value)}; " for var, value in bedrock_launch.env_pairs(bedrock)))
        # A token in the store is not under its config dir, so the securestorage var must be exported (name
        # from `shared_storage`). OMITTED, NEVER EMPTY: defined-but-empty resolves to `~/.claude` (see
        # `build_auth_lines`), and `shq_nested` raises on `""`. Not passed to `prepare-trust`: trust written into
        # a mounted store would apply fleet-wide.
        secure_export = (f"export {shared_storage.SECURESTORAGE_ENV}={shq_nested(securestorage_dir)}; "
                         if securestorage_dir else "")
        # The CLI's argv (including its model flag) comes from the adapter, composed inside the quoting
        # guard. `--model` now precedes `--rc`; a flag-with-value CLI parses both orders identically.
        cli_argv = console_argv(adapter, model)
        if native_session_id:
            import fleet_dispatch

            cli_argv += "".join(
                f" {shq_nested(arg)}" for arg in fleet_dispatch.session_args_for(adapter, native_session_id))
        bin_dirs = bundle_bin()
        # The runner's PATH prefix (`build_launch_sh`): `~/.local/bin` holds the CLI, which Linux's
        # `~/.profile` adds and the macOS runner user has no profile to.
        guard_prefix = ((f"export PATH=\"{RUNNER_PATH_PREFIX}\":{shq_nested(bin_dirs)}:\"$PATH\"; "
                         if bin_dirs else "")
                        + (f"source {shq_nested(guard)}; compose_env_guard {shq_nested(wt)} NODE_ID; "
                           if sources_compose_guard(guard) else ""))
        # The Architectural Guard's exports (`guard_bundle.env_for`), only for a guarded tab; inherited by every
        # hook the CLI runs.
        auth_export += "".join(f"export {var}={shq_nested(value)}; " for var, value in guard_env.items())
        if guard_env:
            guard_bundle = cloud_modules.load("guard_bundle")  # present: `parse_guard_env` admitted `guard_env`
            if guard_bundle.redirects_hooks(guard_env):
                auth_export += f"{guard_bundle.HOOKS_REDIRECT_SH}; "
        rc_label = shq_nested(f"{name}-{tag}")
        preflight = (key_preflight(api_key_account) if key_helper
                     else bedrock_preflight(bedrock) if bedrock is not None else "")
    except ValueError as quote_err:
        print(f"clauded-wt: refusing to open a tab for project {name!r}: {quote_err}", file=sys.stderr)
        return 1

    if os.environ.get("DRY_RUN", "0") == "1":
        print(
            f"would open: {win} -> {wt} (fleet '{session}') "
            f"account={'Jaah (Bedrock)' if bedrock is not None else adir or (api_key_account and 'apikey:' + api_key_account) or '<default>'} "
            f"model={model or '<inherit>'}"
        )
        return 0

    # Trust through the tab account's own adapter (`prepare`), as a dispatch seeds it: Claude Code's writes
    # its `.claude.json` record, a vendor with no such file writes none.
    subprocess.run(
        [sys.executable, ctl, "prepare-trust", wt, adir or "", provider, name], env=os.environ.copy(),
    )
    if bedrock is not None:
        print("  account: Jaah (Bedrock)")
    elif adir or api_key_account:
        print(f"  account: {adir or 'apikey:' + api_key_account}")
    if model:
        print(f"  model:   {model}")

    # THE CONSOLE START PATH TAKES NO HOLD: opening never leases, claims or resets the tree —
    # `worktrees.state` moves only by the CP's decision (a dispatch lease, or `slot_state.claim_for_user`).
    # It opens a window to look through, which is needed when a runner wedges before registering a
    # session. `role="user"` wires exactly one hook, the `SessionEnd` report (`user_session.ended`), and
    # the CP decides whether to free a `user` hold; `build_settings` gates the runner-status hooks and
    # StopFailure on `role == "runner"` strictly. VISIBLE_AUTOCLOSE=0 keeps the tab parked at the prompt.
    settings_flag = ""
    if bedrock is not None:
        # The session's credentials ride `awsCredentialExport` in this tab's own settings file.
        err, settings_flag = _write_bedrock_settings(bedrock, wt=wt, name=name, with_hooks=bool(number),
                                                     guard=bool(guard_env))
        if err:
            print(err, file=sys.stderr)
            return 1
    elif key_helper:
        # The account's key rides `apiKeyHelper` in this tab's own settings file (the runner's rule,
        # `fleet_dispatch.launch_runner`), so the key never enters the pane's environment.
        err, settings_flag = _write_api_key_settings(
            provider, key_helper, wt=wt, name=name, with_hooks=bool(number), guard=bool(guard_env))
        if err:
            print(err, file=sys.stderr)
            return 1
    elif number:
        meta = os.path.join(os.path.dirname(wt), "_meta")
        # As the session user (this runs as the daemon; `session_user` owns the ownership contract). A
        # refusal aborts: the account exports live in that file. Mode from `fleet_dispatch.META_DIR_MODE`,
        # its single home — the same `_meta` `launch_runner` writes, so the umask's 0775 would undo it.
        # Lazy import, like the other heavy siblings here.
        from fleet_dispatch import META_DIR_MODE

        dir_err = session_user.ensure_dir_as_session_user(meta, mode=META_DIR_MODE)
        if dir_err:
            print(f"could not create {meta}: {dir_err}", file=sys.stderr)
            return 1
        # `PROJECT_NAME_ENV` carries the resolved `projects.name` into `build_settings` (the path only
        # carries the repo name, which can differ). An env var because `autoclose-flag`'s positional argv
        # is shared with the fleet path.
        # `PROJECT_ID_ENV` beside it: the id the SessionEnd report addresses the slot by.
        env_no_autoclose = {
            **os.environ, "VISIBLE_AUTOCLOSE": "0", PROJECT_NAME_ENV: name,
            **({PROJECT_ID_ENV: str(proj["id"])} if isinstance(proj.get("id"), int) else {}),
        }
        settings_flag = subprocess.run(
            [sys.executable, ctl, "autoclose-flag",
             # Role "user": only the SessionEnd hook (see above).
             os.path.join(meta, f"clauded-{os.path.basename(wt)}.settings.json"), "", wt, "user",
             # Explicit, never read from the env: whether this tab's launch wrote a rule bundle.
             *(("--guard",) if guard_env else ())],
            capture_output=True, text=True, env=env_no_autoclose,
        ).stdout.strip()

    # No NODE_TOKEN is exported and none is needed: the session's verbs go over the daemon's
    # socket, so `_tmux_safe_env()` stripping it is the intended end state.

    # `settings_flag` is NOT re-quoted (it already carries its own `--settings <shq'd path>`). Safe even
    # though an API-writable field reaches that path (`wt` from `repo_url`): `shq`'s ordinary arm is
    # nesting-safe, and `shq_nested(wt)` above already refused the two arms that break out. Keep that order.
    inner = console_inner(
        argv=cli_argv, guard_prefix=guard_prefix, auth_export=auth_export,
        secure_export=secure_export, rc_label=rc_label, settings_flag=settings_flag,
        key_preflight=preflight,
    )
    # A failed tmux-window must not be followed by the success line + exit 0, or the Console reports a
    # session that does not exist.
    window_rc = subprocess.run(
        [sys.executable, ctl, "tmux-window", win, wt, inner, "--session", session,
         # The pane's key grant, recorded where its pid is known (`runner_registry.record_console_key`).
         *(("--api-key-account", api_key_account) if key_helper else ()),
         # A Jaah tab's grant and its live meter, recorded where its pid is known.
         *(("--bedrock-session", bedrock.session_id, "--claude-session", native_session_id,
            "--claude-config-dir", adir or session_user.default_claude_config_dir())
           if bedrock is not None else ()),
         # A folder's git ceiling, so git in it finds no ancestor repository (`fleet_dispatch.folder_git_ceiling`).
         *(("--git-ceiling", os.path.dirname(wt.rstrip("/"))) if folder else ())],
        env=os.environ.copy(),
    ).returncode
    if window_rc != 0:
        print(f"clauded-wt: could not open tmux window '{win}' (ctl.py tmux-window exit {window_rc})",
              file=sys.stderr)
        return 1
    print(f"▶ interactive clauded tab '{win}' in fleet console '{session}' · cwd {wt}")
    # How a HUMAN attaches — single-homed in `open_term.attach_command` (the fleet server is `jaah`'s,
    # so a bare `tmux attach` from a desktop shell starts an empty server).
    from open_term import attach_command

    print(f"  watch: {attach_command(session)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
