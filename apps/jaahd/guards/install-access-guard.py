#!/usr/bin/env python3
"""Installs (or re-syncs, or reverts) the git-native push guard
in a managed project's local clone, for every project.

Usage: install-access-guard.py <project-name-or-clone-path> --branch-prefix <prefix>
                               --push-enabled <true|false> [--integration-branch <branch>]
                               [--allow-force-push <true|false>] [--protect-integration <true|false>]
                               [--jaah-repo-prefix <owner/slug->] [--clone-path <override>]
  <project-name-or-clone-path> — an exact project name in the control-plane API is tried FIRST (every
    primary-repository slot clone this node holds for it, via project_resolve.py); only then a
    literal clone path, so a same-named directory never shadows the DB-declared clone.
  --branch-prefix — the project's `branch_prefix`, WITH its trailing slash (`sn/`), or '' for none.
    Required even when empty: the DB owns the value, so there is no default here.
  --push-enabled — the project's `push_enabled`, `true` or `false`. Required.
  --integration-branch — optional, informational only (a refusal's advice). Omitted, the marker is left
    exactly as it was, so an older CP cannot wipe a value a newer one synced.
  --allow-force-push — optional, `true` or `false`: the repository's Allow force push. True lets a push
    (forced or not) to the integration branch through the hook. Omitted, the marker is left as it was
    (absent reads as false).
  --jaah-repo-prefix — optional: the project's `jaah_repo_prefix` (`<jaah org>/<org slug>-`); '' clears the marker.
    Omitted, name mode reads it off the project row; a clone path leaves the marker as it was.
  --protect-integration — optional: `true` writes the opt-in integration protection (the hook's docstring);
    `false` or omitted writes nothing — the marker only ever adds refusals and is never cleared here.

Steps (one idempotent call moves a clone to whatever the console declares, including unrestricted):
  1. copies access-guard-pre-push.py into .git/hooks/pre-push — the same bytes for any values.
  2. writes both markers explicitly, never unset: the hook blocks on an absent marker.
  3. push-enabled false: points `remote.origin.pushurl` at a disabled sentinel (layer 2 of 2; see the
     policy's "Residual gap"). The fetch URL is untouched.
  4. push-enabled true: unsets that pushurl only if it is our sentinel.

A worktree shares its main clone's .git/hooks and .git/config, so one install covers every worktree.
The console re-runs this whenever `branch_prefix` or `push_enabled` changes (`sync_access_guard`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_resolve  # noqa: E402
from control_plane_client import ControlPlaneApiError  # noqa: E402
from project_resolve import DISABLED_PUSH_URL  # noqa: E402

SELF_DIR = Path(__file__).resolve().parent
GUARD_SRC = SELF_DIR / "access-guard-pre-push.py"
PREFIX_CONFIG_KEY = "access-guard.branch-prefix"
PUSH_ENABLED_CONFIG_KEY = "access-guard.push-enabled"
# Informational only — see access-guard-pre-push.py for why its absence never blocks.
INTEGRATION_BRANCH_CONFIG_KEY = "access-guard.integration-branch"
# Opt-in integration-branch protection (the hook's docstring); only `apply_rules(protect_integration=True)` and
# `--protect-integration true` write it — the console sync never does.
PROTECT_INTEGRATION_CONFIG_KEY = "access-guard.protect-integration"
# The repository's Allow force push (access-guard-pre-push.py): absent or unparseable reads as false.
ALLOW_FORCE_PUSH_CONFIG_KEY = "access-guard.allow-force-push"
# The project's `jaah_repo_prefix` (access-guard-pre-push.py::jaah_prefix_refusal): unset when empty, never a block.
JAAH_REPO_PREFIX_CONFIG_KEY = "access-guard.jaah-repo-prefix"

USAGE = (
    "usage: install-access-guard.py <project-name-or-clone-path> --branch-prefix <prefix> "
    "--push-enabled <true|false> [--integration-branch <branch>] [--allow-force-push <true|false>] "
    "[--protect-integration <true|false>] [--jaah-repo-prefix <owner/slug->] [--clone-path <override>]"
)


def _push_url(clone: str) -> str:
    """The configured `remote.origin.pushurl` (git's effective answer: the last value), or '' if unset.
    Raw config, not `get-url --push`, which falls back to the fetch URL."""
    values = project_resolve.git_config_values(clone, "remote.origin.pushurl")
    return values[-1].strip() if values else ""


def normalize_prefix(raw: str) -> str:
    """A branch prefix as the marker must store it: stripped, and carrying a trailing slash when
    non-empty (`sn` -> `sn/`). The API normalizes on write; this catches a hand-run install."""
    prefix = raw.strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def apply_rules(
    clone: str, prefix: str, push_enabled: bool, integration_branch: str | None = None, *,
    protect_integration: bool = False, allow_force_push: bool | None = None, jaah_repo_prefix: str | None = None,
) -> list[str]:
    """Install the hook + both rule markers in `clone`, plus the informational integration-branch marker
    when one was supplied. Returns the steps taken; raises CalledProcessError / OSError.

    `integration_branch=None` or `""` leaves the marker untouched (the column is NOT NULL);
    `allow_force_push=None` leaves its marker untouched too, as `jaah_repo_prefix=None` does its own; `""` unsets it."""
    steps: list[str] = []

    hooks_dir = project_resolve.git_hooks_dir(clone)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-push"
    shutil.copyfile(GUARD_SRC, dest)
    dest.chmod(0o755)
    steps.append(f"pre-push guard installed: {dest}")

    subprocess.run(["git", "-C", clone, "config", PREFIX_CONFIG_KEY, prefix], check=True)
    steps.append(f"{PREFIX_CONFIG_KEY} = {prefix!r}")
    subprocess.run(
        ["git", "-C", clone, "config", PUSH_ENABLED_CONFIG_KEY, "true" if push_enabled else "false"],
        check=True,
    )
    steps.append(f"{PUSH_ENABLED_CONFIG_KEY} = {'true' if push_enabled else 'false'}")

    if integration_branch:
        subprocess.run(
            ["git", "-C", clone, "config", INTEGRATION_BRANCH_CONFIG_KEY, integration_branch], check=True,
        )
        steps.append(f"{INTEGRATION_BRANCH_CONFIG_KEY} = {integration_branch!r}")

    if allow_force_push is not None:
        value = "true" if allow_force_push else "false"
        subprocess.run(["git", "-C", clone, "config", ALLOW_FORCE_PUSH_CONFIG_KEY, value], check=True)
        steps.append(f"{ALLOW_FORCE_PUSH_CONFIG_KEY} = {value!r}")

    if jaah_repo_prefix:
        subprocess.run(["git", "-C", clone, "config", JAAH_REPO_PREFIX_CONFIG_KEY, jaah_repo_prefix], check=True)
        steps.append(f"{JAAH_REPO_PREFIX_CONFIG_KEY} = {jaah_repo_prefix!r}")
    elif jaah_repo_prefix is not None and project_resolve.git_config_values(clone, JAAH_REPO_PREFIX_CONFIG_KEY, local=True):
        subprocess.run(["git", "-C", clone, "config", "--unset-all", JAAH_REPO_PREFIX_CONFIG_KEY], check=True)
        steps.append(f"{JAAH_REPO_PREFIX_CONFIG_KEY} unset")

    if protect_integration:
        subprocess.run(["git", "-C", clone, "config", PROTECT_INTEGRATION_CONFIG_KEY, "true"], check=True)
        steps.append(f"{PROTECT_INTEGRATION_CONFIG_KEY} = 'true'")

    if not push_enabled:
        subprocess.run(
            ["git", "-C", clone, "config", "remote.origin.pushurl", DISABLED_PUSH_URL], check=True,
        )
        steps.append("origin push URL disabled (fetch URL untouched)")
    elif _push_url(clone) == DISABLED_PUSH_URL:
        # Only our own sentinel — a pushurl a human set deliberately is not ours to remove.
        subprocess.run(
            ["git", "-C", clone, "config", "--unset", "remote.origin.pushurl"], check=True,
        )
        steps.append("origin push URL restored (disabled sentinel removed)")

    return steps


def _parse_argv(
    argv: list[str],
) -> tuple[str, str, bool, str | None, str | None, bool, str | None, bool | None] | None:
    """(target, prefix, push_enabled, clone override, integration branch, protect integration, Jaah repo prefix,
    allow force push) or None on any usage error (already reported)."""
    if not argv:
        print(USAGE, file=sys.stderr)
        return None
    target = argv[0]
    prefix_raw: str | None = None
    push_raw: str | None = None
    override: str | None = None
    integration_branch: str | None = None
    force_raw: str | None = None
    protect_raw: str | None = None
    jaah_repo_prefix: str | None = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--branch-prefix" and i + 1 < len(argv):
            prefix_raw = argv[i + 1]
        elif arg == "--push-enabled" and i + 1 < len(argv):
            push_raw = argv[i + 1]
        elif arg == "--clone-path" and i + 1 < len(argv):
            override = argv[i + 1]
        elif arg == "--integration-branch" and i + 1 < len(argv):
            integration_branch = argv[i + 1]
        elif arg == "--allow-force-push" and i + 1 < len(argv):
            force_raw = argv[i + 1]
        elif arg == "--protect-integration" and i + 1 < len(argv):
            protect_raw = argv[i + 1]
        elif arg == "--jaah-repo-prefix" and i + 1 < len(argv):
            jaah_repo_prefix = argv[i + 1].strip()
        else:
            print(f"error: unexpected argument '{arg}'\n{USAGE}", file=sys.stderr)
            return None
        i += 2
    if prefix_raw is None or push_raw is None:
        # Mandatory: the project row is the one home, so an omitted value is an unknown one.
        print(f"error: --branch-prefix and --push-enabled are both required\n{USAGE}", file=sys.stderr)
        return None
    push_enabled = project_resolve.parse_git_bool(push_raw)
    if push_enabled is None:
        print(
            f"error: --push-enabled must be a git boolean (true/false, yes/no, on/off, 1/0; got '{push_raw}')",
            file=sys.stderr,
        )
        return None
    allow_force_push = None if force_raw is None else project_resolve.parse_git_bool(force_raw)
    if force_raw is not None and allow_force_push is None:
        print(f"error: --allow-force-push must be a git boolean (got '{force_raw}')", file=sys.stderr)
        return None
    protect = None if protect_raw is None else project_resolve.parse_git_bool(protect_raw)
    if protect_raw is not None and protect is None:
        print(f"error: --protect-integration must be a git boolean (got '{protect_raw}')", file=sys.stderr)
        return None
    return (target, normalize_prefix(prefix_raw), push_enabled, override, integration_branch, protect is True,
            jaah_repo_prefix, allow_force_push)


def main(argv: list[str]) -> int:
    parsed = _parse_argv(argv)
    if parsed is None:
        return 1
    (arg, prefix, push_enabled, override, integration_branch, protect_integration, jaah_repo_prefix,
     allow_force_push) = parsed

    try:
        resolved = project_resolve.resolve_one(arg)
    except ControlPlaneApiError as exc:
        print(f"error: could not reach the control-plane API to resolve project '{arg}': {exc}", file=sys.stderr)
        return 1
    if resolved is None:
        print(f"error: no project named '{arg}' in the control-plane API and '{arg}' is not a git clone", file=sys.stderr)
        return 1
    _, clones = resolved
    if integration_branch is None:
        # A hand-run install resolves it from the row. Failure is not fatal: it only shapes advice, and
        # the two rule markers must still be installed.
        try:
            integration_branch = project_resolve.resolve_integration_branch(arg) or None
        except ControlPlaneApiError as exc:
            print(f"note: could not read '{arg}'s integration_branch ({exc}) — leaving "
                  f"{INTEGRATION_BRANCH_CONFIG_KEY} as it is", file=sys.stderr)
    if jaah_repo_prefix is None:
        # Name mode reads it off the row; a clone path names no row, so its marker is left as it was.
        try:
            jaah_repo_prefix = project_resolve.resolve_jaah_repo_prefix(arg)
        except ControlPlaneApiError as exc:
            print(f"note: could not read '{arg}'s jaah_repo_prefix ({exc}) — leaving "
                  f"{JAAH_REPO_PREFIX_CONFIG_KEY} as it is", file=sys.stderr)
    if override:
        clones = [os.path.expanduser(override)]
    if not clones:
        print(
            f"error: no clone path given, and no clone of '{arg}' was found on this node "
            f"(needs JAAH_HOME set here, the project's id on its roster row, and a leased slot clone)",
            file=sys.stderr,
        )
        return 1
    # Name mode covers every primary-repository slot clone this node holds; a failure in one is reported and
    # the rest are still synced. Run it as the clones' owner: it writes into each `.git` as the caller.
    rc = 0
    for clone in clones:
        rc |= _install_one(clone, prefix, push_enabled, integration_branch, protect_integration, allow_force_push,
                           jaah_repo_prefix)
    return rc


def _install_one(clone: str, prefix: str, push_enabled: bool, integration_branch: str | None,
                 protect_integration: bool, allow_force_push: bool | None,
                 jaah_repo_prefix: str | None = None) -> int:
    if not (os.path.isdir(os.path.join(clone, ".git")) or os.path.isfile(os.path.join(clone, ".git"))):
        print(f"error: {clone} is not a git clone", file=sys.stderr)
        return 1

    if not push_enabled:
        check = subprocess.run(["git", "-C", clone, "remote", "get-url", "origin"], capture_output=True)
        if check.returncode != 0:
            print(
                f"error: {clone} has no 'origin' remote (disabling push needs one to disable)",
                file=sys.stderr,
            )
            return 1

    try:
        steps = apply_rules(clone, prefix, push_enabled, integration_branch,
                            protect_integration=protect_integration, allow_force_push=allow_force_push,
                            jaah_repo_prefix=jaah_repo_prefix)
    except subprocess.CalledProcessError as exc:
        print(f"error: git failed while installing the guard in {clone}: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write the guard into {clone}: {exc}", file=sys.stderr)
        return 1

    print(
        f"✅ access guard synced in {clone} "
        f"(branch_prefix: {prefix or '<none>'}, push_enabled: {'true' if push_enabled else 'false'})"
    )
    for step in steps:
        print(f"   • {step}")
    print("   Verify: apps/jaahd/guards/check-access-guard.py")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
