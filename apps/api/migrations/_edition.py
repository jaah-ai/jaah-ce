"""Edition reads for migrations: what a revision may create depends on the edition (`app/edition.py`).

A function, read at call time, so a test can monkeypatch `edition.IS_CE`. The community edition runs on plain
Postgres (no pgvector), so a revision guards every vector column and index behind `vector()`.
"""

from app import edition


def vector() -> bool:
    """True when this edition's database carries pgvector (the cloud); False on the community edition."""
    return not edition.IS_CE


def ce() -> bool:
    """True on the community edition: a revision creates its CE-only objects (the caps' triggers) behind this."""
    return edition.IS_CE
