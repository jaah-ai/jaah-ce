"""jaahd's CLI shim over this node's pool state + the operator's pool-mutation commands
(the daemon is the sole node-local authority and the reconciler the one writer of observed state).

HOLDS NO NODE CREDENTIAL. It runs as user `jaah`: the READ half (`worktrees`) names a verb on
the daemon's local socket, which makes the authenticated call; the MUTATE half (`release`/`retire`/
`set-title`/`create`/`reconcile`) rides the operator's own console cookie session
(`_console_session_api`, the hub `.env`'s ADMIN_*), so the CP's permission keys, arg resolver and audit
row are the gate. A node without that `.env` fails loudly — never deploy ADMIN_* to a node. The daemon's
socket carries no pool mutation (`agent_vocabulary` answers `unknown_verb`).

Failure posture: any unreachable daemon/CP, refusal, or `failed`/`rejected` reply exits NONZERO with a
stderr diagnostic, never a silent no-op. For `worktrees` an unanswerable read prints NO stdout, because
`[]` is a genuine verdict ("pool empty" / "no live session") to consumers that stop stacks and kill processes.

Usage:
    python3 apps/jaahd/ctl.py worktrees
        # prints one JSON array of this node's pool-slot records to stdout.
    python3 apps/jaahd/ctl.py release <project> <N> [--force]
        # --force reaps a slot even with a LIVE session in its worktree (otherwise refused).
        # N=0 (the main clone) is unleasable, so releasing it acts on an already-free slot.
    python3 apps/jaahd/ctl.py retire <project> <N>
        # retire a RESOLVED slot: detach the tree to a fresh base (branch ref kept), free it.
        # Live-session-guarded; does NOT set `merged`.
    python3 apps/jaahd/ctl.py set-title <project> <N> [title]   # empty/omitted title clears the override
    python3 apps/jaahd/ctl.py create <project> [--quiet]
        # materializes a slot the control plane already declares; the node never mints a slot row.
    python3 apps/jaahd/ctl.py reconcile --project <name> --number <n>
        # asks the control plane to dispatch a single-slot reconcile to this node; prints the result JSON.
    python3 apps/jaahd/ctl.py reconcile
        # --project/--number both omitted: a full-pool reconcile sweep.
        # `create` prints the worktree's path on stdout; --quiet suppresses it. Diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.parse

import main as jaahd_main  # sibling module import
# No credential here: user `jaah` has no path to NODE_TOKEN, so reads name a verb on the daemon's
# socket (reachability = group `jaah` on a 2750 dir).
import agent_socket
# Never import `control_plane_api` (the daemon's token client) here: as `jaah` it could only 401.
# Bounded so a wedged CP/daemon fails loudly; `create` (git-heavy) is the slow one. A little under the CP's
# COMMAND_TIMEOUT (180s) so the daemon replies first. `reconcile` has its own shorter default.
_REQUEST_TIMEOUT = 170.0
DEFAULT_RECONCILE_TIMEOUT = 30.0


# ── READ surface (T8): worktrees ───────────────────────────────────────────────
async def _fetch_worktrees() -> list[dict]:
    """This node's whole pool roster, read from the Postgres SSOT via the `worktrees.list` verb on the
    daemon's local socket, in the wire shape the consumers below parse. An empty pool is a legitimate
    empty result; ANY failure to get an answer RAISES. The daemon itself never calls its own socket.

    The verb name and stdout field names are a WIRE CONTRACT; every consumer couples by rendered argv:
      1. the monorepo's pool-port checker — reads `_project`, `_number`, `stack`.
      2. the monorepo's worktree-agents snapshot — reads `path`, `session`. FAIL-OPEN, and its caller KILLS
         PROCESSES: any failure degrades the snapshot to `[]`, which reads as "no live session".
      3. `apps/jaahd/clauded_wt.py` — reads `_project`, `_number`, `title`.
    Only the failure half is pinned (`tests/test_ctl_worktrees_contract.py`)."""
    node_id = jaahd_main.resolve_node_id()
    # Omitted when empty: an empty value would become a literal `?node_id=`; omitted means "no filter".
    args = {"node_id": node_id} if node_id else {}
    # Blocking I/O, hopped off the loop. "No answer" is an EXCEPTION, never `[]` (a verdict consumers kill
    # processes on). Do NOT wrap this in a `try` that yields `[]`.
    records = await asyncio.to_thread(agent_socket.call, "worktrees.list", args)
    if not isinstance(records, list):
        raise RuntimeError(
            f"the worktrees.list verb answered {type(records).__name__}, not a list, for node {node_id!r} — "
            "refusing to report an empty pool, which reads as 'no slots' / 'no live session' to this verb's "
            "consumers"
        )
    return [_wire_record(rec) for rec in records]


def _wire_record(rec: dict) -> dict:
    """One `AgentWorktreeOut` payload rendered in the wire shape this verb's consumers parse.

    Only `stack` is restored (the endpoint spells it `stack_state`); a missing value fails closed to
    `"unknown"` in `check_pool_ports`. `title` passes through as the CP resolved it
    (`app/slot_title.py`) — never add a node-side fallback: the CP is its one author."""
    out = dict(rec)
    out["stack"] = rec.get("stack_state")
    return out


def cmd_worktrees() -> int:
    try:
        records = asyncio.run(_fetch_worktrees())
    except Exception as exc:  # noqa: BLE001 — any transport failure fails LOUD (D1), never silent
        print(f"jaahd.ctl: worktrees: pool roster unreachable: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(records))
    return 0


# ── MUTATE surface (T5/T6): release / retire / set-title / create / reconcile ──────────
class MutationError(RuntimeError):
    """A mutation could not be delivered or the daemon refused/failed it — surfaced as a loud nonzero
    exit with a stderr diagnostic, never a silent no-op."""



def _cp_request(
    node_id: str, command_type: str, args: dict, timeout: float,
) -> dict:
    """Dispatch one pool command to `node_id` through the console's `POST /api/commands` and return its
    reply as `{"status": ..., "result": ...}`. Raises `MutationError` on any transport failure, refusal or
    malformed reply. Serves `retire`/`set_title`/`create`/`reconcile`; `release` is `cmd_release`.

    The operator's session, not a node credential (`_console_session_api`): the CP runs the permission key,
    slot probe, arg resolver and audit row. The `command_type`s sent are pinned against
    `routes/commands.VALID_TYPES` by `tests/test_ctl.py`. A non-201 carries the CP's `detail` verbatim; a
    reply with no `status` is a transport fault, not a refusal."""
    api = _console_session_api()
    body = {"command_type": command_type, "args": args, "node_id": node_id}
    try:
        # Must outlive the CP's own 180s wait, or a landed mutation reads as "control plane unreachable".
        status, raw = api.api_request("POST", "/api/commands", body, timeout=timeout + 20)
    except api.ControlPlaneApiError as exc:
        raise MutationError(str(exc)) from exc
    if status != 201:
        raise MutationError(_api_detail(status, raw))
    try:
        reply = json.loads(raw or b"{}")
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or "status" not in reply:
        raise MutationError(f"POST /api/commands returned a malformed reply for {command_type!r}")
    return {"status": reply.get("status"), "result": reply.get("result")}


def _project_id_of(verb: str, name: str) -> int | None:
    """NAME's `projects.id`, off this node's own project roster (`project_via_socket`, exact match) — the id
    every operator route and command addresses a project by; argv carries the name a human types. The node
    belongs to one org, so its roster holds at most one project of a name.

    None after a stderr refusal when the roster is unreadable, names no such project, or its row has no id:
    sending the name alone to a CP that addresses by id would fail anyway, and less legibly."""
    row, why = project_via_socket(name)
    project_id = (row or {}).get("id")
    if not isinstance(project_id, int) or isinstance(project_id, bool):
        print(
            f"jaahd.ctl: {verb}: cannot resolve project {name!r} to its id: "
            f"{why or 'its roster row carries no id'}",
            file=sys.stderr,
        )
        return None
    return project_id


def cmd_release(project: str, number: str, *, force: bool) -> int:
    """The operator's release, on the operator's route: `POST /api/worktrees/{project_id}/{number}/release`
    (`routes/pool_slots.py::release_slot`). 204 -> 0; else nonzero + the CP's refusal text. `--force` waives
    only the live-session guard. NOT `_mutate`: the node's `_release` frees nothing, so the row would stay
    held while the exit said success. `node_id` pins THIS node's slot."""
    try:
        slot_number = int(number)
    except ValueError:
        print(f"jaahd.ctl: release: number must be an integer, got {number!r}", file=sys.stderr)
        return 1
    project_id = _project_id_of("release", project)
    if project_id is None:
        return 1
    node_id = jaahd_main.resolve_node_id()
    api = _console_session_api()
    path = (
        f"/api/worktrees/{project_id}/{slot_number}/release"
        f"?node_id={urllib.parse.quote(node_id, safe='')}"
    )
    try:
        status, raw = api.api_request("POST", path, {"force": bool(force)}, timeout=_REQUEST_TIMEOUT + 20)
    except api.ControlPlaneApiError as exc:
        print(f"jaahd.ctl: release: {exc}", file=sys.stderr)
        return 1
    if status != 204:
        print(f"jaahd.ctl: release: {_api_detail(status, raw)}", file=sys.stderr)
        return 1
    return 0


def _mutate(command_type: str, args: dict, *, print_path: bool, quiet: bool) -> int:
    """Run one pool-mutation verb end-to-end: request/reply through `POST /api/commands` to the daemon,
    then map the reply to an exit. `done` -> 0 (for `create`, print the path unless `--quiet`);
    anything else or a transport failure -> nonzero + stderr diagnostic.

    A `project` name gains its `project_id` (C5: the CP addresses by id; the name rides for an older CP)."""
    node_id = jaahd_main.resolve_node_id()
    args = {k: v for k, v in args.items() if v is not None}
    if "project" in args:
        project_id = _project_id_of(command_type, str(args["project"]))
        if project_id is None:
            return 1
        args["project_id"] = project_id
    try:
        reply = _cp_request(node_id, command_type, args, _REQUEST_TIMEOUT)
    except MutationError as exc:
        print(f"jaahd.ctl: {command_type}: {exc}", file=sys.stderr)
        return 1
    status = reply.get("status")
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    if status != "done":
        reason = (result or {}).get("reason") or f"daemon replied {status!r}"
        print(f"jaahd.ctl: {command_type}: {reason}", file=sys.stderr)
        return 1
    if print_path and not quiet:
        path = (result or {}).get("path") or ""
        if path:
            print(path)
    return 0


def match_project_row(
    projects: list, name: str, *, allow_shorthand: bool = False,
) -> tuple[dict | None, str]:
    """THE SINGLE HOME for the socket side's project-name matching: pick NAME's row out of a
    `GET /api/projects` ROSTER. Returns `(row, "")` or `(None, why)` — `why` is always non-empty when the
    row is None, so a caller can refuse loudly with a diagnostic instead of a bare "not found".

    Pure. Exact case-sensitive match wins; with `allow_shorthand` (default False — a convenience for a human
    at a prompt only) a case-insensitive substring must hit exactly one row. Non-dict rows are skipped."""
    for proj in projects:
        if isinstance(proj, dict) and proj.get("name") == name:
            return proj, ""
    if not allow_shorthand:
        return None, (
            f"no project is named {name!r} exactly, and this caller did not opt into shorthand matching"
        )
    lname = name.lower()
    hits = [
        proj for proj in projects
        if isinstance(proj, dict) and lname in (proj.get("name") or "").lower()
    ]
    if len(hits) == 1:
        return hits[0], ""
    return None, f"project {name!r} matched {len(hits)} project(s) as a shorthand (need exactly 1)"


def project_via_socket(name: str, *, allow_shorthand: bool = False) -> tuple[dict | None, str]:
    """NAME's project row, read through the `projects.list` verb on the daemon's local socket and matched
    by `match_project_row`. `(row, "")` or `(None, why)`, never a raise.

    Fail-closed, and a transport fault is reported as "could not read the roster", never "no such project"."""
    try:
        rows = agent_socket.call("projects.list")
    except agent_socket.AgentSocketError as exc:
        return None, f"the project roster could not be read from the node daemon ({exc})"
    if not isinstance(rows, list):
        return None, (
            f"the projects.list verb answered {type(rows).__name__}, not a list — the project roster "
            f"could not be read"
        )
    return match_project_row(rows, name, allow_shorthand=allow_shorthand)


def cmd_reconcile(project: str | None, number: int | None, timeout: float) -> int:
    """Ask the control plane to dispatch a `reconcile` to this node — one slot, or the whole pool when
    both are omitted. An operator's tool. Fails loud on any non-`done` outcome."""
    node_id = jaahd_main.resolve_node_id()
    verb_args: dict = {}
    if project is not None and number is not None:
        project_id = _project_id_of("reconcile", project)
        if project_id is None:
            return 1
        verb_args = {"project_id": project_id, "project": project, "number": number}
    try:
        reply = _cp_request(node_id, "reconcile", verb_args, timeout)
    except MutationError as exc:
        print(f"jaahd.ctl: reconcile: {exc}", file=sys.stderr)
        return 1
    if reply.get("status") != "done":
        print(f"jaahd.ctl: reconcile: daemon rejected the request: {reply}", file=sys.stderr)
        return 1
    print(json.dumps(reply.get("result")))
    return 0


# ── SLOT-ROW surface: declare / de-declare which slots exist ─────────────────────────────────────────
#
# The slot set is the CP's to dictate, so these are thin clients of the superadmin-gated
# `POST/DELETE /api/worktrees` (`routes/pool_slots.py`, every guard's home). Fail closed when unreachable.
def _console_session_api():
    """The monorepo's `control_plane_client` — the single home for a CLI making a SESSION-cookie call into the
    Control-plane API (it logs in with the hub `.env`'s ADMIN_* only when the persisted session has expired).

    Not the agent socket: these routes are superadmin/permission-keyed human authority, and the node
    token reaches `/api/agent/*` only. `tools/` is prepended at call time (not on jaahd's import path)."""
    from env_helpers import _REPO_ROOT

    tools_dir = str(_REPO_ROOT / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import control_plane_client

    return control_plane_client


def _api_detail(status: int, raw: bytes) -> str:
    """The API's own refusal text, verbatim where there is one (a 409 names which guard fired)."""
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return f"HTTP {status}: {(raw or b'')[:400].decode('utf-8', 'replace')}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if detail is not None:
        return f"HTTP {status}: {json.dumps(detail)}"
    return f"HTTP {status}: {json.dumps(body)}"


def _print_lookup(verb: str, resolve, *, unknown_note: str) -> int:
    """Print a `project_lookup` answer on stdout (exit 0), or refuse with exit 4 and NOTHING on stdout when
    the project roster could not be read.

    Consumers read stdout and ignore the exit status, and `""` is their permissive branch — so an unreadable
    roster prints nothing. `unknown_note` says what the caller loses by the refusal."""
    import project_lookup

    try:
        print(resolve(project_lookup))
    except project_lookup.RosterUnreadable as exc:
        print(f"jaahd.ctl: {verb}: {exc} — {unknown_note}", file=sys.stderr)
        return 4
    return 0


def cmd_card_model(project: str) -> int:
    """Print PROJECT's `default_model` (empty if unset/not found). Port of visible-lib.sh's
    `card_model_extract`. Exit 4 with no stdout when the roster is unreadable."""
    return _print_lookup(
        "card-model", lambda pl: pl._card_default_model(project),
        unknown_note=(
            "the launcher will fall back to the account's own default model, which is DELIBERATELY "
            "permissive: a model choice is a preference, not a gate (see clauded_wt.py's `model_flag`)"
        ),
    )


def cmd_prepare_trust(worktree: str, config_dir: str, provider: str, project: str = "") -> int:
    """Seed WORKTREE's workspace trust the way PROVIDER's own adapter does at dispatch (`prepare`): Claude
    Code's writes its `.claude.json` record, a vendor that keeps no such file writes none. Raises when the
    adapter refuses a write, so the exit status reports it. Holds `fleet_dispatch.account_lock` (an `flock` on
    the account home) like a dispatch's `prepare`, so a tab opening while a dispatch prepares the same account
    loses neither writer's trust or MCP entry."""
    import agents
    import fleet_dispatch

    with fleet_dispatch.account_lock(config_dir):
        agents.get_adapter(provider).prepare(worktree=worktree, config_dir=config_dir, project=project)
    return 0


def cmd_autoclose_flag(settings_path: str, extra: str, worktree: str, role: str, *, guard: bool = False) -> int:
    """Write a Claude Code `--settings` hooks file (reconcile + idle-autoclose, optionally the
    holder-claim/runner-status hooks for a `role=runner` session) and print the `--settings <path>` flag
    (empty if nothing to install). Port of visible-lib.sh's `vis_autoclose_flag`."""
    import fleet_dispatch
    import fleet_launch

    # The dispatch path's `META_FILE_MODE`: the seam's default `0664` would be a group-writable hook file.
    flag = fleet_launch.write_settings(
        settings_path, extra=extra, worktree=worktree, role=role,
        hooks_dir=str(fleet_dispatch.HOOKS_DIR),
        mode=fleet_dispatch.META_FILE_MODE, guard=guard,
    )
    print(flag)
    return 0


def cmd_tmux_window(label: str, cwd: str, cmd: str, pipe_log: str | None, session: str = "",
                    api_key_account: str = "", bedrock_session: str = "", claude_session: str = "",
                    claude_config_dir: str = "", git_ceiling: str = "") -> int:
    """Add a labelled tmux window running CMD to the shared fleet console session, then pop ONE desktop
    terminal attached to it (unless already showing). Port of visible-lib.sh's `vis_window` (+ its
    trailing `vis_ensure_terminal` call) — the ad-hoc launchers' entry point.

    SESSION is the caller's already-resolved session (`_tmux_safe_env()` may strip the override before it
    reaches here); empty resolves from the env.

    The fleet-server precondition is checked here: `tmux new-window` against a missing socket STARTS A
    SERVER AS THE CALLER — a wrong-uid or unreachable second server — and still reports success.

    API_KEY_ACCOUNT, for a console tab on an API-key account: the new pane is recorded as that account's
    console (`runner_registry.record_console_key`), the one proof the credential broker accepts for the
    tab's `apiKeyHelper`. Recorded here because this is where the pane pid is known.

    BEDROCK_SESSION, for a console tab on the Jaah account: the pane is recorded as that session's console
    (`runner_registry.record_console_bedrock`) — the proof its `awsCredentialExport` needs — and its
    transcript (CLAUDE_SESSION, under CLAUDE_CONFIG_DIR) is registered for live metering
    (`bedrock_usage_tail`). A grant that cannot be recorded fails the open, as an API-key tab's does.

    GIT_CEILING, for a console tab on a FOLDER slot: the window carries it as
    `GIT_CEILING_DIRECTORIES` (`fleet_dispatch.folder_git_env`), so git run in the folder finds no ancestor
    repository — the same ceiling a runner launched into that folder gets."""
    import fleet_launch
    import session_user
    from env_helpers import NoNodeIdentity

    # Resolved before the refusal below quotes it; an unprovisioned node is told so, not handed a name.
    try:
        resolved = fleet_launch.tmux_session(session or None)
    except NoNodeIdentity as exc:
        print(f"ctl.py tmux-window: {exc}", file=sys.stderr)
        return 1

    if session_user.session_uid() is not None and not session_user.fleet_socket_ready():
        print(
            f"ctl.py tmux-window: the fleet tmux server is not running — no socket at "
            f"{session_user.fleet_socket()!r}. Sessions run as {session_user.session_user()!r} on this "
            f"node, so opening a window here would start a SECOND server under the wrong uid. Start it "
            f"with `systemctl start jaah-runner@{resolved}.service` (the instance is "
            f"this node's session name — or unset JAAH_SESSION_USER on a "
            f"host that has no such user).",
            file=sys.stderr,
        )
        return 1

    spawn: dict = {}
    window_env: dict = {}
    if git_ceiling:
        import fleet_dispatch

        window_env = fleet_dispatch.folder_git_env(git_ceiling)
    fleet_launch.vis_window(label, cwd, cmd, pipe_log or None, session=resolved, out=spawn,
                            **({"extra_env": window_env} if window_env else {}))
    if api_key_account:
        import runner_registry

        if not runner_registry.record_console_key(
                pane_pid=spawn.get("pane_pid"), api_key_account=api_key_account, worktree=cwd):
            print(f"ctl.py tmux-window: the tab opened, but its API-key account {api_key_account!r} could not "
                  f"be recorded for it — the credential broker will refuse its key", file=sys.stderr)
            return 1
    if bedrock_session:
        import bedrock_usage_tail
        import runner_registry

        if not runner_registry.record_console_bedrock(
                pane_pid=spawn.get("pane_pid"), bedrock_session=bedrock_session, worktree=cwd):
            print(f"ctl.py tmux-window: the tab opened, but its Jaah session {bedrock_session!r} could not be "
                  f"recorded for it — the credential broker will refuse its credentials", file=sys.stderr)
            return 1
        # Best effort, as for a runner: an unmetered tab is still billed by AWS's invocation log.
        bedrock_usage_tail.register(
            jaah_session_id=bedrock_session, pane_pid=spawn.get("pane_pid"), worktree=cwd,
            config_dir=claude_config_dir or session_user.default_claude_config_dir(),
            native_session_id=claude_session)
    fleet_launch.ensure_terminal(resolved)
    return 0


def cmd_pop_terminal(title: str, cmd: str) -> int:
    """Pop ONE desktop terminal running CMD (best-effort no-op without a DISPLAY/terminal). Port of
    visible-lib.sh's `vis_ensure_terminal` popup half / open-term.sh's `_pop_terminal`."""
    import fleet_launch

    fleet_launch.pop_terminal(title, cmd)
    return 0


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        prog="jaahd.ctl",
        description="read + mutation shim over this node's node_state_<nodeId> KV bucket via jaahd",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # The verb NAME is a wire contract: consumers render it as a bare argv string.
    sub.add_parser("worktrees", help="print this node's pool-slot records as a JSON array")

    p_release = sub.add_parser(
        "release",
        help="free a slot: the operator's POST /api/worktrees/<project_id>/<n>/release (thin client)",
    )
    p_release.add_argument("project")
    p_release.add_argument("number")
    p_release.add_argument(
        "--force", action="store_true",
        help="release even if the slot has a FRESHLY OBSERVED live session in its worktree (the control "
        "plane otherwise refuses with a 409 naming that guard); relayed to the node's own live-session "
        "refusal too",
    )

    p_retire = sub.add_parser(
        "retire",
        help="retire a RESOLVED fleet-issue slot via the daemon (closed-duplicate: detach the tree to a "
        "fresh base, leave the fleet/task-<id> branch intact, free the slot) — thin client",
    )
    p_retire.add_argument("project")
    p_retire.add_argument("number")

    p_set_title = sub.add_parser("set-title", help="set a slot's sticky title via the daemon (thin client)")
    p_set_title.add_argument("project")
    p_set_title.add_argument("number")
    p_set_title.add_argument("title", nargs="?", default="")

    p_create = sub.add_parser("create", help="create the next free worktree slot via the daemon (thin client)")
    p_create.add_argument("project")
    p_create.add_argument("--quiet", action="store_true")

    p_reconcile = sub.add_parser(
        "reconcile",
        help="dispatch a reconcile mutation to this node's jaahd (one slot, or a full sweep if "
        "--project/--number are omitted)",
    )
    p_reconcile.add_argument("--project", default=None, help="pool project name")
    p_reconcile.add_argument("--number", type=int, default=None, help="pool worktree slot number")
    p_reconcile.add_argument(
        "--timeout", type=float, default=DEFAULT_RECONCILE_TIMEOUT,
        help=f"request/reply timeout in seconds (default: {DEFAULT_RECONCILE_TIMEOUT})",
    )

    # ── visible-lib.sh port surface ───────────────────────────────────────────────────────────────────
    p_cm = sub.add_parser(
        "card-model",
        help="print PROJECT's default_model (visible-lib.sh card_model_extract port)",
    )
    p_cm.add_argument("project")

    p_pt = sub.add_parser(
        "prepare-trust",
        help="seed WORKTREE's workspace trust through PROVIDER's adapter (`prepare`), as a dispatch does",
    )
    p_pt.add_argument("worktree")
    p_pt.add_argument("config_dir", help="the account's config dir; empty for none")
    p_pt.add_argument("provider")
    p_pt.add_argument("project", nargs="?", default="")

    p_acf = sub.add_parser(
        "autoclose-flag",
        help="write the --settings hooks file (reconcile + idle-autoclose) and print the --settings "
        "flag (visible-lib.sh vis_autoclose_flag port)",
    )
    p_acf.add_argument("settings_path")
    p_acf.add_argument("extra", nargs="?", default="")
    p_acf.add_argument("worktree", nargs="?", default="")
    p_acf.add_argument("role", nargs="?", default="")
    p_acf.add_argument("--guard", action="store_true",
                       help="the session is under the Architectural Guard (its launch wrote the rule bundle)")

    p_tw = sub.add_parser(
        "tmux-window",
        help="add a labelled tmux window running CMD to the shared fleet console + pop a desktop "
        "terminal (visible-lib.sh vis_window port, ad-hoc-launcher entry point)",
    )
    p_tw.add_argument("label")
    p_tw.add_argument("cwd")
    p_tw.add_argument("cmd_", metavar="cmd")
    p_tw.add_argument("--pipe-log", dest="pipe_log", default=None)
    p_tw.add_argument(
        "--session", default="",
        help="the tmux session the CALLER already resolved (defaults to fleet_launch.tmux_session()'s "
        "own last-resort default). Pass it so the launcher's reported session provably equals the "
        "targeted one — _tmux_safe_env() can strip the override before it reaches this process",
    )
    p_tw.add_argument(
        "--api-key-account", dest="api_key_account", default="",
        help="the API-key account the control plane opened this console tab on: recorded against the new "
        "pane so the credential broker serves that account's key to the tab's apiKeyHelper, and no other",
    )
    p_tw.add_argument(
        "--bedrock-session", dest="bedrock_session", default="",
        help="the Jaah session the control plane opened this console tab on: recorded against the new pane "
        "so the credential broker serves that session's Bedrock credentials to the tab, and no other",
    )
    p_tw.add_argument(
        "--claude-session", dest="claude_session", default="",
        help="with --bedrock-session: the tab's Claude session id, so its transcript is metered live",
    )
    p_tw.add_argument(
        "--claude-config-dir", dest="claude_config_dir", default="",
        help="with --bedrock-session: the config dir the tab's Claude actually runs in (its transcripts' home)",
    )
    p_tw.add_argument(
        "--git-ceiling", dest="git_ceiling", default="",
        help="a folder slot's GIT_CEILING_DIRECTORIES for the window, so git in the folder finds no ancestor repo",
    )

    p_pt = sub.add_parser(
        "pop-terminal",
        help="pop ONE desktop terminal running CMD, best-effort — the port of visible-lib.sh's "
        "vis_ensure_terminal / open-term.sh's _pop_terminal; called by open_term.py",
    )
    p_pt.add_argument("title")
    p_pt.add_argument("cmd_", metavar="cmd")

    args = parser.parse_args(argv)

    if args.cmd == "worktrees":
        return cmd_worktrees()
    if args.cmd == "release":
        return cmd_release(args.project, args.number, force=bool(args.force))
    if args.cmd == "retire":
        return _mutate(
            "retire",
            {"project": args.project, "number": args.number},
            print_path=False, quiet=False,
        )
    if args.cmd == "set-title":
        # Forward the resolved title as an empty string (not None) when clearing — `_mutate` drops None
        # args, but the daemon's `_set_title` must still receive title="" to write the null override.
        return _mutate(
            "set_title",
            {"project": args.project, "number": args.number, "title": args.title or ""},
            print_path=False, quiet=False,
        )
    if args.cmd == "create":
        return _mutate("create", {"project": args.project}, print_path=True, quiet=args.quiet)
    if args.cmd == "reconcile":
        if (args.project is None) != (args.number is None):
            parser.error(
                "reconcile: --project and --number must be given together (or both omitted for a "
                "full sweep)"
            )
        return cmd_reconcile(args.project, args.number, args.timeout)
    if args.cmd == "card-model":
        return cmd_card_model(args.project)
    if args.cmd == "prepare-trust":
        return cmd_prepare_trust(args.worktree, args.config_dir, args.provider, args.project)
    if args.cmd == "autoclose-flag":
        return cmd_autoclose_flag(args.settings_path, args.extra, args.worktree, args.role, guard=args.guard)
    if args.cmd == "tmux-window":
        return cmd_tmux_window(args.label, args.cwd, args.cmd_, args.pipe_log, args.session,
                               args.api_key_account, args.bedrock_session, args.claude_session,
                               args.claude_config_dir, args.git_ceiling)
    if args.cmd == "pop-terminal":
        return cmd_pop_terminal(args.title, args.cmd_)
    parser.error(f"unknown command {args.cmd!r}")
    return 2  # pragma: no cover — argparse.error() already exits


if __name__ == "__main__":
    raise SystemExit(main())
