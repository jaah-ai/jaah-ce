"""Per-forge git connections: the single writer of `user_git_connections`, and the one reader that lists them.

A person may connect any number of accounts on one root; exactly one of them is the root's default.
Resolution — which credential a task uses — is `app/git_access.py`'s; this module only writes what it
reads, and describes it. Profile's single-credential picker and the admin user form write through
`select_credential`, which makes their pick its root's default; the one credential those forms show is
`selected_credential_ids`. `connect_github` is the one home of storing a
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


def _row(session: Session, user: AdminUser, credential_id: UUID) -> Optional[UserGitConnection]:
    return session.scalars(
        select(UserGitConnection).where(
            UserGitConnection.user_id == user.id, UserGitConnection.credential_id == credential_id
        )
    ).first()


def _default_row(session: Session, user: AdminUser, root: str) -> Optional[UserGitConnection]:
    rows = git_access.root_connections(session, user, root)
    return rows[0] if rows else None


def set_connection(
    session: Session, user: AdminUser, actor: AdminUser, root: str, credential: Credential,
    *, make_default: bool = False,
) -> UserGitConnection:
    """Connect `credential` as one of `user`'s accounts on `root` (the caller has validated both): a new row
    beside the root's others — its default when it is the first, or when `make_default` — or the existing
    row for it. Flushes, never commits. A NodeVault
    credential is refused: the control plane opens a git connection itself (D1, AC-5.3)."""
    credential_consumers.refuse_if_nodevault(credential, credential_consumers.GIT_ACCESS)
    forge = forges.forge_of_root(root)
    if forge is None or git_access.credential_root(credential) != root:
        # A caller bug: every route validates the pair before calling.
        raise ValueError(f"credential {credential.id} does not authenticate {root}")
    current_default = _default_row(session, user, root)
    row = _row(session, user, credential.id)
    if row is None:
        row = UserGitConnection(user_id=user.id, org_id=user.org_id, root=root, forge=forge,
                                credential_id=credential.id, is_default=current_default is None)
        stamp_actor(row, actor, created=True)
        session.add(row)
    else:
        row.forge = forge
        stamp_actor(row, actor)
    if make_default and current_default is not None and current_default is not row:
        # Cleared and flushed first: one default per root is a partial unique index.
        current_default.is_default = False
        session.flush()
        row.is_default = True
    elif current_default is None:
        row.is_default = True
    session.flush()
    # A different (or newly re-pasted) token: its next use checks it at once.
    git_probe.forget(credential.id)
    return row


def delete_connection(
    session: Session, user: AdminUser, root: str, credential_id: Optional[UUID] = None
) -> None:
    """Remove `user`'s connection to `credential_id` on `root` (every connection on `root` when None); the
    root's oldest remaining one becomes its default. Idempotent. Flushes, never commits."""
    stmt = delete(UserGitConnection).where(UserGitConnection.user_id == user.id, UserGitConnection.root == root)
    if credential_id is not None:
        stmt = stmt.where(UserGitConnection.credential_id == credential_id)
    deleted = session.scalars(stmt.returning(UserGitConnection.credential_id)).all()
    if user.git_credential_id in deleted:
        # The outgoing release falls back to this pointer on a root with no connection: clear it, so that
        # release never revives a deleted one. Write-only; dropped with the column.
        user.git_credential_id = None
    session.flush()
    remaining = git_access.root_connections(session, user, root)
    if remaining and not remaining[0].is_default:
        remaining[0].is_default = True
        session.flush()


def selected_credential_ids(session: Session, user_ids: list[UUID]) -> dict[UUID, UUID]:
    """Each of `user_ids`' one shown Git Access credential: their `github.com` default, else their oldest
    default on another root. A user with no connection is absent. One query, whatever the list's size."""
    rows = session.execute(
        select(UserGitConnection.user_id, UserGitConnection.credential_id)
        .where(UserGitConnection.user_id.in_(user_ids), UserGitConnection.is_default.is_(True))
        .order_by(
            UserGitConnection.user_id, UserGitConnection.root != "github.com",
            UserGitConnection.created_at, UserGitConnection.id,
        )
    ).all()
    selected: dict[UUID, UUID] = {}
    for user_id, credential_id in rows:
        selected.setdefault(user_id, credential_id)
    return selected


def select_credential(
    session: Session, user: AdminUser, actor: AdminUser, credential_id: Optional[UUID]
) -> None:
    """The single-credential forms' write (`credential_id` already validated by `routes/auth.py`):
    selecting a git credential connects it as its root's default; clearing deletes the connection
    `selected_credential_ids` shows. Flushes, never commits."""
    if credential_id is None:
        current = selected_credential_ids(session, [user.id]).get(user.id)
        row = _row(session, user, current) if current is not None else None
        if row is not None:
            delete_connection(session, user, row.root, current)
        # The pre-0291 pointer grandfathers an org credential (`authz.git_identity_select`'s `current_id`):
        # an explicit clear ends it too. Write-only; dropped with the column.
        user.git_credential_id = None
        session.flush()
        return
    credential = session.scalars(select(Credential).where(Credential.id == credential_id)).one()
    root = git_access.credential_root(credential)
    if root is None:
        # A caller bug: `validated_git_credential_id` refuses a non-git credential.
        raise ValueError(f"credential {credential.id} authenticates no git root")
    set_connection(session, user, actor, root, credential, make_default=True)


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


def _renewed_account(session: Session, credential: Credential, forge: str) -> Optional[str]:
    """A legacy grant row's account when its token had expired: the grant renewed first
    (`git_access.renew_if_expiring`) on a session of its own, since the rotation commits (`token_for`) and the
    caller's transaction (a Connect callback's) must not, then read again. A grant its forge refused to renew is
    marked Needs reconnect (`git_probe.mark_revoked`), so a runner's fetch refuses it (410) and a reconnect's live
    grant takes its place as the default (`default_grant_dead`). None when nothing was renewed or the renewed token's read failed too."""
    from app.db import BridgedSession

    if git_access.grant_dead(credential):
        return None
    before = git_access.readable_token(credential)
    try:
        try:
            with BridgedSession() as side:
                # A select, not `session.get`: the tenancy seam filters it to the request's bound org.
                row = side.scalar(select(Credential).where(Credential.id == credential.id))
                if row is None:
                    return None
                git_access.renew_if_expiring(side, row)
        except git_access.GitAccessNeedsReconnect:
            git_probe.mark_revoked(credential.id, reason="could not be renewed at a reconnect")
            session.refresh(credential)
            return None
        session.refresh(credential)
    except Exception as exc:  # noqa: BLE001 — a failed renewal is "no match", never a failed connect
        log.warning("git-connections: credential id=%s renewal failed (%s)", credential.id, type(exc).__name__)
        return None
    if git_access.readable_token(credential) == before:
        return None
    return _forge_account(credential, forge, grant=True)


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
    names are tried first, then the newest; a pasted row is renewed only while it is connected (an
    unconnected personal token may serve something else). A row with no `account` yet is asked of its forge
    (`_forge_account`), its expired grant renewed first when that read fails (`_renewed_account`), and stamped
    when it matches. With no `account` known (the forge's account read failed), only a row with none matches, so failed reads converge on one row. Read through
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
    connected_ids = {c.credential_id for c in git_access.root_connections(session, user, root)}
    connected = [row for row in candidates if row.id in connected_ids]
    for row in connected if not grant else connected + [r for r in candidates if r not in connected]:
        stored = (row.config or {}).get("account")
        if account is None:
            if not stored:
                return row
            continue
        if not stored:
            resolved = _forge_account(row, forge, grant)
            if resolved is None and grant:
                resolved = _renewed_account(session, row, forge)
            # A legacy row its forge still cannot read never matches, so that reconnect creates a new row beside
            # it. One whose renewal the forge refused is marked Needs reconnect first; an unexpired one whose read
            # failed is left alone (KNOWN LIMIT: a failed read alone does not prove the grant dead).
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
    selection's grant dead (`git_access.grant_dead`), or the selection already being this account's own row.
    Never over a different live connection (a machine user's, a pasted PAT)."""
    selected_id = git_access.selected_credential_id(session, user, root_ref("github.com"))
    if selected_id is None:
        return True
    selected = session.scalars(select(Credential).where(Credential.id == selected_id)).first()
    if selected is None or git_access.grant_dead(selected):
        return True
    own = _own_github_row(session, user, principal, login)
    return own is not None and own.id == selected.id


def default_grant_dead(session: Session, user: AdminUser, root: str) -> bool:
    """Whether `user`'s default connection on `root` holds a dead grant (`git_access.grant_dead`): a reconnect's live
    grant then heals it by becoming the default (`set_connection`'s `make_default`)."""
    default = _default_row(session, user, root)
    if default is None:
        return False
    credential = session.scalars(select(Credential).where(Credential.id == default.credential_id)).first()
    return credential is not None and git_access.grant_dead(credential)


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
    the grant (`git_access.store_grant`), connect it beside any other GitHub account (`set_connection`) and
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
    # Until a connection names the row, `git_access.resolve_for_user` refuses this person's GitHub tasks. Another
    # account joins beside the connected ones and the root's default stays theirs (`set_connection`), unless the
    # default's grant is dead (`git_access.grant_dead`): then this live grant heals it by becoming the default.
    heals = default_grant_dead(session, user, "github.com")
    set_connection(session, user, user, "github.com", credential, make_default=heals)
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


def sign_in_install_url(user_id: UUID, slug: str, login: Optional[str], access_token: str) -> Optional[str]:
    """The GitHub App's install page when a sign-in's kept connection reaches no installation, else None
    (`routes/git_connect.py::needs_installation`). On a fresh session bound to the membership's own row, as
    `keep_sign_in_grant`; blocking (it pages GitHub's installations), so an async caller runs it on a thread."""
    from app.db import BridgedSession
    from app.routes import git_connect
    from app.tenancy import seam

    with BridgedSession() as session, seam.bind_from(session, AdminUser, user_id):
        if git_connect.needs_installation(session, slug, login, access_token):
            return git_connect.install_url(slug)
    return None


def keep_sign_in_grant(user_id: UUID, grant: github_oauth.TokenGrant, *, login: Optional[str]) -> bool:
    """Store a GitHub sign-in's grant as `user_id`'s connection (`connect_github`) when
    `sign_in_may_connect` allows it, and commit; True when stored. The caller (`routes/login_oauth.py`) has
    already issued that membership a full session and refused a token from the wrong kind of app.

    On a fresh session bound to the membership's own row (`bind_from`): the sign-in door is BOOTSTRAP and
    binds nothing, and the principal is asked only about this user's OWN credential rows."""
    # Deferred: `app.db` installs ORM listeners at import time; route modules import this one.
    from app.db import BridgedSession
    from app.tenancy import seam

    with BridgedSession() as session, seam.bind_from(session, AdminUser, user_id):
        user = session.scalars(select(AdminUser).where(AdminUser.id == user_id)).one()
        principal = principal_for_user(session, user)
        if not sign_in_may_connect(session, user, principal, login):
            return False
        connected = connect_github(session, user, principal, grant, login=login, via="GitHub sign-in")
        session.commit()
    revoke_replaced(connected, grant)
    return True


def listed_roots(session: Session, user: AdminUser) -> list[str]:
    """The four fixed roots, then every self-managed GitLab root `user` has connected, sorted."""
    extra = set(session.scalars(
        select(UserGitConnection.root).where(UserGitConnection.user_id == user.id)
    ).all())
    return list(FIXED_ROOTS) + sorted(extra - set(FIXED_ROOTS))


class ConnectionState:
    """One account's resolved state on one root, before it is rendered: the credential (when one is
    connected or needs reconnecting), whether it is the root's default, and the refusal sentence when it is
    not usable."""

    def __init__(self, root: str, status: Status, credential: Optional[Credential], detail: Optional[str],
                 *, is_default: bool = True):
        self.root = root
        self.forge = forges.forge_of_root(root) or "gitlab"
        self.status = status
        self.credential = credential
        self.detail = detail
        self.is_default = is_default


def connection_state(
    session: Session, user: AdminUser, root: str, credential_id: Optional[UUID] = None, *, is_default: bool = True
) -> ConnectionState:
    """`root`'s state through the account `credential_id` (resolution's own pick when None), as resolution
    would answer it now (`git_access.resolve_for_user`), without asking the forge."""
    ref = root_ref(root)
    assert ref is not None  # narrowing: every caller passes a listed or validated root
    try:
        credential = git_access.resolve_for_user(
            session, user.id, ref, retry=_RETRY_PAGE, credential_id=credential_id
        )
    except git_access.GitAccessNeedsReconnect as exc:
        selected = credential_id or git_access.selected_credential_id(session, user, ref)
        stale = (
            session.scalars(select(Credential).where(Credential.id == selected)).first()
            if selected is not None else None
        )
        return ConnectionState(root, "needs_reconnect", stale, str(exc), is_default=is_default)
    except git_access.GitAccessError as exc:
        return ConnectionState(root, "not_connected", None, str(exc), is_default=is_default)
    return ConnectionState(root, "connected", credential, None, is_default=is_default)


def connection_states(session: Session, user: AdminUser) -> list[ConnectionState]:
    """Every listed root's state — one entry per connected account, default first, or the root's single
    `not_connected` entry — with each stale connection re-checked at its forge first (`git_probe`, D-A14): in
    parallel, under ONE `CHECK_BUDGET_SECONDS` bound for the whole page. A check that does not finish leaves
    the state as it was."""
    states: list[ConnectionState] = []
    for root in listed_roots(session, user):
        if root_ref(root) is None:
            continue
        rows = git_access.root_connections(session, user, root)
        if not rows:
            states.append(connection_state(session, user, root))
            continue
        states.extend(
            connection_state(session, user, root, row.credential_id, is_default=row.is_default) for row in rows
        )
    _check_stale(states)
    return states


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
