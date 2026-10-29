#!/usr/bin/env python3
"""Pre-push guard for a managed repo whose project row restricts what we may push
(its `branch_prefix` and `push_enabled`). Standalone and stdlib-only: install-access-guard.py copies it
byte-for-byte into the target clone's .git/hooks/pre-push, where nothing else of ours exists.

The rules come from the clone's own git config, written by install-access-guard.py (tier 2 of the
contract in tools/project_config.py — a hook in a foreign clone cannot reach our API):
  • `access-guard.push-enabled` — false blocks every push (layer 1 of 2; see the policy's "Residual gap").
  • `access-guard.branch-prefix` — stored with its trailing slash, or '' for none. Non-empty is an
    allowlist: only branches under the prefix may be pushed.
  • Both present, push enabled, empty prefix -> unrestricted; stdin is never read.
  • `access-guard.protect-integration` — opt-in, written only for a MicroVM session clone
    (`microvm_session`): `true` also refuses a push to the integration branch, `master` and `main`, whatever
    the prefix. Unset (every other clone) changes nothing.
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
# Where the node's signed package installs the resolver; the refusal's command line comes from it.
PACKAGED_PROJECT_CONFIG = "/usr/lib/jaahd/tools/project_config.py"
# The Architectural Guard's hook, from the same root-owned package (never the clone's own `tools/`).
PACKAGED_ARCHITECTURAL_GUARD = "/usr/lib/jaahd/tools/architectural_guard.py"
# Its lint rules may each run up to 120 s; past this the push goes ahead (the guard never blocks on itself).
ARCHITECTURAL_GUARD_TIMEOUT_S = 300
# `architectural_guard.PRE_PUSH_REFUSED`, restated because this file is copied alone into a clone: the ONLY exit code
# that refuses — an uncaught error or a usage error (exit 1/2) is the guard's own failure and lets the push through.
ARCHITECTURAL_GUARD_REFUSED = 3


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


def check_prefix(lines: list[str], prefix: str, integration_branch: str = "") -> int:
    allowed_ref_prefix = f"refs/heads/{prefix}"
    blocked = False
    saw_ref = False
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[2]:
            continue
        saw_ref = True
        remote_ref = parts[2]
        if not remote_ref.startswith(allowed_ref_prefix):
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


def check_protected(lines: list[str], integration_branch: str, prefix: str) -> int:
    """Refuse a push whose DESTINATION is the integration branch, `master` or `main`. No ref tuples at all
    fails closed, as `check_prefix` does."""
    protected = {f"refs/heads/{b}" for b in (integration_branch, *ALWAYS_PROTECTED) if b}
    saw_ref, blocked = False, False
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[2]:
            continue
        saw_ref = True
        if parts[2] in protected:
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
    protect = parse_push_enabled(read_config(PROTECT_INTEGRATION_CONFIG_KEY)) is True
    lines = None
    if prefix or protect:
        # Read AFTER the two rule markers. It changes no verdict of its own — only the message, and, when
        # protection was opted into, which branch that protection names.
        integration_branch = read_config(INTEGRATION_BRANCH_CONFIG_KEY) or ""
        lines = list(sys.stdin)
        if protect and check_protected(lines, integration_branch, prefix):
            return 1
        if prefix and check_prefix(lines, prefix, integration_branch):
            return 1
    # Synced AND unrestricted: stdin is read only by a guarded session's Architectural Guard step.
    return architectural_guard(lines)


if __name__ == "__main__":
    sys.exit(main())
