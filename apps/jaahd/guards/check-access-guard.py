#!/usr/bin/env python3
"""check-access-guard.py — verify the git-native push guard is
installed, intact, and set to the values the console declares; the audit companion to
install-access-guard.py.

Usage: check-access-guard.py [project-name-or-clone-path]
  An exact control-plane project name is tried FIRST, then a literal clone path (a same-named directory
  must never shadow the DB-declared clone). Omit to sweep every restricted project plus every project
  with a local clone. Exit 0 = all conform (or nothing enrolled); 1 = unresolved argument or any FAIL.

RESTRICTED (non-empty `branch_prefix` and/or `push_enabled = false`): the pre-push hook is present,
byte-identical and executable; `core.hooksPath` does not redirect away from it; both markers are SET
(an empty prefix is a value) and match the console; the origin push URL is the disabled sentinel iff
push is disabled. UNRESTRICTED: the clone carries no leftover restriction (markers or sentinel) — the
opposite drift, left by a failed `sync_access_guard`, a hand-edit, or a pre-guard clone.
"""
from __future__ import annotations

import filecmp
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_resolve  # noqa: E402
from control_plane_client import ControlPlaneApiError  # noqa: E402
from project_resolve import DeclaredGovernance, parse_git_bool  # noqa: E402

SELF_DIR = Path(__file__).resolve().parent
GUARD_SRC = SELF_DIR / "access-guard-pre-push.py"
PREFIX_CONFIG_KEY = "access-guard.branch-prefix"
PUSH_ENABLED_CONFIG_KEY = "access-guard.push-enabled"
HOOKS_PATH_CONFIG_KEY = "core.hooksPath"
# Byte-identical to install-access-guard.py's constant, retired tier name and all — see there for why.
DISABLED_PUSH_URL = "disabled://readonly-guard-see-docs-policies-repo-access-levels-md"

RED, GREEN, YELLOW, RESET = "\033[31m", "\033[32m", "\033[33m", "\033[0m"

_fail_count = 0


def _red(msg: str) -> None:
    global _fail_count
    print(f"  {RED}✗ FAIL{RESET} {msg}")
    _fail_count += 1


def _warn(msg: str) -> None:
    print(f"  {YELLOW}! WARN{RESET} {msg}")


def _ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def _resync_hint(name: str, declared: DeclaredGovernance | None) -> str:
    """The exact command that fixes whatever this repo got wrong."""
    if declared is None:
        return f"install-access-guard.py {name} --branch-prefix <prefix> --push-enabled <true|false>"
    return (
        f"install-access-guard.py {name} --branch-prefix '{declared.branch_prefix}' "
        f"--push-enabled {'true' if declared.push_enabled else 'false'}"
    )


def _git_config(clone: str, key: str) -> str | None:
    """`key`'s value **in the clone's own config file**, or None when it is UNSET or multi-valued.

    None vs '' is load-bearing: an empty prefix is a written value. `--local --get-all` must mirror
    `access-guard-pre-push.py::read_config` EXACTLY so the checker cannot pass a clone the hook would
    refuse; a wider scope would let `~/.gitconfig` or `GIT_CONFIG_*` satisfy the audit (see that
    function for the reproduced bypass)."""
    result = subprocess.run(
        ["git", "-C", clone, "config", "--local", "--get-all", key],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines()]
    return values[0] if len(values) == 1 else None


def _hooks_path_override(clone: str) -> str | None:
    """The clone's OWN `core.hooksPath`, verbatim, or None when the key is unset.

    Multi-valued yields git's effective answer (the LAST value) rather than blocking as an ambiguous
    marker does — git always resolves this to one directory. An EMPTY value is a value. Wider-scope
    overrides are not read here and not a hole: `_check_hook_file` uses git's full resolution, so
    `_check_hook_file` fails wherever such an override points."""
    result = subprocess.run(
        ["git", "-C", clone, "config", "--local", "--get-all", HOOKS_PATH_CONFIG_KEY],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines()]
    return values[-1] if values else None


def _check_hooks_path(name: str, clone: str, declared: DeclaredGovernance | None) -> bool:
    """Does `core.hooksPath` still let the installed guard run? True → `_check_hook_file` audits it;
    False → the redirection was already reported as the failure.

    One `git config core.hooksPath /tmp/none` disables layer 1 with nothing removed. Checked separately
    because `_check_hook_file` would report it as "no hook installed", whose remedy (re-run the installer)
    follows the override and entrenches it; and a git that ignored the override in `--git-path hooks`
    would give a green audit with zero enforcement. A redirection with the guard intact there (e.g.
    husky's `.husky`) is legitimate and only warned."""
    override = _hooks_path_override(clone)
    if override is None:
        return True
    target = Path(override) if Path(override).is_absolute() else Path(clone) / override
    hook = target / "pre-push"
    if hook.is_file() and os.access(hook, os.X_OK) and filecmp.cmp(hook, GUARD_SRC, shallow=False):
        _warn(
            f"{HOOKS_PATH_CONFIG_KEY} = {override!r} redirects hooks to {target} (the repo's own tooling, "
            "e.g. husky) — the guard IS installed and intact there, so it still runs"
        )
        return True
    _red(
        f"{HOOKS_PATH_CONFIG_KEY} = {override!r} redirects EVERY hook to {target}, where the access guard "
        f"is not installed (or has drifted) — layer 1 is disabled in this clone whatever its own "
        f".git/hooks holds, and a push to any ref now runs no guard at all. Remove the redirection "
        f"(git -C {clone} config --unset {HOOKS_PATH_CONFIG_KEY}), then re-run {_resync_hint(name, declared)}"
    )
    return False


def _installed_governance(clone: str) -> DeclaredGovernance | None:
    """The markers as INSTALLED, or None when either is unset/unparseable (the hook blocks every push
    then). Parsed by the hook's own rules."""
    prefix = _git_config(clone, PREFIX_CONFIG_KEY)
    raw_push = _git_config(clone, PUSH_ENABLED_CONFIG_KEY)
    if prefix is None or raw_push is None:
        return None
    push_enabled = parse_git_bool(raw_push)
    if push_enabled is None:
        return None
    return DeclaredGovernance(branch_prefix=prefix.strip(), push_enabled=push_enabled)


def _check_hook_file(name: str, clone: str, declared: DeclaredGovernance | None) -> None:
    try:
        hooks_dir = project_resolve.git_hooks_dir(clone)
    except subprocess.CalledProcessError:   # git cannot answer: fall back to `.git/hooks`
        hooks_dir = Path(clone) / ".git" / "hooks"
    hook = hooks_dir / "pre-push"
    if not hook.is_file():
        _red(f"no pre-push hook installed at {hook} — run {_resync_hint(name, declared)}")
    elif not filecmp.cmp(hook, GUARD_SRC, shallow=False):
        _red(f"{hook} has drifted from {GUARD_SRC} — re-run {_resync_hint(name, declared)}")
    elif not os.access(hook, os.X_OK):
        _red(f"{hook} is not executable")
    else:
        _ok("pre-push guard installed and matches source")


def _describe(governance: DeclaredGovernance) -> str:
    return (
        f"branch_prefix={governance.branch_prefix or '<none>'} "
        f"push_enabled={'true' if governance.push_enabled else 'false'}"
    )


def _check_markers(
    name: str, clone: str, declared: DeclaredGovernance | None
) -> DeclaredGovernance | None:
    """Verify both markers against what the console declares. Returns the governance to check the rest
    against: the installed markers, or the declared values when a marker is missing."""
    installed = _installed_governance(clone)
    if installed is None:
        _red(
            f"{PREFIX_CONFIG_KEY}/{PUSH_ENABLED_CONFIG_KEY} are not both set and parseable — the "
            f"installed hook now refuses EVERY push in this state (it no longer no-ops); "
            f"run {_resync_hint(name, declared)}"
        )
        return declared
    if declared is None:
        _warn(
            f"{_describe(installed)} (no Jaah project resolved for this path — the markers "
            "themselves are unverified against declared values)"
        )
    elif installed != declared:
        _red(
            f"markers say {_describe(installed)} but the control plane declares {_describe(declared)} — "
            f"re-run {_resync_hint(name, declared)}"
        )
    else:
        _ok(f"markers match the control plane ({_describe(installed)})")
    return installed


def _check_push_url(name: str, clone: str, governance: DeclaredGovernance | None) -> None:
    fetch = subprocess.run(
        ["git", "-C", clone, "remote", "get-url", "origin"], capture_output=True, text=True,
    )
    push_disabled = governance is not None and not governance.push_enabled
    if fetch.returncode != 0 or not fetch.stdout.strip():
        # Only a push-disabled repo NEEDS an origin (it has one to disable).
        if push_disabled:
            _red(
                "no 'origin' remote found — layer 2 (the disabled push URL) cannot be in force for a "
                "repo whose push is disabled"
            )
        else:
            _warn("no 'origin' remote found — nothing to check for the push-URL layer")
        return
    push_url = _git_config(clone, "remote.origin.pushurl")
    if push_disabled:
        if push_url != DISABLED_PUSH_URL:
            _red(
                "origin push URL is not disabled (currently resolves to the fetch URL) — run "
                f"{_resync_hint(name, governance)}"
            )
        else:
            _ok(f"origin push URL disabled ({push_url})")
    elif push_url == DISABLED_PUSH_URL:
        _red(
            "origin push URL is still the disabled sentinel while push is enabled — this clone "
            f"cannot push at all; run {_resync_hint(name, governance)}"
        )
    else:
        _ok("origin push URL is not disabled (correct for this project)")


def _check_local_branches(clone: str, prefix: str) -> None:
    # Stray local branches only matter once pushed (the hook blocks that); this surfaces drift early.
    # The prefix is the DB-declared one, never a hardcoded literal.
    result = subprocess.run(
        ["git", "-C", clone, "for-each-ref", "--format=%(refname:short)", "refs/heads/"],
        capture_output=True, text=True,
    )
    branches = [b for b in result.stdout.splitlines() if b]
    stray = [b for b in branches if not b.startswith(prefix)]
    if stray:
        _warn(
            f"local branch(es) not prefixed {prefix} (fine to keep locally, just never push them): "
            + " ".join(stray)
        )
    else:
        _ok(f"all local branches are {prefix}-prefixed (or none exist)")


def _check_no_stale_restriction(name: str, clone: str, declared: DeclaredGovernance) -> None:
    """For a project the console declares UNRESTRICTED: assert its clone is not still restricted.

    A missing hook or unset markers are correct here, so only a leftover restriction is looked for."""
    installed = _installed_governance(clone)
    push_url = _git_config(clone, "remote.origin.pushurl")
    stale: list[str] = []
    if installed is not None and installed.restricted:
        stale.append(f"markers say {_describe(installed)}")
    if push_url == DISABLED_PUSH_URL:
        stale.append(f"remote.origin.pushurl is the disabled sentinel ({push_url})")
    if stale:
        _red(
            "this clone is MORE restricted than the control plane declares — "
            + "; ".join(stale)
            + f" — while the control plane declares {_describe(declared)}. A revert never landed (a failed "
            f"sync_access_guard dispatch, a hand-edit, or a clone predating the guard): run "
            f"{_resync_hint(name, declared)}"
        )
        return
    _ok(
        "unrestricted, and the clone carries no leftover restriction "
        f"(markers: {_describe(installed) if installed is not None else 'unset'})"
    )


def check_repo(name: str, clone: str, declared: DeclaredGovernance | None) -> None:
    declared_label = "<not a Jaah project>" if declared is None else _describe(declared)
    print(f"\n── {name}  ({clone or '<no clone path>'})  declares: {declared_label} ──")
    if not clone or not os.path.isdir(clone):
        _warn("clone not present locally — skipped (fine before first clone)")
        return

    # A real unrestricted project gets the over-restriction check instead. `declared is None` (bare path)
    # keeps the suite, reported as unverified.
    if declared is not None and not declared.restricted:
        _check_no_stale_restriction(name, clone, declared)
        return

    # `core.hooksPath` first: it decides where the hook-file check should look (see `_check_hooks_path`).
    if _check_hooks_path(name, clone, declared):
        _check_hook_file(name, clone, declared)
    governance = _check_markers(name, clone, declared)
    _check_push_url(name, clone, governance)
    if governance is not None and governance.branch_prefix:
        _check_local_branches(clone, governance.branch_prefix)


def main(argv: list[str]) -> int:
    print("Access guard conformance")
    try:
        if argv:
            resolved = project_resolve.resolve_one_with_governance(argv[0])
            if resolved is None:
                print(
                    f"error: no project named '{argv[0]}' in the control-plane API and '{argv[0]}' is not a git clone",
                    file=sys.stderr,
                )
                return 1
            targets = [resolved]
        else:
            # Keep a project if either side could fail: declared-restricted (even uncloned, so "not
            # cloned" is reported), or any project with a local clone to inspect for leftovers.
            targets = [
                (name, clone, declared)
                for name, clone, declared in project_resolve.sweep_all()
                if declared.restricted or (clone and os.path.isdir(clone))
            ]
    except ControlPlaneApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not targets:
        print("no restricted projects and no locally-resolvable clones found", file=sys.stderr)
        return 0

    for name, clone, declared in targets:
        check_repo(name, clone, declared)

    print()
    if _fail_count == 0:
        print(f"{GREEN}✅ every checked repo matches what the control plane declares{RESET}")
        return 0
    print(f"{RED}❌ {_fail_count} conformance failure(s){RESET}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
