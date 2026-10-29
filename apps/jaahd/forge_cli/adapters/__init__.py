"""One adapter per forge, all REST against the login's `api_base`. `for_login` is the only selector."""
from __future__ import annotations

import importlib
from typing import Callable

from ..login import Login
from ..transport import Client, Transport
from .base import Adapter

_ADAPTERS = {"github": ("github", "GitHub"), "gitlab": ("gitlab", "GitLab"),
             "bitbucket": ("bitbucket", "Bitbucket"), "azure_devops": ("azure", "Azure")}


def for_login(login: Login, transport: Transport, refetch: "Callable[[], Login] | None" = None,
              on_rejected: "Callable[[], None] | None" = None) -> Adapter:
    """`refetch`: the same login read past the daemon's cache, asked once on a 401 (`Client._reauth`). Its
    header is used only when it is for the same `api_base`, so a fresh read never moves the client.
    `on_rejected`: told once when a 401 stands after that fresh read (`Client._report_rejected`)."""
    module, name = _ADAPTERS[login.forge]
    cls = getattr(importlib.import_module(f"{__name__}.{module}"), name)

    def reauth() -> str:
        fresh = refetch()
        return fresh.auth_header() if fresh.api_base == login.api_base else ""

    return cls(login, Client(login.api_base, login.auth_header(), transport, login.label, login.repo_path,
                             reauth if refetch else None, on_rejected, write_problem=login.write_problem))
