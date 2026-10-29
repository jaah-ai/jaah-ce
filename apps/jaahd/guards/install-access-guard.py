#!/usr/bin/env python3
"""Installs (or re-syncs, or reverts) the git-native push guard
in a managed project's local clone, for every project.

Usage: install-access-guard.py <project-name-or-clone-path> --branch-prefix <prefix>
                               --push-enabled <true|false> [--integration-branch <branch>]
                               [--clone-path <override>]
  <project-name-or-clone-path> — an exact project name in the control-plane API is tried FIRST (clone
    path computed via project_resolve.py); only then a literal clone path, so a same-named directory
    never shadows the DB-declared clone.
  --branch-prefix — the project's `branch_prefix`, WITH its trailing slash (`sn/`), or '' for none.
    Required even when empty: the DB owns the value, so there is no default here.
  --push-enabled — the project's `push_enabled`, `true` or `false`. Required.
  --integration-branch — optional, informational only (a refusal's advice). Omitted, the marker is left
    exactly as it was, so an older CP cannot wipe a value a newer one synced.

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

SELF_DIR = Path(__file__).resolve().parent
GUARD_SRC = SELF_DIR / "access-guard-pre-push.py"
PREFIX_CONFIG_KEY = "access-guard.branch-prefix"
PUSH_ENABLED_CONFIG_KEY = "access-guard.push-enabled"
# Informational only — see access-guard-pre-push.py for why its absence never blocks.
INTEGRATION_BRANCH_CONFIG_KEY = "access-guard.integration-branch"
# Opt-in integration-branch protection (the hook's docstring); only `apply_rules(protect_integration=True)`
# writes it — the CLI and the console sync never do.
PROTECT_INTEGRATION_CONFIG_KEY = "access-guard.protect-integration"
# Never rename: live clones already carry this literal, and step 4 removes only a sentinel it recognizes,
# so a rename would leave those clones permanently unpushable.
DISABLED_PUSH_URL = "disabled://readonly-guard-see-docs-policies-repo-access-levels-md"

USAGE = (
    "usage: install-access-guard.py <project-name-or-clone-path> --branch-prefix <prefix> "
    "--push-enabled <true|false> [--integration-branch <branch>] [--clone-path <override>]"
)


def _push_url(clone: str) -> str:
    """The configured `remote.origin.pushurl`, or '' if unset. Raw config, not `get-url --push`, which
    falls back to the fetch URL."""
    result = subprocess.run(
        ["git", "-C", clone, "config", "--get", "remote.origin.pushurl"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_prefix(raw: str) -> str:
    """A branch prefix as the marker must store it: stripped, and carrying a trailing slash when
    non-empty (`sn` -> `sn/`). The API normalizes on write; this catches a hand-run install."""
    prefix = raw.strip()
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def apply_rules(
    clone: str, prefix: str, push_enabled: bool, integration_branch: str | None = None, *,
    protect_integration: bool = False,
) -> list[str]:
    """Install the hook + both rule markers in `clone`, plus the informational integration-branch marker
    when one was supplied. Returns the steps taken; raises CalledProcessError / OSError.

    `integration_branch=None` or `""` leaves the marker untouched (the column is NOT NULL)."""
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


def _parse_argv(argv: list[str]) -> tuple[str, str, bool, str | None, str | None] | None:
    """(target, prefix, push_enabled, clone override, integration branch) or None on any usage error
    (already reported)."""
    if not argv:
        print(USAGE, file=sys.stderr)
        return None
    target = argv[0]
    prefix_raw: str | None = None
    push_raw: str | None = None
    override: str | None = None
    integration_branch: str | None = None
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
    return target, normalize_prefix(prefix_raw), push_enabled, override, integration_branch


def main(argv: list[str]) -> int:
    parsed = _parse_argv(argv)
    if parsed is None:
        return 1
    arg, prefix, push_enabled, override, integration_branch = parsed

    try:
        resolved = project_resolve.resolve_one(arg)
    except ControlPlaneApiError as exc:
        print(f"error: could not reach the control-plane API to resolve project '{arg}': {exc}", file=sys.stderr)
        return 1
    if resolved is None:
        print(f"error: no project named '{arg}' in the control-plane API and '{arg}' is not a git clone", file=sys.stderr)
        return 1
    _, clone = resolved
    if integration_branch is None:
        # A hand-run install resolves it from the row. Failure is not fatal: it only shapes advice, and
        # the two rule markers must still be installed.
        try:
            integration_branch = project_resolve.resolve_integration_branch(arg) or None
        except ControlPlaneApiError as exc:
            print(f"note: could not read '{arg}'s integration_branch ({exc}) — leaving "
                  f"{INTEGRATION_BRANCH_CONFIG_KEY} as it is", file=sys.stderr)
    if override:
        clone = os.path.expanduser(override)
    if not clone:
        print(
            f"error: no clone path given, and none could be computed for '{arg}' "
            f"(needs JAAH_HOME set on this node and the project's id on its roster row)",
            file=sys.stderr,
        )
        return 1
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
        steps = apply_rules(clone, prefix, push_enabled, integration_branch)
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
