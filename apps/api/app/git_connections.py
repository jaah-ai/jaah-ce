"""Per-forge git connections: the single writer of `user_git_connections` and of the pre-0291 pointer
while both exist, and the one reader that lists them.

Resolution — which credential a task uses — is `app/git_access.py`'s; this module only writes what it
reads, and describes it. The overlap rules, exactly as pinned while two releases share the database:
  * READ (`git_access.selected_credential_id`): the row for the repo's root; with none, the pointer
    only when its credential's root is the repo's root.
  * WRITE (`set_connection`): the row, and the pointer too when the root matches the pointer's own root
    (or the pointer is empty and the root is `github.com`, the only root the outgoing release serves).
  * DELETE (`delete_connection`): the row, and the pointer too when it names that root, so a deleted
    connection is not resurrected by the fallback.
The pre-0291 writers of the pointer (Profile's picker, the admin user form, Connect GitHub) go through
`select_pointer`, which keeps the table in step with them. `connect_github` is the one home of storing a
GitHub OAuth grant as a person's connection, for Connect GitHub and a GitHub sign-in alike.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Literal, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import credential_consumers, forge_oauth, forges, git_access, git_probe, github_oauth
from app.actors import stamp_actor
from app.authz.principal import Principal, principal_for_user
from app.authz.visibility import scoped_own_credential_select
from app.forges import FIXED_ROOTS, FORGE_LABELS, RepoRef
from app.models.credentials import Credential, UserGitConnection
from app.models.identity import AdminUser
from app.user_activity import record_write

log = logging.getLogger("app.git_connections")

Status = Literal["connected", "needs_reconnect", "not_connected"]

# Bounds the page's probe threads (one per stale root; the tester's own permit pool caps the wire at 4).
_MAX_PROBES = 8

# The Git Access page has no task to restart.
_RETRY_PAGE = "then reload this page"


def root_ref(root: str) -> Optional[RepoRef]:
    """A `RepoRef` standing for a whole root (no repository path), for resolving a connection by root
    alone; None for a malformed root."""
    forge = forges.forge_of_root(root)
    if forge is None:
        return None
    return RepoRef(forge=forge, host=root.split("/", 1)[0], root=root, path="")


def _row(session: Session, user: AdminUser, root: str) -> Optional[UserGitConnection]:
    return session.scalars(
        select(UserGitConnection).where(
            UserGitConnection.user_id == user.id, UserGitConnection.root == root
        )
    ).first()


def _pointer_root(session: Session, user: AdminUser) -> Optional[str]:
    """The root the pre-0291 pointer's credential authenticates, or None (no pointer, or not git)."""
    if user.git_credential_id is None:
        return None
    pointed = session.scalars(select(Credential).where(Credential.id == user.git_credential_id)).first()
    return git_access.credential_root(pointed) if pointed is not None else None


def set_connection(
    session: Session, user: AdminUser, actor: AdminUser, root: str, credential: Credential
) -> UserGitConnection:
    """Make `credential` `user`'s connection for `root` (the caller has validated both), and keep the
    pointer in step (module docstring). Flushes, never commits. A NodeVault credential is refused: the
    control plane opens a git connection itself (D1, AC-5.3)."""
    credential_consumers.refuse_if_nodevault(credential, credential_consumers.GIT_ACCESS)
    forge = forges.forge_of_root(root)
    if forge is None or git_access.credential_root(credential) != root:
        # A caller bug: every route validates the pair before calling.
        raise ValueError(f"credential {credential.id} does not authenticate {root}")
    pointer_root = _pointer_root(session, user)
    row = _row(session, user, root)
    if row is None:
        row = UserGitConnection(user_id=user.id, org_id=user.org_id, root=root, forge=forge,
                                credential_id=credential.id)
        stamp_actor(row, actor, created=True)
        session.add(row)
    else:
        row.credential_id = credential.id
        row.forge = forge
        stamp_actor(row, actor)
    # Never an org credential (`owner_id IS NULL`): the pointer is the one selection resolution still
    # grandfathers past a lost org role (`git_access._readable_credential`), and only pre-0291 choices earn that.
    if credential.owner_id is not None and (
        pointer_root == root or (user.git_credential_id is None and root == "github.com")
    ):
        user.git_credential_id = credential.id
    session.flush()
    # A different (or newly re-pasted) token: its next use checks it at once.
    git_probe.forget(credential.id)
    return row


def delete_connection(session: Session, user: AdminUser, root: str) -> None:
    """Remove `user`'s connection for `root` alone, and the pointer with it when the pointer names that
    root. Idempotent. Flushes, never commits."""
    if _pointer_root(session, user) == root:
        user.git_credential_id = None
    session.execute(
        delete(UserGitConnection).where(
            UserGitConnection.user_id == user.id, UserGitConnection.root == root
        )
    )
    session.flush()


def select_pointer(
    session: Session, user: AdminUser, actor: AdminUser, credential_id: Optional[UUID]
) -> None:
    """A pre-0291 pointer write (`credential_id` already validated by `routes/auth.py`), mirrored into
    the table: selecting a git credential connects it for its own root; clearing the pointer deletes the
    connection for the root it named. Flushes, never commits."""
    if credential_id is None:
        root = _pointer_root(session, user)
        if root is not None:
            delete_connection(session, user, root)
        user.git_credential_id = None
        session.flush()
        return
    credential = session.scalars(select(Credential).where(Credential.id == credential_id)).first()
    if credential is not None:
        credential_consumers.refuse_if_nodevault(credential, credential_consumers.GIT_ACCESS)
    root = git_access.credential_root(credential) if credential is not None else None
    if credential is not None and root is not None:
        set_connection(session, user, actor, root, credential)
    user.git_credential_id = credential_id
    session.flush()


def connection_name(*segments: str) -> str:
    """A generated connection row's name: lowercase `/`-joined segments, whitespace as `-`. Only a new
    row is named; a reconnect keeps the row's own name, which its owner may have changed."""
    return "/".join("-".join(segment.lower().split()) for segment in segments)


def free_name(session: Session, user: AdminUser, name: str) -> str:
    """`name`, or `name-2`, `name-3`… — the first one no other row in `user`'s personal home holds (a
    credential name is unique per home, case-insensitively)."""
    taken = {
        held.lower()
        for held in session.scalars(select(Credential.name).where(Credential.owner_id == user.id)).all()
    }
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def github_credential_name(user: AdminUser, login: Optional[str]) -> str:
    """What a NEW connected GitHub row is called: `github/<login>`, else `github/<console email>`."""
    return connection_name("github", login or user.email or str(user.id))


def _github_description(login: Optional[str], access_token: str) -> str:
    """Says how the row got here — through which of the two apps `access_token` was minted by — and what
    maintains it, so a reader knows hand-editing the token will be overwritten by the next connect or sign-in."""
    who = f" as {login}" if login else ""
    app = "GitHub App" if github_oauth.is_app_issued(access_token) else "GitHub OAuth App"
    return (
        f"Connected through the console's {app}{who}, by Connect GitHub or a GitHub sign-in. "
        "The token is maintained automatically — reconnect from Profile > Git rather than editing it."
    )


def holds_grant(credential: Credential) -> bool:
    """Whether `credential` holds a forge's OAuth grant (a Connect made it) rather than a pasted token: a
    refresh token, the `oauth` kind (`forge_oauth.ForgeOAuthSpec.token_kind`), or a GitHub OAuth token
    (`gho_`/`ghu_`; a classic OAuth App's carries no refresh token). There is no marker field (`config`
    is registry-validated), and the name is the owner's to change."""
    config = credential.config or {}
    if config.get("refresh_token") or config.get("kind") == "oauth":
        return True
    token = git_access.readable_token(credential) if credential.type == "github" else None
    return token is not None and token.startswith(("gho_", "ghu_"))


def stamp_account(credential: Credential, account: Optional[str]) -> None:
    """Record the forge account `credential`'s token belongs to (the registry's `account` field), or nothing
    when the forge named none. The dict is reassigned: JSONB is tracked by identity."""
    if account:
        credential.config = {**(credential.config or {}), "account": account}


def _forge_account(credential: Credential, forge: str, grant: bool) -> Optional[str]:
    """Who a row from before `account` existed belongs to, asked of its forge with the call that stamps the
    field: a grant's account read (`forge_oauth.fetch_account`), a pasted token's test. Best effort and
    time-boxed by those calls; None on any failure."""
    token = git_access.readable_token(credential)
    if not token:
        return None
    try:
        if grant:
            return forge_oauth.fetch_account(forge_oauth.FORGE_OAUTH[forge], token)
        result = git_probe._run_tester(
            forge, git_probe.tester_config(credential.config or {}, token), git_probe.new_deadline()
        )
        return result.identity if result.ok else None
    except Exception as exc:  # noqa: BLE001 — a failed read is "no match", never a failed connect
        log.warning("git-connections: credential id=%s account read failed (%s)", credential.id, type(exc).__name__)
        return None


def own_row_for_root(
    session: Session,
    user: AdminUser,
    principal: Optional[Principal],
    forge: str,
    root: str,
    *,
    grant: bool,
    account: Optional[str],
) -> Optional[Credential]:
    """The row a reconnect renews: `user`'s OWN row of `forge` authenticating `root`, made by a Connect
    (`grant`) or a paste (not `grant`, `holds_grant`), for the same forge `account` — never by name, and never
    another account's row (a person may hold several accounts on one forge). The row `root`'s connection
    names is tried first, then the newest; a pasted row is renewed only while it is the connection (an
    unselected personal token may serve something else). A row with no `account` yet is asked of its forge
    (`_forge_account`) and stamped when it matches. With no `account` known (the forge's account read
    failed), only a row with none matches, so failed reads converge on one row. Read through
    `scoped_own_credential_select`, so a row the caller does not own is never written into."""
    candidates = [
        row
        for row in session.scalars(
            scoped_own_credential_select(principal)
            .where(
                # Defence in depth beside the scope: the row is this user's personal one, whoever the principal.
                Credential.owner_id == user.id,
                Credential.type == forge,
            )
            .order_by(Credential.created_at.desc(), Credential.id.desc())
        ).all()
        if git_access.credential_root(row) == root and holds_grant(row) == grant
    ]
    connection = _row(session, user, root)
    connected = [row for row in candidates if connection is not None and row.id == connection.credential_id]
    for row in connected if not grant else connected + [r for r in candidates if r not in connected]:
        stored = (row.config or {}).get("account")
        if account is None:
            if not stored:
                return row
            continue
        if not stored:
            resolved = _forge_account(row, forge, grant)
            # KNOWN LIMIT: a legacy row (no `account`) whose token its forge cannot read (revoked, expired,
            # unreachable) never matches here, so that reconnect creates a new row beside it.
            if resolved is None or resolved.lower() != account.lower():
                continue
            stamp_account(row, resolved)
            return row
        if str(stored).lower() == account.lower():
            return row
    return None


def _own_github_row(
    session: Session, user: AdminUser, principal: Optional[Principal], login: Optional[str]
) -> Optional[Credential]:
    """The github.com row an earlier connect made for this GitHub account (`own_row_for_root`)."""
    return own_row_for_root(session, user, principal, "github", "github.com", grant=True, account=login)


def sign_in_may_connect(
    session: Session, user: AdminUser, principal: Optional[Principal], login: Optional[str]
) -> bool:
    """Whether a GitHub sign-in's grant may become `user`'s github.com connection: nothing selected, the
    selection's grant dead (`grant_dead_at`), or the selection already being this account's own row.
    Never over a different live connection (a machine user's, a pasted PAT). A selection that cannot be
    read cleanly (a non-git pointer) is not replaced either."""
    try:
        selected_id = git_access.selected_credential_id(session, user, root_ref("github.com"))
    except git_access.GitAccessError:
        return False
    if selected_id is None:
        return True
    selected = session.scalars(select(Credential).where(Credential.id == selected_id)).first()
    if selected is None or (selected.config or {}).get("grant_dead_at"):
        return True
    own = _own_github_row(session, user, principal, login)
    return own is not None and own.id == selected.id


class GitHubConnected(NamedTuple):
    """What `connect_github` did: the row it stored on, and the token that row held before (None for a new
    row or an unreadable one), for `revoke_replaced` once the caller has committed. Never log `replaced`."""

    credential: Credential
    replaced: Optional[str]


def connect_github(
    session: Session,
    user: AdminUser,
    principal: Optional[Principal],
    grant: github_oauth.TokenGrant,
    *,
    login: Optional[str],
    via: str,
) -> GitHubConnected:
    """Store `grant` as `user`'s github.com connection: find or create their row (`_own_github_row`), write
    the grant (`git_access.store_grant`), select it (the pre-0291 pointer with it, `select_pointer`) and
    record `git_connected`. THE ONE HOME for Connect GitHub (`routes/git_connect.py`) and a GitHub sign-in
    (`routes/login_oauth.py`); `via` names which in the audit sentence. The caller has refused a token from
    the wrong kind of app (`git_access.minted_by`), and after its commit gives the replaced
    token back (`revoke_replaced`). Flushes, never commits."""
    replaced: Optional[str] = None
    credential = _own_github_row(session, user, principal, login)
    if credential is not None:
        replaced = git_access.readable_token(credential)
        # An UPDATE: `stamp_actor` touches only `updated_by_id`.
        stamp_actor(credential, user)
        credential.description = _github_description(login, grant.access_token)
    else:
        credential = Credential(
            name=free_name(session, user, github_credential_name(user, login)),
            type="github",
            description=_github_description(login, grant.access_token),
            # Empty: `git_access.store_grant` is the single writer of the OAuth fields.
            config={},
            # Always personal, never `stamp_owner_on_create`'s org row for an Org Owner/Admin: this is the
            # person's own GitHub token, and the lookup above matches on ownership, so an org row would be
            # duplicated on every reconnect and usable by every admin.
            owner_id=user.id,
            # Named, not left to the scope: a GET callback on the fleet plane stamps nothing.
            org_id=user.org_id,
        )
        stamp_actor(credential, user, created=True)
        session.add(credential)
        session.flush()
    git_access.store_grant(credential, grant)
    stamp_account(credential, login)
    # Until the connection names the row, `git_access.resolve_for_user` refuses this person's GitHub tasks.
    select_pointer(session, user, user, credential.id)
    record_write(
        session, user, "git_connected",
        f"Connected GitHub account {login or '(login unavailable)'} to credential {credential.name} ({via})",
        data={"credential_id": credential.id, "login": login},
    )
    session.flush()
    return GitHubConnected(credential=credential, replaced=replaced)


def revoke_replaced(connected: GitHubConnected, grant: github_oauth.TokenGrant) -> None:
    """Give back the token a renewed row held before (`connect_github`), once the new grant is committed: a
    classic OAuth App token never expires, so a superseded one would otherwise stay live at GitHub. Revoked
    with the pair of the app that minted it (`git_access.token_app_kind`), which may be the other app than the
    new grant's. Best effort (`github_oauth.revoke_user_token` never raises; a stray fault, an undesignated
    app included, is logged by class): a connect that stored its grant never fails over the old one."""
    if not connected.replaced or connected.replaced == grant.access_token:
        return
    try:
        app = git_access.app_config(kind=git_access.token_app_kind(connected.replaced))
        if not github_oauth.revoke_user_token(
            client_id=app.client_id, client_secret=app.client_secret, access_token=connected.replaced
        ):
            log.warning("git-connections: credential id=%s replaced token NOT revoked", connected.credential.id)
    except Exception as exc:  # noqa: BLE001 — the new grant is committed; nothing here may fail it
        log.warning("git-connections: credential id=%s replaced-token revoke failed (%s)",
                    connected.credential.id, type(exc).__name__)


def keep_sign_in_grant(user_id: UUID, grant: github_oauth.TokenGrant, *, login: Optional[str]) -> bool:
    """Store a GitHub sign-in's grant as `user_id`'s connection (`connect_github`) when
    `sign_in_may_connect` allows it, and commit; True when stored. The caller (`routes/login_oauth.py`) has
    already issued that membership a full session and refused a token from the wrong kind of app.

    On a fresh session bound to the membership's own row (`bind_from`): the sign-in door is BOOTSTRAP and
    binds nothing, and the principal is asked only about this user's OWN credential rows."""
    # Deferred: `app.db` installs ORM listeners at import time; route modules import this one.
    from app.db import SessionLocal
    from app.tenancy import seam

    with SessionLocal() as session, seam.bind_from(session, AdminUser, user_id):
        user = session.scalars(select(AdminUser).where(AdminUser.id == user_id)).one()
        principal = principal_for_user(session, user)
        if not sign_in_may_connect(session, user, principal, login):
            return False
        connected = connect_github(session, user, principal, grant, login=login, via="GitHub sign-in")
        session.commit()
    revoke_replaced(connected, grant)
    return True


def listed_roots(session: Session, user: AdminUser) -> list[str]:
    """The four fixed roots, then every self-managed GitLab root `user` has connected (a row, or the
    pointer's own root), sorted."""
    extra = set(session.scalars(
        select(UserGitConnection.root).where(UserGitConnection.user_id == user.id)
    ).all())
    pointer_root = _pointer_root(session, user)
    if pointer_root is not None:
        extra.add(pointer_root)
    return list(FIXED_ROOTS) + sorted(extra - set(FIXED_ROOTS))


class ConnectionState:
    """One root's resolved state, before it is rendered: the credential (when one is connected or needs
    reconnecting) and the refusal sentence when it is not usable."""

    def __init__(self, root: str, status: Status, credential: Optional[Credential], detail: Optional[str]):
        self.root = root
        self.forge = forges.forge_of_root(root) or "gitlab"
        self.status = status
        self.credential = credential
        self.detail = detail


def connection_states(session: Session, user: AdminUser) -> list[ConnectionState]:
    """Every listed root's state, as resolution would answer it now (`git_access.resolve_for_user`), with
    each stale connection re-checked at its forge first (`git_probe`, D-A14): in parallel, under ONE
    `CHECK_BUDGET_SECONDS` bound for the whole page. A check that does not finish leaves the state as
    it was."""
    states: list[ConnectionState] = []
    for root in listed_roots(session, user):
        ref = root_ref(root)
        if ref is None:
            continue
        try:
            credential = git_access.resolve_for_user(session, user.id, ref, retry=_RETRY_PAGE)
        except git_access.GitAccessNeedsReconnect as exc:
            states.append(ConnectionState(root, "needs_reconnect", _selected(session, user, ref), str(exc)))
            continue
        except git_access.GitAccessError as exc:
            states.append(ConnectionState(root, "not_connected", None, str(exc)))
            continue
        states.append(ConnectionState(root, "connected", credential, None))
    _check_stale(states)
    return states


def _selected(session: Session, user: AdminUser, ref: RepoRef) -> Optional[Credential]:
    credential_id = git_access.selected_credential_id(session, user, ref)
    if credential_id is None:
        return None
    return session.scalars(select(Credential).where(Credential.id == credential_id)).first()


def _check_stale(states: list[ConnectionState]) -> None:
    """Run the forge tester for every connected, stale credential, in parallel under one bound. A token
    due for renewal is skipped: it would test as dead only because it is about to be renewed, which is
    the next use's job (`git_access.token_for`)."""
    jobs: list[tuple[ConnectionState, dict]] = []
    for entry in states:
        credential = entry.credential
        if entry.status != "connected" or credential is None or not git_probe.is_stale(credential.id):
            continue
        if git_access._needs_refresh(credential) or not git_probe.claim_check(credential.id):
            continue
        try:
            token = git_access._stored_token(credential)
        except git_access.GitAccessError:
            continue
        jobs.append((entry, git_probe.tester_config(dict(credential.config or {}), token)))
    if not jobs:
        return
    deadline = git_probe.new_deadline()
    # Not a `with` block: its exit waits for every worker, which would unbound the page.
    pool = ThreadPoolExecutor(max_workers=min(len(jobs), _MAX_PROBES), thread_name_prefix="git-probe")
    futures = {
        pool.submit(git_probe.probe, entry.credential.type, entry.root, config, deadline=deadline): entry
        for entry, config in jobs
    }
    done, _pending = wait(futures, timeout=git_probe.CHECK_BUDGET_SECONDS + 0.5)
    pool.shutdown(wait=False)
    for future in done:
        entry = futures[future]
        credential = entry.credential
        if credential is None or future.result() != git_probe.REVOKED or git_probe.just_stored(
            credential.updated_at
        ):
            continue
        git_probe.mark_revoked(credential.id)
        entry.status = "needs_reconnect"
        label = FORGE_LABELS[entry.forge]
        entry.detail = (
            f"{label} reports that the token for {entry.root} has been revoked. Reconnect or paste "
            "a new token."
        )
