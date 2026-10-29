"""Where a shared credential account's token store lives: the one path meant to be the same on every
node that mounts one share.

Opposite in shape to `app/node_clone.py::resolve_auth_dir`, which is per-node (from that node's
`jaah_home`). Here two hosts must open the same `.credentials.json`, so the root must not vary across
the nodes of one share (the legacy root), or is the node's own `{jaah_home}/auth/.shared` mount of the org's
accounts NodeLink. The root lives on `nodes.shared_accounts_root` (one share per org). The CP owns the
path; the node obeys it verbatim (`GET /api/agent/accounts` `securestorage_dir`, and the `lease_slot` intent).

`CLAUDE_SECURESTORAGE_CONFIG_DIR` moves only the credential file; the per-node `CLAUDE_CONFIG_DIR` keeps
transcripts, plugins and session state. A later shared need gets its own `/mnt/jaah-shared/<need>`.
"""
from __future__ import annotations

import re
from typing import Optional

# Both mirrored in `apps/jaahd/shared_storage.py` (no cross-import; three AST guards pin them).
# The accounts NodeLink mounts at `{jaah_home}/<SHARED_ACCOUNTS_SEGMENT>`; the legacy root stays mounted
# (by `jaah-shared-mount`) while nodes move over. The node reports which one it resolved
# (`nodes.shared_accounts_root`) and the CP composes from that report — it never derives the root;
# `app/vnodes.py::permitted_accounts_roots` is the set a node may report. Changing either here alone points
# shared runners at an unmounted dir, which the CLI reads as an empty store: a silent "Not logged in".
SHARED_ACCOUNTS_SEGMENT = "auth/.shared"
LEGACY_SHARED_ACCOUNTS_ROOT = "/mnt/jaah-shared/accounts"


# A Windows node's home and root (`C:\\ProgramData\\Jaah\\home`): a drive letter, then backslashes only.
_WINDOWS_ABS = re.compile(r"[A-Za-z]:\\")


def _windows_absolute(value: str) -> str:
    """VALUE as a Windows absolute path without its trailing separators — a drive letter, backslashes only, no
    `..` segment, no control character — or `""`."""
    path = value.rstrip("\\")
    if not _WINDOWS_ABS.match(path + "\\") or len(path) < 3 or "/" in path or ".." in path.split("\\") \
            or has_control_character(path):
        return ""
    return path


def accounts_root_under(jaah_home: Optional[str]) -> str:
    """`{jaah_home}/auth/.shared` — the accounts NodeLink's root on a node with that home — or `""` for a
    blank, relative, `..`-bearing or control-character home (nothing composes from a home that is not one).
    A Windows home (`C:\\...`) composes `{jaah_home}\\auth\\.shared`."""
    windows = _windows_absolute((jaah_home or "").strip())
    if windows:
        return windows + "\\" + SHARED_ACCOUNTS_SEGMENT.replace("/", "\\")
    home = (jaah_home or "").strip().rstrip("/")
    if not home.startswith("/") or ".." in home.split("/") or has_control_character(home):
        return ""
    return f"{home}/{SHARED_ACCOUNTS_SEGMENT}"


def has_control_character(value: str) -> bool:
    """True if VALUE carries a character that must never reach `CLAUDE_SECURESTORAGE_CONFIG_DIR`.

    Refuses the whole class (C0, DEL, C1, U+2028/U+2029), not a denylist: the path is exported into a
    runner's environment, and anything `str.splitlines()` breaks on can inject a variable into a
    line-wise env file; `\\0` truncates at C-level callers. No legitimate root or slug contains one.
    Leading/trailing whitespace-class members are trimmed by the caller's `.strip()` before this runs."""
    return any(
        ch < " " or ch == "\x7f" or "\x80" <= ch <= "\x9f" or ch in "  "
        for ch in value
    )


def resolve_shared_auth_dir(slug: Optional[str], root: Optional[str] = None) -> str:
    """A shared account's credential-store directory under `root` — `""` when the slug cannot compose one.

    `root` defaults to the fleet constant; a caller holding a node passes `node.shared_accounts_root`.
    Fails closed to `""` (callers refuse on it): a defined-but-empty `CLAUDE_SECURESTORAGE_CONFIG_DIR`
    would mean `~/.claude`, an unauthenticated store. Blank, relative, `..`-bearing or control-character
    roots refuse. Slugs refuse when empty, containing `/`, `.`/`..`, or a control character — refused,
    not normalized, so one account's dir can never resolve to every account's."""
    root = (root if root is not None else LEGACY_SHARED_ACCOUNTS_ROOT).strip()
    windows = _windows_absolute(root)
    if windows:
        # A Windows node's root: the slug also refuses `\\` and `:` (a separator, an alternate stream).
        slug = (slug or "").strip().strip("/")
        if not slug or any(sep in slug for sep in "/\\:") or slug in (".", "..") or has_control_character(slug):
            return ""
        return f"{windows}\\{slug}"
    root = root.rstrip("/")
    # The root is an operator-settable column, so it gets the same traversal and control-character
    # refusal as the slug; this is the one place every caller composes through.
    if not root or not root.startswith("/") or ".." in root.split("/") or has_control_character(root):
        return ""
    slug = (slug or "").strip().strip("/")
    if not slug:
        return ""
    if "/" in slug or slug in (".", "..") or has_control_character(slug):
        return ""
    return f"{root}/{slug}"
