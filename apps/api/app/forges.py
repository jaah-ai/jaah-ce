"""The forge model: which code-hosting platform a project's repo lives on, and every URL derived from it.

The single home of repo-URL parsing on the control plane. Every caller
that needs a repo's identity — dispatch, the pool tick, the settle `pr_url` check, placement, the hub
check — goes through `repo_ref(project)`; nothing else parses a `repo_url`. The node never parses one
either: it is sent `clone_url`.

A *forge* is a code-hosting platform. Four are supported: GitHub (github.com), GitLab (gitlab.com or a
self-managed install, including one under a sub-path), Bitbucket Cloud (bitbucket.org) and Azure DevOps
Services (dev.azure.com). A known host derives its forge; any other host must be DECLARED a self-managed
GitLab (`projects.forge = 'gitlab'`, O3), or it is refused at save.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping, Optional, get_args

if TYPE_CHECKING:
    from app.models.projects import Project

Forge = Literal["github", "gitlab", "bitbucket", "azure_devops"]
FORGES: tuple[str, ...] = get_args(Forge)
FORGE_LABELS: dict[str, str] = {
    "github": "GitHub", "gitlab": "GitLab", "bitbucket": "Bitbucket", "azure_devops": "Azure DevOps",
}
KNOWN_HOSTS: dict[str, Forge] = {
    "github.com": "github", "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket", "dev.azure.com": "azure_devops",
}
# Hosts that normalise onto a known one. `{org}.visualstudio.com` is handled by pattern (the org moves into
# the path), and `ssh.dev.azure.com`/`vs-ssh.visualstudio.com` carry the ssh v3 path `v3/org/project/repo`.
_HOST_ALIASES: dict[str, str] = {
    "www.github.com": "github.com",
    "ssh.dev.azure.com": "dev.azure.com",
    "vs-ssh.visualstudio.com": "dev.azure.com",
}
_VISUALSTUDIO_HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.visualstudio\.com$")
# `[user@]host:path`, git's scp-like ssh form. The path must not start with `//` (that is a URL).
# A dotted hostname only, so `javascript:x` or a bare word is never read as a host.
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):(?P<path>(?!//).+)$")
_PR_NUMBER = re.compile(r"[0-9]+")

# The sentence every refusal ends with, so a refused operator always learns what IS accepted.
SUPPORTED_FORGES = (
    "Supported forges: GitHub (github.com), GitLab (gitlab.com or self-managed), Bitbucket (bitbucket.org) "
    "and Azure DevOps (dev.azure.com). For a self-managed GitLab, set Forge to \"GitLab (self-managed)\" "
    "on the project form (API: \"forge\": \"gitlab\"), and give its GitLab base URL too when it is "
    "installed under a sub-path (for example https://example.org/gitlab)."
)


class ForgeUrlError(ValueError):
    """A repo URL that names no supported forge's repository. The message is operator-facing: it says what
    was wrong and names the supported forges and how to declare a self-managed GitLab."""

    def __init__(self, problem: str) -> None:
        super().__init__(f"{problem}. {SUPPORTED_FORGES}")
        self.problem = problem


@dataclass(frozen=True)
class RepoRef:
    """One repository on one forge. `root` is the connection key (D-A2, D-A12): `host[/sub-path]`."""

    forge: Forge
    host: str  # lowercase, `host[:port]` only for a non-443 https port
    root: str  # host[/sub-path], lowercase, no scheme
    path: str  # github "o/r" | gitlab "g/sub/.../r" | bitbucket "ws/r" | azure "org/project/r"

    def _azure_parts(self) -> tuple[str, str, str]:
        org, project, repo = self.path.split("/")
        return org, project, repo

    def _web_path(self) -> str:
        """The repo page's path under `https://{root}/`."""
        if self.forge == "azure_devops":
            org, project, repo = self._azure_parts()
            return f"{org}/{project}/_git/{repo}"
        return self.path

    def web_url(self) -> str:
        return f"https://{self.root}/{self._web_path()}"

    def clone_url(self) -> str:
        """The credential-free https remote. Azure's git endpoint takes no `.git` suffix."""
        if self.forge == "azure_devops":
            return self.web_url()
        return f"{self.web_url()}.git"

    def api_base(self) -> str:
        if self.forge == "github":
            return "https://api.github.com"
        if self.forge == "gitlab":
            return f"https://{self.root}/api/v4"
        if self.forge == "bitbucket":
            return "https://api.bitbucket.org/2.0"
        org, project, _repo = self._azure_parts()
        return f"https://dev.azure.com/{org}/{project}/_apis"

    def pr_number(self, url: str) -> Optional[int]:
        """The PR/MR number when `url` names a pull/merge request under THIS repo on THIS host, else None.

        The parsed hostname (and port) is compared exactly — `www.` only for GitHub — never a substring,
        so `github.com.evil.example/o/r/pull/1` is refused; the path must sit under this repo, so another
        repo's PR is refused too."""
        raw = (url or "").strip()
        # A browser reads `\` as `/`, so `https://evil.example\@github.com/…` opens evil.example while
        # `urlsplit` sees github.com; and a PR link never carries userinfo.
        if "\\" in raw:
            return None
        try:
            parts = urllib.parse.urlsplit(raw)
            port = parts.port
        except ValueError:
            return None
        if parts.scheme.lower() != "https" or "@" in parts.netloc:
            return None
        # `…/pull/1/../../other/x/pull/2` passes a prefix test but opens another repo's PR.
        if any(seg in (".", "..") for seg in urllib.parse.unquote(parts.path).split("/")):
            return None
        host = (parts.hostname or "").lower()
        if port is not None and port != 443:
            host = f"{host}:{port}"
        accepted = {self.host, "www.github.com"} if self.forge == "github" else {self.host}
        if host not in accepted:
            return None
        sub_path = self.root[len(self.host):].strip("/")
        prefix = "/".join(p for p in (sub_path, self._web_path()) if p)
        marker = {
            "github": "pull", "gitlab": "-/merge_requests", "bitbucket": "pull-requests",
            "azure_devops": "pullrequest",
        }[self.forge]
        want = _norm(f"{prefix}/{marker}") + "/"
        got = _norm(parts.path.strip("/")) + "/"
        if not got.startswith(want):
            return None
        number = got[len(want):].split("/", 1)[0]
        # ASCII only: `str.isdigit` admits `²` (which `int` then refuses) and other scripts' digits.
        if not _PR_NUMBER.fullmatch(number) or int(number) < 1:
            return None
        return int(number)


def _norm(path: str) -> str:
    """Case-folded, percent-decoded: every forge routes repo paths case-insensitively, and Azure project
    names travel percent-encoded."""
    return urllib.parse.unquote(path).lower()


def _split(url: str) -> tuple[str, Optional[int], list[str]]:
    """`(hostname, https port or None, path segments)` for an https, ssh or scp-like URL. `.git` and
    empty segments are dropped. The ssh port is not a web port, so it is discarded."""
    raw = url.strip()
    scp = _SCP_LIKE.match(raw) if "://" not in raw else None
    if scp:
        host, port, path = scp.group("host"), None, scp.group("path")
    else:
        try:
            parts = urllib.parse.urlsplit(raw)
            parsed_port = parts.port
        except ValueError:
            raise ForgeUrlError(f"{url!r} is not a repository URL") from None
        scheme = parts.scheme.lower()
        if scheme not in ("https", "http", "ssh", "git+ssh") or not parts.hostname:
            raise ForgeUrlError(
                f"{url!r} is not a repository URL — use its https (https://host/path) or ssh "
                "(git@host:path) address"
            )
        host, path = parts.hostname, parts.path
        port = parsed_port if scheme in ("https", "http") and parsed_port not in (None, 443, 80) else None
    segments = [s for s in path.split("/") if s]
    if segments and segments[-1].lower().endswith(".git"):
        segments[-1] = segments[-1][:-4]
        if not segments[-1]:
            segments.pop()
    return host.lower().rstrip("."), port, segments


def _known_path(forge: Forge, host_in: str, segments: list[str], url: str) -> str:
    """The canonical repo path on a known forge; refuses a URL that names no repository."""
    label = FORGE_LABELS[forge]
    if forge in ("github", "bitbucket"):
        # Extra segments (`/tree/main`, `/src/main`) are a page under the repo, not part of its identity.
        if len(segments) >= 2:
            return f"{segments[0]}/{segments[1]}"
        owner = "workspace" if forge == "bitbucket" else "owner"
        raise ForgeUrlError(
            f"{url!r} does not name a {label} repository — expected https://{host_in}/<{owner}>/<repo>"
        )
    if forge == "gitlab":
        return _gitlab_path(segments, url, "gitlab.com")
    # Azure DevOps: https `org/project/_git/repo`, ssh v3 `v3/org/project/repo`.
    if segments[:1] == ["v3"] and len(segments) >= 4:
        return "/".join(segments[1:4])
    if len(segments) >= 4 and segments[2] == "_git":
        return f"{segments[0]}/{segments[1]}/{segments[3]}"
    raise ForgeUrlError(
        f"{url!r} does not name an Azure DevOps repository — expected "
        "https://dev.azure.com/<organization>/<project>/_git/<repo>"
    )


def _gitlab_path(segments: list[str], url: str, host: str) -> str:
    """`group[/subgroup...]/repo`: everything before GitLab's `/-/` page separator, at least two segments."""
    if "-" in segments:
        segments = segments[: segments.index("-")]
    if len(segments) < 2:
        raise ForgeUrlError(
            f"{url!r} does not name a GitLab repository — expected https://{host}/<group>[/<subgroup>…]/<repo>"
        )
    return "/".join(segments)


def _normalised_host(host: str, segments: list[str]) -> tuple[str, list[str]]:
    """Fold an alias onto its known host: `{org}.visualstudio.com/project/_git/r` becomes
    `dev.azure.com/org/project/_git/r` (and its `DefaultCollection` path segment is dropped)."""
    if host in _HOST_ALIASES:
        return _HOST_ALIASES[host], segments
    match = _VISUALSTUDIO_HOST.match(host)
    if match:
        rest = segments[1:] if segments[:1] and segments[0].lower() == "defaultcollection" else segments
        return "dev.azure.com", [match.group(1), *rest]
    return host, segments


def parse_repo_url(url: str, declared: Optional[str], base_url: Optional[str] = None) -> RepoRef:
    """Parse a repo URL (https, or ssh as `git@host:path` / `ssh://`) into its `RepoRef`.

    Known host → that forge, and `declared` must be None or equal it. Unknown host → `declared` must be
    `'gitlab'` (self-managed, O3); ssh is normalised to the https `clone_url`. `base_url` (self-managed
    only) splits root from path for a sub-path install; it defaults to `https://{host}`. Anything else
    raises `ForgeUrlError`."""
    if not isinstance(url, str) or not url.strip():
        raise ForgeUrlError("the project has no repo URL")
    if declared is not None and declared not in FORGES:
        raise ForgeUrlError(f"{declared!r} is not a forge — use one of {', '.join(FORGES)}")
    host, port, segments = _split(url)
    host, segments = _normalised_host(host, segments)
    known = KNOWN_HOSTS.get(host) if port is None else None
    if known is not None:
        if declared is not None and declared != known:
            raise ForgeUrlError(
                f"{url!r} is on {host}, which is {FORGE_LABELS[known]}, not {FORGE_LABELS[declared]} — "
                "clear the forge choice, or fix the URL"
            )
        if base_url:
            raise ForgeUrlError(
                f"a GitLab base URL applies only to a self-managed GitLab, and {host} is "
                f"{FORGE_LABELS[known]} — clear it"
            )
        return RepoRef(forge=known, host=host, root=host, path=_known_path(known, host, segments, url))

    web_host = f"{host}:{port}" if port is not None else host
    if declared != "gitlab":
        if declared is None:
            problem = f"{web_host} is not a known forge host, so {url!r} cannot be saved as it is"
        else:
            problem = (
                f"{FORGE_LABELS[declared]} can only be hosted at {_known_host_of(declared)}, and {url!r} "
                f"is on {web_host}; only GitLab can be self-managed on another host"
            )
        raise ForgeUrlError(problem)

    sub_path = ""
    if base_url:
        sub_path = _base_url_sub_path(base_url, web_host)
        prefix = [s.lower() for s in sub_path.split("/") if s]
        if [s.lower() for s in segments[: len(prefix)]] != prefix:
            raise ForgeUrlError(
                f"{url!r} is not under the GitLab base URL {base_url!r} — the repo URL must start with it"
            )
        segments = segments[len(prefix):]
    path = _gitlab_path(segments, url, f"{web_host}/{sub_path}".rstrip("/"))
    root = f"{web_host}/{sub_path}".rstrip("/").lower()
    return RepoRef(forge="gitlab", host=web_host, root=root, path=path)


def _known_host_of(forge: str) -> str:
    return next(host for host, known in KNOWN_HOSTS.items() if known == forge)


def _base_url_sub_path(base_url: str, web_host: str) -> str:
    """The sub-path of a self-managed GitLab's https base URL, which must be on the repo's own host."""
    try:
        parts = urllib.parse.urlsplit(base_url.strip())
        port = parts.port
    except ValueError:
        raise ForgeUrlError(f"the GitLab base URL {base_url!r} is not a URL") from None
    host = (parts.hostname or "").lower()
    if port is not None and port != 443:
        host = f"{host}:{port}"
    if parts.scheme.lower() != "https" or not parts.hostname or parts.query or parts.fragment:
        raise ForgeUrlError(
            f"the GitLab base URL {base_url!r} must be an https address such as https://example.org/gitlab"
        )
    if host != web_host:
        raise ForgeUrlError(
            f"the GitLab base URL {base_url!r} is on {host}, but the repo URL is on {web_host} — "
            "they must be the same host"
        )
    return parts.path.strip("/")


def repo_ref(project: "Project") -> RepoRef:
    """The project's repo: `parse_repo_url(project.repo_url, project.forge, project.forge_base_url)`."""
    return parse_repo_url(project.repo_url or "", project.forge, project.forge_base_url)


def repo_ref_or_none(project: Optional["Project"]) -> Optional[RepoRef]:
    """`repo_ref`, or None for a missing project or one whose repo URL does not parse. For the callers
    whose own refusal or skip already names the problem."""
    if project is None:
        return None
    try:
        return repo_ref(project)
    except ForgeUrlError:
        return None


def github_repo_path(repo_url: Optional[str]) -> Optional[str]:
    """`owner/repo` when `repo_url` is a github.com repository, else None. For the GitHub-only surfaces
    (docs browser, GHA runners, the Git tab's org reach), which keep their GitHub behaviour."""
    try:
        ref = parse_repo_url(repo_url or "", None)
    except ForgeUrlError:
        return None
    return ref.path if ref.forge == "github" else None


# ── Connections: which forge root a git credential authenticates (architecture §2.4, D-A2, D-A12) ─────

#: The roots every user's Git Access lists whether or not anything is connected, in display order. A
#: self-managed GitLab root is listed only once connected.
FIXED_ROOTS: tuple[str, ...] = ("github.com", "gitlab.com", "bitbucket.org", "dev.azure.com")
# A self-managed GitLab root: `host[:port][/sub/path]`, lowercase, no scheme, no trailing slash.
_ROOT = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?(/[a-z0-9._~-]+)*$")


def forge_of_root(root: str) -> Optional[Forge]:
    """The forge a connection root belongs to: a known host's own forge, any other well-formed root a
    self-managed GitLab (the only forge that can be self-managed, O3), or None for a malformed root."""
    if root in KNOWN_HOSTS:
        return KNOWN_HOSTS[root]
    if not _ROOT.fullmatch(root or "") or root.split("/", 1)[0].split(":", 1)[0] in KNOWN_HOSTS:
        return None
    return "gitlab"


def credential_root(credential_type: str, base_url: Optional[str]) -> Optional[str]:
    """The root a git credential of `credential_type` authenticates, or None for a non-git type.

    GitHub is always `github.com` (GitHub Enterprise is not a supported forge; the migration's backfill
    reads the same rule). GitLab is `gitlab.com` unless its `base_url` names another instance, whose root
    is `host[:port][/sub-path]` exactly as `parse_repo_url` spells a self-managed repo's root. Bitbucket
    Cloud and Azure DevOps Services each have one host."""
    if credential_type == "github":
        return "github.com"
    if credential_type == "bitbucket":
        return "bitbucket.org"
    if credential_type == "azure_devops":
        return "dev.azure.com"
    if credential_type != "gitlab":
        return None
    raw = (base_url or "").strip()
    if not raw:
        return "gitlab.com"
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return None
    if port is not None and port != 443:
        host = f"{host}:{port}"
    return f"{host}/{parts.path.strip('/')}".rstrip("/").lower()


def revoked(forge: str, status: int, body: bytes, headers: Mapping[str, str]) -> bool:
    """Whether a forge's answer to a connection test means the token is dead for good (D-A14).

    The single home of that decision, and deliberately narrow: a 401 on every forge, plus GitLab's
    `invalid_token` (its OAuth answer for a revoked or expired grant). A 403 (scope, SSO, IP allow-list),
    a 429 or any rate-limit answer is NOT a revocation — it is read like a network error and the token is
    still served, because marking a working grant dead costs the person a reconnect."""
    if status == 401:
        return True
    if status == 429 or "retry-after" in {key.lower() for key in headers}:
        return False
    if forge == "gitlab" and status != 403:
        challenge = " ".join(value for key, value in headers.items() if key.lower() == "www-authenticate")
        return "invalid_token" in challenge or b'"invalid_token"' in body
    return False
