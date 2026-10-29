"""Snapshot an SDK agent's repo at one commit into object storage (architecture §4.1/§4.2, DD3–DD5).

Registration, an edit of `ref`/`subpath`, the console's Refresh and the task-pipeline sweep all call ONE
function, `take_snapshot`, which reads the repo as the person named on the row (their own GitHub
connection, Profile > Git, exactly as the docs browser does, `routes/projects.py::_docs_token`):

1. resolve `ref` to a 40-hex commit (`resolve_commit`);
2. download that commit's tarball (`fetch_archive`) and keep only the agent root (`subpath`);
3. read `jaah-agent.toml` from it and validate it against the tree (`app/agents.py::validate_manifest`);
4. re-pack the agent root as a deterministic `.tar.gz` (members relative to the agent root, regular files
   and directories only, owner 0, mode 0644/0755), capped at `MAX_BUNDLE_BYTES`.

`store_bundle` then puts it at `agent-bundles/<agent_id>/<commit>.tar.gz` in the attachment bucket; nodes
fetch it through `GET /api/agent/tasks/{id}/agent-bundle` and check `bundle_sha256` (K4).

Every failure is `SnapshotRefused` carrying sentences an author can act on and writes nothing. v1 reads
GitHub only (DD5): any other forge is refused at registration by `parse_repo`. The forge is read here, off
the dispatch path (DD4): dispatch only ever reads the row this module wrote.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import logging
import posixpath
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import acp_registry, agents, attachment_storage, forges, git_access, github_api
from app.agents import MANIFEST_FILENAME, STATUS_ACTIVE, Manifest, short_sha, validate_manifest
from app.models.agent_sessions import Agent
from app.models.identity import AdminUser

log = logging.getLogger("app.agent_bundles")

#: The object-storage prefix, disjoint from `transcripts/`, `artifacts/` and `task-attachments/`.
KEY_PREFIX = "agent-bundles"
#: The largest re-packed bundle a node will be asked to download (§4.1).
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
#: The largest whole-repo archive read to cut a bundle from; the agent root is usually a small part of it.
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
#: The largest agent root kept in memory to re-pack: a root bigger than the bundle cap cannot be a bundle.
MAX_ROOT_BYTES = 2 * MAX_BUNDLE_BYTES
#: The most bytes decompressed from one archive, and the most entries read from it, so a compressed bomb or
#: a million empty files fails with a sentence rather than holding the API's memory.
MAX_UNPACKED_BYTES = 400 * 1024 * 1024
MAX_MEMBERS = 20_000
#: A branch, tag or commit as git spells one; it is put into an API path, so nothing else is accepted.
_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

_ARCHIVE_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


class SnapshotRefused(Exception):
    """The repo could not be snapshotted. `errors` are sentences for the author; nothing was written.
    `status` is the HTTP status a console route answers with (422, or 503 when storage is down)."""

    def __init__(self, errors: list[str], *, status: int = 422) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.status = status


@dataclass(frozen=True)
class Snapshot:
    """One commit of one agent root, validated and packed."""

    commit: str
    manifest: Manifest
    bundle: bytes
    sha256: str


def parse_repo(repo_url: str) -> forges.RepoRef:
    """The GitHub repo `repo_url` names, or a refusal sentence (DD5: other forges are deferred)."""
    try:
        repo = forges.parse_repo_url((repo_url or "").strip(), None)
    except forges.ForgeUrlError as exc:
        raise SnapshotRefused([f"`repo_url` is not a repository URL: {exc}"]) from None
    if repo.forge != "github":
        raise SnapshotRefused([
            f"Agents can be registered from GitHub repositories only for now; {repo.web_url()} is on "
            f"{forges.FORGE_LABELS[repo.forge]}"
        ])
    return repo


def normalised_subpath(subpath: Optional[str]) -> str:
    """`subpath` as a clean relative directory (`""` = the repo root), or a refusal sentence."""
    raw = (subpath or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    parts = raw.split("/")
    if ".." in parts or any(not part for part in parts):
        raise SnapshotRefused([f"`subpath` must be a folder inside the repo with no '..', got {subpath}"])
    return posixpath.normpath(raw)


def bundle_key(agent_id: UUID, commit: str) -> str:
    """Where one agent's bundle for one commit lives. Derived from the row alone, never client-supplied."""
    return f"{KEY_PREFIX}/{UUID(str(agent_id))}/{commit}.tar.gz"


def _token(session: Session, user_id: UUID, repo: forges.RepoRef, *, retry: str) -> str:
    """`user_id`'s own GitHub token for `repo`, or the refusal naming what to reconnect."""
    try:
        credential = git_access.resolve_for_user(session, user_id, repo, retry=retry)
        return git_access.token_for(session, credential)
    except git_access.GitAccessError as exc:
        raise SnapshotRefused([str(exc)]) from None


def _unreachable(repo: forges.RepoRef, detail: str) -> SnapshotRefused:
    return SnapshotRefused([f"GitHub could not be read for {repo.path} ({detail}); try again shortly"])


def resolve_commit(repo: forges.RepoRef, ref: str, *, token: str) -> str:
    """The 40-hex commit `ref` (a branch, tag or sha) names in `repo`."""
    owner, name = repo.path.split("/", 1)
    try:
        with github_api._client(token) as client:
            resp = client.get(
                f"/repos/{owner}/{name}/commits/{ref}", headers={"Accept": "application/vnd.github.sha"}
            )
    except httpx.HTTPError as exc:
        raise _unreachable(repo, type(exc).__name__) from None
    if resp.status_code == 200 and len(resp.text.strip()) == 40:
        return resp.text.strip().lower()
    if resp.status_code in (404, 422):
        if not github_api._repo_visible(repo.path, token=token):
            raise SnapshotRefused([
                f"{repo.path} did not answer — it is private to the GitHub account you connected, or it "
                f"does not exist"
            ])
        raise SnapshotRefused([f"`ref` {ref} is not a branch, tag or commit of {repo.path}"])
    raise _unreachable(repo, f"HTTP {resp.status_code}")


def fetch_archive(forge: str, repo: forges.RepoRef, sha: str, *, token: str) -> bytes:
    """The `.tar.gz` of `repo` at commit `sha`, capped at `MAX_ARCHIVE_BYTES`. GitHub only (DD5): the one
    seam a later forge plugs into."""
    if forge != "github":
        raise SnapshotRefused([f"Reading agent code from {forges.FORGE_LABELS.get(forge, forge)} is not supported yet"])
    owner, name = repo.path.split("/", 1)
    chunks: list[bytes] = []
    total = 0
    try:
        # The tarball endpoint redirects to codeload; httpx drops the Authorization header cross-origin.
        with github_api._client(token, timeout=_ARCHIVE_TIMEOUT) as client:
            with client.stream("GET", f"/repos/{owner}/{name}/tarball/{sha}", follow_redirects=True) as resp:
                if resp.status_code != 200:
                    raise _unreachable(repo, f"archive download answered HTTP {resp.status_code}")
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise SnapshotRefused([
                            f"{repo.path} at commit {short_sha(sha)} is larger than "
                            f"{MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB to download; register a smaller repo"
                        ])
                    chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise _unreachable(repo, type(exc).__name__) from None
    return b"".join(chunks)


@dataclass(frozen=True)
class _Root:
    """The agent root cut out of a repo archive: every regular file's bytes and mode, and its directories."""

    files: dict[str, tuple[bytes, int]]
    dirs: list[str]


def _cut_root(archive: bytes, subpath: str, *, where: str) -> _Root:
    """The files under `subpath` in a GitHub archive (whose single top folder is `<owner>-<repo>-<sha>`),
    keyed relative to `subpath`. Links and devices are dropped: a bundle never points outside itself."""
    prefix = f"{subpath}/" if subpath else ""
    files: dict[str, tuple[bytes, int]] = {}
    dirs: list[str] = []
    unpacked = kept = members = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                members += 1
                unpacked += max(member.size, 0)
                if members > MAX_MEMBERS or unpacked > MAX_UNPACKED_BYTES:
                    raise SnapshotRefused([
                        f"{where} is too large to read: more than {MAX_MEMBERS} files or "
                        f"{MAX_UNPACKED_BYTES // (1024 * 1024)} MiB unpacked"
                    ])
                _, _, inner = member.name.partition("/")
                inner = posixpath.normpath(inner) if inner else ""
                if not inner or inner == "." or inner.startswith("../") or not inner.startswith(prefix):
                    continue
                relative = inner[len(prefix):]
                if not relative:
                    continue
                if member.isdir():
                    dirs.append(relative)
                elif member.isreg():
                    kept += member.size
                    if kept > MAX_ROOT_BYTES:
                        raise SnapshotRefused([
                            f"The agent folder in {where} unpacks to more than "
                            f"{MAX_ROOT_BYTES // (1024 * 1024)} MiB; set `subpath` to the agent's own folder"
                        ])
                    handle = tar.extractfile(member)
                    files[relative] = (handle.read() if handle is not None else b"", member.mode)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SnapshotRefused([f"The archive GitHub returned for {where} could not be read ({type(exc).__name__})"]) from None
    return _Root(files=files, dirs=dirs)


def _pack(root: _Root) -> bytes:
    """A deterministic `.tar.gz` of `root`: sorted members, owner 0, normalised modes, gzip mtime 0 — the
    same commit always packs to the same bytes, so its sha256 is stable across refreshes."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(set(root.dirs)):
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name in sorted(root.files):
            body, mode = root.files[name]
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), 0o755 if mode & 0o111 else 0o644
            tar.addfile(info, io.BytesIO(body))
    packed = io.BytesIO()
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def _text(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _Source:
    """Where one snapshot is read from: the repo, the agent root, whose token, and the commit."""

    repo: forges.RepoRef
    subpath: str
    token: str
    commit: str


def _source(session: Session, *, user_id: UUID, repo_url: str, subpath: str, ref: str, retry: str) -> _Source:
    repo = parse_repo(repo_url)
    subpath = normalised_subpath(subpath)
    if not (ref or "").strip():
        raise SnapshotRefused(["`ref` is required: a branch, tag or commit"])
    if not _REF_RE.fullmatch(ref.strip()) or ".." in ref.split("/"):
        raise SnapshotRefused([f"`ref` must be a branch, tag or commit name, got {ref.strip()[:80]}"])
    token = _token(session, user_id, repo, retry=retry)
    return _Source(repo=repo, subpath=subpath, token=token, commit=resolve_commit(repo, ref.strip(), token=token))


def _snapshot_of(source: _Source) -> Snapshot:
    repo, subpath, commit = source.repo, source.subpath, source.commit
    where = f"{repo.path} at commit {short_sha(commit)}"
    root = _cut_root(fetch_archive(repo.forge, repo, commit, token=source.token), subpath, where=where)
    manifest_file = root.files.get(MANIFEST_FILENAME)
    if manifest_file is None:
        at = f"at sub-path {subpath}" if subpath else "at the repo root"
        raise SnapshotRefused([f"No {MANIFEST_FILENAME} {at} of {where}"])
    result = validate_manifest(
        _text(manifest_file[0]),
        tree=set(root.files),
        read=lambda path: _text(root.files[path][0]) if path in root.files else None,
    )
    if isinstance(result, list):
        raise SnapshotRefused([error.message for error in result])
    bundle = _pack(root)
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise SnapshotRefused([
            f"The agent folder in {where} packs to {len(bundle) // (1024 * 1024)} MiB, over the "
            f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MiB limit; set `subpath` to the agent's own folder"
        ])
    return Snapshot(commit=commit, manifest=result, bundle=bundle, sha256=hashlib.sha256(bundle).hexdigest())


def take_snapshot(
    session: Session, *, user_id: UUID, repo_url: str, subpath: str, ref: str, retry: str
) -> Snapshot:
    """Resolve, download, validate and pack one agent root as `user_id`. Writes nothing anywhere.

    `retry` closes a connection-refusal sentence with what this caller's reader can do next."""
    return _snapshot_of(
        _source(session, user_id=user_id, repo_url=repo_url, subpath=subpath, ref=ref, retry=retry)
    )


def store_bundle(agent_id: UUID, snapshot: Snapshot) -> str:
    """Put `snapshot`'s bundle in the attachment bucket and return its key. The object is immutable per
    commit, so a re-put of the same commit rewrites identical bytes."""
    where = attachment_storage.target()
    if where is None:
        raise SnapshotRefused(
            ["Agent code cannot be stored: this control plane has no object storage configured"], status=503
        )
    key = bundle_key(agent_id, snapshot.commit)
    try:
        attachment_storage.put(where, key, snapshot.bundle)
    except attachment_storage.StorageUnavailable as exc:
        log.warning("agent bundle put failed for %s: %s", agent_id, exc)
        raise SnapshotRefused(["Agent code could not be stored; try again shortly"], status=503) from None
    return key


def slug_taken(session: Session, org_id: UUID, slug: str, *, other_than: Optional[UUID] = None) -> bool:
    """Whether `org_id` already has an agent named `slug` (`uq_agents_org_slug`), besides `other_than`."""
    query = select(Agent.id).where(Agent.org_id == org_id, Agent.slug == slug)
    if other_than is not None:
        query = query.where(Agent.id != other_than)
    return session.scalars(query.limit(1)).first() is not None


def slug_taken_sentence(slug: str) -> str:
    return f"An agent named {slug} is already registered in this organization; rename `name` in {MANIFEST_FILENAME}"


def refuse_secret_env(manifest: Optional[dict], env: Optional[dict]) -> None:
    """`SnapshotRefused` (a 422) when a manifest 2 agent would carry a secret-shaped variable
    (`agents.secret_env_refusals`): refused when it is saved or refreshed to version 2, never first at launch."""
    refusals = agents.secret_env_refusals(manifest, env)
    if refusals:
        raise SnapshotRefused(refusals)


def apply_snapshot(agent: Agent, snapshot: Snapshot, key: str, *, now: datetime) -> None:
    """Write a good snapshot onto `agent`: the manifest's fields, the commit and its bundle, a cleared error."""
    manifest = snapshot.manifest
    agent.slug = manifest.name
    agent.name = manifest.display_name
    agent.display_name = manifest.display_name
    agent.description = manifest.description
    agent.agent_type = manifest.type
    agent.language = manifest.language
    agent.entrypoint = manifest.entrypoint
    agent.version = manifest.version
    agent.maintainer = manifest.maintainer
    agent.personas = list(manifest.personas)
    agent.manifest = manifest.to_json()
    agent.resolved_commit = snapshot.commit
    agent.bundle_key = key
    agent.bundle_sha256 = snapshot.sha256
    agent.bundle_bytes = len(snapshot.bundle)
    agent.resolve_error = None
    agent.resolved_at = now


#: The `retry` clause a refresh's connection refusal ends with: nobody is waiting on the sweep.
REFRESH_RETRY = "then press Refresh on the agent"


def refresh(session: Session, agent: Agent, *, now: datetime) -> Optional[SnapshotRefused]:
    """Re-resolve `agent.ref` as its `repo_access_user_id` and re-snapshot when the commit moved.

    None on success. On failure the refusal is returned AND its sentences recorded as `resolve_error`, and the
    last good `resolved_commit`/bundle is kept (DD4): a forge outage never takes away code already held. An
    unexpected fault is recorded too, so the sweep's 15-minute throttle holds for it.

    The forge is read WITHOUT a row lock; the row is then locked and the result applied only if its ref,
    sub-path and reader are still the ones read — an edit made meanwhile wins, and nothing is written. The
    caller commits."""
    ref, subpath, reader_id = agent.ref, agent.subpath, agent.repo_access_user_id
    result: Optional[SnapshotRefused] = None
    snapshot: Optional[Snapshot] = None
    key = ""
    try:
        reader = session.scalars(select(AdminUser).where(AdminUser.id == reader_id)).one_or_none()
        if reader is None or reader.disabled_at is not None:
            # An offboarded person's GitHub connection is never used again on the org's behalf.
            raise SnapshotRefused([
                f"The person whose GitHub connection reads {agent.display_name} is no longer active; edit the "
                f"agent's ref to read it as yourself"
            ])
        source = _source(session, user_id=reader_id, repo_url=agent.repo_url, subpath=subpath, ref=ref,
                         retry=REFRESH_RETRY)
        if source.commit != agent.resolved_commit or not agent.bundle_key:
            snapshot = _snapshot_of(source)
            if slug_taken(session, agent.org_id, snapshot.manifest.name, other_than=agent.id):
                raise SnapshotRefused([slug_taken_sentence(snapshot.manifest.name)])
            refuse_secret_env(snapshot.manifest.to_json(), agent.env)
            key = store_bundle(agent.id, snapshot)
        elif (why := agents.unsupported(agent.manifest)) is not None:
            # An SDK v1 row whose ref has not moved: kept, and the reason it cannot run recorded (S4).
            raise SnapshotRefused([why])
    except SnapshotRefused as exc:
        result = exc
    except Exception:  # noqa: BLE001 — recorded, so it is visible and throttled, never a silent retry loop
        log.exception("agent refresh: %s failed unexpectedly", agent.id)
        result = SnapshotRefused(["Refreshing this agent failed unexpectedly; it will be retried"], status=500)
    session.refresh(agent, with_for_update=True)
    if (agent.ref, agent.subpath, agent.repo_access_user_id) != (ref, subpath, reader_id):
        return None
    if result is not None:
        agent.resolve_error, agent.resolved_at = "; ".join(result.errors), now
        return result
    if snapshot is None:
        agent.resolve_error, agent.resolved_at = None, now
    else:
        apply_snapshot(agent, snapshot, key, now=now)
    return None


#: How often the sweep re-resolves one branch-ref agent (§4.2), and how many it takes per pass, so a burst of
#: moved refs spreads over passes instead of stalling the task-pipeline cron.
REFRESH_INTERVAL = timedelta(minutes=15)
REFRESH_BATCH = 20


def sweep_refresh(*, now: Optional[datetime] = None) -> int:
    """The task-pipeline cron's pass over moving refs: every ACTIVE ORG agent whose `ref` is not a fixed
    commit and that was not resolved in the last `REFRESH_INTERVAL`. One session and one commit per agent,
    so one agent's failure never rolls back another's. Returns how many were refreshed or marked failed.

    Catalog connectors are pinned to an exact commit (`routes/agents.py::publish_catalog_agent`), so they
    never move and are skipped by construction as well as by the `is_catalog` filter. ACP registry installs
    (`manifest.kind = acp_registry`) have no repo to re-read and are skipped too."""
    from app.db import SessionLocal

    now = now or datetime.now(timezone.utc)
    due_before = now - REFRESH_INTERVAL
    with SessionLocal() as session:
        ids = session.scalars(
            select(Agent.id).where(
                Agent.status == STATUS_ACTIVE,
                Agent.is_catalog.is_(False),
                # A registry install is pinned at its version (`app/acp_registry.py`); a NULL manifest still moves.
                Agent.manifest["kind"].astext.is_distinct_from(acp_registry.MANIFEST_KIND),
                ~Agent.ref.op("~")("^[0-9a-f]{40}$"),
                or_(Agent.resolved_at.is_(None), Agent.resolved_at < due_before),
            ).order_by(Agent.resolved_at.asc().nulls_first()).limit(REFRESH_BATCH)
        ).all()
    done = 0
    for agent_id in ids:
        try:
            with SessionLocal() as session:
                agent = session.scalars(select(Agent).where(Agent.id == agent_id)).one_or_none()
                if agent is None:
                    continue
                refused = refresh(session, agent, now=now)
                session.commit()
                if refused is not None:
                    log.info("agent refresh: %s kept commit %s: %s", agent_id, short_sha(agent.resolved_commit),
                             refused.errors)
                done += 1
        except Exception:  # noqa: BLE001 — one agent's fault never stops the pass
            log.exception("agent refresh: %s raised; retried next pass", agent_id)
    return done
