"""The one refresh mechanism under the per-project bundle files the daemon keeps beside a session: the Architectural
Guard's rules (`guard_bundle`) and project memory (`project_memory_bundle`), each a `BundleRegistry` of files keyed by
path, re-asked from the control plane with `since=<revision>` off the loop, one pass at a time — 200 rewrites the
file (atomically, as the session user), 204 keeps it, 410 writes the empty bundle, 404 (the guard route's "no such
project": deleted, or no longer this node's org's) removes the file and ends its registration, a miss keeps what is
there. The
two differ only in the registry's fields, each a real difference said at the field. Each module keeps its own launch
write, exports and refresh step, and re-exports the registry's names its callers and tests reach. No hook loads this.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import file_io
import session_user

#: The most of a bundle file the refresh reads back to check it is unchanged: a bigger one (a guest's sparse file
#: planted in its launch share) is never read at all — it is treated as edited and rewritten.
BUNDLE_READ_MAX = 16 * 1024 * 1024
Writer = Optional[Callable[[str, str, int], str]]


@dataclass
class Registration:
    project_id: int
    #: The CP's revision token of the content last written (`BundleRegistry.revision_of`); None asks in full.
    revision: str | int | None
    mode: int
    #: sha256 of the content last written (`verify_written`): an edited or removed file is re-fetched in full.
    written: str = ""
    #: `(path, content, mode) -> "" | error`: how the file is written; None is the session user's atomic replace.
    #: A file with its own writer (a VM's launch share) has no spool for the daemon to ship.
    writer: Writer = None


@dataclass(kw_only=True)
class BundleRegistry:
    #: The log prefix; the logger is the feature's own.
    label: str
    log: logging.Logger
    #: `(project_id, since=) -> (status, bundle)`, raising on anything but 200/204/404/410.
    fetch: Callable[..., tuple[int, dict]]
    #: The bundle with nothing in it, what 410 writes.
    empty: Callable[[int], dict]
    #: A bundle's revision token as the registry keeps it: a `sha256:` string for the guard, an int for memory.
    revision_of: Callable[[dict], str | int | None]
    #: The pass interval: the most a pass waits for a file another re-ask holds.
    interval_s: float
    #: True (the guard): every pass checks the file is still exactly what was last written (its sha256) and asks for
    #: an edited or deleted one in full, so it is rewritten (a session user cannot make a bundle stay away), and only
    #: a file whose folder is gone (the slot's tree was removed) ends its registration. False (memory): the file is
    #: trusted, and one that is gone ends it.
    verify_written: bool
    #: What 410 (the feature is off for the project) does after writing the empty bundle. True (memory): drops the
    #: registration — no open ON task is left. False (the guard): keeps re-asking, so an org switching the guard back
    #: on reaches the session as the next 200; the empty bundle is written once (`since` already naming it).
    drop_on_410: bool
    #: `(path, project_id, mode)` of a file the daemon writes itself (no `writer`), after each re-ask of it: how the
    #: guard ships the project's spool.
    after_refresh: Optional[Callable[[str, int, int], object]] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Serialises a file's fetch-and-write between the periodic pass and an on-demand re-ask.
    io_lock: threading.Lock = field(default_factory=threading.Lock)
    entries: dict[str, Registration] = field(default_factory=dict)

    def register(self, path: str, project_id: int, bundle: dict, mode: int, writer: Writer = None) -> None:
        """Write `bundle` as `path` and refresh it every pass from now on (a later launch on the file replaces it)."""
        written = self.write(path, bundle, mode, writer)
        with self.lock:
            self.entries[path] = Registration(project_id=int(project_id), revision=self.revision_of(bundle),
                                              mode=mode, written=written, writer=writer)

    def registered(self) -> dict[str, Registration]:
        with self.lock:
            return dict(self.entries)

    def drop(self, path: str) -> None:
        with self.lock:
            self.entries.pop(path, None)

    def write(self, path: str, bundle: dict, mode: int, writer: Writer = None) -> str:
        """`bundle` written atomically (as the session user, or as `writer` says); its digest, "" when it failed."""
        content = json.dumps(bundle, sort_keys=True)
        err = (writer(path, content, mode) if writer is not None
               else session_user.replace_file_as_session_user(path, content, mode=mode))
        if err:
            self.log.warning("%s: could not write %s (%s) — the session's hooks keep what is there", self.label,
                             path, err)
            return ""
        return hashlib.sha256(content.encode()).hexdigest()

    def unchanged_since_written(self, path: str, entry: Registration) -> bool:
        """Is `path` still exactly what the daemon last wrote? False when it is gone, unreadable, edited, or no
        longer a regular file of at most `BUNDLE_READ_MAX` bytes (`file_io.read_bounded`: a symlink, FIFO or huge
        file planted in its place — a VM guest owns its launch share — is never followed, blocked on or read)."""
        try:
            text = file_io.read_bounded(path, BUNDLE_READ_MAX)
        except ValueError:
            return False
        return bool(entry.written) and hashlib.sha256(text).hexdigest() == entry.written

    def refresh_file(self, path: str, entry: Registration) -> Optional[int]:
        """One re-ask for one file (the module docstring); its status, None on a miss. Callers hold `io_lock`."""
        since = entry.revision if not self.verify_written or self.unchanged_since_written(path, entry) else None
        try:
            status, bundle = self.fetch(entry.project_id, since=since)
        except Exception as exc:  # noqa: BLE001 — a miss keeps the file there is; the next tick retries
            self.log.info("%s: refresh of %s failed (%s) — keeping the current bundle", self.label, path, exc)
            return None
        if status == 200:
            self._rewritten(path, entry, bundle)
        elif status == 404:
            self._forget(path, entry)
        elif status == 410:
            empty = self.empty(entry.project_id)
            if self.drop_on_410 or since != self.revision_of(empty):  # a kept file already empty is left alone
                self._rewritten(path, entry, empty)
            if self.drop_on_410:
                self.drop(path)
        return status

    def _forget(self, path: str, entry: Registration) -> None:
        """The project is gone (404): stop re-asking and remove its file, so a daemon restart (`rehydrate`) does not
        adopt it again. Asking forever would only log the same 404 every pass."""
        self.drop(path)
        try:
            os.remove(path)
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                self.log.warning("%s: project %s is gone but %s could not be removed (%s)", self.label,
                                 entry.project_id, path, exc)
                return
        self.log.info("%s: project %s is gone — removed %s and stopped refreshing it", self.label,
                      entry.project_id, path)

    def _rewritten(self, path: str, entry: Registration, bundle: dict) -> None:
        """Write `bundle` over `path` and remember what was written, for the next re-ask's `since`."""
        written = self.write(path, bundle, entry.mode, entry.writer)
        with self.lock:
            current = self.entries.get(path)
            if written and current is not None:
                current.revision, current.written = self.revision_of(bundle), written

    def refresh_once(self, projects: frozenset[int] | None = None) -> None:
        """One pass over every registration (or those of `projects`): `refresh_file`, then `after_refresh`."""
        for path, entry in self.registered().items():
            if projects is not None and entry.project_id not in projects:
                continue
            if not (os.path.isdir(os.path.dirname(path)) if self.verify_written else os.path.exists(path)):
                self.drop(path)
                continue
            if not self.io_lock.acquire(timeout=self.interval_s):
                continue
            try:
                current = self.registered().get(path)
                if current is not None:
                    self.refresh_file(path, current)
            finally:
                self.io_lock.release()
            if self.after_refresh is not None and entry.writer is None:
                self.after_refresh(path, entry.project_id, entry.mode)
