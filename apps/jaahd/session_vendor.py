"""Which vendor's transcript belongs to which control-plane session — this node's own note to itself.

WHY THIS FILE EXISTS AT ALL. `session_sweep` is handed a session to drain as `(session_id, config_dir,
worktree_path)` and nothing else (`GET /api/agent/sessions/work`), and for a Claude session that is
enough: the id it carries IS the id Claude Code was launched with (`--session-id`), and the transcript
sits at `<config_dir>/projects/<munge(worktree)>/<that id>.jsonl`. For a vendor whose CLI MINTS ITS OWN
session id — `base.SESSION_DISCOVER` — the same three values identify nothing on disk: the id the control
plane knows is the dispatcher's `runner_session_id`, the vendor's rollout/chat file is named after a
different id entirely, and no amount of scanning `projects/` finds a file that was never written there.
So the sweep would resolve NO transcript for every such runner, and Audit > Sessions would show
that run with no transcript and no token count — which is exactly the gap this closes.

WHY IT IS NODE-LOCAL AND NOT A COLUMN ON THE SESSION ROW. "Which file on THIS machine holds session X's
transcript" is a node fact, and the control plane already refuses to hold its sibling: `sessions` carries
no `config_dir` either, deriving it per node from `jaah_home` + the account slug
(`app/routes/agent_sessions.py`). A CP column would mean a migration, a wire field on the binding POST,
and a deploy — for a value only the node that wrote it can ever use. The dispatcher knows every part of
the answer at launch, the sweep needs it minutes-to-hours later on the same box, and a small file under
the daemon's own state dir is the whole distance between them.

**IT LIVES IN THE DAEMON'S OWN STATE ROOT, NOT IN `$JAAH_HOME`, AND THAT IS A SECURITY DECISION.**
`$JAAH_HOME` is the SESSION USER's tree: uid `jaah` — every runner on this fleet, each of them a
prompt-injectable agent — can create, rename, unlink and symlink entries there. A note the runner can
write is a note the runner can turn into a FIFO (an `open()` that never returns, wedging the whole sweep
and with it every transcript this node would ever upload), or a symlink that redirects the daemon's own
write, or a directory symlink that aims `prune`'s `unlink` at files the runner cannot delete itself. Two
review lanes found exactly those three. So the notes go under `env_helpers.daemon_state_dir()`
(`/var/lib/jaahd`, `0750 jaahd:jaah` — a runner may traverse it, and may not create or delete
inside it) in a subdirectory this module creates `0700`. The reads below are STILL hardened against a
planted file, because a fence you can also stand behind is worth having twice.

WHAT IS RECORDED, AND WHAT IS NOT. Only facts the dispatcher HAS: the provider, the account config dir,
the worktree, and when the process was launched — plus, once anything has managed to resolve it, the
vendor's OWN session id (`vendor_session_id`), which PINS the binding so a later drain of the same
session cannot answer with a different, newer transcript. Nothing here is secret: a config dir is a path,
not a credential, and no token, key or prompt text is written.

A CLAUDE SESSION RESOLVES UNCHANGED, and the precise shape of that is worth stating rather than rounding
to "never read". The vendor leg consults this only AFTER the existing Claude-shaped resolution has come
up empty — so a Claude session that HAS its transcript resolves exactly as it always did, on the same
first try. A Claude session whose transcript is GONE (a rolled window, a released worktree) does reach
here and its note IS read, and then the vendor leg stops at the next check: only an adapter that mints
its own session id goes on to look, and Claude's is assigned. Either way no Claude transcript is ever
resolved through this path. The one other reader is the fold's pricing tier
(`session_sweep.claude_cli_line_tier`): it reads the note's `provider` to tell a DeepSeek session — Claude
Code, Claude's layout — from Claude's own, and for Claude's own it changes nothing.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import time
from pathlib import Path
from typing import Optional

import env_helpers

log = logging.getLogger("jaahd.session_vendor")

# One file per dispatched session, under the DAEMON's own state root (see the module header for why not
# `$JAAH_HOME`, and why that is not a matter of taste).
_SUBDIR = "session-vendor"

# A session id is a path SEGMENT here, and this is the fence that keeps it one. `\A…\Z` and not `^…$`,
# matching `session_sweep._SESSION_ID` exactly: Python's `$` also matches before a trailing newline, so
# the lax form accepts `"abc\n"` and composes `abc\n.json` where the sweep's own check refuses it — two
# files disagreeing about what a session id is, under a comment claiming they cannot.
#
# The rule itself: `pathlib` discards the left side of a join when the right side is absolute, so an
# unchecked id of `/etc/shadow` composes `/etc/shadow.json`, and one carrying `../` walks out of the
# state dir entirely. Both callers are in-daemon today; the check is here because the VALUE is a
# control-plane one either way.
_SESSION_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# The DEFAULT age at which a record is collectable — deliberately not the operative one. The sweep passes
# a bound derived from its OWN reporting horizon (`session_sweep.OBSERVED_HORIZON`), because that is the
# oldest session it will still report and therefore the oldest note it can still want; a bound below it
# would prune the note of a session the sweep goes on to drain, which is a transcript lost rather than a
# file saved. This value is the floor for a caller that names none.
PRUNE_AFTER_SECONDS = 30 * 24 * 3600

_DIR_MODE = 0o700
_FILE_MODE = 0o600

# Windows has no directory descriptors, no `O_DIRECTORY`/`O_NOFOLLOW` and no uids, so the fd-pinned POSIX
# path below cannot run there. Its fence is the installer's ACL instead: the state root grants SYSTEM,
# Administrators and the daemon's service SID only, so a runner can plant nothing in it. What the
# Windows path still refuses is a reparse point (symlink or junction) where the root or a note belongs,
# and NTFS has no FIFO for a read to hang on.
_WINDOWS = os.name == "nt"


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (a junction needs no privilege to create)."""
    return stat.S_ISLNK(st.st_mode) or bool(
        getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _windows_root() -> Optional[Path]:
    """The note root when it is a real directory, never a reparse point; None otherwise."""
    root = _root()
    try:
        st = os.lstat(root)
    except OSError:
        return None
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        return None
    return root


def _root() -> Path:
    return env_helpers.daemon_state_dir(_SUBDIR)


def _path(session_id: str) -> Optional[Path]:
    if not _SESSION_ID.match(session_id or ""):
        return None
    return _root() / f"{session_id}.json"


def _finite(value: object) -> Optional[float]:
    """`value` as a usable epoch, or None. NaN AND infinity ARE REFUSED, and that is the point rather
    than pedantry: `json.loads` accepts both, and every `mtime < since` comparison against a NaN is
    False — so a NaN `launched_at` silently ERASES the floor that stops a lookup naming the account's
    previous runner. A negative or zero instant is refused for the same reason."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def launch_floor(note: dict) -> float:
    """A note's `launched_at` as a usable epoch, or 0.0 — the SINGLE home of that read.

    Here rather than in the caller because 0.0 is not a harmless default: it is the absence of a floor,
    and the floor is what stops a vendor lookup naming the account's PREVIOUS runner. A caller that
    coerced this itself would eventually coerce a NaN — which `json.loads` accepts and which makes every
    `mtime < since` comparison False, i.e. erases the floor while looking like one.
    """
    return _finite(note.get("launched_at")) or 0.0


def usage_floor(note: dict) -> float:
    """A note's `usage_floor` as a usable epoch, or 0.0 (no floor) — the instant before which a line's
    spend is not this session's.

    Set only on a console hand-off's RESUMED session (`record(..., usage_floor=True)`): the vendor
    transcript it resumes also holds the chat front's turns, which the control plane already counted from
    that front's own ACP events, so the node folds this row only from its launch on. Read through `_finite`
    for `launch_floor`'s reason: a NaN would erase the floor while looking like one.
    """
    return _finite(note.get("usage_floor")) or 0.0


def usage_ceiling(note: dict) -> float:
    """A note's `usage_ceiling` as a usable epoch, or 0.0 (none) — the instant from which a line's spend is
    no longer this session's. Set when a console hand-off moved the session's conversation to another front
    (`close_usage_window`): that front resumes the SAME vendor transcript and appends its own turns, which
    the control plane counts on that front's row."""
    return _finite(note.get("usage_ceiling")) or 0.0


def close_usage_window(session_id: str, at: Optional[float] = None) -> bool:
    """Stamp `usage_ceiling` (`at`, else now) on `session_id`'s note, once its front has handed the
    conversation on and is gone. True when it landed; False when the session has no note (an ACP front,
    counted from its events) or it could not be written. First stamp wins, like the pin."""
    note = read(session_id)
    if not note or usage_ceiling(note):
        return False
    stamp = _finite(at) if at is not None else time.time()
    note["usage_ceiling"] = stamp or time.time()
    return _write(session_id, note)


def record(session_id: str, *, provider: str, config_dir: str, worktree: str,
           launched_at: Optional[float] = None, usage_floor: bool = False) -> bool:
    """Note that `session_id` was dispatched as `provider`, and where. True when it landed.

    BEST EFFORT BY CONTRACT, and the return value is for a test and a log line rather than for a caller
    to branch on: this is called on the dispatch path, and a launch the control plane has already
    committed a slot and a Task state to must never be unwound because a diagnostic note could not be
    written. What it costs when it fails is one session's transcript, which is the same thing that was
    lost before this file existed. `ValueError` is caught beside `OSError` for that contract's sake — a
    payload the writer cannot serialize or encode raises one, and that is not an `OSError`.

    Written for EVERY provider, with no branch on which one — the reader decides what to do with it, and
    a dispatcher that asked "is this Claude?" would be spelling a vendor name at a launch construction
    site, which is the one thing that layer may not do (`test_agent_adapters` censuses it).
    """
    # A NOTE WITH NO ACCOUNT HOME CAN NEVER RESOLVE, so it is refused HERE rather than warned about on
    # every tick for the rest of the session's observation window. `config_dir` is empty BY
    # CONSTRUCTION on two ordinary dispatch paths — an API-key account whose home could not be derived
    # (`fleet_dispatch.api_key_account_dir`), and an unleased dispatch with no account at all — and the reader's fence is "the recorded home must be one
    # the control plane named", which nothing can satisfy with "". Writing it anyway produced a
    # per-tick warning that ACCUSED THE CONTROL PLANE of naming a different home than the dispatcher,
    # when in truth neither named one: a confidently wrong log line an operator would believe, for
    # ninety days, about a session whose transcript was never reachable from here in the first place.
    #
    # **WHAT IS GENUINELY LOST, SAID PLAINLY HERE BECAUSE NOTHING ELSE SAYS IT YET.** An API-key account
    # whose home could not be derived has no directory at all, so EVERY such run — for any provider whose CLI mints its own
    # session id — has no locatable transcript and is reported terminally `unavailable`, by
    # construction rather than by mishap. An earlier version of this comment claimed that was "published
    # as a degradation row"; it is not: no row anywhere tells an operator that choosing an API-key
    # account costs them the transcript. That row is a DEFERRED FOLLOW-UP — it
    # is a degradation on each provider row under a parity gate and a per-provider count the console
    # asserts, so it is a change to the operator-facing catalogue rather than to this ingest.
    # The refusal binds a CLI that MINTS its own id (the only reader of the home). A CLI whose id the
    # dispatcher assigns (Claude Code, DeepSeek's runners) is resolved through Claude's layout instead, and
    # its note is read only for its `provider` — the pricing tier (`session_sweep.claude_cli_line_tier`) —
    # which an API-key DeepSeek runner, always home-less, would otherwise never have.
    if not (config_dir or "").strip() and _mints_its_own_id(provider):
        log.debug("session_vendor: session %s was dispatched as %r with no account directory, so its "
                  "transcript is not locatable through a note — recording none", session_id, provider)
        return False
    stamp = _finite(launched_at) if launched_at is not None else time.time()
    if stamp is None:
        log.warning("session_vendor: session %s was handed a launch instant that is not a usable epoch "
                    "(%r) — recording now() instead, so the lookup keeps a floor", session_id,
                    launched_at)
        stamp = time.time()
    payload = {
        "session_id": session_id,
        "provider": provider or "",
        "config_dir": config_dir or "",
        "worktree": worktree or "",
        "launched_at": stamp,
    }
    if usage_floor:
        # The same instant as `launched_at`: the resumed transcript's turns before it are the chat front's.
        payload["usage_floor"] = stamp
    return _write(session_id, payload)


def _mints_its_own_id(provider: str) -> bool:
    """Whether `provider`'s adapter discovers its session id (`SESSION_DISCOVER`). True for an unknown or
    unreadable provider: the refusal above is then kept, the fail-safe direction."""
    try:
        import agents  # noqa: PLC0415 — in-function: this module stays a light leaf for the one-shot

        return agents.get_adapter(provider).session_binding() != agents.SESSION_ASSIGN
    except Exception:  # noqa: BLE001 — an unusable adapter keeps the old refusal
        return True


def pin_vendor_session(session_id: str, vendor_session_id: str, *, replace: bool = False) -> bool:
    """Record the VENDOR's own session id for `session_id`, once something has resolved it. First one
    wins; a note that already carries one is left alone.

    **WHY PINNING MATTERS AND WHAT IT PREVENTS.** A vendor session is otherwise found by "the newest
    transcript on this account, after this launch" — a FLOOR with no ceiling. That answer can CHANGE
    between drain attempts: the slot gets re-leased, a newer session writes its own transcript, and the
    retry now resolves the wrong run's conversation into this session's permanent audit record. Freezing
    the first answer makes the binding stable, so a retry cannot drift onto a newer file.

    FIRST-WINS RATHER THAN LAST: the earliest resolution is the one taken closest to the run itself, and
    every later one is strictly more likely to be looking at somebody else's transcript.

    **`replace=True` IS THE ONE EXIT FROM FIRST-WINS, and it exists because first-wins alone made the
    pin a weapon.** A co-tenant on the same account can write a transcript naming the VICTIM's worktree,
    win the victim's first resolution, have its id frozen here — and then delete its decoy, after which
    the victim resolves nothing forever and its record is reported terminally unavailable while its real
    transcript sits on disk. So the caller may replace a pin that RESOLVED TO NOTHING, and only that: a
    pin that still resolves is never replaced, which is the whole of what first-wins was protecting.

    **AND THAT EVIDENCE IS WEAKER THAN "PROVEN DEAD", which this said until it was checked.** What the
    caller actually observes is that the adapter answered no transcript for the pinned id — and an
    adapter answers that for several reasons besides the file being gone: its content gate refused a
    file whose name matched, or its walk was truncated before reaching it and it says so rather than
    claiming the file is absent. So the replacement rule is "the pin no longer resolves AND re-discovery
    names a different id", which is enough to take away the destruction primitive above without ever
    displacing a pin that still works — but it is not proof of death, and a reader deciding what else
    may replace a pin should not treat it as one. See `session_sweep._vendor_transcript`.
    """
    if not _SESSION_ID.match(vendor_session_id or ""):
        # SAID OUT LOUD, because a silent refusal here disables the very drift protection this function
        # exists to provide, permanently and invisibly. The vendor id comes out of a file the RUNNER
        # wrote, so a value this regex refuses — a slash, a newline, 200 characters — is a value a runner
        # can choose: the pin never lands, and the caller therefore re-discovers "the newest transcript
        # after the floor" on EVERY tick, forever, which is exactly the floating resolution the pin
        # freezes. `%r` on the value: it is runner-chosen, and one carrying a newline would forge a
        # journal record.
        log.warning("session_vendor: refusing to pin %r as session %s's vendor session id — not a name "
                    "that can be a path segment, so this session's transcript will be re-discovered on "
                    "every tick instead of pinned", vendor_session_id, session_id)
        return False
    note = read(session_id)
    if not note:
        return False
    if note.get("vendor_session_id") and not replace:
        return False
    note["vendor_session_id"] = vendor_session_id
    return _write(session_id, note)


def _private_dir(directory: Path) -> bool:
    """Is `directory` this process's own and closed to everyone else? Logged and False when not.

    **THE FENCE THIS MODULE RESTS ON IS A PROPERTY OF THE DEPLOYMENT, so it is ASSERTED rather than
    documented.** `daemon_state_dir()` resolves `$HOME`, which the packaged unit pins to a root the
    session user cannot write — that is what stops a runner planting a FIFO or a symlink where a note
    belongs. A mispackaged node, a unit that lost its `Environment=HOME=`, or a directory that somehow
    pre-exists group-writable would un-fence the whole module SILENTLY, and the module would go on
    reading files it believes only the daemon can create. This makes that state a visible refusal
    instead: no note is written, the log says why, and the transcripts of that node's vendor runs are
    the cost — which is the same cost as before this module existed, and a great deal cheaper than a
    sweep a runner can wedge.

    `mkdir(mode=…)` cannot repair a directory that already exists, which is exactly why the check is
    here and not left to the create path.
    """
    try:
        # `lstat`, NOT `stat`: `prune` refuses a SYMLINKED root outright (`O_DIRECTORY | O_NOFOLLOW`), so
        # a check that followed one here would write notes into a directory the pruner then declines to
        # visit — an accumulation that never ages out, under two functions that disagree about what the
        # root is.
        st = os.lstat(directory)
    except OSError:
        return False
    if _is_link(st) or not stat.S_ISDIR(st.st_mode):
        log.warning("session_vendor: %s is not a directory — refusing to write a dispatch note there",
                    directory)
        return False
    if _WINDOWS:
        # No owner uid or mode bits to assert: the installer's ACL is the fence (see `_WINDOWS`).
        return True
    if st.st_uid != os.geteuid():
        log.warning("session_vendor: %s is not owned by this process (uid %d, not %d) — refusing to "
                    "write a dispatch note there", directory, st.st_uid, os.geteuid())
        return False
    if st.st_mode & 0o022:
        log.warning("session_vendor: %s is group- or world-writable (mode %o) — refusing to write a "
                    "dispatch note there, because anything that can write it can plant one",
                    directory, st.st_mode & 0o777)
        return False
    return True


def _write(session_id: str, payload: dict) -> bool:
    path = _path(session_id)
    if path is None:
        log.warning("session_vendor: refusing to record %r — not a name that can be a path segment",
                    session_id)
        return False
    try:
        # `mode=` on the FIRST creation only, so a pre-existing directory keeps whatever it has; the
        # explicit `chmod` below is what makes the note itself private regardless of umask.
        path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not _private_dir(path.parent):
            return False
        # Written through a temp file in the same directory and renamed: the reader is another pass of
        # this daemon and a half-written record would read as a corrupt one.
        #
        # A UNIQUE TEMP NAME, NOT `<sid>.json.tmp`, and the fixed one made the atomicity claim above
        # HALF TRUE. Two writers for one session are ordinary here, not exotic: the dispatcher records
        # the note and the daemon's own discovery thread pins the vendor id onto it, and the sweep
        # subprocess pins from a third process. On the fixed name they share ONE file — both `open(…,
        # "w")` it, both interleave their `json.dump` into it, and both `os.replace` it, so the rename
        # is atomic while the bytes being renamed are a splice of two records. `mkstemp` gives each
        # writer its own inode, so the rename is the only shared moment and last-writer-wins is the
        # worst outcome. `first-wins` in `pin_vendor_session` is still not atomic ACROSS processes
        # (read-modify-write); that costs at most one transcript's attribution, needs the daemon-only
        # directory to reach, and is named as a follow-up rather than locked here.
        # The generated name must still end `.json.tmp`, because that is one of the two suffixes
        # `prune` collects — a temp file that leaked past the helper's own cleanup (a hard kill) would
        # otherwise never age out of a directory whose whole cleanup is suffix-and-age.
        env_helpers.atomic_write_json(path, payload, mode=_FILE_MODE, suffix=".json.tmp")
    except (OSError, ValueError) as exc:
        log.warning("session_vendor: could not record session %s under %s (%s: %s) — its transcript may "
                    "not be findable", session_id, path.parent, type(exc).__name__, exc)
        return False
    return True


def read(session_id: str) -> Optional[dict]:
    """What was recorded for `session_id`, or None when nothing was (or it is unreadable).

    None is a REAL ANSWER and the common one: every session dispatched before this file existed has no
    record, and so does every session this node did not dispatch (an interactive console session). The
    caller's answer to None is to fall back to what it did before, never to fail.

    OPENED WITH `O_NOFOLLOW | O_NONBLOCK` AND CHECKED FOR `S_ISREG` — the same three flags, for the same
    three reasons, that one adapter's own `credential_state` was hardened with. A plain `open()` follows
    a symlink,
    and on a FIFO it NEVER RETURNS: no `except` can catch a call that does not come back, so one
    `mkfifo` would hang the sweep subprocess until its 1800-second kill and then hang the next one, and
    this node would never upload another transcript. The directory is the daemon's own now, which is the
    fence; this is the belt.
    """
    path = _path(session_id)
    if path is None:
        return None
    if _WINDOWS:
        return _read_windows(path)
    # THE ROOT IS PINNED BY DESCRIPTOR, exactly as `prune` pins it, and for the consistency that matters:
    # `O_NOFOLLOW` guards only the FINAL component, so a symlinked note DIRECTORY would be read through
    # while the pruner refuses to visit it and the writer refuses to write it — three functions with
    # three different ideas of where the notes are. `O_DIRECTORY | O_NOFOLLOW` gives all three the same
    # one, and the `openat` below cannot be redirected by a rename after the check.
    try:
        dir_fd = os.open(str(_root()), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                     | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    except OSError:
        return None
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            log.warning("session_vendor: %s is not a regular file — refusing to read it", path)
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            record_json = json.loads(fh.read(65536))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(record_json, dict):
        return None
    return record_json


def prune(*, older_than: float = PRUNE_AFTER_SECONDS, now: Optional[float] = None) -> int:
    """Delete records whose file has not been touched in `older_than` seconds. Returns how many went.

    Keyed on the FILE's mtime rather than on the `launched_at` inside it, deliberately: a record that
    cannot be parsed still has an mtime, so a corrupt file is collectable by the same rule as a stale one
    instead of living forever.

    **THE ROOT IS PINNED BY FILE DESCRIPTOR AND CHECKED FOR OWNERSHIP BEFORE A SINGLE `unlink`.**
    `os.scandir(path)` FOLLOWS a symlinked root, so a loop that trusted the name would delete every
    `*.json` older than the bound in whatever directory somebody pointed the name at — as the privileged
    uid, in a directory that uid can write and the planter cannot. `O_DIRECTORY | O_NOFOLLOW` refuses a
    symlinked root outright, the `st_uid` check refuses a directory this process does not own, and every
    `unlink` goes through `dir_fd` so the directory cannot be swapped underneath the loop.
    """
    stamp = time.time() if now is None else now
    if _WINDOWS:
        return _prune_windows(older_than, stamp)
    root = _root()
    try:
        dir_fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return 0
    gone = 0
    try:
        try:
            if os.fstat(dir_fd).st_uid != os.geteuid():
                log.warning("session_vendor: %s is not owned by this process — refusing to prune it",
                            root)
                return 0
            names = os.listdir(dir_fd)
        except OSError:
            return 0
        for name in names:
            if not name.endswith((".json", ".json.tmp")):
                continue
            try:
                st = os.lstat(name, dir_fd=dir_fd)
                if not stat.S_ISREG(st.st_mode) or (stamp - st.st_mtime) <= older_than:
                    continue
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                continue
            gone += 1
    finally:
        os.close(dir_fd)
    return gone


def _read_windows(path: Path) -> Optional[dict]:
    """`read` on Windows: the root and the note must each be what they claim, never a reparse point."""
    if _windows_root() is None:
        return None
    try:
        st = os.lstat(path)
        if _is_link(st) or not stat.S_ISREG(st.st_mode):
            log.warning("session_vendor: %s is not a regular file — refusing to read it", path)
            return None
        with open(path, "rb") as fh:
            record_json = json.loads(fh.read(65536))
    except (OSError, ValueError):
        return None
    return record_json if isinstance(record_json, dict) else None


def _prune_windows(older_than: float, stamp: float) -> int:
    """`prune` on Windows: by name under a root that is not a reparse point, regular files only."""
    root = _windows_root()
    if root is None:
        return 0
    gone = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.endswith((".json", ".json.tmp")):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
            if _is_link(st) or not stat.S_ISREG(st.st_mode) or (stamp - st.st_mtime) <= older_than:
                continue
            os.unlink(entry.path)
        except OSError:
            continue
        gone += 1
    return gone


__all__ = ["record", "pin_vendor_session", "read", "prune", "launch_floor", "usage_floor", "usage_ceiling",
           "close_usage_window",
           "PRUNE_AFTER_SECONDS"]
