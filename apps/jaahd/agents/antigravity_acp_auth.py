"""agents/antigravity_acp_auth.py — a signed-in Antigravity account's ACP sign-in, written from its terminal sign-in.

Read off the Python that `agy_acp_server` 1.4.1 ships (the `[agy-acp-server]` pin):
  * Its Google sign-in (`authenticate` `oauth-personal`, `oauth/credential_manager.py`) is an interactive loopback
    login: `127.0.0.1` on an ephemeral port, ONE request within 300 s, the authorize URL on stderr. A session
    reaches it only when no stored credential refreshes silently: with `auth.type` `oauth-personal` in
    `$GEMINI_HOME/antigravity-acp/settings.json` (`server._infer_auth_state`) the server signs in with no
    `authenticate` call, from `$GEMINI_HOME/antigravity-acp/acp_token.json` (`paths.consumer_token_path`).
  * That file is Google's authorized-user JSON (`CredentialInfo`): `client_id`, `client_secret`, `refresh_token`,
    `token_uri`, `scopes`, `project_id`. No access token, nothing bound to the machine; an empty `project_id` is
    resolved by the server's own onboarding (`OAuthManager._resolve_backend`), which then saves it.
  * Its OAuth client is the CLI's: the server's client id is the one agy 1.2.11's authorize URL carries
    (`agents/antigravity.py::_AUTH_URL_RE`), and the CLI binary holds its paired secret.

So the account's ONE console sign-in (the CLI's, `agents/antigravity_login_pty.py`) is the ACP's too: `seed` writes
the file from the CLI's token — its refresh token, the client id from its id_token's `aud`, and the secret read out
of the installed `agy` by `provider_health._antigravity_secrets` (the one home of that read; no secret is copied
into this repo), paired by one refresh. Not seen working on a live run: that Google accepts the CLI's refresh token
for the ACP's scopes (the binary carries all three) is the catalogue's `not-yet-observed`.

Never linked onto the token store: the server replaces the file by rename (`FileCredentialStore.write`,
`os.replace`), which turns a link into a node-local file. Each node writes its own copy from the CLI token (linked
for a Shared account) at every ACP launch, rewritten only when that token's refresh token changed.

Runs as the account's uid: the daemon re-execs this file as the session user (`seed_oauth_sign_in`, as
`collector.probe_account` does), since on a uid-split node only that user reads the token.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    # The session-user re-exec runs this file by path, `-I`: the daemon tree is put on the path explicitly.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.append(str(Path(__file__).resolve().parent))   # last: `staged_write` only, shadowing nothing

import agent_providers  # noqa: E402 — after the re-exec's path entry

if __package__:
    from . import staged_write
else:
    import staged_write  # type: ignore[no-redef]  # noqa: E402 — the re-exec's path entry

log = logging.getLogger(__name__)

# The server's state under `$GEMINI_HOME` (the account's `.gemini`, `antigravity_runner.acp_launch`), relative to
# the account dir: `paths.acp_dir`, `consumer_token_path`, `settings_path` (agy_acp_server 1.4.1).
ACP_STATE_DIR = os.path.join(".gemini", "antigravity-acp")
TOKEN_FILE = os.path.join(ACP_STATE_DIR, "acp_token.json")
SETTINGS_FILE = os.path.join(ACP_STATE_DIR, "settings.json")
# `server.AUTH_METHOD_OAUTH`: the `auth.type` the server signs in with when no `authenticate` call names one.
OAUTH_METHOD = "oauth-personal"
# What `OAuthCredentialManager._save_credentials` writes for a credential it minted itself.
_TOKEN_URI = "https://oauth2.googleapis.com/token"
# `ccpa_connection.oauth_manager._DEFAULT_SCOPES`; the server refreshes with its own list, so this is the record.
_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/aicode",
)
# The re-exec's bound: a guarded read, at most one token request per candidate secret, two small writes.
_SEED_TIMEOUT = 90


def acp_token_document(*, client_id: str, client_secret: str, refresh_token: str, project_id: str = "") -> dict:
    """The server's `CredentialInfo`; `project_id` empty for its onboarding to resolve. Pure."""
    return {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
            "token_uri": _TOKEN_URI, "scopes": list(_SCOPES), "project_id": project_id}


def _write_private(path: str, text: str) -> None:
    """Replace PATH with TEXT: a `0600` temp file (mode set at create) in a `0700` dir, renamed over the name,
    so a link planted at PATH is replaced, never followed (`staged_write.replace_file`). The server's own write is
    the same shape."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    staged_write.replace_file(path, text)


def _select_oauth(path: str) -> str:
    """Make the server's `auth.type` `OAUTH_METHOD`, keeping every other key — "" or a reason. Untouched when
    it already says so."""
    try:
        with open(path, encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        settings = {}
    except (OSError, ValueError) as exc:
        return f"{path} could not be read ({type(exc).__name__})"
    if not isinstance(settings, dict):
        return f"{path} is not a JSON object"
    auth = settings.get("auth")
    if isinstance(auth, dict) and auth.get("type") == OAUTH_METHOD:
        return ""
    settings["auth"] = {**(auth if isinstance(auth, dict) else {}), "type": OAUTH_METHOD}
    _write_private(path, json.dumps(settings, indent=2))
    return ""


def seed(config_dir: str, credential_home: str) -> str:
    """Write `<config_dir>/TOKEN_FILE` from the CLI token under `credential_home` (the token store for a linked
    Shared account, else `config_dir`) and select `OAUTH_METHOD` — "" or a one-line reason, never a token.

    AS THE ACCOUNT'S UID. The file is rewritten only when it does not already carry this client and refresh token,
    so an ordinary launch makes no request; a rewrite pairs the secret by one refresh first, and keeps a
    `project_id` the server's onboarding already resolved and saved."""
    import google_id_token  # noqa: PLC0415 — the daemon tree; deferred like `provider_health`'s own imports
    import provider_health  # noqa: PLC0415

    present, document = provider_health._read_guarded_json(
        Path(credential_home) / agent_providers.ANTIGRAVITY.credential_file)
    token = document.get("token") if isinstance(document, dict) else None
    refresh_token = token.get("refresh_token") if isinstance(token, dict) else None
    if not (present is True and isinstance(document, dict) and isinstance(token, dict)
            and isinstance(refresh_token, str) and refresh_token):
        return f"no signed-in terminal token with a refresh token under {credential_home}"
    client_id = google_id_token.id_token_claim(document.get("id_token", ""), "aud")
    if not client_id:
        return f"the terminal token under {credential_home} names no OAuth client (id_token `aud`)"
    target = os.path.join(config_dir, TOKEN_FILE)
    _, current = provider_health._read_guarded_json(Path(target))
    if not (isinstance(current, dict) and current.get("refresh_token") == refresh_token
            and current.get("client_id") == client_id and current.get("client_secret")):
        key, _candidates = provider_health._antigravity_secrets()
        _access, outcome = provider_health._antigravity_refresh_outcome(token, client_id)
        secret = provider_health._antigravity_secret_worked.get(key or "")
        if outcome == provider_health.REFRESH_INVALID_GRANT:
            return "Google refused the terminal sign-in's refresh token (invalid_grant): sign the account in again"
        if outcome == provider_health.REFRESH_UNREACHABLE:
            return ("Google's token endpoint could not be reached to pair the terminal sign-in's OAuth client (a "
                    "network failure, not a refused client): retry once the node reaches oauth2.googleapis.com")
        if outcome == provider_health.REFRESH_REFUSED or not secret:
            return (f"no OAuth client secret in the installed {agent_providers.ANTIGRAVITY.binary} refreshed the "
                    f"terminal sign-in ({outcome or 'invalid_client'})")
        project_id = current.get("project_id") if isinstance(current, dict) else None
        try:
            _write_private(target, json.dumps(acp_token_document(
                client_id=client_id, client_secret=secret, refresh_token=refresh_token,
                project_id=project_id if isinstance(project_id, str) else "")))
        except OSError as exc:
            return f"{target} could not be written ({type(exc).__name__})"
    try:
        return _select_oauth(os.path.join(config_dir, SETTINGS_FILE))
    except OSError as exc:
        return f"the ACP sign-in method could not be selected in {config_dir} ({type(exc).__name__})"


def seed_oauth_sign_in(config_dir: str) -> str:
    """The daemon's side of `seed`, for a signed-in run's ACP launch: "" or a reason the launch fails on.

    As the session user where this host has one (`session_user.run_as_session_user`, this file re-exec'd); in-process
    only where no switch is needed (the daemon IS the account's uid). A switched host that cannot reach its session
    user refuses: the daemon would write the account's files as itself, which the session user then cannot read or
    replace. The token's home is resolved here, as `collector` does for its probe (`credential_home`)."""
    import session_user  # noqa: PLC0415 — deferred: daemon-only modules
    import shared_storage  # noqa: PLC0415

    home = shared_storage.credential_home(config_dir, agent_providers.ANTIGRAVITY.value)
    if not session_user.switch_needed():
        return seed(config_dir, home)
    if not session_user.can_run_as_session_user():
        log.warning("antigravity acp: the session user %s is unreachable on this switched node; not seeding the ACP "
                    "sign-in as the daemon", session_user.session_user())
        return (f"the ACP sign-in must run as {session_user.session_user()}, which this node cannot reach now "
                "(no fleet socket or spool); it is not written as the daemon")
    argv = [sys.executable, "-I", str(Path(__file__).resolve()), "--seed", config_dir, home]
    try:
        proc = session_user.run_as_session_user(argv, timeout=_SEED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        return f"the ACP sign-in could not run as {session_user.session_user()} ({type(exc).__name__})"
    try:
        reason = json.loads(proc.stdout)["reason"]
    except Exception:  # noqa: BLE001 — no parseable reply is a broken re-exec, whatever the exit
        reason = None
    if proc.returncode != 0 or not isinstance(reason, str):
        return (f"the ACP sign-in as {session_user.session_user()} exited {proc.returncode}: "
                f"{(proc.stderr or '').strip()[-300:] or '(no stderr)'}")
    return reason


def main(argv: list[str]) -> int:
    """`python3 -I antigravity_acp_auth.py --seed <config_dir> <credential_home>` — the session-user side. Prints
    `{"reason": …}`, the whole protocol; exits 0 whenever it produced one."""
    if len(argv) != 3 or argv[0] != "--seed":
        print("usage: antigravity_acp_auth.py --seed <config_dir> <credential_home>", file=sys.stderr)
        return 2
    print(json.dumps({"reason": seed(argv[1], argv[2])}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
