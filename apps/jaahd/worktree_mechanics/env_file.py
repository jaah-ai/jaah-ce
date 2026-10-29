"""A slot's `.env` file I/O — the cached reader, the private-mode writer, tree-mode hardening and the `KEY=value` edit primitives."""
from __future__ import annotations

import logging
import os
import re
import stat

import host_platform
import session_user

log = logging.getLogger("jaahd.worktree_mechanics.env_file")


# ── A slot's `.env` is a SECRET FILE, and the uid split is how the daemon still reaches it ──────────────
# `.env` carries real secrets. `0600` excludes the world, the daemon uid and group `jaah` from the FILE
# — not the content in flight (a delegated read/write crosses the group-readable exec spool). NOT a
# boundary between runners: every session is uid `jaah` — G1's accepted intra-org residual.
# The daemon therefore reads/writes via `env_text`/`_env_write_text` (delegated fallback), cached on stat
# identity and batched, because reconcile senses every slot's ports every tick (a tmux round trip each).
WORKTREE_ENV_MODE = 0o600

# Files in a leased worktree whose mode this module owns. Tracked files are eligible: `_run_git` delegates
# every git call to the session user (the owner), git records only the EXECUTE bit (a 0664→0600 chmod
# leaves `git status` clean), and a checkout that RECREATES a file does so at the umask — which is why
# this table is converged AFTER `reset_to_base` on every claim, not once at create time.
WORKTREE_PRIVATE_MODES: "tuple[tuple[str, int], ...]" = (
    (".env", WORKTREE_ENV_MODE),
    (".env.local", WORKTREE_ENV_MODE),
    # Hook configuration the session EXECUTES. Nothing outside the worktree reads it (`session_sweep`
    # uploads `_meta/<ref>.settings.json`, not this), so no group read; the mode stops a group member
    # rewriting it, git the content (reset every claim; a tamper is a dirty tree).
    (".claude/settings.json", 0o600),
    # The CLI's per-project permission allowlist: group-writable = a grant a group member can widen.
    (".claude/settings.local.json", 0o600),
)

# How a `.env` is opened as text. Empty on posix (today's locale default, byte-identical). On Windows,
# UTF-8 — the bundle's Python (3.14) would otherwise read cp1252 and raise on a UTF-8 secret — and, for the
# write, `newline=""`: text mode there turns every `\n` into `\r\n`, and a CRLF `.env` hands compose and
# Git Bash's `source` values ending in `\r`.
_ENV_READ_KW: "dict[str, str]" = {"encoding": "utf-8"} if host_platform.is_windows() else {}
_ENV_WRITE_KW: "dict[str, str]" = {"encoding": "utf-8", "newline": ""} if host_platform.is_windows() else {}

# path → (stat identity, text). Bounded by the number of worktrees on the node.
_ENV_TEXT_CACHE: "dict[str, tuple[tuple[int, int, int], str]]" = {}


def _env_stat_key(path: str) -> "tuple[int, int, int]":
    """The file identity a cached read is keyed on. `os.stat` is the ONE never-delegated operation: a mode
    gates content, not metadata, so the daemon can stat a `0600` `.env` itself. The inode is in the key
    because a whole-file rewrite via staged `cp` can land inside one mtime tick."""
    st = os.stat(path)
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _env_read_delegated(path: str) -> str:
    """`cat PATH` as the session user — the read path for a `.env` this uid may not open.

    RAISES on every failure, never returns "": the port scan reads "no assignment" as "port free", so a
    silent "" would hand two slots the same port. `PermissionError` is an `OSError`, so callers' existing
    `except OSError` keeps catching it."""
    if not session_user.switch_needed():
        raise PermissionError(f"{path} is unreadable by this uid and no session-user switch applies")
    try:
        proc = session_user.run_as_session_user(["cat", "--", path], timeout=30)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch unavailable), TimeoutExpired, OSError
        raise PermissionError(f"delegated read of {path} failed: {exc}") from exc
    if proc.returncode != 0:
        raise PermissionError(
            f"delegated read of {path} returned rc {proc.returncode}: "
            f"{(proc.stderr or '').strip() or '(no stderr)'}")
    return proc.stdout or ""


def env_text(path: str) -> str:
    """A worktree `.env`'s text, capped at `_MAX_ENV_BYTES` — direct where this uid may read, delegated
    where it may not. Raises `OSError` like `open`. THE ONE READER of a pool `.env` in the daemon
    (`reconcile.context.cpn_of` included): a second `open()` stops working the day
    `WORKTREE_ENV_MODE` is honoured. A file that HITS the cap is announced — an assignment past it is a
    port two slots can both be handed."""
    try:
        key = _env_stat_key(path)
    except OSError:
        _ENV_TEXT_CACHE.pop(path, None)
        raise
    cached = _ENV_TEXT_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        with open(path, **_ENV_READ_KW) as fh:
            text = fh.read(_MAX_ENV_BYTES)
    except PermissionError:
        text = _env_read_delegated(path)[:_MAX_ENV_BYTES]
    if len(text) >= _MAX_ENV_BYTES:
        log.warning(
            "%s is larger than %d bytes and was read only that far — a port assigned past that point is "
            "invisible to the collision scan. Trim the file", path, _MAX_ENV_BYTES,
        )
    _ENV_TEXT_CACHE[path] = (key, text)
    return text


def _env_text_for_rewrite(path: str) -> str:
    """A `.env`'s text for a WHOLE-FILE REWRITE — refusing a read `env_text` cut short. A READ CAP IS NOT
    A WRITE CAP: a capped read followed by a rewrite silently deletes everything past the cap, i.e. the
    rest of the slot's secrets. Raises rather than truncating."""
    text = env_text(path)
    if len(text) >= _MAX_ENV_BYTES:
        raise OSError(
            f"{path} is at or past the {_MAX_ENV_BYTES}-byte read cap, so it cannot be rewritten without "
            "losing everything past that point — refusing to write it at all. Trim the file")
    return text


def _env_write_text(path: str, text: str) -> None:
    """Replace a worktree `.env`'s WHOLE text, delegating when this uid may not write it. Raises `OSError`
    on failure: a port band that could not be written is not a no-op.

    The direct leg REWRITES AN EXISTING INODE and inherits its mode; only the delegated leg forces
    `WORKTREE_ENV_MODE` (`cp && chmod`). Fine while every caller writes a file `env_text` just read — a
    caller that CREATES a `.env` here would land `0664` under `UMask=0002`. Whole-file, never patch: the
    delegated leg stages and `cp`s, so a partial write has no counterpart there."""
    _ENV_TEXT_CACHE.pop(path, None)
    try:
        with open(path, "w", **_ENV_WRITE_KW) as fh:
            fh.write(text)
        return
    except PermissionError:
        pass
    if not session_user.switch_needed():
        raise PermissionError(f"{path} is unwritable by this uid and no session-user switch applies")
    err = session_user.write_file_as_session_user(path, text, mode=WORKTREE_ENV_MODE)
    if err:
        raise PermissionError(f"delegated write of {path} failed: {err}")


def harden_worktree_modes(wt: str) -> None:
    """Converge this slot's secret files onto `WORKTREE_PRIVATE_MODES`, on every claim AFTER `reset_to_base`
    (a checkout recreates files at the umask; the CLI writes `settings.local.json` mid-session under
    `UMask=0002`). `lstat` first, delegated `chmod` only where the mode differs. NEVER RAISES: a mode that
    could not be tightened is a logged non-narrowing, not a failed lease.

    A no-op ON WINDOWS: a mode there is two bits of read-only flag, not an access control — the slot's
    files take the ACL the installer set on the home (no chmod on Windows), and a "tightening"
    would only flip read-only on and break the next write to the `.env`."""
    if host_platform.is_windows():
        return
    for rel, mode in WORKTREE_PRIVATE_MODES:
        path = os.path.join(wt, rel)
        try:
            st = os.lstat(path)
        except OSError:
            continue          # absent — most slots have no `.env.local` and no `settings.local.json`
        if stat.S_ISLNK(st.st_mode):
            # `chmod` follows the link. Defence against accident, NOT a control: the lstat→chmod TOCTOU is
            # unclosable and harmless — the delegated chmod runs as uid `jaah`, the runner's own, and
            # `chmod(2)` requires ownership, so every reachable target is one the runner could chmod itself.
            log.warning("%s is a symlink — refusing to chmod through it; left as it is", path)
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        current = stat.S_IMODE(st.st_mode)
        if current == mode:
            continue
        err = session_user.chmod_as_session_user(path, mode)
        if err:
            log.warning("could not tighten %s from %o to %o: %s", path, current, mode, err)
        else:
            log.info("tightened %s %o -> %o", path, current, mode)


def _clean_env_value(raw: str) -> str:
    """A `.env` right-hand side as the value it means: comment cut at the first `#`, quotes and spaces
    stripped. One home so `_env_port` and `_env_spent_ports` cannot disagree."""
    value = re.sub(r"\s*#.*", "", raw, count=1)
    return re.sub(r"[\"' ]", "", value).strip("\n")


# ── .env applicators ───────────────────────────────────────────────────────────────────────────────────
def _ensure_eol_nl(path: str) -> None:
    """Ensure FILE ends with a newline so an appended KEY=VALUE cannot glue onto the last line. NEVER FATAL
    (unlike `_set_env_vars`): the caller about to append refuses loudly for the same reason."""
    try:
        text = _env_text_for_rewrite(path)
    except OSError as exc:
        log.warning("could not read %s to terminate its last line: %s", path, exc)
        return
    if not text or text.endswith("\n"):
        return
    try:
        _env_write_text(path, text + "\n")
    except OSError as exc:
        log.warning("could not append the trailing newline to %s: %s", path, exc)


def _apply_env_assignments(text: str, assignments: "dict[str, str]") -> str:
    """TEXT with each `KEY=value` rewritten in place (the first `^\\s*KEY=` line wins, its leading
    whitespace kept and everything after the `=` replaced) or appended when the key is absent."""
    lines = text.splitlines(keepends=True)
    for key, value in assignments.items():
        pat = re.compile(r"^(\s*" + re.escape(key) + r"=).*")
        for i, line in enumerate(lines):
            match = pat.match(line)
            if match:
                nl = "\n" if line.endswith("\n") else ""
                lines[i] = match.group(1) + value + nl
                break
        else:
            lines.append(f"{key}={value}\n")
    return "".join(lines)


def _managed_value_span(lines: "list[str]", i: int, value: str) -> "int | None":
    """The index one PAST the last physical line of the assignment starting at `lines[i]` with right-hand
    side `value` — `i + 1` for one line, further for a QUOTED value continuing onto later lines, `None`
    when the quote never closes. A removal that deleted only the first physical line of
    `KEY="-----BEGIN\\n…\\n-----END"` would leave the rest behind as orphan assignments."""
    quote = value[:1]
    if quote not in ("'", '"'):
        return i + 1
    if value.count(quote) >= 2:
        return i + 1
    for j in range(i + 1, len(lines)):
        if quote in lines[j]:
            return j + 1
    return None


def _remove_env_keys(text: str, names: "list[str] | tuple[str, ...]") -> str:
    """TEXT with every assignment of every NAME DELETED. A REMOVAL, never a blanking: `.env.example` ships
    blanks for exactly the secrets a managed variable supplies, and `NAME=` LOOKS set and fails deep inside
    a container, while an absent variable every consumer can see. EVERY match (a duplicate key would
    otherwise keep the blank) and every spelling including `export KEY=` — a missed removal ships the
    example's blank secret, so the negative grammar is broader than `_apply_env_assignments`'s."""
    if not names:
        return text
    pat = re.compile(r"^\s*(?:export\s+)?(?:" + "|".join(re.escape(n) for n in names) + r")=(.*?)\r?\n?$")
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = pat.match(lines[i])
        if match is None:
            out.append(lines[i])
            i += 1
            continue
        span = _managed_value_span(lines, i, match.group(1))
        if span is None:
            # Unterminated quote: deleting to EOF would take the whole `.env`. Kept, loudly.
            log.warning(
                "could not remove an unresolvable managed variable from a .env: its value opens a quote "
                "that never closes, so the assignment has no end to delete to. Fix the .env.example line"
            )
            out.append(lines[i])
            i += 1
            continue
        i = span
    return "".join(out)


def _sub_embedded_port(text: str, old: int, new: int) -> str:
    """`<host>:OLD` → `<host>:NEW` for localhost/127.0.0.1/0.0.0.0. The `\\b` guards against matching a
    longer number."""
    pat = re.compile(r"(localhost|127\.0\.0\.1|0\.0\.0\.0):" + str(old) + r"\b")
    return pat.sub(lambda m: f"{m.group(1)}:{new}", text)


def _set_env_vars(path: str, assignments: "dict[str, str]",
                  embedded: "tuple[tuple[int, int], ...] | list[tuple[int, int]]" = ()) -> None:
    """Apply several `.env` assignments — and the `<host>:old` → `<host>:new` URL rewrites that follow —
    in ONE read and ONE write. Batched because at `WORKTREE_ENV_MODE` a daemon write is a delegated tmux
    round trip and the applicators set half a dozen keys on EVERY claim. Unchanged text writes nothing.

    AN UNDELIVERABLE WRITE RAISES: a band that did not land leaves the slot on the `.env.example` seed,
    identical in every slot, so every slot claimed in the window binds the SAME host ports. The READ side
    only warns because a failed read leaves the file untouched (absent `.env` = repo-contract fault
    `apply_port_band` must not make unmaterialisable; over-cap = refused so the bytes stay as they are),
    whereas a failed write may already have reached disk."""
    if not assignments and not embedded:
        return
    try:
        text = _env_text_for_rewrite(path)
    except OSError as exc:
        log.warning("could not read %s to set %s: %s", path, sorted(assignments), exc)
        return
    new_text = _apply_env_assignments(text, assignments)
    for old, new in embedded:
        new_text = _sub_embedded_port(new_text, old, new)
    if new_text == text:
        return
    try:
        _env_write_text(path, new_text)
    except OSError as exc:
        # Logged first so the path and the KEYS (never the values) reach the journal, then raised.
        log.error("could not write %s (%s) — %s not applied", path, exc, sorted(assignments))
        raise


def _set_env_var(path: str, key: str, value: str) -> None:
    """One assignment, through the batch writer — kept because three applicators set exactly one key."""
    _set_env_vars(path, {key: value})

# `KEY=value` with the key captured. `export` accepted: a MISSED assignment is the expensive direction.
_ENV_KV_RX = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# A pool `.env` is ~2 KB. The sibling scan reads EVERY other slot's file on every lease, so one absurd
# file would otherwise be read whole into the daemon each time.
_MAX_ENV_BYTES = 1 << 20
