#!/usr/bin/env python3
"""Pre-push guard for a managed repo whose project row restricts what we may push
(its `branch_prefix` and `push_enabled`). Standalone and stdlib-only: install-access-guard.py copies it
byte-for-byte into the target clone's .git/hooks/pre-push, where nothing else of ours exists.

The rules come from the clone's own git config, written by install-access-guard.py (tier 2 of the
contract in tools/project_config.py — a hook in a foreign clone cannot reach our API):
  • `access-guard.push-enabled` — false blocks every push (layer 1 of 2; see the policy's "Residual gap").
  • `access-guard.branch-prefix` — stored with its trailing slash, or '' for none. Non-empty is an
    allowlist: only branches under the prefix may be pushed.
    A GENESIS push (`is_genesis`, below) may also write the integration branch, `master` and `main`.
  • Both present, push enabled, empty prefix -> unrestricted; stdin is never read.
  • `access-guard.protect-integration` — opt-in, written for a MicroVM session clone (`microvm_session`) and
    for a slot clone landed on an empty forge (`worktree_mechanics/lifecycle.py::guard_clone`): `true` also
    refuses a push to the integration branch, `master` and `main`, whatever the prefix. Unset (every other clone) changes nothing. Sole exception: a GENESIS push (`is_genesis`) —
    every ref's remote sha is the zero sha AND `git ls-remote --heads` of the remote git is pushing to lists no
    branch at all — goes through; an `ls-remote` that fails or times out refuses (branching-policy §Genesis).
  • `access-guard.allow-force-push` — the repository's Allow force push: `true` lets a push (forced or not)
    whose destination is `access-guard.integration-branch` through both checks above (`master`/`main` only
    when one of them IS that branch). Absent, unparseable, or no integration-branch marker -> false.
  • `access-guard.jaah-repo-prefix` — the project's `jaah_repo_prefix` (`<jaah org>/<org slug>-`): a push whose
    URL (argv[2]) is in that Jaah GitHub org but whose `owner/repo` does not start with the prefix is refused
    (`jaah_prefix_refusal`), whatever the other markers say. Unset or empty -> no check (an older control plane).
Markers absent or unparseable -> BLOCK, naming the fix: unrestricted is a positively written pair, so
absence can only mean an uninstalled or tampered guard. The prefix check reads the push DESTINATION ref
from stdin, never the local branch name; no ref tuples on a restricted repo fails closed.

The final step, run only once every check above passed: in an Architectural Guard session (`JAAH_GUARD_BUNDLE`
set, inherited through git's environment) the PACKAGED `architectural_guard.py --pre-push` judges the pushed
commits and may refuse them (its docstring is the contract). No bundle, or no packaged script -> no-op; a guard
that crashes or hangs never blocks (only its dedicated refusal code refuses). Where the repository runs a pre-push of
its own instead of this hook, a guarded session still reaches that step through `guard-pre-push.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse

PREFIX_CONFIG_KEY = "access-guard.branch-prefix"
PUSH_ENABLED_CONFIG_KEY = "access-guard.push-enabled"
# The one marker whose absence blocks nothing: it decides no push, it only lets a refusal name where work
# goes. Failing closed on it would turn a sync skew into a portfolio-wide push outage.
INTEGRATION_BRANCH_CONFIG_KEY = "access-guard.integration-branch"
# Opt-in, and only ever ADDS refusals: an unset or unparseable value is "off", never a block, so no clone
# installed before it existed is affected.
PROTECT_INTEGRATION_CONFIG_KEY = "access-guard.protect-integration"
#: Refused under `PROTECT_INTEGRATION_CONFIG_KEY` beside the integration branch: the operator's branches.
ALWAYS_PROTECTED = ("master", "main")
# Opt-in, and only ever REMOVES the integration-branch refusal: anything but a parseable true is "off".
ALLOW_FORCE_PUSH_CONFIG_KEY = "access-guard.allow-force-push"
# Only ever ADDS a refusal: unset or empty is "no check", never a block (`jaah_prefix_refusal`).
JAAH_REPO_PREFIX_CONFIG_KEY = "access-guard.jaah-repo-prefix"
# Where the node's signed package installs the resolver; the refusal's command line comes from it.
PACKAGED_PROJECT_CONFIG = "/usr/lib/jaahd/tools/project_config.py"
# The Architectural Guard's hook, from the same root-owned package (never the clone's own `tools/`).
PACKAGED_ARCHITECTURAL_GUARD = "/usr/lib/jaahd/tools/architectural_guard.py"
# Its lint rules may each run up to 120 s; past this the push goes ahead (the guard never blocks on itself).
ARCHITECTURAL_GUARD_TIMEOUT_S = 300
# `architectural_guard.PRE_PUSH_REFUSED`, restated because this file is copied alone into a clone: the ONLY exit code
# that refuses — an uncaught error or a usage error (exit 1/2) is the guard's own failure and lets the push through.
ARCHITECTURAL_GUARD_REFUSED = 3
# A genesis push (`is_genesis`): the remote shas a ref git does not have on the remote carries, and the bound on
# the `ls-remote` that proves the remote has no branch at all.
ZERO_SHAS = frozenset({"0" * 40, "0" * 64})
GENESIS_LS_REMOTE_TIMEOUT_S = 10
# The only `GIT_*` variables the genesis `ls-remote` inherits: how to reach an ssh remote. Every other one (a
# `GIT_DIR`, `GIT_CONFIG_PARAMETERS`/`GIT_CONFIG_COUNT` — a pusher's `-c url.<empty>.insteadOf=<forge>`) could
# point the check at another repository than the one pushed to.
GENESIS_GIT_ENV_KEPT = frozenset({"GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS"})


def read_config(key: str) -> str | None:
    """The raw value of `key` **in this repository's own config file**, or None when it is UNSET or
    has been tampered into ambiguity.

    None vs '' is load-bearing: '' is a written "no prefix restriction"; unset means uninstalled and blocks.

    `--local` is the guard's security boundary: git ranks `-c` and `GIT_CONFIG_KEY_*`/`VALUE_*` above
    the repo file (and `git push` exports `GIT_CONFIG_PARAMETERS` to its hook), so an unscoped read
    lets the pusher supply the policy (`git -c access-guard.branch-prefix= push ...`). `--get-all` with
    a length check refuses a second value appended by `git config --add`, which plain `--get` would
    return. No `-C`/GIT_DIR: git runs the hook with its own repo as the context already.
    """
    try:
        result = subprocess.run(
            ["git", "config", "--local", "--get-all", key],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines()]
    if len(values) != 1:
        return None
    return values[0]


def parse_push_enabled(raw: str | None) -> bool | None:
    """Git boolean spellings -> bool; None for unset or unparseable, and both block. Never a truthiness
    test: `bool('false')` is True."""
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in ("true", "1", "yes", "on"):
        return True
    if low in ("false", "0", "no", "off"):
        return False
    return None


def packaged_recipe(prefix: str, integration_branch: str) -> str | None:
    """`tools/project_config.py::delivery_recipe`'s line, from the node's PACKAGED copy, or None.

    Only the root-owned packaged copy is loaded — never the clone's own `tools/`, which whoever can write
    the clone controls. Message only: nothing here reaches the verdict, so any failure (no package on
    this host, an older one without the function) is None and the refusal names the step without a CLI."""
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_packaged_project_config", PACKAGED_PROJECT_CONFIG)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # Registered first: its `@dataclass` resolves annotations through `sys.modules[__module__]`.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.delivery_recipe(prefix, integration_branch)
    except Exception:  # noqa: BLE001 — a message helper must never turn a refusal into a crash
        return None


def refusal_recipe(prefix: str, integration_branch: str) -> list[str]:
    """The lines every refusal below ends with: what to do INSTEAD, in this clone's own values.

    The command itself is `delivery_recipe`'s (`packaged_recipe`), so a runner is told `jaah-forge`
    and anyone else `gh` from one home; this file restates no CLI line of its own. A refusal that names
    only the rejected ref teaches one rule per rejected push."""
    recipe = packaged_recipe(prefix, integration_branch) if integration_branch else None
    if recipe:
        return [f"   Land it instead: {recipe}"]
    lines = [f"   Push a branch of our own instead: git push origin HEAD:refs/heads/{prefix}<name>"]
    if integration_branch:
        lines.append(f"   Then open the PR against '{integration_branch}' from that branch.")
    else:
        # Stated rather than omitted: "not synced" differs from "nothing to say".
        lines.append("   The integration branch is not synced into this clone "
                     f"({INTEGRATION_BRANCH_CONFIG_KEY} unset) — read it off the project's control-plane row "
                     "before opening the PR.")
    return lines


def block_unconfigured(missing: str) -> int:
    print(f"❌ access-guard: {missing} in this clone's git config — refusing every push.")
    print("   The guard's rules are synced from the control plane and cannot be resolved here, so this")
    print("   clone cannot be shown to be pushable. Re-sync it:")
    print("     apps/jaahd/guards/install-access-guard.py <project> --branch-prefix <p> "
          "--push-enabled <true|false>")
    return 1


def block_push_disabled() -> int:
    print("❌ This repo has push_enabled = false: no push is ever allowed.")
    return 1


def remote_repo_path(url: str) -> str:
    """The forge path (`owner/repo`, `.git` stripped) of a push URL — https, ssh (`ssh://…`) or scp-style
    (`git@host:owner/repo.git`) — or "" for a local path or anything without a host."""
    url = (url or "").strip()
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        if not parts.netloc or parts.scheme.lower() == "file":
            return ""
        path = parts.path
    else:
        host, sep, path = url.partition(":")
        if not sep or not host or "/" in host:
            return ""
    path = urllib.parse.unquote(path).strip("/")
    return path[:-len(".git")] if path.lower().endswith(".git") else path


def jaah_prefix_refusal(path: str, prefix: str) -> str | None:
    """Why `path` (`owner/repo`) may not be pushed to under `prefix` (`<jaah org>/<org slug>-`), or None: a path
    whose owner is the Jaah org (case-insensitive) must start with the prefix (case-insensitive), with no dot or
    empty segment. Twin of `apps/jaahd/git_credential_helper.py::jaah_prefix_refusal`, restated because this
    file is copied alone into a clone."""
    prefix = (prefix or "").strip().lstrip("/")
    owner = prefix.split("/", 1)[0]
    if "/" not in prefix or not owner:
        return None
    repo = urllib.parse.unquote(path or "").strip("/")
    segments = repo.split("/")
    dotted = any(seg in ("", ".", "..") for seg in segments)
    # The owner is the first segment; with a dot or empty segment any segment naming the Jaah org counts, since
    # `./jaah-apps/x` reaches it once normalized (and such a path is refused outright below).
    if segments[0].casefold() != owner.casefold() and not (
            dotted and owner.casefold() in (seg.casefold() for seg in segments)):
        return None
    if dotted or not repo.casefold().startswith(prefix.casefold()):
        return f"{repo!r} is in the Jaah org {owner!r} but not this org's (its repositories start {prefix!r})"
    return None


def check_jaah_repo(remote: str, prefix: str) -> int:
    """1 (refused, printed) when the push URL `remote` is another org's repository in the Jaah org, else 0."""
    refusal = jaah_prefix_refusal(remote_repo_path(remote), prefix)
    if refusal is None:
        return 0
    print(f"❌ A push to {remote!r} is refused: {refusal}.")
    return 1


def check_prefix(lines: list[str], prefix: str, integration_branch: str = "", allow_integration: bool = False,
                 remote: str | None = None, genesis_of=None) -> int:
    """Refuse a push whose DESTINATION is outside the prefix — the integration branch excepted when
    `allow_integration` (Allow force push), and the integration branch, `master` and `main` excepted on a genesis
    push to `remote` (`genesis_of`, default `is_genesis`), so a prefixed project can bootstrap an empty
    repository."""
    allowed_ref_prefix = f"refs/heads/{prefix}"
    allowed_integration = f"refs/heads/{integration_branch}" if allow_integration and integration_branch else None
    genesis_refs = {f"refs/heads/{b}" for b in (integration_branch, *ALWAYS_PROTECTED) if b}
    genesis: bool | None = None  # asked once, and only when such a destination is pushed
    blocked = False
    saw_ref = False
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[2]:
            continue
        saw_ref = True
        remote_ref = parts[2]
        if remote_ref in genesis_refs and not remote_ref.startswith(allowed_ref_prefix):
            if genesis is None:
                genesis = (genesis_of or is_genesis)(lines, remote)
            if genesis:
                continue
        if not remote_ref.startswith(allowed_ref_prefix) and remote_ref != allowed_integration:
            print(f"❌ This repo is branch-prefix restricted: push to '{remote_ref}' is blocked.")
            print(f"   Only branches prefixed '{prefix}' may be pushed.")
            for extra in refusal_recipe(prefix, integration_branch):
                print(extra)
            blocked = True

    # No stdin tuples at all (e.g. a dry-run invocation) — fail closed, don't assume it's safe.
    if not saw_ref:
        print("❌ access-guard: no push refs read from stdin — refusing to allow by default.")
        return 1

    return 1 if blocked else 0


def is_genesis(lines: list[str], remote: str | None) -> bool:
    """True only for a new repository's first push (branching-policy §Genesis): EVERY ref line's remote sha is
    the zero sha (git holds no such ref on the remote) AND `git ls-remote --heads <remote>` — `remote` being the
    URL git pushes to, from its pre-push argv — answers within `GENESIS_LS_REMOTE_TIMEOUT_S` and lists no branch
    at all. A failure, a timeout, a malformed line or no lines is False: the push is refused (fail closed).
    `ls-remote` runs outside the repository (`cwd="/"`) with no inherited `GIT_*` but `GENESIS_GIT_ENV_KEPT`, so
    the clone's own config cannot rewrite the URL it asks, and a URL any remaining config rewrites
    (`ls-remote --get-url`) refuses; the node's credential helper, in the system config, still answers."""
    tuples = [line.split() for line in lines if line.strip()]
    if not remote or not tuples or any(len(parts) < 4 or parts[3] not in ZERO_SHAS for parts in tuples):
        return False
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in GENESIS_GIT_ENV_KEPT},
           "GIT_TERMINAL_PROMPT": "0"}

    def ls_remote(*args: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", "ls-remote", *args, "--end-of-options", remote], capture_output=True,
                                  text=True, check=False, stdin=subprocess.DEVNULL,
                                  timeout=GENESIS_LS_REMOTE_TIMEOUT_S, cwd="/", env=env)
        except (OSError, subprocess.TimeoutExpired):
            return None

    # An `insteadOf` left in any config git still reads (the user's own) would make `ls-remote` ask another
    # repository than the one pushed to: a URL that does not resolve to itself is never a genesis.
    resolved = ls_remote("--get-url")  # local: reads config only, so the forge round trip stays one 10 s bound
    if resolved is None or resolved.returncode != 0 or resolved.stdout.strip() != remote:
        return False
    proc = ls_remote("--heads")
    return proc is not None and proc.returncode == 0 and not proc.stdout.strip()


def check_protected(lines: list[str], integration_branch: str, prefix: str, allow_integration: bool = False,
                    remote: str | None = None, genesis_of=None) -> int:
    """Refuse a push whose DESTINATION is the integration branch, `master` or `main` — the integration branch
    excepted when `allow_integration` (Allow force push), and every protected ref excepted on a genesis push to
    `remote` (`is_genesis`). No ref tuples at all fails closed, as `check_prefix` does."""
    protected = {f"refs/heads/{b}" for b in (integration_branch, *ALWAYS_PROTECTED) if b}
    if allow_integration and integration_branch:
        protected.discard(f"refs/heads/{integration_branch}")
    saw_ref, blocked = False, False
    genesis: bool | None = None  # asked once, and only when a protected destination is pushed
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[2]:
            continue
        saw_ref = True
        if parts[2] in protected:
            if genesis is None:
                genesis = (genesis_of or is_genesis)(lines, remote)
            if genesis:
                continue
            print(f"❌ A push to '{parts[2]}' is refused: it is this project's integration branch or the "
                  "operator's.")
            for extra in refusal_recipe(prefix, integration_branch):
                print(extra)
            blocked = True
    if not saw_ref:
        print("❌ access-guard: no push refs read from stdin — refusing to allow by default.")
        return 1
    return 1 if blocked else 0


def architectural_guard(lines: list[str] | None, remote: str | None = None) -> int:
    """The final step: 1 only when the Architectural Guard refuses the pushed commits, else 0. `remote` defaults to
    this hook's own first argument; `guard-pre-push.py`, which runs this step from a guarded session's hook redirect,
    passes git's."""
    if not os.environ.get("JAAH_GUARD_BUNDLE") or not os.path.isfile(PACKAGED_ARCHITECTURAL_GUARD):
        return 0
    if lines is None:
        lines = list(sys.stdin)
    # The remote travels in the environment: in argv a remote URL/path of the pusher's choosing could read as a flag.
    env = {**os.environ, "JAAH_GUARD_REMOTE": remote or (sys.argv[1:2] or ["origin"])[0]}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", PACKAGED_ARCHITECTURAL_GUARD, "--pre-push"],
            input="".join(lines), text=True, check=False, timeout=ARCHITECTURAL_GUARD_TIMEOUT_S, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return 1 if proc.returncode == ARCHITECTURAL_GUARD_REFUSED else 0


def main() -> int:
    prefix = read_config(PREFIX_CONFIG_KEY)
    push_enabled = parse_push_enabled(read_config(PUSH_ENABLED_CONFIG_KEY))
    if prefix is None:
        return block_unconfigured(f"{PREFIX_CONFIG_KEY} is unset")
    if push_enabled is None:
        return block_unconfigured(f"{PUSH_ENABLED_CONFIG_KEY} is unset or unparseable")
    if not push_enabled:
        return block_push_disabled()
    jaah_prefix = read_config(JAAH_REPO_PREFIX_CONFIG_KEY) or ""
    if jaah_prefix and check_jaah_repo((sys.argv[2:3] or [""])[0], jaah_prefix):
        return 1
    protect = parse_push_enabled(read_config(PROTECT_INTEGRATION_CONFIG_KEY)) is True
    lines = None
    if prefix or protect:
        # Read AFTER the two rule markers. It changes no verdict of its own — only the message, which branch
        # protection names, and which branch Allow force push lets through (absent, none is).
        integration_branch = read_config(INTEGRATION_BRANCH_CONFIG_KEY) or ""
        allow = bool(integration_branch) and parse_push_enabled(read_config(ALLOW_FORCE_PUSH_CONFIG_KEY)) is True
        lines = list(sys.stdin)
        # The URL git pushes to (argv[2]), never the remote's name: its fetch URL, which `ls-remote` would read,
        # may differ from where the push goes.
        remote = (sys.argv[2:3] or [""])[0]
        verdicts: dict[bool, bool] = {}  # the forge is asked at most once per push, though both checks may ask

        def genesis_of(push_lines: list[str], url: str | None) -> bool:
            if True not in verdicts:
                verdicts[True] = is_genesis(push_lines, url)
            return verdicts[True]

        if protect and check_protected(lines, integration_branch, prefix, allow, remote, genesis_of):
            return 1
        if prefix and check_prefix(lines, prefix, integration_branch, allow, remote, genesis_of):
            return 1
    # Synced AND unrestricted: stdin is read only by a guarded session's Architectural Guard step.
    return architectural_guard(lines)


if __name__ == "__main__":
    sys.exit(main())
