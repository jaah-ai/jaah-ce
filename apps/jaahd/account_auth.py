#!/usr/bin/env python3
"""account_auth.py — the node-side half of HOW A DISPATCHED ACCOUNT AUTHENTICATES, and the ONE home of the
spellings both sides speak.

A subscription-OAuth account materializes a credential directory here (`CLAUDE_CONFIG_DIR`); an API-key
account materializes no credential (a token-less home at most, `api_key_config_dir`) and the runner
authenticates with its key instead (`apps/api/app/account_auth.py` holds that split's policy). This module
holds the spellings the node needs for the second path: no lookup, no policy, no value.

The two kinds, the two prefixes, the slug grammar and the parser are defined HERE and borrowed by the control
plane by path (`apps/api/app/account_auth.py` through `app/shared_module.py`; this tree never imports that
one), so the name a node composes is parsed by the same code that defined it. No secret enters
this process: the daemon composes a NAME into `launch.sh` and the runner fetches the value
(`fleet_dispatch.build_auth_lines`). Import-free leaf on Linux (the `edition` probe below tolerates absence):
loaded by path without the daemon tree.
"""
from __future__ import annotations

import re
import sys

# ── The wire contract (the CP re-exports each name from `apps/api/app/account_auth.py`) ───────────────

# `account_api_key:<slug>`: how a node asks for ONE account's API key. Derived from the slug, so the CP's
# branch is an exact parse. The colon keeps it non-path-shaped: a reader that forgot to branch cannot
# basename it into a plausible slug.
NODE_CREDENTIAL_PREFIX = "account_api_key:"

# `apikey:<slug>`: the dispatch reference stamped on a runner-break report so the CP can attribute a break
# to an account with no credential dir. A separate namespace from the credential name; neither parser
# accepts the other's. Without it a revoked key (`auth_rejected` is its only automatic exclusion) could
# never leave the spillover pool.
API_KEY_REF_PREFIX = "apikey:"

# ── The one PARSER of the credential name (both sides) ────────────────────────────────────────────────


# The slug grammar — a deliberate second spelling of the CP's `_SLUG_RE` on the account write paths
# (`routes/commands.py`, `routes/accounts.py`). Bounded (32 chars) so a node-token holder cannot push
# arbitrary bytes into the CP's log through it; padded and dot-segment forms are refused by construction.
_NODE_CREDENTIAL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")


def slug_from_node_credential_name(name):
    """The account slug a node-facing credential name asks about, or `""` when the name is not one of ours.

    `cred_broker._resolve` uses this to tell the name shapes apart (`github_token` answers `""`); the CP's
    `routes/agent/task_secrets.py::get_account_api_key` parses with the same function. Exact match, no
    stripping: a normalising parser has two spellings for one credential. `cred_broker._credential_name`
    strips at the socket (transport hygiene) before this runs."""
    text = str(name or "")
    if not text.startswith(NODE_CREDENTIAL_PREFIX):
        return ""
    slug = text[len(NODE_CREDENTIAL_PREFIX):]
    return slug if _NODE_CREDENTIAL_SLUG_RE.match(slug) else ""


# The credential-broker client: prints the value, or on ANY failure prints nothing and exits non-zero.
# Absolute because `launch.sh` runs in a tmux pane with no trusted PATH. Mirror of
# the reprovision script's `CREDENTIAL_HELPER_PATH`, pinned by the parity test.
CREDENTIAL_HELPER = "/usr/local/bin/jaah-credential"
if sys.platform == "win32":
    # A Windows node ships the client in its bundle (`bin/jaah-credential`),
    # spelled with forward slashes: `launch.sh` runs it from Git Bash.
    from host_platform import windows as _windows
    CREDENTIAL_HELPER = _windows.paths().lib_root.replace("\\", "/") + "/bin/jaah-credential"
elif sys.platform == "darwin":
    # So does a macOS node (`bin/jaah-credential`): nothing installs the Linux path there,
    # and every API-key runner refused to launch without its key (seen on hardware).
    from host_platform import macos as _macos
    CREDENTIAL_HELPER = _macos.paths().lib_root + "/bin/jaah-credential"
else:
    try:
        import edition
        _CE = edition.IS_CE
    except ImportError:  # read by path, without the daemon tree (the api, the repo's tests): never a node
        _CE = False
    if _CE:
        # So does a community-edition node: its installer has no root for /usr/local/bin, so the client is rendered
        # into the bundle's own `bin/` by the bundle's staging step.
        import host_platform
        CREDENTIAL_HELPER = host_platform.paths().lib_root + "/bin/jaah-credential"

# `accounts.auth_kind` for an API-key account.
AUTH_KIND_API_KEY = "api_key"
# Every other account (the CP's default and its migration's server default).
AUTH_KIND_OAUTH = "oauth"


def api_key_config_dir(jaah_home, slug):
    """THIS node's Claude config dir for an API-key account: `<jaah_home>/auth/<slug>`, or `""`.

    It holds the managed layer (plugins, settings, hooks) and the account's session files, never a token:
    the key still arrives per request through `apiKeyHelper`. The same path `app/node_clone.py::resolve_auth_dir`
    composes for an OAuth account, so the CP attributes these sessions to the account by the same parse.
    `""` for a home that is not absolute or a slug outside the grammar — never a guessed path."""
    home = str(jaah_home or "").rstrip("/")
    text = str(slug or "")
    if not home.startswith("/") or not _NODE_CREDENTIAL_SLUG_RE.match(text):
        return ""
    return f"{home}/auth/{text}"
