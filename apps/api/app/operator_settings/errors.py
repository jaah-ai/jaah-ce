"""The two refusals the operator-settings resolver and writers raise."""
from __future__ import annotations


class OperatorSettingsScopeError(RuntimeError):
    """An org-tier knob was read with no org while some org has a value for it (`_refuse_unscoped_read`).

    A `RuntimeError`, not an `OperatorSettingsError`: this is a caller defect, and sharing the base would
    let a route's `except OperatorSettingsError` turn it into a misleading 422."""


class OperatorSettingsError(ValueError):
    """Raised by `apply_overrides` on an unknown key, a wrong-typed value, or an out-of-range value.
    The route answers 422 with `str(exc)`; every knob is operational (never a secret), so echoing the key
    and range is safe."""
