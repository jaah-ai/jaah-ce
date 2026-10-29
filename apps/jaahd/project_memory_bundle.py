"""The node half of project memory's delivery: the bundle file beside an ON runner's prompts, and its refresh.

An ON dispatch's `lease_slot` payload carries `memory` (`{"project_id", "persona", "revision", "prompt_ids",
"paths"}`, ids and the task's own paths only — the CP's `lease_memory`). Only then does `fleet_dispatch.launch_runner`
call `prepare_launch`, which:

  1. fetches the bundle over `memory.bundle` (`control_plane_api.agent_api_memory_bundle`) within
     `FETCH_TIMEOUT_S`; any miss is an EMPTY bundle and the launch goes ahead;
  2. writes it as `<ref_key>.memory.json` beside `<ref_key>.sys` in `_meta` — outside the worktree, as the
     session user, at `<ref_key>.sys`'s mode;
  3. returns the five exports for `launch.sh` (`env_for`): `JAAH_PROJECT_MEMORY`, `JAAH_MEMORY_PERSONA`,
     `JAAH_MEMORY_PROMPT_IDS`, `JAAH_MEMORY_PATHS` (the SessionStart slice's path anchors) — the presence of the first is the only switch the runner's hooks read —
     and `OMC_DISABLE_TOOLS`, OMC's own switch, naming its memory tool groups: an ON session's memory is project
     memory and nothing else, so the OMC notepad, project-memory, wiki, shared-memory and state tools are not
     published to it. An OFF session (no export) keeps every OMC tool, and its charter's memory section is OMC's;
  4. (`start_node_helper`, `helper_launch_lines`) — `launch.sh` starts the memory node helper
     (`HELPER_RELPATH`) with `--background` inside the pane, so its `jaah-call` verbs and its `git fetch` are THAT task's (the daemon
     resolves both from process ancestry; a child of jaahd would be nobody's). Its cwd is the worktree, so a
     slot release's tree sweep (`reconcile.enact_reset._clear_tree`) is what kills it; its claims lapse by expiry. The slot
     is remembered, so on release `ship_orphan_spool` sends whatever the runner's hooks spooled and never
     shipped through `memory.report`, as that task;
  5. registers the file for `refresh_step`, which re-asks every `REFRESH_INTERVAL_S` with `since=<revision>`
     and rewrites it atomically: 204 keeps it, 410 empties it, a miss keeps what is there.

A payload without `memory` (OFF, or an older control plane) touches none of this: the launch is byte-identical.
The slot registry (`_slots`) is in memory only: a daemon restart forgets it, and a spool orphaned across one stays
in the cache (`<cache>/project-memory`, safe to delete) rather than being shipped.
A registration is per bundle FILE (`_meta` is per project, shared by its slots), and is dropped once the
control plane answers 410 — no open ON task left in the project — or its file is gone, so the registry is
bounded by the project's live ON runners.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass

import control_plane_api
import env_helpers
import fleet_launch
import host_platform
import session_user

log = logging.getLogger("jaahd.project_memory_bundle")

BUNDLE_SUFFIX = ".memory.json"
#: The launch's budget for the first fetch (§2.6): never worth delaying a runner past it.
FETCH_TIMEOUT_S = 2.0
REFRESH_INTERVAL_S = 60.0
#: The five exports, and nothing else, an ON launch adds (§4.4).
ENV_BUNDLE = "JAAH_PROJECT_MEMORY"
ENV_PERSONA = "JAAH_MEMORY_PERSONA"
ENV_PROMPT_IDS = "JAAH_MEMORY_PROMPT_IDS"
ENV_PATHS = "JAAH_MEMORY_PATHS"
#: OMC's tool-group switch (`src/mcp/disable-tools.ts` in the plugin) and the groups an ON session goes without.
#: A foreign, unpinned variable: if OMC renames it the tools come back, which the prompt's memory section does
#: not mention, so nothing points a runner at them. The hooks have no per-handler twin (OMC's `OMC_SKIP_HOOKS`
#: names hook EVENTS, not the notepad or project-memory handlers inside them), so an ON session may still be
#: shown a notepad an OFF session left in the worktree at SessionStart; accepted. Not `trace`: its group carries
#: `trace_summary`, which every runner's finish needs, so OMC's `session_search` (a trace tool) is served to an
#: ON session; the deleted per-runner deny list used to withhold it. The export REPLACES any inherited
#: `OMC_DISABLE_TOOLS` (the launch owns a runner's env; a host value is not expected).
ENV_OMC_DISABLE_TOOLS = "OMC_DISABLE_TOOLS"
OMC_MEMORY_TOOL_GROUPS = ("memory", "notepad", "wiki", "shared-memory", "state")
#: The node helper, relative to the hub root `launch.sh` sources its tools from (the installed bundle).
HELPER_RELPATH = ("tools", "project_memory_node.py")
#: `--drain-spool` reads a few hundred KiB at most; the session-user hop is the slow part.
DRAIN_TIMEOUT_S = 15.0
REPORT_TIMEOUT_S = 10.0


@dataclass
class _Registration:
    project_id: int
    revision: int
    mode: int


_lock = threading.Lock()
_registry: dict[str, _Registration] = {}
_refresh_in_flight = False


def bundle_path(meta: str, ref_key: str) -> str:
    return os.path.join(meta, f"{ref_key}{BUNDLE_SUFFIX}")


def empty_bundle(project_id: int) -> dict:
    """What a runner reads when there is nothing to match: the bundle shape with no units."""
    return {"v": 1, "project_id": int(project_id), "revision": 0, "units": []}


def fetch(project_id: int, since: object = None) -> tuple[int, dict]:
    """`(status, bundle)` from the control plane; raises on anything but 200/204/410."""
    return control_plane_api.agent_api_memory_bundle(project_id, since, timeout=FETCH_TIMEOUT_S)


def _write(path: str, bundle: dict, mode: int) -> bool:
    err = session_user.replace_file_as_session_user(path, json.dumps(bundle, sort_keys=True), mode=mode)
    if err:
        log.warning("project memory: could not write %s (%s) — the runner's hooks read no memory", path, err)
    return not err


def env_for(path: str, memory: dict) -> dict[str, str]:
    """The five exports, in launch order. A path carrying a comma is dropped: the list is comma-joined."""
    ids = [str(token) for token in memory.get("prompt_ids") or () if str(token)]
    paths = [str(p) for p in memory.get("paths") or () if str(p) and "," not in str(p)]
    return {
        ENV_BUNDLE: os.path.abspath(path),
        ENV_PERSONA: str(memory.get("persona") or ""),
        ENV_PROMPT_IDS: ",".join(ids),
        ENV_PATHS: ",".join(paths),
        ENV_OMC_DISABLE_TOOLS: ",".join(OMC_MEMORY_TOOL_GROUPS),
    }


def prepare_launch(memory: dict, *, meta: str, ref_key: str, mode: int, task_id: object = None,
                   worktree: str = "", session_id: str = "") -> dict[str, str]:
    """Launch steps 1–5 for an ON runner (the module docstring); returns the exports. Never raises for
    a fetch or write miss: memory is never worth a launch."""
    try:
        project_id = int(memory["project_id"])
    except (KeyError, TypeError, ValueError):
        log.warning("project memory: a malformed memory payload (%s) — launching without memory", sorted(memory))
        return {}
    path = bundle_path(meta, ref_key)
    try:
        status, bundle = fetch(project_id)
    except Exception as exc:  # noqa: BLE001 — any miss is an empty bundle; the launch goes ahead
        log.warning("project memory: bundle fetch for project %s failed (%s) — launching with an empty one",
                    project_id, exc)
        status, bundle = 0, {}
    if status != 200:
        bundle = empty_bundle(project_id)
    _write(path, bundle, mode)
    start_node_helper(task_id=task_id, worktree=worktree, session_id=session_id)
    register(path, project_id, int(bundle.get("revision") or 0), mode)
    return env_for(path, memory)


@dataclass
class _Slot:
    task_id: str
    #: Every session launched into the slot for this task (a relaunch adds one), so none is orphaned unshipped.
    session_ids: list[str]


_slots: dict[str, _Slot] = {}


def start_node_helper(*, task_id: object, worktree: str, session_id: str) -> None:
    """Launch step 4's daemon half: remember whose runner holds `worktree`, for `ship_orphan_spool`. The helper
    process itself is started by `launch.sh` (`helper_launch_lines`) — see the module docstring for why."""
    task = str(task_id or "").strip()
    if not (task.isdigit() and worktree and session_id):
        return
    key = os.path.normpath(worktree)
    with _lock:
        held = _slots.get(key)
        if held is not None and held.task_id == task:
            if session_id not in held.session_ids:
                held.session_ids.append(session_id)
        else:
            _slots[key] = _Slot(task_id=task, session_ids=[session_id])


def helper_launch_lines(memory_env: "dict[str, str] | None", *, hub_root: str, worktree: str) -> list[str]:
    """The ONE `launch.sh` line that starts the helper in the background, after the exports it reads. None when
    OFF, so an OFF `launch.sh` is unchanged. Its output goes nowhere: the helper never speaks."""
    if not memory_env:
        return []
    helper = os.path.join(hub_root, *HELPER_RELPATH)
    shq = fleet_launch.shq
    return [f"{helper_interpreter()} {shq(helper)} --background --worktree {shq(worktree)} </dev/null >/dev/null 2>&1 &"]


def helper_interpreter() -> str:
    """The interpreter word `helper_launch_lines` starts the helper with: the node's own
    (`host_platform.paths().python` — the `jaah-python` package on Linux, the bundle's elsewhere, MSYS-spelled on
    Windows, where launch.sh is a Git Bash script). A bare `python3` is whatever the pane's PATH finds first — the
    distro's 3.10 on Ubuntu 22.04, on a Mac the Xcode stub `/usr/bin/python3`, which opens an install dialog."""
    python = host_platform.paths().python
    return fleet_launch.shq(fleet_launch.msys_path(python) if host_platform.is_windows() else python)


#: What a spool event may be; `_drain` re-checks every one, because the drain's output is the session user's.
_UNIT_RE = re.compile(r"^mem-\d{1,9}$")
_DRAIN_KINDS = ("injected", "followed", "recurred")
#: The helper caps a spool read at 1 MiB; a deduped event list is far smaller. Anything bigger is refused.
DRAIN_OUTPUT_MAX = 256 * 1024
EVENTS_MAX = 500


def _drain(session_id: str, hub_root: str) -> list[dict]:
    """The released runner's unsent spool events, read and deleted AS THE SESSION USER (the spool is that uid's
    0600 file) by the helper's `--drain-spool`; `[]` on any miss.

    The output is UNTRUSTED (that uid runs every runner on the node): it is size-capped and rebuilt event by
    event — only `mem-N` ids and the three hook kinds, stamped with THIS slot's session — and the task it is sent
    as comes from `_slots`, never from it. `-I` keeps the interpreter off the session user's site-packages."""
    argv = [sys.executable or "python3", "-I", os.path.join(hub_root, *HELPER_RELPATH), "--drain-spool",
            session_id]
    env = {"JAAH_HOME": env_helpers.jaah_home()}
    if session_user.switch_needed():
        proc = session_user.run_as_session_user(argv, env=env, timeout=DRAIN_TIMEOUT_S)
    else:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=DRAIN_TIMEOUT_S,
                              env={**os.environ, **env}, stdin=subprocess.DEVNULL)
    out = proc.stdout or ""
    if proc.returncode != 0 or len(out) > DRAIN_OUTPUT_MAX:
        return []
    raw = json.loads(out or "[]")
    events, seen = [], set()
    for event in raw if isinstance(raw, list) else ():
        if not isinstance(event, dict):
            continue
        unit, kind = str(event.get("unit") or ""), event.get("kind")
        if _UNIT_RE.match(unit) and kind in _DRAIN_KINDS and (unit, kind) not in seen:
            seen.add((unit, kind))
            events.append({"unit": unit, "kind": kind, "channel": "hook", "session_id": session_id[:200]})
    return events[:EVENTS_MAX]


def ship_orphan_spool(worktree: str, *, hub_root: str) -> int:
    """Slot release: send the departed runner's unshipped spool through `memory.report` as its task, then forget
    the slot. Returns how many events were sent. Never raises."""
    with _lock:
        slot = _slots.pop(os.path.normpath(worktree), None)
    if slot is None:
        return 0
    sent = 0
    for session_id in slot.session_ids:
        try:
            events = _drain(session_id, hub_root)
            if events:
                control_plane_api.agent_api_memory_report(slot.task_id, events, timeout=REPORT_TIMEOUT_S)
                sent += len(events)
        except Exception as exc:  # noqa: BLE001 — events are best-effort; the CP dedupes a later resend anyway
            log.info("project memory: orphan spool of task %s not shipped (%s)", slot.task_id, exc)
    return sent


def ship_orphan_spool_later(worktree: str, *, hub_root: str) -> None:
    """`ship_orphan_spool` on a daemon thread: a release never waits on memory (the drain's session-user hop
    alone can take seconds). A no-op, with no thread, for a slot no ON runner held."""
    with _lock:
        if os.path.normpath(worktree) not in _slots:
            return
    threading.Thread(target=ship_orphan_spool, args=(worktree,), kwargs={"hub_root": hub_root},
                     name="project-memory-orphan-spool", daemon=True).start()


def register(path: str, project_id: int, revision: int, mode: int) -> None:
    """Refresh `path` every `REFRESH_INTERVAL_S` from now on (a relaunch of the same task replaces its entry)."""
    with _lock:
        _registry[path] = _Registration(project_id=project_id, revision=revision, mode=mode)


def registered() -> dict[str, _Registration]:
    with _lock:
        return dict(_registry)


def refresh_once() -> None:
    """One pass over every registration: re-ask with `since`, rewrite on a change; on 410 empty the file once
    and stop refreshing it."""
    for path, entry in registered().items():
        if not os.path.exists(path):
            with _lock:
                _registry.pop(path, None)
            continue
        try:
            status, bundle = fetch(entry.project_id, since=entry.revision)
        except Exception as exc:  # noqa: BLE001 — a miss keeps the file there is; the next tick retries
            log.info("project memory: refresh of %s failed (%s) — keeping the current bundle", path, exc)
            continue
        if status == 204:
            continue
        if status == 410:
            _write(path, empty_bundle(entry.project_id), entry.mode)
            with _lock:
                _registry.pop(path, None)
            continue
        if _write(path, bundle, entry.mode):
            with _lock:
                if path in _registry:
                    _registry[path].revision = int(bundle.get("revision") or 0)


async def refresh_step() -> None:
    """The 60s `project-memory` PeriodicStep: single-flight, off the loop, silent about its own failures."""
    global _refresh_in_flight
    if _refresh_in_flight or not registered():
        return
    _refresh_in_flight = True
    try:
        await asyncio.to_thread(refresh_once)
    except Exception as exc:  # noqa: BLE001 — never cost the daemon its idle loop
        log.warning("project memory: refresh pass failed (%s)", exc)
    finally:
        _refresh_in_flight = False
