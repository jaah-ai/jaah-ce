"""Where a shared credential account's token store lives: the one path meant to be the same on every
node that mounts one share.

Opposite in shape to `app/node_clone.py::resolve_auth_dir`, which is per-node (from that node's
`jaah_home`). Here two hosts must open the same `.credentials.json`, so the root must not vary across
the nodes of one share (the legacy root), or is the node's own `{jaah_home}/auth/.shared` mount of the org's
accounts NodeLink. The root lives on `nodes.shared_accounts_root` (one share per org), as the node reports it;
the CP composes the expected store from it to check a login's report (`routes/commands.py`), and the node
resolves its own at launch (`apps/jaahd/shared_storage.py`).

The rule itself has ONE home, the daemon's `apps/jaahd/shared_storage_rule.py`. This module LOADS it by
path (`app/shared_module.py`: the api image COPYs it to `/app/shared/shared_storage_rule.py` per the API
image's Dockerfile; the local stack mounts it read-only) and re-exports it, never a second copy.

`CLAUDE_SECURESTORAGE_CONFIG_DIR` moves only the credential file; the per-node `CLAUDE_CONFIG_DIR` keeps
transcripts, plugins and session state. A later shared need gets its own `/mnt/jaah-shared/<need>`.
"""
from __future__ import annotations

from app import shared_module

_node = shared_module.load("_node_shared_storage_rule", shared_module.candidates("shared_storage_rule.py", "apps/jaahd"),
                           consequence="; no shared account's store can be composed or checked without it")

SHARED_ACCOUNTS_SEGMENT: str = _node.SHARED_ACCOUNTS_SEGMENT
LEGACY_SHARED_ACCOUNTS_ROOT: str = _node.LEGACY_SHARED_ACCOUNTS_ROOT
accounts_root_under = _node.accounts_root_under
has_control_character = _node.has_control_character
resolve_shared_auth_dir = _node.resolve_shared_auth_dir
windows_alias = _node.windows_alias
