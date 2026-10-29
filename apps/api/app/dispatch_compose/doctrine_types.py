"""The doctrine bundle's shape: what each edition's doctrine module supplies (`dispatch_compose/doctrine.py`)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class DoctrineBundle:
    """Every piece of role prose the composer splices, by the name its readers use.

    Both editions' bundles carry the same keys at every level (`tests/test_doctrine_bundle.py`); a value may be
    empty where an edition withholds that section."""

    #: charter key -> role row (`role_render.render_role_section_with` reads its keys), in role-table order.
    roles: Mapping[str, Mapping[str, str]]
    #: Prose constants `role_render`, `system_prompt` and the playbook sandbox's contracts splice, by name.
    shared: Mapping[str, str]
    #: (authored fragment, twin) tables the composer applies per slot shape, delivery and merge lane, by name.
    rewrites: Mapping[str, tuple[tuple[str, str], ...]]
    #: The autonomy directives `directives._autonomous_directive` picks from, by name.
    directives: Mapping[str, str]
