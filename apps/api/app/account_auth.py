"""How an account authenticates, and how that answer is spelled on the wire — the single home.

Two kinds: a subscription-OAuth account materializes a credential directory per node
(`app/node_clone.py::resolve_auth_dir`); an API-key account materializes nothing — its secret is a
broker row fetched at launch into the CLI's own key variable or key helper (the node adapter's answer,
`apps/jaahd/agents/base.py::api_key_env`), with no per-node token, quota window or refresh.
They stay visibly separate because they are different compliance postures; an API-key account routed down the
OAuth path fails silently with "Not logged in".

On the wire an OAuth account is its per-node config dir; an API-key account is `apikey:<slug>`, not
path-shaped so `account_slug_from_dir` refuses it. `accounts` holds only a `credential_id` pointer,
never key material.

The spellings the NODE also needs — the two kinds, the two prefixes, and the parser of the node-facing
credential name — have one home, the node's `apps/jaahd/account_auth.py` (the node never imports this
tree), LOADED here by path (`app/shared_module.py`: the api image COPYs it to `/app/shared/account_auth.py`;
the local stack mounts it read-only) and re-exported under the same names.
"""
from __future__ import annotations

from typing import Optional

from app import shared_module

_node = shared_module.load("_node_account_auth", shared_module.candidates("account_auth.py", "apps/jaahd"),
                           consequence="; the account-auth wire spellings need it")

# ── The vocabulary ────────────────────────────────────────────────────────────────────────────────────
#
# Text validated app-side, not a DB enum, so a new kind costs a constant rather than a migration. The DB
# enforces only `ck_accounts_api_key_has_credential`: an API-key account without a pointer is undispatchable.

AUTH_KIND_OAUTH: str = _node.AUTH_KIND_OAUTH
AUTH_KIND_API_KEY: str = _node.AUTH_KIND_API_KEY

#: Every kind a row may declare. `AUTH_KIND_OAUTH` is the default and the migration's server default.
AUTH_KINDS: tuple[str, ...] = (AUTH_KIND_OAUTH, AUTH_KIND_API_KEY)

#: The credential type an ANTHROPIC API-key account points at (`app/credentials/registry.py`). Each
#: provider names its own as `agent_providers.AgentProvider.api_key_credential_type`, which is what the
#: account writer reads; this constant is Claude's value, kept for its readers.
API_KEY_CREDENTIAL_TYPE = "anthropic_api_key"

#: The key inside that credential's config holding the secret. One spelling for reader and writer.
API_KEY_CONFIG_KEY = "api_key"


def is_api_key(auth_kind: Optional[str]) -> bool:
    """Whether this kind authenticates with an API key. Absent/unknown reads as OAuth — fail-safe: an
    unrecognised kind never acquires the quota-exempt, node-unpinned posture.
    """
    return auth_kind == AUTH_KIND_API_KEY


# ── The wire reference ────────────────────────────────────────────────────────────────────────────────

#: The prefix that makes an API-key reference unmistakable: no leading slash, so
#: `node_clone.account_slug_from_dir` refuses it outright. A node stamps it on a runner-break report.
API_KEY_REF_PREFIX: str = _node.API_KEY_REF_PREFIX


def _prefixed_slug(prefix: str, slug: Optional[str]) -> str:
    """`<prefix><slug>`, failing closed to `""` on the shapes `resolve_auth_dir` refuses (separators, dot
    segments). The one rule behind both wire spellings of an account below."""
    slug = (slug or "").strip().strip("/")
    if not slug or "/" in slug or slug in (".", ".."):
        return ""
    return f"{prefix}{slug}"


def api_key_account_ref(slug: Optional[str]) -> str:
    """`apikey:<slug>` — how an API-key account names itself on the dispatch plan and the lease intent."""
    return _prefixed_slug(API_KEY_REF_PREFIX, slug)


def is_api_key_account_ref(ref: Optional[str]) -> bool:
    """Whether this dispatch reference names an API-key account rather than a credential directory."""
    return str(ref or "").startswith(API_KEY_REF_PREFIX)


def account_slug_from_ref(ref: Optional[str]) -> str:
    """The account slug behind either spelling of a dispatch reference — `""` when unresolvable.

    An `apikey:` reference is parsed here; anything else goes to `node_clone.account_slug_from_dir`,
    which must stay byte-unchanged (pinned by its SQL twin and `tests/test_account_conversion.py`).
    """
    from app.node_clone import account_slug_from_dir

    text = str(ref or "").strip()
    if not text:
        return ""
    if text.startswith(API_KEY_REF_PREFIX):
        slug = text[len(API_KEY_REF_PREFIX):].strip()
        # The same refusals as `api_key_account_ref`, so the pair round-trips and cannot smuggle a path.
        if not slug or "/" in slug or slug in (".", ".."):
            return ""
        return slug
    return account_slug_from_dir(text)


# ── The node-facing credential name ───────────────────────────────────────────────────────────────────
#
# A node fetches an API-key account's secret over its own credential socket by a name derived from the
# slug (as git's askpass fetches `github_token`, `apps/jaahd/cred_broker.py`), so the CP parses exactly.

#: The prefix a node-facing credential name carries when it asks for one account's API key.
NODE_CREDENTIAL_PREFIX: str = _node.NODE_CREDENTIAL_PREFIX


def node_credential_name(slug: Optional[str]) -> str:
    """`account_api_key:<slug>` — the name a node asks its credential socket for. `""` when unresolvable.

    Same refusals as `api_key_account_ref` (`_prefixed_slug`): the string lands in a query parameter on a
    node-token route.
    """
    return _prefixed_slug(NODE_CREDENTIAL_PREFIX, slug)


# The parser of that name and its slug grammar (32 characters, lowercase alphanumerics plus `-`/`_`, alphanumeric
# first — a second spelling of `_SLUG_RE` on the account write paths, `routes/commands.py`, `routes/accounts.py`)
# live in the node's file: it composes the name (`cred_broker`), the CP parses it back
# (`routes/agent/task_secrets.py::get_account_api_key`). Exact match, no stripping — stricter than
# `api_key_account_ref`: on a secret-serving surface a normalising parser gives one credential two spellings.
slug_from_node_credential_name = _node.slug_from_node_credential_name
