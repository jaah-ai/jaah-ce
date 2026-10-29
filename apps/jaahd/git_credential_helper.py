#!/usr/bin/env python3
"""jaah-git-credential — the node's ONE git credential helper, with NO TOKEN AT REST.

Installed at `/usr/lib/jaahd/jaah-git-credential` and wired as the system `credential.helper` (with
`credential.useHttpPath=true`) by the node's provisioning (`ensure_git_credential_helper`); every
git process the node starts also pins it per process (`apps/jaahd/git_env.py::BROKER_ONLY`). Standalone
and stdlib-only: it is a copied file, so it imports nothing from this package. A Windows or macOS node
copies nothing: git runs the bundle's own `apps/jaahd` copy under the bundle's interpreter
(`git_env.windows_credential_helper` / `macos_credential_helper`), and only there does it import the seam —
for the named pipe on Windows, for the socket's path on macOS.

git runs it as `jaah-git-credential <get|store|erase>` with a credential description on stdin
(`key=value` lines). On `get` it asks the local credential broker for `git_credential` — the login of
whoever the broker derives the asking process belongs to (a runner's task launcher, or the slot declarer
for the daemon's own pool clone; `apps/jaahd/cred_broker.py`). Nothing here names an identity: the broker
reads the kernel's answer (SO_PEERCRED + /proc ancestry), never a field in the request. The reply is the
control plane's pinned git-token wire (`apps/api/app/routes/agent/task_secrets.py::GitTokenOut`).

A TOKEN NEVER GOES TO A FOREIGN DESTINATION. The login is for one repository (`web_url`), so the answer
is refused — nothing printed, exit 1 — unless git's `protocol` is https, its `host` equals the served
`host`, and its `path` is that repository's (`check_destination`). The same check runs twice: here, and in
the broker, which this helper tells where git is going (`"for"` on the request) so a login is refused at
its source too. Answering nothing lets git carry on to whatever else it has (on a runner, nothing:
`GIT_TERMINAL_PROMPT=0` makes it fail fast).

HOW THE LOGIN IS SPOKEN. `scheme=basic` → `username=` + `password=`. `scheme=bearer` (an Azure DevOps
Entra token, not accepted as a Basic password) → `authtype=Bearer` + `credential=`, which git accepts only
under the `authtype` capability it advertises (git ≥ 2.46, the node converge's floor). Without it this
FAILS LOUDLY on stderr rather than downgrading the token to a Basic password the forge would refuse.

A RUNNER-WRITABLE CONFIG CANNOT STEER THE CREDENTIALED CONNECTION (`check_config_scopes`). Before asking the
broker, `get` reads git's own view of the config it is running under (`git config --list --show-scope
--show-origin --includes`, in the repository git runs it from) and refuses — before any network I/O on the
login — when a key in scope `local`, `worktree` or `global`, or one pulled in by an include, matches
`http.*` (generic, URL- or host-scoped), `url.*`, `include.*`, `includeIf.*`, `credential.*` or
`remote.<name>.(proxy|vcs|uploadpack|receivepack)`: each can redirect, proxy, re-trust or add a header to the
connection the login is sent on. `system` (root-owned: a corporate `http.sslCAInfo` keeps working) and
`command` (the node's own pins, `git_env.BROKER_ONLY`) are trusted; a key a command-scope include pulls in from
a file is not. Scopes are as git reports them: a process that sets its own `GIT_CONFIG_*` environment is out of
reach, as for `git_env.pinned_config`. A config that cannot be read refuses too. This is the interim closure
for the runner-session push and fetch (`fleet_dispatch`'s `BROKER_ONLY` export says why it is not moved to the
daemon's mirror).

FAIL CLOSED. Any broker failure — the socket missing, a timeout, malformed JSON, or a refusal — prints the
reason on stderr (git inherits it, so the control plane's "<user> has not connected a <forge> account"
lands in the runner's terminal beside the failed fetch) and NOTHING on stdout. `store` writes nothing: there
is nothing at rest to update. `erase` (git's word for "the server refused that login") asks the broker past
its cache, so the next `get` serves the token the control plane has since refreshed.

A REFUSED LOGIN IS REPORTED (`report_forge_auth`). `erase` also tells the control plane, through the broker,
that the forge refused the task's login (`forge_auth.rejected`); `store` (git's word for "the server took it")
says `forge_auth.accepted`, which the broker forwards only after a refusal it holds for that task and host. A
merge-lane run refused twice in one attempt ends there, naming the credential (the control plane's
`forge-auth-rejected` route, `apps/api/app/routes/agent/task_secrets.py`). Fire-and-forget: a report never fails git.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.parse

# Same precedence as `jaah-credential` (the node provisioning's `CREDENTIAL_HELPER_SCRIPT`)
# and `apps/jaahd/environment.py::credential_socket_explicit`: the test/vnode override, then the node's
# fixed path. Which socket is dialled never changes WHOSE answer comes back.
_FIXED_SOCKET = "/run/jaah/creds/sock"
# A Windows node's broker is a named pipe (`host_platform.windows`' `creds_socket`), not a file: same
# precedence, different fixed name.
_WINDOWS_FIXED_SOCKET = "\\\\.\\pipe\\jaah-creds"
_SOCKET_ENVS = ("JAAH_CRED_SOCKET",)
# A wedged broker must fail fast rather than hang git.
TIMEOUT_S = 5
# The broker's name for the whole login (`cred_broker._GIT_CREDENTIAL`).
CREDENTIAL_NAME = "git_credential"
_MAX_REPLY_BYTES = 65536

_PROG = "jaah-git-credential"

# The vocabulary verbs a forge's verdict on the served login is reported by (`agent_vocabulary.py`).
FORGE_AUTH_REJECTED = "forge_auth.rejected"
FORGE_AUTH_ACCEPTED = "forge_auth.accepted"


class Refused(Exception):
    """No answer for git. `loud` says whether the reason is worth an operator's attention on stderr."""

    def __init__(self, reason: str, *, loud: bool = True) -> None:
        super().__init__(reason)
        self.loud = loud


def socket_path(environ: "dict[str, str] | os._Environ[str]") -> str:
    for name in _SOCKET_ENVS:
        value = environ.get(name)
        if value:
            return value
    if sys.platform == "win32":
        return _WINDOWS_FIXED_SOCKET
    if sys.platform == "darwin":
        # A macOS node runs this file in place from the bundle's `apps/jaahd` (`git_env.macos_credential_helper`),
        # so the seam beside it names the socket (`/var/run/jaah/creds/sock`).
        import host_platform  # noqa: PLC0415 — macOS only; see `_connect` for why never at import

        return host_platform.paths().creds_socket
    return _FIXED_SOCKET


def parse_description(text: str) -> tuple[dict[str, str], set[str]]:
    """git's `key=value` lines → (single-valued attributes, advertised capabilities). Stops at a blank line."""
    attrs: dict[str, str] = {}
    capabilities: set[str] = set()
    for line in text.splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "capability[]":
            capabilities.add(value)
        elif not key.endswith("[]"):
            attrs[key] = value
    return attrs, capabilities


def _connect(path: str):
    """A connected stream to the broker at `path`; raises `OSError` like `connect`.

    On Windows the broker is a named pipe, dialled through `host_platform.connect_socket`, which also
    proves the pipe's server IS the daemon (a squatted pipe would otherwise collect the request). Imported
    lazily and only there: on Linux this file is a standalone copy with no package beside it, while a
    Windows node runs it in place from the bundle's `apps/jaahd`, where the seam sits next to it."""
    if sys.platform == "win32":
        import host_platform  # noqa: PLC0415 — see the docstring

        return host_platform.connect_socket(path, timeout=TIMEOUT_S)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(TIMEOUT_S)
        sock.connect(path)
    except BaseException:
        sock.close()
        raise
    return sock


def _working_directory() -> str:
    try:
        return os.getcwd()
    except OSError:  # a deleted cwd: no checkout to report, so the primary's login, as before
        return ""


def ask_broker(path: str, checkout: "str | None" = None, *, fresh: bool = False,
               destination: "dict[str, str] | None" = None) -> dict:
    """The broker's login object, or `Refused` carrying why not.

    `checkout` (default: this process's working directory, which git sets to the repository it is working
    on) tells the broker which of the task's repositories the login is for; the broker reads the
    repository off that checkout itself, among the caller's own slot only. `fresh`: the forge just refused
    the login the broker served, so it reads past its cache (`cred_broker._CALLER_FRESH`). `destination`:
    git's `protocol`/`host`/`path`, sent as `"for"` so the broker refuses a login for anywhere else
    (`check_destination`)."""
    request: dict[str, object] = {
        "credential": CREDENTIAL_NAME, "checkout": _working_directory() if checkout is None else checkout}
    if fresh:
        request["fresh"] = True
    if destination is not None:
        request["for"] = {key: destination.get(key, "") for key in DESTINATION_KEYS}
    try:
        with _connect(path) as sock:
            sock.sendall((json.dumps(request) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
            buf = b""
            while b"\n" not in buf and len(buf) < _MAX_REPLY_BYTES:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise Refused(f"credential broker unreachable at {path}: {exc}") from None
    try:
        reply = json.loads(buf.split(b"\n", 1)[0])
    except ValueError:
        raise Refused("credential broker returned malformed JSON") from None
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        detail = reply.get("error") if isinstance(reply, dict) else None
        # One line: the text reaches a terminal, and a newline in it is not ours to pass on. A destination
        # refusal is as quiet as this helper's own (`check_destination`): git carries on to whatever else it has.
        raise Refused(" ".join(str(detail or "credential broker declined the git credential").split()),
                      loud=not (isinstance(reply, dict) and reply.get("destination_refused") is True))
    value = reply.get("value")
    if not isinstance(value, dict):
        raise Refused("credential broker answered without a git login")
    return value


def report_forge_auth(path: str, verb: str, host: str, repo_path: str = "") -> None:
    """Tell the control plane, through the broker's `call` arm, that the forge at `host` refused
    (`FORGE_AUTH_REJECTED`) or took (`FORGE_AUTH_ACCEPTED`) the login it served for `repo_path`. The broker
    attaches the task from the caller's identity, as for the login itself. Fire-and-forget: every failure is
    swallowed, since a report must never fail the git or forge command it is about."""
    request = {"call": verb, "args": {"host": host, "repo_path": repo_path or None}}
    try:
        with _connect(path) as sock:
            sock.sendall((json.dumps(request) + "\n").encode())
            sock.shutdown(socket.SHUT_WR)
            sock.recv(4096)  # the broker's answer, unread: waited for so the report is not cut short
    except Exception:  # noqa: BLE001 — fire-and-forget (a Windows pipe check raises more than `OSError`)
        pass


def _field(login: dict, key: str) -> str:
    value = login.get(key)
    if not isinstance(value, str):
        raise Refused(f"credential broker's git login carries no {key!r}")
    # A newline or NUL would inject a second attribute into git's protocol.
    if "\n" in value or "\r" in value or "\0" in value:
        raise Refused(f"credential broker's git login has an unusable {key!r}")
    return value


# The attributes of git's request that say where a login would be sent.
DESTINATION_KEYS = ("protocol", "host", "path")


def check_destination(attrs: dict[str, str], login: dict) -> None:
    """`Refused` unless git's `protocol`/`host`/`path` (`attrs`) are the login's own repository: https, the
    served `host`, and a `path` that is the repository `web_url` names (`o/r`, `o/r.git`, or below either —
    an LFS endpoint). The one home of this rule: the broker runs it too, on the `"for"` this helper sends
    (`cred_broker._refuse_foreign_destination`), so a planted remote, bundle URI or rewrite that points git
    at another host or another repository on the same host gets nothing."""
    host = _field(login, "host").lower()
    # The root's case is kept for the path comparison below; only the host part is case-insensitive.
    root = _field(login, "root")
    if not host or not root:
        # A control plane older than the forge wire serves no `host`/`root`: say so, never fail silently.
        raise Refused("credential broker's git login names no host/root — is the control plane older than "
                      "the per-forge git wire?")
    if attrs.get("protocol", "").lower() != "https":
        raise Refused(f"not answering for protocol {attrs.get('protocol')!r}; the login is for https", loud=False)
    asked = attrs.get("host", "").lower()
    if asked != host:
        raise Refused(f"not answering for host {asked!r}; the login is for {host!r}", loud=False)
    on_host = root.lower() == host or root.lower().startswith(host + "/")
    sub_path = root[len(host):].strip("/") if on_host else None
    if sub_path is None:
        raise Refused(f"credential broker's git login root {root!r} is not on its host {host!r}")
    web = urllib.parse.urlsplit(_field(login, "web_url"))
    web_path = urllib.parse.unquote(web.path).strip("/")
    repo = web_path[len(sub_path):].strip("/") if not sub_path or web_path.startswith(sub_path + "/") else ""
    if web.scheme != "https" or web.netloc.lower() != host or not repo:
        raise Refused(f"credential broker's git login names no repository under {root!r}")
    # No dot segments: git passes `o/..%2fother` through as `o/../other`, which a front proxy that resolves
    # it would route elsewhere. The sub-path is the proxy's (case-sensitive); the repository below it is the
    # forge's, which resolves any case.
    path = attrs.get("path", "").strip("/")
    below = path
    if sub_path:
        below = path[len(sub_path) + 1:] if path.startswith(sub_path + "/") else ""
    # The repository itself, its `.git` spelling or anything below that, or its LFS endpoint (`<repo>/info/lfs`
    # where the remote has no `.git`, as Azure's never does). Never another path below `<repo>/`: a GitLab
    # subgroup may share the name.
    mine, below = repo.casefold(), below.casefold()
    if any(seg in ("", ".", "..") for seg in path.split("/")) or not (
            below in (mine, mine + ".git") or below.startswith((mine + ".git/", mine + "/info/"))):
        raise Refused(f"not answering for path {path!r}; the login is for {web_path!r} on {host!r}", loud=False)


# Config keys that can redirect, proxy, re-trust or add a header to the credentialed connection. git prints
# section and key names lowercased (`includeif`), keeping a subsection's case; matched case-insensitively.
_STEERING_KEY = re.compile(
    r"^(http\.|url\.|include(if)?\.|credential\.|remote\.[^.]+(\..+)?\.(proxy|vcs|uploadpack|receivepack)$)",
    re.IGNORECASE | re.DOTALL)
# Scopes a runner cannot write: root's system config, and the command line the node's pins ride on.
_TRUSTED_SCOPES = ("system", "command")


def _printable(text: str) -> str:
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def check_config_scopes() -> None:
    """`Refused` when a runner-writable config scope sets a key that can steer the connection a login is sent
    on (the docstring's rule). Reads git's own merged view in this process's working directory (git runs the
    helper at the repository's top), so a key reached through any include is seen where git sees it."""
    # `GIT_CONFIG` makes `git config` (and only it) read one file in place of every scope: the transport git
    # ignores it, so it would hide what git actually reads.
    env = {name: value for name, value in os.environ.items() if name != "GIT_CONFIG"}
    try:
        listing = subprocess.run(
            ["git", "config", "--list", "--show-scope", "--show-origin", "--includes", "-z"],
            env=env, capture_output=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"refused: could not read git config: {exc}") from None
    if listing.returncode != 0:
        error = " ".join(listing.stderr.decode(errors="replace").split()) or f"exit {listing.returncode}"
        raise Refused(f"refused: could not read git config: {error}")
    # `-z`: `<scope>\0<origin>\0<key>\n<value>\0` per entry.
    fields = listing.stdout.split(b"\0")
    for i in range(0, len(fields) - 2, 3):
        scope = fields[i].decode(errors="replace")
        origin = fields[i + 1].decode(errors="replace")
        key = fields[i + 2].split(b"\n", 1)[0].decode(errors="replace")
        included = origin.startswith("file:") and scope == "command"
        if (scope in _TRUSTED_SCOPES and not included) or not _STEERING_KEY.match(key):
            continue
        path = origin[len("file:"):] if origin.startswith("file:") else origin
        if path and not os.path.isabs(path) and origin.startswith("file:"):
            path = os.path.join(_working_directory(), path)  # git prints the repository's own relative to its top
        # The key's subsection and the path are the planter's text, and the fix is a command someone may paste:
        # no control characters reach the terminal, and both are shell-quoted. `--file`, not `--<scope>`: the
        # key may sit in a file an include pulled in; `--unset-all`: it may be set more than once.
        key, path = _printable(key), _printable(path)
        fix = f"git config --file {shlex.quote(path)} --unset-all {shlex.quote(key)}"
        raise Refused(f"refused: {'included' if included else scope} config sets {key} ({path}) — a key that can "
                      f"redirect or intercept the credentialed connection; remove it ({fix})")


def answer(attrs: dict[str, str], capabilities: set[str], login: dict) -> str:
    """The lines git reads back for this request, or `Refused`."""
    check_destination(attrs, login)
    host = _field(login, "host").lower()
    scheme = _field(login, "scheme")
    token = _field(login, "token")
    if not token:
        raise Refused("credential broker's git login carries an empty token")
    if scheme == "basic":
        username = _field(login, "username")
        if not username:
            raise Refused("credential broker's basic git login carries no username")
        return f"username={username}\npassword={token}\n"
    if scheme == "bearer":
        if "authtype" not in capabilities:
            raise Refused(
                f"{host} takes a bearer token, and this git did not advertise the `authtype` credential "
                "capability (git 2.46 or newer is required) — refusing rather than sending it as a password")
        return f"capability[]=authtype\nauthtype=Bearer\ncredential={token}\nephemeral=1\n"
    raise Refused(f"credential broker's git login has an unknown scheme {scheme!r}")


def main(argv: list[str], stdin=None, stdout=None, stderr=None, environ=None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    environ = os.environ if environ is None else environ
    action = argv[0] if argv else ""
    text = stdin.read()
    attrs, capabilities = parse_description(text)
    if action == "erase":
        # git erases a login the server just refused. Nothing is at rest here, but the broker caches the
        # login it served, which a forge revokes on refresh (GitHub does): ask past that cache — the one fresh
        # mint — so the broker holds the live token for git's next command, then report the refusal.
        # Best-effort; erase never fails git.
        try:
            ask_broker(socket_path(environ), fresh=True)
        except Refused:
            pass
        report_forge_auth(socket_path(environ), FORGE_AUTH_REJECTED, attrs.get("host", ""), attrs.get("path", ""))
        return 0
    if action == "store":
        # The server took the login; nothing is at rest to update. The broker drops this unless it holds a
        # refusal for this task and host, so a healthy git command costs no control-plane call.
        report_forge_auth(socket_path(environ), FORGE_AUTH_ACCEPTED, attrs.get("host", ""), attrs.get("path", ""))
        return 0
    if action != "get":
        return 0  # any future action: nothing is at rest to update
    try:
        # Before the broker: a refused request never reaches it.
        check_config_scopes()
        lines = answer(attrs, capabilities, ask_broker(socket_path(environ), destination=attrs))
    except Refused as refused:
        if refused.loud:
            print(f"{_PROG}: {refused}", file=stderr)
        return 1
    stdout.write(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
