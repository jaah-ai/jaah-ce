"""agents/base.py — the RUNNER ADAPTER PROTOCOL: everything a fleet runner's CLI is allowed to differ
about, named once, so no vendor is privileged in the control flow (PRD R2.2).

The adapter owns the WHOLE exec line (not a base to append to), and every launch site reaches it through
`agents.get_adapter(provider)`, Claude included.

Stdlib only, no daemon imports at module scope, so `fleet_launch` can import the package without a cycle.
Shapes only: no registry (`agents/__init__.py`) and no vendor name.

A `Protocol`, not an ABC: structural typing, nothing to inherit. Conformance is checked by
`tests/test_agent_adapters.py` against `ADAPTER_MEMBERS`, not at runtime.
"""
from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class LifecycleChains:
    """WHAT the fleet wants to happen at each point of a runner's life, in VENDOR-NEUTRAL terms.

    Each field is one shell command already composed by `fleet_launch._lifecycle_chains` (which hooks,
    order, role gate are fleet facts); only the rendering is per vendor.

      on_session_start  — the session has started                (Claude: SessionStart)
      on_turn_end       — one turn finished                      (Claude: Stop)
      on_session_end    — the session is ending                  (Claude: SessionEnd)
      on_error          — the CLI failed while the session lives  (Claude: StopFailure)
      on_prompt_submit  — the operator submitted a prompt        (Claude: UserPromptSubmit)
      on_pre_tool       — before a tool call                     (Claude: PreToolUse)
      on_post_tool      — after a tool call                      (Claude: PostToolUse)
      runner            — a fleet runner, not an operator's console tab (the memory tools a session has are
                          its task's memory arm's to decide in `launch.sh`, never a renderer's deny list)

    Empty means "nothing to install": a renderer omits the event. A renderer must never drop a non-empty
    chain (`on_pre_tool` carries the Architectural Guard on a guarded session; `on_post_tool` is unwired today).
    """

    on_session_start: str = ""
    on_turn_end: str = ""
    on_session_end: str = ""
    on_error: str = ""
    on_prompt_submit: str = ""
    on_pre_tool: str = ""
    on_post_tool: str = ""
    runner: bool = False

    def any_set(self) -> bool:
        """Is there anything at all to install? (`build_settings` returns `(None, "")` when not.)"""
        return any(
            (self.on_session_start, self.on_turn_end, self.on_session_end, self.on_error,
             self.on_prompt_submit, self.on_pre_tool, self.on_post_tool)
        )


# How far above the composer placeholder a live busy line is looked for: measured 2 and 4 lines on the two
# vendor TUIs (a status line, or a spinner row then a rule, a mode line and the box top); 8 is headroom.
_BUSY_LINES_ABOVE_COMPOSER = 8


def _last_line_with(lines: list[str], marker: str) -> int | None:
    """Index of the last line containing `marker`, or None."""
    for i in range(len(lines) - 1, -1, -1):
        if marker in lines[i]:
            return i
    return None


@dataclass(frozen=True)
class ComposerTui:
    """What one vendor's TUI looks like on screen, for anything PASTED into its pane — a task prompt, or a
    question's answer (`answer_delivery.deliver_answer`).

    Per adapter because TUIs differ (chevron `❯` vs `›`, U+00A0 vs space, paste collapsing).
    `answer_delivery` owns the vendor-neutral tmux mechanism. Every field is MEASURED off a live pane.
    """

    #: Present when the composer is up and empty — the CLI's own placeholder text.
    ready_marker: str
    #: Present ONLY while a turn is in flight; its absence is the other half of "ready".
    busy_marker: str
    #: How this TUI collapses a LARGE paste (`[Pasted text #1 +3 lines]`, `[Pasted Content 5150 chars]`);
    #: smaller pastes show verbatim (see `paste_landed`).
    paste_placeholder: re.Pattern
    #: Characters of an uncollapsed payload to look for: short enough not to wrap, long enough to be unique.
    payload_prefix_chars: int = 24
    #: Present ONLY while this CLI's structured-question dialog is on screen — the one screen an answer's
    #: `Esc` may be sent into (`answer_delivery.deliver_answer`). Positive, because a turn's busy line can
    #: show beside the dialog (measured, per adapter), and absence of "ready" also describes a working turn.
    dialog_marker: str = ""
    #: The dialog's free-text row ("type your own answer"), its group 1 the key that opens it — where a CLI
    #: offers one, the answer is TYPED INTO the dialog (`answer_delivery`) and comes back as the tool's own
    #: result, never queued behind a dismissed turn. None = no such row: `Esc`, then the answer as a message.
    write_in_row: "re.Pattern | None" = None
    #: Present ONLY while that free-text field is open and takes typed text — the proof the key landed.
    write_in_prompt: str = ""

    def is_ready(self, pane: str) -> bool:
        """Is this pane's composer up, idle, and safe to paste into? The conjunction is the single home.

        "Idle" is read only in the lines just above the LAST composer placeholder, where a live busy line
        sits: a TUI can leave a frozen busy line higher up the screen (measured: a cancelled turn's spinner
        27 lines above an idle composer), and reading the whole pane would call that composer busy forever."""
        lines = pane.splitlines()
        at = _last_line_with(lines, self.ready_marker)
        if at is None:
            return False
        return self.busy_marker not in "\n".join(lines[max(0, at - _BUSY_LINES_ABOVE_COMPOSER):])

    def dialog_up(self, pane: str) -> bool:
        """Is this CLI's question dialog the live screen? Its marker must come AFTER the last composer
        placeholder (the dialog hides the composer), so a dialog left in the scrollback never counts."""
        if not self.dialog_marker:
            return False
        lines = pane.splitlines()
        dialog = _last_line_with(lines, self.dialog_marker)
        composer = _last_line_with(lines, self.ready_marker)
        return dialog is not None and (composer is None or dialog > composer)

    def write_in_key(self, pane: str) -> str | None:
        """The key that opens the LIVE dialog's free-text row, or None (no such row declared, no dialog up,
        or none on screen). Read off the rows below the last composer, as `dialog_up` reads its marker: a row
        left in the scrollback by an earlier dialog never counts."""
        if self.write_in_row is None or not self.dialog_up(pane):
            return None
        lines = pane.splitlines()
        composer = _last_line_with(lines, self.ready_marker)
        for line in reversed(lines[0 if composer is None else composer + 1:]):
            match = self.write_in_row.match(line)
            if match:
                return match.group(1)
        return None

    def write_in_open(self, pane: str) -> bool:
        """Is the dialog's free-text field open? Only on its own prompt — a row that is EXACTLY the prompt, so
        a question or answer quoting it never counts — below the last composer."""
        return self.write_in_field(pane) is not None

    def write_in_field(self, pane: str) -> str | None:
        """What the open free-text field holds (its rows after the prompt, joined), or None when none is open."""
        if not self.write_in_prompt:
            return None
        lines = pane.splitlines()
        composer = _last_line_with(lines, self.ready_marker)
        for i in range(len(lines) - 1, -1 if composer is None else composer, -1):
            if lines[i].strip() == self.write_in_prompt:
                return "\n".join(lines[i + 1:])
        return None

    def paste_landed(self, pane: str, payload: str = "") -> bool:
        """Did PAYLOAD visibly land on this composer? BOTH TESTS ARE POSITIVE, deliberately.

        Above the vendor's threshold the paste collapses to a placeholder; below it the payload shows
        verbatim with no placeholder, so the payload text is the other evidence. Never "the empty-composer
        placeholder is gone": a crashed CLI shows none either."""
        if self.paste_placeholder.search(pane):
            return True
        flat, seen = " ".join(payload.split()), " ".join(pane.split())
        n = self.payload_prefix_chars
        # Head OR tail: the tail is where dispatches differ (every prompt opens with the same banner);
        # the head survives a composer scrolled past its end (`_capture` reads the visible pane only).
        return bool(flat) and (flat[:n] in seen or flat[-n:] in seen)

    def submitted(self, pane: str) -> bool:
        """Did the composer's contents go? POSITIVE, on either of two signals.

        `busy_marker` (a turn in flight) or the empty-composer placeholder back. Not "the payload is gone":
        a submitted short payload stays visible in the transcript."""
        return self.busy_marker in pane or self.ready_marker in pane


@dataclass(frozen=True)
class ConsentDialog:
    """One startup consent dialog a CLI can raise before its first turn, and the row that ACCEPTS it
    (`consent_watch.watch_launch`). Both strings are MEASURED off a live pane, per adapter.

    The accept row is named, never assumed highlighted: several dialogs default to their REJECT row
    (Claude's folder trust and bypass-mode warning highlight "No, exit"), so a bare `Enter` would end the
    runner. `enter_accepts` is the one exception, for a dialog whose default focus is MEASURED to be its
    accept row."""

    #: Which dialog, for the node log.
    name: str
    #: Text present only while this dialog is on screen.
    signature: str
    #: The accept row's whole label, exactly as rendered once the cursor glyph is stripped.
    accept: str
    #: True only where the dialog opens focused on its accept row, so a bare `Enter` accepts it even on a
    #: screen that shows the dialog without its rows (`consent_watch.next_key`).
    enter_accepts: bool = False


@dataclass(frozen=True)
class QuitSignature:
    """How this CLI's `SessionEnd` looks when the OPERATOR quit at the prompt (`/exit`), as opposed to every
    other ending — the evidence `runner-status.py console_exit` needs before it completes an interactive
    chat (`fleet_launch._lifecycle_chains` passes both fields as its argv).

    Measured per vendor in each adapter's quit signature. `reason` alone is enough only
    where the vendor's enum separates a keyboard quit from the rest (Claude's `prompt_input_exit`); where it
    does not (one vendor's reason also covers starting a new conversation, another's a SIGTERM/SIGHUP),
    `pane_marker` is text this CLI draws on its own pane ONLY while quitting at the operator's request,
    captured from inside the hook."""

    #: The `SessionEnd` payload's `reason` on an operator quit.
    reason: str
    #: Present on the runner's visible pane at `SessionEnd` time only on an operator quit; empty = the
    #: `reason` is sufficient on its own.
    pane_marker: str = ""


# The five priced token classes, as `session_sweep._TOKEN_CLASSES` (the fold's single home); names only.
@dataclass(frozen=True)
class TokenCounts:
    """One response's token spend, per class, plus the model that spent it.

    `model` is verbatim, never normalised (the CP reports an unpriced model as a visible gap). A provider
    with no usage accounting returns None from `read_usage`, never a zeroed `TokenCounts`.
    """

    model: str
    # Internal pricing bucket selected from this response's own prompt size. Kept separate from `model`
    # so the session still reports the vendor's exact model id while usage can retain a tiered rate.
    pricing_tier: str = ""
    input: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0


@dataclass(frozen=True)
class TurnError:
    """A runner's LAST turn ended on a vendor API error — `AgentAdapter.turn_error`'s answer.

    `marker` names that turn (the vendor's own turn/record id) so a reporter files each such turn once;
    `text` is the vendor's own words for the error, which `hooks/break_cause.classify_vendor_error` reads
    for a cause."""

    marker: str
    text: str


# ── Reading a file a RUNNER wrote ─────────────────────────────────────────────────────────────────
#
# Every read of a path under an account's config home goes through one of the functions below: a plain
# `open()` on a writerless FIFO never returns, a node-wide sweep outage for one `mkfifo`. A new shape is
# added there (`file_io.py`, the one home; re-exported here), not spelled at a call site. `open_regular`
# collapses every failure into None, the shape a reader wants; the credential probes need a third state
# (UNREADABLE is never "absent": the autoclose ladder reaps on absent), so that three-state probe is
# `credential_state` here, on the raw `_regular_fd`.
#
# A per-file bound is not a per-call bound: a runner planting 100k files would still kill the sweep, so a
# discovery bounds both counts (`MAX_TRANSCRIPT_SCAN` entries, `MAX_TRANSCRIPT_PROBES` opens).
#
# Newest-first makes a cap safe. PROBES gets it free (applied to an mtime-sorted list); SCAN does not (it
# aborts the walk in filesystem order). A date-bucketed adapter must sort each level DESCENDING and tell
# its caller whether the walk COMPLETED, so a pinning discovery can refuse a knowingly partial set.

#: Directory entries one discovery will `lstat`: busy accounts hold hundreds, far below outage scale.
MAX_TRANSCRIPT_SCAN = 20_000

#: Entries one discovery will OPEN for a header; the answer is within the newest few by construction.
MAX_TRANSCRIPT_PROBES = 256


class SessionTreeWalk:
    """Regular single-link files under a session's places, ONE entry budget (`MAX_TRANSCRIPT_SCAN`) across them.

    THE ONE HOME of every session/transcript directory walk (fenced by `tests/test_session_walk_fence.py`).
    `lstat` only: no symlink is followed, no FIFO or hard-linked file is returned (a runner could otherwise
    link the account's credential into its session dir); an absent place is empty. What the walk could
    not see goes on `shortfall` — the budget crossed (the walk stops there), a directory that would not
    list, and with `deep_is_short` a tree deeper than `max_depth` — so the caller refuses a knowingly
    partial set its own way (each adapter raises its own scan failure). A directory that vanishes mid-walk
    is a shortfall too: a scan racing a cleanup never passes a partial set off as the whole. What a
    listing yielded before it stopped (budget or error) is still walked.

    Options, each a caller's fact as data, never a vendor branch here:
      * `admit(path, lstat)` — the caller's filter, asked of every regular single-link file (False: not
        returned) and every directory below a place (False: not descended, so it costs no budget);
      * `newest_first` — each directory's entries visited in DESCENDING name order, so a date-bucketed tree
        (`YYYY/MM/DD`) cut short by the budget loses its oldest buckets first."""

    def __init__(self, root: str, *, deep_is_short: bool = False,
                 admit: Callable[[str, os.stat_result], bool] | None = None,
                 newest_first: bool = False) -> None:
        self.root = root
        self.deep_is_short = deep_is_short
        self.admit = admit
        self.newest_first = newest_first
        self.scanned = 0
        self.shortfall: list[str] = []

    def files(self, place: str, *, max_depth: int) -> list[str]:
        return [path for path, _st in self.stats(place, max_depth=max_depth)]

    def stats(self, place: str, *, max_depth: int) -> list[tuple[str, os.stat_result]]:
        """`files`, each with the `lstat` the walk took of it (its mtime, without a second, racing lookup)."""
        try:
            st = os.lstat(place)
        except OSError:
            return []
        if stat.S_ISREG(st.st_mode):
            return [(place, st)] if st.st_nlink == 1 and self._admitted(place, st) else []
        if not stat.S_ISDIR(st.st_mode):
            return []
        found: list[tuple[str, os.stat_result]] = []
        self._walk(place, max_depth, found)
        return sorted(found, key=lambda item: item[0])

    def _admitted(self, path: str, st: os.stat_result) -> bool:
        return self.admit is None or self.admit(path, st)

    def _spent(self) -> bool:
        return self.scanned > MAX_TRANSCRIPT_SCAN

    def _walk(self, directory: str, depth: int, found: list[tuple[str, os.stat_result]]) -> None:
        if self._spent():
            return  # already on `shortfall`; a later place lists nothing more
        # Counted WHILE listing, so a directory of millions of entries costs the budget, not memory.
        listed = []
        try:
            handle = os.scandir(directory)
        except OSError as exc:
            self.shortfall.append(f"the session directory {directory!r} could not be listed ({exc.strerror or exc})")
            return
        try:
            with handle as entries:
                for entry in entries:
                    self.scanned += 1
                    if self._spent():
                        self.shortfall.append(f"the session files under {self.root!r} exceed "
                                              f"{MAX_TRANSCRIPT_SCAN} entries — refusing to list further")
                        break
                    listed.append(entry)
        except OSError as exc:
            self.shortfall.append(f"listing {directory!r} stopped early ({type(exc).__name__}: {exc}) — "
                                  "this listing is PARTIAL")
        if self.newest_first:
            listed.sort(key=lambda item: item.name, reverse=True)
        for entry in listed:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            # ONE LINK ONLY: the CLIs never hardlink what they write, and a runner on this account could
            # otherwise hardlink the account's own credential into its session dir and have it kept forever.
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                if self._admitted(entry.path, st):
                    found.append((entry.path, st))
            elif stat.S_ISDIR(st.st_mode) and self._admitted(entry.path, st):
                if depth > 1:
                    # A spent budget lists nothing more; what this listing yielded is still collected.
                    if not self._spent():
                        self._walk(entry.path, depth - 1, found)
                elif self.deep_is_short:
                    self.shortfall.append(f"{entry.path!r} is nested deeper than a member name can carry")


from file_io import (  # noqa: E402,F401 — the one home moved to `file_io`; re-exported for its readers.
    # A test patches `file_io._regular_fd`, never `base`'s name: these call `file_io`'s binding.
    _REGULAR_READ_FLAGS, _regular_fd, final_path_under, json_object, open_regular, read_bounded)



# A credential's size bound when its adapter states none: the largest any adapter's `MAX_CREDENTIAL_BYTES`.
_RUNNER_READ_MAX_BYTES = 1024 * 1024


def credential_state(path: str, present: Callable[[], bool], *, runner_shape: str = "file",
                     max_bytes: int = _RUNNER_READ_MAX_BYTES) -> str:
    """Which of `agent_config_dirs`' three states a provider's credential file at `path` is in.

    `CREDS_UNREADABLE` is `PermissionError` alone — it is THERE and this process may not read it — and is
    never collapsed into `CREDS_NONE`, which the autoclose idle ladder reaps on (the false reap). Any other
    failure (absent, a symlink, a FIFO, a directory) is `CREDS_NONE`; a readable regular file is
    `CREDS_TOKEN` iff `present()` (the adapter's own shape check, `credential_file_present`) says so.
    `agent_config_dirs.creds_state` (Claude's file) answers through it too.

    A path this daemon cannot see (a Windows NodeLink store) is asked of the runner account instead, by
    `runner_shape` (`runner_credentials.SHAPE_*`) within `max_bytes` — never answered `none` from here."""
    import agent_config_dirs  # a daemon module: local, so this package stays importable without a cycle
    import runner_credentials  # noqa: PLC0415 — a daemon module, as above

    if runner_credentials.read_through_runner(path):
        return runner_credentials.credential_state(path, shape=runner_shape, max_bytes=max_bytes)
    try:
        os.close(_regular_fd(path))
    except PermissionError:
        return agent_config_dirs.CREDS_UNREADABLE
    except OSError:
        return agent_config_dirs.CREDS_NONE
    return agent_config_dirs.CREDS_TOKEN if present() else agent_config_dirs.CREDS_NONE


def credential_file_present(
    path: str, *, max_bytes: int, validate: Callable[[bytes], bool] | None = None,
    single_link: bool = False,
) -> bool:
    """Is the credential file at `path` there and shaped like a credential? The ONE credential-file read
    every adapter's presence check makes; the shape rule stays the adapter's (`validate`).

    With `validate`: `read_bounded` (`O_RDONLY|O_NOFOLLOW|O_NONBLOCK`, a regular file by `fstat`, at most
    `max_bytes`), then `validate(raw)` on a non-empty payload. Without it nothing is read: an `lstat` says a
    regular, non-empty file of at most `max_bytes` is there, so a credential this process may stat but not
    read (a session user's 0600 file) still counts. Either way a symlink, FIFO, directory, oversized or (with
    `single_link`) hardlinked file is False, and no FIFO can block the caller. Every refusal, a `validate`
    that raises included, is False: the caller polls again. Two-state; `credential_state` keeps
    `unreadable` apart."""
    if validate is None:
        try:
            info = os.lstat(path)
        except OSError:
            return False
        return (stat.S_ISREG(info.st_mode) and 0 < info.st_size <= max_bytes
                and (not single_link or info.st_nlink == 1))
    raw = read_bounded(path, max_bytes, single_link=single_link)
    if not raw:
        return False
    try:
        return bool(validate(raw))
    except Exception:  # noqa: BLE001 — a half-written or malformed document is "not yet", polled again
        return False


def read_head_lines(path: str, limit: int, lines: int = 1) -> list[bytes]:
    """Up to `lines` lines from the head of `path`, each capped at `limit` bytes — `[]` for anything that
    is not a plain readable regular file. For a line-oriented file a RUNNER wrote (a transcript).

    A few lines, not one, so a CLI writing anything before its metadata stays attributable.
    `readline(limit)` continues within an oversized line, so `lines * limit` bounds the call. `[]` for
    every failure ("cannot attribute", the safe direction).
    """
    fd = open_regular(path)
    if fd is None:
        return []
    try:
        head: list[bytes] = []
        with os.fdopen(fd, "rb", closefd=False) as handle:
            for _ in range(max(1, lines)):
                raw = handle.readline(limit)
                if not raw:
                    break
                head.append(raw)
        return head
    except OSError:
        return []
    finally:
        os.close(fd)


def read_tail_lines(path: str, limit: int) -> list[bytes]:
    """The COMPLETE lines within the last `limit` bytes of `path`, oldest first — `[]` for anything that
    is not a plain readable regular file. For asking how a transcript a RUNNER writes currently ENDS.

    A line cut by the seek is dropped rather than half-parsed; a file within the bound is read whole."""
    fd = open_regular(path)
    if fd is None:
        return []
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            size = os.fstat(fd).st_size
            if size > limit:
                handle.seek(size - limit)
                tail = handle.read(limit)
                # Everything up to the first newline is the remainder of a line the seek cut.
                cut = tail.find(b"\n")
                tail = tail[cut + 1:] if cut != -1 else b""
            else:
                tail = handle.read(limit)
    except OSError:
        return []
    finally:
        os.close(fd)
    return [line for line in tail.splitlines() if line.strip()]


def adapter_env(
    config_dir: str, account: Mapping[str, str] | Callable[[], Mapping[str, str]] | None = None,
    run: Mapping[str, str] | tuple[tuple[str, str], ...] = (),
) -> dict[str, str]:
    """An adapter's `env()`: THE ONE HOME of the rule every adapter's dispatch env follows.

    `account` is the account pin (a mapping, or a callable building one so nothing is derived from an empty
    `config_dir`): dropped whole when `config_dir` is empty. `run` is the dispatch's run-scoped variables,
    kept without an account (an API-key dispatch has none). Any variable without a value is OMITTED, never
    defined empty: a defined-but-empty pin resolves to `$HOME` or another account."""
    pinned = (account() if callable(account) else account or {}) if config_dir else {}
    return {name: value for name, value in (*dict(pinned).items(), *dict(run).items()) if value}


def count_int(value: object) -> int:
    """A token count read off a transcript, or 0. THE ONE HOME of that read for every adapter.

    `session_sweep._int` is deliberately separate (the Claude fold's pinned contract; differs on `true`).
    `null` and a float read as 0 (no rounding rule for a number nobody wrote); `bool` is excluded
    explicitly, since `True` would otherwise be priced as one token.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_folder_slot(worktree: str) -> bool:
    """Is `worktree` a FOLDER slot (a project with no repository) rather than a git tree — a
    `<jaah_home>/repos/<pid>/<n>` directory with no `.git` at its root?

    For a `prepare` step that would run git-aware tooling as the daemon, outside the runner's env and so
    outside the folder's `GIT_CEILING_DIRECTORIES` (`fleet_dispatch.folder_git_env`): there git discovery
    climbs out of the folder into an ancestor repository and the step would act on THAT repo. A pool
    worktree always has `.git` at its root; a path outside the pool layout is never answered True, so every
    other caller keeps today's behaviour. An unreadable folder (owner-only, as the daemon) reads as absent
    `.git`, which is the folder answer."""
    import environment  # noqa: PLC0415 — deferred: a leaf every adapter module already reaches

    root = environment.pool_root_or_empty().rstrip("/")
    if not worktree or not root:
        return False
    rel = os.path.relpath(os.path.realpath(worktree), os.path.realpath(os.path.join(root, "repos")))
    parts = rel.split(os.sep)
    if len(parts) != 2 or rel.startswith("..") or not parts[1].isdigit() or parts[1] == "0":
        return False
    return not os.path.lexists(os.path.join(worktree, ".git"))


#: The fleet harness's project-memory entries all open with this guard. Every
#: provider runs them (`capabilities.project_memory_hooks`), so a translating adapter wires them like any row.
MEMORY_HOOK_PREFIX = '[ -n "$JAAH_PROJECT_MEMORY" ] || exit 0;'


# ── The fleet harness, read by every adapter that TRANSLATES it ──────────────────────────────────────

def hub_root() -> str:
    """The root this daemon was installed from — the repo root in a checkout, `/usr/lib/jaahd` under the
    package. From this file, not `fleet_dispatch.HUB_ROOT` (import cycle; a test pins the two equal).
    `realpath`: the root may sit behind a symlink, and two spellings of one directory would name a bridge
    through an unresolved link."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))


def harness_settings_path() -> str:
    """`<hub_root>/infra/host/fleet-harness/settings.json` — the SINGLE HOME of the static hook layer."""
    return os.path.join(hub_root(), "infra", "host", "fleet-harness", "settings.json")


def tool_path(name: str) -> str:
    """`<hub_root>/tools/<name>` — a hook bridge or helper script by its installed path."""
    return os.path.join(hub_root(), "tools", name)


def hardened_python(script: str, *args: str) -> str:
    """`<node interpreter> <script> <args…>` for a hook bridge, hardened (defence in depth behind `QUARANTINED_DIRS`):
    the `LD_PRELOAD= LD_AUDIT= LD_LIBRARY_PATH=` prefixes occupy those names (empty is a glibc no-op), `-E`
    ignores `PYTHON*`, `-s` drops the runner-writable user site dir. A trusted workspace can make a
    checked-in env file authoritative for every unset variable, so the bridge also scrubs the family from
    the wrapped hook's env; both halves together are the fence. Each of `args` is shell-quoted."""
    import runner_env  # noqa: PLC0415
    from fleet_launch import shq  # in-function: fleet_launch imports this package

    return " ".join([f"LD_PRELOAD= LD_AUDIT= LD_LIBRARY_PATH= {shq(runner_env.python_cmd())} -E -s {shq(script)}",
                     *(shq(a) for a in args)])


def read_harness(path: str, vendor: str) -> dict:
    """The harness settings, or a RAISE naming what is missing — NOT fail-soft: the guards live in this
    layer, and a runner rendered with none would be indistinguishable from a governed one.

    One exception, by construction: a community-edition node ships no fleet harness (it and the `tools/` guards its
    hooks run stay in the private tree, `tools/community_edition/manifest.toml`), so there an ABSENT file is the
    empty layer, never an error. A present but broken one still raises.

    Its bare `python3` hook commands come back on the node's interpreter (`runner_env.rehome_python`), as the
    fleet-harness reconcile installs them for Claude."""
    import runner_env  # noqa: PLC0415

    try:
        with open(path, encoding="utf-8") as f:
            data = json.loads(runner_env.rehome_python(f.read(), runner_env.python_cmd()))
    except OSError as exc:
        import edition  # noqa: PLC0415 — read at call time (`apps/jaahd/edition.py`)

        if isinstance(exc, FileNotFoundError) and edition.IS_CE:
            return {}
        raise RuntimeError(
            f"{vendor} hooks: the fleet harness settings are not readable at {path} ({exc}); a runner "
            f"cannot be dispatched without them because the guard layer lives there") from exc
    except ValueError as exc:
        raise RuntimeError(
            f"{vendor} hooks: the fleet harness settings at {path} are not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{vendor} hooks: the fleet harness settings at {path} are not an object")
    return data


def harness_timeout(timeout: object, command: str, vendor: str) -> float | None:
    """One harness hook's `"timeout"` in SECONDS (Claude's unit), None when it declared none — or a RAISE.

    A non-positive timeout is REFUSED rather than floored: a hook killed on its first poll can never pass,
    which reads to an operator as a guard that is installed and simply always failing. The harness is
    human-edited, so the message names the hook and the fix. `bool` is refused as non-numeric (as
    `count_int` does)."""
    if timeout is None:
        return None
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise RuntimeError(
            f"{vendor} hooks: the fleet harness declares a non-numeric timeout {timeout!r} on the hook "
            f"{command[:60]!r}. Fix it to a positive number of SECONDS in "
            f"infra/host/fleet-harness/settings.json, or remove the key to declare no budget")
    if timeout <= 0:
        raise RuntimeError(
            f"{vendor} hooks: the fleet harness declares the timeout {timeout!r} on the hook "
            f"{command[:60]!r}, which can never pass (the hook is killed on its first poll). Fix it to a "
            f"positive number of SECONDS in infra/host/fleet-harness/settings.json, or remove the key")
    return float(timeout)


def harness_groups(
    settings: dict, *, vendor: str, events: dict[str, str | None], matchers: dict[str, str | None],
) -> Iterator[tuple[str, str, str | None, list[tuple[str, float | None]]]]:
    """The static harness layer's `hooks`, walked for a translating adapter: one
    `(claude_event, vendor_event, vendor_matcher, [(command, timeout_seconds)…])` per hook group. `vendor` is
    the adapter's catalogue `value`, which also names it in every refusal.

    Translated, not copied, so the fleet harness's static hook layer stays the single home of which
    guard runs on which tool. An unrecognised event or matcher RAISES and stops the dispatch: a
    passed-through Claude matcher is a guard that looks installed and never fires. One mapped to None
    (the vendor has no such moment or tool) is skipped."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for claude_event, groups in hooks.items():
        if claude_event not in events:
            raise RuntimeError(
                f"{vendor} hooks: the fleet harness declares the event {claude_event!r}, which this adapter "
                f"has no counterpart for. Add a row to `_HARNESS_EVENTS` (None if the vendor has no such "
                f"moment) — a runner must not be dispatched with a harness row silently missing")
        event = events[claude_event]
        if not event or not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher")
            if matcher is not None:
                if matcher not in matchers:
                    raise RuntimeError(
                        f"{vendor} hooks: the fleet harness matches on the tool {matcher!r}, which this "
                        f"adapter has no spelling for. Add a row to `_HARNESS_MATCHERS` (None if the vendor "
                        f"has no such tool) — a Claude tool name passed through would never fire")
                matcher = matchers[matcher]
                if matcher is None:
                    continue
            commands = []
            for hook in group.get("hooks") or ():
                if not isinstance(hook, dict) or hook.get("type") != "command":
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or not command:
                    continue
                commands.append((command, harness_timeout(hook.get("timeout"), command, vendor)))
            yield claude_event, event, matcher, commands


def _command_group(command: str, _row: tuple) -> dict:
    return {"hooks": [{"type": "command", "command": command}]}


def chain_hook_groups(chains: "LifecycleChains", events: tuple[tuple[str, ...], ...], *,
                      group: Callable[[str, tuple], Any] = _command_group) -> dict:
    """`{<event>: [group(<chain>, row)]}` for each SET chain, in `events`' order — THE one chain → event
    mapping every hook-rendering adapter uses. Each row starts `(LifecycleChains field, the vendor's event
    name)`; any further columns are the adapter's own data, handed to `group` with the row. The default
    `group` is the `{"hooks": [{"type": "command", "command": …}]}` shape the Claude-format files share."""
    hooks: dict = {}
    for row in events:
        command = getattr(chains, row[0])
        if command:
            hooks.setdefault(row[1], []).append(group(command, row))
    return hooks


def harness_entries(settings: dict, *, vendor: str, events: dict[str, str | None],
                    matchers: dict[str, str | None]) -> dict[str, list[dict]]:
    """The static harness layer's `hooks` as entries of a per-run hook file (`harness_dispatch`), keyed by the
    vendor's slot (`harness_groups` walks and refuses). A `session_start` slot holds bare commands; every other
    entry is `{matcher, claude_event, command[, timeout]}`, the timeout in whole SECONDS (Claude's unit)."""
    out: dict[str, list[dict]] = {}
    for claude_event, slot, matcher, commands in harness_groups(
            settings, vendor=vendor, events=events, matchers=matchers):
        for command, seconds in commands:
            entry: dict = {"command": command}
            if slot != "session_start":
                entry = {"matcher": matcher, "claude_event": claude_event, "command": command}
                if seconds is not None:
                    entry["timeout"] = max(1, int(round(seconds)))
            out.setdefault(slot, []).append(entry)
    return out


def _dispatch_entry(command: str, row: tuple[str, str, str]) -> str | dict:
    """One chain's hook-file entry: the bare command in the `session_start` slot, else an event entry."""
    _field_name, slot, claude_event = row
    if slot == "session_start":
        return command
    return {"matcher": None, "claude_event": claude_event, "command": command}


def harness_dispatch(chains: "LifecycleChains", *, vendor: str, chain_events: tuple[tuple[str, str, str], ...],
                     events: dict[str, str | None], matchers: dict[str, str | None],
                     tool_names: dict[str, str] | None = None,
                     host_lifecycle: tuple[str, ...] = ()) -> dict:
    """THE per-run hook file every in-band dispatch reads (`hook_bridge_core.py::dispatch`, through
    `$JAAH_HOOKS_FILE`): `{"session_start": [cmd…], "events": {slot: [entry…]}}` — the fleet `chains` first within
    each slot (on `Stop` the heartbeat runs before the quick gate), then the harness. The adapter contributes only
    declarations: `chain_events` rows `(LifecycleChains field, slot, the Claude event the command believes it
    runs under)`, the harness event and matcher maps `harness_groups` reads, and — for an engine whose payload
    reaches the dispatcher untranslated — `tool_names`, `{vendor tool name: Claude's}`, carried as the file's
    `tool_names` for `dispatch` to respell.

    `host_lifecycle` (an ACP launch's `AcpDialect.host_lifecycle`): the Claude events the ACP host runs itself
    (`host_lifecycle_dispatch`), left out here so none fires twice."""
    if host_lifecycle:
        chain_events = tuple(row for row in chain_events if row[2] not in host_lifecycle)
        events = {name: None if name in host_lifecycle else slot for name, slot in events.items()}
    hooks: dict = chain_hook_groups(chains, chain_events, group=_dispatch_entry)
    session_start: list[str] = hooks.pop("session_start", [])
    for slot, entries in harness_entries(read_harness(harness_settings_path(), vendor), vendor=vendor,
                                         events=events, matchers=matchers).items():
        if slot == "session_start":
            session_start.extend(e["command"] for e in entries)
        else:
            hooks.setdefault(slot, []).extend(entries)
    return {"session_start": session_start, "events": hooks, **({"tool_names": tool_names} if tool_names else {})}


#: The chain rows of the ACP host's lifecycle file: `(LifecycleChains field, slot, Claude event)`, each slot the
#: Claude event itself, the name `acp/lifecycle.py::HostLifecycle` dispatches by. `PreCompact` has no chain.
HOST_CHAIN_EVENTS: tuple[tuple[str, str, str], ...] = (
    ("on_session_start", "SessionStart", "SessionStart"),
    ("on_prompt_submit", "UserPromptSubmit", "UserPromptSubmit"),
    ("on_turn_end", "Stop", "Stop"),
    ("on_session_end", "SessionEnd", "SessionEnd"),
)


def host_lifecycle_dispatch(chains: "LifecycleChains", *, vendor: str, host_lifecycle: tuple[str, ...]) -> dict:
    """The ACP host's own hook file, the counterpart of `harness_dispatch(..., host_lifecycle=…)`: the `chains` and
    harness rows of the `host_lifecycle` events only, keyed by their Claude names, in the file shape `dispatch`
    reads. Nothing in it is per vendor: the host runs Claude's events, so no event or matcher map is needed."""
    hooks: dict = chain_hook_groups(chains, tuple(row for row in HOST_CHAIN_EVENTS if row[2] in host_lifecycle),
                                    group=_dispatch_entry)
    harness = read_harness(harness_settings_path(), vendor)
    events = {name: name if name in host_lifecycle else None for name in harness.get("hooks") or {}}
    for slot, entries in harness_entries(harness, vendor=vendor, events=events, matchers={}).items():
        hooks.setdefault(slot, []).extend(entries)
    return {"session_start": [], "events": hooks}


def acp_hook_files(chains: "LifecycleChains", *, vendor: str, dispatch: Callable[..., dict]) -> tuple[dict, dict]:
    """An ACP run's two hook files, split by the row's `AcpDialect.host_lifecycle`: the engine's (`dispatch`, the
    adapter's `harness_dispatch` partial, without the events the host runs) and the host's own
    (`host_lifecycle_dispatch`), so none fires twice. `vendor` is the catalogue value the dialect is read by."""
    import agent_providers  # noqa: PLC0415 — deferred: this module imports nothing of the daemon at load

    host = agent_providers.dialect_for(vendor).host_lifecycle
    return dispatch(chains, host_lifecycle=host), host_lifecycle_dispatch(chains, vendor=vendor, host_lifecycle=host)


# ── What every translating adapter declares alike, for its own tables to extend ─────────────────────
# Each adapter composes `{**<shared>, <its deltas>}` and spells only what its engine does differently; a row
# repeated in two adapters is a copy, and belongs here.

#: The chain rows of an in-band engine's hook file: `(LifecycleChains field, slot, the Claude event the command
#: believes it runs under)`. The lifecycle slots are Claude's names; `session_start` holds bare commands
#: (`harness_dispatch`). Absent: `on_error` — the ACP host classifies a failed turn itself.
ACP_CHAIN_EVENTS: tuple[tuple[str, str, str], ...] = (
    ("on_session_start", "session_start", "SessionStart"),
    ("on_prompt_submit", "UserPromptSubmit", "UserPromptSubmit"),
    ("on_turn_end", "Stop", "Stop"),
    ("on_session_end", "SessionEnd", "SessionEnd"),
    ("on_pre_tool", "PreToolUse", "PreToolUse"),
    ("on_post_tool", "PostToolUse", "PostToolUse"),
)

#: Claude Code's harness events -> the slot an in-band engine dispatches them under (`harness_groups`): Claude's
#: own names, but for `SessionStart`, the launch's `session_start` slot. `PostToolUseFailure` is None — the
#: failed-command memory nudge — until an engine reports a failed call apart from a succeeded one.
ACP_HARNESS_EVENTS: dict[str, str | None] = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "UserPromptSubmit",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "PostToolUseFailure": None,
    "Stop": "Stop",
    "SessionEnd": "SessionEnd",
    "PreCompact": "PreCompact",
}

#: The harness matchers no engine but Claude Code has a tool for (None = the row is dropped): the wakeup
#: scheduler.
CLAUDE_ONLY_MATCHERS: dict[str, None] = {
    "ScheduleWakeup": None,
}

#: The control server's settle tools as an ACP engine's payload spells them, `mcp__<server>__<tool>` over
#: `control_mcp_server`'s name — the matcher the settle guard fires on. `task_settle` is the pre-rename name,
#: still served unlisted (`agent_vocabulary.ALIASES`). An engine that respells the server name (one underscore,
#: a bridge that unwraps the call) keeps its own two rows instead.
CONTROL_TOOL_MATCHERS: dict[str, str] = {
    "mcp__plugin_jaah_control__task_complete": "mcp__control__task_complete",
    "mcp__plugin_jaah_control__task_settle": "mcp__control__task_settle",
}
#: Their inverse, the file's `tool_names` (`harness_dispatch`): the engine's spelling -> Claude's, so the settle
#: guard reads the name it compares.
CONTROL_TOOL_NAMES: dict[str, str] = {vendor: claude for claude, vendor in CONTROL_TOOL_MATCHERS.items()}


# How a vendor CLI hands a hook command to a shell on Windows — the adapter's datum, passed to
# `host_hook_commands`. Every fleet chain is bash; each value names the step that would mangle it there.
# The CLI runs hooks in the Git Bash it was given, but first collapses each `\\` pair to one.
WINDOWS_HOOK_SHELL_UNESCAPING_BASH = "unescaping-bash"
# The CLI runs hooks in its detected shell, PowerShell, never the Git Bash that launched it.
WINDOWS_HOOK_SHELL_POWERSHELL = "powershell"


def _powershell_runs_bash(command: str) -> str:
    """A bash hook COMMAND as the PowerShell line that runs it: Git Bash is named and handed the command
    base64-encoded (Windows PowerShell 5.1 passes a native argument's embedded `"` unescaped, and an encoded
    body leaves none to mangle). Sourced from a process substitution, not piped, so the hook's stdin is still
    the CLI's event JSON; `exit $LASTEXITCODE` carries bash's status out (PowerShell's own would flatten a
    blocking exit 2 to 1)."""
    from host_platform import windows  # noqa: PLC0415 — Windows-only

    encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
    bash = windows.git_bash().replace("'", "''")
    return f"& '{bash}' -c '. <(base64 -d <<<{encoded})'; exit $LASTEXITCODE"


_WINDOWS_HOOK_COMMAND: dict[str, Callable[[str], str]] = {
    # Doubling is the collapse's exact inverse: undoubled, the chains' escaped paths (`C:\\Program\ Files\\…`)
    # reached bash as a relative `C:Program FilesJaah…` and no fleet hook ever ran (a real PC).
    WINDOWS_HOOK_SHELL_UNESCAPING_BASH: lambda command: command.replace("\\", "\\\\"),
    WINDOWS_HOOK_SHELL_POWERSHELL: _powershell_runs_bash,
}


def host_hook_commands(hooks: dict, windows_shell: str) -> dict:
    """`hooks` (`{<event>: [{"hooks": [{"command": …}, …]}, …]}`), every command made runnable by the
    Windows hook shell `windows_shell` names (a `WINDOWS_HOOK_SHELL_*`) — the ONE Windows hook-command fix.
    Off Windows nothing changes, so Linux and macOS stay byte-identical. Mutates and returns `hooks`."""
    import host_platform  # noqa: PLC0415 — in-function, like every daemon-space import an adapter makes

    fix = _WINDOWS_HOOK_COMMAND[windows_shell]  # an unknown datum raises on every host, not only Windows
    if host_platform.is_windows():
        for hook in (h for groups in hooks.values() for g in groups for h in g["hooks"]):
            hook["command"] = fix(hook["command"])
    return hooks


def write_rendered_hooks(adapter: "AgentAdapter", *, settings_path: str, chains: "LifecycleChains",
                         mode: int | None = None, config_dir: str = "") -> str:
    """`adapter.render_hooks` plus the write to `settings_path`, returning the flag (empty when there
    was nothing to install) — shared by every vendor whose CLI takes its hook file wherever it is put.

    Written as the session user; a refused write raises rather than dispatching with no hooks. `mode` is
    passed through uninspected."""
    import session_user  # in-function, like every daemon-space import an adapter makes

    content, flag = adapter.render_hooks(
        settings_path=settings_path, chains=chains, config_dir=config_dir)
    if content is None:
        return ""
    kwargs = {} if mode is None else {"mode": mode}
    err = session_user.write_file_as_session_user(settings_path, content, **kwargs)
    if err:
        raise RuntimeError(f"write_settings: could not write {settings_path}: {err}")
    return flag


# How a runner's task prompt reaches the CLI: positional, a flag, or pasted into the pane once it is up.
TASK_ARGV_POSITIONAL = "argv_positional"
TASK_ARGV_FLAG = "argv_flag"
TASK_TMUX_PASTE = "tmux_paste"

# `assign` = the dispatcher mints the session id and tells the CLI; `discover` = the CLI mints its own and
# the node finds it. Load-bearing for the CP's `end_attempt` fence (reported vs recorded id).
SESSION_ASSIGN = "assign"
SESSION_DISCOVER = "discover"


#: The control server's ACP entry name for Claude Code: the `jaah` plugin's own server spelling, so its tools are
#: `mcp__plugin_jaah_control__*`, the names the prompt and the account's settle guard use. Claude Code drops a
#: plugin MCP server that runs the same command as a configured one, and the ACP entry runs exactly the plugin's
#: (`agent_layer.jaah_mcp_command`): named `control`, it left a Claude ACP session only `mcp__control__*`.
CLAUDE_CONTROL_SERVER_NAME = "plugin_jaah_control"
#: The names an ACP launch's control server may carry (`AcpLaunch` requires one).
CONTROL_SERVER_NAMES = frozenset({"control", CLAUDE_CONTROL_SERVER_NAME})


def control_mcp_server(command: str, args, env, *, name: str = "control") -> dict:
    """The Jaah control server as an ACP stdio entry (`env` as ACP's `[{name, value}]` list), under `name`
    (one of `CONTROL_SERVER_NAMES`). A credential in `env` is refused (ValueError naming the variable): the
    server gets its identity from the daemon."""
    from acp import launch

    if name not in CONTROL_SERVER_NAMES:
        raise ValueError(f"not a control server name: {name!r}")
    launch.validate_env(dict(env), where="control mcp server env")
    return {"name": name, "command": command, "args": list(args),
            "env": [{"name": str(k), "value": str(v)} for k, v in dict(env).items()]}


@runtime_checkable
class AgentAdapter(Protocol):
    """One agent CLI, as the fleet's launch/ingest paths need to know it.

    Every method is a place two vendors legitimately differ. Nothing here may branch on a vendor name —
    an adapter IS the branch, resolved once by `agents.get_adapter`.
    """

    #: The `accounts.provider` value this adapter serves — the registry key, and the only name by which
    #: any call site may ask for it (`agent_providers.PROVIDER_VALUES` is the vocabulary).
    provider: str

    def base_argv(self, *, model: str) -> tuple[str, ...]:
        """The CLI invocation, with the model choice already spelled this vendor's way.

        `model` empty ⇒ NO model flag at all. Element 0 is the executable as PATH finds it (`clauded_wt`
        preflights it with `shutil.which`), so a launcher prefix (`npx -y …`) would attest nothing."""
        ...

    def exec_line(
        self, *, sys_file: str, task_file: str, settings_flag: str,
        model_args: tuple[str, ...], session_args: tuple[str, ...], rc_flag: str = "",
    ) -> list[str]:
        """The `exec` lines of a dispatch's `launch.sh`, ready to join with newlines.

        The whole line, since charter and task prompt reach each CLI differently (see `task_delivery`).
        Values arrive shell-unsafe and every interpolated one is quoted (`fleet_launch.shq`), except
        `settings_flag`, already composed by `write_hooks`. `rc_flag` is ONE argv element, unused today
        but pinned by `tests/golden/claude_launch.sh`, and must be quoted like the rest."""
        ...

    def resume_exec_line(
        self, *, sys_file: str, settings_flag: str, model_args: tuple[str, ...], native_ref: str,
    ) -> "list[str] | None":
        """The `exec` lines of a `launch.sh` that reopens the engine's own session `native_ref` in this CLI's TUI,
        with no task prompt — the console handing a chat over to the terminal (`fleet_dispatch.launch_runner`'s
        `resume_ref`). Quoted like `exec_line`. None = this CLI's terminal cannot resume a session, so the
        catalogue offers no hand-off for it (`agent_providers.Console.toggle`)."""
        ...

    def task_delivery(self) -> str:
        """One of `TASK_ARGV_POSITIONAL` / `TASK_ARGV_FLAG` / `TASK_TMUX_PASTE` — how the task prompt
        reaches this CLI, so a paste-arranging caller need not parse argv."""
        ...

    def tui(self) -> "ComposerTui | None":
        """This CLI's on-screen vocabulary, or None for Claude Code, whose answer-delivery screen lives in
        `answer_delivery` itself (and whose task prompt rides the argv).

        Required for `TASK_TMUX_PASTE` (`answer_delivery.deliver_task_prompt` refuses a paste without
        one); asserted registry-wide by `test_every_paste_delivered_adapter_publishes_a_screen_to_paste_into`."""
        ...

    def consent_dialogs(self) -> "tuple[ConsentDialog, ...]":
        """The startup consent dialogs this CLI can open in a runner's pane, for `consent_watch` to accept
        when the pre-seeded consents missed. `()` for a CLI with none known."""
        ...

    def inherits_harness_hooks(self) -> bool:
        """Does this CLI already get the fleet's ACCOUNT-LEVEL hook layer, outside `LifecycleChains`?

        The layer is the fleet harness's static hook file (quick gate, context router, guards). True
        ⇒ do not compose them into the chains too (they would run twice); False ⇒ the chains are the only
        channel. A capability question, so `_lifecycle_chains` never branches on a vendor."""
        ...

    def additional_dirs_settings(self, dirs: "tuple[str, ...]") -> dict:
        """The settings that let a session of this CLI work in DIRS beside its cwd (a multi-repo slot's
        non-primary worktrees, `slot_repos.extra_dirs`), merged into the launch's settings file — or `{}` when
        this CLI's settings carry no such key."""
        ...

    def api_key_settings(self, helper: str) -> dict:
        """The settings an API-KEY launch of this CLI needs, given `helper` (a shell command printing the
        key) — or `{}` when this CLI takes the key from its environment. Read only when `api_key_env()` is
        empty (`fleet_dispatch.api_key_helper_command`)."""
        ...

    def api_key_env(self) -> str:
        """The variable an API-KEY launch exports the fetched key under (`fleet_dispatch.build_auth_lines`),
        or `""` when this CLI takes its key from `api_key_settings`' helper instead — the one decision
        between the two routes, so no launch site branches on a vendor."""
        ...

    def api_key_argv(self) -> tuple[str, ...]:
        """Extra argv an API-KEY launch of this CLI needs so it reads `api_key_env()` instead of opening
        its sign-in screen, or `()`. Shell-unsafe elements; the exec line quotes them."""
        ...

    def api_key_prepare(self, *, config_dir: str) -> None:
        """What an API-KEY launch needs on disk in its token-less account home before the CLI starts
        (a setting selecting key auth), or nothing. Never writes the key; never raises."""
        ...

    def account_layer(self) -> str:
        """Which account layer this CLI's home carries — its catalogue row's `capabilities.account_layer`.

        "omc" = the node's Claude Code layer (`provision_accounts` + `plugin_repair`: Claude settings and a
        `plugins/` tree of marketplace checkouts, which another CLI may import and break on) is converged
        into it. Anything else = leave the home to `prepare` + `register_mcp` and this vendor's own
        `account_layer_converge`."""
        ...

    def account_layer_refresh(self, run, *, prefix: str) -> dict:
        """This vendor's own NODE-LEVEL package refresh, once per online sweep, before any account's
        `account_layer_converge` — the packages its account layer is copied from, installed under
        `prefix` (the session user's `~/.local`). `{"applied": bool, "skipped": str, "error": str}`; a
        vendor with no such layer answers `skipped`. `run(argv, *, env=None, timeout=None, cwd=None)` returns
        `{"exit_code", "stdout", "stderr"}`; a missing binary is exit -1, never a raise."""
        ...

    def account_layer_converge(self, *, config_dir: str, run, offline: bool) -> dict:
        """This vendor's own PER-ACCOUNT layer, run by the sweep for an account whose
        `account_layer()` is not "omc" — what the Claude Code layer is to a Claude home. At least
        `{"present": bool, "error": str}` (`present` re-asked of `account_layer_missing` after the
        writes); the rest is the vendor's own detail. `offline` = touch no network. Never raises."""
        ...

    def account_layer_missing(self, *, config_dir: str) -> str:
        """"" when this vendor's account layer is present in `config_dir`, else the FIRST absent marker
        — the sweep's scheduling predicate and its verdict after a converge. Read-only, no fork."""
        ...

    def env(self, *, config_dir: str, worktree: str) -> dict[str, str]:
        """The environment variables this CLI needs for THIS DISPATCH — the account pin among them.

        `config_dir` is `{jaah_home}/auth/{slug}`, mapped onto whatever the vendor understands.
        Never an empty VALUE: an account-scoped variable is OMITTED when `config_dir` is empty (a defined-
        but-empty one resolves to `$HOME` or another account). A RUN property is not dropped with the pin:
        an API-key dispatch has no `config_dir`, and losing e.g. an auto-approve variable stalls the
        runner silently at its first tool call."""
        ...

    def prepare(self, *, worktree: str, config_dir: str, project: str) -> None:
        """Anything that must exist on disk before this CLI is exec'd into `worktree`. On the dispatch
        path: fast and idempotent."""
        ...

    def write_hooks(self, *, settings_path: str, chains: LifecycleChains,
                    mode: int | None = None, config_dir: str = "") -> str:
        """Render `chains` this vendor's way, WRITE it, and return the flag that installs it (empty when
        there is nothing to install), via `session_user` (the runner uid reads it back). `mode` is the
        caller's, passed through untouched (`None` = seam default). `config_dir` is for a vendor that
        loads hooks from one path under its own home and ignores `settings_path`; empty = no account."""
        ...

    def render_hooks(self, *, settings_path: str, chains: LifecycleChains,
                     config_dir: str = "") -> tuple[str | None, str]:
        """`write_hooks` without the write: `(file content or None, flag)`.

        Pure, for `fleet_launch.build_settings`, whose output is asserted against
        `tests/golden/claude_settings.json`."""
        ...

    def quit_signature(self) -> QuitSignature:
        """What this CLI's `SessionEnd` looks like when the operator quit at the prompt — see
        `QuitSignature`. An interactive chat is completed on exactly that and nothing else."""
        ...

    def register_mcp(self, *, config_dir: str) -> None:
        """Make the control-plane MCP server reachable from this account's CLI (architecture A9).

        Tool-name derivation differs per vendor (`agent_providers`' `mcp_tool_prefix`). A no-op when the
        registration already ships with the account's configuration."""
        ...

    def session_binding(self) -> str:
        """`SESSION_ASSIGN` or `SESSION_DISCOVER` — see those constants."""
        ...

    def discover_session_id(self, *, config_dir: str, worktree: str, since: float) -> str | None:
        """For a `SESSION_DISCOVER` vendor: the id of the session this CLI started after `since`
        (epoch seconds), or None when it cannot be told apart yet. Never called for `SESSION_ASSIGN`.

        `None` means "looked, nothing there" and is reported terminally; a partial enumeration must RAISE
        (retryable in `session_sweep`). A cap shedding provably-older candidates of a sorted list may
        still answer; one shedding unreached entries may not. The caller pins the first id resolved, so a
        wrong answer is permanent."""
        ...

    def credential_state(self, *, config_dir: str) -> str:
        """One of `agent_config_dirs.CREDS_TOKEN` / `CREDS_NONE` / `CREDS_UNREADABLE` for this account.

        No adapter may collapse UNREADABLE into NONE: a kill-safety gate doing so reaps a live runner."""
        ...

    def transcript(self, *, config_dir: str, worktree: str, session_id: str) -> str | None:
        """Path to this session's transcript, or None when the vendor records none / it is not there yet.
        None is terminal, as in `discover_session_id`: an incomplete enumeration RAISES. A match found
        despite truncation is still returned (truncation can cost a match, never invent one)."""
        ...

    def transcript_root(self, *, config_dir: str) -> str:
        """The ONE directory under `config_dir` holding this vendor's transcripts — the containment root
        for `transcript()`'s answer.

        A security seam: containing against the account home would put the stored OAuth credential one
        runner-planted hardlink away from the forever-kept transcript bucket (whose redaction cannot match
        an opaque token). A `SESSION_DISCOVER` adapter MUST narrow it; an ASSIGN one never reaches the
        vendor containment leg (`session_sweep._vendor_transcript`)."""
        ...

    def session_members(self, *, config_dir: str, transcript: str) -> list[tuple[str, str]]:
        """Every OTHER file the vendor itself wrote for the session whose main record is `transcript` —
        `[(member name, path)]`, so the capture is the vendor's whole record and not one file of it.

        Two name shapes, nothing else: `subagent-<id>.jsonl.gz` for a sub-agent's own thread (its spend is
        folded like the main file's) and `vendor/<path relative to transcript_root>.gz` for any other
        session file (uploaded verbatim, never folded). Every path lies under `transcript_root`; the sweep
        re-checks that. An ASSIGN adapter answers `[]` (the sweep reads Claude's sub-agents itself). An
        enumeration that could not complete RAISES — the sweep records the omission on the manifest rather
        than let a partial set pass as the whole."""
        ...

    def turn_error(self, *, transcript: str) -> TurnError | None:
        """The error the session's LAST turn ended on, when that turn ended on a vendor API error after the
        CLI's own retries — else None (still working, ended cleanly, or unreadable: never a guess).

        The node-side stand-in for Claude Code's `StopFailure` on a CLI that fires no hook on an API-error
        turn (`vendor_turn_errors`). Read off the vendor's OWN structured error record only, never prose,
        so a runner merely working on the subject of errors is never condemned. An adapter whose CLI has a
        `StopFailure` hook answers None: the hook already reports it."""
        ...

    def read_usage(self, line: bytes) -> TokenCounts | None:
        """One transcript line's token spend, or None when that line reports none (which is most lines,
        and every line for a vendor with no usage accounting).

        A line naming a model but spending nothing MAY answer an all-zero `TokenCounts` with that model,
        for transcripts that put model and counts on different lines (the fold carries the model forward;
        `usage_by_model()` drops empty buckets). Distinct from the no-accounting rule, which answers None."""
        ...


class AdapterDefaults:
    """The most common answer to each of `AgentAdapter`'s trivially-constant members, for adapters to
    subclass and override where they differ. Not a `Protocol` subclass, so a missing method stays missing."""

    #: The catalogue `value` and the account-pin environment (`AgentAdapter.provider`, `.env`), which the defaults
    #: below read. Annotations only, as `base_command`: each adapter sets them, and a missing one stays missing.
    provider: str
    env: Callable[..., dict[str, str]]

    #: The vendor's invocation before the model flag (element 0 the attested binary) — what `base_argv`
    #: extends. Annotation only, no default: an adapter that keeps the default `base_argv` and forgets
    #: this fails loudly, never with an argv that attests nothing.
    base_command: tuple[str, ...]

    #: What the four constant accessors answer — `task_delivery`, `tui`, `quit_signature`, `api_key_env`.
    #: Annotations only, like `base_command`: each adapter sets the four beside the comment saying why.
    task_delivery_kind: str
    tui_vocabulary: "ComposerTui | None"
    quit_sig: QuitSignature
    api_key_variable: str

    #: `register_mcp`'s `mcp add` invocation: the vendor's binary, and the scope arguments passed only
    #: when there are any (`()` leaves `agent_layer.register_control_mcp`'s own default).
    mcp_binary: str
    mcp_scope_args: tuple[str, ...] = ()

    #: The `login.LoginAdapter` class whose `credential_path` + `credential_present` the default
    #: `credential_state` reads. An adapter with its own probe (`agent_config_dirs.creds_state`, or an
    #: `lstat` that never reads the file) overrides the method instead.
    login_adapter: type

    def base_argv(self, *, model: str) -> tuple[str, ...]:
        """`base_command` plus `--model <model>` when there is one."""
        return self.base_command + (("--model", model) if model else ())

    def task_delivery(self) -> str:
        return self.task_delivery_kind

    def resume_exec_line(self, *, sys_file: str, settings_flag: str, model_args: tuple[str, ...],
                         native_ref: str) -> "list[str] | None":
        return None

    def tui(self) -> "ComposerTui | None":
        return self.tui_vocabulary

    def quit_signature(self) -> QuitSignature:
        return self.quit_sig

    def api_key_env(self) -> str:
        return self.api_key_variable

    def write_hooks(self, *, settings_path: str, chains: LifecycleChains,
                    mode: int | None = None, config_dir: str = "") -> str:
        """`render_hooks` plus the write, `base.write_rendered_hooks`."""
        return write_rendered_hooks(
            self, settings_path=settings_path, chains=chains, mode=mode, config_dir=config_dir)

    def inherits_harness_hooks(self) -> bool:
        return True

    def consent_dialogs(self) -> "tuple[ConsentDialog, ...]":
        return ()

    def api_key_settings(self, helper: str) -> dict:
        return {}

    def additional_dirs_settings(self, dirs: "tuple[str, ...]") -> dict:
        return {}

    def api_key_argv(self) -> tuple[str, ...]:
        return ()

    def api_key_prepare(self, *, config_dir: str) -> None:
        return None

    def account_layer(self) -> str:
        import agent_providers  # noqa: PLC0415 — deferred: this module imports nothing of the daemon at load

        return agent_providers.account_layer(self.provider)

    def account_layer_refresh(self, run, *, prefix: str) -> dict:
        return {"applied": False, "skipped": "no vendor account layer", "error": ""}

    def account_layer_converge(self, *, config_dir: str, run, offline: bool) -> dict:
        return {"present": True, "error": "", "skipped": "no vendor account layer"}

    def account_layer_missing(self, *, config_dir: str) -> str:
        return ""

    def register_mcp(self, *, config_dir: str) -> None:
        """Re-assert the control-plane MCP server on this account with `mcp_binary`'s own `mcp add`
        (`_register_control_mcp`). Non-raising: a degraded agent layer, never a stranded dispatch."""
        if not config_dir:
            return None
        try:
            self._register_control_mcp(config_dir=config_dir)
        except Exception:  # noqa: BLE001 — non-raising by contract; nothing is re-raised
            return None
        return None

    def _register_control_mcp(self, *, config_dir: str) -> dict:
        """`agent_layer.register_control_mcp` under the full `env()`, so the registration lands in THIS
        account's store; its result, for an adapter that acts on it. Raises what the call raises."""
        from . import agent_layer  # a daemon-facing sibling: local, so this module stays importable alone

        return agent_layer.register_control_mcp(
            binary=self.mcp_binary, config_dir=config_dir, env=self.env(config_dir=config_dir),
            **({"scope_args": self.mcp_scope_args} if self.mcp_scope_args else {}))

    def session_binding(self) -> str:
        return SESSION_DISCOVER

    def discover_session_id(self, *, config_dir: str, worktree: str, since: float) -> str | None:
        """None: an ASSIGN vendor is never asked, and an ACP engine's `sessionId` is stored by the host."""
        return None

    def session_members(self, *, config_dir: str, transcript: str) -> list[tuple[str, str]]:
        """`[]`: no sub-agent threads of the vendor's own to upload beside the main record."""
        return []

    def credential_state(self, *, config_dir: str) -> str:
        """Which of `agent_config_dirs`' three states `login_adapter`'s credential file is in —
        `base.credential_state` over its path and shape check. Not `agent_config_dirs.creds_state`
        (Claude's file). `unreadable` is never collapsed into `none`: the autoclose idle ladder acts on
        `none`, so that would be a false reap."""
        login = self.login_adapter()
        return credential_state(login.credential_path(config_dir=config_dir),
                                lambda: login.credential_present(config_dir=config_dir))

    def turn_error(self, *, transcript: str) -> TurnError | None:
        return None


class LoginAdapterBase:
    """What a vendor's `login.LoginAdapter` answers OFF ITS CATALOGUE ROW, for the login adapters to subclass:
    `row` is the `agent_providers.AgentProvider`, and `provider`, `completion`, `credential_path` and the agent-layer
    `finalize` read it. Each vendor keeps what it MEASURED (`login_argv`, `login_env`, `auth_url_re`,
    `credential_present`, a refusal it has a code for). The seam itself stays structural (`agents/login.py`)."""

    #: The catalogue row (`agent_providers.<VENDOR>`); `provider` (the registry key) is derived from it by
    #: `__init_subclass__`, never spelled again.
    row: ClassVar[Any]
    provider: str  # not `ClassVar`: the seam declares it an instance attribute, and a Protocol match is exact
    #: The members each vendor MEASURES, declared so the seam's shape is one class (`login.LoginAdapter`); annotations
    #: only, so a vendor that forgets one is still an `AttributeError` the registry test catches, never a stub.
    login_argv: Callable[[], tuple[str, ...]]
    login_env: Callable[..., dict]
    auth_url_re: Callable[[], re.Pattern]
    credential_present: Callable[..., bool]
    #: Optional attribute (`agents/login.py`): the login machine's own steps finish this login, so `finalize` is
    #: the machine's (`accounts.py::_finalize_account_add`) and this class's is a no-op.
    machine_finished: bool = False

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        cls.provider = cls.row.value

    def completion(self) -> str:
        """The row's `login_completion`, DECLARED — never inferred from the URL: a device-auth login is
        non-loopback yet completes on disk. The catalogue field keeps this node and the CP's twin identical."""
        return self.row.login_completion

    def refusal(self, log_tail: str) -> str | None:
        """None: no refusal a wait cannot fix was measured for this vendor (a vendor with one declares a closed
        `REFUSAL_CODES` set in its module and overrides this). Nothing is read from `log_tail`."""
        del log_tail
        return None

    def credential_path(self, *, config_dir: str) -> str:
        """`<config_dir>/<row.credential_file>`, the one file a completed login writes. Pure path arithmetic:
        called before the dir exists, so it never stats."""
        return os.path.join(config_dir, self.row.credential_file)

    def finalize(self, *, config_dir: str) -> None:
        """`finalize_steps`, contained: idempotent and NEVER raises — an exception would strand a good credential —
        and the agent-layer import sits inside the `try` too, so a broken `agents/agent_layer.py` can neither
        escape a method typed non-raising nor make the login registry uncollectable. Silent: the layers log."""
        if self.machine_finished:
            return None
        try:
            self.finalize_steps(config_dir=config_dir)
        except Exception:  # noqa: BLE001 — see the docstring; nothing is re-raised
            return None
        return None

    def finalize_env(self, *, config_dir: str) -> dict:
        """The env overlay `finalize_steps` runs the agent layer under: the login's own by default. A vendor
        whose login overlay carries TUI-only variables narrows it to the store overlay."""
        return self.login_env(config_dir=config_dir)

    def finalize_steps(self, *, config_dir: str) -> None:
        """The shared PROVIDER agent layer (`agent_layer.finalize_provider_account`: native MCP registration, then
        the skills root) against the row's binary, under `finalize_env`. A vendor with a tail of its own extends
        it, after `super()`; it may raise freely, `finalize` contains it."""
        from . import agent_layer  # a daemon-facing sibling: local, so this module stays importable alone

        agent_layer.finalize_provider_account(
            binary=self.row.binary, config_dir=config_dir, env=self.finalize_env(config_dir=config_dir))


class IncompleteSessionMembers(Exception):
    """`session_members` could not see every candidate, and CARRIES what it did verify (`members`).

    A cap on a busy account must cost the members it could not reach, never the ones it found; the sweep
    uploads `members` and names the shortfall on the manifest."""

    def __init__(self, message: str, members: list[tuple[str, str]]) -> None:
        super().__init__(message)
        self.members = members


# ── The ACP launch grammar (architecture §3.4b) ──────────────────────────────────────────────────────
# An OPTIONAL adapter member, `acp_launch(ctx: AcpLaunchContext) -> AcpLaunch`: a provider that has one can run
# under `jaah-acp-host` when the dispatch's `runtime` fact says `acp` (`acp_runtime.py`). Not in
# `ADAPTER_MEMBERS`: a provider without it runs the legacy path whatever the switch says.


@dataclass(frozen=True)
class AcpLaunchContext:
    """What a provider's `acp_launch` is told about one run. Paths are as the engine will see them (guest paths
    on a MicroVM)."""

    task_id: str
    session_id: str
    worktree: str
    #: The account's config home; `""` when the run has none.
    config_dir: str
    project: str
    model: str
    #: An API-key account's slug, never the key; `""` for OAuth.
    api_key_account: str
    additional_dirs: tuple[str, ...] = ()
    #: The credential-helper COMMAND an API-key run takes its key from (`fleet_dispatch.api_key_helper_command`):
    #: it names the credential, never holds it. `""` for OAuth and for an adapter whose key rides another way.
    key_helper: str = ""
    #: The Architectural Guard is on for this run (its `JAAH_GUARD_*` env rides `launch.sh`).
    guard: bool = False
    #: A Jaah-account run's plan (`bedrock_launch.BedrockPlan`), None otherwise: an adapter that can run on the
    #: Jaah account routes its credential-export command and non-secret env through its own settings.
    bedrock: Any = None
    #: A MicroVM guest's launch (`microvm_session.model_launch_files`): `worktree` and `config_dir` are the guest's,
    #: so nothing of them is on this host, and an API-key run signs in through `AcpLaunch.authenticate`.
    guest: bool = False


@dataclass(frozen=True)
class AcpLaunch:
    """How one provider's ACP agent is started; `launch.json` carries the first four fields and `authenticate`
    under the same names."""

    #: The ACP agent command: the engine binary with its ACP flag.
    argv: tuple[str, ...]
    #: Config home and the like; never an API key value (a key reaches the engine by helper, stdin or file).
    env: Mapping[str, str]
    #: Merged into ACP `session/new` `_meta`.
    session_meta: Mapping[str, Any]
    #: ACP `McpServer` entries; the Jaah control server among them.
    mcp_servers: tuple[Mapping[str, Any], ...]
    #: Pre-launch file convergence, run by the node before the host is spawned, where `adapter.prepare` runs on
    #: the legacy path. Never serialised.
    prepare: Callable[[], None] | None = None
    #: launch.json `authenticate` (`acp.launch.AUTHENTICATE_FIELDS`): the host signs in with the run's API-key account
    #: before the session, the key read from the daemon (`runtime.account_key`). Names the method, never a key.
    authenticate: Mapping[str, str] | None = None
    #: The ACP host's own lifecycle hook file (`host_lifecycle_dispatch`), for a row declaring
    #: `AcpDialect.host_lifecycle`: launch.json carries it as `runtime.lifecycle_hooks`. None = the host runs none.
    host_hooks: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        """Refused at construction, not discovered at settle time: a launch without the Jaah control server
        (the engine could not report its task), or with a credential in its env, an MCP server's env or a
        session_meta key (`acp.launch.validate_secrets`, a ValueError naming the variable, never the value)."""
        if not any(isinstance(s, Mapping) and s.get("name") in CONTROL_SERVER_NAMES for s in self.mcp_servers):
            raise ValueError("AcpLaunch needs the Jaah control MCP server (an entry named one of "
                             f"{sorted(CONTROL_SERVER_NAMES)})")
        from acp import launch  # noqa: PLC0415 — stdlib-only; keeps this module free of package imports

        launch.validate_secrets(dict(self.env), self.mcp_servers, dict(self.session_meta))


# The protocol's member list, for the conformance test.
ADAPTER_MEMBERS: tuple[str, ...] = (
    "provider", "base_argv", "exec_line", "resume_exec_line", "task_delivery", "tui", "consent_dialogs", "inherits_harness_hooks",
    "api_key_settings", "api_key_env", "api_key_argv", "api_key_prepare",
    "account_layer", "account_layer_refresh", "account_layer_converge",
    "account_layer_missing", "env", "prepare", "write_hooks",
    "render_hooks", "quit_signature", "register_mcp", "session_binding", "discover_session_id",
    "credential_state",
    "transcript", "transcript_root", "session_members", "read_usage", "turn_error",
)

__all__ = [
    "AcpLaunch", "AcpLaunchContext",
    "AdapterDefaults", "AgentAdapter", "ComposerTui", "ConsentDialog", "LifecycleChains", "LoginAdapterBase",
    "QuitSignature", "TokenCounts", "TurnError",
    "ADAPTER_MEMBERS",
    "IncompleteSessionMembers", "SessionTreeWalk",
    "control_mcp_server", "CLAUDE_CONTROL_SERVER_NAME", "CONTROL_SERVER_NAMES", "count_int", "json_object", "read_bounded", "read_head_lines", "read_tail_lines", "write_rendered_hooks",
    "chain_hook_groups", "harness_dispatch", "harness_entries", "host_lifecycle_dispatch", "acp_hook_files",
    "HOST_CHAIN_EVENTS", "ACP_CHAIN_EVENTS", "ACP_HARNESS_EVENTS", "CLAUDE_ONLY_MATCHERS", "CONTROL_TOOL_MATCHERS",
    "CONTROL_TOOL_NAMES", "host_hook_commands", "WINDOWS_HOOK_SHELL_UNESCAPING_BASH", "WINDOWS_HOOK_SHELL_POWERSHELL",
    "MAX_TRANSCRIPT_SCAN", "MAX_TRANSCRIPT_PROBES",
    "TASK_ARGV_POSITIONAL", "TASK_ARGV_FLAG", "TASK_TMUX_PASTE",
    "SESSION_ASSIGN", "SESSION_DISCOVER",
]
