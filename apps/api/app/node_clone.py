"""Where a project's main clone and an account's config dir live on a given node; the one resolution.

Two readers must never disagree about the same slot: `app/routes/pool_slots.py` writes `path` when a
slot is declared, and the pool tick in `app/routes/agent/pool_tick.py` ships `clone` to the node, which runs
`git -C <clone> worktree add` from it. A mismatch would make the complement DELETE read the declared
row as a slot with no tree.

`resolve_clone` returns `<jaah_home>/repos/<project_id>/main`, keyed on project identity
(`projects.id`), so a repo rename moves nothing and two projects sharing one repo never collide; or `""`
when either input is missing: no implicit default. Callers already refuse on `""` (the tick omits the
slots, the declare route answers 409).
"""
from __future__ import annotations

import re
from typing import Optional


REPOS_DIRNAME = "repos"
# A multi-repo slot's non-primary repos sit beside the primary's slots, under
# `<home>/repos/<project_id>/_repos/`: a clone per repo (`<repo_id>/main`), its worktrees (`<repo_id>/<N>`) and
# one manifest per slot (`slot-<N>.json`). The node owns the layout; these mirror it. Nothing under it is a slot.
NON_PRIMARY_DIRNAME = "_repos"

# A Windows node's home is a drive path (`C:\ProgramData\Jaah\home`); every path this module
# builds under it still joins with `/`, which Windows accepts. A node reports such paths back with `\`.
_WINDOWS_ABSOLUTE = re.compile(r"[A-Za-z]:[\\/]")


def is_absolute_node_path(path: str) -> bool:
    """An absolute path on SOME node OS: posix `/…`, or a Windows drive path `C:\\…` / `C:/…`.

    The one home of that rule for every node-reported path (`session_transcripts.is_a_plain_absolute_path`
    reads it too): a second, posix-only copy is how a Windows node's session report came to 422."""
    return path.startswith("/") or bool(_WINDOWS_ABSOLUTE.match(path))


def node_path_segments(path: str) -> list[str]:
    """Path components across both separators, so a `..` cannot hide behind a backslash."""
    return path.replace("\\", "/").split("/")


def resolve_clone(jaah_home: Optional[str], project_id: Optional[int]) -> str:
    """The main-clone absolute path for a project on a node (module docstring has the rule).
    `project_id` is `projects.id`."""
    home = (jaah_home or "").strip().rstrip("/")
    if not home or project_id is None:
        return ""
    return f"{home}/{REPOS_DIRNAME}/{project_id}/main"


def _non_primary_root(jaah_home: Optional[str], project_id: Optional[int]) -> str:
    """Beside the main clone, so the home and missing-input rules stay `resolve_clone`'s."""
    clone = resolve_clone(jaah_home, project_id)
    return f"{clone.rsplit('/', 1)[0]}/{NON_PRIMARY_DIRNAME}" if clone else ""


def repo_clone(jaah_home: Optional[str], project_id: Optional[int], repo_id: Optional[int]) -> str:
    """A non-primary repo's own clone, `<home>/repos/<pid>/_repos/<rid>/main`; `""` on a missing input."""
    root = _non_primary_root(jaah_home, project_id)
    return f"{root}/{repo_id}/main" if root and repo_id is not None else ""


def repo_worktree(
    jaah_home: Optional[str], project_id: Optional[int], repo_id: Optional[int], number: Optional[int]
) -> str:
    """That repo's worktree for slot `number`, `<home>/repos/<pid>/_repos/<rid>/<N>`; `""` on a missing input."""
    root = _non_primary_root(jaah_home, project_id)
    return f"{root}/{repo_id}/{number}" if root and repo_id is not None and number is not None else ""


def clone_for_slot_path(jaah_home: Optional[str], path: Optional[str], number: int) -> str:
    """The main clone a slot's STORED path implies — `path` itself for slot 0, else `<dirname>/main` — or
    `""` when `path` is not shaped `.../{main,N}` for this `number` or does not sit under the node's
    recorded `jaah_home` (the same containment the node's erase applies; no home, no answer).

    What an erase must aim at: the row's `worktrees.path` is where the node last reported the tree."""
    home = (jaah_home or "").strip().rstrip("/")
    if _WINDOWS_ABSOLUTE.match(home):
        return _windows_clone_for_slot_path(home, path, number)
    raw = (path or "").rstrip("/")
    # A `..` segment could climb out of the home a prefix test says it is under: refused, not normalized.
    if not home or not raw.startswith(home + "/") or ".." in raw.split("/"):
        return ""
    # A non-primary repo's worktree is shaped `.../<rid>/<N>` too, but it is no slot and has no main clone of its own.
    if NON_PRIMARY_DIRNAME in raw[len(home) + 1:].split("/"):
        return ""
    parent, base = raw.rsplit("/", 1)
    if number == 0:
        return raw if base == "main" else ""
    return f"{parent}/main" if base == str(number) else ""


def _windows_clone_for_slot_path(home: str, path: Optional[str], number: int) -> str:
    """`clone_for_slot_path` for a Windows node's drive-path home, same rule, same refusals.

    Such a node reports a slot as `C:\\…\\repos\\7\\3` (ntpath-normalized) while the CP's own derivation
    joins under the home with `/`, so containment compares both separators as one and case-insensitively
    (NTFS). The answer keeps the input's own spelling, with `main` joined by the separator the input used
    at that point: the node joins with whatever it is handed, and Windows accepts either."""
    home, raw = home.rstrip("/\\"), (path or "").rstrip("/\\")

    def key(value: str) -> str:
        return value.replace("\\", "/").lower()

    # A bare drive (`C:`) is refused as the posix side refuses `/`; `..` is refused under either separator.
    if not home[2:] or not key(raw).startswith(key(home) + "/") or ".." in node_path_segments(raw):
        return ""
    if NON_PRIMARY_DIRNAME in node_path_segments(raw[len(home) + 1:]):
        return ""
    cut = max(raw.rfind("/"), raw.rfind("\\"))
    parent, separator, base = raw[:cut], raw[cut], raw[cut + 1:]
    if number == 0:
        return raw if base == "main" else ""
    return f"{parent}{separator}main" if base == str(number) else ""

AUTH_DIRNAME = "auth"


def resolve_auth_dir(jaah_home: Optional[str], slug: Optional[str]) -> str:
    """Where a credential account's config dir lives on a given node: `{jaah_home}/auth/<slug>`.

    The CP answers it and the node obeys verbatim; jaahd has no say (owner directive). It is per-node
    because one global `accounts.config_dir` could only be right for one host.

    Fails closed to `""` like `resolve_clone`; never a literal `/home/jaah` fallback, which would point a
    runner at a directory nobody provisioned."""
    home = (jaah_home or "").strip().rstrip("/")
    slug = (slug or "").strip().strip("/")
    if not home or not slug:
        return ""
    # The home gets the same discipline as the slug so this and `account_slug_from_dir` are a true pair;
    # `jaah_home` is free text, so a typo would otherwise silently unattribute a whole node.
    if not is_absolute_node_path(home) or ".." in node_path_segments(home):
        return ""
    # Closed on a slug that is not one path segment: this builds the path a deprovision later deletes.
    # Roster writers enforce `_SLUG_RE`, so this only fires on a row that bypassed them.
    if "/" in slug or slug in (".", ".."):
        return ""
    return f"{home}/{AUTH_DIRNAME}/{slug}"


# ── contract S3: the inverse of `resolve_auth_dir`, kept beside it so the two cannot drift ──
# Used by `app/fleet_observe.py` and `app/account_fold.py`. Not injective: `registry_norm` does not
# `normpath`, so `…/auth//acct2` and `…/auth/acct2` both map to `acct2`; callers must survive that.

from typing import Iterable, Optional as _Optional  # noqa: E402  (grouped with the S3 block)
from uuid import UUID  # noqa: E402


def account_slug_from_dir(config_dir: _Optional[str]) -> str:
    """The account slug a credential dir names (its last component), `""` when unresolvable.

    Normalizes first: the CP-side home of the rule mirrored by `apps/jaahd/env_helpers.py::_norm_dir`;
    both sides must produce the identical string or the read-time join misses.

    Refuses a non-absolute path (no `expanduser`: `~` would expand against the container's `$HOME`) and
    a `..` segment (normpath would turn `<home>/auth/acct1/..` into `auth`, a valid slug).

    `""` rather than a raise; the caller decides what "no account" costs. 0109's `_basename_sql` is the
    SQL twin, and `tests/test_account_conversion.py` runs the pair over a corpus."""
    import os

    stripped = str(config_dir or "").strip()
    if not stripped or not is_absolute_node_path(stripped):
        return ""
    if ".." in node_path_segments(stripped):
        return ""
    if not stripped.startswith("/"):
        # A Windows node normalizes to `\\` (`apps/jaahd/env_helpers.py::_norm_dir` runs ntpath there).
        return [part for part in node_path_segments(stripped) if part][-1]
    return os.path.basename(os.path.normpath(stripped))


def account_ids_for_dirs(session, dirs: Iterable[_Optional[str]]) -> dict[str, UUID]:
    """`{config_dir as given: accounts.id}` for every dir that resolves, in one query.

    Batched because it runs per worktree row inside the 15s ledger ingest. Keyed by the caller's own
    spelling, so two spellings of one account appear as two keys. An unresolved dir is absent.

    Also resolves the `apikey:<slug>` spelling via `account_auth.account_slug_from_ref`, which hands
    everything else to `account_slug_from_dir` unchanged. A node never reports `apikey:<slug>`: an API-key
    runner's session lives in the node-derived, token-less `<home>/auth/<slug>`, parsed like any other; the
    CP's decision is `leased_account_id`, written by `app/slot_state.py::bind_account`."""
    from sqlalchemy import select

    from app import account_auth
    from app.models.credentials import Account

    slugs: dict[str, str] = {}
    for config_dir in dirs:
        if config_dir is None:
            continue
        slug = account_auth.account_slug_from_ref(config_dir)
        if slug:
            slugs[str(config_dir)] = slug
    if not slugs:
        return {}
    ids_by_slug = {
        slug: account_id
        for account_id, slug in session.execute(
            select(Account.id, Account.slug).where(Account.slug.in_(set(slugs.values())))
        ).all()
    }
    return {
        config_dir: ids_by_slug[slug]
        for config_dir, slug in slugs.items()
        if slug in ids_by_slug
    }


def account_id_for_dir(session, ref: _Optional[str]):
    """One reference resolved to its account id, or None when it names no roster account.

    A shape adapter over `account_ids_for_dirs` (so there is one lookup), for `app/lease_drain.py`, which
    holds a single reference. Accepts the `apikey:<slug>` spelling too."""
    return account_ids_for_dirs(session, [ref]).get(ref)


def account_slug_expr(column):
    """An account-uuid `column` rendered as that account's slug, resolved at query time.

    A correlated subquery (like `app/node_identity.py::name_expr`) because the payload builders already
    carry outer joins and another join would change their row multiplicity. The path itself is built in
    Python by `resolve_auth_dir`, never in SQL.

    NULL-preserving: an unresolved reference renders NULL, which `resolve_auth_dir` turns into `""`."""
    from sqlalchemy import select

    from app.models.credentials import Account

    return select(Account.slug).where(Account.id == column).scalar_subquery()
