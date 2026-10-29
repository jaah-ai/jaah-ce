#!/usr/bin/env python3
"""github_settings.py — the repository's GitHub settings as code: export, diff, apply.

The DECLARED state is single-homed in `DECLARED_DIR` (its README is the runbook): one ruleset body per
file under `rulesets/`, and `repo.json` for the repository settings, the Actions workflow-token settings,
the repository's OIDC `sub` customisation and the `drift_enforced` flag. This tool compares that against the LIVE repository.

  github_settings.py diff   [--live-fixture F] [--ci]   what apply would change; exit 1 on drift
  github_settings.py export [--live-fixture F]          live settings, scrubbed, into the evidence store
  github_settings.py apply  [--yes]                     make the live repo match (owner's admin token)

`--ci` makes `diff` honour `drift_enforced`: while it is false the drift is printed and the exit is 0. `apply` without `--yes` prints the calls and makes none.
Rulesets are matched by NAME; a live ruleset no file declares is deleted, and only after every declared
one is created or updated, so the branches are never left unprotected in between.

A section the token cannot read (the Actions settings need admin; a workflow token gets 403) is reported
as unreadable, never as drift. Credentials: `GH_TOKEN` or `GITHUB_TOKEN` from the environment — the
token is sent only in the Authorization header and never written anywhere. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
DECLARED_DIR = ROOT / "infra" / "github"

UNREADABLE = "unreadable"

#: Fields GitHub adds to every ruleset response that are bookkeeping, not settings.
_VOLATILE = frozenset({"_links", "node_id", "created_at", "updated_at", "current_user_can_bypass", "id",
                       "source", "source_type"})
#: A token-shaped string anywhere in a response is redacted on export, whatever key carries it.
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")
_SECRET_KEY = re.compile(r"token|secret|password|private_key", re.I)

Fetch = Callable[[str, str, Optional[dict]], Any]


class Unreadable(Exception):
    """The token may not read this endpoint (401/403/404)."""


# ── declared state ─────────────────────────────────────────────────────────────────────────────────


def load_declared(root: Path = DECLARED_DIR) -> dict:
    repo = json.loads((root / "repo.json").read_text())
    rulesets = [json.loads(p.read_text()) for p in sorted((root / "rulesets").glob("*.json"))]
    return {
        "repo": repo["repo"],
        "drift_enforced": bool(repo["drift_enforced"]),
        "repository": repo["repository"],
        "actions_workflow_permissions": repo["actions_workflow_permissions"],
        "oidc_sub": repo["oidc_sub"],
        "rulesets": rulesets,
    }


# ── live state ─────────────────────────────────────────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib keeps the Authorization header across a redirect to another host; refuse every redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(newurl, code, f"refused redirect to {newurl}", headers, fp)


def http_fetch(token: str, api: str = "https://api.github.com") -> Fetch:
    if not api.startswith("https://"):
        raise SystemExit(f"github_settings: refusing to send a token to non-https {api!r}")
    opener = urllib.request.build_opener(_NoRedirect)

    def fetch(method: str, path: str, body: Optional[dict] = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{api}/{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        })
        try:
            with opener.open(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404) and method == "GET":
                raise Unreadable(f"HTTP {exc.code}") from None
            raise SystemExit(f"github_settings: {method} {path}: HTTP {exc.code}: {exc.read()[:300]!r}")
        return json.loads(raw) if raw else None
    return fetch


def _ruleset(fetch: Fetch, repo: str, summary: dict) -> dict:
    try:
        return fetch("GET", f"repos/{repo}/rulesets/{summary['id']}", None)
    except Unreadable as exc:
        raise SystemExit(f"github_settings: cannot read ruleset {summary.get('name')!r} ({exc})") from None


def pick(got: dict, keys: Optional[list]) -> dict:
    """The `keys` of a read section — or unreadable when none came back: GitHub omits the repository's merge
    settings for a token without admin (a workflow token), which is not a setting of `null`."""
    if not keys:
        return got
    if all(got.get(k) is None for k in keys):
        return {UNREADABLE: "fields not visible to this token"}
    return {k: got.get(k) for k in keys}


def read_live(fetch: Fetch, repo: str) -> dict:
    def section(path: str, keys: Optional[list] = None) -> Any:
        try:
            got = fetch("GET", path, None)
        except Unreadable as exc:
            return {UNREADABLE: str(exc)}
        return pick(got, keys)

    declared_repo_keys = list(load_declared()["repository"])
    try:
        summaries = fetch("GET", f"repos/{repo}/rulesets?includes_parents=false&per_page=100", None)
    except Unreadable as exc:
        raise SystemExit(f"github_settings: cannot list {repo}'s rulesets ({exc})") from None
    return {
        "repo": repo,
        "rulesets": [_ruleset(fetch, repo, s) for s in summaries],
        "repository": section(f"repos/{repo}", declared_repo_keys),
        "actions_workflow_permissions": section(f"repos/{repo}/actions/permissions/workflow"),
        "oidc_sub": section(f"repos/{repo}/actions/oidc/customization/sub"),
    }


# ── diff ───────────────────────────────────────────────────────────────────────────────────────────


def _canon(value: Any) -> Any:
    """Order-insensitive where GitHub's order carries no meaning: required checks, actors, rules."""
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        key = lambda v: json.dumps(v, sort_keys=True)  # noqa: E731
        return sorted((_canon(v) for v in value), key=key)
    if isinstance(value, dict):
        return {k: _canon(v) for k, v in value.items()}
    return value


def _subset(path: str, want: Any, have: Any, out: list) -> None:
    """Every declared key must hold its declared value; keys GitHub adds that we do not declare are left
    alone, so a new default on GitHub's side is not drift."""
    if isinstance(want, dict) and isinstance(have, dict):
        for k, v in want.items():
            _subset(f"{path}.{k}", v, have.get(k), out)
    elif _canon(want) != _canon(have):
        out.append(f"~ {path}: {json.dumps(_canon(have))} -> {json.dumps(_canon(want))}")


def _ruleset_changes(want: dict, have: dict) -> list:
    name, out = want["name"], []
    for key in ("name", "target", "enforcement", "conditions"):
        _subset(f"ruleset {name} {key}", want[key], have.get(key), out)
    if "bypass_actors" in have:  # absent when the token may not see it: diff() reports that as unreadable
        _subset(f"ruleset {name} bypass_actors", want["bypass_actors"], have["bypass_actors"], out)
    live_rules = {r["type"]: r for r in have.get("rules", [])}
    for rule in want["rules"]:
        live = live_rules.pop(rule["type"], None)
        if live is None:
            out.append(f"+ ruleset {name} rule {rule['type']}")
            _subset(f"ruleset {name} rule {rule['type']}", rule.get("parameters", {}), {}, out)
        else:
            _subset(f"ruleset {name} rule {rule['type']}", rule.get("parameters", {}),
                    live.get("parameters", {}), out)
    out.extend(f"- ruleset {name} rule {t}" for t in sorted(live_rules))
    return out


def _match(want: dict, live_by_name: dict) -> Optional[dict]:
    """The live ruleset `want` governs: its own name."""
    return live_by_name.pop(want["name"], None)


def _body(want: dict) -> dict:
    """What GitHub is sent: the declared ruleset minus this tool's own `_`-prefixed keys."""
    return {k: v for k, v in want.items() if not k.startswith("_")}


def diff(declared: dict, live: dict) -> tuple[list, list]:
    """(changes, unreadable): every change apply would make, and the sections the token could not read."""
    changes, unreadable = [], []
    live_by_name = {r["name"]: r for r in live["rulesets"]}
    for want in declared["rulesets"]:
        have = _match(want, live_by_name)
        if have is None:
            changes.append(f"+ ruleset {want['name']} (create)")
            changes.extend(_ruleset_changes(want, {"bypass_actors": None}))
        else:
            changes.extend(_ruleset_changes(want, have))
            if "bypass_actors" not in have:
                unreadable.append(f"? ruleset {want['name']} bypass_actors: not visible to this token")
    changes.extend(f"- ruleset {name} (delete: no file declares it)" for name in sorted(live_by_name))
    for key in ("repository", "actions_workflow_permissions", "oidc_sub"):
        if key == "oidc_sub" and key not in declared:  # the api's evidence read does not compare the OIDC
            continue  # customisation (app/compliance_evidence/github.py); any other missing section is a KeyError
        have = live.get(key) or {}
        if UNREADABLE in have:
            unreadable.append(f"? {key}: {have[UNREADABLE]}")
            continue
        _subset(key, declared[key], have, changes)
    return changes, unreadable


# ── apply ──────────────────────────────────────────────────────────────────────────────────────────


def plan_apply(declared: dict, live: dict) -> list:
    """The (method, path, body) calls that make `live` match `declared` — every create before any update
    (an update may narrow a live ruleset a create is about to take over, so a failed create must not
    leave a branch uncovered), every update before any delete, and nothing for a section in step."""
    repo, creates, updates = declared["repo"], [], []
    live_by_name = {r["name"]: r for r in live["rulesets"]}
    for want in declared["rulesets"]:
        have = _match(want, live_by_name)
        if have is None:
            creates.append(("POST", f"repos/{repo}/rulesets", _body(want)))
        elif _ruleset_changes(want, have):
            updates.append(("PUT", f"repos/{repo}/rulesets/{have['id']}", _body(want)))
    calls = creates + updates
    for have in live_by_name.values():
        calls.append(("DELETE", f"repos/{repo}/rulesets/{have['id']}", None))
    for key, method, path in (("repository", "PATCH", f"repos/{repo}"),
                              ("actions_workflow_permissions", "PUT", f"repos/{repo}/actions/permissions/workflow"),
                              ("oidc_sub", "PUT", f"repos/{repo}/actions/oidc/customization/sub")):
        out: list = []
        have = live.get(key) or {}
        if UNREADABLE in have:
            raise SystemExit(f"github_settings: cannot read {key} ({have[UNREADABLE]}); apply needs an admin token")
        _subset(key, declared[key], have, out)
        if out:
            calls.append((method, path, declared[key]))
    return calls


# ── export ─────────────────────────────────────────────────────────────────────────────────────────


def scrub(value: Any) -> Any:
    """Evidence-safe copy: GitHub bookkeeping dropped, anything secret-shaped redacted."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if _SECRET_KEY.search(k) and isinstance(v, str) else scrub(v))
                for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _TOKEN.sub("<redacted>", value)
    return value


def export_body(declared: dict, live: dict, source: str) -> bytes:
    """`source` says where `live` came from (the API, or a fixture file) so a recording is never filed as
    a live read."""
    changes, unreadable = diff(declared, live)
    return (json.dumps({"source": source, "live": scrub(live), "drift_from_declared": scrub(changes),
                        "unreadable": unreadable}, indent=2, sort_keys=True) + "\n").encode()


# ── CLI ────────────────────────────────────────────────────────────────────────────────────────────


def _token_fetch() -> Fetch:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("github_settings: set GH_TOKEN (or GITHUB_TOKEN)")
    return http_fetch(token, os.environ.get("GITHUB_API_URL", "https://api.github.com"))


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("verb", choices=("diff", "export", "apply"))
    ap.add_argument("--live-fixture", type=Path, help="read the live state from this JSON, not the API")
    ap.add_argument("--ci", action="store_true", help="diff: exit 0 on drift unless drift_enforced")
    ap.add_argument("--yes", action="store_true", help="apply: actually make the calls")
    args = ap.parse_args(argv)

    declared = load_declared()
    if args.live_fixture:
        if args.verb == "apply":
            raise SystemExit("github_settings: apply reads the live repo, never a fixture")
        live, source = json.loads(args.live_fixture.read_text()), f"fixture:{args.live_fixture.name}"
    else:
        fetch = _token_fetch()
        live, source = read_live(fetch, declared["repo"]), "api"

    if args.verb == "export":
        # Imported here, not at the top: `apply` runs under the owner's admin token and loads nothing but
        # this code-owned file (CODEOWNERS owns evidence_store.py too, for the export path).
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import evidence_store

        name = declared["repo"].split("/")[-1]
        body = export_body(declared, live, source)
        print(evidence_store.write("github-settings", f"{name}-settings.json", body))
        return 0

    if args.verb == "diff":
        changes, unreadable = diff(declared, live)
        for line in changes + unreadable:
            print(line)
        if not changes:
            print("github settings: in step with infra/github/")
            return 0
        if args.ci and not declared["drift_enforced"]:
            print(f"github settings: {len(changes)} drift(s) — ADVISORY until infra/github/repo.json "
                  "sets drift_enforced")
            return 0
        return 1

    calls = plan_apply(declared, live)
    for method, path, _ in calls:
        print(f"{method} {path}")
    if not calls:
        print("github settings: nothing to apply")
        return 0
    if not args.yes:
        print("github settings: dry run — re-run with --yes to make these calls")
        return 0
    for method, path, body in calls:
        fetch(method, path, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
