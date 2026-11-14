"""The ledger's derived dimensions — the single home of each: model family, review tier, language and the
complexity bucket (Architecture §6)."""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

from app import model_catalog
from app.model_prices import canonical_model, MODEL_FAMILIES
from app.models.tasks import REVIEW_TIERS as TASK_REVIEW_TIERS

_VENDOR_SPLIT = re.compile(r"[-/:.@_\[]")


def model_family(model: Optional[str]) -> Optional[str]:
    """The family a concrete model id belongs to: a tier, or an id of a catalogue family, folds onto that family
    (`model_prices.MODEL_FAMILIES`); any other vendor's id is `vendor:<first id segment>`; None for no model."""
    raw = (model or "").strip()
    if not raw:
        return None
    tier = model_catalog.tier_of(raw)
    if tier:
        return model_catalog.TIER_FAMILY.get(tier)
    family = canonical_model(raw).split("@", 1)[0]
    if family in MODEL_FAMILIES:
        return family
    head = _VENDOR_SPLIT.split(raw.lower(), 1)[0]
    return f"vendor:{head or raw.lower()}"


#: The code-review policy's tiers, each kept as its own stratum: derived from the column's set, never restated, so a
#: tier a runner may declare can never fold to `unknown`. `trivial` in particular is kept, never mapped: it is the most
#: common small-change tier, and folding it would blank the Outcome panel's Review chip and lump trivial changes in
#: with undeclared ones.
REVIEW_TIERS: tuple[str, ...] = TASK_REVIEW_TIERS
REVIEW_TIER_UNKNOWN = "unknown"
#: The longest value a settle may pass; longer is read as `unknown`, never a 422.
REVIEW_TIER_MAX_LEN = 16


def normalise_review_tier(value: object) -> str:
    """A runner's declared review tier, folded onto the closed set. Lenient by rule: anything unreadable is
    `unknown`, never a refusal (the `spec_verdict` leniency)."""
    if not isinstance(value, str):
        return REVIEW_TIER_UNKNOWN
    folded = value.strip().lower()
    if len(folded) > REVIEW_TIER_MAX_LEN:
        return REVIEW_TIER_UNKNOWN
    return folded if folded in REVIEW_TIERS else REVIEW_TIER_UNKNOWN


#: File extension → language, for the most-changed file. A filter on the read side, never a stratum key.
LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".fs": "fsharp", ".swift": "swift", ".m": "objective-c",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "powershell",
    ".sql": "sql", ".tf": "terraform", ".hcl": "terraform",
    ".md": "markdown", ".mdx": "markdown", ".rst": "text", ".txt": "text",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".vue": "vue", ".svelte": "svelte", ".dart": "dart", ".lua": "lua", ".r": "r", ".jl": "julia",
    ".ex": "elixir", ".exs": "elixir", ".erl": "erlang", ".hs": "haskell", ".clj": "clojure",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml", ".xml": "xml",
}


def language_of(paths: Iterable[str]) -> Optional[str]:
    """The language most of `paths` are in (ties: first seen), or None when none maps."""
    counts: dict[str, int] = {}
    for path in paths:
        language = LANGUAGE_BY_EXTENSION.get(os.path.splitext(str(path))[1].lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda language: counts[language])


BUCKETS: tuple[str, ...] = ("XS", "S", "M", "L")
BUCKET_NA = "na"
#: `(bucket, max size, max files)` — the first that holds both wins; anything larger is `L`.
_EDGES: tuple[tuple[str, int, int], ...] = (("XS", 20, 2), ("S", 100, 5), ("M", 500, 20))
#: Below this clarity the work was harder than its diff: the bucket moves up one class (capped at L).
CLARITY_BUMP_BELOW = 0.7


def clarity(score: Optional[float]) -> Optional[float]:
    """The task's clarity-gate score as the ledger records it: on the gate's 0–1 scale, else NULL (a score written on
    another scale is not a fact this column can hold — `task_outcomes.clarity_score` is NUMERIC(4,3))."""
    if score is None or not 0 <= float(score) <= 1:
        return None
    return float(score)


def complexity_bucket(size: Optional[int], files: Optional[int], clarity: Optional[float] = None) -> str:
    """`XS`/`S`/`M`/`L` from the diff size (additions + deletions, generated files excluded) and files changed,
    bumped one class when `clarity < 0.7`; `na` when the diff is unknown."""
    if size is None or files is None:
        return BUCKET_NA
    bucket = next((name for name, max_size, max_files in _EDGES if size <= max_size and files <= max_files), "L")
    if clarity is not None and float(clarity) < CLARITY_BUMP_BELOW:
        bucket = BUCKETS[min(BUCKETS.index(bucket) + 1, len(BUCKETS) - 1)]
    return bucket
