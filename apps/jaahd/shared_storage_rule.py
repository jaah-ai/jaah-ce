#!/usr/bin/env python3
"""shared_storage_rule.py — THE ONE HOME of the shared-account path rule: where the token store sits under a
node's home, and how an account's slug composes a directory under a store's root.

Two deployables compose the same path and never import each other. The daemon resolves its own store at
launch (`shared_storage.py`, a sibling import, re-exports the names below); the control plane checks a login's
report against the node's reported root, and borrows this file BY PATH (its image copies it beside the other
daemon files it loads that way; its local stack mounts it read-only at the same path). `nodelink_mount.py` runs
`-I`-isolated as root and ships alone, so it keeps its own `windows_path`: the one sanctioned same-language copy
of `windows_absolute`, held equal by `tests/test_nodelink_windows.py`.

STDLIB ONLY, NO SIBLING IMPORT: the api loads this where no daemon sibling exists, and the daemon's hook-loaded
leaf (`agent_config_dirs.py` → `shared_storage.py`) imports it.
"""
from __future__ import annotations

import re

# The token store, joined to the node's home. A leading `.` keeps it out of the account-slug space
# (`^[a-z0-9]…`), so it can never collide with a per-node config dir beside it. The accounts NodeLink mounts
# there; the legacy root stays mounted (by `jaah-shared-mount`) while nodes move over. The node reports which
# one it resolved (`nodes.shared_accounts_root`) and the CP composes from that report — it never derives the
# root, and holds the set a node may report. The legacy mount helper's mount point is held equal to the legacy
# root by the monorepo's packaging tests. A changed value points shared runners at an unmounted dir, which the
# CLI reads as an empty store: a silent "Not logged in".
SHARED_ACCOUNTS_SEGMENT = "auth/.shared"
LEGACY_SHARED_ACCOUNTS_ROOT = "/mnt/jaah-shared/accounts"

# A Windows node's path: a drive letter, then backslashes only.
_WINDOWS_ABS = re.compile(r"[A-Za-z]:\\")
# A folder name Windows opens as a device whatever its extension (`NUL.txt` is NUL).
_WINDOWS_DEVICE = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]) *(\..*)?", re.IGNORECASE)


def windows_alias(name: str) -> bool:
    """Would Windows open folder NAME as another — empty, ending in `.` or a space (so `.` and `..` too), or a
    device whatever its extension? The one rule for every Windows path the CP accepts or the node composes."""
    return not name or name.endswith((".", " ")) or _WINDOWS_DEVICE.fullmatch(name) is not None


def has_control_character(value: str) -> bool:
    """True if VALUE carries a character that must never reach a runner's environment.

    Refuses the whole class (C0, DEL, C1, U+2028/U+2029), not a denylist: the path is exported as the
    securestorage variable, and anything `str.splitlines()` breaks on can inject a variable into a line-wise
    env file; `\\0` truncates at C-level callers. No legitimate root or slug contains one. Leading/trailing
    whitespace-class members are trimmed by the caller's `.strip()` before this runs."""
    return any(ch < " " or ch == "\x7f" or "\x80" <= ch <= "\x9f" or ch in "\u2028\u2029" for ch in value)


def windows_absolute(path: str) -> str:
    """PATH as a Windows absolute path without its trailing separators — drive-absolute, `\\` separators only,
    no control character, and no folder that Windows would alias (empty, ending in `.` or a space — so never `.`
    or `..` — or a device name) — or `""`. THE one rule for a Windows node's paths: the entry `path` the
    converger acts on (`nodelink_windows`, through `nodelink_mount.windows_path`) and every path the daemon and
    the CP compose a store under."""
    if not _WINDOWS_ABS.match(path) or "/" in path:
        return ""
    path = path.rstrip("\\")
    if len(path) < 3 or has_control_character(path):
        return ""
    if any(windows_alias(name) for name in path.split("\\")[1:]):
        return ""
    return path


def accounts_root_under(jaah_home: str | None) -> str:
    """`{jaah_home}/auth/.shared` — the accounts NodeLink's root on a node with that home — or `""` for a
    blank, relative, `..`-bearing or control-character home (nothing composes from a home that is not one).
    A Windows home (`C:\\...`) composes `{jaah_home}\\auth\\.shared`."""
    windows = windows_absolute((jaah_home or "").strip())
    if windows:
        return windows + "\\" + SHARED_ACCOUNTS_SEGMENT.replace("/", "\\")
    home = (jaah_home or "").strip().rstrip("/")
    if not home.startswith("/") or ".." in home.split("/") or has_control_character(home):
        return ""
    return f"{home}/{SHARED_ACCOUNTS_SEGMENT}"


def resolve_shared_auth_dir(slug: str | None, root: str | None = None) -> str:
    """A shared account's credential-store directory under `root` — `""` when the slug cannot compose one.

    `root` defaults to the legacy root; a caller holding a node passes `node.shared_accounts_root`, and the
    daemon passes its own `account_token_root()`. Fails closed to `""` (callers refuse on it): a
    defined-but-empty securestorage variable would mean the CLI's default home, an unauthenticated store.
    Blank, relative, `..`-bearing or control-character roots refuse. Slugs refuse when empty, containing `/`,
    `.`/`..`, or a control character — refused, not normalized, so one account's dir can never resolve to
    every account's."""
    root = (root if root is not None else LEGACY_SHARED_ACCOUNTS_ROOT).strip()
    windows = windows_absolute(root)
    if windows:
        # A Windows node's root: the slug also refuses `\\` and `:` (a separator, an alternate stream). Under a
        # POSIX root the refused class is exactly the control class (the codepoint oracle in
        # tests/test_shared_storage.py pins it), so those two stay ordinary characters there.
        slug = (slug or "").strip().strip("/")
        if not slug or any(sep in slug for sep in "/\\:") or slug in (".", "..") or has_control_character(slug):
            return ""
        return f"{windows}\\{slug}"
    root = root.rstrip("/")
    # The root is an operator-settable column on the CP, so it gets the same traversal and control-character
    # refusal as the slug; this is the one place every caller composes through.
    if not root or not root.startswith("/") or ".." in root.split("/") or has_control_character(root):
        return ""
    slug = (slug or "").strip().strip("/")
    if not slug or "/" in slug or slug in (".", "..") or has_control_character(slug):
        return ""
    return f"{root}/{slug}"
