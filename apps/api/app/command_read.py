"""The `(args, result)` a reader is served off one `command_audit_log` row — the single tenant read model,
shared by `routes/portfolio.py::command_log` (the log page) and `routes/commands.py::_echo` (the 201 echo),
so the two cannot narrow differently. Nothing under `app/routes/` is imported here.

Unlike `routes/commands.py::_credential_safe_args` (write-side, all readers), this narrows per reader: a
credential is nobody's to read, a host path is the operator's.

Host layout is the operator's fact (as in `routes/nodes/node_wire.py::_narrow_for_tenant` and
`routes/portfolio.py::_wt_out`). It is narrowed by shape, not a flat key list: host-path keys at any depth
and every absolute path in any string leaf. The verdict is the reader's org
(`tenancy/ids.py::is_tenant_org`), never the superadmin flag.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from app.user_activity import MAX_DEPTH

#: Keys whose value is only a fact about the operator's host, at any depth. Blanked to `None`, not
#: dropped, so the wire shape stays pinned. `stdout`/`stderr` are not here: that output is mostly the
#: tenant's (gate output, `account_add`'s `user_code` that `Accounts.tsx` renders), so they
#: take the string sweep instead, which leaves JSON text valid.
HOST_PATH_KEYS = frozenset({
    "config_dir", "path", "clone_path", "repo_root", "jaah_home", "securestorage_dir",
    "probed_config_dirs", "cwd_processes",
})
#: The node keeps only the last 400 chars of a failure `reason` (`apps/jaahd/commands.py`), so
#: `_CUT_PATH_HEAD_RE` runs on it at every depth.
_REASON_KEY = "reason"

#: An absolute POSIX path of two or more segments, swept from every tenant-read string. Conservative and
#: URL-aware: a `/` after a word char (relative path, fraction, date) is left alone, and the run after
#: `https:`/`http:`/`ssh:`/`git:` is the tenant's URL. `/+` also catches `//x` and `file:///x`; other
#: schemes (`s3://…`) are over-withheld, accepted. `/api/…` survives. A trailing full stop is not taken.
#: Unambiguous quantifiers keep it linear on adversarial input.
_ABS_PATH_RE = re.compile(
    r"(?<![/\w])(?<!https:)(?<!http:)(?<!ssh:)(?<!git:)/+(?!api/)"
    r"\.?[\w@+-]+(?:\.[\w@+-]+)*"
    r"(?:/\.?[\w@+-]+(?:\.[\w@+-]+)*)+"
)
#: An absolute Windows path (a Windows node): a drive path `C:\…` / `C:/…`, or a UNC / long-path
#: one (`\\host\share\…`, `\\?\C:\…`), continued across a space
#: only while the next token still holds a separator, so `C:\Program Files\Jaah\x` goes whole while the
#: prose after a path survives. A drive letter glued to a word (`https:`, `ssh:`) is a scheme, left alone.
_WINDOWS_PATH_RE = re.compile(
    r"(?:(?<![\w])[A-Za-z]:[\\/]|(?<![\w\\])\\\\)[^\s\"'<>|]*(?: (?=[^\s\"'<>|]*[\\/])[^\s\"'<>|]*)*"
)
#: A truncated path's tail (`pt/jaah/acme/repo/1/.git`) has lost its leading `/`. A relative run of
#: three or more segments at string start or after whitespace/colon is withheld; `{2,}` keeps
#: `fleet/task-42` whole. Over-withholds prose like `and/or/but`, accepted. Applied to `reason` only.
_CUT_PATH_HEAD_RE = re.compile(r"(?:^|(?<=[\s:]))[\w@+.-]+(?:/\.?[\w@+-]+(?:\.[\w@+-]+)*){2,}")
PATH_WITHHELD = "<path withheld>"
#: A serialized column inside a `reason` (e.g. a base64-pickled DBOS exception `gASV…`): one 40+ char
#: base64-minus-`/` token mixing upper, lower and digit. Read-side backstop for old rows (the writer,
#: `durable/accounts.py::_error_text`, is fixed), applied for every reader. The mixed-class rule and the
#: `/` boundary spare paths, branch names and 40-hex shas.
_SERIALIZED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+=_/-])"
    r"(?=[A-Za-z0-9+=_-]*[A-Z])(?=[A-Za-z0-9+=_-]*[a-z])(?=[A-Za-z0-9+=_-]*\d)"
    r"[A-Za-z0-9+=_-]{40,}(?![A-Za-z0-9+=_/-])"
)


def withhold_host_paths(value: Any, *, _depth: int = 0) -> Any:
    """`value` as a tenant may read it, recursively. A `HOST_PATH_KEYS` key becomes `None`; a `reason`
    string loses truncated-path tails; other strings are swept by `_ABS_PATH_RE`. Always a new object:
    callers pass ORM JSON columns that must not be written through.

    Depth-capped at `user_activity.MAX_DEPTH`, fail-closed (`None` past the cap): `args` is a
    caller-controlled body committed before the echo and never pruned, so a `RecursionError` would 500
    every later tenant read of `GET /api/command-log`."""
    if _depth >= MAX_DEPTH:
        return None
    if isinstance(value, dict):
        return {
            key: (
                None if key in HOST_PATH_KEYS
                else _withhold_reason(item) if key == _REASON_KEY and isinstance(item, str)
                else withhold_host_paths(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [withhold_host_paths(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _sweep_absolute_paths(value)
    return value


def _sweep_absolute_paths(text: str) -> str:
    """Every absolute path in `text` withheld, on either node OS's spelling."""
    return _ABS_PATH_RE.sub(PATH_WITHHELD, _WINDOWS_PATH_RE.sub(PATH_WITHHELD, text))


def _withhold_reason(reason: str) -> str:
    return _CUT_PATH_HEAD_RE.sub(PATH_WITHHELD, _sweep_absolute_paths(reason))


def without_serialized_reason(result: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """`result` with a serialized token scrubbed from its top-level `reason`, for every reader; `None`
    when the token was all there was. A new dict if changed, else the same object."""
    if result is None:
        return None
    reason = result.get(_REASON_KEY)
    if not isinstance(reason, str):
        return result
    scrubbed = _SERIALIZED_TOKEN_RE.sub("", reason).strip(" :—-")
    if scrubbed == reason:
        return result
    return {**result, _REASON_KEY: scrubbed or None}


def command_read(
    args: Any, result: Optional[dict[str, Any]], *, tenant: bool,
) -> tuple[Any, Optional[dict[str, Any]]]:
    """The `(args, result)` a reader is served off one `command_audit_log` row (or its 201 echo).

    Every reader: `without_serialized_reason`. A tenant (`tenancy/ids.py::is_tenant_org` on the reader's
    org, decided by the caller) also gets `withhold_host_paths` on both. Otherwise byte-identical."""
    result = without_serialized_reason(result)
    if not tenant:
        return args, result
    narrowed: Optional[dict[str, Any]] = None if result is None else withhold_host_paths(result)
    return withhold_host_paths(args), narrowed
