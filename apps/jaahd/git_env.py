"""The git environment every git process this node starts must carry — the one home for it.

The broker is the only credential path, reached through ONE credential helper,
`jaah-git-credential` (source: `apps/jaahd/git_credential_helper.py`; installed and wired as the system
`credential.helper` by the node's provisioning, `ensure_git_credential_helper`). git consults
every configured `credential.helper` in order, so a stray helper in `~/.gitconfig` would answer first.
`GIT_CONFIG_COUNT`/`KEY`/`VALUE` config is read after every file scope, and an empty `credential.helper`
resets the helper list there (URL-scoped and `includeIf` helpers included, verified against git 2.53) — so
the reset is followed by the node's own helper, leaving it the only answer. `credential.useHttpPath` makes
git send the repo `path=`, which the helper checks against a sub-path root. `GIT_TERMINAL_PROMPT=0`: with
no askpass left, a helper that answers nothing (foreign host, broker refusal) would otherwise fall through
to a terminal prompt nobody in a runner pane can answer.

A value is never a secret here, so these names are safe to write into `launch.sh` and to log."""
from __future__ import annotations

import re
import sys

# `sys.platform`, not `host_platform`: this module is also imported where `apps/jaahd` is not on the path
# (the node provisioning script's selftest), so the seam is imported only inside the Windows/macOS helpers.
_WINDOWS = sys.platform == "win32"
_MACOS = sys.platform == "darwin"

#: Where the node's git credential helper is installed. Mirrored in the node provisioning script
#: (`GIT_CREDENTIAL_HELPER_PATH`; no import across the two), pinned by that module's tests.
_POSIX_CREDENTIAL_HELPER = "/usr/lib/jaahd/jaah-git-credential"


def windows_credential_helper(paths=None) -> str:
    """The helper as a Windows node names it: a `!` shell snippet running the bundle's interpreter on the
    bundle's own copy of `git_credential_helper.py` — nothing is installed beside git there, and Git for
    Windows cannot exec a `.py` (its shebang would want a `python3` on PATH that no Windows node has).

    git runs a `!` helper through its own `sh` with the action appended, so both paths are double-quoted
    (`C:\\Program Files` has a space) and spelled with forward slashes, which that `sh` leaves alone
    where it would eat a backslash. `paths` is `host_platform.paths()` unless a test passes its own."""
    if paths is None:
        import host_platform  # noqa: PLC0415 — see `_WINDOWS`

        paths = host_platform.paths()
    p = paths
    script = f"{p.lib_root}\\apps\\jaahd\\git_credential_helper.py"
    return '!"{}" "{}"'.format(p.python.replace("\\", "/"), script.replace("\\", "/"))


def macos_credential_helper(paths=None) -> str:
    """The helper as a macOS node names it — Windows' shape (`windows_credential_helper`) for the same
    reason: nothing is installed beside git there, so git runs the bundle's interpreter on the bundle's own
    copy of `git_credential_helper.py`. Double-quoted, as git's `sh` takes a `!` helper; `-E -s` so no
    PYTHONPATH or user site of whoever runs git reaches it (not `-I`: it imports the seam beside it). `paths` is
    `host_platform.paths()` unless a test passes its own."""
    if paths is None:
        import host_platform  # noqa: PLC0415 — see `_WINDOWS`

        paths = host_platform.paths()
    return '!"{}" -E -s "{}/apps/jaahd/git_credential_helper.py"'.format(paths.python, paths.lib_root.rstrip("/"))


def _community_edition() -> bool:
    """A community-edition node (`apps/jaahd/edition.py`)? False where the daemon tree is not importable (see
    `_WINDOWS`), which is never a node."""
    try:
        import edition  # noqa: PLC0415 — see `_WINDOWS`
    except ImportError:
        return False
    return edition.IS_CE


# A community-edition Linux node takes macOS's shape for macOS's reason: its installer has no root to put a helper
# beside git, so git runs the node's interpreter on the bundle's own copy (`host_platform.linux._ce_paths`).
GIT_CREDENTIAL_HELPER = (windows_credential_helper() if _WINDOWS
                         else macos_credential_helper() if _MACOS or _community_edition() else _POSIX_CREDENTIAL_HELPER)

# Git for Windows' installer defaults `core.autocrlf=true` in its SYSTEM config, which would check every
# slot out with CRLF endings: the repo's shell hooks and gates then die in Git Bash on `\r`, and the tree
# differs byte-for-byte from the same commit on a Linux node. Pinned off per process, like the helper.
# Linux and macOS pin nothing here.
_WINDOWS_CHECKOUT_PINS: tuple[tuple[str, str], ...] = (("core.autocrlf", "false"),)


def pinned_config(helper: str = GIT_CREDENTIAL_HELPER, *,
                  windows: bool | None = None) -> tuple[tuple[str, str], ...]:
    """The config pairs, in order: reset the helper list, then name the node's helper as its only entry.

    `http.extraHeader` is the other file-borne credential git sends without a helper, cleared the same way.
    Transport keys that redirect the connection (`url.<x>.insteadOf`, `http.<url>.*`) are not this function's:
    the daemon's own network git reads no runner-writable config (`git_mirror`), and the node's credential
    helper refuses to answer a runner's git whose non-system config sets one
    (`git_credential_helper.check_config_scopes`). Defence in depth,
    not a boundary: a process that sets its own `GIT_CONFIG_COUNT` replaces this silently. On Windows the
    checkout pins (`_WINDOWS_CHECKOUT_PINS`) follow. `helper` and `windows` are parameters for tests only;
    production reads the constants below."""
    pins = (
        ("credential.helper", ""),
        ("credential.helper", helper),
        ("credential.useHttpPath", "true"),
        ("http.extraHeader", ""),
    )
    if _WINDOWS if windows is None else windows:
        pins += _WINDOWS_CHECKOUT_PINS
    return pins


def broker_only(helper: str = GIT_CREDENTIAL_HELPER) -> dict[str, str]:
    """`pinned_config` as `GIT_CONFIG_*` environment, plus `GIT_TERMINAL_PROMPT=0`."""
    pairs = pinned_config(helper)
    env = {"GIT_CONFIG_COUNT": str(len(pairs)), "GIT_TERMINAL_PROMPT": "0"}
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


# Users: a runner session's own git (`fleet_dispatch`), the push guard's bundle (`guard_bundle`), and the
# daemon's network git through `DECLARED_OP`.
BROKER_ONLY: dict[str, str] = broker_only()

# The global config scope cut off for a daemon-side git call: delegated git runs as the session user, whose
# `~/.gitconfig` (and `$XDG_CONFIG_HOME/git/config`, which git skips once this is set) every runner on the node
# can write, so a key planted there would reach every declared op. `/dev/null` on every platform: Git for
# Windows maps it to `nul`. Not in `BROKER_ONLY`, which also reaches a runner's own session, whose
# `~/.gitconfig` is its own business.
NO_USER_CONFIG: dict[str, str] = {"GIT_CONFIG_GLOBAL": "/dev/null"}
# Git for Windows starts the `!` helper through its MSYS `sh`, quoting that command line for the runtime's default
# parse. The Windows daemon runs with `MSYS=noglob` (`host_platform.windows.with_msys_noglob`, for tmux), under which
# that `sh` keeps the quotes and splits `C:/Program Files` at its space, so every network op of the daemon's failed.
# Empty: none of the daemon's other MSYS options matter to git.
_DAEMON_GIT_MSYS: dict[str, str] = {"MSYS": ""} if _WINDOWS else {}
# The environment pins a daemon-side network git call (a declared op, `clone_identity`) carries. Users: the mirror's
# refresh/ls-remote/push (`git_mirror`) and an image source clone (`microvm_images._source`). `NO_USER_CONFIG`
# alone: the mirror's local reads (`git_mirror`).
DECLARED_OP: dict[str, str] = {**BROKER_ONLY, **NO_USER_CONFIG, **_DAEMON_GIT_MSYS}

# Repo-local code pinned off for a daemon-side git call: a hook, fsmonitor, template or askpass program planted
# in a tree a runner writes must never run under the daemon's credential declaration (`clone_identity`), as the
# daemon. `core.askPass` empty: git runs it when the credential helper answers nothing (a refused destination),
# and as a descendant of the declared op it would be served the login. Users, at the seams: `worktree_mechanics.core._run_git`,
# `commands._native_run`'s callers (`worktree_ops`), `reconcile.context._fetch_base_blocking`, `git_mirror`,
# `microvm_session._git`, `microvm_images._git`.
NO_REPO_CODE: tuple[str, ...] = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 "-c", "init.templateDir=", "-c", "protocol.ext.allow=never",
                                 "-c", "core.askPass=")

def tracking_refspec(branch: str) -> str:
    """The refspec a `fetch origin <branch>` updated opportunistically, spelled out for a fetch from the project's
    mirror (`git_mirror.local_fetch_args`), which names no remote."""
    return f"+refs/heads/{branch}:refs/remotes/origin/{branch}"


# Git reads author/committer from these above every config scope, so a commit names the task's launcher
# rather than a shared clone's `user.*`.
AUTHOR_NAME_VARS = ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")
AUTHOR_EMAIL_VARS = ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")


def author_env(identity: "tuple[str, str] | None") -> dict[str, str]:
    """`{GIT_AUTHOR_NAME: …, GIT_COMMITTER_EMAIL: …}` for a `(name, email)`, or `{}` when there is none.

    Empty rather than blanks: git refuses an empty ident, where an unset one falls back to config."""
    if not identity:
        return {}
    name, email = identity
    return {**{var: name for var in AUTHOR_NAME_VARS}, **{var: email for var in AUTHOR_EMAIL_VARS}}


def redact_userinfo(text: str) -> str:
    """TEXT with every URL's userinfo masked (`https://u:token@host/…` → `https://***@host/…`), for an error
    or log line — the one home for it. Redact the whole text BEFORE cutting it: a cut inside the userinfo
    leaves no `@` for the pattern to find. It masks up to the last `@` before the host, so a raw `@` in a
    password goes too. The scp form (`git@host:o/r`) is left as is — its user is a fixed, non-secret name and it
    has no password field."""
    return re.sub(r"(://)[^/\s'\"]*@", r"\1***@", text)


def parse_identity(raw: object) -> "tuple[str, str] | None":
    """The intent's `git_author` field → `(name, email)`, or None for anything not exactly that.

    Strict, because both values land in a generated shell script and git's ident: a missing field, a
    non-string, an email without `@`, or any control character (a newline would forge a second ident
    line) is refused whole."""
    if not isinstance(raw, dict):
        return None
    name, email = raw.get("name"), raw.get("email")
    if not isinstance(name, str) or not isinstance(email, str):
        return None
    name, email = name.strip(), email.strip()
    if not name or "@" not in email or any(ord(ch) < 32 or ch in "<>" for ch in name + email):
        return None
    return name, email
