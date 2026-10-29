"""The runner doctrine this edition composes: the one place an edition picks its role prose.

`BUNDLE` (a `doctrine_types.DoctrineBundle`) is the full doctrine where the edition map's `full_doctrine` feature
is on (`doctrine_full.py`), else the community baseline (`doctrine_community.py`). Every composer module reads
role prose through it, never from a prose module directly."""
from __future__ import annotations

from app import edition_features

if edition_features.enabled("full_doctrine"):
    from app.dispatch_compose.doctrine_full import BUNDLE
else:
    from app.dispatch_compose.doctrine_community import BUNDLE  # type: ignore[no-redef]  # one name, one edition's bundle

__all__ = ["BUNDLE"]
