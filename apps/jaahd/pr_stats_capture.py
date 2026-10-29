"""A `merged_pr` settle's PR facts, read off the forge by the daemon and stamped into the settle body (R9).

`cred_broker._call_as` calls `capture_all` on every `task.complete` whose `terminal_type` is `merged_pr`, with the
settling task's OWN forge login (the same `_fetch_git_login` seam `jaah-forge` reaches through the broker) and
the settle's `pr_url`. It answers one value per daemon-stamped key: `pr_stats` (the `forge_cli.model.DiffStats`
wire) and `pr_provenance` (`pr_provenance_capture`), each a `not_captured` answer with a coarse reason when it
could not be read — never an exception: capture NEVER blocks a settle (AC9.2), and the whole read (login and
both keys) gets `BUDGET_S` in total. The caller may not send either key itself
(`agent_vocabulary.Verb.daemon_body_keys`), so a model cannot forge its own LOC or commits.
The CP halves that store them: `apps/api/app/pr_stats.py`, `apps/api/app/pr_changes.py`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable

from forge_cli import login as forge_login
from forge_cli.adapters import for_login
from forge_cli.model import CliExit, ForgeError
from forge_cli.transport import Transport

import pr_provenance_capture

log = logging.getLogger(__name__)

# The TOTAL time a settle waits on capture: the login fetch and every forge page of `pr_stats` AND
# `pr_provenance` together (`agent_vocabulary`'s `task.complete` timeout is sized from it).
BUDGET_S = 20.0

# Captures in flight across the daemon: an abandoned read (past its budget) holds its slot until its own
# deadline ends it, so a runner looping settles cannot pile up threads and page buffers.
_IN_FLIGHT = threading.BoundedSemaphore(2)

# The PR path under a login's `web_url`, one per forge: GitHub `pull/N`, GitLab `-/merge_requests/N`,
# Bitbucket `pull-requests/N`, Azure DevOps `pullrequest/N`.
_PR_PATH = re.compile(r"(?:pull|-/merge_requests|pull-requests|pullrequest)/([0-9]+)(?:[/?#].*)?", re.IGNORECASE)


def not_captured(reason: str) -> dict:
    return {"status": "not_captured", "reason": reason}


class _NotCaptured(Exception):
    """A coarse reason `capture` answers with."""


def pr_number(web_url: str, pr_url: object) -> int:
    """The PR number `pr_url` names on THIS login's repo, or `_NotCaptured`: the stats are read with the
    task's own login, so a URL on any other repo would count a different PR."""
    prefix = web_url.rstrip("/").lower() + "/"
    if not isinstance(pr_url, str) or not pr_url.lower().startswith(prefix):
        raise _NotCaptured("pr_url_not_this_repo")
    match = _PR_PATH.fullmatch(pr_url[len(prefix):])
    if not match:
        raise _NotCaptured("pr_url_unrecognized")
    return int(match.group(1))


# The bytes of successful GET answers one capture keeps for its next reader: `pr_stats` and `pr_provenance` page
# the same file list, and a second walk of it would spend the shared budget twice.
_MEMO_MAX_BYTES = 8 * 1024 * 1024


class _Deadline:
    """A `Transport` that refuses any request past `deadline`, so an abandoned read stops paging — and answers
    a GET it already made in this capture from memory (up to `_MEMO_MAX_BYTES`)."""

    def __init__(self, inner: Transport, deadline: float) -> None:
        self._inner, self._deadline = inner, deadline
        self._memo: dict = {}
        self._memo_bytes = 0

    def send(self, method, url, headers, body):
        if method == "GET" and url in self._memo:
            return self._memo[url]
        if time.monotonic() >= self._deadline:
            raise _NotCaptured("timeout")
        response = self._inner.send(method, url, headers, body)
        size = len(getattr(response, "body", b"") or b"")
        if method == "GET" and getattr(response, "status", 0) == 200 and \
                self._memo_bytes + size <= _MEMO_MAX_BYTES:
            self._memo[url] = response
            self._memo_bytes += size
        return response


def _reason(exc: BaseException) -> str:
    """The coarse reason a failed read answers with — never the exception's text, which can quote a header."""
    if isinstance(exc, _NotCaptured):
        return str(exc)
    if isinstance(exc, ForgeError):
        # The CLI's own capped, one-line message (no header, no token): what the operator acts on.
        return f"forge_error: {exc.message}"
    if isinstance(exc, CliExit):
        return "credential_refused"
    if isinstance(exc, NotImplementedError):
        # An adapter without this read (`Adapter.provenance` on Bitbucket/Azure DevOps): never a guess.
        return "unsupported_forge"
    # An answer shaped unlike the forge's documented one: the type only, as the CLI does.
    return f"unexpected_answer: {type(exc).__name__}"


def _stats(adapter, number: int) -> dict:
    return adapter.diff_stats(number).to_json()


def _stats_failed(reason: str, _forge: "str | None", _number: "int | None") -> dict:
    return not_captured(reason)


# Each daemon-stamped settle key: (read it off the adapter, the `not_captured` answer for a reason). Read in this
# order, sharing one login, one PR number and one budget.
_READERS: "dict[str, tuple[Callable, Callable]]" = {
    "pr_stats": (_stats, _stats_failed),
    "pr_provenance": (pr_provenance_capture.read, pr_provenance_capture.not_captured),
}


def _read(fetch_login: Callable[[], dict], pr_url: object, transport: Transport, keys: "tuple[str, ...]",
          outcome: dict) -> None:
    """Fill `outcome` with each key's answer as it lands, and `forge`/`number` once known — so a read abandoned
    at its budget keeps what it already has. A failure before the reads fails every key alike."""
    try:
        value = fetch_login()
    except Exception:  # noqa: BLE001 — a CP refusal or an unreachable CP: the coarse reason is the answer
        raise _NotCaptured("forge_login_unavailable") from None
    if not value:
        raise _NotCaptured("no_forge_login")
    login = forge_login.from_wire(value)
    outcome["forge"] = login.forge
    number = outcome["number"] = pr_number(login.web_url, pr_url)
    adapter = for_login(login, transport)
    for key in keys:
        read, failed = _READERS[key]
        try:
            outcome[key] = read(adapter, number)
        except Exception as exc:  # noqa: BLE001 — one key's failure never costs another key its answer
            outcome[key] = _logged(key, pr_url, _reason(exc), failed, login.forge, number)


def capture_all(fetch_login: Callable[[], dict], pr_url: object, *, budget_s: float = BUDGET_S,
                transport: "Transport | None" = None, keys: "tuple[str, ...]" = tuple(_READERS)) -> dict:
    """`{key: answer}` for each daemon-stamped key, within `budget_s` IN TOTAL. Never raises.

    The reads run on a daemon thread joined for the budget: a forge still answering past it is abandoned (its
    own transport deadline ends it) and every key not yet answered is `not_captured`/`timeout`."""
    if not _IN_FLIGHT.acquire(blocking=False):
        return {key: _logged(key, pr_url, "busy", _READERS[key][1]) for key in keys}
    outcome: dict = {}
    paced = _Deadline(transport or Transport(), time.monotonic() + budget_s)

    def work() -> None:
        try:
            _read(fetch_login, pr_url, paced, keys, outcome)
        except Exception as exc:  # noqa: BLE001 — mapped to its coarse reason
            outcome["reason"] = _reason(exc)
        finally:
            _IN_FLIGHT.release()

    thread = threading.Thread(target=work, name="pr-stats-capture", daemon=True)
    try:
        thread.start()
    except RuntimeError:  # no thread to be had: the settle still goes out
        _IN_FLIGHT.release()
        return {key: _logged(key, pr_url, "capture_failed", _READERS[key][1]) for key in keys}
    thread.join(budget_s)
    # A snapshot: an abandoned thread may still write, and nothing it writes after the budget is sent.
    seen = dict(outcome)
    late = "timeout" if thread.is_alive() else None
    reason = late or seen.get("reason") or "timeout"
    return {key: seen[key] if key in seen else
            _logged(key, pr_url, reason, _READERS[key][1], seen.get("forge"), seen.get("number")) for key in keys}


def capture(fetch_login: Callable[[], dict], pr_url: object, *, budget_s: float = BUDGET_S,
            transport: "Transport | None" = None) -> dict:
    """The `pr_stats` alone for `pr_url` (`capture_all` with that one key). Never raises."""
    return capture_all(fetch_login, pr_url, budget_s=budget_s, transport=transport, keys=("pr_stats",))["pr_stats"]


def _logged(key: str, pr_url: object, reason: str, failed: Callable = _stats_failed,
            forge: "str | None" = None, number: "int | None" = None) -> dict:
    # `repr`, capped: `pr_url` is the runner's text, and a raw newline would forge a journal line.
    log.warning("%s: %s not captured: %s", key, repr(pr_url)[:200], reason)
    return failed(reason, forge, number)


def refused_keys(status: int, cp_detail: str, keys: "tuple[str, ...]" = tuple(_READERS)) -> "set[str]":
    """The stamped `keys` a CP's 422 on the settle names — a CP that forbids those fields. The settle is then
    sent ONCE more without them (version skew must never cost the settle). Empty for any other refusal."""
    if status != 422:
        return set()
    try:
        detail = json.loads(cp_detail).get("detail")
    except (ValueError, AttributeError):
        return set()
    if not isinstance(detail, list):
        return set()
    # The error's LOCATION, never its text: a message may quote the caller's own input.
    return {key for key in keys for e in detail
            if isinstance(e, dict) and isinstance(e.get("loc"), list) and key in e["loc"]}
