"""Per-pass sensing: `ReconcileContext`'s snapshots and the worktree git/stack/tmux/session probes."""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Callable

import control_plane_api
import edition
import host_platform
import console as console_mod  # sibling module import, mirroring commands.py's `import console as ...`
import runner_env
import session_user
from worktree_mechanics import env_file, processes
from env_helpers import compose_project_name, now_iso
from reconcile import vocab, accounts_probe
from reconcile.vocab import CONSOLE_ATTACHED, CONSOLE_NOT_ATTACHED, CONSOLE_PROBE_UNAVAILABLE, GIT_FAST_TIMEOUT
from reconcile.accounts_probe import AgentsProbeCache, AgentsProbeFn

if TYPE_CHECKING:
    import runner_registry

log = logging.getLogger(__name__)


class ReconcileContext:
    """Per-pass cache of the docker/agents/tmux snapshots — one probe each, reused across every slot.
    Probes are injectable for tests.

    Every snapshot carries an `ok` flag, from which the encoder derives the record's nulls (R1)."""

    def __init__(
        self,
        *,
        config_dirs: list[str] | None = None,
        agents_probe: AgentsProbeFn | None = None,
        console_live_probe: Callable[[str, str], bool] | None = None,
        node_id: str | None = None,
        agents_cache: "AgentsProbeCache | None" = None,
        auto_ports: "tuple[int, int] | None" = None,
        coverage_known: bool | None = None,
    ) -> None:
        self._config_dirs = config_dirs if config_dirs is not None else accounts_probe._agent_config_dirs()
        # Whether this process knows which accounts exist (`_roster_coverage_known`); None ⇒ ask it.
        self._coverage_known = coverage_known
        self._agents_probe = agents_probe or accounts_probe._default_agents_probe
        # Wired ⇒ `agents()` serves from the cross-tick cache and forks nothing; unwired ⇒ one probe per dir
        # (one-shot).
        self._agents_cache = agents_cache
        # (project, number) -> bool, off the live ConsoleBridge (a console has no agent process). Unwired is
        # `probe_unavailable`, never `not_attached` (R1).
        self._console_live_probe = console_live_probe
        self._node_id = node_id
        # The node-assigned port range from the tick's `grid`; None ⇒ `worktree_mechanics`' defaults, applied there only.
        self._auto_ports = auto_ports
        # Gate-0 answers per slot path from one `scan_pool_cwds` walk. None ⇒ `cwd_scan` scans the one tree
        # asked about (an optimisation, not a precondition).
        self._cwd_scans: dict[str, "processes.CwdScan"] | None = None
        self._agents_done = False
        self._agents_ok = False
        self._agents_json: list[dict] = []
        self._agent_cwds: list[str] = []
        # The per-account reading behind `slot_agents`: the union over the accounts that were read, and the
        # accounts that were not. `_agents_blind` None = no per-account reading (every slot blind).
        self._agents_partial: tuple[list[dict], list[str]] = ([], [])
        self._agents_blind: frozenset[str] | None = None
        self._docker_done = False
        self._docker_ok = False
        self._docker_names: list[str] = []
        self._tmux_done = False
        self._tmux_ok = False
        # `(pane cwd, session:window_index, window_name)`: the name tells agent windows from `|term`/`|web`
        # tabs.
        self._tmux_panes: list[tuple[str, str, str]] = []
        # The CP's worktrees projection, fetched lazily; after `_worktrees_done`, None means the read failed.
        self._worktrees_done = False
        self._worktree_records: list[dict] | None = None
        # One lock per memo, so a concurrent caller (`execute_intents` runs chains at once) waits for the first
        # answer instead of reading the memo stamped before its first await. Created unbound (3.10+).
        self._memo_locks = {name: asyncio.Lock() for name in ("agents", "docker", "tmux", "worktrees")}

    @property
    def config_dirs(self) -> list[str]:
        """The account config_dirs the agents snapshot covers this pass — published as `probed_config_dirs`
        (R1). A slot whose account falls outside this set has unobserved liveness, the one failure no `ok`
        flag catches.

        Every known config_dir, always: the cache changes freshness, never membership (leased floor + one
        tick, full-scan floor, or one tick after a dirty mark). Shrinking it to "what we re-probed" would
        strip liveness observation from every unleased account's slots."""
        return list(self._config_dirs)

    @property
    def agents_cache(self) -> "AgentsProbeCache | None":
        return self._agents_cache

    def note_leased_account(self, config_dir: str) -> None:
        """Tell the cache this account is working a leased slot, for the next tick. No-op without a cache."""
        if self._agents_cache is not None:
            self._agents_cache.note_leased(config_dir)

    @property
    def node_id(self) -> str | None:
        return self._node_id

    @property
    def auto_ports(self) -> "tuple[int, int] | None":
        """The `(floor, ceiling)` for this pass's port walk, or None for the module defaults. On the
        context: one fact per pass (a node belongs to one org), not an argument threaded through every
        executor."""
        return self._auto_ports

    def adopt_auto_ports(self, auto_ports: "tuple[int, int] | None") -> None:
        """Bind this pass's port range from the tick, after construction (the two callers build and tick
        in opposite orders). None is a no-op, not a clear."""
        if auto_ports is not None:
            self._auto_ports = auto_ports

    def adopt_cwd_scans(self, scans: "dict[str, processes.CwdScan]") -> None:
        """Bind this pass's pool-wide gate-0 answers, replacing (not merging) any earlier set: one instant."""
        self._cwd_scans = dict(scans)

    def note_cwd_scan(self, wt: str, scan: "processes.CwdScan") -> None:
        """Overwrite one tree's answer — the orphan reap's re-scan of the tree it just emptied."""
        if self._cwd_scans is not None:
            self._cwd_scans[wt] = scan

    async def cwd_scan(self, wt: str) -> "processes.CwdScan":
        """Gate 0 for one tree: the pre-scanned answer, or a scan of just that tree (same function, one
        path). `collect=True` to match the pre-scan; the caller decides what to report."""
        scans = self._cwd_scans
        if scans is not None:
            scan = scans.get(wt)
            if scan is not None:
                return scan
        return await asyncio.to_thread(
            functools.partial(processes.scan_cwd_processes, wt, collect=True),
        )

    def console_state(self, project: str, number: str) -> str:
        """Tri-state console observation (R1): `attached` / `not_attached` / `probe_unavailable`."""
        if self._console_live_probe is None:
            return CONSOLE_PROBE_UNAVAILABLE
        try:
            return CONSOLE_ATTACHED if self._console_live_probe(project, number) else CONSOLE_NOT_ATTACHED
        except Exception as exc:  # noqa: BLE001 — a raising bridge is an unavailable probe, not "nobody home"
            log.warning("reconcile: console probe failed for %s #%s: %s", project, number, exc)
            return CONSOLE_PROBE_UNAVAILABLE

    def invalidate_agents(self) -> None:
        """Drop this pass's memoised session snapshot so the next `agents()` re-reads the cache.

        The execute arm latches the memo (`observe_slot` → `agents()`) while the refresh overlaps it, which
        would publish a pre-refresh (on a cold cache, `ok=False`) snapshot. A no-op without a cache: there
        the next `agents()` would re-probe every dir, doubling the fan-out."""
        if self._agents_cache is None:
            return
        self._agents_done = False

    async def agents(self) -> tuple[bool, list[dict], list[str]]:
        """`_agents_once` under its memo lock (`_memo_locks`)."""
        async with self._memo_locks["agents"]:
            return await self._agents_once()

    async def _agents_once(self) -> tuple[bool, list[dict], list[str]]:
        """The session snapshot for this pass: ok only if every config dir was observed and returned a JSON
        array; union and dedup by sessionId, tagging each with its account.

        With a cache wired, reads it and forks nothing; without, probes every dir once. Answers
        affirmatively only when this process knows which accounts exist (`_roster_coverage_known`). Also
        memoises the per-account reading `slot_agents` serves."""
        if self._agents_done:
            return self._agents_ok, self._agents_json, self._agent_cwds
        self._agents_done = True
        # Blind everywhere until a per-account reading is taken below.
        self._agents_blind = None
        dirs = self._config_dirs
        if not dirs:
            self._agents_ok = False
            return self._agents_ok, self._agents_json, self._agent_cwds

        # Coverage before content: `dirs` always carries the default CLI's own config dir, so an unknown
        # roster still arrives non-empty and would probe clean. Answer as `not dirs` does, so the CP holds the slot.
        # (`refresh_agents_cache` does not consult coverage; its fan-out is still paid.)
        known = self._coverage_known
        if known is None:
            known = accounts_probe._roster_coverage_known()
        accounts_probe._log_agents_coverage(known, dirs)
        if not known:
            self._agents_ok = False
            return self._agents_ok, self._agents_json, self._agent_cwds

        if self._agents_cache is not None:
            sessions, cwds, blind = self._agents_cache.partial(dirs)
        else:
            sessions, cwds, blind = _merge_probe_results(
                dirs, await accounts_probe._gather_agents_probes(self._agents_probe, dirs))
        self._agents_partial = (sessions, cwds)
        self._agents_blind = blind
        self._agents_ok = not blind
        self._agents_json = sessions if not blind else []
        self._agent_cwds = cwds if not blind else []
        return self._agents_ok, self._agents_json, self._agent_cwds

    def slot_agents(self, wt: str, account: str | None) -> tuple[bool, list[dict], list[str]]:
        """`agents()`'s answer for ONE slot, after `agents()` ran this pass: per account, not all-or-nothing.

        With every account read it is `agents()`'s. With some blind (a slow or failing probe) the slot is blind
        when `account` — the account this node launched the slot's runner on — is one of them; otherwise it reads
        ok when a read account lists a session in `wt` (positive evidence from a list that was read), or when
        `account` was read and lists none there.
        Otherwise the slot is blind, as every slot was before. The node-wide readers (reaps, erase, deliver)
        keep `agents()`'s all-or-nothing answer."""
        if self._agents_ok or self._agents_blind is None:
            return self._agents_ok, self._agents_json, self._agent_cwds
        sessions, cwds = self._agents_partial
        norm = accounts_probe._registry_norm
        mine = norm(account) if account else ""
        if mine and mine in {norm(d) for d in self._agents_blind}:
            # The runner's own account was not read: another account's session in the tree (a predecessor, an
            # operator's CLI) is not this runner, and reading it would rule the launch DEAD on a mismatch.
            return False, [], []
        if live_session_in(wt, cwds):
            return True, sessions, cwds
        if mine and mine in {norm(d) for d in self._config_dirs}:
            return True, sessions, cwds
        return False, [], []

    async def docker(self) -> tuple[bool, list[str]]:
        """`_docker_once` under its memo lock (`_memo_locks`)."""
        async with self._memo_locks["docker"]:
            return await self._docker_once()

    async def _docker_once(self) -> tuple[bool, list[str]]:
        """One `docker ps --format '{{.Names}}'` per pass.

        As the session user wherever the switch is live: this uid cannot see docker (`stack_ops._dc_step`),
        and an in-process probe would publish `stack_up: null` fleet-wide. In-process on a single-uid host."""
        if self._docker_done:
            return self._docker_ok, self._docker_names
        # Stamped before the first await; `docker()`'s lock keeps a concurrent caller from reading it early.
        self._docker_done = True
        argv = ["docker", "ps", "--format", "{{.Names}}"]
        if session_user.can_run_as_session_user():
            # Gated on the switch, not `_have("docker")` (this process's PATH, not `delegated_path()`); a
            # missing binary returns 127. `run_as_session_user` does not bound its own 30s tmux spawn.
            try:
                # The node-managed engine's endpoint where the CLI default is not it (macOS); `{}` elsewhere.
                proc = await asyncio.to_thread(
                    session_user.run_as_session_user, argv, env=runner_env.docker_env() or None,
                    timeout=GIT_FAST_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._docker_ok = False
                return self._docker_ok, self._docker_names
            except Exception as exc:  # noqa: BLE001 — RuntimeError (switch vanished mid-flight), OSError
                # "Could not switch" is not "no containers" (that would publish `stack_up: false` for running
                # stacks, R1). Broad catch: a truncated spool rc raises `ValueError`.
                log.warning(
                    "docker sensor: delegation to %r failed (%s) — publishing docker_ok=false "
                    "(unknown), NOT an empty container list",
                    session_user.session_user(), type(exc).__name__)
                self._docker_ok = False
                return self._docker_ok, self._docker_names
            if proc.returncode == 0:
                self._docker_ok = True
                self._docker_names = [ln for ln in (proc.stdout or "").splitlines() if ln]
            else:
                self._docker_ok = False
            return self._docker_ok, self._docker_names
        if session_user.switch_needed():
            log.warning(
                "docker sensor runs IN-PROCESS as this daemon's own uid: a session user exists but the "
                "switch is unavailable (socket/spool missing) — `docker ps` will fail on socket EACCES "
                "unless this uid has docker access, and every slot's stack_up publishes as null")
        if not vocab._have("docker"):
            self._docker_ok = False
            return self._docker_ok, self._docker_names
        rc, out, _err = await vocab._sh(argv, timeout=GIT_FAST_TIMEOUT)
        if rc == 0:
            self._docker_ok = True
            self._docker_names = [ln for ln in out.splitlines() if ln]
        else:
            self._docker_ok = False
        return self._docker_ok, self._docker_names

    async def tmux(self) -> tuple[bool, list[tuple[str, str]]]:
        """`_tmux_once` under its memo lock (`_memo_locks`)."""
        async with self._memo_locks["tmux"]:
            return await self._tmux_once()

    async def _tmux_once(self) -> tuple[bool, list[tuple[str, str]]]:
        """One `tmux list-panes -a` per socket per pass → (ok, [(pane_path, target, name)]). `ok` False when
        tmux is absent, a socket unreachable, or a listing failed — distinct from a real "no panes".

        Every socket or nothing (`session_user.manage_sockets`): an empty listing from a server the runner
        is not on reads as "window gone", and the CP frees on confident absence. An absent socket is a
        failure and is never probed (a tmux client with no server starts one as this process)."""
        if self._tmux_done:
            return self._tmux_ok, self._tmux_panes
        self._tmux_done = True
        if not vocab._have("tmux"):
            return self._tmux_ok, self._tmux_panes
        panes: list[tuple[str, str]] = []
        for socket in session_user.manage_sockets():
            if socket and not os.path.exists(socket):
                # A local node starts its tmux server with its first session, so an idle one has no socket.
                log.log(
                    logging.DEBUG if edition.IS_CE else logging.WARNING,
                    "tmux sensor: no socket at %s — publishing tmux_ok=false (unknown), NOT 'no windows'",
                    socket,
                )
                return self._tmux_ok, self._tmux_panes
            rc, out, _err = await vocab._sh(
                session_user.tmux_cmd(
                    socket, "list-panes", "-a", "-F",
                    "#{pane_current_path}\t#{session_name}:#{window_index}\t#{window_name}"),
                timeout=GIT_FAST_TIMEOUT,
            )
            if rc != 0:
                return self._tmux_ok, self._tmux_panes
            for ln in out.splitlines():
                if "\t" not in ln:
                    continue
                # An unnamed window is `""`: its only consumer uses the name to skip, so unreadable means not
                # skipped (conservative).
                path, target, name = (ln.split("\t", 2) + ["", ""])[:3]
                if console_mod.is_ephemeral_view_session(target):
                    # A `_view_*` grouped session duplicates every window; an ephemeral target must never
                    # publish as the slot's. Filtered in the parse, never by narrowing the query.
                    continue
                if path and target:
                    panes.append((path, target, name))
        self._tmux_ok = True
        self._tmux_panes = panes
        return self._tmux_ok, self._tmux_panes

    async def tmux_panes(self) -> list[tuple[str, str, str]]:
        """The pane list alone, for callers that already know the probe succeeded."""
        _ok, panes = await self.tmux()
        return panes

    async def worktree_records(self) -> list[dict] | None:
        """`_worktree_records_once` under its memo lock (`_memo_locks`)."""
        async with self._memo_locks["worktrees"]:
            return await self._worktree_records_once()

    async def _worktree_records_once(self) -> list[dict] | None:
        """The CP's worktrees projection for this node (`GET /api/agent/worktrees`), one read per pass.

        None means the read failed, `[]` that this node holds no slots; every caller treats None as
        fail-closed and refuses — else it reads as "no live session"/"nobody owns this tree" for every pending
        intent.

        Lazy on purpose: awaited only from inside the executors that need it, so a pass with nothing to
        kill or deliver makes no GET. Do not hoist this call into the pass. Memoised: N intents cost one
        GET."""
        if self._worktrees_done:
            return self._worktree_records
        # Stamped before the await, as `docker()`; `worktree_records()`'s lock keeps a concurrent caller out.
        self._worktrees_done = True
        records = await asyncio.to_thread(control_plane_api.agent_api_pool_worktrees, self._node_id or "")
        if records is None:
            # Transport failure stays None, never `[]`.
            return self._worktree_records
        self._worktree_records = [r for r in records if isinstance(r, dict)]
        return self._worktree_records


# ── Ground-truth git/env/tmux probes (port of the per-slot helpers) ───────────
async def probe_wt_branch(wt: str) -> str | None:
    """The checked-out branch, `"DETACHED"` for an observed detached HEAD, or None on probe failure (R1).
    `symbolic-ref --quiet` exits 1 silently for detached HEAD; 124/127/128 are probe failures."""
    rc, out, _err = await vocab._sh(["git", "-C", wt, "symbolic-ref", "--quiet", "--short", "HEAD"], GIT_FAST_TIMEOUT)
    br = out.strip()
    if rc == 0 and br:
        return br
    if rc == 1:
        return "DETACHED"
    return None


async def branch_merged(wt: str, base: str) -> bool | None:
    """Did HEAD merge into `origin/<base>`? None on probe failure. git owns "what merged"; the DB owns
    phase. `merge-base --is-ancestor`: 0 yes, 1 no, else error."""
    rc, _out, _err = await vocab._sh(
        ["git", "-C", wt, "merge-base", "--is-ancestor", "HEAD", f"origin/{base}"], GIT_FAST_TIMEOUT
    )
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


async def wt_ahead_behind(wt: str, base: str) -> tuple[int, int] | None:
    """(behind, ahead), or None on error. `rev-list --left-right --count origin/base...HEAD` gives
    left=behind, right=ahead."""
    rc, out, _err = await vocab._sh(
        ["git", "-C", wt, "rev-list", "--left-right", "--count", f"origin/{base}...HEAD"], GIT_FAST_TIMEOUT
    )
    if rc != 0:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# Unpushed vs unmerged: `ahead` counts pushed commits awaiting a PR; freeing a slot needs "would anything be
# unreachable", i.e. absent from every `origin/*` ref.


async def wt_unpushed(wt: str) -> int | None:
    """How many commits at HEAD are absent from every `origin/*` ref, or None on probe failure (R1).

    The CP reads it as unpushed work, so it counts the same commits a push would carry. On `HEAD` so a detached checkout answers. A branch pushed from elsewhere over-reports, holding
    the slot — the safe direction."""
    rc, out, _err = await vocab._sh(
        ["git", "-C", wt, "rev-list", "--count", "HEAD", "--not", "--remotes=origin"], GIT_FAST_TIMEOUT
    )
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


async def wt_dirty(wt: str) -> bool | None:
    """Uncommitted tracked or untracked changes, or None on probe failure (R1). `git diff --quiet`: 0 clean,
    1 differences, else failure.

    Untracked files count (`ls-files --others --exclude-standard`): `app/slot_state.py`'s "uncommitted work
    never reaches `free`" reads this value. Do not narrow it back."""
    rc1, _o, _e = await vocab._sh(["git", "-C", wt, "diff", "--quiet"], GIT_FAST_TIMEOUT)
    if rc1 == 1:
        return True
    if rc1 != 0:
        return None
    rc2, _o, _e = await vocab._sh(["git", "-C", wt, "diff", "--cached", "--quiet"], GIT_FAST_TIMEOUT)
    if rc2 == 1:
        return True
    if rc2 != 0:
        return None
    rc3, out3, _e = await vocab._sh(
        ["git", "-C", wt, "ls-files", "--others", "--exclude-standard"], GIT_FAST_TIMEOUT
    )
    if rc3 != 0:
        return None
    if out3.strip():
        return True
    return False


async def wt_head_and_last_commit(wt: str) -> tuple[str, str]:
    """(head sha, last-commit time as `...Z`) from one `git log -1`. ("", "") when unreadable."""
    rc, out, _err = await vocab._sh(["git", "-C", wt, "log", "-1", "--format=%H%x09%cI"], GIT_FAST_TIMEOUT)
    if rc != 0 or not out.strip():
        return "", ""
    sha, _, committed = out.strip().partition("\t")
    return sha, vocab._to_utc_iso(committed.strip())


async def wt_index_mtime(wt: str) -> str:
    """The git index's mtime as `...Z`, or "" — the second raw input of `last_active`. Folding the two is
    the CP's decision; the node publishes both as ages."""
    rc, out, _err = await vocab._sh(["git", "-C", wt, "rev-parse", "--git-path", "index"], GIT_FAST_TIMEOUT)
    if rc != 0 or not out.strip():
        return ""
    idx = out.strip()
    idx_path = idx if os.path.isabs(idx) else os.path.join(wt, idx)
    if not os.path.isfile(idx_path):
        return ""
    return now_iso(os.path.getmtime(idx_path))


def cpn_of(wt: str, fallback: str = "") -> str:
    """.env COMPOSE_PROJECT_NAME (last occurrence wins), else `fallback`, else the dir basename.

    `fallback` should be the caller's `<project>`/`<project>-N`: under the `{main,N}` layout the basename
    is a bare `N` and would collide across projects. Only reached for a slot predating
    `apply_stack_identity`."""
    try:
        text = env_file.env_text(os.path.join(wt, ".env"))
    except OSError:
        text = ""
    return compose_project_name(text) or fallback or os.path.basename(wt)


def stack_running(wt: str, docker_names: list[str], fallback: str = "") -> bool:
    return f"{cpn_of(wt, fallback)}-postgres-1" in docker_names


def _merge_probe_results(dirs: list[str], results: list[tuple[int, str]]
                         ) -> tuple[list[dict], list[str], frozenset[str]]:
    """`AgentsProbeCache.partial`'s shape from one fresh probe per dir (`results` in `dirs` order): the union
    over the dirs whose stdout is a JSON array, and the rest as blind."""
    merged: list[dict] = []
    seen_sids: set = set()
    blind: set[str] = set()
    for account, (_rc, out) in zip(dirs, results, strict=False):
        try:
            arr = json.loads(out)
        except ValueError:
            arr = None
        if not isinstance(arr, list):
            blind.add(account)
            continue
        for sess in arr:
            tagged = {**sess, "_account": account}
            sid = tagged.get("sessionId")
            if sid in seen_sids:
                continue
            seen_sids.add(sid)
            merged.append(tagged)
    return merged, [s.get("cwd") for s in merged if s.get("cwd")], frozenset(blind)


def live_session_in(wt: str, agent_cwds: list[str]) -> bool:
    for c in agent_cwds:
        if not c:
            continue
        if host_platform.is_under(c, wt):
            return True
    return False


def wt_live_session_id(wt: str, agents_json: list[dict]) -> str:
    for sess in agents_json:
        cwd = sess.get("cwd") or ""
        if host_platform.is_under(cwd, wt):
            return sess.get("sessionId") or ""
    return ""


# Re-exported: `fleet_dispatch` calls `context.proc_started_at` by name, while the single home sits in the
# lower module so both ends of the reap's identity check share it.
proc_started_at = processes.proc_started_at


def _session_metas_for(wt: str, agents_json: list[dict]) -> list[dict]:
    """Every agent whose cwd matches the worktree → status/id/account/started/pid, plus `proc_started`, the
    kernel's start instant for that pid. In probe order.

    `started` is the agent's self-report; only `(pid, proc_started)` is an identity the CP can re-verify.
    All of them, not the first: a reap naming one of two runners in a tree would leave the slot occupied
    forever. `_session_meta_for` is the observation's single-session view."""
    out: list[dict] = []
    for sess in agents_json:
        cwd = sess.get("cwd") or ""
        if not host_platform.is_under(cwd, wt):
            continue
        started = ""
        if sess.get("startedAt"):
            started = now_iso(sess["startedAt"] / 1000)
        pid = sess.get("pid")
        pid = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        out.append({
            "status": sess.get("status") or "",
            "id": sess.get("sessionId") or "",
            "account": sess.get("_account") or "",
            "started": started,
            "pid": pid,
            "proc_started": proc_started_at(pid) if pid is not None else "",
        })
    return out


# `session_status` for a pid-sighted runner: running, busy-or-idle unknown (the session list says which).
VENDOR_SESSION_STATUS = "running"


def _vendor_runner_meta(wt: str) -> tuple[dict, bool] | None:
    """`(record, is_acp)`: the observation's session record for a runner this node launched into `wt`, sighted
    by its pane, and whether that pane is `jaah-acp-host` — or None (none recorded, pane gone, a default-CLI
    runner — another provider's on that CLI included — or an unreadable start time).

    `pid`/`proc_started` are the pane's, exactly what the launch ack sent, so the CP's identity test
    (`runner_liveness._verdict_from_row`) matches. A default-CLI run is excluded: its session list is
    authoritative, and a pane outliving its CLI must still read dead. An ACP run is not: the host is the
    pane root and lives exactly as long as the attempt, whatever engine it drives."""
    import acp_runtime  # noqa: PLC0415 — deferred, as the module's other provider reads
    import agent_providers  # noqa: PLC0415 — deferred, as the module's other two provider reads
    import runner_registry  # noqa: PLC0415 — deferred: a stdlib leaf read only on this path

    ident = runner_registry.live_in_worktree(wt)
    if ident is None or not ident.provider:
        return None
    is_acp = ident.runtime == acp_runtime.RUNTIME_ACP
    if not is_acp and agent_providers.account_layer(ident.provider) == "omc":
        return None
    started = proc_started_at(ident.pane_pid)
    if not started:
        return None
    return ({"status": VENDOR_SESSION_STATUS, "id": ident.session_id, "account": "", "started": "",
             "pid": ident.pane_pid, "proc_started": started}, is_acp)


def launched_pane(wt: str) -> tuple[runner_registry.RunnerIdentity | None, bool]:
    """The runner pane this node launched into `wt`, while it is still the recorded process, for ANY provider —
    or None — and whether that None is definite (`runner_registry.live_in_worktree_certain`). Read once per slot
    by `observe_slot`: its account picks the slot's session list (`ReconcileContext.slot_agents`), its pid is
    the identity a blind list still ships, and a definite None is `probes.launched_pane: false`."""
    import runner_registry  # noqa: PLC0415 — deferred, as `_vendor_runner_meta`

    return runner_registry.live_in_worktree_certain(wt)


def launched_pane_identity(ident: runner_registry.RunnerIdentity | None) -> tuple[int, str] | None:
    """`(pid, proc_started)` of a `launched_pane` answer, or None. Exactly the launch ack's pair
    (`fleet_dispatch` acks the pane pid, and every `exec` in `launch.sh` keeps it), so it is an identity, not a
    guess. For `observe_slot` when the session list could not be read: that blindness is not "no runner"."""
    if ident is None:
        return None
    started = proc_started_at(ident.pane_pid)
    return (ident.pane_pid, started) if started else None


def _session_meta_for(wt: str, agents_json: list[dict]) -> dict:
    """The first agent registered against the worktree (the observation's one-session columns), or the
    all-empty record."""
    metas = _session_metas_for(wt, agents_json)
    return metas[0] if metas else {
        "status": "", "id": "", "account": "", "started": "", "pid": None, "proc_started": ""}


async def wt_tmux_target(wt: str, ctx: ReconcileContext) -> str:
    """The `session:window_index` of this worktree's pane from the per-pass snapshot — exact cwd wins, else
    first prefix match. A sensor only; ambiguous across servers (both host a session named after this
    node), so every kill site re-resolves per socket."""
    panes = await ctx.tmux_panes()
    if not panes:
        return ""
    prefix = ""
    wt_key = host_platform.path_key(wt)
    for path, target, _name in panes:
        if host_platform.path_key(path) == wt_key:
            return target
        if not prefix and host_platform.is_under(path, wt):
            prefix = target
    return prefix


async def wt_tmux_targets(wt: str, ctx: ReconcileContext) -> tuple[str, ...]:
    """Every distinct `session:window_index` with a pane at or under `wt`, from the per-pass snapshot.

    For a kill, not a sensor: `kill_gates`' `expect_cwd` fence cannot tell two windows in one tree apart,
    so a caller refuses an ambiguous tree instead of picking. Deduplicated (one entry per window)."""
    import worktree_ops  # noqa: PLC0415 — deferred: only this one caller needs the suffix list

    panes = await ctx.tmux_panes()
    seen: dict[str, None] = {}
    for path, target, name in panes:
        if not host_platform.is_under(path or "", wt):
            continue
        # A console tab is not ambiguity: `console.py`'s persistent `|term`/`|web` windows live in the slot's
        # tree unreaped, so counting them would refuse every inspected slot. Discriminated by name, from
        # `worktree_ops._NON_AGENT_WINDOW_SUFFIXES` like the other three sites.
        if name and name.endswith(worktree_ops._NON_AGENT_WINDOW_SUFFIXES):
            continue
        seen.setdefault(target, None)
    return tuple(seen)


async def fresh_window_id_for(wt: str) -> str:
    """The stable `#{window_id}` of the live window whose pane cwd is `wt` or under it, via a live
    `tmux list-panes -a`. Empty when nothing matches.

    A kill-target resolver: `renumber-windows on` shifts indices when a window dies, so a persisted
    `session:window_index` can name another window. First server with a match wins (ids are per-server); an
    absent socket is skipped, never probed (that would start a server as this process)."""
    for socket in session_user.manage_sockets():
        if socket and not os.path.exists(socket):
            continue
        rc, out, _err = await vocab._sh(
            session_user.tmux_cmd(socket, "list-panes", "-a", "-F",
                                  "#{pane_current_path}\t#{window_id}"),
            GIT_FAST_TIMEOUT,
        )
        if rc != 0:
            continue
        prefix = ""
        for line in out.splitlines():
            path, _, wid = line.partition("\t")
            if not wid:
                continue
            if host_platform.path_key(path) == host_platform.path_key(wt):
                return wid
            if not prefix and host_platform.is_under(path, wt):
                prefix = wid
        if prefix:
            return prefix
    return ""


# Two reconcile drivers share one loop (the idle-tick projection and the `reconcile` bus command) and one
# lock; a caller finding it held skips rather than waits. Concurrent passes would duplicate probes and could
# land a staler observation after a fresher one.
def new_reconcile_lock() -> asyncio.Lock:
    """A fresh reconcile-serialization lock, built lazily by its first user (binding its loop) and shared
    across both drivers by `main._serve`. Without injection a component serializes only its own reconciles."""
    return asyncio.Lock()


class ReconcileLockHolder:
    """The two drivers' (`commands.CommandBridge`, `main.NodeStatePublisher`) lazy reconcile lock: each sets
    `_reconcile_lock` to the one `main._serve` injects (shared with the other driver), or None standalone."""

    _reconcile_lock: asyncio.Lock | None

    def _serialization_lock(self) -> asyncio.Lock:
        """The shared reconcile-serialization lock, created lazily on first use so it binds to the running
        event loop."""
        if self._reconcile_lock is None:
            self._reconcile_lock = new_reconcile_lock()
        return self._reconcile_lock


def _matched_record(records: list[dict], project: str, number: str) -> dict | None:
    """The projection record for one slot, matched by `(project, number)` and nothing else.

    No branch derivation: a released slot keeps its `fleet/task-<id>` checkout, so a branch match can hit a
    stale slot and report success having killed nothing."""
    import kill_gates  # noqa: PLC0415 — deferred: subprocess-heavy sibling, needed only on these verbs

    for record in records:
        if kill_gates._record_project(record) != project:
            continue
        if str(record.get("number")) != str(number):
            continue
        return record
    return None


def _acp_hub():
    """The ACP runtime hub (`acp/runtime.py`) — the session table every ACP-aware verb asks first."""
    from acp import runtime as acp_runtime  # noqa: PLC0415 — deferred: only the session-addressed verbs need it

    return acp_runtime.hub()
